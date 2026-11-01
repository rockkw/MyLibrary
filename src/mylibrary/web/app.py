"""MyLibrary review UI (Flask), styled after MyMagnet.

This app never touches media files. It:
  * reads inventory.db to show proposals,
  * records decisions/edits in inventory.db (review module, DB only),
  * runs whitelisted `mylib` CLI commands as background jobs for anything
    that scans or renames, so the UI and terminal share one code path.

It deliberately does not import mylibrary.apply or mylibrary.safefs
(tests/test_no_delete.py checks this).
"""

import os
import subprocess
import sys
import threading
import time
from pathlib import Path

from flask import Flask, abort, jsonify, render_template, request

from mylibrary import config, db, dupes, plan, review

SRC = Path(__file__).resolve().parents[2]          # .../src
app = Flask(__name__)
CFG = config.load()
MYLIB = [sys.executable, "-m", "mylibrary.cli"] + (
    ["--config", str(CFG.source)] if CFG.source else [])
JOB_ENV = {**os.environ,
           "PYTHONPATH": os.pathsep.join(filter(None, [str(SRC), os.environ.get("PYTHONPATH")]))}

# name -> (argv builder, description). Only these can be launched from the UI.
JOBS = {
    "scan":      (lambda p: ["scan"], "Inventory drives"),
    "parse":     (lambda p: ["parse"], "Parse names"),
    "classify":  (lambda p: ["classify"], "Ask local LLM"),
    "plan":      (lambda p: ["plan"], "Build plan"),
    "pipeline":  (lambda p: ["pipeline"], "Scan → parse → classify → plan"),
    "apply_dry": (lambda p: ["apply", "--plan", _int(p, "plan")], "Apply (dry run)"),
    "apply":     (lambda p: ["apply", "--plan", _int(p, "plan"), "--execute"], "Apply"),
    "verify":    (lambda p: ["verify", "--run", _int(p, "run")], "Verify run"),
    "undo_dry":  (lambda p: ["undo", "--run", _int(p, "run")], "Undo (dry run)"),
    "undo":      (lambda p: ["undo", "--run", _int(p, "run"), "--execute"], "Undo run"),
}

_jobs: dict[int, dict] = {}
_job_lock = threading.Lock()
_next_id = [1]


def _int(params, key):
    try:
        return str(int(params[key]))
    except (KeyError, TypeError, ValueError):
        abort(400, f"{key} must be an integer")


def con():
    return db.connect(CFG.db_file)


def _run_job(job):
    proc = subprocess.Popen(job["argv"], stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, bufsize=1, env=JOB_ENV)
    for line in proc.stdout:
        job["log"].append(line.rstrip("\n"))
    job["rc"] = proc.wait()
    job["state"] = "done" if job["rc"] == 0 else "error"
    job["finished"] = time.time()


@app.get("/")
def index():
    return render_template("index.html", host=CFG.host, port=CFG.port)


@app.get("/api/summary")
def api_summary():
    c = con()
    pid = plan.current_plan(c)
    s = review.summary(c, pid)
    s["ollama_model"] = CFG.ollama_model if CFG.ollama_enabled else None
    s["auto_approve"] = CFG.auto_approve
    return jsonify(s)


@app.get("/api/proposals")
def api_proposals():
    c = con()
    pid = request.args.get("plan", type=int) or plan.current_plan(c)
    if not pid:
        return jsonify({"plan_id": None, "items": []})
    items = review.list_proposals(
        c, pid, request.args.get("status") or None, request.args.get("q") or None,
        request.args.get("root") or None, limit=request.args.get("limit", 500, type=int),
        offset=request.args.get("offset", 0, type=int))
    return jsonify({"plan_id": pid, "items": items})


@app.get("/api/proposal/<int:pid>")
def api_proposal(pid):
    d = review.detail(con(), pid)
    return jsonify(d) if d else abort(404)


