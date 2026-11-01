import io
import json
import os
import shutil
import sqlite3
import time
import urllib.error

import pytest

from conftest import tree_digest
from mylibrary import apply, classify, cli, config, db, downloads, dupes, leftovers, parse
from mylibrary import plan, review, scan


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


@pytest.mark.parametrize("height,width,label", [
    (804, 1920, " - 1080p"), (1600, 3840, " - 2160p"), (536, 1280, " - 720p"),
    (1080, 1920, " - 1080p"), (720, 1280, " - 720p"), (544, 720, " - 576p"),
    (480, 854, " - 480p"), (1080, None, " - 1080p"), (None, None, ""),
])
def test_quality_counts_cropped_widescreen(height, width, label):
    assert plan.quality(height, width) == label


def _cfg_stub():
    from types import SimpleNamespace
    return SimpleNamespace(ignore_words=["sample", "trailer"], min_duration_min=40, auto_approve=0.90)


def test_subtitle_after_dash_stays_in_title():
    name = "Spider-Man - Brand New Day (2026) AC3 5.1 1080p H265.mkv"
    r = parse.identify(f"/lib/{name}", 8700, _cfg_stub())
    assert r["title"] == "Spider-Man Brand New Day" and r["year"] == 2026


def test_edition_after_dash_is_not_title():
    r = parse.identify("/lib/Blade Runner - The Final Cut (1982).mkv", 7000, _cfg_stub())
    assert r["title"] == "Blade Runner" and r["edition"] == "The Final Cut"


def test_numbered_file_in_film_folder_needs_review():
    r = parse.identify("/lib/AB12____300_2026/01.mp4", 9000, _cfg_stub())
    assert r["flagged"] == 1 and "numbered file" in r["reasons"]


def test_named_file_in_film_folder_is_still_confident():
    r = parse.identify("/lib/Alien (1979)/alien.dc.mkv", 7000, _cfg_stub())
    assert r["confidence"] == 0.95 and r["flagged"] == 0


def test_llm_unknown_keeps_parser_episode(lib):
    con, cfg = lib["con"], lib["cfg"]
    cfg.ollama_enabled = True
    run_to_plan(lib)
    con.execute("UPDATE parses SET kind='episode' WHERE flagged=1")
    content = json.dumps({"title": "X", "year": None, "kind": "unknown", "confidence": 0.5})
    body = json.dumps({"message": {"content": content}}).encode()
    classify.classify_all(con, cfg, log=lambda *a: None, opener=lambda req, timeout: FakeResp(body))
    assert con.execute("SELECT kind FROM parses WHERE source='llm'").fetchone()["kind"] == "episode"


# TV episodes

EP_DIR = "Agatha.All.Along.S01E04.1080p.WEB.H264-GRP"


def add_files(lib, *names):
    for name in names:
        p = lib["root"] / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(name.encode() * 20)


def proposal(con, like):
    return con.execute("SELECT * FROM proposals WHERE plan_id=(SELECT MAX(id) FROM plans) "
                       "AND src LIKE ?", (f"%{like}%",)).fetchone()


def reparse(lib):
    scan.scan(lib["con"], lib["cfg"], log=lambda *a: None)
    parse.parse_all(lib["con"], lib["cfg"], redo=True, log=lambda *a: None)
    return plan.make_plan(lib["con"], lib["cfg"], log=lambda *a: None)


def test_episode_is_parsed_and_needs_no_llm():
    r = parse.identify("/lib/The.Office.US.S03E01.Gay.Witch.Hunt.1080p.mkv", 1300, _cfg_stub())
    assert r["kind"] == "episode" and r["confidence"] == 0.95 and r["flagged"] == 0
    assert (r["season"], r["episode"], r["episode_last"]) == (3, 1, None)
    assert r["title"] == "The Office (US)" and r["episode_title"] == "Gay Witch Hunt"


def test_lowercase_episode_takes_series_casing_from_folder():
    r = parse.identify(f"/lib/{EP_DIR}/agatha.all.along.s01e04.1080p.mkv", 2400, _cfg_stub())
    assert r["title"] == "Agatha All Along"


def test_lowercase_episode_without_folder_is_capitalized():
    assert parse.identify("/lib/agatha.all.along.s01e04.mkv", 2400, _cfg_stub())["title"] \
        == "Agatha All Along"


def test_bare_episode_takes_series_from_folders():
    r = parse.identify("/lib/Agatha All Along/Season 01/S01E04.mkv", 2400, _cfg_stub())
    assert (r["title"], r["season"], r["episode"]) == ("Agatha All Along", 1, 4)


def test_season_zero_specials_are_numbered():
    r = parse.identify("/lib/Show.S00E02.mkv", 2400, _cfg_stub())
    assert (r["season"], r["episode"], r["confidence"]) == (0, 2, 0.95)


def test_multi_episode_file_is_not_confident():
    r = parse.identify("/lib/Show.S01E01-E03.mkv", 4000, _cfg_stub())
    assert (r["episode"], r["episode_last"]) == (1, 3)
    assert r["confidence"] < 0.90 and "multi-episode" in r["reasons"]


@pytest.mark.parametrize("name", ["Hookup.Episode.473.Some.Name.mp4", "Show.S02.Complete.mkv"])
def test_episode_without_both_numbers_is_not_confident(name):
    r = parse.identify(f"/lib/{name}", 2400, _cfg_stub())
    assert r["kind"] == "episode" and r["episode"] is None and r["confidence"] < 0.90


@pytest.mark.parametrize("name", ["Show.S01E01.sample.mkv", "Show.S01E01.trailer.mkv"])
def test_episode_samples_stay_samples(name):
    assert parse.identify(f"/lib/{name}", 60, _cfg_stub())["kind"] == "sample"


def test_star_wars_episode_is_still_a_movie():
    r = parse.identify("/lib/Star.Wars.Episode.IV.A.New.Hope.1977.1080p.mkv", 7500, _cfg_stub())
    assert r["kind"] == "movie" and r["year"] == 1977


def test_episode_gets_a_tv_target_and_auto_status(lib):
    add_files(lib, "tv/Show.Name.2019.S01E02.The.Title.mkv")
    run_to_plan(lib)
    p = proposal(lib["con"], "Show.Name")
    assert p["status"] == "auto"
    assert p["dst"].endswith(
        "Shows/Show Name (2019)/Season 01/Show Name (2019) - S01E02 - The Title.mkv")


def test_tv_layout_is_configurable(lib):
    lib["cfg"].tv_dir = "TV"
    lib["cfg"].tv_layout = "{title}/{season}x{episode}"
    add_files(lib, "tv/Show.S01E02.mkv")
    run_to_plan(lib)
    assert proposal(lib["con"], "Show.S01E02")["dst"].endswith("TV/Show/01x02.mkv")


def test_episode_quality_uses_width(lib):
    row = {"path": "/r/a.mkv", "root": "/r", "height": 804, "width": 1920}
    p = dict(title="Show", year=None, season=1, episode=2, episode_last=None, episode_title=None)
    assert plan.episode_target(lib["cfg"], row, p) == \
        "/r/Shows/Show/Season 01/Show - S01E02 - 1080p.mkv"


def test_uncertain_episodes_go_to_review_never_auto(lib):
    add_files(lib, "tv/Show.S01E01-E03.mkv", "tv/Hookup.Episode.473.Some.Name.mp4",
              "tv/Other.S02.Complete.mkv")
    run_to_plan(lib)
    multi = proposal(lib["con"], "S01E01-E03")
    assert multi["status"] == "review" and multi["dst"].endswith("Show - S01E01-E03.mkv")
    for like in ("Hookup", "Other.S02"):
        p = proposal(lib["con"], like)
        assert p["status"] == "review" and p["dst"] is None
        assert "needs series, season and episode" in p["reason"]


def test_same_episode_twice_conflicts(lib):
    add_files(lib, "a/Show.S01E02.mkv", "b/Show.S01E02.mkv")
    run_to_plan(lib)
    counts = [proposal(lib["con"], d)["status"] for d in ("/a/Show", "/b/Show")]
    assert sorted(counts) == ["auto", "conflict"]


