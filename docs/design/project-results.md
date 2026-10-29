# Project results tab

English | [한국어](project-results.ko.md)

Status: built 2026-10-22, deployed 2026-10-23. Decided with the operator on 2026-10-22: a read-only "Results" tab in the existing
dashboard, for every project kind (code, ops, design). The question that started it: only design projects had a place to
read what a project produced (the Artifacts tab); a code or ops project had only the per-debate report dialog.

## What it shows

One list per project, newest first, one row per debate thread. A thread appears when it left either

- a debate report, `<data dir>/decisions/projects/<project>/<thread>.md` (written by `debate_report.py`), or
- design artifacts, `<data dir>/design-artifacts/<project>/<thread>/vN/` (written by `artifact_ops.py`).

Both are joined by thread id. A row shows the title, the decision word, the rounds, the artifact counts and the age. A row
opens `#/debates/results/<thread>`: the report inline (the same viewer as the debate's report dialog, `ReportView`) and, for a
design project whose thread has artifacts, a link to the Artifacts viewer (`#/debates/artifacts/<thread>`).

Out of scope: audit reports (they belong to audit phase A3), action and plan panels (they are reached from Approvals), and any
write.

## API

`GET /api/projects/{id}/results` (`apps/agent/results_handler.go`), read-only, behind the global `withSecurity(withAuth(mux))`
chain, answers `{"project", "items", "total"}`. Items carry `thread_id, title, status, rounds, updated_at, has_report,
report_bytes, artifact_versions, artifact_files`. `Cache-Control: no-store`.

Like the report and artifact routes it reads straight from disk and needs no CLI. It reuses their guards: `idPattern` for the
project (400 otherwise), `threadPattern` for threads, `isRealDir` and `openRegular` (no symlinks, regular files only).

What comes from disk is data, so the list only trusts what it can bound:

- the title is the first heading line of the report, stripped of control and format characters (bidirectional overrides
  included), cut at 120 runes; without one the thread id is shown;
- the decision word must match `[A-Z][A-Z_]{0,23}` or it is dropped; the rounds must be a number up to 999;
- only the first 4 KiB of a report are read;
- the response holds the newest 200 threads; `total` tells the dashboard that more exist.

## Dashboard

`apps/web-desktop`: `pages/results/ResultsTab.tsx`, `lib/results.ts` (route parsing, tone of a decision word), strings in
`i18n/{ko,en}/results.ts` and `debate.page.tabResults`. The tab is offered in `DebatesPage` for all kinds (code: Debates, Wiki,
Results; ops: Debates, Results; design: Debates, Artifacts, Results). Decision words are shown through
`debate.decisionLabel` with the raw word as the chip title; an unknown word falls back to itself.

## Implementation notes

- The list is one request; the tab filters nothing client-side. A thread older than the cap can still be opened by its URL: the
  report route answers 404 when there is none.
- `ReportView` was split out of `ReportButton.tsx`'s dialog so both share the load, download and 404 handling.
- Tests: `apps/agent/results_handler_test.go` (order, merge, odd heads, symlinks and strange names, validation, cap),
  `tests/results.lib.test.ts`, `tests/results.test.tsx`.
- Open: deploy; real-browser look at the tab on the hub's data; whether audit reports should join the list once A3 exists.
