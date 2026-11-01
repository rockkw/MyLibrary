"""Step 5: apply approved renames, verify, undo.

Safety layers, in order:
  1. Dry run by default; nothing moves without execute=True (CLI: --execute).
  2. Pre-checks: file unchanged since scan (same inode, size, mtime), target free,
     same drive, no two items claiming the same target.
  3. Hard-link safety snapshot of every file before it moves, under
     <root>/.mylibrary-safety/run-<N>/ (costs no disk space).
  4. safe_move(): atomic no-replace rename, never copy+delete.
  5. Verify: every moved inode is at its target with the same size, sources are
     gone, and the file count of each root is unchanged.
  6. Every move is logged so undo() can reverse a whole run.
"""

import contextlib
import fcntl
import json
import os

from . import safefs
from .db import now
from .scan import SAFETY_DIR, count_files


@contextlib.contextmanager
def _lock(cfg):
    path = str(cfg.db_file) + ".lock"
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a") as fh:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("another apply/undo is already running")
        yield


def _nearest_existing(path: str) -> str:
    while path and not os.path.exists(path):
        parent = os.path.dirname(path)
        if parent == path:
            break
        path = parent
    return path


def _sidecars(src: str, dst: str, sidecar_ext: set[str]):
    """Find subtitle/nfo files sharing the video's stem; map them next to the new name."""
    d = os.path.dirname(src)
    stem = os.path.splitext(os.path.basename(src))[0]
    new_stem = os.path.splitext(dst)[0]
    out = []
    try:
        names = os.listdir(d)
    except OSError:
        return out
    for name in names:
        if name == os.path.basename(src) or not name.startswith(stem):
            continue
        ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
        if ext in sidecar_ext:
            out.append((os.path.join(d, name), new_stem + name[len(stem):]))
    return out


def prepare(con, cfg, plan_id) -> dict:
    items = con.execute("""SELECT pr.id pid, pr.file_id, pr.src, pr.dst, f.root, f.inode, f.size, f.mtime
                           FROM proposals pr JOIN files f ON f.id=pr.file_id
                           WHERE pr.plan_id=? AND pr.status IN ('auto','approved')
                           AND pr.dst IS NOT NULL ORDER BY pr.src""", (plan_id,)).fetchall()
    ok, refused, targets = [], [], set()
    for it in items:
        src, dst = it["src"], it["dst"]
        why = None
        try:
            st = os.lstat(src)
        except FileNotFoundError:
            st, why = None, "source missing (run scan)"
        # Downloaders preallocate, so size alone cannot show that a file is still filling up.
        if st and (st.st_ino != it["inode"] or st.st_size != it["size"]
                   or abs(st.st_mtime - (it["mtime"] or 0)) >= 1):
            why = "file changed since scan (run scan)"
        elif os.path.lexists(dst):
            why = "target already exists"
        elif dst in targets:
            why = "two items share this target"
        elif st and os.stat(_nearest_existing(os.path.dirname(dst))).st_dev != st.st_dev:
            why = "target is on a different drive"
        sidecars = []
        if not why:
            for s_src, s_dst in _sidecars(src, dst, cfg.sidecar_ext):
                if os.path.lexists(s_dst) or s_dst in targets:
                    why = f"sidecar target exists: {os.path.basename(s_dst)}"
                    break
                sidecars.append((s_src, s_dst))
        if why:
            refused.append({"proposal_id": it["pid"], "src": src, "dst": dst, "reason": why})
            continue
        targets.add(dst)
        targets.update(d for _, d in sidecars)
        ok.append({"proposal_id": it["pid"], "file_id": it["file_id"], "root": it["root"],
                   "src": src, "dst": dst, "inode": st.st_ino, "size": st.st_size,
                   "sidecars": sidecars})
    return {"plan_id": plan_id, "moves": ok, "refused": refused,
            "count": len(ok), "sidecar_count": sum(len(m["sidecars"]) for m in ok),
            "bytes_moved_on_disk": 0}


