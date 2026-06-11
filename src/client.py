#!/usr/bin/env python3
"""WAFL-PEFT学習デバイスクライアント。

4スレッド並列アーキテクチャ:
  Thread 1: 管理サーバー交信リスナー（制御プレーン）
  Thread 2: P2P生TCP交換・マージ層（データプレーン）
  Thread 3: LoRA訓練ループ（計算プレーン）
  Thread 4: 非同期ディスク書き出し層（ロギングプレーン）
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
from transformers import LlamaForCausalLM, LlamaTokenizer

from utils import get_base_dir, get_log_dir, get_hosts_path, load_config

# ============================================================
# グローバル設定
# ============================================================

CONFIG = load_config()
BASE_DIR = get_base_dir()
SERVER_HOST = "0.0.0.0"  # コンテナ内ではlocalhostと同じ
SERVER_PORT = CONFIG["server_port"]
P2P_PORT = CONFIG["client_p2p_port"]
PEER_ID = int(os.environ.get("PEER_ID", "0"))

LOG_DIR = get_log_dir()
LOG_DIR.mkdir(parents=True, exist_ok=True)

WEIGHT_DIR = LOG_DIR / "weights"
WEIGHT_DIR.mkdir(parents=True, exist_ok=True)


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
    - metrics_queue: Thread 3が書き込み、Thread 4が消費
    """

    def __init__(self) -> None:
        self.peer_whitelist: dict[int, float] = {}
        self.whitelist_lock = threading.Lock()

        self.shadow_weights: dict[str, torch.Tensor] | None = None
        self.weights_lock = threading.Lock()

        self.merge_queue: queue.Queue[dict[int, dict[str, torch.Tensor]]] = queue.Queue(
            maxsize=32
        )

        self.metrics_queue: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=256)

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


def initialize_model(config: dict[str, Any]) -> tuple[LlamaForCausalLM, LlamaTokenizer]:
    """LoRA付きLlamaモデルを初期化。"""
    model = LlamaForCausalLM.from_pretrained(
        config["model_id"],
        torch_dtype=torch.float16,
        device_map="auto",
        trust_remote_code=True,
    )

    lora_config = LoraConfig(
        r=config["lora_rank"],
        lora_alpha=config["lora_alpha"],
        target_modules=["q_proj", "v_proj"],
        lora_dropout=0.05,
        bias="none",
        task_type="CAUSAL_LM",
    )

    model = get_peft_model(model, lora_config)
    model.print_trainable_parameters()

    return model, LlamaTokenizer.from_pretrained(config["model_id"])


def load_sharded_dataset(config: dict[str, Any], peer_id: int) -> Dataset:
    """peer固有の訓練ファイルを読み込み。"""
    train_file = BASE_DIR / "data" / "train" / f"peer_{peer_id}.json"
    with open(train_file) as f:
        data = json.load(f)
    samples = data.get("samples", [])
    print(f"[Peer {peer_id}] Loaded {len(samples)} samples from {train_file}")
    return Dataset.from_list(samples)


def tokenize_dataset(
    dataset: Dataset,
    tokenizer: LlamaTokenizer,
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
        sock.send(len(payload).to_bytes(4, "big"))
        sock.send(payload)
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


def server_listener_thread(state: SharedState, model: LlamaForCausalLM) -> None:
    """管理サーバーと永続TCP接続を維持し、シグナルを受信。

    接続時にpeer_idを登録し、以降は1秒周期のシグナルを待受。
    切断された場合は自動再接続する。
    """
    print(f"[Peer {PEER_ID}] Thread 1 (Server Listener) started")

    while state.running:
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.settimeout(2.0)
                s.connect(("localhost", SERVER_PORT))

                # ペアID登録
                if not _send_json(s, {"type": "register", "peer_id": PEER_ID}):
                    time.sleep(1.0)
                    continue

                # モデルロード完了を待ってReadyを送信
                state.ready_to_send.wait(timeout=60.0)
                _send_json(s, {"type": "ready", "peer_id": PEER_ID})

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
                            f"Duration: {data.get('duration')}s"
                        )

                    elif msg_type == "experiment_stop":
                        print(f"[Peer {PEER_ID}] Experiment STOPPED.")
                        state.running = False
                        break

        except (OSError, ConnectionError):
            if state.running:
                print(f"[Peer {PEER_ID}] Server connection lost. Reconnecting...")
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


def p2p_exchange_thread(state: SharedState, model: LlamaForCausalLM) -> None:
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
# Thread 3: LoRA訓練ループ（計算プレーン）
# ============================================================


