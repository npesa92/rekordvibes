"""Export analysis.db + track metadata to JSON for the analysis UI.

Usage: ./venv/bin/python scripts/export_ui.py [OUT_JSON]
Default output: <skill dir>/exports/data.json (override via argv or RBX_UI_OUT).
Read-only everywhere; safe while rekordbox runs.
"""

import json
import os
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import rbx  # noqa: E402
import analysis as an  # noqa: E402

OUT = Path(
    sys.argv[1] if len(sys.argv) > 1
    else os.environ.get("RBX_UI_OUT", rbx.SKILL_DIR / "exports" / "data.json")
)

db = rbx.open_db()
con = an.open_cache()

# playlist memberships (all playlists, flagged rbx-created vs baseline)
pl_of_track: dict[str, list] = {}
playlists = []
for p in db.get_playlist():
    if p.Attribute == 1:  # folder
        continue
    songs = [str(s.ContentID) for s in db.get_playlist_songs(PlaylistID=p.ID)]
    playlists.append({"id": str(p.ID), "name": p.Name, "n": len(songs),
                      "rbx": bool(p.Name and p.Name.startswith("[rbx]"))})
    for cid in songs:
        pl_of_track.setdefault(cid, []).append(p.Name)

tracks = []
for row in con.execute(
    "SELECT content_id, grid, rhythm, timbre, errors FROM analysis"
):
    cid, g_json, r_json, t_json, e_json = row
    c = rbx.content_by_id(db, cid)
    if c is None:
        continue
    key = c.Key.ScaleName if c.Key else None
    tracks.append({
        "id": str(c.ID),
        "artist": c.Artist.Name if c.Artist else None,
        "title": c.Title,
        "album": c.Album.Name if c.Album else None,
        "bpm": (c.BPM or 0) / 100 or None,
        "key": key,
        "camelot": rbx.camelot(key),
        "seconds": c.Length,
        "folder": str(Path(c.FolderPath).parent) if rbx.is_local(c.FolderPath) else "streaming",
        "playlists": pl_of_track.get(str(c.ID), []),
        "grid": json.loads(g_json) if g_json else None,
        "rhythm": json.loads(r_json) if r_json else None,
        "timbre": json.loads(t_json) if t_json else None,
        "errors": json.loads(e_json) if e_json else None,
    })

OUT.parent.mkdir(parents=True, exist_ok=True)
OUT.write_text(json.dumps({
    "exported": datetime.now().isoformat(timespec="seconds"),
    "tracks": tracks,
    "playlists": playlists,
}))
print(f"wrote {OUT} — {len(tracks)} tracks, {len(playlists)} playlists, "
      f"{OUT.stat().st_size // 1024} KB")
