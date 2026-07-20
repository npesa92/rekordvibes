"""rbx transfer — export/import self-contained track bundles.

A bundle is a zip holding everything a track needs to land in another
rekordbox library with performance data intact:

    manifest.json         format version, per-track metadata + cues + hashes
    audio/NNN_<filename>  bit-exact copies of the audio files
    anlz/<src_id>/...     ANLZ analysis files (beat grid, waveform, phrases)

Export is read-only (safe while rekordbox runs). Import writes the local
master.db through pyrekordbox only — with the standard machinery: rekordbox
must be quit, fresh backup first, dry-run by default, undo journal after.
Imported audio is COPIED into a destination folder; existing files are never
overwritten and library files are never touched.

Machine paths resolve through rbxpaths (env -> config.json -> probe), same as
the rest of the CLI. Bundles are platform-neutral: the manifest stores no
absolute paths, so a macOS export imports on Windows and vice versa
(Windows: UNTESTED branch, like everywhere else in this repo).
"""

import hashlib
import json
import shutil
import socket
import sys
import tempfile
import zipfile
from datetime import datetime
from pathlib import Path
from uuid import uuid4

import rbxpaths
from rbxpaths import is_local

BUNDLE_FORMAT = "rbx-transfer/1"

# DjmdCue columns that are portable between libraries. Identity, sync and
# timestamp columns are regenerated on import.
CUE_COLS = [
    "InMsec", "InFrame", "InMpegFrame", "InMpegAbs",
    "OutMsec", "OutFrame", "OutMpegFrame", "OutMpegAbs",
    "Kind", "Color", "ColorTableIndex", "ActiveLoop", "Comment",
    "BeatLoopSize", "CueMicrosec", "InPointSeekInfo", "OutPointSeekInfo",
]

# DjmdContent columns copied verbatim (ColorID indexes the fixed 8-color table).
CONTENT_COLS = [
    "BPM", "Length", "TrackNo", "DiscNo", "Rating", "ColorID", "Commnt",
    "ReleaseYear", "ReleaseDate", "Subtitle", "BitRate", "BitDepth",
    "SampleRate", "Analysed",
]

ANLZ_NAMES = {"DAT": "ANLZ0000.DAT", "EXT": "ANLZ0000.EXT", "2EX": "ANLZ0000.2EX"}


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def anlz_dir_for(analysis_data_path: str | None) -> Path | None:
    """Resolve AnalysisDataPath ('/PIONEER/USBANLZ/xxx/yyy/ANLZ0000.DAT')
    against the real rekordbox share dir (get_anlz_dir resolves against the
    DB's own dir, which is wrong when reading from the snapshot copy)."""
    if not analysis_data_path:
        return None
    d = (rbxpaths.rb_share() / analysis_data_path.lstrip("/\\")).parent
    return d if d.is_dir() else None


# ---------------------------------------------------------------- export