def training_loop_thread(
    state: SharedState,
    model: LlamaForCausalLM,
    tokenizer: LlamaTokenizer,
    train_data: list[dict[str, torch.Tensor]],
) -> None:
    """LoRA訓練ループ。

    1バッチごとに順伝播・逆伝播・.optimizer.step()を実行。
    通信スレッドによるマージはイテレーションの境界で受け入れる。
    """
    print(f"[Peer {PEER_ID}] Thread 3 (Training Loop) started")

    optimizer = AdamW(
        model.parameters(),
        lr=CONFIG["learning_rate"],
        weight_decay=0.01,
    )

    log_freq = CONFIG.get("weight_dump_frequency_steps", 10)
    step = 0
    total_tokens = 0
    start_wait = time.time()

    while state.running:
        if not state.experiment_running.is_set():
            # Ready待ち（モデルロード完了まで）
            if state.shadow_weights is None and step == 0:
                # シャドウコピーを初期化
                with state.weights_lock:
                    state.shadow_weights = {
                        k: v.detach().clone() for k, v in model.state_dict().items()
                    }
                print(f"[Peer {PEER_ID}] Model loaded, shadow weights ready")
                state.ready_to_send.set()
            time.sleep(0.1)
            continue

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

        # メトリクス送信
        loss_value = loss.item()
        tokens_per_sec = (
            total_tokens / (time.time() - start_wait) if time.time() > start_wait else 0
        )

        metric = {
            "type": "metric",
            "peer_id": PEER_ID,
            "step": current_step_for_merge,
            "elapsed": state.elapsed_time,
            "loss": loss_value,
            "tokens_per_sec": tokens_per_sec,
            "total_tokens": total_tokens,
        }
        try:
            state.metrics_queue.put_nowait(metric)
        except queue.Full:
            pass

        # チェックポイント保存
        if current_step_for_merge % log_freq == 0 and current_step_for_merge > 0:
            ckpt_metric = {**metric, "type": "checkpoint"}
            try:
                state.metrics_queue.put_nowait(ckpt_metric)
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
                f"saved {ckpt_path}"
            )

        step += 1

        # 訓練ステップ数制限
        max_steps = CONFIG.get("num_training_steps", 10000)
        if step >= max_steps:
            print(f"[Peer {PEER_ID}] Reached max steps ({max_steps}). Stopping.")
            state.running = False
            break

    print(f"[Peer {PEER_ID}] Thread 3 (Training Loop) stopped")


# ============================================================
# Thread 4: 非同期ディスク書き出し層（ロギングプレーン）
# ============================================================


def async_logging_thread(state: SharedState) -> None:
    """キュー経由でメトリクスとチェックポイントを非同期にディスクへ書き出し。"""
    print(f"[Peer {PEER_ID}] Thread 4 (Async Logger) started")

    metric_log_path = LOG_DIR / f"metrics_peer_{PEER_ID}.jsonl"
    metric_log_path.write_text("")

    while state.running or not state.metrics_queue.empty():
        try:
            metric = state.metrics_queue.get(timeout=0.5)
        except queue.Empty:
            continue

        # メトリクスをJSONLへ追記
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
    final_log_path = LOG_DIR / f"metrics_peer_{PEER_ID}_final.jsonl"
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
    global PEER_ID
    PEER_ID = int(os.environ.get("PEER_ID", "0"))

    print(f"=" * 60)
    print(f"[Peer {PEER_ID}] WAFL-PEFT Client Starting")
    print(f"=" * 60)

    # モデル・データセットの初期化
    print(f"[Peer {PEER_ID}] Loading model...")
    model, tokenizer = initialize_model(CONFIG)

    print(f"[Peer {PEER_ID}] Loading dataset...")
    raw_dataset = load_sharded_dataset(CONFIG, PEER_ID)
    train_data = tokenize_dataset(raw_dataset, tokenizer, CONFIG["max_seq_len"])
    print(f"[Peer {PEER_ID}] Tokenized {len(train_data)} training samples")

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
        args=(state, model, tokenizer, train_data),
        daemon=True,
    )
    training_thread.start()

    # 全スレッドの終了を待機
    print(f"[Peer {PEER_ID}] All 4 threads started. Waiting...")
    for t in [listener_thread, p2p_thread, training_thread, logger_thread]:
        t.join()

    print(f"[Peer {PEER_ID}] All threads stopped. Client exiting.")


if __name__ == "__main__":
    main()
