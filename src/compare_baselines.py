#!/usr/bin/env python3
"""WAFL-PEFT とベースライン（self-training / FedAvg 近似）の比較図・比較表を生成する。

複数の実験ディレクトリ（results/<name>_<timestamp>/）を読み込み、条件間で
  - 各ノードの GSM8K accuracy（学習前→後の改善・最終値）の平均
  - 全ノード平均マージモデルの収束
を比較する。self-training（P2P 無効）と FedAvg 近似（全結合トポロジー）を WAFL-PEFT（時変 P2P）と
並べ、「P2P 知識伝播が孤立学習より優れ、中央集約に近づくか」を検証する目的の図・表を作る。

使い方（管理サーバー不要、ローカルで実行）:
  uv run python src/compare_baselines.py \
      "WAFL-PEFT=results/Iter10_20260712T150353" \
      "Self-training=results/SelfTrain_XXXX" \
      "FedAvg近似=results/FedAvgApprox_XXXX"

引数を省略すると、results/ 下から Iter10_* / SelfTrain_* / FedAvgApprox_* の最新をそれぞれ自動選択する。
出力: results/fig07_baseline_comparison.png と、標準出力への比較表（Markdown）。
"""

import json
import sys
from pathlib import Path
from typing import Any

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt

from utils import get_base_dir

BASE_DIR = get_base_dir()
# 比較図の出力先（results/ 直下。個別実験の output/ と異なり複数実験を横断するため専用の場所に置く）
_FIG_PATH = BASE_DIR / "results" / "fig07_baseline_comparison.png"
# 引数省略時に自動選択する条件（ラベル, results 下のフォルダ名 prefix）。
# ベースラインは self-training のみ（FedAvg 近似は実施しない方針）。
_DEFAULT_CONDITIONS = [
    ("WAFL-PEFT", "Iter10_"),
    ("Self-training", "SelfTrain_"),
]


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    """JSON Lines を読み込む（無ければ空リスト）。"""
    if not path.exists():
        return []
    out: list[dict[str, Any]] = []
    for line in path.read_text().strip().splitlines():
        if line.strip():
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out


def _latest_dir(prefix: str) -> Path | None:
    """results/ 下で prefix に一致する最新（名前降順＝タイムスタンプ最大）のディレクトリを返す。"""
    results = BASE_DIR / "results"
    cands = sorted(
        (d for d in results.glob(f"{prefix}*") if d.is_dir()),
        key=lambda d: d.name, reverse=True,
    )
    return cands[0] if cands else None


def load_per_node_accuracy(exp_dir: Path) -> dict[int, tuple[float, float]]:
    """各ノードの (first, last) accuracy を返す。

    優先: results/<exp>/device_eval.log（評価専用ホスト集約、{"peer_id","step","accuracy"}）。
    無ければ各 peer の metrics ログ内の "type":"eval" レコード（学習ノード自己評価）へフォールバック。
    """
    by_peer: dict[int, list[tuple[int, float]]] = {}

    device_log = _read_jsonl(exp_dir / "device_eval.log")
    if device_log:
        for r in device_log:
            pid = r.get("peer_id")
            if pid is None or "accuracy" not in r:
                continue
            by_peer.setdefault(int(pid), []).append((int(r.get("step", 0)), float(r["accuracy"])))
    else:
        # フォールバック: 自己評価の "eval" レコード
        logs_dir = exp_dir / "logs"
        for peer_dir in sorted(logs_dir.glob("peer_*")):
            for f in list(peer_dir.glob("metrics_peer_*_final.log")) + list(
                peer_dir.glob("logs/metrics_peer_*_final.log")
            ):
                for line in f.read_text().strip().splitlines():
                    try:
                        m = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if m.get("type") == "eval" and "accuracy" in m:
                        pid = int(m.get("peer_id"))
                        by_peer.setdefault(pid, []).append((int(m.get("step", 0)), float(m["accuracy"])))

    result: dict[int, tuple[float, float]] = {}
    for pid, recs in by_peer.items():
        recs.sort(key=lambda t: t[0])
        if recs:
            result[pid] = (recs[0][1], recs[-1][1])
    return result


def load_merged_curve(exp_dir: Path) -> list[float]:
    """global_eval.log からマージモデル accuracy の時系列（accuracy のみ）を返す。"""
    recs = _read_jsonl(exp_dir / "global_eval.log")
    recs.sort(key=lambda r: r.get("step", 0))
    return [float(r["accuracy"]) for r in recs if "accuracy" in r]


