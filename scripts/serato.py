"""Serato format writers: library database, crates, and performance-data tags.

Serato DJ keeps almost nothing in its database — `_Serato_/database V2` is a
track list and `_Serato_/Subcrates/*.crate` are playlists, both simple
tag-length-value files. Cues and beat grids live INSIDE each audio file's
tags (GEOB frames for ID3, `----:com.serato.dj:*` freeform atoms for MP4,
`SERATO_*` Vorbis comments for FLAC/OGG). Formats documented by the
serato-tags / triseratops projects and Mixxx's Serato importer.

This module is format-only + per-file tag IO; DB/ANLZ orchestration stays in
rbx.py. Nothing here touches the audio stream — tag headers only.
"""

from __future__ import annotations

import base64
import struct
from pathlib import Path

# ------------------------------------------------------------ colors

# rekordbox hot-cue palette: ColorTableIndex / ANLZ color_id -> RGB.
# Source: beat-link CueList.findRekordboxColor (Deep Symmetry).
REKORDBOX_CUE_COLORS = {
    0x01: 0x305AFF, 0x02: 0x5073FF, 0x03: 0x508CFF, 0x04: 0x50A0FF,
    0x05: 0x50B4FF, 0x06: 0x50B0F2, 0x07: 0x50AEE8, 0x08: 0x45ACDB,
    0x09: 0x00E0FF, 0x0A: 0x19DAF0, 0x0B: 0x32D2E6, 0x0C: 0x21B4B9,
    0x0D: 0x20AAA0, 0x0E: 0x1FA392, 0x0F: 0x19A08C, 0x10: 0x14A584,
    0x11: 0x14AA7D, 0x12: 0x10B176, 0x13: 0x30D26E, 0x14: 0x37DE5A,
    0x15: 0x3CEB50, 0x16: 0x28E214, 0x17: 0x7DC13D, 0x18: 0x8CC832,
    0x19: 0x9BD723, 0x1A: 0xA5E116, 0x1B: 0xA5DC0A, 0x1C: 0xAAD208,
    0x1D: 0xB4C805, 0x1E: 0xB4BE04, 0x1F: 0xBAB404, 0x20: 0xC3AF04,
    0x21: 0xE1AA00, 0x22: 0xFFA000, 0x23: 0xFF9600, 0x24: 0xFF8C00,
    0x25: 0xFF7500, 0x26: 0xE0641B, 0x27: 0xE0461E, 0x28: 0xE0301E,
    0x29: 0xE02823, 0x2A: 0xE62828, 0x2B: 0xFF376F, 0x2C: 0xFF2D6F,
    0x2D: 0xFF127B, 0x2E: 0xF51E8C, 0x2F: 0xEB2DA0, 0x30: 0xE637B4,
    0x31: 0xDE44CF, 0x32: 0xDE448D, 0x33: 0xE630B4, 0x34: 0xE619DC,
    0x35: 0xE600FF, 0x36: 0xDC00FF, 0x37: 0xCC00FF, 0x38: 0xB432FF,
    0x39: 0xB93CFF, 0x3A: 0xC542FF, 0x3B: 0xAA5AFF, 0x3C: 0xAA72FF,
    0x3D: 0x8272FF, 0x3E: 0x6473FF,
}
DEFAULT_CUE_COLOR = 0x28E214  # rekordbox default hot-cue green


def cue_color(color_table_index) -> int:
    if color_table_index:
        return REKORDBOX_CUE_COLORS.get(int(color_table_index), DEFAULT_CUE_COLOR)
    return DEFAULT_CUE_COLOR


# ------------------------------------------------------------ database V2 / crates

DB_VERSION = "2.0/Serato Scratch LIVE Database"
CRATE_VERSION = "1.0/Serato ScratchLive Crate"


def _chunk(tag: str, payload: bytes) -> bytes:
    return tag.encode("ascii") + struct.pack(">I", len(payload)) + payload


def _text(tag: str, value: str) -> bytes:
    return _chunk(tag, value.encode("utf-16-be"))


