#!/usr/bin/env python3
"""ローカル環境および全ノードのデプロイ成果物をクリーンアップするスクリプト。

ローカル環境から以下のリソースを削除する:
  - cache/ （ダウンロード済みモデル）
  - .venv/ （仮想環境）
  - data/ （全ファイルとディレクトリ）

管理サーバーと学習デバイスから以下のリソースを削除する:
  - Docker コンテナ（wafl-peft-server, wafl-peft-client-*, registry）
  - Docker イメージ（wafl-peft:latest, registry:2）
  - デプロイディレクトリ

学習デバイスは管理サーバー経由でSSHする。
"""

import os
import subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed

from utils import get_base_dir, _get_str

SERVER_HOST = _get_str("server", "server_host")


def load_hosts() -> list[str]:
    """hosts.txtからIPリストを読み込み。"""
    hosts_path = get_base_dir() / "config" / "hosts.txt"
    ips = []
    for line in hosts_path.read_text().strip().splitlines():
        ip = line.strip()
        if ip and not ip.startswith("#"):
            ips.append(ip)
    return ips


def clean_node(user: str, target: str, deploy_dir: str, peer_id: int | None = None) -> None:
    """1ノードからデプロイ成果物をクリーンアップ。

    管理サーバー（peer_id=None）には直接SSHし、学習デバイスには
    ローカル→管理サーバー→学習デバイス(IP) のネストSSHで接続する。

    Args:
        user: SSH接続ユーザー
        target: ターゲットIPアドレス（管理サーバーはホスト名）
        deploy_dir: クリーンアップ対象のデプロイディレクトリ
        peer_id: クライアントコンテナの識別子（Noneなら管理サーバー）
    """
    def run(cmd: str, timeout: int = 60) -> None:
        """コマンドを実行し、結果を出力。"""
        result = subprocess.run(
            f"ssh -o StrictHostKeyChecking=no {user}@{target} "
            f"bash -s <<'CLEANEOF'\n{cmd}\nCLEANEOF",
            shell=True, capture_output=True, text=True, timeout=timeout,
        )
        if result.returncode != 0 and result.stderr.strip():
            print(f"    WARN: {result.stderr.strip()[:200]}")

    def run_via_master(cmd: str, timeout: int = 60) -> None:
        """管理サーバー経由でコマンドを実行（ネストSSH）。

        ローカル→管理サーバー→学習デバイス(IP) の順でSSHする。
        deploy_distribute.pyと同様、heredocでコマンドを渡す。
        """
        result = subprocess.run(
            f"ssh -o StrictHostKeyChecking=no {user}@{SERVER_HOST} "
            f"ssh -o StrictHostKeyChecking=no {user}@{target} "
            f"bash -s <<'CLEANEOF'\n{cmd}\nCLEANEOF",
            shell=True, capture_output=True, text=True, timeout=timeout,
        )
        if result.returncode != 0 and result.stderr.strip():
            print(f"    WARN: {result.stderr.strip()[:200]}")

    if peer_id is not None:
        run_fn = run_via_master
        print(f"  [client-{peer_id}] Cleaning {target} via {SERVER_HOST}...")
    else:
        run_fn = run
        print(f"  [server] Cleaning {target}...")

    # Step 1: コンテナ削除
    if peer_id is not None:
        run_fn(f"docker rm -f wafl-peft-client-{peer_id} 2>/dev/null || true", timeout=30)
    else:
        run_fn("docker rm -f wafl-peft-server 2>/dev/null || true", timeout=30)
        run_fn("docker rm -f registry 2>/dev/null || true", timeout=30)

    # Step 2: Docker イメージを削除（本アプリケーション関連のみ）
    run_fn("docker rmi -f localhost:5000/wafl-peft:latest 2>/dev/null || true", timeout=30)
    run_fn("docker rmi -f 127.0.0.1:5000/wafl-peft:latest 2>/dev/null || true", timeout=30)
    run_fn("docker rmi -f wafl-peft:latest 2>/dev/null || true", timeout=30)
    run_fn("docker rmi -f registry:2 2>/dev/null || true", timeout=30)

    # Step 3: デプロイディレクトリ削除
    run_fn(f"rm -rf {deploy_dir}", timeout=60)


def clean_local() -> None:
    """ローカル環境のキャッシュ・仮想環境・データを削除。"""
    base = get_base_dir()
    print("[clean:local] Removing local build artifacts...")
    subprocess.run(["rm", "-rf", str(base / "cache")], check=True)
    print("  Removed cache/")
    subprocess.run(["rm", "-rf", str(base / ".venv")], check=True)
    print("  Removed .venv/")
    data_path = base / "data"
    if data_path.exists():
        subprocess.run(["rm", "-rf", str(data_path)], check=True)
        print("  Removed data/")
    print("  OK (local)")


def confirm(prompt: str) -> bool:
    """確認プロンプトを表示。y/Enter で True、他で False。"""
    import sys
    try:
        # TTYでない場合（mise実行時など）はデフォルトで許可
        if not sys.stdin.isatty():
            return True
        return input(prompt).strip().lower() in ("y", "yes", "")
    except (EOFError, KeyboardInterrupt):
        print("\nAborted.")
        return False


def main() -> None:
    """メインエントリポイント。"""
    if not confirm("[WARN] This will delete cache/, .venv/, data/ and all Docker containers/images. Continue? "):
        print("Aborted.")
        return

    user = _get_str("deployment", "ssh_user")
    deploy_dir = os.path.expanduser(_get_str("deployment", "deploy_dir"))
    hosts = load_hosts()

    print("[clean] Full cleanup: local + management server + learning devices...")
    print(f"\n[clean] Cleaning local (cache, .venv, data)...")
    clean_local()

    # 管理サーバーのクリーン
    print(f"\n[clean] Cleaning management server ({SERVER_HOST})...")
    clean_node(user, SERVER_HOST, deploy_dir)
    print("  OK (server)")

    # 学習デバイスのクリーン（管理サーバー経由のネストSSH）
    print("\n[clean] Cleaning learning devices...")
    max_workers = len(hosts)  # hosts.txtのノード数だけ並列実行
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {
            executor.submit(clean_node, user, ip, deploy_dir, i): i
            for i, ip in enumerate(hosts)
        }
        for future in as_completed(futures):
            peer_id = futures[future]
            print(f"  OK (client-{peer_id} on {hosts[peer_id]})")

    print("\n[clean] Cleanup complete.")


if __name__ == "__main__":
    main()
