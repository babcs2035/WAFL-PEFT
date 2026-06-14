#!/usr/bin/env python3
"""WAFL-PEFT学習デバイスクライアント。

4スレッド並列アーキテクチャ:
  Thread 1: 管理サーバー交信リスナー（制御プレーン）
  Thread 2: P2P生TCP交換・マージ層（データプレーン）
  Thread 3: LoRA訓練ループ（計算プレーン）
  Thread 4: 非同期ディスク書き出し層（ロギングプレーン）

メトリクスは訓練ループ内でキューへputし、ロガスレッドが非同期にファイルへ書き出す。
各ステップで loss, throughput, train/test スコアをログ出力。
"""

import json
import os
import queue
import socket
import sys
import threading
import time
from pathlib import Path
from typing import Any

import torch
from datasets import Dataset
from peft import LoraConfig, get_peft_model
from torch.optim import AdamW
from transformers import AutoConfig, AutoModelForCausalLM, AutoTokenizer

from utils import get_base_dir, get_hosts_path, get_log_dir, _get, _get_float, _get_int, _get_str

# ============================================================
# グローバル設定
# ============================================================

# コンテナ内では /app がプロジェクトルート（ホストの DEPLOY_DIR にマッピング）
BASE_DIR = Path("/app")
SERVER_HOST = _get_str("server", "server_host")  # 管理サーバーのホスト名（wafl-ctrl1等）
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

# ============================================================
# 共有状態（スレッド間データ構造）
# ============================================================


class SharedState:
    """4スレッド間で共有される状態。

    - peer_whitelist: Thread 1が更新、Thread 2が参照
    - shadow_weights: Thread 3が更新、Thread 2が読み取り
    - merge_queue: Thread 2が書き込み、Thread 3が消費
    - metrics_queue: Thread 3が書き込み、Thread 4が消費（キュー満杯時はブロック）
    """

    def __init__(self) -> None:
        self.peer_whitelist: dict[int, float] = {}
        self.whitelist_lock = threading.Lock()

        self.shadow_weights: dict[str, torch.Tensor] | None = None
        self.weights_lock = threading.Lock()

        self.merge_queue: queue.Queue[dict[int, dict[str, torch.Tensor]]] = queue.Queue(
            maxsize=32
        )

        self.metrics_queue: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=8192)

        self.step_lock = threading.Lock()
        self.current_step: int = 0

        self.experiment_running = threading.Event()
        self.experiment_start_time: float = 0.0
        self.elapsed_time: float = 0.0

        # サーバーへのReady送信を同期
        self.ready_to_send = threading.Event()

        self.running = True


