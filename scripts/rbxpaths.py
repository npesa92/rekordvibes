"""Environment resolution for the portable rekordvibes skill.

Single source of truth for every machine-dependent value. Resolution order
per value — first hit wins:

  1. explicit env var        (RBX_REKORDBOX_DIR, RBX_FPCALC)
  2. persisted config.json   (written by `rbx setup`; per-machine, gitignored)
  3. platform auto-discovery (probe list below)

macOS is the verified platform. The Windows branch is written to spec but
UNTESTED until `rbx setup` has been run on real Windows.
"""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parent.parent
CONFIG_PATH = SKILL_DIR / "config.json"
IS_WINDOWS = sys.platform == "win32"

_rb_dir_cache: Path | None = None


def load_config() -> dict:
    if CONFIG_PATH.exists():
        try:
            return json.loads(CONFIG_PATH.read_text())
        except Exception:
            return {}
    return {}


def save_config(cfg: dict):
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2))


def probe_candidates() -> list[Path]:
    """Locations a rekordbox library dir may live, most likely first."""
    if IS_WINDOWS:  # UNTESTED branch
        cands = []
        appdata = os.environ.get("APPDATA")
        if appdata:
            cands.append(Path(appdata) / "Pioneer" / "rekordbox")
        cands.append(Path.home() / "AppData" / "Roaming" / "Pioneer" / "rekordbox")
        return cands
    return [Path.home() / "Library" / "Pioneer" / "rekordbox"]


def _is_library(p: Path) -> bool:
    return (p / "master.db").exists()


def try_rb_dir() -> Path | None:
    """Resolve the rekordbox library dir, or None if not found. Never exits."""
    global _rb_dir_cache
    if _rb_dir_cache is not None:
        return _rb_dir_cache
    env = os.environ.get("RBX_REKORDBOX_DIR")
    if env:
        # an explicit override is authoritative — no silent fallback past it
        p = Path(env).expanduser()
        _rb_dir_cache = p if _is_library(p) else None
        return _rb_dir_cache
    cfg = load_config()
    if cfg.get("rekordbox_dir"):
        p = Path(cfg["rekordbox_dir"]).expanduser()
        if _is_library(p):
            _rb_dir_cache = p
            return p
        print(f"note: config.json points at {p} but no master.db there — re-probing",
              file=sys.stderr)
    for c in probe_candidates():
        if _is_library(c):
            _rb_dir_cache = c
            return c
    # shallow fallback for relocated setups: one dir level under home
    try:
        for c in sorted(Path.home().glob("*/Pioneer/rekordbox")):
            if _is_library(c):
                _rb_dir_cache = c
                return c
    except OSError:
        pass
    return None


def rb_dir() -> Path:
    """Resolve the rekordbox library dir or exit with a friendly diagnostic."""
    p = try_rb_dir()
    if p is not None:
        return p
    env = os.environ.get("RBX_REKORDBOX_DIR")
    if env:
        sys.exit(
            f"REFUSED: RBX_REKORDBOX_DIR={env} contains no master.db.\n"
            "Point it at the directory that holds your rekordbox master.db."
        )
    probed = "\n  ".join(str(c) for c in probe_candidates())
    sys.exit(
        "REFUSED: no rekordbox library (master.db) found.\n"
        f"Probed:\n  {probed}\n"
        "If your library lives elsewhere: RBX_REKORDBOX_DIR=/path/to/rekordbox\n"
        "For a full environment diagnostic: rbx setup"
    )


def master() -> Path:
    return rb_dir() / "master.db"


def backup_dir() -> Path:
    return rb_dir() / "skill-backups"


def rb_share() -> Path:
    return rb_dir() / "share"


def rekordbox_running() -> bool:
    if IS_WINDOWS:  # UNTESTED branch
        r = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq rekordbox.exe"],
            capture_output=True, text=True,
        )
        return "rekordbox.exe" in (r.stdout or "")
    r = subprocess.run(["pgrep", "-x", "rekordbox"], capture_output=True)
    return r.returncode == 0


def fpcalc_path() -> str | None:
    env = os.environ.get("RBX_FPCALC")
    if env and Path(env).exists():
        return env
    cfg = load_config()
    if cfg.get("fpcalc") and Path(cfg["fpcalc"]).exists():
        return cfg["fpcalc"]
    found = shutil.which("fpcalc.exe" if IS_WINDOWS else "fpcalc")
    if found:
        return found
    for cand in ("/opt/homebrew/bin/fpcalc", "/usr/local/bin/fpcalc"):
        if Path(cand).exists():
            return cand
    return None


def is_local(path: str | None) -> bool:
    """True if a DB FolderPath points at a file on disk (vs a streaming link)."""
    if not path:
        return False
    if path.startswith("/"):
        return True
    # Windows drive-letter paths, e.g. C:/Users/... (UNTESTED branch)
    return len(path) > 2 and path[1] == ":" and path[2] in "\\/"
