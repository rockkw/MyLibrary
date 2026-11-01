import io
import json
import os
import sqlite3
import time
import urllib.error

import pytest

from conftest import tree_digest
from mylibrary import apply, classify, config, db, downloads, dupes, parse, plan, review, scan


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
    r = parse.identify("/lib/DA13____540_2026/01.mp4", 9000, _cfg_stub())
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

