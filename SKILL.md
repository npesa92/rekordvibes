---
name: rekordvibes
description: Manage the local rekordbox 7 DJ collection — fix polluted track metadata (titles containing artist/album/filename junk), sync tags between the DB and audio files, additive playlist management, hygiene checks (missing files, duplicates), queries and exports. Use when the user mentions rekordbox, their DJ library/collection, track metadata cleanup, playlists, or cue/track organization.
---

# rekordvibes

Manage the local rekordbox 7 library via the `rbx` CLI. The library location
(`master.db`, SQLCipher-encrypted; pyrekordbox handles the key) is discovered
automatically — env `RBX_REKORDBOX_DIR` overrides; `rbx status` shows the
resolved paths.

## Running commands

Always use the skill's venv, from the skill's own directory (wherever this
file lives):

```bash
cd "$(dirname <path to this SKILL.md>)" && ./venv/bin/python scripts/rbx.py <command>
```

Commands: `setup`, `init`, `status`, `recommend`, `clean`, `resolve`,
`tagsync`, `doctor`, `playlist`, `query`, `backup`, `undo`, `remove`,
`analyze`, `similar`, `mixable`, `clusters`. Run with `-h` for flags.

First time on a machine: `./bootstrap.sh` (creates the venv, installs core
deps, runs `rbx setup`), then `rbx init`. See INSTALL.md.

## Hard rules (enforced by the CLI, respect them in your workflow too)

1. **Never rename, move, or delete audio files.** Only metadata changes (DB
   fields + embedded tags). File paths must stay identical so rekordbox never
   has to re-find tracks.
2. **Writes require rekordbox to be quit.** The CLI refuses otherwise. Ask the
   user to quit rekordbox; never kill the process yourself. Reads are safe
   anytime (they work from a snapshot copy).
3. **No writes before `rbx init`.** The CLI refuses every mutating command
   until a baseline exists — without it the frozen-playlist protection can't
   work. Never work around this; run `rbx init` (with the user's OK) instead.
4. **Baseline playlists are frozen.** Everything that existed at `rbx init` is
   read-only forever. Only playlists created afterward may be edited. Never
   work around the CLI's refusal.
5. **Dry-run first, always.** Run the command without `--apply`, show the user
   the proposed changes, get explicit confirmation, then re-run with `--apply`.
   Never jump straight to `--apply`.
6. **Never write to master.db with raw sqlite/SQL.** All writes go through
   `rbx` (pyrekordbox ORM) so rekordbox's `rb_local_usn` sync counters stay
   valid. `query --sql` is read-only SELECT and fine.

## Standard workflow

1. `rbx status` — check state (initialized? rekordbox running? backups?).
2. If no baseline: run `rbx init` (one-time per library; snapshots DB, freezes
   playlists).
3. `rbx recommend` — when the user asks "what should I do / clean up?".
4. For fixes: dry-run → show user a digestible summary (counts + a sample,
   not 500 raw lines) → user confirms → `--apply` → report the undo journal
   path and remind them `rbx undo` reverses the batch.
5. After any write batch, suggest reopening rekordbox to verify.

## Metadata cleanup notes

- `clean` parses junk out of Title into Artist/Album/Title/TrackNo. Confidence
  tiers: `high` (safe to batch), `medium` (show the table first), `low`
  (review individually — use `--limit` and `--ids` to cherry-pick).
- `--no-tags` writes DB only; default also writes ID3/MP4/FLAC tags in-place
  (file content changes, path does not).
- Streaming tracks (SoundCloud/Beatport paths) get DB-only fixes automatically.
- Undo journals live in `undo-journals/`; each `--apply` creates one.

## Resolving ambiguous cases via a delegated agent

Medium/low-confidence parses and remixer-rule hits (e.g. "is the name in
'(X Flip)' the producer or the famous source artist?") need world knowledge,
not regex. The lever is **delegation, not an API call**:

1. `rbx resolve export [--playlist NAME]` — writes `resolve/cases-<ts>.json`
   and prints the expected `resolve/verdicts-<ts>.json` path.
2. Spawn a subagent with the Agent tool (`subagent_type: general-purpose`,
   `model: "sonnet"`) using the prompt template in `RESOLVE_PROMPT.md`, with
   both file paths filled in. The agent only reads the cases file and writes
   the verdicts file — never the DB.
3. `rbx clean --verdicts <verdicts file>` — dry-run; the file is
   schema-validated and rejected wholesale on any malformed entry. Review
   with the user (the model's `reason` is shown per fix), confirm, then
   `--apply`. Same backup + undo-journal machinery as normal clean;
   `--min-conf` filters on the model's own confidence.

## Removing tracks from the collection

`rbx remove --folder PREFIX [--without-hot-cues]` removes DB rows only —
audio files always stay on disk. Tracks in frozen baseline playlists are
auto-excluded. Dry-run → confirm → `--apply`; the undo journal stores full
row copies (content + cues + history + playlist links) so `rbx undo`
re-inserts everything.

## Track analysis (C7)

`rbx analyze [--playlist NAME|--folder PREFIX|--ids ...]` runs all four
lanes per track — grid (rekordbox ANLZ: beat grid, phrases, energy curve),
rhythm (drum patterns snapped to the grid), timbre (sound texture), audioid
(chromaprint) — cached in `analysis.db`, resumable, incremental. It's
read-only; safe while rekordbox runs. `--status` shows coverage. ~10-15 s per
track on first analysis, so run large scopes in the background. The rhythm
and timbre lanes need the analysis extras
(`pip install -r scripts/requirements-analysis.txt`); grid works without them.

Then: `rbx similar TRACK --by rhythm|timbre|both`, `rbx mixable TRACK`
(key + BPM + intro length + energy handoff), `rbx clusters [--playlists]`
(rhythm families; `--playlists` creates `[rbx] rhythm ...` playlists — that
one writes, so dry-run first without the flag and confirm). TRACK is a
content ID or fuzzy "artist - title".

## Troubleshooting

- First-time setup or broken venv: run `./bootstrap.sh` (Windows:
  `bootstrap.ps1` — untested branch). It probes for Python 3.11+, builds the
  venv, and ends with `rbx setup`, which diagnoses the rest.
- Library not found: `rbx setup` prints the probed locations; set
  `RBX_REKORDBOX_DIR=/path/to/rekordbox` if the library lives elsewhere.
- pyrekordbox key errors: open rekordbox once, then retry — or run
  `./venv/bin/python -m pyrekordbox download-key` (one-time, needs network).
- If master.db looks corrupted, STOP and restore the newest backup from the
  `skill-backups` dir under the rekordbox library dir (`rbx status` shows the
  path; baseline-* is the full pre-skill state, pre-write-* are per-batch).
