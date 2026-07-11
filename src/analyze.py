#!/usr/bin/env python3
"""WAFL-PEFT実験結果の分析・グラフ生成スクリプト。

回収した全ノードのメトリクスログをマージし、以下の評価グラフを生成:
  1. スループットの平坦性とストールフリーの実証（Token/s vs 時間）
  2. 各ノードの損失関数の推移（Loss vs 時間）
  3. 各ノードの loss vs throughput 散布図
  5. マージモデルの収束性能（Accuracy vs 時間）
  6. 各ノード個別モデルの収束性能（Accuracy vs 時間）

評価（accuracy算出）はこのスクリプト自身では行わない。マージモデルの
収束性能（評価5）は実験中に管理サーバー（server.pyのGlobalEvalスレッド）が
専用GPU上で評価し results/{experiment_dir}/global_eval.log にライブ記録した
ものを読む。ノード別の収束性能（評価6）は実験終了後に各学習デバイスが自分の
GPUで評価し（client.pyのrun_post_experiment_evaluation）、既存のログ回収
（collect_logs.py）でresults/{experiment_dir}/logs/peer_X/へ集約済みの
メトリクスログから読む。このスクリプトはGPUを使わず、それらを読んでプロット
するだけである。環境変数 ANALYZE_CONVERGENCE=0 を設定すると評価5・6の
グラフをスキップできる。
"""

import json
import os
import sys
from pathlib import Path
from typing import Any

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

from utils import (
    get_base_dir,
    get_experiment_name,
    get_latest_experiment_dir,
    get_log_dir,
    get_output_dir,
    _get,
    _get_float,
    _get_int,
    _get_str,
)

BASE_DIR = get_base_dir()

# 実験ディレクトリの解決: EXPERIMENT_DIR 環境変数で明示指定されていればそれを、
# なければ results/ 下で最後に作成されたディレクトリ（server.py が実験開始時に
# 一度だけ作成したもの）を自動選択する
_env_dir = os.environ.get("EXPERIMENT_DIR")
if _env_dir:
    EXPERIMENT_DIR = Path(_env_dir)
else:
    _latest_dir = get_latest_experiment_dir()
    if _latest_dir is None:
        print("Error: No experiment directory found under results/.", file=sys.stderr)
        sys.exit(1)
    EXPERIMENT_DIR = _latest_dir

OUTPUT_DIR = EXPERIMENT_DIR / "output"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

# ============================================================
# データ読み込み
# ============================================================


def load_metrics(log_dir: Path) -> dict[int, list[dict[str, Any]]]:
    """全ノードのメトリクスログを読み込み。

    collect_logs.py 回収時は logs/peer_X/weights/ に重みが配置される。
    二重ネスト logs/peer_X/logs/ も後方互換で対応。
    """
    all_metrics: dict[int, list[dict[str, Any]]] = {}

    for peer_dir in sorted(log_dir.glob("peer_*")):
        if not peer_dir.is_dir():
            continue
        # 直接のログファイル
        for f in sorted(peer_dir.glob("metrics_peer_*_final.log")):
            parts = f.stem.split("_")
            peer_id = int(parts[2])
            metrics: list[dict[str, Any]] = []
            for line in f.read_text().strip().splitlines():
                if line.strip():
                    try:
                        metrics.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
            all_metrics[peer_id] = metrics

        # 二重ネスト logs/peer_X/logs/
        nested_logs = peer_dir / "logs"
        if nested_logs.is_dir():
            for f in sorted(nested_logs.glob("metrics_peer_*_final.log")):
                if f.stem in all_metrics:
                    continue
                parts = f.stem.split("_")
                peer_id = int(parts[2])
                metrics: list[dict[str, Any]] = []
                for line in f.read_text().strip().splitlines():
                    if line.strip():
                        try:
                            metrics.append(json.loads(line))
                        except json.JSONDecodeError:
                            continue
                all_metrics[peer_id] = metrics

    if not all_metrics:
        for f in sorted(log_dir.glob("metrics_peer_*_final.log")):
            parts = f.stem.split("_")
            peer_id = int(parts[2])
            metrics: list[dict[str, Any]] = []
            for line in f.read_text().strip().splitlines():
                if line.strip():
                    try:
                        metrics.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
            all_metrics[peer_id] = metrics

    return all_metrics


