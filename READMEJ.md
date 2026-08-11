# AIradio — AIニュースラジオ局

GIGAZINE のニュースを AI が要約して読み上げ、ローカル生成した Lo-Fi BGM を
挟みながら 24 時間回し続けるネットラジオ。すべてローカルで完結する
(ニュースの取得だけがオンライン処理で、それは別プロジェクトの Gcrawler が担う)。

このファイルは **git clone から起動までの手順書**。設計の理由・実測値・
性能の話は [TECHNICALJ.md](TECHNICALJ.md)。
English: [README.md](README.md) / [TECHNICAL.md](TECHNICAL.md)。

配信・表示・VOICEVOX・llama-server 連携は [AIjukebox](https://github.com/kotetsuy/AIjukebox) の資産を
流用している。

```
起動 ─→ INTRO (自己紹介) ─→ SPEAKING (ニュース 約30秒)
                                 ↓
                    BGM プールに曲がある? ──yes──→ MUSIC_PLAYING (30秒)
                                 │no                      │
                                 ▼                        │
                            FILLER_LOOP                   │
                     相槌 → ショートトーク → 相槌 …        │
                     BGM が届いたら次の切れ目で音楽へ ─────┘
                                                          │
                                        ← ← ← ← ← ← ← ← ←┘
                                   (音楽中に次の原稿を生成)
```

---

## 必要なもの

| | |
|---|---|
| OS | Ubuntu 26.04 (resolute) |
| GPU | AMD Ryzen AI Max+ 395 / Radeon 8060S (gfx1151) |
| Python | 3.12 以上と [uv](https://docs.astral.sh/uv/) |
| Liquidsoap | 2.4 (**2.x 必須**。1.x とは API 名が大きく違う) |
| Icecast | 2.5 |
| LLM | Qwen3.6-35B-A3B (llama-server, gfx1151 ネイティブビルド) |
| TTS | VOICEVOX ENGINE (docker) |
| BGM | [HeartMuLa](https://github.com/kotetsuy/heartlib) (`~/heartlib/.venv` で動かす) |
| その他 | ffmpeg (ラウドネス正規化), tmux, docker, google-chrome (任意) |

```bash
sudo apt install liquidsoap icecast2 ffmpeg tmux
```

`start_all.sh` は起動前にこれらの存在を確認し、足りなければ理由を出して止まる。

---

## セットアップ

### 1. AIradio

```bash
git clone <このリポジトリ> AIradio
cd AIradio

uv venv && uv pip install httpx aiohttp
```

**`bgm_worker.py` だけはこの venv では動かない。** torch (ROCm/gfx1151) が要るので
`~/heartlib/.venv` の python で起動する (`start_all.sh` がやる)。
AIradio 側の venv に torch は入れないこと。

### 2. アバターと設定

```bash
# 個人環境の値 (Gcrawler の場所、Icecast のパスワード等) を上書きしたい場合
cp config/settings.local.toml.example config/settings.local.toml
```

`vroid/dj.vrm` にアバターの VRM を置く。無くても音は出る (警告が出るだけ)。

> **⚠ Icecast のパスワードを変えること。**
> リポジトリには Icecast の既定値 `hackme` が入ったままになっている。
> `config/icecast.xml` は `0.0.0.0` で待ち受ける (LAN のスマホから聴くため) ので、
> **同じ LAN にいる誰でも admin 画面に入れる。**自宅 LAN の外に出すなら必ず変更する。
>
> **Liquidsoap と Icecast は TOML を読まないので、3 箇所を揃えて変える必要がある。**
>
> | 場所 | 変えるもの |
> |---|---|
> | `config/icecast.xml` | `<source-password>` / `<relay-password>` / `<admin-password>` |
> | `liquidsoap/radio.liq` | `output.icecast` の `password` (= source-password と同じ値) |
> | `config/settings.toml` の `[icecast] password` | 同上。`settings.local.toml` に書けば git に載らない |

### 3. Gcrawler (ニュースの供給元)

**先にこれを回さないと読むニュースが無い。**

```bash
cd ~/Gcrawler
uv sync && uv run playwright install chromium
cp config.toml.example config.toml      # User-Agent の連絡先は書き換えること

./start_llm.sh --bg                     # 要約用の llama-server
uv run --no-sync gigazine_crawler.py    # 既定 10 件
```

詳細は `Gcrawler/READMEJ.md`。

**記事は `[news] exclude_ring` (既定 10 件) より十分多く保つこと。**
記事数がリング以下だと同じニュースを繰り返し読むことになる。
`--limit 10` 以上で**毎日**回すのが前提の設計。cron の例:

```
0 7 * * * cd $HOME/Gcrawler && ./.venv/bin/python gigazine_crawler.py >> crawl.log 2>&1
```

### 4. llama-server と GGUF モデル

`start_all.sh` の先頭で場所を指定している。違う場所に置いている場合は
ここを書き換える。

```bash
LLAMA_BIN="$HOME/llama.cpp/build/bin/llama-server"
QWEN_MODEL="$HOME/AIassistant/qwen3.6/Qwen3.6-35B-A3B-UD-Q4_K_XL.gguf"
```

---

## 起動 / 停止

```bash
./start_all.sh          # VOICEVOX → llama → Icecast → Liquidsoap → bgm_worker → news_service
./stop_all.sh           # 全部止める
./stop_all.sh --keep-llama --keep-voicevox   # 重いものは残す
```

`start_all.sh` は各サービスが応答するまで待ってから次へ進む。
すべて tmux セッション `airadio` の別ウィンドウで走る。

| | |
|---|---|
| 表示系 | <http://localhost:8765/> (Chrome で自動オープン) |
| ネットラジオ | `http://<このマシンのIP>:8100/radio.mp3` |
| ログ | `tmux attach -t airadio` (Ctrl-b d でデタッチ) |

**設定変更は stop → 編集 → start の再起動運用。** ホットリロードは無い。

### 起動できているかの確認

```bash
curl -s localhost:50021/version                    # VOICEVOX
curl -s localhost:8080/health                      # llama-server
curl -s localhost:8100/status-json.xsl | head      # Icecast (source が居ること)
curl -s localhost:8765/ -o /dev/null -w '%{http_code}\n'   # 表示系
```

番組が回っているかは tmux の `program` ウィンドウを見るのが早い。
`{'event': 'state', 'phase': 'SPEAKING'}` → `MUSIC_PLAYING` が
**60 秒前後で交互に出ていれば正常**。

**起動直後 3〜5 分は BGM プールの回復が遅い** (HeartMuLa の 1 本目だけ
カーネル探索で 2〜4 倍かかる)。その間 FILLER_LOOP に入るのは仕様。

---

## 手で動かす

番組進行サービスの標準入力に打てるコマンド: `skip` / `pause` / `play` /
`status` / `quit`。

```bash
uv run --no-sync scripts/news.py                 # 直近3日分のニュース一覧
uv run --no-sync scripts/news.py --pick          # 1件選ぶ
uv run --no-sync scripts/dj_script.py --news     # ニュース原稿を1本生成
uv run --no-sync scripts/dj_script.py --intro    # 自己紹介
uv run --no-sync scripts/voicevox_synth.py --list-speakers
uv run --no-sync scripts/liquidsoap_client.py status
uv run --no-sync scripts/audio.py in.wav out.wav # ラウドネス正規化だけ

# BGM 生成まわりは HeartMuLa の venv で動かす
PYTHONPATH=scripts ~/heartlib/.venv/bin/python scripts/bgm_worker.py --status
PYTHONPATH=scripts ~/heartlib/.venv/bin/python scripts/bgm_worker.py --once
PYTHONPATH=scripts ~/heartlib/.venv/bin/python scripts/bench_phase0.py --clips 3
```

---

## 設定

`config/settings.toml` が唯一の情報源。`config/settings.local.toml` を置くと
**キー単位で再帰的に**上書きされる (テーブルごと潰れない)。
`start_all.sh` もポート等をここから読むので、二重管理にはならない。

よく触るのは以下。

| キー | 既定 | 意味 |
|---|---|---|
| `[news] crawler_dir` | `../Gcrawler/news` | Gcrawler の `news/` の場所 |
| `[news] days` | `3` | 選定対象の日数 |
| `[news] exclude_ring` | `10` | 直近除外するニュース件数 |
| `[bgm] prompts` | Lo-Fi | BGM のスタイル |
| `[bgm] duration_sec` | `30` | BGM の尺 (**上限**。短く終わることがある) |
| `[bgm] min_duration_sec` | `18` | これより短く出来た曲は捨てて作り直す |
| `[bgm] pool_target` | `3` | プール目標在庫数 |
| `[bgm] used_keep` | `100` | `used/` に残す再生済みの曲数（1曲5MB）。0以下で無制限 |
| `[bgm] reuse_count` | `5` | 同じ曲を何サイクル使い回してから捨てるか |
| `[script] max_chars` | `220` | 原稿の長さ (≒30秒) |
| `[script] filler_max_chars` | `60` | ショートトークの長さ (≒5〜10秒) |
| `[loudness] target_lufs` | `-16.0` | 全 wav を揃えるラウドネス |
| `[voicevox] speaker_name` | `波音リツ` | DJ の声。規約で必要なクレジット表示にも使う |
| `[dj] program_name` / `persona` | | 番組名と DJ 設定 |
| `[dj] name` | `ディージェー` | 番組内で名乗る名前。`speaker_name` とは別（後者は話者検索とクレジット表示に使う） |

**`[bgm] reuse_count` は下げないこと。** BGM の生成は実時間の約 4.7 倍かかり、
1 サイクル 1 曲では供給が追いつかない。1 にすると FILLER_LOOP が常態化する。
理由と実測は [TECHNICALJ.md](TECHNICALJ.md) §6。

BGM のプロンプトか尺を変えると、bgm_worker が次の起動時に**プール内の
古いスタイルの曲を捨てる** (ファイル名にプロンプトの指紋が入っている)。

---

## ファイルの置き場

```
cache/bgm_pool/       生成済み BGM (再生待ち)
cache/bgm_pool/used/  再生済み。[bgm] used_keep 曲だけ残して古いものから消える
cache/fillers/        相槌 wav (起動時に事前生成、以後使い回し)
cache/scripts_tts/    ニュース原稿とショートトークの wav
db/state.json         除外リングと BGM の再生回数 (再起動をまたいで持ち越す)
logs/program.log      生成した原稿の JSONL。直近の言い回しの重複回避に使う
logs/bench_phase0.json  Phase 0 ベンチの生データ
```

`Gcrawler/news/` の掃除は Gcrawler 側の責務。AIradio は直近 N 日ぶんを
読むだけで、消しには行かない。

---

## 困ったとき

| 症状 | 見るところ |
|---|---|
| FILLER_LOOP から抜けない | `tmux attach -t airadio` の `bgm` ウィンドウ。HeartMuLa が生成できているか。起動直後 3〜5 分なら仕様 |
| 同じニュースばかり読む | 記事数が `[news] exclude_ring` 以下。Gcrawler を `--limit 10` 以上で回す |
| 音が出ない / 無音が続く | Icecast に source が居るか (`curl -s localhost:8100/status-json.xsl`)。Liquidsoap ウィンドウのエラー |
| 喋らず BGM だけ流れる | VOICEVOX が落ちている (退避動作)。`docker ps` と `curl localhost:50021/version` |
| 起動時に INTRO が 1 秒で切れる | 既知の落とし穴。`flush_if_busy()` で対処済み ([TECHNICALJ.md](TECHNICALJ.md) §4) |
| `hipErrorInvalidImage` 等 | `HSA_OVERRIDE_GFX_VERSION` を設定していないか確認。gfx1151 では**設定してはいけない** |
| 依存が壊れた | `uv run` ではなく `uv run --no-sync` を使うこと (`uv sync` が venv を作り直す) |

`vroid/dj.vrm` が無い、`google-chrome` が無い、`~/heartlib/.venv` が無いは
いずれも警告どまりで起動は続く (それぞれアバター無し / 手動でブラウザを開く /
BGM 生成無しになる)。

---

## ライセンス

[Apache License 2.0](LICENSE)。Copyright 2026 Kotetsu Yamamoto。

`web/libs/` に同梱している three.js と @pixiv/three-vrm は、それぞれの
MIT ライセンスのまま無改変で再配布しているもので、**上記 Apache ライセンスの
対象外**。全一覧と、実行時に使うサービスの利用条件は [NOTICE](NOTICE) にある。
とくに **VOICEVOX は合成音声を公開する際に話者のクレジット表示が必要**で、
これは `web/` の表示系が行っている。

ニュース記事の本文・要約・スクリーンショットはこのリポジトリに含まれない
(実行時に取得するもので、権利は発行元にある)。
