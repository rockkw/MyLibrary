# MyLibrary — notes for Claude Code

Local, LLM-assisted movie library organizer for Rock's home media server
(Ubuntu box, SSD1/SSD2/SSD3 on an OWC Thunderbolt dock, Jellyfin, Ollama on an
RTX 5080). Renames messy movie files into Jellyfin layout
`Movies/Title (Year)/Title (Year) - Director's Cut - 1080p.ext`, and TV episodes into
`Shows/Series (Year)/Season 01/Series (Year) - S01E04 - Title - 1080p.ext`. Music goes to
`Music/Artist/Album/01 - Title.ext` and software to `Software/Title/original-name.ext`. **It must never delete or
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
- `preflight.py`, `tree.py`, `analyze.py` and `player.py` are read-only and reachable from the web UI, so they
  must never import `apply` or `safefs`. `preflight.prepare` is the one check for what a run would do.
- The tree's player (`/api/play`, TASK-30) streams only through `player.resolve`: a video file in a configured
  root, no symlink anywhere on the path, nothing in `.mylibrary-safety`, no disc images. GET only.
- Taking a root out of `roots` keeps its rows, so decisions and history survive. Plans, the
  LLM step and counts filter with `cfg.root_filter()`, and `preflight.prepare` refuses that root.
- Duplicates and empty or leftover folders: report only. `mylib leftovers` suggests. Approving
  one moves it aside with `safe_move` into `.mylibrary-safety/run-N/leftovers/`, and undo
  restores it. Never delete it. No Python file may contain the removal command. Snapshot folders
  (`<root>/.mylibrary-safety/run-N/`) are removed by the user, never by code.
- Move to trash (TASK-31): the UI and `mylib trash` only record marks in the `trash` table
  (`trash.py`, read-only, with the file's inode/size/mtime). The next `apply --execute` moves ready
  marks with `safe_move` into `.mylibrary-safety/run-N/trash/`, and undo restores them. A marked file
  is held out of the plan; a changed, downloading or torrent-held file is refused. Never delete.
- Every video with an allowed extension is organized, short ones included: they go to review.
  Only `ignore_words` matches (samples, trailers) are ignored by kind.
- Episodes skip the LLM and IMDb check. Without one season and one episode number they
  have no target and go to review; multi-episode files always go to review.
- Music identity comes only from embedded tags and skips the LLM. Tags are untrusted input:
  they reach file paths, so every part goes through `plan.clean`.
- `parse.group_albums` runs after every parse, so compilation and disc-folder rules need no
  `--redo`. A release is a folder; only an album artist on every track outranks detection.
- Scenes (`kind='scene'`) the parser recognises skip the LLM and IMDb. The LLM may suggest a scene
  (site, date, performers, title) for a file the parser could not read; that always goes to review. `parse.find_scenes` runs after every parse, so
  no `--redo` is needed, and reads the first recorded move for a moved file's original name.
  Auto needs an `XXX` marker and a real date; anything else goes to review. Names are
  untrusted, so every part goes through `plan.clean`.
- Software files are never probed, opened, or run. Only installers with a clear name and
  version can be auto; archives, ISOs, and LLM-suggested titles always need review.
- Unfinished torrents are off limits, finished files in them included. `downloads.check`
  is read-only; its `.error` means unknown, so nothing auto-approves or applies. Never log
  the Transmission password.
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
src/mylibrary/   config db scan parse classify imdb plan review preflight apply safefs dupes tree analyze player trash cli
src/mylibrary/web/  Flask UI (MyMagnet look: #111 bg, #f90 amber, three panes)
tests/           pytest
infra/compose/   systemd user units (web service, nightly pipeline timer: no renames)
mylib.py         run-from-repo shim
```

## Commands
```bash
pip3 install -e ".[test]" --break-system-packages
pytest -q
mylib pipeline        # scan + parse + classify + plan
mylib review | approve ID | skip ID | edit ID --title T --year Y [--edition E]
mylib edit ID --title SERIES --season S --episode E [--episode-last N] [--episode-title T]
mylib edit ID --title TRACK --artist A --album B [--track N] [--disc N]
mylib edit ID --title PRODUCT --kind software
mylib edit ID --site S --date YYYY-MM-DD --performers "A, B" [--title T]
mylib edit ID ID ... --kind scene --site S   # shared fields; empty ones keep each file's own
mylib apply           # dry run; --execute to rename (then auto-verify)
mylib trash PATH...   # mark for trash; the next apply --execute moves them; trash [--clear PATH...]
mylib runs | verify --run N | undo --run N [--execute] | dupes | status
mylib serve           # UI on http://127.0.0.1:5051
```

## Project docs and backlog (Google Drive, Archetype layout)
`My Drive/Projects/MyLibrary/` (synced to Ubuntu via rclone): `docs/` (overview,
architecture, runbook), `diagrams/` (Mermaid), `backlog/` (Backlog.md: tasks,
decisions), `testing/` (plans, results, experiments), `config/`.
Use the `backlog` CLI for task changes rather than hand-editing task files.

## Versions (TASK-26, as in MyMagnet)
- `VERSION` holds the version (semantic versioning: PATCH = bug fixes, MINOR = features, MAJOR =
  breaking changes); pyproject and `__version__` read it. `version.py` builds the label
  `v<VERSION>+<commit>[.dirty]`; the UI shows it under ⚙ Config (and in ℹ About), `/api/version` returns it,
  `mylib serve` logs it at start-up and `mylib --version` prints it.
- A release: bump `VERSION`, add a `CHANGELOG.md` entry from the Done tasks, create the backlog
  milestone `v<VERSION>` (`backlog milestone add`) and assign the shipped tasks
  (`backlog task edit <id> -m v<VERSION>`), commit, then `git tag -a v<VERSION>`. Only bump or
  tag when Rock asks.
