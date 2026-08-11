# AIradio — an AI news radio station

An AI reads summarized GIGAZINE news out loud, with locally generated lo-fi
music between the stories, streamed as internet radio around the clock.
Everything runs locally; the only online step is fetching the news, and that is
a separate project ([Gcrawler](https://github.com/kotetsuy/Gcrawler)) connected by nothing but files on disk.

This file is the **setup guide, from git clone to a running station**. The
reasoning, measurements and performance notes are in [TECHNICAL.md](TECHNICAL.md).
日本語版: [READMEJ.md](READMEJ.md)。

Streaming, the VRM display, VOICEVOX and llama-server integration are reused
from [AIjukebox](https://github.com/kotetsuy/AIjukebox).

```
start ─→ INTRO (self-introduction) ─→ SPEAKING (a news story, ~30 s)
                                          ↓
                          music in the pool? ──yes──→ MUSIC_PLAYING (30 s)
                                          │no                  │
                                          ▼                    │
                                    FILLER_LOOP                │
                          interjection → small talk → …        │
                          switch to music at the next gap ─────┘
                                                               │
                                          ← ← ← ← ← ← ← ← ← ← ←┘
                                (the next script is written during the music)
```

---

## Requirements

| | |
|---|---|
| OS | Ubuntu 26.04 (resolute) |
| GPU | AMD Ryzen AI Max+ 395 / Radeon 8060S (gfx1151) |
| Python | 3.12+ and [uv](https://docs.astral.sh/uv/) |
| Liquidsoap | 2.4 (**2.x required** — the 1.x API names differ substantially) |
| Icecast | 2.5 |
| LLM | Qwen3.6-35B-A3B via llama-server (native gfx1151 build) |
| TTS | VOICEVOX ENGINE (docker) |
| Music | [HeartMuLa](https://github.com/kotetsuy/heartlib), run from `~/heartlib/.venv` |
| Also | ffmpeg (loudness), tmux, docker, google-chrome (optional) |

```bash
sudo apt install liquidsoap icecast2 ffmpeg tmux
```

`start_all.sh` checks for all of these and stops with a reason if any is missing.

---

## Setup

### 1. AIradio

```bash
git clone <this repository> AIradio
cd AIradio

uv venv && uv pip install httpx aiohttp
```

**`bgm_worker.py` does not run in this venv.** It needs torch (ROCm/gfx1151), so
it is launched with `~/heartlib/.venv`'s python (`start_all.sh` handles it).
Do not add torch to AIradio's venv.

### 2. Avatar and settings

```bash
# optional: override machine-specific values (where Gcrawler lives, Icecast password, …)
cp config/settings.local.toml.example config/settings.local.toml
```

Put a VRM avatar at `vroid/dj.vrm`. The audio works without it (you just get a
warning).

> **⚠ Change the Icecast password.**
> The repository still carries Icecast's default, `hackme`. Since
> `config/icecast.xml` listens on `0.0.0.0` (so phones on the LAN can tune in),
> **anyone on the same network can reach the admin page.** Change it before
> exposing this beyond your home network.
>
> **Liquidsoap and Icecast do not read the TOML, so three places must agree:**
>
> | Where | What |
> |---|---|
> | `config/icecast.xml` | `<source-password>` / `<relay-password>` / `<admin-password>` |
> | `liquidsoap/radio.liq` | `password` in `output.icecast` (same value as source-password) |
> | `config/settings.toml`, `[icecast] password` | same again — put it in `settings.local.toml` to keep it out of git |

### 3. Gcrawler (where the news comes from)

**Run this first — without it there is nothing to read.**

```bash
cd ~/Gcrawler
uv sync && uv run playwright install chromium
cp config.toml.example config.toml      # do change the contact in the User-Agent

./start_llm.sh --bg                     # llama-server for the summaries
uv run --no-sync gigazine_crawler.py    # 10 stories by default
```

See `Gcrawler/README.md` for details.

**Keep comfortably more stories than `[news] exclude_ring` (10 by default).**
With fewer stories than the ring, the same news gets read over and over. The
design assumes a **daily** run with `--limit 10` or more:

```
0 7 * * * cd $HOME/Gcrawler && ./.venv/bin/python gigazine_crawler.py >> crawl.log 2>&1
```

### 4. llama-server and the GGUF model

Their locations are set at the top of `start_all.sh`. Edit them if yours differ.

```bash
LLAMA_BIN="$HOME/llama.cpp/build/bin/llama-server"
QWEN_MODEL="$HOME/AIassistant/qwen3.6/Qwen3.6-35B-A3B-UD-Q4_K_XL.gguf"
```

---

## Running

```bash
./start_all.sh          # VOICEVOX → llama → Icecast → Liquidsoap → bgm_worker → news_service
./stop_all.sh
./stop_all.sh --keep-llama --keep-voicevox   # leave the heavy things up
```

`start_all.sh` waits for each service to answer before starting the next.
Everything runs in its own window of the tmux session `airadio`.

| | |
|---|---|
| Display | <http://localhost:8765/> (opened in Chrome automatically) |
| Radio | `http://<this machine's IP>:8100/radio.mp3` |
| Logs | `tmux attach -t airadio` (Ctrl-b d to detach) |

**Changing settings means stop → edit → start.** There is no hot reload.

### Checking that it came up

```bash
curl -s localhost:50021/version                    # VOICEVOX
curl -s localhost:8080/health                      # llama-server
curl -s localhost:8100/status-json.xsl | head      # Icecast (a source must be connected)
curl -s localhost:8765/ -o /dev/null -w '%{http_code}\n'   # display
```

The quickest check that the show is running is the `program` window in tmux.
`{'event': 'state', 'phase': 'SPEAKING'}` alternating with `MUSIC_PLAYING`
**roughly every 60 seconds is healthy**.

**The pool recovers slowly for the first 3–5 minutes** — HeartMuLa's first clip
takes 2–4× longer because of kernel search. Dropping into `FILLER_LOOP` during
that window is expected.

---

## Driving it by hand

Commands accepted on `news_service`'s stdin: `skip` / `pause` / `play` /
`status` / `quit`.

```bash
uv run --no-sync scripts/news.py                 # stories from the last 3 days
uv run --no-sync scripts/news.py --pick          # pick one
uv run --no-sync scripts/dj_script.py --news     # write one script
uv run --no-sync scripts/dj_script.py --intro    # the self-introduction
uv run --no-sync scripts/voicevox_synth.py --list-speakers
uv run --no-sync scripts/liquidsoap_client.py status
uv run --no-sync scripts/audio.py in.wav out.wav # loudness normalization only

# anything touching HeartMuLa runs from its own venv
PYTHONPATH=scripts ~/heartlib/.venv/bin/python scripts/bgm_worker.py --status
PYTHONPATH=scripts ~/heartlib/.venv/bin/python scripts/bgm_worker.py --once
PYTHONPATH=scripts ~/heartlib/.venv/bin/python scripts/bench_phase0.py --clips 3
```

---

## Configuration

`config/settings.toml` is the single source of truth. Drop a
`config/settings.local.toml` next to it and it is merged **key by key** (tables
are not replaced wholesale). `start_all.sh` reads ports from it too, so there is
no second copy to keep in sync.

| Key | Default | Meaning |
|---|---|---|
| `[news] crawler_dir` | `../Gcrawler/news` | where Gcrawler's `news/` lives |
| `[news] days` | `3` | how far back to look |
| `[news] exclude_ring` | `10` | how many recent stories to skip |
| `[bgm] prompts` | lo-fi | music style |
| `[bgm] duration_sec` | `30` | clip length (**an upper bound** — clips often end early) |
| `[bgm] min_duration_sec` | `18` | clips shorter than this are discarded and regenerated |
| `[bgm] pool_target` | `3` | target stock in the pool |
| `[bgm] reuse_count` | `5` | how many cycles one clip is replayed before retiring |
| `[script] max_chars` | `220` | script length (≈30 s) |
| `[script] filler_max_chars` | `60` | small-talk length (≈5–10 s) |
| `[loudness] target_lufs` | `-16.0` | the level every wav is normalized to |
| `[voicevox] speaker_name` | `波音リツ` | the DJ's voice; also goes into the prompt |
| `[dj] program_name` / `persona` | | program name and DJ character |

**Do not lower `[bgm] reuse_count`.** Music generation takes about 4.7× real
time, so one clip per cycle cannot keep up; at 1 the show lives permanently in
`FILLER_LOOP`. The reasoning and measurements are in [TECHNICAL.md](TECHNICAL.md) §6.

Change the music prompt or its length and `bgm_worker` discards the now-stale
clips on its next start (the prompt fingerprint is in the filename).

---

## Where files go

```
cache/bgm_pool/       generated music waiting to be played
cache/bgm_pool/used/  already played; start_all.sh deletes after 3 days
cache/fillers/        interjection wavs, generated once at startup
cache/scripts_tts/    news scripts and small talk
db/state.json         exclusion ring and music play counts (kept across restarts)
logs/program.log      JSONL of everything written, used to avoid repeating phrasings
logs/bench_phase0.json  raw Phase 0 benchmark data
```

Cleaning `Gcrawler/news/` is Gcrawler's job. AIradio only reads the last N days
and never deletes.

---

## Troubleshooting

| Symptom | Where to look |
|---|---|
| Stuck in `FILLER_LOOP` | The `bgm` window in `tmux attach -t airadio` — is HeartMuLa producing? Expected for the first 3–5 minutes |
| The same story over and over | Fewer stories than `[news] exclude_ring`. Run Gcrawler with `--limit 10` or more |
| No audio / long silences | Is a source connected to Icecast (`curl -s localhost:8100/status-json.xsl`)? Check the Liquidsoap window for errors |
| Music but no speech | VOICEVOX is down (fallback mode). Check `docker ps` and `curl localhost:50021/version` |
| The INTRO is cut off after a second | Known trap, already handled by `flush_if_busy()` ([TECHNICAL.md](TECHNICAL.md) §4) |
| `hipErrorInvalidImage` and friends | Check that `HSA_OVERRIDE_GFX_VERSION` is unset. On gfx1151 it **must not** be set |
| Dependencies broke | Use `uv run --no-sync`, not `uv run` (a sync rebuilds the venv) |

A missing `vroid/dj.vrm`, `google-chrome` or `~/heartlib/.venv` is a warning
only and startup continues — you get no avatar, no auto-opened browser, or no
music generation respectively.

---

## License

[Apache License 2.0](LICENSE). Copyright 2026 Kotetsu Yamamoto.

The libraries bundled under `web/libs/` (three.js and @pixiv/three-vrm) are
redistributed unmodified under their own MIT licenses and are **not** covered by
the Apache license above. See [NOTICE](NOTICE) for the full list, along with the
terms attached to the services used at runtime — in particular **VOICEVOX
requires the speaker to be credited whenever synthesized audio is published**,
which the display in `web/` does for you.

No news article text, summaries or screenshots are contained in this repository;
those are fetched at runtime and belong to their publisher.
