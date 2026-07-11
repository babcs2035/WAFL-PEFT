#!/usr/bin/env python3
"""管理サーバー上から全学習デバイスへ並列SSHでログを回収。

各ノードのローカルSSDに蓄積されたメトリクスログと
LoRA重みチェックポイントを管理サーバーへ一括回収する。
"""

import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

from utils import get_base_dir, get_hosts_path, get_latest_experiment_dir, _get, _get_str

# クライアントコンテナ終了待ちの最大秒数。experiment_stop受信後もThread 5
# （非同期評価）の完了待ちでThread 4のログrename（.log -> _final.log）が
# 最大87秒程度（実測のmodel.generate()所要時間）遅れることがあるため、
# それより十分長い余裕を持たせる
_CONTAINER_EXIT_TIMEOUT_SECONDS = 180
_CONTAINER_EXIT_POLL_INTERVAL_SECONDS = 3

BASE_DIR = get_base_dir()

# collect_logs.py は管理サーバー上で実行される想定であり、server.py が実験開始時に
# results/ 直下へ一度だけ作成した実験ディレクトリ（{experiment_name}_{timestamp}）が
# ここから見えるはず。存在しなければ実験が一度も開始していないため回収できない
_experiment_dir = get_latest_experiment_dir()
if _experiment_dir is None:
    print(
        "[collect_logs] No experiment directory found under results/. "
        "`mise run start` で実験が開始されていることを確認してください。",
        file=sys.stderr,
    )
    sys.exit(1)

SSH_USER = _get_str("deployment", "ssh_user")
DEPLOY_DIR = os.path.expanduser(_get_str("deployment", "deploy_dir"))
EXPERIMENT_DIR_NAME = _experiment_dir.name
COLLECT_DIR = _experiment_dir

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


def _wait_for_client_container_exit(ip: str, peer_id: int, jump_flag: str) -> bool:
    """クライアントコンテナ（wafl-peft-client-{peer_id}）が終了するまで待つ。

    experiment_stop受信直後はThread 4がまだログをmetrics_peer_X.logから
    metrics_peer_X_final.logへrenameし終えていない可能性があり、この状態で
    回収すると前回実験の古い_final.logをそのまま取得してしまう
    （実機テストで実際に発生し、2回の実験結果が同一データになった）。
    コンテナ自体の終了（プロセス終了）をもって、ログ書き出しが完了した
    signalとして扱う。
    """
    container_name = f"wafl-peft-client-{peer_id}"
    deadline = time.time() + _CONTAINER_EXIT_TIMEOUT_SECONDS
    while time.time() < deadline:
        check = subprocess.run(
            f"ssh -o StrictHostKeyChecking=no {jump_flag}{SSH_USER}@{ip} "
            f"\"docker ps --format '{{{{.Names}}}}' | grep -qx {container_name} && echo running || echo stopped\"",
            shell=True, capture_output=True, text=True,
        )
        if check.stdout.strip() == "stopped":
            return True
        time.sleep(_CONTAINER_EXIT_POLL_INTERVAL_SECONDS)
    return False


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

    if not _wait_for_client_container_exit(ip, peer_id, jump_flag):
        return (
            f"FAILED logs (peer={peer_id}): container wafl-peft-client-{peer_id} "
            f"still running after {_CONTAINER_EXIT_TIMEOUT_SECONDS}s wait, skipping collection"
        )

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


def main() -> None:
    """メインエントリポイント。"""
    print("[collect_logs] Starting log collection...")
    print(f"[collect_logs] Experiment directory: {EXPERIMENT_DIR_NAME}")

    hosts = load_hosts()
    print(f"[collect_logs] Target devices: {len(hosts)}")

    max_workers = len(hosts)  # hosts.txtのノード数だけ並列実行

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
