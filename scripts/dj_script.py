#!/usr/bin/env python3
"""Qwen3.6 (llama-server) で DJ の喋る原稿を作る。

  uv run --no-sync scripts/dj_script.py --news        # ニュース原稿を1本
  uv run --no-sync scripts/dj_script.py --intro       # 起動時の自己紹介
  uv run --no-sync scripts/dj_script.py --talk        # フィラー用ショートトーク
  uv run --no-sync scripts/dj_script.py --news --dry-run  # プロンプトだけ見る

方針は AIjukebox の dj_prompt.py を踏襲する。

- thinking は切る (レイテンシ優先)。Qwen3 系は既定で thinking を吐き、
  reasoning_content に max_tokens を食われて content が空で返る
- 生成後に記号除去と文字数トリムをかけてから TTS に渡す
- **生成に失敗しても番組は止めない**。呼び出し側が拾える固定文をここに置く
"""

from __future__ import annotations

import argparse
import random
import sys

import httpx

from common import clean_for_tts, load_settings, recent_scripts, resolve_path

# ---- 固定フォールバック --------------------------------------------------
# LLM が落ちていても番組が続くように、生成物と同じ役割の固定文を持つ
# (TECHNICALJ.md §8)。ここを空にしないこと。

FALLBACK_INTRO = (
    "こんにちは。ここはAIがお送りするニュースラジオです。"
    "最新のニュースと、ゆるやかな音楽をお届けします。どうぞごゆっくり。"
)

FALLBACK_NO_NEWS = (
    "ただいまニュースの準備中です。準備ができるまで、音楽をお楽しみください。"
)

FALLBACK_TALKS = [
    "さて、次の曲へまいりましょう。",
    "いい天気ですね。音楽がよく似合う時間です。",
    "こういう時間、嫌いじゃないんですよね。",
    "ちょっとひと息つきましょうか。",
]

# 相槌。起動時に事前生成して cache/fillers に置く (TECHNICALJ.md §3)。
# 短く、単体で流れても不自然でないものだけにすること。
AIZUCHI = [
    "えーと。",
    "そうですね。",
    "ふむ。",
    "なるほど。",
    "はい。",
    "ええ。",
    "うーん、そうだなあ。",
    "さて。",
]


# ---- プロンプト ----------------------------------------------------------

NEWS_SYSTEM = """あなたはローカルAIラジオ「{program}」のDJ「{speaker}」です。
与えられたニュースを、ラジオで読み上げる日本語の原稿にしてください。

# ルール
1. 与えられたタイトルと要約のみを事実として扱う
2. 与えられていない具体的事実は絶対に足さない(数字・企業名・時期・背景など)
3. 出典に自然に触れる。例:「{source}の記事によると」
4. 出力は音声合成で読み上げるため、記号(*, #, ☆, ♪, 括弧の多用)や改行を使わない
5. 全体で{max_chars}文字以内。これを超えてはいけない
6. 前回までの原稿と同じ言い回しの繰り返しを避ける
7. 必ず「。」で終わる完結した文にする
8. 字数を埋めるための無意味な繰り返し(「さて、さて、さて」など)を書かない
9. キャラクター設定: {persona}

# 出力形式
原稿本文のみを出力する。前置きや説明は不要。"""

INTRO_SYSTEM = """あなたはローカルAIラジオ「{program}」のDJ「{speaker}」です。
放送開始の自己紹介を書いてください。

# ルール
1. ニュースの内容には触れない(まだ読んでいない)
2. 番組名と、これからニュースと音楽をお届けすることを伝える
3. 出力は音声合成で読み上げるため、記号や改行を使わない
4. 全体で{max_chars}文字以内
5. 必ず「。」で終わる完結した文にする
6. キャラクター設定: {persona}

# 出力形式
本文のみを出力する。前置きや説明は不要。"""

TALK_SYSTEM = """あなたはローカルAIラジオ「{program}」のDJ「{speaker}」です。
曲と曲のあいだをつなぐ、短いひとりごとを書いてください。

# ルール
1. ニュースや特定の曲の話はしない。当たり障りのない雑談にとどめる
2. 事実や固有名詞を持ち出さない
3. 出力は音声合成で読み上げるため、記号や改行を使わない
4. 全体で{max_chars}文字以内。短いほどよい
5. 必ず「。」で終わる完結した文にする
6. キャラクター設定: {persona}

# 出力形式
本文のみを出力する。前置きや説明は不要。"""


def _system_vars(settings: dict, max_chars: int) -> dict:
    dj = settings["dj"]
    return {
        "program": dj["program_name"],
        "persona": dj["persona"],
        "source": dj["source_name"],
        # 番組内で名乗る名前。未設定なら VOICEVOX の話者名をそのまま使う。
        # speaker_name 自体を変えてはいけない (話者検索とクレジット表示に使う)。
        "speaker": dj.get("name") or settings["voicevox"]["speaker_name"],
        "max_chars": max_chars,
    }