def apply_plan(con, cfg, plan_id, execute=False, log=print) -> dict:
    prep = prepare(con, cfg, plan_id)
    log(f"plan #{plan_id}: {prep['count']} files (+{prep['sidecar_count']} sidecars) "
        f"ready, {len(prep['refused'])} refused")
    for r in prep["refused"]:
        log(f"  refused: {os.path.basename(r['src'])}: {r['reason']}")
    if not execute:
        for m in prep["moves"][:50]:
            log(f"  {m['src']}\n    -> {m['dst']}")
        log("DRY RUN: nothing was changed. Re-run with --execute to apply.")
        prep["dry_run"] = True
        return prep
    if not prep["moves"]:
        log("nothing to apply")
        prep["dry_run"] = False
        return prep

    with _lock(cfg):
        roots = sorted({m["root"] for m in prep["moves"]})
        before = {r: count_files(r) for r in roots}
        run_id = con.execute("""INSERT INTO runs (plan_id, kind, started_at, status, counts_before)
                                VALUES (?, 'apply', ?, 'running', ?)""",
                             (plan_id, now(), json.dumps(before))).lastrowid
        snaps = {r: os.path.join(r, SAFETY_DIR, f"run-{run_id}") for r in roots}
        con.execute("UPDATE runs SET snapshot_dirs=? WHERE id=?",
                    (json.dumps(list(snaps.values())), run_id))
        con.commit()
        moved = failed = 0
        for m in prep["moves"]:
            pairs = [(m["src"], m["dst"], 0)] + [(s, d, 1) for s, d in m["sidecars"]]
            video_ok = False
            for src, dst, side in pairs:
                if side and not video_ok:
                    break
                st = os.lstat(src)
                try:
                    rel = os.path.relpath(src, m["root"])
                    safefs.snapshot_link(src, os.path.join(snaps[m["root"]], rel))
                    safefs.safe_move(src, dst)
                    status, err = "moved", None
                except OSError as e:
                    status, err = "failed", str(e)
                con.execute("""INSERT INTO moves (run_id, file_id, proposal_id, src, dst, inode,
                               size, is_sidecar, status, error, at)
                               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                            (run_id, m["file_id"], m["proposal_id"], src, dst, st.st_ino,
                             st.st_size, side, status, err, now()))
                if not side:
                    video_ok = status == "moved"
                    if video_ok:
                        con.execute("UPDATE files SET path=? WHERE id=?", (dst, m["file_id"]))
                        con.execute("UPDATE proposals SET status='applied' WHERE id=?",
                                    (m["proposal_id"],))
                        moved += 1
                    else:
                        con.execute("UPDATE proposals SET status='failed', reason=? WHERE id=?",
                                    (err, m["proposal_id"]))
                        failed += 1
                        log(f"  FAILED {src}: {err}")
            con.commit()
        con.execute("UPDATE runs SET counts_after=?, finished_at=? WHERE id=?",
                    (json.dumps({r: count_files(r) for r in roots}), now(), run_id))
        con.commit()
    log(f"run #{run_id}: moved {moved}, failed {failed}")
    rep = verify(con, run_id, log=log)
    return {**prep, "dry_run": False, "run_id": run_id, "moved": moved,
            "failed": failed, "verify": rep}


def verify(con, run_id, log=print) -> dict:
    run = con.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
    if not run:
        raise ValueError(f"no run #{run_id}")
    problems = []
    rows = con.execute("SELECT * FROM moves WHERE run_id=? AND status='moved'", (run_id,)).fetchall()
    for m in rows:
        try:
            st = os.lstat(m["dst"])
            if st.st_ino != m["inode"] or st.st_size != m["size"]:
                problems.append(f"changed at target: {m['dst']}")
        except FileNotFoundError:
            problems.append(f"missing at target: {m['dst']}")
        if os.path.lexists(m["src"]):
            problems.append(f"source still present: {m['src']}")
    before = json.loads(run["counts_before"] or "{}")
    after = {r: count_files(r) for r in before}
    for r, n in before.items():
        if after.get(r) != n:
            problems.append(f"file count changed in {r}: {n} -> {after.get(r)}")
    ok = not problems
    status = "ok" if ok else "verify_failed"
    if ok and con.execute("SELECT 1 FROM moves WHERE run_id=? AND status='failed'",
                          (run_id,)).fetchone():
        status = "partial"
    con.execute("UPDATE runs SET verify_ok=?, verify_report=?, status=?, counts_after=? WHERE id=?",
                (int(ok), json.dumps(problems), status, json.dumps(after), run_id))
    con.commit()
    if ok:
        log(f"VERIFY OK run #{run_id}: {len(rows)} moves checked, file counts unchanged {after}")
    else:
        log(f"VERIFY FAILED run #{run_id}:")
        for p in problems:
            log(f"  ! {p}")
    return {"run_id": run_id, "ok": ok, "problems": problems, "checked": len(rows),
            "counts_before": before, "counts_after": after}


def undo(con, cfg, run_id, execute=False, log=print) -> dict:
    run = con.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
    if not run or run["kind"] != "apply":
        raise ValueError(f"run #{run_id} is not an apply run")
    moves = con.execute("SELECT * FROM moves WHERE run_id=? AND status='moved' ORDER BY id DESC",
                        (run_id,)).fetchall()
    ready, refused = [], []
    for m in moves:
        try:
            st = os.lstat(m["dst"])
        except FileNotFoundError:
            refused.append({"src": m["dst"], "reason": "no longer at target"})
            continue
        if st.st_ino != m["inode"]:
            refused.append({"src": m["dst"], "reason": "different file at target"})
        elif os.path.lexists(m["src"]):
            refused.append({"src": m["dst"], "reason": "original location is occupied"})
        else:
            ready.append(m)
    log(f"undo run #{run_id}: {len(ready)} moves reversible, {len(refused)} refused")
    for r in refused:
        log(f"  refused: {r['src']}: {r['reason']}")
    if not execute:
        log("DRY RUN: nothing was changed. Re-run with --execute to undo.")
        return {"run_id": run_id, "dry_run": True, "count": len(ready), "refused": refused}
    with _lock(cfg):
        roots = sorted(json.loads(run["counts_before"] or "{}"))
        before = {r: count_files(r) for r in roots}
        uid = con.execute("""INSERT INTO runs (plan_id, kind, undo_of, started_at, status,
                             counts_before) VALUES (?, 'undo', ?, ?, 'running', ?)""",
                          (run["plan_id"], run_id, now(), json.dumps(before))).lastrowid
        done = 0
        for m in ready:
            try:
                safefs.safe_move(m["dst"], m["src"])
                status, err = "moved", None
                done += 1
                if not m["is_sidecar"]:
                    con.execute("UPDATE files SET path=? WHERE id=?", (m["src"], m["file_id"]))
                    con.execute("UPDATE proposals SET status='approved' WHERE id=?",
                                (m["proposal_id"],))
            except OSError as e:
                status, err = "failed", str(e)
            con.execute("""INSERT INTO moves (run_id, file_id, proposal_id, src, dst, inode, size,
                           is_sidecar, status, error, at) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                        (uid, m["file_id"], m["proposal_id"], m["dst"], m["src"], m["inode"],
                         m["size"], m["is_sidecar"], status, err, now()))
        con.execute("UPDATE runs SET finished_at=? WHERE id=?", (now(), uid))
        con.commit()
    log(f"undo run #{uid}: restored {done}")
    rep = verify(con, uid, log=log)
    return {"run_id": uid, "undo_of": run_id, "dry_run": False, "restored": done, "verify": rep}
