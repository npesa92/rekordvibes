"""`rbx usb` — rekordbox USB export checker + Serato bootstrap.

  usb check DRIVE    read-only integrity ladder over a rekordbox device export
  usb serato DRIVE   create _Serato_/ (database V2 + crates) from export.pdb;
                     --cues additionally writes Serato cue/grid tags onto the
                     stick's audio copies (from the stick's own ANLZ files)

Rails: `check` never writes a byte. `serato` writes only _Serato_/ and (with
--cues) embedded tag headers on the stick's audio files. PIONEER/, the pdb
files, and audio streams are never modified.
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import rbxpdb
import serato

# ANLZ files contain tags pyrekordbox doesn't know (PVDI etc.) — not our problem
logging.getLogger("pyrekordbox.anlz.file").setLevel(logging.ERROR)

AUDIO_EXTS = {".mp3", ".wav", ".aiff", ".aif", ".flac", ".m4a", ".mp4", ".ogg"}
FAT32_LIMIT = 4 * 1024**3 - 1

# export.pdb records the library DB's file_size, which can lag the real file
# when tags were edited after import — a small deficit with an intact header
# is metadata drift, not a truncated copy
SIZE_DRIFT_ABS = 64 * 1024
SIZE_DRIFT_FRAC = 0.001

SUPPORTED_FS = {
    "msdos": "FAT32", "fat32": "FAT32", "exfat": "exFAT",
    "hfs": "HFS+", "vfat": "FAT32",
}


@dataclass
class Report:
    findings: list = field(default_factory=list)  # (severity, code, message)
    stats: dict = field(default_factory=dict)

    def fail(self, code, msg):
        self.findings.append(("fail", code, msg))

    def warn(self, code, msg):
        self.findings.append(("warn", code, msg))

    def info(self, code, msg):
        self.findings.append(("info", code, msg))

    @property
    def failed(self):
        return any(s == "fail" for s, _, _ in self.findings)


def _drive_root(arg: str) -> Path:
    root = Path(arg).expanduser().resolve()
    if not root.is_dir():
        sys.exit(f"not a directory: {root}")
    return root


def _export_pdb(root: Path) -> Path:
    return root / "PIONEER" / "rekordbox" / "export.pdb"


def _load_pdb(root: Path, rep: Report):
    pdb_path = _export_pdb(root)
    if not (root / "PIONEER").is_dir():
        rep.fail("structure", f"no PIONEER/ folder — {root} is not a rekordbox export")
        return None
    if not pdb_path.is_file():
        rep.fail("structure", "PIONEER/rekordbox/export.pdb missing")
        return None
    try:
        lib = rbxpdb.parse_pdb(pdb_path)
    except (rbxpdb.PdbError, OSError) as e:
        rep.fail("pdb", f"export.pdb unreadable: {e}")
        return None
    if lib.row_errors:
        for table, page, slot, msg in lib.row_errors[:10]:
            rep.warn("pdb-row", f"{table} page {page} slot {slot}: {msg}")
        if len(lib.row_errors) > 10:
            rep.warn("pdb-row", f"... and {len(lib.row_errors) - 10} more damaged rows")
    if not lib.tracks:
        rep.fail("pdb", "export.pdb parsed but contains zero tracks")
    return lib


def _track_audio(root: Path, t: dict) -> Path:
    return root / t["file_path"].lstrip("/\\")


def _audio_matches_pdb(path: Path, t: dict) -> bool:
    """Header parses as audio and decoded duration agrees with the pdb (±5 s)."""
    import mutagen

    try:
        mf = mutagen.File(str(path))
    except Exception:
        return False
    if mf is None:
        return False
    length = getattr(mf.info, "length", 0) or 0
    return not t["duration"] or abs(length - t["duration"]) <= 5


def _fs_info(root: Path) -> tuple[str | None, str | None]:
    """(filesystem personality, volume label) — macOS only, best effort."""
    if sys.platform != "darwin":
        return None, None
    try:
        out = subprocess.run(["diskutil", "info", "-plist", str(root)],
                             capture_output=True, timeout=15).stdout
        import plistlib

        info = plistlib.loads(out)
        return (info.get("FilesystemName") or info.get("FilesystemType"),
                info.get("VolumeName"))
    except Exception:
        return None, None


# ---------------------------------------------------------------- check


def cmd_check(args, rbx):
    root = _drive_root(args.drive)
    rep = Report()
    lib = _load_pdb(root, rep)

    if lib:
        rep.stats["tracks"] = len(lib.tracks)
        rep.stats["playlists"] = sum(1 for p in lib.playlists if not p["is_folder"])

        # --- cross-reference: audio files
        n_missing = n_sizemism = n_drift = 0
        for t in lib.tracks:
            audio = _track_audio(root, t)
            if not audio.is_file():
                n_missing += 1
                if n_missing <= 15:
                    rep.fail("audio-missing", f"track {t['id']} ({t['title'] or t['filename']}): "
                                              f"{t['file_path']} not on drive")
                continue
            actual = audio.stat().st_size
            if actual == 0:
                n_sizemism += 1
                rep.fail("audio-empty", f"{t['file_path']}: zero bytes")
            elif t["file_size"] and actual < t["file_size"]:
                deficit = t["file_size"] - actual
                small = deficit <= max(SIZE_DRIFT_ABS, int(t["file_size"] * SIZE_DRIFT_FRAC))
                if small and _audio_matches_pdb(audio, t):
                    n_drift += 1
                    if n_drift <= 15:
                        rep.warn("audio-size-drift",
                                 f"{t['file_path']}: {deficit} bytes smaller than pdb's "
                                 f"{t['file_size']}, but header parses and duration matches "
                                 "(stale library size in pdb, not truncation)")
                else:
                    n_sizemism += 1
                    if n_sizemism <= 15:
                        rep.fail("audio-size", f"{t['file_path']}: {actual} bytes on drive, "
                                               f"pdb says {t['file_size']} (truncated copy?)")
            elif t["file_size"] and actual > t["file_size"]:
                # tags were added after export (e.g. `usb serato --cues`) or the
                # pdb carries a stale library size — growth is benign either way;
                # truncation is the corruption signal
                rep.info("audio-grew", f"{t['file_path']}: {actual - t['file_size']} bytes "
                                       "larger than at export time (tags added?)")
        if n_missing > 15:
            rep.fail("audio-missing", f"... and {n_missing - 15} more missing files")
        if n_sizemism > 15:
            rep.fail("audio-size", f"... and {n_sizemism - 15} more size mismatches")
        if n_drift > 15:
            rep.warn("audio-size-drift", f"... and {n_drift - 15} more small size drifts")

        # --- cross-reference: ANLZ analysis files
        n_anlz_missing = n_anlz_bad = 0
        try:
            from pyrekordbox.anlz import AnlzFile
        except ImportError:
            AnlzFile = None
            rep.warn("anlz", "pyrekordbox not importable — skipping ANLZ parse checks")
        for t in lib.tracks:
            if not t["analyze_path"]:
                rep.warn("anlz-none", f"track {t['id']} ({t['title']}): no analysis path")
                continue
            dat = root / t["analyze_path"].lstrip("/\\")
            ext = dat.with_suffix(".EXT")
            if not dat.is_file():
                n_anlz_missing += 1
                if n_anlz_missing <= 10:
                    rep.fail("anlz-missing", f"track {t['id']}: {t['analyze_path']} missing "
                                             "(no beat grid/waveform on players)")
                continue
            if AnlzFile is not None:
                for p in (dat, ext):
                    if not p.is_file():
                        continue
                    try:
                        AnlzFile.parse_file(str(p))
                    except Exception as e:
                        n_anlz_bad += 1
                        if n_anlz_bad <= 10:
                            rep.fail("anlz-corrupt", f"{p.relative_to(root)}: parse failed ({e})")
        if n_anlz_missing > 10:
            rep.fail("anlz-missing", f"... and {n_anlz_missing - 10} more")
        if n_anlz_bad > 10:
            rep.fail("anlz-corrupt", f"... and {n_anlz_bad - 10} more")

        # --- cross-reference: playlist entries
        ids = {t["id"] for t in lib.tracks}
        pl_ids = {p["id"] for p in lib.playlists}
        dangling = [e for e in lib.playlist_entries if e["track_id"] not in ids]
        if dangling:
            rep.fail("playlist-dangling",
                     f"{len(dangling)} playlist entries point at nonexistent tracks")
        orphan_pl = [e for e in lib.playlist_entries if e["playlist_id"] not in pl_ids]
        if orphan_pl:
            rep.warn("playlist-orphan",
                     f"{len(orphan_pl)} playlist entries belong to no playlist row")

        # --- orphaned audio files on the drive
        referenced = {str(_track_audio(root, t)) for t in lib.tracks}
        orphans = []
        for p in root.rglob("*"):
            parts = p.parts
            if "PIONEER" in parts or "_Serato_" in parts or p.name.startswith("."):
                continue
            if p.is_file() and p.suffix.lower() in AUDIO_EXTS and str(p) not in referenced:
                orphans.append(p)
        if orphans:
            rep.info("orphans", f"{len(orphans)} audio files on drive not referenced by "
                                f"export.pdb (first: {orphans[0].relative_to(root)})")

        # --- audio integrity
        _audio_integrity(root, lib, rep, deep=args.deep)

    # --- device sanity
    fs, label = _fs_info(root)
    if fs:
        pers = fs.lower().replace(" ", "")
        known = next((v for k, v in SUPPORTED_FS.items() if k in pers), None)
        if known:
            rep.info("filesystem", f"{fs} ({known}) — CDJ-readable")
        else:
            rep.warn("filesystem", f"filesystem '{fs}' — most CDJs need FAT32/exFAT (HFS+ on newer models)")
        if known == "FAT32" and lib:
            over = [t for t in lib.tracks if t["file_size"] > FAT32_LIMIT]
            for t in over:
                rep.fail("fat32-limit", f"{t['file_path']}: {t['file_size']} bytes exceeds FAT32 4 GB limit")
        rep.stats["filesystem"] = fs
    if label is not None:
        if label:
            rep.info("label", f"volume label: {label}")
        else:
            rep.warn("label", "volume has no label (some players display it)")

    _emit(rep, args, root)


def _audio_integrity(root: Path, lib, rep: Report, deep: bool):
    import mutagen

    n_bad = n_dur = 0
    files = [(t, _track_audio(root, t)) for t in lib.tracks]
    files = [(t, p) for t, p in files if p.is_file()]
    ffmpeg = shutil.which("ffmpeg") if deep else None
    if deep and not ffmpeg:
        rep.warn("deep", "--deep requested but ffmpeg not found (brew install ffmpeg) — "
                         "falling back to quick checks")
    for i, (t, p) in enumerate(files, 1):
        try:
            mf = mutagen.File(str(p))
        except Exception as e:
            n_bad += 1
            rep.fail("audio-header", f"{t['file_path']}: unreadable ({e})")
            continue
        if mf is None:
            n_bad += 1
            rep.fail("audio-header", f"{t['file_path']}: not recognized as audio")
            continue
        length = getattr(mf.info, "length", 0) or 0
        if t["duration"] and abs(length - t["duration"]) > 5:
            n_dur += 1
            if n_dur <= 10:
                rep.fail("audio-truncated",
                         f"{t['file_path']}: decodes to {length:.0f}s, pdb says "
                         f"{t['duration']}s (truncated or corrupt)")
        if ffmpeg:
            r = subprocess.run([ffmpeg, "-v", "error", "-xerror", "-i", str(p),
                                "-f", "null", "-"], capture_output=True, text=True)
            if r.returncode != 0:
                n_bad += 1
                first = (r.stderr or "").strip().splitlines()
                rep.fail("audio-decode", f"{t['file_path']}: decode error"
                                         + (f" ({first[0][:120]})" if first else ""))
            if i % 25 == 0 or i == len(files):
                print(f"  [deep decode {i}/{len(files)}]", file=sys.stderr, flush=True)
    if n_dur > 10:
        rep.fail("audio-truncated", f"... and {n_dur - 10} more duration mismatches")
    rep.stats["audio_checked"] = len(files)
    rep.stats["audio_mode"] = "deep" if ffmpeg else "quick"


def _emit(rep: Report, args, root: Path):
    if args.json:
        print(json.dumps({"drive": str(root), "stats": rep.stats,
                          "findings": [{"severity": s, "code": c, "message": m}
                                       for s, c, m in rep.findings],
                          "ok": not rep.failed}, indent=2))
    else:
        order = {"fail": 0, "warn": 1, "info": 2}
        print(f"=== rbx usb check: {root} ===")
        for k, v in rep.stats.items():
            print(f"  {k}: {v}")
        print()
        for sev in ("fail", "warn", "info"):
            group = [f for f in rep.findings if f[0] == sev]
            if not group:
                continue
            print(f"--- {sev.upper()} ({len(group)})")
            for _, code, msg in sorted(group, key=lambda f: f[1]):
                print(f"  [{code}] {msg}")
            print()
        n_fail = sum(1 for s, _, _ in rep.findings if s == "fail")
        print("RESULT: " + ("FAIL" if rep.failed else "OK")
              + (f" — {n_fail} failure(s)" if n_fail else " — stick looks healthy"))
    sys.exit(1 if rep.failed else 0)


# ---------------------------------------------------------------- serato


def _stick_cues(root: Path, t: dict) -> tuple[list, list | None, float | None]:
    """Hot cues + beat grid for one track, read from the stick's ANLZ files.
    Returns (cues, grid_markers, end_bpm); cues use serato.markers2 dict shape."""
    from pyrekordbox.anlz import AnlzFile

    if not t["analyze_path"]:
        return [], None, None
    dat = root / t["analyze_path"].lstrip("/\\")
    ext = dat.with_suffix(".EXT")
    cues, grid, end_bpm = [], None, None
    for p in (ext, dat):  # EXT first: PCO2 has colors + comments
        if not p.is_file():
            continue
        try:
            f = AnlzFile.parse_file(str(p))
        except Exception:
            continue
        if not cues:
            cues = _cues_from_anlz(f)
        if grid is None:
            grid, end_bpm = _grid_from_anlz(f)
        if cues and grid is not None:
            break
    return cues, grid, end_bpm


def _cues_from_anlz(f) -> list:
    """Extract hot cues from PCO2 (preferred: colors/comments) or PCOB tags."""
    best = []
    for tag in f.tags:
        if tag.type not in ("PCO2", "PCOB"):
            continue
        c = tag.struct.content
        # PCOB names the list kind "cue_type", PCO2 names it "type"
        if "hot" not in str(c.get("cue_type") or c.get("type") or ""):
            continue
        out = []
        for e in c.entries:
            hot = int(e.get("hot_cue", 0) or 0)
            if not 1 <= hot <= 8:
                continue
            cue = {"index": hot - 1, "ms": int(e.get("time", 0) or 0), "name": ""}
            if "color_id" in e and e.get("color_id"):
                cue["color"] = serato.cue_color(int(e["color_id"]))
            elif all(k in e for k in ("color_red", "color_green", "color_blue")):
                cue["color"] = (int(e["color_red"]) << 16 | int(e["color_green"]) << 8
                                | int(e["color_blue"]))
            else:
                cue["color"] = serato.DEFAULT_CUE_COLOR
            if e.get("comment"):
                cue["name"] = str(e["comment"]).rstrip("\x00")
            out.append(cue)
        if len(out) > len(best):
            best = out
    return best


def _grid_from_anlz(f) -> tuple[list | None, float | None]:
    if "PQTZ" not in f.tag_types:
        return None, None
    beats, bpms, times = f.get_tag("PQTZ").get()
    return serato.grid_to_serato_markers([float(t) for t in times],
                                         [float(b) for b in bpms])


def cmd_serato(args, rbx):
    root = _drive_root(args.drive)
    sdir = root / "_Serato_"

    if args.wipe:
        if not sdir.exists():
            sys.exit(f"nothing to wipe: {sdir} does not exist")
        if not args.apply:
            n = sum(1 for _ in sdir.rglob("*"))
            print(f"DRY RUN — would remove {sdir} ({n} entries). Re-run with --apply.")
            return
        shutil.rmtree(sdir)
        print(f"removed {sdir} — stick is rekordbox-only again "
              "(tag data from --cues, if any, is undone via `rbx undo`)")
        return

    rep = Report()
    lib = _load_pdb(root, rep)
    if lib is None or not lib.tracks:
        for s, c, m in rep.findings:
            print(f"[{s}] [{c}] {m}")
        sys.exit("cannot build Serato library: export.pdb unusable")

    # tracks that actually exist on the stick
    tracks = [t for t in lib.tracks if _track_audio(root, t).is_file()]
    skipped = len(lib.tracks) - len(tracks)
    if skipped:
        print(f"note: {skipped} pdb tracks have no audio file on the stick — excluded "
              "(run `rbx usb check` for details)")

    # playlist tree -> crates (folders become %% prefixes)
    by_id = {p["id"]: p for p in lib.playlists}
    entries_by_pl = {}
    for e in lib.playlist_entries:
        entries_by_pl.setdefault(e["playlist_id"], []).append(e)
    track_by_id = {t["id"]: t for t in tracks}

    def folder_chain(p):
        names, cur, hops = [], p, 0
        while cur["parent_id"] and cur["parent_id"] in by_id and hops < 10:
            cur = by_id[cur["parent_id"]]
            names.append(cur["name"])
            hops += 1
        return list(reversed(names))

    wanted = None
    if args.playlist:
        wanted = {n.strip().lower() for n in args.playlist}
    crates = []  # (filename, [relpaths])
    for p in lib.playlists:
        if p["is_folder"]:
            continue
        if wanted and p["name"].strip().lower() not in wanted:
            continue
        es = sorted(entries_by_pl.get(p["id"], []), key=lambda e: e["entry_index"])
        paths = [track_by_id[e["track_id"]]["file_path"].lstrip("/\\")
                 for e in es if e["track_id"] in track_by_id]
        if paths:
            crates.append((serato.crate_filename(folder_chain(p), p["name"]), paths))

    db_tracks = [{
        "path": t["file_path"].lstrip("/\\"),
        "title": t["title"], "artist": lib.artists.get(t["artist_id"], ""),
        "album": lib.albums.get(t["album_id"], ""), "genre": lib.genres.get(t["genre_id"], ""),
        "key": lib.keys.get(t["key_id"], ""),
        "bpm": (t["tempo_100"] or 0) / 100 or None,
        "length_ms": (t["duration"] or 0) * 1000 or None,
    } for t in tracks]

    print(f"Serato library for {root}:")
    print(f"  database V2 : {len(db_tracks)} tracks")
    print(f"  crates      : {len(crates)}")
    for name, paths in crates[:20]:
        print(f"    {name[:-6]:40s} {len(paths)} tracks")
    if len(crates) > 20:
        print(f"    ... and {len(crates) - 20} more")

    cue_plan = []
    if args.cues:
        for t in tracks:
            cues, grid, end_bpm = _stick_cues(root, t)
            if cues or grid:
                cue_plan.append((t, cues, grid, end_bpm))
        n_cues = sum(len(c) for _, c, _, _ in cue_plan)
        print(f"  cue tags    : {len(cue_plan)} files would get Serato tags "
              f"({n_cues} hot cues, {sum(1 for _, _, g, _ in cue_plan if g)} beat grids)")
        if not cue_plan:
            print("    (no hot cues found in the stick's ANLZ files)")

    if not args.apply:
        print("\nDRY RUN — nothing written. Re-run with --apply.")
        return

    (sdir / "Subcrates").mkdir(parents=True, exist_ok=True)
    (sdir / "database V2").write_bytes(serato.build_database(db_tracks))
    for name, paths in crates:
        (sdir / "Subcrates" / name).write_bytes(serato.build_crate(paths))
    print(f"\nwrote {sdir / 'database V2'} and {len(crates)} crates")

    if args.cues and cue_plan:
        journal = {"cmd": "serato-tags", "time": datetime.now().isoformat(),
                   "drive": str(root), "entries": []}
        n_ok = n_err = 0
        for t, cues, grid, end_bpm in cue_plan:
            path = str(_track_audio(root, t))
            payloads = {}
            if cues:
                payloads[serato.MARKERS2_NAME] = serato.markers2_payload(cues)
            if grid:
                payloads[serato.BEATGRID_NAME] = serato.beatgrid_payload(grid, end_bpm)
            try:
                old = serato.write_serato_tags(path, payloads)
            except Exception as e:
                n_err += 1
                print(f"  tag write failed: {t['file_path']}: {e}", file=sys.stderr)
                continue
            if old is None:
                n_err += 1
                print(f"  unsupported container, skipped: {t['file_path']}", file=sys.stderr)
                continue
            journal["entries"].append({
                "path": path,
                "old_serato": {k: (v.hex() if v else None) for k, v in old.items()
                               if k in payloads},
            })
            n_ok += 1
        rbx.UNDO_DIR.mkdir(exist_ok=True)
        jpath = rbx.UNDO_DIR / f"serato-tags-{rbx.ts()}.json"
        jpath.write_text(json.dumps(journal, indent=2))
        print(f"tagged {n_ok} files ({n_err} failed). Undo journal: {jpath}")
    print("Done — eject cleanly, then open the drive in Serato DJ (Files panel).")


def dispatch(args, rbx):
    {"check": cmd_check, "serato": cmd_serato}[args.usb_cmd](args, rbx)
