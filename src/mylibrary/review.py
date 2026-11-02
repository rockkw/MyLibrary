"""Human decisions. Shared by CLI and UI. Writes to the DB only, never to disk."""

import json
import os
import re
from datetime import date

from collections import Counter

from .classify import scene_title
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
        # Conflicts can be skipped but not approved until fixed, and an item with no target
        # has nowhere to go, so approving it would only hide it from apply.
        allowed = DECIDABLE if new == "skipped" else ("auto", "review", "skipped", "approved")
        target = "" if new == "skipped" else " AND dst IS NOT NULL"
        n += con.execute(
            f"UPDATE proposals SET status=?, decided_at=? WHERE id=? AND status IN "
            f"({','.join('?' * len(allowed))}){target}", (new, now(), pid, *allowed)).rowcount
    con.commit()
    return n


def edit(con, cfg, proposal_id: int, title: str, year: int | None,
         edition: str | None = None, kind: str | None = None, season: int | None = None,
         episode: int | None = None, episode_last: int | None = None,
         episode_title: str | None = None, artist: str | None = None,
         album: str | None = None, track: int | None = None, disc: int | None = None,
         site: str | None = None, scene_date: str | None = None,
         performers: str | None = None) -> dict:
    """Set a manual identification and recompute the target. Approves if no conflict.

    edition=None keeps the current edition; "" clears it. episode_title works the same way.
    kind is movie, episode, music, software or scene. None infers it from the fields given, then
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
    kept_title = title is None
    title = (title or "").strip()
    if kind not in (None, "movie", "episode", "music", "software", "scene"):
        raise ValueError("kind must be movie, episode, music, software or scene")
    if scene_date:
        try:
            date.fromisoformat(scene_date)
        except ValueError:
            raise ValueError("date must be YYYY-MM-DD") from None
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", scene_date):
            raise ValueError("date must be YYYY-MM-DD")
    if any(n is not None and n < 0 for n in (season, episode, episode_last, track, disc)):
        raise ValueError("season, episode, track and disc cannot be negative")
    cur = con.execute("SELECT * FROM parses WHERE file_id=?", (f["id"],)).fetchone()
    was = dict(cur) if cur else {}
    if kind is None:
        if any(v is not None for v in (season, episode)):
            kind = "episode"
        elif any(v is not None for v in (artist, album, track, disc)):
            kind = "music"
        elif any(v is not None for v in (site, scene_date, performers)):
            kind = "scene"
        else:
            kind = was.get("kind") if was.get("kind") in ("episode", "music", "software", "scene") \
                else "movie"
    kept = lambda value, key: value if value is not None else was.get(key)
    if edition is None:
        edition = was.get("edition")
    edition = (edition or "").strip() or None
    ident = dict.fromkeys(("season", "episode", "episode_last", "episode_title",
                           "artist", "album", "track", "disc",
                           "site", "scene_date", "performers"))
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
    elif kind == "scene":
        edition = None
        ident.update(site=(kept(site, "site") or "").strip() or None,
                     scene_date=(kept(scene_date, "scene_date") or "").strip() or None,
                     performers=(kept(performers, "performers") or "").strip() or None)
        year = int(ident["scene_date"][:4]) if ident["scene_date"] else None
        title = (was.get("title") or "") if kept_title else title
    ident.update(title=title, year=year, edition=edition, kind=kind)
    con.execute("""UPDATE parses SET title=:title, year=:year, edition=:edition, kind=:kind,
                   season=:season, episode=:episode, episode_last=:episode_last,
                   episode_title=:episode_title, artist=:artist, album=:album, track=:track,
                   disc=:disc, site=:site, scene_date=:scene_date, performers=:performers,
                   source='manual', confidence=1.0, flagged=0, updated_at=:now
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


# Shared by a whole selection. Episode, last episode, episode title and track stay per file.
BULK_FIELDS = ("kind", "title", "year", "edition", "season", "artist", "album", "disc",
               "site", "scene_date", "performers")
KINDS = ("movie", "episode", "music", "software", "scene")


