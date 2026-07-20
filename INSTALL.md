# Installing rekordvibes on a new machine

Requirements: rekordbox 6.6.5+ (developed against 7.x), Python 3.11+.
macOS is the verified platform; the Windows path is implemented but
**untested** — `rbx setup` will tell you which branch you're on.

## Steps

```bash
# 1. Get the skill (clone into your Claude skills dir, or wherever you keep it)
git clone <repo-url> ~/.claude/skills/rekordvibes
cd ~/.claude/skills/rekordvibes

# 2. Bootstrap: builds venv, installs core deps, runs the diagnostic
./bootstrap.sh          # Windows: .\bootstrap.ps1

# 3. Fix anything setup flags (see below), then establish the baseline
./venv/bin/python scripts/rbx.py init
```

`rbx setup` is idempotent — re-run it any time. It discovers the rekordbox
library, checks Python/deps/the SQLCipher key, reports optional extras, and
writes `config.json` (per-machine, gitignored).

## What setup may flag

- **Library not found** — set `RBX_REKORDBOX_DIR=/path/to/rekordbox` (the
  directory containing `master.db`) and re-run.
- **db open FAILED** — the SQLCipher key isn't cached yet. Either open
  rekordbox once and re-run, or:
  `./venv/bin/python -m pyrekordbox download-key` (one-time, needs network).
- **Analysis extras not installed** — only needed for `analyze`'s
  rhythm/timbre lanes (~400 MB):
  `./venv/bin/pip install -r scripts/requirements-analysis.txt`
- **fpcalc not found** — only needed for the audioid lane:
  `brew install chromaprint` (or set `RBX_FPCALC`).

## Why `rbx init` matters

`init` snapshots the library and freezes every playlist that exists at that
moment — the skill will never modify them, only playlists created afterward.
The baseline lives under the *library* (`<rekordbox dir>/skill-backups/`), so
each machine's library gets its own. **All write commands refuse to run until
init has been done** — that's deliberate; don't work around it.

## Hard safety rules (built in)

- Audio files are never renamed, moved, or deleted.
- Writes require rekordbox to be quit; reads are safe anytime.
- Every write batch: fresh DB backup first, undo journal after (`rbx undo`).
- Baseline playlists are read-only forever.
