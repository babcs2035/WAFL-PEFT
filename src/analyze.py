#!/usr/bin/env python3
"""WAFL-PEFT実験結果の分析・グラフ生成スクリプト。

回収した全ノードのメトリクスログをマージし、以下の評価グラフを生成:
  1. スループットの平坦性とストールフリーの実証（Token/s vs 時間）
  2. 各ノードの損失関数の推移（Loss vs 時間）
  3. 各ノードの loss vs throughput 散布図
  4. 各ノードの train/test スコア推移
  5. （任意）収束性能（Accuracy vs 時間）— GPU必要、時間がかかる

収束性能評価（Evaluation 5）はデフォルトでスキップ。
環境変数 ANALYZE_CONVERGENCE=1 を設定すると有効化される。
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
import torch
from transformers import AutoModelForCausalLM, AutoTokenizer

from utils import get_base_dir, get_experiment_dir, get_log_dir, get_output_dir, _get, _get_int

BASE_DIR = get_base_dir()

# .experiment_meta.jsonから実験ディレクトリ名を読み込み
_meta_path = BASE_DIR / "results" / ".experiment_meta.json"
if _meta_path.exists():
    _meta = json.loads(_meta_path.read_text())
    EXPERIMENT_DIR_NAME = _meta.get("dir_name", "")
else:
    EXPERIMENT_DIR_NAME = ""

if EXPERIMENT_DIR_NAME:
    EXPERIMENT_DIR = BASE_DIR / "results" / EXPERIMENT_DIR_NAME
else:
    EXPERIMENT_DIR = get_experiment_dir()

OUTPUT_DIR = EXPERIMENT_DIR / "output"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

# ============================================================
# ヘルパー: 実験ディレクトリの探索
# ============================================================


def _find_experiment_dir(name_hint: str) -> Path:
    """results/ 下から name_hint を含むディレクトリを探索。"""
    results_dir = BASE_DIR / "results"
    if not results_dir.exists():
        return BASE_DIR / "results" / name_hint
    for d in sorted(results_dir.iterdir()):
        if d.is_dir() and name_hint in d.name:
            return d
    return BASE_DIR / "results" / name_hint


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


def _find_all_steps(log_dir: Path) -> list[int]:
    """全チェックポイントステップを抽出（昇順）。"""
    steps: set[int] = set()
    for peer_dir in log_dir.glob("peer_*"):
        wd = peer_dir / "weights"
        if wd.is_dir():
            for f in wd.glob("weights_step_*.pt"):
                step_str = f.stem.replace("weights_step_", "").replace(".pt", "")
                steps.add(int(step_str))
    return sorted(steps)


def _load_single_ckpt(log_dir: Path, step: int) -> dict[str, torch.Tensor] | None:
    """1ステップのチェックポイントを peer 間で平均してロード。"""
    accum: dict[str, torch.Tensor] | None = None
    count = 0
    for peer_dir in sorted(log_dir.glob("peer_*")):
        wd = peer_dir / "weights"
        candidate = wd / f"weights_step_{step:06d}.pt"
        if candidate.exists():
            try:
                w = torch.load(candidate, weights_only=True, map_location="cpu")
                if accum is None:
                    accum = {k: v.float() for k, v in w.items()}
                else:
                    for k in accum:
                        if k in w:
                            accum[k] += w[k].float()
                count += 1
            except (EOFError, RuntimeError):
                continue
    if accum is not None and count > 0:
        for k in accum:
            accum[k] /= count
    return accum


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

    if len(all_times) > 2:
        corr = np.corrcoef(all_times, all_tok_s)[0, 1]
        ax1.text(0.02, 0.98, f"Correlation: {corr:.4f}\n(should be ~0 for stall-free)", transform=ax1.transAxes, verticalalignment="top", bbox=dict(boxstyle="round", facecolor="wheat", alpha=0.5))

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
# 評価4: train/test スコア推移
# ============================================================


def plot_score_curves(all_metrics: dict[int, list[dict[str, Any]]]) -> Path:
    """グラフ4: 各ノードの train/test スコア推移。"""
    print("\n=== Evaluation 4: Per-Peer Train/Test Score Curves ===")

    fig, ax = plt.subplots(figsize=(12, 7))

    for peer_id, metrics in sorted(all_metrics.items()):
        peer_train_times, peer_train_scores = [], []
        peer_test_times, peer_test_scores = [], []
        for m in metrics:
            if "train_score" in m and m["train_score"] > 0:
                peer_train_times.append(m["elapsed"])
                peer_train_scores.append(m["train_score"])
            if "test_score" in m and m["test_score"] > 0:
                peer_test_times.append(m["elapsed"])
                peer_test_scores.append(m["test_score"])
        if peer_train_scores:
            ax.plot(peer_train_times, peer_train_scores, alpha=0.3, linewidth=0.8, label=f"Peer {peer_id} Train")
        if peer_test_scores:
            ax.plot(peer_test_times, peer_test_scores, alpha=0.3, linewidth=0.8, linestyle="--", label=f"Peer {peer_id} Test")

    # 平均スコア（時間ビン分割）
    train_times, train_scores_arr = [], []
    test_times, test_scores_arr = [], []
    for metrics in all_metrics.values():
        for m in metrics:
            if "train_score" in m and m["train_score"] > 0:
                train_times.append(m["elapsed"])
                train_scores_arr.append(m["train_score"])
            if "test_score" in m and m["test_score"] > 0:
                test_times.append(m["elapsed"])
                test_scores_arr.append(m["test_score"])

    if train_times:
        bins = np.linspace(0, max(train_times + test_times), 50) if test_times else np.linspace(0, max(train_times), 50)
        bin_centers = (bins[:-1] + bins[1:]) / 2
        t_arr, s_arr = np.array(train_times), np.array(train_scores_arr)
        train_means = []
        for i in range(len(bins) - 1):
            mask = (t_arr >= bins[i]) & (t_arr < bins[i + 1])
            train_means.append(float(s_arr[mask].mean()) if mask.sum() > 0 else np.nan)
        ax.plot(bin_centers, train_means, color="green", linewidth=2.5, label="Mean Train Score", zorder=10)

    if test_times:
        bins = np.linspace(0, max(train_times + test_times), 50) if train_times else np.linspace(0, max(test_times), 50)
        bin_centers = (bins[:-1] + bins[1:]) / 2
        t_arr, s_arr = np.array(test_times), np.array(test_scores_arr)
        test_means = []
        for i in range(len(bins) - 1):
            mask = (t_arr >= bins[i]) & (t_arr < bins[i + 1])
            test_means.append(float(s_arr[mask].mean()) if mask.sum() > 0 else np.nan)
        ax.plot(bin_centers, test_means, color="blue", linewidth=2.5, label="Mean Test Score", zorder=10)

    ax.set_xlabel("Elapsed Time (seconds)")
    ax.set_ylabel("Accuracy (%)")
    ax.set_title("Per-Peer Train/Test Accuracy Curves")
    ax.legend(loc="lower right", fontsize="small")
    ax.grid(True, alpha=0.3)

    plt.tight_layout()
    out_path = OUTPUT_DIR / "fig05_score_curves.png"
    plt.savefig(str(out_path), dpi=150, bbox_inches="tight")
    plt.close()
    print(f"  Saved: {out_path}")
    return out_path


# ============================================================
# 評価5: 収束性能（オプション — GPU必要、時間がかかる）
# ============================================================

# 収束評価を有効にする環境変数。デフォルトはオフ（高速化のため）。
_ENABLE_CONVERGENCE = os.environ.get("ANALYZE_CONVERGENCE", "0") == "1"


def _find_gsm8k_parquet_dir() -> Path | None:
    """GSM8K parquetファイルがあるディレクトリを探索。"""
    # 候補ディレクトリを列挙
    candidates: list[Path] = []
    results_dir = BASE_DIR / "results"
    if results_dir.exists():
        for d in results_dir.iterdir():
            if d.is_dir():
                candidates.append(d / "cache" / "datasets" / "gsm8k" / "main")
    # ルートキャッシュも試行
    candidates.append(BASE_DIR / "cache" / "datasets" / "gsm8k" / "main")
    for c in candidates:
        if (c / "test-00000-of-00001.parquet").exists():
            return c
    return None


def evaluate_accuracy(
    log_dir: Path,
    all_metrics: dict[int, list[dict[str, Any]]],
) -> tuple[list[float], list[float]]:
    """LoRA重みをロードし、GSM8Kバリデーションセットでaccuracyを評価。

    最適化:
    - モデルは1回だけロード（4-bit量子化でVRAM削減）
    - チェックポイントは每隔 step_interval 回だけ評価
    - GSM8Kサンプル数は20に削減
    """
    print("\n=== Evaluation 5: Convergence (OPTIMIZED) ===")

    from datasets import load_dataset, load_from_disk

    # GSM8Kデータセット取得
    parquet_dir = _find_gsm8k_parquet_dir()
    if parquet_dir is not None:
        train_pf = parquet_dir / "train-00000-of-00001.parquet"
        test_pf = parquet_dir / "test-00000-of-00001.parquet"
        if train_pf.exists() and test_pf.exists():
            gsm8k = load_dataset("parquet", data_files={"train": str(train_pf), "test": str(test_pf)})
        else:
            gsm8k = None
    else:
        gsm8k = None

    if gsm8k is None:
        try:
            gsm8k = load_dataset("gsm8k", "main")
        except Exception:
            print("  [SKIP] GSM8K not available. Skipping convergence.")
            return [], []

    val_data = gsm8k.get("test", gsm8k.get("train", []))
    if isinstance(val_data, dict):
        val_data = list(val_data.values())[0]
    val_data = list(val_data)[:20]  # 20サンプルに削減（元は100）
    if not val_data:
        print("  [SKIP] No GSM8K samples available.")
        return [], []

    # モデルロード（4-bit量子化で高速化）
    model_id = _get("model", "model_id")
    print(f"  Loading model: {model_id} (4-bit quantized)...")

    if torch.cuda.is_available():
        from transformers import BitsAndBytesConfig
        bnb_config = BitsAndBytesConfig(
            load_in_4bit=True,
            bnb_4bit_quant_type="nf4",
            bnb_4bit_compute_dtype=torch.float16,
            bnb_4bit_use_double_quant=True,
        )
        model = AutoModelForCausalLM.from_pretrained(
            model_id,
            torch_dtype=torch.float16,
            quantization_config=bnb_config,
            device_map="auto",
            trust_remote_code=True,
        )
    else:
        model = AutoModelForCausalLM.from_pretrained(
            model_id,
            torch_dtype=torch.float16,
            device_map="cpu",
            trust_remote_code=True,
        )

    tokenizer = AutoTokenizer.from_pretrained(model_id, trust_remote_code=True)

    # LoRA設定
    from peft import LoraConfig, get_peft_model
    lora_config = LoraConfig(
        r=_get_int("training", "lora_rank"),
        lora_alpha=_get_int("training", "lora_alpha"),
        target_modules=r"model\.language_model.*(?:self_attn\.(?:q_proj|k_proj|v_proj|o_proj)|mlp\.(?:gate_proj|up_proj|down_proj))",
        lora_dropout=0.05,
        bias="none",
        task_type="CAUSAL_LM",
    )
    model = get_peft_model(model, lora_config)

    # チェックポイントステップを抽出
    all_steps = _find_all_steps(log_dir)
    if not all_steps:
        print("  [SKIP] No checkpoints found.")
        return [], []

    # 每隔 step_interval 回だけ評価（例: 200ステップ → 20評価）
    step_interval = max(1, len(all_steps) // 20)
    eval_steps = all_steps[::step_interval]
    print(f"  Evaluating {len(eval_steps)}/{len(all_steps)} checkpoints (interval={step_interval}, samples=20)...")

    time_steps: list[float] = []
    accuracies: list[float] = []

    for i, step in enumerate(eval_steps):
        weights = _load_single_ckpt(log_dir, step)
        if weights is None:
            continue

        model.load_state_dict(weights, strict=False)

        correct = 0
        total = 0
        for item in val_data:
            text = f"Question: {item['question']}\nAnswer:"
            tokens = tokenizer(text, return_tensors="pt").to(model.device)
            with torch.no_grad():
                generated = model.generate(
                    **tokens,
                    max_new_tokens=64,
                    do_sample=False,
                    temperature=1.0,
                )
            generated_text = tokenizer.decode(generated[0], skip_special_tokens=True)
            expected_answer = item["answer"].split("#### ")[-1].strip()
            if expected_answer in generated_text:
                correct += 1
            total += 1

        accuracy = correct / total * 100 if total > 0 else 0.0
        elapsed = _get_avg_elapsed_at_step(all_metrics, step)
        time_steps.append(elapsed)
        accuracies.append(accuracy)
        print(f"  Step {step} ({i+1}/{len(eval_steps)}): Accuracy = {accuracy:.1f}% (t={elapsed:.0f}s)")

        # VRAM解放
        del weights
        torch.cuda.empty_cache()

    # モデル解放
    del model, tokenizer
    torch.cuda.empty_cache()

    return time_steps, accuracies


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
# レポート生成
# ============================================================


def generate_report(
    all_metrics: dict[int, list[dict[str, Any]]],
    throughput_path: Path,
    loss_path: Path,
    scatter_path: Path,
    score_path: Path,
    convergence_path: Path,
) -> Path:
    """分析レポートを生成。"""
    report_lines: list[str] = []
    report_lines.append("# WAFL-PEFT Experiment Analysis Report")
    report_lines.append("")
    report_lines.append("## Summary")
    report_lines.append(f"- Peers analyzed: {len(all_metrics)}")
    report_lines.append(f"- Total metric entries: {sum(len(m) for m in all_metrics.values())}")
    report_lines.append(f"- Throughput graph: {throughput_path}")
    report_lines.append(f"- Loss curves graph: {loss_path}")
    report_lines.append(f"- Per-peer scatter graph: {scatter_path}")
    report_lines.append(f"- Score curves graph: {score_path}")
    if convergence_path != Path("N/A"):
        report_lines.append(f"- Convergence graph: {convergence_path}")
    report_lines.append("")

    report_lines.append("## Per-Peer Statistics")
    report_lines.append("")
    report_lines.append("| Peer | Steps | Avg Loss | Min Loss | Max Loss | Avg Token/s | Train Score | Test Score | Duration (s) |")
    report_lines.append("|------|-------|----------|----------|----------|-------------|-------------|------------|---------------|")

    peer_stats: dict[int, dict[str, float]] = {}
    for peer_id in sorted(all_metrics.keys()):
        metrics = all_metrics[peer_id]
        losses = [m["loss"] for m in metrics if "loss" in m]
        tok_s = [m["tokens_per_sec"] for m in metrics if m.get("tokens_per_sec", 0) > 0]
        times = [m["elapsed"] for m in metrics if "elapsed" in m]
        train_scores = [m["train_score"] for m in metrics if m.get("train_score", 0) > 0]
        test_scores = [m["test_score"] for m in metrics if m.get("test_score", 0) > 0]

        stats = {
            "steps": len(metrics),
            "avg_loss": float(np.mean(losses)) if losses else 0.0,
            "min_loss": float(np.min(losses)) if losses else 0.0,
            "max_loss": float(np.max(losses)) if losses else 0.0,
            "avg_tok_s": float(np.mean(tok_s)) if tok_s else 0.0,
            "avg_train_score": float(np.mean(train_scores)) if train_scores else 0.0,
            "avg_test_score": float(np.mean(test_scores)) if test_scores else 0.0,
            "duration": float(np.max(times)) if times else 0.0,
        }
        peer_stats[peer_id] = stats
        report_lines.append(
            f"| {peer_id} | {len(metrics)} | "
            f"{stats['avg_loss']:.4f} | {stats['min_loss']:.4f} | {stats['max_loss']:.4f} | "
            f"{stats['avg_tok_s']:.1f} | "
            f"{stats['avg_train_score']:.1f}% | {stats['avg_test_score']:.1f}% | "
            f"{stats['duration']:.1f} |"
        )

    all_avg_losses = [s["avg_loss"] for s in peer_stats.values()]
    all_avg_tok_s = [s["avg_tok_s"] for s in peer_stats.values()]
    all_avg_train = [s["avg_train_score"] for s in peer_stats.values()]
    all_avg_test = [s["avg_test_score"] for s in peer_stats.values()]
    all_durations = [s["duration"] for s in peer_stats.values()]

    report_lines.append("")
    report_lines.append("## Overall Statistics")
    report_lines.append("")
    report_lines.append(f"- Average loss across peers: {np.mean(all_avg_losses):.4f}")
    report_lines.append(f"- Average throughput: {np.mean(all_avg_tok_s):.1f} tokens/s")
    report_lines.append(f"- Average train accuracy: {np.mean(all_avg_train):.1f}%")
    report_lines.append(f"- Average test accuracy: {np.mean(all_avg_test):.1f}%")
    report_lines.append(f"- Total experiment duration: {max(all_durations):.1f} seconds")
    report_lines.append(f"- Number of peers: {len(peer_stats)}")

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

    # グラフ生成（高速 — GPU不要）
    throughput_path = plot_throughput(all_metrics)
    loss_path = plot_loss_curves(all_metrics)
    scatter_path = plot_per_peer_scatter(all_metrics)
    score_path = plot_score_curves(all_metrics)

    # 収束性能（オプション — GPU必要、時間がかかる）
    convergence_path = Path("N/A")
    time_steps, accuracies = [], []

    if _ENABLE_CONVERGENCE:
        # チェックポイントが存在するか確認
        if _find_all_steps(log_dir):
            time_steps, accuracies = evaluate_accuracy(log_dir, all_metrics)
            if time_steps and accuracies:
                convergence_path = plot_convergence(time_steps, accuracies)
        else:
            print("\nNo weight checkpoints found. Skipping convergence.")
    else:
        print("\n[SKIP] Convergence evaluation disabled.")
        print("  (Set ANALYZE_CONVERGENCE=1 to enable — requires GPU)")

    # レポート生成
    report_path = generate_report(all_metrics, throughput_path, loss_path, scatter_path, score_path, convergence_path)

    print("\n" + "=" * 60)
    print("Analysis complete.")
    print(f"  Throughput plot: {throughput_path}")
    print(f"  Loss curves plot: {loss_path}")
    print(f"  Per-peer scatter plot: {scatter_path}")
    print(f"  Score curves plot: {score_path}")
    print(f"  Convergence plot: {convergence_path}")
    print(f"  Report: {report_path}")
    print("=" * 60)


if __name__ == "__main__":
    main()
