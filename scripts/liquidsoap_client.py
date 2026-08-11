#!/usr/bin/env python3
"""Liquidsoap を telnet 経由で制御する。

  uv run --no-sync scripts/liquidsoap_client.py status
  uv run --no-sync scripts/liquidsoap_client.py push cache/scripts_tts/news_0001.wav
  uv run --no-sync scripts/liquidsoap_client.py skip
  uv run --no-sync scripts/liquidsoap_client.py pause / play

AIjukebox からの流用。違いは音楽と DJ の 2 キューではなく program 1 本を
順番に鳴らすこと (radio.liq 参照)。

telnetlib は Python 3.13 で削除されたので素の socket で話す。
"""

from __future__ import annotations

import argparse
import socket
import sys
from pathlib import Path

from common import load_settings, resolve_path

TIMEOUT = 5.0
# Liquidsoap の telnet は各応答を END 行で締める
END = b"END\r\n"


class LiquidsoapError(RuntimeError):
    pass


class LiquidsoapClient:
    """1 コマンドごとに接続する薄いクライアント。

    常時接続にしないのは、Liquidsoap を再起動しても Python 側が壊れないため。
    コマンド頻度はトラックの切り替わり程度なので接続コストは問題にならない。
    """

    def __init__(self, host: str, port: int, conf: dict | None = None):
        self.host = host
        self.port = port
        self.conf = conf or {}
        self.queue = self.conf.get("queue", "program")

    def commands(self, *cmds: str) -> list[str]:
        """複数のコマンドを 1 接続で投げて、応答を順に返す。

        1 コマンド 1 接続だと、番組進行の毎秒のポーリングで Liquidsoap の
        接続ログが膨れる。まとめて聞けるものはまとめる。
        """
        try:
            with socket.create_connection((self.host, self.port), TIMEOUT) as sock:
                sock.settimeout(TIMEOUT)
                sock.sendall("\n".join(cmds).encode("utf-8") + b"\n")
                buf = b""
                while buf.count(END) < len(cmds):
                    chunk = sock.recv(4096)
                    if not chunk:
                        break
                    buf += chunk
        except OSError as e:
            raise LiquidsoapError(
                f"Liquidsoap に接続できません ({self.host}:{self.port}): {e}"
            ) from e

        parts = buf.split(END)[: len(cmds)]
        return [p.decode("utf-8", "replace").strip() for p in parts]

    def command(self, cmd: str) -> str:
        return self.commands(cmd)[0]

    # ---- キュー投入 ------------------------------------------------------

    @staticmethod
    def to_uri(path: str | Path) -> str:
        """ローカルパスを push 用の文字列にする。

        Liquidsoap 2.4 は push の引数を行末までまるごと URI として扱うので、
        絶対パスをそのまま渡せばスペースも日本語も通る。逆に file:// +
        パーセントエンコードにすると %20 等が解釈されずリクエストが解決に
        失敗する (AIjukebox 実測)。
        """
        resolved = str(Path(path).resolve())
        if "\n" in resolved or "\r" in resolved:
            raise LiquidsoapError(f"改行を含むパスは push できません: {resolved!r}")
        return resolved

    def push(self, path: str | Path) -> str:
        return self.command(f"{self.queue}.push {self.to_uri(path)}")

    def skip(self) -> str:
        return self.command(f"{self.queue}.skip")

    def flush_queue(self) -> str:
        """キューを空にして再生中のトラックも止める。

        Liquidsoap は news_service より長生きするので、サービスだけ再起動すると
        前回 push した wav がキューに残ったまま鳴り続ける。

        **何も鳴っていないときに呼んではいけない。** request.queue のコマンドは
        flush_and_skip しかなく、空の状態で呼ぶと skip が予約されて、次に
        push した 1 本目が即座に飛ばされる (実測: 起動直後の INTRO が 1 秒で
        切れた)。掃除は flush_if_busy を使うこと。
        """
        return self.command(f"{self.queue}.flush_and_skip")

    def flush_if_busy(self) -> str | None:
        """鳴っている or 積まれているときだけキューを掃除する。"""
        remaining, waiting = self.queue_state()
        if waiting == 0 and remaining <= 0:
            return None
        return self.flush_queue()

    def queue_length(self) -> int:
        """キューで待機している (まだ鳴っていない) リクエスト数。"""
        raw = self.command(f"{self.queue}.queue")
        return len(raw.split())

    # ---- 再生制御 --------------------------------------------------------

    def set_paused(self, value: bool) -> str:
        """radio.liq の interactive.bool "paused" を切り替える。

        Liquidsoap 2.4 の output には .stop / .start が無いため、上流の
        switch 一箇所でソースの pull を止める (radio.liq 参照)。
        """
        return self.command(f"var.set paused = {'true' if value else 'false'}")

    def pause(self) -> str:
        return self.set_paused(True)

    def play(self) -> str:
        return self.set_paused(False)

    def is_paused(self) -> bool:
        return self.command("var.get paused").strip().lower().endswith("true")

    # ---- 状態取得 --------------------------------------------------------

    def remaining(self) -> float:
        """再生中トラックの残り秒数。何も鳴っていなければ負値。

        進行の主軸は radio.liq の on_track 通知 + wav の長さなので、これは
        補助 (ズレの検出とデバッグ) に使う。
        """
        raw = self.command(f"{self.queue}.pos")
        try:
            return float(raw.split()[0])
        except (ValueError, IndexError):
            return -1.0

    def queue_state(self) -> tuple[float, int]:
        """(再生中トラックの残り秒, 待機しているリクエスト数)。

        番組進行が毎秒呼ぶので 1 接続にまとめてある。
        """
        pos, queue = self.commands(f"{self.queue}.pos", f"{self.queue}.queue")
        try:
            remaining = float(pos.split()[0])
        except (ValueError, IndexError):
            remaining = -1.0
        return remaining, len(queue.split())

    def status(self) -> dict[str, str]:
        remaining, waiting = self.queue_state()
        return {
            "queue": self.command(f"{self.queue}.queue"),
            "paused": self.command("var.get paused"),
            "remaining": f"{remaining:.1f}",
            "waiting": str(waiting),
        }


def main() -> int:
    ap = argparse.ArgumentParser(description="Liquidsoap を telnet で制御する")
    ap.add_argument(
        "action",
        choices=["status", "push", "skip", "flush", "pause", "play", "remaining", "raw"],
    )
    ap.add_argument("arg", nargs="?", help="パス、または raw のときはコマンド文字列")
    args = ap.parse_args()

    conf = load_settings()["liquidsoap"]
    client = LiquidsoapClient(conf["telnet_host"], conf["telnet_port"], conf)

    try:
        if args.action == "status":
            for k, v in client.status().items():
                print(f"{k:12} {v}")
        elif args.action == "push":
            if not args.arg:
                ap.error("push にはパスが必要です")
            print(client.push(resolve_path(args.arg)))
        elif args.action == "skip":
            print(client.skip())
        elif args.action == "flush":
            print(client.flush_queue())
        elif args.action == "pause":
            print(client.pause())
        elif args.action == "play":
            print(client.play())
        elif args.action == "remaining":
            print(f"{client.remaining():.1f} 秒")
        elif args.action == "raw":
            if not args.arg:
                ap.error("raw にはコマンド文字列が必要です")
            print(client.command(args.arg))
    except LiquidsoapError as e:
        print(e, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