def _get_avg_elapsed_at_step(
    all_metrics: dict[int, list[dict[str, Any]]],
    target_step: int,
) -> float:
    """指定ステップ付近の平均 elapsed time を取得。"""
    elapsed_values: list[float] = []
    for metrics in all_metrics.values():
        for m in metrics:
            if m.get("step", 0) == target_step:
                elapsed_values.append(m.get("elapsed", 0.0))
    if elapsed_values:
        return float(np.mean(elapsed_values))
    best_elapsed: list[float] = []
    for metrics in all_metrics.values():
        for m in metrics:
            if abs(m.get("step", 0) - target_step) <= 1:
                best_elapsed.append(m.get("elapsed", 0.0))
    if best_elapsed:
        return float(np.mean(best_elapsed))
    return 0.0


# ============================================================
# 評価1: スループットの平坦性
# ============================================================

# GPU/モデルのウォームアップ期間として除外する秒数。contact_pattern.jsonの
# 最初のイベント時刻（t=60s）に合わせる
_WARMUP_EXCLUDE_SECONDS = 60.0


def _compute_throughput_correlations(
    all_metrics: dict[int, list[dict[str, Any]]],
) -> tuple[float | None, float | None]:
    """elapsed time と tokens_per_sec の相関係数を計算する。

    (全期間の相関, ウォームアップ除外後の相関) を返す。データ不足時は None。
    GPU/モデルのウォームアップ（CUDAカーネルJITコンパイル・メモリアロケータの
    安定化）による立ち上がりは、通信起因のストールとは別の要因で相関係数を
    押し上げるため、両者を区別できるよう分けて算出する。
    """
    all_times = [m["elapsed"] for metrics in all_metrics.values() for m in metrics if m.get("tokens_per_sec", 0) > 0]
    all_tok_s = [m["tokens_per_sec"] for metrics in all_metrics.values() for m in metrics if m.get("tokens_per_sec", 0) > 0]
    if len(all_times) <= 2:
        return None, None

    times_arr = np.array(all_times)
    tok_s_arr = np.array(all_tok_s)
    corr_full = float(np.corrcoef(times_arr, tok_s_arr)[0, 1])

    warmup_mask = times_arr >= _WARMUP_EXCLUDE_SECONDS
    corr_post_warmup = float(np.corrcoef(times_arr[warmup_mask], tok_s_arr[warmup_mask])[0, 1]) if warmup_mask.sum() > 2 else None
    return corr_full, corr_post_warmup


