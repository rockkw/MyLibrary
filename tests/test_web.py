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
