#!/usr/bin/env python3
"""WAFL-PEFT実験管理サーバー。

contact_pattern.jsonに基づき各クライアントにシグナルを配信し、
Datetimeハンドシェイクで実験開始を同期し、実験終了後にログを回収する。
"""

import json
import socket
import threading
import time
from datetime import datetime, timezone
from typing import Any

from utils import get_base_dir, get_hosts_path, load_config



class ClientSession:
    """1クライアントごとのセッション管理。"""

    def __init__(self, peer_id: int, conn: socket.socket, addr: tuple[str, int]) -> None:
        self.peer_id = peer_id
        self.conn = conn
        self.addr = addr
        self.ready = False
        self.last_heartbeat = time.time()

    def send_json(self, data: dict[str, Any]) -> None:
        """JSONデータをクライアントへ送信。"""
        try:
            payload = json.dumps(data).encode("utf-8")
            # 4バイト長さヘッダ + JSONボディ
            self.conn.send(len(payload).to_bytes(4, "big"))
            self.conn.send(payload)
        except OSError:
            pass

    def receive_json(self, timeout: float = 5.0) -> dict[str, Any] | None:
        """クライアントからJSONデータを受信。"""
        try:
            self.conn.settimeout(timeout)
            # 4バイト長さヘッダ
            header = self._recv_exact(4)
            if header is None:
                return None
            length = int.from_bytes(header, "big")
            if length > 10 * 1024 * 1024:  # 10MB制限
                return None
            body = self._recv_exact(length)
            if body is None:
                return None
            return json.loads(body.decode("utf-8"))
        except (OSError, json.JSONDecodeError, ValueError):
            return None

    def _recv_exact(self, n: int) -> bytes | None:
        """正確にnバイトを受信。"""
        buf = b""
        while len(buf) < n:
            try:
                chunk = self.conn.recv(n - len(buf))
                if not chunk:
                    return None
                buf += chunk
            except OSError:
                return None
        return buf

    def close(self) -> None:
        """セッションを終了。"""
        try:
            self.conn.close()
        except OSError:
            pass


