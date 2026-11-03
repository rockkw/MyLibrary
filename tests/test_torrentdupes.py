"""TASK-33: pause a torrent that would download a file the library already has.

The torrents here are real .torrent files built in the test, with SHA-1 piece hashes, so the
checksum match runs exactly as on a live client. Only the RPC is faked.
"""

import hashlib
import io
import json
import re
import urllib.error
from pathlib import Path

import pytest

from mylibrary import cli, downloads, scan, torrentdupes
from mylibrary.torrentdupes import BencodeError, bdecode, piece_indexes

PLEN = 16384
SRC = Path(__file__).resolve().parents[1] / "src" / "mylibrary"


def benc(v) -> bytes:
    if isinstance(v, int):
        return b"i%de" % v
    if isinstance(v, str):
        v = v.encode()
    if isinstance(v, bytes):
        return b"%d:%s" % (len(v), v)
    if isinstance(v, list):
        return b"l" + b"".join(benc(x) for x in v) + b"e"
    return b"d" + b"".join(benc(k) + benc(v[k]) for k in sorted(v)) + b"e"


def blob(seed: str, size: int) -> bytes:
    out, i = b"", 0
    while len(out) < size:
        out += hashlib.sha256(f"{seed}{i}".encode()).digest()
        i += 1
    return out[:size]


def make_torrent(path: Path, name: str, files: list[tuple[str, bytes]], single=False, v2_only=False):
    data = b"".join(d for _, d in files)
    pieces = b"".join(hashlib.sha1(data[i:i + PLEN]).digest() for i in range(0, len(data), PLEN))
    info = {"name": name, "piece length": PLEN}
    if single:
        info["length"] = len(files[0][1])
    else:
        info["files"] = [{"length": len(d), "path": rel.split("/")} for rel, d in files]
    if v2_only:
        info["meta version"] = 2
    else:
        info["pieces"] = pieces
    path.write_bytes(benc({"announce": "http://tracker.invalid/", "info": info}))
    return str(path)


class FakeResp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class RPC:
    """Session-id handshake, torrent-get, and a record of every other method asked for."""

    def __init__(self, torrents):
        self.torrents, self.calls = torrents, []

    def __call__(self, req, timeout):
        if req.get_header("X-transmission-session-id") != "s1":
            raise urllib.error.HTTPError(req.full_url, 409, "Conflict",
                                         {"X-Transmission-Session-Id": "s1"}, io.BytesIO())
        body = json.loads(req.data)
        self.calls.append(body)
        args = {"torrents": self.torrents} if body["method"] == "torrent-get" else {}
        return FakeResp(json.dumps({"result": "success", "arguments": args}).encode())

    def stops(self):
        return [c["arguments"]["ids"] for c in self.calls if c["method"] == "torrent-stop"]


@pytest.fixture
def setup(lib, tmp_path):
    """A library holding one known film, scanned, and a download folder outside it."""
    cfg = lib["cfg"]
    cfg.transmission_enabled, cfg.dupe_min_mb = True, 0  # test files are KBs, not MBs
    film = blob("film", PLEN * 5 + 123)
    have = lib["root"] / "Movies" / "Some Film (2001)" / "Some Film (2001) - 1080p.mkv"
    have.parent.mkdir(parents=True)
    have.write_bytes(film)
    scan.scan(lib["con"], cfg, log=lambda *a: None)
    dl = tmp_path / "downloads"
    dl.mkdir()
    return {**lib, "film": film, "have": str(have), "dl": dl, "tdir": tmp_path}


def torrent(setup, name, files, left=1, single=False, **extra):
    tf = make_torrent(setup["tdir"] / f"{name}.torrent", name, files, single=single,
                      v2_only=extra.pop("v2_only", False))
    rpc_files = [{"name": name if single else f"{name}/{rel}", "length": len(d),
                  "bytesCompleted": 0 if left else len(d)} for rel, d in files]
    return {"id": 1, "hashString": hashlib.sha1(name.encode()).hexdigest(), "name": name,
            "status": 4, "downloadDir": str(setup["dl"]), "torrentFile": tf, "files": rpc_files,
            "wanted": [True] * len(files), "leftUntilDone": left, "percentDone": 0.1,
            "metadataPercentComplete": 1, **extra}


def test_identical_file_under_another_name_is_found_by_checksum(setup):
    t = torrent(setup, "Totally.Different.Name", [("x.mkv", setup["film"]), ("info.nfo", b"n")])
    res = torrentdupes.find(setup["con"], setup["cfg"], RPC([t]))
    assert res["error"] is None and res["checked"] == 1
    assert [(d["file"], d["match"], d["pieces"]) for d in res["dupes"]] == [
        ("Totally.Different.Name/x.mkv", setup["have"], 5)]


