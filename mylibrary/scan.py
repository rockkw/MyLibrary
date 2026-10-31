"""Step 1: inventory. Walk the configured roots, ffprobe new/changed files. Read-only."""

import hashlib
import json
import os
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from .db import now

SAFETY_DIR = ".mylibrary-safety"
CHUNK = 1024 * 1024


def quick_hash(path: str, size: int) -> str:
    """sha1 of size + first and last 1 MiB. Cheap fingerprint for duplicate detection."""
    h = hashlib.sha1(str(size).encode())
    with open(path, "rb") as f:
        h.update(f.read(CHUNK))
        if size > 2 * CHUNK:
            f.seek(-CHUNK, os.SEEK_END)
            h.update(f.read(CHUNK))
    return h.hexdigest()


def ffprobe(path: str) -> dict:
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-print_format", "json",
             "-show_format", "-show_streams", path],
            capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired) as e:
        return {"probe_error": str(e)}
    if out.returncode != 0:
        return {"probe_error": (out.stderr or "ffprobe failed").strip()[:500]}
    data = json.loads(out.stdout or "{}")
    streams = data.get("streams", [])
    v = next((s for s in streams if s.get("codec_type") == "video"
              and s.get("disposition", {}).get("attached_pic") != 1), {})
    a = [s for s in streams if s.get("codec_type") == "audio"]
    subs = [s for s in streams if s.get("codec_type") == "subtitle"]
    lang = lambda s: (s.get("tags") or {}).get("language", "und")
    fmt = data.get("format", {})
    dur = fmt.get("duration") or v.get("duration")
    return {
        "duration": float(dur) if dur else None,
        "width": v.get("width"),
        "height": v.get("height"),
        "vcodec": v.get("codec_name"),
        "acodec": a[0].get("codec_name") if a else None,
        "audio_langs": ",".join(sorted({lang(s) for s in a})),
        "sub_langs": ",".join(sorted({lang(s) for s in subs})),
        "container": fmt.get("format_name"),
        "probe_error": None,
    }


def iter_videos(root: Path, video_ext: set[str]):
    for dirpath, dirnames, filenames in os.walk(root):
        # skip hidden folders, including our own safety snapshots
        dirnames[:] = [d for d in dirnames if not d.startswith(".")]
        for name in filenames:
            if name.startswith("."):
                continue
            if name.rsplit(".", 1)[-1].lower() in video_ext:
                yield os.path.join(dirpath, name)


def count_files(root: Path) -> int:
    """Count every regular file under root except safety snapshots. Used by verify."""
    n = 0
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d != SAFETY_DIR]
        n += len(filenames)
    return n


def scan(con, cfg, roots=None, log=print, workers=4) -> dict:
    roots = [Path(r) for r in (roots or cfg.roots)]
    stats = {"roots": [], "new": 0, "changed": 0, "unchanged": 0,
             "missing": 0, "skipped_roots": []}
    for root in roots:
        if not root.is_dir():
            log(f"WARN root not mounted / not found, skipping: {root}")
            stats["skipped_roots"].append(str(root))
            continue
        stats["roots"].append(str(root))
        known = {r["path"]: r for r in con.execute(
            "SELECT path, inode, size, mtime FROM files WHERE root=?", (str(root),))}
        todo, seen = [], set()
        for path in iter_videos(root, cfg.video_ext):
            seen.add(path)
            st = os.stat(path)
            k = known.get(path)
            if k and k["inode"] == st.st_ino and k["size"] == st.st_size \
                    and abs((k["mtime"] or 0) - st.st_mtime) < 1:
                stats["unchanged"] += 1
                continue
            stats["changed" if k else "new"] += 1
            todo.append((path, st))
        log(f"{root}: {len(seen)} videos, {len(todo)} to probe")

        def work(item):
            path, st = item
            info = ffprobe(path)
            try:
                info["quick_hash"] = quick_hash(path, st.st_size)
            except OSError as e:
                info["quick_hash"] = None
                info["probe_error"] = str(e)
            return path, st, info

        with ThreadPoolExecutor(max_workers=workers) as pool:
            for i, (path, st, info) in enumerate(pool.map(work, todo), 1):
                con.execute("""
                    INSERT INTO files (path, root, dev, inode, size, mtime, quick_hash,
                        duration, width, height, vcodec, acodec, audio_langs, sub_langs,
                        container, probe_error, missing, scanned_at)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,0,?)
                    ON CONFLICT(path) DO UPDATE SET
                        dev=excluded.dev, inode=excluded.inode, size=excluded.size,
                        mtime=excluded.mtime, quick_hash=excluded.quick_hash,
                        duration=excluded.duration, width=excluded.width,
                        height=excluded.height, vcodec=excluded.vcodec,
                        acodec=excluded.acodec, audio_langs=excluded.audio_langs,
                        sub_langs=excluded.sub_langs, container=excluded.container,
                        probe_error=excluded.probe_error, missing=0,
                        scanned_at=excluded.scanned_at
                """, (path, str(root), st.st_dev, st.st_ino, st.st_size, st.st_mtime,
                      info.get("quick_hash"), info.get("duration"), info.get("width"),
                      info.get("height"), info.get("vcodec"), info.get("acodec"),
                      info.get("audio_langs"), info.get("sub_langs"),
                      info.get("container"), info.get("probe_error"), now()))
                if i % 100 == 0:
                    con.commit()
                    log(f"  probed {i}/{len(todo)}")
        gone = [p for p in known if p not in seen]
        for p in gone:  # flag only; rows are kept for history
            con.execute("UPDATE files SET missing=1 WHERE path=?", (p,))
        con.executemany("UPDATE files SET missing=0 WHERE path=? AND missing=1",
                        [(p,) for p in seen])
        stats["missing"] += len(gone)
        con.commit()
    return stats
