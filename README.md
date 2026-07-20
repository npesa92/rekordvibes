# rekordvibes

A command-line toolkit for managing a rekordbox 7 library: metadata
cleanup, tag synchronization, library hygiene, playlist management, track
analysis, and library-to-library transfer. All changes are reversible, and
audio files are never modified, renamed, moved, or deleted.

It reads and writes the encrypted rekordbox database directly (via
[pyrekordbox](https://github.com/dylanljones/pyrekordbox)), so fixes apply
to the library itself — no XML export/import round-trips.

## Features

- **Metadata cleanup** — parses artist / title / album / track number out
  of polluted title fields (e.g.
  `01. Artist_-_Track_Name_(Official Video)_[FREE DL]_320kbps.mp3`), strips
  the residue, and writes the result to both the rekordbox DB *and* the
  embedded file tags (ID3/MP4/FLAC), so the fix persists everywhere.
- **Confidence-tiered fixes** — every proposed change gets a confidence
  tier. High-confidence fixes can be batch-applied; ambiguous cases (is the
  name in `(X Flip)` the producer, or the artist being flipped?) are
  exported for adjudication by an AI agent with music knowledge. Nothing is
  written without explicit approval.
- **Mix analysis** — `rbx mixable` ranks candidate transitions by Camelot
  key compatibility, BPM (half/double-time aware), intro length, and energy
  handoff. `rbx similar` finds tracks with matching drum patterns or sound
  texture. `rbx clusters` groups a collection into rhythm families.
- **Library hygiene** — `rbx doctor` reports missing files, duplicates,
  tag drift, and untracked audio.
- **Library-to-library transfer** — `rbx transfer` packages a playlist
  into a single zip (audio, metadata, hot cues, memory cues, beat grid)
  that imports into another rekordbox library via the same CLI. Cues and
  grid arrive intact; no re-analysis or re-cueing required. The bundle is a
  plain file — transfer it by AirDrop, scp, or USB.
- **USB checker** — `rbx usb check` verifies an exported stick end to end:
  database parses, every track's audio is present and un-truncated, beat
  grids/waveforms intact, playlists resolve, filesystem is player-friendly.
  Read-only, needs no library — vet anyone's stick before a gig.
- **Serato interop** — `rbx tagsync --serato` writes your hot cues and beat
  grids into the files' tags in Serato's own format, so every USB you export
  from rekordbox also opens in Serato DJ with cues intact; `rbx usb serato`
  puts a Serato database + crates (mirroring your playlists) on the stick.
- **Playlist management** — additive only. The tool can create and manage
  its own playlists; every playlist that existed before initialization is
  permanently read-only.

## Safety model

1. **Audio files are never renamed, moved, or deleted.** Metadata only.
2. **No writes while rekordbox is open.** The CLI refuses to run mutating
   commands while the process is detected.
3. **No writes before `rbx init`.** Initialization snapshots the library
   and freezes every existing playlist — read-only from then on.
4. **Every write is dry-run first.** The proposed changes are shown as a
   table; nothing happens without `--apply`. Each applied batch takes a
   fresh backup first and leaves an undo journal, so `rbx undo` can
   reverse it completely.

## Installation

```bash
git clone https://github.com/npesa92/rekordvibes.git
cd rekordvibes
./bootstrap.sh    # builds the venv, locates the library, runs diagnostics
./venv/bin/python scripts/rbx.py init    # one-time: snapshot + freeze
./venv/bin/python scripts/rbx.py recommend    # prioritized cleanup suggestions
```

Works out of the box on macOS; the library is located automatically (or set
`RBX_REKORDBOX_DIR`). Windows support is implemented but untested — run
`bootstrap.ps1` and report results. Details in [INSTALL.md](INSTALL.md);
the full command reference with examples is [COMMANDS.md](COMMANDS.md), the
agent operating manual is [SKILL.md](SKILL.md), and the design doc is
[SPEC.md](SPEC.md).

## Using it with Claude Code

This repo is also a [Claude Code](https://claude.com/claude-code) skill —
it is designed to be driven by an agent rather than memorized. To install
it that way, open Claude Code and say:

> Install the skill at https://github.com/npesa92/rekordvibes.git
> into my skills directory

Claude will clone it into `~/.claude/skills/rekordvibes` and run the
bootstrap. From then on, requests map to commands directly:

> *"what's the state of my rekordbox library?"* → `rbx status` / `recommend`
> *"clean up my metadata"* → dry-run, shows the table, waits for confirmation
> *"what mixes out of this track?"* → `rbx mixable`

Claude Code runs with real system permissions — it can create the venv,
read the library, and write fixes — so it will ask for command approval
along the way; that is the permission model working as intended. The
guardrails travel with the skill: [SKILL.md](SKILL.md) instructs the agent
to always dry-run first, present a digestible summary, and never apply
anything without explicit confirmation — and the CLI enforces the same
rules independently.

## Credits & contributing

This project depends on
[pyrekordbox](https://github.com/dylanljones/pyrekordbox), which handles
the hard part — reading and writing the encrypted rekordbox 7 database.

Contributions are welcome — especially Windows testing reports, new junk
patterns for the title cleaner, and bug reports with an `rbx status` dump
attached. Open an issue or a PR. All contributions must follow the safety
model above: anything that touches the library must be dry-run first,
reversible, and must never modify audio files.