# ============================================================
# モデル・データセットの初期化
# ============================================================


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

    model = AutoModelForCausalLM.from_pretrained(
        str(local_model_path) if local_model_path.exists() else model_id,
        torch_dtype=torch.float16,
        device_map="auto",
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
    model = get_peft_model(model, lora_config)
    model.print_trainable_parameters()

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
                    data = _recv_json(s, timeout=2.0)
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
                        with state.whitelist_lock:
                            state.peer_whitelist = new_whitelist

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


def _serialize_weights(state_dict: dict[str, torch.Tensor]) -> bytes:
    """state_dictをpickle + gzip圧縮してバイナリ化。"""
    import gzip
    import io

    buf = io.BytesIO()
    torch.save(state_dict, buf)
    return gzip.compress(buf.getvalue())


def _deserialize_weights(data: bytes) -> dict[str, torch.Tensor]:
    """gzip圧縮pickleバイナリをstate_dictに復元。"""
    import gzip
    import io

    buf = io.BytesIO(gzip.decompress(data))
    return torch.load(buf, weights_only=True, map_location="cpu")


def p2p_exchange_thread(state: SharedState, model: Any) -> None:
    """P2P重み交換スレッド。

    - ホワイトリストを監視し、接続可能なpeerへ積極的に接続
    - シャドウコピーを読み出して送信
    - 受信データを一時バッファへ格納
    - 訓練イテレーションの境界でマージを実行
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

    # 接続管理
    active_connections: dict[int, socket.socket] = {}
    conn_lock = threading.Lock()

    # 受信ハンドラスレッド
    def accept_incoming() -> None:
        """着信P2P接続を受付け。"""
        while state.running:
            try:
                conn, addr = p2p_socket.accept()
                conn.settimeout(30.0)

                # peer_idを受信
                header = conn.recv(4)
                if not header:
                    conn.close()
                    continue
                length = int.from_bytes(header, "big")
                pid_bytes = conn.recv(length)
                peer_info = json.loads(pid_bytes.decode("utf-8"))
                incoming_peer_id = peer_info.get("peer_id", -1)
                if incoming_peer_id < 0:
                    conn.close()
                    continue

                with conn_lock:
                    if incoming_peer_id not in active_connections:
                        active_connections[incoming_peer_id] = conn

                # 重みデータを受信
                while state.running and incoming_peer_id in active_connections:
                    wh = conn.recv(4)
                    if not wh:
                        break
                    wlen = int.from_bytes(wh, "big")
                    if wlen == 0 or wlen > 50 * 1024 * 1024:
                        break
                    weight_data = conn.recv(wlen)
                    if not weight_data:
                        break

                    with receive_lock:
                        if incoming_peer_id not in receive_buffers:
                            receive_buffers[incoming_peer_id] = []
                        receive_buffers[incoming_peer_id].append(weight_data)

                # 受信完了
                with conn_lock:
                    active_connections.pop(incoming_peer_id, None)
                conn.close()

            except OSError:
                continue

    acceptor = threading.Thread(target=accept_incoming, daemon=True)
    acceptor.start()

    last_merge_step: int = -1

    while state.running:
        # ホワイトリストを取得
        with state.whitelist_lock:
            whitelist = dict(state.peer_whitelist)

        if not whitelist:
            time.sleep(0.1)
            continue

        # 未接続のpeerへ接続
        with conn_lock:
            for pid in list(whitelist.keys()):
                if pid not in active_connections:
                    try:
                        conn = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                        conn.settimeout(5.0)
                        # peer_idからIPを取得（localhostまたはhosts.txt）
                        peer_ip = host_map.get(pid, "localhost")
                        conn.connect((peer_ip, P2P_PORT))
                        # peer_idを通知
                        pid_msg = json.dumps({"peer_id": PEER_ID}).encode("utf-8")
                        conn.send(len(pid_msg).to_bytes(4, "big"))
                        conn.send(pid_msg)
                        with conn_lock:
                            active_connections[pid] = conn
                        print(f"[Peer {PEER_ID}] P2P connected to peer {pid} ({peer_ip})")
                    except OSError:
                        pass

        # 接続済みpeerへ重みを送信
        current_step = state.current_step
        if state.shadow_weights is not None and current_step != last_merge_step:
            serialized = _serialize_weights(state.shadow_weights)
            with conn_lock:
                peers_to_send = list(active_connections.keys())
            for pid in peers_to_send:
                try:
                    conn = active_connections[pid]
                    # 長さヘッダ + 重みデータ
                    conn.send(len(serialized).to_bytes(4, "big"))
                    conn.send(serialized)
                except OSError:
                    with conn_lock:
                        active_connections.pop(pid, None)

        # 受信バッファのマージ（訓練イテレーションの境界で実行）
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

                    with state.weights_lock:
                        if state.shadow_weights is not None:
                            for k in merged:
                                if k in state.shadow_weights:
                                    state.shadow_weights[k].copy_(merged[k].to(
                                        state.shadow_weights[k].dtype
                                    ))

                    # modelの重みを更新
                    model.load_state_dict(state.shadow_weights, strict=False)
                    last_merge_step = current_step
                    print(
                        f"[Peer {PEER_ID}] Merged weights from {count} peers "
                        f"at step {current_step}"
                    )

        time.sleep(0.01)

    # クリーンアップ
    p2p_socket.close()
    with conn_lock:
        for conn in active_connections.values():
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
    max_eval_samples: int = 50,
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
# Thread 3: LoRA訓練ループ（計算プレーン）
# ============================================================


def training_loop_thread(
    state: SharedState,
    model: Any,
    tokenizer: Any,
    train_data: list[dict[str, torch.Tensor]],
    train_samples: list[dict[str, str]],
    test_samples: list[dict[str, str]],
) -> None:
    """LoRA訓練ループ。

    1バッチごとに順伝播・逆伝播・.optimizer.step()を実行。
    通信スレッドによるマージはイテレーションの境界で受け入れる。
    各ステップでメトリクスをキュー経由でロガスレッドへ送信。
    """
    print(f"[Peer {PEER_ID}] Thread 3 (Training Loop) started", flush=True)

    # シャドウコピーを初期化（LoRAパラメータのみ＝メモリ節約）
    lora_keys = [k for k in model.state_dict().keys() if "lora" in k.lower()]
    print(f"[Peer {PEER_ID}]   Cloning {len(lora_keys)} LoRA weight tensors to CPU...", flush=True)
    with state.weights_lock:
        state.shadow_weights = {
            k: model.state_dict()[k].detach().cpu().clone() for k in lora_keys
        }
    print(f"[Peer {PEER_ID}] Model loaded, shadow weights ready (LoRA keys: {len(lora_keys)})")
    state.ready_to_send.set()

    optimizer = AdamW(
        model.parameters(),
        lr=_get_float("training", "learning_rate"),
        weight_decay=0.01,
    )

    # 全ステップをログ出力（チェックポイントは最終ステップのみ）
    log_freq = 1
    eval_freq = max(1, _get_int("training", "num_training_steps") // 10)
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

        # データローディング
        idx = step % len(train_data)
        batch = train_data[idx]
        input_ids = batch["input_ids"].unsqueeze(0)
        labels = batch["labels"].unsqueeze(0)

        # フォワードパス
        optimizer.zero_grad()
        outputs = model(input_ids=input_ids, labels=labels)
        loss = outputs.loss
        total_tokens += int(input_ids.numel())

        # バックワードパス
        loss.backward()

        # optimizerステップ
        with state.step_lock:
            # マージ待ち（あれば適用）
            if not state.merge_queue.empty():
                try:
                    peer_weights = state.merge_queue.get_nowait()
                    # これはThread 2からの直接マージではなく、
                    # Thread 2が既にshadow_weightsへ適用済み
                    del peer_weights
                except queue.Empty:
                    pass

            state.current_step = step + 1
            current_step_for_merge = step + 1

        optimizer.step()

        # シャドウコピー更新
        with state.weights_lock:
            if state.shadow_weights is not None:
                for k, v in model.state_dict().items():
                    if k in state.shadow_weights:
                        state.shadow_weights[k].copy_(v.detach().cpu())

        # メトリクス計算
        loss_value = loss.item()
        step_duration = time.time() - step_start_time
        tokens_per_sec = (
            total_tokens / (time.time() - start_wait) if time.time() > start_wait else 0
        )
        state.elapsed_time = time.time() - state.experiment_start_time

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

        # 定期的に train/test スコアを評価
        train_score = 0.0
        test_score = 0.0
        if current_step_for_merge % eval_freq == 0 and current_step_for_merge > 0:
            train_score = evaluate_batch(model, tokenizer, train_samples, _get_int("training", "max_seq_len"))
            test_score = evaluate_batch(model, tokenizer, test_samples, _get_int("training", "max_seq_len"))

        metric = {
            "type": "metric",
            "peer_id": PEER_ID,
            "step": current_step_for_merge,
            "elapsed": state.elapsed_time,
            "loss": loss_value,
            "tokens_per_sec": tokens_per_sec,
            "total_tokens": total_tokens,
            "step_duration": step_duration,
            "train_score": train_score,
            "test_score": test_score,
        }
        try:
            state.metrics_queue.put(metric, timeout=1.0)
        except queue.Full:
            pass

        # チェックポイント保存（最終ステップのみ）
        if current_step_for_merge % log_freq == 0 and current_step_for_merge > 0:
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
                f"train={train_score:.1f}%, test={test_score:.1f}%, "
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
            # 実験終了 + キュー空 → シャットダウン
            if not state.running and state.metrics_queue.empty():
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
        target=async_logging_thread, args=(state,), daemon=True
    )
    logger_thread.start()

    # Thread 1（サーバーリスナー）を起動
    listener_thread = threading.Thread(
        target=server_listener_thread, args=(state, model), daemon=True
    )
    listener_thread.start()

    # Thread 2（P2P交換）を起動
    p2p_thread = threading.Thread(
        target=p2p_exchange_thread, args=(state, model), daemon=True
    )
    p2p_thread.start()

    # Thread 3（訓練ループ）を起動
    training_thread = threading.Thread(
        target=training_loop_thread,
        args=(state, model, tokenizer, train_data, train_samples, test_samples),
        daemon=True,
    )
    training_thread.start()

    # 全スレッドの終了を待機
    print(f"[Peer {PEER_ID}] All threads started. Waiting for experiment...", flush=True)
    sys.stdout.flush()
    for t in [listener_thread, p2p_thread, training_thread]:
        t.join()

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