def summarize(exp_dir: Path) -> dict[str, Any]:
    """1 実験の per-node accuracy 改善・最終値の平均、マージ最終/ピークを集計する。"""
    per_node = load_per_node_accuracy(exp_dir)
    n = len(per_node)
    if n == 0:
        return {"n": 0, "avg_first": 0.0, "avg_last": 0.0, "avg_gain": 0.0,
                "merged_final": None, "merged_peak": None, "per_node": {}}
    avg_first = sum(f for f, _ in per_node.values()) / n
    avg_last = sum(l for _, l in per_node.values()) / n
    merged = load_merged_curve(exp_dir)
    return {
        "n": n,
        "avg_first": avg_first,
        "avg_last": avg_last,
        "avg_gain": avg_last - avg_first,
        "merged_final": (merged[-1] if merged else None),
        "merged_peak": (max(merged) if merged else None),
        "per_node": per_node,
    }


def plot_comparison(labeled: list[tuple[str, dict[str, Any]]]) -> Path:
    """条件ごとの per-node 平均 accuracy（first/last）とマージ最終値を棒グラフで比較する。"""
    labels = [lab for lab, _ in labeled]
    firsts = [s["avg_first"] for _, s in labeled]
    lasts = [s["avg_last"] for _, s in labeled]
    merged_finals = [(s["merged_final"] or 0.0) for _, s in labeled]

    x = range(len(labels))
    width = 0.26
    fig, ax = plt.subplots(figsize=(8, 5))
    # 図内ラベルは英語で統一する（matplotlib 既定フォントに CJK が無く豆腐化するため）
    ax.bar([i - width for i in x], firsts, width, label="per-node acc before (avg)", color="#bbbbbb")
    ax.bar([i for i in x], lasts, width, label="per-node acc after (avg)", color="#1f77b4")
    ax.bar([i + width for i in x], merged_finals, width, label="merged model final acc", color="#ff7f0e")
    # 値ラベルを棒の上に付す
    for i, v in zip(x, lasts):
        ax.text(i, v + 0.4, f"{v:.1f}", ha="center", va="bottom", fontsize=9)
    ax.set_xticks(list(x))
    ax.set_xticklabels(labels)
    ax.set_ylabel("GSM8K accuracy (%)")
    ax.set_title("WAFL-PEFT vs Self-training: knowledge convergence")
    ax.legend()
    ax.grid(axis="y", alpha=0.3)
    _FIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    fig.tight_layout()
    fig.savefig(_FIG_PATH, dpi=120)
    plt.close(fig)
    return _FIG_PATH


def main() -> None:
    """メインエントリポイント。"""
    conditions: list[tuple[str, Path]] = []
    if len(sys.argv) > 1:
        for arg in sys.argv[1:]:
            if "=" not in arg:
                print(f"引数は 'ラベル=results/フォルダ' の形式で指定する: {arg}", file=sys.stderr)
                sys.exit(1)
            label, path = arg.split("=", 1)
            conditions.append((label, (BASE_DIR / path) if not Path(path).is_absolute() else Path(path)))
    else:
        for label, prefix in _DEFAULT_CONDITIONS:
            d = _latest_dir(prefix)
            if d is not None:
                conditions.append((label, d))

    if not conditions:
        print("比較対象の実験ディレクトリが見つからない（引数指定するか、対象実験を実行すること）。", file=sys.stderr)
        sys.exit(1)

    labeled: list[tuple[str, dict[str, Any]]] = []
    for label, d in conditions:
        if not d.exists():
            print(f"警告: {label} のディレクトリが存在しない: {d}", file=sys.stderr)
            continue
        labeled.append((label, summarize(d)))

    # 比較表（Markdown）を標準出力へ
    print("\n| 条件 | ノード数 | 学習前平均(%) | 学習後平均(%) | 改善(pt) | マージ最終(%) | マージピーク(%) |")
    print("|------|---------|--------------|--------------|---------|--------------|----------------|")
    for label, s in labeled:
        mf = "-" if s["merged_final"] is None else f"{s['merged_final']:.1f}"
        mp = "-" if s["merged_peak"] is None else f"{s['merged_peak']:.1f}"
        print(f"| {label} | {s['n']} | {s['avg_first']:.1f} | {s['avg_last']:.1f} | "
              f"{s['avg_gain']:+.1f} | {mf} | {mp} |")

    fig = plot_comparison(labeled)
    print(f"\n比較図を保存した: {fig}")


if __name__ == "__main__":
    main()
