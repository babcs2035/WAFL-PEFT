#!/usr/bin/env python3
"""WAFL-PEFT学習デバイスクライアント。

4スレッド並列アーキテクチャ:
  Thread 1: 管理サーバー交信リスナー（制御プレーン）
  Thread 2: P2P生TCP交換・マージ層（データプレーン）
  Thread 3: LoRA訓練ループ（計算プレーン）
  Thread 4: 非同期ディスク書き出し層（ロギングプレーン）

accuracy 評価は学習ノードでは一切行わない（実験中も実験後も）。学習ノードは訓練・
P2Pマージ・チェックポイント保存に専念する。これは (1) 学習中モデルは
gradient_checkpointing により use_cache=False で generate() 評価が GPU/GIL を長時間
占有し訓練を巻き込むストールを起こすこと、(2) 実験後評価も学習ノードの VRAM を消費し、
外部 GPU 競合下では OOM や逼迫を招くこと、の 2 点を避けるためである。

代わりに評価は「学習と別のハードウェア」に分離する:
  - 実験中の収束傾向: 管理サーバー（server.py の GlobalEval スレッド）がマージモデルを
    一定間隔で評価する。
  - 各 peer の checkpoint 別 accuracy: config/hosts.eval.txt の評価専用ホストで動く
    eval_worker.py が、担当 peer の logs/weights/ を rsync で随時取得し評価する。
このクライアントは実験終了時に logs/weights/.training_done マーカーを書くだけで、
評価ワーカーがそれを検出して残りを評価し、管理サーバーへ完了を通知する。
（run_post_experiment_evaluation / notify_server_evaluation_complete は旧アーキテクチャの
自己評価用関数で、現在は呼び出されない。評価を学習ノード側へ戻す場合の参照用に残置。）

メトリクスは訓練ループ内でキューへputし、ロガスレッドが非同期にファイルへ書き出す。
各ステップで loss, throughput をログ出力する。

ログのprefixは "[+{経過秒}s]\t[Peer {peer_id}]\t[<スレッド識別子>]\t<本文>" の
tab区切り形式で、フィールドを揃えて読みやすくする。時刻は実時刻(HH:MM:SS)ではなく
「実験開始からの経過秒数」を表す（本アプリは時間ベースで実験を制御するため、
経過時間の方がログ追跡に有用。実験開始前は "init" を表示）。
  [Main]        : メインスレッド（モデル・データ初期化，起動/終了ログ）
  [T1:Listener] : Thread 1（管理サーバー交信リスナー）
  [T2:P2P]      : Thread 2（P2P重み交換・マージ）
  [T3:Train]    : Thread 3（LoRA訓練ループ）
  [T4:Logger]   : Thread 4（非同期ディスク書き出し）
"""

import json
import math
import os
import queue
import random
import socket
import sys
import threading
import time
from pathlib import Path
from typing import Any

# CUDA メモリアロケータの断片化を軽減（torch インポート前に設定する必要がある）
os.environ.setdefault("PYTORCH_CUDA_ALLOC_CONF", "expandable_segments:True")

import torch
import torch.nn.functional as F
import bitsandbytes as bnb
from datasets import Dataset
from peft import LoraConfig, get_peft_model
from transformers import AutoConfig, AutoModelForCausalLM, AutoTokenizer, BitsAndBytesConfig

from utils import get_base_dir, get_hosts_path, get_log_dir, _get, _get_float, _get_int, _get_str

# ============================================================
# グローバル設定
# ============================================================

# chunked cross-entropy のトークン分割サイズ。巨大 vocab (262144) の logits を
# fp32 で全 materialize すると 1 サンプルでも数百 MB の transient になり、外部 GPU
# 競合下で OOM を誘発する。トークン次元をこのサイズで分割して逐次に CE を計算し、
# fp32 化・log_softmax のピークを chunk 分に抑える（保存すべき勾配は logits 全体の
# fp16 のみ）。値が小さいほど省メモリ・iteration 増、大きいほど逆
_CE_CHUNK_TOKENS = 64


def _memory_efficient_causal_lm_loss(
    logits: "torch.Tensor", labels: "torch.Tensor", ignore_index: int = -100
) -> "torch.Tensor":
    """巨大 vocab 向けの省メモリ Causal LM 損失。

    transformers の ForCausalLMLoss と同一の値を返す（1 トークンぶん右シフトし、
    ignore_index を除いた answer トークンでの平均 cross-entropy）。ただし logits を
    fp32 で一括 materialize せず、トークン次元を _CE_CHUNK_TOKENS ごとに分割して
    逐次に sum を積み上げ、最後に非マスクトークン数で割る。autograd は各 chunk を
    通して logits へ勾配を流すため、学習結果は一括計算と一致する。
    """
    # 標準的な causal shift: 位置 t の logits で t+1 のラベルを予測する
    shift_logits = logits[:, :-1, :]
    shift_labels = labels[:, 1:]
    vocab_size = shift_logits.size(-1)
    flat_logits = shift_logits.reshape(-1, vocab_size)
    flat_labels = shift_labels.reshape(-1)

    loss_sum = flat_logits.new_zeros((), dtype=torch.float32)
    token_count = (flat_labels != ignore_index).sum()
    for start in range(0, flat_logits.size(0), _CE_CHUNK_TOKENS):
        chunk_logits = flat_logits[start : start + _CE_CHUNK_TOKENS].float()
        chunk_labels = flat_labels[start : start + _CE_CHUNK_TOKENS]
        loss_sum = loss_sum + F.cross_entropy(
            chunk_logits, chunk_labels, ignore_index=ignore_index, reduction="sum"
        )
    # 非マスクトークンが 0 の異常系でのゼロ除算を避ける
    return loss_sum / token_count.clamp(min=1).to(loss_sum.dtype)

# コンテナ内では /app がプロジェクトルート（ホストの DEPLOY_DIR にマッピング）
BASE_DIR = Path("/app")
SERVER_HOST = _get_str("server", "server_host")  # 管理サーバーのホスト名（settings.json で定義）
SERVER_PORT = _get_int("server", "server_port")
P2P_PORT = _get_int("communication", "client_p2p_port")
PEER_ID = int(os.environ.get("PEER_ID", "0"))

LOG_DIR = get_log_dir()
LOG_DIR.mkdir(parents=True, exist_ok=True)

WEIGHT_DIR = LOG_DIR / "weights"
WEIGHT_DIR.mkdir(parents=True, exist_ok=True)


# 実験開始のwall-clock時刻（time.time()）。experiment_start受信時にセットされ、
# 以降ログのprefix時刻は「実験開始からの経過秒数」を表す。実験開始前（モデル
# ロード等）はNoneのままで、prefixには "init" を表示する
_EXP_START_WALL: float | None = None


def _now() -> str:
    """ログprefix用の時刻文字列を返す。

    実験開始後は「実験開始からの経過秒数」を +NNNN.Ns 形式（固定幅9文字）で返す。
    本アプリは時間ベースで実験を制御するため、実時刻(HH:MM:SS)より経過時間の方が
    ログを追う上で有用。実験開始前は "init" を返す。
    """
    if _EXP_START_WALL is None:
        return f"{'init':^9}"
    return f"+{time.time() - _EXP_START_WALL:7.1f}s"


def _eta_str(seconds: float) -> str:
    """秒数を人間 readable な文字列へ変換（例: 3700s → 1.0h）."""
    if seconds < 60:
        return f"{seconds:.0f}s"
    if seconds < 3600:
        return f"{seconds/60:.0f}m{int(seconds%60):02d}s"
    return f"{seconds/3600:.1f}h"


def resolve_hosts() -> dict[int, str]:
    """hosts.txtからpeer_id -> IPのマッピングを構築。"""
    hosts_path = get_hosts_path()
    mapping: dict[int, str] = {}
    if hosts_path.exists():
        for i, line in enumerate(hosts_path.read_text().strip().splitlines()):
            ip = line.strip()
            if ip and not ip.startswith("#"):
                mapping[i] = ip
    return mapping


def set_deterministic_seed(peer_id: int) -> int:
    """peer固有だが決定論的なシードをrandom/torchへ設定する。

    settings.jsonのdata.seedをpeer_idでオフセットすることで、peerごとに
    異なるが再現可能なRNG系列を持たせる（BlazeFL的なRNG分離の簡易版）。
    LoRA行列の初期化やデータローディング順序の再現性はこのシード設定に依存するため、
    モデル初期化より前に呼ぶ必要がある。
    """
    base_seed = _get_int("data", "seed", 42)
    seed = base_seed + peer_id
    random.seed(seed)
    torch.manual_seed(seed)
    if torch.cuda.is_available():
        torch.cuda.manual_seed_all(seed)
    return seed

# ============================================================
# 共有状態（スレッド間データ構造）
# ============================================================


class SharedState:
    """4スレッド間で共有される状態。

    - peer_whitelist: Thread 1が更新、Thread 2が参照。現在接触中のpeer_idの集合。
      サーバーからstart/endイベントを受信するたびに増減する（remaining_timeに
      基づく自動失効の仕組みは廃止し、接触の終了は必ずサーバーからの明示的な
      endイベントで制御される）
    - shadow_weights: Thread 2・Thread 3の双方が読み取り専用で参照するCPU上のコピー。
      実体の更新は常にThread 3が行う（Thread 2はマージ計算のみでmodelには触れない）。
      管理サーバーのGlobalEvalスレッドがSSH+rsyncで収集するのもこのディスク上の
      チェックポイント（Thread 3が定期保存する重み）であり、評価はクライアント側では
      一切行わない
    - merge_queue: Thread 2がマージ済み重みを書き込み、Thread 3がステップ境界で消費して
      model本体に反映する（Thread 2がmodel.load_state_dict()を直接呼ぶと、Thread 3の
      forward/backward/optimizer.stepの実行中にパラメータが書き換わるデータ競合が
      発生するため、反映は必ずThread 3側で行う）
    - metrics_queue: Thread 3が書き込み、Thread 4が消費（キュー満杯時はブロック）
    - checkpoint_elapsed: Thread 3がチェックポイント保存時に書き込む
      {step: elapsed_time} の対応表。実験終了後にmain()（訓練スレッド join 後、
      他スレッドと並行実行されない）が読み取り、実験後評価の結果を元の
      経過時間軸に正しく紐づけるために使う（Thread 3は既に終了しているため
      ロック不要）
    """

    def __init__(self) -> None:
        self.peer_whitelist: set[int] = set()
        self.whitelist_lock = threading.Lock()

        self.shadow_weights: dict[str, torch.Tensor] | None = None
        self.weights_lock = threading.Lock()

        # shadow_weights が実際に更新された回数（Thread 3 が勾配累積境界で更新する
        # たびに +1）。Thread 2 はこの版番号の変化を見て「重みが変わった時だけ」
        # シリアライズ・送信する。これがないと Thread 2 はループ毎に 48MB の重みを
        # torch.save で再シリアライズし続け、GIL を保持して Thread 3 の forward/backward を
        # 断続的に数秒ブロックする（step 時間が 0.7s→7s に跳ねる原因になっていた）
        self.shadow_version = 0

        self.merge_queue: queue.Queue[dict[str, torch.Tensor]] = queue.Queue(maxsize=32)

        self.metrics_queue: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=8192)

        self.checkpoint_elapsed: dict[int, float] = {}

        self.step_lock = threading.Lock()
        self.current_step: int = 0

        self.experiment_running = threading.Event()
        self.experiment_start_time: float = 0.0
        self.experiment_duration: float = 0.0
        self.elapsed_time: float = 0.0

        # サーバーへのReady送信を同期
        self.ready_to_send = threading.Event()

        self.running = True


