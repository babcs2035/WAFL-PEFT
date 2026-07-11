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
    """グラフ1: Token/s vs 時間（実時間軸でのスループット平坦性）。1画像1グラフ。"""
    print("\n=== Evaluation 1: Throughput Flatness ===")

    fig, ax1 = plt.subplots(figsize=(12, 7))
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

    plt.tight_layout()
    out_path = OUTPUT_DIR / "fig01_throughput_flatness.png"
    plt.savefig(str(out_path), dpi=150, bbox_inches="tight")
    plt.close()
    print(f"  Saved: {out_path}")
    return out_path


def plot_cumulative_tokens(all_metrics: dict[int, list[dict[str, Any]]]) -> Path:
    """グラフ1b: 各ノードの累積トークン数の推移。1画像1グラフ。"""
    print("\n=== Evaluation 1b: Cumulative Tokens ===")
    fig, ax = plt.subplots(figsize=(12, 7))
    for peer_id, metrics in sorted(all_metrics.items()):
        peer_times, peer_cumulative = [], []
        for m in metrics:
            if "total_tokens" in m:
                peer_times.append(m["elapsed"])
                peer_cumulative.append(m["total_tokens"])
        if peer_times:
            ax.plot(peer_times, peer_cumulative, alpha=0.7, linewidth=1.2, label=f"Peer {peer_id}")
    ax.set_xlabel("Elapsed Time (seconds)")
    ax.set_ylabel("Cumulative Tokens")
    ax.set_title("Evaluation 1b: Cumulative Token Count per Peer")
    ax.legend(loc="upper left", fontsize="small")
    ax.grid(True, alpha=0.3)
    plt.tight_layout()
    out_path = OUTPUT_DIR / "fig01b_cumulative_tokens.png"
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
    cumulative_path: Path,
    loss_path: Path,
    scatter_path: Path,
    convergence_path: Path,
    convergence_time_steps: list[float],
    convergence_accuracies: list[float],
    per_peer_acc: dict[int, tuple[list[float], list[float]]],
    per_peer_acc_path: Path,
) -> Path:
    """自己完結的な分析レポート（Markdown）を生成する。

    このファイル単体を読むだけで、実験の目的・設定・各指標の意味・結果・解釈が
    すべて分かるように構成する。本文（設定→指標定義→結果→解釈）を先に置き、
    グラフ画像はファイル末尾にまとめて 1 画像 1 グラフで埋め込む。画像リンクは
    レポートと同じ OUTPUT_DIR 内のファイル名のみの相対パスにする（別環境へ
    コピーしてもリンク切れしないように）。
    """
    L: list[str] = []

    # ---- 集計 ----
    peer_stats: dict[int, dict[str, float]] = {}
    for peer_id in sorted(all_metrics.keys()):
        metrics = all_metrics[peer_id]
        losses = [m["loss"] for m in metrics if "loss" in m]
        tok_s = [m["tokens_per_sec"] for m in metrics if m.get("tokens_per_sec", 0) > 0]
        times = [m["elapsed"] for m in metrics if "elapsed" in m]
        stalls = [m["stall_duration"] for m in metrics if "stall_duration" in m]
        step_vals = [m["step"] for m in metrics if m.get("type") == "metric" and "step" in m]
        checkpoint_count = sum(1 for m in metrics if m.get("type") == "checkpoint")
        contact_event_count = sum(1 for m in metrics if m.get("type") == "contact_event")
        acc_first, acc_last, acc_peak = 0.0, 0.0, 0.0
        if peer_id in per_peer_acc and per_peer_acc[peer_id][1]:
            acc_list = per_peer_acc[peer_id][1]
            acc_first, acc_last, acc_peak = acc_list[0], acc_list[-1], max(acc_list)
        peer_stats[peer_id] = {
            "train_steps": int(max(step_vals)) if step_vals else len(metrics),
            "avg_loss": float(np.mean(losses)) if losses else 0.0,
            "min_loss": float(np.min(losses)) if losses else 0.0,
            "avg_tok_s": float(np.mean(tok_s)) if tok_s else 0.0,
            "avg_stall": float(np.mean(stalls)) if stalls else 0.0,
            "checkpoints": checkpoint_count,
            "contacts": contact_event_count,
            "acc_first": acc_first,
            "acc_last": acc_last,
            "acc_peak": acc_peak,
            "duration": float(np.max(times)) if times else 0.0,
        }

    all_avg_losses = [s["avg_loss"] for s in peer_stats.values()]
    all_avg_tok_s = [s["avg_tok_s"] for s in peer_stats.values()]
    all_durations = [s["duration"] for s in peer_stats.values()]
    acc_deltas = [s["acc_last"] - s["acc_first"] for s in peer_stats.values() if s["acc_last"] or s["acc_first"]]
    acc_lasts = [s["acc_last"] for s in peer_stats.values() if s["acc_last"] or s["acc_first"]]
    corr_full, corr_post_warmup = _compute_throughput_correlations(all_metrics)
    corr_judge = corr_post_warmup if corr_post_warmup is not None else corr_full

    # ---- タイトルと概要 ----
    L.append("# WAFL-PEFT 実験分析レポート")
    L.append("")
    L.append(
        "本レポートは WAFL-PEFT（Wireless Ad-hoc Federated Learning + PEFT/LoRA）実験の結果をまとめたものである。"
        "複数の学習デバイス（peer）が中央サーバーを介さず P2P で LoRA アダプタを交換・平均マージしながら、"
        "時変トポロジー（接触パターン）の下で GSM8K 数学文章題を分散学習する。各 peer は Non-IID な"
        "データシャードを持ち、実験時間は接触パターンのタイムラインで時間ベースに制御される。"
    )
    L.append("")
    L.append(f"- 実験名 / ディレクトリ: `{get_experiment_name()}` / `{EXPERIMENT_DIR.name}`")
    L.append(f"- 参加ノード数: {len(all_metrics)}（peer_0 〜 peer_{len(all_metrics) - 1}）")
    L.append(f"- 総メトリクスエントリ数: {sum(len(m) for m in all_metrics.values())}")
    if all_durations:
        L.append(f"- 実験継続時間: {max(all_durations):.0f} 秒")
    L.append("")

    # ---- 総合結果（最重要指標を冒頭に） ----
    L.append("## 総合結果（要点）")
    L.append("")
    if acc_deltas:
        L.append(
            f"- **各ノードの GSM8K accuracy の平均改善（学習前→後）: {np.mean(acc_deltas):+.1f} ポイント**"
            f"（最終 accuracy の平均: {np.mean(acc_lasts):.1f}%）。"
            "これが「各ノードのモデル性能が着実に向上したか」を示す中心指標である。"
        )
    if convergence_accuracies:
        L.append(
            f"- 全ノード平均マージモデルの accuracy: {convergence_accuracies[0]:.1f}% → {convergence_accuracies[-1]:.1f}%"
            f"（ピーク {max(convergence_accuracies):.1f}%）。フェデレーテッド学習全体の収束傾向を示す。"
        )
    L.append(f"- 平均訓練損失（全ノード平均）: {np.mean(all_avg_losses):.4f}")
    L.append(f"- 平均スループット（全ノード平均）: {np.mean(all_avg_tok_s):.1f} tokens/s")
    if corr_judge is not None:
        verdict = "ストールフリー設計が機能" if abs(corr_judge) < 0.1 else "無視できないストールの可能性"
        L.append(
            f"- スループットと経過時間の相関: {corr_judge:+.4f}（|r|<0.1 が目標）→ {verdict}。"
        )
    L.append("")

    # ---- 実験設定 ----
    L.append("## 実験設定")
    L.append("")
    L.append("| 項目 | 値 |")
    L.append("|------|----|")
    L.append(f"| モデル | `{_get('model', 'model_id')}`（4-bit QLoRA） |")
    L.append(f"| LoRA rank / alpha | {_get_int('training', 'lora_rank')} / {_get_int('training', 'lora_alpha')} |")
    L.append(f"| 学習率 | {_get_float('training', 'learning_rate')} |")
    L.append(f"| micro-batch / 勾配累積 | {_get_int('training', 'batch_size')} / {_get_int('training', 'grad_accum_steps', 1)}"
             f"（実効バッチ {_get_int('training', 'batch_size') * _get_int('training', 'grad_accum_steps', 1)}） |")
    L.append(f"| LR warmup steps / min ratio | {_get_int('training', 'lr_warmup_steps', 0)} / {_get_float('training', 'lr_min_ratio', 1.0)} |")
    L.append(f"| 最大系列長 | {_get_int('training', 'max_seq_len')} |")
    L.append(f"| チェックポイント間隔 | {_get_float('training', 'eval_interval_seconds', 60.0):.0f} 秒 |")
    L.append(f"| 接触パターン | `{_get_str('experiment', 'contact_pattern_file')}` |")
    L.append("")

    # ---- 指標の定義 ----
    L.append("## 指標の定義")
    L.append("")
    L.append("- **Train steps**: そのノードが実験時間内に実行した学習ステップ（micro-batch forward/backward）数。")
    L.append("- **Avg / Min Loss**: 学習中の per-token 損失（answer 部分のみ、prompt は -100 でマスク）の平均・最小。"
             "小シャードで過学習すると低くなるため、低いほど良いとは限らない（汎化とのトレードオフ）。")
    L.append("- **Avg Token/s**: 1 ステップあたりの瞬間スループット（そのステップのトークン数 / 所要時間）の平均。")
    L.append("- **Avg Stall (s)**: 1 ステップのうち forward/backward/optimizer 以外（P2P マージ反映・GPU 解放等）に"
             "要した時間の平均。通信・マージが計算をブロックしていないかの指標（小さいほど良い）。")
    L.append("- **Contact Events**: そのノードが接触パターンに従って P2P 接触の開始/終了を経験した回数。")
    L.append("- **Accuracy (first→last)**: そのノード単体の LoRA モデルを、実験終了後に自分の GPU で GSM8K 検証セットに対し"
             "生成評価（`#### 数値` の厳密一致）した accuracy の、学習序盤→終盤の変化。**本実験の主目的の指標**。")
    L.append("")

    # ---- ノード別統計 ----
    L.append("## ノード別統計")
    L.append("")
    L.append("| Peer | Train steps | Checkpoints | Avg Loss | Min Loss | Avg Token/s | Avg Stall (s) "
             "| Contact Events | Accuracy first→last (peak) | Duration (s) |")
    L.append("|------|-------------|-------------|----------|----------|-------------|---------------"
             "|----------------|-----------------------------|--------------|")
    for peer_id in sorted(peer_stats.keys()):
        s = peer_stats[peer_id]
        L.append(
            f"| {peer_id} | {s['train_steps']} | {s['checkpoints']} | {s['avg_loss']:.4f} | {s['min_loss']:.4f} "
            f"| {s['avg_tok_s']:.1f} | {s['avg_stall']:.2f} | {s['contacts']} "
            f"| {s['acc_first']:.1f}%→{s['acc_last']:.1f}% (peak {s['acc_peak']:.1f}%) | {s['duration']:.1f} |"
        )
    L.append("")

    # ---- 全ノード平均マージモデルの収束 ----
    if convergence_time_steps and convergence_accuracies:
        L.append("## 全ノード平均マージモデルの収束（実験中サーバー評価）")
        L.append("")
        L.append("管理サーバーが実験中、一定間隔で全ノードの LoRA 重みを収集・平均マージし GSM8K で評価した記録。")
        L.append("")
        L.append("| 経過時間 (s) | Accuracy (%) |")
        L.append("|-------------|--------------|")
        for t, acc in zip(convergence_time_steps, convergence_accuracies):
            L.append(f"| {t:.1f} | {acc:.1f} |")
        L.append("")
        L.append(f"- 初回 {convergence_accuracies[0]:.1f}%（{convergence_time_steps[0]:.1f}s）→ "
                 f"最終 {convergence_accuracies[-1]:.1f}%（{convergence_time_steps[-1]:.1f}s）、"
                 f"変化 {convergence_accuracies[-1] - convergence_accuracies[0]:+.1f}pt、ピーク {max(convergence_accuracies):.1f}%。")
        L.append("")

    # ---- スループット相関（ストールフリー性）の解釈 ----
    L.append("## スループット平坦性（ストールフリー性）")
    L.append("")
    if corr_full is not None:
        L.append(f"- 経過時間との相関（全期間）: {corr_full:+.4f}")
        if corr_post_warmup is not None:
            L.append(f"- 経過時間との相関（t≥{_WARMUP_EXCLUDE_SECONDS:.0f}s, ウォームアップ除外）: {corr_post_warmup:+.4f}")
        if corr_judge is not None and abs(corr_judge) < 0.1:
            L.append(f"- 解釈: |相関|={abs(corr_judge):.4f} < 0.1 のため、通信中でも計算スループットがほぼ一定であり、"
                     "P2P 通信・マージ処理が学習計算をブロックしていない（stall-free 設計が機能している）と判断できる。")
        elif corr_judge is not None:
            L.append(f"- 解釈: |相関|={abs(corr_judge):.4f} ≥ 0.1 であり、経過時間とスループットに無視できない相関がある"
                     "（通信・マージ処理によるストールの可能性がある）。")
    else:
        L.append("- データ不足のため相関を算出できなかった。")
    L.append("")

    # ---- グラフ（末尾に集約、1画像1グラフ） ----
    L.append("## グラフ")
    L.append("")
    L.append("### 図1: スループット平坦性（Token/s vs 経過時間）")
    L.append("各ノードのステップ毎スループット（薄線）と時間ビン平均（赤線）。水平に平坦なら stall-free。")
    L.append("")
    L.append(f"![Throughput Flatness]({throughput_path.name})")
    L.append("")
    L.append("### 図1b: 累積トークン数 vs 経過時間")
    L.append("各ノードが処理した累積トークン数。傾きがスループット、直線的なら安定した学習進行を意味する。")
    L.append("")
    L.append(f"![Cumulative Tokens]({cumulative_path.name})")
    L.append("")
    L.append("### 図2: ノード別 損失曲線（Loss vs 経過時間）")
    L.append("各ノードの学習損失の推移（薄線）と全体の時間ビン平均（赤線）。")
    L.append("")
    L.append(f"![Loss Curves]({loss_path.name})")
    L.append("")
    L.append("### 図3: 損失 vs スループット 散布図")
    L.append("ノード別（色分け）の損失とスループットの関係。スループットが損失値に依らず一定域にあれば stall-free。")
    L.append("")
    L.append(f"![Loss vs Throughput]({scatter_path.name})")
    L.append("")
    if convergence_path != Path("N/A"):
        L.append("### 図4: 全ノード平均マージモデルの accuracy 収束")
        L.append("実験中にサーバーが評価した、全ノード平均マージモデルの GSM8K accuracy の時間推移。")
        L.append("")
        L.append(f"![Merged Convergence]({convergence_path.name})")
        L.append("")
    if per_peer_acc_path != Path("N/A"):
        L.append("### 図5: ノード別モデルの accuracy 推移（各ノード単体）")
        L.append("実験終了後に各ノードが自分のチェックポイント履歴を評価した、ノード単体モデルの accuracy 推移。"
                 "**各ノードの性能が着実に向上したかを直接示す図**。")
        L.append("")
        L.append(f"![Per-Peer Accuracy]({per_peer_acc_path.name})")
        L.append("")

    report_path = OUTPUT_DIR / "analysis_report.md"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text("\n".join(L))
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

    # グラフ生成（GPU不要 — メトリクスログのみから作図）。1画像1グラフ
    throughput_path = plot_throughput(all_metrics)
    cumulative_path = plot_cumulative_tokens(all_metrics)
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
        all_metrics, throughput_path, cumulative_path, loss_path, scatter_path, convergence_path,
        time_steps, accuracies, per_peer_acc, per_peer_acc_path,
    )

    print("\n" + "=" * 60)
    print("Analysis complete.")
    print(f"  Throughput plot: {throughput_path}")
    print(f"  Cumulative tokens plot: {cumulative_path}")
    print(f"  Loss curves plot: {loss_path}")
    print(f"  Per-peer scatter plot: {scatter_path}")
    print(f"  Convergence plot: {convergence_path}")
    print(f"  Per-peer accuracy plot: {per_peer_acc_path}")
    print(f"  Report: {report_path}")
    print("=" * 60)


if __name__ == "__main__":
    main()
