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

Files marked for trash (TASK-31) move in the same run, with safe_move, into
<root>/.mylibrary-safety/run-<N>/trash/. Nothing is deleted; undo() puts them back.
"""

import contextlib
import fcntl
import json
import os

from . import leftovers, safefs, trash
from .db import now
from .preflight import prepare
from .scan import SAFETY_DIR, count_files


@contextlib.contextmanager
def _lock(cfg):
    """One apply, undo, cleanup or pipeline at a time, so nothing scans files mid-move."""
    path = str(cfg.db_file) + ".lock"
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a") as fh:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("another apply, undo or pipeline is already running")
        yield


def apply_plan(con, cfg, plan_id, execute=False, log=print) -> dict:
    prep = prepare(con, cfg, plan_id)
    log(f"plan #{plan_id}: {prep['count']} files (+{prep['sidecar_count']} sidecars) "
        f"ready, {len(prep['refused'])} refused")
    for r in prep["refused"]:
        log(f"  refused: {os.path.basename(r['src'])}: {r['reason']}")
    binned = prep["trash"]
    if binned["ready"] or binned["refused"]:
        log(f"to trash: {len(binned['ready'])} files ready, {len(binned['refused'])} refused")
        for r in binned["refused"]:
            log(f"  trash refused: {os.path.basename(r['path'])}: {r['reason']}")
    if not execute:
        for m in prep["moves"][:50]:
            log(f"  {m['src']}\n    -> {m['dst']}")
        for t in binned["ready"][:50]:
            log(f"  {t['path']}\n    -> trash (.mylibrary-safety/run-N/trash/)")
        log("DRY RUN: nothing was changed. Re-run with --execute to apply.")
        prep["dry_run"] = True
        return prep
    if not prep["moves"] and not binned["ready"]:
        log("nothing to apply")
        prep["dry_run"] = False
        return prep

    with _lock(cfg):
        roots = sorted({m["root"] for m in prep["moves"]} | {t["root"] for t in binned["ready"]})
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
        gone = _move_to_trash(con, binned["ready"], run_id, plan_id, log)
        for root, n in gone.items():  # verify expects these to have left the count
            before[root] -= n
        con.execute("UPDATE runs SET counts_before=?, counts_after=?, finished_at=? WHERE id=?",
                    (json.dumps(before), json.dumps({r: count_files(r) for r in roots}), now(),
                     run_id))
        con.commit()
    trashed = sum(gone.values())
    log(f"run #{run_id}: moved {moved}, failed {failed}"
        + (f", moved {trashed} to trash (undo with: mylib undo --run {run_id})" if trashed else ""))
    rep = verify(con, run_id, log=log)
    return {**prep, "dry_run": False, "run_id": run_id, "moved": moved,
            "failed": failed, "trashed": trashed, "verify": rep}


def _move_to_trash(con, items, run_id, plan_id, log) -> dict:
    """Move marked files into the run's trash folder. Returns how many left each root."""
    gone = {}
    for t in items:
        dst = os.path.join(t["root"], SAFETY_DIR, f"run-{run_id}", trash.TRASH_DIR, t["rel"])
        row = con.execute("SELECT id FROM files WHERE path=?", (t["path"],)).fetchone()
        file_id = row["id"] if row else None
        try:
            safefs.safe_move(t["path"], dst)
            status, err = "moved", None
        except OSError as e:
            status, err = "failed", str(e)
            log(f"  FAILED to trash {t['path']}: {err}")
        con.execute("""INSERT INTO moves (run_id, file_id, src, dst, inode, size, is_sidecar,
                       status, error, at, trash) VALUES (?,?,?,?,?,?,0,?,?,?,1)""",
                    (run_id, file_id, t["path"], dst, t["inode"], t["size"], status, err, now()))
        if status == "moved":
            gone[t["root"]] = gone.get(t["root"], 0) + 1
            con.execute("UPDATE trash SET status='moved', run_id=?, dst=? WHERE id=?",
                        (run_id, dst, t["id"]))
            if file_id:
                con.execute("UPDATE files SET missing=1 WHERE id=?", (file_id,))
                con.execute("""UPDATE proposals SET status='skipped', reason=? WHERE file_id=?
                               AND plan_id=? AND status NOT IN ('applied', 'failed')""",
                            (f"moved to trash (run {run_id})", file_id, plan_id))
        con.commit()
    return gone


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


