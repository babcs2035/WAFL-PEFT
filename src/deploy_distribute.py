#!/usr/bin/env python3
"""管理サーバー上から学習デバイスへの並列デプロイスクリプト。

Phase 0: ufwポート開放
Phase 2: rsyncで設定ファイル・データを各デバイスへ転送
Phase 3: 各デバイスが管理サーバーのレジストリからSSHトンネル経由でpull

コンテナの起動は行わない（start_clients.py / mise run start が担当する）。
以前はこのスクリプトの中でコンテナ起動まで行っていたため、後続の
`mise run start` を実行すると起動済みの実験を強制終了して起動し直す
という重複が生じていた。デプロイ（配布）と実験実行（起動）を明確に分離する。
"""

import os
import socket
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from log import dot, fail, info, ok, phase, skip, summary
from utils import get_base_dir, get_hosts_path, _get, _get_int, _get_str

BASE_DIR = get_base_dir()
DEPLOY_DIR = os.path.expanduser(_get_str("deployment", "deploy_dir"))
SERVER_HOST = _get_str("server", "server_host")

SSH_USER = _get_str("deployment", "ssh_user")
REGISTRY_PORT = 5000
IMAGE_NAME = f"127.0.0.1:{REGISTRY_PORT}/wafl-peft:latest"
SERVER_PORT = _get_int("server", "server_port")
CLIENT_P2P_PORT = _get_int("communication", "client_p2p_port")
UFW_ALLOW_FROM = [
    s.strip() for s in _get("server", "ufw_allow_from", "").split(",") if s.strip()
]

# 管理サーバーのLAN IP（学習デバイスからアクセス可能）
# settings.json の server_ip を優先。なければ DNS 解決（ただし設定必須）
_server_ip = _get("server", "server_ip")
if _server_ip:
    SERVER_HOST_IP = _server_ip
else:
    SERVER_HOST_IP = socket.gethostbyname(SERVER_HOST)

# 管理サーバー上かローカルかでSSH接続方法を変える
# 管理サーバー上: 直接SSH。ローカル: 管理サーバー経由のジャンプSSH
_CURRENT_HOSTNAME = socket.gethostname()
if _CURRENT_HOSTNAME == SERVER_HOST or SERVER_HOST_IP in _CURRENT_HOSTNAME:
    _JUMP = ""
else:
    _JUMP = f"-J {SSH_USER}@{SERVER_HOST}"

# eval モード: `python3 deploy_distribute.py eval` で評価専用ホスト（hosts.eval.txt）へ
# 配布する。学習ノードと同じ Phase（ufw / rsync / registry pull）を再利用し、配布先と
# 同期対象だけを評価ワーカー向けに切り替える。image は学習ノードと同じく registry から
# pull する（save/load は使わない）
_EVAL_MODE = len(sys.argv) > 1 and sys.argv[1] == "eval"


def _peer_ssh(ip: str, cmd: str, stdin_data: str | None = None) -> subprocess.CompletedProcess[str]:
    """ピアノードへSSHしコマンドを実行。

    管理サーバー上では直接SSH、ローカルでは管理サーバー経由のジャンプSSH。
    stdin_data が指定された場合、一時ファイル経由でデータを渡す。
    """
    jump_flag = f"-J {SSH_USER}@{SERVER_HOST} " if _JUMP else ""
    if stdin_data is not None:
        # スクリプトを一時ファイルに書き、それを実行
        script_file = f"/tmp/_deploy_script_{os.getpid()}_{ip.replace('.', '_')}.py"
        cleanup = f"rm -f {script_file}"
        full_cmd = f"cat > {script_file} && sudo python3 {script_file} && {cleanup}"
        return subprocess.run(
            f"ssh -o StrictHostKeyChecking=no {jump_flag}{SSH_USER}@{ip} '{full_cmd}'",
            shell=True, input=stdin_data, capture_output=True, text=True, timeout=120,
        )
    return subprocess.run(
        f"ssh -o StrictHostKeyChecking=no {jump_flag}{SSH_USER}@{ip} '{cmd}'",
        shell=True, capture_output=True, text=True, timeout=120,
    )

