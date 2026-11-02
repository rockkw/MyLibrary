"""Version label (TASK-26): v<VERSION>+<commit>[.dirty], tied to git tags and the backlog."""

import subprocess

import pytest

from mylibrary import cli, version
from test_web import client  # noqa: F401


def git(repo, *args):
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True,
                   env={"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
                        "GIT_COMMITTER_EMAIL": "t@t", "PATH": "/usr/bin:/bin", "HOME": str(repo)})


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "MyLibrary" / "code" / "MyLibrary"
    r.mkdir(parents=True)
    (r / "VERSION").write_text("2.3.4\n")
    (r / ".gitignore").write_text("config.ini\n")
    git(r, "init", "-q")
    git(r, "add", ".")
    git(r, "commit", "-qm", "first")
    return r


def commit(repo):
    return subprocess.run(["git", "-C", str(repo), "rev-parse", "--short", "HEAD"],
                          capture_output=True, text=True).stdout.strip()


def test_clean_label(repo):
    v = version.info(repo)
    assert v["label"] == f"v2.3.4+{commit(repo)}" and not v["dirty"] and v["tag"] is None


def test_changed_tracked_file_is_dirty(repo):
    (repo / "VERSION").write_text("2.3.5\n")
    assert version.info(repo)["label"] == f"v2.3.5+{commit(repo)}.dirty"


def test_untracked_and_ignored_files_are_not_dirty(repo):
    (repo / "config.ini").write_text("secret")
    (repo / "notes.txt").write_text("x")
    assert not version.info(repo)["dirty"]


def test_tag(repo):
    git(repo, "tag", "-a", "v2.3.4", "-m", "release")
    assert version.info(repo)["tag"] == "v2.3.4"


def test_no_git(tmp_path):
    (tmp_path / "VERSION").write_text("1.2.3")
    v = version.info(tmp_path)
    assert v["label"] == "v1.2.3+unknown" and v["commit"] is None


def test_missing_version_file_never_breaks(tmp_path, monkeypatch):
    import importlib.metadata as md
    monkeypatch.setattr(md, "version", lambda name: (_ for _ in ()).throw(md.PackageNotFoundError))
    assert version.read_version(tmp_path) == "0.0.0"


def test_milestone_found_beside_the_code(repo):
    backlog = repo.parents[1] / "backlog"
    (backlog / "milestones").mkdir(parents=True)
    (backlog / "tasks").mkdir()
    (backlog / "completed").mkdir()
    (backlog / "milestones" / "m-0 - v2.3.4.md").write_text('---\nid: m-0\ntitle: "v2.3.4"\n---\n')
    (backlog / "tasks" / "task-1.md").write_text("---\nid: TASK-1\nstatus: Done\nmilestone: m-0\n---\n")
    (backlog / "tasks" / "task-2.md").write_text("---\nid: TASK-2\nstatus: To Do\nmilestone: v2.3.4\n---\n")
    (backlog / "tasks" / "task-3.md").write_text("---\nid: TASK-3\nstatus: Done\n---\n")
    (backlog / "completed" / "task-4.md").write_text("---\nid: TASK-4\nstatus: Done\nmilestone: m-0\n---\n")
    assert version.milestone(repo) == {"id": "m-0", "title": "v2.3.4", "done": 2, "total": 3}
    assert version.milestone(repo, "9.9.9") is None


def test_no_backlog_means_no_milestone(repo):
    assert version.milestone(repo) is None


def test_this_repo_reads_its_version_file():
    assert version.read_version() == (version.ROOT / "VERSION").read_text().strip()
    import mylibrary
    assert mylibrary.__version__ == version.read_version()


def test_cli_version(capsys):
    with pytest.raises(SystemExit):
        cli.main(["--version"])
    assert capsys.readouterr().out.strip() == f"mylib {version.label()}"


def test_api_version(client):  # noqa: F811
    v = client.get("/api/version").get_json()
    assert v["label"] == version.label() and v["version"] == version.read_version()
    assert set(v) >= {"commit", "branch", "dirty", "tag", "milestone"}


def test_config_menu_holds_about_and_the_label(client):  # noqa: F811
    page = client.get("/").get_data(as_text=True)
    menu = page[page.index('id="config-menu"'):page.index('id="config-menu"') + 600]
    assert 'id="about-btn"' in menu and 'id="app-version"' in menu
    assert 'id="about-overlay"' in page and 'id="version"' not in page
    assert page.index('id="stats-bar"') < page.index('id="config-menu"')  # right of the stats