def test_single_file_torrent(setup):
    t = torrent(setup, "Film.2001.mkv", [("Film.2001.mkv", setup["film"])], single=True)
    assert len(torrentdupes.find(setup["con"], setup["cfg"], RPC([t]))["dupes"]) == 1


def test_same_size_but_different_bytes_is_not_a_duplicate(setup):
    other = bytearray(setup["film"])
    other[PLEN * 3 + 7] ^= 0xFF  # one byte in piece 3, which the 3-piece sample (0, 2, 4) misses
    t = torrent(setup, "Same.Size", [("x.mkv", bytes(other))])
    assert torrentdupes.find(setup["con"], setup["cfg"], RPC([t]))["dupes"] == []


def test_file_at_an_unaligned_offset_in_a_pack_is_still_matched(setup):
    t = torrent(setup, "Pack", [("a.mkv", blob("other", PLEN * 3 + 999)), ("b.mkv", setup["film"])])
    res = torrentdupes.find(setup["con"], setup["cfg"], RPC([t]))
    assert [d["file"] for d in res["dupes"]] == ["Pack/b.mkv"]


def test_small_unwanted_and_non_media_files_are_not_checked(setup):
    setup["cfg"].dupe_min_mb = 0
    nfo = setup["root"] / "Movies" / "Some Film (2001)" / "film.nfo"
    nfo.write_bytes(blob("nfo", PLEN * 3))
    setup["con"].execute("INSERT INTO files (path, root, size, missing) VALUES (?,?,?,0)",
                         (str(nfo), str(setup["root"]), PLEN * 3))
    t = torrent(setup, "T", [("film.nfo", blob("nfo", PLEN * 3)), ("x.mkv", setup["film"])])
    t["wanted"] = [True, False]
    assert torrentdupes.find(setup["con"], setup["cfg"], RPC([t]))["dupes"] == []
    setup["cfg"].dupe_min_mb = 1
    t["wanted"] = [True, True]
    assert torrentdupes.find(setup["con"], setup["cfg"], RPC([t]))["dupes"] == []  # 80 KB < 1 MB


def test_a_missing_library_file_or_the_torrents_own_file_is_no_proof(setup):
    t = torrent(setup, "Film.2001.mkv", [("Film.2001.mkv", setup["film"])], single=True)
    own = setup["dl"] / "Film.2001.mkv"
    own.write_bytes(setup["film"])  # a re-verified torrent whose own data was scanned before
    setup["con"].execute("INSERT INTO files (path, root, size, missing) VALUES (?,?,?,0)",
                         (str(own), str(setup["root"]), len(setup["film"])))
    setup["con"].execute("UPDATE files SET missing=1 WHERE path=?", (setup["have"],))
    assert torrentdupes.find(setup["con"], setup["cfg"], RPC([t]))["dupes"] == []


def test_finished_waiting_and_v2_only_torrents(setup):
    done = torrent(setup, "Done", [("x.mkv", setup["film"])], left=0, percentDone=1)
    magnet = {**torrent(setup, "Magnet", [("x.mkv", setup["film"])]),
              "metadataPercentComplete": 0.2, "torrentFile": ""}
    v2 = torrent(setup, "V2", [("x.mkv", setup["film"])], v2_only=True)
    res = torrentdupes.find(setup["con"], setup["cfg"], RPC([done, magnet, v2]))
    assert res["dupes"] == [] and res["checked"] == 0
    assert res["waiting"] == ["Magnet"] and res["unverifiable"] == ["V2"]


def test_pause_once_and_a_resume_sticks(setup):
    t = torrent(setup, "Pack", [("a.mkv", blob("new", PLEN * 4)), ("b.mkv", setup["film"])])
    rpc = RPC([t])
    res = torrentdupes.pause_dupes(setup["con"], setup["cfg"], rpc, log=lambda *a: None)
    assert res["paused"] == ["Pack"] and rpc.stops() == [[t["hashString"]]]
    rows = torrentdupes.listing(setup["con"])
    assert len(rows) == 1 and rows[0]["paused_at"] and rows[0]["match"] == setup["have"]
    # the user resumed it in Transmission: the next run leaves it running
    res = torrentdupes.pause_dupes(setup["con"], setup["cfg"], rpc, log=lambda *a: None)
    assert res["paused"] == [] and res["skipped"] == ["Pack"] and len(rpc.stops()) == 1


def test_report_only_never_pauses_or_records(setup, monkeypatch, capsys):
    t = torrent(setup, "T", [("x.mkv", setup["film"])])
    rpc = RPC([t])
    real = torrentdupes.find
    monkeypatch.setattr(torrentdupes, "find", lambda con, cfg: real(con, cfg, rpc))
    monkeypatch.setattr("mylibrary.config.load", lambda path=None: setup["cfg"])
    assert cli.main(["downloads"]) == 0
    assert "Nothing was paused" in capsys.readouterr().out
    assert rpc.stops() == [] and torrentdupes.listing(setup["con"]) == []


