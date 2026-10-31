"""Step 3: ask a local Ollama model about flagged files only.

The model receives text and returns JSON. It has no tools and no file access;
its answer is just a suggestion stored in the database. LLM confidence is
capped (llm_max_confidence) so an LLM-only answer is never auto-approved
unless the offline IMDb check confirms it later.
"""

import json
import os
import urllib.error
import urllib.request

from .db import now

SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": ["string", "null"]},
        "year": {"type": ["integer", "null"]},
        "kind": {"type": "string",
                 "enum": ["movie", "episode", "extra", "sample", "unknown"]},
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
    "samples/trailers, 'episode' for TV, 'unknown' if you cannot tell."
)


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


def ask(cfg, prompt: str, opener=urllib.request.urlopen) -> dict:
    body = json.dumps({
        "model": cfg.ollama_model,
        "stream": False,
        "format": SCHEMA,
        "options": {"temperature": 0},
        "messages": [{"role": "system", "content": SYSTEM},
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
    sql = f"""SELECT f.id, f.path, f.duration, p.guessit_json FROM parses p
              JOIN files f ON f.id=p.file_id WHERE f.missing=0 AND {where}
              ORDER BY f.id"""
    rows = con.execute(sql + (f" LIMIT {int(limit)}" if limit else "")).fetchall()
    log(f"{len(rows)} flagged files to send to {cfg.ollama_model}")
    for i, r in enumerate(rows, 1):
        prompt = build_prompt(r["path"], r["duration"], json.loads(r["guessit_json"] or "{}"))
        try:
            ans = ask(cfg, prompt, **({"opener": opener} if opener else {}))
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
        title = (ans.get("title") or "").strip() or None
        con.execute("""UPDATE parses SET title=?, year=?, kind=?, source='llm',
                       confidence=?, flagged=?, llm_json=?, updated_at=? WHERE file_id=?""",
                    (title, year, kind, round(conf, 2), int(conf < cfg.auto_approve),
                     json.dumps(ans, ensure_ascii=False), now(), r["id"]))
        stats["asked"] += 1
        if i % 25 == 0:
            con.commit()
            log(f"  {i}/{len(rows)}")
    con.commit()
    log(f"LLM answered {stats['asked']}, errors {stats['errors']}")
    return stats