def test_apply_moves_episode_and_sidecar_then_rescan_is_noop(lib):
    con, cfg = lib["con"], lib["cfg"]
    add_files(lib, "tv/Show.Name.2019.S01E02.The.Title.mkv",
              "tv/Show.Name.2019.S01E02.The.Title.en.srt")
    run_to_plan(lib)
    before = tree_digest(lib["root"])
    res = apply.apply_plan(con, cfg, plan.current_plan(con), execute=True, log=lambda *a: None)
    assert res["count"] == 3  # the episode and the two fixture movies
    assert res["sidecar_count"] >= 1
    new = lib["root"] / "Shows/Show Name (2019)/Season 01"
    assert (new / "Show Name (2019) - S01E02 - The Title.mkv").exists()
    assert (new / "Show Name (2019) - S01E02 - The Title.en.srt").exists()
    assert not (lib["root"] / "tv/Show.Name.2019.S01E02.The.Title.mkv").exists()
    assert tree_digest(lib["root"]) == before
    reparse(lib)
    assert proposal(con, "Season 01")["status"] == "noop"


@pytest.mark.parametrize("name", [
    "Show.S01E02.The.Title.mkv", "Show.S01E02.mkv", "Show.S01E01-E03.mkv",
    "The.Office.US.S03E01.Gay.Witch.Hunt.mkv"])
def test_new_episode_names_parse_back_to_themselves(lib, name):
    con = lib["con"]
    add_files(lib, f"tv/{name}")
    run_to_plan(lib)
    first = proposal(con, "/tv/")
    new = first["dst"]
    r = parse.identify(new, 2400, _cfg_stub())
    row = {"path": new, "root": str(lib["root"]), "height": None, "width": None}
    assert plan.episode_target(lib["cfg"], row, r) == new


def _first_movie(con):
    return con.execute("SELECT id FROM proposals WHERE plan_id=(SELECT MAX(id) FROM plans) "
                       "AND src LIKE '%Matrix%mkv'").fetchone()["id"]


def test_edit_turns_a_file_into_an_episode(lib):
    con, cfg = lib["con"], lib["cfg"]
    run_to_plan(lib)
    res = review.edit(con, cfg, _first_movie(con), "Show", None, season=2, episode=5,
                      episode_last=6, episode_title="Part One")
    assert res["status"] == "approved"
    assert res["dst"].endswith("Shows/Show/Season 02/Show - S02E05-E06 - Part One.mkv")


def test_edit_episode_keeps_numbers_and_can_become_a_movie(lib):
    con, cfg = lib["con"], lib["cfg"]
    add_files(lib, "tv/Show.S01E02.Old.mkv")
    run_to_plan(lib)
    pid = proposal(con, "Show.S01E02")["id"]
    assert review.edit(con, cfg, pid, "Better Name", None)["dst"].endswith(
        "Shows/Better Name/Season 01/Better Name - S01E02 - Old.mkv")
    res = review.edit(con, cfg, pid, "A Film", 2001, kind="movie")
    assert "Movies/A Film (2001)/" in res["dst"]
    row = con.execute("SELECT kind, season, episode, episode_title FROM parses WHERE title='A Film'"
                      ).fetchone()
    assert tuple(row) == ("movie", None, None, None)


def test_edit_episode_without_numbers_stays_in_review(lib):
    con, cfg = lib["con"], lib["cfg"]
    add_files(lib, "tv/Hookup.Episode.473.Some.Name.mp4")
    run_to_plan(lib)
    pid = proposal(con, "Hookup")["id"]
    res = review.edit(con, cfg, pid, "Hookup", None, kind="episode")
    assert res["status"] == "review" and res["dst"] is None
    res = review.edit(con, cfg, pid, "Hookup", None, season=1, episode=473)
    assert res["status"] == "approved" and res["dst"].endswith("Hookup - S01E473 - Some Name.mp4")


@pytest.mark.parametrize("kwargs", [{"kind": "show"}, {"season": -1}, {"episode": -2}])
def test_edit_rejects_bad_input(lib, kwargs):
    con, cfg = lib["con"], lib["cfg"]
    run_to_plan(lib)
    with pytest.raises(ValueError):
        review.edit(con, cfg, _first_movie(con), "X", None, **kwargs)


def test_old_database_gains_episode_columns(tmp_path):
    f = tmp_path / "old.db"
    old = sqlite3.connect(f)
    old.execute("CREATE TABLE parses (file_id INTEGER PRIMARY KEY, title TEXT)")
    old.commit(); old.close()
    con = db.connect(f)
    cols = {r["name"] for r in con.execute("PRAGMA table_info(parses)")}
    assert {"edition", "season", "episode", "episode_last", "episode_title"} <= cols


# Music

TAGS = {"artist": "Camila Cabello feat. Young Thug", "album_artist": "Camila Cabello",
        "album": "Camila", "title": "Havana", "track": "3/11", "disc": "1/1",
        "date": "2017-08-03"}


def probe_with(monkeypatch, tags_by_name):
    """Serve tags from a fake ffprobe, so these tests need no real audio files."""
    def fake(path):
        return {"tags": tags_by_name.get(os.path.basename(path), {}), "duration": 200.0}
    monkeypatch.setattr(scan, "ffprobe", fake)


def test_tags_are_read_from_format_and_stream(monkeypatch):
    out = json.dumps({"format": {"tags": {"TITLE": "From Format", "Album": "A"}},
                      "streams": [{"codec_type": "audio", "tags": {"ARTIST": "From Stream"}}]})
    class Done:
        returncode, stdout, stderr = 0, out, ""
    monkeypatch.setattr(scan.subprocess, "run", lambda *a, **k: Done)
    assert scan.ffprobe("x.ogg")["tags"] == {"title": "From Format", "album": "A",
                                             "artist": "From Stream"}


def test_album_artist_beats_track_artist():
    r = parse.identify_music(TAGS)
    assert r["kind"] == "music" and r["flagged"] == 0 and r["confidence"] == 0.95
    assert (r["artist"], r["album"], r["title"]) == ("Camila Cabello", "Camila", "Havana")
    assert (r["track"], r["disc"], r["year"]) == (3, None, 2017)


def test_compilation_files_under_various_artists():
    tags = {**TAGS, "compilation": "1"}
    del tags["album_artist"]
    assert parse.identify_music(tags)["artist"] == "Various Artists"


def test_disc_is_kept_only_for_multi_disc_albums():
    assert parse.identify_music({**TAGS, "disc": "2/2"})["disc"] == 2
    assert parse.identify_music({**TAGS, "disc": "1"})["disc"] is None


@pytest.mark.parametrize("change,missing", [
    ({"album": ""}, "album"), ({"title": None}, "title"),
    ({"artist": "Unknown Artist", "album_artist": ""}, "artist"),
    ({"album": "Unknown Album"}, "album"), ({"title": "Track 01"}, "title")])
def test_missing_or_placeholder_tags_are_not_confident(change, missing):
    r = parse.identify_music({**TAGS, **change})
    assert r["confidence"] < 0.90 and f"missing tags: {missing}" in r["reasons"]


def test_music_gets_target_and_auto_status(lib, monkeypatch):
    probe_with(monkeypatch, {"havana.MP3": TAGS})
    add_files(lib, "dl/havana.MP3")
    run_to_plan(lib)
    p = proposal(lib["con"], "havana")
    assert p["status"] == "auto"
    assert p["dst"].endswith("Music/Camila Cabello/Camila/03 - Havana.mp3")


def test_multi_disc_track_name(lib, monkeypatch):
    probe_with(monkeypatch, {"a.flac": {**TAGS, "disc": "2/2"}})
    add_files(lib, "dl/a.flac")
    run_to_plan(lib)
    assert proposal(lib["con"], "a.flac")["dst"].endswith("Camila/2-03 - Havana.flac")


def test_incomplete_music_goes_to_review(lib, monkeypatch):
    probe_with(monkeypatch, {"a.mp3": {"artist": "X"}, "b.mp3": {**TAGS, "track": ""}})
    add_files(lib, "dl/a.mp3", "dl/b.mp3")
    run_to_plan(lib)
    a, b = proposal(lib["con"], "a.mp3"), proposal(lib["con"], "b.mp3")
    assert a["status"] == "review" and a["dst"] is None
    assert "needs artist, album and title" in a["reason"]
    assert b["status"] == "review" and b["dst"].endswith("Camila/Havana.mp3")


