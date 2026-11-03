# Changelog

Versions follow semantic versioning: PATCH = bug fixes, MINOR = new features,
MAJOR = breaking changes. The running app shows its full label, for example
`v1.0.0+c4f061d` (version + git commit, `.dirty` with uncommitted changes), next
to the title in the web UI, with release, commit and milestone details on hover.
`mylib --version` prints it too. Each version has a git tag `v<version>` and a
backlog milestone of the same name.

## v1.2.0 — 2026-11-03

See where the space goes from the Tree tab. Backlog milestone `v1.2.0` lists
the tasks it contains.

### Web UI
- A Show sizes checkbox in the Tree tab tools shows the total size of every
  folder and root in MB, GB or TB. Totals come from the same read-only walk,
  count only files on disk now (planned incoming rows are not counted twice;
  files marked for trash still count), follow Show hidden files, and stay
  correct with Only changes on. Toggling it redraws the tree without fetching
  it again. File rows use the same units (TASK-32).

## v1.1.0 — 2026-11-03

Play and trash files straight from the Tree tab. Backlog milestone `v1.1.0`
lists the tasks it contains.

### Web UI
- Click a video in the tree to play it in an overlay over the page, with
  seeking, full screen, Open in Review, and ◀ ▶ arrows (or P and N) to step
  through the videos in the tree. Streaming is read only and limited to video
  files inside a configured root; formats browsers cannot play (AC3/EAC3/DTS
  audio, HEVC, .avi, .wmv, .ts) get a clear warning (TASK-30).
- Move to trash from the tree or the player: marking a file only records the
  decision. The next Apply moves it into `.mylibrary-safety/run-N/trash/`, and
  Undo puts it back. Changed, downloading and torrent-held files are refused,
  and marked files are held out of the plan. Nothing is deleted; disk space
  comes back when you remove the trash folder yourself (TASK-31).
- The tabs now read Tree, Review, Duplicates, Leftovers, Analyze, Runs.

### Tooling
- `mylib trash PATH...`, `mylib trash` and `mylib trash --clear PATH...` mark,
  list and unmark files for trash (TASK-31).

## v1.0.0 — 2026-11-02

First versioned release: MyLibrary as it runs on the home Ubuntu box, organizing
a live download folder. Backlog milestone `v1.0.0` lists the tasks it contains.

### Organizing
- Movies into Jellyfin layout, with editions such as Director's Cut kept in the
  name (TASK-1, TASK-6 groundwork).
- TV episodes, music from embedded tags, and software installers each get their
  own layout (TASK-13, TASK-14, TASK-15).
- Compilations stay together under Various Artists, with disc numbers from
  CD1/Disc 1 folders (TASK-19).
- Adult scenes by site, date and performer, from dotted, bracketed and
  `[YYMMDD][Brand]` names; the local LLM can suggest a scene for names the
  parser cannot read, always for review (TASK-20, TASK-24).
- Every video with an allowed extension is organized, short ones included:
  they go to review instead of being ignored.

### Safety
- Never deletes or overwrites: atomic no-replace renames, hard-link snapshots,
  verify and undo for every run (TASK-1).
- Files still downloading in Transmission are left alone (TASK-17).
- Folders emptied by a run are suggested for cleanup and moved aside, never
  deleted; undo restores them (TASK-16).
- A root taken out of the config is ignored by plans, the LLM and apply, while
  its data and decisions are kept for when it comes back (TASK-23).
- The pipeline and an apply or undo never run at the same time.

### Web UI
- Review with keyboard keys, bulk approve and skip, and bulk edit of shared
  fields across selected files (TASK-1, TASK-25).
- Tree tab: every file on each root, with what the next apply would move
  (TASK-21).
- Analyze tab: a keyword heatmap of the library; a keyword shows its files in a
  tree (TASK-22).
- Approve refuses items with no target, and the UI says why.

### Tooling
- CLI-first: every UI action is a `mylib` command. Pytest suite with a
  no-delete guard, CI on every push (TASK-1, TASK-2).
- systemd user units for the UI and a nightly pipeline (installing them is
  TASK-9, not done).
- Versioning: VERSION file, `v<version>+<commit>` label, changelog, milestone
  and tag (TASK-26).
