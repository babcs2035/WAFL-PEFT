"""WAFL-PEFT共通ログ出力ユーティリティ。

外部依存なしで、setup/deploy スクリプトの進捗表示を統一する。
タグ形式（[OK] [FAIL] [SKIP] [INFO]）で構造化出力する。
"""

import sys


def phase(title: str) -> None:
    """フェーズ見出しを出力。"""
    print(f"\n{'=' * 50}")
    print(f"  {title}")
    print(f"{'=' * 50}")
    sys.stdout.flush()


def ok(msg: str) -> None:
    """成功ステータスを出力。"""
    print(f"  [OK] {msg}")
    sys.stdout.flush()


def fail(msg: str) -> None:
    """失敗ステータスを出力。"""
    print(f"  [FAIL] {msg}")
    sys.stdout.flush()


def skip(msg: str) -> None:
    """スキップステータスを出力。"""
    print(f"  [SKIP] {msg}")
    sys.stdout.flush()


def info(msg: str) -> None:
    """補足情報を出力。"""
    print(f"  [INFO] {msg}")
    sys.stdout.flush()


def dot(peer_id: int, label: str = "") -> None:
    """並列処理の進捗を出力。完了した peer ごとに 1 回呼ぶ。"""
    suffix = f" peer={peer_id}" if label == "" else f" {label}"
    print(f"[peer={peer_id}]", end="", flush=True)


def summary(label: str, ok_count: int, fail_count: int, skip_count: int = 0) -> None:
    """フェーズ完了のサマリーを出力。"""
    parts = [f"{ok_count} OK", f"{fail_count} FAIL"]
    if skip_count > 0:
        parts.append(f"{skip_count} SKIP")
    print(f"\n  [{label}] {', '.join(parts)}")
    sys.stdout.flush()
