# AIradio ミキシング・出力層 (Liquidsoap 2.4)
#
# ここは薄いミキサーに徹する。何をいつ鳴らすかの判断はすべて Python
# (scripts/news_service.py) が持ち、telnet 経由で program キューに push してくる。
# Liquidsoap 側に playlist や自動選曲を持たせると、表示している字幕・背景と
# 実際に鳴っている音がずれるため、絶対に足さないこと。
#
#   liquidsoap liquidsoap/radio.liq
#
# telnet:
#   echo 'program.push /path/to/news_0012.wav' | nc 127.0.0.1 1234
#   echo 'program.pos'  | nc 127.0.0.1 1234     → 残り秒
#   echo 'var.set paused = true' | nc 127.0.0.1 1234

# ---- telnet サーバー (Python からの制御用) -------------------------------
settings.server.telnet.set(true)
settings.server.telnet.bind_addr.set("127.0.0.1")
settings.server.telnet.port.set(1234)
# news_service は毎秒キューの状態を見に来る。既定 (3=info) のままだと
# 接続のたびに "New client" / "disconnected" が出てログが毎日数十万行になる。
settings.server.log.level.set(2)

# ---- キュー --------------------------------------------------------------
# AIjukebox は音楽 + DJ の 2 キースをダッキングで混ぜていたが、AIradio は
# スピーチ → フィラー → 音楽を「順番に」鳴らすので 1 本のキューで足りる。
# 押し込む順序がそのまま番組の進行になる。
program = request.queue(id="program")

# ---- news_service へのトラック通知 ---------------------------------------
# Python 側は「今どのファイルが鳴り始めたか」を知らないと、プール確認や
# フィラー投入の判断が遅れて無音 (dead air) になる。トラックが切り替わる
# たびに HTTP で叩いて起点を渡す。終了時刻は Python が wav の長さから出す。
#
# http.post はブロッキングなので必ず thread.run の中で呼ぶこと。
# ストリーミングのループを止めると音が途切れる。
# news_service が落ちていても番組(音)は流れ続けなければならないので、
# 失敗しても握り潰す。
event_url = "http://127.0.0.1:8765/api/track_event"

def notify_track(m) =
  # json.stringify に連想リストを渡すと JSON の「配列」になってしまう
  # ([["event","track_start"],…])。オブジェクトが欲しいので組み立てる。
  # 文字列としてのエスケープ (パス中の " や \) は json.stringify に任せる。
  payload =
    '{"event":"track_start","filename":'
    ^ json.stringify(m["filename"])
    ^ '}'
  thread.run(fast=false, fun () -> begin
    ignore(
      http.post(
        headers=[("Content-Type", "application/json")],
        data=payload,
        timeout=5.,
        event_url
      )
    )
  end)
end

# 通知はキューに直接ぶら下げる。下の blank() 側に付けると、無音の 1 秒
# トラックごとに POST が飛んでしまう。
# synchronous=false: ハンドラをストリーミングのループの外で呼ばせる。
# 中で thread.run しているので実害は無いはずだが、二重に守っておく。
program.on_track(synchronous=false, notify_track)

# ---- 位置取得 ------------------------------------------------------------
#   telnet: program.pos → "<残り秒>"
# 何も鳴っていなければ負値が返る。Python 側はこれを補助的な確認に使う
# (主たる進行判断は on_track + wav の長さ)。
server.register(
  namespace = "program",
  description = "再生中トラックの残り秒",
  "pos",
  fun (_) -> "#{source.remaining(program)}"
)

# ---- キューが空のときの無音 ----------------------------------------------
# キューが枯れた瞬間に出力が落ちると Icecast のリスナーが切断される。
# blank を後ろに置いて無音を流し続ける。duration=1. にしているのは、
# track_sensitive=true の fallback が「トラックの切れ目」でしか切り替わらず、
# 無限長の blank だと二度とキューに戻れなくなるため。
radio = fallback(track_sensitive=true, [program, blank(duration=1.)])

# ---- PAUSE ---------------------------------------------------------------
# Liquidsoap 2.4 の output には .stop / .start が無い (1.x にはあった)。
# 代わりに「radio を pull しない」ことで一時停止する。Liquidsoap はプル型
# なので、選択されていないソースはフレームを要求されず再生位置ごと凍結する。
# PAUSE 中も Icecast には無音が流れるのでリスナーは切断されない。
#
#   telnet: var.set paused = true / false
paused = interactive.bool("paused", false)

stream = switch(track_sensitive=false, [(paused, blank()), ({true}, radio)])

# ---- 出力 ----------------------------------------------------------------

output.icecast(
  %mp3(bitrate = 128),
  id = "radio_ice",
  host = "localhost",
  # 8000 は表示系が使うため 8100 (config/icecast.xml と揃える)
  port = 8100,
  password = "hackme",
  mount = "radio.mp3",
  name = "AIradio",
  description = "AIニュースラジオ局",
  fallible = true,
  stream
)

output.pulseaudio(id = "radio_local", fallible = true, stream)
