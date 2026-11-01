# MyLibrary

Local, LLM-assisted organizer for a large movie collection. It identifies messy
file names, proposes Jellyfin-style names, lets you review them in a
MyMagnet-styled UI, and renames files **without ever deleting or overwriting
anything**.

- Open source and fully local: guessit, ffprobe, SQLite, Flask, Ollama.
  No cloud services.
- The CLI is the source of truth. The web UI only records decisions and runs
  whitelisted `mylib` commands.
- Standalone app on its own port (5051) with its own database. To link it from
  MyMagnet later, add a nav button pointing to `http://<host>:5051`.

## How it works

```
scan ──► parse ──► classify ──► plan ──► review ──► apply ──► verify
ffprobe  guessit   Ollama on     DB only  UI / CLI   rename    inode +
+ hash   + score   flagged only                      only      count check
```

| Step | What happens | Touches files? |
|---|---|---|
| `mylib scan` | Walks `[library] roots`, runs ffprobe, fingerprints (first+last 1 MiB). Unplugged drives are skipped, not marked missing. | read only |
| `mylib parse` | guessit on filename and parent folder, then a confidence score. Low scores are flagged. | no |
| `mylib classify` | Sends **flagged files only** to local Ollama; strict JSON schema, temperature 0. | no |
| `mylib plan` | Builds target paths `Movies/Title (Year)/Title (Year) - 1080p.ext` or `Shows/Series (Year)/Season 01/Series (Year) - S01E04 - Title - 1080p.ext`, detects conflicts, auto-approves ≥ 0.90. | no |
| review | Approve / skip / edit in the UI or with `mylib approve|skip|edit`. | no |
| `mylib apply` | **Dry run.** Add `--execute` to rename. | renames only |
| `mylib verify --run N` | Re-checks a run any time. | no |
| `mylib undo --run N` | Dry run; `--execute` reverses a whole run. | renames only |


**Music** is identified only from embedded tags (album artist first) and planned into
`music_dir` using `music_layout`. Tracks skip the LLM. Missing artist, album or title
means no target until reviewed, and a missing track number also needs review.
Placeholder tags such as "Unknown Artist" count as missing. Only the audio file moves:
cover art and other album files stay behind unless they share its stem and extension
is in `sidecar_extensions`.

**Software** is identified from names under `software_extensions` and planned into
`software_dir/Title/`, keeping the original file name. Files are fingerprinted but
never opened or run. Only an installer with a clear name and version can be auto;
archives, ISOs and titles suggested by the LLM always need review. Each ISO stays a
film or becomes software from the signs in its name, and unclear ones go to review.

**Leftover folders:** `mylib leftovers` and the Leftovers page list folders your moves
left empty or holding only small extras such as `.nfo`, `.txt` and images. Tick folders
on that page and approve them, or run `mylib leftovers --clean PATH --execute`. They move
to `.mylibrary-safety/run-N/leftovers/`, nothing is deleted, and `mylib undo --run N` or
the Runs page puts them back. Each folder is re-checked at that moment, and one that has
changed is refused. To delete instead, remove them yourself:

```bash
mylib leftovers --print0 | xargs -0 rm -r --
```

Only folders a run moved files out of qualify. A folder a torrent still lists is shown
apart and left out of `--print0`: remove the torrent first. If Transmission cannot be
reached, nothing is suggested. `mylib undo` recreates a removed folder if needed.

**Downloads in progress:** with `[transmission]` enabled, every file in an unfinished
torrent is skipped by the scan, ignored by the plan, and refused by `apply`, even when
paused or stalled, and it is picked up once the torrent finishes. If Transmission cannot
be reached, nothing auto-approves or applies until it can. `mylib status` shows what is
unfinished. As a backstop for any other downloader, a file changed within `settle_minutes`
(default 10) goes to review, and `apply` refuses any file whose modified time changed
since the scan. Moving a file a torrent client is still using can make it download again, so
finish or pause downloads first, and expect seeding to stop for renamed files.

**TV episodes** are recognised by `S01E04`-style names and planned into `tv_dir`
using `tv_layout`. They skip the LLM and IMDb check. A file without one season and
one episode number has no target and waits for review; multi-episode files always
need review. After upgrading, run `mylib parse --redo`, `mylib classify`, then
`mylib plan` once, so already-scanned episodes pick up their numbers. That re-asks
the LLM about flagged files, including ones already organized, so it can propose a
new title for a file you renamed earlier: skip those in review. Fix an episode by
hand with `mylib edit ID --title "Series" --season 1 --episode 4`, or use
`--kind movie` if it is not an episode.

