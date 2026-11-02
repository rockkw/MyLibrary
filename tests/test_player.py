"""TASK-30: play a video from the tree in an overlay. Streaming is read only and limited to
video files inside a configured root. All names here are made up (the repo is public)."""

import ast
import os
from pathlib import Path

import pytest

from conftest import tree_digest
from mylibrary import apply, downloads, plan, player, tree
from test_pipeline import add_files, quiet, run_to_plan
from test_tree import find
from test_web import client  # noqa: F401

SRC = Path(player.__file__).parent
MATRIX = "incoming/The.Matrix.1999.1080p.BluRay.x264-GRP.mkv"


def play_url(path):
    from urllib.parse import quote
    return "/api/play?path=" + quote(str(path))


# ── which paths may be streamed ──

def test_a_video_inside_a_root_resolves(lib):
    p = lib["root"] / MATRIX
    assert player.resolve(lib["cfg"], str(p)) == os.path.realpath(p)


@pytest.mark.parametrize("bad", [
    "relative/movie.mkv", "", "/etc/passwd",
    "{root}/../outside.mkv", "{root}/incoming/../incoming/The.Matrix.1999.1080p.BluRay.x264-GRP.mkv",
    "{root}//incoming/x.mkv", "{root}/misc/notes.txt", "{root}/misc/nothing-here.mkv",
    "{root}2/elsewhere.mkv", "{root}/incoming/The.Matrix.1999.1080p.BluRay.x264-GRP.en.srt",
])
def test_paths_outside_the_rules_are_refused(lib, bad):
    with pytest.raises(player.Refused):
        player.resolve(lib["cfg"], bad.format(root=lib["root"]))


def test_a_root_name_prefix_is_not_inside_the_root(lib, tmp_path):
    other = Path(str(lib["root"]) + "2")
    other.mkdir()
    (other / "film.mkv").write_bytes(b"x")
    with pytest.raises(player.Refused, match="configured root"):
        player.resolve(lib["cfg"], str(other / "film.mkv"))


