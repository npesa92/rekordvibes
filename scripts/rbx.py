#!/usr/bin/env python
"""rbx — rekordbox collection manager.

Manages the local rekordbox 7 library (master.db) safely:
  * metadata cleanup (parse "Artist - Album - 01 Title" pollution out of Title)
  * DB <-> embedded-tag sync (mutagen)
  * additive-only playlist management (baseline playlists are frozen)
  * hygiene doctor (missing files, duplicates, tag drift, untracked files)
  * recommendations, query, export, backup, undo

Hard rules enforced here:
  * audio files are NEVER renamed, moved, or deleted
  * rekordbox must be quit for any write to master.db
  * every write batch: fresh backup first, undo journal after
  * playlists that existed at `rbx init` are read-only forever
  * no writes at all before `rbx init` (the freeze needs a baseline to exist)

Machine-dependent paths (rekordbox dir, fpcalc, process check) resolve via
rbxpaths.py: env var -> config.json -> platform probe. Run `rbx setup` for a
full environment diagnostic.
"""

import argparse
import csv
import html
import json
import re
import shutil
import sys
import unicodedata
from datetime import datetime
from pathlib import Path

import rbxpaths
from rbxpaths import is_local, rekordbox_running  # platform seam lives there

SKILL_DIR = Path(__file__).resolve().parent.parent
UNDO_DIR = SKILL_DIR / "undo-journals"
SNAP_DIR = SKILL_DIR / ".snapshot"
RESOLVE_DIR = SKILL_DIR / "resolve"
AUDIO_EXTS = {".mp3", ".wav", ".aiff", ".aif", ".flac", ".m4a", ".mp4", ".ogg"}

# ---------------------------------------------------------------- infra


def ts() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S")


def require_closed():
    if rekordbox_running():
        sys.exit(
            "REFUSED: rekordbox is running. Quit rekordbox before any write "
            "operation (reads are fine)."
        )


def require_baseline():
    """No writes before `rbx init`: without a baseline manifest the frozen-
    playlist guard sees an empty set and the additive-only protection
    silently doesn't exist."""
    if baseline_dir() is None:
        sys.exit(
            "REFUSED: no baseline for this library — run `rbx init` first.\n"
            "(init snapshots the DB and freezes existing playlists; writes are\n"
            "blocked until then so the baseline protection can't be skipped)"
        )


def _copy_db_files(dest: Path):
    dest.mkdir(parents=True, exist_ok=True)
    for suffix in ("", "-wal", "-shm"):
        src = Path(str(rbxpaths.master()) + suffix)
        if src.exists():
            shutil.copy2(src, dest / src.name)


def backup_now(label: str = "pre-write") -> Path:
    """Timestamped backup of master.db (+wal/shm). Prunes to last 10 pre-write
    backups; baseline-* dirs are never pruned."""
    dest = rbxpaths.backup_dir() / f"{label}-{ts()}"
    _copy_db_files(dest)
    prunable = sorted(d for d in rbxpaths.backup_dir().glob("pre-write-*") if d.is_dir())
    for old in prunable[:-10]:
        shutil.rmtree(old)
    return dest


def open_db(write: bool = False):
    """Open master.db. Reads go through a snapshot copy (safe while rekordbox
    runs); writes open the real DB and require rekordbox closed + a baseline."""
    from pyrekordbox import Rekordbox6Database

    if write:
        require_closed()
        require_baseline()
        return Rekordbox6Database(path=rbxpaths.master())
    if SNAP_DIR.exists():
        shutil.rmtree(SNAP_DIR)
    _copy_db_files(SNAP_DIR)
    return Rekordbox6Database(path=SNAP_DIR / "master.db")


def baseline_dir() -> Path | None:
    dirs = sorted(rbxpaths.backup_dir().glob("baseline-*"))
    return dirs[0] if dirs else None


def load_manifest() -> dict | None:
    b = baseline_dir()
    if b and (b / "manifest.json").exists():
        return json.loads((b / "manifest.json").read_text())
    return None


def frozen_playlist_ids() -> set:
    m = load_manifest()
    return {p["id"] for p in m["playlists"]} if m else set()


def content_by_id(db, cid):
    """pyrekordbox returns the instance directly for ID lookups, a query otherwise."""
    r = db.get_content(ID=cid)
    return r.first() if hasattr(r, "first") else r


def norm(s: str | None) -> str:
    s = unicodedata.normalize("NFKC", s or "").lower()
    return re.sub(r"\s+", " ", s).strip()


# ---------------------------------------------------------------- tag io

ID3_MAP = {"title": "TIT2", "artist": "TPE1", "album": "TALB"}
MP4_MAP = {"title": "\xa9nam", "artist": "\xa9ART", "album": "\xa9alb"}


def read_tags(path: str) -> dict | None:
    """Return {'title','artist','album'} from embedded tags, or None."""
    import mutagen

    try:
        f = mutagen.File(path)
    except Exception:
        return None
    if f is None:
        return None
    out = {}
    tags = f.tags
    if tags is None:
        return {"title": "", "artist": "", "album": ""}
    from mutagen.mp4 import MP4Tags

    if isinstance(tags, MP4Tags):
        for k, frame in MP4_MAP.items():
            v = tags.get(frame)
            out[k] = str(v[0]) if v else ""
    elif hasattr(tags, "getall"):  # ID3 (mp3/wav/aiff)
        for k, frame in ID3_MAP.items():
            v = tags.getall(frame)
            out[k] = str(v[0].text[0]) if v and v[0].text else ""
    else:  # vorbis-comment style (flac/ogg)
        for k in ("title", "artist", "album"):
            v = tags.get(k)
            out[k] = str(v[0]) if v else ""
    return out


def write_tags(path: str, values: dict) -> dict | None:
    """Write {'title','artist','album'} (only keys present) to embedded tags.
    Returns the previous values for the undo journal, or None on failure."""
    import mutagen
    from mutagen.mp4 import MP4Tags

    old = read_tags(path)
    if old is None:
        return None
    try:
        f = mutagen.File(path)
        if f.tags is None:
            f.add_tags()
        tags = f.tags
        if isinstance(tags, MP4Tags):
            for k, v in values.items():
                tags[MP4_MAP[k]] = [v]
        elif hasattr(tags, "getall"):
            from mutagen.id3 import TALB, TIT2, TPE1

            frames = {"title": TIT2, "artist": TPE1, "album": TALB}
            for k, v in values.items():
                tags.setall(ID3_MAP[k], [frames[k](encoding=3, text=[v])])
        else:
            for k, v in values.items():
                tags[k] = [v]
        f.save()
        return old
    except Exception as e:
        print(f"  tag write failed for {path}: {e}", file=sys.stderr)
        return None


# ---------------------------------------------------------------- parser

JUNK_RES = [
    re.compile(r"\s*[\(\[](official\s+(music\s+)?(video|audio)|lyric\s+video|audio|visualizer|hd|hq)[\)\]]", re.I),
    re.compile(r"\s*[\(\[]free\s*(dl|download)?[\)\]]", re.I),
    re.compile(r"\s*[\(\[]?\d{3,4}\s*kbps[\)\]]?", re.I),
    re.compile(r"\.(mp3|wav|aiff?|flac|m4a|ogg)$", re.I),
]
TRACKNUM_RE = re.compile(r"^(\d{1,2})[\s.\-_]+(?=\S)")
REMIX_TAIL_RE = re.compile(r"(remix|flip|edit|bootleg|vip|rework|refix|mashup)\s*$", re.I)
FEAT_RE = re.compile(r"\b(ft\.?|featuring)\s", re.I)
# "(X Flip)" / "(X Remix)" etc. at the end of a title: X is the artist
REMIXER_RE = re.compile(
    r"\(([^()]+?)\s+(remix|flip|bootleg|rework|refix|reboot|mashup)\)\s*$", re.I
)
GENERIC_MIX_WORDS = {"original", "extended", "club", "radio", "instrumental", "official", "shortened", "version", "the", "a"}


def strip_junk(s: str) -> str:
    for rx in JUNK_RES:
        s = rx.sub("", s)
    return re.sub(r"\s+", " ", s).strip(" -_")


def normalize_feat(s: str) -> str:
    return FEAT_RE.sub("feat. ", s)