def build_database(tracks: list[dict]) -> bytes:
    """`database V2` bytes. Each track dict: path (drive-relative, no leading
    slash) plus optional title/artist/album/genre/key/bpm/length_ms."""
    out = [_text("vrsn", DB_VERSION)]
    for t in tracks:
        fields = [_text("ttyp", Path(t["path"]).suffix.lstrip(".").lower()),
                  _text("pfil", t["path"])]
        for tag, k in (("tsng", "title"), ("tart", "artist"), ("talb", "album"),
                       ("tgen", "genre"), ("tkey", "key")):
            if t.get(k):
                fields.append(_text(tag, str(t[k])))
        if t.get("bpm"):
            fields.append(_text("tbpm", f"{t['bpm']:.2f}".rstrip("0").rstrip(".")))
        if t.get("length_ms"):
            fields.append(_text("tlen", str(int(t["length_ms"]))))
        out.append(_chunk("otrk", b"".join(fields)))
    return b"".join(out)


def build_crate(track_paths: list[str]) -> bytes:
    out = [_text("vrsn", CRATE_VERSION)]
    for p in track_paths:
        out.append(_chunk("otrk", _text("ptrk", p)))
    return b"".join(out)


def crate_filename(folder_names: list[str], playlist_name: str) -> str:
    """Serato nests subcrates with %% in the filename."""
    def clean(s):
        return "".join("-" if ch in '/\\:%*?"<>|' else ch for ch in s).strip() or "untitled"
    parts = [clean(n) for n in folder_names] + [clean(playlist_name)]
    return "%%".join(parts) + ".crate"


# ------------------------------------------------------------ Markers2 (cues)


def _b64_lines(data: bytes, width: int) -> bytes:
    """Serato-style base64: unpadded, linefeed every `width` chars."""
    b64 = base64.b64encode(data).rstrip(b"=")
    return b"\n".join(b64[i:i + width] for i in range(0, len(b64), width))


def _b64_decode_forgiving(data: bytes) -> bytes:
    d = data.replace(b"\n", b"").rstrip(b"\x00")
    d += b"A==" if len(d) % 4 == 1 else b"=" * (-len(d) % 4)
    return base64.b64decode(d)


def markers2_payload(cues: list[dict], track_color: int | None = None,
                     bpm_lock: bool | None = False) -> bytes:
    """Serato Markers2 tag payload (the ID3 GEOB body).

    cues: [{index: 0-7, ms: int, color: 0xRRGGBB, name: str}]
    """
    entries = []
    if track_color is not None:
        entries.append(_m2_entry("COLOR", b"\x00" + track_color.to_bytes(3, "big")))
    for c in sorted(cues, key=lambda c: c["index"]):
        body = (b"\x00" + bytes([c["index"]]) + struct.pack(">I", int(c["ms"]))
                + b"\x00" + int(c.get("color", DEFAULT_CUE_COLOR)).to_bytes(3, "big")
                + b"\x00\x00" + str(c.get("name") or "").encode("utf-8") + b"\x00")
        entries.append(_m2_entry("CUE", body))
    if bpm_lock is not None:
        entries.append(_m2_entry("BPMLOCK", bytes([1 if bpm_lock else 0])))
    inner = b"\x01\x01" + b"".join(entries) + b"\x00"
    payload = b"\x01\x01" + _b64_lines(inner, 72)
    if len(payload) < 470:  # Serato null-pads the tag to a minimum size
        payload += b"\x00" * (470 - len(payload))
    return payload


def _m2_entry(name: str, body: bytes) -> bytes:
    return name.encode("ascii") + b"\x00" + struct.pack(">I", len(body)) + body