def plot_throughput(all_metrics: dict[int, list[dict[str, Any]]]) -> Path:
    """グラフ1: Token/s vs 時間（実時間軸でのスループット平坦性）。"""
    print("\n=== Evaluation 1: Throughput Flatness ===")

    fig, axes = plt.subplots(2, 1, figsize=(14, 10))
    ax1 = axes[0]
    all_times: list[float] = []
    all_tok_s: list[float] = []

    for peer_id, metrics in all_metrics.items():
        peer_times: list[float] = []
        peer_tok_s: list[float] = []
        for m in metrics:
            if "tokens_per_sec" in m and m["tokens_per_sec"] > 0:
                peer_times.append(m["elapsed"])
                peer_tok_s.append(m["tokens_per_sec"])
        if peer_times:
            ax1.plot(peer_times, peer_tok_s, alpha=0.3, color="steelblue", linewidth=0.5)
            all_times.extend(peer_times)
            all_tok_s.extend(peer_tok_s)

    if all_times and all_tok_s:
        times_arr = np.array(all_times)
        tok_s_arr = np.array(all_tok_s)
        bins = np.linspace(0, times_arr.max(), 50)
        bin_centers = (bins[:-1] + bins[1:]) / 2
        means, stds = [], []
        for i in range(len(bins) - 1):
            mask = (times_arr >= bins[i]) & (times_arr < bins[i + 1])
            if mask.sum() > 0:
                means.append(tok_s_arr[mask].mean())
                stds.append(tok_s_arr[mask].std())
            else:
                means.append(np.nan)
                stds.append(np.nan)
        ax1.plot(bin_centers, means, color="red", linewidth=2, label="Mean (binned)", zorder=10)
        ax1.fill_between(bin_centers, np.array(means) - np.array(stds), np.array(means) + np.array(stds), alpha=0.2, color="red")

    ax1.set_xlabel("Elapsed Time (seconds)")
    ax1.set_ylabel("Throughput (tokens/s)")
    ax1.set_title("Evaluation 1: Throughput Flatness (Stall-Free Demonstration)")
    ax1.legend(loc="lower right")
    ax1.grid(True, alpha=0.3)

    corr_full, corr_post_warmup = _compute_throughput_correlations(all_metrics)
    if corr_full is not None:
        corr_text = f"Correlation (full): {corr_full:.4f}\n(should be ~0 for stall-free)"
        if corr_post_warmup is not None:
            corr_text += f"\nCorrelation (t>={_WARMUP_EXCLUDE_SECONDS:.0f}s, excl. warmup): {corr_post_warmup:.4f}"
        ax1.text(0.02, 0.98, corr_text, transform=ax1.transAxes, verticalalignment="top", bbox=dict(boxstyle="round", facecolor="wheat", alpha=0.5))

    ax2 = axes[1]
    for peer_id, metrics in all_metrics.items():
        peer_times, peer_cumulative = [], []
        for m in metrics:
            if "total_tokens" in m:
                peer_times.append(m["elapsed"])
                peer_cumulative.append(m["total_tokens"])
        if peer_times:
            ax2.plot(peer_times, peer_cumulative, alpha=0.4, linewidth=1)
    ax2.set_xlabel("Elapsed Time (seconds)")
    ax2.set_ylabel("Cumulative Tokens")
    ax2.set_title("Cumulative Token Count per Peer")
    ax2.grid(True, alpha=0.3)

    plt.tight_layout()
    out_path = OUTPUT_DIR / "fig01_throughput_flatness.png"
    plt.savefig(str(out_path), dpi=150, bbox_inches="tight")
    plt.close()
    print(f"  Saved: {out_path}")
    return out_path


# ============================================================
# 評価2: 損失関数の推移
# ============================================================


def plot_loss_curves(all_metrics: dict[int, list[dict[str, Any]]]) -> Path:
    """グラフ2: 各ノードのloss推移。"""
    print("\n=== Evaluation 2: Per-Peer Loss Curves ===")

    fig, ax = plt.subplots(figsize=(12, 7))
    for peer_id, metrics in sorted(all_metrics.items()):
        peer_times, peer_losses = [], []
        for m in metrics:
            if "loss" in m:
                peer_times.append(m["elapsed"])
                peer_losses.append(m["loss"])
        if peer_times:
            ax.plot(peer_times, peer_losses, alpha=0.4, linewidth=0.8, label=f"Peer {peer_id}")

    all_times, all_losses = [], []
    for metrics in all_metrics.values():
        for m in metrics:
            if "loss" in m:
                all_times.append(m["elapsed"])
                all_losses.append(m["loss"])

    if all_times:
        times_arr = np.array(all_times)
        losses_arr = np.array(all_losses)
        bins = np.linspace(0, times_arr.max(), 50)
        bin_centers = (bins[:-1] + bins[1:]) / 2
        means, stds = [], []
        for i in range(len(bins) - 1):
            mask = (times_arr >= bins[i]) & (times_arr < bins[i + 1])
            if mask.sum() > 0:
                means.append(losses_arr[mask].mean())
                stds.append(losses_arr[mask].std())
            else:
                means.append(np.nan)
                stds.append(np.nan)
        ax.plot(bin_centers, means, color="red", linewidth=2.5, label="Mean (binned)", zorder=10)
        ax.fill_between(bin_centers, np.array(means) - np.array(stds), np.array(means) + np.array(stds), alpha=0.2, color="red")

    ax.set_xlabel("Elapsed Time (seconds)")
    ax.set_ylabel("Training Loss")
    ax.set_title("Per-Peer Loss Curves (Training Progress)")
    ax.legend(loc="upper right", fontsize="small")
    ax.grid(True, alpha=0.3)

    plt.tight_layout()
    out_path = OUTPUT_DIR / "fig02_loss_curves.png"
    plt.savefig(str(out_path), dpi=150, bbox_inches="tight")
    plt.close()
    print(f"  Saved: {out_path}")
    return out_path


