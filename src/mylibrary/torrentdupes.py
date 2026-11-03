"""Pause a torrent that would download a file the library already has (TASK-33).

The proof is a checksum, not a name. A .torrent file holds a SHA-1 for every piece of its
data, so a library file of the same byte size is hashed over the same byte ranges and
compared before the download has finished, or even started. Only pieces that lie wholly
inside one file can be compared. PIECES of them (first, middle, last) are a cheap first
test; a file that passes it is read in full and every whole piece must match. A file too
small to hold one whole piece cannot be proven and is never called a duplicate. A torrent
already paused is not checked again, so the full read happens once.

The one change made to a torrent is torrent-stop (pause). Resume in Transmission undoes it
and loses nothing; MyLibrary never removes a torrent or its data. A torrent paused once is
never paused again, so a resume by the user sticks. If any media file in a torrent is a
duplicate, the whole torrent is paused.

The .torrent file and every name in it are untrusted: the parser is bounded, and names are
only compared and shown, never used to open a file.
"""

import hashlib
import os
import urllib.error
import urllib.request

from . import downloads
from .config import has_ext
from .db import now

PIECES = 3
MAX_TORRENT_BYTES = 32 * 1024 * 1024
MAX_PIECE_BYTES = 64 * 1024 * 1024
MAX_DEPTH = 64
FIELDS = ["id", "hashString", "name", "status", "downloadDir", "torrentFile", "files",
          "wanted", "leftUntilDone", "percentDone", "metadataPercentComplete"]


class BencodeError(ValueError):
    pass


def bdecode(data: bytes):
    """Decode one bencoded value that must fill `data`. Raises BencodeError on bad input."""
    def value(i, depth):
        if depth > MAX_DEPTH:
            raise BencodeError("nested too deep")
        c = data[i:i + 1]
        if c == b"i":
            j = data.find(b"e", i)
            if j < 0:
                raise BencodeError("unterminated integer")
            try:
                return int(data[i + 1:j]), j + 1
            except ValueError:
                raise BencodeError("bad integer") from None
        if c in (b"l", b"d"):
            i, items = i + 1, []
            while data[i:i + 1] != b"e":
                if i >= len(data):
                    raise BencodeError("unterminated list")
                item, i = value(i, depth + 1)
                items.append(item)
            if c == b"l":
                return items, i + 1
            if len(items) % 2 or not all(isinstance(k, bytes) for k in items[::2]):
                raise BencodeError("bad dictionary")
            return dict(zip(items[::2], items[1::2])), i + 1
        j = data.find(b":", i)
        if j < 0 or not data[i:j].isdigit():
            raise BencodeError("bad string")
        end = j + 1 + int(data[i:j])
        if end > len(data):
            raise BencodeError("string runs past the end")
        return data[j + 1:end], end

    out, end = value(0, 0)
    if end != len(data):
        raise BencodeError("trailing data")
    return out


def _text(b) -> str:
    return b.decode("utf-8", "replace") if isinstance(b, bytes) else str(b)


def read_torrent(path: str) -> dict | None:
    """Piece length, v1 piece hashes, and each file's name, length and offset in the data.
    None if the file is unreadable, malformed, or has no v1 hashes (v2-only)."""
    try:
        if os.path.getsize(path) > MAX_TORRENT_BYTES:
            return None
        with open(path, "rb") as fh:
            info = bdecode(fh.read()).get(b"info")
    except (OSError, BencodeError, AttributeError):
        return None
    if not isinstance(info, dict):
        return None
    plen, pieces, name = info.get(b"piece length"), info.get(b"pieces"), info.get(b"name")
    if not (isinstance(plen, int) and 0 < plen <= MAX_PIECE_BYTES
            and isinstance(pieces, bytes) and len(pieces) % 20 == 0 and isinstance(name, bytes)):
        return None
    if b"files" in info:
        listed = info[b"files"]
        if not isinstance(listed, list):
            return None
    else:
        listed = [{b"length": info.get(b"length"), b"path": []}]
    files, offset = [], 0
    for f in listed:
        length, parts = (f.get(b"length"), f.get(b"path")) if isinstance(f, dict) else (None, None)
        if not (isinstance(length, int) and length >= 0 and isinstance(parts, list)):
            return None
        # pad files (BEP 47) take up room in the pieces but are not real files
        pad = b"p" in (f.get(b"attr") or b"") if isinstance(f.get(b"attr"), bytes) else False
        if not pad:
            files.append({"name": "/".join([_text(name)] + [_text(p) for p in parts]),
                          "length": length, "offset": offset})
        offset += length
    if len(pieces) // 20 < -(-offset // plen):
        return None
    return {"piece_length": plen, "pieces": pieces, "files": files}


