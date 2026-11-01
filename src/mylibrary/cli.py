"""mylib — MyLibrary command line. The CLI is the source of truth; the web UI runs these commands.

  mylib scan                      inventory configured roots (ffprobe, fingerprints)
  mylib parse                     identify files with guessit, flag uncertain ones
  mylib classify [--limit N]      ask local Ollama about flagged files only
  mylib plan                      build a rename plan (DB only)
  mylib pipeline                  scan + parse + classify + plan
  mylib review [--status review]  list proposals
  mylib approve ID... | skip ID... | reset ID...
  mylib edit ID --title T --year Y [--edition E]
  mylib edit ID --title SERIES --season S --episode E [--episode-last N] [--episode-title T]
  mylib edit ID --title TRACK --artist A --album B [--track N] [--disc N]
  mylib edit ID --title PRODUCT --kind software
  mylib apply [--plan N]          dry run; add --execute to actually rename
  mylib verify --run N
  mylib undo --run N              dry run; add --execute to reverse a run
  mylib runs | status | dupes
  mylib imdb-import FILE          optional offline IMDb title data
  mylib serve                     start the review UI (default http://127.0.0.1:5051)

Add --json to any command for machine-readable output (logs go to stderr).
"""

import argparse
import json
import sys

from . import __version__, apply as applymod, classify, config, db, downloads, dupes, imdb
from . import parse, plan, review, scan


