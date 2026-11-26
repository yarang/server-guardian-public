# Wiki viewer: an index to start from, and moving between pages while reading

English | [한국어](wiki-viewer.ko.md)

Status: proposed 2026-10-27; decisions 1-5 approved the same day as recommended. V0 (page links), V1 (index, reading view), V2 (`summary`, `links`) and V3 (shortcuts) are built and deployed (2026-10-28, `d84562c`). V4 (three modes: Read, Files, History; decision 6, approved 2026-10-28) is built and deployed (2026-10-28, `5a8df55`). V0-V4 checked in a real browser on 2026-10-31 at desktop width (see the last notes); 390 px is not checked.
Base documents: "Layer 2. Code wiki" in `debate-code-index-wiki.md` and `code-wiki-coverage.md` (page kinds, file view). This
document only changes how the dashboard shows pages; page storage, the writer and the evidence rules do not change.

## 1. Problem

The operator can read the wiki now (the hub's repository address was fixed on 2026-10-27) and asked for a viewer that opens at
a first index screen and lets one move from page to page while reading. Today:

1. **There is no index.** `#/debates/wiki` shows a flat list of pages (group pages indented under their overview) next to a
   "select a page" card. A reader sees ids, a state chip and a date, but not what a page is about, nor where to start.
2. **Pages do not lead to each other.** The writer must close every page with related pages written as `[[id]]` (`wiki_build.py`
   lines 508, 553, 712, 722), but the dashboard never turned `[[id]]` into a link, and `report.ts` prints every link that is not
   `http(s)` as written. Reported by the operator on 2026-10-27: "links are not parsed, so they cannot be clicked".
3. **The reading view has no sense of place.** No path (index > overview > group), no previous or next page, no list of the
   pages that point here. On a narrow screen the list and the page are two separate screens with one back link.
4. **Page actions crowd the page.** Rewrite, edit and delete sit in the header card of every page, although most visits only read.

## 2. What the data already gives (no new API for V1)

`GET /api/projects/{id}/wiki` returns for each page `id`, `title`, `scope`, `author`, `updated`, `state`, `kind`; the page
view adds the narrative, `docs`, the staleness details and the generated map.

- **Tree.** A group page id is `<overview id>--<name>` (`GROUP_SEPARATOR`), so the parent is the id before the first `--`. A
  `feature-*` page has no parent. `dir` and `overview` pages are roots.
- **Order.** The list comes in storage order (ids sort `dir` before `dir--part`). `KIND_ORDER` (`dir`/`overview`, `feature`, `group`) only
  orders the pages in a debate's first prompt, so V1 computes its own index order (each root followed by its groups, then the features) in `lib/wiki.ts`.
- **State.** `fresh | stale | broken` per page, so the index can say how much of the wiki to trust.
- **Related pages.** Only inside the narrative (`[[id]]`). Reverse links (who points here) need every narrative, which the list
  does not carry.

## 3. Design

### 3.1 Routes

| URL | Screen |
|---|---|
| `#/debates/wiki` | **Index** (new): the entry screen |
| `#/debates/wiki/<page>` | **Reading view** (page body with place and neighbours) |
| `#/debates/wiki?view=files`, `?file=<path>` | **Files** mode: file list and file view (V4: reached from the mode control) |
| `#/debates/wiki?view=history` | **History** mode (V4): build jobs and every action that starts a build |

Browser back and forward work as they do now, since every move is a hash route. A reload keeps the place.

### 3.2 Index screen

Full width, one column of sections; the page list on the left is gone from this screen.

1. **Header:** project switcher, search box (filters title, id and scope), `Reload`, `New page`, `Build`, and a link to the file
   view. The state line says "N pages, M need attention" with the date of the newest page.
2. **Attention** (only when something is `stale` or `broken`): the pages concerned, with the state chip and the first reason
   (changed files, broken citations), so the reader knows what not to trust before reading.
3. **Start here:** the roots (`overview` and `dir` pages) as cards. A card shows the title, the kind, the state chip, the scope
   in one line and the number of group pages below it; with V2 also a one-line summary.
4. **Design features:** the `feature-*` pages as a compact list (title, state), because they are read for intent, not for code.
5. **Groups** are shown inside the card of their overview (collapsed to a list), not as a separate section.

An empty wiki shows the existing empty-state text and the `Build` action.

### 3.3 Reading view

- **Place bar** above the page: `Index › <overview> › <this page>`. Each part is a link. On a narrow screen only `‹ Index` and the
  parent are shown.
- **Neighbours** below the page: `‹ previous` and `next ›` in index order (each root followed by its groups, then the features), and
  for an overview page the list of its group pages with their titles. These links give a way to read the whole wiki in sequence.
- **Related pages:** `[[id]]` in the narrative are links (V0), shown as small chips; an id that is not in the list is shown dashed
  and not clickable, since the writer may name a page that was deleted.
- **Referenced by** (V2): the pages whose narrative links here.
- **Left tree (wide screens only):** a narrow, collapsible copy of the index tree, so a reader can jump without going back. It
  replaces today's list; on narrow screens it is not shown and the place bar does the job.
- **Actions:** `Rewrite`, `Edit` and `Delete` move into one `Page actions` menu in the header card. The state chip, author and
  checked-at stay visible. Behaviour of each action does not change (the confirmations stay).
- **Table of contents and citation chips** stay as built (`WikiNarrative`). Opening a citation goes to the file view as today.
- **Scroll:** moving to another page scrolls to the top; going back restores the position the browser keeps.

### 3.4 Narrow screens

Index is one column of cards. The reading view is one column with the place bar on top and the neighbours at the bottom; the
tree and the table of contents are hidden (the table of contents is already container-query based).

### 3.5 Keyboard (optional, V3)

`[` and `]` for previous and next page, `g i` for the index, `/` focuses the search box. Off while an input or dialog has focus.

## 4. Phases

| Phase | Content | Server change | Done when |
|---|---|---|---|
| V0 | `[[id]]` becomes a link to the page, an unknown id is shown as missing | none | Built 2026-10-27, tests pass |
| V1 | Index screen, place bar, neighbours, page actions menu, left tree, narrow layout, ko and en strings | none | The operator can start from the index and read the whole wiki by following links, on wide and narrow screens |
| V2 | `summary` (first paragraph of the Role / Intent section, at most 160 characters) and `links` (ids found as `[[id]]`) in each list entry; "Referenced by"; summaries on cards | `listing()` in `code_wiki.py`, `wiki_ops` output, `WikiPageSummary` | Reverse links shown without reading every page from the browser |
| V3 | Keyboard shortcuts, remembered collapse state of the tree | none | |
| V4 | Three modes (Read, Files, History) behind one control; build buttons and "New page" only in History; build job table | none | The reading view carries no build button; every build can still be started and followed from History |

V1 does not wait for V2: without a summary a card shows the scope line, and "Referenced by" is simply absent.

## 5. Rules

- The viewer shows pages as they are stored. It never rewrites a narrative to add links; `[[id]]` is turned into a link in the
  DOM after sanitizing, as citation chips are.
- Page ids come from the stored list. A link is built only from an id that matches `^[a-z0-9][a-z0-9-]{0,63}$`.
- `localStorage` keeps only conveniences (tree collapsed, citations hidden) and the page works without it.
- Every new string goes into both catalogs (`ko`, `en`); both use the same shape (`Messages` type).
- Reading wiki pages stays unchanged as evidence: the wiki is navigation, not evidence, and debate agents still do not see it unless
  `GUARDIAN_CODE_WIKI=1`.

## 6. Verification

- Unit tests for the link pass (`tests/wikiCitations.test.ts`), the tree and order helpers, the place bar and neighbour
  computation (pure functions in `lib/wiki.ts`).
- Component tests: the index with a mixed list (overview with groups, features, a stale page), search, empty wiki, a missing
  `[[id]]`; the reading view at both widths; each page action still asks its confirmation.
- A check in a real browser at 1280 px and 390 px (the desktop app is the same code); `scripts/ci.sh web` as a whole.

## 7. Decisions for the operator (all approved 2026-10-27, as recommended)

1. **Index layout.** Full-width cards by section (3.2) as recommended, or a plain tree list.
2. **Left tree in the reading view** on wide screens: keep (recommended), or reading view without it.
3. **Server additions in V2** (`summary`, `links` in the list): approve, or keep the viewer on what the list gives today.
4. **Page actions menu** (Rewrite, Edit, Delete behind one button): approve, or keep the three buttons visible.
5. **Keyboard shortcuts** (V3): build, or skip.
6. **Separate reading, files and history** (V4, approved 2026-10-28 as recommended): modes Read | Files | History; History shows build jobs only (no server change), not page edits; `?view=` stays the route key; "New page" and all build buttons live only in History; the reading view keeps the page-actions menu; the narrow-screen behaviour of V1 is kept.

## 8. Implementation notes

### V0: page links (built 2026-10-27, deployed 2026-10-28)

- `decorateNarrative` takes an optional `PageLinks` (`known` ids or `null`, `href`). A text pass outside `pre`, `code` and
  links, plus inline code that is only `[[id]]`, becomes `a.sg-wikilink`; an id outside `known` becomes a dashed
  `span.sg-wikilink-missing`. While the page list has not loaded, `known` is `null` and every id is linked (an id that does not
  exist then shows the page's own error).
- The route is `#/debates/wiki/<id>` through `hrefFor`. `WikiTab` passes the ids of the list to `WikiPageView`, which gives
  `WikiNarrative` the `PageLinks`.
- Not done: links are decided when the page is rendered, so a list that arrives later does not re-mark ids; a wiki page opened
  directly by URL may show a missing id as a link until the next visit.
- **Unverified:** only the `[[id]]` form is handled, because it is the form the writer prompt requires. No stored narrative was read
  (no hub access in that session), so whether real pages also contain relative Markdown links (`[text](page-id)`, `[text](docs/x.md)`) or bare
  paths without `:N` is open. If they do, the wiki pass (not `report.ts`) will map a target that is a known page id to a page link and a
  readable repository path to the file view.
- Not touched on purpose: `report.ts` still prints non-`http(s)` Markdown links as text, because agent debate text must not
  produce links to arbitrary targets. Only the wiki narrative pass above creates internal links.

### V1: index and reading view (built 2026-10-27, deployed 2026-10-28)

- `lib/wiki.ts` holds the pure parts: `buildWikiTree`, `indexOrder`, `pageNeighbours`, `parentOf`, `filterPages`, `attentionPages`,
  `referencedBy`. A parent is derived only for `kind === 'group'` (the longest known prefix before a `--`), because the page id
  pattern allows `--` in a hand-made `dir` id. A group whose overview is missing, or is not a root, is shown as a root of its own.
- **Index order** is each root followed by its groups, then the features. The design text said "roots, features, then groups"; that
  would put a group far from its overview, and the reading order is meant to be read in sequence. It is a permutation of the list.
- `WikiIndex.tsx` is the entry screen, `WikiReading.tsx` the frame around a page (place bar, tree, neighbours), `WikiTab.tsx` chooses
  between index, reading view and the unchanged file view. The old two-column list and its "select a page" card are gone, and with them
  the catalog keys `tab.selectPage` and `tab.pagesTitle`.
- The state line does not carry the date of the newest page (3.2): it shows "N pages, M need attention" and the checked commit; dates are on each page.
- The search box sits in the body of the index, not in the header card of the tab: the header is shared with the page and file views.
- **Attention** shows only the state chip. The list has no reason (changed files, broken citations), and fetching every page for it would
  cost one request per page; the reason stays on the page itself.
- **Page actions** is a disclosure (`aria-expanded`), not a `role="menu"`: the three buttons stay ordinary buttons with their own labels.
  It closes on a choice, on Escape and on a mouse press outside. Rewrite stays disabled while a build runs.
- Moving to another page scrolls its top anchor into view (`scrollIntoView`). This also replaces the position the browser would restore on
  Back, so "going back restores the position" in 3.3 is not kept for hash-route moves between wiki pages.
- The tree is shown on wide screens only and is not rebuilt from the page request: it uses the list, so while the list loads, the reading view
  has no tree and no neighbours. A list that fails to load is not reported in the reading view (the page has its own error).
- V0's note about a late-arriving list is closed: `knownPages` is memoized from the list, so `[[id]]` links are decided against the list
  once it is there.

### V2: `summary` and `links` (built 2026-10-27, deployed 2026-10-28)

- `listing()` in `code_wiki.py` adds `kind`, `summary` and `links` to each entry; `wiki_ops.list_pages` passes them through. The Go agent copies
  the CLI's JSON unchanged (`respondConfig`), so no Go change was needed.
- `page_summary`: the first paragraph of the **first section**, whatever its heading (the writer's heading names vary), at most 160
  characters. Line citations (`` `path:12` ``), empty parentheses left behind and `[[id]]` brackets are stripped, so a card reads as prose.
- `page_links`: `[[id]]` ids in order of appearance, without the page itself, unknown ids, duplicates and fenced code; at most 60.
- The summary is built from the stored narrative on each list request; nothing new is stored.
- **Referenced by** is empty when the hub sends no `links` (an older hub); the card falls back to the scope line.

### V3: shortcuts and remembered tree state (built 2026-10-27, deployed 2026-10-28)

- `hooks/useWikiShortcuts.ts`: `[` and `]` (previous, next), `g` then `i` within one second (index), `/` (search). Keys are matched on
  `event.code`, so a Korean input layout does not break them. Ignored while composing, with ctrl, meta or alt, in an input, textarea,
  select or editable element, and while any dialog is open. `preventDefault` is called only when a handler runs.
- `/` focuses the search box on the index; from a page it goes to `#/debates/wiki?find=1`, and the index focuses the box when it sees `find=1`.
- Shortcuts are off in the file view. The collapsed state of the tree is kept in `localStorage` (`sg.wiki.tree.collapsed`), wrapped in
  try/catch; the page works without it.

### V4: Read, Files and History modes (built and deployed 2026-10-28)

- `lib/wiki.ts`: `wikiMode(page, query)` picks `read` (also the default), `files` (`?view=files`, or any `?file=`) or `history` (`?view=history`); a page id always means `read`.
  `wikiJobs(jobs, project)` keeps the wiki build jobs (`kind: 'code_wiki'`) of the project, newest first.
- `WikiTab` is a shell: project switcher and a `SegmentedControl` for the mode, the build banner and the "build running" notice (shown in every mode, so a build
  started in History is still visible after switching), then the body of the mode. `hooks/useWikiBuild.ts` owns starting a build, the running-build lookup, the
  banner and the reload when a build ends.
- `WikiHistory.tsx` holds what used to sit above the index: Reload, New page, Write first page or Rewrite stale, Rewrite all, Write design doc pages, Split large
  directories (each still behind its confirmation), a table of the project's build jobs (task, state, started, duration, link to the monitor) and the list of
  pages that need attention. The reading view keeps only the page-actions menu; a rewrite from it still goes through `useWikiBuild`.
- Deviations: "wiki pages covering this file" already existed in the file view, so nothing was added there. The strings `files.browse` and `files.toPages` were removed
  (the mode control replaces them). On a narrow screen the header is hidden while a page or a file is open, and the file view gets a back link to the file list.
- History is scoped to build jobs. The wiki edit and delete handlers keep no history the dashboard can read (the list only carries each page's `updated`), so page edits and deletions are not shown; adding them needs a server-side log with a read API.

### Tests and what is still open

- `tests/wikiViewer.test.ts` (helpers), `tests/wiki.test.tsx` (index, search, split directories, reading view, actions, tree state,
  shortcuts), `python3 -m unittest test_code_wiki` (92 tests, `ViewerListingTests`). V4: `Wiki view: modes` in `tests/wiki.test.tsx` (7 tests) and the changed build tests; `scripts/ci.sh web` passed with 826 tests.
- **Real-browser check (2026-10-31, hub dashboard in Chrome, about 1180 px wide, read-only):** V0: a stored page had 11 `[[id]]`
  links, none marked missing, a click opened the target page. V1: index, search, status, start list and cards. V2: summaries in
  the list and the "referenced by" section. V3: `]`/`[`, `g i`, and `/` (opens `?find=1` with the field focused). V4: Read |
  Files (979 files) | History, build buttons only in History. No defect found in the wiki view itself.
- **Not done:** 390 px (the browser window could not be narrowed from the tool; the built-in pane needs a token login), the tree's
  collapse state after a reload, and the build buttons themselves (they write).
