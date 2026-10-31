"""Build-breaking guard: no code path in MyLibrary may delete or overwrite files."""

import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
# Python only: the browser JS has no filesystem access; it can only call the Flask API.
SOURCES = [p for p in ROOT.rglob("*.py") if "tests" not in p.parts and ".git" not in p.parts]

FORBIDDEN = [
    r"\bos\.remove\b", r"\bos\.unlink\b", r"\.unlink\(", r"\brmtree\b", r"\bos\.rmdir\b",
    r"\.rmdir\(", r"\bshutil\.move\b", r"\bshutil\.copy", r"\bsend2trash\b",
    r"\bos\.rename\b", r"\bos\.replace\b",
    r"\.rename\(", r"\bos\.truncate\b", r"['\"]rm['\"]", r"\brm -", r"\bmv\b['\" ]",
    r"open\([^)]*['\"]w", r"\.write_bytes\(", r"\.write_text\(",
]


def test_no_forbidden_calls():
    hits = []
    for path in SOURCES:
        for n, line in enumerate(path.read_text().splitlines(), 1):
            code = line.split("#", 1)[0] if path.suffix == ".py" else line
            for pat in FORBIDDEN:
                if re.search(pat, code):
                    hits.append(f"{path.relative_to(ROOT)}:{n}: {line.strip()}")
    assert not hits, "delete/overwrite-capable calls found:\n" + "\n".join(hits)


def test_no_path_replace_calls():
    """Path.replace(target) overwrites; str.replace(a, b) always has two args."""
    bad = []
    for path in SOURCES:
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
                    and node.func.attr == "replace" and len(node.args) < 2:
                bad.append(f"{path.relative_to(ROOT)}:{node.lineno}")
    assert not bad, bad


def test_only_safefs_renames():
    for path in SOURCES:
        if path.suffix != ".py" or path.name == "safefs.py":
            continue
        text = path.read_text()
        assert "renameat2" not in text and "renamex_np" not in text, path


def test_web_never_imports_filesystem_modules():
    tree = ast.parse((ROOT / "src" / "mylibrary" / "web" / "app.py").read_text())
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            names.update(a.name for a in node.names)
            names.add(node.module or "")
        elif isinstance(node, ast.Import):
            names.update(a.name for a in node.names)
    assert not names & {"apply", "safefs", "mylibrary.apply", "mylibrary.safefs", "shutil"}


def test_safe_move_has_no_fallback():
    src = (ROOT / "src" / "mylibrary" / "safefs.py").read_text()
    assert "RENAME_NOREPLACE" in src and "RENAME_EXCL" in src
    assert "except" not in src.split("def safe_move", 1)[1].split("def snapshot_link")[0]
