#!/usr/bin/env python3
"""GSM8Kバリデーションセットを用いたLoRAモデルのaccuracy評価ロジック。

analyze.py（実験終了後の一括評価）とglobal_eval.py（実験中のリアルタイム
グローバルモデル評価）の両方から共有される評価ロジックを1箇所にまとめる。
4bit量子化モデルのロード・LoRA適用・model.generate()によるgreedy decoding
評価を担う。
"""

import re
from pathlib import Path
from typing import Any

import torch
from peft import LoraConfig, get_peft_model
from transformers import AutoModelForCausalLM, AutoTokenizer, BitsAndBytesConfig

# GSM8K の回答（reasoning ... "#### N"）から最終数値を取り出す正規表現。
# カンマ区切り（例: 1,234）と負号・小数点に対応する
_NUMBER_RE = re.compile(r"-?\d[\d,]*(?:\.\d+)?")


def normalize_number(raw: str) -> str | None:
    """数値文字列を比較用に正規化する（カンマ除去・末尾の.0除去）。

    GSM8K の gold と生成文から取り出した数値を厳密一致で比較するため、
    "1,234" → "1234"、"71.0" → "71" のように表記ゆれを吸収する。
    数値として解釈できない場合は None を返す。
    """
    s = raw.replace(",", "").strip().rstrip(".")
    if not s:
        return None
    try:
        f = float(s)
    except ValueError:
        return None
    # 整数なら整数表記、小数なら余分な末尾0を落とした表記に揃える
    return str(int(f)) if f == int(f) else repr(f)


def extract_gold_answer(answer: str) -> str | None:
    """GSM8K の正解文字列（"reasoning ... #### N"）から最終数値を抽出する。"""
    tail = answer.split("####")[-1]
    m = _NUMBER_RE.search(tail)
    return normalize_number(m.group()) if m else None


def extract_predicted_answer(generated_text: str) -> str | None:
    """モデルの生成文から予測数値を抽出する。

    学習データが "#### N" 形式で終わるため、生成文に "####" が含まれれば
    その直後の数値を優先する。含まれない場合（reasoning の途中で生成が
    打ち切られた等）は、生成文中の最後の数値を予測とみなす（GSM8K の
    標準的な採点でよく用いられる last-number ヒューリスティック）。
    """
    if "####" in generated_text:
        tail = generated_text.split("####")[-1]
        m = _NUMBER_RE.search(tail)
        if m:
            return normalize_number(m.group())
    matches = _NUMBER_RE.findall(generated_text)
    return normalize_number(matches[-1]) if matches else None

# LoRA適用対象モジュール。client.py/analyze.pyのLoRA設定と一致させる必要がある
# （学習時と評価時でtarget_modulesが異なると、保存済みのLoRA state_dictの
# キー名と一致せず、load_state_dict(strict=False)が全パラメータを無視して
# 静かに未学習の初期状態のまま評価してしまう）
LORA_TARGET_MODULES = (
    r"model\.language_model.*(?:self_attn\.(?:q_proj|k_proj|v_proj|o_proj)|"
    r"mlp\.(?:gate_proj|up_proj|down_proj))"
)


def find_all_checkpoint_steps(log_dir: Path) -> list[int]:
    """log_dir配下のpeer_*/weights/にある全チェックポイントステップを抽出（昇順）。"""
    steps: set[int] = set()
    for peer_dir in log_dir.glob("peer_*"):
        wd = peer_dir / "weights"
        if wd.is_dir():
            for f in wd.glob("weights_step_*.pt"):
                step_str = f.stem.replace("weights_step_", "").replace(".pt", "")
                steps.add(int(step_str))
    return sorted(steps)


def load_merged_checkpoint(log_dir: Path, step: int) -> dict[str, torch.Tensor] | None:
    """1ステップのチェックポイントを、log_dir配下の全peer間で平均マージしてロードする。"""
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


def find_gsm8k_parquet_dir(base_dir: Path) -> Path | None:
    """GSM8K parquetファイルがあるディレクトリを探索。"""
    candidates: list[Path] = []
    results_dir = base_dir / "results"
    if results_dir.exists():
        for d in results_dir.iterdir():
            if d.is_dir():
                candidates.append(d / "cache" / "datasets" / "gsm8k" / "main")
    candidates.append(base_dir / "cache" / "datasets" / "gsm8k" / "main")
    for c in candidates:
        if (c / "test-00000-of-00001.parquet").exists():
            return c
    return None


