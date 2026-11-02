import urllib.error

import pytest

from conftest import tree_digest
from mylibrary import apply, classify, plan, preflight, review, tree
from test_pipeline import proposal, run_to_plan
from test_web import client  # noqa: F401


@pytest.fixture
def two(lib, tmp_path):
    """A second root holding one film, scanned and planned alongside the first."""
    second = tmp_path / "second"
    (second / "in").mkdir(parents=True)
    (second / "in/Heat.1995.1080p.BluRay.x264-GRP.mkv").write_bytes(b"heat" * 100)
    lib["cfg"].roots.append(second)
    lib["second"] = second
    return lib


def refuse(*a, **k):
    raise urllib.error.URLError("no LLM in tests")


def drop(lib):
    lib["cfg"].roots.remove(lib["second"])


def add_back(lib):
    lib["cfg"].roots.append(lib["second"])


def snapshot(con):
    return {t: [tuple(r) for r in con.execute(f"SELECT * FROM {t} ORDER BY 1")]
            for t in ("files", "parses", "moves")}


def test_a_new_plan_leaves_out_the_removed_root(two):
    run_to_plan(two)
    assert proposal(two["con"], "Heat.1995")
    drop(two)
    run_to_plan(two)
    assert proposal(two["con"], "Heat.1995") is None
    assert proposal(two["con"], "The.Matrix")


def test_a_plan_already_built_cannot_move_it(two):
    con, cfg = two["con"], two["cfg"]
    pid = run_to_plan(two)["plan_id"]
    drop(two)
    prep = preflight.prepare(con, cfg, pid)
    assert not [m for m in prep["moves"] if "Heat" in m["src"]]
    assert [r["reason"] for r in prep["refused"] if "Heat" in r["src"]] == ["root is not in the config"]


def test_a_preapproved_item_cannot_move_either(two):
    con, cfg = two["con"], two["cfg"]
    pid = run_to_plan(two)["plan_id"]
    heat = proposal(con, "Heat.1995")
    review.decide(con, [heat["id"]], "approve")
    drop(two)
    before = tree_digest(two["second"])
    names = sorted(p.name for p in two["second"].rglob("*"))
    seen = []
    res = apply.apply_plan(con, cfg, pid, execute=True, log=seen.append)
    assert res["moved"] == 2  # the first root still ran
    assert tree_digest(two["second"]) == before
    assert sorted(p.name for p in two["second"].rglob("*")) == names
    assert any("root is not in the config" in line for line in seen)


def test_dry_run_reports_the_refusal(two):
    pid = run_to_plan(two)["plan_id"]
    drop(two)
    seen = []
    apply.apply_plan(two["con"], two["cfg"], pid, execute=False, log=seen.append)
    assert any("Heat" in line and "root is not in the config" in line for line in seen)


def test_counts_and_list_agree_with_the_tree(two):
    con, cfg = two["con"], two["cfg"]
    pid = run_to_plan(two)["plan_id"]
    drop(two)
    s = review.summary(con, cfg, pid)
    assert [r["root"] for r in s["roots"]] == [str(two["root"])]
    assert s["files"] == s["roots"][0]["n"]
    assert s["counts"]["auto"] == 2
    assert not [i for i in review.list_proposals(con, cfg, pid) if "Heat" in i["src"]]
    assert tree.build(con, cfg)["moves"] == 3


def test_endpoints_hide_it(two, client):  # noqa: F811
    drop(two)
    s = client.get("/api/summary").get_json()
    assert s["counts"]["auto"] == 2


def test_rows_are_kept(two):
    con = two["con"]
    run_to_plan(two)
    before = snapshot(con)
    drop(two)
    run_to_plan(two)
    assert snapshot(con) == before


def test_the_llm_is_not_asked_about_it(two):
    con, cfg = two["con"], two["cfg"]
    run_to_plan(two)
    con.execute("UPDATE parses SET flagged=1, source='guessit'")
    drop(two)
    cfg.ollama_enabled = True
    asked = []
    classify.classify_all(con, cfg, limit=None, log=asked.append, opener=refuse)
    total = int(asked[0].split()[0])
    assert total == con.execute("SELECT COUNT(*) FROM files WHERE root=? AND missing=0",
                                (str(two["root"]),)).fetchone()[0]


def test_adding_it_back_restores_decisions_without_reprocessing(two):
    con = two["con"]
    run_to_plan(two)
    heat = proposal(con, "Heat.1995")
    review.decide(con, [heat["id"]], "skip")
    row = tuple(con.execute("SELECT * FROM files WHERE path LIKE '%Heat%'").fetchone())
    drop(two)
    run_to_plan(two)
    add_back(two)
    run_to_plan(two)
    assert proposal(con, "Heat.1995")["status"] == "skipped"
    fresh = con.execute("SELECT * FROM files WHERE path LIKE '%Heat%'").fetchone()
    assert fresh["id"] == row[0] and fresh["missing"] == 0
    assert con.execute("SELECT COUNT(*) FROM files WHERE path LIKE '%Heat%'").fetchone()[0] == 1


def test_a_reset_decision_is_not_brought_back(two):
    con = two["con"]
    run_to_plan(two)
    heat = proposal(con, "Heat.1995")
    review.decide(con, [heat["id"]], "skip")
    run_to_plan(two)
    review.decide(con, [proposal(con, "Heat.1995")["id"]], "reset")
    run_to_plan(two)
    assert proposal(con, "Heat.1995")["status"] != "skipped"


def test_past_runs_still_verify_and_undo(two):
    con, cfg = two["con"], two["cfg"]
    pid = run_to_plan(two)["plan_id"]
    res = apply.apply_plan(con, cfg, pid, execute=True, log=lambda *a: None)
    original = two["second"] / "in/Heat.1995.1080p.BluRay.x264-GRP.mkv"
    assert res["verify"]["ok"] and not original.exists()
    drop(two)
    assert apply.verify(con, res["run_id"], log=lambda *a: None)["ok"]
    apply.undo(con, cfg, res["run_id"], execute=True, log=lambda *a: None)
    assert original.exists()


def test_no_roots_at_all_is_not_an_error(two):
    con, cfg = two["con"], two["cfg"]
    pid = run_to_plan(two)["plan_id"]
    cfg.roots.clear()
    assert review.summary(con, cfg, pid)["files"] == 0
    assert plan.make_plan(con, cfg, log=lambda *a: None)["counts"] == {}
