#!/usr/bin/env python3
"""管理サーバー上から評価専用ホストへ評価ワーカーコンテナを起動する。

config/hosts.eval.txt を読み込み、各評価ホストへソース・設定・モデル/データセット
キャッシュを rsync してから、eval_worker.py を動かすコンテナを起動する。各評価ホストは
config/hosts.eval.txt の行番号に対応する学習 peer（config/hosts.txt の同じ行）の
checkpoint を担当する。

前提: 各評価ホストは事前に `mise deploy:eval`（deploy_distribute.py eval）で provisioning
済みであること。すなわち docker イメージ (127.0.0.1:5000/wafl-peft:latest) を registry から
pull 済みで、ベースモデルと GSM8K データセットキャッシュも配布済みであること。本スクリプトは
最新ソースの同期とコンテナ起動のみを行う（image は registry pull で配布し、save/load は使わない）。
未 pull のホストは起動をスキップして警告する。

評価は学習ノードの weights を rsync で取得するため、評価コンテナには SSH 鍵
（/home/$SSH_USER/.ssh）を read-only でマウントする。
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
SERVER_HOST_IP = _server_ip if _server_ip else socket.gethostbyname(SERVER_HOST)
REGISTRY_PORT = 5000
IMAGE_NAME = f"127.0.0.1:{REGISTRY_PORT}/wafl-peft:latest"

_CURRENT_HOSTNAME = socket.gethostname()
if _CURRENT_HOSTNAME == SERVER_HOST or SERVER_HOST_IP in _CURRENT_HOSTNAME:
    _JUMP = ""
else:
    _JUMP = f"-J {SSH_USER}@{SERVER_HOST}"


def load_eval_hosts() -> list[str]:
    """hosts.eval.txt から評価ホスト IP リストを読み込む（行順＝担当 peer_id）。"""
    path = BASE_DIR / "config" / "hosts.eval.txt"
    ips: list[str] = []
    for line in path.read_text().strip().splitlines():
        ip = line.strip()
        if ip and not ip.startswith("#"):
            ips.append(ip)
    return ips


def _ssh_prefix() -> str:
    jump_flag = f"-J {SSH_USER}@{SERVER_HOST} " if _JUMP else ""
    return f"ssh -o StrictHostKeyChecking=no {jump_flag}"


def sync_src_to_eval_host(ip: str, eval_peer_id: int) -> str:
    """評価ホストへ最新のソース・設定のみを rsync で転送する。

    ベースモデル・GSM8K データセット・docker イメージは `mise deploy:eval`
    （deploy_distribute.py eval、registry pull）で配布済みの前提。ここでは反復ごとに
    変わりうるソース/設定だけを軽量に同期する（start_clients.py の sync_source と同様）。
    """
    jump_flag = f"-J {SSH_USER}@{SERVER_HOST} " if _JUMP else ""
    ssh_e = f"-e 'ssh -o StrictHostKeyChecking=no {jump_flag}'"
    src = (
        f"rsync -az {ssh_e} --delete "
        f"--exclude='__pycache__' --exclude='*.pyc' --exclude='.git' --exclude='.venv' "
        f"--exclude='data/' --exclude='logs/' --exclude='output/' --exclude='cache/' "
        f"--exclude='results/' "
        f"{BASE_DIR}/ {SSH_USER}@{ip}:{DEPLOY_DIR}/"
    )
    r = subprocess.run(src, shell=True, capture_output=True, text=True, timeout=180)
    if r.returncode != 0:
        return f"FAILED rsync src (eval_peer={eval_peer_id}, ip={ip}): {r.stderr[:200]}"
    return f"OK sync (eval_peer={eval_peer_id}, ip={ip})"


def start_eval_container(ip: str, eval_peer_id: int) -> str:
    """1 評価ホストで eval_worker コンテナを起動する。"""
    sync_result = sync_src_to_eval_host(ip, eval_peer_id)
    if not sync_result.startswith("OK"):
        return sync_result

    # image の有無を確認。未取得なら起動をスキップして警告する（image の配布は
    # `mise deploy:eval` が registry pull で行う。save/load は使わない）
    img_check = subprocess.run(
        f"{_ssh_prefix()}{SSH_USER}@{ip} "
        f"'docker images {IMAGE_NAME} --format {{{{.Repository}}}} | head -1'",
        shell=True, capture_output=True, text=True, timeout=60,
    )
    if IMAGE_NAME.split("/")[-1].split(":")[0] not in img_check.stdout:
        return (
            f"FAILED (eval_peer={eval_peer_id}, ip={ip}): docker image 未 pull。"
            f"先に `mise deploy:eval` を実行してから再試行すること"
        )

    jump_flag = f"-J {SSH_USER}@{SERVER_HOST} " if _JUMP else ""
    cmd = (
        f"ssh -o StrictHostKeyChecking=no {jump_flag}{SSH_USER}@{ip} "
        f"'export LC_ALL=C; mkdir -p {DEPLOY_DIR}/logs; "
        f"docker rm -f wafl-peft-eval-{eval_peer_id} 2>/dev/null || true; "
        f"docker run -d --name wafl-peft-eval-{eval_peer_id} "
        f"--gpus all --net=host "
        f"--add-host {SERVER_HOST}:{SERVER_HOST_IP} "
        f"-e EVAL_PEER_ID={eval_peer_id} "
        f"-e PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True "
        f"-v /home/{SSH_USER}/.ssh:/home/{SSH_USER}/.ssh:ro "
        f"-v {DEPLOY_DIR}/src:/app/src "
        f"-v {DEPLOY_DIR}/config:/app/config "
        f"-v {DEPLOY_DIR}/cache:/app/cache "
        f"-v {DEPLOY_DIR}/logs:/app/logs "
        f"-v {DEPLOY_DIR}/results:/app/results "
        f"{IMAGE_NAME} "
        f"/app/.venv/bin/python src/eval_worker.py'"
    )
    result = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=120)
    if result.returncode == 0:
        return f"OK (eval_peer={eval_peer_id}, ip={ip}, container={result.stdout.strip()[:12]})"
    return f"FAILED start (eval_peer={eval_peer_id}, ip={ip}): {result.stderr[:300]}"


def main() -> None:
    """メインエントリポイント。

    いずれかの評価ホストで起動に失敗した場合（image 未 pull を含む）は非ゼロで
    終了する。image が配布されていないまま評価が始まらないのは異常状態のため、
    スキップではなくエラーとして扱う。
    """
    import sys

    print("[start_eval_workers] Starting evaluation worker containers...")
    hosts = load_eval_hosts()
    print(f"[start_eval_workers] Eval hosts: {len(hosts)} (1:1 with training peers)")

    fail_count = 0
    with ThreadPoolExecutor(max_workers=max(1, len(hosts))) as executor:
        futures = {
            executor.submit(start_eval_container, ip, i): i
            for i, ip in enumerate(hosts)
        }
        for future in as_completed(futures):
            eval_peer_id = futures[future]
            try:
                result = future.result()
                print(f"  [{result}]")
                if not result.startswith("OK"):
                    fail_count += 1
            except Exception as e:
                print(f"  [FAIL] eval_peer={eval_peer_id}: {e}")
                fail_count += 1

    if fail_count > 0:
        print(f"\n[start_eval_workers] FAILED: {fail_count} eval host(s) did not start. "
              "image 未 pull の場合は `mise deploy:eval` を先に実行すること。")
        sys.exit(1)
    print("\n[start_eval_workers] Done.")


if __name__ == "__main__":
    main()
