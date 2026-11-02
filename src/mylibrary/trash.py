"""Move to trash (TASK-31): mark files from the tree; apply moves them aside. Read only here.

Marking only records a decision in SQLite, with the file's identity (inode, size, mtime).
The next `mylib apply --execute` moves each ready mark with safe_move into
<root>/.mylibrary-safety/run-N/trash/<same relative path>, and `mylib undo --run N` puts it
back. Nothing is ever deleted: the person removes the trash folder when sure, so disk space
comes back only then. The web UI may reach this module, so it never imports apply or safefs.
"""

import os
import stat

from . import downloads
from .config import has_ext
from .db import now
from .player import Refused, locate

TRASH_DIR = "trash"


def check(cfg, path: str, active) -> tuple[str, os.stat_result]:
    """Return (root, lstat) for a file that may go to trash now, or raise Refused."""
    root, _ = locate(cfg, path)
    try:
        st = os.lstat(path)
    except OSError:
        raise Refused("no such file")
    if stat.S_ISLNK(st.st_mode):
        raise Refused("symlinks are not moved to trash")
    if not stat.S_ISREG(st.st_mode):
        raise Refused("only files can be moved to trash")
    if active.error:
        raise Refused(f"{active.error}, so nothing goes to trash")
    if path in active.paths:
        raise Refused("still downloading")
    if path in active.held:
        raise Refused(f"held by the torrent {active.held[path]}")
    if st.st_dev != os.stat(root).st_dev:
        raise Refused("on a different drive from its root")
    return root, st


def sidecars(cfg, path: str) -> list[str]:
    """Subtitle and nfo files next to a video that share its name, as the planner finds them."""
    if not has_ext(os.path.basename(path), cfg.video_ext):
        return []
    folder, name = os.path.split(path)
    stem = os.path.splitext(name)[0]
    try:
        names = sorted(os.listdir(folder))
    except OSError:
        return []
    return [os.path.join(folder, n) for n in names
            if n != name and n.startswith(stem) and "." in n
            and n.rsplit(".", 1)[-1].lower() in cfg.sidecar_ext
            and os.path.isfile(os.path.join(folder, n))
            and not os.path.islink(os.path.join(folder, n))]


def _marked(con) -> dict[str, dict]:
    return {r["path"]: dict(r) for r in con.execute("SELECT * FROM trash WHERE status='marked'")}


def preview(con, cfg, path: str, active=None) -> dict:
    """What the confirmation shows: the file, its sidecars, and whether it can be marked."""
    active = active or downloads.check(cfg)
    marked = _marked(con)
    out = {"path": path, "name": os.path.basename(path), "marked": path in marked,
           "error": None, "size": None, "sidecars": []}
    try:
        out["size"] = check(cfg, path, active)[1].st_size
    except Refused as e:
        out["error"] = str(e)
        return out
    for s in sidecars(cfg, path):
        try:
            size, why = check(cfg, s, active)[1].st_size, None
        except Refused as e:
            size, why = None, str(e)
        out["sidecars"].append({"path": s, "name": os.path.basename(s), "size": size,
                                "marked": s in marked, "error": why})
    return out


def mark(con, cfg, paths, active=None) -> dict:
    """Record the decision to move these files to trash. Changes nothing on disk."""
    active = active or downloads.check(cfg)
    done, refused = [], []
    for path in dict.fromkeys(paths):
        try:
            root, st = check(cfg, path, active)
        except Refused as e:
            refused.append({"path": path, "reason": str(e)})
            continue
        con.execute("""INSERT INTO trash (path, root, inode, size, mtime, marked_at, status)
                       VALUES (?,?,?,?,?,?,'marked')
                       ON CONFLICT(path) DO UPDATE SET root=excluded.root, inode=excluded.inode,
                       size=excluded.size, mtime=excluded.mtime, marked_at=excluded.marked_at,
                       status='marked', run_id=NULL, dst=NULL""",
                    (path, root, st.st_ino, st.st_size, st.st_mtime, now()))
        done.append(path)
    con.commit()
    return {"marked": done, "refused": refused}


def clear(con, paths) -> dict:
    """Take marks off files that have not moved yet."""
    paths = list(dict.fromkeys(paths))
    n = 0
    for path in paths:
        n += con.execute("DELETE FROM trash WHERE path=? AND status='marked'", (path,)).rowcount
    con.commit()
    return {"cleared": n}


def listing(con) -> list[dict]:
    return [dict(r) for r in con.execute(
        "SELECT * FROM trash WHERE status='marked' ORDER BY path")]


def prepare(con, cfg, active) -> dict:
    """Which marks the next apply would move, and why the rest would stay. Read only."""
    ready, refused = [], []
    roots = {os.path.normpath(str(r)) for r in cfg.roots}
    for m in _marked(con).values():
        why = None
        try:
            root, st = check(cfg, m["path"], active)
        except Refused as e:
            why = str(e)
        if m["root"] not in roots and not why:
            why = "root is not in the config"
        if not why and (st.st_ino != m["inode"] or st.st_size != m["size"]
                        or abs(st.st_mtime - (m["mtime"] or 0)) >= 1):
            why = "file changed since it was marked; mark it again"
        if why:
            refused.append({"id": m["id"], "path": m["path"], "reason": why})
        else:
            ready.append({"id": m["id"], "path": m["path"], "root": root,
                          "rel": os.path.relpath(m["path"], root),
                          "inode": st.st_ino, "size": st.st_size})
    return {"ready": ready, "refused": refused}
