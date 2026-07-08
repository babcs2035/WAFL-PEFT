#!/usr/bin/env python3
"""管理サーバー上から全学習デバイスへ並列SSHでログを回収。

各ノードのローカルSSDに蓄積されたメトリクスログと
LoRA重みチェックポイントを管理サーバーへ一括回収する。
"""

import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path

from utils import get_base_dir, get_hosts_path, _get, _get_int, _get_str

BASE_DIR = get_base_dir()


# 実験ディレクトリ名の取得（.experiment_meta.json を優先）
def _load_experiment_dir_name() -> str:
    """実験ディレクトリ名を .experiment_meta.json から読み込み、なければ生成する。"""
    meta_path = BASE_DIR / "results" / ".experiment_meta.json"
    if meta_path.exists():
        try:
            with open(meta_path) as f:
                meta = json.load(f)
            name = meta.get("dir_name")
            if name:
                return name
        except (json.JSONDecodeError, Exception):
            pass

    exp_name = _get("experiment", "experiment_name", "default")
    timestamp = datetime.now(timezone(timedelta(hours=9))).strftime('%Y%m%dT%H%M%S')
    return f"{exp_name}_{timestamp}"


SSH_USER = _get_str("deployment", "ssh_user")
DEPLOY_DIR = os.path.expanduser(_get_str("deployment", "deploy_dir"))
EXPERIMENT_DIR_NAME = _load_experiment_dir_name()
COLLECT_DIR = BASE_DIR / "results" / EXPERIMENT_DIR_NAME
COLLECT_DIR.mkdir(parents=True, exist_ok=True)

# 管理サーバー上の実験ディレクトリ名（collect_logs.py 上で管理）
SERVER_EXPERIMENT_DIR_NAME = EXPERIMENT_DIR_NAME

# 管理サーバー上かローカルかでSSH接続方法を変える
import socket
_server_ip = _get("server", "server_ip")
if _server_ip:
    SERVER_HOST_IP = _server_ip
else:
    SERVER_HOST_IP = socket.gethostbyname(_get_str("server", "server_host"))
_current_hostname = socket.gethostname()
if _current_hostname == _get_str("server", "server_host") or SERVER_HOST_IP in _current_hostname:
    _JUMP = ""
else:
    _JUMP = f"-J {SSH_USER}@{_get_str('server', 'server_host')}"


def load_hosts() -> list[str]:
    """hosts.txtからIPリストを読み込み。"""
    ips = []
    for line in get_hosts_path().read_text().strip().splitlines():
        ip = line.strip()
        if ip and not ip.startswith("#"):
            ips.append(ip)
    return ips


def collect_from_device(ip: str, peer_id: int) -> str:
    """1デバイスからログと重みをrsyncで回収。

    rsyncの末尾スラッシュ構文により、リモートディレクトリの内容が
    ローカルディレクトリに直接コピーされ、不要な中間ディレクトリが
    作成されないようにする。
    """
    remote_log_dir = f"{DEPLOY_DIR}/logs"
    local_peer_dir = COLLECT_DIR / "logs" / f"peer_{peer_id}"
    local_peer_dir.mkdir(parents=True, exist_ok=True)

    # SSHオプション（管理サーバー上かローカルかで動的）
    jump_flag = f"-J {SSH_USER}@{_get_str('server', 'server_host')} " if _JUMP else ""

    # ログファイル回収（rsyncでコンテンツのみ同期）
    cmd_logs = (
        f"rsync -az -e 'ssh -o StrictHostKeyChecking=no {jump_flag}' "
        f"{SSH_USER}@{ip}:{remote_log_dir}/ "
        f"{str(local_peer_dir)}/"
    )
    result = subprocess.run(cmd_logs, shell=True, capture_output=True, text=True)
    if result.returncode != 0:
        return f"FAILED logs (peer={peer_id}): {result.stderr[:200]}"

    # 設定ファイルも回収
    cmd_config = (
        f"scp -o StrictHostKeyChecking=no {jump_flag} "
        f"{SSH_USER}@{ip}:{DEPLOY_DIR}/config/settings.json "
        f"{str(local_peer_dir)}/"
    )
    subprocess.run(cmd_config, shell=True, capture_output=True)

    return f"OK (peer={peer_id}, ip={ip})"


def save_experiment_meta() -> None:
    """実験ディレクトリ名をメタファイルへ保存。"""
    meta_path = BASE_DIR / "results" / ".experiment_meta.json"
    meta_path.write_text(json.dumps({"dir_name": EXPERIMENT_DIR_NAME}))


def main() -> None:
    """メインエントリポイント。"""
    print("[collect_logs] Starting log collection...")

    hosts = load_hosts()
    print(f"[collect_logs] Target devices: {len(hosts)}")

    max_workers = 10

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {
            executor.submit(collect_from_device, ip, i): i
            for i, ip in enumerate(hosts)
        }
        for future in as_completed(futures):
            peer_id = futures[future]
            try:
                result = future.result()
                print(f"  [OK] {result}")
            except Exception as e:
                print(f"  [FAIL] peer={peer_id}: {e}")

    print(f"\n[collect_logs] Collection complete. Logs saved to: {COLLECT_DIR}")

    # 統計を出力
    total_metrics = 0
    total_weights = 0
    logs_dir = COLLECT_DIR / "logs"
    if logs_dir.exists():
        for peer_dir in logs_dir.iterdir():
            if peer_dir.is_dir():
                for f in peer_dir.glob("metrics_peer_*_final.log"):
                    content = f.read_text().strip()
                    if content:
                        total_metrics += sum(1 for line in content.splitlines() if line.strip())
                # weights/ サブディレクトリ内も検索
                wd = peer_dir / "weights"
                if wd.is_dir():
                    for f in wd.glob("weights_step_*.pt"):
                        total_weights += 1
                # 直接の重みファイルも検索（後方互換）
                for f in peer_dir.glob("weights_step_*.pt"):
                    total_weights += 1

    print(f"  Total metric entries: {total_metrics}")
    print(f"  Total weight checkpoints: {total_weights}")
    # 実験ディレクトリ名を出力（miseタスクがローカルに保存するために使用）
    print(f"__EXPERIMENT_DIR__:{EXPERIMENT_DIR_NAME}")
    print("[collect_logs] Ready for analysis: mise run analyze:evaluate")


if __name__ == "__main__":
    main()