def propose(content) -> dict | None:
    """Analyze one track; return a proposal dict or None if nothing to fix.

    Proposal: {id, path, cur_title, cur_artist, cur_album, title, artist,
               album, track_no, conf ('high'|'medium'|'low'), reason}
    """
    raw = content.Title or ""
    cur_artist = content.Artist.Name if content.Artist else ""
    cur_album = content.Album.Name if content.Album else ""

    t = normalize_feat(strip_junk(html.unescape(raw)))
    new = {"title": None, "artist": None, "album": None, "track_no": None}
    conf, reason = None, None

    parts = [p.strip() for p in t.split(" - ") if p.strip()]

    # remixer convention: "(X Flip)" / "(X Remix)" at the end => X is the artist
    remixer = None
    rm = REMIXER_RE.search(t)
    if rm:
        name = rm.group(1).strip(" -_")
        words = norm(name).split()
        if words and not all(w in GENERIC_MIX_WORDS for w in words):
            remixer = name

    if remixer and not (cur_artist and norm(remixer) in norm(cur_artist)):
        new["artist"] = remixer
        if t != raw:
            new["title"] = t  # keep full descriptive title, junk-cleaned
        conf = "high" if not cur_artist else "medium"
        reason = f"remixer '({remixer} ...)' is the artist"
    # artist name redundantly appended: "Song Name - Artist"
    elif len(parts) == 2 and cur_artist and norm(parts[1]) == norm(cur_artist):
        new["title"], conf, reason = parts[0], "high", "artist repeated after title"
    # "Artist - Song" where artist matches the artist field
    elif len(parts) == 2 and cur_artist and norm(parts[0]) == norm(cur_artist):
        new["title"], conf, reason = parts[1], "high", "artist repeated before title"
    # bandcamp style: Artist - Album [- ...] - NN Title
    elif len(parts) >= 3 and TRACKNUM_RE.match(parts[-1]):
        m = TRACKNUM_RE.match(parts[-1])
        new["artist"] = parts[0]
        new["album"] = " - ".join(parts[1:-1])
        new["title"] = parts[-1][m.end():].strip()
        new["track_no"] = int(m.group(1))
        conf, reason = "high", "Artist - Album - NN Title"
    # simple "Artist - Title" with empty artist field
    elif len(parts) == 2 and not cur_artist:
        if REMIX_TAIL_RE.search(parts[1]) and "(" not in parts[1]:
            # "Flipper - Song Flip": leading name is the artist (user convention)
            new["artist"], new["title"] = parts[0], parts[1]
            conf, reason = "medium", "A - B Flip: A is the artist"
        else:
            new["artist"], new["title"] = parts[0], parts[1]
            conf, reason = "medium", "Artist - Title (artist field empty)"
    # multi-part, artist known and leads the string
    elif len(parts) >= 3 and cur_artist and norm(parts[0]) == norm(cur_artist):
        new["title"], conf = " - ".join(parts[1:]), "medium"
        reason = "leading artist stripped, rest kept as title"
    elif len(parts) >= 3:
        new["artist"], new["title"] = parts[0], " - ".join(parts[1:])
        conf, reason = "low", "multi-hyphen, best guess"

    # even if no split happened, junk/entity cleanup alone may be worth it
    if conf is None:
        if t != raw:
            new["title"], conf, reason = t, "high", "junk/entity cleanup only"
        else:
            return None
    if new["title"] is not None and t != raw and new["title"] == raw:
        new["title"] = t

    # drop no-op fields
    if new["title"] == raw:
        new["title"] = None
    if new["artist"] and norm(new["artist"]) == norm(cur_artist):
        new["artist"] = None
    if new["album"] and norm(new["album"]) == norm(cur_album):
        new["album"] = None
    if new["track_no"] and content.TrackNo:
        new["track_no"] = None
    if not any(v is not None for v in new.values()):
        return None

    return {
        "id": content.ID,
        "path": content.FolderPath or "",
        "cur_title": raw,
        "cur_artist": cur_artist,
        "cur_album": cur_album,
        **new,
        "conf": conf,
        "reason": reason,
    }


CONF_ORDER = {"high": 0, "medium": 1, "low": 2}


def collect_proposals(db, scope_playlist=None, min_conf="high", limit=None):
    if scope_playlist:
        pl = db.get_playlist(Name=scope_playlist).first()
        if pl is None:
            sys.exit(f"playlist not found: {scope_playlist}")
        contents = [s.Content for s in db.get_playlist_songs(PlaylistID=pl.ID).all()]
    else:
        contents = db.get_content().all()
    props = []
    for c in contents:
        p = propose(c)
        if p and CONF_ORDER[p["conf"]] <= CONF_ORDER[min_conf]:
            props.append(p)
    props.sort(key=lambda p: CONF_ORDER[p["conf"]])
    return props[:limit] if limit else props


def print_proposals(props):
    for p in props:
        print(f"[{p['conf'].upper():6}] id={p['id']}  ({p['reason']})")
        print(f"    title : {p['cur_title']!r}")
        if p["title"] is not None:
            print(f"         -> {p['title']!r}")
        if p["artist"] is not None:
            print(f"    artist: {p['cur_artist']!r} -> {p['artist']!r}")
        if p["album"] is not None:
            print(f"    album : {p['cur_album']!r} -> {p['album']!r}")
        if p["track_no"] is not None:
            print(f"    track#: -> {p['track_no']}")


# ---------------------------------------------------------------- resolve
# Ambiguous cases get exported to JSON, adjudicated by a delegated model
# agent (spawned by Claude via the Agent tool — no API calls from here), and
# re-imported via `clean --verdicts FILE`. The verdicts path reuses the exact
# same dry-run / backup / apply / undo machinery as the parser path.

VERDICT_KEYS = {"id", "title", "artist", "album", "track_no", "conf", "reason", "skip"}


def cmd_resolve(args):
    db = open_db()
    props = collect_proposals(db, args.playlist, "low")
    # everything below high confidence, plus remixer-rule hits at any tier —
    # "(X Flip)" can be inverted (famous source artist in the parens), which
    # only world knowledge can catch.
    cases = [
        p for p in props
        if p["conf"] != "high" or p["reason"].startswith("remixer")
    ]
    if not cases:
        print("No ambiguous cases in scope — nothing to export.")
        return
    RESOLVE_DIR.mkdir(exist_ok=True)
    out = RESOLVE_DIR / f"cases-{ts()}.json"
    payload = {
        "created": datetime.now().isoformat(),
        "scope": args.playlist or "whole collection",
        "note": "Parser proposals for these tracks are uncertain. See "
                "RESOLVE_PROMPT.md for conventions and the verdict schema.",
        "cases": [
            {
                "id": str(p["id"]),
                "path": p["path"],
                "cur_title": p["cur_title"],
                "cur_artist": p["cur_artist"],
                "cur_album": p["cur_album"],
                "parser": {
                    "title": p["title"],
                    "artist": p["artist"],
                    "album": p["album"],
                    "track_no": p["track_no"],
                    "conf": p["conf"],
                    "reason": p["reason"],
                },
            }
            for p in cases
        ],
    }
    out.write_text(json.dumps(payload, indent=2))
    verdicts = out.with_name(out.name.replace("cases-", "verdicts-"))
    print(f"Exported {len(cases)} ambiguous cases -> {out}")
    print(f"Expected verdicts file             -> {verdicts}")
    print("Next: delegate to a model agent (see RESOLVE_PROMPT.md), then")
    print(f"  rbx clean --verdicts {verdicts}")


