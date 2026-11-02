"""TASK-31: move a file to trash from the tree. Marking records a decision only; apply moves
the file with safe_move into .mylibrary-safety/run-N/trash/; undo restores it; nothing is
deleted. All names here are made up (the repo is public)."""

import ast
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from conftest import ROOT, tree_digest
from mylibrary import apply, downloads, leftovers, plan, preflight, trash, tree
from test_pipeline import add_files, proposal, quiet, run_to_plan
from test_tree import find
from test_web import client  # noqa: F401

MATRIX = "incoming/The.Matrix.1999.1080p.BluRay.x264-GRP.mkv"
SUBS = "incoming/The.Matrix.1999.1080p.BluRay.x264-GRP.en.srt"
MYSTERY = "misc/final_cut_v2.mp4"
NOTES = "misc/notes.txt"


def p(lib, rel):
    return str(lib["root"] / rel)


def marks(lib):
    return {r["path"]: r["status"] for r in lib["con"].execute("SELECT path, status FROM trash")}


def execute(lib):
    con = lib["con"]
    return apply.apply_plan(con, lib["cfg"], plan.current_plan(con), execute=True, log=quiet)


def binned(lib, run_id, rel):
    return lib["root"] / ".mylibrary-safety" / f"run-{run_id}" / "trash" / rel


# ── marking ──

def test_marking_records_a_decision_and_changes_nothing(lib):
    before = tree_digest(lib["root"])
    res = trash.mark(lib["con"], lib["cfg"], [p(lib, MYSTERY), p(lib, NOTES)])
    assert res["marked"] == [p(lib, MYSTERY), p(lib, NOTES)] and not res["refused"]
    assert marks(lib) == {p(lib, MYSTERY): "marked", p(lib, NOTES): "marked"}
    assert tree_digest(lib["root"]) == before


def test_marking_twice_keeps_one_mark(lib):
    trash.mark(lib["con"], lib["cfg"], [p(lib, NOTES), p(lib, NOTES)])
    trash.mark(lib["con"], lib["cfg"], [p(lib, NOTES)])
    assert len(trash.listing(lib["con"])) == 1


def test_files_that_may_not_go_to_trash_are_refused(lib, tmp_path):
    outside = tmp_path / "outside.mkv"
    outside.write_bytes(b"x")
    add_files(lib, ".mylibrary-safety/run-1/Old.Film.2001.mkv")
    os.symlink(lib["root"] / MATRIX, lib["root"] / "misc/alias.mkv")
    bad = {str(outside): "configured root", p(lib, ".mylibrary-safety/run-1/Old.Film.2001.mkv"): "safety",
           p(lib, "misc/alias.mkv"): "symlink", p(lib, "misc"): "only files",
           p(lib, "misc/gone.mkv"): "no such file", "relative.mkv": "absolute",
           f"{lib['root']}/misc/../misc/notes.txt": "normal form"}
    res = trash.mark(lib["con"], lib["cfg"], list(bad))
    assert not res["marked"] and marks(lib) == {}
    for r in res["refused"]:
        assert bad[r["path"]] in r["reason"], r


def test_downloading_held_and_unknown_downloads_are_refused(lib, monkeypatch):
    active = downloads.Active(paths={p(lib, MYSTERY)}, held={p(lib, MATRIX): "Some Torrent"})
    res = trash.mark(lib["con"], lib["cfg"], [p(lib, MYSTERY), p(lib, MATRIX)], active=active)
    assert {r["reason"] for r in res["refused"]} == {"still downloading",
                                                      "held by the torrent Some Torrent"}
    res = trash.mark(lib["con"], lib["cfg"], [p(lib, NOTES)],
                     active=downloads.Active(error="Transmission is not reachable"))
    assert "Transmission is not reachable" in res["refused"][0]["reason"]
    assert marks(lib) == {}


def test_preview_offers_sidecars(lib):
    d = trash.preview(lib["con"], lib["cfg"], p(lib, MATRIX))
    assert d["error"] is None and d["size"] == 600
    assert [s["path"] for s in d["sidecars"]] == [p(lib, SUBS)]
    assert trash.preview(lib["con"], lib["cfg"], p(lib, NOTES))["sidecars"] == []
    assert "configured root" in trash.preview(lib["con"], lib["cfg"], "/etc/hosts")["error"]


def test_a_mark_can_be_cleared_before_apply(lib):
    trash.mark(lib["con"], lib["cfg"], [p(lib, NOTES)])
    assert trash.clear(lib["con"], [p(lib, NOTES), p(lib, MYSTERY)])["cleared"] == 1
    assert marks(lib) == {}


# ── dry run and tree ──

def test_dry_run_lists_trash_and_changes_nothing(lib):
    run_to_plan(lib)
    trash.mark(lib["con"], lib["cfg"], [p(lib, NOTES)])
    before = tree_digest(lib["root"])
    lines = []
    con = lib["con"]
    res = apply.apply_plan(con, lib["cfg"], plan.current_plan(con), log=lines.append)
    assert res["dry_run"] and [t["path"] for t in res["trash"]["ready"]] == [p(lib, NOTES)]
    assert any(line.startswith("to trash: 1 files ready") for line in lines)
    assert tree_digest(lib["root"]) == before and marks(lib)[p(lib, NOTES)] == "marked"


