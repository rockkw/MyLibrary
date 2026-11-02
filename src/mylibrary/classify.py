"""Step 3: ask a local Ollama model about flagged files only.

The model receives text and returns JSON. It has no tools and no file access;
its answer is just a suggestion stored in the database. LLM confidence is
capped (llm_max_confidence) so an LLM-only answer is never auto-approved
unless the offline IMDb check confirms it later.
"""

import json
import os
import re
import urllib.error
import urllib.request

from datetime import date, timedelta

from .db import now

SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": ["string", "null"]},
        "year": {"type": ["integer", "null"]},
        "kind": {"type": "string",
                 "enum": ["movie", "episode", "extra", "sample", "scene", "unknown"]},
        "site": {"type": ["string", "null"]},
        "scene_date": {"type": ["string", "null"]},
        "performers": {"type": ["string", "null"]},
        "confidence": {"type": "number"},
        "notes": {"type": "string"},
    },
    "required": ["title", "year", "kind", "confidence"],
}

SYSTEM = (
    "You identify movies from messy file names for a home media library. "
    "Reply with JSON only. Use the film's original release title in its usual "
    "English or original-language form, and its release year. If you are not "
    "sure, lower the confidence (0.0-1.0) instead of guessing; use null for an "
    "unknown year. kind is 'extra' for featurettes/bonus content, 'sample' for "
    "samples/trailers, 'episode' for TV, 'unknown' if you cannot tell. "
    "kind is 'scene' for an adult (porn) scene. For a scene also give site (the studio, site "
    "or brand named in the file name), scene_date as YYYY-MM-DD only when a full date is in "
    "the name, performers as a comma-separated list of people's names only, and title as "
    "the scene title without the site, date or performers. Use JSON null, never the word null, "
    "for anything the name does not show. For any other kind, site, scene_date and performers "
    "are null."
)


SOFTWARE_SYSTEM = (
    "You identify software products from messy file names. Reply with JSON only. "
    "title is the product name without version, architecture or installer words. "
    "If the name is random or you are not sure, use null for the title and lower the "
    "confidence (0.0-1.0) instead of guessing. year is null. kind is 'unknown'."
)


NOTHING = {"null", "none", "n/a", "na", "unknown", "-"}


def _text(v) -> str | None:
    """A real answer, or None. Models sometimes write null as the word "null"."""
    v = v.strip() if isinstance(v, str) else ""
    return None if v.casefold() in NOTHING else v or None


