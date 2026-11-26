# Desktop export folder

English | [한국어](desktop-export.ko.md)

Status: **removed 2026-11-24** (UD3 of [ui-verification-review.md](ui-verification-review.md), approved by the operator):
the export folder, manual and automatic, is gone from the dashboard and the desktop shell (`export.rs`, the dialog
plugin); the shell keeps `save_download` for Markdown. The rest of this document describes the removed feature.
Earlier: approved 2026-10-25 (with the amendments marked "amended" below), nothing built at that time. The operator asked for the
desktop app to save results into a directory of their choice, organized. Decisions are in chapter 5; automatic export
(E3) is in scope, which departs from the manual-first recommendation.

## 1. Problem

Today the desktop shell (Tauri) can write exactly one thing: a Markdown file into the OS Downloads folder
(`save_file.rs::save_download`). The thread report has a download button (`ReportButton.tsx`), an artifact file has one
(`ArtifactsTab.tsx`, Markdown only: `.mmd` and `.svg` are refused in the shell). Results of one debate end up as loose
files with names like `thread_ab12.md`, in a folder shared with everything else, and there is no way to keep the results
of a project together.

## 2. What it does

A one-time choice of an **export root** (a native folder picker), then a "Save to folder" action that writes results
under it in a fixed layout:

```
<root>/
  <project>/
    <thread>/
      report.md                 the debate report, assets inlined (the same text the download button builds)
      v<n>/<artifact files>     design artifacts of that thread, one folder per stored version
```

Sources, all already served by the read-only API and shown in the Results tab (`docs/design/project-results.md`):

- the thread report (`GET /api/threads/{thread}/report`),
- design artifacts (`GET /api/threads/{thread}/artifacts`, `.md`, `.mmd`, `.svg`).

Two entry points: "Save to folder" on one Results row / report dialog / artifact version, and "Save all of this project"
on the Results tab, which skips what is already there. Out of scope: audit reports, action and plan panels, code wiki
pages, any write back to the hub. The browser build keeps its Blob downloads and shows no folder button.

## 3. Design

### 3.1 The webview never names a path

The security property worth keeping: even a compromised page (a stored XSS in rendered Markdown, say) cannot write
outside the folder the operator picked.

- The folder picker is opened from **Rust** (`export_choose_root`); the webview gets back only the display path.
- The root is stored by Rust in the app config directory (`export.json`), not in `localStorage` and not by the webview.
- The write command takes `project`, `thread`, and a list of `{ rel_path, contents }`; it builds the path itself under
  the stored root. The webview cannot pass a root or an absolute path.

### 3.2 Commands (new module `export.rs`, registered in `main.rs`)

| command | does |
|---|---|
| `export_get_root()` | the stored root, or `null` when none is set or it no longer exists |
| `export_choose_root()` | native folder dialog, stores and returns the choice; `null` when cancelled |
| `export_clear_root()` | forgets the root (the folder and its files stay) |
| `export_save(project, thread, files)` | writes the files, returns `{written, skipped, renamed}` per file |
| `export_reveal()` | opens the root in the OS file manager (root path only) |

`tauri-plugin-dialog` is used **from Rust only** (`app.dialog().file().pick_folder`); no dialog or fs permission is
granted to the webview, so no `capabilities` file is needed beyond the default and the CSP does not change. The new
dependency (it pulls in `rfd`) is the cost of a native picker; the alternative is `rfd` directly, same weight.

### 3.3 Path safety (reuses and tightens `save_file.rs`)