# 学習デバイスの /etc/docker/daemon.json に insecure-registries を追加するスクリプト
# （既存設定は保持し、127.0.0.1:5000 のみ追加する）
_DAEMON_JSON_UPDATE_SCRIPT = """import json, os
p = "/etc/docker/daemon.json"
d = json.load(open(p)) if os.path.exists(p) else {}
regs = sorted(set(d.get("insecure-registries", []) + ["127.0.0.1:5000"]))
d["insecure-registries"] = regs
json.dump(d, open(p, "w"), indent=2)
"""


def load_hosts() -> list[str]:
    """配布対象の IP リストを読み込む（eval モードなら hosts.eval.txt）。"""
    fname = "hosts.eval.txt" if _EVAL_MODE else "hosts.txt"
    hosts_path = BASE_DIR / "config" / fname
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


def _rsync_to_peer(
    ip: str, src: str, dst: str, extra_args: str = ""
) -> subprocess.CompletedProcess[str]:
    """ピアノードへ rsync をSSHで実行。

    -e オプションにはrsyncが内部で使うリモートシェルコマンドのみを渡す。
    ユーザー名は宛先側（{SSH_USER}@{ip}:{dst}）で指定するため、ここに
    含めてはならない（含めると rsync がユーザー名をホスト名と誤認して
    名前解決に失敗し、転送が常に失敗する）。
    """
    jump_flag = f"-J {SSH_USER}@{SERVER_HOST} " if _JUMP else ""
    return subprocess.run(
        f"rsync -az -e 'ssh -o StrictHostKeyChecking=no {jump_flag}' "
        f"{extra_args} --info=progress2 '{src}' '{SSH_USER}@{ip}:{dst}'",
        shell=True, capture_output=True, text=True, timeout=600,
    )


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
    result = _peer_ssh(ip, f"export LC_ALL=C; {cmd}")
    if result.returncode == 0:
        return f"OK (client_p2p_port={CLIENT_P2P_PORT}, allow_from={UFW_ALLOW_FROM})"
    return f"FAILED: {result.stderr[:300]}"


def _rsync(src: str, dst: str, extra_args: list[str] | None = None) -> None:
    """rsync を進捗表示付きで実行する。

    --quiet を外し --info=progress2 を設定することで、
    rsync 標準の進捗バーをターミナルに表示する。
    """
    cmd = ["rsync", "-az", "--info=progress2"]
    if extra_args:
        cmd += extra_args
    cmd += [src, dst]
    subprocess.run(cmd, check=True)


