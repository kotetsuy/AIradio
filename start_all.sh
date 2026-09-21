#!/usr/bin/env bash
# AIradio 一括起動スクリプト。
#
# 起動順 (前のものが上がってから次に進む):
#   1. VOICEVOX ENGINE (docker)   :50021
#   2. llama-server (Qwen3.6)     :9931
#   3. Icecast                    :8100   ← config/icecast.xml (sudo不要)
#   4. Liquidsoap                 :1234 (telnet) → Icecast へ配信
#   5. 相槌の事前生成 (無ければ作る。あればスキップ)
#   6. bgm_worker                 ← HeartMuLa 側の venv で走る
#   7. news_service               :8765   ← 表示系 + WebSocket + track_event
#   8. Chrome で表示系を開く
#
# 各サービスは tmux セッション "airadio" の別ウィンドウで走る。
#   tmux attach -t airadio   (ログを見る)
#   ./stop_all.sh            (全部止める)
#
# ポートは config/settings.toml を唯一の情報源として読む。
# 設定を変えたときは stop → 編集 → start。ホットリロードは無い (TECHNICALJ.md §2-3)。

set -euo pipefail

cd "$(dirname "$(readlink -f "$0")")"

SESSION="airadio"

LLAMA_BIN="$HOME/llama.cpp/build/bin/llama-server"
QWEN_MODEL="$HOME/AIassistant/qwen3.6/Qwen3.6-35B-A3B-UD-Q4_K_XL.gguf"
LLAMA_CTX="8192"
LLAMA_NGL="99"

VOICEVOX_CONTAINER="voicevox_engine"
VOICEVOX_IMAGE="voicevox/voicevox_engine:cpu-ubuntu20.04-latest"

# bgm_worker は torch (ROCm) が要るので HeartMuLa 側の venv で動かす。
# ここを settings.toml の [bgm].python と食い違わせないこと。
HEART_PYTHON="$HOME/heartlib/.venv/bin/python"

# 掃除の保持日数 (cache/bgm_pool/used と cache/scripts_tts)
KEEP_DAYS=3

# Chrome / GUIアプリが PipeWire の pulse ソケットに繋がるようにする。
# Liquidsoap の output.pulseaudio もここを使う。
export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
export PULSE_SERVER="${PULSE_SERVER:-unix:${XDG_RUNTIME_DIR}/pulse/native}"

# gfx1151 (Ryzen AI Max+ 395) 向け ROCm env。
# HSA_OVERRIDE_GFX_VERSION は設定しない。llama.cpp も torch も gfx1151 の
# ネイティブビルドなので override すると壊れる。
unset HSA_OVERRIDE_GFX_VERSION
# このマシンの ROCm 10 配置。別の配置では ROCM_PATH で上書きできる。
export ROCM_PATH="${ROCM_PATH:-/opt/rocm/core-10.0}"
export HIP_VISIBLE_DEVICES="${HIP_VISIBLE_DEVICES:-0}"
export AMDGPU_TARGETS="${AMDGPU_TARGETS:-gfx1151}"

# ---- helpers ------------------------------------------------------------

log()  { printf '\033[1;34m[launch]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[launch]\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31m[launch]\033[0m %s\n' "$*" >&2; exit 1; }

# wait_http <name> <url> <timeout_sec>
wait_http() {
    local name="$1" url="$2" timeout="${3:-120}" start now
    start=$(date +%s)
    log "waiting for ${name} (${url}) ..."
    while true; do
        if curl -sf -o /dev/null -m 2 "$url"; then
            log "  ${name} is up"
            return 0
        fi
        now=$(date +%s)
        (( now - start > timeout )) && die "${name} が ${timeout}s で起動しませんでした"
        sleep 2
    done
}

# wait_tcp <name> <host> <port> <timeout_sec>
wait_tcp() {
    local name="$1" host="$2" port="$3" timeout="${4:-60}" start now
    start=$(date +%s)
    log "waiting for ${name} (${host}:${port}) ..."
    while true; do
        if (exec 3<>"/dev/tcp/${host}/${port}") 2>/dev/null; then
            log "  ${name} is up"
            return 0
        fi
        now=$(date +%s)
        (( now - start > timeout )) && die "${name} が ${timeout}s で起動しませんでした"
        sleep 1
    done
}

# new_window <name> <command>
new_window() {
    tmux new-window -t "$SESSION" -n "$1"
    tmux send-keys -t "${SESSION}:$1" "$2" C-m
}

# ---- preflight ----------------------------------------------------------