class WAFLServer:
    """WAFL-PEFT実験管理サーバー。

    TCPポートで待機し、クライアントからのReady信号を受信後、
    contact_pattern.jsonに基づき時変トポロジーシグナルを配信する。
    """

    def __init__(self) -> None:
        self.config = load_config()
        base_dir = get_base_dir()

        # 接触パターン読み込み
        contact_path = base_dir / "config" / "contact_pattern.json"
        with open(contact_path) as f:
            self.contact_pattern: dict[str, dict[str, Any]] = json.load(f)

        # 時間ベースのシグナルを整理（sorted by time key）
        self.timeline: list[tuple[float, dict[str, Any]]] = []
        for time_str, peers in sorted(self.contact_pattern.items(), key=lambda x: float(x[0])):
            t = float(time_str)
            self.timeline.append((t, peers))

        self.server_port = self.config["server_port"]
        self.client_p2p_port = self.config["client_p2p_port"]
        self.experiment_duration = self.config.get("experiment_duration_seconds", 300)

        # クライアント管理
        self.sessions: dict[int, ClientSession] = {}
        self.sessions_lock = threading.Lock()
        self.all_ready = False
        self.experiment_start_time: float | None = None
        self.experiment_end_time: float | None = None

        # ソケット
        self.server_socket: socket.socket | None = None

    def start(self) -> None:
        """サーバーを起動。"""
        print(f"[SERVER] Starting WAFL-PEFT Experiment Server on port {self.server_port}")
        print(f"[SERVER] Contact pattern timeline: {len(self.timeline)} events")

        # TCPサーバーソケット作成
        self.server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.server_socket.settimeout(1.0)
        self.server_socket.bind(("0.0.0.0", self.server_port))
        self.server_socket.listen(128)

        # クライアント受信スレッド
        accept_thread = threading.Thread(target=self._accept_clients, daemon=True)
        accept_thread.start()

        # シグナル配信スレッド
        signal_thread = threading.Thread(target=self._broadcast_signals, daemon=True)
        signal_thread.start()

        # 実験終了監視スレッド
        monitor_thread = threading.Thread(target=self._monitor_experiment, daemon=True)
        monitor_thread.start()

        print(f"[SERVER] Waiting for clients... (max={len(self.contact_pattern.get('60', {}))})")

    def _accept_clients(self) -> None:
        """クライアント接続を受付け。登録後、Ready信号を待受。"""
        while self.server_socket and not self.experiment_end_time:
            try:
                conn, addr = self.server_socket.accept()
                session = ClientSession(-1, conn, addr)

                # 登録メッセージを受信
                msg = session.receive_json(timeout=10.0)
                if not msg or msg.get("type") != "register":
                    session.close()
                    continue

                peer_id = msg["peer_id"]
                with self.sessions_lock:
                    self.sessions[peer_id] = session

                print(f"[SERVER] Client registered: peer_id={peer_id}, addr={addr}")

                # Ready信号を待受（別スレッドで処理）
                def wait_ready(sess: ClientSession, pid: int) -> None:
                    ready_msg = sess.receive_json(timeout=300.0)
                    if ready_msg and ready_msg.get("type") == "ready":
                        with self.sessions_lock:
                            if pid in self.sessions:
                                self.sessions[pid].ready = True
                        print(f"[SERVER] Client {pid} is ready.")

                ready_thread = threading.Thread(
                    target=wait_ready, args=(session, peer_id), daemon=True
                )
                ready_thread.start()

            except OSError:
                continue

    def _broadcast_signals(self) -> None:
        """接触パターンに基づきシグナルを配信。"""
        while not self.experiment_end_time:
            if self.experiment_start_time is None:
                time.sleep(0.1)
                continue

            elapsed = time.time() - self.experiment_start_time

            # 現在のタイムラインイベントを適用
            current_signal: dict[int, dict[str, Any]] = {}
            for event_time, peers in self.timeline:
                if event_time <= elapsed:
                    current_signal.update(peers)

            # 全クライアントへシグナル配信
            signal_data: dict[str, Any] = {
                "type": "signal",
                "elapsed": elapsed,
                "peers": current_signal,
            }

            with self.sessions_lock:
                for peer_id, session in self.sessions.items():
                    session.send_json(signal_data)

            time.sleep(1.0)

    def _wait_for_ready(self) -> None:
        """全クライアントのReadyを待受。"""
        # contact_patternから期待されるクライアント数を推定
        first_signal = self.timeline[0][1] if self.timeline else {}
        expected = len(first_signal)

        while not self.all_ready and not self.experiment_end_time:
            with self.sessions_lock:
                ready_count = sum(
                    1 for s in self.sessions.values() if s.ready
                )
                if ready_count >= expected and expected > 0:
                    self.all_ready = True
                    # 実験開始時刻をブロードキャスト
                    now = datetime.now(timezone.utc).isoformat()
                    broadcast = {
                        "type": "experiment_start",
                        "datetime": now,
                        "duration": self.experiment_duration,
                    }
                    for session in self.sessions.values():
                        session.send_json(broadcast)
                    self.experiment_start_time = time.time()
                    print(f"[SERVER] All {ready_count} clients ready. Experiment START at {now}")
                    return

            time.sleep(0.5)

    def _monitor_experiment(self) -> None:
        """実験状態を監視。"""
        self._wait_for_ready()

        if self.experiment_start_time is None:
            return

        # 実験終了まで待機
        while True:
            if self.experiment_end_time:
                break
            elapsed = time.time() - self.experiment_start_time
            if elapsed >= self.experiment_duration:
                self.experiment_end_time = time.time()
                # 実験終了をブロードキャスト
                stop_signal = {
                    "type": "experiment_stop",
                    "elapsed": self.experiment_duration,
                }
                with self.sessions_lock:
                    for session in self.sessions.values():
                        session.send_json(stop_signal)
                print(f"[SERVER] Experiment STOPPED after {self.experiment_duration}s")
                break
            time.sleep(1.0)

    def get_client_list(self) -> list[str]:
        """SSH接続用のクライアントIPリストを返す。"""
        hosts = []
        for line in get_hosts_path().read_text().strip().splitlines():
            ip = line.strip()
            if ip and not ip.startswith("#"):
                hosts.append(ip)
        return hosts


def main() -> None:
    """メインエントリポイント。"""
    server = WAFLServer()

    # SIGINT/SIGTERMでシャットダウン
    import signal

    def shutdown(signum: int, frame: Any) -> None:
        print(f"[SERVER] Received signal {signum}. Shutting down...")
        server.experiment_end_time = time.time()

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    server.start()

    # メインスレッドで待機
    try:
        while server.server_socket:
            time.sleep(1)
    finally:
        if server.server_socket:
            server.server_socket.close()
        print("[SERVER] Server stopped.")


if __name__ == "__main__":
    main()