def test_unreachable_or_disabled_client_changes_nothing(setup):
    def down(req, timeout):
        raise urllib.error.URLError("refused")
    res = torrentdupes.pause_dupes(setup["con"], setup["cfg"], down, log=lambda *a: None)
    assert res["error"].startswith("cannot check downloads") and res["paused"] == []
    setup["cfg"].transmission_enabled = False
    assert "not enabled" in torrentdupes.find(setup["con"], setup["cfg"], down)["error"]


def test_failed_pause_is_recorded_and_tried_again(setup):
    t = torrent(setup, "T", [("x.mkv", setup["film"])])

    class Refuses(RPC):
        def __call__(self, req, timeout):
            if json.loads(req.data or b"{}").get("method") == "torrent-stop" \
                    and req.get_header("X-transmission-session-id") == "s1":
                raise urllib.error.HTTPError(req.full_url, 500, "boom", {}, io.BytesIO())
            return super().__call__(req, timeout)

    res = torrentdupes.pause_dupes(setup["con"], setup["cfg"], Refuses([t]), log=lambda *a: None)
    assert res["paused"] == [] and torrentdupes.listing(setup["con"])[0]["error"]
    rpc = RPC([t])
    assert torrentdupes.pause_dupes(setup["con"], setup["cfg"], rpc, log=lambda *a: None)["paused"] == ["T"]


def test_cli_pause_dupes(setup, monkeypatch, capsys):
    t = torrent(setup, "T", [("x.mkv", setup["film"])])
    rpc = RPC([t])
    real = torrentdupes.pause_dupes
    monkeypatch.setattr(torrentdupes, "pause_dupes",
                        lambda con, cfg, log=print: real(con, cfg, rpc, log=log))
    monkeypatch.setattr("mylibrary.config.load", lambda path=None: setup["cfg"])
    assert cli.main(["downloads", "--pause-dupes"]) == 0
    out = capsys.readouterr().out
    assert "[duplicate] T" in out and "paused: T" in out and rpc.stops()


def test_rpc_refuses_every_method_but_get_and_stop(setup):
    with pytest.raises(ValueError, match="not allowed"):
        downloads.call(setup["cfg"], RPC([]), "torrent-remove", {"ids": ["x"]})
    with pytest.raises(ValueError, match="not allowed"):
        downloads.call(setup["cfg"], RPC([]), "torrent-set", {"ids": ["x"]})


def test_no_code_names_a_torrent_changing_method_but_stop():
    pattern = re.compile(r"torrent-(remove|set|add|start|verify|reannounce|rename|set-location)"
                         r"|queue-move|session-set|free-space")
    hits = [f"{p.name}:{n}" for p in SRC.rglob("*.py")
            for n, line in enumerate(p.read_text().splitlines(), 1) if pattern.search(line)]
    assert hits == []


@pytest.mark.parametrize("bad", [b"", b"i12", b"5:ab", b"l", b"d1:ai1e", b"di1ei2ee",
                                 b"i1ei2e", b"x", b"l" * 200 + b"e" * 200, b"-1:a"])
def test_bdecode_rejects_bad_input(bad):
    with pytest.raises(BencodeError):
        bdecode(bad)


def test_bdecode_round_trip():
    v = {b"a": [1, b"xy", {b"k": -3}], b"b": b""}
    assert bdecode(benc(v)) == v


def test_malformed_torrent_file_is_unverifiable(setup):
    t = torrent(setup, "Bad", [("x.mkv", setup["film"])])
    Path(t["torrentFile"]).write_bytes(b"d4:infod4:name1:x12:piece lengthi0eee")
    res = torrentdupes.find(setup["con"], setup["cfg"], RPC([t]))
    assert res["unverifiable"] == ["Bad"] and res["dupes"] == []


@pytest.mark.parametrize("offset,length,expect", [
    (0, PLEN * 5 + 1, [0, 2, 4]), (100, PLEN * 2, [1]), (0, PLEN - 1, []), (0, PLEN, [0])])
def test_only_whole_pieces_inside_the_file_are_compared(offset, length, expect):
    assert piece_indexes(offset, length, PLEN) == expect


def test_ui_lists_paused_downloads_and_can_start_the_check(setup):
    from mylibrary.web import app as webapp
    assert webapp.JOBS["pause_dupes"][0]({}) == ["downloads", "--pause-dupes"]
    setup["con"].execute("INSERT INTO torrent_dupes (hash, torrent, file, match, pieces, found_at, "
                         "paused_at) VALUES ('h','T','T/x.mkv','/m',3,'t','t')")
    setup["con"].commit()
    webapp.CFG = setup["cfg"]
    rows = webapp.app.test_client().get("/api/torrent-dupes").get_json()
    assert [r["torrent"] for r in rows] == ["T"]
