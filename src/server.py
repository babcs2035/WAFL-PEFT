#!/usr/bin/env python3
"""WAFL-PEFT実験管理サーバー。

contact_pattern.jsonに基づき各クライアントにシグナルを配信し、
Datetimeハンドシェイクで実験開始を同期し、実験終了後にログを回収する。

サーバー自体もマルチスレッドで動作し、実験のライフサイクル管理（シグナル配信・
終了判定）と、フェデレーテッド学習全体としての収束性能（全peer平均マージモデルの
accuracy）のリアルタイム評価を並行して行う。

評価の役割分担:
  - 実験中: 管理サーバーがマージモデルのみを一定間隔で評価する（各学習デバイス
    からLoRA重みを収集・平均マージし、1回のGSM8K評価で済むため高速に多くの
    ラウンドを回せる）。学習デバイス側では実験中に一切評価を行わない
    （学習中モデルはgradient_checkpointingによりuse_cache=Falseのため、
    model.generate()を使う評価を行うとGPU/GILを長時間占有し訓練スレッドを
    巻き込むストールを引き起こすことが実機テストで確認された）。
  - 実験終了後: 各学習デバイスが自分のGPU（訓練終了により空いている）で
    自分のチェックポイント履歴を評価し、メトリクスログに記録する
    （client.pyのrun_post_experiment_evaluation）。5台が並列に実行するため、
    1台分の評価時間で全ノードの収束曲線が得られる。管理サーバーは
    collect_logs.pyの既存rsync機構でこれを回収するだけでよい。

ログのprefixは "[HH:MM:SS][SERVER][<スレッド識別子>]" の形式で、実時刻と
どのスレッドの出力かを一目で判別できるようにする。
  [Main]       : メインスレッド（起動・シャットダウン処理）
  [Accept]     : クライアント接続受付スレッド（_accept_clients）。実験開始前は
                 登録・Ready待受を、実験終了後は各クライアントの実験後評価
                 完了通知（evaluation_complete）を受け付け、全デバイスが
                 実験・評価を完了したことを検出・ログ出力する。
  [Broadcast]  : シグナル配信スレッド（_broadcast_signals）
  [Monitor]    : 実験状態監視スレッド（_wait_for_ready, _monitor_experiment）
  [GlobalEval] : グローバルモデル（平均マージ）の収束性能のリアルタイム評価
                 スレッド（_global_eval_thread）。各学習デバイスからLoRA重み
                 チェックポイントをSSH+rsyncで収集し、平均マージしたモデルを
                 GSM8Kバリデーションセットで評価する。結果は
                 results/{experiment_dir}/global_eval.log に追記される。
"""

import json
import os
import shutil
import socket
import subprocess
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any

from utils import (
    get_base_dir,
    get_experiment_name,
    get_hosts_path,
    _get,
    _get_float,
    _get_int,
    _get_str,
)

# 最後のイベント（開始または終了）の後，P2P通信の後処理や最終集計のための
# 猶予として設ける固定バッファ秒数
_EXPERIMENT_END_BUFFER_SECONDS = 60.0

# 実験終了後、各クライアントが自分のチェックポイント履歴の評価
# （evaluation_complete通知）を送ってくるのを待つ最大秒数。5台が並列に
# 実行するため通常はもっと早く終わるが、応答がないクライアントのために
# _accept_clientsが無期限に待ち続けないよう上限を設ける。
# client.pyのrun_post_experiment_evaluationは最大8チェックポイントを
# 評価し、実機テストで1チェックポイントあたり約2〜2.5分（1問256トークン
# まで生成・20問）かかることを確認済み（8チェックポイントで実測17〜18分）。
# 900秒（15分）では実際にこのタイムアウトが評価完了より先に発生し、
# 完了通知を静かに取りこぼす不具合が実機で発生したため、十分な安全マージンを
# 見て2400秒（40分）とする
_POST_EXPERIMENT_EVAL_GRACE_SECONDS = 2400.0


def _now() -> str:
    """現在時刻をHH:MM:SS形式で返す（ログの実時刻prefix用）。"""
    return time.strftime("%H:%M:%S")