def load_verdicts(db, path: Path) -> list:
    """Validate a verdicts file and convert entries into proposal dicts.
    Hard-fails on schema problems; silently drops no-ops and skips."""
    try:
        data = json.loads(Path(path).read_text())
    except Exception as e:
        sys.exit(f"cannot read verdicts file: {e}")
    entries = data["verdicts"] if isinstance(data, dict) and "verdicts" in data else data
    if not isinstance(entries, list):
        sys.exit("verdicts file must be a JSON list (or {'verdicts': [...]})")
    props, errors = [], []
    for i, v in enumerate(entries):
        if not isinstance(v, dict) or "id" not in v:
            errors.append(f"entry {i}: not an object with an 'id'")
            continue
        unknown = set(v) - VERDICT_KEYS
        if unknown:
            errors.append(f"entry {i} (id={v.get('id')}): unknown keys {sorted(unknown)}")
            continue
        if v.get("skip"):
            continue
        if v.get("track_no") is not None and not isinstance(v["track_no"], int):
            errors.append(f"entry {i} (id={v['id']}): track_no must be an integer")
            continue
        c = content_by_id(db, str(v["id"]))
        if c is None:
            errors.append(f"entry {i}: id={v['id']} not found in DB")
            continue
        cur_title = c.Title or ""
        cur_artist = c.Artist.Name if c.Artist else ""
        cur_album = c.Album.Name if c.Album else ""
        new = {
            "title": v.get("title"),
            "artist": v.get("artist"),
            "album": v.get("album"),
            "track_no": v.get("track_no"),
        }
        if new["title"] == cur_title:
            new["title"] = None
        if new["artist"] and norm(new["artist"]) == norm(cur_artist):
            new["artist"] = None
        if new["album"] and norm(new["album"]) == norm(cur_album):
            new["album"] = None
        if new["track_no"] and c.TrackNo:
            new["track_no"] = None
        if not any(x is not None for x in new.values()):
            continue
        props.append(
            {
                "id": c.ID,
                "path": c.FolderPath or "",
                "cur_title": cur_title,
                "cur_artist": cur_artist,
                "cur_album": cur_album,
                **new,
                "conf": v.get("conf", "medium"),
                "reason": f"model: {v.get('reason', 'no reason given')}",
            }
        )
    if errors:
        sys.exit("verdicts file rejected:\n  " + "\n  ".join(errors))
    props.sort(key=lambda p: CONF_ORDER.get(p["conf"], 2))
    return props


# ---------------------------------------------------------------- commands


def cmd_init(args):
    if baseline_dir():
        sys.exit(f"baseline already exists: {baseline_dir()} (init is one-time)")
    db = open_db()  # snapshot read is fine while app runs
    playlists = []
    for pl in db.get_playlist().all():
        songs = [s.ContentID for s in db.get_playlist_songs(PlaylistID=pl.ID).all()]
        playlists.append(
            {
                "id": pl.ID,
                "name": pl.Name,
                "parent": pl.ParentID,
                "attribute": pl.Attribute,
                "tracks": songs,
            }
        )
    dest = rbxpaths.backup_dir() / f"baseline-{ts()}"
    _copy_db_files(dest)
    manifest = {
        "created": datetime.now().isoformat(),
        "track_count": db.get_content().count(),
        "playlists": playlists,
    }
    (dest / "manifest.json").write_text(json.dumps(manifest, indent=2))
    print(f"Baseline snapshot: {dest}")
    print(f"Protected playlists/folders: {len(playlists)} (frozen forever)")
    print(f"Tracks at baseline: {manifest['track_count']}")


def cmd_status(args):
    master = rbxpaths.master()  # exits with a friendly diagnostic if not found
    print(f"master.db        : {master} ({master.stat().st_size // 1024 // 1024} MB)")
    print(f"backups dir      : {rbxpaths.backup_dir()}")
    print(f"rekordbox running: {rekordbox_running()}  (writes require it closed)")
    b = baseline_dir()
    m = load_manifest()
    print(f"baseline         : {b or 'NOT INITIALIZED — run: rbx init'}")
    if m:
        print(f"  protected playlists: {len(m['playlists'])}")
    pre = sorted(rbxpaths.backup_dir().glob("pre-write-*"))
    print(f"pre-write backups: {len(pre)}" + (f" (latest {pre[-1].name})" if pre else ""))
    journals = sorted(UNDO_DIR.glob("*.json"))
    print(f"undo journals    : {len(journals)}" + (f" (latest {journals[-1].name})" if journals else ""))
    db = open_db()
    print(f"tracks           : {db.get_content().count()}")
    print(f"playlists/folders: {len(db.get_playlist().all())}")


def cmd_setup(args):
    """Environment diagnostic + config writer. Idempotent, read-only with
    respect to the library. Safe to re-run any time."""
    branch = "Windows (UNTESTED branch — please report results)" if rbxpaths.IS_WINDOWS else "macOS"
    print(f"platform   : {sys.platform} -> {branch}")
    print(f"python     : {sys.version.split()[0]}  ({sys.executable})")
    if sys.version_info < (3, 11):
        print("  !! python 3.11+ required — re-run bootstrap with a newer interpreter")

    rb = rbxpaths.try_rb_dir()
    if rb is None:
        probed = "\n  ".join(str(c) for c in rbxpaths.probe_candidates())
        print("rekordbox  : NOT FOUND — no master.db at any probed location:")
        print(f"  {probed}")
        print("Set RBX_REKORDBOX_DIR=/path/to/rekordbox and re-run `rbx setup`.")
        sys.exit(1)
    master = rb / "master.db"
    print(f"library    : {rb}")
    print(f"master.db  : {master.stat().st_size // 1024 // 1024} MB")
    if not rbxpaths.IS_WINDOWS:
        apps = sorted(Path("/Applications").glob("rekordbox*"))
        if apps:
            print(f"app        : {apps[-1]}")
    print(f"running    : {rekordbox_running()}  (writes require it closed)")

    deps_ok = True
    for mod, label in (("pyrekordbox", "pyrekordbox"), ("mutagen", "mutagen")):
        try:
            m = __import__(mod)
            print(f"{label:11}: {getattr(m, '__version__', getattr(m, 'version_string', '?'))}")
        except ImportError:
            deps_ok = False
            print(f"{label:11}: MISSING — ./venv/bin/pip install -r scripts/requirements.txt")

    db_ok = None  # None = couldn't test (deps missing), True/False = tested
    if deps_ok:
        try:
            db = open_db()  # snapshot read; also exercises the SQLCipher key
            print(f"db open    : OK ({db.get_content().count()} tracks)")
            db_ok = True
        except Exception as e:
            db_ok = False
            print(f"db open    : FAILED ({str(e)[:120]})")
            print("  likely the SQLCipher key — one-time fix (needs network):")
            print("    ./venv/bin/python -m pyrekordbox download-key")
            print("  (or open rekordbox once, then re-run `rbx setup`)")

    try:
        import librosa  # noqa: F401
        print("analysis   : extras installed (librosa)")
    except ImportError:
        print("analysis   : extras not installed — only needed for `analyze` rhythm/timbre lanes")
        print("    ./venv/bin/pip install -r scripts/requirements-analysis.txt")
    fp = rbxpaths.fpcalc_path()
    print(f"fpcalc     : {fp or 'not found — audioid lane off (brew install chromaprint)'}")

    if db_ok is False:
        # don't persist a library dir we just failed to open — a later run
        # would silently keep resolving to it via config.json
        print("\nconfig     : NOT written (db open failed — fix that, re-run `rbx setup`)")
    else:
        cfg = rbxpaths.load_config()
        cfg.update({"rekordbox_dir": str(rb), "platform": sys.platform,
                    "updated": datetime.now().isoformat()})
        if fp:
            cfg["fpcalc"] = fp
        rbxpaths.save_config(cfg)
        print(f"\nconfig     : written -> {rbxpaths.CONFIG_PATH}")
    b = baseline_dir()
    print(f"baseline   : {b or 'none — run `rbx init` next (one-time; freezes current playlists)'}")


