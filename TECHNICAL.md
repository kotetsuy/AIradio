# AIradio technical notes

Why things are built the way they are, what was measured, and what those
measurements settled. For setup and operation see [README.md](README.md).
日本語版: [TECHNICALJ.md](TECHNICALJ.md)。

Section numbers match [TECHNICALJ.md](TECHNICALJ.md); source comments cite the
Japanese file.

---

## 1. The shape of the system

```
  Gcrawler/news/YYYY-MM-DD/*.json     ← RSS + screenshot + AI summary
          │  file contract only — no IPC, no shared process
  ┌───────▼──────────────────────────────────────────────┐
  │ news_service.py   the conductor (asyncio)             │
  │   pick a story → Qwen3.6 writes it → VOICEVOX speaks  │
  └───────┬───────────────────────────┬──────────────────┘
          │ push to the program queue │ WebSocket
  ┌───────▼────────┐                  │  subtitles, background, lip-sync
  │   Liquidsoap   │  on_track ───────┤
  │  (one queue)   │  HTTP POST       │
  └───────┬────────┘                  │
          │                    ┌──────▼────────┐
  ┌───────▼────────┐           │  web/ (VRM)   │
  │    Icecast     │           └───────────────┘
  │ :8100/radio.mp3│
  └────────────────┘
          ▲
  ┌───────┴────────┐
  │  bgm_worker.py │  HeartMuLa keeps generating music → cache/bgm_pool/
  └────────────────┘  (separate process; the show survives its death)
```

### Processes

| Process | venv | Role |
|---|---|---|
| `news_service.py` | `AIradio/.venv` | Show logic, script generation, TTS, display HTTP/WebSocket (:8765) |
| `bgm_worker.py` | `~/heartlib/.venv` | Generates music with HeartMuLa into the pool |
| llama-server | — | Script generation (:8080) |
| VOICEVOX ENGINE | docker | TTS (:50021) |
| Liquidsoap | — | Plays whatever is queued, in order (telnet :1234) |
| Icecast | — | Streaming (:8100) |

**Only `bgm_worker` runs in the other venv.** It needs torch (ROCm/gfx1151);
keeping torch out of `AIradio/.venv` keeps `news_service` fast to start and
avoids importing the ROCm load-order problem into it. The single point of
contact is the file contract in `scripts/bgm_pool.py`, so **that module must
depend on the standard library only**.

---

## 2. Settled decisions (do not revisit)

1. `news_service.py` (asyncio) holds all control. **Liquidsoap only plays what
   it is handed**, in order, and makes no decisions
2. Music is generated independently of the news. Pool-buffer model
3. Lo-fi by default. The prompt lives in `settings.toml` and takes effect on
   **restart** — hot reload is deliberately not implemented
4. Story selection is uniform-random over the last N days minus an exclusion ring
5. Gcrawler is a separate project, joined **by files only**
6. Startup begins with a self-introduction (LLM, with a fixed fallback)
7. Fillers are pre-generated interjections plus small talk generated on demand
8. Nothing that fails stops the broadcast (§8)

All AIradio assumes of Gcrawler is that `[news] crawler_dir` contains
`YYYY-MM-DD/*.json` and `*.png`.

---

## 3. The show's state machine

```
start
 └─ INTRO         self-introduction (LLM; no news)
      ↓
 ┌─ SPEAKING      a news script, spoken (~30 s)
 │    ↓ when the speech ends, check the music pool
 │    ├─ pool has a clip → MUSIC_PLAYING
 │    └─ pool empty      → FILLER_LOOP
 │                          interjections + small talk on repeat.
 │                          When music arrives, let the current filler
 │                          finish naturally, then switch
 │    ↓
 ├─ MUSIC_PLAYING music (30 s). Meanwhile the next script is written
 │                (LLM) and synthesized (TTS)
 └────↩ back to SPEAKING when the music ends
```

- **`FILLER_LOOP` is not an error path.** It is how the show waits when the
  pool is empty — insurance against generation jitter and cold start, not the
  main road
- At startup `want` is `news`, so the INTRO is followed by a story rather than
  by music (it was `music` at first, which put a clip right after the intro)

### INTRO

LLM-generated from the program name, DJ persona and today's date.
**A fixed fallback line is mandatory** for when the LLM fails.

### FILLER_LOOP

- The fixed interjections (8 of them) are **pre-generated before startup
  completes** into `cache/fillers/`. If nothing is ready the moment the loop is
  entered, that moment is dead air
- Small talk is generated on demand (LLM + TTS). Generation of the first one
  starts as the loop is entered; interjections cover the gap
