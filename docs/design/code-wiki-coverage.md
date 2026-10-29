# Code wiki coverage: design document links, splitting large scopes, file view (draft)

English | [한국어](code-wiki-coverage.ko.md)

Status: draft (2026-10-13). A, B and C are all implemented (see "Implementation notes (A)", "(B)", "(C)" below); they went in on
2026-10-13 in the order C → A → B. The items that needed a decision are collected in "Open decisions" at the end (all are decided).
Base design: "Layer 2. Code wiki" in `debate-code-index-wiki.md`. This document only describes the changes on top of it.
The rules for evidence, retrieval and citation checks (the wiki is navigation, not evidence) do not change.

## Problem

On 2026-10-13 the hub had one page, `cli-llm-agent`. Values measured the same day:

1. **One page covers too much.** `cli/llm-agent/` has 38 source files without tests (debate_engine.py alone is 117 KB), yet
   the page is 13.7 KB and only about 8 files were actually read (`basis`). One session with a body target of about 6000
   characters cannot cover a large directory. The default set rule splits only one level below the top level
   (`wiki_build.default_candidates`). `apps/web-desktop/` is a single page scope of 124 source files, 746 KB.
2. **Design intent is missing from the wiki.** The answer to problem 5 of the base design ("why is it built this way") is already
   in the decisions and implementation notes of the 13 `docs/design/*.md` documents. But the writer prompt says "cite the code",
   and the default set only picks code directories. `feature:` pages were only defined as "made by people", so in practice
   there are none.
