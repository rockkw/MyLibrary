# MyLibrary — notes for Claude Code

Local, LLM-assisted movie library organizer for Rock's home media server
(Ubuntu box, SSD1/SSD2/SSD3 on an OWC Thunderbolt dock, Jellyfin, Ollama on an
RTX 5080). Renames messy movie files into Jellyfin layout
`Movies/Title (Year)/Title (Year) - 1080p.ext`. **It must never delete or
overwrite a file.**

## Hard rules (do not break, do not weaken tests to make a change pass)
- `src/mylibrary/safefs.py` is the ONLY module that changes the filesystem:
  `make_dirs`, `snapshot_link` (hard link), `safe_move` (atomic no-replace rename:
  `renameat2 RENAME_NOREPLACE` on Linux, `renamex_np RENAME_EXCL` on macOS).
  No copy+delete, no fallback, cross-drive moves refused.
- `tests/test_no_delete.py` bans `os.remove`, `unlink`, `rmtree`, `shutil.move/copy`,
  `os.rename`, `os.replace`, `Path.replace`, file writes, `rm`/`mv` outside safefs.
- The web UI (`src/mylibrary/web/app.py`) must not import `apply` or `safefs`. It only
  writes decisions to SQLite and launches whitelisted `mylib` CLI jobs.
- LLM output is a suggestion only (JSON, no tools). Confidence capped at 0.85 so
  LLM-only answers always need human review unless the offline IMDb check confirms.
- Duplicates and empty folders: report only. Snapshot folders
  (`<root>/.mylibrary-safety/run-N/`) are removed by the user, never by code.
- When testing on real media: dry run first (`mylib apply` without `--execute`).
  Never run `--execute` or `undo --execute` on real drives without Rock's explicit OK.

## Principles (from Rock's Archetype)
- Open source, self-hosted, no cloud services. Google Drive is the one exception
  (docs + backlog). No external metadata APIs; IMDb data is an offline import.
- CLI-first, config-driven (`config.ini`, `${VAR:-default}` expansion like MyMagnet).
- Security / IAM: UI binds to 127.0.0.1 or the Tailscale IP, never 0.0.0.0.
- `pip3 install ... --break-system-packages` is the accepted pattern on this box.

## Layout
```
src/mylibrary/   config db scan parse classify imdb plan review apply safefs dupes cli
src/mylibrary/web/  Flask UI (MyMagnet look: #111 bg, #f90 amber, three panes)
tests/           pytest (25 tests)
infra/compose/   systemd user units (web service, nightly pipeline timer: no renames)
mylib.py         run-from-repo shim
```

## Commands
```bash
pip3 install -e ".[test]" --break-system-packages
pytest -q
mylib pipeline        # scan + parse + classify + plan
mylib review | approve ID | skip ID | edit ID --title T --year Y
mylib apply           # dry run; --execute to rename (then auto-verify)
mylib runs | verify --run N | undo --run N [--execute] | dupes | status
mylib serve           # UI on http://127.0.0.1:5051
```

## Project docs and backlog (Google Drive, Archetype layout)
`My Drive/Projects/MyLibrary/` (synced to Ubuntu via rclone): `docs/` (overview,
architecture, runbook), `diagrams/` (Mermaid), `backlog/` (Backlog.md: tasks,
decisions), `testing/` (plans, results, experiments), `config/`.
Use the `backlog` CLI for task changes rather than hand-editing task files.

## Status (2026-10-31)
- v0.1.0 on GitHub, CI green. Not yet run on real drives.
- Next: TASK-3 first real run on ONE SSD per `testing/plans/first-real-run-test-plan.md`.
- Then: TASK-4 compare Ollama models, TASK-5 IMDb import, TASK-6 keep editions
  (Director's Cut etc. currently dropped from names).
