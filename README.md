# rekordvibes 🎛️

Your rekordbox library, but the vibes are immaculate.

You know the tracks. The ones titled
`01. Artist_-_Track_Name_(Official Video)_[FREE DL]_320kbps.mp3` with an
empty artist field and an album called nothing. They play fine, but every
time you scroll past one, a little part of your set dies. **rekordvibes**
reads your rekordbox 7 library, finds the mess, and fixes the metadata —
carefully, reversibly, and without ever touching your audio files.

## What it does

- **Cleans the junk** — parses artist / title / album / track number out of
  polluted title fields, strips the `(Official Video)` `[FREE DL]` `320kbps`
  residue, and writes it back to both the rekordbox DB *and* the embedded
  tags (ID3/MP4/FLAC), so it stays fixed everywhere.
- **Knows when it doesn't know** — every proposed fix gets a confidence
  tier. High-confidence fixes batch clean; ambiguous ones ("is the name in
  that `(X Flip)` the producer, or the artist they flipped?") get exported
  for an AI agent with actual music knowledge to adjudicate. You approve
  everything before it lands.
- **Checks the vibe compatibility** — `rbx mixable` ranks what mixes into a
  track by Camelot key, BPM (half/double-time aware), intro length, and
  energy handoff. `rbx similar` finds tracks that *feel* alike by drum
  pattern and sound texture. `rbx clusters` sorts your crates into rhythm
  families.
- **Plays doctor** — missing files, duplicates, tag drift, untracked audio.
- **Builds playlists** — additive only. It can create and manage its own;
  everything that existed before it arrived is frozen, forever.

## The sacred rules

The vibes are chill; the safety model is not.

1. **Your audio files are never renamed, moved, or deleted.** Metadata only.
2. **No writes while rekordbox is open.** It refuses, politely.
3. **No writes before `rbx init`.** Init snapshots your library and freezes
   every existing playlist — read-only forever after.
4. **Everything is dry-run first.** You see the table, you say yes, then it
   writes — after taking a fresh backup, and it leaves an undo journal so
   `rbx undo` can reverse any batch completely.

## Get the vibes

```bash
git clone https://github.com/npesa92/rekordvibes.git
cd rekordvibes
./bootstrap.sh    # builds the venv, finds your library, diagnoses everything
./venv/bin/python scripts/rbx.py init    # one-time: snapshot + freeze
./venv/bin/python scripts/rbx.py recommend    # "what should I clean up?"
```

Works out of the box on macOS; finds your library automatically (or point
`RBX_REKORDBOX_DIR` at it). Windows support is written but untested — run
`bootstrap.ps1` and tell us how it went. Details in [INSTALL.md](INSTALL.md);
the full command reference lives in [SKILL.md](SKILL.md), and the design doc
is [SPEC.md](SPEC.md).

## Or just let Claude Code do it 🤖

This repo *is* a [Claude Code](https://claude.com/claude-code) skill — the
whole thing is designed to be driven by Claude, not memorized by you. The
lazy (correct) install is to open Claude Code and say:

> Install the skill at https://github.com/npesa92/rekordvibes.git
> into my skills directory

Claude will clone it into `~/.claude/skills/rekordvibes` and run the
bootstrap. From then on, just talk to it about your library:

> *"what's the state of my rekordbox library?"* → `rbx status` / `recommend`
> *"clean up my metadata"* → dry-run, shows you the table, waits for your yes
> *"what mixes out of this track?"* → `rbx mixable`

Claude Code runs with real system permissions — it can create the venv, read
the library, and write fixes — so it will ask you to approve commands along
the way; that's the permission model working as intended. The guardrails
travel with the skill: [SKILL.md](SKILL.md) instructs Claude to always
dry-run first, show you a digestible summary, and never apply anything
without your explicit confirmation — and the CLI enforces the same rules
underneath even if it's asked nicely.

Now go fix your library. Your future self, mid-set, in the dark, scrolling
for the next track — they're counting on you. ✨

## Credits & contributing

None of this would work without
[pyrekordbox](https://github.com/dylanljones/pyrekordbox), which does the
genuinely hard part — reading and writing the encrypted rekordbox 7
database. rekordvibes is the vibes layer on top; pyrekordbox is the
foundation. Go star it.

Contributions are welcome — especially Windows testing reports, new junk
patterns for the title cleaner, and bug reports with a `rbx status` dump
attached. Open an issue or a PR. Just remember the sacred rules above:
anything that touches the library must be dry-run first, reversible, and
must never lay a finger on the audio files.
