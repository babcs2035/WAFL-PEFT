#!/usr/bin/env python3
"""管理サーバー上から学習デバイスへの並列デプロイスクリプト。

Phase 1: rsyncで設定ファイル・データを各デバイスへ転送
Phase 2: 各デバイスが管理サーバーのレジストリからSSHトンネル経由でpull
Phase 3: サーバー・クライアントコンテナを起動
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

import socket

SSH_USER = CONFIG["ssh_user"]
DEPLOY_DIR = os.path.expanduser(CONFIG["deploy_dir"])
SERVER_HOST = CONFIG.get("server_host", "wafl-ctrl1")
REGISTRY_PORT = 5000
IMAGE_NAME = f"localhost:{REGISTRY_PORT}/wafl-peft:latest"

# 管理サーバーのLAN IP（学習デバイスからアクセス可能）
try:
    SERVER_HOST_IP = socket.gethostbyname(SERVER_HOST)
except socket.gaierror:
    SERVER_HOST_IP = "127.0.0.1"


def load_hosts() -> list[str]:
    """hosts.txtからIPリストを読み込み。"""
    hosts_path = BASE_DIR / "config" / "hosts.txt"
    ips = []
    for line in hosts_path.read_text().strip().splitlines():
        ip = line.strip()
        if ip and not ip.startswith("#"):
            ips.append(ip)
    return ips


def rsync_to_device(ip: str, peer_id: int) -> str:
    """1デバイスへ設定・peer固有データ・ソースコードを転送。"""
    remote_path = f"{SSH_USER}@{ip}:{DEPLOY_DIR}"

    # リモートディレクトリ作成
    subprocess.run(
        f"ssh -o StrictHostKeyChecking=no {SSH_USER}@{ip} "
        f"'mkdir -p {DEPLOY_DIR}/config {DEPLOY_DIR}/data/train {DEPLOY_DIR}/data/test'",
        shell=True, capture_output=True, text=True, timeout=30,
    )

    # 設定ファイル転送
    subprocess.run(
        [
            "rsync", "-az", "--quiet",
            str(BASE_DIR / "config" / "settings.json"),
            f"{remote_path}/config/settings.json",
        ],
        check=True,
    )

    # peer固有の訓練データ転送
    train_file = BASE_DIR / "data" / "train" / f"peer_{peer_id}.json"
    if train_file.exists():
        subprocess.run(
            [
                "rsync", "-az", "--quiet",
                str(train_file),
                f"{remote_path}/data/train/peer_{peer_id}.json",
            ],
            check=True,
        )

    # peer固有のテストデータ転送
    test_file = BASE_DIR / "data" / "test" / f"peer_{peer_id}.json"
    if test_file.exists():
        subprocess.run(
            [
                "rsync", "-az", "--quiet",
                str(test_file),
                f"{remote_path}/data/test/peer_{peer_id}.json",
            ],
            check=True,
        )

    # ソースコード転送（data/ は転送しない）
    subprocess.run(
        [
            "rsync", "-az", "--quiet", "--delete",
            "--exclude", "__pycache__", "--exclude", "*.pyc",
            "--exclude", ".git", "--exclude", ".venv", "--exclude", "data",
            "--exclude", "logs", "--exclude", "output",
            str(BASE_DIR) + "/",
            f"{remote_path}/",
        ],
        check=True,
    )

    return f"OK (peer={peer_id}, ip={ip})"


def pull_docker_image(ip: str, peer_id: int) -> str:
    """1デバイスが管理サーバーのレジストリからSSHトンネル経由でpull。

    各デバイス上で:
    1. 自デバイス上のport 5000を使用中ならkill
    2. 管理サーバーへSSHトンネル（-L 5000:127.0.0.1:5000）を確実に張る
    3. 127.0.0.1:5000へDocker pullを発行
    """
    import time

    control_path = os.path.expanduser(f"~/.ssh/control-registry-{peer_id}")

    # 各デバイス上でport 5000を使用中のプロセスをkill（SSHトンネル用）
    subprocess.run(
        f"ssh -o StrictHostKeyChecking=no {SSH_USER}@{ip} "
        f"'lsof -ti:5000 | xargs -r kill -9 2>/dev/null; "
        f"sleep 1; "
        f"ss -tln 2>/dev/null | grep -q :5000 && echo port5000_still_used || echo port5000_free'",
        shell=True, capture_output=True, text=True, timeout=15,
    )

    # Docker insecure-registries設定（再起動は初回のみで十分）
    subprocess.run(
        f"ssh {SSH_USER}@{ip} "
        f'''bash -s <<'PYEOF'
export LC_ALL=C
mkdir -p /etc/docker
python3 -c 'import json; d=json.load(open("/etc/docker/daemon.json","r")) if __import__("os").path.exists("/etc/docker/daemon.json") else {{}}; d["insecure-registries"]=list(set(d.get("insecure-registries",[])+["127.0.0.1:5000"])); json.dump(d,open("/etc/docker/daemon.json","w"))'
PYEOF
''',
        shell=True, capture_output=True, text=True, timeout=30,
    )

    # 各デバイス上でトンネルを張る（-f でバックグラウンド、nohup は不要）
    tunnel_script = (
        f"mkdir -p ~/.ssh; "
        f"ssh -S {control_path} -O exit {SSH_USER}@{SERVER_HOST} 2>/dev/null; "
        f"ssh -N -L 5000:127.0.0.1:{REGISTRY_PORT} "
        f"-S {control_path} "
        f"-o StrictHostKeyChecking=no "
        f"-o UserKnownHostsFile=/dev/null "
        f"-o ServerAliveInterval=60 "
        f"-o ServerAliveCountMax=3 "
        f"-f {SSH_USER}@{SERVER_HOST}; "
        f"sleep 2; "
        f"ssh -S {control_path} -O check {SSH_USER}@{SERVER_HOST} 2>/dev/null"
    )
    tunnel_result = subprocess.run(
        f"ssh {SSH_USER}@{ip} '{tunnel_script}'",
        shell=True, capture_output=True, text=True, timeout=30,
    )
    if tunnel_result.returncode != 0:
        stderr = tunnel_result.stderr.strip()
        return f"FAILED tunnel (peer={peer_id}, ip={ip}): {stderr[:200]}"

    # トンネルが立つまで待つ（最大30秒）
    for _ in range(30):
        check = subprocess.run(
            f"ssh {SSH_USER}@{ip} 'ss -tln 2>/dev/null | grep -q 5000 && echo ok'",
            shell=True, capture_output=True, text=True, timeout=5,
        )
        if check.stdout.strip() == "ok":
            break
        time.sleep(1)

    try:
        # トンネル経由でpull（127.0.0.1:5000 はトンネル先＝管理サーバーのregistry）
        pull_cmd = (
            f"ssh {SSH_USER}@{ip} "
            f"'DOCKER_MAX_CONCURRENT_DOWNLOADS=5 DOCKER_MAX_CONCURRENT_PULLS=5 docker pull {IMAGE_NAME}'"
        )
        pull_result = subprocess.run(
            pull_cmd, shell=True, capture_output=True, text=True, timeout=600,
        )
        if pull_result.returncode == 0:
            return f"Docker pulled (peer={peer_id}, ip={ip})"
        return f"FAILED (peer={peer_id}, ip={ip}): {pull_result.stderr[:500]}"
    finally:
        # トンネルを閉じる
        subprocess.run(
            f"ssh -S {control_path} -O exit {SSH_USER}@{SERVER_HOST} 2>/dev/null",
            shell=True, capture_output=True, text=True, timeout=10,
        )
        subprocess.run(
            f"rm -f {control_path}",
            shell=True, capture_output=True, text=True, timeout=5,
        )


def start_server_container() -> str:
    """管理サーバー上でサーバーコンテナを起動。"""
    cmd = (
        f"ssh -o StrictHostKeyChecking=no {SSH_USER}@localhost "
        f"'docker rm -f wafl-peft-server 2>/dev/null; "
        f"docker run -d --name wafl-peft-server "
        f"-p 127.0.0.1:8080:8080 "
        f"-v {DEPLOY_DIR}/src:/app/src "
        f"-v {DEPLOY_DIR}/config:/app/config "
        f"-v {DEPLOY_DIR}/data:/app/data "
        f"-v {DEPLOY_DIR}/logs:/app/logs "
        f"{IMAGE_NAME} "
        f"uv run python src/server.py'"
    )
    result = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    if result.returncode == 0:
        container_id = result.stdout.strip()[:12]
        return f"OK (container={container_id})"
    return f"FAILED: {result.stderr[:300]}"


def start_client_container(ip: str, peer_id: int) -> str:
    """1デバイス上でクライアントコンテナを起動（ポート開放なし）。"""
    cmd = (
        f"ssh -o StrictHostKeyChecking=no {SSH_USER}@{ip} "
        f"'docker rm -f wafl-peft-client-{peer_id} 2>/dev/null; "
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
    print("[deploy_distribute] Starting distribution...")

    hosts = load_hosts()
    print(f"[deploy_distribute] Target devices: {len(hosts)}")

    max_workers = 5

    # Phase 1: rsync for config and data
    print("\n[deploy_distribute] Phase 1: Rsync config and data...")
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {
            executor.submit(rsync_to_device, ip, i): i
            for i, ip in enumerate(hosts)
        }
        for future in as_completed(futures):
            peer_id = futures[future]
            try:
                result = future.result()
                print(f"  [OK] {result}")
            except Exception as e:
                print(f"  [FAIL] peer={peer_id}: {e}")

    # Phase 2: Pull Docker image from management server registry
    print("\n[deploy_distribute] Phase 2: Pull Docker images from registry...")
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {
            executor.submit(pull_docker_image, ip, i): i
            for i, ip in enumerate(hosts)
        }
        for future in as_completed(futures):
            peer_id = futures[future]
            try:
                result = future.result()
                print(f"  [OK] {result}")
            except Exception as e:
                print(f"  [FAIL] peer={peer_id}: {e}")

    # Phase 3: Start server container
    print("\n[deploy_distribute] Phase 3: Start server container...")
    result = start_server_container()
    print(f"  [OK] {result}")

    # Phase 4: Start client containers
    print("\n[deploy_distribute] Phase 4: Start client containers...")
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

    print("\n[deploy_distribute] Distribution complete.")
    print("[deploy_distribute] Monitor with: docker exec -it wafl-peft-server tail -f /app/logs/metrics_peer_0_final.jsonl")


if __name__ == "__main__":
    main()