def piece_indexes(offset: int, length: int, plen: int) -> list[int]:
    """Up to PIECES pieces lying wholly inside one file: the first, the last and one between."""
    first, last = -(-offset // plen), (offset + length) // plen - 1
    if first > last:
        return []
    return sorted({first, last, (first + last) // 2})


def same_content(path: str, f: dict, meta: dict, indexes: list[int]) -> bool:
    """True if every given piece of torrent file `f` hashes the same in the file at `path`."""
    plen, pieces = meta["piece_length"], meta["pieces"]
    try:
        with open(path, "rb") as fh:
            if os.fstat(fh.fileno()).st_size != f["length"]:
                return False
            for i in indexes:
                fh.seek(i * plen - f["offset"])
                if hashlib.sha1(fh.read(plen)).digest() != pieces[i * 20:i * 20 + 20]:
                    return False
    except OSError:
        return False
    return bool(indexes)


def _media(cfg, name: str) -> bool:
    low = os.path.basename(name).lower()
    return has_ext(low, cfg.video_ext | cfg.audio_ext | cfg.software_ext) \
        and not any(w in low for w in cfg.ignore_words)


def _same(a: str, b: str) -> bool:
    try:
        return os.path.samefile(a, b)
    except OSError:
        return False


def find(con, cfg, opener=urllib.request.urlopen, skip=frozenset()) -> dict:
    """Unfinished torrents holding a media file the library already has. Read-only.
    Torrents whose hash is in `skip` are listed under "skipped" and not checked."""
    out = {"error": None, "checked": 0, "waiting": [], "unverifiable": [], "skipped": [],
           "dupes": []}
    if not cfg.transmission_enabled:
        out["error"] = "Transmission is not enabled in config.ini"
        return out
    try:
        torrents = downloads._torrents(cfg, opener, FIELDS)
    except (urllib.error.URLError, OSError, ValueError) as e:
        out["error"] = downloads.describe(e)
        return out
    min_bytes = cfg.dupe_min_mb * 1024 * 1024
    for t in torrents:
        if not downloads._unfinished(t):
            continue
        name = t.get("name", "?")
        if t.get("hashString") in skip:
            out["skipped"].append(name)
            continue
        if t.get("metadataPercentComplete", 1) < 1 or not t.get("torrentFile"):
            out["waiting"].append(name)
            continue
        meta = read_torrent(t["torrentFile"])
        if meta is None:
            out["unverifiable"].append(name)
            continue
        out["checked"] += 1
        wanted = t.get("wanted") or []
        listed = {f.get("name"): (wanted[i] if i < len(wanted) else True)
                  for i, f in enumerate(t.get("files", []))}
        for f in meta["files"]:
            if f["length"] < min_bytes or not _media(cfg, f["name"]) or not listed.get(f["name"], True):
                continue
            indexes = piece_indexes(f["offset"], f["length"], meta["piece_length"])
            if not indexes:
                continue
            every = range(indexes[0], indexes[-1] + 1)
            own = os.path.normpath(os.path.join(t.get("downloadDir", ""), f["name"]))
            for r in con.execute("SELECT path FROM files WHERE missing=0 AND size=? ORDER BY id",
                                 (f["length"],)):
                if r["path"] == own or _same(r["path"], own):
                    continue
                if same_content(r["path"], f, meta, indexes) \
                        and same_content(r["path"], f, meta, every):
                    out["dupes"].append({"hash": t.get("hashString"), "id": t.get("id"),
                                         "torrent": name, "status": t.get("status"),
                                         "file": f["name"], "length": f["length"],
                                         "match": r["path"], "pieces": len(every)})
                    break
    return out


def pause_dupes(con, cfg, opener=urllib.request.urlopen, log=print) -> dict:
    """Pause each torrent holding a duplicate, once. Records every finding in torrent_dupes."""
    paused = {r["hash"] for r in con.execute(
        "SELECT DISTINCT hash FROM torrent_dupes WHERE paused_at IS NOT NULL")}
    res = find(con, cfg, opener, skip=paused)
    res["paused"] = []
    if res["error"]:
        return res
    by_hash: dict[str, list[dict]] = {}
    for d in res["dupes"]:
        if d["hash"]:
            by_hash.setdefault(d["hash"], []).append(d)
    for h, found in by_hash.items():
        for d in found:
            con.execute("""INSERT INTO torrent_dupes (hash, torrent, file, length, match, pieces,
                           found_at) VALUES (?,?,?,?,?,?,?) ON CONFLICT(hash, file) DO UPDATE SET
                           match=excluded.match, pieces=excluded.pieces""",
                        (h, d["torrent"], d["file"], d["length"], d["match"], d["pieces"], now()))
        try:
            downloads.call(cfg, opener, "torrent-stop", {"ids": [h]})
            con.execute("UPDATE torrent_dupes SET paused_at=?, error=NULL WHERE hash=?", (now(), h))
            res["paused"].append(found[0]["torrent"])
            log(f"paused: {found[0]['torrent']}")
        except (urllib.error.URLError, OSError, ValueError) as e:
            con.execute("UPDATE torrent_dupes SET error=? WHERE hash=?", (downloads.describe(e), h))
            log(f"WARN could not pause {found[0]['torrent']}: {downloads.describe(e)}")
        con.commit()
    return res


def listing(con, limit: int = 200) -> list[dict]:
    """What has been found and paused, newest first. Read-only; used by the UI."""
    return [dict(r) for r in con.execute(
        """SELECT hash, torrent, file, length, match, pieces, found_at, paused_at, error
           FROM torrent_dupes ORDER BY COALESCE(paused_at, found_at) DESC, id DESC LIMIT ?""",
        (limit,))]
