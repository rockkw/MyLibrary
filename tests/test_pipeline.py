import io
import json
import os
import sqlite3

import pytest

from conftest import tree_digest
from mylibrary import apply, classify, db, dupes, parse, plan, review, scan


def run_to_plan(lib):
    scan.scan(lib["con"], lib["cfg"], log=lambda *a: None)
    parse.parse_all(lib["con"], lib["cfg"], log=lambda *a: None)
    return plan.make_plan(lib["con"], lib["cfg"], log=lambda *a: None)


def statuses(con, pid):
    return {os.path.basename(r["src"]): r["status"] for r in
            con.execute("SELECT src, status FROM proposals WHERE plan_id=?", (pid,))}


def test_end_to_end_apply_verify_undo(lib):
    con, cfg, root = lib["con"], lib["cfg"], lib["root"]
    before = tree_digest(root)
    p = run_to_plan(lib)
    st = statuses(con, p["plan_id"])
    assert st["The.Matrix.1999.1080p.BluRay.x264-GRP.mkv"] == "auto"
    assert st["alien.dc.mkv"] == "auto"
    assert st["final_cut_v2.mp4"] == "review"
    assert st["Movie.Name.2019.sample.mkv"] == "ignored"

    dry = apply.apply_plan(con, cfg, p["plan_id"], execute=False, log=lambda *a: None)
    assert dry["dry_run"] and tree_digest(root) == before
    assert (root / "incoming/The.Matrix.1999.1080p.BluRay.x264-GRP.mkv").exists()

    res = apply.apply_plan(con, cfg, p["plan_id"], execute=True, log=lambda *a: None)
    assert res["moved"] == 2 and res["verify"]["ok"]
    assert (root / "Movies/The Matrix (1999)/The Matrix (1999).mkv").exists()
    assert (root / "Movies/The Matrix (1999)/The Matrix (1999).en.srt").exists()
    assert (root / "Movies/Alien (1979)/Alien (1979) - Director's Cut.mkv").exists()
    assert (root / "misc/final_cut_v2.mp4").exists()          # unreviewed: untouched
    assert tree_digest(root) == before                         # nothing lost or altered
    snap = root / ".mylibrary-safety" / f"run-{res['run_id']}"
    assert (snap / "incoming/alien (1979)".replace("alien", "Alien") / "alien.dc.mkv").exists()

    u = apply.undo(con, cfg, res["run_id"], execute=True, log=lambda *a: None)
    assert u["restored"] == 3 and u["verify"]["ok"]
    assert (root / "incoming/The.Matrix.1999.1080p.BluRay.x264-GRP.en.srt").exists()
    assert tree_digest(root) == before


def test_rescan_after_apply_is_noop(lib):
    con, cfg = lib["con"], lib["cfg"]
    p = run_to_plan(lib)
    apply.apply_plan(con, cfg, p["plan_id"], execute=True, log=lambda *a: None)
    p2 = run_to_plan(lib)
    st = statuses(con, p2["plan_id"])
    assert st["The Matrix (1999).mkv"] == "noop" and st["Alien (1979) - Director's Cut.mkv"] == "noop"


def test_existing_target_is_conflict_and_untouched(lib):
    con, cfg, root = lib["con"], lib["cfg"], lib["root"]
    t = root / "Movies/The Matrix (1999)/The Matrix (1999).mkv"
    t.parent.mkdir(parents=True)
    t.write_bytes(b"already here")
    p = run_to_plan(lib)
    st = statuses(con, p["plan_id"])
    assert st["The.Matrix.1999.1080p.BluRay.x264-GRP.mkv"] == "conflict"
    # conflicts cannot be approved
    pid = con.execute("SELECT id FROM proposals WHERE status='conflict'").fetchone()[0]
    assert review.decide(con, [pid], "approve") == 0
    apply.apply_plan(con, cfg, p["plan_id"], execute=True, log=lambda *a: None)
    assert t.read_bytes() == b"already here"


def test_two_files_same_target(lib):
    con, root = lib["con"], lib["root"]
    (root / "dl").mkdir()
    (root / "dl/The Matrix 1999.mkv").write_bytes(b"other copy")
    p = run_to_plan(lib)
    counts = p["counts"]
    assert counts.get("conflict") == 1


def test_file_changed_since_scan_is_refused(lib):
    con, cfg, root = lib["con"], lib["cfg"], lib["root"]
    p = run_to_plan(lib)
    f = root / "incoming/Alien (1979)/alien.dc.mkv"
    f.write_bytes(b"changed!")  # same inode, different size
    res = apply.apply_plan(con, cfg, p["plan_id"], execute=True, log=lambda *a: None)
    assert any("changed since scan" in r["reason"] for r in res["refused"])
    assert f.exists()


