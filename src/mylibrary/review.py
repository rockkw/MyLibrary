"""Human decisions. Shared by CLI and UI. Writes to the DB only, never to disk."""

import json
import os

from .db import now
from .plan import destination, needs

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
         edition: str | None = None, kind: str | None = None, season: int | None = None,
         episode: int | None = None, episode_last: int | None = None,
         episode_title: str | None = None, artist: str | None = None,
         album: str | None = None, track: int | None = None, disc: int | None = None) -> dict:
    """Set a manual identification and recompute the target. Approves if no conflict.

    edition=None keeps the current edition; "" clears it. episode_title works the same way.
    kind is movie, episode, music or software. None infers it from the fields given, then
    keeps the file's current kind, then falls back to movie. For an episode, title is the
    series name; for music, the track title. Other fields keep their current value when
    None, a new episode clears episode_last, and "" clears artist or album.
    """
    p = con.execute("SELECT * FROM proposals WHERE id=?", (proposal_id,)).fetchone()
    if not p:
        raise ValueError(f"no proposal #{proposal_id}")
    if p["status"] in ("applied",):
        raise ValueError("already applied")
    f = con.execute("SELECT * FROM files WHERE id=?", (p["file_id"],)).fetchone()
    title = (title or "").strip()
    if kind not in (None, "movie", "episode", "music", "software"):
        raise ValueError("kind must be movie, episode, music or software")
    if any(n is not None and n < 0 for n in (season, episode, episode_last, track, disc)):
        raise ValueError("season, episode, track and disc cannot be negative")
    cur = con.execute("SELECT * FROM parses WHERE file_id=?", (f["id"],)).fetchone()
    was = dict(cur) if cur else {}
    if kind is None:
        if any(v is not None for v in (season, episode)):
            kind = "episode"
        elif any(v is not None for v in (artist, album, track, disc)):
            kind = "music"
        else:
            kind = was.get("kind") if was.get("kind") in ("episode", "music", "software") \
                else "movie"
    kept = lambda value, key: value if value is not None else was.get(key)
    if edition is None:
        edition = was.get("edition")
    edition = (edition or "").strip() or None
    ident = dict.fromkeys(("season", "episode", "episode_last", "episode_title",
                           "artist", "album", "track", "disc"))
    if kind == "episode":
        edition = None
        ident.update(season=kept(season, "season"), episode=kept(episode, "episode"),
                     episode_last=episode_last if episode is not None or episode_last is not None
                     else was.get("episode_last"),
                     episode_title=(kept(episode_title, "episode_title") or "").strip() or None)
    elif kind == "music":
        edition = None
        ident.update(artist=(kept(artist, "artist") or "").strip() or None,
                     album=(kept(album, "album") or "").strip() or None,
                     track=kept(track, "track"), disc=kept(disc, "disc"))
    elif kind == "software":
        edition = None
    ident.update(title=title, year=year, edition=edition, kind=kind)
    con.execute("""UPDATE parses SET title=:title, year=:year, edition=:edition, kind=:kind,
                   season=:season, episode=:episode, episode_last=:episode_last,
                   episode_title=:episode_title, artist=:artist, album=:album, track=:track,
                   disc=:disc, source='manual', confidence=1.0, flagged=0, updated_at=:now
                   WHERE file_id=:file_id""", {**ident, "now": now(), "file_id": f["id"]})
    dst = destination(cfg, f, ident)
    clash = con.execute("""SELECT id FROM proposals WHERE plan_id=? AND dst=? AND id<>?
                           AND status NOT IN ('ignored','noop','skipped')""",
                        (p["plan_id"], dst, proposal_id)).fetchone()
    if not dst:
        status, reason = "review", needs(kind)
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
                    p.imdb_id, p.season, p.episode, p.episode_last, p.episode_title,
                    p.artist, p.album, p.track, p.disc
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
                              p.season, p.episode, p.episode_last, p.episode_title,
                              p.artist, p.album, p.track, p.disc,
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
