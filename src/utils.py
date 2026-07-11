"""WAFL-PEFT共通ユーティリティ。

設定ファイルからのパス解決を一元管理する。
"""

import json
from pathlib import Path
from typing import Any

# settings.jsonの検索パスリスト
_SETTINGS_CACHE: dict[str, dict] = {}


def _find_settings() -> Path:
    """settings.jsonを現在地から探索。"""
    candidates = [
        Path(__file__).parent.parent / "config" / "settings.json",
        Path.cwd() / "config" / "settings.json",
    ]
    for p in candidates:
        if p.exists():
            return p
    raise FileNotFoundError("settings.json not found in any search path")


def load_config() -> dict:
    """settings.jsonを読み込み、キャッシュを返す。

    戻り値はカテゴリネスト構造（例: config["training"]["learning_rate"]）。
    後方互換のため、扁平キーでもアクセス可能なラッパーを返す。
    """
    key = str(_find_settings())
    if key not in _SETTINGS_CACHE:
        with open(_find_settings()) as f:
            _SETTINGS_CACHE[key] = json.load(f)
    return _SETTINGS_CACHE[key]


def _get(category: str, key: str, default: Any = None) -> Any:
    """settings.json からカテゴリ内の値を取得。

    例: _get("training", "learning_rate") → config["training"]["learning_rate"]
    """
    config = load_config()
    return config.get(category, {}).get(key, default)


def _get_str(category: str, key: str, default: str = "") -> str:
    """settings.json から文字列値を取得。"""
    return str(_get(category, key, default))


def _get_int(category: str, key: str, default: int = 0) -> int:
    """settings.json から整数値を取得。"""
    return int(_get(category, key, default))


def _get_float(category: str, key: str, default: float = 0.0) -> float:
    """settings.json から浮動小数点値を取得。"""
    return float(_get(category, key, default))


def get_base_dir() -> Path:
    """プロジェクトルートディレクトリを返す。"""
    return Path.cwd()


def get_experiment_name() -> str:
    """settings.json から実験名を取得。デフォルトは "default"。"""
    return _get_str("experiment", "experiment_name", "default")


def get_latest_experiment_dir() -> Path | None:
    """results/ 下で最後に作成された実験ディレクトリを返す（なければ None）。

    実験ディレクトリ名（"{experiment_name}_{timestamp}"）は server.py が
    全クライアントのready確認後・実験開始時に一度だけ生成し、その場で
    results/ 直下に空ディレクトリとして作成する（唯一のタイムスタンプ発行元）。
    そのため、最終更新時刻が最も新しいディレクトリが直近の実験に対応する。
    """
    results_dir = get_base_dir() / "results"
    if not results_dir.exists():
        return None
    candidates = [d for d in results_dir.iterdir() if d.is_dir() and not d.name.startswith(".")]
    if not candidates:
        return None
    return max(candidates, key=lambda d: d.stat().st_mtime)


def get_data_dir() -> Path:
    """データセットディレクトリを返す。"""
    return get_base_dir() / "data"


def get_log_dir() -> Path:
    """ログディレクトリを返す。"""
    return get_base_dir() / "logs"


def get_output_dir() -> Path:
    """分析出力ディレクトリを返す。"""
    return get_base_dir() / "output"


def get_cache_dir() -> Path:
    """ローカルキャッシュディレクトリを返す。"""
    return get_base_dir() / "cache"


def get_hosts_path() -> Path:
    """hosts.txtのパスを返す。"""
    return get_base_dir() / "config" / "hosts.txt"