- **Keep one piece of small talk to 5–10 seconds** (`[script] filler_max_chars`).
  Longer and the switch back to music becomes sluggish

---

## 4. Liquidsoap integration and track_event

Speech → filler → music is a **single sequential queue**, so if `news_service`
does not know what is playing and when it ends, the decision to check the pool
or push a filler comes too late and **you get dead air**.

### Chosen mechanism: an HTTP callback from Liquidsoap

The `on_track` hook in `radio.liq` POSTs on every track change.

```
POST http://localhost:8765/api/track_event
{ "event": "track_start", "filename": "cache/scripts_tts/news_0012.wav" }
```

- `news_service` identifies what just started from the filename and advances
- Every wav's length is known at synthesis time, so `track_start` + length
  predicts the end. **The decision to push the next item happens
  `[program] prefetch_lead_sec` seconds before that**, so the queue never empties
- Telnet polling is kept for debugging only (`queue_state()`)
- Display events (background, subtitles, lip-sync) are also driven from
  track_event via WebSocket. **Playback and display sync through this one path**

### Trap 1: never send `flush_and_skip` to an empty queue

It is the only flush primitive `request.queue` offers, but calling it while
nothing is playing **books a skip that eats the next item you push**. Found
because the INTRO was cut off after one second. `LiquidsoapClient.flush_if_busy()`
avoids it.

### Trap 2: Liquidsoap's `json.stringify` turns an assoc list into a JSON array

We were sending `[["event","track_start"],…]`. Since an object is wanted, the
string is assembled in `radio.liq` and only escaping is left to `stringify`.

### Trap 3: telnet polling bloats the Liquidsoap log

Two commands per second produced 7000 lines in 25 minutes.
`settings.server.log.level` is now 2, and the two commands share one connection
(`queue_state()`).

### Loudness normalization (mandatory)

VOICEVOX speech and HeartMuLa music sit at very different levels. **Every wav is
normalized with ffmpeg loudnorm (EBU R128) before it is queued**, and the sample
rate is unified at the same time (`[loudness] sample_rate`).

Liquidsoap could normalize instead, but matching levels at the file stage is far
easier to audition and debug.

### The display (web/)

three-vrm with lip-sync, subtitles, WebSocket and the buttons come from
AIjukebox. What AIradio adds:

- **Background**: a screenshot of the story being read. During `MUSIC_PLAYING`
  it **stays on the previous story** — the clip has nothing to do with the news,
  so clearing the background there would just empty the screen of meaning
- **Subtitles**: the news script or filler text
- **Attribution**: `Source: GIGAZINE` and the article title are shown **at all
  times**. The show reads summaries aloud, so the source never leaves the screen
- **Credits**: the VOICEVOX speaker credit, as in AIjukebox
- Buttons are limited to PAUSE/RESUME and SKIP (next story)

Lip-sync offset is corrected with `[program] lipsync_delay_ms` — the lag between
pushing a wav and hearing it.

---

## 5. Story selection

- Candidates are the stories under `news/YYYY-MM-DD/` for the last
  `[news] days` days
- The last `[news] exclude_ring` ids are held in a deque and excluded; one of
  the rest is picked uniformly at random
- The ring is **persisted** to `db/state.json` so a restart does not read the
  same story twice in a row

### The ring does nothing unless there are more stories than ring slots

With only 3 stories available, the ring (10) filled with duplicate ids and
310 cycles read those 3 stories 96–107 times each. Harmless, but inert.

With 13 stories over 143 cycles, **all 13 appeared 9–12 times each and no story
came back sooner than 11 cycles** (mean 13.0) — exactly what a ring of 10 plus
random choice should do.

**Keep comfortably more stories than `[news] exclude_ring`**, which means
running Gcrawler daily with `--limit 10` or more.

---

## 6. The music pool and its economics

This is the tightest part of the system.

### 6.1 Phase 0 benchmark — "30 s in 60 s" does not hold

HeartMuLa's "about 60 seconds for a 30-second clip" is a **standalone** figure.
In production the same GPU is also running llama-server (Qwen3.6-35B-A3B) for
scripts. Measured under that load (2026-08-10 / gfx1151 / raw data in
`logs/bench_phase0.json`):

| Condition | Elapsed (median) | Audio produced (median) | Per second of audio |
|---|---|---|---|
| Standalone | 64.9 s | 22.7 s | 5.36 s |
| Alongside llama-server | 141.6 s | 30.1 s | **4.71 s** |

