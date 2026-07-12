#!/usr/bin/env python3
"""評価専用ホストで動く「随時評価」ワーカー。

学習ノードとは別のデバイス（config/hosts.eval.txt に列挙）で起動し、担当する
学習 peer が保存する LoRA チェックポイント（logs/weights/weights_step_*.pt）を
rsync で随時取得して GSM8K accuracy を評価する。学習ノード側の VRAM を評価で
圧迫しないための「学習と評価のハード分離」を実現する。

役割分担:
  - 学習ノード（client.py）: 学習と checkpoint 保存に専念。自己評価はしない。
    実験終了時に logs/weights/.training_done マーカーを書く。
  - 評価ワーカー（本ファイル）: 担当 peer の weights を rsync でポーリングし、
    新しい checkpoint を採点。結果は管理サーバーへ {"type":"eval_result"} で送信。
    .training_done を検出したら残りを評価し {"type":"evaluation_complete"} を
    送って終了する（サーバーの全デバイス完了検出に用いられる）。

環境変数:
  EVAL_PEER_ID  評価対象の学習 peer_id（config/hosts.txt の行番号に対応）。
                本ワーカーが動くホストは config/hosts.eval.txt の同じ行に対応する。

利用方法（管理サーバー上から）:
  src/start_eval_workers.py が各評価ホストでコンテナを起動し、EVAL_PEER_ID を渡す。
"""

import json
import os
import socket
import subprocess
import time
from pathlib import Path
from typing import Any

import torch

import gsm8k_eval
from utils import get_base_dir, get_log_dir, _get, _get_float, _get_int, _get_str

# 評価対象の学習 peer_id（= 監視する checkpoint の持ち主）
EVAL_PEER_ID = int(os.environ.get("EVAL_PEER_ID", "0"))

BASE_DIR = get_base_dir()
LOG_DIR = get_log_dir()
SERVER_HOST = _get_str("server", "server_host")
SERVER_PORT = _get_int("server", "server_port")
SSH_USER = _get_str("deployment", "ssh_user")
DEPLOY_DIR = os.path.expanduser(_get_str("deployment", "deploy_dir"))

# checkpoint ポーリング間隔（秒）。checkpoint 保存間隔（eval_interval_seconds、
# 既定 60s）より短くして取りこぼしを防ぐ
_POLL_INTERVAL_SECONDS = 20.0
# 1 checkpoint あたりの評価サンプル数。実験中に多数の checkpoint を随時評価するため
# 過大にすると追従できない。実験後の最終 sweep でも同じ値を使う
_EVAL_SAMPLE_LIMIT = _get_int("global_eval", "sample_limit", 40)
# 最大生成トークン数（CoT が "#### N" に到達するのに十分な長さ）
_EVAL_MAX_NEW_TOKENS = 256
# .training_done を最後まで受け取れないまま学習ノードが落ちた場合の保険。
# これだけ経過したら未評価分を最終評価して終了する（実験窓 + 十分な余裕）
_MAX_RUNTIME_SECONDS = 12000.0

_WORKER_START_WALL = time.time()


def _now() -> str:
    """ワーカー起動からの経過秒数（固定幅）をログ prefix 用に返す。"""
    return f"+{time.time() - _WORKER_START_WALL:7.1f}s"


def _log(msg: str) -> None:
    """整列した prefix つきでログ出力する（client.py と同じ体裁）。"""
    print(f"[{_now()}]\t[Eval {EVAL_PEER_ID}]\t[EvalWorker]\t{msg}", flush=True)


def resolve_train_ip(peer_id: int) -> str:
    """config/hosts.txt から学習 peer_id に対応する学習ノードの IP を得る。"""
    hosts_path = BASE_DIR / "config" / "hosts.txt"
    ips: list[str] = []
    for line in hosts_path.read_text().strip().splitlines():
        ip = line.strip()
        if ip and not ip.startswith("#"):
            ips.append(ip)
    if peer_id >= len(ips):
        raise ValueError(f"EVAL_PEER_ID={peer_id} が hosts.txt の範囲外（{len(ips)} 台）")
    return ips[peer_id]


def _send_to_server(payload: dict[str, Any]) -> bool:
    """管理サーバーへ短命 TCP 接続で JSON を 1 件送る（4byte 長ヘッダ + 本体）。"""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(10.0)
            s.connect((SERVER_HOST, SERVER_PORT))
            body = json.dumps(payload).encode("utf-8")
            s.sendall(len(body).to_bytes(4, "big"))
            s.sendall(body)
        return True
    except OSError as e:
        _log(f"サーバー送信失敗: {e}")
        return False