def test_hostile_tags_stay_inside_the_music_folder(lib, monkeypatch):
    bad = {"album_artist": "../../../etc", "album": "/abs/path", "title": "a\x00b/c" + "é" * 300,
           "track": "1"}
    probe_with(monkeypatch, {"a.mp3": bad})
    add_files(lib, "dl/a.mp3")
    run_to_plan(lib)
    dst = proposal(lib["con"], "a.mp3")["dst"]
    music = str(lib["root"] / "Music") + os.sep
    assert os.path.normpath(dst).startswith(music)
    assert os.path.normpath(dst) == dst
    assert len(dst[len(music):].split(os.sep)) == 3
    assert all(len(part.encode()) <= 255 for part in dst.split(os.sep))


def test_tag_of_only_dots_has_no_target(lib, monkeypatch):
    probe_with(monkeypatch, {"a.mp3": {**TAGS, "album": ".."}})
    add_files(lib, "dl/a.mp3")
    run_to_plan(lib)
    assert proposal(lib["con"], "a.mp3")["dst"] is None


def test_music_applies_then_rescans_as_noop(lib, monkeypatch):
    con, cfg = lib["con"], lib["cfg"]
    probe_with(monkeypatch, {"a.mp3": TAGS, "03 - Havana.mp3": TAGS})
    add_files(lib, "dl/a.mp3")
    run_to_plan(lib)
    before = tree_digest(lib["root"])
    apply.apply_plan(con, cfg, plan.current_plan(con), execute=True, log=lambda *a: None)
    assert (lib["root"] / "Music/Camila Cabello/Camila/03 - Havana.mp3").exists()
    assert tree_digest(lib["root"]) == before
    reparse(lib)
    assert proposal(con, "Music/")["status"] == "noop"


def test_edit_fixes_music(lib, monkeypatch):
    con, cfg = lib["con"], lib["cfg"]
    probe_with(monkeypatch, {"a.mp3": {"title": "Song"}})
    add_files(lib, "dl/a.mp3")
    run_to_plan(lib)
    pid = proposal(con, "a.mp3")["id"]
    res = review.edit(con, cfg, pid, "Song", None, artist="Band", album="Debut", track=2)
    assert res["status"] == "approved" and res["dst"].endswith("Music/Band/Debut/02 - Song.mp3")
    res = review.edit(con, cfg, pid, "Song 2", None)
    assert res["dst"].endswith("Music/Band/Debut/02 - Song 2.mp3")
    res = review.edit(con, cfg, pid, "Song", None, kind="music", artist="")
    assert res["status"] == "review" and res["dst"] is None


# Software

def sw_cfg(**kw):
    from types import SimpleNamespace
    return SimpleNamespace(software_dir="Software", auto_approve=0.90, **kw)


@pytest.mark.parametrize("name,title", [
    ("VSCode-1.85.2-x64-setup.exe", "VSCode"), ("7-Zip-23.01-x64.exe", "7 Zip"),
    ("code_1.85.2-1705561292_amd64.deb", "Code"), ("Blender.4.0.2.Linux.AppImage", "Blender")])
def test_installer_with_name_and_version_is_confident(name, title):
    r = parse.identify_software(f"/r/dl/{name}", "/r", sw_cfg())
    assert (r["title"], r["kind"], r["flagged"]) == (title, "software", 0)
    assert r["confidence"] >= 0.90


@pytest.mark.parametrize("name", ["Tool.exe", "Tool-x64.exe", "NaXQGMOqsSNirTJ.rar",
                                  "Tool-1.2.3.zip", "Tool-1.2.3.tar.gz", "Tool.7z"])
def test_everything_else_needs_a_human(name):
    r = parse.identify_software(f"/r/dl/{name}", "/r", sw_cfg())
    assert r["confidence"] < 0.90 and r["flagged"] == 1


def test_generic_installer_uses_folder_and_is_not_confident():
    r = parse.identify_software("/r/Adobe.Photoshop.2024.v25.0/setup.exe", "/r", sw_cfg())
    assert r["title"].startswith("Adobe Photoshop") and r["flagged"] == 1


def test_generic_installer_at_root_has_no_title():
    assert parse.identify_software("/r/setup.exe", "/r", sw_cfg())["title"] is None


def test_tar_gz_name_is_stripped():
    r = parse.identify_software("/r/dl/Tool-1.2.3.tar.gz", "/r", sw_cfg())
    assert r["title"] == "Tool"


def test_multi_part_archive_is_called_out():
    r = parse.identify_software("/r/dl/Big.part01.rar", "/r", sw_cfg())
    assert r["title"] == "Big" and "multi-part" in r["reasons"]


@pytest.mark.parametrize("name,width,verdict", [
    ("Alien.1979.iso", None, False), ("Movie.2019.1080p.BluRay.iso", None, False),
    ("The.Final.Countdown.1980.iso", None, False), ("Anything.iso", 1920, False),
    ("ubuntu-22.04-desktop-amd64.iso", None, True), ("Windows-Server-x64.iso", None, True),
    ("Windows.11.2023.x64.iso", None, None), ("backup.iso", None, None)])
def test_iso_is_sorted_or_left_unclear(name, width, verdict):
    assert parse.iso_is_software(f"/r/{name}", width) is verdict


def test_software_is_scanned_but_never_probed(lib, monkeypatch):
    calls = []
    real = scan.ffprobe
    monkeypatch.setattr(scan, "ffprobe", lambda p: calls.append(p) or real(p))
    add_files(lib, "dl/Tool-1.2.3-x64.exe", "dl/Thing.zip", "dl/Alien.1979.iso")
    run_to_plan(lib)
    assert not any(c.endswith((".exe", ".zip")) for c in calls)
    assert any(c.endswith(".iso") for c in calls)
    row = lib["con"].execute("SELECT quick_hash FROM files WHERE path LIKE '%.exe'").fetchone()
    assert row["quick_hash"]


def test_software_plan_keeps_the_original_file_name(lib):
    con = lib["con"]
    add_files(lib, "dl/Tool-1.2.3-x64.exe", "dl/Thing-1.0.zip", "dl/Stuff-1.0.TAR.GZ")
    run_to_plan(lib)
    p = proposal(con, "Tool-1.2.3")
    assert p["status"] == "auto"
    assert p["dst"].endswith("Software/Tool/Tool-1.2.3-x64.exe")
    assert proposal(con, "Thing-1.0")["status"] == "review"
    assert proposal(con, "Stuff-1.0")["dst"].endswith("Software/Stuff/Stuff-1.0.TAR.GZ")


def test_custom_software_layout(lib):
    lib["cfg"].software_dir, lib["cfg"].software_layout = "Apps", "{title}/{filename}"
    add_files(lib, "dl/Tool-1.2.3-x64.exe")
    run_to_plan(lib)
    assert "/Apps/Tool/" in proposal(lib["con"], "Tool-1.2.3")["dst"]


def _ask_llm(lib, answer):
    con, cfg = lib["con"], lib["cfg"]
    cfg.ollama_enabled = True
    seen = {}
    def opener(req, timeout):
        seen["body"] = json.loads(req.data)
        return FakeResp(json.dumps({"message": {"content": json.dumps(answer)}}).encode())
    classify.classify_all(con, cfg, log=lambda *a: None, opener=opener)
    return seen["body"]


def test_llm_software_title_always_needs_review(lib):
    con, cfg = lib["con"], lib["cfg"]
    cfg.llm_max_conf = 0.99  # even a misconfigured cap must not let it through
    add_files(lib, "dl/Thing.zip")
    run_to_plan(lib)
    con.execute("UPDATE parses SET flagged=(kind='software')")
    body = _ask_llm(lib, {"title": "Thing Pro", "year": 2020, "kind": "movie", "confidence": 0.99})
    assert "software" in body["messages"][0]["content"]
    r = con.execute("SELECT * FROM parses WHERE source='llm'").fetchone()
    assert (r["title"], r["kind"], r["year"]) == ("Thing Pro", "software", None)
    plan.make_plan(con, cfg, log=lambda *a: None)
    p = proposal(con, "Thing.zip")
    assert p["status"] == "review" and "suggested by the LLM" in p["reason"]
    assert p["dst"].endswith("Software/Thing Pro/Thing.zip")


