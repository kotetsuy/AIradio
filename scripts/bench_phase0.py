"""Phase 0: 同時負荷ベンチ。

「HeartMuLa 30 秒生成 ≒ 60 秒」は単独実行の実測値だが、実運用では同じ GPU
(gfx1151) の上で llama-server の原稿生成が同時に走る。番組の収支
(1 サイクル 60 秒で BGM を 1 曲消費) が崩れないかを確かめるためのベンチ。

  ~/heartlib/.venv/bin/python scripts/bench_phase0.py --clips 2

1. 単独で BGM を生成して基準を取る
2. llama-server に原稿生成を投げ続けながら同じことをして比較する

llama-server が上がっていない場合は 2 をスキップする (基準だけ出す)。
結果は logs/bench_phase0.json に残す。
"""

from __future__ import annotations

import argparse
import json
import statistics
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

from audio import wav_duration
from bgm_gen import BgmGenerator
from common import PROJECT_ROOT, load_settings

# 実運用の原稿生成と同じくらいの長さを吐かせる (30 秒ぶん ≒ 220 字)
BENCH_PROMPT = (
    "次のニュースを、ラジオDJ が 30 秒で読み上げる日本語の原稿にしてください。"
    "220 文字以内、記号は使わず、必ず「。」で終わる文にしてください。\n\n"
    "タイトル: 新型の家庭用ロボット掃除機が発表される\n"
    "要約: 段差を乗り越える脚部と自己診断機能を備えた新型が発表された。"
    "従来機より静音性が向上し、集じん性能も改善されているという。"
)


def llm_once(base_url: str, max_tokens: int, timeout: float = 180.0) -> float:
    """原稿生成を 1 回投げて所要秒を返す。失敗したら -1。"""
    payload = json.dumps(
        {
            "messages": [{"role": "user", "content": BENCH_PROMPT}],
            "temperature": 0.8,
            "max_tokens": max_tokens,
            "stream": False,
            # Qwen3 系は既定で thinking を吐き、reasoning_content に
            # max_tokens を食われて content が空で返る (AIjukebox 知見)
            "chat_template_kwargs": {"enable_thinking": False},
        }
    ).encode()
    req = urllib.request.Request(
        f"{base_url}/v1/chat/completions",
        data=payload,
        headers={"Content-Type": "application/json"},
    )
    t0 = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            r.read()
    except (urllib.error.URLError, TimeoutError, OSError):
        return -1.0
    return time.monotonic() - t0


def llama_alive(base_url: str) -> bool:
    try:
        with urllib.request.urlopen(f"{base_url}/health", timeout=3) as r:
            return r.status == 200
    except (urllib.error.URLError, TimeoutError, OSError):
        return False


class LlmLoad:
    """llama-server に原稿生成を投げ続ける負荷スレッド。"""

    def __init__(self, conf: dict):
        self.conf = conf
        self.stop = threading.Event()
        self.latencies: list[float] = []
        self.failures = 0
        self.thread = threading.Thread(target=self._run, daemon=True)

    def _run(self) -> None:
        while not self.stop.is_set():
            sec = llm_once(self.conf["base_url"], self.conf["max_tokens"])
            if sec < 0:
                self.failures += 1
                # サーバが詰まっているときに叩き続けても意味がないので少し待つ
                self.stop.wait(2.0)
            else:
                self.latencies.append(sec)

    def __enter__(self) -> LlmLoad:
        self.thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self.stop.set()
        self.thread.join(timeout=30)


def run_clips(gen: BgmGenerator, tags: str, duration: float, n: int, out: Path,
              label: str) -> list[tuple[float, float]]:
    """(所要秒, 実際に生成された音の秒数) を n 本ぶん返す。

    HeartMuLa は audio_eos が出た時点で打ち切るので、max_audio_length_ms は
    上限でしかない。指定尺で割って比を出すと、途中で終わった曲が「速い」と
    誤って記録される。必ず出来上がった wav の長さで割ること。
    """
    result: list[tuple[float, float]] = []
    for i in range(n):
        path = out / f"{label}_{i:02d}.wav"
        elapsed = gen.generate(tags, path, duration)
        made = wav_duration(path)
        result.append((elapsed, made))
        print(
            f"  [{label} {i + 1}/{n}] {elapsed:.1f} 秒で {made:.1f} 秒ぶん "
            f"(音1秒あたり {elapsed / made:.2f} 秒)",
            flush=True,
        )
    return result


