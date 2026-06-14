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
from datetime import datetime, timezone
from pathlib import Path

import datasets

from utils import get_base_dir, get_experiment_dir, get_hosts_path, _get, _get_int, _get_str

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
        print(f"  Saved train/peer_{peer_id}.json ({len(sharded[peer_id])} samples)")

        test_file = test_path / f"peer_{peer_id}.json"
        test_file.write_text(
            json.dumps(
                {"num_samples": len(test_sharded.get(peer_id, [])), "samples": test_sharded.get(peer_id, [])},
                ensure_ascii=False,
                indent=2,
            )
        )
        total_test += len(test_sharded.get(peer_id, []))
        print(f"  Saved test/peer_{peer_id}.json ({len(test_sharded.get(peer_id, []))} samples)")

    print(f"Train total: {total_train}, Test total: {total_test}")


def main() -> None:
    """メインエントリポイント。"""
    base_dir = get_base_dir()
    experiment_dir = get_experiment_dir()
    experiment_dir.mkdir(parents=True, exist_ok=True)

    # 実験ディレクトリメタ情報を保存（deploy_distribute.py などが参照する）
    exp_name = _get("experiment", "experiment_name", "default")
    timestamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    meta = {
        "experiment_name": exp_name,
        "timestamp": timestamp,
        "dir_name": f"{exp_name}_{timestamp}",
    }
    (experiment_dir / ".experiment_meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2)
    )

    # hosts.txtの読み込み
    hosts = []
    for line in get_hosts_path().read_text().strip().splitlines():
        ip = line.strip()
        if ip and not ip.startswith("#"):
            hosts.append(ip)
    num_peers = len(hosts)
    print(f"Detected {num_peers} peers from hosts.txt")

    # GSM8Kデータセットの読み込み
    print("Loading GSM8K dataset...")
    cache_dir = experiment_dir / "cache" / "datasets" / "gsm8k"
    cache_dir.mkdir(parents=True, exist_ok=True)
    train_path = cache_dir / "main" / "train-00000-of-00001.parquet"
    if not train_path.exists():
        print("Downloading gsm8k dataset...")
        subprocess.run(
            [
                "hf", "download", "gsm8k", "--repo-type", "dataset",
                "--include", "main/*", "--local-dir", str(cache_dir),
            ],
            check=True,
        )
    dataset = datasets.load_dataset(
        "parquet", data_dir=str(cache_dir / "main"), split="train"
    )
    raw_data = dataset.to_list()

    print(f"Loaded {len(raw_data)} total examples")

    # train/test 分割（設定からデフォルト90/10）
    validation_split = _get("data", "validation_split", 0.1)
    rng = random.Random(_get_int("data", "seed", 42))
    rng.shuffle(raw_data)
    split_idx = int(len(raw_data) * (1.0 - validation_split))
    train_data = raw_data[:split_idx]
    test_data = raw_data[split_idx:]
    print(f"Train: {len(train_data)}, Test: {len(test_data)}")

    # 訓練データにカテゴリラベルを付与
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

    # カテゴリ分布の統計を出力
    print("=== Non-IID Train Shard Distribution ===")
    for peer_id in sorted(sharded.keys()):
        cat_counts: dict[str, int] = {}
        for item in sharded[peer_id]:
            cat = classify_problem(item["question"])
            cat_counts[cat] = cat_counts.get(cat, 0) + 1
        print(
            f"  Peer {peer_id} (specialty={peer_categories[peer_id]}): "
            f"total={len(sharded[peer_id])}, "
            f"distribution={cat_counts}"
        )
    print("========================================")

    # テストデータをpeerごとに均等分配（ランダム）
    test_sharded: dict[int, list[dict]] = {i: [] for i in range(num_peers)}
    for idx, item in enumerate(test_data):
        peer_id = idx % num_peers
        test_sharded[peer_id].append(item)
    for peer_id in test_sharded:
        rng.shuffle(test_sharded[peer_id])

    # peerごとのファイルとして保存（プロジェクトルート直下の data/ へ）
    print("\nSaving peer files...")
    save_peer_files(
        sharded, test_sharded, base_dir,
        train_dir_name="data/train",
        test_dir_name="data/test",
    )


if __name__ == "__main__":
    main()
