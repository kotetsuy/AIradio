"""wav の正規化と長さ取得。

VOICEVOX の音声と HeartMuLa の楽曲はラウドネスもサンプルレートも揃っていない
(前者は 24kHz のトーク、後者は 48kHz の音楽)。そのまま交互に流すと曲の頭で
音量が跳ねる。キューに積む前に **ファイル段階で** EBU R128 に揃えておく。
Liquidsoap 側の normalize でもできるが、ファイルにしておく方が試聴と
デバッグが楽なので TECHNICALJ.md §4 の方針どおりこちらを採る。

  uv run --no-sync scripts/audio.py in.wav out.wav
"""

from __future__ import annotations

import argparse
import contextlib
import subprocess
import sys
import wave
from pathlib import Path


class AudioError(RuntimeError):
    pass


def wav_duration(path: Path) -> float:
    """wav の長さ(秒)。正規化後のファイルは必ず PCM なので wave で読める。"""
    try:
        with contextlib.closing(wave.open(str(path), "rb")) as w:
            rate = w.getframerate()
            return w.getnframes() / rate if rate else 0.0
    except (OSError, wave.Error) as e:
        raise AudioError(f"wav を読めません: {path} ({e})") from e


def normalize(src: Path, dst: Path, conf: dict) -> Path:
    """loudnorm をかけて dst に書く。dst を返す。

    1 パスの loudnorm (linear=false) を使う。2 パス測ってからの方が精度は
    出るが、番組の進行を待たせてまで詰める精度ではない。サンプルレートと
    ビット深度もここで揃える (44.1kHz / 16bit)。

    書き切ってから差し替える。生成途中のファイルを news_service が拾って
    push してしまうのを防ぐため。
    """
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_suffix(dst.suffix + ".tmp.wav")
    loudnorm = (
        f"loudnorm=I={conf.get('target_lufs', -16.0)}"
        f":LRA={conf.get('target_lra', 11.0)}"
        f":TP={conf.get('target_tp', -1.5)}"
    )
    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        "-i", str(src),
        "-af", loudnorm,
        "-ar", str(conf.get("sample_rate", 44100)),
        "-ac", "2",
        "-c:a", "pcm_s16le",
        str(tmp),
    ]
    try:
        subprocess.run(cmd, check=True, capture_output=True, text=True)
    except FileNotFoundError as e:
        raise AudioError("ffmpeg がありません (apt install ffmpeg)") from e
    except subprocess.CalledProcessError as e:
        tmp.unlink(missing_ok=True)
        raise AudioError(f"loudnorm に失敗しました: {src}\n{e.stderr.strip()}") from e

    tmp.replace(dst)
    return dst


def normalize_bytes(data: bytes, dst: Path, conf: dict) -> Path:
    """メモリ上の wav (VOICEVOX の応答) をそのまま正規化して書き出す。"""
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_suffix(dst.suffix + ".src.wav")
    tmp.write_bytes(data)
    try:
        return normalize(tmp, dst, conf)
    finally:
        tmp.unlink(missing_ok=True)


def main() -> int:
    ap = argparse.ArgumentParser(description="wav を EBU R128 で正規化する")
    ap.add_argument("src")
    ap.add_argument("dst")
    args = ap.parse_args()

    from common import load_settings

    try:
        out = normalize(Path(args.src), Path(args.dst), load_settings()["loudness"])
    except AudioError as e:
        print(e, file=sys.stderr)
        return 1
    print(f"{out} ({wav_duration(out):.2f} 秒)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
