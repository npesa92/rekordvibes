# Spec: `rekordvibes` skill

## 1. Purpose

A skill that lets Claude safely manage the local rekordbox 7 collection.
The headline problem: many tracks have **polluted metadata** — the song Title
field contains the whole filename (artist, song, album, junk suffixes), so
track data isn't correctly split across Artist / Title / Album. The skill
parses and fixes that, writing corrections to **both the rekordbox database
and the audio files' embedded tags (ID3/MP4)**.

**Hard rule: audio files are never renamed or moved.** File paths on disk stay
exactly as they are, so rekordbox never has to re-find a track. Only metadata
(DB fields + embedded tags) changes.

### Confirmed decisions

- Tag writes: **DB + file tags** (mutagen for ID3/MP4/FLAC)
- Cloud Library Sync: **not used** — local-only library, no sync conflicts
- v1 scope: **all capabilities below**, including a recommendations mode

## 2. Environment (reference — verify with `rbx setup`)

> The table below describes the machine this skill was developed on.
> On any other machine, paths and versions are discovered by
> `scripts/rbxpaths.py` (env var -> config.json -> platform probe);
> run `rbx setup` for the live values.

| Item | Value |
|---|---|
| App | rekordbox 7.2.16, `/Applications/rekordbox 7/` |
| Library DB | `~/Library/Pioneer/rekordbox/master.db` (54 MB, SQLCipher-encrypted SQLite, WAL mode) |
| App's own backups | `master.backup.db`, `.backup2`, `.backup3` |
| Key tables | `djmdContent` (tracks; `Title`, `FolderPath`, FKs to artist/album/genre), `djmdArtist`, `djmdAlbum`, `djmdGenre`, `djmdPlaylist` + `djmdSongPlaylist`, `djmdCue`, `djmdHistory` |
| Access library | [pyrekordbox](https://github.com/dylanljones/pyrekordbox) — handles SQLCipher key and `rb_local_usn` sync-counter bookkeeping |

## 3. Capabilities

### C1 — Metadata cleanup (headline)

Fix tracks whose Title (or other fields) contain concatenated filename data.

- **Parse rules**: split patterns like `Artist - Title`, `Artist - Title (Album)`,
  `01. Artist - Title`, `Artist_-_Title_[label]`; strip junk suffixes
  ("(Official Video)", "[FREE DL]", "320kbps", "master", file extensions);
  normalize separators and whitespace.
- **Field routing**: parsed pieces go to the right places — Artist → `djmdArtist`
  link, Album → `djmdAlbum` link, clean song name → `Title`. Existing correct
  fields are never overwritten (only fill/replace what's wrong, configurable).
- **feat. handling**: normalize "ft./feat/featuring" into a consistent form and
  place (artist field vs. title suffix — default: keep in Artist).
- **Casing**: optional title-case / preserve-as-is per run.
- **Confidence tiers**: each proposed fix is scored. Unambiguous parses
  (clear `A - B` with a known artist name) can be batch-approved; ambiguous
  ones (multiple hyphens, no separator) are shown individually.
- Every run: **dry-run table first** (current → proposed, per field), user
  confirms, then apply. Nothing writes without confirmation.

### C2 — Tag sync (DB ↔ files)

- Apply C1 corrections to embedded tags too (ID3v2 for MP3, MP4 atoms for AAC,
  Vorbis for FLAC) via mutagen. File **content** changes (tag header), file
  **name/path** never does — rekordbox matches by path, so nothing is lost.
- Direction control: DB→files (default), files→DB (import cleanup), or
  report-only diff of where DB and embedded tags disagree.

### C3 — Playlists (additive-only over a protected baseline)

- **Init snapshot**: `rbx init` (first run, one-time) copies the current
  `master.db` (+ WAL) to a permanent baseline snapshot under
  `<rekordbox dir>/skill-backups/baseline-<date>/` — never pruned,
  separate from the rolling pre-write backups — and records a **manifest** of
  every playlist/folder that exists at that moment (IDs, names, track
  memberships).
- **Baseline playlists are frozen**: the skill refuses to rename, delete,
  reorder, or add/remove tracks in any playlist in the manifest. They are
  read-only forever (exportable, queryable — just never modified).
- **Additive only**: the skill may create *new* playlists and folders and
  freely manage those (they're tracked as skill-created and stay editable).
- Build new playlists from queries ("all 174 BPM added in the last 30 days").
- M3U8 export of any playlist; M3U8 import → new playlist only.
- Note: C1 metadata cleanup edits track fields, which changes how tracks
  *display* inside baseline playlists, but never touches playlist structure or
  membership. Restoring the baseline snapshot recovers pre-cleanup metadata too.

### C4 — Hygiene doctor

- **Missing files**: DB rows whose path no longer exists; offer relink by
  filename/duration match.
- **Duplicates**: same artist+title+duration (or file hash); report which copy
  is in playlists. Removal is opt-in, DB-row-only or move-file-to-Trash — never
  a hard delete.
- **Untracked files**: audio in your music folders not in the collection.
- **Tag/DB drift**: tracks where embedded tags disagree with DB.

### C5 — Recommendations mode (`rbx recommend`)

The "what should I do?" entry point. Scans the collection and produces a
prioritized report, e.g.:

- "312 tracks look like the Title contains `Artist - Title` — run cleanup?"
- "48 tracks have empty Album but the filename contains one"
- "17 probable duplicates, 9 of them in active playlists"
- "23 missing files, likely moved from ~/Downloads"
- "Inconsistent artist spellings: 'Chase & Status' vs 'Chase and Status'"

Each recommendation maps to a concrete command the user can approve.

### C5b — Delegated model resolve (`rbx resolve` + `clean --verdicts`)

Ambiguous parses (medium/low tiers, and remixer-rule hits like "(X Flip)"
where X could be the producer *or* the famous source artist) need world
knowledge. Rather than an API integration, the skill uses **agent
delegation**: `rbx resolve export` dumps the uncertain cases to
`resolve/cases-<ts>.json`; Claude spawns a Sonnet subagent (Agent tool) with
the prompt in `RESOLVE_PROMPT.md`; the subagent writes
`resolve/verdicts-<ts>.json`; `rbx clean --verdicts FILE` schema-validates it
(whole-file rejection on any malformed entry) and feeds the verdicts through
the identical dry-run → confirm → backup → apply → undo pipeline. The
subagent never touches the DB or any other file.

### C7 — Track analysis (`rbx analyze` + similar/mixable/clusters)

One scoped command runs **all four lanes** per track, cached in `analysis.db`
(SQLite sidecar in the skill dir — never master.db), keyed on content ID +
file mtime, resumable and incremental. Naming convention: build lanes are
named for the data they produce, query commands for the question they answer.

Lanes:
- **grid** — reads rekordbox's own ANLZ analysis: beat grid (PQTZ), phrase
  structure (PSSI: mood, intro/verse/chorus/outro with beat positions,
  intro/outro lengths), waveform energy curve (PWV3, downsampled to 100
  points). Works for streaming tracks too. Near-instant.
- **rhythm** — drum-pattern fingerprint from the audio: percussive layer
  (HPSS), onsets split into low (kick) and high (snare/hat) bands, snapped to
  the rekordbox beat grid, per-bar 16-step vectors + pattern string like
  `X..x..X.........`. Local files only.
- **timbre** — sound-texture vector (13 MFCC mean+std, spectral centroid/
  rolloff/flatness, ZCR). Tempo-independent "sounds like".
- **audioid** — chromaprint fingerprint via fpcalc (`brew install
  chromaprint`); identifies identical recordings across files/bitrates.

Queries:
- `rbx similar TRACK [--by rhythm|timbre|both]` — ranked cosine-distance list.
- `rbx mixable TRACK` — Camelot key compatibility (map handles both `8A` and
  `Am` notations) + BPM range incl. half/double-time + intro-length bonus +
  energy handoff (target's outro energy vs candidate's intro energy).
- `rbx clusters [--playlists]` — leader-clustering on rhythm distance within
  BPM bands; `--playlists` materializes clusters as `[rbx] rhythm ...`
  playlists (additive-only; undo deletes them).
- TRACK arg = content ID or fuzzy "artist - title" match.

Known gotcha: pyrekordbox `AnlzFile.__len__` infinitely recurses — never
truthiness-test an AnlzFile, use `is not None`.

### C6 — Query & export

- Free-form read-only questions via SQL/ORM ("tracks over 130 BPM in 8A").
- CSV/JSON export of the collection or any subset.

### C8 — Library-to-library transfer (`rbx transfer`)

Move tracks to another person's rekordbox library with performance data
intact. Design decision: **never write the recipient's DB from the outside
world** — the bundle is inert data, and the import runs on the recipient's
machine through this same CLI, under the same rails (baseline required,
rekordbox quit, backup, dry-run, undo journal).

Bundle = one zip, platform-neutral (no absolute paths inside):
- `manifest.json` — format version (`rbx-transfer/1`), per-track metadata
  (title/artist/album/genre/key + verbatim DjmdContent columns), all DjmdCue
  rows (portable columns only; identity/sync columns regenerated on import),
  SHA-256 per audio file.
- `audio/NNN_<name>` — bit-exact copies. `anlz/<id>/` — beat grid/waveform/
  phrase files.

Commands:
- `transfer export --playlist|--ids -o zip` — read-only; skips streaming and
  missing tracks with a warning; refuses whole-collection export.
- `transfer inspect zip` — contents listing.
- `transfer import zip [--dest DIR] [--apply]` — hash-verify before any
  write; copy audio into dest (never overwrite; skip tracks already in the
  collection); create rows via pyrekordbox (`add_content`, get-or-create
  artist/album/genre/key, cue rows); install ANLZ files under a fresh
  `share/PIONEER/USBANLZ/<uuid>` dir with the embedded audio path rewritten
  (`AnlzFile.set_path`) so grid/waveform survive — on any ANLZ failure, fall
  back to `Analysed=0` and let rekordbox re-analyze; recreate the bundle's
  playlist (post-baseline, so editable). Undo deletes created rows + ANLZ
  dirs; copied audio stays on disk.

Not transferred (by design/format): MyTags, play counts, histories, mixer
params, intelligent playlists.

### C9 — USB checker + Serato setup (`rbx usb`)

Operates on a **rekordbox device export** (a USB prepared via rekordbox's
"Export to device"), not on master.db. Two subcommands: a read-only
integrity check, and an opt-in Serato bootstrap so the same stick works in
Serato DJ.

What's on such a USB (all reverse-engineered, well documented by
Deep Symmetry's crate-digger project):

- `PIONEER/rekordbox/export.pdb` — DeviceSQL database (tracks, artists,
  albums, keys, colors, playlist tree, playlist entries). **Not** SQLCipher;
  a page-based binary format with a public Kaitai Struct definition
  (`rekordbox_pdb.ksy`) that compiles to Python.
- `PIONEER/rekordbox/exportExt.pdb` — rb6+ extension DB (MyTags etc.).
  Parsed if present; failures downgrade to warnings (less-documented format).
- `PIONEER/USBANLZ/…/ANLZ0000.DAT|.EXT|.2EX` — per-track analysis: beat
  grid, waveforms, cue/loop data (PCOB/PCO2), phrase. Parsed with the same
  pyrekordbox `anlz` module C7 already uses.
- `Contents/…` (or user layout) — the audio files, referenced from the pdb
  by drive-relative path.

#### `rbx usb check DRIVE` — read-only integrity report

Verification ladder, cheap to expensive:

1. **Structure**: expected directories present; `export.pdb` exists,
   parses page-by-page, required tables present with sane row counts;
   ANLZ directory present.
2. **Cross-reference**: every track row → audio file exists on the drive,
   non-zero size; ANLZ .DAT/.EXT exist and parse; every playlist entry
   points to an existing track row; report orphaned audio files on the
   drive that no track row references.
3. **Audio integrity** — `--quick` (default): mutagen opens every file,
   header/stream-info sane, file size consistent with duration × bitrate
   (catches truncated copies, the most common USB corruption). Size-rule
   caveat: export.pdb carries the library DB's recorded file_size, which
   can differ by a few bytes/KB from the exported file when tags were
   edited after import — a small deficit (≤ ~64 KB or 0.1%) whose header
   still parses with pdb-matching duration is WARN `audio-size-drift`;
   only larger deficits FAIL as truncation.
   `--deep`: full decode of every file via ffmpeg (`-f null`) to surface
   mid-file corruption; slow, progress-barred, resumable.
4. **Device sanity**: filesystem is FAT32/exFAT/HFS+ (CDJ-readable set,
   with per-model caveats noted), no file over the FAT32 4 GB limit,
   volume has a label.

Output: human-readable report grouped by severity (fail / warn / info) +
`--json` for scripting; non-zero exit code on any fail. **Never writes a
byte to the drive.** Works standalone — no `rbx init`, no master.db, no
rekordbox install needed, so it can vet a stranger's stick before a gig.

#### `rbx usb serato DRIVE` — make the same stick Serato-ready

Serato DJ treats any drive containing a root-level `_Serato_` folder as a
portable library. Two phases:

- **Phase 1 — library + crates (default)**: generate
  `_Serato_/database V2` and one `_Serato_/Subcrates/<name>.crate` per
  rekordbox playlist (folder nesting via Serato's `%%` subcrate naming),
  paths drive-relative, from the same export.pdb the checker parses.
  Both files are simple, well-documented tag-length-value formats
  (documented by the triseratops / serato-tags projects); writers are
  small and live in this repo. Purely **additive**: creates `_Serato_/`
  only, touches nothing else. Re-runnable (regenerates); `--wipe` removes
  the `_Serato_` folder to return the stick to rekordbox-only state.
- **Phase 2 — performance data**: Serato keeps cues and beat grid
  **inside the audio files' tags** — GEOB frames (`Serato Markers2`,
  `Serato BeatGrid`) for MP3, equivalent MP4 atoms / FLAC Vorbis fields
  elsewhere — its database holds only the track list. So performance data
  is delivered by tagging the audio files, and the **primary home for
  that is the library, not the stick**: a new tagsync lane
  (`rbx tagsync --serato`) writes/refreshes Serato cue+grid frames on the
  local library files from master.db/ANLZ data. Because rekordbox's
  "Export to device" copies files verbatim, every subsequently exported
  USB carries the Serato tags automatically — `usb serato` then only has
  to build `_Serato_/`. Same change class as C2 (tag header only, audio
  stream and paths untouched; rekordbox addresses cues in audio-time, so
  added tag bytes are invisible to it), same rails: drift report,
  dry-run, per-file undo journal. `usb serato --cues` remains as a
  fallback that applies the same tagging directly to a stick's copies
  (for sticks exported before the library was tagged).

Mapping decisions (confirmed): **hot cues only, straight across, max 8**
— A–H → Serato cues 1–8, colors carried over; memory cues are dropped
entirely. Beat grid → Serato grid markers (both formats support
non-constant grids); key → Serato's key field. Loops, phrase data,
MyTags, ratings: out of scope.

#### C9 rails

- `PIONEER/`, `Contents/`, and the pdb files are **never written** — the
  rekordbox side of the stick is read-only to this feature in all modes.
- All writes (Phase 1 files, Phase 2 tags) target only `_Serato_/` or
  embedded tag headers, are dry-run-first, journaled, and undoable.
- `usb check` re-run after `usb serato --cues` must still pass — that's
  an acceptance test, not a hope.

## 4. Safety model (non-negotiable rails)

1. **rekordbox must be quit for any write** (preflight `pgrep`; skill asks the
   user to quit, never kills the app). Read-only operations are fine while
   it's running (work from a snapshot copy).
2. **Fresh timestamped backup** of `master.db` (+ WAL) before every write
   batch, kept under `<rekordbox dir>/skill-backups/`, pruned to
   the last 10.
3. **Dry-run by default** on every mutating command; explicit confirm to apply.
4. **Transactional + verified**: DB changes in one transaction; tag writes per
   file with per-file error capture; verify pass at the end.
5. **Undo journal**: every applied batch logs before/after values (JSON) so any
   batch can be fully reversed — including embedded-tag changes.
6. **USN correctness**: all DB writes go through pyrekordbox's ORM so
   `rb_local_usn` bookkeeping stays consistent. No raw SQL writes.
7. Never rename/move/delete audio files (Trash-move for confirmed duplicate
   removal is the sole, opt-in exception). Never touch `master.backup*.db`,
   ANLZ analysis files, or `share/PIONEER/`.
8. **Baseline playlist protection**: playlists existing at `rbx init` time are
   permanently read-only to the skill (see C3). Playlist operations are
   additive-only — new playlists can be created and managed, baseline ones can
   only be read/exported. The init baseline snapshot is never pruned and can
   restore the entire pre-skill state.

## 5. Architecture

```
~/.claude/skills/rekordvibes/
├── SKILL.md              # triggers + workflow instructions for Claude
├── SPEC.md               # this file
├── scripts/
│   ├── rbx.py            # CLI: init (baseline snapshot + playlist manifest),
│   │                     #      status, recommend, clean (C1), tagsync (C2),
│   │                     #      playlist (C3), doctor (C4), query, export,
│   │                     #      backup, undo
│   └── requirements.txt  # pyrekordbox, mutagen
└── (venv created on first use)
```

- Workflow the SKILL.md enforces: snapshot/read → recommend or dry-run →
  show user the table → confirm → backup → apply → verify → log undo journal.
- One-time setup: `pip install`, then `python -m pyrekordbox download-key`
  (rekordbox ≥ 6.6.5 keys aren't extractable locally; pyrekordbox fetches the
  known key once, needs network).

- Clean 5 polluted tracks in a test playlist → rekordbox reopens showing
  correct Artist/Title/Album, all 5 playable, cues/grids intact, no missing-
  file badges, embedded tags match.
- `undo` restores previous DB values and embedded tags exactly.
- Kill the script mid-batch → every track fully updated or fully untouched.
- Dry-run provably writes nothing (DB + file checksums unchanged).
- A track whose fields are already correct is left 100% untouched.
- Transfer round-trip: export tracks with hot cues → bundle cues match
  DjmdCue rows column-for-column, audio hashes match the originals, ANLZ
  files parse → import on a clean dest → cues at identical ms positions,
  grid visible in rekordbox without re-analysis, ANLZ embedded path matches
  the copied file → `undo` removes rows + installed ANLZ dirs completely.
- Import refuses: hash mismatch (before any write), dest file collision,
  track already in collection (per-track skip, not abort).
- `usb check` on a fresh rekordbox export passes clean; truncate one audio
  file → `--quick` flags exactly that file (a small size deficit with an
  intact header and matching duration is only WARNed as `audio-size-drift`
  — real sticks show stale pdb sizes when tags changed after import, so it
  must not fail the check); delete one ANLZ dir → flagged;
  drive with no `PIONEER/` → clear "not a rekordbox export" failure, exit
  non-zero. Byte-compare the whole drive before/after → identical (read-only
  proof).
- `tagsync --serato` on a test playlist → Serato loads each local file
  showing cues 1–8 at rekordbox's hot cue positions (millisecond match,
  colors carried), grid downbeats agree; rekordbox still opens the same
  files with cues/grid unchanged; `undo` strips the frames byte-exactly.
  A USB exported *after* tagging carries the cues into Serato with no
  further steps.
- `usb serato` on that stick → Serato DJ opens the drive, every crate
  matches its source playlist name and track list, all tracks load and play.
  With `--cues` (fallback for pre-tagging exports): same cue/grid checks
  as above. Re-run `usb check` → still passes. `--wipe` (plus tag undo)
  restores the pre-serato drive state.
