"""Read-only parser for rekordbox device exports (PIONEER/rekordbox/export.pdb).

The .pdb file is "DeviceSQL" — a page-based binary format, unrelated to the
SQLCipher master.db. Layout reverse-engineered by Deep Symmetry's crate-digger
project (rekordbox_pdb.ksy); this is a minimal pure-Python reader for the
tables the USB checker and Serato builder need. Never writes.

Structure recap:
  * file header: page size, table directory (type, first/last page)
  * each table is a chain of fixed-size pages (next_page links)
  * rows live in a heap at page offset 0x28; row offsets are indexed from the
    END of the page in groups of 16 with a presence bitmask per group
  * strings are "DeviceSQL strings": 1-byte kind (short ASCII with embedded
    length, or 0x40/0x90 long ASCII/UTF-16LE)
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field
from pathlib import Path

PAGE_TYPES = {
    0: "tracks", 1: "genres", 2: "artists", 3: "albums", 4: "labels",
    5: "keys", 6: "colors", 7: "playlist_tree", 8: "playlist_entries",
    11: "history_playlists", 12: "history_entries", 13: "artwork",
    16: "columns", 19: "history",
}


class PdbError(Exception):
    pass


def _read_string(buf: bytes, pos: int) -> str:
    """DeviceSQL string at absolute offset `pos`."""
    kind = buf[pos]
    if kind == 0x40:  # long ASCII: u2 total len, u1 pad, bytes
        (ln,) = struct.unpack_from("<H", buf, pos + 1)
        raw = buf[pos + 4 : pos + ln]
        return raw.decode("ascii", "replace")
    if kind == 0x90:  # long UTF-16LE
        (ln,) = struct.unpack_from("<H", buf, pos + 1)
        raw = buf[pos + 4 : pos + ln]
        return raw.decode("utf-16-le", "replace").rstrip("\x00")
    # short ASCII: length (incl. header byte) = kind >> 1
    ln = (kind >> 1) - 1
    if ln < 0:
        return ""
    return buf[pos + 1 : pos + 1 + ln].decode("ascii", "replace")


@dataclass
class PdbLibrary:
    path: Path
    len_page: int = 0
    tracks: list = field(default_factory=list)          # dicts, see _parse_track
    playlists: list = field(default_factory=list)        # {id,parent_id,sort_order,is_folder,name}
    playlist_entries: list = field(default_factory=list) # {entry_index,track_id,playlist_id}
    artists: dict = field(default_factory=dict)          # id -> name
    albums: dict = field(default_factory=dict)
    genres: dict = field(default_factory=dict)
    keys: dict = field(default_factory=dict)
    colors: dict = field(default_factory=dict)
    row_errors: list = field(default_factory=list)       # (table, page_index, slot, message)

    @property
    def tracks_by_id(self) -> dict:
        return {t["id"]: t for t in self.tracks}


def parse_pdb(path: str | Path) -> PdbLibrary:
    """Parse an export.pdb. Raises PdbError on structural corruption; per-row
    problems are collected in .row_errors instead of aborting."""
    path = Path(path)
    buf = path.read_bytes()
    lib = PdbLibrary(path=path)
    if len(buf) < 0x1C:
        raise PdbError(f"file too small to be a pdb ({len(buf)} bytes)")

    zero, len_page, num_tables, next_unused = struct.unpack_from("<IIII", buf, 0)
    if zero != 0:
        raise PdbError(f"bad signature word {zero:#x} (expected 0)")
    if len_page == 0 or len_page % 512 or len_page > 65536:
        raise PdbError(f"implausible page size {len_page}")
    if len(buf) % len_page:
        raise PdbError(f"file size {len(buf)} is not a multiple of page size {len_page}")
    n_pages = len(buf) // len_page
    if num_tables == 0 or num_tables > 64:
        raise PdbError(f"implausible table count {num_tables}")
    lib.len_page = len_page

    tables = []
    tpos = 0x1C
    if tpos + num_tables * 16 > len_page:
        raise PdbError("table directory overruns first page")
    for _ in range(num_tables):
        ttype, _empty, first_page, last_page = struct.unpack_from("<IIII", buf, tpos)
        tables.append((ttype, first_page, last_page))
        tpos += 16

    handlers = {
        0: _parse_track,
        1: _row_named(lib_attr="genres"),
        2: _parse_artist,
        3: _parse_album,
        5: _parse_key,
        6: _parse_color,
        7: _parse_playlist_tree,
        8: _parse_playlist_entry,
    }

    for ttype, first_page, last_page in tables:
        handler = handlers.get(ttype)
        if handler is None:
            continue
        page_idx = first_page
        seen = set()
        while page_idx and page_idx not in seen:
            seen.add(page_idx)
            if page_idx >= n_pages:
                lib.row_errors.append((PAGE_TYPES.get(ttype, ttype), page_idx, -1,
                                       "page index past end of file"))
                break
            at_last = page_idx == last_page
            page_idx = _parse_page(buf, len_page, page_idx, ttype, handler, lib)
            if at_last:
                break
    return lib


def _parse_page(buf, len_page, page_idx, ttype, handler, lib) -> int:
    """Parse one page; returns next_page index."""
    off = page_idx * len_page
    (_gap, pidx, ptype, next_page, _seq, _unk) = struct.unpack_from("<IIIIII", buf, off)
    # 24-bit field: low 13 bits = row offsets ever allocated, high 11 = valid rows
    b0, b1, b2 = buf[off + 0x18 : off + 0x1B]
    n24 = b0 | (b1 << 8) | (b2 << 16)
    num_row_offsets = n24 & 0x1FFF
    page_flags = buf[off + 0x1B]
    is_data_page = (page_flags & 0x40) == 0

    if is_data_page and ptype == ttype and num_row_offsets:
        heap = off + 0x28
        n_groups = (num_row_offsets - 1) // 16 + 1
        for g in range(n_groups):
            base = off + len_page - g * 0x24
            (present,) = struct.unpack_from("<H", buf, base - 4)
            n_slots = min(16, num_row_offsets - g * 16)
            for i in range(n_slots):
                if not (present >> i) & 1:
                    continue
                (ofs_row,) = struct.unpack_from("<H", buf, base - (6 + 2 * i))
                row_base = heap + ofs_row
                if row_base >= off + len_page:
                    lib.row_errors.append((PAGE_TYPES.get(ttype, ttype), pidx, g * 16 + i,
                                           "row offset outside page"))
                    continue
                try:
                    handler(buf, row_base, lib)
                except Exception as e:  # collect, don't abort: one bad row != bad stick
                    lib.row_errors.append((PAGE_TYPES.get(ttype, ttype), pidx, g * 16 + i,
                                           f"{type(e).__name__}: {e}"))
    return next_page


# ---------------------------------------------------------------- row parsers

# track_row fixed part: subtype, index_shift, bitmask, sample_rate, composer_id,
# file_size, u4, u2, u2, artwork_id, key_id, orig_artist_id, label_id,
# remixer_id, bitrate, track_number, tempo, genre_id, album_id, artist_id, id,
# disc, play_count, year, sample_depth, duration, u2, color, rating, u2, u2
_TRACK_FMT = "<HHIIIIIHHIIIIIIIIIIIIHHHHHHBBHH"
_TRACK_SIZE = struct.calcsize(_TRACK_FMT)
# names of the 21 trailing string offsets we care about (index -> field)
_TRACK_STRINGS = {10: "date_added", 12: "mix_name", 14: "analyze_path",
                  16: "comment", 17: "title", 19: "filename", 20: "file_path"}


def _parse_track(buf, base, lib):
    v = struct.unpack_from(_TRACK_FMT, buf, base)
    ofs_strings = struct.unpack_from("<21H", buf, base + _TRACK_SIZE)
    t = {
        "id": v[20], "sample_rate": v[3], "file_size": v[5],
        "artwork_id": v[9], "key_id": v[10], "label_id": v[12],
        "bitrate": v[14], "track_number": v[15], "tempo_100": v[16],
        "genre_id": v[17], "album_id": v[18], "artist_id": v[19],
        "disc": v[21], "play_count": v[22], "year": v[23],
        "sample_depth": v[24], "duration": v[25], "color_id": v[27],
        "rating": v[28],
    }
    for idx, name in _TRACK_STRINGS.items():
        t[name] = _read_string(buf, base + ofs_strings[idx])
    lib.tracks.append(t)


def _parse_artist(buf, base, lib):
    subtype, _shift, aid = struct.unpack_from("<HHI", buf, base)
    if subtype & 0x04:
        (ofs,) = struct.unpack_from("<H", buf, base + 0x0A)
    else:
        ofs = buf[base + 0x09]
    lib.artists[aid] = _read_string(buf, base + ofs)


def _parse_album(buf, base, lib):
    subtype, _shift, _u, artist_id, alid = struct.unpack_from("<HHIII", buf, base)
    if subtype & 0x04:
        (ofs,) = struct.unpack_from("<H", buf, base + 0x16)
    else:
        ofs = buf[base + 0x15]
    lib.albums[alid] = _read_string(buf, base + ofs)


def _row_named(lib_attr):
    """Rows shaped as (u4 id, inline name): genres, labels."""
    def parse(buf, base, lib):
        (rid,) = struct.unpack_from("<I", buf, base)
        getattr(lib, lib_attr)[rid] = _read_string(buf, base + 4)
    return parse


def _parse_key(buf, base, lib):
    kid, _id2 = struct.unpack_from("<II", buf, base)
    lib.keys[kid] = _read_string(buf, base + 8)


def _parse_color(buf, base, lib):
    (cid,) = struct.unpack_from("<H", buf, base + 5)
    lib.colors[cid] = _read_string(buf, base + 8)


def _parse_playlist_tree(buf, base, lib):
    parent_id, _u, sort_order, pid, raw_is_folder = struct.unpack_from("<IIIII", buf, base)
    lib.playlists.append({
        "id": pid, "parent_id": parent_id, "sort_order": sort_order,
        "is_folder": raw_is_folder != 0,
        "name": _read_string(buf, base + 20),
    })


def _parse_playlist_entry(buf, base, lib):
    entry_index, track_id, playlist_id = struct.unpack_from("<III", buf, base)
    lib.playlist_entries.append({
        "entry_index": entry_index, "track_id": track_id, "playlist_id": playlist_id,
    })