def _iso_date(v) -> str | None:
    """A real, past YYYY-MM-DD date, or None. The model may make one up from a year."""
    if not (isinstance(v, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", v.strip())):
        return None
    try:
        when = date.fromisoformat(v.strip())
    except ValueError:
        return None
    return when.isoformat() if when <= date.today() + timedelta(days=1) else None


def scene_title(path: str, site: str | None, title: str | None) -> str | None:
    """Build a scene title from a name ending in its site, such as "Series _ Scene _ Site (15)".

    Web downloads name files after the page title, and the model drops the words that tell one
    file from the next, so they would collide. The name has them all; the site comes from the model.
    """
    parts = os.path.splitext(os.path.basename(path))[0].split(" _ ")
    if not site or len(parts) < 2 or not parts[-1].casefold().startswith(site.casefold()):
        return title
    rest = parts[-1][len(site):].strip()
    return " - ".join(p.strip() for p in parts[:-1] if p.strip()) + (f" {rest}" if rest else "")


def build_prompt(path: str, duration, guess: dict) -> str:
    parts = os.path.normpath(path).split(os.sep)
    return json.dumps({
        "filename": parts[-1],
        "parent_folders": parts[-3:-1],
        "duration_minutes": round(duration / 60) if duration else None,
        "parser_guess": {k: guess.get(k) for k in ("title", "year", "type",
                                                    "alternative_title", "language")
                         if guess.get(k) is not None},
    }, ensure_ascii=False)


def ask(cfg, prompt: str, opener=urllib.request.urlopen, system: str = SYSTEM) -> dict:
    body = json.dumps({
        "model": cfg.ollama_model,
        "stream": False,
        "format": SCHEMA,
        "options": {"temperature": 0},
        "messages": [{"role": "system", "content": system},
                     {"role": "user", "content": prompt}],
    }).encode()
    req = urllib.request.Request(f"{cfg.ollama_url}/api/chat", data=body,
                                 headers={"Content-Type": "application/json"})
    with opener(req, timeout=cfg.ollama_timeout) as resp:
        data = json.loads(resp.read())
    content = data.get("message", {}).get("content", "{}")
    out = json.loads(content)
    if not isinstance(out, dict):
        raise ValueError("model did not return a JSON object")
    return out


def classify_all(con, cfg, limit=None, redo=False, log=print, opener=None) -> dict:
    stats = {"asked": 0, "errors": 0, "skipped": 0}
    if not cfg.ollama_enabled:
        log("Ollama disabled in config.ini; skipping")
        return stats
    where = "p.flagged=1 AND p.source='guessit'" if not redo else \
            "p.flagged=1 AND p.source IN ('guessit','llm')"
    in_roots, names = cfg.root_filter()
    sql = f"""SELECT f.id, f.path, f.duration, p.guessit_json, p.kind FROM parses p
              JOIN files f ON f.id=p.file_id WHERE f.missing=0 AND {in_roots} AND {where}
              ORDER BY f.id"""
    rows = con.execute(sql + (f" LIMIT {int(limit)}" if limit else ""), names).fetchall()
    log(f"{len(rows)} flagged files to send to {cfg.ollama_model}")
    for i, r in enumerate(rows, 1):
        prompt = build_prompt(r["path"], r["duration"], json.loads(r["guessit_json"] or "{}"))
        try:
            ans = ask(cfg, prompt, **({"opener": opener} if opener else {}),
                      system=SOFTWARE_SYSTEM if r["kind"] == "software" else SYSTEM)
        except (urllib.error.URLError, OSError, ValueError, json.JSONDecodeError) as e:
            stats["errors"] += 1
            log(f"  ! {os.path.basename(r['path'])}: {e}")
            if stats["errors"] >= 5 and stats["asked"] == 0:
                log("Ollama keeps failing (is `ollama serve` running?); stopping")
                break
            continue
        try:
            year = int(ans["year"]) if ans.get("year") is not None else None
        except (TypeError, ValueError):
            year = None
        conf = max(0.0, min(float(ans.get("confidence") or 0), cfg.llm_max_conf))
        kind = ans.get("kind") if ans.get("kind") in SCHEMA["properties"]["kind"]["enum"] \
            else "unknown"
        if kind == "unknown":  # "can't tell" should not erase what the parser saw
            kind = r["kind"] or "unknown"
        if r["kind"] == "software":  # the movie question does not apply
            kind, year = "software", None
        title = _text(ans.get("title"))
        scene = dict.fromkeys(("site", "scene_date", "performers"))
        if kind == "scene":  # suggestions only: plan sends every LLM scene to review
            scene = {"site": _text(ans.get("site")), "scene_date": _iso_date(ans.get("scene_date")),
                     "performers": _text(ans.get("performers"))}
            year = int(scene["scene_date"][:4]) if scene["scene_date"] else None
            title = scene_title(r["path"], scene["site"], title)
        con.execute("""UPDATE parses SET title=?, year=?, kind=?, site=?, scene_date=?,
                       performers=?, source='llm', confidence=?, flagged=?, llm_json=?,
                       updated_at=? WHERE file_id=?""",
                    (title, year, kind, scene["site"], scene["scene_date"], scene["performers"],
                     round(conf, 2), int(conf < cfg.auto_approve),
                     json.dumps(ans, ensure_ascii=False), now(), r["id"]))
        stats["asked"] += 1
        if i % 25 == 0:
            con.commit()
            log(f"  {i}/{len(rows)}")
    con.commit()
    log(f"LLM answered {stats['asked']}, errors {stats['errors']}")
    return stats