3. **No place to look at a single file.** Layer 1 knows a file's outline, definitions and referencing files exactly, but a person
   has no way to see them in the dashboard. The citation chips in the wiki body have nowhere to go either (the dashboard does
   not know the repository's web URL).

Measurement: each of the 13 design documents mentions at least 2 code files of the repository by path (a full path, or a file
name that is unique in the repository). These mentions alone cover 22 of 38 code files in `cli/llm-agent`, 10 of 22 in
`apps/agent`, 6 of 124 in `apps/web-desktop` and 0 of 33 in `packages/guardian-ui`.
So design documents alone cannot split the pages (2), and a size-based split (3) is needed separately.

## Decision summary

| No. | Change | LLM cost | What changes |
|---|---|---|---|
| A | A feature page per design document (`feature-<document name>`) | 1 session per document | Default set, writer prompt, page meta (`kind`, `docs`) |
| B | Split directory scopes over a threshold into group pages; the original directory page becomes an overview page | 1 session per group | Default set, write order, overview prompt |
| C | File view (no LLM): outline, definitions, incoming references, pages and documents that cover this file | none | New read commands and API, dashboard screen, citation chip links |

All three widen the page storage format (front matter) in a backward-compatible way; existing pages still read as they are.
`WIKI_PROMPT_VERSION` goes up, so existing LLM pages become `--stale` targets (pages written by people stay as they are).

## Page kinds

Add `kind` to the front matter. Without it a page reads as `dir` (every page today).

| kind | id | scope | Who makes it |
|---|---|---|---|
| `dir` | `cli-llm-agent` (as today) | one directory | Default set, directories under the threshold |
| `overview` | `cli-llm-agent` (same id kept) | one directory | Default set, directories split because they exceed the threshold |
| `group` | `cli-llm-agent--<group name>` | file list | Default set (B) |
| `feature` | `feature-<document file name>` | one design document + the code files it mentions | Default set (A), people |

- The id rule (`^[a-z0-9][a-z0-9-]{0,63}$`) stays. `--` is already an allowed string.
- Raise the `scope` entry cap `MAX_SCOPE_ENTRIES` from 20 to 60. `debate-code-index-wiki.md` alone mentions 41 code files.
- The overview page uses the same id as the directory page, so today's `[[cli-llm-agent]]` links and dashboard URLs do not break.

## A. Design documents and feature pages

### Finding documents

- Add an optional field `wiki.docs` (a list of globs) to the project settings (`configs/projects/<id>.json`; projects registered
  in the dashboard use the same format). Without it the default is
  `["docs/design/*.md", "docs/adr/*.md", "docs/architecture/*.md", "ARCHITECTURE.md", "DESIGN.md"]`.
- Only readable files (`RepoTools.readable`) of at most 1 MB are used. The secret pattern exclusion still applies.
- A document that mentions fewer than 2 code files gets no feature page (there is no code to link). It still shows up in C's
  "documents that mention this file".

### Linking documents and code (deterministic)

- Find mentions of repository files in the document body with the same token rule as `RepoTools.cited_locations`. Count both
  full paths and file names that are unique in the repository (`wiki_build.py`). Names that collide are not counted.
- The feature page's `scope` = `[document path] + the mentioned code files (in order of first appearance, up to 59)`. No
  directories. With a directory every new file inside it would make the page `stale`, though that file may have nothing to do
  with the document.
- Mentions of other documents such as `.md` do not go into `scope`; they go into the front matter `docs` (for showing related
  documents).

### Writer prompt (feature)

It uses the same retrieval protocol as the normal prompt; only the task and answer format differ.

- Sections: `## 설계 의도`, `## 구현 위치`, `## 설계와 다른 점`, `## 주의점`, `## 관련 페이지` (in English `Intent`, `Where it lives`, `Drift`, `Caveats`, `Related pages`).
- **Citation rule:** design intent and decisions cite document lines (`docs/design/x.md:120-128`); claims about behaviour cite
  code lines. Never claim behaviour with a document citation. When the document and the code differ, put both citations under
  `설계와 다른 점` (Drift). Do not copy differences the document already records in its "implementation notes"; write only
  those you confirmed still hold in the current code.
- Document citations go through the same line-level citation check (documents are repository files too). The one-retry rule is
  the same.
- For a long document (over 48 KB) the first prompt gets only its heading list (h2/h3 with line numbers), and the session reads
  the sections it needs with `read`.

### Staleness

- The document file is in `basis`, so editing the document makes the page `stale`. Code changes make it `stale` as today.
- Since `scope` is a file list, the "new file in scope" check practically does not apply. Instead, when the document starts
  mentioning a new file, the document has changed, so the page is `stale`.
- When the document is deleted the page is `stale` with `removed`; the next `--stale` skips rewriting it and records the reason
  `doc_removed` (people delete pages).

### Other documents (README, operations documents, evaluation reports)

No separate pages. They appear only as a list of "documents that mention files in this scope" (path and a one-line title) in C's
file view and in the overview page prompt.
Reason: operating procedures and evaluation results age faster than design intent and map weakly to code. If needed, a person
creates a `feature-` page by hand.

## B. Splitting large scopes

### Threshold

For each directory candidate (today's `default_candidates` result) count code files and bytes (tests and previews excluded, as
today). Split when `files > 15` or `bytes > 200KB`. `GUARDIAN_CODE_WIKI_SPLIT_FILES` and `GUARDIAN_CODE_WIKI_SPLIT_BYTES` change
the two values.

Measured (2026-10-13): `cli/llm-agent` 38 files / 774 KB, `apps/web-desktop` 124 / 746 KB and `apps/agent` 22 / 264 KB are split.
Of `scripts` 13 / 113 KB and `packages/guardian-ui` 33 / 65 KB, only `guardian-ui` is split, by file count.

### How to group (deterministic, no LLM)

1. **If there are subdirectories, split by subdirectory first** (`apps/web-desktop/src/pages/`, `src/lib/` …). If a result still
   exceeds the threshold, apply this rule again inside it (depth at most 4).
2. **A flat directory (or the direct files left after the split) is grouped by the reference graph.**
   - Edge: A→B when file A references a name declared only in file B (layer 1 declaration table and identifier occurrences; a
     name declared in several files does not count).
   - Hub files excluded: a file referenced by at least 1/3 of the files in the same directory (expected: `repo_tools.py`,
     `debate_engine.py`) is left out of the edge computation and put into the group that references it most.
   - The connected components of the remaining graph are the groups. A group over the threshold is cut in file name order to fit;
     groups with fewer than 2 files are collected into an "other" group.
   - Group name: the name (without extension) of the file in the group most referenced by files of the same directory. Example:
     `cli-llm-agent--code-wiki`.
3. **Files already covered by a feature page (A) are not removed from groups.** The feature page takes the design view and the
   group page the code view, so overlap is fine. Instead the group prompt gets the ids of feature pages that cover its files and
   links them.

Groups are not recomputed for every sha. The `scope` (file list) fixed at the first write is stored in the page; after that it
is handled through staleness as today.
When a new file appears in the scope, the fact that no group covers it becomes the overview page's `stale` reason ("new file not
covered by any group").
Regrouping happens only with `--code-wiki-build --regroup DIR` (an explicit job a person chooses). Existing group pages that lost
all their files are not deleted but left `broken` for a person to clean up.

`--dry-run` shows the proposed groups (id, files, bytes, hub files) without an LLM. Tune the thresholds with it first.

### Overview page

- The page with the original id of a split directory becomes `kind: overview`. It is written after all its group pages (the
  order is fixed within one build).
- The prompt gets the child pages' id, title and the first paragraph of `## 역할` (Role), and asks for the role of the whole
  directory and "which page to read for what". The sections are the same as today (`역할`, `흐름`, `주의점`, `관련 페이지`;
  Role, Flow, Caveats, Related pages).
- Child page narratives are not evidence, so the overview also cites code for every claim (the current rule). The overview reads
  little code (mostly entry points), so the session is short.
- Child pages do not go into the overview's `basis`. Rewriting a child page does not make the overview `stale` (only the related
  links need to be right).

