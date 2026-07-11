#!/usr/bin/env python3
"""Random Waypoint Mobility (RWP) シミュレーションによる contact_pattern.json 生成スクリプト。

hosts.txtの行数からノード数を決定し，指定エリア内でRWPモデルに従って
ノードを移動させ，無線到達距離内に入った接触区間を検出する。検出した
接触区間を，WAFL-PEFTのserver.pyが読み込む開始/終了イベント形式（時刻
キー → その時刻に発生したイベントのリスト）に変換してJSON出力する。

出力先は data/contact_pattern/ 配下（config/contact_pattern.json への
反映はユーザーが手動で行う）。
"""

import argparse
import json
import math
import random
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from tqdm import tqdm

from log import fail, info, ok, phase
from utils import get_base_dir, get_hosts_path

# シミュレーション既定値
DEFAULT_N_TIME = 600
DEFAULT_MIN_SPEED = 1.0
DEFAULT_MAX_SPEED = 5.0
DEFAULT_RADIO_RANGE = 100
DEFAULT_AREA_SIZE = 500
DEFAULT_POSE_TIME = 10
DEFAULT_SEED = 42

# アニメーション既定値（元のRWP参考実装から踏襲）
ANIMATION_MAX_FRAMES = 256
ANIMATION_FRAME_DURATION_MS = 64

OUTPUT_SUBDIR = "contact_pattern"


def count_nodes_from_hosts() -> int:
    """hosts.txtの行数からノード数を決定する。

    setup_data.py, clean.pyのhosts.txt読み込みロジックと同様に，
    コメント行（#始まり）と空行を除いた行数をノード数として扱う。
    """
    hosts = []
    for line in get_hosts_path().read_text().strip().splitlines():
        ip = line.strip()
        if ip and not ip.startswith("#"):
            hosts.append(ip)
    return len(hosts)


def initialize_nodes(
    n_node: int, area_size: int, rng: random.Random
) -> list[tuple[float, float]]:
    """各ノードの初期位置をエリア内にランダム配置する。"""
    return [
        (float(rng.randint(0, area_size)), float(rng.randint(0, area_size)))
        for _ in range(n_node)
    ]


def simulate_rwp(
    n_time: int,
    n_node: int,
    min_speed: float,
    max_speed: float,
    radio_range: int,
    area_size: int,
    pose_time: int,
    rng: random.Random,
    on_step: Callable[[int, list[tuple[float, float]]], None] | None = None,
) -> list[dict[int, list[int]]]:
    """RWPモデルで全ノードを時間発展させ，各ステップの接触隣接リストを返す。

    Args:
        on_step: 各ステップ終了後に (t, node_location) を渡して呼ばれる
            コールバック（アニメーション用スナップショット取得のためのフック）。
            Noneなら何もしない。
    Returns:
        各要素が {peer_id: [接触中のpeer_idのリスト]} の辞書であるリスト。
        リストのインデックスがそのままステップ番号（t，秒）に対応する。
    """
    node_location = initialize_nodes(n_node, area_size, rng)
    # ノードは常にpose_time秒静止した後にしか移動を開始しないため，
    # 実際に参照される時点では必ず一度書き込まれた後の値になる。
    # 未使用時のプレースホルダとして意味のある値を入れ，Optional型を避ける
    node_travel_speed: list[float] = [0.0] * n_node
    node_pose_remaining_time = [pose_time] * n_node
    node_next_location: list[tuple[float, float]] = list(node_location)

    contact_list: list[dict[int, list[int]]] = []

    for t in tqdm(range(n_time), desc="Simulating RWP", unit="step"):
        if on_step is not None:
            on_step(t, node_location)

        for i in range(n_node):
            if node_pose_remaining_time[i] == 0:
                x, y = node_location[i]
                tx, ty = node_next_location[i]
                ax, ay = tx - x, ty - y
                distance = math.sqrt(ax**2 + ay**2)
                speed = node_travel_speed[i]
                vx, vy = speed * ax / distance, speed * ay / distance
                x, y = x + vx, y + vy

                if (x - tx) ** 2 + (y - ty) ** 2 < speed**2:
                    node_location[i] = (tx, ty)
                    node_pose_remaining_time[i] = pose_time
                else:
                    node_location[i] = (x, y)
            else:
                node_pose_remaining_time[i] -= 1
                if node_pose_remaining_time[i] == 0:
                    node_next_location[i] = (
                        float(rng.randint(0, area_size)),
                        float(rng.randint(0, area_size)),
                    )
                    node_travel_speed[i] = rng.uniform(min_speed, max_speed)

        node_in_contact: dict[int, list[int]] = {i: [] for i in range(n_node)}
        for i in range(n_node):
            for j in range(n_node):
                if i == j:
                    continue
                xi, yi = node_location[i]
                xj, yj = node_location[j]
                if (xi - xj) ** 2 + (yi - yj) ** 2 < radio_range**2:
                    node_in_contact[i].append(j)
        contact_list.append(node_in_contact)

    return contact_list


