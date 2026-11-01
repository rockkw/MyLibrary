"""The whole library as a tree, marked with what the next apply would do. Read only.

The disk is the source, because the database tracks only media. Moves come from
preflight.prepare, the same check apply runs, so this preview matches the dry run.
Symlinks are listed but never followed.
"""

import os

from . import downloads, preflight
from .config import has_ext
from .scan import SAFETY_DIR

GROUPS = ("video", "music", "software", "other")
WONT_MOVE = {"review": "decide", "conflict": "decide", "skipped": "skipped", "ignored": "ignored"}


def group_of(name: str, cfg) -> str:
    if has_ext(name, cfg.video_ext):
        return "video"
    if has_ext(name, cfg.audio_ext):
        return "music"
    return "software" if has_ext(name, cfg.software_ext) else "other"


def _blank_dir(name: str, path: str, new: bool = False) -> dict:
    return {"type": "dir", "name": name, "path": path, "new": new, "children": [],
            "counts": dict.fromkeys(GROUPS, 0), "in": 0, "out": 0, "left": False}


def _marks(con, cfg, plan_id, active) -> tuple[dict, list]:
    """Map each source path to its marker, and list every planned move."""
    marks, moves = {}, []
    if not plan_id:
        return marks, moves
    prep = preflight.prepare(con, cfg, plan_id, active)
    for r in con.execute("""SELECT id, src, status, reason FROM proposals WHERE plan_id=?""",
                         (plan_id,)):
        if r["status"] in WONT_MOVE:
            marks[r["src"]] = {"state": WONT_MOVE[r["status"]], "reason": r["reason"],
                               "proposal_id": r["id"]}
        else:
            marks[r["src"]] = {"proposal_id": r["id"]}
    for r in prep["refused"]:
        marks[r["src"]] = {"state": "decide", "reason": f"apply refuses: {r['reason']}",
                           "proposal_id": r["proposal_id"]}
    for m in prep["moves"]:
        for src, dst in [(m["src"], m["dst"])] + list(m["sidecars"]):
            marks[src] = {"state": "out", "to": dst, "proposal_id": m["proposal_id"]}
            moves.append((src, dst, m["proposal_id"]))
    return marks, moves


def _walk(path: str, name: str, ctx: dict) -> dict:
    node = _blank_dir(name, path)
    try:
        entries = sorted(os.scandir(path), key=lambda e: e.name.casefold())
    except OSError as e:
        node["error"] = e.strerror or "cannot read"
        return node
    for entry in entries:
        if not ctx["hidden"] and (entry.name == SAFETY_DIR or entry.name.startswith("._")):
            continue
        if entry.is_dir(follow_symlinks=False):
            node["children"].append(_walk(entry.path, entry.name, ctx))
        else:
            node["children"].append(_file(entry, ctx))
    return node


def _file(entry, ctx: dict) -> dict:
    size = entry.stat(follow_symlinks=False).st_size
    node = {"type": "file", "name": entry.name, "path": entry.path, "size": size,
            "group": group_of(entry.name, ctx["cfg"]), "state": "",
            "symlink": entry.is_symlink(), **ctx["marks"].get(entry.path, {})}
    active = ctx["active"]
    if entry.path in active.paths:
        node.update(state="downloading", reason="still downloading")
    elif entry.path in active.held:
        node["torrent"] = active.held[entry.path]
    if not node["state"] and entry.path not in ctx["known"]:
        node["state"] = "untracked"
    return node


def _add_incoming(root: dict, src: str, dst: str, proposal_id: int) -> None:
    """Add a planned file at its target, with any folders the run would create."""
    node, at = root, root["path"]
    parts = os.path.relpath(dst, root["path"]).split(os.sep)
    if parts[0] == os.pardir:
        return
    for part in parts[:-1]:
        at = os.path.join(at, part)
        found = next((c for c in node["children"] if c["name"] == part), None)
        if found is None:
            found = _blank_dir(part, at, new=True)
            node["children"].append(found)
        elif found["type"] != "dir":
            return
        node = found
    node["children"].append({"type": "file", "name": parts[-1], "path": dst, "size": None,
                             "group": "", "state": "in", "from": src, "symlink": False,
                             "proposal_id": proposal_id})


def _total(node: dict, keep: set, cfg) -> int:
    """Fill in folder totals, and return how many media files stay here after the run."""
    remaining = 0
    node["children"].sort(key=lambda c: (c["type"] != "dir", c["name"].casefold()))
    for c in node["children"]:
        if c["type"] == "dir":
            remaining += _total(c, keep, cfg)
            for key in ("in", "out"):
                node[key] += c[key]
            for group in GROUPS:
                node["counts"][group] += c["counts"][group]
        elif c["state"] == "in":
            node["in"] += 1
            remaining += group_of(c["name"], cfg) != "other"
        else:
            node["counts"][c["group"]] += 1
            node["out"] += c["state"] == "out"
            remaining += c["group"] != "other" and c["state"] != "out"
    node["left"] = bool(node["out"]) and not remaining and node["path"] not in keep
    return remaining


def build(con, cfg, hidden: bool = False) -> dict:
    plan = con.execute("SELECT id, created_at FROM plans WHERE superseded=0 "
                       "ORDER BY id DESC LIMIT 1").fetchone()
    plan_id = plan["id"] if plan else None
    active = downloads.check(cfg)
    marks, moves = _marks(con, cfg, plan_id, active)
    known = {r["path"] for r in con.execute("SELECT path FROM files WHERE missing=0")}
    ctx = {"cfg": cfg, "hidden": hidden, "marks": marks, "active": active, "known": known}
    trees, missing = [], []
    for root in map(str, cfg.roots):
        if not os.path.isdir(root):
            missing.append(root)
            continue
        node = _walk(root, root, ctx)
        for src, dst, proposal_id in moves:
            if src.startswith(root + os.sep):
                _add_incoming(node, src, dst, proposal_id)
        keep = {root} | {os.path.join(root, d) for d in (cfg.movies_dir, cfg.tv_dir, cfg.music_dir,
                                                         cfg.software_dir, cfg.adult_dir)}
        _total(node, keep, cfg)
        trees.append(node)
    scanned = con.execute("SELECT MAX(scanned_at) FROM files").fetchone()[0]
    return {"plan_id": plan_id, "roots": trees, "missing": missing, "error": active.error,
            "stale": bool(plan and scanned and scanned > plan["created_at"]),
            "totals": {g: sum(t["counts"][g] for t in trees) for g in GROUPS},
            "moves": sum(t["in"] for t in trees)}