def cmd_recommend(args):
    db = open_db()
    contents = db.get_content().all()
    props = [p for c in contents if (p := propose(c))]
    by_conf = {"high": 0, "medium": 0, "low": 0}
    for p in props:
        by_conf[p["conf"]] += 1
    no_artist = sum(1 for c in contents if not c.Artist and is_local(c.FolderPath))
    streaming = sum(1 for c in contents if not is_local(c.FolderPath))
    missing = [
        c for c in contents
        if is_local(c.FolderPath) and not Path(c.FolderPath).exists()
        and not (len(Path(c.FolderPath).parts) > 2 and Path(c.FolderPath).parts[1] == "Volumes"
                 and not Path("/Volumes", Path(c.FolderPath).parts[2]).exists())
    ]
    groups = {}
    for c in contents:
        k = (norm(c.Title), norm(c.Artist.Name if c.Artist else ""), c.Length)
        groups.setdefault(k, []).append(c)
    dupes = [g for g in groups.values() if len(g) > 1 and g[0].Title]

    print("=== rbx recommendations ===\n")
    n = 1
    if by_conf["high"]:
        print(f"{n}. {by_conf['high']} tracks have HIGH-confidence metadata fixes")
        print("   -> rbx clean --min-conf high            (dry-run, then --apply)\n")
        n += 1
    if by_conf["medium"]:
        print(f"{n}. {by_conf['medium']} tracks have MEDIUM-confidence fixes (e.g. 'Artist - Title' with empty artist)")
        print("   -> rbx clean --min-conf medium\n")
        n += 1
    if by_conf["low"]:
        print(f"{n}. {by_conf['low']} tracks are ambiguous (multi-hyphen etc.) — review individually")
        print("   -> rbx clean --min-conf low --limit 20\n")
        n += 1
    if missing:
        print(f"{n}. {len(missing)} tracks point at files that no longer exist")
        print("   -> rbx doctor --missing\n")
        n += 1
    if dupes:
        print(f"{n}. {len(dupes)} probable duplicate groups (same title+artist+length)")
        print("   -> rbx doctor --dupes\n")
        n += 1
    if no_artist:
        print(f"{n}. {no_artist} local tracks have an empty artist field")
        print("   (most are covered by the clean command above)\n")
        n += 1
    print(f"(info) {streaming} tracks are streaming links (SoundCloud etc.) — DB-only, no file tags")
    if not baseline_dir():
        print("\n!! No baseline yet — run `rbx init` first to freeze current playlists.")


def cmd_clean(args):
    db = open_db(write=args.apply)
    if args.verdicts:
        if args.playlist:
            sys.exit("--verdicts and --playlist are exclusive (scope was set at export)")
        props = load_verdicts(db, Path(args.verdicts))
        props = [p for p in props if CONF_ORDER.get(p["conf"], 2) <= CONF_ORDER[args.min_conf]]
        if args.limit:
            props = props[: args.limit]
    else:
        props = collect_proposals(db, args.playlist, args.min_conf, args.limit)
    if not props:
        print("Nothing to fix in scope.")
        return
    if args.ids:
        keep = set(args.ids.split(","))
        props = [p for p in props if str(p["id"]) in keep]
    print_proposals(props)
    print(f"\n{len(props)} proposed fixes ({args.min_conf}+ confidence).")
    if not args.apply:
        print("DRY RUN — nothing written. Re-run with --apply to write DB"
              + ("" if args.no_tags else " + file tags") + ".")
        return

    bdir = backup_now()
    print(f"\nBackup: {bdir}")
    journal = {"cmd": "clean", "time": datetime.now().isoformat(), "entries": []}
    applied = 0
    for p in props:
        c = content_by_id(db, p["id"])
        entry = {
            "id": c.ID,
            "path": c.FolderPath,
            "old": {"Title": c.Title, "ArtistID": c.ArtistID, "AlbumID": c.AlbumID, "TrackNo": c.TrackNo},
            "new": {},
            "old_tags": None,
        }
        if p["title"] is not None:
            c.Title = p["title"]
            entry["new"]["Title"] = p["title"]
        if p["artist"] is not None:
            a = db.get_artist(Name=p["artist"]).first() or db.add_artist(p["artist"])
            c.ArtistID = a.ID
            entry["new"]["ArtistID"] = a.ID
        if p["album"] is not None:
            al = db.get_album(Name=p["album"]).first() or db.add_album(p["album"])
            c.AlbumID = al.ID
            entry["new"]["AlbumID"] = al.ID
        if p["track_no"] is not None:
            c.TrackNo = p["track_no"]
            entry["new"]["TrackNo"] = p["track_no"]

        if not args.no_tags and is_local(p["path"]) and Path(p["path"]).exists():
            vals = {}
            if p["title"] is not None:
                vals["title"] = p["title"]
            if p["artist"] is not None:
                vals["artist"] = p["artist"]
            if p["album"] is not None:
                vals["album"] = p["album"]
            if vals:
                entry["old_tags"] = write_tags(p["path"], vals)
        journal["entries"].append(entry)
        applied += 1
    db.commit()
    UNDO_DIR.mkdir(exist_ok=True)
    jpath = UNDO_DIR / f"clean-{ts()}.json"
    jpath.write_text(json.dumps(journal, indent=2))
    print(f"Applied {applied} fixes. Undo journal: {jpath}")
    print("Reopen rekordbox to verify; `rbx undo` reverses this batch.")


# Tables holding per-track rows that must go when a track leaves the collection.
REMOVE_RELATED_TABLES = [
    "ContentActiveCensor", "ContentCue", "ContentFile", "DjmdActiveCensor",
    "DjmdCue", "DjmdSongHistory", "DjmdSongHotCueBanklist", "DjmdMixerParam",
    "DjmdSongMyTag", "DjmdSongPlaylist", "DjmdSongRelatedTracks",
    "DjmdSongSampler", "DjmdSongTagList",
]


def _row_dict(row) -> dict:
    return {c.name: getattr(row, c.name) for c in row.__table__.columns}


def _row_restore(db, tables_mod, tname: str, cols: dict):
    cls = getattr(tables_mod, tname)
    kwargs = {}
    for c in cls.__table__.columns:
        if c.name not in cols:
            continue
        v = cols[c.name]
        if v is not None and isinstance(v, str):
            try:
                if c.type.python_type is datetime:
                    v = datetime.fromisoformat(v)
            except (NotImplementedError, ValueError):
                pass
        kwargs[c.name] = v
    db.add(cls(**kwargs))


def cmd_remove(args):
    """Remove tracks from the rekordbox collection. DB rows only — audio files
    are NEVER touched (hard rule)."""
    from pyrekordbox.db6 import tables

    if not args.folder and not args.ids:
        sys.exit("need --folder PREFIX or --ids")
    db = open_db(write=args.apply)

    if args.ids:
        cands = [c for cid in args.ids.split(",") if (c := content_by_id(db, cid.strip()))]
    else:
        prefix = str(Path(args.folder).expanduser())
        if not prefix.endswith("/"):
            prefix += "/"
        cands = [c for c in db.get_content() if (c.FolderPath or "").startswith(prefix)]

    if args.without_hot_cues:
        hot = {
            str(cue.ContentID)
            for cue in db.query(tables.DjmdCue).filter(tables.DjmdCue.Kind > 0)
        }
        cands = [c for c in cands if str(c.ID) not in hot]

    # frozen-baseline-playlist guard: removal would change their membership
    frozen = {str(i) for i in frozen_playlist_ids()}
    pl_names = {str(p.ID): p.Name for p in db.get_playlist()}
    frozen_hits = {}
    for sp_row in db.query(tables.DjmdSongPlaylist):
        if str(sp_row.PlaylistID) in frozen:
            frozen_hits.setdefault(str(sp_row.ContentID), set()).add(
                pl_names.get(str(sp_row.PlaylistID), "?"))
    excluded = [c for c in cands if str(c.ID) in frozen_hits]
    cands = [c for c in cands if str(c.ID) not in frozen_hits]
    if args.limit:
        cands = cands[: args.limit]

    if excluded:
        print(f"EXCLUDED {len(excluded)} tracks (member of frozen baseline playlists):")
        for c in excluded:
            pls = ", ".join(sorted(frozen_hits[str(c.ID)]))
            print(f"  id={c.ID}  {c.Artist.Name if c.Artist else '?'} - {c.Title}  [{pls}]")
        print()
    if not cands:
        print("Nothing to remove in scope.")
        return

    by_folder = {}
    for c in cands:
        by_folder.setdefault(str(Path(c.FolderPath).parent), []).append(c)
    print(f"{len(cands)} tracks to REMOVE from the collection (DB only — files stay on disk):")
    for folder in sorted(by_folder, key=lambda f: -len(by_folder[f])):
        print(f"  {len(by_folder[folder]):5d}  {folder}")
    if not args.apply:
        print("\nDRY RUN — nothing removed. Re-run with --apply to remove.")
        return

    bdir = backup_now()
    print(f"\nBackup: {bdir}")
    journal = {"cmd": "remove", "time": datetime.now().isoformat(), "entries": []}
    for c in cands:
        entry = {"id": str(c.ID), "path": c.FolderPath,
                 "content": _row_dict(c), "related": {}}
        for tname in REMOVE_RELATED_TABLES:
            cls = getattr(tables, tname)
            rows = db.query(cls).filter(cls.ContentID == c.ID).all()
            if rows:
                entry["related"][tname] = [_row_dict(r) for r in rows]
                for r in rows:
                    db.delete(r)
        db.delete(c)
        journal["entries"].append(entry)
    db.commit()
    UNDO_DIR.mkdir(exist_ok=True)
    jpath = UNDO_DIR / f"remove-{ts()}.json"
    jpath.write_text(json.dumps(journal, indent=2, default=str))
    print(f"Removed {len(cands)} tracks from the collection. Files untouched.")
    print(f"Undo journal: {jpath}")
    print("Reopen rekordbox to verify; `rbx undo` re-inserts the removed rows.")