## No-delete guarantees

1. **The LLM never touches files.** It returns JSON suggestions; it has no tools.
   LLM answers are capped at 0.85 confidence, so they always need your review
   unless the offline IMDb check confirms title, year and runtime.
2. **No delete code exists.** `mylibrary/safefs.py` is the only module that
   changes the filesystem, and it can only make folders, add hard links and
   rename. `tests/test_no_delete.py` fails the build if `os.remove`, `unlink`,
   `rmtree`, `shutil.move`, `os.rename`, `os.replace`, `Path.replace`, file
   writes or `rm`/`mv` shell calls appear anywhere else.
3. **No overwrite, no cross-drive copy+delete.** Renames use the kernel's atomic
   no-replace rename (`renameat2(RENAME_NOREPLACE)` on Linux,
   `renamex_np(RENAME_EXCL)` on macOS). If the target exists, or it is on another
   drive, the move is refused. There is no fallback.
4. **Hard-link safety snapshot.** Before a file moves, a second hard link is made
   under `<root>/.mylibrary-safety/run-N/<original path>`. It uses no extra disk
   space and keeps the data even if something went wrong.
5. **Pre-checks.** A file must be unchanged since the scan (same inode and size),
   the target must be free, and no two items may claim one target.
6. **Verify after every run.** Each moved inode must be at its target with the
   same size, sources must be gone, and each root's file count must be unchanged.
7. **Full log + undo.** Every move is in the `moves` table; `mylib undo` reverses
   a run.
8. **Duplicates are reported, never removed.** Empty folders are left in place.

When you're happy with a run, you can free the snapshot names yourself:
`rm -r <root>/.mylibrary-safety/run-N` (data stays, because the renamed file
still points to it). MyLibrary never does this for you.

## Install (Ubuntu)

```bash
sudo apt install ffmpeg            # provides ffprobe
git clone https://github.com/rockkw/MyLibrary.git ~/src/MyLibrary && cd ~/src/MyLibrary
pip3 install -e . --break-system-packages   # installs the `mylib` command
cp config.ini.sample config.ini    # set [library] roots to your SSD mounts
ollama pull mistral                # Ollama >= 0.5 (JSON-schema output)
```

Optional offline IMDb verification: download `title.basics.tsv.gz` once from
<https://datasets.imdbws.com/>, then `mylib imdb-import title.basics.tsv.gz`.

## Use

```bash
mylib pipeline             # scan + parse + classify + plan
mylib review               # list items needing review / conflicts
mylib edit 42 --title "Pengabdi Setan" --year 2017
mylib apply                # dry run
mylib apply --execute      # rename + verify
mylib runs
mylib undo --run 3         # dry run; add --execute
mylib dupes
mylib serve                # UI on http://127.0.0.1:5051
```

Without installing, `python3 mylib.py <command>` does the same from the repo.

Add `--json` to any command for machine-readable output (logs go to stderr).

UI keys: `j`/`k` move, `a` approve, `s` skip, `e` edit, `x` select, `/` search.

Try it on one root with a small `--root` first, and compare models on a sample
of your messiest names (`mylib classify --model qwen2.5:14b --redo`).

## Config

See `config.ini.sample`. Values support `${VAR:-default}` like MyMagnet;
`MYLIBRARY_CONFIG` points to an alternate file. The server refuses to bind to
`0.0.0.0`; use `127.0.0.1` or your Tailscale IP.

## Layout

```
src/mylibrary/        core: config, db, scan, parse, classify, imdb, plan, review, apply, safefs, dupes
src/mylibrary/cli.py  the `mylib` CLI
src/mylibrary/web/    Flask UI (app.py, templates/, static/)
tests/                pytest, including the no-delete guard
infra/compose/        systemd units (UI service, nightly pipeline timer)
.github/workflows/    CI: runs the tests on every push
mylib.py              run-from-repo shim
```

Project docs, diagrams and backlog live in the MyLibrary project folder on
Google Drive (Archetype layout).

## Tests

```bash
pip3 install -e ".[test]" --break-system-packages
pytest -q
```