# ============================================================
# 評価3: per-peer 散布図
# ============================================================


def plot_per_peer_scatter(all_metrics: dict[int, list[dict[str, Any]]]) -> Path:
    """グラフ3: 各ノードの loss vs throughput 散布図。"""
    print("\n=== Evaluation 3: Per-Peer Scatter (Loss vs Throughput) ===")

    fig, ax = plt.subplots(figsize=(10, 7))
    colors = plt.cm.Set1(np.linspace(0, 1, len(all_metrics)))

    for (peer_id, metrics), color in zip(sorted(all_metrics.items()), colors):
        peer_losses, peer_tok_s = [], []
        for m in metrics:
            if "loss" in m and "tokens_per_sec" in m and m["tokens_per_sec"] > 0:
                peer_losses.append(m["loss"])
                peer_tok_s.append(m["tokens_per_sec"])
        if peer_losses:
            ax.scatter(peer_tok_s, peer_losses, c=[color], s=15, alpha=0.6, label=f"Peer {peer_id}", edgecolors="none")

    ax.set_xlabel("Throughput (tokens/s)")
    ax.set_ylabel("Training Loss")
    ax.set_title("Per-Peer Loss vs Throughput (Color = Peer ID)")
    ax.legend(loc="upper right", fontsize="small")
    ax.grid(True, alpha=0.3)

    plt.tight_layout()
    out_path = OUTPUT_DIR / "fig04_per_peer_scatter.png"
    plt.savefig(str(out_path), dpi=150, bbox_inches="tight")
    plt.close()
    print(f"  Saved: {out_path}")
    return out_path


# ============================================================
# 評価5・6: 収束性能（サーバーが実験中にライブ記録したログを読むだけ）
# ============================================================

# 評価5・6を無効化する環境変数。デフォルトはオン。
_ENABLE_CONVERGENCE = os.environ.get("ANALYZE_CONVERGENCE", "1") == "1"


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    """JSON Linesファイルを読み込む。存在しなければ空リスト。"""
    if not path.exists():
        return []
    records: list[dict[str, Any]] = []
    for line in path.read_text().strip().splitlines():
        if not line.strip():
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return records


def load_global_eval_log(
    experiment_dir: Path,
    all_metrics: dict[int, list[dict[str, Any]]],
) -> tuple[list[float], list[float]]:
    """server.py の GlobalEval スレッドが実験中にライブ記録した
    global_eval.log（マージモデルaccuracy）を読み込む。

    評価そのものは管理サーバーの専用GPU上で実験中に完了済みのため、
    ここでは再評価せずログを読んで時系列に整形するだけでよい
    （generate()を伴う評価は学習デバイス・分析側どちらでも行わない）。
    """
    records = _read_jsonl(experiment_dir / "global_eval.log")
    if not records:
        return [], []
    records.sort(key=lambda r: r["step"])
    time_steps = [_get_avg_elapsed_at_step(all_metrics, r["step"]) for r in records]
    accuracies = [r["accuracy"] for r in records]
    return time_steps, accuracies