def cmd_export(args, rbx):
    """Build a bundle zip from a playlist or explicit content IDs."""
    from pyrekordbox.db6 import tables

    db = rbx.open_db()  # read-only snapshot — safe while rekordbox runs
    tracks = rbx._scope_tracks(db, args)
    if not tracks:
        sys.exit("nothing in scope")
    if not args.playlist and not args.ids:
        sys.exit("need --playlist NAME or --ids (refusing to export the whole collection)")

    label = args.playlist or "tracks"
    out = Path(args.output or f"transfer-{rbx.norm(label).replace(' ', '-')}-{rbx.ts()}.zip")
    if out.exists():
        sys.exit(f"refusing to overwrite existing {out}")

    cues_by_content = {}
    for cue in db.query(tables.DjmdCue):
        cues_by_content.setdefault(str(cue.ContentID), []).append(cue)

    manifest = {
        "format": BUNDLE_FORMAT,
        "created": datetime.now().isoformat(),
        "source": socket.gethostname(),
        "playlist": args.playlist,
        "tracks": [],
    }
    skipped = []
    entries = []  # (zip_name, src_path) queued file copies
    for i, c in enumerate(tracks, 1):
        path = c.FolderPath or ""
        title = f"{c.Artist.Name if c.Artist else '?'} - {c.Title}"
        if not is_local(path):
            skipped.append((title, "streaming track — no local file"))
            continue
        src = Path(path)
        if not src.exists():
            skipped.append((title, f"file missing: {path}"))
            continue

        audio_name = f"audio/{i:03d}_{src.name}"
        entries.append((audio_name, src))

        cues = sorted(
            cues_by_content.get(str(c.ID), []),
            key=lambda q: (q.Kind, q.InMsec or 0),
        )
        anlz = {}
        adir = anlz_dir_for(c.AnalysisDataPath)
        if adir:
            for f in adir.iterdir():
                kind = f.suffix.lstrip(".").upper()
                if kind in ANLZ_NAMES:
                    zn = f"anlz/{c.ID}/{ANLZ_NAMES[kind]}"
                    entries.append((zn, f))
                    anlz[kind] = zn

        manifest["tracks"].append({
            "src_id": str(c.ID),
            "file": audio_name,
            "filename": src.name,
            "sha256": sha256(src),
            "size": src.stat().st_size,
            "meta": {
                "Title": c.Title,
                "Artist": c.Artist.Name if c.Artist else None,
                "Album": c.Album.Name if c.Album else None,
                "Genre": c.Genre.Name if c.Genre else None,
                "Key": c.Key.ScaleName if c.Key else None,
                **{k: getattr(c, k) for k in CONTENT_COLS},
            },
            "cues": [
                {k: getattr(q, k) for k in CUE_COLS} for q in cues
            ],
            "anlz": anlz,
        })

    if not manifest["tracks"]:
        sys.exit("no exportable tracks in scope (all streaming/missing?)")

    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, compresslevel=1) as zf:
        zf.writestr("manifest.json", json.dumps(manifest, indent=2, default=str))
        for zn, src in entries:
            zf.write(src, zn)

    n = len(manifest["tracks"])
    n_hot = sum(1 for t in manifest["tracks"] for q in t["cues"] if q["Kind"] > 0)
    n_mem = sum(1 for t in manifest["tracks"] for q in t["cues"] if q["Kind"] == 0)
    print(f"Bundle: {out}  ({out.stat().st_size / 1e6:.1f} MB)")
    print(f"  {n} tracks, {n_hot} hot cues, {n_mem} memory cues, "
          f"{sum(1 for t in manifest['tracks'] if t['anlz'])} with beat-grid files")
    for t in manifest["tracks"]:
        hot = sum(1 for q in t["cues"] if q["Kind"] > 0)
        print(f"    {t['meta']['Artist'] or '?'} - {t['meta']['Title']}  [{hot} hot cues]")
    if skipped:
        print(f"  SKIPPED {len(skipped)}:")
        for title, why in skipped:
            print(f"    {title}: {why}")


# ---------------------------------------------------------------- inspect


def load_bundle(bundle: Path) -> dict:
    with zipfile.ZipFile(bundle) as zf:
        return json.loads(zf.read("manifest.json"))


def cmd_inspect(args, rbx):
    m = load_bundle(Path(args.bundle))
    print(f"{args.bundle}: {m['format']}, from {m['source']} at {m['created']}")
    print(f"playlist: {m['playlist'] or '(none)'}   tracks: {len(m['tracks'])}")
    for t in m["tracks"]:
        hot = sum(1 for q in t["cues"] if q["Kind"] > 0)
        mem = sum(1 for q in t["cues"] if q["Kind"] == 0)
        grid = "grid" if t["anlz"] else "no-grid"
        print(f"  {t['meta']['Artist'] or '?'} - {t['meta']['Title']}  "
              f"[{hot} hot, {mem} mem, {grid}, {t['size'] / 1e6:.1f} MB]")


# ---------------------------------------------------------------- import


