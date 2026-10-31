"""Optional offline verification against IMDb's free title.basics.tsv.gz dataset.

Download it yourself (https://datasets.imdbws.com/) and run:
    mylib imdb-import /path/to/title.basics.tsv.gz
After that, lookups are fully offline.
"""

import csv
import gzip
import re
import sys
import unicodedata

KINDS = {"movie", "tvMovie", "video"}


def norm(title: str) -> str:
    t = unicodedata.normalize("NFKD", title or "").encode("ascii", "ignore").decode()
    t = re.sub(r"[^a-z0-9 ]+", " ", t.lower())
    t = re.sub(r"\b(the|a|an)\b", " ", t)
    return " ".join(t.split())


def import_basics(con, path, log=print) -> int:
    csv.field_size_limit(sys.maxsize)
    n = 0
    opener = gzip.open if str(path).endswith(".gz") else open
    with opener(path, "rt", encoding="utf-8", newline="") as f:
        rd = csv.reader(f, delimiter="\t", quoting=csv.QUOTE_NONE)
        next(rd, None)
        batch = []
        for row in rd:
            if len(row) < 8 or row[1] not in KINDS:
                continue
            year = int(row[5]) if row[5].isdigit() else None
            rt = int(row[7]) if row[7].isdigit() else None
            for t in {row[2], row[3]}:
                batch.append((row[0] if t == row[2] else row[0] + "~o",
                              norm(t), row[2], year, rt, row[1]))
            if len(batch) >= 5000:
                con.executemany("INSERT OR REPLACE INTO imdb VALUES (?,?,?,?,?,?)", batch)
                n += len(batch)
                batch.clear()
        con.executemany("INSERT OR REPLACE INTO imdb VALUES (?,?,?,?,?,?)", batch)
        n += len(batch)
    con.commit()
    log(f"imported {n} IMDb title rows")
    return n


def available(con) -> bool:
    return con.execute("SELECT 1 FROM imdb LIMIT 1").fetchone() is not None


def check(con, title, year, duration_s) -> dict:
    """Return {match: bool|None, tconst, title, runtime_ok, note}. None = no data."""
    if not title or not available(con):
        return {"match": None}
    rows = con.execute(
        "SELECT * FROM imdb WHERE title_norm=? AND year BETWEEN ? AND ?",
        (norm(title), (year or 0) - 1, (year or 9999) + 1) if year else
        (norm(title), 0, 9999)).fetchall()
    if not rows:
        return {"match": False, "note": "not found in IMDb dataset"}
    rows = sorted(rows, key=lambda r: (abs((r["year"] or 0) - (year or 0)),
                                       r["kind"] != "movie"))
    best = rows[0]
    runtime_ok = None
    if duration_s and best["runtime"]:
        runtime_ok = abs(duration_s / 60 - best["runtime"]) <= max(12, best["runtime"] * 0.15)
    return {"match": True, "tconst": best["tconst"].split("~")[0], "title": best["title"],
            "year": best["year"], "runtime_ok": runtime_ok,
            "ambiguous": len({r["tconst"].split("~")[0] for r in rows}) > 1}