def main(argv=None):
    ap = argparse.ArgumentParser(prog="mylib", description="MyLibrary movie organizer")
    ap.add_argument("--config", help="path to config.ini")
    ap.add_argument("--json", action="store_true", help="JSON output; logs to stderr")
    ap.add_argument("--version", action="version", version=__version__)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("scan"); p.add_argument("--root", action="append")
    p.add_argument("--workers", type=int, default=4)
    p = sub.add_parser("parse"); p.add_argument("--redo", action="store_true")
    p = sub.add_parser("classify"); p.add_argument("--limit", type=int)
    p.add_argument("--redo", action="store_true"); p.add_argument("--model")
    p = sub.add_parser("plan"); p.add_argument("--note", default="")
    p = sub.add_parser("pipeline"); p.add_argument("--limit", type=int)
    p = sub.add_parser("review"); p.add_argument("--status", default="review,conflict")
    p.add_argument("--plan", type=int); p.add_argument("--q")
    p.add_argument("--limit", type=int, default=100)
    for name in ("approve", "skip", "reset"):
        p = sub.add_parser(name); p.add_argument("ids", type=int, nargs="+")
    p = sub.add_parser("edit"); p.add_argument("id", type=int)
    p.add_argument("--title", required=True); p.add_argument("--year", type=int)
    p.add_argument("--edition", help='e.g. "Final Cut"; "" clears it')
    p.add_argument("--kind", choices=("movie", "episode", "music", "software"), help="change what this file is")
    p.add_argument("--season", type=int); p.add_argument("--episode", type=int)
    p.add_argument("--episode-last", type=int, help="last episode of a multi-episode file")
    p.add_argument("--episode-title", help='"" clears it')
    p.add_argument("--artist"); p.add_argument("--album")
    p.add_argument("--track", type=int); p.add_argument("--disc", type=int)
    p = sub.add_parser("apply"); p.add_argument("--plan", type=int)
    p.add_argument("--execute", action="store_true", help="actually rename files")
    p = sub.add_parser("verify"); p.add_argument("--run", type=int, required=True)
    p = sub.add_parser("undo"); p.add_argument("--run", type=int, required=True)
    p.add_argument("--execute", action="store_true")
    sub.add_parser("runs"); sub.add_parser("status"); sub.add_parser("dupes")
    p = sub.add_parser("imdb-import"); p.add_argument("file")
    sub.add_parser("serve")

    a = ap.parse_args(argv)
    cfg = config.load(a.config)
    if a.cmd == "serve":
        if a.config:
            import os
            os.environ["MYLIBRARY_CONFIG"] = a.config
        from .web.app import serve
        return serve()
    out = sys.stderr if a.json else sys.stdout
    log = lambda msg="": print(msg, file=out, flush=True)
    con = db.connect(cfg.db_file)
    plan_id = getattr(a, "plan", None) or plan.current_plan(con)

    def need_plan():
        if not plan_id:
            sys.exit("no plan yet: run `mylib plan` first")
        return plan_id

    if a.cmd == "scan":
        if not (a.root or cfg.roots):
            sys.exit("no roots: set [library] roots in config.ini or pass --root")
        res = scan.scan(con, cfg, a.root, log=log, workers=a.workers)
    elif a.cmd == "parse":
        res = parse.parse_all(con, cfg, redo=a.redo, log=log)
    elif a.cmd == "classify":
        if a.model:
            cfg.ollama_model = a.model
        res = classify.classify_all(con, cfg, limit=a.limit, redo=a.redo, log=log)
    elif a.cmd == "plan":
        res = plan.make_plan(con, cfg, note=a.note, log=log)
    elif a.cmd == "pipeline":
        res = {"scan": scan.scan(con, cfg, log=log),
               "parse": parse.parse_all(con, cfg, log=log),
               "classify": classify.classify_all(con, cfg, limit=a.limit, log=log),
               "plan": plan.make_plan(con, cfg, note="pipeline", log=log)}
    elif a.cmd == "review":
        res = review.list_proposals(con, need_plan(), a.status, a.q, limit=a.limit)
        for r in res:
            log(f"#{r['id']:<6} {r['status']:<9} {r['confidence']:.2f}  {r['src']}\n"
                f"{'':<20}-> {r['dst']}   [{r['reason'] or ''}]")
    elif a.cmd in ("approve", "skip", "reset"):
        n = review.decide(con, a.ids, a.cmd)
        res = {"updated": n}
        log(f"{a.cmd}: {n} updated")
    elif a.cmd == "edit":
        res = review.edit(con, cfg, a.id, a.title, a.year, a.edition, a.kind, a.season, a.episode,
                          a.episode_last, a.episode_title, a.artist, a.album, a.track, a.disc)
        log(f"#{res['id']} {res['status']}: {res['dst']}")
    elif a.cmd == "apply":
        res = applymod.apply_plan(con, cfg, need_plan(), execute=a.execute, log=log)
        res.pop("moves", None) if a.json and len(res.get("moves", [])) > 200 else None
    elif a.cmd == "verify":
        res = applymod.verify(con, a.run, log=log)
    elif a.cmd == "undo":
        res = applymod.undo(con, cfg, a.run, execute=a.execute, log=log)
    elif a.cmd == "runs":
        res = [dict(r) for r in con.execute(
            "SELECT id, plan_id, kind, undo_of, started_at, finished_at, status, verify_ok "
            "FROM runs ORDER BY id DESC LIMIT 50")]
        for r in res:
            log(f"run #{r['id']} {r['kind']:<5} plan {r['plan_id']} {r['status']:<13} "
                f"{r['started_at']}")
    elif a.cmd == "status":
        res = review.summary(con, plan.current_plan(con))
        res["config"] = str(cfg.source)
        res["db"] = str(cfg.db_file)
        res["imdb"] = imdb.available(con)
        active = downloads.check(cfg)
        res["downloads"] = {"checked": cfg.transmission_enabled, "error": active.error,
                            "unfinished_torrents": active.torrents}
        log(json.dumps(res, indent=2))
    elif a.cmd == "dupes":
        res = dupes.find(con)
        log(f"{len(res['exact'])} exact duplicate groups, "
            f"{len(res['same_movie'])} movies with more than one file")
        for g in res["same_movie"]:
            log(f"  {g['title']} ({g['year']}){' - ' + g['edition'] if g['edition'] else ''}: "
                f"keep? {g['best']}")
            for f in g["files"][1:]:
                log(f"      also: {f['path']}")
    elif a.cmd == "imdb-import":
        res = {"rows": imdb.import_basics(con, a.file, log=log)}
    if a.json:
        print(json.dumps(res, default=str, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
