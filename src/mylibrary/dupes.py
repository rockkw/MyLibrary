"""Duplicate report. Report only: MyLibrary never removes a duplicate for you.

Different editions of one film are separate versions, not duplicates.
"""


def find(con) -> dict:
    exact = []
    for g in con.execute("""SELECT quick_hash, size, COUNT(*) n FROM files
                            WHERE missing=0 AND quick_hash IS NOT NULL
                            GROUP BY quick_hash, size HAVING n>1"""):
        exact.append({"size": g["size"], "files": [r["path"] for r in con.execute(
            "SELECT path FROM files WHERE quick_hash=? AND size=? AND missing=0",
            (g["quick_hash"], g["size"]))]})
    same_movie = []
    for g in con.execute("""SELECT lower(p.title) t, p.year, p.edition, COUNT(*) n FROM parses p
                            JOIN files f ON f.id=p.file_id
                            WHERE f.missing=0 AND p.kind='movie' AND p.title IS NOT NULL
                            GROUP BY lower(p.title), p.year, p.edition HAVING n>1"""):
        rows = con.execute("""SELECT f.path, f.size, f.height, f.vcodec, f.duration
                              FROM parses p JOIN files f ON f.id=p.file_id
                              WHERE lower(p.title)=? AND p.year IS ? AND p.edition IS ?
                                AND f.missing=0
                              ORDER BY f.height DESC, f.size DESC""",
                           (g["t"], g["year"], g["edition"])).fetchall()
        same_movie.append({"title": g["t"], "year": g["year"], "edition": g["edition"],
                           "best": rows[0]["path"], "files": [dict(r) for r in rows]})
    return {"exact": exact, "same_movie": same_movie}
