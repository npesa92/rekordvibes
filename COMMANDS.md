# rbx command reference

Every command, what it does, and a working example. All invocations run from
the repo directory as `./venv/bin/python scripts/rbx.py <command>` — written
below as `rbx <command>` for brevity.

**The R/W column is the safety model at a glance.** `read` commands never
touch anything and are safe while rekordbox is running. `write` commands
refuse to run unless rekordbox is quit and `rbx init` has been run, always
take a fresh backup first, are dry-run by default (nothing happens without
`--apply`), and leave an undo journal that `rbx undo` reverses. Audio files
are never renamed, moved, or deleted by anything, ever.

## Setup & state

| Command | R/W | What it does | Example |
|---|---|---|---|
| `rbx setup` | read | Environment diagnostic: finds your library (env var → config.json → platform probe), checks deps and the DB key, writes `config.json`. | `rbx setup` |
| `rbx init` | write | One-time baseline: snapshots the DB and freezes every playlist that exists right now (read-only forever). All other writes are refused until this has run. | `rbx init` |
| `rbx status` | read | Library + skill state overview: DB path/size, rekordbox running?, baseline, backups, undo journals, track count. | `rbx status` |
| `rbx recommend` | read | Scans the collection and suggests what to fix, in priority order. The "what should I do?" command. | `rbx recommend` |
| `rbx backup` | read | Manual timestamped backup of master.db (+wal/shm) into the library's `skill-backups/`. | `rbx backup` |
| `rbx undo` | write | Reverses the latest write batch (or a named journal) completely — metadata, removals, imports, playlists. | `rbx undo` · `rbx undo undo-journals/clean-20260719-1200.json` |

## Metadata cleanup

| Command | R/W | What it does | Example |
|---|---|---|---|
| `rbx clean` | write | Parses junk out of polluted Titles (`01. Artist_-_Track_(Official Video)_320kbps`) into proper Artist / Title / Album / TrackNo, then writes the fix to the DB **and** the embedded file tags. Every proposal gets a confidence tier; dry-run shows the full table first. | `rbx clean` (dry-run, high-confidence) · `rbx clean --min-conf medium --playlist "crate 1"` · `rbx clean --apply` |
| | | `--min-conf high\|medium\|low` include lower tiers · `--ids 123,456` cherry-pick · `--limit N` · `--no-tags` DB only, skip file tags · `--apply` write | |
| `rbx resolve` | read | Exports the ambiguous cases (is the name in "(X Flip)" the producer or the source artist?) to JSON for an AI agent with music knowledge to adjudicate. Prints the expected verdicts path. | `rbx resolve` · `rbx resolve --playlist "new stuff"` |
| `rbx clean --verdicts` | write | Applies a schema-validated verdicts file from the resolve step — same dry-run/backup/undo machinery, and the model's reasoning is shown per fix. | `rbx clean --verdicts resolve/verdicts-20260719.json` then `--apply` |
| `rbx tagsync` | write | Reports drift between DB metadata and embedded file tags; `--apply` writes DB values into the tags (DB is source of truth). | `rbx tagsync` · `rbx tagsync --apply` |

## Hygiene

| Command | R/W | What it does | Example |
|---|---|---|---|
| `rbx doctor` | read | Library health checks — run one or `--all`. | `rbx doctor --all` |
| | | `--missing` DB rows whose file is gone · `--dupes` same recording under multiple entries · `--untracked ROOT...` audio on disk that rekordbox doesn't know about | `rbx doctor --untracked ~/Music/music` |
| `rbx remove` | write | Removes tracks from the collection — DB rows only, files always stay on disk. Tracks in frozen baseline playlists are auto-excluded; undo re-inserts everything (cues, history, playlist links). | `rbx remove --folder ~/Music/old-crate` · `rbx remove --ids 123,456 --apply` |
| | | `--without-hot-cues` only tracks you never cued · `--limit N` | `rbx remove --folder ~/Music/og --without-hot-cues` |

## Playlists (additive-only)

Playlists that existed at `rbx init` are frozen — list/show/export always
work, but only playlists created *after* the baseline can be modified.