def rsync_to_device(ip: str, peer_id: int) -> str:
    """1デバイスへ設定・peer固有データ・モデルキャッシュ・ソースコードを転送。

    各 rsync 操作で --info=progress2 により進捗バーが表示される。
    """
    # モデルキャッシュの転送先はHuggingFaceキャッシュ形式（org/name）でネストするため、
    # 事前にその中間ディレクトリまでmkdirしておく必要がある（rsyncの末尾スラッシュ構文は
    # 転送先の親ディレクトリを自動生成しないため、なければ "mkdir failed: No such file
    # or directory" で転送自体が失敗する）
    model_id = _get("model", "model_id", "google/gemma-4-E2B")
    model_path_parts = model_id.split("/")
    model_cache_dir = f"{DEPLOY_DIR}/cache/models/{model_path_parts[0]}"

    # リモートディレクトリ作成
    result = _peer_ssh(ip, f"export LC_ALL=C; mkdir -p {DEPLOY_DIR}/config {DEPLOY_DIR}/data/train {DEPLOY_DIR}/data/test {DEPLOY_DIR}/logs {model_cache_dir} {DEPLOY_DIR}/cache/datasets {DEPLOY_DIR}/results {DEPLOY_DIR}/src")
    if result.returncode != 0:
        return f"FAILED mkdir (peer={peer_id}, ip={ip})"

    # 設定ファイル転送
    r = _rsync_to_peer(ip, str(BASE_DIR / "config" / "settings.json"), f"{DEPLOY_DIR}/config/settings.json")
    if r.returncode != 0:
        return f"FAILED rsync settings.json (peer={peer_id}, ip={ip}): {r.stderr[:300]}"

    if _EVAL_MODE:
        # 評価ホストは学習しないため peer 固有の訓練/テストデータは不要。代わりに
        # 評価の検証セット（GSM8K データセットキャッシュ）を配る。学習ノード向けの
        # deploy では datasets を配らないので、評価モードでのみ明示的に転送する
        ds_src = str(BASE_DIR / "cache" / "datasets" / "gsm8k")
        if Path(ds_src).exists():
            r = _rsync_to_peer(ip, ds_src + "/", f"{DEPLOY_DIR}/cache/datasets/gsm8k/")
            if r.returncode != 0:
                return f"FAILED rsync datasets (eval_host={peer_id}, ip={ip}): {r.stderr[:300]}"
    else:
        # peer固有の訓練データ転送（deploy_dir/data/ 下を参照）
        train_file = Path(DEPLOY_DIR) / "data" / "train" / f"peer_{peer_id}.json"
        if train_file.exists():
            r = _rsync_to_peer(ip, str(train_file), f"{DEPLOY_DIR}/data/train/peer_{peer_id}.json")
            if r.returncode != 0:
                return f"FAILED rsync train data (peer={peer_id}, ip={ip}): {r.stderr[:300]}"

        # peer固有のテストデータ転送（deploy_dir/data/ 下を参照）
        test_file = Path(DEPLOY_DIR) / "data" / "test" / f"peer_{peer_id}.json"
        if test_file.exists():
            r = _rsync_to_peer(ip, str(test_file), f"{DEPLOY_DIR}/data/test/peer_{peer_id}.json")
            if r.returncode != 0:
                return f"FAILED rsync test data (peer={peer_id}, ip={ip}): {r.stderr[:300]}"

    # ソースコード転送（data/・cache/・results/ は別途転送または不要のため除外）
    r = _rsync_to_peer(
        ip, str(BASE_DIR) + "/", DEPLOY_DIR + "/",
        extra_args="--exclude='data/' --exclude='cache/' --exclude='results/' "
                   "--exclude='.git/' --exclude='__pycache__' --exclude='.venv/'",
    )
    if r.returncode != 0:
        return f"FAILED rsync source (peer={peer_id}, ip={ip}): {r.stderr[:300]}"

    # モデルキャッシュ転送（HuggingFaceキャッシュ形式）
    model_src = str(BASE_DIR / "cache" / "models" / model_path_parts[0] / model_path_parts[1])
    model_dst = f"{DEPLOY_DIR}/cache/models/{model_id}/"
    if Path(model_src).exists():
        r = _rsync_to_peer(ip, model_src + "/", model_dst)
        if r.returncode != 0:
            return f"FAILED rsync model cache (peer={peer_id}, ip={ip}): {r.stderr[:300]}"

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
    result = _peer_ssh(ip, f"export LC_ALL=C; lsof -ti:5000 | xargs -r kill -9 2>/dev/null; sleep 1; ss -tln 2>/dev/null | grep -q :5000 && echo port5000_still_used || echo port5000_free")

    # insecure-registries に 127.0.0.1:5000 を追加（既存設定を保持）
    # python3スクリプトは標準入力で渡す（シェルのクォート二重エスケープで
    # 壊れるため、shell=Trueのコマンド文字列に埋め込まない）
    update_result = _peer_ssh(ip, "sudo python3", stdin_data=_DAEMON_JSON_UPDATE_SCRIPT)
    if update_result.returncode != 0:
        return f"FAILED docker config (peer={peer_id}, ip={ip}): {update_result.stderr[:500]}"

    # Docker再起動
    jump_flag = f"-J {SSH_USER}@{SERVER_HOST} " if _JUMP else ""
    restart_cmd = (
        f"ssh -o StrictHostKeyChecking=no {jump_flag}{SSH_USER}@{ip} "
        f"'export LC_ALL=C; "
        f"echo \"daemon.json updated:\"; "
        f"cat /etc/docker/daemon.json; "
        f"sudo systemctl restart docker; "
        f"sleep 5; "
        f"sudo docker info >/dev/null 2>&1 && echo \"docker_ok\" || echo \"docker_fail\"; "
        f"echo \"done\"'"
    )
    ensure_result = subprocess.run(
        restart_cmd, shell=True, capture_output=True, text=True, timeout=120,
    )
    if ensure_result.returncode != 0 or "docker_fail" in ensure_result.stdout:
        return f"FAILED docker restart (peer={peer_id}, ip={ip}): {ensure_result.stdout.strip()[-500:]}"
    info(f"peer={peer_id} Docker config updated: {ensure_result.stdout.strip()[:200]}")

    # タグなし（dangling）の古いビルド中間イメージをpullの前にpruneしておく。
    # docker buildは--cache-fromで同じタグを繰り返し上書きするため、古いレイヤーが
    # タグなしイメージとして蓄積し続け、実機で学習デバイスのディスクが完全に
    # 枯渇してrsync・docker pullが失敗する障害が実際に発生したため、デプロイの
    # たびに解消する（-aは付けず、稼働中の他プロジェクトのタグ付きイメージは
    # 対象外にする）
    prune_result = _peer_ssh(ip, "docker image prune -f")
    if prune_result.returncode == 0:
        info(f"peer={peer_id} Pruned dangling images: {prune_result.stdout.strip()[-200:]}")

    # SSHトンネルを張って管理サーバーのregistryへ接続
    tunnel_cmd = (
        f"ssh -o StrictHostKeyChecking=no {jump_flag}{SSH_USER}@{ip} "
        f"'export LC_ALL=C; mkdir -p ~/.ssh; "
        f"(ssh -S {control_path} -O exit -o StrictHostKeyChecking=no {SSH_USER}@{SERVER_HOST_IP} 2>/dev/null) || true; "
        f"sleep 1; "
        f"ssh -N -L 127.0.0.1:{REGISTRY_PORT}:127.0.0.1:{REGISTRY_PORT} "
        f"-S {control_path} "
        f"-o StrictHostKeyChecking=no "
        f"-o UserKnownHostsFile=/dev/null "
        f"-o ServerAliveInterval=60 "
        f"-o ServerAliveCountMax=3 "
        f"-f {SSH_USER}@{SERVER_HOST_IP}'"
    )
    tunnel_result = subprocess.run(
        tunnel_cmd, shell=True, capture_output=True, text=True, timeout=30,
    )
    if tunnel_result.returncode != 0:
        return f"FAILED tunnel (peer={peer_id}, ip={ip}): {tunnel_result.stderr[:200]}"

    # トンネルが張れるか確認
    for _ in range(30):
        check = _peer_ssh(ip, f"export LC_ALL=C; ss -tln 2>/dev/null | grep -q 127.0.0.1:{REGISTRY_PORT} && echo ok")
        if check.stdout.strip() == "ok":
            break
        time.sleep(1)

    # docker pull を実行（insecure-registries + SSHトンネル → HTTP で接続）
    pull_cmd = (
        f"ssh -o StrictHostKeyChecking=no {jump_flag}{SSH_USER}@{ip} "
        f"'export LC_ALL=C; DOCKER_MAX_CONCURRENT_DOWNLOADS=5 DOCKER_MAX_CONCURRENT_PULLS=5 docker pull {IMAGE_NAME}'"
    )
    pull_failed = None
    for attempt in range(3):
        pull_result = subprocess.run(
            pull_cmd, shell=True, capture_output=False, text=True, timeout=600,
        )
        if pull_result.returncode == 0:
            if attempt > 0:
                return f"OK (peer={peer_id}, ip={ip}, retries={attempt+1})"
            return f"OK (peer={peer_id}, ip={ip})"
        pull_failed = pull_result
        if attempt < 2:
            time.sleep(5)
    return f"FAILED (peer={peer_id}, ip={ip}): {pull_failed.stderr[:500]}"


