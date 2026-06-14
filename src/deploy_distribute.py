#!/usr/bin/env python3
"""管理サーバー上から学習デバイスへの並列デプロイスクリプト。

Phase 1: rsyncで設定ファイル・データを各デバイスへ転送
Phase 2: 各デバイスが管理サーバーのレジストリからSSHトンネル経由でpull
Phase 3: サーバー・クライアントコンテナを起動
"""

import json
import os
import socket
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

from utils import get_base_dir, get_experiment_dir, get_hosts_path, _get, _get_int, _get_str

BASE_DIR = get_base_dir()
DEPLOY_DIR = os.path.expanduser(_get_str("deployment", "deploy_dir"))
SERVER_HOST = _get_str("server", "server_host")


# 実験ディレクトリ名の取得（setup_data.py の .experiment_meta.json を優先参照）
def _load_experiment_dir_name() -> str:
    """実験ディレクトリ名を .experiment_meta.json から読み込み、なければ生成する。"""
    meta_path = BASE_DIR / "results" / ".experiment_meta.json"
    if meta_path.exists():
        with open(meta_path) as f:
            meta = json.load(f)
        name = meta.get("dir_name")
        if name:
            return name
    # fallback: 自前で生成
    exp_name = _get("experiment", "experiment_name", "default")
    timestamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    return f"{exp_name}_{timestamp}"


EXPERIMENT_DIR_NAME = _load_experiment_dir_name()
EXPERIMENT_DIR_REMOTE = f"{DEPLOY_DIR}/results/{EXPERIMENT_DIR_NAME}"

SSH_USER = _get_str("deployment", "ssh_user")
REGISTRY_PORT = 5000
IMAGE_NAME = f"127.0.0.1:{REGISTRY_PORT}/wafl-peft:latest"
SERVER_PORT = _get_int("server", "server_port")
CLIENT_P2P_PORT = _get_int("communication", "client_p2p_port")
UFW_ALLOW_FROM = [
    s.strip() for s in _get("server", "ufw_allow_from", "").split(",") if s.strip()
]

# 管理サーバーのLAN IP（学習デバイスからアクセス可能）
SERVER_HOST_IP = _get("server", "server_ip", socket.gethostbyname(SERVER_HOST))


def load_hosts() -> list[str]:
    """hosts.txtからIPリストを読み込み。"""
    hosts_path = BASE_DIR / "config" / "hosts.txt"
    ips = []
    for line in hosts_path.read_text().strip().splitlines():
        ip = line.strip()
        if ip and not ip.startswith("#"):
            ips.append(ip)
    return ips


def open_ufw_on_server() -> str:
    """管理サーバーにufwルールを設定しserver_portを開放。

    全インターフェース（0.0.0.0）には開放しない。
    指定サブネットからのみ許可する。
    """
    if not UFW_ALLOW_FROM:
        return "SKIPPED (ufw_allow_from not configured)"

    # 0.0.0.0 への開放は行わず、指定サブネットからのみ許可
    cmds = []
    for subnet in UFW_ALLOW_FROM:
        cmds.append(f"sudo ufw allow from {subnet} to any port {SERVER_PORT} proto tcp")

    cmd = " && ".join(cmds)
    result = subprocess.run(
        f"ssh -o StrictHostKeyChecking=no {SSH_USER}@localhost '{cmd}'",
        shell=True, capture_output=True, text=True, timeout=30,
    )
    if result.returncode == 0:
        return f"OK (server_port={SERVER_PORT}, allow_from={UFW_ALLOW_FROM})"
    return f"FAILED: {result.stderr[:300]}"