- `project` must match `^[a-z0-9][a-z0-9_-]{0,63}$` and `thread` `^thread_[A-Za-z0-9_]{1,80}$` (the hub's own patterns);
  `rel_path` is split on `/`, every component must match `^[A-Za-z0-9][A-Za-z0-9._-]{0,80}$`, with no `..`, no empty
  component, depth at most 3, and the extension one of `md`, `mmd`, `svg`.
- Windows-reserved names (`CON`, `NUL`, `COM1`, ...) and trailing dots are refused so the same export works on every OS.
  Amended: the check is on the stem, so `CON.md` and `NUL.svg` are refused too, and names are compared
  case-insensitively (macOS and Windows treat `Report.md` and `report.md` as one file) both here and for collisions.
- Checked against the hub 2026-10-25: stored artifact names are flat and lower case (`artifact_ops.NAME_PATTERN`
  `[a-z0-9][a-z0-9._-]{0,80}`, extensions `.md`, `.mmd`, `.svg`), a subset of the component rule above, so
  `v<n>/<name>` is depth 2 and fits the limit of 3.
- Threat model: a local attacker who can swap a directory for a symlink between the check and the write is out of
  scope (temporary name plus rename narrows the window); the property kept is that the webview cannot choose a path.
- Directories are created one component at a time with `symlink_metadata`; a symlink anywhere below the root is refused.
  The final path is checked to start with the canonical root.
- 16 MiB per file (as `save_download`) and 64 MiB per call; at most 500 files per call.
- Files are written to a temporary name in the same folder and renamed (no half-written result).

### 3.4 Collisions

A stored artifact version never changes, so exporting it twice must not duplicate it. A thread report can change (a
continued thread appends rounds and overwrites the report on the hub). Rule: never overwrite. If the target exists with
identical bytes the file is `skipped`; if it differs it is written next to it as `report (1).md`, like `save_download`.
The operator keeps every earlier copy and can delete what they do not want.

Amended: the comparison covers the base name **and every suffixed variant** (`report (1).md`, `report (2).md`, ...). If
any of them holds identical bytes the file is `skipped`, otherwise it takes the next free suffix. Without this a changed
report exported twice would be stored twice, and "Save all" and automatic export would not be idempotent.

### 3.5 Dashboard

- The Results tab and the report dialog show, in the desktop shell only, the export root (or "Choose a folder") and the
  save buttons; a row result says how many files were written, skipped and renamed, and offers "Show folder".
- Settings gets a small "Export folder" row: current path, Change, Forget.
- New strings go into `src/i18n/ko` and `src/i18n/en`; the shell is detected as elsewhere (`'__TAURI_INTERNALS__' in window`).
- `ArtifactsTab.saveArtifact` keeps `save_download` for a single Markdown file; with a root set it uses the export
  instead, which also lifts the Markdown-only restriction for `.mmd` and `.svg`.

### 3.6 Tests

Rust unit tests for the path rules (traversal, absolute paths, reserved names, depth, bad extension, symlink below the
root, a root that disappeared, identical re-export, changed re-export, the size caps), using a scratch directory. Web
tests for the buttons with `invoke` mocked. Note that `scripts/ci.sh` runs the tauri section only when it is named
(`scripts/ci.sh tauri`).

### 3.7 Automatic export (E3)

Decided in scope (D3); behaviour confirmed by the operator on 2026-10-25. It uses the same path rules and the same
never-overwrite rule as `export_save`, with one narrow exception for files Guardian itself wrote (below), and adds
Rust commands `export_auto_get`, `export_auto_set`, `export_auto_marks`, `export_auto_recent` and `export_auto_save`.

- **Opt-in.** A Settings switch "Save new results automatically", off by default, usable only while a root is set; it is
  stored by Rust next to the root (`export.json`), not by the webview. Clearing the root turns it off.
- **What triggers it.** A finished debate whose status word is `APPROVED` or `DISPUTED` (the raw words the Results tab
  holds; an earlier draft of this section called the first one `PASSED`) and that has not been exported at its current
  `updated_at`. In-progress, failed and cancelled results are not exported automatically; they stay one click away.
- **From when.** Enabling it does not back-fill: only results whose `updated_at` is at or after the moment the switch was
  turned on are exported. "Save all of this project" remains the way to back-fill.
- **Where it runs.** In the dashboard, while the app is open: once on start and then every 30 seconds. Nothing is
  exported while the app is closed; the next start exports what finished in the meantime, provided it finished after the
  switch was turned on.
- **Update or duplicate.** The report of a continued thread changes. If `report.md` still hashes to what Guardian last
  wrote there, it is replaced in place (`updated`); if the operator edited it, it is left alone and the new text goes to
  `report (1).md` (`renamed`). A file Guardian has no record of is never updated, and a suffixed copy is never the one
  updated. Artifact versions `v<n>/...` are immutable, so they are skipped when identical. Manual saves keep the plain
  never-overwrite rule (they feed the record but never update).
- **Bookkeeping.** Rust keeps in `export.json` a hash per exported file (what Guardian last wrote at the base name) and a
  marker per thread (the result's `updated_at` that was exported). The dashboard compares markers and never reads the
  folder; Rust uses the hashes only for the in-place check. Choosing a different root or clearing it forgets both, since
  they describe the old folder.
- **Visibility.** Every automatic write shows a notice with the counts and "Show folder", and the last 20 are listed in
  the Settings row, so a write to the disk never happens silently. A result that cannot be exported is not retried
  every tick; it is tried again when its `updated_at` changes or the app restarts.

## 4. Phases

- **E1** root commands and `export_save`, Rust tests (including the amended collision and reserved-name cases), Settings
  row.
- **E2** Results tab / report dialog / artifact buttons, "Save all of this project", `.mmd`/`.svg` in the shell.
- **E3** automatic export (3.7): switch, `APPROVED`/`DISPUTED` trigger, in-place update of an untouched report, notice and recent list.

## 5. Decisions

Decided 2026-10-25: D1 as recommended; D2 and D3 scope = report and design artifacts, manual (E1-E2) **and** automatic
(E3); D4-D6 as recommended. The text of each decision below is the original proposal.

- **D1 Layout.** Recommended `<root>/<project>/<thread>/...` as above. Alternative: a date level (`2026-10/`) or a title
  slug in the folder name (readable, but titles come from model output and would need heavy sanitizing; the thread id is
  kept in a `thread.json` instead).
- **D2 Scope of "organized".** Recommended: report and design artifacts only. Alternatives: also plans and audit reports
  (they have no file form on the hub yet), or an `index.md` per project listing the exported threads.
- **D3 Manual or automatic.** Recommended: manual first (E1-E2). Automatic export of newly approved results would run
  from the dashboard poll, writes to the disk without a click, and only works while the app is open.
- **D4 Collisions.** Recommended: never overwrite, suffix on a differing report. Alternative: overwrite the report and
  keep only artifact history.
- **D5 One root or one per project.** Recommended: one root. Per-project roots add a mapping to store and repair.
- **D6 Dependency.** Recommended: `tauri-plugin-dialog` used from Rust only. Alternative: `rfd`.

## 6. Implementation notes

### E1 (2026-10-25)

Built as designed; the deviations and the choices the design left open:

- **Result shape.** `export_save` returns one `{ relPath, outcome, savedAs }` per file, camelCase (serde
  `rename_all`), not the `rel_path` the file list in 3.1 uses for its input in prose. `outcome` is `written`,
  `skipped` or `renamed`; `savedAs` is the final relative path (the suffixed name when renamed, the existing file
  when skipped). The input list is `{ relPath, contents }` for the same reason.
- **Pure core.** All checks and writes live in `save_files(root, project, thread, files)`, which does not touch
  Tauri; the `export_*` commands only resolve the root from `export.json` and call it. The Rust tests (17 for the
  export module, 20 in the crate) run without an app handle.
- **Config file.** `export.json` in the app config dir holds `{ "root": ... }` and keeps unknown keys through
  `#[serde(flatten)]`, so E3 can add its fields (switch, hash bookkeeping) without a migration or losing the root.
  It is written atomically (temp file, rename).
- **Root checks.** `export_choose_root` refuses a drive or filesystem root and stores the canonical path.
  `export_get_root` returns `null` when the stored folder is gone. A root that is itself a symlink is refused at
  write time like any link below it.
- **Whole call first.** The entire file list is validated (paths, extensions, sizes, duplicates within the call)
  before the first byte is written, so a bad entry rejects the call without leaving a partial export.
- **`export_reveal`** runs `open` (macOS), `explorer` (Windows) or `xdg-open` (other) on the stored root only.
- **Dialog plugin.** `tauri-plugin-dialog` is registered in `main.rs` but only Rust calls it
  (`blocking_pick_folder` inside an async command); the capabilities file is unchanged. The build regenerated
  `gen/schemas/*` and `Cargo.lock`, which are tracked and committed with E1.
- **Dashboard.** `ExportFolderSection` (Settings, hub-connection section) is rendered only when
  `__TAURI_INTERNALS__` exists, through `lib/exportFolder.ts`. It never passes a path to Rust. Strings are in
  `shell.exportFolder` of both catalogs. The save buttons are E2 (below).

### E2 (2026-10-25)

Dashboard only; no Rust change. Deviations and open choices:

- **One call per thread.** `exportThread(project, thread, {report, artifacts})` (`lib/exportThread.ts`) builds
  `report.md` plus `v<n>/<file>` for every live version and sends them in a single `export_save`, so Rust's
  whole-call validation makes a thread all-or-nothing. A report or version list whose `project` differs from the one
  asked for is refused before anything is sent.
- **Save all.** `exportProject` walks the listed results one thread at a time (bounded memory, steady progress),
  collects failures as `{thread, message}` and carries on; the notice counts written, skipped and renamed files and
  lists the threads that failed. Only results with a report or artifact versions are visited.
- **What is skipped.** Expired versions and files, and kinds the shell cannot store (not md/mmd/svg), are left out
  silently; they are not failures.
- **No per-row buttons.** The result rows are anchors, and a button inside an anchor is invalid nesting. The detail
  view's "Save this result" covers one thread; "Save all of this project" sits above the list.
- **Report view.** `ReportView` gained `exportable` (default on); the Results detail turns it off because it offers
  "Save this result" itself. The existing "MD download" button is unchanged.
- **Artifact Download.** In the shell with a root set, the file viewer's Download writes `v<n>/<path>` to the
  export folder (and reports the full path); without a root Markdown still goes to Downloads through
  `save_download`, and other kinds fail with `desktopMarkdownOnly`, whose text now says a folder can be chosen in
  Settings. A "Save this version" button sits in each live version's file list.
- **Root on demand.** `useExportAction` (`components/ExportButton.tsx`) asks for the root first and opens the folder
  dialog when none is stored; cancelling writes nothing. Outside the shell it renders no button.
- **Tests.** `tests/exportSave.test.tsx` (11) mocks `invoke` and the hub API: payload shape, foreign-project refusal,
  failed thread does not stop the rest, expired version never fetched, no buttons outside the shell, Download
  routing.

### E3 (2026-10-25)

Rust and dashboard. Section 3.7 above was amended to the behaviour the operator confirmed; deviations and open
choices:

- **Ledger and markers.** `Config` gained `auto`, `autoSinceMs`, `hashes` (`<project>/<thread>/<relPath>` -> FNV-1a 64
  of what Guardian last wrote at the base name), `marks` (`<project>/<thread>` -> `updated_at`) and `recent` (20
  entries); unknown keys are still preserved. FNV-1a is not a security hash: it only recognises "this file is still what
  I wrote", and an operator who edits the file to collide with the old hash gains nothing.
- **One new outcome.** `updated` joins `written`, `skipped` and `renamed`. `export_save` never returns it;
  `export_auto_save` does. `place_file` takes `recorded` and `update`: only the base name can be replaced, only when its
  current hash equals the recorded one.
- **Serialised.** All reads and writes of the config and the folder go through one `EXPORT_LOCK`, so the 30-second tick
  and a manual save cannot interleave a read-modify-write of `export.json`.
- **Empty runs are quiet.** The marker is recorded after every successful automatic save, but a `Recent` entry and the
  dashboard notice appear only when something was written, renamed or updated; an all-skipped pass is not news.
- **Switch state is derived.** `export_auto_get` reports `enabled` only while a root exists, so a root deleted outside
  Guardian cannot leave the switch looking on.
- **Runner.** `AutoExport` (`components/AutoExport.tsx`) is mounted by `AppStores` under the project store, is inert
  outside the shell, and walks the project list (not only the selected project). A re-entrancy guard stops overlapping
  ticks; `AuthRequiredError` and other failures end the tick quietly. The notice is a fixed, dismissible box (the
  dashboard has no toast component), and a window event `guardian:auto-export` makes the Settings list reload.
- **`threadFiles`.** `exportThread` was split so manual and automatic saves build the same files.
- **Tests.** Rust: 24 in `export.rs`, 27 in the crate (new: in-place update, edited report kept, manual never updates but feeds the
  ledger, no record means no update, suffixed copy never updated, hash vector, config defaults). Dashboard:
  `tests/autoExport.test.tsx` (11: eligibility by status, `since`, marker and content; switch off; marker handed to
  Rust; `updated` counted; failure not retried until `updated_at` changes; notice and "Show folder"; silent outside the
  shell and while off) and three switch tests in `tests/exportFolder.test.tsx`.
- **Not done.** No real-desktop run of the whole path (a debate finishing while the app is open); the checks use a mocked
  `invoke` and Rust unit tests.