def rsync_weights(train_ip: str, local_dir: Path) -> bool:
    """学習ノードの logs/weights/ を評価ホストのローカルへ rsync で取得する。

    サーバーの _collect_latest_weights と同じ方式。転送中の不完全ファイルは
    torch.load 側の例外で弾かれるため、ここでは成否のみ確認する。
    """
    remote = f"{SSH_USER}@{train_ip}:{DEPLOY_DIR}/logs/weights/"
    local_dir.mkdir(parents=True, exist_ok=True)
    cmd = (
        "rsync -az -e 'ssh -o StrictHostKeyChecking=no' "
        f"{remote} {local_dir}/"
    )
    result = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=120)
    return result.returncode == 0


def list_checkpoint_steps(weights_dir: Path) -> list[int]:
    """ローカルへ取得済みの weights_step_*.pt から step 番号を昇順で返す。"""
    steps: list[int] = []
    for f in weights_dir.glob("weights_step_*.pt"):
        try:
            steps.append(int(f.stem.replace("weights_step_", "")))
        except ValueError:
            continue
    return sorted(steps)


def main() -> None:
    """メインエントリポイント。担当 peer の checkpoint を随時評価する。"""
    train_ip = resolve_train_ip(EVAL_PEER_ID)
    _log(f"担当学習 peer={EVAL_PEER_ID} (train_ip={train_ip}) の checkpoint を随時評価する")
    _log(f"評価サンプル数={_EVAL_SAMPLE_LIMIT}, poll={_POLL_INTERVAL_SECONDS}s")

    # ベースモデル + LoRA を 1 度だけ構築（以降は重みだけ差し替えて再利用）
    device_id = 0 if torch.cuda.is_available() else None
    model, tokenizer = gsm8k_eval.build_lora_model(
        model_id=_get_str("model", "model_id"),
        lora_rank=_get_int("training", "lora_rank"),
        lora_alpha=_get_int("training", "lora_alpha"),
        device_id=device_id,
        base_dir=BASE_DIR,
    )
    val_data = gsm8k_eval.load_gsm8k_val_data(BASE_DIR, sample_limit=_EVAL_SAMPLE_LIMIT)
    _log(f"ベースモデル構築完了 / 検証サンプル {len(val_data)} 件をロード")

    local_weights = LOG_DIR / f"eval_pull_peer_{EVAL_PEER_ID}" / "weights"
    evaluated: set[int] = set()

    def evaluate_step(step: int) -> None:
        """1 つの checkpoint を評価し、結果をサーバーへ送る。"""
        ckpt = local_weights / f"weights_step_{step:06d}.pt"
        try:
            weights = torch.load(ckpt, weights_only=True, map_location="cpu")
        except (EOFError, RuntimeError) as e:
            # rsync 途中の不完全ファイル等。次ループで再取得されるため未評価のままにする
            _log(f"step {step}: 読み込み失敗（次回再試行）: {e}")
            return
        accuracy = gsm8k_eval.evaluate_weights(
            model, tokenizer, weights, val_data, max_new_tokens=_EVAL_MAX_NEW_TOKENS
        )
        evaluated.add(step)
        _send_to_server(
            {"type": "eval_result", "peer_id": EVAL_PEER_ID, "step": step, "accuracy": accuracy}
        )
        _log(f"step {step}: accuracy={accuracy:.1f}% を送信 ({len(evaluated)} 件評価済み)")

    # ポーリングループ: .training_done を検出しきるまで随時評価する
    while True:
        elapsed = time.time() - _WORKER_START_WALL
        rsync_ok = rsync_weights(train_ip, local_weights)
        if not rsync_ok:
            _log("rsync 失敗（学習ノード未起動 or ネットワーク）。待機して再試行する")

        steps = list_checkpoint_steps(local_weights)
        new_steps = [s for s in steps if s not in evaluated]
        for step in new_steps:
            evaluate_step(step)

        training_done = (local_weights / ".training_done").exists()
        if training_done and not [s for s in list_checkpoint_steps(local_weights) if s not in evaluated]:
            _log("学習完了マーカーを検出し、全 checkpoint の評価を完了した")
            break
        if elapsed > _MAX_RUNTIME_SECONDS:
            _log(f"最大稼働時間 {_MAX_RUNTIME_SECONDS:.0f}s を超過したため終了する")
            break

        time.sleep(_POLL_INTERVAL_SECONDS)

    # サーバーへ評価完了を通知（全デバイス完了検出に用いられる）
    _send_to_server({"type": "evaluation_complete", "peer_id": EVAL_PEER_ID})
    _log("評価完了をサーバーへ通知して終了する")


if __name__ == "__main__":
    main()
