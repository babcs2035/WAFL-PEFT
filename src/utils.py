"""WAFL-PEFT共通ユーティリティ。

設定ファイルからのパス解決を一元管理する。
"""

import json
import os
from datetime import datetime, timedelta, timezone
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


def get_experiment_dir() -> Path:
    """実験固有の results ディレクトリを返す。

    EXPERIMENT_DIR 環境変数が設定されていればそれを優先し、
    なければ results/ 下に "{experiment_name}_{timestamp}" を生成する。
    """
    exp_dir = os.environ.get("EXPERIMENT_DIR")
    if exp_dir:
        return Path(exp_dir)

    exp_name = get_experiment_name()
    timestamp = datetime.now(timezone(timedelta(hours=9))).strftime('%Y%m%dT%H%M%S')
    return get_base_dir() / "results" / f"{exp_name}_{timestamp}"


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