def edit_many(con, cfg, proposal_ids, fields: dict) -> dict:
    """Give several items the same identification, each keeping its own value for every field
    left empty, and recompute each target with the single-edit rules.

    Items that would share a target are left in conflict, none of them approved.
    """
    unknown = set(fields) - set(BULK_FIELDS)
    if unknown:
        raise ValueError(f"cannot set {', '.join(sorted(unknown))} for several files at once")
    fields = {k: v.strip() if isinstance(v, str) else v for k, v in fields.items()}
    fields = {k: v for k, v in fields.items() if v not in (None, "")}
    if not fields:
        raise ValueError("fill in at least one field")
    if fields.get("kind") not in (None, *KINDS):
        raise ValueError("kind must be movie, episode, music, software or scene")
    results = []
    for pid in dict.fromkeys(proposal_ids):
        row = con.execute("""SELECT f.path, p.* FROM proposals pr JOIN files f ON f.id=pr.file_id
                             LEFT JOIN parses p ON p.file_id=f.id WHERE pr.id=?""", (pid,)).fetchone()
        cur = dict(row) if row else {}
        given = lambda key: fields.get(key, cur.get(key))
        kind = fields.get("kind") or (cur.get("kind") if cur.get("kind") in KINDS else None)
        title = fields.get("title")
        if title is None:
            title = cur.get("title")
            if kind == "scene" and cur:  # "Series _ Scene _ Site (15)" keeps its own words
                title = scene_title(cur["path"], given("site"), title)
        try:
            results.append(edit(con, cfg, pid, title, given("year"), given("edition"), kind,
                                given("season"), cur.get("episode"), cur.get("episode_last"),
                                cur.get("episode_title"), given("artist"), given("album"),
                                cur.get("track"), given("disc"), given("site"),
                                given("scene_date"), given("performers")))
        except ValueError as e:
            results.append({"id": pid, "dst": None, "status": "error", "reason": str(e)})
    by_target = {}
    for r in results:
        if r["dst"]:
            by_target.setdefault(r["dst"], []).append(r)
    for same in by_target.values():
        for r in same if len(same) > 1 else ():
            others = ", ".join(f"#{o['id']}" for o in same if o is not r)
            r.update(status="conflict", reason=f"same target as selected {others}")
            con.execute("UPDATE proposals SET status='conflict', reason=? WHERE id=?",
                        (r["reason"], r["id"]))
    con.commit()
    return {"results": results, "counts": dict(Counter(r["status"] for r in results))}


def list_proposals(con, cfg, plan_id, status=None, q=None, root=None, limit=500, offset=0):
    sql = """SELECT pr.*, f.size, f.duration, f.width, f.height, f.vcodec, f.acodec,
                    f.audio_langs, f.sub_langs, f.root, p.title, p.year, p.edition, p.kind, p.source,
                    p.imdb_id, p.season, p.episode, p.episode_last, p.episode_title,
                    p.artist, p.album, p.track, p.disc, p.site, p.scene_date, p.performers
             FROM proposals pr JOIN files f ON f.id=pr.file_id
             LEFT JOIN parses p ON p.file_id=f.id WHERE pr.plan_id=?"""
    in_roots, names = cfg.root_filter()
    sql += f" AND {in_roots}"
    args = [plan_id] + names
    if status:
        statuses = status.split(",")
        sql += f" AND pr.status IN ({','.join('?' * len(statuses))})"
        args += statuses
    if root:
        sql += " AND f.root=?"
        args.append(root)
    if q:
        sql += " AND (pr.src LIKE ? OR p.title LIKE ? OR p.performers LIKE ?)"
        args += [f"%{q}%", f"%{q}%", f"%{q}%"]
    sql += " ORDER BY pr.confidence ASC, pr.src LIMIT ? OFFSET ?"
    args += [limit, offset]
    return [dict(r) for r in con.execute(sql, args)]


def detail(con, proposal_id):
    r = con.execute("""SELECT pr.*, f.*, p.title, p.year, p.edition, p.kind, p.source,
                              p.season, p.episode, p.episode_last, p.episode_title,
                              p.artist, p.album, p.track, p.disc,
                              p.site, p.scene_date, p.performers,
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


def summary(con, cfg, plan_id):
    out = {"plan_id": plan_id, "counts": {}, "roots": []}
    in_roots, names = cfg.root_filter()
    if plan_id:
        for r in con.execute(f"""SELECT pr.status, COUNT(*) n FROM proposals pr
                                 JOIN files f ON f.id=pr.file_id
                                 WHERE pr.plan_id=? AND {in_roots} GROUP BY pr.status""",
                             [plan_id] + names):
            out["counts"][r["status"]] = r["n"]
    out["roots"] = [dict(r) for r in con.execute(
        f"SELECT f.root, COUNT(*) n FROM files f WHERE f.missing=0 AND {in_roots} GROUP BY f.root",
        names)]
    out["files"] = sum(r["n"] for r in out["roots"])
    return out
