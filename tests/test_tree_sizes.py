"""TASK-32: folder size totals in the tree. Read only. All names here are made up (the repo is public)."""

import json
import os
import re
import subprocess
from pathlib import Path

import pytest

from mylibrary import apply, plan, trash, tree
from mylibrary.scan import SAFETY_DIR
from test_pipeline import add_files, quiet, run_to_plan
from test_tree import build, find, flat
from test_web import client  # noqa: F401

APP_JS = Path(tree.__file__).parent / "web" / "static" / "app.js"


def disk_bytes(root, hidden=False):
    """What the tree should add up: every file listed, symlinks by their own size."""
    total = 0
    for dirpath, dirs, files in os.walk(root):
        if not hidden:
            dirs[:] = [d for d in dirs if d != SAFETY_DIR and not d.startswith("._")]
            files = [f for f in files if not f.startswith("._")]
        links = [d for d in dirs if os.path.islink(os.path.join(dirpath, d))]  # listed as files
        total += sum(os.lstat(os.path.join(dirpath, f)).st_size for f in files + links)
    return total


def check_sums(node):
    """Each folder's bytes equal its files on disk plus its subfolders."""
    expect = 0
    for c in node["children"]:
        if c["type"] == "dir":
            expect += check_sums(c)
        elif c["state"] != "in":
            expect += c["size"]
    assert node["bytes"] == expect, node["path"]
    return expect


def test_root_and_folder_totals_match_the_disk(lib):
    add_files(lib, "a/b/song.mp3", "a/tool.exe")
    data = build(lib)
    root = data["roots"][0]
    assert root["bytes"] == disk_bytes(lib["root"]) == data["bytes"] > 0
    check_sums(root)
    a = find(data, "/a")
    assert a["bytes"] == disk_bytes(lib["root"] / "a")


def test_incoming_rows_are_not_counted(lib):
    before = build(lib)["roots"][0]["bytes"]
    run_to_plan(lib)
    data = build(lib)
    assert any(n.get("state") == "in" for n in flat(data["roots"][0]).values())
    assert data["roots"][0]["bytes"] == before
    check_sums(data["roots"][0])


def test_hidden_files_and_safety_folder_count_only_when_shown(lib):
    con, cfg = lib["con"], lib["cfg"]
    add_files(lib, "misc/._resource.mp4")
    run_to_plan(lib)
    apply.apply_plan(con, cfg, plan.current_plan(con), execute=True, log=quiet)
    shown, hidden = build(lib, hidden=True), build(lib)
    assert hidden["bytes"] == disk_bytes(lib["root"])
    assert shown["bytes"] == disk_bytes(lib["root"], hidden=True) > hidden["bytes"]
    check_sums(shown["roots"][0])


def test_files_marked_for_trash_still_count(lib):
    before = build(lib)["bytes"]
    p = str(lib["root"] / "misc/final_cut_v2.mp4")
    assert trash.mark(lib["con"], lib["cfg"], [p])["marked"]
    data = build(lib)
    assert find(data, "final_cut_v2.mp4")["state"] == "trash"
    assert data["bytes"] == before


def test_symlinks_count_their_own_size_and_are_not_followed(lib, tmp_path):
    big = tmp_path / "outside"
    big.mkdir()
    (big / "huge.mkv").write_bytes(b"x" * 100_000)
    os.symlink(big, lib["root"] / "away")
    data = build(lib)
    away = find(data, "/away")
    assert away["type"] == "file" and away["size"] == os.lstat(lib["root"] / "away").st_size
    assert data["bytes"] == disk_bytes(lib["root"]) < 100_000


def test_unreadable_folder_counts_zero(lib):
    locked = lib["root"] / "locked"
    locked.mkdir()
    (locked / "x.mkv").write_bytes(b"x" * 10)
    locked.chmod(0)
    try:
        if os.access(locked, os.R_OK):
            pytest.skip("running as root")
        assert find(build(lib), "/locked")["bytes"] == 0
    finally:
        locked.chmod(0o755)


# ── the formatter the page uses ──

def fmt_units(values):
    src = next(line for line in APP_JS.read_text().splitlines() if line.startswith("const fmtUnits"))
    out = subprocess.run(["node", "-e", f"{src}\nconsole.log(JSON.stringify({json.dumps(values)}.map(fmtUnits)))"],
                         capture_output=True, text=True, check=True).stdout
    return json.loads(out)


@pytest.mark.skipif(not __import__("shutil").which("node"), reason="node not installed")
def test_units_switch_at_mb_gb_tb_boundaries():
    cases = {0: "0.0 MB", 1: "< 0.1 MB", 49_999: "< 0.1 MB", 50_000: "0.1 MB", 512_000_000: "512.0 MB",
             999_940_000: "999.9 MB", 999_950_000: "1.0 GB", 1_000_000_000: "1.0 GB",
             3_400_000_000: "3.4 GB", 999_949_999_999: "999.9 GB", 999_950_000_000: "1.0 TB",
             1_200_000_000_000: "1.2 TB", 25_000_000_000_000: "25.0 TB"}
    assert dict(zip(cases, fmt_units(list(cases)))) == cases


# ── the page ──

def test_page_has_the_show_sizes_checkbox(client):  # noqa: F811
    page = client.get("/").get_data(as_text=True)
    assert re.search(r'<input type="checkbox" id="tree-sizes">\s*Show sizes', page)
    assert 'id="tree-sizes" checked' not in page


def test_tree_endpoint_returns_bytes(client, lib):  # noqa: F811
    d = client.get("/api/tree").get_json()
    assert d["bytes"] == d["roots"][0]["bytes"] == disk_bytes(lib["root"])
