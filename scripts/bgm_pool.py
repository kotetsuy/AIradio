"""BGM プールのファイル契約。

bgm_worker (HeartMuLa 側の venv) と news_service (AIradio の venv) の唯一の
接点。両方から import されるので **標準ライブラリだけ** に依存させること
(ここに torch を引き込むと news_service が起動しなくなる)。

  cache/bgm_pool/bgm_<プロンプト指紋>_<生成時刻>.wav   再生待ち
  cache/bgm_pool/used/…                                 再生済み
"""

from __future__ import annotations

import hashlib
import os
import shutil
from pathlib import Path

from common import load_state, save_state

# db/state.json 内で再生回数を持つキー。{ファイル名: 再生済み回数}
PLAYS_KEY = "bgm_plays"

# 使用済みを寄せておく場所。すぐ消さないのは、生成が詰まったときに手で戻して
# 延命できるようにするため。古いものの掃除は start_all.sh。
USED_SUBDIR = "used"


def prompt_hash(prompt: str, duration_sec: float) -> str:
    """ファイル名に埋めるプロンプトの指紋。

    プロンプトか尺を変えたら別物として扱う。再起動時にこれが一致しない
    ファイルを消すことで、設定を変えたのに古いスタイルの曲が流れ続ける
    のを防ぐ (TECHNICALJ.md §6)。
    """
    key = f"{prompt}|{duration_sec:g}"
    return hashlib.sha1(key.encode("utf-8")).hexdigest()[:8]


def pool_files(pool_dir: Path) -> list[Path]:
    """再生可能な曲を古い順に返す。生成途中の中間ファイルは除く。"""
    if not pool_dir.is_dir():
        return []
    files = [
        p
        for p in pool_dir.glob("*.wav")
        if p.is_file() and not p.name.endswith((".tmp.wav", ".raw.wav"))
    ]
    return sorted(files, key=lambda p: p.stat().st_mtime)


def take_bgm(pool_dir: Path, state_path: Path, reuse_count: int = 1) -> Path | None:
    """次に流す 1 曲を返す。プールが空なら None。

    reuse_count 回まで同じ曲を使い回してから used/ へ退ける。gfx1151 では
    BGM の生成が実時間の 4〜5 倍かかり、1 サイクル 1 曲では供給が追いつかない
    (logs/bench_phase0.json / TECHNICALJ.md §6)。使い回しは §6.5 の
    「消費レートを下げる」緩和策で、番組の構造には手を入れずに済む。

    取り出すのは常に最古の 1 曲。返す前に mtime を更新するので、
    プールの中を順繰りに回る (同じ曲が連続で 2 回鳴ることはない)。
    """
    files = pool_files(pool_dir)
    if not files:
        return None

    src = files[0]
    state = load_state(state_path)
    # プールから消えた曲の回数は残しておいても意味がないので落とす
    # (プロンプトを変えた再起動で sweep_stale が消した分など)
    alive = {p.name for p in files}
    plays = {k: v for k, v in state.get(PLAYS_KEY, {}).items() if k in alive}
    count = int(plays.get(src.name, 0)) + 1

    if count >= max(1, reuse_count):
        used_dir = pool_dir / USED_SUBDIR
        used_dir.mkdir(parents=True, exist_ok=True)
        dst = used_dir / src.name
        try:
            shutil.move(str(src), str(dst))
        except OSError:
            return None  # ワーカーが書き換えた等。次のサイクルで拾い直す
        plays.pop(src.name, None)
        state[PLAYS_KEY] = plays
        save_state(state_path, state)
        return dst

    plays[src.name] = count
    state[PLAYS_KEY] = plays
    save_state(state_path, state)
    # 使った曲を列の最後尾へ回す (pool_files は mtime の古い順)
    os.utime(src)
    return src


def sweep_stale(pool_dir: Path, valid_hashes: set[str]) -> int:
    """現在のプロンプト指紋と一致しないプール内のファイルを消す。

    bgm_worker が起動時に 1 回だけ呼ぶ。used/ の中には触らない。
    """
    removed = 0
    for path in pool_files(pool_dir):
        # bgm_<hash>_<timestamp>.wav
        parts = path.stem.split("_")
        if len(parts) >= 2 and parts[1] in valid_hashes:
            continue
        path.unlink(missing_ok=True)
        removed += 1
    return removed
