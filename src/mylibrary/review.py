"""Human decisions. Shared by CLI and UI. Writes to the DB only, never to disk."""

import json
import os

from .db import now
from .plan import target_for

DECIDABLE = ("auto", "review", "approved", "skipped", "conflict")


def decide(con, proposal_ids, action: str) -> int:
    new = {"approve": "approved", "skip": "skipped"}.get(action)
    if action == "reset":
        n = 0
        for pid in proposal_ids:
            r = con.execute("SELECT confidence FROM proposals WHERE id=?", (pid,)).fetchone()
            if r:
                n += con.execute("UPDATE proposals SET status='review', decided_at=? "
                                 "WHERE id=? AND status IN ('approved','skipped')",
                                 (now(), pid)).rowcount
        con.commit()
        return n
    if not new:
        raise ValueError("action must be approve, skip or reset")
    n = 0
    for pid in proposal_ids:
        # conflicts can be skipped but not approved until fixed
        allowed = DECIDABLE if new == "skipped" else ("auto", "review", "skipped", "approved")
        n += con.execute(
            f"UPDATE proposals SET status=?, decided_at=? WHERE id=? AND status IN "
            f"({','.join('?' * len(allowed))})", (new, now(), pid, *allowed)).rowcount
    con.commit()
    return n


def edit(con, cfg, proposal_id: int, title: str, year: int | None,
         edition: str | None = None) -> dict:
    """Set a manual identification and recompute the target. Approves if no conflict.

    edition=None keeps the current edition; "" clears it.
    """
    p = con.execute("SELECT * FROM proposals WHERE id=?", (proposal_id,)).fetchone()
    if not p:
        raise ValueError(f"no proposal #{proposal_id}")
    if p["status"] in ("applied",):
        raise ValueError("already applied")
    f = con.execute("SELECT * FROM files WHERE id=?", (p["file_id"],)).fetchone()
    title = (title or "").strip()
    if edition is None:
        cur = con.execute("SELECT edition FROM parses WHERE file_id=?", (f["id"],)).fetchone()
        edition = cur["edition"] if cur else None
    edition = (edition or "").strip() or None
    con.execute("""UPDATE parses SET title=?, year=?, edition=?, kind='movie', source='manual',
                   confidence=1.0, flagged=0, updated_at=? WHERE file_id=?""",
                (title, year, edition, now(), f["id"]))
    dst = target_for(cfg, f, title, year, edition)
    clash = con.execute("""SELECT id FROM proposals WHERE plan_id=? AND dst=? AND id<>?
                           AND status NOT IN ('ignored','noop','skipped')""",
                        (p["plan_id"], dst, proposal_id)).fetchone()
    if not dst:
        status, reason = "review", "needs a title"
    elif dst == f["path"]:
        status, reason = "noop", "already in place"
    elif os.path.lexists(dst):
        status, reason = "conflict", "a file already exists at the target"
    elif clash:
        status, reason = "conflict", f"same target as proposal #{clash['id']}"
    else:
        status, reason = "approved", "identified manually"
    con.execute("""UPDATE proposals SET dst=?, status=?, confidence=1.0, reason=?, decided_at=?
                   WHERE id=?""", (dst, status, reason, now(), proposal_id))
    con.commit()
    return {"id": proposal_id, "dst": dst, "status": status, "reason": reason}


def list_proposals(con, plan_id, status=None, q=None, root=None, limit=500, offset=0):
    sql = """SELECT pr.*, f.size, f.duration, f.width, f.height, f.vcodec, f.acodec,
                    f.audio_langs, f.sub_langs, f.root, p.title, p.year, p.edition, p.kind, p.source,
                    p.imdb_id
             FROM proposals pr JOIN files f ON f.id=pr.file_id
             LEFT JOIN parses p ON p.file_id=f.id WHERE pr.plan_id=?"""
    args = [plan_id]
    if status:
        statuses = status.split(",")
        sql += f" AND pr.status IN ({','.join('?' * len(statuses))})"
        args += statuses
    if root:
        sql += " AND f.root=?"
        args.append(root)
    if q:
        sql += " AND (pr.src LIKE ? OR p.title LIKE ?)"
        args += [f"%{q}%", f"%{q}%"]
    sql += " ORDER BY pr.confidence ASC, pr.src LIMIT ? OFFSET ?"
    args += [limit, offset]
    return [dict(r) for r in con.execute(sql, args)]


def detail(con, proposal_id):
    r = con.execute("""SELECT pr.*, f.*, p.title, p.year, p.edition, p.kind, p.source,
                              p.confidence AS p_conf,
                              p.reasons AS p_reasons, p.guessit_json, p.llm_json, p.imdb_id,
                              pr.id AS proposal_id, pr.status AS status, pr.reason AS reason,
                              pr.confidence AS confidence
                       FROM proposals pr JOIN files f ON f.id=pr.file_id
                       LEFT JOIN parses p ON p.file_id=f.id WHERE pr.id=?""",
                    (proposal_id,)).fetchone()
    if not r:
        return None
    d = dict(r)
    for k in ("guessit_json", "llm_json"):
        d[k] = json.loads(d[k]) if d.get(k) else None
    return d


def summary(con, plan_id):
    out = {"plan_id": plan_id, "counts": {}, "roots": []}
    if plan_id:
        for r in con.execute("SELECT status, COUNT(*) n FROM proposals WHERE plan_id=? "
                             "GROUP BY status", (plan_id,)):
            out["counts"][r["status"]] = r["n"]
    out["files"] = con.execute("SELECT COUNT(*) FROM files WHERE missing=0").fetchone()[0]
    out["roots"] = [dict(r) for r in con.execute(
        "SELECT root, COUNT(*) n FROM files WHERE missing=0 GROUP BY root")]
    return out