def test_edit_then_apply(lib):
    con, cfg, root = lib["con"], lib["cfg"], lib["root"]
    p = run_to_plan(lib)
    pid = con.execute("SELECT id FROM proposals WHERE src LIKE '%final_cut_v2%'").fetchone()[0]
    r = review.edit(con, cfg, pid, "Blade Runner: The Final Cut", 1982)
    assert r["status"] == "approved"
    assert r["dst"].endswith("Movies/Blade Runner - The Final Cut (1982)/"
                             "Blade Runner - The Final Cut (1982).mp4")
    apply.apply_plan(con, cfg, p["plan_id"], execute=True, log=lambda *a: None)
    assert os.path.exists(r["dst"])


def test_hostile_title_cannot_escape_root(lib):
    con, cfg, root = lib["con"], lib["cfg"], lib["root"]
    p = run_to_plan(lib)
    pid = con.execute("SELECT id FROM proposals WHERE src LIKE '%final_cut_v2%'").fetchone()[0]
    for title in ("../../../etc/passwd", "..", "/abs/path", "a\\..\\..\\b"):
        r = review.edit(con, cfg, pid, title, 2000)
        if r["dst"]:
            real = os.path.realpath(r["dst"])
            assert real.startswith(str(root / "Movies") + os.sep), (title, real)


@pytest.mark.parametrize("name, edition", [
    ("Alien.1979.Directors.Cut.1080p.BluRay.x264-GRP.mkv", "Director's Cut"),
    ("Blade.Runner.1982.Final.Cut.1080p.mkv", "Final Cut"),
    ("Apocalypse.Now.1979.Redux.mkv", "Redux"),
    ("Aliens.1986.SE.720p.mkv", "Special Edition"),
    ("Star.Wars.1977.Remastered.mkv", "Remastered"),
    ("Movie.2000.Remastered.Directors.Cut.mkv", "Remastered - Director's Cut"),
    ("Movie.2000.25th.Anniversary.Edition.mkv", "25th Anniversary Edition"),
    ("Movie.2000.1080p.BluRay.mkv", None),
    ("Kill.Bill.2003.The.Whole.Bloody.Affair.mkv", None),   # subtitle, not an edition
    ("The.Final.Cut.2004.mkv", None),                       # the film's real title
])
def test_edition_from_filename(lib, name, edition):
    assert parse.identify(f"/x/{name}", None, lib["cfg"])["edition"] == edition


def test_edition_from_film_folder_only(lib):
    cfg = lib["cfg"]
    assert parse.identify("/x/Alien (1979) Directors Cut/alien.1979.mkv", None,
                          cfg)["edition"] == "Director's Cut"
    assert parse.identify("/x/Directors Cuts/alien.1979.mkv", None, cfg)["edition"] is None


@pytest.mark.parametrize("edition", ["Director's Cut", "Final Cut", "Redux", "Special Edition",
                                     "Extended Edition", "Collector's Edition", "Theatrical Cut",
                                     "Fan Edit", "Remastered - Director's Cut",
                                     "25th Anniversary Edition"])
def test_edition_survives_its_own_target_name(lib, edition):
    # otherwise every re-plan would propose another rename
    name = f"Alien (1979) - {edition} - 1080p.mkv"
    assert parse.identify(f"/x/Alien (1979)/{name}", None, lib["cfg"])["edition"] == edition


def test_two_editions_do_not_conflict_or_count_as_duplicates(lib):
    con, cfg, root = lib["con"], lib["cfg"], lib["root"]
    (root / "dl").mkdir()
    (root / "dl/Alien.1979.mkv").write_bytes(b"theatrical")
    (root / "dl/Alien.1979.Final.Cut.mkv").write_bytes(b"final")
    p = run_to_plan(lib)
    assert p["counts"].get("conflict") is None
    res = apply.apply_plan(con, cfg, p["plan_id"], execute=True, log=lambda *a: None)
    assert res["verify"]["ok"]
    folder = root / "Movies/Alien (1979)"
    assert (folder / "Alien (1979).mkv").read_bytes() == b"theatrical"
    assert (folder / "Alien (1979) - Final Cut.mkv").read_bytes() == b"final"
    assert (folder / "Alien (1979) - Director's Cut.mkv").exists()
    assert dupes.find(con)["same_movie"] == []


