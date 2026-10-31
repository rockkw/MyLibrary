"""Step 2: deterministic name parsing with guessit, plus a confidence score.

Anything below the auto-approve threshold is flagged for the LLM step.
Manual (human) identifications are never overwritten.
"""

import json
import os
import re

from guessit import guessit

from .db import now

GENERIC = {"movie", "video", "film", "final", "cut", "untitled", "new", "copy",
           "output", "export", "clip", "vts", "title", "main", "feature", "disc"}


def _guess(text: str) -> dict:
    try:
        g = dict(guessit(text, {"type": "movie"}))
    except Exception:  # guessit can choke on odd strings
        return {}
    out = {}
    for k, v in g.items():
        out[k] = v if isinstance(v, (str, int, float, bool, type(None))) else str(v)
    return out


def identify(path: str, duration: float | None, cfg) -> dict:
    """Return {title, year, kind, confidence, flagged, reasons, guessit}."""
    name = os.path.basename(path)
    parent = os.path.basename(os.path.dirname(path))
    g_file = _guess(name)
    g_dir = _guess(parent) if parent else {}
    reasons = []

    g = g_file
    # A clean parent folder ("Alien (1979)") often beats a messy filename.
    if not g_file.get("year") and g_dir.get("year") and g_dir.get("title"):
        g = {**g_file, "title": g_dir["title"], "year": g_dir["year"]}
        reasons.append("title/year taken from folder name")

    title = (g.get("title") or "").strip()
    year = g.get("year") if isinstance(g.get("year"), int) else None
    kind = "movie"
    conf = 0.95

    lower = name.lower()
    if any(re.search(rf"\b{re.escape(w)}\b", lower) for w in cfg.ignore_words):
        kind, reasons = "sample", reasons + ["matches ignore word"]
    if g_file.get("episode") or g_file.get("season"):
        kind, conf = "episode", conf - 0.3
        reasons.append("looks like a TV episode")
    if duration and duration < cfg.min_duration_min * 60 and kind == "movie":
        kind, conf = "extra", conf - 0.3
        reasons.append(f"short ({duration/60:.0f} min)")
    if not year:
        conf -= 0.35
        reasons.append("no year")
    words = re.findall(r"[a-z]+", title.lower())
    if not title or len(title) < 2 or title.isdigit():
        conf -= 0.5
        reasons.append("no usable title")
    elif words and all(w in GENERIC for w in words):
        conf -= 0.5
        reasons.append("generic title")
    if g.get("other") and "Sample" in str(g.get("other")):
        kind = "sample"
    if year and not (1888 <= year <= 2100):
        conf -= 0.4
        reasons.append("implausible year")

    conf = max(0.0, min(conf, 0.99))
    return {
        "title": title or None,
        "year": year,
        "kind": kind,
        "confidence": round(conf, 2),
        "flagged": int(conf < cfg.auto_approve and kind in ("movie", "episode")),
        "reasons": "; ".join(reasons),
        "guessit": g_file,
    }


def parse_all(con, cfg, redo=False, log=print) -> dict:
    rows = con.execute("""
        SELECT f.id, f.path, f.duration, p.source, p.updated_at, f.scanned_at
        FROM files f LEFT JOIN parses p ON p.file_id = f.id
        WHERE f.missing = 0""").fetchall()
    stats = {"parsed": 0, "flagged": 0, "kept_manual": 0, "kept_llm": 0}
    for r in rows:
        if r["source"] == "manual":
            stats["kept_manual"] += 1
            continue
        if r["source"] == "llm" and not redo and (r["updated_at"] or "") >= (r["scanned_at"] or ""):
            stats["kept_llm"] += 1
            continue
        if r["source"] == "guessit" and not redo and (r["updated_at"] or "") >= (r["scanned_at"] or ""):
            continue
        info = identify(r["path"], r["duration"], cfg)
        con.execute("""
            INSERT INTO parses (file_id, title, year, kind, source, confidence, flagged,
                                reasons, guessit_json, llm_json, updated_at)
            VALUES (?,?,?,?, 'guessit', ?,?,?,?, NULL, ?)
            ON CONFLICT(file_id) DO UPDATE SET title=excluded.title, year=excluded.year,
                kind=excluded.kind, source='guessit', confidence=excluded.confidence,
                flagged=excluded.flagged, reasons=excluded.reasons,
                guessit_json=excluded.guessit_json, llm_json=NULL, imdb_id=NULL,
                updated_at=excluded.updated_at
        """, (r["id"], info["title"], info["year"], info["kind"], info["confidence"],
              info["flagged"], info["reasons"], json.dumps(info["guessit"]), now()))
        stats["parsed"] += 1
        stats["flagged"] += info["flagged"]
    con.commit()
    log(f"parsed {stats['parsed']} files, {stats['flagged']} flagged for the LLM")
    return stats