def test_tree_marks_trash_and_counts_it_as_leaving(lib):
    run_to_plan(lib)
    trash.mark(lib["con"], lib["cfg"], [p(lib, NOTES), p(lib, MYSTERY)])
    data = tree.build(lib["con"], lib["cfg"])
    n = find(data, "notes.txt")
    assert n["state"] == "trash" and n["was"]["state"] == "untracked"
    assert find(data, MYSTERY)["was"]["state"] == "decide"
    misc = find(data, "/misc", "dir")
    assert misc["trash"] == 2 and misc["out"] == 2 and data["trash"] == 2


def test_tree_offers_trash_only_where_allowed(lib, monkeypatch):
    run_to_plan(lib)
    apply.apply_plan(lib["con"], lib["cfg"], plan.current_plan(lib["con"]), execute=True, log=quiet)
    os.symlink(lib["root"] / NOTES, lib["root"] / "misc/alias.txt")
    monkeypatch.setattr(downloads, "check", lambda cfg: downloads.Active(paths={p(lib, MYSTERY)}))
    data = tree.build(lib["con"], lib["cfg"], hidden=True)
    assert find(data, "notes.txt")["trash_ok"]
    assert not find(data, "alias.txt")["trash_ok"]
    assert not find(data, MYSTERY)["trash_ok"]
    from test_tree import flat
    safety = [n for (path, _), n in flat(data["roots"][0]).items()
              if ".mylibrary-safety" in path and n["type"] == "file"]
    assert safety and not any(n["trash_ok"] for n in safety)


def test_a_marked_file_is_held_out_of_the_plan(lib):
    run_to_plan(lib)
    trash.mark(lib["con"], lib["cfg"], [p(lib, MATRIX)])
    prep = preflight.prepare(lib["con"], lib["cfg"], plan.current_plan(lib["con"]))
    assert p(lib, MATRIX) not in {m["src"] for m in prep["moves"]}
    assert any(r["src"] == p(lib, MATRIX) and r["reason"] == "marked for trash"
               for r in prep["refused"])


def test_a_marked_sidecar_goes_to_trash_not_with_its_video(lib):
    run_to_plan(lib)
    trash.mark(lib["con"], lib["cfg"], [p(lib, SUBS)])
    prep = preflight.prepare(lib["con"], lib["cfg"], plan.current_plan(lib["con"]))
    m = next(m for m in prep["moves"] if m["src"] == p(lib, MATRIX))
    assert m["sidecars"] == []
    res = execute(lib)
    assert res["moved"] == 2 and res["trashed"] == 1 and res["verify"]["ok"]  # videos only
    assert binned(lib, res["run_id"], SUBS).read_bytes() == b"subs"


# ── apply, verify, undo ──

def test_apply_moves_marked_files_into_the_run_trash_folder(lib):
    run_to_plan(lib)
    con = lib["con"]
    trash.mark(con, lib["cfg"], [p(lib, MATRIX), p(lib, SUBS), p(lib, NOTES)])
    res = execute(lib)
    rid = res["run_id"]
    assert res["trashed"] == 3 and res["moved"] == 1 and res["verify"]["ok"]
    assert binned(lib, rid, MATRIX).read_bytes() == b"matrix" * 100
    assert binned(lib, rid, NOTES).read_bytes() == b"not a video"
    assert not (lib["root"] / MATRIX).exists() and not (lib["root"] / NOTES).exists()
    assert set(marks(lib).values()) == {"moved"}
    assert con.execute("SELECT missing FROM files WHERE path=?", (p(lib, MATRIX),)).fetchone()[0] == 1
    assert proposal(con, "The.Matrix")["status"] == "skipped"
    rows = con.execute("SELECT * FROM moves WHERE run_id=? AND trash=1", (rid,)).fetchall()
    assert len(rows) == 3 and all(r["status"] == "moved" for r in rows)
    assert apply.verify(con, rid, log=quiet)["ok"]


def test_apply_with_only_trash_runs(lib):
    run_to_plan(lib)
    con = lib["con"]
    con.execute("UPDATE proposals SET status='skipped'")
    trash.mark(con, lib["cfg"], [p(lib, NOTES)])
    res = execute(lib)
    assert res["moved"] == 0 and res["trashed"] == 1 and res["verify"]["ok"]


def test_a_file_changed_after_marking_is_refused(lib):
    run_to_plan(lib)
    trash.mark(lib["con"], lib["cfg"], [p(lib, NOTES)])
    (lib["root"] / NOTES).write_bytes(b"new content, more bytes")
    res = execute(lib)
    assert res.get("trashed", 0) == 0 and (lib["root"] / NOTES).exists()
    assert "changed since it was marked" in res["trash"]["refused"][0]["reason"]
    assert marks(lib)[p(lib, NOTES)] == "marked"


