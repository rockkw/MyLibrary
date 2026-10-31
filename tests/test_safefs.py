import pytest

from mylibrary import safefs


def test_move_works(tmp_path):
    a = tmp_path / "a.mkv"; a.write_bytes(b"x")
    safefs.safe_move(a, tmp_path / "sub" / "b.mkv")
    assert not a.exists() and (tmp_path / "sub" / "b.mkv").read_bytes() == b"x"


def test_never_overwrites(tmp_path):
    a = tmp_path / "a.mkv"; a.write_bytes(b"new")
    b = tmp_path / "b.mkv"; b.write_bytes(b"precious")
    with pytest.raises(safefs.RefusedError):
        safefs.safe_move(a, b)
    assert b.read_bytes() == b"precious" and a.read_bytes() == b"new"


def test_missing_source_refused(tmp_path):
    with pytest.raises(safefs.RefusedError):
        safefs.safe_move(tmp_path / "nope", tmp_path / "x")


def test_kernel_noreplace_even_if_precheck_raced(tmp_path, monkeypatch):
    """Simulate a race: the pre-check says the target is free, but it is not."""
    a = tmp_path / "a"; a.write_bytes(b"new")
    b = tmp_path / "b"; b.write_bytes(b"precious")
    real = safefs.os.path.lexists
    monkeypatch.setattr(safefs.os.path, "lexists", lambda p: False if str(p) == str(b) else real(p))
    with pytest.raises(safefs.RefusedError):
        safefs.safe_move(a, b)
    assert b.read_bytes() == b"precious"


def test_snapshot_link_shares_data(tmp_path):
    a = tmp_path / "a"; a.write_bytes(b"x")
    safefs.snapshot_link(a, tmp_path / "snap" / "a")
    assert (tmp_path / "snap" / "a").stat().st_ino == a.stat().st_ino
    with pytest.raises(FileExistsError):
        safefs.snapshot_link(a, tmp_path / "snap" / "a")
