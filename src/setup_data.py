#!/usr/bin/env python3
"""GSM8KデータセットのNon-IIDシャード生成スクリプト。

hosts.txtの行数に応じてノード数を決定し、各ノードに極端に偏った
カテゴリ割り当て（Non-IID）を行ったシャードデータを生成する。
出力は data/train/peer_X.json と data/test/peer_X.json に分割される。
"""

import json
import random
import re
import subprocess
import threading
import time
from pathlib import Path

import datasets

from log import dot, fail, info, ok, phase, skip
from utils import get_base_dir, get_hosts_path, _get, _get_int, _get_str

# GSM8K問題のカテゴリ分類用キーワード
CATEGORY_KEYWORDS = {
    "add_sub": [
        r"\b(total|more|less|left|added|sum|increase|decrease|difference)\b",
        r"\bplus\b",
        r"\bminus\b",
    ],
    "mul_div": [
        r"\b(times|product|multipl|each\b.*\b\d+|split|divide|per\b.*\bhour|rate)\b",
        r"\bx\b[^a-z\d]\d+",
    ],
    "percentage": [
        r"\b(\d+)%\b",
        r"\b(percent|discount|interest|tax)\b",
    ],
    "average": [
        r"\b(average|mean|median|per\s+capita)\b",
    ],
    "mixed": [],
}


def classify_problem(text: str) -> str:
    """問題テキストをカテゴリに分類する。

    キーワードマッチに基づき、最も関連性の高いカテゴリを返す。
    複数のカテゴリに該当する場合は、一致数の多い方を選択。
    """
    scores: dict[str, int] = {}
    for cat, patterns in CATEGORY_KEYWORDS.items():
        if cat == "mixed":
            continue
        scores[cat] = sum(
            1 for pattern in patterns if re.search(pattern, text, re.IGNORECASE)
        )

    if not scores or max(scores.values()) == 0:
        return "mixed"

    return max(scores, key=scores.get)  # type: ignore[arg-type]


def save_peer_files(
    sharded: dict[int, list[dict]],
    test_sharded: dict[int, list[dict]],
    base_dir: Path,
    train_dir_name: str,
    test_dir_name: str,
) -> None:
    """peerごとのデータを個別ファイルとして保存。"""
    train_path = base_dir / train_dir_name
    test_path = base_dir / test_dir_name
    train_path.mkdir(parents=True, exist_ok=True)
    test_path.mkdir(parents=True, exist_ok=True)

    total_train = 0
    total_test = 0

    for peer_id in sorted(sharded.keys()):
        train_file = train_path / f"peer_{peer_id}.json"
        train_file.write_text(
            json.dumps(
                {"num_samples": len(sharded[peer_id]), "samples": sharded[peer_id]},
                ensure_ascii=False,
                indent=2,
            )
        )
        total_train += len(sharded[peer_id])
        ok(f"train/peer_{peer_id}.json ({len(sharded[peer_id])} samples)")

        test_file = test_path / f"peer_{peer_id}.json"
        test_file.write_text(
            json.dumps(
                {"num_samples": len(test_sharded.get(peer_id, [])), "samples": test_sharded.get(peer_id, [])},
                ensure_ascii=False,
                indent=2,
            )
        )
        total_test += len(test_sharded.get(peer_id, []))
        ok(f"test/peer_{peer_id}.json ({len(test_sharded.get(peer_id, []))} samples)")

    ok(f"Train total: {total_train}, Test total: {total_test}")


def _download_with_progress(cmd: list[str], cache_dir: Path) -> None:
    """hf download を実行中、進行中にドット進捗を表示する。"""
    print(f"\n  Downloading gsm8k dataset to {cache_dir} ...")
    stop = threading.Event()

    def _dots() -> None:
        while not stop.is_set():
            print(".", end="", flush=True)
            time.sleep(3)

    t = threading.Thread(target=_dots, daemon=True)
    t.start()
    try:
        subprocess.run(cmd, check=True)
    finally:
        stop.set()
        t.join(timeout=2)
        print()  # dot 行の改行


