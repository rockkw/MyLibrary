"""Work out what an apply would do, changing nothing.

apply uses this before it moves anything, and the file tree uses it to preview a run, so
the two cannot disagree. The web UI may reach this module, so it must stay read-only and
must never import apply or safefs.
"""

import os

from . import downloads


def _nearest_existing(path: str) -> str:
    while path and not os.path.exists(path):
        parent = os.path.dirname(path)
        if parent == path:
            break
        path = parent
    return path


def _sidecars(src: str, dst: str, sidecar_ext: set[str]):
    """Find subtitle/nfo files sharing the video's stem; map them next to the new name."""
    d = os.path.dirname(src)
    stem = os.path.splitext(os.path.basename(src))[0]
    new_stem = os.path.splitext(dst)[0]
    out = []
    try:
        names = os.listdir(d)
    except OSError:
        return out
    for name in names:
        if name == os.path.basename(src) or not name.startswith(stem):
            continue
        ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
        if ext in sidecar_ext:
            out.append((os.path.join(d, name), new_stem + name[len(stem):]))
    return out


def prepare(con, cfg, plan_id, active=None) -> dict:
    items = con.execute("""SELECT pr.id pid, pr.file_id, pr.src, pr.dst, f.root, f.inode, f.size, f.mtime
                           FROM proposals pr JOIN files f ON f.id=pr.file_id
                           WHERE pr.plan_id=? AND pr.status IN ('auto','approved')
                           AND pr.dst IS NOT NULL ORDER BY pr.src""", (plan_id,)).fetchall()
    ok, refused, targets = [], [], set()
    active = active or downloads.check(cfg)
    for it in items:
        src, dst = it["src"], it["dst"]
        why = None
        try:
            st = os.lstat(src)
        except FileNotFoundError:
            st, why = None, "source missing (run scan)"
        if active.error:
            why = active.error
        elif src in active.paths:
            why = "still downloading"
        # Downloaders preallocate, so size alone cannot show that a file is still filling up.
        elif st and (st.st_ino != it["inode"] or st.st_size != it["size"]
                   or abs(st.st_mtime - (it["mtime"] or 0)) >= 1):
            why = "file changed since scan (run scan)"
        elif os.path.lexists(dst):
            why = "target already exists"
        elif dst in targets:
            why = "two items share this target"
        elif st and os.stat(_nearest_existing(os.path.dirname(dst))).st_dev != st.st_dev:
            why = "target is on a different drive"
        sidecars = []
        if not why:
            for s_src, s_dst in _sidecars(src, dst, cfg.sidecar_ext):
                if os.path.lexists(s_dst) or s_dst in targets:
                    why = f"sidecar target exists: {os.path.basename(s_dst)}"
                    break
                sidecars.append((s_src, s_dst))
        if why:
            refused.append({"proposal_id": it["pid"], "src": src, "dst": dst, "reason": why})
            continue
        targets.add(dst)
        targets.update(d for _, d in sidecars)
        ok.append({"proposal_id": it["pid"], "file_id": it["file_id"], "root": it["root"],
                   "src": src, "dst": dst, "inode": st.st_ino, "size": st.st_size,
                   "sidecars": sidecars})
    return {"plan_id": plan_id, "moves": ok, "refused": refused,
            "count": len(ok), "sidecar_count": sum(len(m["sidecars"]) for m in ok),
            "bytes_moved_on_disk": 0}