def load_gsm8k_val_data(base_dir: Path, sample_limit: int = 20) -> list[dict[str, Any]]:
    """GSM8Kバリデーションセットを最大sample_limit件だけ読み込む。"""
    from datasets import load_dataset

    parquet_dir = find_gsm8k_parquet_dir(base_dir)
    gsm8k = None
    if parquet_dir is not None:
        train_pf = parquet_dir / "train-00000-of-00001.parquet"
        test_pf = parquet_dir / "test-00000-of-00001.parquet"
        if train_pf.exists() and test_pf.exists():
            gsm8k = load_dataset("parquet", data_files={"train": str(train_pf), "test": str(test_pf)})

    if gsm8k is None:
        try:
            gsm8k = load_dataset("gsm8k", "main")
        except Exception:
            return []

    val_data = gsm8k.get("test", gsm8k.get("train", []))
    if isinstance(val_data, dict):
        val_data = list(val_data.values())[0]
    return list(val_data)[:sample_limit]


def build_lora_model(
    model_id: str,
    lora_rank: int,
    lora_alpha: int,
    device_id: int | None,
    base_dir: Path | None = None,
) -> tuple[Any, Any]:
    """4bit量子化ベースモデルをロードしLoRAを適用する（重みは未ロードの初期状態）。

    base_dir配下にローカルキャッシュ（cache/models/{org}/{name}/）があれば
    そちらを優先する。指定しない、またはローカルキャッシュが無い場合は
    HuggingFace Hubから直接ダウンロードする（client.py の initialize_model()
    と同じ解決順序）。
    """
    model_path_parts = model_id.split("/")
    local_model_path = (
        base_dir / "cache" / "models" / model_path_parts[0] / model_path_parts[1]
        if base_dir is not None
        else None
    )
    load_path = (
        str(local_model_path)
        if local_model_path is not None and (local_model_path / "config.json").exists()
        else model_id
    )

    if device_id is not None:
        torch.cuda.set_device(device_id)
        bnb_config = BitsAndBytesConfig(
            load_in_4bit=True,
            bnb_4bit_quant_type="nf4",
            bnb_4bit_compute_dtype=torch.float16,
            bnb_4bit_use_double_quant=True,
        )
        model = AutoModelForCausalLM.from_pretrained(
            load_path,
            torch_dtype=torch.float16,
            quantization_config=bnb_config,
            device_map={"": device_id},
            trust_remote_code=True,
        )
    else:
        model = AutoModelForCausalLM.from_pretrained(
            load_path,
            torch_dtype=torch.float16,
            device_map="cpu",
            trust_remote_code=True,
        )

    tokenizer = AutoTokenizer.from_pretrained(load_path, trust_remote_code=True)

    lora_config = LoraConfig(
        r=lora_rank,
        lora_alpha=lora_alpha,
        target_modules=LORA_TARGET_MODULES,
        # 学習側（client.py）と揃える。eval は model.eval() で dropout 無効なため
        # accuracy には影響しないが、LoRA 構成の一貫性のため同値にする
        lora_dropout=0.15,
        bias="none",
        task_type="CAUSAL_LM",
    )
    model = get_peft_model(model, lora_config)
    # 事後評価では勾配チェックポイントを使わないため、KVキャッシュを有効化して
    # autoregressive生成を高速化する（学習中クライアントのモデルは
    # gradient_checkpointing により use_cache=False になっているのと対照的）
    model.config.use_cache = True
    if hasattr(model, "generation_config") and model.generation_config is not None:
        model.generation_config.use_cache = True
    model.eval()
    return model, tokenizer


# 1回のgenerate()呼び出しで処理する問題数。全問を1バッチにまとめるとKVキャッシュ
# がバッチサイズに比例して増えOOMになりうるため、client.py の evaluate_batch
# と同様にミニバッチへ分割する
_EVAL_MINI_BATCH_SIZE = 5