def test_unknown_downloads_at_apply_move_nothing_to_trash(lib, monkeypatch):
    run_to_plan(lib)
    trash.mark(lib["con"], lib["cfg"], [p(lib, NOTES)])
    monkeypatch.setattr(downloads, "check", lambda cfg: downloads.Active(error="no client"))
    res = execute(lib)
    assert res.get("trashed", 0) == 0 and (lib["root"] / NOTES).exists()


def test_undo_puts_trashed_files_back(lib):
    run_to_plan(lib)
    con = lib["con"]
    before = tree_digest(lib["root"])
    trash.mark(con, lib["cfg"], [p(lib, MYSTERY), p(lib, NOTES)])
    rid = execute(lib)["run_id"]
    assert not (lib["root"] / MYSTERY).exists()
    dry = apply.undo(con, lib["cfg"], rid, log=quiet)
    assert dry["dry_run"] and not (lib["root"] / MYSTERY).exists()
    res = apply.undo(con, lib["cfg"], rid, execute=True, log=quiet)
    assert res["verify"]["ok"]
    assert (lib["root"] / MYSTERY).read_bytes() == b"mystery" * 100
    assert (lib["root"] / NOTES).exists()
    assert tree_digest(lib["root"]) == before
    assert set(marks(lib).values()) == {"restored"}
    assert con.execute("SELECT missing FROM files WHERE path=?", (p(lib, MYSTERY),)).fetchone()[0] == 0
    assert proposal(con, "final_cut")["status"] == "review"
    assert trash.listing(con) == []  # the mark does not come back by itself


def test_a_folder_emptied_by_trash_is_suggested_as_leftover(lib):
    add_files(lib, "old/Some.Clip.2010.mp4")
    run_to_plan(lib)
    lib["con"].execute("UPDATE proposals SET status='skipped'")
    trash.mark(lib["con"], lib["cfg"], [p(lib, "old/Some.Clip.2010.mp4")])
    execute(lib)
    assert p(lib, "old") in {i["path"] for i in leftovers.find(lib["con"], lib["cfg"])["empty"]}


# ── web and CLI ──

def test_web_marks_and_clears_without_touching_files(client, lib):  # noqa: F811
    before = tree_digest(lib["root"])
    r = client.post("/api/trash", json={"paths": [p(lib, NOTES), "/etc/hosts"]}).get_json()
    assert r["marked"] == [p(lib, NOTES)] and r["refused"][0]["path"] == "/etc/hosts"
    assert r["cli"].startswith("mylib trash ")
    assert [m["path"] for m in client.get("/api/trash").get_json()] == [p(lib, NOTES)]
    assert client.get("/api/summary").get_json()["trash"] == 1
    d = client.get("/api/trash/preview?path=" + p(lib, MATRIX)).get_json()
    assert d["sidecars"][0]["path"] == p(lib, SUBS)
    assert client.post("/api/trash/clear", json={"paths": [p(lib, NOTES)]}).get_json()["cleared"] == 1
    assert tree_digest(lib["root"]) == before


def test_web_rejects_bad_bodies(client):  # noqa: F811
    for body in ({}, {"paths": "x"}, {"paths": []}, {"paths": [1]}):
        assert client.post("/api/trash", json=body).status_code == 400
        assert client.post("/api/trash/clear", json=body).status_code == 400
    assert client.delete("/api/trash").status_code == 405


def test_page_has_the_trash_dialog_and_player_button(client):  # noqa: F811
    page = client.get("/").get_data(as_text=True)
    for el in ('id="trash-overlay"', 'id="trash-ok"', 'id="trash-cancel"', 'id="player-trash"'):
        assert el in page
    assert "space comes back only when you remove that trash folder yourself" in " ".join(page.split())


def test_cli_marks_lists_and_clears(lib):
    def mylib(*args):
        return subprocess.run([sys.executable, "-m", "mylibrary.cli", "--config", str(lib["ini"]), *args],
                              capture_output=True, text=True, env={**os.environ, "PYTHONPATH": str(ROOT / "src")})
    r = mylib("trash", p(lib, NOTES))
    assert r.returncode == 0 and "1 files marked" in r.stdout and "Nothing has moved" in r.stdout
    assert p(lib, NOTES) in mylib("trash").stdout
    r = mylib("--json", "trash", "/etc/hosts")
    assert r.returncode == 1 and json.loads(r.stdout)["refused"][0]["path"] == "/etc/hosts"
    assert "1 marks cleared" in mylib("trash", "--clear", p(lib, NOTES)).stdout
    assert (lib["root"] / NOTES).exists()


def test_trash_module_stays_read_only():
    names = set()
    for node in ast.walk(ast.parse(Path(trash.__file__).read_text())):
        if isinstance(node, ast.Import):
            names |= {a.name for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            names |= {a.name for a in node.names} | {node.module}
    assert not names & {"apply", "safefs", "mylibrary.apply", "mylibrary.safefs"}
