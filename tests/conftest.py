import hashlib
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from mylibrary import config, db  # noqa: E402


@pytest.fixture
def lib(tmp_path, monkeypatch):
    """A tiny fake library + config. Files are not real video (ffprobe errors are fine)."""
    root = tmp_path / "root"
    files = {
        "incoming/The.Matrix.1999.1080p.BluRay.x264-GRP.mkv": b"matrix" * 100,
        "incoming/The.Matrix.1999.1080p.BluRay.x264-GRP.en.srt": b"subs",
        "incoming/Alien (1979)/alien.dc.mkv": b"alien" * 100,
        "misc/final_cut_v2.mp4": b"mystery" * 100,
        "misc/Movie.Name.2019.sample.mkv": b"sample",
        "misc/notes.txt": b"not a video",
    }
    for rel, data in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
    ini = tmp_path / "config.ini"
    ini.write_text(f"""
[paths]
db_file = {tmp_path}/state/inv.db
[library]
roots = {root}
min_duration_minutes = 0
settle_minutes = 0
[ollama]
enabled = false
""")
    monkeypatch.setenv("MYLIBRARY_CONFIG", str(ini))
    cfg = config.load(ini)
    con = db.connect(cfg.db_file)
    return {"root": root, "cfg": cfg, "con": con, "ini": ini}


def tree_digest(root: Path) -> dict:
    """{sha256: count} of every file outside the safety dir — content survives renames."""
    out = {}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d != ".mylibrary-safety"]
        for n in filenames:
            h = hashlib.sha256(Path(dirpath, n).read_bytes()).hexdigest()
            out[h] = out.get(h, 0) + 1
    return out