def cmd_undo(args):
    journals = sorted(UNDO_DIR.glob("*.json"))
    if not journals:
        sys.exit("no undo journals")
    jpath = Path(args.journal) if args.journal else journals[-1]
    journal = json.loads(jpath.read_text())
    n_entries = len(journal.get("entries", journal.get("playlists", [])))
    print(f"Undoing {jpath.name} ({n_entries} entries)")
    db = open_db(write=True)
    bdir = backup_now("pre-undo")
    print(f"Backup: {bdir}")
    if journal["cmd"] == "playlists":
        for p in journal["playlists"]:
            try:
                db.delete_playlist(p["id"])
                print(f"  deleted playlist {p['name']}")
            except Exception as e:
                print(f"  could not delete {p['name']}: {e}")
        db.commit()
        done = jpath.with_suffix(".undone")
        jpath.rename(done)
        print(f"Undo complete. Journal archived as {done.name}")
        return
    if journal["cmd"] == "remove":
        from pyrekordbox.db6 import tables

        restored = 0
        for e in journal["entries"]:
            if content_by_id(db, e["id"]) is not None:
                print(f"  skipped id={e['id']} (already back in DB)")
                continue
            _row_restore(db, tables, "DjmdContent", e["content"])
            for tname, rows in e["related"].items():
                for cols in rows:
                    _row_restore(db, tables, tname, cols)
            restored += 1
        db.commit()
        done = jpath.with_suffix(".undone")
        jpath.rename(done)
        print(f"Re-inserted {restored} tracks. Journal archived as {done.name}")
        return
    for e in journal["entries"]:
        c = content_by_id(db, e["id"])
        if c is None:
            print(f"  skipped id={e['id']} (no longer in DB)")
            continue
        for field, val in e["old"].items():
            if field in e["new"]:
                setattr(c, field, val)
        if e.get("old_tags") and is_local(e["path"]) and Path(e["path"]).exists():
            write_tags(e["path"], {k: v for k, v in e["old_tags"].items()})
    db.commit()
    done = jpath.with_suffix(".undone")
    jpath.rename(done)
    print(f"Undo complete. Journal archived as {done.name}")


def cmd_tagsync(args):
    db = open_db()
    drift = []
    contents = [c for c in db.get_content().all() if is_local(c.FolderPath)]
    for c in contents[: args.limit] if args.limit else contents:
        if not Path(c.FolderPath).exists():
            continue
        tags = read_tags(c.FolderPath)
        if tags is None:
            continue
        db_vals = {
            "title": c.Title or "",
            "artist": c.Artist.Name if c.Artist else "",
            "album": c.Album.Name if c.Album else "",
        }
        diffs = {k: (tags[k], db_vals[k]) for k in db_vals if norm(tags[k]) != norm(db_vals[k])}
        if diffs:
            drift.append((c, diffs))
    print(f"{len(drift)} local files disagree with the DB:")
    for c, diffs in drift[:200]:
        print(f"  id={c.ID} {Path(c.FolderPath).name}")
        for k, (filev, dbv) in diffs.items():
            print(f"    {k}: file={filev!r}  db={dbv!r}")
    if len(drift) > 200:
        print(f"  ... and {len(drift) - 200} more")
    if not args.apply:
        if drift:
            print("\nDRY RUN. --apply writes DB values into the file tags (DB is source of truth).")
        return
    require_closed()
    require_baseline()  # tag writes bypass open_db(write=True), so gate here too
    journal = {"cmd": "tagsync", "time": datetime.now().isoformat(), "entries": []}
    for c, diffs in drift:
        vals = {
            "title": c.Title or "",
            "artist": c.Artist.Name if c.Artist else "",
            "album": c.Album.Name if c.Album else "",
        }
        old = write_tags(c.FolderPath, vals)
        journal["entries"].append(
            {"id": c.ID, "path": c.FolderPath, "old": {}, "new": {}, "old_tags": old}
        )
    UNDO_DIR.mkdir(exist_ok=True)
    jpath = UNDO_DIR / f"tagsync-{ts()}.json"
    jpath.write_text(json.dumps(journal, indent=2))
    print(f"Wrote tags on {len(drift)} files. Undo journal: {jpath}")


# ---------------------------------------------------------------- analysis (C7)

CAMELOT = {
    "Abm": "1A", "G#m": "1A", "B": "1B", "Ebm": "2A", "D#m": "2A", "F#": "2B",
    "Gb": "2B", "Bbm": "3A", "A#m": "3A", "Db": "3B", "C#": "3B", "Fm": "4A",
    "Ab": "4B", "Cm": "5A", "Eb": "5B", "Gm": "6A", "Bb": "6B", "Dm": "7A",
    "F": "7B", "Am": "8A", "C": "8B", "Em": "9A", "G": "9B", "Bm": "10A",
    "D": "10B", "F#m": "11A", "Gbm": "11A", "A": "11B", "Dbm": "12A",
    "C#m": "12A", "E": "12B",
}


def camelot(key: str | None) -> str | None:
    if not key:
        return None
    key = key.strip()
    if re.fullmatch(r"\d{1,2}[AB]", key):
        return key
    return CAMELOT.get(key)


def key_score(k1: str | None, k2: str | None) -> int | None:
    """2 = same/relative, 1 = neighbor on the wheel, 0 = clash, None = unknown."""
    c1, c2 = camelot(k1), camelot(k2)
    if not c1 or not c2:
        return None
    n1, l1 = int(c1[:-1]), c1[-1]
    n2, l2 = int(c2[:-1]), c2[-1]
    if n1 == n2:
        return 2
    if l1 == l2 and (abs(n1 - n2) == 1 or abs(n1 - n2) == 11):
        return 1
    return 0


def _scope_tracks(db, args):
    if getattr(args, "ids", None):
        return [c for cid in args.ids.split(",") if (c := content_by_id(db, cid.strip()))]
    if getattr(args, "playlist", None):
        pl = db.get_playlist(Name=args.playlist)
        pl = pl.first() if hasattr(pl, "first") else pl
        if pl is None:
            sys.exit(f"no playlist named {args.playlist!r}")
        return [c for sp_ in db.get_playlist_songs(PlaylistID=pl.ID)
                if (c := content_by_id(db, sp_.ContentID))]
    if getattr(args, "folder", None):
        prefix = str(Path(args.folder).expanduser()).rstrip("/") + "/"
        return [c for c in db.get_content() if (c.FolderPath or "").startswith(prefix)]
    return list(db.get_content())