def test_software_applies_then_rescans_as_noop(lib):
    con, cfg = lib["con"], lib["cfg"]
    add_files(lib, "dl/Tool-1.2.3-x64.exe", "dl/setup.exe")
    run_to_plan(lib)
    review.edit(con, cfg, proposal(con, "dl/setup.exe")["id"], "My Tool", None, kind="software")
    before = tree_digest(lib["root"])
    apply.apply_plan(con, cfg, plan.current_plan(con), execute=True, log=lambda *a: None)
    assert (lib["root"] / "Software/My Tool/setup.exe").exists()
    assert tree_digest(lib["root"]) == before
    reparse(lib)
    assert proposal(con, "Software/My Tool/setup.exe")["status"] == "noop"
    assert proposal(con, "Software/Tool/")["status"] == "noop"


def test_edit_can_turn_an_unclear_iso_into_a_movie(lib):
    con, cfg = lib["con"], lib["cfg"]
    add_files(lib, "dl/backup.iso")
    run_to_plan(lib)
    p = proposal(con, "backup.iso")
    assert p["status"] == "review" and "unclear ISO" in p["reason"]
    res = review.edit(con, cfg, p["id"], "A Film", 1999, kind="movie")
    assert "Movies/A Film (1999)/" in res["dst"]


def test_tar_gz_is_matched_and_plain_gz_is_not(lib):
    add_files(lib, "dl/Tool-1.0.tar.gz", "dl/log.gz")
    run_to_plan(lib)
    assert proposal(lib["con"], "Tool-1.0") and not proposal(lib["con"], "log.gz")


def test_old_database_gains_music_columns(tmp_path):
    f = tmp_path / "old.db"
    old = sqlite3.connect(f)
    old.execute("CREATE TABLE files (id INTEGER PRIMARY KEY, path TEXT)")
    old.execute("CREATE TABLE parses (file_id INTEGER PRIMARY KEY, title TEXT)")
    old.commit(); old.close()
    con = db.connect(f)
    assert "tags" in {r["name"] for r in con.execute("PRAGMA table_info(files)")}
    assert {"artist", "album", "track", "disc"} <= {
        r["name"] for r in con.execute("PRAGMA table_info(parses)")}


# Files still downloading

def test_recent_file_needs_review_then_auto_once_settled(lib):
    con, cfg = lib["con"], lib["cfg"]
    cfg.settle_minutes = 10
    add_files(lib, "dl/Fresh.Show.S01E01.mkv")
    run_to_plan(lib)
    p = proposal(con, "Fresh.Show")
    assert p["status"] == "review" and "may still be downloading" in p["reason"]
    old = time.time() - 3600
    os.utime(p["src"], (old, old))
    reparse(lib)
    assert proposal(con, "Fresh.Show")["status"] == "auto"


def test_apply_refuses_preallocated_file_written_since_scan(lib):
    con, cfg = lib["con"], lib["cfg"]
    add_files(lib, "dl/Show.S01E01.mkv")
    run_to_plan(lib)
    src = lib["root"] / "dl/Show.S01E01.mkv"
    size = src.stat().st_size
    src.write_bytes(b"y" * size)  # same size and inode, as a filling torrent piece would be
    later = time.time() + 30
    os.utime(src, (later, later))
    res = apply.apply_plan(con, cfg, plan.current_plan(con), execute=False, log=lambda *a: None)
    assert any("changed since scan" in r["reason"] for r in res["refused"])
    assert src.exists()


# Transmission

class FakeRPC:
    """Speaks Transmission's session-id handshake, then answers torrent-get."""

    def __init__(self, torrents):
        self.torrents, self.requests = torrents, []

    def __call__(self, req, timeout):
        self.requests.append(req)
        if req.get_header("X-transmission-session-id") != "abc":
            raise urllib.error.HTTPError(req.full_url, 409, "Conflict",
                                         {"X-Transmission-Session-Id": "abc"}, io.BytesIO())
        body = {"result": "success", "arguments": {"torrents": self.torrents}}
        return FakeResp(json.dumps(body).encode())


def torrent(lib, name, left=0, done=None, **extra):
    done = {} if done is None else done
    files = [{"name": n, "length": 10, "bytesCompleted": done.get(n, 10)} for n in name]
    return {"name": "T", "downloadDir": str(lib["root"]), "files": files, "leftUntilDone": left,
            "percentDone": 0 if left else 1, "metadataPercentComplete": 1, "status": 4, **extra}


REAL_CHECK = downloads.check


def transmission(lib, monkeypatch, torrents=None, error=None):
    lib["cfg"].transmission_enabled = True
    def fake(cfg, opener=None):
        if error:
            return downloads.Active(error=error)
        return REAL_CHECK(cfg, opener=FakeRPC(torrents))
    monkeypatch.setattr(downloads, "check", fake)


def test_disabled_client_is_never_contacted(lib):
    def boom(req, timeout):
        raise AssertionError("contacted")
    assert downloads.check(lib["cfg"], opener=boom) == downloads.Active()


def test_handshake_and_login_are_sent(lib):
    cfg = lib["cfg"]
    cfg.transmission_enabled, cfg.transmission_user, cfg.transmission_password = True, "u", "p"
    rpc = FakeRPC([])
    assert downloads.check(cfg, opener=rpc).error is None
    assert len(rpc.requests) == 2
    assert rpc.requests[1].get_header("Authorization") == "Basic dTpw"


@pytest.mark.parametrize("change", [
    {"left": 5}, {"status": 2}, {"metadataPercentComplete": 0.5}])
def test_whole_torrent_counts_until_it_finishes(lib, change):
    lib["cfg"].transmission_enabled = True
    t = torrent(lib, ["a/1.mkv", "a/2.mkv"], **change)
    res = downloads.check(lib["cfg"], opener=FakeRPC([t]))
    assert res.paths == {str(lib["root"] / "a/1.mkv"), str(lib["root"] / "a/2.mkv")}


def test_partial_file_counts_even_if_torrent_says_done(lib):
    lib["cfg"].transmission_enabled = True
    t = torrent(lib, ["1.mkv"], done={"1.mkv": 3})
    assert downloads.check(lib["cfg"], opener=FakeRPC([t])).paths


def test_finished_seeding_torrent_is_free_to_process(lib):
    lib["cfg"].transmission_enabled = True
    assert not downloads.check(lib["cfg"], opener=FakeRPC([torrent(lib, ["1.mkv"])])).paths


def test_paused_unfinished_torrent_still_counts(lib):
    lib["cfg"].transmission_enabled = True
    t = torrent(lib, ["1.mkv"], left=5, status=0)
    assert downloads.check(lib["cfg"], opener=FakeRPC([t])).paths


def test_login_failure_is_an_error_without_the_password(lib):
    cfg = lib["cfg"]
    cfg.transmission_enabled, cfg.transmission_password = True, "hunter2"
    def refuse(req, timeout):
        raise urllib.error.HTTPError(req.full_url, 401, "Unauthorized", {}, io.BytesIO())
    res = downloads.check(cfg, opener=refuse)
    assert "login refused" in res.error and "hunter2" not in res.error


def test_unreachable_client_is_an_error(lib):
    lib["cfg"].transmission_enabled = True
    def down(req, timeout):
        raise urllib.error.URLError("connection refused")
    assert "cannot check downloads" in downloads.check(lib["cfg"], opener=down).error


def test_unfinished_file_is_not_scanned_then_arrives_when_done(lib, monkeypatch):
    con = lib["con"]
    add_files(lib, "dl/Show.S01E01.mkv")
    transmission(lib, monkeypatch, [torrent(lib, ["dl/Show.S01E01.mkv"], left=5)])
    res = scan.scan(con, lib["cfg"], log=lambda *a: None)
    assert res["downloading"] == 1
    assert not con.execute("SELECT 1 FROM files WHERE path LIKE '%Show%'").fetchone()
    transmission(lib, monkeypatch, [torrent(lib, ["dl/Show.S01E01.mkv"])])
    run_to_plan(lib)
    assert proposal(con, "Show.S01E01")["status"] == "auto"


def test_known_file_back_in_a_torrent_is_ignored_and_not_marked_missing(lib, monkeypatch):
    con = lib["con"]
    add_files(lib, "dl/Show.S01E01.mkv")
    run_to_plan(lib)
    transmission(lib, monkeypatch, [torrent(lib, ["dl/Show.S01E01.mkv"], left=5)])
    run_to_plan(lib)
    p = proposal(con, "Show.S01E01")
    assert p["status"] == "ignored" and "still downloading" in p["reason"]
    assert con.execute("SELECT missing FROM files WHERE path LIKE '%Show%'").fetchone()[0] == 0


