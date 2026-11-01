import ast
import os
from pathlib import Path

from conftest import tree_digest
from mylibrary import apply, downloads, plan, preflight, review, scan, tree
from test_pipeline import add_files, proposal, quiet, run_to_plan
from test_web import client  # noqa: F401

SRC = Path(tree.__file__).parent


def flat(node, out=None):
    out = {} if out is None else out
    for c in node["children"]:
        out[(c["path"], c["state"] if c["type"] == "file" else "dir")] = c
        if c["type"] == "dir":
            flat(c, out)
    return out


def find(data, end, state=None):
    hits = [n for r in data["roots"] for (p, s), n in flat(r).items()
            if p.endswith(end) and (state is None or s == state)]
    assert hits, end
    return hits[0]


def build(lib, **kw):
    return tree.build(lib["con"], lib["cfg"], **kw)


def test_counts_every_file_by_type(lib):
    add_files(lib, "a/song.mp3", "a/tool.exe", "a/clip.wmv", "a/unscanned.mp4")
    t = build(lib)["totals"]
    assert t == {"video": 6, "music": 1, "software": 1, "other": 2}


def test_video_total_includes_every_kind_and_untracked_files(lib):
    add_files(lib, "Acme.26.06.21.Ann.Lee.XXX.1080p.mp4", "Show.S01E02.1080p.mkv")
    run_to_plan(lib)
    add_files(lib, "later.mkv")
    data = build(lib)
    assert data["totals"]["video"] == 7
    assert find(data, "later.mkv")["state"] == "untracked"
    assert find(data, "incoming")["counts"]["video"] == 2


def test_building_changes_nothing(lib):
    run_to_plan(lib)
    before = tree_digest(lib["root"])
    paths = sorted(str(p) for p in lib["root"].rglob("*"))
    build(lib, hidden=True)
    assert tree_digest(lib["root"]) == before
    assert sorted(str(p) for p in lib["root"].rglob("*")) == paths


def test_moves_match_the_dry_run_including_sidecars(lib):
    con, cfg = lib["con"], lib["cfg"]
    run_to_plan(lib)
    data = build(lib)
    prep = preflight.prepare(con, cfg, plan.current_plan(con))
    pairs = [(m["src"], m["dst"]) for m in prep["moves"]]
    pairs += [s for m in prep["moves"] for s in m["sidecars"]]
    assert len(pairs) > len(prep["moves"])  # a sidecar is in play
    found = {(n["path"], n["to"]) for (_, s), n in flat(data["roots"][0]).items() if s == "out"}
    assert found == set(pairs)
    arrived = {(n["from"], n["path"]) for (_, s), n in flat(data["roots"][0]).items() if s == "in"}
    assert arrived == set(pairs) and data["moves"] == len(pairs)


def test_new_folders_and_change_totals(lib):
    run_to_plan(lib)
    data = build(lib)
    movies = find(data, "/Movies", "dir")
    assert movies["new"] and movies["in"] == 3
    assert find(data, "/incoming", "dir")["out"] == 3
    assert data["roots"][0]["in"] == data["roots"][0]["out"] == 3


def test_incoming_files_do_not_change_the_totals(lib):
    before = build(lib)["totals"]
    run_to_plan(lib)
    assert build(lib)["totals"] == before


def test_files_that_will_not_move_say_why(lib):
    con, cfg = lib["con"], lib["cfg"]
    run_to_plan(lib)
    data = build(lib)
    n = find(data, "final_cut_v2.mp4")
    assert n["state"] == "decide" and n["proposal_id"]
    assert find(data, "sample.mkv")["state"] == "ignored"
    review.decide(con, [proposal(con, "final_cut")["id"]], "skip")
    assert find(build(lib), "final_cut_v2.mp4")["state"] == "skipped"


def test_a_file_apply_would_refuse_is_marked(lib):
    run_to_plan(lib)
    (lib["root"] / "incoming/Alien (1979)/alien.dc.mkv").write_bytes(b"changed")
    n = find(build(lib), "alien.dc.mkv")
    assert n["state"] == "decide" and "apply refuses" in n["reason"]