# ============================================================
# モデル・データセットの初期化
# ============================================================


def _prepare_kbit_model_for_training(model: Any, use_gradient_checkpointing: bool = True) -> Any:
    """4-bit量子化モデルの学習準備（peft.prepare_model_for_kbit_training()の代替実装）。

    元関数は非4bitパラメータ（float16/bfloat16 かつ Params4bit でないもの）を
    無条件にfloat32へキャストする。Gemma4の embed_tokens_per_layer
    （Per-Layer Embedding、約4.7GB）のような巨大な非LoRA対象パラメータまで
    float32化すると一時的に約9GBを追加要求しVRAM不足でOOMになるため、
    LayerNormの重み等（常に1次元）のみに限定してfloat32へキャストする。
    """
    for param in model.parameters():
        param.requires_grad = False

    for param in model.parameters():
        if (
            param.dtype in (torch.float16, torch.bfloat16)
            and param.__class__.__name__ != "Params4bit"
            and param.dim() == 1
        ):
            param.data = param.data.to(torch.float32)

    if use_gradient_checkpointing:
        model.enable_input_require_grads()
        model.gradient_checkpointing_enable()

    return model


def initialize_model() -> tuple[Any, Any]:
    """LoRA付きモデルを初期化（AutoModelで自動判定）。

    ローカルの cache/models/ にモデルがあればそちらを優先し、
    なければ HuggingFace Hub からダウンロードする。
    """
    import sys

    model_id = _get("model", "model_id")
    model_path_parts = model_id.split("/")
    local_model_path = BASE_DIR / "cache" / "models" / model_path_parts[0] / model_path_parts[1]

    if local_model_path.exists() and (local_model_path / "config.json").exists():
        print(f"[{_now()}]\t[Peer {PEER_ID}]\t[Main       ]\t  Using local model from {local_model_path}", flush=True)
    else:
        print(f"[{_now()}]\t[Peer {PEER_ID}]\t[Main       ]\t  Local model not found, downloading from {model_id}...", flush=True)
        sys.stdout.flush()

    # GPU が存在する場合は 4-bit QLoRA (NF4) でモデルを量子化してロードする。
    # 5.1B パラメータを float16 でロードすると ~10.2 GiB を消費し 12GB VRAM に入らないため、
    # 4-bit 量子化で ~2.5 GiB に圧縮する。LoRA アダプタは float16 で学習する。
    # GPU が存在しない場合は float16 のまま CPU でロードする。
    if torch.cuda.is_available():
        bnb_config = BitsAndBytesConfig(
            load_in_4bit=True,
            bnb_4bit_quant_type="nf4",
            bnb_4bit_compute_dtype=torch.float16,
            bnb_4bit_use_double_quant=True,
        )
        _device_map = "auto"
        _quantization_config = bnb_config
        print(f"[{_now()}]\t[Peer {PEER_ID}]\t[Main       ]\t  Using 4-bit NF4 quantization (QLoRA)", flush=True)
    else:
        _device_map = "cpu"
        _quantization_config = None
        print(f"[{_now()}]\t[Peer {PEER_ID}]\t[Main       ]\t  No GPU found, loading in float16 on CPU", flush=True)

    model = AutoModelForCausalLM.from_pretrained(
        str(local_model_path) if local_model_path.exists() else model_id,
        torch_dtype=torch.float16,
        device_map=_device_map,
        quantization_config=_quantization_config,
        trust_remote_code=True,
    )
    print(f"[{_now()}]\t[Peer {PEER_ID}]\t[Main       ]\t  Model weights loaded, device={next(model.parameters()).device}", flush=True)
    sys.stdout.flush()

    lora_config = LoraConfig(
        r=_get_int("training", "lora_rank"),
        lora_alpha=_get_int("training", "lora_alpha"),
        target_modules=r"model\.language_model.*(?:self_attn\.(?:q_proj|k_proj|v_proj|o_proj)|mlp\.(?:gate_proj|up_proj|down_proj))",
        # 過学習抑制の正則化レバー。gentle LR 減衰（lr_min 0.5）でも一部ノードで last≪peak の
        # 過学習が残ったため 0.10→0.15 に引き上げる（VRAM 中立で外部競合下でも安全）
        lora_dropout=0.15,
        bias="none",
        task_type="CAUSAL_LM",
    )

    print(f"[{_now()}]\t[Peer {PEER_ID}]\t[Main       ]\t  Applying LoRA (rank={_get_int('training', 'lora_rank')}, alpha={_get_int('training', 'lora_alpha')})...", flush=True)
    sys.stdout.flush()
    # 4-bit 量子化モデルでは学習前の準備（ベースモデル凍結・gradient checkpointing）が必須。
    if _quantization_config is not None:
        model = _prepare_kbit_model_for_training(model, use_gradient_checkpointing=True)
    else:
        model.enable_input_require_grads()
        model.gradient_checkpointing_enable()
    model = get_peft_model(model, lora_config)
    model.print_trainable_parameters()

    # device_map="auto" + PEFT の組み合わせで LoRA パラメータが meta デバイスに
    # 残ることがある（PEFT の既知の挙動）。meta テンソルは実データを持たないため
    # forward/backward/clone が全て失敗する。ここで CPU/GPU へ実体化する。
    _fallback_device = next(
        (p.device for p in model.parameters() if p.device.type != "meta"),
        torch.device("cpu"),
    )
    for module in model.modules():
        for pname, param in list(module.named_parameters(recurse=False)):
            if param.device.type != "meta":
                continue
            new_data = torch.empty(param.shape, dtype=param.dtype, device=_fallback_device)
            # lora_B はゼロ初期化。lora_A は kaiming_uniform（2D 以上のみ）。
            # 1D テンソル（バイアス等）はゼロ初期化で安全に代替。
            if "lora_b" in pname.lower() or new_data.dim() < 2:
                torch.nn.init.zeros_(new_data)
            else:
                torch.nn.init.kaiming_uniform_(new_data, a=math.sqrt(5))
            setattr(module, pname, torch.nn.Parameter(new_data, requires_grad=param.requires_grad))
    print(f"[{_now()}]\t[Peer {PEER_ID}]\t[Main       ]\t  LoRA applied, target_device={_fallback_device}", flush=True)

    print(f"[{_now()}]\t[Peer {PEER_ID}]\t[Main       ]\t  Loading tokenizer...", flush=True)
    sys.stdout.flush()
    tokenizer = AutoTokenizer.from_pretrained(
        str(local_model_path) if local_model_path.exists() else model_id,
    )
    print(f"[{_now()}]\t[Peer {PEER_ID}]\t[Main       ]\t  Tokenizer loaded", flush=True)
    sys.stdout.flush()

    return model, tokenizer


def load_sharded_dataset(peer_id: int) -> Dataset:
    """peer固有の訓練ファイルを読み込み。"""
    train_file = BASE_DIR / "data" / "train" / f"peer_{peer_id}.json"
    with open(train_file) as f:
        data = json.load(f)
    samples = data.get("samples", [])
    print(f"[{_now()}]\t[Peer {peer_id}]\t[Main       ]\tLoaded {len(samples)} samples from {train_file}", flush=True)
    return Dataset.from_list(samples)


def tokenize_dataset(
    dataset: Dataset,
    tokenizer: Any,
    max_seq_len: int,
) -> list[dict[str, torch.Tensor]]:
    """データセットをトークン化し、入力・ラベルペアに変換。

    Question部分のラベルは-100（ignore_index）でマスクし、損失計算から
    除外する。Question全体をラベルに含めたまま学習すると、モデルが
    「質問文の再構成」という無関係なタスクにも学習容量を割いてしまい、
    本来の目的である「Answer部分の生成」の学習効率が下がる（実機テストで
    accuracyが向上/低下を繰り返すだけで着実な収束が見られなかった一因）。
    """
    tokenized: list[dict[str, torch.Tensor]] = []
    skipped_fully_truncated = 0
    for item in dataset:
        prompt = f"Question: {item['question']}\nAnswer:"
        full_text = f"{prompt} {item['answer']}"

        prompt_ids = tokenizer(prompt, truncation=False)["input_ids"]
        tokens = tokenizer(
            full_text,
            truncation=True,
            max_length=max_seq_len,
            padding=False,
        )
        full_ids = tokens["input_ids"]
        if len(full_ids) <= 1:
            continue
        prompt_len = min(len(prompt_ids), len(full_ids))
        # 解答トークンが truncation で全て失われた例（prompt_len が系列長以上）は、
        # 全ラベルが -100 になり損失 0・勾配 0 の無駄なステップになる。さらに
        # 「解答形式（#### N）を一切含まない」ため学習に寄与しないので除外する
        # （max_seq_len を下げた際に長い解答の末尾が切れて発生しうる）
        if prompt_len >= len(full_ids):
            skipped_fully_truncated += 1
            continue
        input_ids = torch.tensor(full_ids, dtype=torch.long)
        labels = input_ids.clone()
        # promptがtruncationで欠けている場合に備え、full_idsの長さを超えない
        # 範囲でマスクする（トークナイザーの結合時の境界ズレにも安全）
        labels[:prompt_len] = -100
        tokenized.append({"input_ids": input_ids, "labels": labels})
    if skipped_fully_truncated > 0:
        print(
            f"[{_now()}]\t[Peer {PEER_ID}]\t[Main       ]\t"
            f"Skipped {skipped_fully_truncated} fully-truncated samples "
            f"(answer lost at max_seq_len={max_seq_len})", flush=True,
        )
    return tokenized