def main() -> int:
    ap = argparse.ArgumentParser(description="Phase 0 同時負荷ベンチ")
    ap.add_argument("--clips", type=int, default=2, help="各条件で生成する曲数")
    ap.add_argument("--duration", type=float, help="BGM の秒数 (既定は settings)")
    ap.add_argument("--out", default="cache/bench", help="生成物の置き場")
    args = ap.parse_args()

    settings = load_settings()
    bgm, llm = settings["bgm"], settings["llm"]
    duration = args.duration or bgm["duration_sec"]
    tags = bgm["prompts"][0]

    out = PROJECT_ROOT / args.out
    out.mkdir(parents=True, exist_ok=True)

    print(f"BGM: {duration:.0f} 秒 / タグ: {tags}", flush=True)
    gen = BgmGenerator(bgm)
    print(f"モデルロード: {gen.load_sec:.1f} 秒\n", flush=True)

    print("== 単独 ==", flush=True)
    solo = run_clips(gen, tags, duration, args.clips, out, "solo")

    loaded: list[tuple[float, float]] = []
    llm_stats: dict = {}
    if not llama_alive(llm["base_url"]):
        print(
            f"\n!! llama-server ({llm['base_url']}) が応答しません。"
            "同時負荷の計測はスキップします",
            flush=True,
        )
    else:
        print("\n== llama-server 同時負荷 ==", flush=True)
        with LlmLoad(llm) as load:
            loaded = run_clips(gen, tags, duration, args.clips, out, "loaded")
        llm_stats = {
            "requests": len(load.latencies),
            "failures": load.failures,
            "latency_median_sec": (
                round(statistics.median(load.latencies), 2) if load.latencies else None
            ),
        }
        print(
            f"  原稿生成: {llm_stats['requests']} 回 / 失敗 {llm_stats['failures']} 回"
            f" / 中央値 {llm_stats['latency_median_sec']} 秒",
            flush=True,
        )

    def summary(runs: list[tuple[float, float]]) -> dict:
        if not runs:
            return {}
        costs = [elapsed / made for elapsed, made in runs]
        return {
            "n": len(runs),
            "elapsed_median_sec": round(statistics.median(e for e, _ in runs), 1),
            "audio_median_sec": round(statistics.median(m for _, m in runs), 1),
            # 音 1 秒を作るのに何秒かかるか。尺を変えたときの見積りはこれを使う
            "cost_per_audio_sec_median": round(statistics.median(costs), 2),
            "cost_per_audio_sec_max": round(max(costs), 2),
        }

    report = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "requested_duration_sec": duration,
        "tags": tags,
        "model_load_sec": round(gen.load_sec, 1),
        "solo": summary(solo),
        "with_llm_load": summary(loaded),
        "llm": llm_stats,
    }

    # 収支の判定。1 サイクル (スピーチ + BGM) で 1 曲消費するので、
    #   生成時間 = cost × BGM尺  ≤  スピーチ尺 + BGM尺
    # を満たさないとプールが枯れて FILLER_LOOP が常態化する。
    # 満たせない場合に必要な再利用回数 (同じ曲を何サイクル使い回すか) も出す。
    worst = loaded or solo
    if worst:
        cost = statistics.median(e / m for e, m in worst)
        speech = float(settings["script"]["max_wav_sec"])
        cycle = speech + duration
        need = cost * duration
        reuse = -(-need // cycle)  # 切り上げ
        report["verdict"] = "ok" if need <= cycle else "pool_underrun"
        report["cycle_sec"] = cycle
        report["gen_sec_per_clip"] = round(need, 1)
        report["required_reuse_count"] = int(reuse)
        # 尺を変えて収支を合わせる場合の上限。cost·D ≤ S + D を D について解く
        report["sustainable_duration_sec"] = (
            round(speech / (cost - 1), 1) if cost > 1 else None
        )
        print(
            f"\n判定: {report['verdict']}\n"
            f"  1曲の生成 {need:.0f} 秒 / 1サイクル {cycle:.0f} 秒"
            f" (スピーチ {speech:.0f} + BGM {duration:.0f})\n"
            f"  収支を合わせるには: 同じ曲を {int(reuse)} 回使い回す、"
            f"または BGM 尺を {report['sustainable_duration_sec']} 秒以下にする",
            flush=True,
        )

    report_path = PROJECT_ROOT / "logs" / "bench_phase0.json"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"レポート: {report_path}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