def test_downloading_and_torrent_files_are_marked(lib, monkeypatch):
    run_to_plan(lib)
    root = lib["root"]
    active = downloads.Active(paths={str(root / "misc/notes.txt")},
                              held={str(root / "misc/final_cut_v2.mp4"): "A Torrent"})
    monkeypatch.setattr(tree.downloads, "check", lambda cfg: active)
    data = build(lib)
    assert find(data, "notes.txt")["state"] == "downloading"
    assert find(data, "final_cut_v2.mp4")["torrent"] == "A Torrent"


def test_left_behind_only_when_no_media_remains(lib):
    run_to_plan(lib)
    data = build(lib)
    assert find(data, "incoming/Alien (1979)", "dir")["left"]
    assert find(data, "/incoming", "dir")["left"]
    assert not find(data, "/misc", "dir")["left"]


def test_library_folders_are_never_left_behind(lib):
    add_files(lib, "Movies/Acme.26.06.21.Ann.Lee.XXX.1080p.mp4")
    run_to_plan(lib)
    data = build(lib)
    n = find(data, "/Movies", "dir")
    assert n["out"] == 1 and not n["left"]


def test_stale_plan_is_flagged(lib):
    con = lib["con"]
    run_to_plan(lib)
    assert not build(lib)["stale"]
    con.execute("UPDATE plans SET created_at='2000-01-01T00:00:00'")
    assert build(lib)["stale"]


def test_no_plan_still_shows_the_disk(lib):
    data = build(lib)
    assert data["plan_id"] is None and data["moves"] == 0
    assert find(data, "notes.txt")["state"] == "untracked"


def test_hidden_files_and_safety_folder(lib):
    con, cfg = lib["con"], lib["cfg"]
    add_files(lib, "misc/._junk.mp4")
    run_to_plan(lib)
    apply.apply_plan(con, cfg, plan.current_plan(con), execute=True, log=quiet)
    plain, shown = build(lib), build(lib, hidden=True)
    names = lambda d: [p for p, _ in flat(d["roots"][0])]
    assert not any("._junk" in p or ".mylibrary-safety" in p for p in names(plain))
    assert any("._junk" in p for p in names(shown))
    assert any(".mylibrary-safety" in p for p in names(shown))


def test_symlinks_are_listed_but_not_followed(lib, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.mkv").write_bytes(b"x")
    os.symlink(outside, lib["root"] / "link")
    os.symlink(lib["root"], lib["root"] / "misc/loop")
    data = build(lib)
    assert find(data, "/link")["symlink"] and find(data, "/link")["type"] == "file"
    assert not any("secret" in p for p, _ in flat(data["roots"][0]))


def test_missing_root_is_reported(lib, tmp_path):
    lib["cfg"].roots.append(tmp_path / "unplugged")
    assert build(lib)["missing"] == [str(tmp_path / "unplugged")]


def test_tree_modules_stay_read_only():
    for name in ("tree.py", "preflight.py"):
        names = set()
        for node in ast.walk(ast.parse((SRC / name).read_text())):
            if isinstance(node, ast.Import):
                names |= {a.name for a in node.names}
            elif isinstance(node, ast.ImportFrom):
                names |= {a.name for a in node.names} | {node.module}
        assert not names & {"apply", "safefs", "mylibrary.apply", "mylibrary.safefs"}


def test_tree_endpoint(client, lib):  # noqa: F811
    d = client.get("/api/tree").get_json()
    assert d["roots"][0]["path"] == str(lib["root"]) and d["moves"] == 3
    assert client.post("/api/tree").status_code == 405


def test_tree_tab_comes_before_review(client):  # noqa: F811
    page = client.get("/").get_data(as_text=True)
    assert page.index('data-page="tree"') < page.index('data-page="review"')
    assert 'id="tree-toggle"' in page