| Command | R/W | What it does | Example |
|---|---|---|---|
| `rbx playlist list` | read | All playlists with `[frozen]`/`[editable]` markers. | `rbx playlist list` |
| `rbx playlist show` | read | Tracks in a playlist, in order. | `rbx playlist show "warmup"` |
| `rbx playlist export` | read | Playlist → `.m3u8` file. | `rbx playlist export "warmup" -o warmup.m3u8` |
| `rbx playlist create` | write | New (editable) playlist. | `rbx playlist create "[rbx] to review"` |
| `rbx playlist add` | write | Add tracks by content ID. | `rbx playlist add "[rbx] to review" --ids 123,456` |
| `rbx playlist remove` | write | Remove tracks by content ID. | `rbx playlist remove "[rbx] to review" --ids 123` |
| `rbx playlist rename` | write | Rename an editable playlist. | `rbx playlist rename "[rbx] to review" "reviewed"` |
| `rbx playlist delete` | write | Delete an editable playlist. | `rbx playlist delete "reviewed"` |

## Analysis & digging

`TRACK` below is a content ID or a fuzzy `"artist - title"` string.

| Command | R/W | What it does | Example |
|---|---|---|---|
| `rbx analyze` | read | Runs all four analysis lanes per track — grid (rekordbox's own beat grid / phrases / energy curve), rhythm (drum-pattern fingerprint), timbre (sound texture), audioid (chromaprint) — cached in a sidecar `analysis.db`, incremental and resumable. ~10–15 s/track first time. | `rbx analyze --playlist "crate 1"` · `rbx analyze --status` (coverage report) |
| | | Scope: `--playlist` / `--folder PREFIX` / `--ids` / `--limit` | `rbx analyze --folder ~/Music/music --limit 100` |
| `rbx similar` | read | Ranks the collection by similarity to a track — drum pattern, sound texture, or both (cosine distance). | `rbx similar "sfam - out my face" --by rhythm` |
| `rbx mixable` | read | What mixes *into* a track: Camelot key compatibility (handles `8A` and `Am` notations), BPM window incl. half/double-time, intro length, and outro→intro energy handoff. | `rbx mixable 14055890` · `rbx mixable "here & now" --bpm-range 6 --any-key` |
| `rbx clusters` | read* | Groups a scope into rhythm families within BPM bands. `--playlists` materializes them as `[rbx] rhythm ...` playlists (*that flag writes — dry-run without it first). | `rbx clusters --playlist "all dubstep"` · `rbx clusters --threshold 0.15 --playlists` |
| `rbx query` | read | Free-form collection queries and exports. Filters or raw read-only SQL. | `rbx query --bpm-min 138 --bpm-max 145 --key 8A` · `rbx query --sql "SELECT COUNT(*) FROM djmdContent"` · `rbx query --csv collection.csv` |

## Transfer (library → library)

Moves tracks to another rekordbox library with hot cues, memory cues, loops,
beat grid, and metadata intact. The bundle zip is platform-neutral and
travels however you like (AirDrop, scp, USB). The receiving machine runs
this same CLI (bootstrapped + `rbx init`). Not transferred: MyTags, play
counts, histories, mixer params.

| Command | R/W | What it does | Example |
|---|---|---|---|
| `rbx transfer export` | read | Packages a playlist (or explicit IDs) into a single zip: audio copies, manifest (metadata + every cue + SHA-256 hashes), and ANLZ beat-grid files. Skips streaming/missing tracks with a warning. | `rbx transfer export --playlist "for alex" -o for-alex.zip` · `rbx transfer export --ids 123,456 -o two-tracks.zip` |
| `rbx transfer inspect` | read | Lists a bundle's contents: tracks, cue counts, grid presence, sizes. | `rbx transfer inspect for-alex.zip` |
| `rbx transfer import` | write | Imports a bundle into *this* library: verifies every hash before touching anything, copies audio into `--dest` (never overwrites; tracks already in the collection are skipped), creates the DB rows, installs the beat-grid files with their embedded path rewritten (so no re-analysis needed), and recreates the bundle's playlist. Undo removes it all; copied audio stays on disk. | `rbx transfer import for-alex.zip` (dry-run) · `rbx transfer import for-alex.zip --dest ~/Music/from-nick --apply` |
| | | `--dest DIR` where audio lands (default `~/Music/rbx-imports/<bundle>/`) · `--no-anlz` let rekordbox re-analyze instead · `--no-playlist` skip playlist recreation | |

## The standard write workflow

Every mutating command follows the same shape:

```bash
rbx clean --playlist "crate 1"          # 1. dry run — see the table
rbx clean --playlist "crate 1" --apply  # 2. backup, write, undo journal
rbx undo                                # 3. (if needed) full reverse
```
