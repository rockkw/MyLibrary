"""Step 4: turn identifications into a reviewable rename plan. Touches only the DB."""

import json
import os
import re

from . import imdb
from .db import now

BAD_CHARS = re.compile(r'[<>"/\\|?*\x00-\x1f]')


def clean(title: str) -> str:
    t = title.replace(":", " -")
    t = BAD_CHARS.sub("", t)
    t = re.sub(r"\s+", " ", t).strip().rstrip(".")
    return t


def quality(height) -> str:
    if not height:
        return ""
    for h, label in ((1800, "2160p"), (1000, "1080p"), (700, "720p"), (540, "576p"), (440, "480p")):
        if height >= h:
            return f" - {label}"
    return ""


def target_for(cfg, f, title, year) -> str | None:
    if not title:
        return None
    layout = cfg.layout if year else cfg.layout.replace(" ({year})", "").replace("({year})", "")
    rel = layout.format(title=clean(title), year=year or "", quality=quality(f["height"]))
    ext = os.path.splitext(f["path"])[1].lower()
    return os.path.join(f["root"], cfg.movies_dir, rel + ext)


def score(con, cfg, f, p) -> tuple[float, list[str], dict]:
    conf = p["confidence"] or 0.0
    notes = [p["reasons"]] if p["reasons"] else []
    if p["source"] == "manual":
        return 1.0, ["identified manually"], {}
    im = imdb.check(con, p["title"], p["year"], f["duration"])
    if im.get("match") is True:
        if im.get("runtime_ok") is False:
            conf = min(conf, 0.5)
            notes.append("IMDb runtime does not match file duration")
        elif im.get("ambiguous"):
            notes.append("several IMDb titles match")
        else:
            conf = max(conf, 0.92) if p["source"] == "llm" else min(0.99, conf + 0.03)
            notes.append(f"IMDb confirmed {im['tconst']}")
    elif im.get("match") is False:
        conf -= 0.2
        notes.append(im.get("note", "IMDb mismatch"))
    return round(max(0.0, conf), 2), notes, im


def make_plan(con, cfg, note="", log=print) -> dict:
    prev = con.execute("SELECT id FROM plans WHERE superseded=0 ORDER BY id DESC LIMIT 1").fetchone()
    carried = {}
    if prev:
        for r in con.execute("""SELECT file_id, dst, status FROM proposals
                                WHERE plan_id=? AND status IN ('approved','skipped')""",
                             (prev["id"],)):
            carried[(r["file_id"], r["dst"])] = r["status"]
    con.execute("UPDATE plans SET superseded=1 WHERE superseded=0")
    plan_id = con.execute("INSERT INTO plans (created_at, note) VALUES (?,?)",
                          (now(), note)).lastrowid

    rows = con.execute("""SELECT f.*, p.title, p.year, p.kind, p.source, p.confidence,
                                 p.reasons, p.file_id
                          FROM files f JOIN parses p ON p.file_id=f.id
                          WHERE f.missing=0 ORDER BY f.path""").fetchall()
    claimed, counts = {}, {}
    for r in rows:
        dst = target_for(cfg, r, r["title"], r["year"])
        conf, notes, im = score(con, cfg, r, r)
        if im.get("tconst"):
            con.execute("UPDATE parses SET imdb_id=? WHERE file_id=?", (im["tconst"], r["id"]))
        if r["kind"] in ("sample", "extra", "episode"):
            status, notes = "ignored", notes + [f"kind={r['kind']}"]
        elif not dst:
            status, notes = "review", notes + ["needs a title"]
        elif dst == r["path"]:
            status = "noop"
        elif os.path.lexists(dst):
            status, notes = "conflict", notes + ["a file already exists at the target"]
        elif dst in claimed:
            status, notes = "conflict", notes + [f"same target as file #{claimed[dst]}"]
        elif (r["id"], dst) in carried:
            status = carried[(r["id"], dst)]
            notes.append("decision carried over from previous plan")
        elif r["source"] == "manual":
            status = "approved"
        elif conf >= cfg.auto_approve:
            status = "auto"
        else:
            status = "review"
        if dst and status not in ("ignored", "noop"):
            claimed.setdefault(dst, r["id"])
        con.execute("""INSERT INTO proposals (plan_id, file_id, src, dst, confidence, status, reason)
                       VALUES (?,?,?,?,?,?,?)""",
                    (plan_id, r["id"], r["path"], dst, conf, status, "; ".join(n for n in notes if n)))
        counts[status] = counts.get(status, 0) + 1
    con.commit()
    log(f"plan #{plan_id}: " + ", ".join(f"{k} {v}" for k, v in sorted(counts.items())))
    return {"plan_id": plan_id, "counts": counts}


def current_plan(con):
    r = con.execute("SELECT id FROM plans WHERE superseded=0 ORDER BY id DESC LIMIT 1").fetchone()
    return r["id"] if r else None