### First prompt and caps

- Raise the default page cap `MAX_DEFAULT_PAGES` from 20 to 80 (expected for this repository: 13 feature + about 30 group + 7
  overview and directory pages).
- The debate first prompt's wiki list (at most 40 lines) puts `overview` and `dir` first, then `feature`, then `group`. Overflow
  is `... N more fresh pages` (as today).
- A build now runs many sessions, so for `--all` or the first default build the dashboard shows the session count in a
  confirmation dialog. Add a `--kind` selection so the work can be split with `--page` or `--kind feature` instead of running
  everything at once.

## C. File view (no LLM)

### Read commands and API

Add `--code-file-show PATH --project P [--sha S]` to `wiki_ops.py` (2026-10-18: implemented in `file_view.py` and called through
`guardian.py`; the routes take the path as the query `?path=`; see "Implementation notes (C)" below). Output:

```
{"ok", "project", "sha", "path", "language", "size",
 "outline": [{"name", "kind", "start", "end", "signature"}],
 "incoming": [{"path", "count"}],          // other files referencing this file's declarations (layer 1; type info for Go)
 "outgoing": [{"path", "count"}],          // other files this file references
 "pages": [{"id", "kind", "title", "state", "via": "scope" | "basis" | "cited"}],
 "docs": [{"path", "title", "lines": [N]}] // documents that mention this file (A's linking rule)
}
```

- `--code-file-read PATH --start N --end M` returns raw lines (at most 400 lines, 64 KB). Unreadable files (secrets, symbolic
  links, binaries) are an error.
- Go API: `GET /api/projects/{id}/files/{path...}?sha=` → show, `GET .../files/{path...}/lines?start=&end=&sha=` → read.
  Authentication and actor recording are the same as for the wiki routes.
  This is the first route that sends repository source to the dashboard, so responses only cover files that pass
  `RepoTools.readable`, and the path and line range are written to an audit log (`files.jsonl` in the same format as
  `audit/exec.jsonl`).

### Dashboard

- The wiki tab gets a second list, "Files" (tree, search). URL `#/debates/wiki/file/<path>`. (2026-10-18: the implemented URLs are
  `#/debates/wiki?file=<path>` and `?view=files`; see "Implementation notes (C)" below.)