def _run_parallel(
    func,
    hosts: list[str],
    max_workers: int = 5,
    phase_name: str = "",
) -> list[str | None]:
    """並列実行して結果リストを返す。完了ごとにドット進捗を表示。"""
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {
            executor.submit(func, ip, i): i
            for i, ip in enumerate(hosts)
        }
        results: list[str | None] = [None] * len(hosts)
        for future in as_completed(futures):
            peer_id = futures[future]
            try:
                results[peer_id] = future.result()
            except Exception as e:
                results[peer_id] = str(e)
            dot(peer_id)
    print()  # ドット行の改行
    for i, r in enumerate(results):
        if r and (r.startswith("FAILED") or r.startswith("Error")):
            print(f"  [DEBUG] peer={i}: {r[:300]}")
    return results



def main() -> None:
    """メインエントリポイント。"""
    hosts = load_hosts()
    info(f"{len(hosts)} target devices")

    max_workers = len(hosts)  # hosts.txtのノード数だけ並列実行

    total_fail_count = 0

    # Phase 0: Open ufw ports
    phase("Phase 0: Open ufw ports")
    info(f"server_port={SERVER_PORT}, client_p2p_port={CLIENT_P2P_PORT}")
    info(f"ufw_allow_from={UFW_ALLOW_FROM}")

    result = open_ufw_on_server()
    if result.startswith("OK"):
        ok(result)
    elif result.startswith("SKIPPED"):
        skip(result)
    else:
        fail(result)
        total_fail_count += 1

    results = _run_parallel(open_ufw_on_device, hosts, max_workers, "ufw")
    ok_count = sum(1 for r in results if r is not None and r.startswith("OK"))
    skip_count = sum(1 for r in results if r is not None and (r.startswith("SKIPPED") or r.startswith("SKIP")))
    fail_count = sum(1 for r in results if r is None or (not r.startswith("OK") and not r.startswith("SKIPPED") and not r.startswith("SKIP")))
    summary("Phase 0", ok_count, fail_count, skip_count)
    total_fail_count += fail_count

    # Phase 2: rsync for config and data
    phase("Phase 2: Rsync config and data")
    results = _run_parallel(rsync_to_device, hosts, max_workers, "rsync")
    ok_count = sum(1 for r in results if r is not None and r.startswith("OK"))
    fail_count = sum(1 for r in results if r is None or (not r.startswith("OK")))
    summary("Phase 2", ok_count, fail_count)
    total_fail_count += fail_count

    # Phase 3: Pull Docker image from management server registry
    phase("Phase 3: Pull Docker images from registry")
    results = _run_parallel(pull_docker_image, hosts, max_workers, "pull")
    ok_count = sum(1 for r in results if r is not None and r.startswith("OK"))
    fail_count = sum(1 for r in results if r is None or (not r.startswith("OK")))
    summary("Phase 3", ok_count, fail_count)
    total_fail_count += fail_count

    # Final summary（コンテナ起動は行わない。mise run start が担当する）
    print()
    if total_fail_count > 0:
        fail(f"Distribution finished with {total_fail_count} failure(s) across all phases")
        info("Fleet state is inconsistent — do not run `mise run start` until failures are resolved")
        sys.exit(1)
    ok("Distribution complete")
    info("Next: run `mise run start` to launch the server and client containers")


if __name__ == "__main__":
    main()