def test_same_edition_twice_is_still_a_conflict_and_duplicate(lib):
    con, root = lib["con"], lib["root"]
    (root / "dl").mkdir()
    (root / "dl/Alien.1979.Directors.Cut.mkv").write_bytes(b"other copy")
    p = run_to_plan(lib)
    assert p["counts"].get("conflict") == 1
    assert [g["edition"] for g in dupes.find(con)["same_movie"]] == ["Director's Cut"]


def test_edit_keeps_sets_and_clears_edition(lib):
    con, cfg = lib["con"], lib["cfg"]
    p = run_to_plan(lib)
    pid = con.execute("SELECT id FROM proposals WHERE src LIKE '%alien.dc%'").fetchone()[0]
    assert review.edit(con, cfg, pid, "Alien", 1979)["dst"].endswith(
        "Alien (1979) - Director's Cut.mkv")
    assert review.edit(con, cfg, pid, "Alien", 1979, "Final Cut")["dst"].endswith(
        "Alien (1979) - Final Cut.mkv")
    assert review.edit(con, cfg, pid, "Alien", 1979, "")["dst"].endswith("/Alien (1979).mkv")
    assert statuses(con, p["plan_id"])["alien.dc.mkv"] == "approved"


def test_hostile_edition_cannot_escape_root(lib):
    con, cfg, root = lib["con"], lib["cfg"], lib["root"]
    run_to_plan(lib)
    pid = con.execute("SELECT id FROM proposals WHERE src LIKE '%alien.dc%'").fetchone()[0]
    for edition in ("../../../etc/passwd", "..", "/abs/path", "{title}"):
        dst = review.edit(con, cfg, pid, "Alien", 1979, edition)["dst"]
        assert os.path.dirname(dst) == str(root / "Movies/Alien (1979)"), (edition, dst)


def test_old_database_gains_edition_column(tmp_path):
    f = tmp_path / "old.db"
    old = sqlite3.connect(f)
    old.execute("CREATE TABLE parses (file_id INTEGER PRIMARY KEY, title TEXT)")
    old.commit()
    old.close()
    con = db.connect(f)
    assert "edition" in {r["name"] for r in con.execute("PRAGMA table_info(parses)")}


class FakeResp(io.BytesIO):
    def __enter__(self): return self
    def __exit__(self, *a): pass


def test_llm_answer_is_capped_and_stored(lib):
    con, cfg = lib["con"], lib["cfg"]
    cfg.ollama_enabled = True
    run_to_plan(lib)
    seen = {}

    def opener(req, timeout):
        seen["body"] = json.loads(req.data)
        ans = {"title": "Blade Runner", "year": 1982, "kind": "movie", "confidence": 0.99}
        return FakeResp(json.dumps({"message": {"content": json.dumps(ans)}}).encode())

    res = classify.classify_all(con, cfg, log=lambda *a: None, opener=opener)
    assert res["asked"] == 1
    assert seen["body"]["format"]["type"] == "object"
    row = con.execute("SELECT * FROM parses WHERE source='llm'").fetchone()
    assert row["title"] == "Blade Runner" and row["confidence"] == cfg.llm_max_conf
    p = plan.make_plan(con, cfg, log=lambda *a: None)
    assert statuses(con, p["plan_id"])["final_cut_v2.mp4"] == "review"  # never auto


def test_llm_garbage_is_ignored(lib):
    con, cfg = lib["con"], lib["cfg"]
    cfg.ollama_enabled = True
    run_to_plan(lib)
    opener = lambda req, timeout: FakeResp(b'{"message":{"content":"not json"}}')
    res = classify.classify_all(con, cfg, log=lambda *a: None, opener=opener)
    assert res["errors"] == 1 and res["asked"] == 0


def test_decisions_carry_over_to_new_plan(lib):
    con, cfg = lib["con"], lib["cfg"]
    p = run_to_plan(lib)
    pid = con.execute("SELECT id FROM proposals WHERE src LIKE '%alien.dc%'").fetchone()[0]
    review.decide(con, [pid], "skip")
    p2 = plan.make_plan(con, cfg, log=lambda *a: None)
    assert statuses(con, p2["plan_id"])["alien.dc.mkv"] == "skipped"


def test_missing_root_is_skipped_not_marked_missing(lib, tmp_path):
    con, cfg = lib["con"], lib["cfg"]
    run_to_plan(lib)
    res = scan.scan(con, cfg, roots=[tmp_path / "unplugged"], log=lambda *a: None)
    assert res["skipped_roots"]
    assert con.execute("SELECT COUNT(*) FROM files WHERE missing=1").fetchone()[0] == 0
