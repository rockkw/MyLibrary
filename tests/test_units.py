"""The systemd units: the timer may only plan, and the UI must never be exposed."""

import configparser
import fcntl
from pathlib import Path

import pytest

from mylibrary import apply, cli
from test_pipeline import run_to_plan

UNITS = Path(__file__).resolve().parents[1] / "infra" / "compose"


def unit(name):
    cp = configparser.ConfigParser(interpolation=None, strict=False)
    cp.optionxform = str
    cp.read(UNITS / name)
    return cp


def test_every_unit_is_present():
    assert {p.name for p in UNITS.glob("mylibrary-*")} == {
        "mylibrary-web.service", "mylibrary-pipeline.service", "mylibrary-pipeline.timer"}


def test_nothing_but_the_ui_and_the_pipeline_runs():
    assert unit("mylibrary-web.service")["Service"]["ExecStart"] == "%h/.local/bin/mylib serve"
    assert unit("mylibrary-pipeline.service")["Service"]["ExecStart"] == "%h/.local/bin/mylib pipeline"


def test_no_unit_can_apply_undo_or_clean():
    for path in UNITS.glob("mylibrary-*"):
        lines = [x for x in path.read_text().splitlines() if not x.lstrip().startswith("#")]
        text = "\n".join(lines)
        for word in ("apply", "undo", "--execute", "--clean", "leftovers", "0.0.0.0", "rm ", "mv "):
            assert word not in text, f"{path.name} mentions {word}"


def test_the_timer_runs_the_pipeline_nightly_and_catches_up():
    timer = unit("mylibrary-pipeline.timer")
    assert timer["Timer"]["OnCalendar"] == "*-*-* 03:17"
    assert timer["Timer"]["Persistent"] == "true"
    assert "Unit" not in timer["Timer"]  # so it starts mylibrary-pipeline.service
    assert timer["Install"]["WantedBy"] == "timers.target"


def test_restarting_the_ui_lets_a_running_job_finish():
    web = unit("mylibrary-web.service")["Service"]
    assert web["KillMode"] == "process" and web["Restart"] == "on-failure"


def test_units_do_not_hardcode_anyones_paths():
    for path in UNITS.glob("mylibrary-*"):
        assert "/home/" not in path.read_text()


def hold_lock(cfg):
    fh = open(str(cfg.db_file) + ".lock", "a")
    fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    return fh


def test_pipeline_skips_while_an_apply_runs(lib):
    run_to_plan(lib)
    plans = lib["con"].execute("SELECT COUNT(*) FROM plans").fetchone()[0]
    fh = hold_lock(lib["cfg"])
    try:
        with pytest.raises(SystemExit) as e:
            cli.main(["--config", str(lib["ini"]), "pipeline"])
        assert "pipeline skipped" in str(e.value)
    finally:
        fh.close()
    assert lib["con"].execute("SELECT COUNT(*) FROM plans").fetchone()[0] == plans


def test_apply_refuses_while_a_pipeline_holds_the_lock(lib):
    pid = run_to_plan(lib)["plan_id"]
    fh = hold_lock(lib["cfg"])
    try:
        with pytest.raises(RuntimeError, match="already running"):
            apply.apply_plan(lib["con"], lib["cfg"], pid, execute=True, log=lambda *a: None)
    finally:
        fh.close()


def test_pipeline_runs_and_releases_the_lock(lib):
    assert cli.main(["--config", str(lib["ini"]), "pipeline"]) in (0, None)
    hold_lock(lib["cfg"]).close()
