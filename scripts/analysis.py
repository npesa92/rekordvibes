"""Track analysis lanes for rbx (spec C7).

Four lanes, all cached in analysis.db (SQLite sidecar — never master.db):
  grid    — rekordbox's own analysis (ANLZ files): beat grid, phrase
            structure, waveform energy curve. Read-only, near-instant.
  rhythm  — drum-pattern fingerprint: percussive onsets snapped to the
            rekordbox beat grid, per-bar 16-step patterns in 2 bands.
  timbre  — sound-texture vector (MFCC + spectral stats).
  audioid — chromaprint fingerprint (needs fpcalc; optional).

Audio files are only ever READ. All caching keyed on (content_id, file mtime).
"""

import json
import sqlite3
import subprocess
from pathlib import Path

import rbxpaths

SKILL_DIR = Path(__file__).resolve().parent.parent
CACHE_DB = SKILL_DIR / "analysis.db"

LANES = ["grid", "rhythm", "timbre", "audioid"]

# PSSI phrase-kind labels by mood (Deep Symmetry / crate-digger findings).
PHRASE_LABELS = {
    1: {1: "intro", 2: "up", 3: "down", 5: "chorus", 6: "outro"},          # high
    2: {1: "intro", 2: "verse", 3: "verse", 4: "verse", 5: "verse",
        6: "verse", 7: "verse", 8: "bridge", 9: "chorus", 10: "outro"},    # mid
    3: {1: "intro", 2: "verse", 3: "verse", 4: "verse", 5: "verse",
        6: "verse", 7: "verse", 8: "bridge", 9: "chorus", 10: "outro"},    # low
}

ENERGY_POINTS = 100   # energy curve resolution stored per track
RHYTHM_STEPS = 16     # slots per bar


def open_cache() -> sqlite3.Connection:
    con = sqlite3.connect(CACHE_DB)
    con.execute(
        """CREATE TABLE IF NOT EXISTS analysis (
            content_id TEXT PRIMARY KEY,
            path TEXT,
            mtime REAL,
            grid TEXT, rhythm TEXT, timbre TEXT, audioid TEXT,
            errors TEXT
        )"""
    )
    con.commit()
    return con


def cache_row(con, cid: str):
    cur = con.execute("SELECT * FROM analysis WHERE content_id=?", (str(cid),))
    cur.row_factory = sqlite3.Row
    return cur.fetchone()


def file_mtime(path: str) -> float | None:
    try:
        return Path(path).stat().st_mtime
    except OSError:
        return None


# ---------------------------------------------------------------- grid lane


def _anlz_files(content):
    from pyrekordbox.anlz import get_anlz_paths, AnlzFile

    if not content.AnalysisDataPath:
        return {}
    d = rbxpaths.rb_share() / Path(content.AnalysisDataPath.strip("\\/")).parent
    out = {}
    for kind, p in get_anlz_paths(d).items():
        if p:
            try:
                out[kind] = AnlzFile.parse_file(p)
            except Exception:
                pass
    return out


