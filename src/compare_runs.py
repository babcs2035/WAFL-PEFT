#!/usr/bin/env python3
"""WAFL-PEFT と self-training を「複数回反復」の平均±標準偏差で比較する。

compare_baselines.py が 1 条件 1 実験を比較するのに対し、本スクリプトは 1 条件あたり
複数の反復実験（例: Iter10r1..r3 と SelfTrainr1..r3）を集計し、指標ごとに平均と標準偏差を
出して条件間の差がノイズ（反復間ばらつき）に対して有意かを見えるようにする。

集計指標（各実験を compare_baselines.summarize で要約し、run 間で mean/std を取る）:
  - 各デバイスの最終 accuracy 平均（avg_last）
  - 学習前→後の改善（avg_gain）
  - 平均マージモデルの最終 accuracy（merged_final）
  - 平均訓練損失（analysis_report.md から抽出）

使い方（管理サーバー不要、ローカルで実行）:
  uv run python src/compare_runs.py
    引数省略時は results/ 下の Iter10r[1-3]_* と SelfTrainr[1-3]_* を自動選択する。
  uv run python src/compare_runs.py "WAFL-PEFT=Iter10r1_*,Iter10r2_*,Iter10r3_*" \
                                    "Self-training=SelfTrainr1_*,SelfTrainr2_*,SelfTrainr3_*"
出力:
  - results/fig07_baseline_comparison.png（誤差棒付きの条件間比較図）
  - 標準出力への比較表（Markdown、mean±std）
"""

import glob
import json
import re
import statistics
import sys
from pathlib import Path
from typing import Any

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt

sys.path.insert(0, str(Path(__file__).resolve().parent))
import compare_baselines as cb
from utils import get_base_dir

BASE_DIR = get_base_dir()
# 比較図の出力先（compare_baselines.py と同一パスで上書きする。results/ 直下は .gitignore 対象）。
_FIG_PATH = BASE_DIR / "results" / "fig07_baseline_comparison.png"
# 引数省略時に集計する条件（ラベル, run ディレクトリの glob パターン群）。
_DEFAULT_CONDITIONS: list[tuple[str, list[str]]] = [
    ("WAFL-PEFT", ["Iter10r1_*", "Iter10r2_*", "Iter10r3_*"]),
    ("Self-training", ["SelfTrainr1_*", "SelfTrainr2_*", "SelfTrainr3_*"]),
]
# analysis_report.md から平均訓練損失を取り出す正規表現（例: 「平均訓練損失（全ノード平均）: 0.5081」）。
_LOSS_RE = re.compile(r"平均訓練損失[^:：]*[:：]\s*([0-9]+\.?[0-9]*)")


def _resolve_dirs(patterns: list[str]) -> list[Path]:
    """glob パターン群を results/ 下の実在ディレクトリへ解決する（各パターンの最新 1 件）。"""
    resolved: list[Path] = []
    results = BASE_DIR / "results"
    for pat in patterns:
        cands = sorted((d for d in results.glob(pat) if d.is_dir()), key=lambda d: d.name, reverse=True)
        if cands:
            resolved.append(cands[0])
    return resolved


def _read_avg_loss(exp_dir: Path) -> float | None:
    """analysis_report.md から平均訓練損失を抽出する（無ければ None）。"""
    report = exp_dir / "output" / "analysis_report.md"
    if not report.exists():
        return None
    m = _LOSS_RE.search(report.read_text(encoding="utf-8"))
    return float(m.group(1)) if m else None


def _mean_std(values: list[float]) -> tuple[float, float]:
    """平均と標本標準偏差（n<2 は std=0）を返す。"""
    if not values:
        return (0.0, 0.0)
    mean = statistics.mean(values)
    std = statistics.stdev(values) if len(values) >= 2 else 0.0
    return (mean, std)