@dataclass(frozen=True)
class ContactInterval:
    """1組のpeerペアが接触していた区間（ステップ単位，半開区間）。

    [start_step, end_step) の間，peer_iとpeer_jは接触状態にあったことを表す。
    """

    peer_i: int
    peer_j: int
    start_step: int
    end_step: int


def extract_contact_intervals(
    contact_list: list[dict[int, list[int]]], n_node: int
) -> list[ContactInterval]:
    """各時刻の隣接リスト列から，ペアごとの連続接触区間を検出する。"""
    n_time = len(contact_list)
    intervals: list[ContactInterval] = []

    for i in range(n_node):
        for j in range(i + 1, n_node):
            in_contact_prev = False
            start = 0
            for t in range(n_time):
                in_contact_now = j in contact_list[t].get(i, [])
                if in_contact_now and not in_contact_prev:
                    start = t
                if not in_contact_now and in_contact_prev:
                    intervals.append(ContactInterval(i, j, start, t))
                in_contact_prev = in_contact_now
            if in_contact_prev:
                intervals.append(ContactInterval(i, j, start, n_time))

    return intervals


def build_contact_events(
    intervals: list[ContactInterval],
) -> dict[int, list[dict[str, Any]]]:
    """接触区間リストから，時刻ごとの開始/終了イベント辞書を構築する。

    各ContactIntervalについて，start_stepに"start"イベント，end_stepに
    "end"イベントをそれぞれ1つ生成する。
    """
    events: dict[int, list[dict[str, Any]]] = {}
    for iv in intervals:
        peers = sorted((iv.peer_i, iv.peer_j))
        events.setdefault(iv.start_step, []).append({"event": "start", "peers": peers})
        events.setdefault(iv.end_step, []).append({"event": "end", "peers": peers})
    return events


def to_json_serializable(
    events: dict[int, list[dict[str, Any]]]
) -> dict[str, list[dict[str, Any]]]:
    """イベント辞書をJSON出力用の文字列キー構造に変換する。

    同一ステップ内では"start"を"end"より先に，さらにpeers順でソートして
    出力を安定させる。
    """
    result: dict[str, list[dict[str, Any]]] = {}
    if 0 not in events:
        result["0"] = []
    for t in sorted(events.keys()):
        sorted_events = sorted(
            events[t], key=lambda e: (e["event"] != "start", e["peers"])
        )
        result[str(t)] = sorted_events
    return result


def find_isolated_nodes(
    events: dict[int, list[dict[str, Any]]], n_node: int
) -> set[int]:
    """一度も接触イベントに登場しないノードを検出する。"""
    touched: set[int] = set()
    for step_events in events.values():
        for event in step_events:
            touched.update(event["peers"])
    return set(range(n_node)) - touched


def build_output_filename(
    n_node: int, area_size: int, radio_range: int, pose_time: int, seed: int
) -> str:
    """元プログラムの命名規則に倣ったファイル名（拡張子なし）を生成する。"""
    return (
        f"rwp_n{n_node:02d}_a{area_size:04d}_r{radio_range:03d}"
        f"_p{pose_time:02d}_s{seed:02d}"
    )


def snapshot(
    x: tuple[float, ...],
    y: tuple[float, ...],
    t: int,
    area_size: int,
    radio_range: int,
    images_dir: Path,
    title: str = "",
) -> None:
    """ノード位置と接続関係のスナップショット画像を1枚生成する。"""
    import matplotlib.patches as patches
    import matplotlib.pyplot as plt

    plt.clf()
    plt.scatter(x, y, s=20, c="Black", marker="o")
    n_node = len(x)
    for i in range(n_node):
        c = patches.Circle((x[i], y[i]), radius=radio_range, fc="b", fill=False)
        plt.gca().add_patch(c)
        plt.annotate(str(i), (x[i] + 5, y[i] + 5))
    for i in range(n_node):
        for j in range(i):
            if (x[i] - x[j]) ** 2 + (y[i] - y[j]) ** 2 <= radio_range**2:
                plt.plot([x[i], x[j]], [y[i], y[j]], color="Black")
    plt.xlim([0, area_size])
    plt.ylim([0, area_size])
    plt.gca().set_aspect("equal")
    plt.xlabel("X")
    plt.ylabel("Y")
    if title:
        plt.title(title, fontsize=12)
    plt.savefig(str(images_dir / f"node_location_{t:04d}.png"))