def build_news_prompt(article, recent: list[str]) -> str:
    parts = [
        "# 読み上げるニュース",
        f"タイトル: {article.title}",
        f"要約: {article.summary}",
    ]
    if recent:
        parts += [
            "",
            "# 直近の原稿(同じ言い回しは避けてください)",
            "\n".join(f"- {t}" for t in recent),
        ]
    return "\n".join(parts)


# ---- llama-server 呼び出し ------------------------------------------------


def call_llm(system: str, user: str, conf: dict, *, retries: int = 2) -> str:
    """llama-server に投げて本文を返す。一時エラーはリトライする。

    それでも駄目なら例外を投げる。フォールバック文への退避は呼び出し側の責務
    (どの固定文に落とすかは用途で違うため)。
    """
    payload = {
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "temperature": conf["temperature"],
        "max_tokens": conf["max_tokens"],
        "stream": False,
        # Qwen3 系は既定で thinking を吐く。chat template 側で切る (AIzunda 知見)
        "chat_template_kwargs": {"enable_thinking": False},
    }
    last: Exception | None = None
    for attempt in range(retries + 1):
        try:
            r = httpx.post(
                f"{conf['base_url']}/v1/chat/completions", json=payload, timeout=120.0
            )
            r.raise_for_status()
            data = r.json()
            return data["choices"][0]["message"]["content"].strip()
        except (httpx.HTTPError, KeyError, IndexError, ValueError) as e:
            last = e
            if attempt < retries:
                continue
    raise RuntimeError(f"llama-server の呼び出しに失敗しました: {last}") from last


# ---- 生成 ----------------------------------------------------------------


def generate_news_script(article, settings: dict, recent: list[str] | None = None) -> str:
    """ニュース原稿を生成する。失敗時は例外 (呼び出し側で記事を飛ばす)。"""
    conf = settings["llm"]
    max_chars = settings["script"]["max_chars"]
    if recent is None:
        recent = recent_scripts(
            resolve_path(settings["paths"]["log"]), conf["recent_scripts"]
        )

    raw = call_llm(
        NEWS_SYSTEM.format(**_system_vars(settings, max_chars)),
        build_news_prompt(article, recent),
        conf,
    )
    return clean_for_tts(raw, max_chars)


def generate_intro(settings: dict) -> str:
    """起動時の自己紹介。生成に失敗したら固定文を返す(番組を止めない)。"""
    conf = settings["llm"]
    max_chars = settings["script"]["max_chars"]
    try:
        raw = call_llm(
            INTRO_SYSTEM.format(**_system_vars(settings, max_chars)),
            f"今日は{settings['dj']['program_name']}の放送日です。自己紹介をお願いします。",
            conf,
        )
        text = clean_for_tts(raw, max_chars)
        return text or FALLBACK_INTRO
    except RuntimeError:
        return FALLBACK_INTRO


def generate_short_talk(settings: dict, recent: list[str] | None = None) -> str:
    """フィラー用のショートトーク。失敗したら固定文からランダムに返す。

    BGM プールが空のあいだ繰り返し流すので、長いと音楽への切り替えが
    鈍る。filler_max_chars (既定 60 字 ≒ 5〜10 秒) で必ず抑えること。
    """
    conf = settings["llm"]
    max_chars = settings["script"]["filler_max_chars"]
    user = "ひとりごとをひとつお願いします。"
    if recent:
        user += "\n\n直近のひとりごと(重複を避けてください):\n" + "\n".join(
            f"- {t}" for t in recent
        )
    try:
        raw = call_llm(
            TALK_SYSTEM.format(**_system_vars(settings, max_chars)), user, conf
        )
        text = clean_for_tts(raw, max_chars)
        return text or random.choice(FALLBACK_TALKS)
    except RuntimeError:
        return random.choice(FALLBACK_TALKS)


def main() -> int:
    ap = argparse.ArgumentParser(description="DJ の原稿を生成する")
    ap.add_argument("--news", action="store_true", help="ニュース原稿")
    ap.add_argument("--intro", action="store_true", help="起動時の自己紹介")
    ap.add_argument("--talk", action="store_true", help="ショートトーク")
    ap.add_argument("--dry-run", action="store_true", help="プロンプトだけ表示する")
    args = ap.parse_args()

    settings = load_settings()

    if args.intro:
        print(generate_intro(settings))
        return 0
    if args.talk:
        print(generate_short_talk(settings))
        return 0

    if not args.news:
        ap.error("--news / --intro / --talk のいずれかが必要です")

    from news import NewsSelector

    article = NewsSelector(settings).pick()
    if article is None:
        print("記事がありません。先に Gcrawler を実行してください", file=sys.stderr)
        return 1

    print(f"=== [{article.day}] {article.title}", file=sys.stderr)
    recent = recent_scripts(
        resolve_path(settings["paths"]["log"]), settings["llm"]["recent_scripts"]
    )
    if args.dry_run:
        print(build_news_prompt(article, recent))
        return 0

    try:
        text = generate_news_script(article, settings, recent)
    except RuntimeError as e:
        print(e, file=sys.stderr)
        return 1
    print(f"({len(text)}文字) {text}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
