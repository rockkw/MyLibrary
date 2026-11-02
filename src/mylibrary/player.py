"""Which files the web UI may stream for playback, and what to say about their format.

Read only, and reachable from the web UI, so it never imports apply or safefs. A file can
be played only if it is a regular file with a video extension inside a configured root,
reached without any symlink, and not inside the safety folder. Disc images are refused:
a browser cannot play them.
"""

import os

from .config import has_ext
from .scan import SAFETY_DIR

NOT_PLAYABLE_EXT = {"iso"}
# Containers a browser's <video> element can usually open. Anything else gets a warning.
BROWSER_CONTAINERS = {"mp4", "m4v", "webm", "mov", "mkv"}
BROWSER_VCODECS = {"h264", "vp8", "vp9", "av1"}
BROWSER_ACODECS = {"aac", "mp3", "opus", "vorbis", "flac"}
MIME = {"mkv": "video/x-matroska", "mp4": "video/mp4", "m4v": "video/mp4", "webm": "video/webm",
        "mov": "video/quicktime", "avi": "video/x-msvideo", "wmv": "video/x-ms-wmv",
        "mpg": "video/mpeg", "mpeg": "video/mpeg", "ts": "video/mp2t", "m2ts": "video/mp2t"}


class Refused(ValueError):
    """The path may not be streamed. The message is safe to show."""


def ext_of(path: str) -> str:
    return os.path.splitext(path)[1].lower().lstrip(".")


def locate(cfg, path: str) -> tuple[str, str]:
    """Return (root, real path) for a path inside a configured root, or raise Refused.

    The path must be absolute and normal, outside the safety folder, and reached without any
    symlink. Shared with trash.py. It does not check that the file exists.
    """
    if not isinstance(path, str) or not path.startswith("/") or "\0" in path:
        raise Refused("not an absolute path")
    norm = os.path.normpath(path)
    if norm != path.rstrip("/") or any(p == os.pardir for p in path.split("/")):
        raise Refused("path is not in normal form")
    root = next((os.path.normpath(str(r)) for r in cfg.roots
                 if norm.startswith(os.path.normpath(str(r)) + os.sep)), None)
    if root is None:
        raise Refused("not inside a configured root")
    rel = os.path.relpath(norm, root)
    if SAFETY_DIR in rel.split(os.sep):
        raise Refused("files in the safety folder are off limits")
    real = os.path.realpath(norm)
    if real != os.path.join(os.path.realpath(root), rel):
        raise Refused("the path goes through a symlink")
    return root, real


def resolve(cfg, path: str) -> str:
    """Return the checked real path of a playable video, or raise Refused."""
    _, real = locate(cfg, path)
    name = os.path.basename(path)
    if not has_ext(name, cfg.video_ext) or ext_of(name) in NOT_PLAYABLE_EXT:
        raise Refused("only video files can be played")
    if not os.path.isfile(real):
        raise Refused("no such file")
    return real


def mimetype(path: str) -> str:
    return MIME.get(ext_of(path), "application/octet-stream")


def info(con, cfg, path: str) -> dict:
    """Name, size and format of a playable file, and a warning when a browser may not cope."""
    real = resolve(cfg, path)
    row = con.execute("""SELECT f.vcodec, f.acodec, f.container, f.width, f.height, f.duration,
                         (SELECT p.id FROM proposals p JOIN plans pl ON pl.id=p.plan_id
                          WHERE p.file_id=f.id AND pl.superseded=0 ORDER BY p.id DESC LIMIT 1) pid
                         FROM files f WHERE f.path=? AND f.missing=0""", (path,)).fetchone()
    row = dict(row) if row else {}
    ext = ext_of(path)
    vcodec, acodec = (row.get("vcodec") or "").lower(), (row.get("acodec") or "").lower()
    warnings = []
    if ext not in BROWSER_CONTAINERS:
        warnings.append(f"Browsers usually cannot play .{ext} files.")
    if vcodec and vcodec not in BROWSER_VCODECS:
        warnings.append(f"The video is {vcodec.upper()}, which many browsers cannot decode"
                        + (" (HEVC needs hardware support)." if vcodec in ("hevc", "h265") else "."))
    if acodec and acodec not in BROWSER_ACODECS:
        warnings.append(f"The audio is {acodec.upper()}, which browsers usually cannot play, "
                        "so the file may not play at all or may have no sound.")
    return {"path": path, "name": os.path.basename(path), "size": os.path.getsize(real),
            "ext": ext, "vcodec": vcodec or None, "acodec": acodec or None,
            "width": row.get("width"), "height": row.get("height"),
            "duration": row.get("duration"), "proposal_id": row.get("pid"), "warnings": warnings}