def parse_markers2(payload: bytes) -> dict:
    """Round-trip check helper. Returns {'cues': [...], 'color': int|None}."""
    assert payload[:2] == b"\x01\x01", "bad Markers2 header"
    inner = _b64_decode_forgiving(payload[2:])
    assert inner[:2] == b"\x01\x01", "bad Markers2 inner header"
    pos, out = 2, {"cues": [], "color": None, "bpm_lock": None}
    while pos < len(inner) and inner[pos] != 0:
        end = inner.index(b"\x00", pos)
        name = inner[pos:end].decode("ascii")
        (ln,) = struct.unpack_from(">I", inner, end + 1)
        body = inner[end + 5:end + 5 + ln]
        if name == "CUE":
            (ms,) = struct.unpack_from(">I", body, 2)
            out["cues"].append({
                "index": body[1], "ms": ms,
                "color": int.from_bytes(body[7:10], "big"),
                "name": body[12:].split(b"\x00")[0].decode("utf-8"),
            })
        elif name == "COLOR":
            out["color"] = int.from_bytes(body[1:4], "big")
        elif name == "BPMLOCK":
            out["bpm_lock"] = bool(body[0])
        pos = end + 5 + ln
    return out


# ------------------------------------------------------------ BeatGrid


def grid_to_serato_markers(times: list[float], bpms: list[float]):
    """Collapse rekordbox's per-beat grid (PQTZ: beat times in seconds + BPM
    per beat) into Serato tempo-change markers.
    Returns (markers for beatgrid_payload, end_bpm) or (None, None)."""
    if not times or not bpms or len(times) != len(bpms):
        return None, None
    segments = [[0]]  # beat indices grouped by constant BPM
    for i in range(1, len(bpms)):
        if round(bpms[i], 2) != round(bpms[i - 1], 2):
            segments.append([i])
        else:
            segments[-1].append(i)
    markers = []
    for si, seg in enumerate(segments):
        start = times[seg[0]]
        if si == len(segments) - 1:
            markers.append((start, None))
        else:
            markers.append((start, len(seg)))
    return markers, bpms[segments[-1][0]]


def beatgrid_payload(markers: list[tuple], end_bpm: float) -> bytes:
    """Serato BeatGrid tag payload.

    markers: [(position_seconds, beats_until_next)] for tempo-change points;
    the last grid segment becomes the terminal marker (position, end_bpm).
    Pass markers=[(first_beat_sec, None)] for a constant-tempo grid.
    """
    if not markers:
        raise ValueError("need at least one grid marker")
    body = [b"\x01\x00", struct.pack(">I", len(markers))]
    for pos, beats in markers[:-1]:
        body.append(struct.pack(">fI", float(pos), int(beats)))
    body.append(struct.pack(">ff", float(markers[-1][0]), float(end_bpm)))
    body.append(b"\x00")  # footer
    return b"".join(body)


def parse_beatgrid(payload: bytes) -> dict:
    assert payload[:2] == b"\x01\x00", "bad BeatGrid header"
    (count,) = struct.unpack_from(">I", payload, 2)
    pos, markers = 6, []
    for i in range(count):
        if i == count - 1:
            p, bpm = struct.unpack_from(">ff", payload, pos)
            markers.append({"sec": p, "bpm": bpm})
        else:
            p, beats = struct.unpack_from(">fI", payload, pos)
            markers.append({"sec": p, "beats": beats})
        pos += 8
    return {"markers": markers}


# ------------------------------------------------------------ per-file tag IO

MARKERS2_NAME = "Serato Markers2"
BEATGRID_NAME = "Serato BeatGrid"
GEOB_MIME = "application/octet-stream"
_MP4_MEAN = "com.serato.dj"
_MP4_ATOM = {MARKERS2_NAME: "markersv2", BEATGRID_NAME: "beatgrid"}
_VORBIS = {MARKERS2_NAME: "SERATO_MARKERS_V2", BEATGRID_NAME: "SERATO_BEATGRID"}


def _envelope(name: str, payload: bytes) -> str:
    """MP4/FLAC store base64('application/octet-stream\\0\\0<name>\\0' + payload)."""
    raw = GEOB_MIME.encode() + b"\x00\x00" + name.encode() + b"\x00" + payload
    return _b64_lines(raw, 54).decode("ascii")