def _get_or_create(db, tables, cls_name: str, name: str | None):
    """Get-or-create a name row (DjmdArtist/DjmdAlbum/DjmdGenre/DjmdKey);
    returns its ID or None."""
    if not name:
        return None
    cls = getattr(tables, cls_name)
    field = cls.ScaleName if cls_name == "DjmdKey" else cls.Name
    row = db.query(cls).filter(field == name).first()
    if row:
        return row.ID
    kwargs = {"ScaleName" if cls_name == "DjmdKey" else "Name": name}
    if cls_name == "DjmdKey":
        kwargs["Seq"] = 0
    row = cls.create(ID=db.generate_unused_id(cls), UUID=str(uuid4()), **kwargs)
    db.add(row)
    db.flush()
    return row.ID


def _install_anlz(zf, track, dest_audio: Path) -> str | None:
    """Extract the track's ANLZ files into a fresh share/PIONEER/USBANLZ dir,
    rewrite their embedded audio path, and return the new AnalysisDataPath."""
    from pyrekordbox.anlz import AnlzFile

    if not track["anlz"]:
        return None
    u = str(uuid4())
    rel_dir = f"/PIONEER/USBANLZ/{u[:3]}/{u[3:]}"
    target = rbxpaths.rb_share() / rel_dir.lstrip("/")
    target.mkdir(parents=True, exist_ok=False)
    for kind, zn in track["anlz"].items():
        fpath = target / ANLZ_NAMES[kind]
        fpath.write_bytes(zf.read(zn))
        try:
            af = AnlzFile.parse_file(str(fpath))
            af.set_path(str(dest_audio))
            af.save(str(fpath))
        except Exception as e:
            print(f"    anlz path rewrite failed for {fpath.name}: {e} "
                  "(rekordbox will re-analyze)", file=sys.stderr)
    return f"{rel_dir}/{ANLZ_NAMES['DAT']}"