def cmd_analyze(args):
    import analysis as an

    db = open_db()
    con = an.open_cache()
    tracks = _scope_tracks(db, args)

    if args.status:
        done = {lane: 0 for lane in an.LANES}
        errs = {lane: 0 for lane in an.LANES}
        no_audio = 0
        missing = 0
        for c in tracks:
            row = an.cache_row(con, str(c.ID))
            if row is None:
                missing += 1
                continue
            if not is_local(c.FolderPath):
                no_audio += 1
            rerrs = json.loads(row["errors"] or "{}")
            for lane in an.LANES:
                if row[lane]:
                    done[lane] += 1
                elif lane in rerrs:
                    errs[lane] += 1
        print(f"scope: {len(tracks)} tracks   never analyzed: {missing}   streaming (no audio): {no_audio}")
        for lane in an.LANES:
            print(f"  {lane:8s} done {done[lane]:5d}   errors {errs[lane]}")
        return

    todo = tracks if not args.limit else tracks[: args.limit]
    print(f"Analyzing {len(todo)} tracks (all lanes; resumable, cached in analysis.db)")
    n_done = n_skip = n_err = 0
    for i, c in enumerate(todo, 1):
        cid = str(c.ID)
        path = c.FolderPath or ""
        local = is_local(path) and Path(path).exists()
        mt = an.file_mtime(path) if local else None
        row = an.cache_row(con, cid)
        fresh = row is not None and (mt is None or row["mtime"] == mt)
        data = {lane: (json.loads(row[lane]) if row and row[lane] and fresh else None)
                for lane in an.LANES}
        errors = json.loads(row["errors"]) if row and row["errors"] and fresh else {}
        changed = False

        if data["grid"] is None and "grid" not in errors:
            try:
                data["grid"] = an.analyze_grid(c)
                changed = True
            except Exception as e:
                errors["grid"] = str(e)[:200]
                changed = True
        if local:
            if data["rhythm"] is None and "rhythm" not in errors and data["grid"]:
                try:
                    data["rhythm"] = an.analyze_rhythm(path, data["grid"])
                    changed = True
                except Exception as e:
                    errors["rhythm"] = str(e)[:200]
                    changed = True
            if data["timbre"] is None and "timbre" not in errors:
                try:
                    data["timbre"] = an.analyze_timbre(path)
                    changed = True
                except Exception as e:
                    errors["timbre"] = str(e)[:200]
                    changed = True
            if data["audioid"] is None and "audioid" not in errors:
                try:
                    data["audioid"] = an.analyze_audioid(path)
                    changed = True
                except Exception as e:
                    errors["audioid"] = str(e)[:200]
                    changed = True

        if changed or row is None:
            con.execute(
                "INSERT OR REPLACE INTO analysis "
                "(content_id, path, mtime, grid, rhythm, timbre, audioid, errors) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (cid, path, mt,
                 *(json.dumps(data[lane]) if data[lane] else None for lane in an.LANES),
                 json.dumps(errors) if errors else None),
            )
            con.commit()
            n_done += 1
            if errors:
                n_err += 1
        else:
            n_skip += 1
        if i % 25 == 0 or i == len(todo):
            print(f"  [{i}/{len(todo)}] analyzed {n_done}  cached {n_skip}  with-errors {n_err}",
                  flush=True)
    print("Done. `rbx analyze --status` for coverage; similar/mixable/clusters now usable.")


def find_track(db, q: str):
    if re.fullmatch(r"\d+", q):
        c = content_by_id(db, q)
        if c is None:
            sys.exit(f"no track with ID {q}")
        return c
    qn = norm(q)
    matches = []
    for c in db.get_content():
        s = f"{c.Artist.Name if c.Artist else ''} - {c.Title or ''}"
        if qn in norm(s):
            matches.append((c, s))
    if not matches:
        sys.exit(f"no track matching {q!r}")
    if len(matches) > 1:
        exact = [m for m in matches if norm(m[1]) == qn]
        if len(exact) == 1:
            return exact[0][0]
        print(f"{len(matches)} matches — re-run with an ID:")
        for c, s in matches[:15]:
            print(f"  id={c.ID}  {s}")
        sys.exit(1)
    return matches[0][0]


def _cached_lane(con, an, cid, lane):
    row = an.cache_row(con, str(cid))
    if row and row[lane]:
        return json.loads(row[lane])
    return None


def cmd_similar(args):
    import analysis as an

    db = open_db()
    con = an.open_cache()
    target = find_track(db, args.track)
    t_rhythm = _cached_lane(con, an, target.ID, "rhythm")
    t_timbre = _cached_lane(con, an, target.ID, "timbre")
    use_r = args.by in ("rhythm", "both") and t_rhythm
    use_t = args.by in ("timbre", "both") and t_timbre
    if not use_r and not use_t:
        sys.exit(f"track not analyzed for {args.by} yet — run `rbx analyze` on it first")

    scored = []
    for row in con.execute("SELECT content_id, rhythm, timbre FROM analysis"):
        cid, r_json, t_json = row
        if str(cid) == str(target.ID):
            continue
        ds = []
        if use_r and r_json:
            ds.append(an.rhythm_distance(t_rhythm, json.loads(r_json)))
        if use_t and t_json:
            ds.append(an.timbre_distance(t_timbre, json.loads(t_json)))
        if ds:
            scored.append((sum(ds) / len(ds), cid))
    scored.sort()
    tname = f"{target.Artist.Name if target.Artist else '?'} - {target.Title}"
    print(f"most similar to: {tname}   (by {args.by})\n")
    shown = 0
    for dist, cid in scored:
        c = content_by_id(db, cid)
        if c is None:
            continue
        bpm = (c.BPM or 0) / 100
        key = c.Key.ScaleName if c.Key else "?"
        print(f"  {dist:.3f}  [{bpm:6.1f} {key:>3}]  id={c.ID}  "
              f"{c.Artist.Name if c.Artist else '?'} - {c.Title}")
        shown += 1
        if shown >= args.limit:
            break


def cmd_mixable(args):
    import analysis as an

    db = open_db()
    con = an.open_cache()
    target = find_track(db, args.track)
    t_grid = _cached_lane(con, an, target.ID, "grid")
    t_bpm = (target.BPM or 0) / 100
    t_key = target.Key.ScaleName if target.Key else None
    if not t_bpm:
        sys.exit("target has no BPM")
    t_energy_out = None
    if t_grid and t_grid.get("energy"):
        curve = t_grid["energy"]
        t_energy_out = sum(curve[-15:]) / len(curve[-15:])

    results = []
    for c in db.get_content():
        if str(c.ID) == str(target.ID):
            continue
        bpm = (c.BPM or 0) / 100
        if not bpm:
            continue
        ratios = [1.0] + ([0.5, 2.0] if args.half_time else [])
        diffs = [abs(bpm * r - t_bpm) / t_bpm for r in ratios]
        bpm_diff = min(diffs)
        if bpm_diff > args.bpm_range / 100:
            continue
        ks = key_score(t_key, c.Key.ScaleName if c.Key else None)
        if ks == 0 and not args.any_key:
            continue
        score = (ks if ks is not None else 0.5) - bpm_diff * 10
        grid = _cached_lane(con, an, c.ID, "grid")
        intro_beats = energy_note = None
        if grid:
            intro_beats = grid.get("intro_beats")
            if intro_beats:
                score += 0.5 if intro_beats >= 64 else (0.25 if intro_beats >= 32 else 0)
            if t_energy_out is not None and grid.get("energy"):
                curve = grid["energy"]
                e_in = sum(curve[:15]) / len(curve[:15])
                match = 1 - abs(t_energy_out - e_in)
                score += match
                energy_note = f"in:{e_in:.2f}"
        results.append((score, bpm_diff, c, intro_beats, energy_note, ks))
    results.sort(key=lambda r: -r[0])
    tname = f"{target.Artist.Name if target.Artist else '?'} - {target.Title}"
    e_out = f", outro energy {t_energy_out:.2f}" if t_energy_out is not None else ""
    print(f"mix candidates for: {tname}  [{t_bpm:.1f} {t_key or '?'}{e_out}]\n")
    for score, bpm_diff, c, intro_beats, energy_note, ks in results[: args.limit]:
        bpm = (c.BPM or 0) / 100
        key = c.Key.ScaleName if c.Key else "?"
        bits = []
        if intro_beats:
            bits.append(f"intro {intro_beats}bt")
        if energy_note:
            bits.append(energy_note)
        kmark = {2: "KEY=", 1: "key~", 0: "KEY!", None: "key?"}[ks]
        print(f"  {score:5.2f}  [{bpm:6.1f} {key:>3} {kmark}]  id={c.ID}  "
              f"{c.Artist.Name if c.Artist else '?'} - {c.Title}"
              + (f"   ({', '.join(bits)})" if bits else ""))