def aggregate(label: str, dirs: list[Path]) -> dict[str, Any]:
    """1 条件の複数 run を集計し、指標ごとに mean/std をまとめる。"""
    per_run: list[dict[str, Any]] = []
    for d in dirs:
        s = cb.summarize(d)
        s["avg_loss"] = _read_avg_loss(d)
        s["dir"] = d.name
        per_run.append(s)

    def col(key: str) -> list[float]:
        return [r[key] for r in per_run if r.get(key) is not None]

    return {
        "label": label,
        "n_runs": len(per_run),
        "runs": per_run,
        "first": _mean_std(col("avg_first")),
        "last": _mean_std(col("avg_last")),
        "gain": _mean_std(col("avg_gain")),
        "merged_final": _mean_std(col("merged_final")),
        "merged_peak": _mean_std(col("merged_peak")),
        "loss": _mean_std(col("avg_loss")),
    }


def plot_comparison(aggs: list[dict[str, Any]]) -> Path:
    """条件間で per-node 最終 acc・改善・マージ最終 acc を誤差棒付き棒グラフで比較する。"""
    # 図内ラベルは英語で統一する（matplotlib 既定フォントに CJK が無く豆腐化するため）。
    metrics = [
        ("per-node final acc (%)", "last"),
        ("per-node gain (pt)", "gain"),
        ("merged model final acc (%)", "merged_final"),
    ]
    labels = [a["label"] for a in aggs]
    colors = ["#1f77b4", "#ff7f0e"]

    x = range(len(metrics))
    width = 0.36
    fig, ax = plt.subplots(figsize=(8.5, 5))
    for i, a in enumerate(aggs):
        means = [a[key][0] for _, key in metrics]
        stds = [a[key][1] for _, key in metrics]
        offset = (i - (len(aggs) - 1) / 2) * width
        bars = ax.bar([xi + offset for xi in x], means, width, yerr=stds, capsize=5,
                      label=f"{a['label']} (n={a['n_runs']})", color=colors[i % len(colors)])
        for xi, m, s in zip(x, means, stds):
            ax.text(xi + offset, m + s + 0.4, f"{m:.1f}", ha="center", va="bottom", fontsize=9)
    ax.set_xticks(list(x))
    ax.set_xticklabels([name for name, _ in metrics])
    ax.set_ylabel("value")
    ax.set_title("WAFL-PEFT vs Self-training (mean ± std over repeated runs)")
    ax.legend()
    ax.grid(axis="y", alpha=0.3)
    _FIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    fig.tight_layout()
    fig.savefig(_FIG_PATH, dpi=120)
    plt.close(fig)
    return _FIG_PATH


def _parse_args() -> list[tuple[str, list[str]]]:
    """コマンドライン引数を (ラベル, glob パターン群) の一覧へ解釈する。"""
    if len(sys.argv) <= 1:
        return _DEFAULT_CONDITIONS
    conditions: list[tuple[str, list[str]]] = []
    for arg in sys.argv[1:]:
        if "=" not in arg:
            print(f"引数は 'ラベル=glob1,glob2,...' の形式で指定する: {arg}", file=sys.stderr)
            sys.exit(1)
        label, pats = arg.split("=", 1)
        conditions.append((label, [p.strip() for p in pats.split(",") if p.strip()]))
    return conditions