def clean_leftovers(con, cfg, paths, execute=False, log=print) -> dict:
    """Move approved leftover folders into the run's safety folder. Nothing is deleted.

    Only a folder the report suggests right now can move, so a stale page or a folder
    that gained a file since it was approved is refused. undo() puts them back.
    """
    report = leftovers.find(con, cfg)
    if report["error"]:
        log(f"{report['error']}; nothing moved")
        return {"error": report["error"], "moved": 0, "refused": []}
    allowed = {i["path"]: i for i in report["empty"] + report["leftovers"]}
    ready = [allowed[p] for p in dict.fromkeys(paths) if p in allowed]
    refused = [{"path": p, "reason": "not a current suggestion"} for p in paths if p not in allowed]
    log(f"{len(ready)} folders ready, {len(refused)} refused")
    for r in refused:
        log(f"  refused: {r['path']}: {r['reason']}")
    if not execute:
        log("DRY RUN: nothing was changed. Re-run with --execute to move them aside.")
        return {"dry_run": True, "count": len(ready), "refused": refused, "moved": 0}
    if not ready:
        log("nothing to move")
        return {"dry_run": False, "moved": 0, "refused": refused}
    roots = [str(r) for r in cfg.roots]
    with _lock(cfg):
        counts = {r: count_files(r) for r in roots}
        run_id = con.execute("""INSERT INTO runs (kind, started_at, status, counts_before)
                                VALUES ('cleanup', ?, 'running', ?)""",
                             (now(), json.dumps(counts))).lastrowid
        moved = 0
        for item in ready:
            src = item["path"]
            root = leftovers.root_of(src, roots)
            dst = os.path.join(root, SAFETY_DIR, f"run-{run_id}", "leftovers",
                               os.path.relpath(src, root))
            st = os.lstat(src)
            try:
                safefs.safe_move(src, dst)
                status, err = "moved", None
                moved += 1
                counts[root] -= len(item["files"])  # verify expects these to have left the count
            except OSError as e:
                status, err = "failed", str(e)
                log(f"  ! {src}: {e}")
            con.execute("""INSERT INTO moves (run_id, src, dst, inode, size, is_sidecar, status,
                           error, at) VALUES (?,?,?,?,?,1,?,?,?)""",
                        (run_id, src, dst, st.st_ino, st.st_size, status, err, now()))
        con.execute("UPDATE runs SET finished_at=?, counts_before=? WHERE id=?",
                    (now(), json.dumps(counts), run_id))
        con.commit()
    log(f"run #{run_id}: moved {moved} folders aside, undo with: mylib undo --run {run_id}")
    rep = verify(con, run_id, log=log)
    return {"dry_run": False, "run_id": run_id, "moved": moved, "refused": refused, "verify": rep}


def undo(con, cfg, run_id, execute=False, log=print) -> dict:
    run = con.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
    if not run or run["kind"] not in ("apply", "cleanup"):
        raise ValueError(f"run #{run_id} is not an apply or cleanup run")
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
        for m in ready:  # leftover folders and trashed files rejoin the count
            if f"{os.sep}{SAFETY_DIR}{os.sep}" in m["dst"]:
                n = count_files(m["dst"]) if os.path.isdir(m["dst"]) else 1
                before[leftovers.root_of(m["src"], roots)] += n
        uid = con.execute("""INSERT INTO runs (plan_id, kind, undo_of, started_at, status,
                             counts_before) VALUES (?, 'undo', ?, ?, 'running', ?)""",
                          (run["plan_id"], run_id, now(), json.dumps(before))).lastrowid
        done = 0
        for m in ready:
            try:
                safefs.make_dirs(os.path.dirname(m["src"]))  # may have been cleaned up since
                safefs.safe_move(m["dst"], m["src"])
                status, err = "moved", None
                done += 1
                if m["trash"]:
                    _restore_from_trash(con, m)
                elif not m["is_sidecar"]:
                    con.execute("UPDATE files SET path=? WHERE id=?", (m["src"], m["file_id"]))
                    con.execute("UPDATE proposals SET status='approved' WHERE id=?",
                                (m["proposal_id"],))
            except OSError as e:
                status, err = "failed", str(e)
            con.execute("""INSERT INTO moves (run_id, file_id, proposal_id, src, dst, inode, size,
                           is_sidecar, status, error, at, trash) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (uid, m["file_id"], m["proposal_id"], m["dst"], m["src"], m["inode"],
                         m["size"], m["is_sidecar"], status, err, now(), m["trash"]))
        con.execute("UPDATE runs SET finished_at=? WHERE id=?", (now(), uid))
        con.commit()
    log(f"undo run #{uid}: restored {done}")
    rep = verify(con, uid, log=log)
    return {"run_id": uid, "undo_of": run_id, "dry_run": False, "restored": done, "verify": rep}


def _restore_from_trash(con, m) -> None:
    """Database side of putting a trashed file back. The mark does not come back."""
    con.execute("UPDATE trash SET status='restored' WHERE path=? AND status='moved'", (m["src"],))
    if m["file_id"]:
        con.execute("UPDATE files SET missing=0, path=? WHERE id=?", (m["src"], m["file_id"]))
        con.execute("""UPDATE proposals SET status='review', reason='restored from trash'
                       WHERE file_id=? AND status='skipped' AND reason LIKE 'moved to trash%'""",
                    (m["file_id"],))