- File screen: outline (a collapsible declaration list), incoming and outgoing references, wiki pages that cover this file (state
  chips), documents that mention this file. Clicking a declaration shows the source of that line range below.
- **Citation chip links:** clicking a chip in the wiki body goes to `#/debates/wiki/file/<path>?lines=N-M` (or `#Symbol`). The
  repository's web URL is not needed.
  It opens at the sha currently being viewed (the same base as the wiki list), not the page's sha. If the page is `stale`, a
  notice says the lines may be off.

### Implementation notes (B, 2026-10-13)

Implementation: `cli/llm-agent/wiki_split.py` (`Splitter`), `wiki_build.py` (`page_plan`, overview and group in `select_targets`,
`_context`), `code_wiki.py` (kinds `overview` and `group`, `GROUP_SEPARATOR`), `guardian.py --kind overview|group`,
`--regroup ID`, Go `kind` extension, dashboard ("큰 디렉터리 나눠 쓰기" / split large directories, kind chips, indented group
rows). These are decisions that differ from the design above or were not in it.

- **The grouping method changed.** The design's "connected components without hubs" did not work for this repository. Entry
  points such as `guardian.py` that use almost every file did not meet the hub (much-used file) criterion, so the whole graph
  became one component and was cut by name order. So:
  - A hub is a file whose number of **incoming and outgoing links** (distinct files) is at least 1/3 of the directory's files
    (at least 3). Hubs form one group, `core` (the design said "into the group that uses it most", but then 9 hubs of a Go
    package piled into one group).
  - The rest are merged by **average-linkage cohesion**. Merge the two groups with the highest links per file pair, only when the
    result fits the cap and the linkage is at least `MIN_LINKAGE` 0.25. Files left alone go to `misc`.
  - Small groups under 1/8 of the cap (25 KB) are merged with neighbours **within the same parent directory** up to the cap (name:
    common prefix + `etc`). Groups left under 2 KB are not created (the overview covers them). Directories named `tests`, `test`,
    `testdata` or `fixtures` are left out of groups.
  - Result (this repository, `b565b6b`): `cli-llm-agent` has 9 groups including `core` (debate_engine, guardian), `code-index`
    (code_index, code_select, file_view, repo_map), `db-manager` and `llm-runner`; `apps-web-desktop` has 17 by subdirectory,
    `apps-agent` 3, `packages-guardian-ui` 3. 32 groups + 4 overviews. A Go package cannot be filtered by imports, so name-based
    noise remains (`apps-agent--core` has 9 files).
- **Overview page:** when a directory is split, the `dir` page with the same id becomes `overview`, and the first write after the
  kind change starts fresh without the old narrative. Writer sessions normally do not see other pages' narratives (P3 decision),
  but the overview is an exception: it gets the id, title, scope and first paragraph (300 characters) of the group pages written
  so far. The body is reduced to about 4000 characters and must link every group page with `[[id]]`.
- **Group page:** the sections are the same as `dir`. The prompt gets the overview page id and the ids of feature pages that have
  the file in their scope.
- **Selection:** `--kind group` picks the group pages (new plans + existing groups), then every overview page. A directory that
  already has at least one group page gets no new plan (the design's fixing). `--regroup ID` (CLI only, not in the API or
  dashboard) plans that directory again and writes new groups and the overview. Old group pages are not deleted. The build order
  is group → directory and overview → feature.
- **Not done:** the overview staleness reason "new file not covered by any group" was not built separately. The overview scope is
  the directory, so a new file makes it `stale` as a "new file" as today. `MAX_DEFAULT_PAGES` (directory candidates) stays 20, and
  groups have their own cap of 80. `WIKI_PROMPT_VERSION` was not bumped (the `dir` and `feature` prompts are the same, and the
  overview and group text only goes into those kinds).

### Implementation notes (A, 2026-10-13)