def test_apply_refuses_a_file_that_started_downloading_after_the_plan(lib, monkeypatch):
    con, cfg = lib["con"], lib["cfg"]
    add_files(lib, "dl/Show.S01E01.mkv")
    run_to_plan(lib)
    src = lib["root"] / "dl/Show.S01E01.mkv"
    transmission(lib, monkeypatch, [torrent(lib, ["dl/Show.S01E01.mkv"], left=5)])
    res = apply.apply_plan(con, cfg, plan.current_plan(con), execute=True, log=lambda *a: None)
    assert [r["reason"] for r in res["refused"]] == ["still downloading"]
    assert src.exists()


def test_unknown_download_state_stops_auto_and_apply(lib, monkeypatch):
    con, cfg = lib["con"], lib["cfg"]
    add_files(lib, "dl/Show.S01E01.mkv")
    run_to_plan(lib)
    pid = proposal(con, "Show.S01E01")["id"]
    review.decide(con, [pid], "approve")
    transmission(lib, monkeypatch, error="cannot check downloads (down)")
    before = tree_digest(lib["root"])
    res = apply.apply_plan(con, cfg, plan.current_plan(con), execute=True, log=lambda *a: None)
    assert res["count"] == 0 and res["refused"]
    assert all("cannot check downloads" in r["reason"] for r in res["refused"])
    assert tree_digest(lib["root"]) == before
    run_to_plan(lib)
    assert not lib["con"].execute("SELECT 1 FROM proposals WHERE status='auto' AND plan_id="
                                  "(SELECT MAX(id) FROM plans)").fetchone()


def test_password_with_dollar_stays_literal_and_env_form_expands(tmp_path, monkeypatch):
    ini = tmp_path / "c.ini"
    ini.write_text("[transmission]\nenabled = true\npassword = pa$$word\n")
    assert config.load(ini).transmission_password == "pa$$word"
    monkeypatch.setenv("TX_PW", "from-env")
    ini.write_text("[transmission]\npassword = ${TX_PW}\n")
    assert config.load(ini).transmission_password == "from-env"


# Leftover folders

MESSY = "Show.Name.2019.S01E02.The.Title"


def moved_out(lib, *extras):
    """Apply a move out of a messy folder that also holds extras, then return that folder."""
    con, cfg = lib["con"], lib["cfg"]
    add_files(lib, f"{MESSY}/Show.Name.2019.S01E02.The.Title.mkv", *(f"{MESSY}/{e}" for e in extras))
    run_to_plan(lib)
    apply.apply_plan(con, cfg, plan.current_plan(con), execute=True, log=lambda *a: None)
    return lib["root"] / MESSY


def paths(items):
    return [i["path"] for i in items]


def test_empty_and_leftover_folders_are_reported_and_nothing_changes(lib):
    folder = moved_out(lib, "info.nfo", "._stub.mp4", "cover.JPG", ".DS_Store")
    before = tree_digest(lib["root"])
    res = leftovers.find(lib["con"], lib["cfg"])
    assert str(folder) in paths(res["leftovers"])
    item = next(i for i in res["leftovers"] if i["path"] == str(folder))
    assert sorted(f["name"] for f in item["files"]) == [
        ".DS_Store", "._stub.mp4", "cover.JPG", "info.nfo"]
    assert tree_digest(lib["root"]) == before and folder.is_dir()


def test_fully_empty_folder_is_its_own_group(lib):
    folder = moved_out(lib)
    res = leftovers.find(lib["con"], lib["cfg"])
    assert str(folder) in paths(res["empty"]) and not res["leftovers"]


@pytest.mark.parametrize("keeper", ["Other.S02.Complete.mkv", "song.mp3", "archive.rar", "notes.docx", "noext"])
def test_folder_with_anything_else_is_not_suggested(lib, keeper):
    folder = moved_out(lib, keeper)
    res = leftovers.find(lib["con"], lib["cfg"])
    assert str(folder) not in paths(res["empty"] + res["leftovers"] + res["held"])


def test_large_leftover_type_is_kept(lib):
    lib["cfg"].leftover_max_mb = 0
    folder = moved_out(lib, "big.txt")
    assert str(folder) not in paths(leftovers.find(lib["con"], lib["cfg"])["leftovers"])


def test_symlink_is_not_treated_as_a_leftover(lib):
    folder = moved_out(lib)
    os.symlink("/etc/hostname", folder / "link.txt")
    assert str(folder) not in paths(leftovers.find(lib["con"], lib["cfg"])["leftovers"])


def test_unrelated_empty_folder_is_not_suggested(lib):
    moved_out(lib)
    (lib["root"] / "my-empty-folder").mkdir()
    res = leftovers.find(lib["con"], lib["cfg"])
    assert not any("my-empty-folder" in p for p in paths(res["empty"]))


def test_root_library_and_safety_folders_are_never_suggested(lib):
    moved_out(lib)
    con, cfg = lib["con"], lib["cfg"]
    root = lib["root"]
    r = con.execute("SELECT MAX(id) FROM runs").fetchone()[0]
    for name in ("Movies", "Shows", ".mylibrary-safety", "x"):
        con.execute("INSERT INTO moves (run_id, src, dst, status) VALUES (?,?,'z','moved')",
                    (r, str(root / name / "a.mkv")))
    (root / "x").mkdir()
    con.execute("INSERT INTO moves (run_id, src, dst, status) VALUES (?,?,'z','moved')",
                (r, str(root / "top.mkv")))
    res = leftovers.find(con, cfg)
    found = paths(res["empty"] + res["leftovers"] + res["held"])
    assert str(root / "x") in found
    assert not {str(root), str(root / "Movies"), str(root / "Shows"),
                str(root / ".mylibrary-safety")} & set(found)


def test_parent_is_reported_once_instead_of_each_child(lib):
    con, cfg = lib["con"], lib["cfg"]
    add_files(lib, "pack/season1/Show.S01E01.mkv", "pack/season2/Show.S02E01.mkv", "pack/a.nfo")
    moved_out(lib)
    run_to_plan(lib)
    apply.apply_plan(con, cfg, plan.current_plan(con), execute=True, log=lambda *a: None)
    found = paths(leftovers.find(con, cfg)["leftovers"] + leftovers.find(con, cfg)["empty"])
    assert str(lib["root"] / "pack") in found
    assert not any("season" in p for p in found)


def test_folder_listed_by_a_torrent_is_held_with_its_name(lib, monkeypatch):
    folder = moved_out(lib, "info.nfo")
    transmission(lib, monkeypatch, [torrent(lib, [f"{MESSY}/info.nfo"])])
    res = leftovers.find(lib["con"], lib["cfg"])
    assert str(folder) in paths(res["held"]) and str(folder) not in paths(res["leftovers"])
    assert next(i for i in res["held"] if i["path"] == str(folder))["torrent"] == "T"


def test_unknown_download_state_suggests_nothing(lib, monkeypatch):
    moved_out(lib, "info.nfo")
    transmission(lib, monkeypatch, error="cannot check downloads (down)")
    res = leftovers.find(lib["con"], lib["cfg"])
    assert res["error"] and not (res["empty"] or res["leftovers"] or res["held"])


def test_print0_lists_only_paths_safe_to_remove(lib, monkeypatch, capsys):
    folder = moved_out(lib, "info.nfo")
    cli.main(["--config", str(lib["ini"]), "leftovers", "--print0"])
    assert str(folder) in capsys.readouterr().out.split("\0")
    monkeypatch.setattr(downloads, "check", lambda cfg: downloads.Active(error="down"))
    with pytest.raises(SystemExit) as e:
        cli.main(["--config", str(lib["ini"]), "leftovers", "--print0"])
    assert "not suggesting" in str(e.value) and capsys.readouterr().out == ""


def test_print0_leaves_out_folders_held_by_a_torrent(lib, monkeypatch, capsys):
    folder = moved_out(lib, "info.nfo")
    held = downloads.Active(held={str(folder / "info.nfo"): "T"})
    monkeypatch.setattr(downloads, "check", lambda cfg: held)
    cli.main(["--config", str(lib["ini"]), "leftovers", "--print0"])
    assert str(folder) not in capsys.readouterr().out


