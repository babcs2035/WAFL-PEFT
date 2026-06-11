"""WAFL-PEFT共通ユーティリティ。

設定ファイルからのパス解決を一元管理する。
"""

import json
import os
from pathlib import Path

# settings.jsonの検索パスリスト
_SETTINGS_CACHE: dict[str, dict] = {}


def _find_settings() -> Path:
    """settings.jsonを現在地から探索。"""
    candidates = [
        Path(__file__).parent.parent / "config" / "settings.json",
        Path.cwd() / "config" / "settings.json",
        Path.home() / "workspace" / "ktakahashi" / "WAFL-PEFT" / "config" / "settings.json",
    ]
    for p in candidates:
        if p.exists():
            return p
    raise FileNotFoundError("settings.json not found in any search path")


def load_config() -> dict:
    """settings.jsonを読み込み、キャッシュを返す。"""
    key = str(_find_settings())
    if key not in _SETTINGS_CACHE:
        with open(_find_settings()) as f:
            _SETTINGS_CACHE[key] = json.load(f)
    return _SETTINGS_CACHE[key]


def get_base_dir() -> Path:
    """プロジェクトルートディレクトリを返す。"""
    return Path.cwd()


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
