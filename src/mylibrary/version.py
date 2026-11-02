"""The MyLibrary version label, as in MyMagnet (TASK-26). Read only: it runs git, never changes it.

The version (MAJOR.MINOR.PATCH, semantic versioning) lives in the VERSION file at the repo root;
git adds which commit is running. The label is SemVer with build metadata:

    v1.0.0+2fa330e          the code at commit 2fa330e
    v1.0.0+2fa330e.dirty    ... with uncommitted changes to tracked files
    v1.0.0+unknown          no git (e.g. a copy without .git)

PATCH = bug fixes, MINOR = new features, MAJOR = breaking changes. Each release gets a git tag
v<VERSION>, a backlog milestone of the same name and a CHANGELOG.md entry.
"""

from __future__ import annotations

import subprocess
from functools import lru_cache
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _git(root: Path, *args: str) -> str | None:
    try:
        r = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True,
                           timeout=5)
    except (OSError, subprocess.SubprocessError):
        return None
    return r.stdout.strip() if r.returncode == 0 else None


def read_version(root: Path = ROOT) -> str:
    try:
        return (Path(root) / "VERSION").read_text().strip() or "0.0.0"
    except OSError:  # installed from a wheel: the build copied VERSION into the metadata
        try:
            from importlib.metadata import version
            return version("mylibrary")
        except Exception:
            return "0.0.0"


def info(root: Path | str = ROOT) -> dict:
    """{version, commit, branch, dirty, tag, label} for the checkout at root."""
    root = Path(root)
    version = read_version(root)
    commit = _git(root, "rev-parse", "--short", "HEAD")
    branch = _git(root, "rev-parse", "--abbrev-ref", "HEAD") if commit else None
    # Uncommitted changes to tracked files; git-ignored files such as config.ini don't count.
    dirty = bool(commit) and _git(root, "status", "--porcelain", "--untracked-files=no") not in ("", None)
    tag = _git(root, "describe", "--tags", "--exact-match", "HEAD") if commit else None
    label = f"v{version}+{commit or 'unknown'}" + (".dirty" if dirty else "")
    return {"version": version, "commit": commit, "branch": branch, "dirty": dirty, "tag": tag,
            "label": label}


def backlog_dir(root: Path | str = ROOT) -> Path | None:
    """MyLibrary's backlog sits beside the code (MyLibrary/backlog, code in MyLibrary/code/...)."""
    root = Path(root)
    for d in (root, *list(root.parents)[:2]):
        if (d / "backlog" / "milestones").is_dir():
            return d / "backlog"
    return None


def _front(path: Path) -> dict:
    text = path.read_text(errors="replace")
    head = text.split("---")[1] if text.startswith("---") else ""
    return {k.strip(): v.strip().strip("'\"") for k, v in
            (line.split(":", 1) for line in head.splitlines() if ":" in line and not line.startswith(" "))}


def milestone(root: Path | str = ROOT, version: str | None = None) -> dict | None:
    """The backlog milestone v<version>: {id, title, done, total}, or None."""
    backlog = backlog_dir(root)
    if not backlog:
        return None
    title = f"v{version or read_version(Path(root))}"
    mid = next((f.get("id") for f in map(_front, sorted((backlog / "milestones").glob("*.md")))
                if f.get("title") == title), None)
    if not mid:
        return None
    done = total = 0
    for folder in ("tasks", "completed"):
        for f in map(_front, (backlog / folder).glob("*.md")):
            if f.get("milestone") in (mid, title):
                total += 1
                done += f.get("status") == "Done"
    return {"id": mid, "title": title, "done": done, "total": total}


@lru_cache(maxsize=1)
def current() -> dict:
    """The running code's version, worked out once at start-up."""
    return info()


def label() -> str:
    return current()["label"]