def open_ufw_on_device(ip: str, peer_id: int) -> str:
    """1デバイスにufwルールを設定しclient_p2p_portを開放。

    全インターフェース（0.0.0.0）には開放しない。
    指定サブネットからのみ許可する。
    """
    if not UFW_ALLOW_FROM:
        return "SKIPPED (ufw_allow_from not configured)"

    # 0.0.0.0 への開放は行わず、指定サブネットからのみ許可
    cmds = []
    for subnet in UFW_ALLOW_FROM:
        cmds.append(f"sudo ufw allow from {subnet} to any port {CLIENT_P2P_PORT} proto tcp")

    cmd = " && ".join(cmds)
    result = subprocess.run(
        f"ssh -o StrictHostKeyChecking=no {SSH_USER}@{ip} 'export LC_ALL=C; {cmd}'",
        shell=True, capture_output=True, text=True, timeout=30,
    )
    if result.returncode == 0:
        return f"OK (client_p2p_port={CLIENT_P2P_PORT}, allow_from={UFW_ALLOW_FROM})"
    return f"FAILED: {result.stderr[:300]}"


def rsync_to_device(ip: str, peer_id: int) -> str:
    """1デバイスへ設定・peer固有データ・モデルキャッシュ・ソースコードを転送。"""
    remote_path = f"{SSH_USER}@{ip}:{DEPLOY_DIR}"

    # リモートディレクトリ作成
    subprocess.run(
        f"ssh -o StrictHostKeyChecking=no {SSH_USER}@{ip} "
        f"'export LC_ALL=C; mkdir -p {DEPLOY_DIR}/config {DEPLOY_DIR}/data/train {DEPLOY_DIR}/data/test {DEPLOY_DIR}/logs {DEPLOY_DIR}/cache {DEPLOY_DIR}/results {DEPLOY_DIR}/src'",
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

    # peer固有の訓練データ転送（deploy_dir/data/ 下を参照）
    train_file = Path(DEPLOY_DIR) / "data" / "train" / f"peer_{peer_id}.json"
    if train_file.exists():
        subprocess.run(
            [
                "rsync", "-az", "--quiet",
                str(train_file),
                f"{remote_path}/data/train/peer_{peer_id}.json",
            ],
            check=True,
        )

    # peer固有のテストデータ転送（deploy_dir/data/ 下を参照）
    test_file = Path(DEPLOY_DIR) / "data" / "test" / f"peer_{peer_id}.json"
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

    # モデルキャッシュ転送（HuggingFaceキャッシュ形式）
    model_id = _get("model", "model_id", "google/gemma-4-E2B")
    model_path_parts = model_id.split("/")
    model_src = str(BASE_DIR / "cache" / "models" / model_path_parts[0] / model_path_parts[1])
    model_dst = f"{remote_path}/cache/models/{model_id}/"
    if Path(model_src).exists():
        subprocess.run(
            [
                "rsync", "-az", "--quiet",
                model_src + "/",
                model_dst,
            ],
            check=True,
            timeout=600,
        )

    return f"OK (peer={peer_id}, ip={ip})"


def pull_docker_image(ip: str, peer_id: int) -> str:
    """1デバイスが管理サーバーのレジストリからHTTPでpull。

    各デバイス上で:
    1. insecure-registries に 127.0.0.1:5000 を追加（既存設定を保持）
    2. Docker daemon を再起動
    3. SSHトンネルを張って 127.0.0.1:5000 へ接続
    4. docker pull 127.0.0.1:5000/wafl-peft:latest を実行
    """
    import time

    control_path = os.path.expanduser(f"~/.ssh/control-registry-{peer_id}")

    # 各デバイス上でport 5000を使用中のプロセスをkill（SSHトンネル用）
    subprocess.run(
        f"ssh -o StrictHostKeyChecking=no {SSH_USER}@{ip} "
        f"'export LC_ALL=C; lsof -ti:5000 | xargs -r kill -9 2>/dev/null; "
        f"sleep 1; "
        f"ss -tln 2>/dev/null | grep -q :5000 && echo port5000_still_used || echo port5000_free'",
        shell=True, capture_output=True, text=True, timeout=15,
    )

  # insecure-registries に 127.0.0.1:5000 を追加 + Docker再起動
    # 階層SSHでheredoc quotingが壊れるため、python3 -c でスクリプトファイルを生成
    ensure_cmd = (
        f"ssh {SSH_USER}@{ip} "
        f"'export LC_ALL=C; "
        f"sudo python3 -c \"import json,os; "
        f'p=\"/etc/docker/daemon.json\"; '
        f'd=json.load(open(p)) if os.path.exists(p) else dict(); '
        f'regs=list(set(d.get(\"insecure-registries\",[])+[\"127.0.0.1:5000\"])); '
        f'd[\"insecure-registries\"]=regs; '
        f'json.dump(d,open(p,\"w\"),indent=2)\"; '
        f"echo \"daemon.json updated:\"; "
        f"cat /etc/docker/daemon.json; "
        f"sudo systemctl restart docker; "
        f"sleep 5; "
        f"sudo docker info >/dev/null 2>&1 && echo \"docker_ok\" || echo \"docker_fail\"; "
        f"echo \"done\"; "
        f"exit 0'"
    )
    ensure_result = subprocess.run(
        ensure_cmd, shell=True, capture_output=True, text=True, timeout=120,
    )
    if ensure_result.returncode != 0:
        return f"FAILED docker config (peer={peer_id}, ip={ip}): {ensure_result.stdout.strip()[-500:]}"
    print(f"  [peer={peer_id}] Docker config updated: {ensure_result.stdout.strip()[:200]}")

    # SSHトンネルを張って管理サーバーのregistryへ接続
    tunnel_cmd = (
        f"ssh {SSH_USER}@{ip} "
        f"'export LC_ALL=C; mkdir -p ~/.ssh; "
        f"(ssh -S {control_path} -O exit {SSH_USER}@{SERVER_HOST} 2>/dev/null) || true; "
        f"sleep 1; "
        f"ssh -N -L 127.0.0.1:{REGISTRY_PORT}:127.0.0.1:{REGISTRY_PORT} "
        f"-S {control_path} "
        f"-o StrictHostKeyChecking=no "
        f"-o UserKnownHostsFile=/dev/null "
        f"-o ServerAliveInterval=60 "
        f"-o ServerAliveCountMax=3 "
        f"-f {SSH_USER}@{SERVER_HOST}'"
    )
    tunnel_result = subprocess.run(
        tunnel_cmd, shell=True, capture_output=True, text=True, timeout=30,
    )
    if tunnel_result.returncode != 0:
        return f"FAILED tunnel (peer={peer_id}, ip={ip}): {tunnel_result.stderr[:200]}"

    # トンネルが張れるか確認
    for _ in range(30):
        check = subprocess.run(
            f"ssh {SSH_USER}@{ip} 'export LC_ALL=C; ss -tln 2>/dev/null | grep -q 127.0.0.1:{REGISTRY_PORT} && echo ok'",
            shell=True, capture_output=True, text=True, timeout=5,
        )
        if check.stdout.strip() == "ok":
            break
        time.sleep(1)

    # docker pull を実行（insecure-registries + SSHトンネル → HTTP で接続）
    pull_cmd = (
        f"ssh {SSH_USER}@{ip} "
        f"'export LC_ALL=C; DOCKER_MAX_CONCURRENT_DOWNLOADS=5 DOCKER_MAX_CONCURRENT_PULLS=5 docker pull {IMAGE_NAME}'"
    )
    pull_failed = None
    for attempt in range(3):
        pull_result = subprocess.run(
            pull_cmd, shell=True, capture_output=True, text=True, timeout=600,
        )
        if pull_result.returncode == 0:
            if attempt > 0:
                return f"Docker pulled after {attempt+1} retries (peer={peer_id}, ip={ip})"
            return f"Docker pulled (peer={peer_id}, ip={ip})"
        pull_failed = pull_result
        if attempt < 2:
            time.sleep(5)
    return f"FAILED (peer={peer_id}, ip={ip}): {pull_failed.stderr[:500]}"


def start_server_container() -> str:
    """管理サーバー上でサーバーコンテナを起動。"""
    cmd = (
        f"ssh -o StrictHostKeyChecking=no {SSH_USER}@localhost "
        f"'mkdir -p {DEPLOY_DIR}/logs && chown {SSH_USER}:{SSH_USER} {DEPLOY_DIR}/logs; "
        f"docker rm -f wafl-peft-server 2>/dev/null; "
        f"docker run -d --name wafl-peft-server "
        f"--net=host "
        f"-v {DEPLOY_DIR}/src:/app/src "
        f"-v {DEPLOY_DIR}/config:/app/config "
        f"-v {DEPLOY_DIR}/data:/app/data "
        f"-v {DEPLOY_DIR}/logs:/app/logs "
        f"{IMAGE_NAME} "
        f"/app/.venv/bin/python src/server.py'"
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
        f"'export LC_ALL=C; mkdir -p {DEPLOY_DIR}/logs && chown {SSH_USER}:{SSH_USER} {DEPLOY_DIR}/logs; "
        f"docker rm -f wafl-peft-client-{peer_id} 2>/dev/null; "
        f"docker run -d --name wafl-peft-client-{peer_id} "
        f"--add-host wafl-ctrl1:{SERVER_HOST_IP} "
        f"-e PEER_ID={peer_id} "
        f"-v {DEPLOY_DIR}/src:/app/src "
        f"-v {DEPLOY_DIR}/config:/app/config "
        f"-v {DEPLOY_DIR}/data:/app/data "
        f"-v {DEPLOY_DIR}/cache:/app/cache "
        f"-v {DEPLOY_DIR}/logs:/app/logs "
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
    print("[deploy_distribute] Starting distribution...")

    hosts = load_hosts()
    print(f"[deploy_distribute] Target devices: {len(hosts)}")

    max_workers = 5

    # Phase 0: Open ufw ports
    print("\n[deploy_distribute] Phase 0: Opening ufw ports...")
    print(f"  [INFO] server_port={SERVER_PORT}, client_p2p_port={CLIENT_P2P_PORT}")
    print(f"  [INFO] ufw_allow_from={UFW_ALLOW_FROM}")

    result = open_ufw_on_server()
    if result.startswith("OK"):
        print(f"  [OK] {result}")
    elif result.startswith("SKIPPED"):
        print(f"  [SKIP] {result}")
    else:
        print(f"  [FAIL] {result}")

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {
            executor.submit(open_ufw_on_device, ip, i): i
            for i, ip in enumerate(hosts)
        }
        for future in as_completed(futures):
            peer_id = futures[future]
            try:
                result = future.result()
                if result.startswith("OK"):
                    print(f"  [OK] {result}")
                elif result.startswith("SKIP"):
                    print(f"  [SKIP] {result}")
                else:
                    print(f"  [FAIL] {result}")
            except Exception as e:
                print(f"  [FAIL] peer={peer_id}: {e}")

    # Phase 2: rsync for config and data
    print("\n[deploy_distribute] Phase 2: Rsync config and data...")
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

    # Phase 3: Pull Docker image from management server registry
    print("\n[deploy_distribute] Phase 3: Pull Docker images from registry...")
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

    # Phase 4: Start server container
    print("\n[deploy_distribute] Phase 4: Start server container...")
    result = start_server_container()
    print(f"  [OK] {result}")

    # Phase 5: Start client containers
    print("\n[deploy_distribute] Phase 5: Start client containers...")
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
    print(f"[deploy_distribute] Experiment dir: {EXPERIMENT_DIR_NAME}")
    print(f"[deploy_distribute] Monitor with: docker exec -it wafl-peft-server tail -f /app/results/{EXPERIMENT_DIR_NAME}/logs/metrics_peer_0_final.jsonl")


if __name__ == "__main__":
    main()