def test_symlinks_are_refused_even_to_files_inside_a_root(lib, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.mkv").write_bytes(b"x")
    os.symlink(outside, lib["root"] / "away")
    os.symlink(lib["root"] / MATRIX, lib["root"] / "misc/alias.mkv")
    for p in (lib["root"] / "away/secret.mkv", lib["root"] / "misc/alias.mkv"):
        with pytest.raises(player.Refused, match="symlink"):
            player.resolve(lib["cfg"], str(p))


def test_safety_folder_and_disc_images_are_refused(lib):
    add_files(lib, ".mylibrary-safety/run-1/Some.Film.2001.mkv", "misc/Some.Disc.2002.iso")
    with pytest.raises(player.Refused, match="safety"):
        player.resolve(lib["cfg"], str(lib["root"] / ".mylibrary-safety/run-1/Some.Film.2001.mkv"))
    with pytest.raises(player.Refused, match="video"):
        player.resolve(lib["cfg"], str(lib["root"] / "misc/Some.Disc.2002.iso"))


def test_a_removed_root_is_refused(lib):
    p = str(lib["root"] / MATRIX)
    lib["cfg"].roots.clear()
    with pytest.raises(player.Refused, match="configured root"):
        player.resolve(lib["cfg"], p)


# ── format warnings ──

def test_info_warns_about_formats_browsers_cannot_play(lib):
    add_files(lib, "misc/Old.Film.1990.avi")
    run_to_plan(lib)
    con = lib["con"]
    con.execute("UPDATE files SET vcodec='hevc', acodec='ac3' WHERE path LIKE '%Matrix%mkv'")
    d = player.info(con, lib["cfg"], str(lib["root"] / MATRIX))
    assert d["ext"] == "mkv" and d["vcodec"] == "hevc" and d["proposal_id"]
    assert any("HEVC" in w for w in d["warnings"]) and any("AC3" in w for w in d["warnings"])
    avi = player.info(con, lib["cfg"], str(lib["root"] / "misc/Old.Film.1990.avi"))
    assert any(".avi" in w for w in avi["warnings"])


def test_info_for_a_browser_friendly_file_has_no_warnings(lib):
    run_to_plan(lib)
    lib["con"].execute("UPDATE files SET vcodec='h264', acodec='aac' WHERE path LIKE '%Matrix%mkv'")
    assert player.info(lib["con"], lib["cfg"], str(lib["root"] / MATRIX))["warnings"] == []


# ── the tree offers play only where the player would allow it ──

def test_tree_offers_play_for_videos_only(lib, tmp_path):
    add_files(lib, "misc/Some.Disc.2002.iso")
    os.symlink(lib["root"] / MATRIX, lib["root"] / "misc/alias.mkv")
    data = tree.build(lib["con"], lib["cfg"])
    assert find(data, "final_cut_v2.mp4")["play"] == str(lib["root"] / "misc/final_cut_v2.mp4")
    for name in ("notes.txt", "en.srt", "Some.Disc.2002.iso", "alias.mkv"):
        assert "play" not in find(data, name)


def test_incoming_rows_play_their_source(lib):
    run_to_plan(lib)
    n = find(tree.build(lib["con"], lib["cfg"]), "The Matrix (1999).mkv", "in")
    assert n["play"] == str(lib["root"] / MATRIX)


def test_downloading_files_are_not_offered(lib, monkeypatch):
    p = str(lib["root"] / "misc/final_cut_v2.mp4")
    monkeypatch.setattr(tree.downloads, "check", lambda cfg: downloads.Active(paths={p}))
    assert "play" not in find(tree.build(lib["con"], lib["cfg"]), "final_cut_v2.mp4")


def test_safety_copies_are_not_offered_when_hidden_files_show(lib):
    con, cfg = lib["con"], lib["cfg"]
    run_to_plan(lib)
    apply.apply_plan(con, cfg, plan.current_plan(con), execute=True, log=quiet)
    data = tree.build(con, cfg, hidden=True)
    from test_tree import flat
    safety = [n for (p, _), n in flat(data["roots"][0]).items()
              if ".mylibrary-safety" in p and n["type"] == "file"]
    assert safety and not any("play" in n for n in safety)


# ── endpoints ──

def test_play_streams_the_file_with_range_support(client, lib):  # noqa: F811
    p = lib["root"] / MATRIX
    data = p.read_bytes()
    r = client.get(play_url(p))
    assert r.status_code == 200 and r.data == data
    assert r.headers["Accept-Ranges"] == "bytes" and r.mimetype == "video/x-matroska"
    assert r.headers["Content-Disposition"] == "inline"
    part = client.get(play_url(p), headers={"Range": "bytes=10-19"})
    assert part.status_code == 206 and part.data == data[10:20]
    assert part.headers["Content-Range"] == f"bytes 10-19/{len(data)}"


def test_play_refuses_with_403(client, lib, tmp_path):  # noqa: F811
    (tmp_path / "outside.mkv").write_bytes(b"x")
    for bad in (tmp_path / "outside.mkv", lib["root"] / "misc/notes.txt",
                f"{lib['root']}/../outside.mkv"):
        assert client.get(play_url(bad)).status_code == 403
        assert client.get("/api/play/info?path=" + str(bad)).status_code == 403
    assert client.get("/api/play").status_code == 403


def test_play_is_get_only(client, lib):  # noqa: F811
    for url in (play_url(lib["root"] / MATRIX), "/api/play/info"):
        assert client.post(url).status_code == 405
        assert client.delete(url).status_code == 405


def test_streaming_changes_nothing(client, lib):  # noqa: F811
    before = tree_digest(lib["root"])
    p = lib["root"] / MATRIX
    client.get(play_url(p))
    client.get(play_url(p), headers={"Range": "bytes=0-5"})
    client.get("/api/play/info?path=" + str(p))
    assert tree_digest(lib["root"]) == before


def test_info_endpoint(client, lib):  # noqa: F811
    d = client.get("/api/play/info?path=" + str(lib["root"] / MATRIX)).get_json()
    assert d["name"] == os.path.basename(MATRIX) and d["size"] == 600 and d["proposal_id"]


def test_page_has_the_player_overlay(client):  # noqa: F811
    page = client.get("/").get_data(as_text=True)
    for el in ('id="player-overlay"', 'id="player-video"', 'id="player-close"',
               'id="player-review"', 'id="player-full"', 'id="player-prev"', 'id="player-next"'):
        assert el in page


def test_player_module_stays_read_only():
    names = set()
    for node in ast.walk(ast.parse((SRC / "player.py").read_text())):
        if isinstance(node, ast.Import):
            names |= {a.name for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            names |= {a.name for a in node.names} | {node.module}
    assert not names & {"apply", "safefs", "mylibrary.apply", "mylibrary.safefs"}
