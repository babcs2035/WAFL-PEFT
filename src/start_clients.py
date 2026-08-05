#!/usr/bin/env python3
"""管理サーバー上から全学習デバイスへ並列SSHでクライアントコンテナを起動。

hosts.txtを読み込み、各デバイスへ並列SSHを送り、Dockerコンテナを
ホストネットワークモードでバックグラウンド起動する。
"""

import os
import socket
import subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed

from utils import get_base_dir, _get, _get_int, _get_str

BASE_DIR = get_base_dir()
DEPLOY_DIR = os.path.expanduser(_get_str("deployment", "deploy_dir"))
SERVER_HOST = _get_str("server", "server_host")

SSH_USER = _get_str("deployment", "ssh_user")
_server_ip = _get("server", "server_ip")
if _server_ip:
    SERVER_HOST_IP = _server_ip
else:
    SERVER_HOST_IP = socket.gethostbyname(SERVER_HOST)
REGISTRY_PORT = 5000
IMAGE_NAME = f"127.0.0.1:{REGISTRY_PORT}/wafl-peft:latest"

# ベースライン実験用のクライアント挙動フラグ。起動時の環境変数をコンテナへそのまま渡す。
# WAFL_P2P_ENABLED=0 で P2P 重み交換を無効化（self-training / 孤立訓練ベースライン）。
# WAFL_SELF_EVAL=0 で学習ノードの自己評価を無効化（評価専用ホストへ評価を委譲する場合）。
# WAFL_P2P_SYNC=1 で同期バリア方式（Iter12, client.py 側の切替）を有効化。
# WAFL_P2P_SYNC_TIMEOUT_SEC はバリアのタイムアウト秒（未指定なら client.py が
# config/settings.json の communication.p2p_sync_timeout_sec へフォールバックする）。
_P2P_ENABLED = os.environ.get("WAFL_P2P_ENABLED", "1")
_SELF_EVAL = os.environ.get("WAFL_SELF_EVAL", "0")
_P2P_SYNC = os.environ.get("WAFL_P2P_SYNC", "0")
_P2P_SYNC_TIMEOUT_SEC = os.environ.get("WAFL_P2P_SYNC_TIMEOUT_SEC", "")
# W3: merge_include_self の動的値化（環境変数 WAFL_MERGE_INCLUDE_SELF で制御）
_MERGE_INCLUDE_SELF = os.environ.get("WAFL_MERGE_INCLUDE_SELF", "1")

# 管理サーバー上かローカルかでSSH接続方法を変える
_CURRENT_HOSTNAME = socket.gethostname()
if _CURRENT_HOSTNAME == SERVER_HOST or SERVER_HOST_IP in _CURRENT_HOSTNAME:
    _JUMP = ""
else:
    _JUMP = f"-J {SSH_USER}@{SERVER_HOST}"


def load_hosts() -> list[str]:
    """hosts.txtからIPリストを読み込み。"""
    hosts_path = BASE_DIR / "config" / "hosts.txt"
    ips = []
    for line in hosts_path.read_text().strip().splitlines():
        ip = line.strip()
        if ip and not ip.startswith("#"):
            ips.append(ip)
    return ips


def sync_source(ip: str, peer_id: int) -> str:
    """1デバイスへプロジェクトソースをrsyncで転送。

    データは deploy:distribute で配布済みなので、ここではソースコードのみ転送。
    --info=progress2 で進捗バーを表示する。
    """
    # ソースコード転送（data/ は除外＝deploy:distribute で配布済み）
    jump_flag = f"-J {SSH_USER}@{SERVER_HOST} " if _JUMP else ""
    rsync_cmd = (
        f"rsync -az --info=progress2 --delete "
        f"-e 'ssh -o StrictHostKeyChecking=no {jump_flag}' "
        f"--exclude='__pycache__' --exclude='*.pyc' --exclude='.git' "
        f"--exclude='.venv' --exclude='data/' --exclude='logs/' --exclude='output/' "
        f"--exclude='cache/' --exclude='results/' "
        f"{BASE_DIR}/ {SSH_USER}@{ip}:{DEPLOY_DIR}/"
    )
    # capture_output=False（進捗バーを直接ターミナルへ流すため）なので
    # result.stderr は常にNone。エラー内容自体は標準エラー出力にそのまま
    # 表示されているため、ここではreturncodeのみ報告する
    result = subprocess.run(rsync_cmd, shell=True, capture_output=False, text=True, timeout=120)
    if result.returncode == 0:
        return f"OK (peer={peer_id}, ip={ip})"
    return f"FAILED rsync (peer={peer_id}, ip={ip}): returncode={result.returncode}"