def generate_animation(frame_paths: list[Path], output_dir: Path, filename_stem: str) -> None:
    """スナップショット画像列からGIFアニメーションを生成し，中間画像を削除する。"""
    from PIL import Image

    if not frame_paths:
        return

    gif_path = output_dir / f"{filename_stem}.gif"
    images = [Image.open(frame) for frame in frame_paths[:ANIMATION_MAX_FRAMES]]
    images[0].save(
        str(gif_path),
        save_all=True,
        append_images=images[1:],
        duration=ANIMATION_FRAME_DURATION_MS,
        loop=0,
    )

    images_dir = frame_paths[0].parent
    for frame_path in frame_paths:
        frame_path.unlink(missing_ok=True)
    images_dir.rmdir()


def parse_args() -> argparse.Namespace:
    """コマンドライン引数を解析する。"""
    parser = argparse.ArgumentParser(
        description="Random Waypoint Mobility シミュレーションによりcontact_pattern.jsonを生成する。"
    )
    parser.add_argument("--n-time", type=int, default=DEFAULT_N_TIME, help="シミュレーション総ステップ数（=総秒数）")
    parser.add_argument("--min-speed", type=float, default=DEFAULT_MIN_SPEED, help="最小移動速度")
    parser.add_argument("--max-speed", type=float, default=DEFAULT_MAX_SPEED, help="最大移動速度")
    parser.add_argument("--radio-range", type=int, default=DEFAULT_RADIO_RANGE, help="無線到達距離")
    parser.add_argument("--area-size", type=int, default=DEFAULT_AREA_SIZE, help="正方形エリアの一辺")
    parser.add_argument("--pose-time", type=int, default=DEFAULT_POSE_TIME, help="ウェイポイント到達後の静止時間")
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED, help="乱数シード")
    parser.add_argument("--animation", action="store_true", help="ノード移動のGIFアニメーションも生成する")
    return parser.parse_args()


def main() -> None:
    """メインエントリポイント。"""
    args = parse_args()

    phase("Random Waypoint Mobility シミュレーション")
    n_node = count_nodes_from_hosts()
    info(f"{n_node} nodes from hosts.txt")

    rng = random.Random(args.seed)

    output_dir = get_base_dir() / "data" / OUTPUT_SUBDIR
    output_dir.mkdir(parents=True, exist_ok=True)
    filename_stem = build_output_filename(
        n_node, args.area_size, args.radio_range, args.pose_time, args.seed
    )

    frame_paths: list[Path] = []
    images_dir = output_dir / "images"

    def on_step(t: int, locations: list[tuple[float, float]]) -> None:
        if args.animation and t < ANIMATION_MAX_FRAMES:
            images_dir.mkdir(parents=True, exist_ok=True)
            xs, ys = zip(*locations)
            title = f"{filename_stem} | Epoch: {t}"
            snapshot(xs, ys, t, args.area_size, args.radio_range, images_dir, title)
            frame_paths.append(images_dir / f"node_location_{t:04d}.png")

    contact_list = simulate_rwp(
        n_time=args.n_time,
        n_node=n_node,
        min_speed=args.min_speed,
        max_speed=args.max_speed,
        radio_range=args.radio_range,
        area_size=args.area_size,
        pose_time=args.pose_time,
        rng=rng,
        on_step=on_step if args.animation else None,
    )

    phase("接触区間の検出")
    intervals = extract_contact_intervals(contact_list, n_node)
    ok(f"{len(intervals)} contact intervals detected")

    phase("イベント形式への変換")
    events = build_contact_events(intervals)

    isolated = find_isolated_nodes(events, n_node)
    if isolated:
        fail(f"孤立ノード検出（一度も接触が発生しなかった）: {sorted(isolated)}")

    output_data = to_json_serializable(events)

    phase("出力")
    json_path = output_dir / f"{filename_stem}.json"
    json_path.write_text(json.dumps(output_data, ensure_ascii=False, indent=2))
    ok(f"{json_path}")

    if args.animation:
        phase("アニメーション生成")
        generate_animation(frame_paths, output_dir, filename_stem)
        ok(f"{output_dir / (filename_stem + '.gif')}")

    if isolated:
        # 生成物は確認用に残した上で，CI・自動実行フローが見落とさないよう
        # 非ゼロで終了する（server.pyの_collect_all_peers()が孤立ノードを
        # 期待クライアント数に含めないため，そのノードはP2P通信に一切参加できない）
        sys.exit(1)


if __name__ == "__main__":
    main()