def main() -> None:
    """メインエントリポイント。"""
    base_dir = get_base_dir()

    # hosts.txtの読み込み
    hosts = []
    for line in get_hosts_path().read_text().strip().splitlines():
        ip = line.strip()
        if ip and not ip.startswith("#"):
            hosts.append(ip)
    num_peers = len(hosts)
    info(f"{num_peers} peers from hosts.txt")

    # GSM8Kデータセットの読み込み（実行毎に再ダウンロードしないよう、
    # 実験ディレクトリとは独立した固定キャッシュを使う）
    phase("Load GSM8K dataset")
    cache_dir = base_dir / "cache" / "datasets" / "gsm8k"
    cache_dir.mkdir(parents=True, exist_ok=True)
    train_path = cache_dir / "main" / "train-00000-of-00001.parquet"
    if not train_path.exists():
        _download_with_progress(
            [
                "hf", "download", "gsm8k", "--repo-type", "dataset",
                "--include", "main/*", "--local-dir", str(cache_dir),
            ],
            cache_dir,
        )
    dataset = datasets.load_dataset(
        "parquet", data_dir=str(cache_dir / "main"), split="train"
    )
    raw_data = dataset.to_list()

    ok(f"{len(raw_data)} examples loaded")

    # train/test 分割（設定からデフォルト90/10）
    phase("Split train/test")
    validation_split = _get("data", "validation_split", 0.1)
    rng = random.Random(_get_int("data", "seed", 42))
    rng.shuffle(raw_data)
    split_idx = int(len(raw_data) * (1.0 - validation_split))
    train_data = raw_data[:split_idx]
    test_data = raw_data[split_idx:]
    ok(f"Train: {len(train_data)}, Test: {len(test_data)}")

    # 訓練データにカテゴリラベルを付与
    phase("Classify & shard (Non-IID)")
    labeled_train = []
    for item in train_data:
        labeled_train.append({**item, "_category": classify_problem(item["question"])})

    # カテゴリごとのインデックスリスト
    by_category: dict[str, list[int]] = {}
    for idx, item in enumerate(labeled_train):
        cat = item["_category"]
        if cat not in by_category:
            by_category[cat] = []
        by_category[cat].append(idx)

    for cat in by_category:
        rng.shuffle(by_category[cat])

    # peer_idごとに専門カテゴリを割り当て
    categories = list(by_category.keys())
    peer_categories: list[str] = []
    for i in range(num_peers):
        peer_categories.append(categories[i % len(categories)])

    # 訓練データのNon-IIDシャード
    sharded: dict[int, list[dict]] = {i: [] for i in range(num_peers)}
    for cat, indices in by_category.items():
        target_peers = [
            i for i, pc in enumerate(peer_categories) if pc == cat
        ]
        if not target_peers:
            target_peers = [rng.randrange(num_peers)]

        for idx in indices:
            if rng.random() < (0.7 + 0.2 * rng.random()):
                peer_id = rng.choice(target_peers)
            else:
                peer_id = rng.randrange(num_peers)

            item = labeled_train[idx].copy()
            del item["_category"]
            sharded[peer_id].append(item)

    for peer_id in sharded:
        rng.shuffle(sharded[peer_id])

    # 均等化リバランス（disjoint 維持）。カテゴリサイズの偏りにより peer サイズが
    # 極端に不均衡（実測で 335〜2606）になり、小さい peer は同じ少数データを何度も
    # 周回して過学習しやすい。各ノードの学習データ量を底上げしつつ均等化するため、
    # 全 peer を target（=総数/peer数）へ揃える。超過 peer からは Non-IID 特性の核である
    # specialty サンプルを優先的に残し、非 specialty サンプルを先に抜いてプールへ移し、
    # 不足 peer へ配る（サンプルの重複・欠落は起こさない）。
    target = len(train_data) // num_peers
    pool: list[dict] = []
    for peer_id in range(num_peers):
        shard = sharded[peer_id]
        excess = len(shard) - target
        if excess <= 0:
            continue
        specialty = peer_categories[peer_id]
        specialty_items: list[dict] = []
        non_specialty: list[dict] = []
        for item in shard:
            if classify_problem(item["question"]) == specialty:
                specialty_items.append(item)
            else:
                non_specialty.append(item)
        rng.shuffle(non_specialty)
        rng.shuffle(specialty_items)
        # 非 specialty を先頭に並べ、先頭から excess 個を抜く（specialty を優先保持）
        reordered = non_specialty + specialty_items
        pool.extend(reordered[:excess])
        sharded[peer_id] = reordered[excess:]

    # 不足 peer をプールから補充して target へ底上げ
    rng.shuffle(pool)
    for peer_id in range(num_peers):
        deficit = target - len(sharded[peer_id])
        if deficit <= 0:
            continue
        sharded[peer_id].extend(pool[:deficit])
        pool = pool[deficit:]

    # 端数（target で割り切れない余り）は順に配って全サンプルを使い切る
    while pool:
        sharded[len(pool) % num_peers].append(pool.pop())

    for peer_id in sharded:
        rng.shuffle(sharded[peer_id])

    # カテゴリ分布の統計を出力
    ok("Non-IID shard assignment done (rebalanced)")
    for peer_id in sorted(sharded.keys()):
        cat_counts: dict[str, int] = {}
        for item in sharded[peer_id]:
            cat = classify_problem(item["question"])
            cat_counts[cat] = cat_counts.get(cat, 0) + 1
        info(
            f"Peer {peer_id} (specialty={peer_categories[peer_id]}): "
            f"total={len(sharded[peer_id])}, "
            f"distribution={cat_counts}"
        )

    # テストデータをpeerごとに均等分配（ランダム）
    test_sharded: dict[int, list[dict]] = {i: [] for i in range(num_peers)}
    for idx, item in enumerate(test_data):
        peer_id = idx % num_peers
        test_sharded[peer_id].append(item)
    for peer_id in test_sharded:
        rng.shuffle(test_sharded[peer_id])

    # peerごとのファイルとして保存（プロジェクトルート直下の data/ へ）
    phase("Save peer files")
    save_peer_files(
        sharded, test_sharded, base_dir,
        train_dir_name="data/train",
        test_dir_name="data/test",
    )


if __name__ == "__main__":
    main()