def start_client_container(ip: str, peer_id: int) -> str:
    """1デバイス上でクライアントコンテナを起動。

    --net=host が必須: これがないとコンテナはデフォルトのbridgeネットワークに
    入り、P2P用ポート（client_p2p_port）がホストに全く公開されず、他のpeerから
    一切接続できない（ufwでホスト側ポートを許可していても無意味になる）。
    サーバーコンテナ（start:server）と同じくホストネットワークを直接使う。
    """
    # ソースコード転送
    sync_result = sync_source(ip, peer_id)
    if not sync_result.startswith("OK"):
        return sync_result

    # WAFL_P2P_SYNC_TIMEOUT_SEC は未指定（空文字）ならコンテナへ渡さず、
    # client.py 側で config/settings.json の既定値へフォールバックさせる。
    sync_timeout_flag = (
        f"-e WAFL_P2P_SYNC_TIMEOUT_SEC={_P2P_SYNC_TIMEOUT_SEC} " if _P2P_SYNC_TIMEOUT_SEC else ""
    )
    jump_flag = f"-J {SSH_USER}@{SERVER_HOST} " if _JUMP else ""
    cmd = (
        f"ssh -o StrictHostKeyChecking=no {jump_flag}{SSH_USER}@{ip} "
        f"'export LC_ALL=C; mkdir -p {DEPLOY_DIR}/logs && chown {SSH_USER}:{SSH_USER} {DEPLOY_DIR}/logs; "
        f"docker rm -f wafl-peft-client-{peer_id} 2>/dev/null || true; "
        f"docker run -d --name wafl-peft-client-{peer_id} "
        f"--gpus all "
        f"--net=host "
        f"--add-host {SERVER_HOST}:{SERVER_HOST_IP} "
        f"-e PEER_ID={peer_id} "
        f"-e WAFL_P2P_ENABLED={_P2P_ENABLED} "
        f"-e WAFL_SELF_EVAL={_SELF_EVAL} "
        f"-e WAFL_P2P_SYNC={_P2P_SYNC} "
        f"-e WAFL_MERGE_INCLUDE_SELF={_MERGE_INCLUDE_SELF} "
        f"{sync_timeout_flag}"
        # 断片化由来の OOM を抑える。巨大 vocab(262144) の logits transient と外部 GPU 競合
        # (~2GB, 可変)で reserved が膨らみ、total 12GB 近傍で 256MB 級の割当が断片化により
        # 失敗する事例が発生したため、expandable_segments で予約領域を伸縮可能にする
        # （seq_len を削らずに済むので truncation による学習劣化を招かない）
        f"-e PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True "
        f"-v {DEPLOY_DIR}/src:/app/src "
        f"-v {DEPLOY_DIR}/config:/app/config "
        f"-v {DEPLOY_DIR}/data:/app/data "
        f"-v {DEPLOY_DIR}/cache:/app/cache "
        f"-v {DEPLOY_DIR}/logs:/app/logs "
        f"-v {DEPLOY_DIR}/results:/app/results "
        f"{IMAGE_NAME} "
        f"/app/.venv/bin/python src/client.py'"
    )
    result = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    if result.returncode == 0:
        container_id = result.stdout.strip()[:12]
        return f"OK (peer={peer_id}, ip={ip}, container={container_id})"
    return f"FAILED (peer={peer_id}, ip={ip}): {result.stderr[:300]}"


def main() -> None:
    """メインエントリポイント。"""
    print("[start_clients] Starting client containers...")

    hosts = load_hosts()
    print(f"[start_clients] Target devices: {len(hosts)}")

    max_workers = len(hosts)  # hosts.txtのノード数だけ並列実行

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {
            executor.submit(start_client_container, ip, i): i
            for i, ip in enumerate(hosts)
        }
        for future in as_completed(futures):
            peer_id = futures[future]
            try:
                result = future.result()
                print(f"  [OK] {result}")
            except Exception as e:
                print(f"  [FAIL] peer={peer_id}: {e}")

    print("\n[start_clients] All containers launched.")
    print("[start_clients] Monitor with: docker exec -it wafl-peft-client-0 tail -f /app/logs/metrics_peer_0_final.log")


if __name__ == "__main__":
    main()
