"""SQLite schema and connection helper. One shared inventory.db for CLI and UI."""

import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS files (
    id          INTEGER PRIMARY KEY,
    path        TEXT UNIQUE NOT NULL,
    root        TEXT NOT NULL,
    dev         INTEGER,
    inode       INTEGER,
    size        INTEGER,
    mtime       REAL,
    quick_hash  TEXT,
    duration    REAL,           -- seconds
    width       INTEGER,
    height      INTEGER,
    vcodec      TEXT,
    acodec      TEXT,
    audio_langs TEXT,
    sub_langs   TEXT,
    container   TEXT,
    tags        TEXT,           -- JSON, audio files only
    probe_error TEXT,
    missing     INTEGER DEFAULT 0,
    scanned_at  TEXT
);

-- Current best identification for each file.
CREATE TABLE IF NOT EXISTS parses (
    file_id     INTEGER PRIMARY KEY REFERENCES files(id),
    title       TEXT,
    year        INTEGER,
    edition     TEXT,           -- Director's Cut, Final Cut, ...
    kind        TEXT,           -- movie | episode | music | software | scene | extra | sample | unknown
    season      INTEGER,        -- episodes only; title holds the series name
    episode     INTEGER,
    episode_last INTEGER,       -- last episode of a multi-episode file
    episode_title TEXT,
    artist      TEXT,           -- music only; title holds the track title
    album       TEXT,
    track       INTEGER,
    disc        INTEGER,        -- set only for multi-disc albums
    site        TEXT,           -- scenes only; title holds the scene title
    scene_date  TEXT,           -- YYYY-MM-DD
    performers  TEXT,           -- comma separated
    source      TEXT,           -- guessit | llm | manual
    confidence  REAL,
    flagged     INTEGER,        -- 1 = needs the LLM / a human
    reasons     TEXT,
    guessit_json TEXT,
    llm_json    TEXT,
    imdb_id     TEXT,
    updated_at  TEXT
);

CREATE TABLE IF NOT EXISTS plans (
    id          INTEGER PRIMARY KEY,
    created_at  TEXT,
    superseded  INTEGER DEFAULT 0,
    note        TEXT
);

CREATE TABLE IF NOT EXISTS proposals (
    id          INTEGER PRIMARY KEY,
    plan_id     INTEGER REFERENCES plans(id),
    file_id     INTEGER REFERENCES files(id),
    src         TEXT,
    dst         TEXT,
    confidence  REAL,
    status      TEXT,   -- auto|review|conflict|noop|ignored|approved|skipped|applied|failed
    reason      TEXT,
    decided_at  TEXT
);
CREATE INDEX IF NOT EXISTS ix_prop_plan ON proposals(plan_id, status);

CREATE TABLE IF NOT EXISTS runs (
    id            INTEGER PRIMARY KEY,
    plan_id       INTEGER,
    kind          TEXT,          -- apply | undo
    undo_of       INTEGER,
    started_at    TEXT,
    finished_at   TEXT,
    status        TEXT,          -- running | ok | partial | verify_failed
    counts_before TEXT,          -- JSON {root: file_count}
    counts_after  TEXT,
    verify_ok     INTEGER,
    verify_report TEXT,
    snapshot_dirs TEXT           -- JSON list
);

CREATE TABLE IF NOT EXISTS moves (
    id          INTEGER PRIMARY KEY,
    run_id      INTEGER REFERENCES runs(id),
    file_id     INTEGER,
    proposal_id INTEGER,
    src         TEXT,
    dst         TEXT,
    inode       INTEGER,
    size        INTEGER,
    is_sidecar  INTEGER DEFAULT 0,
    status      TEXT,            -- moved | failed | skipped
    error       TEXT,
    at          TEXT
);

CREATE TABLE IF NOT EXISTS imdb (
    tconst      TEXT PRIMARY KEY,
    title_norm  TEXT,
    title       TEXT,
    year        INTEGER,
    runtime     INTEGER,
    kind        TEXT
);
CREATE INDEX IF NOT EXISTS ix_imdb_norm ON imdb(title_norm, year);
"""


def connect(db_file: Path) -> sqlite3.Connection:
    db_file = Path(db_file)
    db_file.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(db_file, timeout=30)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA foreign_keys=ON")
    con.executescript(SCHEMA)
    for table, cols in (("files", (("tags", "TEXT"),)),
                        ("parses", (("edition", "TEXT"), ("season", "INTEGER"),
                                    ("episode", "INTEGER"), ("episode_last", "INTEGER"),
                                    ("episode_title", "TEXT"), ("artist", "TEXT"),
                                    ("album", "TEXT"), ("track", "INTEGER"),
                                    ("disc", "INTEGER"), ("site", "TEXT"),
                                    ("scene_date", "TEXT"), ("performers", "TEXT")))):
        have = {r["name"] for r in con.execute(f"PRAGMA table_info({table})")}
        for col, kind in cols:
            if col not in have:
                con.execute(f"ALTER TABLE {table} ADD COLUMN {col} {kind}")
    return con


def now() -> str:
    from datetime import datetime
    return datetime.now().isoformat(timespec="seconds")
