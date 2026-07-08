#!/usr/bin/env python3
"""各学習デバイスへの nvidia-container-toolkit インストールと Docker GPU ランタイム設定。

hosts.txt に記載された全ピアノードに SSH し、以下の条件で処理を行う。
- NVIDIA GPU が存在しないノード: スキップ（CPU-only 環境でも正常動作）
- GPU があるが nvidia-container-toolkit 未インストール: インストールして設定
- GPU があり既にインストール済み: Docker ランタイム設定のみ確認して再起動
"""

import subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed

from utils import get_base_dir, _get_str

BASE_DIR = get_base_dir()
SSH_USER = _get_str("deployment", "ssh_user")
SERVER_HOST = _get_str("server", "server_host")

# GPU 検出 + nvidia-container-toolkit インストールを行うシェルスクリプト
# 終了コード 0 かつ最終行が NO_GPU / ALREADY_INSTALLED / INSTALLED のいずれか
_INSTALL_SCRIPT = r"""
set -e

# --- GPU の存在確認 ---
if ! command -v nvidia-smi &>/dev/null || ! nvidia-smi &>/dev/null; then
    echo "NO_GPU"
    exit 0
fi

# --- すでにインストール済みなら何もしない ---
if command -v nvidia-ctk &>/dev/null; then
    echo "ALREADY_INSTALLED"
    exit 0
fi

# --- ディスク確保 ---
sudo apt-get clean -qq 2>/dev/null || true
sudo find /tmp -maxdepth 1 -name 'tcpdump_*' -delete 2>/dev/null || true
sudo journalctl --vacuum-size=50M &>/dev/null || true

# --- GPG キーとリポジトリ登録 ---
if [ ! -f /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg ]; then
    curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey -o /tmp/nvidia-gpg.key
    sudo gpg --batch --yes --dearmor \
        -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg \
        /tmp/nvidia-gpg.key
    rm -f /tmp/nvidia-gpg.key
fi

if [ ! -f /etc/apt/sources.list.d/nvidia-container-toolkit.list ]; then
    curl -s -L https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list \
        | sed 's#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#g' \
        | sudo tee /etc/apt/sources.list.d/nvidia-container-toolkit.list
fi

# --- インストール ---
sudo apt-get update -qq 2>&1 | grep -v '^W:' || true
sudo apt-get install -y nvidia-container-toolkit

echo "INSTALLED"
"""

# Docker nvidia ランタイム設定と再起動を行うシェルスクリプト
_CONFIGURE_SCRIPT = r"""
set -e
sudo nvidia-ctk runtime configure --runtime=docker
sudo systemctl restart docker
echo "CONFIGURED"
"""


def _run_via_master(ip: str, script: str, timeout: int) -> subprocess.CompletedProcess:
    """管理サーバー経由でスクリプトを実行する（ネストSSH）。

    学習デバイスはローカル環境から直接到達できないため、
    ローカル→管理サーバー→学習デバイス(IP) の順でSSHする（clean.pyと同様）。
    """
    return subprocess.run(
        f"ssh -o StrictHostKeyChecking=no {SSH_USER}@{SERVER_HOST} "
        f"ssh -o StrictHostKeyChecking=no {SSH_USER}@{ip} "
        f"bash -s <<'GPUSETUPEOF'\n{script}\nGPUSETUPEOF",
        shell=True, capture_output=True, text=True, timeout=timeout,
    )


def setup_gpu_node(ip: str, peer_id: int) -> str:
    """1 ノードに nvidia-container-toolkit をインストールして Docker を設定する。

    GPU が存在しないノードはスキップし OK を返す。
    """
    # GPU 検出とインストール
    result = _run_via_master(ip, _INSTALL_SCRIPT, timeout=300)
    if result.returncode != 0:
        return f"FAILED install (peer={peer_id}, ip={ip}): {result.stderr[:300]}"

    last_line = result.stdout.strip().splitlines()[-1] if result.stdout.strip() else ""

    if last_line == "NO_GPU":
        return f"OK (peer={peer_id}, ip={ip}, gpu=none, skipped)"

    install_status = "skipped (already installed)" if last_line == "ALREADY_INSTALLED" else "installed"

    # Docker ランタイム設定と再起動
    result2 = _run_via_master(ip, _CONFIGURE_SCRIPT, timeout=60)
    if result2.returncode != 0:
        return f"FAILED configure (peer={peer_id}, ip={ip}): {result2.stderr[:300]}"

    return f"OK (peer={peer_id}, ip={ip}, toolkit={install_status})"


def load_hosts() -> list[str]:
    """hosts.txt からピアノードの IP 一覧を取得する。"""
    hosts_path = BASE_DIR / "config" / "hosts.txt"
    ips = []
    for line in hosts_path.read_text().strip().splitlines():
        ip = line.strip()
        if ip and not ip.startswith("#"):
            ips.append(ip)
    return ips


def main() -> None:
    """メインエントリポイント。"""
    print("[setup_gpu_nodes] Configuring GPU runtime on peer nodes...")

    hosts = load_hosts()
    print(f"[setup_gpu_nodes] Target nodes: {len(hosts)}")

    ok_count = 0
    fail_count = 0

    with ThreadPoolExecutor(max_workers=min(len(hosts), 16)) as executor:
        futures = {
            executor.submit(setup_gpu_node, ip, i): i
            for i, ip in enumerate(hosts)
        }
        for future in as_completed(futures):
            peer_id = futures[future]
            try:
                result = future.result()
                if result.startswith("OK"):
                    ok_count += 1
                    print(f"  [OK] {result}")
                else:
                    fail_count += 1
                    print(f"  [FAIL] {result}")
            except Exception as e:
                fail_count += 1
                print(f"  [FAIL] peer={peer_id}: {e}")

    print(f"\n[setup_gpu_nodes] {ok_count} OK, {fail_count} FAIL")
    if fail_count > 0:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