@app.post("/api/decision")
def api_decision():
    body = request.get_json(force=True)
    ids = [int(i) for i in body.get("ids", [])]
    action = body.get("action")
    if action not in ("approve", "skip", "reset"):
        abort(400)
    n = review.decide(con(), ids, action)
    return jsonify({"updated": n, "cli": f"mylib {action} {' '.join(map(str, ids))}"})


@app.post("/api/edit")
def api_edit():
    body = request.get_json(force=True)
    number = lambda key: int(body[key]) if str(body.get(key) or "").strip().isdigit() else None
    year, season, episode, last, track, disc = (number(k) for k in (
        "year", "season", "episode", "episode_last", "track", "disc"))
    kind = body.get("kind") or None
    try:
        res = review.edit(con(), CFG, int(body["id"]), body.get("title", ""), year,
                          body.get("edition"), kind, season, episode, last,
                          body.get("episode_title"), body.get("artist"), body.get("album"),
                          track, disc)
    except ValueError as e:
        abort(400, str(e))
    res["cli"] = f'mylib edit {res["id"]} --title "{body.get("title","")}"' + \
        (f" --year {year}" if year else "") + \
        (f' --edition "{body["edition"]}"' if body.get("edition") else "") + \
        (f" --kind {kind}" if kind else "") + \
        (f" --season {season}" if season is not None else "") + \
        (f" --episode {episode}" if episode is not None else "") + \
        (f" --episode-last {last}" if last is not None else "") + \
        (f' --episode-title "{body["episode_title"]}"' if body.get("episode_title") else "") + \
        (f' --artist "{body["artist"]}"' if body.get("artist") else "") + \
        (f' --album "{body["album"]}"' if body.get("album") else "") + \
        (f" --track {track}" if track is not None else "") + \
        (f" --disc {disc}" if disc is not None else "")
    return jsonify(res)


@app.get("/api/runs")
def api_runs():
    rows = con().execute("""SELECT r.*, (SELECT COUNT(*) FROM moves m WHERE m.run_id=r.id
                            AND m.status='moved') moved FROM runs r ORDER BY id DESC LIMIT 100""")
    return jsonify([dict(r) for r in rows])


@app.get("/api/dupes")
def api_dupes():
    return jsonify(dupes.find(con()))


@app.post("/api/jobs")
def api_job_start():
    body = request.get_json(force=True)
    name = body.get("name")
    if name not in JOBS:
        abort(400, "unknown job")
    args = JOBS[name][0](body.get("params") or {})
    with _job_lock:
        if any(j["state"] == "running" for j in _jobs.values()):
            abort(409, "a job is already running")
        jid = _next_id[0]
        _next_id[0] += 1
        job = {"id": jid, "name": name, "label": JOBS[name][1], "argv": MYLIB + args,
               "cli": "mylib " + " ".join(args), "state": "running", "log": [],
               "rc": None, "started": time.time(), "finished": None}
        _jobs[jid] = job
    threading.Thread(target=_run_job, args=(job,), daemon=True).start()
    return jsonify({k: v for k, v in job.items() if k != "argv"})


@app.get("/api/jobs/<int:jid>")
def api_job(jid):
    job = _jobs.get(jid) or abort(404)
    since = request.args.get("since", 0, type=int)
    out = {k: v for k, v in job.items() if k not in ("argv", "log")}
    out["log"] = job["log"][since:]
    out["log_len"] = len(job["log"])
    return jsonify(out)


@app.get("/api/jobs")
def api_jobs():
    return jsonify([{k: v for k, v in j.items() if k not in ("argv", "log")}
                    for j in sorted(_jobs.values(), key=lambda j: -j["id"])[:20]])


def serve():
    if CFG.host in ("0.0.0.0", "::"):
        sys.exit("refusing to listen on all interfaces; use 127.0.0.1 or your Tailscale IP")
    print(f"MyLibrary UI on http://{CFG.host}:{CFG.port}")
    app.run(host=CFG.host, port=CFG.port, debug=False, threaded=True)
    return 0


if __name__ == "__main__":
    serve()
