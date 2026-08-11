#!/usr/bin/env python3
"""VOICEVOX 音声合成ラッパー。

  uv run --no-sync scripts/voicevox_synth.py --list-speakers
  uv run --no-sync scripts/voicevox_synth.py --text "こんばんは" --out /tmp/a.wav

AIjukebox からの流用。違いは 2 点。

- 合成した wav をその場で loudnorm にかけてから保存する (音楽と混ぜるため)
- キャッシュのキーが曲ではなく「テキストのハッシュ」。相槌のように同じ文を
  何度も使うものは自然に使い回される

speaker_id はハードコードせず、起動時に /speakers から名前で解決する
(VOICEVOX のバージョン間で ID が変わるため)。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

import httpx

from audio import normalize_bytes, wav_duration
from common import load_settings

TIMEOUT = 60.0

# 解決済み speaker_id のプロセス内キャッシュ。(name, style) -> id
_speaker_cache: dict[tuple[str, str], int] = {}


def text_hash(text: str) -> str:
    """wav キャッシュのキー。同じ文なら合成し直さない。"""
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:16]


def list_speakers(base_url: str) -> list[dict]:
    r = httpx.get(f"{base_url}/speakers", timeout=TIMEOUT)
    r.raise_for_status()
    return r.json()


def resolve_speaker_id(base_url: str, speaker_name: str, style_name: str) -> int:
    """話者名 + スタイル名から speaker_id を引く。

    ID 直書きだと VOICEVOX の更新で別の話者を喋らせてしまうため必ずここを通す。
    """
    key = (speaker_name, style_name)
    if key in _speaker_cache:
        return _speaker_cache[key]

    speakers = list_speakers(base_url)
    for sp in speakers:
        if sp.get("name") != speaker_name:
            continue
        for style in sp.get("styles", []):
            if style.get("name") == style_name:
                _speaker_cache[key] = int(style["id"])
                return _speaker_cache[key]

    available = ", ".join(
        f"{sp.get('name')}/{st.get('name')}"
        for sp in speakers
        for st in sp.get("styles", [])
    )
    raise LookupError(
        f"話者が見つかりません: {speaker_name} / {style_name}\n利用可能: {available}"
    )


def synthesize(
    text: str, speaker_id: int, base_url: str, volume_scale: float = 1.0
) -> tuple[bytes, dict]:
    """テキストを合成して (wav バイト列, audio_query) を返す。

    audio_query も返すのは、表示系のリップシンクが accent_phrases から viseme を
    組み立てるため。キャッシュヒット時に再問い合わせしなくて済む。
    """
    with httpx.Client(timeout=TIMEOUT) as client:
        r = client.post(
            f"{base_url}/audio_query", params={"text": text, "speaker": speaker_id}
        )
        r.raise_for_status()
        query = r.json()
        query["volumeScale"] = volume_scale

        r = client.post(
            f"{base_url}/synthesis",
            params={"speaker": speaker_id},
            json=query,
            headers={"Content-Type": "application/json"},
        )
        r.raise_for_status()
        return r.content, query


def synth_to_file(
    text: str, wav_path: Path, settings: dict, *, force: bool = False
) -> tuple[Path, dict]:
    """text を wav_path に合成し、(wav パス, メタ) を返す。

    メタ (同名の .json) にはテキスト・話者・accent_phrases・長さを入れる。
    news_service はこの長さでトラックの終了時刻を予測し、accent_phrases から
    リップシンクの viseme を組む。wav だけあってもキャッシュヒット時に口が
    動かなくなるので、必ず両方を揃えて保存すること。
    """
    meta_path = wav_path.with_suffix(".json")
    if not force and wav_path.exists() and meta_path.exists():
        try:
            return wav_path, json.loads(meta_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass  # 壊れていれば作り直す

    vv = settings["voicevox"]
    speaker_id = resolve_speaker_id(
        vv["base_url"], vv["speaker_name"], vv["style_name"]
    )
    wav, query = synthesize(
        text, speaker_id, vv["base_url"], vv.get("volume_scale", 1.0)
    )
    # 音楽と交互に流すので、ここでラウドネスを揃えてから置く
    normalize_bytes(wav, wav_path, settings["loudness"])

    meta = {
        "text": text,
        "speaker_id": speaker_id,
        "accent_phrases": query.get("accent_phrases", []),
        "duration_sec": wav_duration(wav_path),
    }
    meta_path.write_text(
        json.dumps(meta, ensure_ascii=False), encoding="utf-8"
    )
    return wav_path, meta


def synth_to_cache(text: str, cache_dir: Path, settings: dict) -> tuple[Path, dict]:
    """テキストのハッシュをファイル名にして cache_dir に合成する。"""
    cache_dir.mkdir(parents=True, exist_ok=True)
    return synth_to_file(text, cache_dir / f"{text_hash(text)}.wav", settings)


def main() -> int:
    ap = argparse.ArgumentParser(description="VOICEVOX で音声合成する")
    ap.add_argument("--text", help="合成するテキスト('-' で標準入力)")
    ap.add_argument("--out", help="出力 wav パス")
    ap.add_argument("--list-speakers", action="store_true", help="話者一覧を表示")
    args = ap.parse_args()

    settings = load_settings()
    vv = settings["voicevox"]

    try:
        if args.list_speakers:
            for sp in list_speakers(vv["base_url"]):
                styles = ", ".join(
                    f"{st['name']}(id={st['id']})" for st in sp.get("styles", [])
                )
                print(f"{sp['name']}: {styles}")
            return 0

        if not args.text:
            ap.error("--text か --list-speakers が必要です")
        text = sys.stdin.read().strip() if args.text == "-" else args.text

        out = Path(args.out) if args.out else Path("speech.wav")
        path, meta = synth_to_file(text, out, settings, force=True)
        print(f"話者: {vv['speaker_name']} / {vv['style_name']}")
        print(f"出力: {path} ({meta['duration_sec']:.2f} 秒)")
    except httpx.HTTPError as e:
        print(f"VOICEVOX に接続できません ({vv['base_url']}): {e}", file=sys.stderr)
        return 1
    except LookupError as e:
        print(e, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