def score_generations(
    model: Any,
    tokenizer: Any,
    samples: list[dict[str, str]],
    max_new_tokens: int = 256,
) -> tuple[float, list[bool]]:
    """GSM8K形式のsamplesに対しgreedy生成を行い、accuracy(%)と各問の正解結果を返す。

    重要な採点仕様（従来実装のバグ修正）:
      1. 生成トークンのみをデコードする。model.generate()の出力には入力
         プロンプト（質問文）も含まれるため、プロンプト全体をデコードして
         部分文字列マッチすると、質問文中に偶然含まれる数値まで正解扱いに
         なってしまう（従来はこれが原因で学習が進んでも accuracy が動かず、
         あるpeerでは全期間 accuracy が完全固定になっていた）。左パディング
         時はバッチ内の入力長が揃うため、input_ids.shape[1] 以降が新規生成分。
      2. max_new_tokens を CoT（chain-of-thought）が "#### N" に到達するのに
         十分な長さに取る。従来の32トークンでは reasoning の途中で打ち切られ、
         最終数値が生成文に現れないため採点が常にほぼ0/ノイズになっていた。
      3. 部分文字列一致ではなく、生成文から取り出した数値と gold 数値の
         厳密一致（正規化後）で採点する。

    返値: (accuracy(%), list[bool]) のタプル。list[bool] は samples と同じ
    順序で各問の正解(True/False)を格納する。McNemar 対比較用に per-question
    結果を保持するため、この関数のみこの形式を返す。

    複数問をまとめてバッチ生成し、全問1バッチによるOOMを避けるため
    _EVAL_MINI_BATCH_SIZEずつ分割する。生成タスクでは左パディングが必須の
    ため、tokenizerのpadding_sideを一時的に変更し、呼び出し前の状態へ必ず復元する。
    """
    total = len(samples)
    if total == 0:
        return (0.0, [])

    original_padding_side = tokenizer.padding_side
    original_pad_token = tokenizer.pad_token
    tokenizer.padding_side = "left"
    if tokenizer.pad_token is None:
        tokenizer.pad_token = tokenizer.eos_token

    per_question: list[bool] = []
    try:
        for start in range(0, total, _EVAL_MINI_BATCH_SIZE):
            mini_batch = samples[start:start + _EVAL_MINI_BATCH_SIZE]
            texts = [f"Question: {item['question']}\nAnswer:" for item in mini_batch]
            gold_numbers = [extract_gold_answer(item["answer"]) for item in mini_batch]

            tokens = tokenizer(texts, return_tensors="pt", padding=True).to(model.device)
            prompt_len = tokens["input_ids"].shape[1]
            with torch.no_grad():
                generated = model.generate(
                    **tokens,
                    max_new_tokens=max_new_tokens,
                    do_sample=False,
                    pad_token_id=tokenizer.pad_token_id,
                )
            for i in range(len(mini_batch)):
                # 入力プロンプト分を除いた新規生成トークンのみをデコードする
                gen_only = generated[i][prompt_len:]
                generated_text = tokenizer.decode(gen_only, skip_special_tokens=True)
                pred = extract_predicted_answer(generated_text)
                correct = (
                    pred is not None
                    and gold_numbers[i] is not None
                    and pred == gold_numbers[i]
                )
                per_question.append(correct)
            del tokens, generated
            torch.cuda.empty_cache()
    finally:
        tokenizer.padding_side = original_padding_side
        tokenizer.pad_token = original_pad_token

    correct_count = sum(per_question)
    accuracy = correct_count / total * 100
    return (accuracy, per_question)


def evaluate_weights(
    model: Any,
    tokenizer: Any,
    weights: dict[str, torch.Tensor],
    val_data: list[dict[str, Any]],
    max_new_tokens: int = 256,
) -> tuple[float, list[bool]]:
    """指定したLoRA重みをモデルへ適用し、GSM8Kバリデーションセットでaccuracy(%)を評価する。

    採点ロジックは score_generations に集約している（client.py の非同期評価と
    共通化し、学習時と分析時で採点基準がずれないようにするため）。
    返値は (accuracy(%), list[bool]) のタプル。list[bool] は McNemar 対比較用。
    """
    model.load_state_dict(weights, strict=False)
    return score_generations(model, tokenizer, val_data, max_new_tokens=max_new_tokens)