def test_undo_recreates_a_folder_that_was_cleaned_up(lib):
    con, cfg = lib["con"], lib["cfg"]
    folder = moved_out(lib, "info.nfo")
    run = con.execute("SELECT MAX(id) FROM runs").fetchone()[0]
    shutil.rmtree(folder)  # the person removed it, as the report suggested
    res = apply.undo(con, cfg, run, execute=True, log=lambda *a: None)
    assert res["verify"]["ok"] if "ok" in res["verify"] else res["restored"]
    assert (folder / "Show.Name.2019.S01E02.The.Title.mkv").exists()


# Approving leftover folders

def quiet(*a):
    return None


def cleanup_state(lib):
    return lib["con"], lib["cfg"], moved_out(lib, "info.nfo", "._x.mp4")


def test_clean_is_a_dry_run_by_default(lib):
    con, cfg, folder = cleanup_state(lib)
    res = apply.clean_leftovers(con, cfg, [str(folder)], log=quiet)
    assert res["dry_run"] and res["count"] == 1 and folder.is_dir()


def test_clean_moves_aside_keeps_every_file_and_verifies(lib):
    con, cfg, folder = cleanup_state(lib)
    before = tree_digest(lib["root"])
    res = apply.clean_leftovers(con, cfg, [str(folder)], execute=True, log=quiet)
    assert res["moved"] == 1 and res["verify"]["ok"] and not folder.exists()
    kept = lib["root"] / f".mylibrary-safety/run-{res['run_id']}/leftovers" / MESSY
    assert sorted(p.name for p in kept.iterdir()) == ["._x.mp4", "info.nfo"]
    assert sum(1 for p in kept.iterdir()) == 2
    run = con.execute("SELECT * FROM runs WHERE id=?", (res["run_id"],)).fetchone()
    assert run["kind"] == "cleanup" and run["status"] == "ok"
    gone = before.copy()
    now_tree = tree_digest(lib["root"])
    assert all(now_tree.get(h, 0) <= n for h, n in gone.items())


def test_clean_undo_puts_it_back(lib):
    con, cfg, folder = cleanup_state(lib)
    before = tree_digest(lib["root"])
    res = apply.clean_leftovers(con, cfg, [str(folder)], execute=True, log=quiet)
    back = apply.undo(con, cfg, res["run_id"], execute=True, log=quiet)
    assert back["restored"] == 1 and back["verify"]["ok"]
    assert (folder / "info.nfo").exists() and tree_digest(lib["root"]) == before


def test_undone_cleanup_never_suggests_the_safety_folder(lib):
    con, cfg, folder = cleanup_state(lib)
    res = apply.clean_leftovers(con, cfg, [str(folder)], execute=True, log=quiet)
    apply.undo(con, cfg, res["run_id"], execute=True, log=quiet)
    report = leftovers.find(con, cfg)
    found = paths(report["empty"] + report["leftovers"] + report["held"])
    assert str(folder) in found and not any(".mylibrary-safety" in p for p in found)


@pytest.mark.parametrize("target", ["", "Movies", "not-a-suggestion", "..", ".mylibrary-safety"])
def test_clean_refuses_anything_not_suggested(lib, target):
    con, cfg, folder = cleanup_state(lib)
    (lib["root"] / "not-a-suggestion").mkdir()
    before = tree_digest(lib["root"])
    res = apply.clean_leftovers(con, cfg, [str(lib["root"] / target)], execute=True, log=quiet)
    assert res["moved"] == 0 and res["refused"]
    assert tree_digest(lib["root"]) == before


def test_clean_refuses_a_folder_that_gained_media_after_approval(lib):
    con, cfg, folder = cleanup_state(lib)
    (folder / "new.mkv").write_bytes(b"x")
    res = apply.clean_leftovers(con, cfg, [str(folder)], execute=True, log=quiet)
    assert res["moved"] == 0 and (folder / "new.mkv").exists()


def test_clean_refuses_a_folder_a_torrent_lists(lib, monkeypatch):
    con, cfg, folder = cleanup_state(lib)
    transmission(lib, monkeypatch, [torrent(lib, [f"{MESSY}/info.nfo"])])
    res = apply.clean_leftovers(con, cfg, [str(folder)], execute=True, log=quiet)
    assert res["moved"] == 0 and folder.is_dir()


def test_clean_stops_when_downloads_cannot_be_checked(lib, monkeypatch):
    con, cfg, folder = cleanup_state(lib)
    transmission(lib, monkeypatch, error="cannot check downloads (down)")
    res = apply.clean_leftovers(con, cfg, [str(folder)], execute=True, log=quiet)
    assert res["error"] and folder.is_dir()


def test_clean_through_cli(lib):
    folder = moved_out(lib, "info.nfo")
    args = ["--config", str(lib["ini"]), "leftovers", "--clean", str(folder)]
    cli.main(args)
    assert folder.is_dir()
    cli.main(args + ["--execute"])
    assert not folder.exists()


# Compilations

def track(artist, n, album="Mix", **extra):
    return {"artist": artist, "album": album, "title": f"Song {n}", "track": str(n), **extra}


def album_dsts(lib, like="rel/"):
    rows = lib["con"].execute("SELECT src, dst, status FROM proposals WHERE plan_id="
                              "(SELECT MAX(id) FROM plans) AND src LIKE ?", (f"%{like}%",))
    return {os.path.basename(r["src"]): r for r in rows}


def load_album(lib, monkeypatch, tracks, folder="rel"):
    names = {f"t{i}.mp3": t for i, t in enumerate(tracks, 1)}
    probe_with(monkeypatch, names)
    add_files(lib, *(f"{folder}/{n}" for n in names))
    return names


def test_many_artists_go_under_various_artists(lib, monkeypatch):
    load_album(lib, monkeypatch, [track(f"Artist {i}", i) for i in range(1, 6)])
    run_to_plan(lib)
    dsts = album_dsts(lib)
    assert all("/Music/Various Artists/Mix/" in r["dst"] for r in dsts.values())
    assert dsts["t2.mp3"]["dst"].endswith("Various Artists/Mix/02 - Song 2.mp3")
    assert {r["status"] for r in dsts.values()} == {"auto"}


def test_solo_album_with_guests_stays_with_its_artist(lib, monkeypatch):
    artists = ["Drake", "Drake feat. Rihanna", "Drake ft. Future", "Drake & Nicki", "Drake, Jay-Z"]
    load_album(lib, monkeypatch, [track(a, i) for i, a in enumerate(artists, 1)])
    run_to_plan(lib)
    assert all("/Music/Drake/Mix/" in r["dst"] for r in album_dsts(lib).values())


def test_album_artist_on_every_track_wins(lib, monkeypatch):
    tracks = [track(f"Artist {i}", i, album_artist="The Curator") for i in range(1, 6)]
    load_album(lib, monkeypatch, tracks)
    run_to_plan(lib)
    assert all("/Music/The Curator/Mix/" in r["dst"] for r in album_dsts(lib).values())


def test_partial_album_artist_tags_do_not_scatter_the_album(lib, monkeypatch):
    tracks = [track(f"Artist {i}", i) for i in range(1, 5)]
    tracks += [track("Dua Lipa", 5, album_artist="Dua Lipa"),
               track("Migos", 6, album_artist="Various Artists")]
    load_album(lib, monkeypatch, tracks)
    run_to_plan(lib)
    assert all("/Music/Various Artists/Mix/" in r["dst"] for r in album_dsts(lib).values())


def test_cut_off_album_tag_joins_the_full_album(lib, monkeypatch):
    tracks = [track(f"A{i}", i, album="Billboard Hot 100 Singles Chart") for i in range(1, 5)]
    tracks.append(track("A9", 9, album="Billboard Hot 100 Singles Char"))
    load_album(lib, monkeypatch, tracks)
    run_to_plan(lib)
    assert all("Various Artists/Billboard Hot 100 Singles Chart/" in r["dst"]
               for r in album_dsts(lib).values())


def test_two_artists_are_not_a_compilation(lib, monkeypatch):
    load_album(lib, monkeypatch, [track("A", 1), track("B", 2), track("A", 3), track("B", 4)])
    run_to_plan(lib)
    assert not any("Various Artists" in r["dst"] for r in album_dsts(lib).values())


