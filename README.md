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
| `mylib plan` | Builds target paths `Movies/Title (Year)/Title (Year) - 1080p.ext`, detects conflicts, auto-approves ≥ 0.90. | no |
| review | Approve / skip / edit in the UI or with `mylib approve|skip|edit`. | no |
| `mylib apply` | **Dry run.** Add `--execute` to rename. | renames only |
| `mylib verify --run N` | Re-checks a run any time. | no |
| `mylib undo --run N` | Dry run; `--execute` reverses a whole run. | renames only |

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