The scripts generated concurrently: 74 of them, 0 failures, median 2.62 s.
**The LLM side stays usable while music generates**; only the music is starved.
Concurrency costs about +28% and is not the dominant factor.

### 6.2 The requested length is only an upper bound

HeartMuLa stops when `audio_eos` fires. Ask for 30 seconds and clips of 7 or
22 seconds show up routinely. Therefore:

- Dividing elapsed time by the *requested* length is **meaningless**. Divide by
  the length of the wav you actually got
- Counting pool stock in clips overstates the seconds of audio you hold
- The worst case ended at **2.3 seconds**. That is not usable as background
  music, so anything shorter than `[bgm] min_duration_sec` (18 s) is
  **discarded and regenerated**. Since generation time scales with length,
  throwing short clips away costs little

**The discard rate runs 29–37% per session.** Discarded clips average 8.0–9.1 s,
so lowering `min_duration_sec` from 18 to 15 would rescue almost none of them.
Design on the assumption that **one generation in three produces nothing usable**.

### 6.3 The first clip of a process is 2–4× slower

MIOpen kernel search runs on the first one: 250 s versus 141 s afterwards.
This is another reason `bgm_worker` is a long-lived process rather than
reloading the model per clip. **Slow pool recovery right after startup is
expected behavior.**

### 6.4 Generation time is nearly proportional to length

Fitting a line through the two loaded measurements (7.0 s → 53.6 s and
30.1 s → 141.6 s):

```
elapsed ≒ 27 s + 3.8 × seconds of audio
```

A fixed cost of 27 s against a coefficient of 3.8 — which is why stretching the
clips does not help.

### 6.5 Evaluating the mitigations

One cycle is speech (up to 35 s) + music (30 s) = 65 s, consuming one clip,
while one clip takes 141 s to make. **Supply is 0.46× demand.**

| Option | Verdict |
|---|---|
| Raise `pool_target` and bank clips during idle time | **No.** It runs 24/7; there is no idle time to bank from. A deeper pool only delays the drought |
| Stretch clips to 45–60 s to lower the consumption rate | **Backwards.** Generation scales with length (coefficient 3.8), so doubling the clip roughly doubles the generation. Solving `27 + 3.8D ≦ 35 + D` gives `D ≦ 2.8 s` — there is no solution in the longer direction |
| Schedule script and music generation into different windows | **No.** Same reason; there is no window to move to |
| **Replay each clip for several cycles** | **Adopted** (below) |

**Adopted: `[bgm] reuse_count`.** A clip taken from the pool is replayed the
given number of times before being retired to `used/`, dividing the consumption
rate by `reuse_count`.

```
cycles needed = generation / cycle = 141 / 65 = 2.2 → 3 or more
```

Because the music is unrelated to the news (§2-2), a clip reappearing every few
cycles does not break the show. The implementation takes the oldest clip and
touches its mtime, so it walks the pool in rotation — **the same clip never
plays twice in a row**.

Play counts live in `db/state.json` under `bgm_plays`; counts for clips that
have left the pool are dropped on the next `take_bgm`.

### 6.6 Viable options not taken

- **Longer scripts.** The budget is `27 + 3.8D ≦ S + D`. Keeping D=30 requires
  speech S ≧ 111 s (about 800 characters). That changes what the show *is*, so
  it was not taken. For a long-form news program, raise `[script] max_chars`
  and `max_wav_sec` and the budget closes
- **Shorter music.** `duration_sec = 9` or less balances the books, but that is
  a jingle, not background music

### 6.7 Do not measure supply as "runtime ÷ clips accepted"

**This was gotten wrong once.**

`bgm_worker` stops generating once stock reaches `pool_target`. So
"runtime ÷ clips accepted" is a figure **capped by demand**, not a measure of
capacity — whenever stock is stable it necessarily sits near the consumption rate.

Over a 2 h 25 m run, consumption was one clip per 302 s and acceptance one per
298 s, which reads as a 1% margin. In reality **the worker was idle 49% of the
time**.

**Capacity is "time spent generating ÷ clips accepted."**

| Run | Time generating | Accepted | Capacity |
|---|---|---|---|
| 2026-08-11 (2 h 25 m) | 4421 s | 29 | **one per 152 s** |
| 2026-08-10 (5 h 12 m) | ~13500 s | 82 | **one per 165 s** (approx.) |

At `reuse_count = 5` consumption is 5 × ~60 s = **one per ~300 s**, so
**capacity is roughly double demand**. The "5% margin" and "24% margin" in
earlier notes were both understatements caused by this measurement error.

---

## 7. Script generation

