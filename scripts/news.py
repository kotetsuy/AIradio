"""Gcrawler が置いたニュース記事の読み込みと選定。

AIradio と Gcrawler は **ファイル契約のみで疎結合** (TECHNICALJ.md §2-5)。ここが
その契約の実装で、前提にしているのは以下だけ。

  <crawler_dir>/YYYY-MM-DD/NNNN.json   { id, url, title, summary, screenshot }
  <crawler_dir>/YYYY-MM-DD/NNNN.png    記事のスクリーンショット

  uv run --no-sync scripts/news.py            # 直近3日分の一覧
  uv run --no-sync scripts/news.py --pick     # 1件選ぶ (除外リングは更新しない)
"""

from __future__ import annotations

import argparse
import json
import random
from collections import deque
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

from common import load_settings, load_state, resolve_path, save_state

STATE_KEY = "exclude_ring"


@dataclass(frozen=True)
class Article:
    id: str
    url: str
    title: str
    summary: str
    # 記事ページのスクリーンショット。無い記事もありうる
    screenshot: Path | None
    day: str

    @property
    def source_date(self) -> str:
        return self.day


def _load_article(path: Path, day: str) -> Article | None:
    """1 件の JSON を読む。壊れていたら None (番組は止めない)。"""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not data.get("title") or not data.get("summary"):
        return None

    shot = data.get("screenshot")
    shot_path = path.parent / shot if shot else None
    if shot_path is not None and not shot_path.exists():
        shot_path = None

    return Article(
        # id が無い古い形式でもファイル名で一意にはなる
        id=str(data.get("id") or f"{day}/{path.stem}"),
        url=data.get("url", ""),
        title=data["title"],
        summary=data["summary"],
        screenshot=shot_path,
        day=day,
    )


def recent_days(days: int, today: date | None = None) -> list[str]:
    """今日を含む直近 N 日の YYYY-MM-DD を新しい順で返す。"""
    today = today or date.today()
    return [(today - timedelta(days=i)).isoformat() for i in range(days)]


def load_articles(crawler_dir: Path, days: int) -> list[Article]:
    """直近 N 日分の記事をすべて読む。ディレクトリが無い日は黙って飛ばす。"""
    articles: list[Article] = []
    for day in recent_days(days):
        day_dir = crawler_dir / day
        if not day_dir.is_dir():
            continue
        for path in sorted(day_dir.glob("*.json")):
            article = _load_article(path, day)
            if article is not None:
                articles.append(article)
    return articles


class NewsSelector:
    """直近 N 日分から、直近読んだ分を除いて一様ランダムに選ぶ。

    除外リングは db/state.json に永続化する。再起動のたびに同じニュースを
    読み直すのを防ぐため (TECHNICALJ.md §5)。
    """

    def __init__(self, settings: dict):
        conf = settings["news"]
        self.crawler_dir = resolve_path(conf["crawler_dir"])
        self.days = int(conf["days"])
        self.state_path = resolve_path(settings["paths"]["state"])

        state = load_state(self.state_path)
        ring = state.get(STATE_KEY, [])
        self.exclude: deque[str] = deque(ring, maxlen=int(conf["exclude_ring"]))

    def available(self) -> list[Article]:
        return load_articles(self.crawler_dir, self.days)

    def pick(self) -> Article | None:
        """1 件選んで除外リングに積む。記事が無ければ None。

        None は異常ではなく「クローラーがまだ走っていない」ケース。
        呼び出し側は固定アナウンスに退避すること (TECHNICALJ.md §8)。
        """
        articles = self.available()
        if not articles:
            return None

        candidates = [a for a in articles if a.id not in self.exclude]
        if not candidates:
            # 記事数が除外リングの長さ以下しかない (クローラーを回した直後など)。
            # 全部を候補に戻すが、直前に読んだものだけは外す。これを入れないと
            # 同じ記事を 2 回続けて読むことがある
            last = self.exclude[-1] if self.exclude else None
            candidates = [a for a in articles if a.id != last] or articles

        chosen = random.choice(candidates)
        self.exclude.append(chosen.id)
        self._persist()
        return chosen

    def _persist(self) -> None:
        state = load_state(self.state_path)
        state[STATE_KEY] = list(self.exclude)
        save_state(self.state_path, state)


def main() -> int:
    ap = argparse.ArgumentParser(description="ニュースの読み込み・選定を確認する")
    ap.add_argument("--pick", action="store_true", help="1件選んで表示する")
    args = ap.parse_args()

    settings = load_settings()
    selector = NewsSelector(settings)
    print(f"crawler_dir: {selector.crawler_dir}")
    if not selector.crawler_dir.is_dir():
        print("  ディレクトリがありません。先に Gcrawler を実行してください")

    articles = selector.available()
    print(f"直近 {selector.days} 日分: {len(articles)} 件 / 除外リング {len(selector.exclude)} 件")

    if args.pick:
        article = selector.pick()
        if article is None:
            print("記事がありません")
            return 1
        print(f"\n[{article.day}] {article.title}\n{article.url}\n{article.summary}")
        print(f"スクショ: {article.screenshot}")
        return 0

    for a in articles:
        mark = "x" if a.id in selector.exclude else " "
        print(f" {mark} [{a.day}] {a.title}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
