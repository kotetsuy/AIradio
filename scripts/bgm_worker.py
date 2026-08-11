#!/usr/bin/env python3
"""BGM を常時生成してプールを埋め続けるワーカー。

  ~/heartlib/.venv/bin/python scripts/bgm_worker.py

news_service とは **独立プロセス**。通信もしない。契約は
「cache/bgm_pool/ に再生可能な wav が置いてある」ことだけ (TECHNICALJ.md §1)。
このワーカーが死んでも番組は FILLER_LOOP で回り続ける。

BGM はニュースと連動しない独立生成なので鮮度の概念がなく、プールは再起動を
またいで持ち越す。起動のたびに事前生成を待たなくてよい。

torch (ROCm) が要るので AIradio の venv ではなく HeartMuLa 側の venv で走る。
PYTHONPATH に scripts/ を通すこと (start_all.sh がやっている)。
"""

from __future__ import annotations

import argparse
import random
import signal
import sys
import time
from pathlib import Path

from audio import AudioError, normalize, wav_duration
from bgm_pool import USED_SUBDIR, pool_files, prompt_hash, sweep_stale
from common import load_settings, resolve_path


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def pick_prompt(conf: dict, counter: int) -> str:
    """prompt_mode に応じて 1 本選ぶ。既定は prompts[0] 固定。"""
    prompts = conf["prompts"]
    mode = conf.get("prompt_mode", "fixed")
    if mode == "random":
        return random.choice(prompts)
    if mode == "sequential":
        return prompts[counter % len(prompts)]
    return prompts[0]


class BgmWorker:
    def __init__(self, settings: dict):
        self.conf = settings["bgm"]
        self.loudness = settings["loudness"]
        self.pool_dir = resolve_path(self.conf["pool_dir"])
        self.pool_dir.mkdir(parents=True, exist_ok=True)
        (self.pool_dir / USED_SUBDIR).mkdir(exist_ok=True)

        self.duration = float(self.conf["duration_sec"])
        self.min_duration = float(self.conf.get("min_duration_sec", 0))
        self.target = int(self.conf["pool_target"])
        self.retry_wait = float(self.conf.get("retry_wait_sec", 30))
        self.counter = 0
        self.stopping = False
        # 直前の生成が「エラー」で落ちたか。短すぎて捨てただけなら間を置かずに
        # 作り直してよい (GPU は正常なので待つ意味がない)
        self.last_failed_hard = False

        valid = {prompt_hash(p, self.duration) for p in self.conf["prompts"]}
        removed = sweep_stale(self.pool_dir, valid)
        if removed:
            log(f"プロンプトが変わったため {removed} 曲を破棄しました")

        # モデルのロードは重いので掃除のあとに。torch はここで初めて読む
        from bgm_gen import BgmGenerator

        self.gen = BgmGenerator(self.conf)
        log(f"HeartMuLa ロード完了: {self.gen.load_sec:.1f} 秒")

    def stop(self, *_) -> None:
        # 生成の途中では抜けられない (1 曲ぶんは待つ)。次のループで止まる
        self.stopping = True
        log("停止要求を受けました。生成中の曲を書き終えたら終了します")

    def generate_one(self) -> Path | None:
        prompt = pick_prompt(self.conf, self.counter)
        self.counter += 1
        stamp = time.strftime("%Y%m%dT%H%M%S")
        name = f"bgm_{prompt_hash(prompt, self.duration)}_{stamp}"

        # 正規化前のファイルが pool_files に見えないよう .raw.wav を経由する
        raw = self.pool_dir / f"{name}.raw.wav"
        final = self.pool_dir / f"{name}.wav"
        self.last_failed_hard = False
        try:
            elapsed = self.gen.generate(prompt, raw, self.duration)
            # スピーチと交互に流すのでラウドネスを揃えてからプールに置く
            normalize(raw, final, self.loudness)
            made = wav_duration(final)
        except (RuntimeError, OSError, AudioError) as e:
            log(f"生成に失敗しました: {e}")
            self.last_failed_hard = True
            return None
        finally:
            raw.unlink(missing_ok=True)

        if made < self.min_duration:
            # audio_eos が早く出て曲になっていない。プールに入れずに作り直す
            log(f"短すぎるので破棄: {final.name} {made:.1f} 秒 (所要 {elapsed:.1f} 秒)")
            final.unlink(missing_ok=True)
            return None

        log(
            f"生成: {final.name} {made:.1f} 秒 "
            f"(所要 {elapsed:.1f} 秒 / 音1秒あたり {elapsed / made:.2f} 秒)"
        )
        return final

    def run(self) -> int:
        log(f"プール: {self.pool_dir} (目標 {self.target} 曲)")
        while not self.stopping:
            have = len(pool_files(self.pool_dir))
            if have >= self.target:
                # 満たしているときは短く寝て、news_service が pop したら
                # すぐ気づけるようにする (ファイル契約なので通知は来ない)
                time.sleep(2.0)
                continue

            log(f"プール {have}/{self.target} → 生成します")
            if self.generate_one() is None and self.last_failed_hard:
                # 連続で失敗して GPU を焼き続けないよう間を置く。
                # このあいだ番組は FILLER_LOOP で回る
                time.sleep(self.retry_wait)
        return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="BGM 生成ワーカー")
    ap.add_argument("--once", action="store_true", help="1曲だけ生成して終了する")
    ap.add_argument("--status", action="store_true", help="プールの状態だけ表示する")
    args = ap.parse_args()

    settings = load_settings()

    if args.status:
        pool_dir = resolve_path(settings["bgm"]["pool_dir"])
        files = pool_files(pool_dir)
        used = len(list((pool_dir / USED_SUBDIR).glob("*.wav")))
        print(
            f"{pool_dir}: {len(files)} 曲 "
            f"(目標 {settings['bgm']['pool_target']}) / 使用済み {used} 曲"
        )
        for p in files:
            print(f"  {p.name}  {wav_duration(p):.1f} 秒")
        return 0

    worker = BgmWorker(settings)
    signal.signal(signal.SIGINT, worker.stop)
    signal.signal(signal.SIGTERM, worker.stop)

    if args.once:
        return 0 if worker.generate_one() else 1
    return worker.run()


if __name__ == "__main__":
    sys.exit(main())
