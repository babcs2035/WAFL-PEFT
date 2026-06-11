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
from pathlib import Path

from utils import get_base_dir, get_hosts_path, load_config

BASE_DIR = get_base_dir()
CONFIG = load_config()

SSH_USER = CONFIG["ssh_user"]
DEPLOY_DIR = os.path.expanduser(CONFIG["deploy_dir"])
COLLECT_DIR = BASE_DIR / "collected_logs"
COLLECT_DIR.mkdir(parents=True, exist_ok=True)


def load_hosts() -> list[str]:
    """hosts.txtからIPリストを読み込み。"""
    ips = []
    for line in get_hosts_path().read_text().strip().splitlines():
        ip = line.strip()
        if ip and not ip.startswith("#"):
            ips.append(ip)
    return ips


def collect_from_device(ip: str, peer_id: int) -> str:
    """1デバイスからログと重みをscpで回収。"""
    remote_log_dir = f"{DEPLOY_DIR}/logs"
    local_peer_dir = COLLECT_DIR / f"peer_{peer_id}"
    local_peer_dir.mkdir(parents=True, exist_ok=True)

    # ログファイル回収
    cmd_logs = (
        f"scp -r {SSH_USER}@{ip}:{remote_log_dir}/ "
        f"{str(local_peer_dir)}/"
    )
    result = subprocess.run(cmd_logs, shell=True, capture_output=True, text=True)
    if result.returncode != 0:
        return f"FAILED logs (peer={peer_id}): {result.stderr[:200]}"

    # 設定ファイルも回収
    cmd_config = (
        f"scp {SSH_USER}@{ip}:{DEPLOY_DIR}/config/settings.json "
        f"{str(local_peer_dir)}/"
    )
    subprocess.run(cmd_config, shell=True, capture_output=True)

    return f"OK (peer={peer_id}, ip={ip})"


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
    for peer_dir in COLLECT_DIR.iterdir():
        if peer_dir.is_dir():
            for f in peer_dir.glob("metrics_peer_*_final.jsonl"):
                total_metrics += sum(1 for _ in f.read_text().strip().splitlines() if _.strip())
            for f in peer_dir.glob("weights_step_*.pt"):
                total_weights += 1

    print(f"  Total metric entries: {total_metrics}")
    print(f"  Total weight checkpoints: {total_weights}")
    print("[collect_logs] Ready for analysis: mise run analyze:evaluate")


if __name__ == "__main__":
    main()
