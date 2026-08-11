"""AIradio 共通ユーティリティ: 設定読み込み・パス解決・状態永続化・ログ。

AIjukebox の scripts/common.py が原型。曲ライブラリの sqlite は使わないので
(ニュースは Gcrawler が置いた JSON がそのままソース) DB 関連は落とし、
代わりに除外リングを持ち回るための db/state.json を足している。

bgm_worker.py はこのモジュールを HeartMuLa 側の venv から import する。
標準ライブラリ以外に依存させないこと。
"""

from __future__ import annotations

import json
import os
import re
import tomllib
from datetime import datetime
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SETTINGS_PATH = PROJECT_ROOT / "config" / "settings.toml"
# git 管理外の上書き設定。settings.toml と同じ構造で一部だけ書けばよい。
LOCAL_SETTINGS_PATH = PROJECT_ROOT / "config" / "settings.local.toml"


def _deep_merge(base: dict, override: dict) -> dict:
    """override の値で base を再帰的に上書きする(テーブル単位では潰さない)。"""
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            _deep_merge(base[key], value)
        else:
            base[key] = value
    return base


def load_settings(path: Path | None = None) -> dict[str, Any]:
    """settings.toml を読み、settings.local.toml があれば上書きして返す。"""
    with open(path or SETTINGS_PATH, "rb") as f:
        settings = tomllib.load(f)

    if path is None and LOCAL_SETTINGS_PATH.exists():
        with open(LOCAL_SETTINGS_PATH, "rb") as f:
            _deep_merge(settings, tomllib.load(f))
    return settings


def resolve_path(value: str) -> Path:
    """設定中のパスを解決する。相対パスはプロジェクトルート基準。"""
    p = Path(value).expanduser()
    return p if p.is_absolute() else (PROJECT_ROOT / p)


# ---- 状態の永続化 --------------------------------------------------------
# 除外リングを再起動でも引き継ぐ (同じニュースを続けて読まないため)。
# 書き込みは一時ファイル経由。途中で落ちても壊れた JSON を残さない。


def load_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        # 壊れていても番組は止めない。空から作り直す
        return {}


def save_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


# ---- 生成テキストのログ --------------------------------------------------


def append_script_log(log_path: Path, kind: str, text: str, **extra: Any) -> None:
    """生成した原稿を JSONL で追記する。recent_scripts の復元元になる。"""
    log_path.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "ts": datetime.now().isoformat(timespec="seconds"),
        "kind": kind,
        "text": text,
        **extra,
    }
    with open(log_path, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def recent_scripts(log_path: Path, n: int, kind: str = "news") -> list[str]:
    """直近 n 件の原稿本文を古い順で返す。ログが無ければ空リスト。"""
    if not log_path.exists():
        return []
    texts: list[str] = []
    with open(log_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue  # 壊れた行があっても復元を諦めない
            if record.get("kind") == kind and record.get("text"):
                texts.append(record["text"])
    return texts[-n:]


# ---- テキスト整形 --------------------------------------------------------

# 音声合成が「ほし」「おんぷ」などと読んでしまう記号。「」は無音で読まれるので残す。
_STRIP_CHARS = re.compile(
    r"[*#`~_<>\[\]{}|\\/^=+☆★♪♬♩♂♀※→←↑↓○●◎△▲▽▼□■◆◇『』【】〈〉《》]"
)
# 「コメント:」「原稿:」のような前置き
_PREAMBLE = re.compile(r"^\s*(コメント|原稿|紹介文?|出力|回答|本文)\s*[:：]\s*")
# 完結した文の終わり
_SENTENCE_END = ("。", "!", "?", "！", "？")


def trim_to_sentence(text: str) -> str:
    """末尾の未完成な文を落とす。無理なら「。」で締める。"""
    if text.endswith(_SENTENCE_END):
        return text
    cut = max(text.rfind(c) for c in _SENTENCE_END)
    if cut >= 0:
        return text[: cut + 1]
    return text.rstrip("、 ") + "。"


def clean_for_tts(text: str, max_chars: int) -> str:
    """LLM の生成結果を TTS に流せる形に整える。

    記号を落とし、max_chars を超える場合は文末で切る。max_tokens 到達で文の
    途中で切れて返ることがあるので、末尾が句点でなければそこも落とす
    (読み上げが尻切れになるのを防ぐ)。
    """
    text = _PREAMBLE.sub("", text.strip())
    text = _STRIP_CHARS.sub("", text)
    text = re.sub(r"\s+", " ", text).strip()
    text = text.strip("　 「」\"'")

    if len(text) > max_chars:
        text = text[:max_chars]
    return trim_to_sentence(text)
