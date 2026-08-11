"""HeartMuLa で BGM を 1 曲生成する薄いラッパー。

このモジュールだけは AIradio の venv ではなく **HeartMuLa 側の venv**
(~/heartlib/.venv、torch は gfx1151 ネイティブの ROCm ビルド) で動く。
そのため import してよいのは標準ライブラリと heartlib / torch だけ。
httpx や aiohttp をここに持ち込まないこと。

  ~/heartlib/.venv/bin/python scripts/bgm_gen.py --out /tmp/a.wav

モデルのロードは 20GB 超あって数十秒かかるので、bgm_worker は
BgmGenerator を 1 個だけ作って使い回す (lazy_load=False で常駐させる)。
"""

from __future__ import annotations

import argparse
import contextlib
import time
import wave
from pathlib import Path

import torch

from common import load_settings, resolve_path

# HeartMuLa は歌モデルなので歌詞が必須。BGM は歌わせたくないため、
# 歌詞の代わりにこのタグだけを渡してインストゥルメンタルにする。
INSTRUMENTAL_LYRICS = "[instrumental]"

# 出力は codec の都合で必ず 48kHz。ラウドネス正規化のときに 44.1kHz へ落とす。
HEARTMULA_SAMPLE_RATE = 48000


def _write_wav(path: str, wav: torch.Tensor, sample_rate: int) -> None:
    """float の [チャンネル, サンプル] テンソルを 16bit PCM の wav にする。"""
    data = wav.detach().to(torch.float32).cpu()
    if data.ndim == 1:
        data = data.unsqueeze(0)
    # 生成物はごく稀に ±1 を超える。切り捨てないと巻き返してノイズになる
    pcm = (data.clamp(-1.0, 1.0) * 32767.0).to(torch.int16)
    with contextlib.closing(wave.open(path, "wb")) as w:
        w.setnchannels(pcm.shape[0])
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        w.writeframes(pcm.t().contiguous().numpy().tobytes())


def _patch_torchaudio_save() -> None:
    """torchaudio.save を自前の wav ライタに差し替える。

    torchaudio 2.9 は save を torchcodec 経由に一本化しており、torchcodec が
    入っていない環境では HeartMuLa の postprocess が ImportError で落ちる
    (gfx1151 向けの torchcodec ホイールは無い)。欲しいのは 48kHz の wav を
    1 本吐くことだけなので、標準ライブラリで書く方が依存が減る。

    heartlib 側には手を入れない。あちらは上流の取り込みを続けたいので、
    環境差の吸収は AIradio 側で閉じる。
    """
    import torchaudio

    try:
        import torchcodec  # noqa: F401
    except ImportError:
        torchaudio.save = lambda path, wav, sample_rate, **_: _write_wav(
            str(path), wav, sample_rate
        )


class BgmGenerator:
    """常駐させて使う想定の生成器。1 プロセス 1 インスタンス。"""

    def __init__(self, conf: dict):
        from heartlib import HeartMuLaGenPipeline

        _patch_torchaudio_save()
        self.conf = conf
        model_path = str(resolve_path(conf["model_path"]))
        device = torch.device("cuda" if torch.cuda.is_available() else "cpu")

        t0 = time.monotonic()
        # lazy_load=False。毎回ロードし直すと 1 曲あたり数十秒を無駄にする。
        # VRAM は llama-server と取り合うので、start_all.sh の起動順で
        # llama-server を先に上げてから常駐させること。
        self.pipe = HeartMuLaGenPipeline.from_pretrained(
            model_path,
            device={"mula": device, "codec": device},
            dtype={"mula": torch.bfloat16, "codec": torch.float32},
            version=conf.get("version", "3B"),
            lazy_load=False,
        )
        self.load_sec = time.monotonic() - t0

    def generate(self, tags: str, out_path: Path, duration_sec: float) -> float:
        """1 曲生成して out_path (wav) に書く。かかった秒数を返す。

        書き切ってから差し替える。生成途中のファイルを bgm_worker や
        news_service が拾って再生してしまうのを防ぐため。
        """
        out_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = out_path.with_suffix(out_path.suffix + ".tmp.wav")

        t0 = time.monotonic()
        with torch.no_grad():
            self.pipe(
                {"lyrics": INSTRUMENTAL_LYRICS, "tags": tags},
                max_audio_length_ms=int(duration_sec * 1000),
                save_path=str(tmp),
                topk=int(self.conf.get("topk", 50)),
                temperature=float(self.conf.get("temperature", 1.0)),
                cfg_scale=float(self.conf.get("cfg_scale", 1.5)),
            )
        elapsed = time.monotonic() - t0
        tmp.replace(out_path)
        return elapsed


def main() -> int:
    ap = argparse.ArgumentParser(description="HeartMuLa で BGM を 1 曲生成する")
    ap.add_argument("--out", default="bgm.wav", help="出力 wav パス")
    ap.add_argument("--tags", help="生成タグ (省略時は settings.toml の prompts[0])")
    ap.add_argument("--duration", type=float, help="秒数 (省略時は settings.toml)")
    ap.add_argument("--repeat", type=int, default=1, help="連続生成する曲数")
    args = ap.parse_args()

    conf = load_settings()["bgm"]
    tags = args.tags or conf["prompts"][0]
    duration = args.duration or conf["duration_sec"]

    gen = BgmGenerator(conf)
    print(f"モデルロード: {gen.load_sec:.1f} 秒", flush=True)

    out = Path(args.out)
    for i in range(args.repeat):
        path = out if args.repeat == 1 else out.with_stem(f"{out.stem}_{i:02d}")
        elapsed = gen.generate(tags, path, duration)
        print(
            f"[{i + 1}/{args.repeat}] {path} "
            f"{elapsed:.1f} 秒 (実時間比 {elapsed / duration:.2f}x)",
            flush=True,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