command -v tmux       >/dev/null || die "tmux がありません"
command -v docker     >/dev/null || die "docker がありません"
command -v curl       >/dev/null || die "curl がありません"
command -v ffmpeg     >/dev/null || die "ffmpeg がありません (音量の正規化に使う)"
command -v liquidsoap >/dev/null || die "liquidsoap がありません (apt install liquidsoap)"
command -v icecast2   >/dev/null || die "icecast2 がありません (apt install icecast2)"
command -v google-chrome >/dev/null || warn "google-chrome が見つかりません (自動オープンはスキップ)"

[[ -x "$LLAMA_BIN"   ]] || die "llama-server がありません: $LLAMA_BIN"
[[ -f "$QWEN_MODEL"  ]] || die "Qwen モデルがありません: $QWEN_MODEL"
[[ -x .venv/bin/python ]] || die ".venv がありません。先に 'uv venv && uv pip install httpx aiohttp' を実行してください"
[[ -f config/settings.toml ]] || die "config/settings.toml がありません"
[[ -f config/icecast.xml   ]] || die "config/icecast.xml がありません"
[[ -f liquidsoap/radio.liq ]] || die "liquidsoap/radio.liq がありません"
[[ -f vroid/dj.vrm ]] || warn "vroid/dj.vrm がありません (アバターが表示されません)"
[[ -x "$HEART_PYTHON" ]] || warn "HeartMuLa の venv がありません: ${HEART_PYTHON} (BGM 生成なしで起動します)"

# ポートは settings.toml から取る (スクリプトと設定の二重管理を避ける)。
# load_settings 経由なので settings.local.toml の上書きも効く。
SETTINGS_ENV=$(.venv/bin/python - <<'PY'
import shlex
import sys
from urllib.parse import urlsplit
sys.path.insert(0, "scripts")
from common import load_settings, resolve_path
s = load_settings()
u = urlsplit(s["llm"]["base_url"])
if u.scheme != "http" or not u.hostname or u.username or u.password or u.path not in ("", "/") or u.query or u.fragment:
    raise SystemExit("[llm].base_url must be an http://host:port URL")
print(f"LLAMA_HOST={shlex.quote(u.hostname)}")
print(f"LLAMA_PORT={u.port or 80}")
print(f"LLAMA_HEALTH_URL={shlex.quote(s['llm']['base_url'].rstrip('/') + '/health')}")
print(f'ICECAST_PORT={s["icecast"]["port"]}')
print(f'ICECAST_MOUNT={s["icecast"]["mount"]}')
print(f'TELNET_HOST={s["liquidsoap"]["telnet_host"]}')
print(f'TELNET_PORT={s["liquidsoap"]["telnet_port"]}')
print(f'WEB_PORT={s["program"]["websocket_port"]}')
print(f'CRAWLER_DIR={resolve_path(s["news"]["crawler_dir"])}')
PY
) || die "設定の読み込みに失敗しました"
eval "$SETTINGS_ENV"

# 同じポートを使っていた AIjukebox の HTML キャッシュと区別する。
BROWSER_URL="http://localhost:${WEB_PORT}/?app=airadio"

if [[ ! -d "$CRAWLER_DIR" ]]; then
    warn "ニュースがありません: ${CRAWLER_DIR}"
    warn "  先に Gcrawler を実行してください (無い場合は固定アナウンス + BGM で回ります)"
fi

# 既存セッションは作り直す
if tmux has-session -t "$SESSION" 2>/dev/null; then
    log "既存の tmux セッション ${SESSION} を終了します"
    tmux kill-session -t "$SESSION"
fi

mkdir -p logs cache/bgm_pool/used cache/fillers cache/scripts_tts db

# ---- 0. ディスク掃除 -----------------------------------------------------
# 再生済み BGM と原稿 wav は溜まる一方なので、起動のたびに古いものを消す。
# news/ の掃除は Gcrawler 側の責務 (AIradio は直近3日ぶんを読むだけ)。

log "古いキャッシュを掃除します (${KEEP_DAYS}日より前)"
find cache/bgm_pool/used -type f -mtime "+${KEEP_DAYS}" -delete 2>/dev/null || true
find cache/scripts_tts   -type f -mtime "+${KEEP_DAYS}" -delete 2>/dev/null || true

# ---- 1. VOICEVOX (docker) ----------------------------------------------

log "VOICEVOX コンテナ (${VOICEVOX_CONTAINER}) を起動します"
if docker ps --format '{{.Names}}' | grep -qx "$VOICEVOX_CONTAINER"; then
    log "  すでに running"
elif docker ps -a --format '{{.Names}}' | grep -qx "$VOICEVOX_CONTAINER"; then
    docker start "$VOICEVOX_CONTAINER" >/dev/null
else
    log "  コンテナが無いので新規作成します"
    docker run -d --name "$VOICEVOX_CONTAINER" --restart unless-stopped \
        -p 50021:50021 "$VOICEVOX_IMAGE" >/dev/null
fi