- Input is the selected story's `title` + `summary`
- Output is Japanese that reads aloud in **about 30 seconds**. The limit is
  given in the prompt via `[script] max_chars` (220) and then **verified against
  the synthesized wav**: over `[script] max_wav_sec` (35 s) it is rewritten, up
  to `max_regen` times
- Radio-DJ register, using `[dj] persona` and `[voicevox] speaker_name` in the
  prompt
- The source is named naturally in the script ("according to GIGAZINE…")
- The last `[llm] recent_scripts` scripts are attached to the prompt to avoid
  repeating phrasings. Scripts are appended to `logs/program.log` as JSONL

---

## 8. Behavior on failure (never stop the show)

| Failure | Behavior |
|---|---|
| LLM script generation fails | Skip that story, take the next. After repeated failures, fill with a fixed line |
| INTRO generation fails | Play the fixed fallback |
| Music pool empty | `FILLER_LOOP` (a normal path) |
| `bgm_worker` dies | Keep running on `FILLER_LOOP`; make it visible in the log |
| `news/` empty | Fixed announcement plus music only, and log a prompt to run the crawler |
| VOICEVOX down | The DJ cannot speak, so fall back to continuous music |
| Transient TTS/LLM error | Retry about twice, then fall back as above |

---

## 9. Environment traps

### torchaudio 2.9's `save` requires torchcodec

There is no torchcodec wheel for gfx1151, so HeartMuLa's postprocess dies with
an ImportError. `bgm_gen._patch_torchaudio_save()` substitutes the standard
library's `wave`.

**heartlib itself is left untouched** — we want to keep pulling from upstream,
so environment differences are absorbed on the AIradio side.

### Never set `HSA_OVERRIDE_GFX_VERSION`

Both llama.cpp and torch are native gfx1151 builds; overriding the arch breaks
them. `start_all.sh` explicitly unsets it.

### `uv run --no-sync`, never plain `uv run`

`uv sync` rebuilds the venv and replaces the ROCm builds with PyPI ones.

---

## 10. Measured runs

All on gfx1151 (Radeon 8060S) with Qwen3.6-35B-A3B and VOICEVOX in docker
alongside.

| | 2026-08-10 | 2026-08-11 (1) | 2026-08-11 (2) |
|---|---|---|---|
| Duration | 5 h 12 m | 10 m 43 s | 2 h 25 m |
| `reuse_count` | 4 | 5 | 5 |
| Stories available | 3 | 13 | 13 |
| Cycles | 310 | 10 | 143 |
| Cycle length | 60.3 s avg (45–65) | 58.7 s avg (47–64) | 60.8 s avg (49–65) |
| `FILLER_LOOP` | 0 | 0 | 0 |
| Errors | 0 | 0 | 0 |
| Pool stock | 2–3 throughout | 3 → 2 | 3 throughout |
| Music accepted / discarded | 82 / 34 (29%) | 2 / 1 | 29 / 17 (37%) |
| Generation (accepted) | 116.4 s avg | — | 122.3 s avg (115.7–267.4) |
| Worker duty cycle | (not measured) | — | **51%** |

- **The show survives at both 4 and 5.** At 5, `bgm_..._210856.wav` was
  confirmed to play exactly five times before retiring to `used/`
- Generation settles at 115–120 s from the second clip onward; the 267 s figure
  is the first clip of the process (§6.3)
- Loudness measured on the live Icecast stream: **-17.3 LUFS** (target -16)

---

## 11. Open items

- **No cron for Gcrawler yet.** As stories age, fewer fall inside
  `[news] days = 3` and the exclusion ring stops working (§5). Run it daily
- **Nobody has listened to a long run.** Loudness was measured, but no human
  has sat through five hours
- **The cause of HeartMuLa's short-clip cutoff (29–37%) is unknown.** Sweeping
  `tags` / `cfg_scale` / `topk` might improve the yield. **Low priority**,
  since §6.7 shows roughly 2× headroom in capacity
- Not yet a git repository (`.gitignore` is ready)

---

## 12. Re-measuring

Measure again after a hardware or model change. The benchmark reports the
verdict and the required reuse count.

```bash
PYTHONPATH=scripts ~/heartlib/.venv/bin/python scripts/bench_phase0.py --clips 3
cat logs/bench_phase0.json    # required_reuse_count goes straight into settings.toml
```

To check the economics after a run, compute capacity as **"time spent
generating ÷ clips accepted"** (§6.7). In the `bgm_worker` log, "プール N/M →
生成します" marks a start and "生成:" / "短すぎるので破棄:" mark completion, so
the sum of those intervals is the time spent generating.