def load_device_eval_log(
    all_metrics: dict[int, list[dict[str, Any]]],
) -> dict[int, tuple[list[float], list[float]]]:
    """各クライアントが実験終了後に自分のGPUで記録した"eval"レコード
    （accuracyフィールド付き）をノードごとの時系列に整形する。

    client.py の run_post_experiment_evaluation が、自分のチェックポイント
    履歴を評価してmetrics_queue経由でmetrics_peer_X_final.logへ記録した
    ものであり、collect_logs.pyの既存rsync機構ですでにresults/{exp}/logs/
    peer_X/ 配下に回収済みのため、ここでは再評価せずload_metrics()が読み
    込んだall_metricsから該当レコードを抽出するだけでよい。
    """
    results: dict[int, tuple[list[float], list[float]]] = {}
    for pid, metrics in all_metrics.items():
        records = [
            m for m in metrics
            if m.get("type") == "eval" and "accuracy" in m
        ]
        if not records:
            continue
        records.sort(key=lambda m: m.get("step", 0))
        times = [m.get("elapsed", 0.0) for m in records]
        accs = [m["accuracy"] for m in records]
        results[pid] = (times, accs)
    return results


def plot_convergence(time_steps: list[float], accuracies: list[float]) -> Path:
    """グラフ5: 実時間 vs 総合テストAccuracy（収束曲線）。"""
    print("\n=== Plotting Convergence ===")
    fig, ax = plt.subplots(figsize=(12, 7))

    if time_steps and accuracies:
        ax.plot(time_steps, accuracies, "bo-", linewidth=2, markersize=6, label="WAFL-PEFT (Merged)")
        if len(time_steps) > 5:
            window = min(5, len(time_steps) // 3)
            smoothed = []
            for i in range(len(accuracies)):
                start = max(0, i - window // 2)
                end = min(len(accuracies), i + window // 2 + 1)
                smoothed.append(np.mean(accuracies[start:end]))
            ax.plot(time_steps, smoothed, "r-", linewidth=2, label=f"Smoothed (window={window})")

    ax.axhline(y=85.0, color="green", linestyle="--", alpha=0.7, label="Centralized Upper Bound (est.)")
    ax.axhline(y=65.0, color="orange", linestyle="--", alpha=0.7, label="Nakajima et al. (2026) Round-based (est.)")

    ax.set_xlabel("Elapsed Time (seconds)")
    ax.set_ylabel("Test Accuracy (%)")
    ax.set_title("Evaluation 5: Knowledge Convergence under Time-Varying Topology")
    ax.legend(loc="lower right")
    ax.grid(True, alpha=0.3)

    plt.tight_layout()
    out_path = OUTPUT_DIR / "fig03_convergence.png"
    plt.savefig(str(out_path), dpi=150, bbox_inches="tight")
    plt.close()
    print(f"  Saved: {out_path}")
    return out_path


# ============================================================
# 評価6: ノード別 accuracy 推移（各ノード単体モデルの収束）
# ============================================================


def plot_per_peer_accuracy(per_peer: dict[int, tuple[list[float], list[float]]]) -> Path:
    """グラフ6: 各ノード単体モデルの accuracy 推移。"""
    print("\n=== Plotting Per-Peer Accuracy ===")
    fig, ax = plt.subplots(figsize=(12, 7))
    for pid in sorted(per_peer.keys()):
        times, accs = per_peer[pid]
        ax.plot(times, accs, marker="o", linewidth=1.5, markersize=5, label=f"Peer {pid}")
    ax.set_xlabel("Elapsed Time (seconds)")
    ax.set_ylabel("GSM8K Val Accuracy (%)")
    ax.set_title("Evaluation 6: Per-Peer Model Accuracy over Time (each node's own model)")
    ax.legend(loc="lower right", fontsize="small")
    ax.grid(True, alpha=0.3)
    plt.tight_layout()
    out_path = OUTPUT_DIR / "fig06_per_peer_accuracy.png"
    plt.savefig(str(out_path), dpi=150, bbox_inches="tight")
    plt.close()
    print(f"  Saved: {out_path}")
    return out_path


# ============================================================
# レポート生成
# ============================================================


def generate_report(
    all_metrics: dict[int, list[dict[str, Any]]],
    throughput_path: Path,
    loss_path: Path,
    scatter_path: Path,
    convergence_path: Path,
    convergence_time_steps: list[float],
    convergence_accuracies: list[float],
    per_peer_acc: dict[int, tuple[list[float], list[float]]],
    per_peer_acc_path: Path,
) -> Path:
    """分析レポートを生成。

    画像はレポートと同じOUTPUT_DIR内に保存されるため、リンクはファイル名のみの
    相対パスで埋め込む（絶対パスだとレポートを別環境へコピーした際にリンク切れ
    になるため）。
    """
    report_lines: list[str] = []
    report_lines.append("# WAFL-PEFT Experiment Analysis Report")
    report_lines.append("")

    report_lines.append("## Experiment Configuration")
    report_lines.append("")
    report_lines.append(f"- Experiment name: `{get_experiment_name()}`")
    report_lines.append(f"- Experiment directory: `{EXPERIMENT_DIR.name}`")
    report_lines.append(f"- Model: `{_get('model', 'model_id')}`")
    report_lines.append(
        f"- LoRA rank / alpha: {_get_int('training', 'lora_rank')} / {_get_int('training', 'lora_alpha')}"
    )
    report_lines.append(f"- Learning rate: {_get_float('training', 'learning_rate')}")
    report_lines.append(f"- Batch size: {_get_int('training', 'batch_size')}")
    report_lines.append(f"- Max sequence length: {_get_int('training', 'max_seq_len')}")
    report_lines.append(
        f"- Eval/checkpoint interval: {_get_float('training', 'eval_interval_seconds', 60.0):.0f}s"
    )
    report_lines.append(f"- Contact pattern file: `{_get_str('experiment', 'contact_pattern_file')}`")
    report_lines.append("")

    report_lines.append("## Summary")
    report_lines.append(f"- Peers analyzed: {len(all_metrics)}")
    report_lines.append(f"- Total metric entries: {sum(len(m) for m in all_metrics.values())}")
    report_lines.append("")

    report_lines.append("## Evaluation Graphs")
    report_lines.append("")
    report_lines.append("### Evaluation 1: Throughput Flatness (Stall-Free Demonstration)")
    report_lines.append(f"![Throughput Flatness]({throughput_path.name})")
    report_lines.append("")
    report_lines.append("### Evaluation 2: Per-Peer Loss Curves")
    report_lines.append(f"![Loss Curves]({loss_path.name})")
    report_lines.append("")
    report_lines.append("### Evaluation 3: Loss vs Throughput Scatter")
    report_lines.append(f"![Loss vs Throughput]({scatter_path.name})")
    report_lines.append("")
    if convergence_path != Path("N/A"):
        report_lines.append("### Evaluation 5: Knowledge Convergence (Merged Model Accuracy)")
        report_lines.append(f"![Convergence]({convergence_path.name})")
        report_lines.append("")
    if per_peer_acc_path != Path("N/A"):
        report_lines.append("### Evaluation 6: Per-Peer Model Accuracy over Time")
        report_lines.append(f"![Per-Peer Accuracy]({per_peer_acc_path.name})")
        report_lines.append("")

    report_lines.append("## Per-Peer Statistics")
    report_lines.append("")
    report_lines.append(
        "| Peer | Steps | Checkpoints | Avg Loss | Min Loss | Max Loss | Avg Token/s | "
        "Avg Stall (s) | Contact Events | Accuracy (first→last) | Duration (s) |"
    )
    report_lines.append(
        "|------|-------|-------------|----------|----------|----------|-------------|"
        "---------------|----------------|------------------------|---------------|"
    )

    peer_stats: dict[int, dict[str, float]] = {}
    for peer_id in sorted(all_metrics.keys()):
        metrics = all_metrics[peer_id]
        losses = [m["loss"] for m in metrics if "loss" in m]
        tok_s = [m["tokens_per_sec"] for m in metrics if m.get("tokens_per_sec", 0) > 0]
        times = [m["elapsed"] for m in metrics if "elapsed" in m]
        stalls = [m["stall_duration"] for m in metrics if "stall_duration" in m]
        checkpoint_count = sum(1 for m in metrics if m.get("type") == "checkpoint")
        contact_event_count = sum(1 for m in metrics if m.get("type") == "contact_event")

        # 実験後評価（評価6）由来のノード別 accuracy から最初と最後を引く
        acc_first, acc_last = 0.0, 0.0
        if peer_id in per_peer_acc and per_peer_acc[peer_id][1]:
            acc_list = per_peer_acc[peer_id][1]
            acc_first, acc_last = acc_list[0], acc_list[-1]

        stats = {
            "steps": len(metrics),
            "avg_loss": float(np.mean(losses)) if losses else 0.0,
            "min_loss": float(np.min(losses)) if losses else 0.0,
            "max_loss": float(np.max(losses)) if losses else 0.0,
            "avg_tok_s": float(np.mean(tok_s)) if tok_s else 0.0,
            "avg_stall": float(np.mean(stalls)) if stalls else 0.0,
            "acc_first": acc_first,
            "acc_last": acc_last,
            "duration": float(np.max(times)) if times else 0.0,
        }
        peer_stats[peer_id] = stats
        report_lines.append(
            f"| {peer_id} | {len(metrics)} | {checkpoint_count} | "
            f"{stats['avg_loss']:.4f} | {stats['min_loss']:.4f} | {stats['max_loss']:.4f} | "
            f"{stats['avg_tok_s']:.1f} | {stats['avg_stall']:.2f} | {contact_event_count} | "
            f"{acc_first:.1f}%→{acc_last:.1f}% | "
            f"{stats['duration']:.1f} |"
        )

    all_avg_losses = [s["avg_loss"] for s in peer_stats.values()]
    all_avg_tok_s = [s["avg_tok_s"] for s in peer_stats.values()]
    all_durations = [s["duration"] for s in peer_stats.values()]
    # accuracy の改善量（正の差=改善）
    acc_deltas = [s["acc_last"] - s["acc_first"] for s in peer_stats.values() if s["acc_last"] or s["acc_first"]]

    report_lines.append("")
    report_lines.append("## Overall Statistics")
    report_lines.append("")
    report_lines.append(f"- Average loss across peers: {np.mean(all_avg_losses):.4f}")
    report_lines.append(f"- Average throughput: {np.mean(all_avg_tok_s):.1f} tokens/s")
    if acc_deltas:
        report_lines.append(
            f"- Mean per-peer accuracy change (first→last, positive = improvement): {np.mean(acc_deltas):+.1f} pts"
        )
    report_lines.append(f"- Total experiment duration: {max(all_durations):.1f} seconds")
    report_lines.append(f"- Number of peers: {len(peer_stats)}")

    corr_full, corr_post_warmup = _compute_throughput_correlations(all_metrics)
    if corr_full is not None:
        report_lines.append(f"- Throughput/elapsed correlation (full, should be ~0 for stall-free): {corr_full:.4f}")
        if corr_post_warmup is not None:
            report_lines.append(
                f"- Throughput/elapsed correlation (t>={_WARMUP_EXCLUDE_SECONDS:.0f}s, excl. GPU/model warmup): {corr_post_warmup:.4f}"
            )
        corr_for_judgement = corr_post_warmup if corr_post_warmup is not None else corr_full
        if abs(corr_for_judgement) < 0.1:
            report_lines.append(
                f"  - Interpretation: |correlation|={abs(corr_for_judgement):.4f} < 0.1 なので、"
                "通信中でも計算スループットがほぼ一定であり stall-free 設計が機能していると判断できる。"
            )
        else:
            report_lines.append(
                f"  - Interpretation: |correlation|={abs(corr_for_judgement):.4f} >= 0.1 であり、"
                "経過時間とスループットに無視できない相関がある（通信・マージ処理によるストールの可能性）。"
            )

    if convergence_time_steps and convergence_accuracies:
        report_lines.append("")
        report_lines.append("## Convergence Detail (Merged Model Accuracy over Time)")
        report_lines.append("")
        report_lines.append("| Elapsed (s) | Accuracy (%) |")
        report_lines.append("|-------------|--------------|")
        for t, acc in zip(convergence_time_steps, convergence_accuracies):
            report_lines.append(f"| {t:.1f} | {acc:.1f} |")
        report_lines.append("")
        report_lines.append(f"- First measured accuracy: {convergence_accuracies[0]:.1f}% (at {convergence_time_steps[0]:.1f}s)")
        report_lines.append(f"- Last measured accuracy: {convergence_accuracies[-1]:.1f}% (at {convergence_time_steps[-1]:.1f}s)")
        report_lines.append(f"- Change: {convergence_accuracies[-1] - convergence_accuracies[0]:+.1f} percentage points")
        report_lines.append(f"- Peak accuracy: {max(convergence_accuracies):.1f}%")

    report_path = OUTPUT_DIR / "analysis_report.md"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text("\n".join(report_lines))
    print(f"  Saved: {report_path}")
    return report_path


# ============================================================
# メイン
# ============================================================


def main() -> None:
    """メインエントリポイント。"""
    print("=" * 60)
    print("WAFL-PEFT Experiment Analysis")
    print("=" * 60)

    log_dir = EXPERIMENT_DIR / "logs"
    if not log_dir.exists():
        print(f"Error: Log directory not found: {log_dir}", file=sys.stderr)
        sys.exit(1)

    # メトリクス読み込み
    print(f"\nLoading metrics from {log_dir}...")
    all_metrics = load_metrics(log_dir)
    if not all_metrics:
        print("No metrics found. Nothing to analyze.", file=sys.stderr)
        sys.exit(0)
    print(f"Loaded metrics from {len(all_metrics)} peers")

    # グラフ生成（GPU不要 — メトリクスログのみから作図）
    throughput_path = plot_throughput(all_metrics)
    loss_path = plot_loss_curves(all_metrics)
    scatter_path = plot_per_peer_scatter(all_metrics)

    # 収束性能。評価5はサーバーが実験中にライブ記録した global_eval.log を読む。
    # 評価6は各クライアントが実験終了後に記録したメトリクスログ（"type": "eval"）
    # から読む。どちらもここではGPUを使わない — 評価そのものは実験中/実験後に
    # サーバー・各デバイス側で完了済み
    convergence_path = Path("N/A")
    per_peer_acc_path = Path("N/A")
    time_steps, accuracies = [], []
    per_peer_acc: dict[int, tuple[list[float], list[float]]] = {}

    if _ENABLE_CONVERGENCE:
        time_steps, accuracies = load_global_eval_log(EXPERIMENT_DIR, all_metrics)
        if time_steps and accuracies:
            convergence_path = plot_convergence(time_steps, accuracies)
        else:
            print("\nNo global_eval.log records found. Skipping merged-model convergence.")

        per_peer_acc = load_device_eval_log(all_metrics)
        if per_peer_acc:
            per_peer_acc_path = plot_per_peer_accuracy(per_peer_acc)
        else:
            print("No per-peer post-experiment eval records found. Skipping per-peer accuracy.")
    else:
        print("\n[SKIP] Convergence evaluation disabled (ANALYZE_CONVERGENCE=0).")

    # レポート生成
    report_path = generate_report(
        all_metrics, throughput_path, loss_path, scatter_path, convergence_path,
        time_steps, accuracies, per_peer_acc, per_peer_acc_path,
    )

    print("\n" + "=" * 60)
    print("Analysis complete.")
    print(f"  Throughput plot: {throughput_path}")
    print(f"  Loss curves plot: {loss_path}")
    print(f"  Per-peer scatter plot: {scatter_path}")
    print(f"  Convergence plot: {convergence_path}")
    print(f"  Per-peer accuracy plot: {per_peer_acc_path}")
    print(f"  Report: {report_path}")
    print("=" * 60)


if __name__ == "__main__":
    main()