def test_same_album_name_in_two_releases_is_not_merged(lib, monkeypatch):
    tags = {"a.mp3": track("Abba", 1, album="Greatest Hits"),
            "b.mp3": track("Queen", 1, album="Greatest Hits"),
            "c.mp3": track("Rush", 1, album="Greatest Hits")}
    probe_with(monkeypatch, tags)
    add_files(lib, "one/a.mp3", "two/b.mp3", "three/c.mp3")
    run_to_plan(lib)
    con = lib["con"]
    assert "/Music/Abba/Greatest Hits/" in proposal(con, "one/a.mp3")["dst"]
    assert "/Music/Queen/Greatest Hits/" in proposal(con, "two/b.mp3")["dst"]


@pytest.mark.parametrize("first,second", [("CD1", "CD2"), ("DISC 1", "DISC 2"), ("Disk.1", "Disk.2")])
def test_disc_folders_stop_track_number_collisions(lib, monkeypatch, first, second):
    tags = {"a.mp3": track("A", 1), "b.mp3": track("B", 2), "c.mp3": track("C", 1),
            "d.mp3": track("D", 2)}
    probe_with(monkeypatch, tags)
    add_files(lib, f"rel/{first}/a.mp3", f"rel/{first}/b.mp3",
              f"rel/{second}/c.mp3", f"rel/{second}/d.mp3")
    run_to_plan(lib)
    dsts = album_dsts(lib)
    assert dsts["a.mp3"]["dst"].endswith("Various Artists/Mix/1-01 - Song 1.mp3")
    assert dsts["c.mp3"]["dst"].endswith("Various Artists/Mix/2-01 - Song 1.mp3")
    assert {r["status"] for r in dsts.values()} == {"auto"}


def test_single_cd_folder_adds_no_disc(lib, monkeypatch):
    probe_with(monkeypatch, {"a.mp3": track("A", 1)})
    add_files(lib, "rel/CD1/a.mp3")
    run_to_plan(lib)
    assert album_dsts(lib)["a.mp3"]["dst"].endswith("Music/A/Mix/01 - Song 1.mp3")


def test_detection_needs_no_redo_for_tracks_already_parsed(lib, monkeypatch):
    con, cfg = lib["con"], lib["cfg"]
    load_album(lib, monkeypatch, [track(f"Artist {i}", i) for i in range(1, 6)])
    run_to_plan(lib)
    con.execute("UPDATE parses SET artist='Artist ' || file_id WHERE kind='music'")
    parse.parse_all(con, cfg, log=quiet)
    plan.make_plan(con, cfg, log=quiet)
    assert all("Various Artists" in r["dst"] for r in album_dsts(lib).values())


def test_manual_edit_of_a_compilation_track_is_kept(lib, monkeypatch):
    con, cfg = lib["con"], lib["cfg"]
    load_album(lib, monkeypatch, [track(f"Artist {i}", i) for i in range(1, 6)])
    run_to_plan(lib)
    pid = proposal(con, "rel/t1.mp3")["id"]
    review.edit(con, cfg, pid, "Song 1", None, artist="My Choice")
    run_to_plan(lib)
    assert "/Music/My Choice/Mix/" in proposal(con, "rel/t1.mp3")["dst"]


def test_moved_compilation_stays_put_even_after_redo(lib, monkeypatch):
    con, cfg = lib["con"], lib["cfg"]
    tags = {"a.mp3": track("A", 1), "b.mp3": track("B", 1), "c.mp3": track("C", 2)}
    tags.update({"1-01 - Song 1.mp3": tags["a.mp3"], "2-01 - Song 1.mp3": tags["b.mp3"],
                 "2-02 - Song 2.mp3": tags["c.mp3"]})
    probe_with(monkeypatch, tags)
    add_files(lib, "rel/CD1/a.mp3", "rel/CD2/b.mp3", "rel/CD2/c.mp3")
    run_to_plan(lib)
    apply.apply_plan(con, cfg, plan.current_plan(con), execute=True, log=quiet)
    assert (lib["root"] / "Music/Various Artists/Mix/2-02 - Song 2.mp3").exists()
    reparse(lib)
    rows = con.execute("SELECT status FROM proposals WHERE plan_id=(SELECT MAX(id) FROM plans) "
                       "AND src LIKE '%Various Artists%'").fetchall()
    assert [r["status"] for r in rows] == ["noop"] * 3


def test_part_of_a_compilation_moved_alone_stays_put(lib, monkeypatch):
    con, cfg = lib["con"], lib["cfg"]
    tags = {"01 - Song 1.mp3": track("A", 1)}
    probe_with(monkeypatch, tags)
    add_files(lib, "Music/Various Artists/Mix/01 - Song 1.mp3")
    reparse(lib)
    assert proposal(con, "Various Artists")["status"] == "noop"



# Adult scenes

DOTTED = "Acme.26.06.21.Ann.Lee.New.Friends.XXX.1080p.MP4-GRP[XC]"
ADULT = "Adult/Acme/Acme - 2026-06-21 - Ann Lee - New Friends"


@pytest.mark.parametrize("name,site,when,people,title", [
    (DOTTED, "Acme", "2026-06-21", "Ann Lee", "New Friends"),
    ("Acme.26.06.21.Ann.Lee.And.Kim.Roe.XXX.480p", "Acme", "2026-06-21", "Ann Lee, Kim Roe", None),
    ("Acme.26.06.21.Ann.Lee.Kim.Roe.And.Sue.Poe.XXX", "Acme", "2026-06-21",
     "Ann Lee, Kim Roe, Sue Poe", None),
    ("Acme.21.06.02.LaStar69.Welcome.Home.XXX", "Acme", "2021-06-02", "LaStar69", "Welcome Home"),
    ("Acme.26.08.21.Episode.473.Ann.Lee.XXX", "Acme", "2026-08-21", "Ann Lee", "Episode 473"),
    ("[Acme] Ann Lee Holy Inspection (26.10.16)[720p][x264]", "Acme", "2026-10-16", "Ann Lee",
     "Holy Inspection"),
    ("[Acme] Ann Lee, Kim Roe - The Visit (04.05.2016) rq", "Acme", "2016-05-04",
     "Ann Lee, Kim Roe", "The Visit"),
    ("Acme - Ann Lee - Part 3, Again (17.05.2026) rq", "Acme", "2026-05-17", "Ann Lee",
     "Part 3, Again"),
    ("Acme - Ann Lee, Kim Roe, Sue Poe (16.01.2025) rq", "Acme", "2025-01-16",
     "Ann Lee, Kim Roe, Sue Poe", None),
])
def test_scene_names(name, site, when, people, title):
    s = parse.scene_from_name(name)
    assert (s["site"], s["scene_date"], s["performers"], s["title"]) == (site, when, people, title)


@pytest.mark.parametrize("name", [
    "The.Matrix.1999.1080p.BluRay.x264-GRP", "Show.24.01.15.Guest.Name.720p.WEB",
    "Spider-Man - Brand New Day (2026)", "UFC 312 Du Plessis Vs Strickland 2025-02-08 1080p",
    "[Group] Some Film (2019)", "Tyler Roe - One Title - UltraHD - 4k",
])
def test_ordinary_names_are_not_scenes(name):
    assert parse.scene_from_name(name) is None


@pytest.mark.parametrize("bad", ["Acme - Ann Lee - T (17.13.2025) rq", "Acme - Ann Lee - T (31.02.2025) rq",
                                 "[Acme] Ann Lee T (99.01.01)"])
def test_impossible_or_future_date_is_not_guessed(bad):
    s = parse.scene_from_name(bad)
    assert s["site"] == "Acme" and s["scene_date"] is None


def test_scene_goes_to_adult_folder_without_llm(lib):
    add_files(lib, f"{DOTTED}/acme.26.06.21.ann.lee.new.friends.mp4")
    run_to_plan(lib)
    con = lib["con"]
    p = proposal(con, "acme.26.06.21")
    assert p["status"] == "auto" and p["dst"].endswith(f"{ADULT}.mp4")
    r = con.execute("SELECT kind, flagged FROM parses").fetchone()
    assert r["kind"] == "scene" and r["flagged"] == 0


def test_lowercase_file_takes_site_spelling_from_folder(lib):
    add_files(lib, "CoolSite.26.06.21.Ann.Lee.XXX.1080p/cs.26.06.21.ann.lee.mp4")
    run_to_plan(lib)
    assert "/Adult/CoolSite/CoolSite - 2026-06-21 - Ann Lee." in proposal(lib["con"], "cs.26")["dst"]


