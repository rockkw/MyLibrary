"""Bulk edit: shared fields for several review items, each keeping its own for the rest."""

import pytest

from conftest import tree_digest
from mylibrary import cli, review
from test_pipeline import add_files, proposal, run_to_plan
from test_web import client  # noqa: F401


def items(lib, *likes):
    run_to_plan(lib)
    return [proposal(lib["con"], like) for like in likes]


def test_shared_fields_with_each_files_own_words(lib):
    add_files(lib, "Show Name _ Site (2).mp4", "Show Name _ Site (3).mp4", "Show Name _ Site.mp4")
    ps = items(lib, "Site (2)", "Site (3)", "Site.mp4")
    res = review.edit_many(lib["con"], lib["cfg"], [p["id"] for p in ps],
                           {"kind": "scene", "site": "Site"})
    assert res["counts"] == {"approved": 3}
    dsts = sorted(r["dst"].rsplit("/", 1)[1] for r in res["results"])
    assert dsts == ["Site - Show Name (2).mp4", "Site - Show Name (3).mp4", "Site - Show Name.mp4"]


def test_empty_fields_keep_each_files_values(lib):
    add_files(lib, "Some.Show.S01E02.mkv", "Some.Show.S01E05.mkv")
    a, b = items(lib, "S01E02", "S01E05")
    res = review.edit_many(lib["con"], lib["cfg"], [a["id"], b["id"]],
                           {"title": "Proper Show", "season": 2, "artist": "", "year": None})
    assert res["counts"] == {"approved": 2}
    names = sorted(r["dst"].rsplit("/", 1)[1] for r in res["results"])
    assert names == ["Proper Show - S02E02.mkv", "Proper Show - S02E05.mkv"]


def test_a_shared_title_that_collides_approves_none_of_them(lib):
    add_files(lib, "Clip.One.mkv", "Clip.Two.mkv")
    a, b = items(lib, "Clip.One", "Clip.Two")
    res = review.edit_many(lib["con"], lib["cfg"], [a["id"], b["id"]],
                           {"kind": "movie", "title": "Same Film", "year": 2001})
    assert res["counts"] == {"conflict": 2}
    for p in (a, b):
        row = lib["con"].execute("SELECT status, reason FROM proposals WHERE id=?", (p["id"],)).fetchone()
        assert row["status"] == "conflict" and "same target as selected" in row["reason"]


def test_kind_change_and_no_target_stays_in_review(lib):
    add_files(lib, "Clip.One.mkv", "Clip.Two.mkv")
    a, b = items(lib, "Clip.One", "Clip.Two")
    res = review.edit_many(lib["con"], lib["cfg"], [a["id"], b["id"]], {"kind": "scene", "performers": "Ann Lee"})
    assert res["counts"] == {"review": 2}
    assert all("needs a site" in r["reason"] for r in res["results"])


def test_nothing_filled_in_or_per_file_fields_are_refused(lib):
    a, b = items(lib, "The.Matrix.1999.1080p.BluRay.x264-GRP.mkv", "alien.dc")
    with pytest.raises(ValueError, match="at least one"):
        review.edit_many(lib["con"], lib["cfg"], [a["id"], b["id"]], {"title": " ", "year": None})
    with pytest.raises(ValueError, match="episode"):
        review.edit_many(lib["con"], lib["cfg"], [a["id"], b["id"]], {"episode": 3})


def test_names_are_cleaned(lib):
    add_files(lib, "Clip.One.mkv")
    a, b = items(lib, "Clip.One", "final_cut_v2")
    res = review.edit_many(lib["con"], lib["cfg"], [a["id"], b["id"]],
                           {"kind": "scene", "site": "../../etc", "title": 'Bad: "name"'})
    for r in res["results"]:
        assert "/Adult/....etc/" in r["dst"] and '"' not in r["dst"] and ".." not in r["dst"].split("/Adult/")[0]


def test_an_applied_item_is_reported_not_changed(lib):
    a, b = items(lib, "The.Matrix.1999.1080p.BluRay.x264-GRP.mkv", "alien.dc")
    lib["con"].execute("UPDATE proposals SET status='applied' WHERE id=?", (a["id"],))
    res = review.edit_many(lib["con"], lib["cfg"], [a["id"], b["id"]], {"edition": "Final Cut"})
    assert res["counts"] == {"error": 1, "approved": 1}


def test_disk_is_untouched(lib):
    a, b = items(lib, "The.Matrix.1999.1080p.BluRay.x264-GRP.mkv", "alien.dc")
    before = tree_digest(lib["root"])
    review.edit_many(lib["con"], lib["cfg"], [a["id"], b["id"]], {"kind": "scene", "site": "Site"})
    assert tree_digest(lib["root"]) == before


def test_cli_takes_several_ids(lib, capsys):
    add_files(lib, "Clip.One.mkv", "Clip.Two.mkv")
    a, b = items(lib, "Clip.One", "Clip.Two")
    cli.main(["--config", str(lib["ini"]), "edit", str(a["id"]), str(b["id"]), "--site", "Site",
              "--kind", "scene"])
    out = capsys.readouterr().out
    assert "review 2" in out or "approved" in out
    with pytest.raises(SystemExit, match="one file at a time"):
        cli.main(["--config", str(lib["ini"]), "edit", str(a["id"]), str(b["id"]), "--episode", "3"])


def test_cli_single_edit_still_works(lib):
    a, = items(lib, "final_cut_v2")
    cli.main(["--config", str(lib["ini"]), "edit", str(a["id"]), "--title", "Heat", "--year", "1995"])
    assert proposal(lib["con"], "final_cut_v2")["status"] == "approved"


def test_endpoint(client, lib):  # noqa: F811
    ids = [i["id"] for i in client.get("/api/proposals").get_json()["items"][:2]]
    r = client.post("/api/edit-many", json={"ids": ids, "fields": {"kind": "scene", "site": "Site",
                                                                    "year": ""}})
    assert r.status_code == 200 and r.get_json()["cli"].startswith(f"mylib edit {ids[0]} {ids[1]}")
    assert client.post("/api/edit-many", json={"ids": ids[:1], "fields": {"site": "S"}}).status_code == 400
    assert client.post("/api/edit-many", json={"ids": ids, "fields": {"year": "abc"}}).status_code == 400
    assert client.post("/api/edit-many", json={"ids": ids, "fields": {}}).status_code == 400