# ============================================================
# Thread 1: 管理サーバー交信リスナー（制御プレーン）
# ============================================================


def _send_json(sock: socket.socket, data: dict[str, Any]) -> bool:
    """ソケットへJSONデータを送信。"""
    try:
        payload = json.dumps(data).encode("utf-8")
        header = len(payload).to_bytes(4, "big")
        sock.sendall(header)
        sock.sendall(payload)
        return True
    except OSError:
        return False


def _recv_json(sock: socket.socket, timeout: float = 2.0) -> dict[str, Any] | None:
    """ソケットからJSONデータを受信。"""
    try:
        sock.settimeout(timeout)
        header = b""
        while len(header) < 4:
            chunk = sock.recv(4 - len(header))
            if not chunk:
                return None
            header += chunk
        length = int.from_bytes(header, "big")
        if length > 10 * 1024 * 1024:
            return None
        body = b""
        while len(body) < length:
            chunk = sock.recv(length - len(body))
            if not chunk:
                return None
            body += chunk
        return json.loads(body.decode("utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        return None


def server_listener_thread(state: SharedState, model: Any) -> None:
    """管理サーバーと永続TCP接続を維持し、シグナルを受信。

    接続時にpeer_idを登録し、以降は1秒周期のシグナルを待受。
    切断された場合は自動再接続する。
    """
    print(f"[{_now()}]\t[Peer {PEER_ID}]\t[T1:Listener]\tThread 1 (Server Listener) started. Connecting to {SERVER_HOST}:{SERVER_PORT}...", flush=True)

    while state.running:
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.settimeout(2.0)
                print(f"[{_now()}]\t[Peer {PEER_ID}]\t[T1:Listener]\t  Connecting to server...", flush=True)
                s.connect((SERVER_HOST, SERVER_PORT))
                print(f"[{_now()}]\t[Peer {PEER_ID}]\t[T1:Listener]\t  Connected to server", flush=True)

                # ペアID登録
                if not _send_json(s, {"type": "register", "peer_id": PEER_ID}):
                    print(f"[{_now()}]\t[Peer {PEER_ID}]\t[T1:Listener]\t  Failed to send registration, retrying...", flush=True)
                    time.sleep(1.0)
                    continue
                print(f"[{_now()}]\t[Peer {PEER_ID}]\t[T1:Listener]\t  Registration sent (peer_id={PEER_ID})", flush=True)

                # モデルロード完了を待ってReadyを送信
                print(f"[{_now()}]\t[Peer {PEER_ID}]\t[T1:Listener]\t  Waiting for model load to complete (timeout=60s)...", flush=True)
                state.ready_to_send.wait(timeout=60.0)
                _send_json(s, {"type": "ready", "peer_id": PEER_ID})
                print(f"[{_now()}]\t[Peer {PEER_ID}]\t[T1:Listener]\t  Ready signal sent. Waiting for signals...", flush=True)

                while state.running:
                    # サーバーは全peerがreadyになるまでsignalを一切送信しないため
                    # （experiment_start_timeがNoneの間はbroadcastをスキップする設計）、
                    # タイムアウトを短く取ると準備が早いpeerがreadyになる前に
                    # 何度も再接続を繰り返してしまう（実際にサーバー側のRegistered
                    # カウントが不安定に増減する不具合として観測された）。
                    # 切断自体はrecv()が空バイトを返すことで即座に検知されるため、
                    # タイムアウトを伸ばしても切断検知の遅延にはならない。
                    data = _recv_json(s, timeout=30.0)
                    if data is None:
                        break

                    msg_type = data.get("type")

                    if msg_type == "signal":
                        # サーバーから配信された，未配信のstart/endイベントを処理する。
                        # 自分のpeer_idが関わるイベントのみホワイトリストへ反映する
                        events = data.get("events", [])
                        now = time.time()
                        changed_events: list[dict[str, Any]] = []

                        with state.whitelist_lock:
                            for event in events:
                                peers = event.get("peers", [])
                                if len(peers) != 2:
                                    print(f"[{_now()}]\t[Peer {PEER_ID}]\t[T1:Listener]\tMalformed event (peers must have 2 elements): {event}", flush=True)
                                    continue
                                if PEER_ID not in peers:
                                    continue
                                other_pid = peers[0] if peers[1] == PEER_ID else peers[1]
                                event_type = event.get("event")
                                if event_type == "start":
                                    state.peer_whitelist.add(other_pid)
                                elif event_type == "end":
                                    state.peer_whitelist.discard(other_pid)
                                else:
                                    print(f"[{_now()}]\t[Peer {PEER_ID}]\t[T1:Listener]\tUnknown event type, ignored: {event}", flush=True)
                                    continue
                                changed_events.append({"event": event_type, "other_peer_id": other_pid})
                            current_whitelist_snapshot = sorted(state.peer_whitelist)

                        # 接触イベントのタイムスタンプ付きログ（contact_pattern.jsonとの
                        # 実時刻整合性を事後検証できるように，開始・終了それぞれを個別に記録する）
                        if changed_events:
                            contact_elapsed = (
                                now - state.experiment_start_time
                                if state.experiment_start_time
                                else 0.0
                            )
                            for changed in changed_events:
                                contact_event = {
                                    "type": "contact_event",
                                    "event": changed["event"],
                                    "peer_id": PEER_ID,
                                    "other_peer_id": changed["other_peer_id"],
                                    "elapsed": contact_elapsed,
                                    "allowed_peers": current_whitelist_snapshot,
                                }
                                try:
                                    state.metrics_queue.put(contact_event, timeout=1.0)
                                except queue.Full:
                                    pass

                    elif msg_type == "experiment_start":
                        state.experiment_start_time = time.time()
                        state.experiment_duration = float(data.get("duration", 0.0))
                        # 以降、ログprefixの時刻を「実験開始からの経過秒数」にする
                        global _EXP_START_WALL
                        _EXP_START_WALL = state.experiment_start_time
                        state.experiment_running.set()
                        print(
                            f"[{_now()}]\t[Peer {PEER_ID}]\t[T1:Listener]\tExperiment STARTED. "
                            f"Duration: {data.get('duration')}s",
                            flush=True
                        )

                    elif msg_type == "experiment_stop":
                        print(f"[{_now()}]\t[Peer {PEER_ID}]\t[T1:Listener]\tExperiment STOPPED.", flush=True)
                        state.running = False
                        break

        except (OSError, ConnectionError) as e:
            if state.running:
                print(f"[{_now()}]\t[Peer {PEER_ID}]\t[T1:Listener]\tServer connection lost ({e}). Reconnecting in 1s...", flush=True)
                time.sleep(1.0)

    print(f"[{_now()}]\t[Peer {PEER_ID}]\t[T1:Listener]\tThread 1 (Server Listener) stopped")


# ============================================================
# Thread 2: P2P生TCP交換・マージ層（データプレーン）
# ============================================================


def _recv_exact(conn: socket.socket, n: int) -> bytes | None:
    """ソケットから正確にnバイト受信する。

    socket.recv(n)は要求したnバイトに達するまで待つのではなく、その時点で
    到達済みの分だけを返すことがある（TCPの部分受信）。特にLoRA全重み
    （実測で数十MB）のような大きなペイロードでは、1回のrecv呼び出しで
    全データを受信できるとは限らず、ここでループしないと後続のフレーム境界
    がずれてプロトコル全体が破綻する。
    """
    buf = b""
    while len(buf) < n:
        chunk = conn.recv(min(65536, n - len(buf)))
        if not chunk:
            return None
        buf += chunk
    return buf


def _serialize_weights(state_dict: dict[str, torch.Tensor]) -> bytes:
    """state_dictをfloat16化した上でpickleシリアライズしてバイナリ化。

    LoRAパラメータはPEFTのデフォルト挙動により訓練中はfloat32で保持される
    （実測で410テンソル・96.6MB/チェックポイント）。訓練自体の数値安定性は
    float32のまま維持しつつ、P2P送信時のみfloat16へダウンキャストすることで
    通信量を約半分に抑える（受信側でマージ後、元のdtypeへ戻して適用される）。

    gzip圧縮は行わない。ニューラルネットの重みは乱数に近い分布のため
    圧縮率が低く（float32・96MBの実測で圧縮後88.6MBと8%程度の削減）、
    圧縮・解凍そのものに数秒（実測: 圧縮6.3秒、解凍1.1秒）かかり、この間
    Thread 3（訓練ループ）とのGIL競合でスループットが周期的に停止する
    ストールを引き起こすことが実機テストで確認されたため。
    """
    import io

    half_precision = {k: v.half() for k, v in state_dict.items()}
    buf = io.BytesIO()
    torch.save(half_precision, buf)
    return buf.getvalue()


def _deserialize_weights(data: bytes) -> dict[str, torch.Tensor]:
    """pickleバイナリをstate_dictに復元。"""
    import io

    buf = io.BytesIO(data)
    return torch.load(buf, weights_only=True, map_location="cpu")


def p2p_exchange_thread(state: SharedState, model: Any) -> None:
    """P2P重み交換スレッド。

    - ホワイトリストを監視し、接続可能なpeerへ積極的に接続
    - サーバーからendイベントを受信し除外されたpeerとの接続は切断し、
      時変トポロジーのローテーションを実際に反映する
    - シャドウコピーを読み出して送信
    - 受信データを一時バッファへ格納し、マージ計算のみを行う
    - マージ結果はmerge_queueに渡すだけで、model本体には触れない
      （実際の反映はThread 3がステップ境界で行う。C1/C2参照）

    ベースライン実験用: 環境変数 WAFL_P2P_ENABLED=0 のとき、このスレッドは何もせず終了する。
    送信もマージも行われず merge_queue は空のままなので、各ノードは知識共有なしで自シャードのみを
    学習する（self-training / 孤立訓練ベースライン）。WAFL-PEFT の P2P 協調学習との比較に用いる。
    """
    if os.environ.get("WAFL_P2P_ENABLED", "1") == "0":
        print(f"[{_now()}]\t[Peer {PEER_ID}]\t[T2:P2P     ]\tP2P disabled (WAFL_P2P_ENABLED=0): self-training baseline (no weight exchange).", flush=True)
        return

    print(f"[{_now()}]\t[Peer {PEER_ID}]\t[T2:P2P     ]\tThread 2 (P2P Exchange) started")

    # peer_id -> IP マッピング
    host_map = resolve_hosts()

    # 受信バッファ: peer_id -> 最新のシリアライズ済み重み（1つだけ保持）。
    # 以前は list で受信の度に全バージョンを溜め、マージ時に全ブロブを deserialize
    # して平均に加算していた。これには (1) 新接触時に溜まった多数のバージョンを
    # 一括 deserialize して Thread 3 を数十秒ブロックする性能問題、(2) 多く送ってきた
    # peer が平均で過大重みになる正確性バグ、があった。マージで意味を持つのは各 peer の
    # 最新の重みだけなので、peer ごとに最新の1つだけを上書き保持する
    receive_buffers: dict[int, bytes] = {}
    receive_lock = threading.Lock()

    # P2Pソケット（複数接続をaccept）
    p2p_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    p2p_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    p2p_socket.bind(("0.0.0.0", P2P_PORT))
    p2p_socket.listen(16)
    p2p_socket.settimeout(0.5)

    # 接続管理。送信用（自分がconnectした）と受信用（相手がconnectしてきた）を
    # 別々のdictで管理する。同一peerとの通信は双方が別方向にconnectする2本の
    # TCP接続で成立するため、片方が閉じても他方を誤って消してはならない
    # （旧実装ではこの2つを同じdictで扱っていたため、受信用接続が切れるたびに
    # 送信用接続の参照が誤って失われ、ソケットリークと再接続の無限増加を招いていた）
    active_connections: dict[int, socket.socket] = {}
    incoming_connections: dict[int, socket.socket] = {}
    conn_lock = threading.Lock()

    def _recv_peer_info(conn: socket.socket) -> int | None:
        """接続からpeer_idを受信し、active_connectionsへ登録する。

        着信接続（accept_incoming内）とoutgoing接続の両方で再利用する。
        戻り値: peer_id（成功時）または None（失敗時）
        """
        # peer_idを受信
        header = _recv_exact(conn, 4)
        if header is None:
            return None
        length = int.from_bytes(header, "big")
        pid_bytes = _recv_exact(conn, length)
        if pid_bytes is None:
            return None
        peer_info = json.loads(pid_bytes.decode("utf-8"))
        peer_id = peer_info.get("peer_id", -1)
        if peer_id < 0:
            return None
        with conn_lock:
            active_connections[peer_id] = conn

        # 重みデータを受信
        while state.running:
            with conn_lock:
                if active_connections.get(peer_id) is not conn:
                    break
            wh = _recv_exact(conn, 4)
            if wh is None:
                break
            wlen = int.from_bytes(wh, "big")
            if wlen == 0 or wlen > 100 * 1024 * 1024:
                break
            weight_data = _recv_exact(conn, wlen)
            if weight_data is None:
                break

            with receive_lock:
                receive_buffers[peer_id] = weight_data

        return peer_id

    # 受信ハンドラスレッド
    def accept_incoming() -> None:
        """着信P2P接続を受付け。"""
        while state.running:
            try:
                conn, addr = p2p_socket.accept()
                conn.settimeout(30.0)

                # peer_idを受信
                header = _recv_exact(conn, 4)
                if header is None:
                    conn.close()
                    continue
                length = int.from_bytes(header, "big")
                pid_bytes = _recv_exact(conn, length)
                if pid_bytes is None:
                    conn.close()
                    continue
                peer_info = json.loads(pid_bytes.decode("utf-8"))
                incoming_peer_id = peer_info.get("peer_id", -1)
                if incoming_peer_id < 0:
                    conn.close()
                    continue

                with conn_lock:
                    old_conn = incoming_connections.get(incoming_peer_id)
                    incoming_connections[incoming_peer_id] = conn
                if old_conn is not None:
                    try:
                        old_conn.close()
                    except OSError:
                        pass

                # 重みデータを受信
                while state.running:
                    with conn_lock:
                        if incoming_connections.get(incoming_peer_id) is not conn:
                            break
                    wh = _recv_exact(conn, 4)
                    if wh is None:
                        break
                    wlen = int.from_bytes(wh, "big")
                    if wlen == 0 or wlen > 100 * 1024 * 1024:
                        break
                    weight_data = _recv_exact(conn, wlen)
                    if weight_data is None:
                        break

                    with receive_lock:
                        # 最新の重みだけを保持（古い版は上書きで破棄）
                        receive_buffers[incoming_peer_id] = weight_data

                # 受信完了（自分がaccept_incomingで登録した接続のみを取り除く）
                with conn_lock:
                    if incoming_connections.get(incoming_peer_id) is conn:
                        del incoming_connections[incoming_peer_id]
                conn.close()

            except OSError:
                continue

    acceptor = threading.Thread(target=accept_incoming, daemon=True)
    acceptor.start()

    def _close_peer_connections(pid: int) -> None:
        """指定peerとの送信用・受信用接続を両方クローズする。

        NOTE: receive_buffers はここで消さない。マージチェック（本ループの later）
        が受信バッファを処理する必要があるため。接続切断は送受信用socketのみ。
        """
        with conn_lock:
            out_conn = active_connections.pop(pid, None)
            in_conn = incoming_connections.pop(pid, None)
        for c in (out_conn, in_conn):
            if c is not None:
                try:
                    c.close()
                except OSError:
                    pass

    last_merge_step: int = -1
    # 前回シリアライズ・送信した shadow_weights の版番号。重みが変わった時
    # （版番号が進んだ時）だけ再シリアライズ・送信し、ループ毎の無駄な
    # torch.save による GIL 競合を防ぐ
    last_sent_version: int = -1

    # 前回周回時点でのホワイトリスト（新たに切断すべきpidを検出するための比較用）
    prev_whitelist: set[int] = set()

    while state.running:
        # ホワイトリストを取得（読み取り専用）
        with state.whitelist_lock:
            whitelist = set(state.peer_whitelist)

        # 直前のwhitelistを保存（マージチェックで接触終了peerのバッファを
        # 処理するために必要。接触終了peerの重みはwhitelistから外れる前に
        # 受信済みなので、マージしても安全）
        prev_whitelist_for_merge = prev_whitelist

        # ホワイトリストから外れた（サーバーから明示的にendイベントを受信した）
        # peerを検出し、ログを出す。これにより時変トポロジーのローテーションが
        # 実際の接続状態に反映される
        newly_removed = prev_whitelist - whitelist
        for pid in newly_removed:
            print(f"[{_now()}]\t[Peer {PEER_ID}]\t[T2:P2P     ]\tContact with peer {pid} ended.", flush=True)
        prev_whitelist = whitelist

        # ホワイトリストから外れた（サーバー側で明示的に除外された）peerとの
        # 既存接続も切断する
        with conn_lock:
            stale_pids = [
                pid for pid in set(active_connections) | set(incoming_connections)
                if pid not in whitelist
            ]
        for pid in stale_pids:
            _close_peer_connections(pid)

        if not whitelist:
            time.sleep(0.1)
            continue

        # 未接続のpeerへ接続
        with conn_lock:
            unconnected = [pid for pid in whitelist if pid not in active_connections]
        for pid in unconnected:
            try:
                conn = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                conn.settimeout(5.0)
                # peer_idからIPを取得（hosts.txt）
                if pid not in host_map:
                    print(f"[{_now()}]\t[Peer {PEER_ID}]\t[T2:P2P     ]\t  Skipping peer {pid}: not in hosts.txt", flush=True)
                    continue
                peer_ip = host_map[pid]
                conn.connect((peer_ip, P2P_PORT))
                # 接続確立後は、LoRA全重み（float16化後も実測20MB超）の送信に
                # 十分な時間を確保するため、5秒のconnectタイムアウトから
                # 送信用の長いタイムアウトへ切り替える
                conn.settimeout(30.0)
                # peer_idを通知
                pid_msg = json.dumps({"peer_id": PEER_ID}).encode("utf-8")
                conn.sendall(len(pid_msg).to_bytes(4, "big"))
                conn.sendall(pid_msg)
                with conn_lock:
                    active_connections[pid] = conn
                print(f"[{_now()}]\t[Peer {PEER_ID}]\t[T2:P2P     ]\tP2P connected to peer {pid} ({peer_ip})")
                # outgoing 接続からも相手の重みを受信
                _recv_peer_info(conn)
            except OSError:
                pass

        # 接続済みpeerへ重みを送信。shadow_weights が実際に更新された時
        # （版番号が進んだ時）だけシリアライズ・送信する。以前は
        # current_step != last_merge_step を条件にしていたため、マージが起きるまでの
        # 間ループ毎に 48MB の重みを torch.save で再シリアライズし続け、GIL を保持して
        # Thread 3 の forward/backward を断続的に数秒ブロックしていた（step 時間が
        # 0.7s→7s に跳ねる原因）。版番号ベースにすることで、送信は重みの更新頻度
        # （勾配累積境界ごと）に一致し、無駄な再シリアライズが消える
        current_step = state.current_step
        current_version = state.shadow_version
        with conn_lock:
            peers_to_send = list(active_connections.keys())
        # 新しい版があり、かつ送信先がある時だけシリアライズ・送信する
        # （送信先ゼロで serialize しても無駄なので、接続確立まで版番号は据え置く）
        if state.shadow_weights is not None and current_version != last_sent_version and peers_to_send:
            with state.weights_lock:
                serialized = _serialize_weights(state.shadow_weights)
            sent_ok = False
            for pid in peers_to_send:
                try:
                    conn = active_connections[pid]
                    # 長さヘッダ + 重みデータ。send()は要求バイト数より少なく
                    # 送って早期に返ることがあるため、sendall()で確実に送り切る
                    conn.sendall(len(serialized).to_bytes(4, "big"))
                    conn.sendall(serialized)
                    sent_ok = True
                except OSError:
                    with conn_lock:
                        active_connections.pop(pid, None)
            if sent_ok:
                last_sent_version = current_version

        # 受信バッファのマージ（訓練イテレーションの境界で実行）
        # ここではCPU上のテンソルを平均化するだけで、model・shadow_weightsには
        # 一切触れない。反映はThread 3がステップ境界で安全に行う
        #
        # ホワイトリスト外peerの重みを除外する: accept_incomingは受信専用スレッドで
        # あり、ホワイトリスト外peerからの接続もいったん受理してバッファへ積む
        # （除外に伴う切断は本ループのstale_pids処理が次周回で行うため）。
        # 切断より先にこのマージ処理が走る競合が起きると、ホワイトリスト外の
        # 重みが平均マージに混入しうるため、ここで明示的にwhitelistでフィルタする
        if current_step != last_merge_step and current_step > 0:
            with receive_lock:
                # prev_whitelist_for_mergeを使う: 接触終了peerのバッファも
                # 含める（そのpeerはwhitelistから外れる前に重みを送信済み）
                buffers_to_merge = {
                    pid: bufs for pid, bufs in receive_buffers.items()
                    if pid in prev_whitelist_for_merge
                }
                receive_buffers.clear()

            if buffers_to_merge:
                merged: dict[str, torch.Tensor] | None = None
                count = 0
                # peer ごとに最新の重み1つだけを deserialize して平均する
                # （各 peer が1票。多く送ってきた peer に過大重みが乗らない）
                for weight_bytes in buffers_to_merge.values():
                    try:
                        remote_weights = _deserialize_weights(weight_bytes)
                        if merged is None:
                            merged = {k: v.float() for k, v in remote_weights.items()}
                            count = 1
                        else:
                            for k in merged:
                                if k in remote_weights:
                                    merged[k] += remote_weights[k].float()
                            count += 1
                    except (EOFError, RuntimeError, KeyError):
                        continue

                if merged is not None and count > 0:
                    # W3: 自ノードの重みを加えて平均する（WAFL 原典 Eq.3 準拠）
                    with torch.no_grad():
                        for name, param in model.named_parameters():
                            if name in merged:
                                merged[name] = merged[name].to(param.device)
                                merged[name] = merged[name] + param.float()
                    count += 1

                    for k in merged:
                        merged[k] /= count

                    try:
                        state.merge_queue.put(merged, timeout=1.0)
                        last_merge_step = current_step
                        print(
                            f"[{_now()}]\t[Peer {PEER_ID}]\t[T2:P2P     ]\tQueued merged weights from {count} peers "
                            f"at step {current_step}"
                        )
                    except queue.Full:
                        print(
                            f"[{_now()}]\t[Peer {PEER_ID}]\t[T2:P2P     ]\tmerge_queue full, dropping merge "
                            f"at step {current_step}",
                            flush=True,
                        )

        time.sleep(0.01)

    # クリーンアップ
    p2p_socket.close()
    with conn_lock:
        all_conns = list(active_connections.values()) + list(incoming_connections.values())
    for conn in all_conns:
        try:
            conn.close()
        except OSError:
            pass

    print(f"[{_now()}]\t[Peer {PEER_ID}]\t[T2:P2P     ]\tThread 2 (P2P Exchange) stopped")


# ============================================================
# Thread 3: LoRA訓練ループ（計算プレーン）
# ============================================================


def training_loop_thread(
    state: SharedState,
    model: Any,
    train_data: list[dict[str, torch.Tensor]],
) -> None:
    """LoRA訓練ループ。

    1バッチごとに順伝播・逆伝播・.optimizer.step()を実行。
    通信スレッドによるマージはイテレーションの境界で受け入れる。
    各ステップでメトリクスをキュー経由でロガスレッドへ送信。
    train/testの評価（accuracy算出）はクライアント側では一切行わない。
    このスレッドは一定間隔でLoRA重みをチェックポイントとして保存するのみで、
    評価は管理サーバー（server.pyのGlobalEvalスレッド）が専用GPUで行う。
    """
    print(f"[{_now()}]\t[Peer {PEER_ID}]\t[T3:Train   ]\tThread 3 (Training Loop) started", flush=True)

    # LoRA パラメータを named_parameters() 経由で収集する。
    # state_dict() はディスクオフロードモデルで全重みをロードしてしまう可能性があるが、
    # named_parameters() は訓練可能なパラメータのみ実体化する。
    lora_param_dict = {
        name: param
        for name, param in model.named_parameters()
        if "lora" in name.lower()
    }
    lora_keys = list(lora_param_dict.keys())
    print(f"[{_now()}]\t[Peer {PEER_ID}]\t[T3:Train   ]\t  Cloning {len(lora_keys)} LoRA weight tensors to CPU...", flush=True)
    with state.weights_lock:
        state.shadow_weights = {
            k: lora_param_dict[k].detach().cpu().clone() for k in lora_keys
        }
    print(f"[{_now()}]\t[Peer {PEER_ID}]\t[T3:Train   ]\tModel loaded, shadow weights ready (LoRA keys: {len(lora_keys)})")
    state.ready_to_send.set()

    # GPU 環境では LoRA パラメータがある device に inputs を置く。
    # disk-offload 環境では cpu になるため、そのままで問題ない。
    _train_device = next(
        (p.device for p in model.parameters() if p.requires_grad and p.device.type != "meta"),
        torch.device("cpu"),
    )
    print(f"[{_now()}]\t[Peer {PEER_ID}]\t[T3:Train   ]\t  Training device: {_train_device}", flush=True)

    # VRAM 削減: paged 8-bit AdamW（QLoRA 標準）。optimizer 状態を 8-bit 化して
    # メモリを節約し、さらに GPU 逼迫時に状態を CPU へページングして OOM スパイクを
    # 吸収する。外部 GPU 競合（~2.6GB）下で 12GB に収めるための主要な削減策の一つ
    optimizer = bnb.optim.PagedAdamW8bit(
        [p for p in model.parameters() if p.requires_grad],
        lr=_get_float("training", "learning_rate"),
        weight_decay=0.01,
    )

    # 勾配累積: micro-batch=1 のまま grad_accum_steps 回だけ勾配を貯めてから1回
    # optimizer.step() する。RTX 3060(12GB)+巨大vocab(262144)では真のバッチ拡大が
    # logits テンソル([B,T,V])のメモリでOOMするため、メモリ安全なまま実効バッチを
    # 拡大して勾配を平滑化する（単一サンプルの極めてノイジーな勾配が学習効率を
    # 下げていた）
    grad_accum_steps = max(1, _get_int("training", "grad_accum_steps", 1))
    # LR スケジュール: optimizer ステップ単位で線形 warmup した後、実験の経過時間
    # 割合（elapsed/duration）で cosine 減衰させる。本アプリは時間ベース制御で
    # 総ステップ数が事前に定まらないため、cosine 減衰の horizon をステップ数ではなく
    # 実験時間割合で駆動する（throughput がノード間で違っても、各ノードが実験終盤で
    # 同様に LR を下げられる）。warmup で序盤の不安定を抑え、終盤の減衰で収束を安定化する
    base_lr = _get_float("training", "learning_rate")
    warmup_steps = max(0, _get_int("training", "lr_warmup_steps", 0))
    lr_min_ratio = _get_float("training", "lr_min_ratio", 0.1)
    opt_step = 0  # 実行済み optimizer.step() 回数（warmup 判定用）

    def _lr_scale(opt_step_done: int, elapsed: float, duration: float) -> float:
        """現在の LR 倍率を返す（warmup 線形 → cosine 減衰）。"""
        if warmup_steps > 0 and opt_step_done < warmup_steps:
            return opt_step_done / warmup_steps
        frac = min(1.0, max(0.0, elapsed / duration)) if duration > 0 else 0.0
        return lr_min_ratio + 0.5 * (1.0 - lr_min_ratio) * (1.0 + math.cos(math.pi * frac))

    optimizer.zero_grad()

    # データ走査順序。逐次巡回（step % len）だと毎エポック同じ順序で同じデータを
    # 見て過学習を助長するため、エポック（全データ1周）ごとにシャッフルする
    data_order = list(range(len(train_data)))
    random.shuffle(data_order)
    order_pos = 0

    # メトリクスは全ステップをログ出力。チェックポイント（LoRA全重み保存）は
    # eval_interval_seconds間隔でのみ保存する（毎ステップ保存すると長時間・
    # 大規模実験でディスクを圧迫するため）。実験終了は学習ステップ数の上限では
    # なくサーバーからのexperiment_stopシグナル（contact_pattern.jsonの
    # タイムラインに基づく時間ベース制御）でのみ判定するため、保存の間隔も
    # 経過時間ベースで揃える
    eval_interval_seconds = _get_float("training", "eval_interval_seconds", 60.0)
    last_eval_time = 0.0
    step = 0
    accum_count = 0  # 勾配累積カウンタ。grad_accum_stepsに達したら更新を1回行う
    total_tokens = 0

    # 逐次（同期バリア）方式の切替（Iter12）: WAFL_P2P_SYNC=1 のときのみ、接触中の
    # peer とのマージ完了をブロッキング待機する。既定（未設定/"0"）は従来どおり
    # merge_queue.get_nowait() の非同期・非ブロッキング経路のみを通る（後方互換）
    p2p_sync_enabled = os.environ.get("WAFL_P2P_SYNC", "0") == "1"
    # タイムアウトは env > config/settings.json の優先順。時変トポロジーで接触が
    # 交換完了前に切れた場合、待ち続けるとデッドロック・知識伝播停止になるため、
    # タイムアウト到達時は非同期同様マージなしで前進する
    barrier_timeout = _get_float("communication", "p2p_sync_timeout_sec", 15.0)
    if "WAFL_P2P_SYNC_TIMEOUT_SEC" in os.environ:
        try:
            barrier_timeout = float(os.environ["WAFL_P2P_SYNC_TIMEOUT_SEC"])
        except ValueError:
            print(
                f"[{_now()}]\t[Peer {PEER_ID}]\t[T3:Train   ]\tWAFL_P2P_SYNC_TIMEOUT_SEC="
                f"{os.environ['WAFL_P2P_SYNC_TIMEOUT_SEC']!r} is not a float; "
                f"falling back to config value {barrier_timeout}s.",
                flush=True,
            )

    print(f"[{_now()}]\t[Peer {PEER_ID}]\t[T3:Train   ]\tp2p_sync_enabled={p2p_sync_enabled}, barrier_timeout={barrier_timeout}s", flush=True)

    while state.running:
        if not state.experiment_running.is_set():
            time.sleep(0.1)
            continue

        step_start_time = time.time()

        print(f"[{_now()}]\t[Peer {PEER_ID}]\t[T3:Train   ]\tTraining step {step} (train_data len={len(train_data)})", flush=True)

        # 経過時間を更新
        state.elapsed_time = time.time() - state.experiment_start_time

        # データローディング（inputs を LoRA device へ）。エポック（全データ1周）
        # ごとにシャッフルした data_order を走査し、末尾に達したら再シャッフルする
        if order_pos >= len(data_order):
            random.shuffle(data_order)
            order_pos = 0
        idx = data_order[order_pos]
        order_pos += 1
        batch = train_data[idx]
        input_ids = batch["input_ids"].unsqueeze(0).to(_train_device)
        labels = batch["labels"].unsqueeze(0).to(_train_device)

        # フォワード・バックワード（ここから純計算時間を計測。この区間外＝マージ反映・
        # GPU解放・shadow更新などにかかった時間を stall_duration として分離し、
        # 通信・マージ処理が計算をブロックしていないことを事後検証できるようにする）。
        # 勾配累積のため loss を grad_accum_steps で割ってから backward し、
        # grad_accum_steps 回ぶんの勾配が .grad に加算される
        compute_start_time = time.time()
        # labels を渡さず logits のみ取得し、省メモリ chunked CE で損失を計算する
        # （モデル内蔵の損失は logits 全体を fp32 化するため巨大 vocab で OOM 源になる）
        outputs = model(input_ids=input_ids)
        loss = _memory_efficient_causal_lm_loss(outputs.logits, labels)
        total_tokens += int(input_ids.numel())

        (loss / grad_accum_steps).backward()

        accum_count += 1
        do_optim_step = accum_count % grad_accum_steps == 0

        with state.step_lock:
            state.current_step = step + 1
            current_step_for_merge = step + 1

        # 累積境界でのみパラメータを更新する。累積途中で optimizer.step() や
        # P2Pマージ反映を行うとパラメータが書き換わり、加算中の勾配と不整合になる
        if do_optim_step:
            optimizer.step()
            opt_step += 1
            # warmup 線形 → 経過時間割合で cosine 減衰した LR を各 param group へ設定
            lr_scale = _lr_scale(opt_step, state.elapsed_time, state.experiment_duration)
            for group in optimizer.param_groups:
                group["lr"] = base_lr * lr_scale
            optimizer.zero_grad()
        compute_duration = time.time() - compute_start_time

        # GPUメモリ解放（断片化防止）は毎micro-stepで行う。巨大vocab(262144)の
        # backward は一時的に数百MiBのスパイクを要し、PyTorchのキャッシュアロケータは
        # empty_cache()を呼ばないと解放済みブロックをGPUへ返さないため、累積境界
        # （8ステップ毎）だけの解放ではreservedが膨らみ、次のスパイクで空きが枯渇して
        # OOMする（実機で4/5ノードが「303MiB free / 320MiB要求」でクラッシュした）。
        # gc.collect()はPython側の循環参照回収で、毎ステップ呼んでも実測で
        # スループットに影響しないことをbaselineで確認済み
        import gc
        gc.collect()
        torch.cuda.empty_cache()

        # P2Pマージ結果の反映と shadow_weights 更新は、パラメータが実際に変化した
        # 累積境界でのみ行う。Thread 2はマージ計算のみ行いmerge_queueに積むだけなので、
        # model本体への書き込みは常にここ（Thread 3、かつ optimizer.step() が完了した
        # 安全なタイミング）でのみ行い、計算中のデータ競合を構造的に防ぐ
        barrier_wait = 0.0
        if do_optim_step:
            # 同期バリア（Iter12, WAFL_P2P_SYNC=1）: 現在接触中の peer が居るときだけ、
            # Thread 2 のマージ完了をブロッキング待機する。接触相手が居ない孤立区間で
            # 待つのは無意味なので get_nowait() にフォールバックし単独前進する
            with state.whitelist_lock:
                has_active_peer = len(state.peer_whitelist) > 0
            use_barrier = p2p_sync_enabled and has_active_peer

            if use_barrier:
                barrier_start_time = time.time()
                try:
                    merged_weights = state.merge_queue.get(timeout=barrier_timeout)
                except queue.Empty:
                    # 接触が交換完了前に切れた等でタイムアウト。時変トポロジー下の
                    # デッドロック・知識伝播停止を避けるため、非同期同様マージなしで
                    # 前進する（例外は握りつぶさずログに残す）
                    merged_weights = None
                    print(
                        f"[{_now()}]\t[Peer {PEER_ID}]\t[T3:Train   ]\tP2P sync barrier timed out "
                        f"after {barrier_timeout:.1f}s (WAFL_P2P_SYNC=1); proceeding without merge.",
                        flush=True,
                    )
                barrier_wait = time.time() - barrier_start_time
            else:
                try:
                    merged_weights = state.merge_queue.get_nowait()
                except queue.Empty:
                    merged_weights = None

            if merged_weights is not None:
                with torch.no_grad():
                    for name, param in model.named_parameters():
                        if name in merged_weights:
                            param.copy_(merged_weights[name].to(param.dtype).to(param.device))

            # シャドウコピー更新（named_parameters 経由で LoRA のみ更新）。
            # Thread 2 が P2P 送信するのはこの shadow なので、パラメータ更新後に反映する
            with state.weights_lock:
                if state.shadow_weights is not None:
                    for name, param in model.named_parameters():
                        if name in state.shadow_weights:
                            state.shadow_weights[name].copy_(param.detach().cpu())
            # 版番号を上げ、Thread 2 に「重みが変わった」ことを知らせる（Thread 2 は
            # この変化時のみ再シリアライズ・送信する）
            state.shadow_version += 1

        # メトリクス計算。ログには未スケールの loss を用いる（backward には
        # grad_accum_steps で割った値を渡しているが、監視指標としては元の損失が自然）
        loss_value = loss.item()
        step_duration = time.time() - step_start_time
        # stall_duration: ステップ全体からforward/backward/optimizer.stepの
        # 純計算時間を除いた残り（マージ反映・GPU解放・評価などのオーバーヘッド）。
        # 通信・マージが計算をブロックしていないかを事後検証するための指標
        stall_duration = max(0.0, step_duration - compute_duration)
        # このステップのトークン数 / このステップの所要時間（瞬間スループット）。
        # 旧実装は total_tokens（累積）/ 訓練開始からの累積経過時間 だったため、
        # 通信起因のストールの有無に関わらず、累積平均が定義上下から漸近的に
        # 立ち上がる（実測で経過時間との相関係数0.95という結果になり、真の
        # ストールフリー性とは無関係な見かけ上の上昇トレンドを生んでいた）
        step_tokens = int(input_ids.numel())
        tokens_per_sec = step_tokens / step_duration if step_duration > 0 else 0.0
        state.elapsed_time = time.time() - state.experiment_start_time

        # GPU使用率（SM utilization %）。CPU環境やNVML未対応環境では取得できない
        # ことがあるため、計測補助機能としてどの例外でも計測をクラッシュさせない
        gpu_util_percent = None
        if torch.cuda.is_available():
            try:
                gpu_util_percent = torch.cuda.utilization()
            except Exception:
                gpu_util_percent = None

        # 実験全体の残り時間（サーバーから通知されたdurationベース）。
        # ステップ数の上限を設けないため、「残りステップ数」ではなく
        # 「残り時間」で進捗を見積もる
        remaining_time = max(0.0, state.experiment_duration - state.elapsed_time)
        print(
            f"[{_now()}]\t[Peer {PEER_ID}]\t[T3:Train   ]\tStep {current_step_for_merge}: "
            f"loss={loss_value:.4f}, tok/s={tokens_per_sec:.1f}, "
            f"step={step_duration:.1f}s, "
            f"remaining={_eta_str(remaining_time)}",
            flush=True,
        )

        # 一定時間間隔でチェックポイントを保存する。実験中の評価（accuracy算出）は
        # クライアント側では行わない。保存したチェックポイントは、実験中は管理サーバーの
        # GlobalEvalスレッドがSSH+rsyncで収集してマージモデルを評価するのに使われ、
        # 実験終了後は各クライアント自身が run_post_experiment_evaluation で
        # 自分の履歴を評価するのに使われる
        should_checkpoint = state.elapsed_time - last_eval_time >= eval_interval_seconds
        if should_checkpoint:
            last_eval_time = state.elapsed_time

        metric = {
            "type": "metric",
            "peer_id": PEER_ID,
            "step": current_step_for_merge,
            "elapsed": state.elapsed_time,
            "loss": loss_value,
            "tokens_per_sec": tokens_per_sec,
            "total_tokens": total_tokens,
            "step_duration": step_duration,
            "compute_duration": compute_duration,
            "stall_duration": stall_duration,
            "barrier_wait": barrier_wait,
            "gpu_util_percent": gpu_util_percent,
        }
        try:
            state.metrics_queue.put(metric, timeout=1.0)
        except queue.Full:
            pass

        # チェックポイント保存（eval_interval_seconds間隔）
        if should_checkpoint:
            ckpt_metric = {**metric, "type": "checkpoint"}
            try:
                state.metrics_queue.put(ckpt_metric, timeout=1.0)
            except queue.Full:
                pass

            # LoRA重みをファイルへ保存
            ckpt_path = WEIGHT_DIR / f"weights_step_{current_step_for_merge:06d}.pt"
            with state.weights_lock:
                if state.shadow_weights is not None:
                    torch.save(state.shadow_weights, str(ckpt_path))
            state.checkpoint_elapsed[current_step_for_merge] = state.elapsed_time
            print(
                f"[{_now()}]\t[Peer {PEER_ID}]\t[T3:Train   ]\tStep {current_step_for_merge}: "
                f"loss={loss_value:.4f}, tok/s={tokens_per_sec:.1f}, "
                f"saved {ckpt_path}", flush=True
            )

        step += 1

    # ループはサーバーからのexperiment_stopシグナル（state.running=False）でのみ
    # 終了する。時間ベース制御では「最終ステップ」が事前に定まらないため、
    # 実験終了時点のシャドウ重みを最終チェックポイントとして明示的に残す
    # （analyze.pyの収束性能評価が最新の学習状態を参照できるようにするため）
    if step > 0:
        with state.weights_lock:
            if state.shadow_weights is not None:
                final_ckpt_path = WEIGHT_DIR / f"weights_step_{step:06d}.pt"
                torch.save(state.shadow_weights, str(final_ckpt_path))
        state.checkpoint_elapsed[step] = state.elapsed_time
        print(f"[{_now()}]\t[Peer {PEER_ID}]\t[T3:Train   ]\tFinal checkpoint saved at step {step}", flush=True)

    print(f"[{_now()}]\t[Peer {PEER_ID}]\t[T3:Train   ]\tThread 3 (Training Loop) stopped")


# ============================================================
# Thread 4: 非同期ディスク書き出し層（ロギングプレーン）
# ============================================================


def async_logging_thread(state: SharedState) -> None:
    """キュー経由でメトリクスとチェックポイントを非同期にディスクへ書き出し。

    SHUTDOWN シグネル（None）を受信するまでキューから読み取り続ける。
    state.running が False になった時点では自発的にシャットダウンしない
    （training_loop_threadの終了直後にstate.runningはFalseになるが、その後
    main()がrun_post_experiment_evaluation()を実行し、実験後評価の結果を
    metrics_queueへ積む。ここで早期にシャットダウンすると、その結果を
    誰も受信できずログに書き出されないまま失われる。実際にこの不具合が
    発生し、実験後評価の結果が0件になる事象が確認された）。
    main()はrun_post_experiment_evaluation()完了後、全プロデューサーが
    仕事を終えたことを確認したうえで明示的にNoneを送るため、シャットダウンの
    判定はこのシグネル受信のみに委ねてよい。
    """
    print(f"[{_now()}]\t[Peer {PEER_ID}]\t[T4:Logger  ]\tThread 4 (Async Logger) started")

    metric_log_path = LOG_DIR / f"metrics_peer_{PEER_ID}.log"
    metric_log_path.write_text("")

    received_count = 0
    while True:
        metric = state.metrics_queue.get()

        # SHUTDOWN シグネル
        if metric is None:
            print(f"[{_now()}]\t[Peer {PEER_ID}]\t[T4:Logger  ]\tLogger: shutting down (received={received_count})", flush=True)
            break

        # メトリクスをログファイルへ追記
        try:
            with open(metric_log_path, "a") as f:
                f.write(json.dumps(metric, default=str) + "\n")
                f.flush()
                os.fsync(f.fileno())
            received_count += 1

            if metric.get("type") == "checkpoint":
                print(
                    f"[{_now()}]\t[Peer {PEER_ID}]\t[T4:Logger  ]\tLogger: checkpoint saved at step "
                    f"{metric.get('step')} (loss={metric.get('loss'):.4f})"
                )
        except OSError as e:
            print(f"[{_now()}]\t[Peer {PEER_ID}]\t[T4:Logger  ]\tLogger error: {e}", file=sys.stderr)

    # 最終フラッシュ
    final_log_path = LOG_DIR / f"metrics_peer_{PEER_ID}_final.log"
    try:
        metric_log_path.rename(final_log_path)
    except OSError:
        pass

    print(f"[{_now()}]\t[Peer {PEER_ID}]\t[T4:Logger  ]\tThread 4 (Async Logger) stopped")


# ============================================================
# 実験後評価（訓練終了後、自分のGPUで自分のチェックポイント履歴を評価）
# ============================================================

# 評価に用いるGSM8K検証サンプル数。accuracy のノイズは √(p(1-p)/n) に従い、
# 40サンプルでは ±7% 程度残り学習成果の判別が不明瞭だった。80サンプルで ±5% に
# 下げて向上を明確にする（生成が逐次で重いぶん時間は伸びるが、学習成果を
# 明瞭にすることを優先する方針）。生成バッチは増やさない（eval時のKVキャッシュ
# 拡大は VRAM 固定制約下で OOM リスクがあるため）
_POST_EVAL_SAMPLE_LIMIT = 80
# 評価するチェックポイント数の上限（全期間から均等サンプリング）。6点にして
# 学習序盤→終盤の accuracy 上昇軌跡を滑らかに示す
_POST_EVAL_MAX_CHECKPOINTS = 6


def run_post_experiment_evaluation(state: SharedState, model: Any, tokenizer: Any) -> None:
    """実験終了後、自分のチェックポイント履歴を自分のGPUで評価する。

    実験中はThread 3（訓練ループ）がGPUを占有しているため評価を行わないが、
    training_loop_threadの終了後はGPUが空くため、ここで自分の学習経過を
    振り返り評価する。5台の学習デバイスがそれぞれ自分のGPU上で並列に実行する
    ため、管理サーバーが1GPUで全ノードを逐次評価する場合と異なり、ノード数に
    比例した評価時間の増加が起きない。

    結果はThread 4がすでに停止しているためmetrics_queueに積んでも書き出されない。
    ここではメトリクスログへの直接追記ではなく、metrics_queueへ投入したうえで
    main()側がThread 4のシャットダウン前にこの関数を呼ぶことで、既存のログ
    書き出し・回収（collect_logs.py）・分析（analyze.py）の仕組みにそのまま乗せる。
    """
    import gsm8k_eval

    print(f"[{_now()}]\t[Peer {PEER_ID}]\t[Main       ]\tStarting post-experiment evaluation...", flush=True)

    # 訓練中はgradient_checkpointingによりuse_cache=Falseだったが、評価では
    # もうbackward passを行わないため、KVキャッシュを有効化して生成を高速化する
    model.gradient_checkpointing_disable()
    model.config.use_cache = True
    model.eval()

    val_data = gsm8k_eval.load_gsm8k_val_data(BASE_DIR, sample_limit=_POST_EVAL_SAMPLE_LIMIT)
    if not val_data:
        print(f"[{_now()}]\t[Peer {PEER_ID}]\t[Main       ]\tGSM8K validation data not available. Skipping post-experiment evaluation.", flush=True)
        return

    all_steps = sorted(
        int(f.stem.replace("weights_step_", ""))
        for f in WEIGHT_DIR.glob("weights_step_*.pt")
    )
    if not all_steps:
        print(f"[{_now()}]\t[Peer {PEER_ID}]\t[Main       ]\tNo checkpoints found. Skipping post-experiment evaluation.", flush=True)
        return

    if len(all_steps) > _POST_EVAL_MAX_CHECKPOINTS:
        idx = [
            round(i * (len(all_steps) - 1) / (_POST_EVAL_MAX_CHECKPOINTS - 1))
            for i in range(_POST_EVAL_MAX_CHECKPOINTS)
        ]
        eval_steps = [all_steps[i] for i in sorted(set(idx))]
    else:
        eval_steps = all_steps

    print(
        f"[{_now()}]\t[Peer {PEER_ID}]\t[Main       ]\tEvaluating {len(eval_steps)}/{len(all_steps)} "
        f"checkpoints (samples={_POST_EVAL_SAMPLE_LIMIT})...", flush=True,
    )

    for step in eval_steps:
        ckpt_path = WEIGHT_DIR / f"weights_step_{step:06d}.pt"
        try:
            weights = torch.load(ckpt_path, weights_only=True, map_location="cpu")
        except (EOFError, RuntimeError) as e:
            print(f"[{_now()}]\t[Peer {PEER_ID}]\t[Main       ]\t  Step {step}: failed to load checkpoint ({e}), skipping", flush=True)
            continue

        model.load_state_dict(weights, strict=False)
        accuracy = gsm8k_eval.score_generations(model, tokenizer, val_data, max_new_tokens=256)
        elapsed = state.checkpoint_elapsed.get(step, 0.0)
        # ここの時刻はチェックポイントが訓練中に保存された時点（=学習経過）を表し、
        # prefixの「現在の経過時間」とは別物なので ckpt@Ns と明示する
        print(
            f"[{_now()}]\t[Peer {PEER_ID}]\t[Main       ]\t  Step {step} (ckpt@{elapsed:.0f}s): "
            f"accuracy = {accuracy:.1f}%", flush=True,
        )

        eval_metric = {
            "type": "eval", "peer_id": PEER_ID, "step": step,
            "elapsed": elapsed, "accuracy": accuracy,
        }
        try:
            state.metrics_queue.put(eval_metric, timeout=5.0)
        except queue.Full:
            pass
        torch.cuda.empty_cache()

    print(f"[{_now()}]\t[Peer {PEER_ID}]\t[Main       ]\tPost-experiment evaluation complete.", flush=True)


def notify_server_evaluation_complete() -> None:
    """実験後評価が完了したことを管理サーバーへ通知する。

    Thread 1（サーバー交信リスナー）はexperiment_stop受信後に接続を閉じて
    終了しているため、ここでは新規に短命なTCP接続を開いて通知のみ送る。
    管理サーバーはこれを受けて、全デバイスの実験・評価完了を検出・ログ出力する。
    """
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(10.0)
            s.connect((SERVER_HOST, SERVER_PORT))
            _send_json(s, {"type": "evaluation_complete", "peer_id": PEER_ID})
        print(f"[{_now()}]\t[Peer {PEER_ID}]\t[Main       ]\tNotified server of evaluation completion.", flush=True)
    except OSError as e:
        print(f"[{_now()}]\t[Peer {PEER_ID}]\t[Main       ]\tFailed to notify server of evaluation completion: {e}", flush=True)


# ============================================================
# メイン
# ============================================================


def _thread_wrapper(state: SharedState, target: Any, args: tuple) -> None:
    """スレッド内の未捕捉例外を検知し、プロセス全体を異常終了させるラッパー。

    デーモンスレッドの例外は当該スレッドを終了させるだけで他スレッドには
    伝播しない。state.running を見ているスレッドは無限ループを続けるため、
    main() の join() が永久にブロックされ、コンテナは「Up」のまま実質停止
    する（学習停止が外部から検知できないゾンビ状態になる）。ここで例外を
    捕捉し、state.running を落とした上でプロセスを即時終了させる。
    """
    import traceback

    try:
        target(*args)
    except Exception:
        print(f"[{_now()}]\t[Peer {PEER_ID}]\t[Main       ]\tFATAL: thread '{target.__name__}' crashed:", flush=True)
        traceback.print_exc()
        state.running = False
        os._exit(1)


def main() -> None:
    """クライアントのメインエントリポイント。"""
    import sys
    global PEER_ID
    PEER_ID = int(os.environ.get("PEER_ID", "0"))

    print(f"[{_now()}]\t[Peer {PEER_ID}]\t[Main       ]\t" + "=" * 60, flush=True)
    print(f"[{_now()}]\t[Peer {PEER_ID}]\t[Main       ]\tWAFL-PEFT Client Starting", flush=True)
    print(f"[{_now()}]\t[Peer {PEER_ID}]\t[Main       ]\tPEER_ID={PEER_ID}", flush=True)
    print(f"[{_now()}]\t[Peer {PEER_ID}]\t[Main       ]\tModel ID: {_get('model', 'model_id')}", flush=True)
    print(f"[{_now()}]\t[Peer {PEER_ID}]\t[Main       ]\tServer: {SERVER_HOST}:{SERVER_PORT}", flush=True)
    print(f"[{_now()}]\t[Peer {PEER_ID}]\t[Main       ]\tP2P Port: {P2P_PORT}", flush=True)
    print(
        f"[{_now()}]\t[Peer {PEER_ID}]\t[Main       ]\tMicro-batch: {_get_int('training', 'batch_size')}, "
        f"grad_accum_steps: {_get_int('training', 'grad_accum_steps', 1)} "
        f"(effective batch = {_get_int('training', 'batch_size') * _get_int('training', 'grad_accum_steps', 1)}), "
        f"lr: {_get_float('training', 'learning_rate')} (warmup {_get_int('training', 'lr_warmup_steps', 0)} steps "
        f"-> cosine decay by time to x{_get_float('training', 'lr_min_ratio', 0.1)}), "
        f"max_seq_len: {_get_int('training', 'max_seq_len')}",
        flush=True,
    )
    print(f"[{_now()}]\t[Peer {PEER_ID}]\t[Main       ]\tEval/Checkpoint Interval: {_get_float('training', 'eval_interval_seconds', 60.0)}s", flush=True)
    print(f"[{_now()}]\t[Peer {PEER_ID}]\t[Main       ]\tStop condition: server experiment_stop signal (time-based, no step limit)", flush=True)
    print(f"[{_now()}]\t[Peer {PEER_ID}]\t[Main       ]\t" + "=" * 60, flush=True)
    sys.stdout.flush()

    # 前回実験のチェックポイントをクリア。WEIGHT_DIR はメトリクスログと異なり
    # 実験名に関わらず固定パス（/app/logs/weights）のため、クリアしないと
    # 実験を繰り返すたびに古いLoRA重みチェックポイントが蓄積し続け、
    # ログ回収・分析のたびに不要な大容量データを転送することになる
    old_ckpts = list(WEIGHT_DIR.glob("weights_step_*.pt"))
    if old_ckpts:
        print(f"[{_now()}]\t[Peer {PEER_ID}]\t[Main       ]\tClearing {len(old_ckpts)} checkpoint(s) from previous run...", flush=True)
        for ckpt in old_ckpts:
            ckpt.unlink(missing_ok=True)

    # このホストに残っている他PEER_ID宛の古いメトリクスログもクリアする。
    # hosts.txtの並び順変更や過去のデプロイミスにより、同じホストが以前
    # 別のpeer_idとして実行された場合、そのログファイル（例:
    # metrics_peer_0_final.log）が削除されずに残り続ける。async_logging_thread
    # は自分のPEER_IDのファイルしか初期化しないため、この残骸はcollect_logs.py
    # によって誤って回収され、analyze.pyがpeer_idをファイル名から判定する際に
    # 別peerのデータを上書きしてしまう不具合を実機テストで確認した。
    stale_logs = [
        f for f in LOG_DIR.glob("metrics_peer_*.log")
        if f.name not in (f"metrics_peer_{PEER_ID}.log", f"metrics_peer_{PEER_ID}_final.log")
    ]
    if stale_logs:
        print(f"[{_now()}]\t[Peer {PEER_ID}]\t[Main       ]\tClearing {len(stale_logs)} stale log(s) from other peer_id(s)...", flush=True)
        for stale_log in stale_logs:
            stale_log.unlink(missing_ok=True)

    # 決定論的シード設定（モデル初期化・データローディング順序の再現性のため、
    # モデルロードより前に行う必要がある）
    seed = set_deterministic_seed(PEER_ID)
    print(f"[{_now()}]\t[Peer {PEER_ID}]\t[Main       ]\tDeterministic seed set: {seed} (data.seed + PEER_ID)", flush=True)

    # モデル・データセットの初期化
    model_id = _get("model", "model_id")
    max_seq_len = _get_int("training", "max_seq_len")
    print(f"[{_now()}]\t[Peer {PEER_ID}]\t[Main       ]\t[1/3] Loading model from {model_id}...", flush=True)
    sys.stdout.flush()
    model, tokenizer = initialize_model()
    print(f"[{_now()}]\t[Peer {PEER_ID}]\t[Main       ]\t[1/3] Model loaded successfully", flush=True)
    sys.stdout.flush()

    print(f"[{_now()}]\t[Peer {PEER_ID}]\t[Main       ]\t[2/3] Loading dataset...", flush=True)
    sys.stdout.flush()
    raw_dataset = load_sharded_dataset(PEER_ID)
    print(f"[{_now()}]\t[Peer {PEER_ID}]\t[Main       ]\t[2/3] Dataset loaded: {len(raw_dataset)} raw samples", flush=True)
    sys.stdout.flush()

    print(f"[{_now()}]\t[Peer {PEER_ID}]\t[Main       ]\t[2/3] Tokenizing {len(raw_dataset)} samples (max_seq_len={max_seq_len})...", flush=True)
    sys.stdout.flush()
    train_data = tokenize_dataset(raw_dataset, tokenizer, max_seq_len)
    print(f"[{_now()}]\t[Peer {PEER_ID}]\t[Main       ]\t[2/3] Tokenized {len(train_data)} training samples (filtered: {len(raw_dataset) - len(train_data)} dropped)", flush=True)
    sys.stdout.flush()

    print(f"[{_now()}]\t[Peer {PEER_ID}]\t[Main       ]\t[3/3] Initializing shared state and threads...", flush=True)
    sys.stdout.flush()

    # 共有状態
    state = SharedState()

    # Thread 4（ロガー）を先に起動
    logger_thread = threading.Thread(
        target=_thread_wrapper, args=(state, async_logging_thread, (state,)), daemon=True
    )
    logger_thread.start()

    # Thread 1（サーバーリスナー）を起動
    listener_thread = threading.Thread(
        target=_thread_wrapper, args=(state, server_listener_thread, (state, model)), daemon=True
    )
    listener_thread.start()

    # Thread 2（P2P交換）を起動
    p2p_thread = threading.Thread(
        target=_thread_wrapper, args=(state, p2p_exchange_thread, (state, model)), daemon=True
    )
    p2p_thread.start()

    # Thread 3（訓練ループ）を起動
    training_thread = threading.Thread(
        target=_thread_wrapper,
        args=(state, training_loop_thread, (state, model, train_data)),
        daemon=True,
    )
    training_thread.start()

    # 全スレッドの終了を待機
    print(f"[{_now()}]\t[Peer {PEER_ID}]\t[Main       ]\tAll threads started. Waiting for experiment...", flush=True)
    sys.stdout.flush()
    for t in [listener_thread, p2p_thread, training_thread]:
        t.join()

    # 評価アーキテクチャは 2 系統を用意している:
    #   (A) 評価専用ホスト（config/hosts.eval.txt の eval_worker.py）が checkpoint を随時評価。
    #   (B) 学習ノード自身が実験後に自己評価（run_post_experiment_evaluation）。
    # 環境（評価ホストの provisioning 可否）に応じて WAFL_SELF_EVAL で切り替える。
    # 既定は自己評価（B）＝評価ホストの disk 制約下でも学習ループを止めないため。
    # 評価ホスト運用時（A）は起動側で WAFL_SELF_EVAL=0 を渡して自己評価を無効化する。
    # いずれの場合も .training_done マーカーは書く（評価ワーカーが完了検出に使う）。
    try:
        (WEIGHT_DIR / ".training_done").write_text(f"peer={PEER_ID}\n")
    except OSError as e:
        print(f"[{_now()}]\t[Peer {PEER_ID}]\t[Main       ]\tFailed to write training-done marker: {e}", flush=True)

    if os.environ.get("WAFL_SELF_EVAL", "1") != "0":
        # 学習ノードで自己評価（GPU は訓練終了で空いている）。結果は Thread 4 がまだ
        # 動いているうちに metrics_queue へ積む必要があるため、シャットダウン前に実行する
        try:
            run_post_experiment_evaluation(state, model, tokenizer)
        except Exception as e:
            print(f"[{_now()}]\t[Peer {PEER_ID}]\t[Main       ]\tPost-experiment evaluation failed: {e}", flush=True)
        notify_server_evaluation_complete()
    else:
        print(f"[{_now()}]\t[Peer {PEER_ID}]\t[Main       ]\tSelf-eval disabled (WAFL_SELF_EVAL=0); evaluation offloaded to eval hosts.", flush=True)

    # logger へシャットダウンシグナル（None = SHUTDOWN）。Thread 4はこのシグネル
    # のみでシャットダウンするため、put_nowait()がqueue.Fullで失敗して
    # シグネルを取りこぼすとThread 4が永久にブロックする。ブロッキングputで
    # 確実に届ける
    state.metrics_queue.put(None, timeout=30.0)
    logger_thread.join()

    print(f"[{_now()}]\t[Peer {PEER_ID}]\t[Main       ]\tAll threads stopped. Client exiting.", flush=True)
    sys.stdout.flush()


if __name__ == "__main__":
    main()
