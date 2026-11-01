"""Step 4: turn identifications into a reviewable rename plan. Touches only the DB."""

import json
import os
import re
import time

from . import imdb
from .db import now

BAD_CHARS = re.compile(r'[<>"/\\|?*\x00-\x1f]')


NAME_BYTES = 120


def fit(text: str) -> str:
    """Trim a tag to a safe path-component length, in bytes, on a character boundary."""
    return text.encode()[:NAME_BYTES].decode(errors="ignore").strip()


def clean(title: str) -> str:
    t = title.replace(":", " -")
    t = BAD_CHARS.sub("", t)
    t = re.sub(r"\s+", " ", t).strip().rstrip(".")
    return t


def quality(height, width=None) -> str:
    """Resolution label. Width counts too: a cropped 1920x804 film is 1080p, not 720p."""
    if not height:
        return ""
    width = width or 0
    for h, w, label in ((1800, 3200, "2160p"), (1000, 1800, "1080p"), (700, 1200, "720p"),
                        (540, 10**6, "576p"), (440, 10**6, "480p")):
        if height >= h or width >= w:
            return f" - {label}"
    return ""


def target_for(cfg, f, title, year, edition=None) -> str | None:
    if not title:
        return None
    layout = cfg.layout if year else cfg.layout.replace(" ({year})", "").replace("({year})", "")
    edition = clean(edition or "")
    rel = layout.format(title=clean(title), year=year or "", quality=quality(f["height"], f["width"]),
                        edition=f" - {edition}" if edition else "")
    ext = os.path.splitext(f["path"])[1].lower()
    return os.path.join(f["root"], cfg.movies_dir, rel + ext)


def episode_target(cfg, f, p) -> str | None:
    """Jellyfin TV path. p needs title, year, season, episode, episode_last, episode_title."""
    if not p["title"] or p["season"] is None or p["episode"] is None:
        return None
    episode = f"{p['episode']:02d}"
    if p["episode_last"] and p["episode_last"] != p["episode"]:
        episode += f"-E{p['episode_last']:02d}"
    ep_title = clean(p["episode_title"] or "")[:100].strip()
    rel = cfg.tv_layout.format(title=clean(p["title"]),
                               year=f" ({p['year']})" if p["year"] else "",
                               season=f"{p['season']:02d}", episode=episode,
                               episode_title=f" - {ep_title}" if ep_title else "",
                               quality=quality(f["height"], f["width"]))
    ext = os.path.splitext(f["path"])[1].lower()
    return os.path.join(f["root"], cfg.tv_dir, rel + ext)


def music_target(cfg, f, p) -> str | None:
    """Music/Artist/Album/01 - Title.ext. Tags are untrusted, so every part is cleaned."""
    artist, album, title = (clean(fit(p[k] or "")) for k in ("artist", "album", "title"))
    if not (artist and album and title):
        return None
    track = f"{p['track']:02d} - " if p["track"] else ""
    if track and p["disc"]:
        track = f"{p['disc']}-{track}"
    rel = cfg.music_layout.format(artist=artist, album=album, title=title, track=track,
                                  year=f" ({p['year']})" if p["year"] else "")
    ext = os.path.splitext(f["path"])[1].lower()
    return os.path.join(f["root"], cfg.music_dir, rel + ext)


def software_target(cfg, f, p) -> str | None:
    """Software/Title/ plus the original file name, which installers may depend on."""
    title = clean(fit(p["title"] or ""))
    if not title:
        return None
    rel = cfg.software_layout.format(title=title, filename=os.path.basename(f["path"]))
    return os.path.join(f["root"], cfg.software_dir, rel)


def destination(cfg, f, p) -> str | None:
    """Target path for an identification. p is a parses row or a dict with the same keys."""
    if p["kind"] == "episode":
        return episode_target(cfg, f, p)
    if p["kind"] == "music":
        return music_target(cfg, f, p)
    if p["kind"] == "software":
        return software_target(cfg, f, p)
    return target_for(cfg, f, p["title"], p["year"], p["edition"])


def score(con, cfg, f, p) -> tuple[float, list[str], dict]:
    conf = p["confidence"] or 0.0
    notes = [p["reasons"]] if p["reasons"] else []
    if p["source"] == "manual":
        return 1.0, ["identified manually"], {}
    if p["kind"] in ("episode", "music", "software"):  # IMDb only knows about films
        return round(conf, 2), notes, {}
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


def needs(kind) -> str:
    return {"episode": "needs series, season and episode",
            "music": "needs artist, album and title"}.get(kind, "needs a title")


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

    rows = con.execute("""SELECT f.*, p.title, p.year, p.edition, p.kind, p.source, p.confidence,
                                 p.reasons, p.file_id, p.season, p.episode,
                                 p.episode_last, p.episode_title, p.artist, p.album,
                                 p.track, p.disc
                          FROM files f JOIN parses p ON p.file_id=f.id
                          WHERE f.missing=0 ORDER BY f.path""").fetchall()
    claimed, counts = {}, {}
    for r in rows:
        dst = destination(cfg, r, r)
        conf, notes, im = score(con, cfg, r, r)
        if im.get("tconst"):
            con.execute("UPDATE parses SET imdb_id=? WHERE file_id=?", (im["tconst"], r["id"]))
        if r["kind"] in ("sample", "extra"):
            status, notes = "ignored", notes + [f"kind={r['kind']}"]
        elif not dst:
            status, notes = "review", notes + [needs(r["kind"])]
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
        elif time.time() - (r["mtime"] or 0) < cfg.settle_minutes * 60:
            status, notes = "review", notes + ["changed recently: may still be downloading"]
        elif r["kind"] == "software" and r["source"] == "llm":
            status, notes = "review", notes + ["software title suggested by the LLM"]
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