def cmd_clusters(args):
    import analysis as an

    db = open_db()
    con = an.open_cache()
    tracks = _scope_tracks(db, args)
    items = []
    for c in tracks:
        r = _cached_lane(con, an, c.ID, "rhythm")
        if r:
            items.append((c, r))
    if not items:
        sys.exit("no rhythm-analyzed tracks in scope — run `rbx analyze` first")
    items.sort(key=lambda it: (it[0].BPM or 0))

    clusters = []  # each: {"leader": rhythm, "bpm": float, "members": [content]}
    for c, r in items:
        bpm = (c.BPM or 0) / 100
        placed = False
        for cl in clusters:
            if cl["bpm"] and bpm and abs(bpm - cl["bpm"]) / cl["bpm"] > 0.06:
                continue
            if an.rhythm_distance(cl["leader"], r) <= args.threshold:
                cl["members"].append(c)
                placed = True
                break
        if not placed:
            clusters.append({"leader": r, "bpm": bpm, "members": [c]})
    big = [cl for cl in clusters if len(cl["members"]) >= args.min_size]
    big.sort(key=lambda cl: -len(cl["members"]))
    print(f"{len(items)} rhythm-analyzed tracks -> {len(clusters)} clusters, "
          f"{len(big)} with >= {args.min_size} members\n")
    for n, cl in enumerate(big, 1):
        pat = cl["leader"]["low"]["pattern"]
        print(f"cluster {n}: {len(cl['members'])} tracks  ~{cl['bpm']:.0f} BPM  kick [{pat}]")
        for c in cl["members"][:5]:
            print(f"    {c.Artist.Name if c.Artist else '?'} - {c.Title}")
        if len(cl["members"]) > 5:
            print(f"    ... and {len(cl['members']) - 5} more")
    if not args.playlists:
        if big:
            print("\n--playlists creates a '[rbx] rhythm ...' playlist per cluster shown.")
        return

    db_w = open_db(write=True)
    bdir = backup_now()
    print(f"\nBackup: {bdir}")
    journal = {"cmd": "playlists", "time": datetime.now().isoformat(), "playlists": []}
    for n, cl in enumerate(big, 1):
        name = f"[rbx] rhythm {cl['bpm']:.0f}bpm c{n}"
        pl = db_w.create_playlist(name)
        for c in cl["members"]:
            db_w.add_to_playlist(pl, content_by_id(db_w, c.ID))
        journal["playlists"].append({"id": str(pl.ID), "name": name,
                                     "n": len(cl["members"])})
    db_w.commit()
    UNDO_DIR.mkdir(exist_ok=True)
    jpath = UNDO_DIR / f"playlists-{ts()}.json"
    jpath.write_text(json.dumps(journal, indent=2))
    for p in journal["playlists"]:
        print(f"  created {p['name']}  ({p['n']} tracks)")
    print(f"Undo journal: {jpath} (`rbx undo` deletes these playlists)")


def cmd_doctor(args):
    db = open_db()
    contents = db.get_content().all()
    ran = False
    if args.missing or args.all:
        ran = True
        gone = [c for c in contents if is_local(c.FolderPath) and not Path(c.FolderPath).exists()]
        unmounted, missing = {}, []
        for c in gone:
            parts = Path(c.FolderPath).parts
            if len(parts) > 2 and parts[1] == "Volumes" and not Path("/Volumes", parts[2]).exists():
                unmounted.setdefault(parts[2], []).append(c)
            else:
                missing.append(c)
        for vol, cs in unmounted.items():
            print(f"--- {len(cs)} tracks on unmounted volume '{vol}' (plug it in — not actually missing)")
        print(f"--- missing files: {len(missing)}")
        for c in missing:
            print(f"  id={c.ID}  {c.FolderPath}")
    if args.dupes or args.all:
        ran = True
        groups = {}
        for c in contents:
            if not c.Title:
                continue
            k = (norm(c.Title), norm(c.Artist.Name if c.Artist else ""), c.Length)
            groups.setdefault(k, []).append(c)
        dupes = [g for g in groups.values() if len(g) > 1]
        print(f"--- duplicate groups: {len(dupes)}")
        pl_map = {}
        for s in db.get_playlist_songs().all():
            pl_map.setdefault(s.ContentID, []).append(s.Playlist.Name if s.Playlist else "?")
        for g in dupes:
            print(f"  {g[0].Artist.Name if g[0].Artist else '?'} - {g[0].Title}")
            for c in g:
                pls = pl_map.get(c.ID, [])
                loc = c.FolderPath if is_local(c.FolderPath) else "(streaming)"
                print(f"    id={c.ID} playlists={pls or '-'} {loc}")
    if args.untracked:
        ran = True
        known = {norm(c.FolderPath) for c in contents if is_local(c.FolderPath)}
        found = []
        for root in args.untracked:
            for p in Path(root).expanduser().rglob("*"):
                if p.suffix.lower() in AUDIO_EXTS and norm(str(p)) not in known:
                    found.append(p)
        print(f"--- untracked audio files: {len(found)}")
        for p in found[:300]:
            print(f"  {p}")
    if not ran:
        print("nothing to do: pass --missing / --dupes / --untracked ROOT / --all")


FROZEN_MSG = "REFUSED: '{}' is a baseline playlist (existed at rbx init) — read-only forever."


def _playlist_guard(name_or_id, db):
    pl = db.get_playlist(Name=name_or_id).first() or db.get_playlist(ID=name_or_id).first()
    if pl is None:
        sys.exit(f"playlist not found: {name_or_id}")
    if pl.ID in frozen_playlist_ids():
        sys.exit(FROZEN_MSG.format(pl.Name))
    return pl


def cmd_playlist(args):
    sub = args.pl_cmd
    if sub == "list":
        db = open_db()
        frozen = frozen_playlist_ids()
        for pl in db.get_playlist().all():
            kind = "folder" if pl.Attribute == 1 else ("smart" if pl.Attribute == 4 else "playlist")
            mark = "[frozen]" if pl.ID in frozen else "[editable]"
            print(f"  {mark} {kind:8} {pl.Name}  (id={pl.ID})")
        return
    if sub == "show":
        db = open_db()
        pl = db.get_playlist(Name=args.name).first()
        if pl is None:
            sys.exit(f"playlist not found: {args.name}")
        for s in sorted(db.get_playlist_songs(PlaylistID=pl.ID).all(), key=lambda s: s.TrackNo or 0):
            c = s.Content
            print(f"  {c.Artist.Name if c.Artist else '?'} - {c.Title}")
        return
    if sub == "export":
        db = open_db()
        pl = db.get_playlist(Name=args.name).first()
        if pl is None:
            sys.exit(f"playlist not found: {args.name}")
        out = Path(args.output or f"{args.name}.m3u8")
        lines = ["#EXTM3U"]
        for s in sorted(db.get_playlist_songs(PlaylistID=pl.ID).all(), key=lambda s: s.TrackNo or 0):
            c = s.Content
            artist = c.Artist.Name if c.Artist else ""
            lines.append(f"#EXTINF:{(c.Length or 0)},{artist} - {c.Title}")
            lines.append(c.FolderPath or "")
        out.write_text("\n".join(lines))
        print(f"exported {args.name} -> {out}")
        return

    # everything below mutates
    db = open_db(write=True)
    if sub == "create":
        backup_now()
        pl = db.create_playlist(args.name)
        db.commit()
        print(f"created playlist {args.name} (id={pl.ID}) — editable (created after baseline)")
    elif sub == "delete":
        pl = _playlist_guard(args.name, db)
        backup_now()
        db.delete_playlist(pl)
        db.commit()
        print(f"deleted {args.name}")
    elif sub == "rename":
        pl = _playlist_guard(args.name, db)
        backup_now()
        db.rename_playlist(pl, args.new_name)
        db.commit()
        print(f"renamed {args.name} -> {args.new_name}")
    elif sub == "add":
        pl = _playlist_guard(args.name, db)
        backup_now()
        added = 0
        for cid in args.ids.split(","):
            c = content_by_id(db, cid.strip())
            if c:
                db.add_to_playlist(pl, c)
                added += 1
        db.commit()
        print(f"added {added} tracks to {args.name}")
    elif sub == "remove":
        pl = _playlist_guard(args.name, db)
        backup_now()
        removed = 0
        for s in db.get_playlist_songs(PlaylistID=pl.ID).all():
            if str(s.ContentID) in {i.strip() for i in args.ids.split(",")}:
                db.remove_from_playlist(pl, s)
                removed += 1
        db.commit()
        print(f"removed {removed} tracks from {args.name}")