def _unenvelope(value: str) -> tuple[str, bytes]:
    raw = _b64_decode_forgiving(value.encode("ascii"))
    prefix = GEOB_MIME.encode() + b"\x00\x00"
    if not raw.startswith(prefix):
        raise ValueError("not a Serato envelope")
    rest = raw[len(prefix):]
    name, _, payload = rest.partition(b"\x00")
    return name.decode(), payload


def _tag_kind(path: str):
    """('id3'|'mp4'|'vorbis', mutagen file) for a path, or (None, None)."""
    import mutagen
    from mutagen.mp4 import MP4

    f = mutagen.File(path)
    if f is None:
        return None, None
    if isinstance(f, MP4):
        return "mp4", f
    if hasattr(f.tags, "getall") or f.tags is None and path.lower().endswith((".mp3", ".aif", ".aiff", ".wav")):
        return "id3", f
    if f.tags is not None and hasattr(f.tags, "get"):
        return "vorbis", f
    return None, None


def read_serato_tags(path: str) -> dict:
    """{'Serato Markers2': payload|None, 'Serato BeatGrid': payload|None}.
    Raw payload bytes (GEOB-body form), whatever the container."""
    kind, f = _tag_kind(path)
    out = {MARKERS2_NAME: None, BEATGRID_NAME: None}
    if kind is None or f.tags is None:
        return out
    if kind == "id3":
        for frame in f.tags.getall("GEOB"):
            if frame.desc in out:
                out[frame.desc] = bytes(frame.data)
    elif kind == "mp4":
        for name, atom in _MP4_ATOM.items():
            v = f.tags.get(f"----:{_MP4_MEAN}:{atom}")
            if v:
                try:
                    out[name] = _unenvelope(bytes(v[0]).decode("ascii", "ignore"))[1]
                except ValueError:
                    pass
    else:
        for name, field in _VORBIS.items():
            v = f.tags.get(field.lower()) or f.tags.get(field)
            if v:
                try:
                    out[name] = _unenvelope(str(v[0]))[1]
                except ValueError:
                    pass
    return out


def write_serato_tags(path: str, payloads: dict) -> dict | None:
    """Write {tag_name: payload_bytes|None} (None = leave untouched).
    Returns previous payloads (same shape, for the undo journal), or None if
    the container is unsupported."""
    kind, f = _tag_kind(path)
    if kind is None:
        return None
    old = read_serato_tags(path)
    if f.tags is None:
        f.add_tags()
    if kind == "id3":
        from mutagen.id3 import GEOB

        for name, payload in payloads.items():
            if payload is None:
                continue
            frames = [fr for fr in f.tags.getall("GEOB") if fr.desc != name]
            frames.append(GEOB(encoding=0, mime=GEOB_MIME, filename="",
                               desc=name, data=payload))
            f.tags.setall("GEOB", frames)
    elif kind == "mp4":
        from mutagen.mp4 import MP4FreeForm

        for name, payload in payloads.items():
            if payload is None:
                continue
            key = f"----:{_MP4_MEAN}:{_MP4_ATOM[name]}"
            f.tags[key] = [MP4FreeForm(_envelope(name, payload).encode("ascii"))]
    else:
        for name, payload in payloads.items():
            if payload is None:
                continue
            f.tags[_VORBIS[name]] = [_envelope(name, payload)]
    f.save()
    return old


def remove_serato_tags(path: str, names: list[str]):
    kind, f = _tag_kind(path)
    if kind is None or f.tags is None:
        return
    if kind == "id3":
        f.tags.setall("GEOB", [fr for fr in f.tags.getall("GEOB") if fr.desc not in names])
    elif kind == "mp4":
        for name in names:
            f.tags.pop(f"----:{_MP4_MEAN}:{_MP4_ATOM[name]}", None)
    else:
        for name in names:
            f.tags.pop(_VORBIS[name], None)
    f.save()


def restore_serato_tags(path: str, old: dict):
    """Undo helper: put back previous payloads; None means the tag didn't
    exist before, so remove it."""
    gone = [name for name, payload in old.items() if payload is None]
    keep = {name: payload for name, payload in old.items() if payload is not None}
    if keep:
        write_serato_tags(path, keep)
    if gone:
        remove_serato_tags(path, gone)