def main() -> None:
    """メインエントリポイント。"""
    aggs: list[dict[str, Any]] = []
    for label, patterns in _parse_args():
        dirs = _resolve_dirs(patterns)
        if not dirs:
            print(f"警告: {label} の対象ディレクトリが見つからない: {patterns}", file=sys.stderr)
            continue
        aggs.append(aggregate(label, dirs))

    if not aggs:
        print("比較対象が見つからない。", file=sys.stderr)
        sys.exit(1)

    # 反復ごとの内訳
    print("\n### 反復ごとの内訳")
    for a in aggs:
        print(f"\n**{a['label']}** (n={a['n_runs']})")
        print("| run | 学習前(%) | 学習後(%) | 改善(pt) | マージ最終(%) | 平均訓練損失 |")
        print("|-----|----------|----------|---------|--------------|-------------|")
        for r in a["runs"]:
            mf = "-" if r["merged_final"] is None else f"{r['merged_final']:.1f}"
            ls = "-" if r.get("avg_loss") is None else f"{r['avg_loss']:.4f}"
            print(f"| {r['dir']} | {r['avg_first']:.1f} | {r['avg_last']:.1f} | "
                  f"{r['avg_gain']:+.1f} | {mf} | {ls} |")

    # 集計比較表（mean ± std）
    print("\n### 集計比較（平均 ± 標準偏差）")
    print("| 条件 | n | 学習前(%) | 学習後(%) | 改善(pt) | マージ最終(%) | 平均訓練損失 |")
    print("|------|---|----------|----------|---------|--------------|-------------|")
    for a in aggs:
        print(f"| {a['label']} | {a['n_runs']} | "
              f"{a['first'][0]:.1f}±{a['first'][1]:.1f} | "
              f"{a['last'][0]:.1f}±{a['last'][1]:.1f} | "
              f"{a['gain'][0]:+.1f}±{a['gain'][1]:.1f} | "
              f"{a['merged_final'][0]:.1f}±{a['merged_final'][1]:.1f} | "
              f"{a['loss'][0]:.4f}±{a['loss'][1]:.4f} |")

    # --- 統計検定出力（W1: McNemar 対比較 + Wilson CI） ---
    print("\n### 統計検定（McNemar 対比較 + Wilson 95% CI）")
    print("※ device_eval.log が存在する実験のみ対象。未対応の実験はスキップ。")

    # 各条件・各runのper-question結果を収集
    condition_results: dict[str, list[tuple[Path, list[bool]]]] = {}
    for a in aggs:
        condition_results[a["label"]] = []
        for r in a["runs"]:
            exp_dir = r.get("dir", "")
            if exp_dir:
                d = BASE_DIR / exp_dir
            else:
                continue
            pq = cb.extract_per_question_results(d)
            if pq:
                # 多数決結果のリストを作成
                results_list = list(pq.values()) if isinstance(list(pq.values())[0], list) else []
                if not results_list:
                    # 直接list[bool]の場合
                    results_list = pq if isinstance(pq, list) else []
                condition_results[a["label"]].append((d, results_list))

    # 条件間ペアのMcNemar対比較
    labels = [a["label"] for a in aggs]
    for i in range(len(labels)):
        for j in range(i + 1, len(labels)):
            lab_a, lab_b = labels[i], labels[j]
            res_a = condition_results[lab_a]
            res_b = condition_results[lab_b]
            if not res_a or not res_b:
                continue
            # 両方の条件でper-question結果が存在するrunペアを探す
            for d_a, lst_a in res_a:
                for d_b, lst_b in res_b:
                    if len(lst_a) > 0 and len(lst_b) > 0 and len(lst_a) == len(lst_b):
                        mcn = cb.mcnemar_test(lst_a, lst_b)
                        # Wilson CI
                        total = mcn["n"]
                        a_correct = mcn["a_only"] + mcn["both"]
                        b_correct = mcn["b_only"] + mcn["both"]
                        ci_a = cb.wilson_ci(a_correct, total)
                        ci_b = cb.wilson_ci(b_correct, total)
                        print(
                            f"\n**{lab_a} vs {lab_b}** ({d_a.name} vs {d_b.name})"
                        )
                        print(
                            f"  McNemar chi2={mcn['chi2']:.3f}, p={mcn['pvalue']:.4f}"
                        )
                        print(
                            f"  {lab_a}: {a_correct}/{total} = {a_correct/total*100:.1f}% "
                            f"(Wilson 95% CI [{ci_a[0]*100:.1f}%, {ci_a[1]*100:.1f}%])"
                        )
                        print(
                            f"  {lab_b}: {b_correct}/{total} = {b_correct/total*100:.1f}% "
                            f"(Wilson 95% CI [{ci_b[0]*100:.1f}%, {ci_b[1]*100:.1f}%])"
                        )
                        break
                else:
                    continue
                break

    fig = plot_comparison(aggs)
    print(f"\n比較図を保存した: {fig}")


if __name__ == "__main__":
    main()