def cmd_import(args, rbx):
    from pyrekordbox.db6 import tables

    bundle = Path(args.bundle)
    if not bundle.exists():
        sys.exit(f"no such bundle: {bundle}")
    m = load_bundle(bundle)
    if m["format"] != BUNDLE_FORMAT:
        sys.exit(f"unsupported bundle format {m['format']!r} (expected {BUNDLE_FORMAT})")

    dest_dir = Path(args.dest or Path.home() / "Music" / "rbx-imports" / bundle.stem).expanduser()

    # plan + verify against the current library (read-only pass)
    db = rbx.open_db()
    existing_paths = {c.FolderPath for c in db.get_content()}
    plan, blocked = [], []
    for t in m["tracks"]:
        dest = dest_dir / t["filename"]
        label = f"{t['meta']['Artist'] or '?'} - {t['meta']['Title']}"
        if str(dest) in existing_paths:
            blocked.append((label, f"already in collection: {dest}"))
        elif dest.exists():
            blocked.append((label, f"file already exists on disk: {dest}"))
        else:
            plan.append((t, dest, label))

    pl_name = None
    if m["playlist"] and not args.no_playlist:
        pl_name = m["playlist"]
        if db.get_playlist(Name=pl_name).first() is not None:
            pl_name = f"{pl_name} (transfer {rbx.ts()})"

    print(f"IMPORT plan for {bundle.name} -> {dest_dir}")
    for t, dest, label in plan:
        hot = sum(1 for q in t["cues"] if q["Kind"] > 0)
        mem = sum(1 for q in t["cues"] if q["Kind"] == 0)
        grid = "grid" if t["anlz"] else "re-analyze"
        print(f"  + {label}  [{hot} hot, {mem} mem, {grid}]")
    for label, why in blocked:
        print(f"  ! SKIP {label}: {why}")
    if pl_name:
        print(f"  playlist: will create {pl_name!r}")
    if not plan:
        sys.exit("nothing to import")
    if not args.apply:
        print("\nDRY RUN — nothing written. Verify the plan, then re-run with --apply.")
        return

    # verify hashes before touching anything
    print("\nVerifying hashes...")
    tmp = Path(tempfile.mkdtemp(prefix="rbx-transfer-"))
    try:
        with zipfile.ZipFile(bundle) as zf:
            for t, dest, label in plan:
                p = tmp / t["file"]
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_bytes(zf.read(t["file"]))
                if sha256(p) != t["sha256"]:
                    sys.exit(f"HASH MISMATCH for {label} — bundle corrupt, aborting before any write")

            db = rbx.open_db(write=True)  # refuses if rekordbox is running
            bdir = rbx.backup_now()
            print(f"Backup: {bdir}")
            dest_dir.mkdir(parents=True, exist_ok=True)
            journal = {"cmd": "transfer-import", "time": datetime.now().isoformat(),
                       "bundle": str(bundle), "entries": [], "playlist_id": None}
            imported = []
            for t, dest, label in plan:
                shutil.copy2(tmp / t["file"], dest)
                meta = t["meta"]
                content = db.add_content(
                    str(dest), Title=meta["Title"],
                    **{k: meta.get(k) for k in CONTENT_COLS if meta.get(k) is not None},
                )
                content.ArtistID = _get_or_create(db, tables, "DjmdArtist", meta["Artist"])
                content.AlbumID = _get_or_create(db, tables, "DjmdAlbum", meta["Album"])
                content.GenreID = _get_or_create(db, tables, "DjmdGenre", meta["Genre"])
                content.KeyID = _get_or_create(db, tables, "DjmdKey", meta["Key"])

                cue_ids = []
                for q in t["cues"]:
                    cue = tables.DjmdCue.create(
                        ID=db.generate_unused_id(tables.DjmdCue),
                        ContentID=content.ID, ContentUUID=content.UUID,
                        UUID=str(uuid4()), **q,
                    )
                    db.add(cue)
                    cue_ids.append(str(cue.ID))
                db.flush()

                adp = None if args.no_anlz else _install_anlz(zf, t, dest)
                if adp:
                    content.AnalysisDataPath = adp
                else:
                    content.Analysed = 0  # make rekordbox analyze on first load

                journal["entries"].append({
                    "content_id": str(content.ID), "cue_ids": cue_ids,
                    "audio": str(dest), "anlz_data_path": adp,
                })
                imported.append((content, label))
                print(f"  imported {label} ({len(cue_ids)} cues)")

            if pl_name and imported:
                pl = db.create_playlist(pl_name)
                for content, _ in imported:
                    db.add_to_playlist(pl, content)
                journal["playlist_id"] = str(pl.ID)
                print(f"  created playlist {pl_name!r} ({len(imported)} tracks)")
            db.commit()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    rbx.UNDO_DIR.mkdir(exist_ok=True)
    jpath = rbx.UNDO_DIR / f"transfer-import-{rbx.ts()}.json"
    jpath.write_text(json.dumps(journal, indent=2, default=str))
    print(f"\nImported {len(imported)} tracks. Undo journal: {jpath}")
    print("Reopen rekordbox to verify; `rbx undo` removes the imported rows "
          "(copied audio files stay on disk).")


def undo_import(db, journal, rbx):
    """Reverse a transfer-import batch: delete the created DB rows and the
    installed ANLZ dirs. Copied audio files stay on disk (hard rule)."""
    from pyrekordbox.db6 import tables

    removed = 0
    for e in journal["entries"]:
        c = rbx.content_by_id(db, e["content_id"])
        if c is None:
            print(f"  skipped content {e['content_id']} (already gone)")
            continue
        for cue in db.query(tables.DjmdCue).filter(tables.DjmdCue.ContentID == c.ID):
            db.delete(cue)
        for sp in db.query(tables.DjmdSongPlaylist).filter(
                tables.DjmdSongPlaylist.ContentID == c.ID):
            db.delete(sp)
        db.delete(c)
        if e.get("anlz_data_path"):
            adir = rbxpaths.rb_share() / e["anlz_data_path"].lstrip("/\\")
            shutil.rmtree(adir.parent, ignore_errors=True)
        removed += 1
    if journal.get("playlist_id"):
        try:
            db.delete_playlist(journal["playlist_id"])
        except Exception as e:
            print(f"  could not delete playlist: {e}")
    db.commit()
    print(f"Removed {removed} imported tracks (audio files left on disk).")


def dispatch(args, rbx):
    {"export": cmd_export, "import": cmd_import, "inspect": cmd_inspect}[args.tr_cmd](args, rbx)
