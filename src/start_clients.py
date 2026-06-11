#!/usr/bin/env python3
"""管理サーバー上から全学習デバイスへ並列SSHでクライアントコンテナを起動。

hosts.txtを読み込み、各デバイスへ並列SSHを送り、Dockerコンテナを
ホストネットワークモードでバックグラウンド起動する。
"""

import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from utils import get_base_dir, get_hosts_path, load_config

BASE_DIR = get_base_dir()
CONFIG = load_config()

SSH_USER = CONFIG["ssh_user"]
DEPLOY_DIR = os.path.expanduser(CONFIG["deploy_dir"])
SERVER_HOST = CONFIG.get("server_host", "wafl-ctrl1")
REGISTRY_PORT = 5000
IMAGE_NAME = f"localhost:{REGISTRY_PORT}/wafl-peft:latest"


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
    """1デバイスへプロジェクトソースをrsyncで転送。"""
    # リモートディレクトリ作成
    subprocess.run(
        f"ssh -o StrictHostKeyChecking=no {SSH_USER}@{ip} "
        f"'mkdir -p {DEPLOY_DIR}/data/train {DEPLOY_DIR}/data/test'",
        shell=True, capture_output=True, text=True, timeout=30,
    )

    # peer固有の訓練データ転送
    train_file = BASE_DIR / "data" / "train" / f"peer_{peer_id}.json"
    if train_file.exists():
        subprocess.run(
            f"rsync -az --quiet {train_file} "
            f"{SSH_USER}@{ip}:{DEPLOY_DIR}/data/train/peer_{peer_id}.json",
            shell=True, capture_output=True, text=True, timeout=60,
        )

    # peer固有のテストデータ転送
    test_file = BASE_DIR / "data" / "test" / f"peer_{peer_id}.json"
    if test_file.exists():
        subprocess.run(
            f"rsync -az --quiet {test_file} "
            f"{SSH_USER}@{ip}:{DEPLOY_DIR}/data/test/peer_{peer_id}.json",
            shell=True, capture_output=True, text=True, timeout=60,
        )

    # ソースコード転送（data/ は除外）
    rsync_cmd = (
        f"rsync -az --quiet --delete "
        f"--exclude='__pycache__' --exclude='*.pyc' --exclude='.git' "
        f"--exclude='.venv' --exclude='data/' --exclude='logs/' --exclude='output/' "
        f"{BASE_DIR}/ {SSH_USER}@{ip}:{DEPLOY_DIR}/"
    )
    result = subprocess.run(rsync_cmd, shell=True, capture_output=True, text=True, timeout=120)
    if result.returncode == 0:
        return f"OK (peer={peer_id}, ip={ip})"
    return f"FAILED rsync (peer={peer_id}, ip={ip}): {result.stderr[:200]}"


def start_client_container(ip: str, peer_id: int) -> str:
    """1デバイス上でクライアントコンテナを起動（ポート開放なし）。"""
    # ソースコード転送
    sync_result = sync_source(ip, peer_id)
    if not sync_result.startswith("OK"):
        return sync_result

    cmd = (
        f"ssh {SSH_USER}@{ip} "
        f"'docker rm -f wafl-peft-client-{peer_id} 2>/dev/null || true; "
        f"docker run -d --name wafl-peft-client-{peer_id} "
        f"-e PEER_ID={peer_id} "
        f"-v {DEPLOY_DIR}/src:/app/src "
        f"-v {DEPLOY_DIR}/config:/app/config "
        f"-v {DEPLOY_DIR}/data:/app/data "
        f"-v {DEPLOY_DIR}/logs:/app/logs "
        f"{IMAGE_NAME} "
        f"uv run python src/client.py'"
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

    max_workers = 10  # 同時実行数

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
    print("[start_clients] Monitor with: docker exec -it wafl-peft-server tail -f /app/logs/metrics_peer_0_final.jsonl")


if __name__ == "__main__":
    main()