def test_short_scene_is_not_an_extra(lib, monkeypatch):
    lib["cfg"].min_duration_min = 40
    probe_with(monkeypatch, {})
    add_files(lib, f"{DOTTED}.mp4")
    run_to_plan(lib)
    assert proposal(lib["con"], "Acme.26")["status"] == "auto"


def test_scene_sample_stays_ignored(lib):
    add_files(lib, f"{DOTTED}/sample.mp4")
    run_to_plan(lib)
    assert proposal(lib["con"], "sample.mp4")["status"] == "ignored"


@pytest.mark.parametrize("name,reason", [
    ("Acme - Ann Lee - T (17.05.2026) rq.mp4", "no XXX marker"),
    ("Acme.2024.Ann.Lee.Title.XXX.1080p.mp4", "no scene date"),
    ("Acme - Ann Lee - T (17.13.2025) rq.mp4", "no scene date"),
])
def test_unsure_scene_goes_to_review(lib, name, reason):
    add_files(lib, name)
    run_to_plan(lib)
    p = proposal(lib["con"], "Acme")
    assert p["status"] == "review" and reason in p["reason"]


def test_scene_with_only_a_site_has_no_target(lib):
    add_files(lib, "Acme.26.06.21.XXX.1080p.mp4")
    run_to_plan(lib)
    p = proposal(lib["con"], "Acme")
    assert p["status"] == "review" and p["dst"] is None and "needs a site" in p["reason"]


def test_llm_answer_is_replaced_without_redo(lib):
    con, cfg = lib["con"], lib["cfg"]
    add_files(lib, f"{DOTTED}.mp4")
    run_to_plan(lib)
    con.execute("UPDATE parses SET kind='extra', source='llm', flagged=1, title='Acme'")
    parse.parse_all(con, cfg, log=quiet)
    plan.make_plan(con, cfg, log=quiet)
    assert proposal(con, "Acme.26")["status"] == "auto"


def test_scenes_on_one_site_day_do_not_collide(lib):
    add_files(lib, "Acme.26.06.21.Ann.Lee.XXX.1080p.mp4", "Acme.26.06.21.Kim.Roe.XXX.1080p.mp4")
    run_to_plan(lib)
    st = statuses(lib["con"], plan.current_plan(lib["con"]))
    assert {v for k, v in st.items() if k.startswith("Acme")} == {"auto"}


def test_identical_scene_targets_are_a_conflict(lib):
    add_files(lib, "Acme.26.06.21.Ann.Lee.XXX.1080p.mp4", "Acme.26.06.21.Ann.Lee.XXX.720p.mp4")
    run_to_plan(lib)
    assert "conflict" in statuses(lib["con"], plan.current_plan(lib["con"])).values()


def test_hostile_scene_name_cannot_escape_root(lib):
    add_files(lib, "[...] Ann Lee T (17.05.2026) rq.mp4")
    add_files(lib, "[a$b] Ann Lee T (17.05.2026) rq.mp4")
    con = lib["con"]
    con_rows = run_to_plan(lib)
    root = str(lib["root"])
    for r in con.execute("SELECT dst FROM proposals WHERE plan_id=? AND dst IS NOT NULL",
                         (con_rows["plan_id"],)):
        assert os.path.realpath(r["dst"]).startswith(root + os.sep)


def test_very_long_scene_name_fits_the_file_system(lib):
    people = ", ".join(f"Performer Number{i}" for i in range(30))
    add_files(lib, f"Acme - {people} (16.01.2025) rq.mp4"[:200] + " (16.01.2025) rq.mp4")
    run_to_plan(lib)
    assert len(os.path.basename(proposal(lib["con"], "Acme")["dst"]).encode()) <= 255


def moved_scene(lib):
    con, cfg = lib["con"], lib["cfg"]
    add_files(lib, f"{DOTTED}/acme.26.06.21.ann.lee.new.friends.mp4")
    run_to_plan(lib)
    apply.apply_plan(con, cfg, plan.current_plan(con), execute=True, log=quiet)
    return con, cfg


def test_moved_scene_stays_put_on_rescan_and_redo(lib):
    con, cfg = moved_scene(lib)
    assert (lib["root"] / f"{ADULT}.mp4").exists()
    run_to_plan(lib)
    assert proposal(con, "Adult/Acme")["status"] == "noop"
    reparse(lib)
    assert proposal(con, "Adult/Acme")["status"] == "noop"


def test_placed_scene_with_no_history_stays_put(lib):
    add_files(lib, f"{ADULT}.mp4")
    run_to_plan(lib)
    assert proposal(lib["con"], "Adult/Acme")["status"] == "noop"


def test_scene_already_in_movies_gets_a_proposal_from_its_first_name(lib):
    con, cfg = lib["con"], lib["cfg"]
    add_files(lib, f"{DOTTED}/acme.mp4")
    run_to_plan(lib)
    pid = proposal(con, "acme.mp4")["id"]
    fid = proposal(con, "acme.mp4")["file_id"]
    review.edit(con, cfg, pid, "Acme", None, kind="movie")
    apply.apply_plan(con, cfg, plan.current_plan(con), execute=True, log=quiet)
    moved = lib["root"] / "Movies/Acme/Acme.mp4"
    assert moved.exists()
    con.execute("UPDATE parses SET source='llm' WHERE file_id=?", (fid,))  # as the first run left it
    run_to_plan(lib)
    p = proposal(con, "Movies/Acme")
    assert p["dst"].endswith("Adult/Acme/Acme - 2026-06-21 - Ann Lee - New Friends.mp4")
    assert p["status"] == "auto"
    apply.apply_plan(con, cfg, plan.current_plan(con), execute=True, log=quiet)
    assert (lib["root"] / f"{ADULT}.mp4").exists() and not moved.exists()
    run_to_plan(lib)
    assert proposal(con, "Adult/Acme")["status"] == "noop"


def test_manual_scene_edit_is_kept(lib):
    con, cfg = lib["con"], lib["cfg"]
    add_files(lib, f"{DOTTED}.mp4")
    run_to_plan(lib)
    res = review.edit(con, cfg, proposal(con, "Acme.26")["id"], "My Title", None,
                      site="Other", scene_date="2020-01-02", performers="Sue Poe")
    assert res["status"] == "approved"
    assert res["dst"].endswith("Adult/Other/Other - 2020-01-02 - Sue Poe - My Title.mp4")
    reparse(lib)
    assert proposal(con, "Acme.26")["dst"] == res["dst"]


def test_scene_edit_keeps_unset_fields_and_title_when_none(lib):
    con, cfg = lib["con"], lib["cfg"]
    add_files(lib, f"{DOTTED}.mp4")
    run_to_plan(lib)
    res = review.edit(con, cfg, proposal(con, "Acme.26")["id"], None, None, performers="Sue Poe")
    assert res["dst"].endswith("Acme - 2026-06-21 - Sue Poe - New Friends.mp4")


def test_scene_edit_turns_a_movie_into_a_scene(lib):
    con, cfg = lib["con"], lib["cfg"]
    add_files(lib, "misc/Ann and the visit.mp4")
    run_to_plan(lib)
    res = review.edit(con, cfg, proposal(con, "the visit")["id"], "", None, kind="scene",
                      site="Acme", performers="Ann")
    assert res["dst"].endswith("Adult/Acme/Acme - Ann.mp4")


@pytest.mark.parametrize("bad", ["2026-13-01", "21.06.2026", "../x", "20260621"])
def test_scene_edit_rejects_bad_date(lib, bad):
    con, cfg = lib["con"], lib["cfg"]
    add_files(lib, f"{DOTTED}.mp4")
    run_to_plan(lib)
    with pytest.raises(ValueError):
        review.edit(con, cfg, proposal(con, "Acme.26")["id"], "T", None, scene_date=bad)


def test_old_database_gains_scene_columns(tmp_path):
    f = tmp_path / "old.db"
    c = db.connect(f)
    for col in ("site", "scene_date", "performers"):
        c.execute(f"ALTER TABLE parses DROP COLUMN {col}")
    c.commit(); c.close()
    have = {r["name"] for r in db.connect(f).execute("PRAGMA table_info(parses)")}
    assert {"site", "scene_date", "performers"} <= have


def test_adult_folder_is_never_a_leftover(lib):
    (lib["root"] / "Adult").mkdir()
    report = leftovers.find(lib["con"], lib["cfg"])
    assert not any(i["path"].endswith("/Adult") for i in report["empty"])
