#!/usr/bin/env python3
"""WAFL-PEFT学習デバイスクライアント。

5スレッド並列アーキテクチャ:
  Thread 1: 管理サーバー交信リスナー（制御プレーン）
  Thread 2: P2P生TCP交換・マージ層（データプレーン）
  Thread 3: LoRA訓練ループ（計算プレーン）
  Thread 4: 非同期ディスク書き出し層（ロギングプレーン）
  Thread 5: 非同期評価スレッド（評価プレーン）。train/testスコア計算
            （model.generate()、実測で数十秒かかる）をThread 3から分離し、
            訓練ループを長時間ブロックしないようにする

メトリクスは訓練ループ内でキューへputし、ロガスレッドが非同期にファイルへ書き出す。
各ステップで loss, throughput をログ出力し、train/test スコアは非同期評価スレッドが
別途 "eval" タイプのレコードとしてログ出力する。
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
from datasets import Dataset
from peft import LoraConfig, get_peft_model
from torch.optim import AdamW
from transformers import AutoConfig, AutoModelForCausalLM, AutoTokenizer, BitsAndBytesConfig

from utils import get_base_dir, get_hosts_path, get_log_dir, _get, _get_float, _get_int, _get_str

# ============================================================
# グローバル設定
# ============================================================

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
    """5スレッド間で共有される状態。

    - peer_whitelist: Thread 1が更新、Thread 2が参照
    - peer_whitelist_expiry: Thread 1が更新、Thread 2が参照（peer_idごとの接続失効時刻）
    - shadow_weights: Thread 2・Thread 3の双方が読み取り専用で参照するCPU上のコピー。
      実体の更新は常にThread 3が行う（Thread 2はマージ計算のみでmodelには触れない）
    - merge_queue: Thread 2がマージ済み重みを書き込み、Thread 3がステップ境界で消費して
      model本体に反映する（Thread 2がmodel.load_state_dict()を直接呼ぶと、Thread 3の
      forward/backward/optimizer.stepの実行中にパラメータが書き換わるデータ競合が
      発生するため、反映は必ずThread 3側で行う）
    - eval_request_queue: Thread 3が評価すべきstep番号を書き込み、Thread 5が消費して
      train/testスコアを計算する（model.generate()は数十秒かかることがあり、
      Thread 3内で直列に行うと訓練が長時間完全停止するストールを引き起こすため、
      評価専用スレッドに分離した）
    - metrics_queue: Thread 3・Thread 5が書き込み、Thread 4が消費（キュー満杯時はブロック）
    """

    def __init__(self) -> None:
        self.peer_whitelist: dict[int, float] = {}
        self.peer_whitelist_expiry: dict[int, float] = {}
        self.whitelist_lock = threading.Lock()

        self.shadow_weights: dict[str, torch.Tensor] | None = None
        self.weights_lock = threading.Lock()

        self.merge_queue: queue.Queue[dict[str, torch.Tensor]] = queue.Queue(maxsize=32)

        # maxsize=1: evaluate_batch()は約87秒かかり、eval_freq間隔（実測で
        # 数十秒程度）より遅くなりうる。複数積むと古い評価要求のために
        # Thread 5が延々と処理を続け、実験終了後もいつまでも終わらなくなる
        # ため、常に最新のステップだけを評価対象として保持する
        self.eval_request_queue: queue.Queue[int] = queue.Queue(maxsize=1)

        self.metrics_queue: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=8192)

        self.step_lock = threading.Lock()
        self.current_step: int = 0

        self.experiment_running = threading.Event()
        self.experiment_start_time: float = 0.0
        self.elapsed_time: float = 0.0

        # サーバーへのReady送信を同期
        self.ready_to_send = threading.Event()

        # Thread 5（非同期評価）が完全に終了したことをThread 4へ伝える。
        # Thread 4は「runningがFalse かつ metrics_queueが空」だけで自発的に
        # シャットダウンする経路を持つため、running=False になった直後に
        # 評価処理中でキューが一時的に空のタイミングを迎えると、Thread 5が
        # 後から送るeval結果を誰も受信しなくなる（実機テストで実際に発生した）。
        # これを防ぐため、Thread 5が完全に終わるまではシャットダウンを待たせる。
        self.eval_thread_done = threading.Event()

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
        print(f"[Peer {PEER_ID}]   Using local model from {local_model_path}", flush=True)
    else:
        print(f"[Peer {PEER_ID}]   Local model not found, downloading from {model_id}...", flush=True)
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
        print(f"[Peer {PEER_ID}]   Using 4-bit NF4 quantization (QLoRA)", flush=True)
    else:
        _device_map = "cpu"
        _quantization_config = None
        print(f"[Peer {PEER_ID}]   No GPU found, loading in float16 on CPU", flush=True)

    model = AutoModelForCausalLM.from_pretrained(
        str(local_model_path) if local_model_path.exists() else model_id,
        torch_dtype=torch.float16,
        device_map=_device_map,
        quantization_config=_quantization_config,
        trust_remote_code=True,
    )
    print(f"[Peer {PEER_ID}]   Model weights loaded, device={next(model.parameters()).device}", flush=True)
    sys.stdout.flush()

    lora_config = LoraConfig(
        r=_get_int("training", "lora_rank"),
        lora_alpha=_get_int("training", "lora_alpha"),
        target_modules=r"model\.language_model.*(?:self_attn\.(?:q_proj|k_proj|v_proj|o_proj)|mlp\.(?:gate_proj|up_proj|down_proj))",
        lora_dropout=0.05,
        bias="none",
        task_type="CAUSAL_LM",
    )

    print(f"[Peer {PEER_ID}]   Applying LoRA (rank={_get_int('training', 'lora_rank')}, alpha={_get_int('training', 'lora_alpha')})...", flush=True)
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
    print(f"[Peer {PEER_ID}]   LoRA applied, target_device={_fallback_device}", flush=True)

    print(f"[Peer {PEER_ID}]   Loading tokenizer...", flush=True)
    sys.stdout.flush()
    tokenizer = AutoTokenizer.from_pretrained(
        str(local_model_path) if local_model_path.exists() else model_id,
    )
    print(f"[Peer {PEER_ID}]   Tokenizer loaded", flush=True)
    sys.stdout.flush()

    return model, tokenizer


def load_sharded_dataset(peer_id: int) -> Dataset:
    """peer固有の訓練ファイルを読み込み。"""
    train_file = BASE_DIR / "data" / "train" / f"peer_{peer_id}.json"
    with open(train_file) as f:
        data = json.load(f)
    samples = data.get("samples", [])
    print(f"[Peer {peer_id}] Loaded {len(samples)} samples from {train_file}")
    return Dataset.from_list(samples)


def tokenize_dataset(
    dataset: Dataset,
    tokenizer: Any,
    max_seq_len: int,
) -> list[dict[str, torch.Tensor]]:
    """データセットをトークン化し、入力・ラベルペアに変換。"""
    tokenized: list[dict[str, torch.Tensor]] = []
    for item in dataset:
        text = f"Question: {item['question']}\nAnswer: {item['answer']}"
        tokens = tokenizer(
            text,
            truncation=True,
            max_length=max_seq_len,
            padding=False,
        )
        if len(tokens["input_ids"]) > 1:
            input_ids = torch.tensor(tokens["input_ids"], dtype=torch.long)
            labels = input_ids.clone()
            tokenized.append({"input_ids": input_ids, "labels": labels})
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
    print(f"[Peer {PEER_ID}] Thread 1 (Server Listener) started. Connecting to {SERVER_HOST}:{SERVER_PORT}...", flush=True)

    while state.running:
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.settimeout(2.0)
                print(f"[Peer {PEER_ID}]   Connecting to server...", flush=True)
                s.connect((SERVER_HOST, SERVER_PORT))
                print(f"[Peer {PEER_ID}]   Connected to server", flush=True)

                # ペアID登録
                if not _send_json(s, {"type": "register", "peer_id": PEER_ID}):
                    print(f"[Peer {PEER_ID}]   Failed to send registration, retrying...", flush=True)
                    time.sleep(1.0)
                    continue
                print(f"[Peer {PEER_ID}]   Registration sent (peer_id={PEER_ID})", flush=True)

                # モデルロード完了を待ってReadyを送信
                print(f"[Peer {PEER_ID}]   Waiting for model load to complete (timeout=60s)...", flush=True)
                state.ready_to_send.wait(timeout=60.0)
                _send_json(s, {"type": "ready", "peer_id": PEER_ID})
                print(f"[Peer {PEER_ID}]   Ready signal sent. Waiting for signals...", flush=True)

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
                        # ホワイトリスト更新
                        peers_data = data.get("peers", {})
                        new_whitelist: dict[int, float] = {}
                        if str(PEER_ID) in peers_data:
                            allowed = peers_data[str(PEER_ID)].get("allowed_peers", [])
                            for entry in allowed:
                                pid = entry["peer_id"]
                                rt = entry["remaining_time"]
                                new_whitelist[pid] = rt
                        now = time.time()
                        with state.whitelist_lock:
                            whitelist_changed = new_whitelist != state.peer_whitelist
                            # remaining_time が前回受信時と異なるpeerのみ、新しい接触の
                            # 開始とみなして失効時刻を再計算する。サーバーは同一の接触
                            # イベントを1秒周期で繰り返し送信するため、値が同じ間は既存の
                            # 失効時刻を保持しないと、受信するたびに期限が延び続けてしまう
                            for pid, rt in new_whitelist.items():
                                if state.peer_whitelist.get(pid) != rt:
                                    state.peer_whitelist_expiry[pid] = now + rt
                            for pid in list(state.peer_whitelist_expiry.keys()):
                                if pid not in new_whitelist:
                                    del state.peer_whitelist_expiry[pid]
                            state.peer_whitelist = new_whitelist

                        # 接触イベントのタイムスタンプ付きログ（contact_pattern.jsonとの
                        # 実時刻整合性を事後検証できるように、変化があった時のみ記録する）
                        if whitelist_changed:
                            contact_elapsed = (
                                now - state.experiment_start_time
                                if state.experiment_start_time
                                else 0.0
                            )
                            contact_event = {
                                "type": "contact_event",
                                "peer_id": PEER_ID,
                                "elapsed": contact_elapsed,
                                "allowed_peers": sorted(new_whitelist.keys()),
                            }
                            try:
                                state.metrics_queue.put(contact_event, timeout=1.0)
                            except queue.Full:
                                pass

                    elif msg_type == "experiment_start":
                        state.experiment_start_time = time.time()
                        state.experiment_running.set()
                        print(
                            f"[Peer {PEER_ID}] Experiment STARTED. "
                            f"Duration: {data.get('duration')}s",
                            flush=True
                        )

                    elif msg_type == "experiment_stop":
                        print(f"[Peer {PEER_ID}] Experiment STOPPED.", flush=True)
                        state.running = False
                        break

        except (OSError, ConnectionError) as e:
            if state.running:
                print(f"[Peer {PEER_ID}] Server connection lost ({e}). Reconnecting in 1s...", flush=True)
                time.sleep(1.0)

    print(f"[Peer {PEER_ID}] Thread 1 (Server Listener) stopped")


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
    - 失効した（remaining_time経過後の）peerとの接続は切断し、時変トポロジーの
      ローテーションを実際に反映する
    - シャドウコピーを読み出して送信
    - 受信データを一時バッファへ格納し、マージ計算のみを行う
    - マージ結果はmerge_queueに渡すだけで、model本体には触れない
      （実際の反映はThread 3がステップ境界で行う。C1/C2参照）
    """
    print(f"[Peer {PEER_ID}] Thread 2 (P2P Exchange) started")

    # peer_id -> IP マッピング
    host_map = resolve_hosts()

    # 受信バッファ: peer_id -> list of serialized weights
    receive_buffers: dict[int, list[bytes]] = {}
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
                        if incoming_peer_id not in receive_buffers:
                            receive_buffers[incoming_peer_id] = []
                        receive_buffers[incoming_peer_id].append(weight_data)

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
        """指定peerとの送信用・受信用接続を両方クローズする。"""
        with conn_lock:
            out_conn = active_connections.pop(pid, None)
            in_conn = incoming_connections.pop(pid, None)
        for c in (out_conn, in_conn):
            if c is not None:
                try:
                    c.close()
                except OSError:
                    pass
        with receive_lock:
            receive_buffers.pop(pid, None)

    last_merge_step: int = -1

    # 期限切れとして既に切断・ログ出力済みのpid集合（Thread 2内でのみ保持する）。
    # state.peer_whitelist / state.peer_whitelist_expiry はThread 1が「前回受信した
    # remaining_time値」を判定するための唯一の書き込み者であり、Thread 2側から
    # popして書き換えると、Thread 1が「値が消えた＝新しい接触」と誤認して
    # 同じ接触のexpiryを再計算し続け、数秒おきに切断・再接続を無限に繰り返す
    # 不具合になる（実機テストで実際に発生した）。Thread 2は読み取り専用に徹し、
    # 「今どのpidが期限切れか」はこのローカル集合で自前管理する。
    disconnected_expired: set[int] = set()

    while state.running:
        # ホワイトリストと失効時刻を取得（読み取り専用）
        now = time.time()
        with state.whitelist_lock:
            raw_whitelist = dict(state.peer_whitelist)
            expiry = dict(state.peer_whitelist_expiry)

        # 失効した（remaining_time経過後の）peerを有効なホワイトリストから除外し、
        # 既存の接続も切断する。これにより時変トポロジーのローテーションが
        # 実際の接続状態に反映される
        expired_pids = {pid for pid in raw_whitelist if now >= expiry.get(pid, float("inf"))}
        newly_expired = expired_pids - disconnected_expired
        for pid in newly_expired:
            _close_peer_connections(pid)
            print(f"[Peer {PEER_ID}] Contact with peer {pid} expired. Disconnected.", flush=True)
        # 新たに検出したexpired pidを追跡対象へ追加し、
        # 再接続可能になった（whitelistから消えた、または値の更新でexpiryが
        # 未来に更新された）pidは追跡対象から外す
        disconnected_expired = (disconnected_expired | newly_expired) & expired_pids
        whitelist = {pid: rt for pid, rt in raw_whitelist.items() if pid not in expired_pids}

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
                    print(f"[Peer {PEER_ID}]   Skipping peer {pid}: not in hosts.txt", flush=True)
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
                print(f"[Peer {PEER_ID}] P2P connected to peer {pid} ({peer_ip})")
            except OSError:
                pass

        # 接続済みpeerへ重みを送信
        current_step = state.current_step
        if state.shadow_weights is not None and current_step != last_merge_step:
            with state.weights_lock:
                serialized = _serialize_weights(state.shadow_weights)
            with conn_lock:
                peers_to_send = list(active_connections.keys())
            for pid in peers_to_send:
                try:
                    conn = active_connections[pid]
                    # 長さヘッダ + 重みデータ。send()は要求バイト数より少なく
                    # 送って早期に返ることがあるため、sendall()で確実に送り切る
                    conn.sendall(len(serialized).to_bytes(4, "big"))
                    conn.sendall(serialized)
                except OSError:
                    with conn_lock:
                        active_connections.pop(pid, None)

        # 受信バッファのマージ（訓練イテレーションの境界で実行）
        # ここではCPU上のテンソルを平均化するだけで、model・shadow_weightsには
        # 一切触れない。反映はThread 3がステップ境界で安全に行う
        if current_step != last_merge_step and current_step > 0:
            with receive_lock:
                buffers_to_merge = dict(receive_buffers)
                receive_buffers.clear()

            if buffers_to_merge:
                merged: dict[str, torch.Tensor] | None = None
                count = 0
                for buf_data in buffers_to_merge.values():
                    for weight_bytes in buf_data:
                        try:
                            remote_weights = _deserialize_weights(weight_bytes)
                            if merged is None:
                                merged = {
                                    k: v.float() for k, v in remote_weights.items()
                                }
                                count = 1
                            else:
                                for k in merged:
                                    if k in remote_weights:
                                        merged[k] += remote_weights[k].float()
                                count += 1
                        except (EOFError, RuntimeError, KeyError):
                            continue

                if merged is not None and count > 0:
                    for k in merged:
                        merged[k] /= count

                    try:
                        state.merge_queue.put(merged, timeout=1.0)
                        last_merge_step = current_step
                        print(
                            f"[Peer {PEER_ID}] Queued merged weights from {count} peers "
                            f"at step {current_step}"
                        )
                    except queue.Full:
                        print(
                            f"[Peer {PEER_ID}] merge_queue full, dropping merge "
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

    print(f"[Peer {PEER_ID}] Thread 2 (P2P Exchange) stopped")


# ============================================================
# 簡易評価（train/test スコア）
# ============================================================


def evaluate_batch(
    model: Any,
    tokenizer: Any,
    samples: list[dict[str, str]],
    max_seq_len: int,
    max_eval_samples: int = 5,
) -> float:
    """簡易accuracy評価（GSM8K形式のquestion/answer）。

    返値: accuracy（%）
    """
    correct = 0
    total = min(len(samples), max_eval_samples)
    for item in samples[:total]:
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

    return (correct / total * 100) if total > 0 else 0.0


# ============================================================
# Thread 5: 非同期評価スレッド（評価プレーン）
# ============================================================


def async_eval_thread(
    state: SharedState,
    model: Any,
    tokenizer: Any,
    train_samples: list[dict[str, str]],
    test_samples: list[dict[str, str]],
) -> None:
    """train/testスコアを非同期に評価するスレッド。

    evaluate_batch()内のmodel.generate()はGPU計算を要し、実機テストでは
    train/test合計10サンプルの評価に約87秒かかることが確認された。
    Training Loop Thread（Thread 3）内で直列に実行すると、その間訓練が
    完全に停止するストールを引き起こすため、専用スレッドに分離し、
    訓練と並行して評価を進める。model・tokenizerはThread 3と共有するため
    厳密に同期していないパラメータで評価することになるが、進捗モニタリング
    目的の評価であり、多少のタイミングのずれは許容する。
    """
    print(f"[Peer {PEER_ID}] Thread 5 (Async Evaluator) started", flush=True)
    max_seq_len = _get_int("training", "max_seq_len")

    while state.running or not state.eval_request_queue.empty():
        try:
            step = state.eval_request_queue.get(timeout=1.0)
        except queue.Empty:
            continue

        train_score = evaluate_batch(model, tokenizer, train_samples, max_seq_len)
        test_score = evaluate_batch(model, tokenizer, test_samples, max_seq_len)
        import gc
        gc.collect()
        torch.cuda.empty_cache()

        eval_metric = {
            "type": "eval",
            "peer_id": PEER_ID,
            "step": step,
            "elapsed": state.elapsed_time,
            "train_score": train_score,
            "test_score": test_score,
        }
        try:
            state.metrics_queue.put(eval_metric, timeout=1.0)
        except queue.Full:
            pass
        print(
            f"[Peer {PEER_ID}] Async eval at step {step}: "
            f"train={train_score:.1f}%, test={test_score:.1f}%",
            flush=True,
        )

    state.eval_thread_done.set()
    print(f"[Peer {PEER_ID}] Thread 5 (Async Evaluator) stopped")


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
    train/testスコアの評価はThread 5（非同期評価スレッド）へ依頼するのみで、
    このスレッド自体は行わない（evaluate_batchは重く、直列実行はストールの
    原因になるため）。
    """
    print(f"[Peer {PEER_ID}] Thread 3 (Training Loop) started", flush=True)

    # LoRA パラメータを named_parameters() 経由で収集する。
    # state_dict() はディスクオフロードモデルで全重みをロードしてしまう可能性があるが、
    # named_parameters() は訓練可能なパラメータのみ実体化する。
    lora_param_dict = {
        name: param
        for name, param in model.named_parameters()
        if "lora" in name.lower()
    }
    lora_keys = list(lora_param_dict.keys())
    print(f"[Peer {PEER_ID}]   Cloning {len(lora_keys)} LoRA weight tensors to CPU...", flush=True)
    with state.weights_lock:
        state.shadow_weights = {
            k: lora_param_dict[k].detach().cpu().clone() for k in lora_keys
        }
    print(f"[Peer {PEER_ID}] Model loaded, shadow weights ready (LoRA keys: {len(lora_keys)})")
    state.ready_to_send.set()

    # GPU 環境では LoRA パラメータがある device に inputs を置く。
    # disk-offload 環境では cpu になるため、そのままで問題ない。
    _train_device = next(
        (p.device for p in model.parameters() if p.requires_grad and p.device.type != "meta"),
        torch.device("cpu"),
    )
    print(f"[Peer {PEER_ID}]   Training device: {_train_device}", flush=True)

    optimizer = AdamW(
        [p for p in model.parameters() if p.requires_grad],
        lr=_get_float("training", "learning_rate"),
        weight_decay=0.01,
    )

    # メトリクスは全ステップをログ出力。チェックポイント（LoRA全重み保存）は
    # train/testスコア評価と同じ間隔でのみ保存する（毎ステップ保存すると長時間・
    # 大規模実験でディスクを圧迫するため）
    eval_freq = max(1, _get_int("training", "num_training_steps") // 10)
    checkpoint_freq = eval_freq
    step = 0
    total_tokens = 0
    start_wait = time.time()

    while state.running:
        if not state.experiment_running.is_set():
            time.sleep(0.1)
            continue

        step_start_time = time.time()

        print(f"[Peer {PEER_ID}] Training step {step} (train_data len={len(train_data)})", flush=True)

        # 経過時間を更新
        state.elapsed_time = time.time() - state.experiment_start_time

        # データローディング（inputs を LoRA device へ）
        idx = step % len(train_data)
        batch = train_data[idx]
        input_ids = batch["input_ids"].unsqueeze(0).to(_train_device)
        labels = batch["labels"].unsqueeze(0).to(_train_device)

        # フォワードパス（ここから optimizer.step() 完了までを「純計算時間」として計測する。
        # EmbRace の Computation Stall 指標に倣い、この区間外（マージ反映・GPU解放・
        # 評価など）にかかった時間を stall_duration として分離し、通信・マージ処理が
        # 計算をブロックしていないことを事後検証できるようにする）
        compute_start_time = time.time()
        optimizer.zero_grad()
        outputs = model(input_ids=input_ids, labels=labels)
        loss = outputs.loss
        total_tokens += int(input_ids.numel())

        # バックワードパス
        loss.backward()

        # optimizerステップ
        with state.step_lock:
            state.current_step = step + 1
            current_step_for_merge = step + 1

        optimizer.step()
        compute_duration = time.time() - compute_start_time

        # P2Pマージ結果の反映。Thread 2はマージ計算のみ行いmerge_queueに積む
        # だけなので、model本体への書き込みは常にここ（Thread 3、かつ
        # forward/backward/optimizer.stepが完了した安全なタイミング）でのみ行う。
        # これにより計算中にパラメータが書き換わるデータ競合を構造的に防ぐ
        try:
            merged_weights = state.merge_queue.get_nowait()
        except queue.Empty:
            merged_weights = None

        if merged_weights is not None:
            with torch.no_grad():
                for name, param in model.named_parameters():
                    if name in merged_weights:
                        param.copy_(merged_weights[name].to(param.dtype).to(param.device))

        # GPUメモリ解放（断片化防止。evaluate_batch後の解放と合わせて重要）
        import gc
        gc.collect()
        torch.cuda.empty_cache()

        # シャドウコピー更新（named_parameters 経由で LoRA のみ更新）
        with state.weights_lock:
            if state.shadow_weights is not None:
                for name, param in model.named_parameters():
                    if name in state.shadow_weights:
                        state.shadow_weights[name].copy_(param.detach().cpu())

        # メトリクス計算
        loss_value = loss.item()
        step_duration = time.time() - step_start_time
        # stall_duration: ステップ全体からforward/backward/optimizer.stepの
        # 純計算時間を除いた残り（マージ反映・GPU解放・評価などのオーバーヘッド）。
        # 通信・マージが計算をブロックしていないかを事後検証するための指標
        stall_duration = max(0.0, step_duration - compute_duration)
        tokens_per_sec = (
            total_tokens / (time.time() - start_wait) if time.time() > start_wait else 0
        )
        state.elapsed_time = time.time() - state.experiment_start_time

        # GPU使用率（SM utilization %）。CPU環境やNVML未対応環境では取得できない
        # ことがあるため、計測補助機能としてどの例外でも計測をクラッシュさせない
        gpu_util_percent = None
        if torch.cuda.is_available():
            try:
                gpu_util_percent = torch.cuda.utilization()
            except Exception:
                gpu_util_percent = None

        # 平均ステップ時間と残り推定
        max_steps = _get_int("training", "num_training_steps", 10000)
        avg_step_sec = state.elapsed_time / current_step_for_merge if current_step_for_merge > 0 else 0
        remaining_steps = max_steps - current_step_for_merge
        eta_sec = avg_step_sec * remaining_steps

        eta_str = _eta_str(eta_sec)
        print(
            f"[Peer {PEER_ID}] Step {current_step_for_merge}: "
            f"loss={loss_value:.4f}, tok/s={tokens_per_sec:.1f}, "
            f"step={step_duration:.1f}s, elapsed={state.elapsed_time:.0f}s "
            f"({_eta_str(state.elapsed_time)}), "
            f"ETA={eta_str} ({remaining_steps}steps left)",
            flush=True,
        )

        # 定期的にtrain/testスコアの評価をリクエストする。評価自体（model.generate()）は
        # 数十秒かかることがあり、ここで直列に実行すると訓練が長時間完全停止する
        # ストールを引き起こすことが実機テストで確認されたため、Thread 5（非同期評価
        # スレッド）へ依頼するだけにとどめる。結果は別途"eval"タイプのレコードとして
        # 記録されるため、ここでは常にtrain_score=test_score=0.0を記録する
        if current_step_for_merge % eval_freq == 0 and current_step_for_merge > 0:
            # キュー（maxsize=1）が満杯なら、Thread 5がまだ古い要求を処理中
            # ということ。古い要求を待たず、常に最新のステップを優先する
            try:
                state.eval_request_queue.get_nowait()
            except queue.Empty:
                pass
            try:
                state.eval_request_queue.put_nowait(current_step_for_merge)
            except queue.Full:
                pass

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
            "gpu_util_percent": gpu_util_percent,
            "train_score": 0.0,
            "test_score": 0.0,
        }
        try:
            state.metrics_queue.put(metric, timeout=1.0)
        except queue.Full:
            pass

        # チェックポイント保存（checkpoint_freq間隔 + 最終ステップ）
        is_last_step = current_step_for_merge >= max_steps
        if current_step_for_merge > 0 and (current_step_for_merge % checkpoint_freq == 0 or is_last_step):
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
            print(
                f"[Peer {PEER_ID}] Step {current_step_for_merge}: "
                f"loss={loss_value:.4f}, tok/s={tokens_per_sec:.1f}, "
                f"elapsed={state.elapsed_time:.0f}s, "
                f"saved {ckpt_path}", flush=True
            )

        step += 1

        # 訓練ステップ数制限
        max_steps = _get_int("training", "num_training_steps", 10000)
        if step >= max_steps:
            print(f"[Peer {PEER_ID}] Reached max steps ({max_steps}). Stopping.", flush=True)
            state.running = False
            break

    print(f"[Peer {PEER_ID}] Thread 3 (Training Loop) stopped")


# ============================================================
# Thread 4: 非同期ディスク書き出し層（ロギングプレーン）
# ============================================================


def async_logging_thread(state: SharedState) -> None:
    """キュー経由でメトリクスとチェックポイントを非同期にディスクへ書き出し。

    SHUTDOWN シグネル（None）を受信するまでキューから読み取り、
    実験終了後は残りのメトリクスを全てフラッシュしてから終了する。
    """
    print(f"[Peer {PEER_ID}] Thread 4 (Async Logger) started")

    metric_log_path = LOG_DIR / f"metrics_peer_{PEER_ID}.log"
    metric_log_path.write_text("")

    received_count = 0
    while True:
        try:
            metric = state.metrics_queue.get(timeout=1.0)
        except queue.Empty:
            # 実験終了 + Thread 5（評価）も完全終了 + キュー空 → シャットダウン。
            # Thread 5の完了を待たずにrunning=Falseだけで判定すると、Thread 5が
            # まだ評価処理中（最大87秒）でキューが一時的に空になった瞬間に
            # 早期シャットダウンしてしまい、後から届くeval結果を誰も受信
            # できなくなる（実機テストで実際に発生し、eval結果が0件になった）
            if not state.running and state.eval_thread_done.is_set() and state.metrics_queue.empty():
                print(f"[Peer {PEER_ID}] Logger: shutting down (received={received_count})", flush=True)
                break
            continue

        # SHUTDOWN シグネル
        if metric is None:
            break

        # メトリクスをログファイルへ追記
        try:
            with open(metric_log_path, "a") as f:
                f.write(json.dumps(metric, default=str) + "\n")
                f.flush()
                os.fsync(f.fileno())

            if metric.get("type") == "checkpoint":
                print(
                    f"[Peer {PEER_ID}] Logger: checkpoint saved at step "
                    f"{metric.get('step')} (loss={metric.get('loss'):.4f})"
                )
        except OSError as e:
            print(f"[Peer {PEER_ID}] Logger error: {e}", file=sys.stderr)

    # 最終フラッシュ
    final_log_path = LOG_DIR / f"metrics_peer_{PEER_ID}_final.log"
    try:
        metric_log_path.rename(final_log_path)
    except OSError:
        pass

    print(f"[Peer {PEER_ID}] Thread 4 (Async Logger) stopped")


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
        print(f"[Peer {PEER_ID}] FATAL: thread '{target.__name__}' crashed:", flush=True)
        traceback.print_exc()
        state.running = False
        os._exit(1)


def main() -> None:
    """クライアントのメインエントリポイント。"""
    import sys
    global PEER_ID
    PEER_ID = int(os.environ.get("PEER_ID", "0"))

    print(f"=" * 60, flush=True)
    print(f"[Peer {PEER_ID}] WAFL-PEFT Client Starting", flush=True)
    print(f"[Peer {PEER_ID}] PEER_ID={PEER_ID}", flush=True)
    print(f"[Peer {PEER_ID}] Model ID: {_get('model', 'model_id')}", flush=True)
    print(f"[Peer {PEER_ID}] Server: {SERVER_HOST}:{SERVER_PORT}", flush=True)
    print(f"[Peer {PEER_ID}] P2P Port: {P2P_PORT}", flush=True)
    print(f"[Peer {PEER_ID}] Batch Size: {_get_int('training', 'batch_size')}", flush=True)
    print(f"[Peer {PEER_ID}] Max Steps: {_get_int('training', 'num_training_steps', 10000)}", flush=True)
    print(f"=" * 60, flush=True)
    sys.stdout.flush()

    # 前回実験のチェックポイントをクリア。WEIGHT_DIR はメトリクスログと異なり
    # 実験名に関わらず固定パス（/app/logs/weights）のため、クリアしないと
    # 実験を繰り返すたびに古いLoRA重みチェックポイントが蓄積し続け、
    # ログ回収・分析のたびに不要な大容量データを転送することになる
    old_ckpts = list(WEIGHT_DIR.glob("weights_step_*.pt"))
    if old_ckpts:
        print(f"[Peer {PEER_ID}] Clearing {len(old_ckpts)} checkpoint(s) from previous run...", flush=True)
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
        print(f"[Peer {PEER_ID}] Clearing {len(stale_logs)} stale log(s) from other peer_id(s)...", flush=True)
        for stale_log in stale_logs:
            stale_log.unlink(missing_ok=True)

    # 決定論的シード設定（モデル初期化・データローディング順序の再現性のため、
    # モデルロードより前に行う必要がある）
    seed = set_deterministic_seed(PEER_ID)
    print(f"[Peer {PEER_ID}] Deterministic seed set: {seed} (data.seed + PEER_ID)", flush=True)

    # モデル・データセットの初期化
    model_id = _get("model", "model_id")
    max_seq_len = _get_int("training", "max_seq_len")
    print(f"[Peer {PEER_ID}] [1/3] Loading model from {model_id}...", flush=True)
    sys.stdout.flush()
    model, tokenizer = initialize_model()
    print(f"[Peer {PEER_ID}] [1/3] Model loaded successfully", flush=True)
    sys.stdout.flush()

    print(f"[Peer {PEER_ID}] [2/3] Loading dataset...", flush=True)
    sys.stdout.flush()
    raw_dataset = load_sharded_dataset(PEER_ID)
    print(f"[Peer {PEER_ID}] [2/3] Dataset loaded: {len(raw_dataset)} raw samples", flush=True)
    sys.stdout.flush()

    print(f"[Peer {PEER_ID}] [2/3] Tokenizing {len(raw_dataset)} samples (max_seq_len={max_seq_len})...", flush=True)
    sys.stdout.flush()
    train_data = tokenize_dataset(raw_dataset, tokenizer, max_seq_len)
    print(f"[Peer {PEER_ID}] [2/3] Tokenized {len(train_data)} training samples (filtered: {len(raw_dataset) - len(train_data)} dropped)", flush=True)
    sys.stdout.flush()

    # 訓練サンプルを辞書形式に変換（評価用）
    train_samples: list[dict[str, str]] = [
        {"question": item["question"], "answer": item["answer"]}
        for item in raw_dataset
    ]

    # テストデータセット読み込み（評価用）
    test_file = BASE_DIR / "data" / "test" / f"peer_{PEER_ID}.json"
    if test_file.exists():
        with open(test_file) as f:
            test_data = json.load(f)
        test_samples = test_data.get("samples", [])
        print(f"[Peer {PEER_ID}] [3/3] Test samples loaded: {len(test_samples)}")
    else:
        test_samples = []
        print(f"[Peer {PEER_ID}] [3/3] No test file found, evaluation skipped")
    sys.stdout.flush()

    print(f"[Peer {PEER_ID}] [4/4] Initializing shared state and threads...", flush=True)
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

    # Thread 5（非同期評価）を起動
    eval_thread = threading.Thread(
        target=_thread_wrapper,
        args=(state, async_eval_thread, (state, model, tokenizer, train_samples, test_samples)),
        daemon=True,
    )
    eval_thread.start()

    # 全スレッドの終了を待機
    print(f"[Peer {PEER_ID}] All threads started. Waiting for experiment...", flush=True)
    sys.stdout.flush()
    for t in [listener_thread, p2p_thread, training_thread]:
        t.join()

    # Thread 5はstate.runningがFalseになった後もeval_request_queueが空になるまで
    # 動き続けてmetrics_queueへ書き込むため、Thread 4へのシャットダウン通知より
    # 必ず先に完了を待つ（先にシャットダウンすると評価結果がファイルに書かれない）
    eval_thread.join()

    # logger へシャットダウンシグナル（None = SHUTDOWN）
    try:
        state.metrics_queue.put_nowait(None)
    except queue.Full:
        pass
    logger_thread.join()

    print(f"[Peer {PEER_ID}] All threads stopped. Client exiting.", flush=True)
    sys.stdout.flush()


if __name__ == "__main__":
    main()