tmux new-session -d -s "$SESSION" -n voicevox \
    "docker logs -f --tail 50 ${VOICEVOX_CONTAINER}"

wait_http "VOICEVOX" "http://localhost:50021/version" 60

# ---- 2. llama-server ----------------------------------------------------

# tmux サーバーに古い環境が残っていても、ROCm 10 を優先して使う。
# この LD_LIBRARY_PATH は llama 専用。torch の同梱ライブラリには干渉しない。
printf -v LLAMA_COMMAND '%q ' env -u HSA_OVERRIDE_GFX_VERSION \
    "ROCM_PATH=${ROCM_PATH}" "HIP_VISIBLE_DEVICES=${HIP_VISIBLE_DEVICES}" \
    "LD_LIBRARY_PATH=${ROCM_PATH}/lib:${ROCM_PATH}/lib/llvm/lib${LD_LIBRARY_PATH:+:${LD_LIBRARY_PATH}}" \
    "$LLAMA_BIN" -m "$QWEN_MODEL" --host "$LLAMA_HOST" --port "$LLAMA_PORT" \
    -ngl "$LLAMA_NGL" -c "$LLAMA_CTX" -fit off
new_window "llama" "$LLAMA_COMMAND"

# モデルロードに時間がかかるのでタイムアウト長め
wait_http "llama-server" "$LLAMA_HEALTH_URL" 600

# ---- 3. Icecast ---------------------------------------------------------
# /etc/icecast2 は使わない。config/icecast.xml は chroot / changeowner を
# 使わないのでユーザー権限のまま起動でき、ログもプロジェクト内に出る。

new_window "icecast" "icecast2 -c config/icecast.xml"
wait_http "Icecast" "http://localhost:${ICECAST_PORT}/status.xsl" 30

# ---- 4. Liquidsoap ------------------------------------------------------

new_window "liquidsoap" "liquidsoap liquidsoap/radio.liq"
wait_tcp "Liquidsoap telnet" "$TELNET_HOST" "$TELNET_PORT" 60

# ---- 5. 相槌の事前生成 ---------------------------------------------------
# FILLER_LOOP に入った瞬間に鳴らせるものが無いと、そこがそのまま無音になる。
# 合成済みのものはハッシュ一致でスキップされるので毎回呼んでよい。

log "相槌を用意します"
.venv/bin/python scripts/news_service.py --prepare-fillers \
    || warn "相槌の事前生成に失敗しました (VOICEVOX を確認してください)"

# ---- 6. bgm_worker ------------------------------------------------------
# news_service とは独立プロセス。落ちても番組は FILLER_LOOP で回り続ける。
# llama-server のあとに起動するのは、VRAM の取り合いで先に確保させたいのが
# LLM 側だから (BGM は遅れてもフィラーで繋げるが、原稿が作れないと詰む)。

if [[ -x "$HEART_PYTHON" ]]; then
    new_window "bgm" "env -u HSA_OVERRIDE_GFX_VERSION PYTHONPATH=$(pwd)/scripts ROCM_PATH=${ROCM_PATH} \
HIP_VISIBLE_DEVICES=${HIP_VISIBLE_DEVICES} \
${HEART_PYTHON} scripts/bgm_worker.py"
else
    warn "bgm_worker は起動しません (BGM プールが空のままだと FILLER_LOOP が続きます)"
fi

# ---- 7. news_service (番組進行 + 表示系) --------------------------------

new_window "program" "uv run --no-sync scripts/news_service.py"
wait_http "news_service" "$BROWSER_URL" 180

# ---- 8. Chrome ----------------------------------------------------------

if command -v google-chrome >/dev/null; then
    log "Chrome で ${BROWSER_URL} を開きます"
    google-chrome --new-window "$BROWSER_URL" >/dev/null 2>&1 &
    disown
else
    warn "手動で ${BROWSER_URL} を開いてください"
fi

# ---- done ---------------------------------------------------------------

LAN_IP=$(ip route get 1.1.1.1 2>/dev/null | awk '{print $7; exit}')

cat <<EOF

=========================================================================
 AIradio が起動しました。

   表示系      : ${BROWSER_URL}   ← Chrome で自動オープン
   ネットラジオ: http://${LAN_IP:-<このマシンのIP>}:${ICECAST_PORT}/${ICECAST_MOUNT}
   VOICEVOX    : http://localhost:50021/docs
   llama-server: ${LLAMA_HEALTH_URL}
   Liquidsoap  : telnet ${TELNET_HOST} ${TELNET_PORT}
   ニュース    : ${CRAWLER_DIR}

 tmux:
   tmux attach -t ${SESSION}   (ログを見る / Ctrl-b d でデタッチ)
   ./stop_all.sh               (全部止める)
=========================================================================
EOF
