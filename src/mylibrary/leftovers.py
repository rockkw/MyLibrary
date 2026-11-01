"""Report folders that moves left with nothing worth keeping. Report only.

MyLibrary never deletes a folder: this lists candidates and their contents, and the
person decides. Approved ones are moved aside by apply.clean_leftovers. Only folders a run moved files out of qualify, and their
parents once nothing else remains in them, so unrelated folders are never suggested.

A folder a torrent still lists is reported apart, because removing it makes the
client report missing data. If the client cannot be checked, nothing is suggested.
"""

import os

from . import downloads
from .config import has_ext
from .scan import SAFETY_DIR

ALWAYS_LEFTOVER = {".ds_store", "thumbs.db", "desktop.ini"}


def is_leftover(name: str, size: int, cfg) -> bool:
    if name.startswith("._") or name.lower() in ALWAYS_LEFTOVER:
        return True
    return has_ext(name, cfg.leftover_ext) and size <= cfg.leftover_max_mb * 1024 * 1024


def inspect(folder: str, cfg) -> dict | None:
    """List a folder's files, or return None if anything in it should be kept."""
    files = []
    for dirpath, dirnames, filenames in os.walk(folder):
        if any(os.path.islink(os.path.join(dirpath, d)) for d in dirnames):
            return None
        for name in filenames:
            path = os.path.join(dirpath, name)
            st = os.lstat(path)
            if os.path.islink(path) or not is_leftover(name, st.st_size, cfg):
                return None
            files.append({"name": os.path.relpath(path, folder), "size": st.st_size})
    return {"path": folder, "size": sum(f["size"] for f in files), "files": sorted(
        files, key=lambda f: f["name"])}


def root_of(path: str, roots: list[str]) -> str | None:
    return next((r for r in roots if path.startswith(r + os.sep)), None)


def _candidates(con, cfg) -> set[str]:
    roots = [str(r) for r in cfg.roots]
    found = set()
    for r in con.execute("SELECT DISTINCT src FROM moves WHERE status='moved'"):
        root = root_of(r["src"], roots)
        # An undone cleanup records a path inside the safety folder as its source.
        if not root or f"{os.sep}{SAFETY_DIR}{os.sep}" in r["src"]:
            continue
        protected = {os.path.join(root, d) for d in (cfg.movies_dir, cfg.tv_dir, cfg.music_dir,
                                                     cfg.software_dir, cfg.adult_dir, SAFETY_DIR)}
        folder = os.path.dirname(r["src"])
        while folder != root:
            if folder in protected:
                break
            found.add(folder)
            folder = os.path.dirname(folder)
    return {f for f in found if os.path.isdir(f)}


def find(con, cfg) -> dict:
    active = downloads.check(cfg)
    out = {"error": active.error, "empty": [], "leftovers": [], "held": []}
    if active.error:
        return out
    done = []
    for folder in sorted(_candidates(con, cfg), key=len):  # parents first
        if any(folder.startswith(d + os.sep) for d in done):
            continue
        item = inspect(folder, cfg)
        if not item:
            continue
        done.append(folder)
        torrent = next((name for path, name in active.held.items()
                        if path.startswith(folder + os.sep)), None)
        if torrent:
            out["held"].append({**item, "torrent": torrent})
        else:
            out["leftovers" if item["files"] else "empty"].append(item)
    return out