Implementation: `wiki_build.py` (`doc_paths`, `doc_mentions`, `feature_candidates`, `kind` in `select_targets`, the feature branch
of `build_prompt`, `doc_outline`), `code_wiki.py` (`kind` and `docs` fields, `MAX_SCOPE_ENTRIES` 60, first prompt ordering, a
person's edit keeps the kind), `project_registry.py` (`wiki.docs`), `guardian.py --kind dir|feature`, Go `wikiBuildRequest.Kind`,
dashboard (kind chips, "설계 문서 페이지 작성" / write design document pages, scope and related documents linked to the file
view). `WIKI_PROMPT_VERSION` is `2026-10-13.wiki5`. These are decisions that differ from the design above or were not in it.

- **Only two kinds for now, `dir` and `feature`** (`overview` and `group` come with B). A `dir` page does not write `kind` to its
  front matter, so its bytes equal an older file.
- **Linking rule:** test, story and preview files a document names (`repo_map.is_noise_path`) are left out of the scope. This keeps
  design pages from going stale on every test change. In this repository the scope of `debate-code-index-wiki.md` becomes 24 of
  41 mentions.
- **A feature page rewrite rebuilds the scope from the document at that commit** (files the document newly mentions come in,
  deleted files drop out). When the document is gone the old scope is used and the page is skipped as `scope_missing` (the same
  effect as the design's `doc_removed`, only the reason text is the existing one).
- **Selection:** `--kind feature` alone picks every feature page candidate (rewrite existing ones, write missing ones). With
  `--stale` or `--all` it filters by that kind. It cannot be combined with `--page`. The default build of a project without pages
  writes both directory pages and feature pages. Feature pages are capped at 40.
- **Document outline:** the prompt contains the document's `#` to `####` headings with line numbers (not inside code blocks, up
  to 6 KB). The document body is not included; the session reads line ranges. In effect the design's "outline only over 48 KB"
  applies to every document.
- dry-run (2026-10-13, the commit after `24d6689`): 13 feature pages, first prompts 8.8–10.7 KB. `ui-flow-redesign.md` mentions
  fewer than 2 files and is left out.

### Implementation notes (C, 2026-10-13)

Implementation: `cli/llm-agent/file_view.py` (`--code-file-list`, `--code-file-show PATH`, `--code-file-read PATH --start N --end M`),
`repo_tools.RepoTools.source_texts` (several files with one `git cat-file --batch`), `apps/agent/file_handler.go`,
dashboard `pages/wiki/FileList.tsx` and `FileView.tsx`, `lib/wiki.ts` (`fileHref`, `parseLineRange`), citation chip links
(`lib/wikiCitations.ts`).
These are decisions that differ from the design above or were not in it.

- **Routes:** Go's `{path...}` may only end a pattern, so the path is a query parameter. `GET /api/projects/{id}/files` (list),
  `GET .../files/show?path=`, `GET .../files/lines?path=&start=&end=` (default `start=1`, `end=start+399`). Go only checks the
  shape of the path (empty, over 1024 bytes, absolute, starting with `-`, control characters, empty, `.` or `..` segments → 400).
  The CLI decides whether it is readable, and answers 404 when not.
- **Audit:** instead of a new `files.jsonl`, entries go to the existing `audit/exec.jsonl` (`GET /api/audit`). Route
  `GET /api/projects/{id}/files/lines`, targets `[project]`, command `read <path>:<start>-<end>`; no content is recorded. Requests
  the CLI refused (secret paths and so on) are also recorded with `status: error` and the CLI's error text. Requests Go rejected
  with 400, and list and show requests, are not recorded (no source leaves the hub).
- **References (incoming/outgoing):** two additions to the name-based rule. Only files of the same language are linked, and a use
  counts only when the using file actually imports the declaring file (Python `import`/`from`, TS/JS `from '…/name'`, `import()`,
  `require()`; Go: the same directory, or an import path that ends with that directory). In this repository this removed
  `apps/web-desktop/tests/run.test.tsx` (a name collision) from the outgoing references of `wiki_build.py`.
  Product code "using" test, story or preview files does not count; when such a file uses this file it is marked `test: true`.
  References inside one Go package cannot be filtered by import, so name collisions can remain (the result text says so).
- **Documents:** C's "documents that mention this file" does not wait for A's `wiki.docs` globs; it looks at every readable `.md`,
  `.markdown`, `.mdx` or `.rst` (at most 1 MB) in the repository. It counts full path mentions and name mentions of files whose
  name is unique in the repository. At most the first 20 lines per document and 50 documents.
- **Dashboard:** the left side of the wiki tab has a "pages | files" switch. Instead of the design's `#/debates/wiki/file/<path>`
  the URL is `#/debates/wiki?file=<path>[&lines=N-M|&symbol=S]`, and the list alone is `?view=files` (so it does not collide with
  a page id `file`). Source is not fetched up front but when a declaration, a citation or "view from the start" is clicked (fewer
  audit entries and less transfer). A cited location opens in a window of at least 120 lines with 8 lines of context before it,
  and the cited lines are highlighted. Chips open at the commit being viewed, with a note that the lines may be off.

## Files that change (expected)

(2026-10-18: in practice the file commands live in `cli/llm-agent/file_view.py`, not `wiki_ops.py`, the file routes in
`apps/agent/file_handler.go`, not `wiki_handler.go`, and the split in `cli/llm-agent/wiki_split.py`. See each implementation note.)

- `cli/llm-agent/code_wiki.py`: `kind` and `docs` fields, `MAX_SCOPE_ENTRIES`, first prompt ordering.
- `cli/llm-agent/wiki_build.py`: finding and linking documents, splitting, overview order, feature and overview prompts, `--kind`, `--regroup`, `WIKI_PROMPT_VERSION`.
- `cli/llm-agent/wiki_ops.py`, `guardian.py`: `--code-file-show`, `--code-file-read`.
- `apps/agent/wiki_handler.go`: two file routes.
- `apps/web-desktop/src/pages/wiki/`: file list and file screen, chip links, kind display.
- `cli/llm-agent/project_registry.py`: reading `wiki.docs`.

## Verification

- **Determinism:** at the same sha the default set (feature links, groups) comes out byte for byte the same (compare
  `--dry-run --json` twice).
- **Expected dry-run for this repository:** 13 feature pages, the first entry of each `scope` is the document path. The
  `cli/llm-agent` groups show `debate_engine.py` and `repo_tools.py` as hub files. Append the result to this document's
  "Measurement" before fixing the implementation defaults.
- **Decoy check:** a feature page answer that claims behaviour by citing only document lines ("X does Y
  (`docs/design/a.md:10`)") has no code citation and breaks the prompt rule. This cannot be caught mechanically, so add one
  "drift" question to the evaluation 2 question set and have a person grade it.
- **Staleness:** in a commit that only edits a document, only that feature page becomes `stale`; group pages covering the same
  files stay `fresh`.
- **File view:** show and read of a secret pattern path (`config/secrets/x.yaml`) are refused with 404, and the read attempt is
  recorded as `error` in the audit log. Chip links open the line range.

## Not done

- No LLM narrative per file. C's machine-generated view is enough at file level, and staleness and cost would grow with the
  number of files.
- Design documents are not copied into wiki pages. A feature page only summarizes and cites the document; the document is the
  original.
- The decision DB (debate results) does not go into the wiki. It is hub data outside the repository and not tied to a sha.
- Groups are not re-split automatically on every commit (page ids would shift).

## Open decisions

1. ~~Split thresholds~~: as proposed, 15 files or 200 KB. Decided 2026-10-13.
2. ~~Documents that get feature pages~~: as proposed, design documents such as `docs/design/*.md` only (`docs/operations`
   excluded). Decided 2026-10-13.
3. ~~Exposing file source~~: allowed as proposed (readable files only, with audit records). Decided 2026-10-13.
4. ~~Order of work~~: went in as proposed, C (no cost, fixes chip links) → A (13 sessions) → B (about 30 sessions) (2026-10-13).
   Doing B before A would leave no feature page links to give the group prompt.
