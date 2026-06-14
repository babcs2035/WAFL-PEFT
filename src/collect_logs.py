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
from datetime import datetime, timezone
from pathlib import Path

from utils import get_base_dir, get_hosts_path, _get, _get_int, _get_str

BASE_DIR = get_base_dir()


# 実験ディレクトリ名の取得（最新の実験ディレクトリを自動選択）
def _load_experiment_dir_name() -> str:
    """最新の実験ディレクトリ名を取得。

    1. DEPLOY_DIR/results/ の最新ディレクトリを優先
    2. BASE_DIR/results/.experiment_meta.json を参照
    3. タイムスタンプ付きデフォルト名を生成
    """
    # 管理サーバー上の最新ディレクトリ
    results_dirs = DEPLOY_DIR + "/results"
    try:
        result = subprocess.run(
            f"ssh -o StrictHostKeyChecking=no {SSH_USER}@localhost ls -1t {results_dirs} | head -1",
            shell=True, capture_output=True, text=True, timeout=10,
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
    except (subprocess.TimeoutExpired, Exception):
        pass

    # BASE_DIR上のメタファイル
    meta_path = BASE_DIR / "results" / ".experiment_meta.json"
    if meta_path.exists():
        try:
            with open(meta_path) as f:
                meta = json.load(f)
            name = meta.get("dir_name")
            if name:
                return name
        except json.JSONDecodeError:
            pass

    exp_name = _get("experiment", "experiment_name", "default")
    timestamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    return f"{exp_name}_{timestamp}"


SSH_USER = _get_str("deployment", "ssh_user")
DEPLOY_DIR = os.path.expanduser(_get_str("deployment", "deploy_dir"))
EXPERIMENT_DIR_NAME = _load_experiment_dir_name()
COLLECT_DIR = BASE_DIR / "results" / EXPERIMENT_DIR_NAME
COLLECT_DIR.mkdir(parents=True, exist_ok=True)

# 管理サーバー上の実験ディレクトリ名（collect_logs.py 上で管理）
SERVER_EXPERIMENT_DIR_NAME = EXPERIMENT_DIR_NAME


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
    local_peer_dir = COLLECT_DIR / "logs" / f"peer_{peer_id}"
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
                for f in peer_dir.glob("weights_step_*.pt"):
                    total_weights += 1

    print(f"  Total metric entries: {total_metrics}")
    print(f"  Total weight checkpoints: {total_weights}")
    # 実験ディレクトリ名を出力（miseタスクがローカルに保存するために使用）
    print(f"__EXPERIMENT_DIR__:{EXPERIMENT_DIR_NAME}")
    print("[collect_logs] Ready for analysis: mise run analyze:evaluate")


if __name__ == "__main__":
    main()