class ClientSession:
    """1クライアントごとのセッション管理。"""

    def __init__(self, peer_id: int, conn: socket.socket, addr: tuple[str, int]) -> None:
        self.peer_id = peer_id
        self.conn = conn
        self.addr = addr
        self.ready = False
        self.last_heartbeat = time.time()
        # このセッションがまだ配信していない，timeline中の先頭イベントのインデックス。
        # セッション単位で保持することで，クライアントが切断・再接続した際に
        # 新しいClientSessionインスタンス（next_event_index=0）から始まり，
        # 経過時間以下の全イベントを最初から再配信できる（単一の共有カーソルだと，
        # 再接続後は未来のイベントしか受け取れず，再接続前に発生した接触の
        # start/endが永久に届かなくなる不具合があった）
        self.next_event_index = 0

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
        base_dir = get_base_dir()

        # 接触パターン読み込み。ファイル名はsettings.jsonのexperiment.contact_pattern_file
        # で指定し，data/contact_pattern/ 配下（generate_contact_pattern.pyの出力先）
        # から解決する（1時刻キーに複数の開始/終了イベントが紐づくため，
        # (event_time, event) のフラットなタイムラインに展開する）
        contact_pattern_file = _get_str("experiment", "contact_pattern_file")
        if not contact_pattern_file:
            raise ValueError(
                "settings.json の experiment.contact_pattern_file が未設定です。"
                "data/contact_pattern/ 配下の接触パターンJSONのファイル名を指定してください。"
            )
        contact_path = base_dir / "data" / "contact_pattern" / contact_pattern_file
        with open(contact_path) as f:
            self.contact_pattern: dict[str, list[dict[str, Any]]] = json.load(f)

        self.timeline: list[tuple[float, dict[str, Any]]] = []
        for time_str, events in sorted(self.contact_pattern.items(), key=lambda x: float(x[0])):
            t = float(time_str)
            for event in events:
                self.timeline.append((t, event))

        # experiment_duration は最後のイベント時刻に固定バッファを加えて算出する。
        # remaining_timeという事前予測値が無くなったため，「最後に何らかの
        # 状態変化（開始/終了）が起きた時刻」を基準にできる
        if self.timeline:
            last_event_time = max(t for t, _ in self.timeline)
            self.experiment_duration = last_event_time + _EXPERIMENT_END_BUFFER_SECONDS
        else:
            self.experiment_duration = 300.0

        self.server_port = _get_int("server", "server_port")
        self.client_p2p_port = _get_int("communication", "client_p2p_port")

        # クライアント管理
        self.sessions: dict[int, ClientSession] = {}
        self.sessions_lock = threading.Lock()
        self.all_ready = False
        self.experiment_start_time: float | None = None
        self.experiment_end_time: float | None = None
        self.experiment_dir_name: str | None = None

        # 実験後評価の完了を通知してきたpeer_idの集合。各クライアントは
        # experiment_stop受信後、自分のGPUで自分のチェックポイント履歴を評価し
        # 終わり次第 "evaluation_complete" を送ってくる（_accept_clients参照）
        self.evaluation_done: set[int] = set()
        self.evaluation_done_lock = threading.Lock()
        self._all_evaluations_logged = False

        # ソケット
        self.server_socket: socket.socket | None = None

    def start(self) -> None:
        """サーバーを起動。"""
        import sys
        print(f"[{_now()}][SERVER][Main] ============================================================", flush=True)
        print(f"[{_now()}][SERVER][Main] WAFL-PEFT Experiment Server Starting", flush=True)
        print(f"[{_now()}][SERVER][Main] ============================================================", flush=True)
        print(f"[{_now()}][SERVER][Main] Port: {self.server_port}", flush=True)
        print(f"[{_now()}][SERVER][Main] P2P Port: {self.client_p2p_port}", flush=True)
        print(f"[{_now()}][SERVER][Main] Experiment Duration: {self.experiment_duration}s", flush=True)
        print(f"[{_now()}][SERVER][Main] Contact Pattern Timeline: {len(self.timeline)} events", flush=True)
        for t, event in self.timeline:
            print(f"[{_now()}][SERVER][Main]   t={t}s: {event['event']} peers={event['peers']}", flush=True)
        sys.stdout.flush()

        # TCPサーバーソケット作成
        self.server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.server_socket.settimeout(1.0)
        self.server_socket.bind(("0.0.0.0", self.server_port))
        self.server_socket.listen(128)
        print(f"[{_now()}][SERVER][Main] TCP socket bound to 0.0.0.0:{self.server_port}, listening...", flush=True)
        sys.stdout.flush()

        # クライアント受信スレッド
        accept_thread = threading.Thread(target=self._accept_clients, daemon=True)
        accept_thread.start()

        # シグナル配信スレッド
        signal_thread = threading.Thread(target=self._broadcast_signals, daemon=True)
        signal_thread.start()

        # 実験終了監視スレッド
        monitor_thread = threading.Thread(target=self._monitor_experiment, daemon=True)
        monitor_thread.start()

        # グローバルモデル収束性能のリアルタイム評価スレッド
        global_eval_thread = threading.Thread(target=self._global_eval_thread, daemon=True)
        global_eval_thread.start()

        print(f"[{_now()}][SERVER][Main] All threads started. Waiting for clients...", flush=True)
        print(f"[{_now()}][SERVER][Main] Expected clients: {len(self._collect_all_peers())}", flush=True)
        sys.stdout.flush()

    def _collect_all_peers(self) -> set[int]:
        """contact_patternの全イベントに登場する一意のpeer_idを集める。"""
        all_peers: set[int] = set()
        for _, event in self.timeline:
            all_peers.update(event["peers"])
        return all_peers

    def _all_evaluations_done(self) -> bool:
        """期待される全peerが実験後評価の完了を通知済みか判定する。"""
        expected = self._collect_all_peers()
        with self.evaluation_done_lock:
            return bool(expected) and expected.issubset(self.evaluation_done)

    def _accept_clients(self) -> None:
        """クライアント接続を受付ける。

        実験開始前は登録（register）・Ready待受を、実験終了後は各クライアントの
        実験後評価の完了通知（evaluation_complete）を受け付ける。全peerの評価
        完了を検出した時点、またはグレース期間（_POST_EXPERIMENT_EVAL_GRACE_SECONDS）
        が過ぎた時点でループを終える。
        """
        while self.server_socket:
            if self.experiment_end_time is not None:
                elapsed_since_end = time.time() - self.experiment_end_time
                if self._all_evaluations_done() or elapsed_since_end > _POST_EXPERIMENT_EVAL_GRACE_SECONDS:
                    break

            try:
                conn, addr = self.server_socket.accept()
            except OSError:
                continue

            try:
                session = ClientSession(-1, conn, addr)
                msg = session.receive_json(timeout=10.0)
                if not msg:
                    session.close()
                    continue

                if msg.get("type") == "evaluation_complete":
                    peer_id = msg["peer_id"]
                    with self.evaluation_done_lock:
                        self.evaluation_done.add(peer_id)
                        done_count = len(self.evaluation_done)
                    expected = len(self._collect_all_peers())
                    print(
                        f"[{_now()}][SERVER][Accept] Peer {peer_id} finished experiment and "
                        f"post-experiment evaluation. ({done_count}/{expected})", flush=True,
                    )
                    if self._all_evaluations_done() and not self._all_evaluations_logged:
                        self._all_evaluations_logged = True
                        print(
                            f"[{_now()}][SERVER][Accept] All {expected}/{expected} devices have "
                            "finished the experiment and evaluation.", flush=True,
                        )
                    session.close()
                    continue

                if msg.get("type") != "register":
                    print(f"[{_now()}][SERVER][Accept] Invalid registration from {addr}, closing", flush=True)
                    session.close()
                    continue

                print(f"[{_now()}][SERVER][Accept] New connection from {addr}", flush=True)
                peer_id = msg["peer_id"]
                with self.sessions_lock:
                    self.sessions[peer_id] = session

                print(f"[{_now()}][SERVER][Accept] Client registered: peer_id={peer_id}, addr={addr}", flush=True)
                print(f"[{_now()}][SERVER][Accept] Registered clients: {list(self.sessions.keys())}", flush=True)

                # Ready信号を待受（別スレッドで処理）
                def wait_ready(sess: ClientSession, pid: int) -> None:
                    ready_msg = sess.receive_json(timeout=300.0)
                    if ready_msg and ready_msg.get("type") == "ready":
                        with self.sessions_lock:
                            if pid in self.sessions:
                                self.sessions[pid].ready = True
                        print(f"[{_now()}][SERVER][Accept] Client {pid} is ready. ({sum(1 for s in self.sessions.values() if s.ready)}/{len(self.sessions)} ready)", flush=True)

                ready_thread = threading.Thread(
                    target=wait_ready, args=(session, peer_id), daemon=True
                )
                ready_thread.start()

            except OSError:
                continue

    def _broadcast_signals(self) -> None:
        """接触パターンに基づき，開始/終了イベントを配信する。

        毎秒，各セッションが個別に持つnext_event_indexを見て，まだそのセッションに
        配信していないイベントのうち経過時間以下になったものを送る。イベントが
        1件も無い周回でも空の"events"リストを含むsignalを毎秒必ず送信する。
        クライアント側の受信ループ（server_listener_thread内の_recv_json）は
        一定時間（30秒）応答が無いと切断・再接続する設計のため，イベントが
        発生しない期間が続いても生存確認として毎秒何かを送る必要がある
        （新規イベントがある時だけ送信する設計にした結果，イベント間隔がタイムアウトを
        超えるとクライアントが誤って切断・再接続を繰り返す不具合が実際に発生した）。

        next_event_indexをセッション単位（全クライアント共有ではなく）で
        管理するのは，切断・再接続したクライアントが新しいClientSessionインスタンス
        （next_event_index=0）から始まり，経過時間以下の全イベントを最初から
        受け取れるようにするため。単一の共有カーソルだと，再接続後は未来の
        イベントしか受け取れず，再接続前に発生した接触のstart/endが永久に
        届かなくなる。
        """
        while not self.experiment_end_time:
            if self.experiment_start_time is None:
                time.sleep(0.1)
                continue

            elapsed = time.time() - self.experiment_start_time

            with self.sessions_lock:
                for session in self.sessions.values():
                    new_events: list[dict[str, Any]] = []
                    while (
                        session.next_event_index < len(self.timeline)
                        and self.timeline[session.next_event_index][0] <= elapsed
                    ):
                        _, event = self.timeline[session.next_event_index]
                        new_events.append(event)
                        session.next_event_index += 1

                    signal_data: dict[str, Any] = {
                        "type": "signal",
                        "elapsed": elapsed,
                        "events": new_events,
                    }
                    session.send_json(signal_data)

            time.sleep(1.0)

    def _wait_for_ready(self) -> None:
        """全クライアントのReadyを待受。"""
        expected = len(self._collect_all_peers())
        print(f"[{_now()}][SERVER][Monitor] Expected clients from contact_pattern: {expected} (unique peers across all events)", flush=True)
        print(f"[{_now()}][SERVER][Monitor] Waiting for {expected} clients to be ready...", flush=True)

        while not self.all_ready and not self.experiment_end_time:
            with self.sessions_lock:
                ready_count = sum(
                    1 for s in self.sessions.values() if s.ready
                )
                if ready_count >= expected and expected > 0:
                    self.all_ready = True
                    # 実験開始時刻をここで一度だけ生成し、そのままresults/下に
                    # 実験ディレクトリの実体として作成する。全クライアントは
                    # 同じexperiment_startメッセージのdatetimeを受信するため、
                    # タイムスタンプの発行元をこの一箇所に限定できる
                    # （旧: setup_data.py/deploy_distribute.py/collect_logs.pyが
                    # それぞれ独自に.experiment_meta.jsonへタイムスタンプを書き込んで
                    # おり、実行タイミング次第でresultsの実データと食い違う
                    # 不具合があった。analyze:collectが存在しないディレクトリを
                    # 参照し続ける事象として実際に観測された）
                    now_dt = datetime.now(timezone(timedelta(hours=9)))
                    now = now_dt.isoformat()
                    exp_dir_name = f"{get_experiment_name()}_{now_dt.strftime('%Y%m%dT%H%M%S')}"
                    self.experiment_dir_name = exp_dir_name
                    (get_base_dir() / "results" / exp_dir_name).mkdir(parents=True, exist_ok=True)
                    broadcast = {
                        "type": "experiment_start",
                        "datetime": now,
                        "duration": self.experiment_duration,
                    }
                    for session in self.sessions.values():
                        session.send_json(broadcast)
                    self.experiment_start_time = time.time()
                    print(f"[{_now()}][SERVER][Monitor] All {ready_count}/{expected} clients ready. Experiment START at {now}", flush=True)
                    print(f"[{_now()}][SERVER][Monitor] Experiment directory: results/{exp_dir_name}", flush=True)
                    print(f"[{_now()}][SERVER][Monitor] Registered peers: {list(self.sessions.keys())}", flush=True)
                    return

            with self.sessions_lock:
                current_ready = sum(1 for s in self.sessions.values() if s.ready)
                current_registered = len(self.sessions)
            print(f"[{_now()}][SERVER][Monitor] Ready: {current_ready}/{expected}, Registered: {current_registered}", flush=True)
            time.sleep(1.0)

    def _monitor_experiment(self) -> None:
        """実験状態を監視。"""
        self._wait_for_ready()

        if self.experiment_start_time is None:
            print(f"[{_now()}][SERVER][Monitor] Experiment start time not set. Exiting monitor.", flush=True)
            return

        print(f"[{_now()}][SERVER][Monitor] Experiment running. Duration: {self.experiment_duration}s", flush=True)

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
                print(f"[{_now()}][SERVER][Monitor] Experiment STOPPED after {self.experiment_duration}s", flush=True)
                break
            if int(elapsed) % 10 == 0:
                remaining = self.experiment_duration - elapsed
                print(f"[{_now()}][SERVER][Monitor] Experiment running... Elapsed: {elapsed:.1f}s, Remaining: {remaining:.1f}s", flush=True)
            time.sleep(1.0)

    def _collect_latest_weights(self, ip: str, peer_id: int, tmp_dir: Any) -> str:
        """1学習デバイスから最新のweightsディレクトリをSSH+rsyncで収集する。

        実験中のため転送中のファイルが不完全な場合があるが、
        gsm8k_eval.load_merged_checkpoint() がEOFError/RuntimeErrorを
        握りつぶして次回のポーリングに委ねる設計になっている。
        """
        ssh_user = _get_str("deployment", "ssh_user")
        deploy_dir = os.path.expanduser(_get_str("deployment", "deploy_dir"))
        remote_weights_dir = f"{deploy_dir}/logs/weights"
        local_peer_dir = tmp_dir / f"peer_{peer_id}" / "weights"
        local_peer_dir.mkdir(parents=True, exist_ok=True)

        cmd = (
            f"rsync -az -e 'ssh -o StrictHostKeyChecking=no' "
            f"{ssh_user}@{ip}:{remote_weights_dir}/ "
            f"{local_peer_dir}/"
        )
        result = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=60)
        if result.returncode != 0:
            return f"FAILED (peer={peer_id}): {result.stderr[:200]}"
        return f"OK (peer={peer_id})"

    def _global_eval_thread(self) -> None:
        """グローバルモデル（全peer LoRA重み平均）の収束性能を実験中にリアルタイム評価する。

        各学習デバイスから最新のLoRA重みチェックポイントをSSH+rsyncで収集し、
        平均マージしたモデルをGSM8Kバリデーションセットで評価する。結果は
        results/{experiment_dir}/global_eval.log にJSON Lines形式で追記される。

        マージモデルのみを評価するのは、デバイス個別の評価も同じラウンドで
        行うと学習デバイス数に比例して評価時間が伸び（1台あたり数十秒〜数分の
        generate()呼び出しが必要）、1ラウンドが interval_seconds を大きく超えて
        しまい、短い実験時間内に十分な数のラウンドを回せなくなるため。デバイス
        個別の収束曲線は、実験終了後に各学習デバイス自身が空いた自分のGPUで
        評価する（client.pyのrun_post_experiment_evaluation。5台が並列に実行
        するため1台分の評価時間で済む）。

        実験終了後の一括評価（mise run analyze）を待たずに、フェデレーテッド学習
        全体としての収束傾向をリアルタイムで監視できる。モデルロードは実験開始を
        待たずに行い、実験開始後すぐに評価を始められるようにする。
        """
        import torch

        import gsm8k_eval

        base_dir = get_base_dir()
        interval_seconds = _get_float("global_eval", "interval_seconds", 120.0)
        sample_limit = _get_int("global_eval", "sample_limit", 20)
        model_id = _get("model", "model_id")
        lora_rank = _get_int("training", "lora_rank")
        lora_alpha = _get_int("training", "lora_alpha")

        print(f"[{_now()}][SERVER][GlobalEval] Loading GSM8K validation data...", flush=True)
        val_data = gsm8k_eval.load_gsm8k_val_data(base_dir, sample_limit=sample_limit)
        if not val_data:
            print(f"[{_now()}][SERVER][GlobalEval] GSM8K validation data not available. Disabled.", flush=True)
            return

        device_id = 0 if torch.cuda.is_available() else None
        print(f"[{_now()}][SERVER][GlobalEval] Loading model: {model_id} (4-bit quantized)...", flush=True)
        model, tokenizer = gsm8k_eval.build_lora_model(model_id, lora_rank, lora_alpha, device_id, base_dir=base_dir)
        print(f"[{_now()}][SERVER][GlobalEval] Model ready. Waiting for experiment to start...", flush=True)

        # 実験開始（全peer ready）を待つ
        while not self.experiment_start_time and not self.experiment_end_time:
            time.sleep(1.0)
        if self.experiment_end_time or self.experiment_dir_name is None:
            print(f"[{_now()}][SERVER][GlobalEval] Experiment ended before starting. Exiting.", flush=True)
            return

        experiment_dir = base_dir / "results" / self.experiment_dir_name
        tmp_dir = experiment_dir / "global_eval_tmp"
        tmp_dir.mkdir(parents=True, exist_ok=True)
        log_path = experiment_dir / "global_eval.log"
        hosts = self.get_client_list()

        print(f"[{_now()}][SERVER][GlobalEval] Monitoring started. Interval: {interval_seconds}s", flush=True)

        while not self.experiment_end_time:
            try:
                print(f"[{_now()}][SERVER][GlobalEval] Collecting checkpoints from {len(hosts)} devices...", flush=True)
                for i, ip in enumerate(hosts):
                    result = self._collect_latest_weights(ip, i, tmp_dir)
                    if not result.startswith("OK"):
                        print(f"[{_now()}][SERVER][GlobalEval]   {result}", flush=True)

                steps = gsm8k_eval.find_all_checkpoint_steps(tmp_dir)
                if not steps:
                    print(f"[{_now()}][SERVER][GlobalEval] No checkpoints available yet. Skipping this round.", flush=True)
                else:
                    latest_step = steps[-1]
                    weights = gsm8k_eval.load_merged_checkpoint(tmp_dir, latest_step)
                    if weights is None:
                        print(f"[{_now()}][SERVER][GlobalEval] Failed to load checkpoint at step {latest_step}. Skipping.", flush=True)
                    else:
                        accuracy = gsm8k_eval.evaluate_weights(model, tokenizer, weights, val_data)
                        print(
                            f"[{_now()}][SERVER][GlobalEval] Step {latest_step}: "
                            f"Global merged model accuracy = {accuracy:.1f}%", flush=True,
                        )
                        record = {
                            "step": latest_step, "timestamp": _now(),
                            "accuracy": accuracy, "num_devices": len(hosts),
                        }
                        with open(log_path, "a") as f:
                            f.write(json.dumps(record) + "\n")
                            f.flush()
                            os.fsync(f.fileno())
            except Exception as e:
                print(f"[{_now()}][SERVER][GlobalEval] Error during evaluation round: {e}", flush=True)

            for _ in range(int(interval_seconds)):
                if self.experiment_end_time:
                    break
                time.sleep(1.0)

        shutil.rmtree(tmp_dir, ignore_errors=True)
        print(f"[{_now()}][SERVER][GlobalEval] Stopped.", flush=True)

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
        print(f"[{_now()}][SERVER][Main] Received signal {signum}. Shutting down...")
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
        print("[{_now()}][SERVER][Main] Server stopped.")


if __name__ == "__main__":
    main()
