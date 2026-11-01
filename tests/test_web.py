import importlib
import sys
import time

import pytest

from test_pipeline import run_to_plan


@pytest.fixture
def client(lib):
    import mylibrary.web.app as webapp
    importlib.reload(webapp)  # re-read config for this test's library
    run_to_plan(lib)
    webapp.app.testing = True
    return webapp.app.test_client()


def test_summary_and_list(client):
    s = client.get("/api/summary").get_json()
    assert s["counts"]["auto"] == 2
    items = client.get("/api/proposals?status=review").get_json()["items"]
    assert len(items) == 1


def test_decision_records_only(client, lib):
    pid = client.get("/api/proposals?status=auto").get_json()["items"][0]["id"]
    r = client.post("/api/decision", json={"ids": [pid], "action": "skip"}).get_json()
    assert r["updated"] == 1 and r["cli"] == f"mylib skip {pid}"
    assert (lib["root"] / "incoming/The.Matrix.1999.1080p.BluRay.x264-GRP.mkv").exists()


def test_unknown_job_rejected(client):
    assert client.post("/api/jobs", json={"name": "rm -rf /"}).status_code == 400
    assert client.post("/api/jobs", json={"name": "apply", "params": {"plan": "1; rm"}}).status_code == 400


def test_dry_run_job_changes_nothing(client, lib):
    j = client.post("/api/jobs", json={"name": "apply_dry", "params": {"plan": 1}}).get_json()
    for _ in range(100):
        s = client.get(f"/api/jobs/{j['id']}").get_json()
        if s["state"] != "running":
            break
        time.sleep(0.1)
    assert s["state"] == "done"
    assert (lib["root"] / "incoming/The.Matrix.1999.1080p.BluRay.x264-GRP.mkv").exists()


def test_edit_sets_edition(client):
    rows = client.get("/api/proposals?status=auto").get_json()
    rows = rows["items"] if isinstance(rows, dict) else rows
    row = next(r for r in rows if "alien.dc" in r["src"])
    assert row["edition"] == "Director's Cut"
    res = client.post("/api/edit", json={"id": row["id"], "title": "Alien", "year": 1979,
                                         "edition": "Final Cut"}).get_json()
    assert res["dst"].endswith("Alien (1979) - Final Cut.mkv")
    assert '--edition "Final Cut"' in res["cli"]


def test_edit_episode_through_api(client):
    pid = client.get("/api/proposals?status=auto").get_json()["items"][0]["id"]
    r = client.post("/api/edit", json={
        "id": pid, "title": "Show", "year": "", "edition": "", "kind": "episode",
        "season": "1", "episode": "4", "episode_last": "", "episode_title": "Pilot"}).get_json()
    assert r["status"] == "approved"
    assert "Shows/Show/Season 01/Show - S01E04 - Pilot" in r["dst"]
    assert '--season 1 --episode 4 --episode-title "Pilot"' in r["cli"]
    d = client.get(f"/api/proposals?q=Show").get_json()["items"][0]
    assert (d["kind"], d["season"], d["episode"]) == ("episode", 1, 4)


def test_edit_movie_through_api_still_works(client):
    pid = client.get("/api/proposals?status=auto").get_json()["items"][0]["id"]
    r = client.post("/api/edit", json={
        "id": pid, "title": "A Film", "year": "1999", "edition": "", "kind": "movie",
        "season": "", "episode": "", "episode_last": "", "episode_title": ""}).get_json()
    assert "Movies/A Film (1999)/" in r["dst"]


@pytest.mark.parametrize("extra,expected,cli", [
    ({"kind": "music", "artist": "Band", "album": "Debut", "track": "2"},
     "Music/Band/Debut/02 - A Name", '--artist "Band" --album "Debut" --track 2'),
    ({"kind": "software"}, "Software/A Name/", "--kind software")])
def test_edit_music_and_software_through_api(client, extra, expected, cli):
    pid = client.get("/api/proposals?status=auto").get_json()["items"][0]["id"]
    form = dict(id=pid, title="A Name", year="", edition="", season="", episode="",
                episode_last="", episode_title="", artist="", album="", track="", disc="")
    r = client.post("/api/edit", json={**form, **extra}).get_json()
    assert r["status"] == "approved" and expected in r["dst"] and cli in r["cli"]
    item = client.get(f"/api/proposal/{pid}").get_json()
    assert item["kind"] == extra["kind"]