def cmd_query(args):
    db = open_db()
    if args.sql:
        if not args.sql.strip().lower().startswith("select"):
            sys.exit("only SELECT statements allowed")
        from sqlalchemy import text

        rows = db.session.execute(text(args.sql)).fetchall()
        for r in rows[: args.limit or 500]:
            print("\t".join(str(v) for v in r))
        return
    out_rows = []
    for c in db.get_content().all():
        bpm = (c.BPM or 0) / 100
        if args.bpm_min and bpm < args.bpm_min:
            continue
        if args.bpm_max and bpm > args.bpm_max:
            continue
        artist = c.Artist.Name if c.Artist else ""
        if args.artist and norm(args.artist) not in norm(artist):
            continue
        if args.title and norm(args.title) not in norm(c.Title):
            continue
        key = c.Key.ScaleName if c.Key else ""
        if args.key and norm(args.key) != norm(key):
            continue
        out_rows.append(
            {
                "id": c.ID,
                "artist": artist,
                "title": c.Title,
                "album": c.Album.Name if c.Album else "",
                "bpm": bpm,
                "key": key,
                "path": c.FolderPath,
            }
        )
    if args.csv or args.json:
        dest = Path(args.csv or args.json)
        if args.csv:
            with dest.open("w", newline="") as fh:
                w = csv.DictWriter(fh, fieldnames=out_rows[0].keys() if out_rows else ["id"])
                w.writeheader()
                w.writerows(out_rows)
        else:
            dest.write_text(json.dumps(out_rows, indent=2))
        print(f"{len(out_rows)} rows -> {dest}")
    else:
        for r in out_rows[: args.limit or 100]:
            print(f"  [{r['bpm']:6.1f} {r['key']:>4}] {r['artist']} - {r['title']}")
        print(f"({len(out_rows)} matches)")


def cmd_backup(args):
    print(f"backup: {backup_now('manual')}")


# ---------------------------------------------------------------- main


def main():
    ap = argparse.ArgumentParser(prog="rbx", description=__doc__)
    sp = ap.add_subparsers(dest="cmd", required=True)

    sp.add_parser("setup", help="environment diagnostic: discover library, check deps/key, write config")
    sp.add_parser("init", help="one-time baseline snapshot + freeze current playlists")
    sp.add_parser("status", help="library + skill state overview")
    sp.add_parser("recommend", help="scan collection, suggest what to fix")
    sp.add_parser("backup", help="manual timestamped backup of master.db")

    p = sp.add_parser("clean", help="fix polluted Title/Artist/Album metadata")
    p.add_argument("--apply", action="store_true", help="write changes (default: dry run)")
    p.add_argument("--min-conf", choices=["high", "medium", "low"], default="high")
    p.add_argument("--playlist", help="restrict scope to one playlist")
    p.add_argument("--ids", help="comma-separated content IDs to restrict to")
    p.add_argument("--limit", type=int)
    p.add_argument("--no-tags", action="store_true", help="DB only, skip file tags")
    p.add_argument("--verdicts", help="apply model-agent verdicts JSON instead of parser proposals")

    p = sp.add_parser("resolve", help="export ambiguous cases for a delegated model agent")
    p.add_argument("--playlist", help="restrict scope to one playlist")

    p = sp.add_parser("tagsync", help="report/fix DB vs embedded-tag drift")
    p.add_argument("--apply", action="store_true", help="write DB values into file tags")
    p.add_argument("--limit", type=int)

    p = sp.add_parser("doctor", help="missing files / duplicates / untracked")
    p.add_argument("--missing", action="store_true")
    p.add_argument("--dupes", action="store_true")
    p.add_argument("--untracked", nargs="+", metavar="ROOT")
    p.add_argument("--all", action="store_true")

    p = sp.add_parser("playlist", help="additive playlist management")
    pls = p.add_subparsers(dest="pl_cmd", required=True)
    pls.add_parser("list")
    x = pls.add_parser("show"); x.add_argument("name")
    x = pls.add_parser("create"); x.add_argument("name")
    x = pls.add_parser("delete"); x.add_argument("name")
    x = pls.add_parser("rename"); x.add_argument("name"); x.add_argument("new_name")
    x = pls.add_parser("add"); x.add_argument("name"); x.add_argument("--ids", required=True)
    x = pls.add_parser("remove"); x.add_argument("name"); x.add_argument("--ids", required=True)
    x = pls.add_parser("export"); x.add_argument("name"); x.add_argument("-o", "--output")

    p = sp.add_parser("analyze", help="run all analysis lanes (grid/rhythm/timbre/audioid), cached")
    p.add_argument("--playlist", help="restrict scope to one playlist")
    p.add_argument("--folder", help="restrict scope to a path prefix")
    p.add_argument("--ids", help="comma-separated content IDs")
    p.add_argument("--limit", type=int)
    p.add_argument("--status", action="store_true", help="show cache coverage, analyze nothing")

    p = sp.add_parser("similar", help="rank collection by rhythm/timbre similarity to a track")
    p.add_argument("track", help="content ID or fuzzy 'artist - title'")
    p.add_argument("--by", choices=["rhythm", "timbre", "both"], default="both")
    p.add_argument("--limit", type=int, default=20)

    p = sp.add_parser("mixable", help="what mixes into this track (key + BPM + intro + energy)")
    p.add_argument("track", help="content ID or fuzzy 'artist - title'")
    p.add_argument("--bpm-range", type=float, default=8, help="max BPM distance in %% (default 8)")
    p.add_argument("--any-key", action="store_true", help="include key clashes")
    p.add_argument("--no-half-time", dest="half_time", action="store_false",
                   help="disable half/double-time BPM matching")
    p.add_argument("--limit", type=int, default=25)

    p = sp.add_parser("clusters", help="group scope into rhythm families")
    p.add_argument("--playlist", help="restrict scope to one playlist")
    p.add_argument("--folder", help="restrict scope to a path prefix")
    p.add_argument("--threshold", type=float, default=0.12, help="rhythm distance cutoff")
    p.add_argument("--min-size", type=int, default=3)
    p.add_argument("--playlists", action="store_true",
                   help="materialize clusters as '[rbx] rhythm ...' playlists")

    p = sp.add_parser("remove", help="remove tracks from the collection (DB only; files never touched)")
    p.add_argument("--folder", help="path prefix, e.g. ~/Music/music/og")
    p.add_argument("--ids", help="comma-separated content IDs")
    p.add_argument("--without-hot-cues", action="store_true",
                   help="restrict to tracks that have no hot cues")
    p.add_argument("--limit", type=int)
    p.add_argument("--apply", action="store_true", help="remove (default: dry run)")

    p = sp.add_parser("query", help="query/export the collection (read-only)")
    p.add_argument("--sql", help="raw SELECT against the decrypted schema")
    p.add_argument("--bpm-min", type=float)
    p.add_argument("--bpm-max", type=float)
    p.add_argument("--key")
    p.add_argument("--artist")
    p.add_argument("--title")
    p.add_argument("--csv")
    p.add_argument("--json")
    p.add_argument("--limit", type=int)

    p = sp.add_parser("undo", help="reverse the latest (or named) write batch")
    p.add_argument("journal", nargs="?")

    args = ap.parse_args()
    {
        "setup": cmd_setup,
        "init": cmd_init,
        "status": cmd_status,
        "recommend": cmd_recommend,
        "clean": cmd_clean,
        "resolve": cmd_resolve,
        "analyze": cmd_analyze,
        "similar": cmd_similar,
        "mixable": cmd_mixable,
        "clusters": cmd_clusters,
        "remove": cmd_remove,
        "tagsync": cmd_tagsync,
        "doctor": cmd_doctor,
        "playlist": cmd_playlist,
        "query": cmd_query,
        "undo": cmd_undo,
        "backup": cmd_backup,
    }[args.cmd](args)


if __name__ == "__main__":
    main()