def analyze_grid(content) -> dict:
    """Beat grid + phrases + energy curve from rekordbox's ANLZ files."""
    files = _anlz_files(content)
    if not files:
        raise RuntimeError("no ANLZ analysis files (track not analyzed by rekordbox)")
    out = {}

    # beat grid: prefer DAT PQTZ (full grid), fall back to EXT PQT2
    grid_tag = None
    for kind in ("DAT", "EXT"):
        f = files.get(kind)
        # NB: never truthiness-test AnlzFile — its __len__ infinitely recurses
        if f is not None and "PQTZ" in f.tag_types:
            grid_tag = f.get_tag("PQTZ")
            break
    if grid_tag is not None:
        beats, bpms, times = grid_tag.get()
        beats = [int(b) for b in beats]
        times = [float(t) for t in times]
        downbeats = [t for b, t in zip(beats, times) if b == 1]
        out["n_beats"] = len(beats)
        out["bpm_median"] = float(sorted(bpms)[len(bpms) // 2]) if len(bpms) else None
        out["bpm_stable"] = bool(len(set(round(float(b), 1) for b in bpms)) <= 1) if len(bpms) else None
        out["first_downbeat"] = downbeats[0] if downbeats else (times[0] if times else None)
        out["downbeats"] = [round(t, 4) for t in downbeats]

    # phrases (PSSI in EXT)
    ext = files.get("EXT")
    if ext is not None and "PSSI" in ext.tag_types:
        try:
            s = ext.get_tag("PSSI").get()
            labels = PHRASE_LABELS.get(int(s.mood), {})
            phrases = [
                {"beat": int(e.beat), "kind": int(e.kind),
                 "label": labels.get(int(e.kind), f"kind{int(e.kind)}")}
                for e in s.entries
            ]
            out["mood"] = int(s.mood)
            out["end_beat"] = int(s.end_beat)
            out["phrases"] = phrases
            # intro length = beats until first non-intro phrase; outro start likewise
            if phrases:
                intro_end = next((p["beat"] for p in phrases if p["label"] != "intro"),
                                 phrases[-1]["beat"])
                out["intro_beats"] = intro_end - phrases[0]["beat"]
                outro = next((p for p in phrases if p["label"] == "outro"), None)
                out["outro_beats"] = (int(s.end_beat) - outro["beat"]) if outro else 0
        except Exception:
            pass

    # energy curve from waveform detail (PWV3 heights), downsampled
    if ext is not None and "PWV3" in ext.tag_types:
        try:
            heights, _colors = ext.get_tag("PWV3").get()
            n = len(heights)
            if n:
                step = max(1, n // ENERGY_POINTS)
                curve = [
                    round(float(sum(int(h) for h in heights[i:i + step])) / step / 31.0, 4)
                    for i in range(0, n, step)
                ][:ENERGY_POINTS]
                out["energy"] = curve
                out["energy_mean"] = round(sum(curve) / len(curve), 4)
        except Exception:
            pass

    if not out:
        raise RuntimeError("ANLZ files present but no usable tags")
    return out


# ---------------------------------------------------------------- rhythm lane


def analyze_rhythm(path: str, grid: dict) -> dict:
    """Per-bar drum patterns: percussive onsets snapped to the rekordbox grid.

    Two bands: low (kick) and mid/high (snare/hat). Each is a 16-slot vector
    of mean onset strength per 16th-note slot, plus a binarized dominant
    pattern string like 'X..x..X.........'.
    """
    import numpy as np
    import librosa

    downbeats = grid.get("downbeats")
    if not downbeats or len(downbeats) < 8:
        raise RuntimeError("no usable beat grid for rhythm lane")

    y, sr = librosa.load(path, sr=22050, mono=True)
    # percussive component
    y_perc = librosa.effects.percussive(y, margin=3.0)
    hop = 256

    def onset_env(sig):
        return librosa.onset.onset_strength(y=sig, sr=sr, hop_length=hop)

    # low band (kick) vs full percussive (snare/hats dominate strength)
    y_low = librosa.effects.preemphasis(y_perc, coef=0.0)  # placeholder chain
    sos_low = None
    try:
        from scipy.signal import butter, sosfilt

        sos_low = butter(4, 150, btype="lowpass", fs=sr, output="sos")
        y_low = sosfilt(sos_low, y_perc)
        sos_high = butter(4, 300, btype="highpass", fs=sr, output="sos")
        y_high = sosfilt(sos_high, y_perc)
    except Exception:
        y_low, y_high = y_perc, y_perc

    env_low = onset_env(y_low)
    env_high = onset_env(y_high)
    frame_t = librosa.frames_to_time(np.arange(len(env_low)), sr=sr, hop_length=hop)

    def bar_patterns(env):
        bars = []
        for i in range(len(downbeats) - 1):
            t0, t1 = downbeats[i], downbeats[i + 1]
            if t1 <= t0:
                continue
            slots = np.zeros(RHYTHM_STEPS)
            idx = np.searchsorted(frame_t, [t0 + (t1 - t0) * k / RHYTHM_STEPS
                                            for k in range(RHYTHM_STEPS + 1)])
            for k in range(RHYTHM_STEPS):
                seg = env[idx[k]:idx[k + 1]]
                if len(seg):
                    slots[k] = float(seg.max())
            m = slots.max()
            if m > 0:
                bars.append(slots / m)
        return np.array(bars)

    out = {}
    for band, env in (("low", env_low), ("high", env_high)):
        bars = bar_patterns(env)
        if not len(bars):
            raise RuntimeError("no bars extracted")
        mean_vec = bars.mean(axis=0)
        binary = (bars > 0.5).mean(axis=0)  # fraction of bars where slot is a hit
        pattern = "".join(
            "X" if f > 0.6 else ("x" if f > 0.3 else ".") for f in binary
        )
        out[band] = {
            "vector": [round(float(v), 4) for v in mean_vec],
            "pattern": pattern,
        }
    out["n_bars"] = int(len(downbeats) - 1)
    return out


# ---------------------------------------------------------------- timbre lane


def analyze_timbre(path: str) -> dict:
    """Sound-texture vector: MFCC mean+std plus spectral stats (tempo-free)."""
    import numpy as np
    import librosa

    y, sr = librosa.load(path, sr=22050, mono=True)
    mfcc = librosa.feature.mfcc(y=y, sr=sr, n_mfcc=13)
    cent = librosa.feature.spectral_centroid(y=y, sr=sr)
    roll = librosa.feature.spectral_rolloff(y=y, sr=sr)
    flat = librosa.feature.spectral_flatness(y=y)
    zcr = librosa.feature.zero_crossing_rate(y)
    vec = np.concatenate([
        mfcc.mean(axis=1), mfcc.std(axis=1),
        [cent.mean() / (sr / 2), roll.mean() / (sr / 2),
         float(flat.mean()), float(zcr.mean())],
    ])
    return {"vector": [round(float(v), 5) for v in vec]}


# ---------------------------------------------------------------- audioid lane


def fpcalc_path() -> str | None:
    return rbxpaths.fpcalc_path()  # env -> config -> which -> brew locations


def analyze_audioid(path: str) -> dict:
    fp = fpcalc_path()
    if not fp:
        raise RuntimeError("fpcalc not installed (brew install chromaprint)")
    r = subprocess.run([fp, "-json", "-length", "120", path],
                      capture_output=True, text=True, timeout=120)
    if r.returncode != 0:
        raise RuntimeError(f"fpcalc failed: {r.stderr.strip()[:120]}")
    data = json.loads(r.stdout)
    return {"duration": data.get("duration"), "fingerprint": data.get("fingerprint")}


# ---------------------------------------------------------------- distances


def cosine(a, b) -> float:
    import numpy as np

    a, b = np.asarray(a, dtype=float), np.asarray(b, dtype=float)
    na, nb = np.linalg.norm(a), np.linalg.norm(b)
    if na == 0 or nb == 0:
        return 1.0
    return float(1.0 - np.dot(a, b) / (na * nb))


def rhythm_distance(r1: dict, r2: dict) -> float:
    """Rotation-free distance over both bands (bars already grid-aligned)."""
    d = 0.0
    for band in ("low", "high"):
        d += cosine(r1[band]["vector"], r2[band]["vector"])
    return d / 2


def timbre_distance(t1: dict, t2: dict) -> float:
    return cosine(t1["vector"], t2["vector"])


def audioid_similarity(f1: str, f2: str) -> float:
    """Fraction of matching chromaprint ints (rough but effective for dupes)."""
    try:
        import base64  # noqa: F401  (fingerprints are base64 strings; compare raw)
    except ImportError:
        pass
    if not f1 or not f2:
        return 0.0
    n = min(len(f1), len(f2))
    if n == 0:
        return 0.0
    same = sum(1 for a, b in zip(f1[:n], f2[:n]) if a == b)
    return same / n
