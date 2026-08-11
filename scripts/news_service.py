#!/usr/bin/env python3
"""番組進行の司令塔。ニュース選定・原稿生成・キュー投入・表示系配信。

  uv run --no-sync scripts/news_service.py

AIjukebox の program_service.py の後継。違いは 3 点。

- 曲ではなく「ニュース原稿 → BGM」を交互に流す (BGM は Gcrawler/LLM とは
  独立に bgm_worker がプールへ補充する)
- Liquidsoap のキューは 1 本。押し込んだ順がそのまま番組の進行になる
- 進行の起点を radio.liq からの HTTP コールバック (/api/track_event) にした。
  「今どのファイルが鳴り始めたか」が分からないと、字幕・背景が実際の音と
  ずれるため

状態機械 (TECHNICALJ.md §3):

  INTRO ─→ SPEAKING ─┬→ MUSIC_PLAYING ─→ SPEAKING …
                     └→ FILLER_LOOP ─→(BGMが届いたら)→ MUSIC_PLAYING

FILLER_LOOP は異常系ではなく「BGM プールが空のときの正常な待ち方」。
生成が落ちても番組は止めない (TECHNICALJ.md §8)。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import random
import sys
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

import aiohttp
import httpx
from aiohttp import web

import dj_script
import voicevox_synth
from audio import AudioError, wav_duration
from bgm_pool import take_bgm
from common import (
    PROJECT_ROOT,
    append_script_log,
    clean_for_tts,
    load_settings,
    recent_scripts,
    resolve_path,
)
from liquidsoap_client import LiquidsoapClient, LiquidsoapError
from news import Article, NewsSelector
from visemes import mora_to_visemes

WEB_DIR = PROJECT_ROOT / "web"
VRM_DIR = PROJECT_ROOT / "vroid"

# 表示系に見せる状態名。実際の進行はキューの中身で決まるので、これは
# 「いま鳴っているものの種類」を言い換えただけの表示用ラベル。
PHASE_OF_KIND = {
    "intro": "INTRO",
    "news": "SPEAKING",
    "notice": "SPEAKING",
    "filler": "FILLER_LOOP",
    "music": "MUSIC_PLAYING",
}

# キューの残りを見に行く間隔。prefetch_lead_sec (既定 3 秒) より十分短ければ
# よく、短くしても Liquidsoap への telnet が増えるだけ
POLL_INTERVAL = 1.0
# 原稿が長すぎたときに縮める割合の余裕 (狙いより少し短くしないと戻ってくる)
SHRINK_MARGIN = 0.9


@dataclass
class Item:
    """キューに積む 1 トラック。"""

    path: Path
    kind: str  # intro / news / notice / filler / music
    duration: float
    text: str = ""
    article: Article | None = None
    # VOICEVOX の accent_phrases (リップシンク用)。music には無い
    meta: dict = field(default_factory=dict)


class NewsService:
    def __init__(self, settings: dict):
        self.settings = settings
        self.log_path = resolve_path(settings["paths"]["log"])
        self.tts_dir = resolve_path(settings["paths"]["scripts_tts"])
        self.filler_dir = resolve_path(settings["paths"]["fillers"])
        self.pool_dir = resolve_path(settings["bgm"]["pool_dir"])
        self.state_path = resolve_path(settings["paths"]["state"])
        # 同じ曲を何サイクル使い回すか。生成が実時間に追いつかないための緩和策
        self.reuse_count = int(settings["bgm"].get("reuse_count", 1))
        self.crawler_dir = resolve_path(settings["news"]["crawler_dir"])

        self.selector = NewsSelector(settings)
        ls = settings["liquidsoap"]
        self.client = LiquidsoapClient(ls["telnet_host"], ls["telnet_port"], ls)

        self.lead = float(settings["program"]["prefetch_lead_sec"])
        self.recent = recent_scripts(
            self.log_path, settings["llm"]["recent_scripts"], "news"
        )

        # push 済みでまだ鳴り終わっていないもの。先頭が再生中(または直前)
        self.pending: deque[Item] = deque()
        self.current: Item | None = None
        self.paused = False
        # 次に積みたいもの。フィラーは「間に合わなかったときの繋ぎ」なので
        # これを進めない (TECHNICALJ.md §3 の FILLER_LOOP)
        self.want = "music"

        # 先読みスロット。1 個ずつだけ持つ
        self.next_news: Item | None = None
        self.next_talk: Item | None = None
        self._news_task: asyncio.Task | None = None
        self._talk_task: asyncio.Task | None = None
        self.aizuchi: list[Item] = []
        self._filler_turn = 0

        # 表示系。MUSIC_PLAYING 中は直前のニュースの背景を出したままにする
        self.background: str | None = None
        self.headline: dict | None = None
        self.clients: set[web.WebSocketResponse] = set()
        self.runner: web.AppRunner | None = None
        self._pushing = asyncio.Lock()

    # ---- ログ / 表示系への通知 ------------------------------------------

    def emit(self, event: dict) -> None:
        # viseme 配列は数百要素あるのでコンソールには出さない (WS には全部送る)
        shown = event
        if "visemes" in event:
            shown = {**event, "visemes": f"<{len(event['visemes'])}個>"}
            shown.pop("vtimes", None)
            shown.pop("vdurations", None)
        print(f"[{time.strftime('%H:%M:%S')}] {shown}", flush=True)
        if self.clients:
            asyncio.create_task(self.broadcast(event))

    async def broadcast(self, event: dict) -> None:
        payload = json.dumps(event, ensure_ascii=False)
        for ws in list(self.clients):
            try:
                await ws.send_str(payload)
            except (ConnectionError, RuntimeError):
                self.clients.discard(ws)

    def snapshot(self) -> list[dict]:
        """途中から繋いだブラウザにも今の画面を復元させる。"""
        vv = self.settings["voicevox"]
        events: list[dict] = [
            {"event": "paused", "value": self.paused},
            # VOICEVOX の利用規約で求められるクレジット表示
            {"event": "credit", "text": f"VOICEVOX:{vv['speaker_name']}"},
            {"event": "background", "url": self.background},
        ]
        if self.headline:
            events.append(self.headline)
        if self.current:
            events.append(
                {"event": "state", "phase": PHASE_OF_KIND.get(self.current.kind, "")}
            )
        return events

    # ---- 素材づくり ------------------------------------------------------

    def _synth(self, text: str, path: Path) -> tuple[Path, dict]:
        """ブロッキング。必ず asyncio.to_thread 経由で呼ぶこと。"""
        return voicevox_synth.synth_to_file(text, path, self.settings)

    def _build_speech(self, text: str, kind: str, article: Article | None = None) -> Item:
        """テキストを wav にして Item にする。ブロッキング。

        合成後の長さを見て、狙いより長すぎる場合は文字数を削って作り直す。
        LLM に投げ直すより速く、失敗もしない (TECHNICALJ.md §7 の「実測確認」)。
        """
        conf = self.settings["script"]
        limit = float(conf["max_wav_sec"])
        max_regen = int(conf.get("max_regen", 1))

        for _ in range(max_regen + 1):
            name = f"{kind}_{voicevox_synth.text_hash(text)}.wav"
            path, meta = self._synth(text, self.tts_dir / name)
            duration = meta["duration_sec"]
            if duration <= limit or len(text) <= 20:
                break
            # 読み上げ速度は文字数にほぼ比例するので、必要な比率まで削る
            keep = max(20, int(len(text) * (limit / duration) * SHRINK_MARGIN))
            shortened = clean_for_tts(text[:keep], keep)
            if shortened == text:
                break
            text = shortened

        return Item(path=path, kind=kind, duration=duration, text=text,
                    article=article, meta=meta)

    def _build_news_item(self) -> Item:
        """ニュース 1 本ぶんの原稿と wav を作る。ブロッキング。

        原稿生成に失敗した記事は諦めて次の記事へ移る。全部駄目なら固定
        アナウンスに落とす。ここで例外を投げると番組が止まるので、
        VOICEVOX が落ちている場合 (合成そのものができない) を除いて
        必ず Item を返すこと。
        """
        for _ in range(3):
            article = self.selector.pick()
            if article is None:
                # クローラーがまだ走っていない。記事が無いのは異常ではない
                print(
                    "ニュースがありません。Gcrawler を実行してください "
                    f"({self.crawler_dir})",
                    flush=True,
                )
                return self._build_speech(dj_script.FALLBACK_NO_NEWS, "notice")

            try:
                text = dj_script.generate_news_script(article, self.settings, self.recent)
            except RuntimeError as e:
                self.emit({"event": "error", "where": "script", "detail": str(e)})
                continue
            if not text:
                continue

            append_script_log(
                self.log_path, "news", text, title=article.title, url=article.url
            )
            self.recent = (self.recent + [text])[
                -self.settings["llm"]["recent_scripts"] :
            ]
            return self._build_speech(text, "news", article)

        return self._build_speech(dj_script.FALLBACK_NO_NEWS, "notice")

    def _build_talk_item(self) -> Item:
        """フィラーのショートトーク。ブロッキング。"""
        recent = recent_scripts(self.log_path, 3, "talk")
        text = dj_script.generate_short_talk(self.settings, recent)
        append_script_log(self.log_path, "talk", text)
        return self._build_speech(text, "filler")

    def prepare_aizuchi(self) -> None:
        """相槌を事前生成する。ブロッキング。起動時に 1 回だけ。

        FILLER_LOOP に突入した瞬間に鳴らせるものが 1 つも無いと、そこが
        そのまま無音になる。ショートトークの生成 (LLM 1.5 秒 + TTS) を
        待つあいだの繋ぎとして必ず用意しておくこと (TECHNICALJ.md §3)。
        """
        items: list[Item] = []
        for text in dj_script.AIZUCHI:
            try:
                path, meta = self._synth(
                    text, self.filler_dir / f"aizuchi_{voicevox_synth.text_hash(text)}.wav"
                )
            except (httpx.HTTPError, LookupError, AudioError) as e:
                print(f"相槌の生成に失敗: {text} ({e})", flush=True)
                continue
            items.append(
                Item(path=path, kind="filler", duration=meta["duration_sec"],
                     text=text, meta=meta)
            )
        self.aizuchi = items

    # ---- 先読み ----------------------------------------------------------

    def _ensure_prefetch(self) -> None:
        """次のニュースとショートトークを 1 本ずつ先に作っておく。

        BGM が鳴っているあいだに次の原稿を仕上げるのが定常運転
        (TECHNICALJ.md §3)。間に合わなければフィラーで繋ぐので、ここでは
        「常に 1 本先まで用意しようとする」以上のことをしない。
        """
        if self.next_news is None and (self._news_task is None or self._news_task.done()):
            self._news_task = asyncio.create_task(self._prefetch(self._build_news_item, "news"))
        if self.next_talk is None and (self._talk_task is None or self._talk_task.done()):
            self._talk_task = asyncio.create_task(self._prefetch(self._build_talk_item, "talk"))

    async def _prefetch(self, build, slot: str) -> None:
        try:
            item = await asyncio.to_thread(build)
        except (httpx.HTTPError, LookupError, AudioError, OSError) as e:
            # VOICEVOX が落ちている等。BGM 連続再生に退避して回り続ける
            self.emit({"event": "error", "where": f"prefetch_{slot}", "detail": str(e)})
            await asyncio.sleep(5)
            return
        if slot == "news":
            self.next_news = item
        else:
            self.next_talk = item

    # ---- キュー投入 ------------------------------------------------------

    def _take_music(self) -> Item | None:
        path = take_bgm(self.pool_dir, self.state_path, self.reuse_count)
        if path is None:
            return None
        try:
            duration = wav_duration(path)
        except AudioError:
            return None
        return Item(path=path, kind="music", duration=duration)

    def _take_news(self) -> Item | None:
        item, self.next_news = self.next_news, None
        return item

    def _take_filler(self) -> Item | None:
        """相槌とショートトークを交互に出す。

        トークが未完成なら相槌でつなぐ。相槌すら無い (VOICEVOX が落ちて
        いる) 場合は None を返し、呼び出し側が音楽だけで回す。
        """
        self._filler_turn += 1
        if self._filler_turn % 2 == 0 and self.next_talk is not None:
            item, self.next_talk = self.next_talk, None
            return item
        if self.aizuchi:
            return random.choice(self.aizuchi)
        if self.next_talk is not None:
            item, self.next_talk = self.next_talk, None
            return item
        return None

    async def push_next(self) -> Item | None:
        """状態機械にしたがって次の 1 本を積む。

        欲しいものが用意できていなければ順に代替へ落ちる。何も無ければ
        None (キューが空になるので radio.liq の blank が無音を流す)。
        """
        order = (
            ("music", "filler", "news")
            if self.want == "music"
            else ("news", "filler", "music")
        )
        takers = {
            "music": self._take_music,
            "news": self._take_news,
            "filler": self._take_filler,
        }
        for kind in order:
            item = takers[kind]()
            if item is None:
                continue
            await self._push(item)
            # フィラーは繋ぎなので want を進めない。BGM が届いた時点で
            # 本来の音楽へ戻れるようにしておく
            if kind == "music":
                self.want = "news"
            elif kind == "news":
                self.want = "music"
            return item
        return None

    async def _push(self, item: Item) -> None:
        await asyncio.to_thread(self.client.push, item.path)
        self.pending.append(item)
        # 通知を取りこぼすと pending が減らないので上限を切る。残り秒数は
        # Liquidsoap の実測 (queue_state) から取るので、古い分を捨てても
        # スケジューリングは狂わない
        while len(self.pending) > 64:
            self.pending.popleft()

    # ---- トラック開始イベント -------------------------------------------

    def on_track_start(self, filename: str) -> None:
        """radio.liq からの通知。ここが字幕・背景・リップシンクの起点。"""
        name = Path(filename).name
        for i, item in enumerate(self.pending):
            if item.path.name != name:
                continue
            # 手前のものは (skip 等で) 鳴らずに消えた。まとめて捨てる
            for _ in range(i + 1):
                started = self.pending.popleft()
            self.current = started
            self.announce(started)
            return
        # 前回の残骸など。表示は変えず、鳴っていること自体は妨げない
        print(f"[track_start] 未知のファイル: {name}", flush=True)

    def announce(self, item: Item) -> None:
        self.emit({"event": "state", "phase": PHASE_OF_KIND.get(item.kind, "")})

        if item.article is not None:
            self.background = self.background_url(item.article)
            self.headline = {
                "event": "headline",
                "title": item.article.title,
                "source": self.settings["dj"]["source_name"],
                "url": item.article.url,
            }
            self.emit({"event": "background", "url": self.background})
            self.emit(self.headline)

        if item.kind == "music":
            # 背景と見出しは直前のニュースのまま残す (TECHNICALJ.md §4)
            self.emit({"event": "music", "filename": item.path.name})
            return

        visemes, vtimes, vdurations = mora_to_visemes(item.meta.get("accent_phrases", []))
        self.emit(
            {
                "event": "speech",
                "kind": item.kind,
                "text": item.text,
                "visemes": visemes,
                "vtimes": vtimes,
                "vdurations": vdurations,
                # push から実際に音が出るまでのズレ。環境に合わせて調整する
                "delay_ms": self.settings["program"]["lipsync_delay_ms"],
            }
        )

    def background_url(self, article: Article) -> str | None:
        """記事スクショの URL。Gcrawler の news/ を /news/ で静的配信している。"""
        if article.screenshot is None:
            return None
        try:
            rel = article.screenshot.relative_to(self.crawler_dir)
        except ValueError:
            return None
        return f"/news/{rel.as_posix()}"

    # ---- 進行ループ ------------------------------------------------------

    async def queued_seconds(self) -> float:
        """あと何秒ぶんの音がキューに残っているか。

        Liquidsoap を真とする。track_event を取りこぼしても、ここが正しければ
        無音にはならない。待機中のリクエスト数だけ pending の末尾から
        長さを足す (pending は push 順に並んでいる)。
        """
        rem, waiting = await asyncio.to_thread(self.client.queue_state)
        ahead = max(rem, 0.0)
        if waiting > 0:
            ahead += sum(item.duration for item in list(self.pending)[-waiting:])
        return ahead

    async def pump_loop(self) -> None:
        """キューが尽きる前に次を積み続ける。無音を作らないための唯一の担保。"""
        while True:
            await asyncio.sleep(POLL_INTERVAL)
            # 先読みは再生と独立なので一時停止中も進めておく。ここを paused の
            # 後ろに置くと、PAUSE 中に 1 本も作られず、PLAY した瞬間にゼロから
            # 生成が始まって長いフィラーになる (実測: 起動直後に 90 秒 PAUSE
            # したら INTRO のあと 45 秒ぶん相槌が続いた)。
            self._ensure_prefetch()
            if self.paused:
                continue

            try:
                ahead = await self.queued_seconds()
            except LiquidsoapError as e:
                self.emit({"event": "error", "where": "pump", "detail": str(e)})
                await asyncio.sleep(2)
                continue

            if ahead > self.lead:
                continue

            async with self._pushing:
                item = await self.push_next()
            if item is None:
                # 音源が 1 つも用意できない。BGM プールも TTS も駄目な状態。
                # 番組は無音になるが、復旧したら自動で戻る
                await asyncio.sleep(2)

    async def start(self) -> None:
        """相槌を用意し、キューを掃除して自己紹介から始める。"""
        # 前回の押し込みが残っていると、番組の頭から前回の続きが鳴る。
        # 空のときに flush すると skip が予約されて INTRO が飛ぶので
        # flush_if_busy を使う (liquidsoap_client 参照)
        await asyncio.to_thread(self.client.flush_if_busy)
        self.pending.clear()

        print("相槌を用意しています…", flush=True)
        await asyncio.to_thread(self.prepare_aizuchi)
        print(f"  相槌 {len(self.aizuchi)} 本", flush=True)

        # 1 本目のニュースの生成をここで始める。pump_loop が動き出すのは
        # start() が終わってからなので、ここで蒔いておかないと INTRO の
        # 生成時間ぶんを丸ごと待つことになる。
        self._ensure_prefetch()

        text = await asyncio.to_thread(dj_script.generate_intro, self.settings)
        append_script_log(self.log_path, "intro", text)
        try:
            intro = await asyncio.to_thread(self._build_speech, text, "intro")
            await self._push(intro)
        except (httpx.HTTPError, LookupError, AudioError) as e:
            # VOICEVOX が落ちている。BGM だけで回す (TECHNICALJ.md §8)
            self.emit({"event": "error", "where": "intro", "detail": str(e)})
        # 自己紹介の次はニュース (TECHNICALJ.md §3 の INTRO → SPEAKING)。
        # 自己紹介が鳴っているあいだに先読みが終わるので、たいてい間に合う。
        # 間に合わなければフィラーが入るだけで、順番は崩れない。
        self.want = "news"

    # ---- コマンド --------------------------------------------------------

    async def cmd_pause(self) -> str:
        await asyncio.to_thread(self.client.pause)
        self.paused = True
        self.emit({"event": "paused", "value": True})
        return "pause"

    async def cmd_play(self) -> str:
        await asyncio.to_thread(self.client.play)
        self.paused = False
        self.emit({"event": "paused", "value": False})
        return "play"

    async def cmd_skip(self) -> str:
        """次のニュースへ飛ぶ。

        キューを掃除してからニュースを積む。積んでから skip すると、
        待機していたフィラーや BGM が先に鳴ってしまう。
        """
        async with self._pushing:
            await asyncio.to_thread(self.client.flush_if_busy)
            self.pending.clear()
            self.current = None
            self.want = "news"
            item = await self.push_next()
        return f"skip → {item.kind if item else '積めるものがありません'}"

    async def cmd_status(self) -> str:
        ahead = await self.queued_seconds()
        from bgm_pool import pool_files

        return (
            f"再生中: {self.current.kind if self.current else '-'} "
            f"({self.current.text[:30] if self.current else ''})\n"
            f"  次に積むもの: {self.want} / キュー残 {ahead:.1f} 秒 "
            f"/ 待機 {len(self.pending)} 本\n"
            f"  BGM プール: {len(pool_files(self.pool_dir))} 曲 "
            f"/ 先読み: ニュース={'あり' if self.next_news else 'なし'} "
            f"トーク={'あり' if self.next_talk else 'なし'} "
            f"相槌={len(self.aizuchi)} 本\n"
            f"  除外リング: {len(self.selector.exclude)} 件 / paused={self.paused}"
        )

    # ---- HTTP / WebSocket ------------------------------------------------

    async def track_event(self, request: web.Request) -> web.Response:
        """radio.liq からのトラック通知 (TECHNICALJ.md §4)。"""
        try:
            data = await request.json()
        except (json.JSONDecodeError, ValueError):
            return web.json_response({"ok": False}, status=400)
        if data.get("event") == "track_start" and data.get("filename"):
            self.on_track_start(data["filename"])
        return web.json_response({"ok": True})

    async def ws_handler(self, request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse(heartbeat=30)
        await ws.prepare(request)
        self.clients.add(ws)
        for event in self.snapshot():
            await ws.send_str(json.dumps(event, ensure_ascii=False))

        handlers = {
            "pause": self.cmd_pause,
            "play": self.cmd_play,
            "skip": self.cmd_skip,
        }
        try:
            async for msg in ws:
                if msg.type != aiohttp.WSMsgType.TEXT:
                    continue
                try:
                    cmd = json.loads(msg.data).get("cmd")
                except json.JSONDecodeError:
                    continue
                handler = handlers.get(cmd)
                if handler is None:
                    await ws.send_str(
                        json.dumps({"event": "error", "detail": f"不明なcmd: {cmd}"})
                    )
                    continue
                result = await handler()
                print(f"[{time.strftime('%H:%M:%S')}] ws> {cmd}: {result}", flush=True)
        finally:
            self.clients.discard(ws)
        return ws

    def build_app(self) -> web.Application:
        app = web.Application()
        app.router.add_get("/ws", self.ws_handler)
        app.router.add_post("/api/track_event", self.track_event)
        # no-cache を付けないと、表示系を直したあとリロードしても Chrome が
        # メモリキャッシュを出してきて古い画面のままになる (実測)
        app.router.add_get(
            "/",
            lambda _: web.FileResponse(
                WEB_DIR / "index.html", headers={"Cache-Control": "no-cache"}
            ),
        )
        app.router.add_static("/vrm/", VRM_DIR)
        # Gcrawler の news/ をそのまま配信する。コピーを持つと、記事が
        # 消えたときに掃除する場所が 2 箇所になる
        if self.crawler_dir.is_dir():
            app.router.add_static("/news/", self.crawler_dir)
        # /libs/... は web/libs/... に解決される
        app.router.add_static("/", WEB_DIR)
        return app

    async def serve(self) -> None:
        prog = self.settings["program"]
        self.runner = web.AppRunner(self.build_app(), access_log=None)
        await self.runner.setup()
        site = web.TCPSite(self.runner, prog["websocket_host"], prog["websocket_port"])
        await site.start()
        host = prog["websocket_host"]
        shown = "localhost" if host in ("0.0.0.0", "") else host
        print(f"表示系: http://{shown}:{prog['websocket_port']}/", flush=True)

    async def shutdown(self) -> None:
        """表示系サーバーを畳む。

        これをやらずに終了すると、Ctrl-C のたびに aiohttp の
        「Task was destroyed but it is pending」と Event loop is closed が
        大量に出て、本当のエラーが埋もれる。
        """
        for ws in list(self.clients):
            await ws.close()
        self.clients.clear()
        if self.runner is not None:
            await self.runner.cleanup()
            self.runner = None

    async def stdin_loop(self) -> None:
        reader = asyncio.StreamReader()
        await asyncio.get_running_loop().connect_read_pipe(
            lambda: asyncio.StreamReaderProtocol(reader), sys.stdin
        )
        handlers = {
            "skip": self.cmd_skip,
            "pause": self.cmd_pause,
            "play": self.cmd_play,
            "status": self.cmd_status,
        }
        while True:
            line = await reader.readline()
            if not line:  # パイプ入力が閉じても番組は流し続ける
                return
            cmd = line.decode().strip().lower()
            if not cmd:
                continue
            if cmd in ("quit", "exit"):
                raise KeyboardInterrupt
            handler = handlers.get(cmd)
            if handler is None:
                print(f"不明なコマンド: {cmd} ({'/'.join(handlers)}/quit)", flush=True)
                continue
            print(f"[{time.strftime('%H:%M:%S')}] > {cmd}: {await handler()}", flush=True)


async def amain(args) -> int:
    settings = load_settings()
    service = NewsService(settings)

    try:
        await asyncio.to_thread(service.client.command, "uptime")
    except LiquidsoapError as e:
        print(e, file=sys.stderr)
        print("先に liquidsoap liquidsoap/radio.liq を起動してください", file=sys.stderr)
        return 1

    await service.serve()
    await service.start()
    tasks = [asyncio.create_task(service.pump_loop())]
    if not args.no_stdin:
        tasks.append(asyncio.create_task(service.stdin_loop()))
    try:
        await asyncio.gather(*tasks)
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        for t in tasks:
            t.cancel()
        await service.shutdown()
    return 0


def prepare_fillers_only() -> int:
    """相槌の事前生成だけして終わる。start_all.sh がサービス起動前に呼ぶ。

    合成済みのものはハッシュ一致でそのまま使われるので、毎回呼んでよい。
    """
    settings = load_settings()
    service = NewsService(settings)
    try:
        service.prepare_aizuchi()
    except (httpx.HTTPError, LookupError, AudioError) as e:
        print(f"相槌の事前生成に失敗しました: {e}", file=sys.stderr)
        return 1
    print(f"相槌 {len(service.aizuchi)} 本を用意しました: {service.filler_dir}")
    return 0 if service.aizuchi else 1


def main() -> int:
    ap = argparse.ArgumentParser(description="AIradio 番組進行サービス")
    ap.add_argument("--no-stdin", action="store_true", help="標準入力コマンドを使わない")
    ap.add_argument(
        "--prepare-fillers",
        action="store_true",
        help="相槌の事前生成だけして終了する (起動前の準備用)",
    )
    args = ap.parse_args()

    if args.prepare_fillers:
        return prepare_fillers_only()
    try:
        return asyncio.run(amain(args))
    except KeyboardInterrupt:
        print("\n終了します。")
        return 0


if __name__ == "__main__":
    sys.exit(main())
