# Symbol index, repository map and code wiki for debate agents (design)

English | [한국어](debate-code-index-wiki.ko.md)

This adds navigation aids on top of the retrieval in `debate-agent-retrieval.md` (repo blocks, `repo_*` MCP) without changing it.
Retrieval is still the only way to get evidence; every feature in this document only narrows down "where to read".

Current values (2026-10-18): `INDEX_VERSION` 2 (`code_index.py`), `MAP_VERSION` 3 (`repo_map.py`), `GOINDEX_VERSION` 1 (`go_facts.py`),
`WIKI_PROMPT_VERSION` `2026-10-16.wiki6` (`wiki_build.py`), `RETRIEVAL_PROMPT_VERSION` `2026-10-17.p10` (`debate_engine.py`; the bumps
after p2.6 come from features outside this document). Version values in the implementation notes below are those of their phase.

## Problem

1. **Language bias.** `outline` and `path#Symbol` support only Go and Python (`code_select.language`). Of 479 tracked files, 4.2 MB,
   on 2026-10-12, TSX 843 KB and TS 249 KB (apps/web-desktop and others) have no outline and rely on grep and full reads.
2. **No relation lookup.** There is no op to ask "who calls this function" or "where is this type defined", so agents imitate it with
   regex grep.
3. **Over-reading.** In a hub run on 2026-10-09 one Expert step requested 1,301 items, 5.6 MB. Without a way to find the path, agents
   cope by reading a lot.
4. **First entry point.** The first prompt only gives a file list (at most 500 lines). Apart from path names there is no way to know
   what is where.
5. **Design intent.** "Why is it built this way" has to be inferred again from code every time, and every debate repeats the same
   exploration.

## Decision summary

| Layer | Content | Accuracy | Usable as evidence |
|---|---|---|---|
| 0. Retrieval (existing) | `list`/`grep`/`read`/`outline` | Source | Yes |
| 1. Symbol index | Declaration and reference tables at a fixed sha. `defs`/`refs` ops, TS/TSX `outline` | Machine-generated, matches the sha | Only as location (what counts as read: "Citation checks" below) |
| 2a. Repository map (wiki generated layer) | A per-sha summary of directories, files and main symbols built from the index. Goes into the first prompt and the top of wiki pages | Machine-generated | No |
| 2b. Wiki narrative layer | Explanations per directory or feature (role, flow, caveats). Records the basis files and their blob hashes and becomes `stale` when they change | Written by an LLM, can go stale | **No** |

- 2a and 2b are two layers of one wiki page. Opening a page shows 2a (regenerated at the current sha) together with 2b (the stored
  explanation and its staleness).
- Neither people nor the LLM edit 2a. 2b does not describe locations in prose, only through citations (`path:N`, `path#Symbol`).
  Citations are checked at the current sha on read.
- Dependencies are standard library only (the principle of `requirements.txt`). No tree-sitter, LSP or embeddings.

## Layer 1. Symbol index `cli/llm-agent/code_index.py`

### Parser

It follows the same principle as `code_select` (layout parsing that assumes code formatted by gofmt or prettier); one function per
language returns a file's declarations and identifier occurrences.

| Language | Declarations | References |
|---|---|---|
| Python | `ast`: module-level `def`/`class`, methods inside classes (`Type.method`), module-level assigned constants | Identifiers and lines of `ast` `Name`, `Attribute` and `Call` nodes |
| Go | The existing `_go_decl_span` rule: `func`/`func (r T)`/`type`/`var`/`const` blocks at column 0 | Identifier tokens without comments and strings |
| TS/TSX/JS | Column 0 `export`? + `function`/`class`/`interface`/`type`/`enum`/`const X =`/`let X =`, `export default function X`. The end line is the next column-0 `}` or `};`, or just before the next column-0 declaration | Identifier tokens without comments, strings and template literals, including JSX tag names (`<Name`) |
| Others | None (only the path appears in the map) | None |

- Declaration entry: `{"name", "qualified", "kind", "path", "start", "end", "signature"}`. `signature` is the declaration's first line
  cut at 200 characters.
- References are **name-based**. Without type information, other symbols with the same name get mixed in. The result says so
  (`refs` below).
- A parse failure (syntax error, irregular layout) leaves that file "without declarations" and does not fail the whole index. Only
  the path goes into `index_errors`.
- `outline` (an existing op) switches to this parser and also supports TS/TSX/JS. `path#Symbol` looks up the same declaration
  table (Go and Python results must stay as they are, and the existing `test_code_select.py` must pass unchanged).

### Scope and exclusions

Only the same set as `RepoTools.readable_paths()` is indexed (secret patterns, symbolic links, submodules and files with NUL bytes
excluded). A file over 1 MB is not indexed (treated as generated).

### Cache

- Location: `<GUARDIAN_DATA_DIR>/code-index/<project>/blobs/<first 2 characters of the blob id>/<blob id>.json`. Equal content means
  an equal blob id, so when the sha changes only changed files are parsed again.
- The blob id is the third field of `git ls-tree -r -l`. `RepoTools.entries` is widened to store the blob id with `(mode, size)`.
- When the parser version (`INDEX_VERSION`) changes, cache files are ignored and rebuilt. The cache JSON holds only the declaration
  table (`signature` is the declaration's first line with string literal contents masked as `"…"`, P2.1) and identifier
  occurrences (`{identifier: [lines]}`), no source body.
- Per-sha aggregates (full declaration table, reverse reference table, map ranking) are built in memory and kept in
  `<project>/sha/<sha>.json`. Only the 20 most recent shas are kept. (2026-10-18: the per-sha files were not built; the aggregates
  live in memory only. See "Implementation notes (P1)" and "(P2)" below.)
- It works without a cache or when writing fails (built in memory for that run only).
- Lifetime (P2.1): cache hits and writes update the file mtime. The first time a process writes the cache, files in the project
  cache older than `GUARDIAN_CODE_INDEX_MAX_AGE_DAYS` (default 30) are deleted, and if the total exceeds
  `GUARDIAN_CODE_INDEX_MAX_MB` (default 256) the oldest go first (0 turns that rule off). The nightly backup tarball excludes
  `code-index/`.

### New ops

| op | Arguments | Result |
|---|---|---|
| `defs` | `symbol` (`Name` or `Type.Method`, `SYMBOL_PATTERN`), `glob` (optional) | `path:start-end kind signature` lines, at most 100 |
| `refs` | `symbol`, `glob` (optional), `include_defs` (default false) | `path:line: text` lines (same format as grep), at most 200 lines, 300 characters per line. A header line says "name-based result; other symbols with the same name may be mixed in" |
| `map` | `path` (directory, default the root), `depth` (1–4, default 2) | That part of the 2a repository map |
| `wiki` | `page` (page id) or `query` (string) | The layer 2 wiki (below) |

- The result shape is the same as before (`{"op", "target", "ok", "bytes", "truncated", "error", "text"}`). All budget and
  `deferred` rules apply as for the existing ops.
- Added to the dispatch in `RepoTools.run` and exposed in `repo_mcp.py` as the tools `repo_defs`, `repo_refs`, `repo_map` and
  `repo_wiki`. The server log format is the same.
- The four ops are added to the op descriptions in the prompts (`_retrieval_intro`, MCP guidance), with the advice "to find a
  location use defs/refs rather than grep; for a large file use outline first".

### Relation to citation checks

Rules for `provided_paths` (files whose code was given to the agent):

| op | Files counted as provided |
|---|---|
| `defs` | None. A one-line signature is not code evidence (`outline` counts as it does today) |
| `refs` | Like grep, the files whose lines appear |
| `map` | None (like `list`) |
| `wiki` | None |

`mentioned_in` follows the same rule. Paths in `defs`, `map` and `wiki` text do not count as plain mentions. So citing `path:N`
after seeing only the wiki or the map is caught by the citation check and asked again, as today.

### Implementation notes (P1)

Decisions that differ from the design or were not in it.

- The per-sha JSON (`<project>/sha/<sha>.json`) is not built in P1. The aggregates (declaration table, identifier reverse
  reference table) are built once in memory per `RepoTools` instance when the first `defs`/`refs` arrives (under a lock), and with
  a blob cache only changed files are parsed again. The 20-sha retention rule comes together with the sha files (P2 or later).
- The cache location is injected with `RepoTools(workspace, sha, cache_dir=...)`. The engine passes
  `code_index.default_cache_dir(project.id)`, and the MCP server takes `--cache-dir` (the `cache_dir` of the spec built by
  `McpTurn`). Without the argument only memory is used. `default_cache_dir` only reads `GUARDIAN_DATA_DIR` and does not create the
  directory (it appears when the first cache file is written).
- A declaration's `start` is the line with the declaration keyword (the preceding comment block is not included). `refs` treats
  the `start` line as the definition line and leaves it out. Go's existing `path#Symbol` keeps its current rule, which includes the
  comment block.
- Only files of `readable_paths()` in languages with a parser (`.go` `.py` `.ts` `.tsx` `.js` `.jsx` `.mjs` `.cjs`) are indexed.
  Other files do not appear in `defs`/`refs` (use grep).
- Python constants are simple-name assignments at module level; upper-case names are `const`, the rest `variable`. Methods of
  nested classes are recorded as `Outer.Inner.deep` and also found as `Inner.deep`. Go also records names indented by one tab
  inside `const (`/`var (`/`type (` blocks as declarations.
- TS/JS end line rule: starting after the declaration line, a line whose column 0 is `}` `)` `]` ends it. If that line ends with
  `{` `(` `[` `=>` (`): Foo {`, `} else {`), it is treated as a continuation of the signature or body. If another column-0 line
  (the next declaration, import/export, a comment) comes first, it ends at the last non-empty line before it. Class members,
  declarations inside functions, decorators on their own line and CommonJS exports are not handled.
- Limits of the identifier scan: a regex literal containing quotes or `//`, and an apostrophe in JSX text, can hide the rest of
  the line. Template literals drop only the text parts; `${...}` expressions are scanned.
- `code_select.language` returns `"ts"` for the TS family, and `outline`/`symbol_spans`/`import_spans` import `code_index` lazily
  and use it. So `path#Symbol` selection in code context also works for TS files. Go and Python results do not change.
- `refs` in `provided_paths` uses the same regex as `grep`. The header line has no `path:N: ` shape, so it does not count any file
  as provided.

## Layer 1 extension. Compiler-assisted index (P2.6, Go first)

### Why

Layer 1 `refs` is name-based. Other symbols with the same name get mixed in (`run`, `Close`, `ServeHTTP`), and it cannot tell
method calls or package boundaries apart.
The 2026-10-12 architecture review (`docs/analysis/2026-10-12-retrieval-architecture-review.md`, W1) first put an import graph
built with the standard library into P2.6.
But the compiler already knows these relations exactly. So P2.6 becomes **extracting facts type-checked with the hub's toolchain
and adding them to the index**. Source stays in git as it is; the worktree and artifacts used for the build are deleted every time.

- Go comes first. `go list` and `go/types` type checking do not run the target code (provided `go generate`, tests and cgo are not
  used). The hub has Go 1.26.8 (`~/.local/go/bin`), and server-guardian's `apps/agent` is a module without external dependencies.
- TS (`tsc`) needs `node_modules`. `npm install` can run arbitrary code through dependency install scripts, so it is not part of
  this phase ("Next languages" below).
- Python keeps using `ast` (layer 1).
- The debate engine (Python) keeps using only the standard library. A separate executable does the extraction; the engine only
  reads its JSON output.

### Structure

```
fixed sha ──git worktree add --detach──▶ temporary worktree (0700)
                                           │ goindex (Go helper, standard library only)
                                           │   go list -json -export -deps ./...
                                           │   go/parser + go/types (gc export data)
                                           ▼
                                 facts JSON ──▶ <GUARDIAN_DATA_DIR>/code-index/<project>/go/<sha>.json
                                           │
                git worktree remove --force, build artifacts deleted
```

1. **Temporary worktree.** The engine creates it with `git -C <workspace> worktree add --detach <tmp>/src <sha>`. `<tmp>` comes from
   `mkdtemp` (0700).
   - The worktree and index of the workspace clone that publish uses are not touched. Only the `.git/worktrees/` entry changes, and
     it is removed at the end.
   - The engine runs `git worktree prune` once at start and before every extraction to clean up entries of earlier interrupted
     extractions.
2. **The `goindex` helper.** It lives in `tools/goindex/`, has its own `go.mod` and uses only the standard library. The install
   script cross-builds it like the agent and puts it at `~/.local/libexec/guardian/goindex` on the hub.
   - Input: the worktree path, the module directories (directories with `go.mod`; `.git`, `node_modules` and `vendor` excluded by
     the same rule as `MCPClient._go_modules`), the output file path.
   - For each module, `go list -json -export -deps ./...` gives packages, files, imports and export data paths. Standard library
     packages yield no facts; they are only used through export data.
   - Each package of the target module is read with `go/parser` and type-checked by `go/types` with
     `importer.ForCompiler(fset, "gc", lookup)`. The lookup opens the export files `go list` reported.
   - `types.Info.Defs`, `Uses` and `Selections` resolve definitions and references. For each reference the enclosing function
     (`FuncDecl`, or `Type.Method` for methods) is recorded.
   - Test files (`_test.go`) are included. The test packages of `go list -test` are used, and results are marked `test: true`.
3. **Environment.** The helper and its children are forced to:
   - `CGO_ENABLED=0`: no C compiler.
   - `GOTOOLCHAIN=local`: no toolchain download even with a `toolchain` directive in `go.mod`.
   - `GOFLAGS=-mod=readonly` (other values are overwritten), `GOPROXY=off`. Dependencies missing from the module cache are not
     fetched; the module fails ("Failures" below).
   - `GOCACHE=<GUARDIAN_DATA_DIR>/code-index/.gocache`: separate from the user build cache (`~/.cache/go-build`).
   - Secret environment variables (tokens, `llm.env` contents) are not passed (the same `tool_env` rule as
     `MCPClient.tool_environ`).
   - `~/.local/go/bin` goes first in `PATH`. The hub service's PATH has no user Go.
   - The time limit is `GUARDIAN_GOINDEX_TIMEOUT` (default 180 seconds). Past it the process group is killed and the run fails.
4. **Cleanup.** In `finally`, success or failure:
   - `git worktree remove --force <tmp>/src`, then delete `<tmp>`
   - `go clean -cache` when `GOCACHE` exceeds `GUARDIAN_GOINDEX_CACHE_MB` (default 1024)
   - No binaries are built (no `go build`, only `go list -export`). The only artifact left outside the worktree is `GOCACHE`.

### Fact format

`<GUARDIAN_DATA_DIR>/code-index/<project>/go/<sha>.json`. The key is `(sha, GOINDEX_VERSION, go version)`; if any of the three
differs, the file is rebuilt.

```json
{"version": 1, "sha": "…", "go": "go1.26.8", "modules": ["apps/agent"],
 "packages": [{"path": "github.com/server-guardian/agent", "dir": "apps/agent", "imports": ["net/http", "…"],
               "errors": []}],
 "defs": [{"id": 17, "name": "followEvents", "qualified": "debateHub.followEvents", "kind": "method",
           "pkg": "github.com/server-guardian/agent", "path": "apps/agent/debate_events.go", "start": 168, "end": 230,
           "exported": false, "test": false}],
 "refs": [{"def": 17, "path": "apps/agent/debate_events.go", "line": 120, "enclosing": "debateHub.debateEvents",
           "call": true, "test": false}],
 "unresolved": [{"path": "apps/agent/x.go", "line": 9, "name": "Foo", "reason": "type error"}]}
```

- Paths are relative to the repository. Definitions and references in files not in `readable_paths()` (secret patterns, symbolic
  links) are **not written**. Those files are still used for compiling but leave nothing in the facts JSON.
- No source body, string literals or comments (only locations and names). So this file holds less than the layer 1 cache.
- `call` says whether the reference is a call expression (the function part of a `CallExpr`). An interface method call points at
  the interface method definition; which implementation runs is not resolved ("Limits" below).
- Only the 20 most recent sha files per project are kept. As decided they are excluded from backups and the oldest go first.

### When they are built

- When retrieval is turned on for a debate (`_setup_retrieval`), if there are no facts for that sha and the repository has
  `go.mod`, extraction starts in a **background thread**. The debate start does not wait for it.
- When `callers`, `refs` or `defs` first arrives for a Go target while extraction is running, it waits at most
  `GUARDIAN_GOINDEX_WAIT` (default 45 seconds, shorter than the 60-second MCP tool call limit of some CLIs (codex)). If it does not
  finish in time, the layer 1 name-based result is returned with the header "type information being prepared, name-based result".
- Only one extraction per sha runs per project (file lock `<sha>.lock`). Two debates that start at the same time wait for the same
  result.
- Right after a project sync (`--project-sync`) the new HEAD can also be extracted in advance (optional,
  `GUARDIAN_GOINDEX_PREFETCH=1`). (2026-10-18: not built; see "Implementation notes (P2.6)" below.)

### Op changes

| op | Change |
|---|---|
| `refs` | For a Go target (the symbol's definition is in a Go file, or `glob` selects only Go) with facts present, only type-checked references are returned. The header is `[refs of X: type-checked (go1.26.8), N packages; non-Go files are name-based]`. Other symbols with the same name drop out. Non-Go files are appended name-based as today, with their own header before that group of lines |
| `defs` | Go definitions use the facts' `start`/`end` and package. When the same name exists in several packages the package path is shown |
| `callers` (new op) | Arguments `symbol`, `glob` (optional), `include_tests` (default false). Only lines that **call** the symbol (`call: true`), as `path:line: enclosing  text`. At most 200 lines. Without Go facts it returns `error: "no type information for this language"` and points to `refs` |
| `callees` (new op) | Argument `symbol`. The call targets inside that function or method body with their definition location (`path:start`). At most 200 lines. Targets outside the module (standard library) show only package path and name |

- MCP tools `repo_callers` and `repo_callees` are added under the same rules.
- Prompt guidance: "Find Go call relations with callers/callees, and confirm with read before citing."
- **Evidence rule:** like `defs`, `callers` and `callees` count no file as provided (architecture review decision). They only give
  locations; to cite, the source must be fetched with `read`. Type-checked `refs` counts like grep, as today. Once line-range
  citation checks (P2.5) arrive, their rules apply.
- The repository map (2a) ranking of Go files also uses the facts' reference counts (real references, not equal names). The map
  text changes, so `MAP_VERSION` goes up.

### Failures

A failed extraction does not stop the debate. Go files that failed fall back to layer 1 name-based results.

| Case | Handling |
|---|---|
| No `go`, no `goindex` | No extraction. One event, no retry for the rest of this run |
| Dependency missing from the module cache (`GOPROXY=off`) | Only that module is recorded as failed. Other modules continue |
| Package with type errors | The resolved parts are written; unresolved identifiers go into `unresolved`. `refs` and `callers` results end with "N locations not resolved due to type errors" (the same principle as P2.1's countermeasure against silent false negatives) |
| Time limit exceeded | The process group is ended, failure recorded. The same sha is not retried in this run |
| Cleanup failure (`worktree remove`) | Warning event. Recovered by `git worktree prune` and deleting leftover temporary directories before the next extraction |

### Records

- Event `compiler_index`: `data {"lang": "go", "sha", "status": "built"|"cached"|"failed"|"timeout"|"unavailable", "packages", "defs", "refs", "unresolved", "ms", "error"}`. Emitted once when extraction ends.
- The thread's `context_snapshot.index` gets `go: {"version", "go", "status", "packages"}`.
- Entries of `retrieval_served.results[]` get `resolved: "types"|"name"` (`refs`, `defs`, `callers`, `callees`).

### Limits

- Interface calls resolve only to the interface method. No call graph into implementations (pointer analysis). The `callers`
  header says "calls through an interface are shown under the interface method".
- Function values, reflection and `go:linkname` are not resolved.
- Build tags follow the hub's `GOOS/GOARCH` (linux/arm64). Files for other platforms are not in the facts and stay with layer 1
  results.
- Language boundaries (Go calling `guardian.py` through `exec.CommandContext`) are not connected by type information. Such paths
  are still found with grep and read (evaluation 2's E2 measures this case).

### Next languages (outside P2.6)

- **TS:** a `tsc --noEmit` based helper only when the project has a lockfile and an already installed `node_modules`. If the hub
  would have to install it, only `npm ci --ignore-scripts` is allowed, and that needs a separate decision.
- **Separate worktree for test runs:** project tests currently run inside the workspace clone and leave artifacts
  (`apps/agent/agent`, `__pycache__`). Moving them to the same temporary worktree approach changes the action execution flow
  (`MCPClient`, publish checks), so it is designed as a separate phase.

### Verification (P2.6)

- `goindex` unit tests (Go): same name in different packages, method vs function, interface calls, test files, packages with type
  errors, excluding secret paths.
- Engine tests (Python, skipped without a real `go`): reading the facts JSON and `refs`/`callers`/`callees` results, failure paths
  (no helper, timeout), cleanup (no worktree or temporary directory left, `git worktree list` back to the original).
- Go version of decoy 1: with the same name `Handle` in two packages and only one called, `refs` and `callers` show only the called
  one.
- Hub measurements: extraction time on server-guardian (first, reuse of the same sha), `GOCACHE` size, peak memory.
- In evaluation 2, answer accuracy on B1–B3 and E2 must be equal to or better than P2.1, and grep requests must drop for Go
  questions.

### Implementation notes (P2.6)

Implementation: `tools/goindex/` (Go helper, standard library only), `cli/llm-agent/go_facts.py` (reading, extracting, storing and
waiting for facts), `go_ops.py` (the bodies of `refs`/`defs`/`callers`/`callees`), and the wiring in `repo_tools.py`,
`repo_map.py`, `repo_mcp.py` and `debate_engine.py`. Decisions that differ from the design or were not in it.

**Fact format and helper**

- The helper arguments are `-root -module (repeated) -readable -out` plus `-sha` (written to the output as is) and `-timeout`
  (0 means no limit). Exit codes: 0 (facts written), 1 (no module indexed; facts with `module_errors` are still written), 2 (usage
  or I/O error), 3 (timeout, nothing written). The engine passes a `-timeout` 30 seconds longer than its own limit, so a helper
  orphaned by a dying engine ends by itself.
- `-readable` gets only readable `.go` files (non-Go files do not appear in the facts).
- `modules` is, as designed, the list of directory strings indexed successfully; failed modules go separately into
  `module_errors: [{dir, error}]`. `files` (the `.go` files type-checked and reported) was added. The engine keeps treating Go
  files not in this list (build tags or other platforms) with layer 1 name-based results. `go`, `goos`, `goarch` and `sha` are
  written too.
- `kind` uses the same names as layer 1 (`function` `method` `struct` `interface` `type` `var` `const`). A method declared in an
  interface is a `method` definition named `Iface.Method`, and interface calls point there. Fields, parameters and local variables
  are not definitions. `init` and `_` are left out.
- The facts have no signatures. `defs` takes the signature (first line with masked literals) from the layer 1 declaration at the
  same location (`path`, `start`), or writes the qualified name when there is none.
- Call targets outside the module (standard library, dependencies) are kept only as `external: true` definitions (no path or
  line) and `call: true` references. Only one is kept per calling function and target pair (the first call line), so they serve
  `callees`; `refs`/`callers` only cover definitions inside the module. Calls to functions defined in an unreadable file of the
  module (secret names) are not kept even as external targets.
- When the same declaration is checked several times (a package and its test variant), a definition appears once, by name
  position (`file:line:column`). Test variants report only definitions and references of `_test.go` files, and the generated test
  main of `go list -test` is skipped. Packages inside the module are type-checked from source rather than from `go list` export
  data (so that dependents still get types from a package with type errors); only the standard library and dependencies use
  export data.
- Unresolved identifiers (`unresolved`) are identifiers in packages with type errors that are in neither `Defs` nor `Uses`
  (excluding `_`, package names and labels). Error message bodies can contain source fragments, so they are not stored;
  `packages[].errors` keeps only `path:line` (errors of unreadable files lose even the location). `unresolved` holds at most 5000.
- The environment adds `GOWORK=off` to the four designed values, and `go list` gets `-buildvcs=false`. `go.mod` files under
  `testdata` do not count as modules (an addition to the `MCPClient._go_modules` rule).

**Engine**

- `GoFactsProvider` is created when `RepoTools` gets a `cache_dir` (`<cache_dir>/go/` is the store, the build cache
  `<GUARDIAN_DATA_DIR>/code-index/.gocache`). Only the engine's `_setup_retrieval` starts extraction (`start()`); the repo MCP server
  process uses the same class but only reads stored files. If the file is missing and another process holds `<sha>.lock`, it
  waits at most `GUARDIAN_GOINDEX_WAIT` seconds.
- When extraction ends it leaves `<sha>.status.json` (status, error, time). Reading processes use it to report
  "failed/timeout/unavailable", and a new extraction deletes it at start. It is not used for cache hits. The facts file's mtime is
  updated on every hit (the 20-most-recent rule uses that mtime), and deleting it also deletes `.status.json` and `.lock`.
- Waiting happens only once, when a request first meets the in-progress state. Later requests do not wait and get the name-based
  result with the header notice right away. If the wait ends without a result, type-checked results start with the first request
  after extraction has finished.
- When the debate ends, a still-running extraction is cancelled (the helper's process group is killed and the worktree and
  temporary directory deleted). So a very short debate may not finish the extraction, and the prewarming
  `GUARDIAN_GOINDEX_PREFETCH` was not built.
- Temporary directories have the prefix `guardian-goindex-`; before extraction those older than an hour are deleted, then
  `git worktree prune` runs. `git worktree add` gets `core.hooksPath=/dev/null` so repository hooks do not run. The only change in
  the workspace clone is `.git/worktrees/`.
- A cleanup failure (`worktree remove` failing or the temporary directory not deleted) is reported with a separate event
  `compiler_index_warning {lang, sha, message}`. This is the design's "warning event".
- `compiler_index` is held back and emitted after `debate_started` (a cache hit happens inside `_setup_retrieval`, which would
  otherwise have no thread id and come first). The event's `defs` is the number of definitions inside the module (external call
  targets excluded) and `packages` the number of distinct package paths.
- The snapshot's `index.go` is `{version, go, status, packages}` and is recorded only when `index` exists (when `GUARDIAN_REPO_MAP`
  is on). It is updated twice, at setup and at the end of the run, so if extraction is still running at the end it is `pending`.
- Test isolation: `testenv.isolate_data_dir()` points `GUARDIAN_GOINDEX_BIN` at a missing file (this repository has a `go.mod`
  too, and unit tests on a hub that has the helper must not start a real extraction). `test_debate_engine.py` sets the same value.
- `GUARDIAN_GOINDEX_MAP_WAIT` (default 0 seconds) was added. The repository map in the first prompt waits only this long for
  extraction. With 0 it uses facts only if they already exist (cache hit); otherwise the name-based map gets a notice line.

**Ops**

- Go target decision: if the facts are already loaded, when the facts have a definition of the symbol. If they are not loaded,
  wait for the facts only when layer 1 found a declaration of the symbol in a `.go` file or `glob` ends with `.go` (other symbols do
  not wait and are name-based). `callers`/`callees` always wait.
- `refs` (with facts): the header is as designed; when two or more definitions share the name, the second line is
  `[N definitions match this name (packages: …); references to all of them are listed]` (no paths; paths in the result count as
  mentions). There is no syntax to qualify a symbol by package, so `Handle` is the `Handle` of every package. Below come the
  type-checked `path:line: text` lines, then, under their own header, the name-based lines of files without facts (non-Go files,
  Go files not type-checked). The 200-line cap counts both together. `include_defs` adds the definition lines to the type-checked
  part.
- End notice: locations not resolved due to type errors are merged into the P2.1 index coverage line
  (`[index coverage: 1 location unresolved due to type errors — results may be incomplete; use grep for these: 경로]`). So it is a
  line that `provided_paths`/`mentioned_in` ignore. Only `unresolved` entries with the same identifier name count, and with a
  `glob` only those inside it.
- `defs` (with facts): a `[defs of X: type-checked (go…)]` header, the definition lines (with `(package …)` at the end of the line
  when there are several packages), then the name-based lines of files without facts under
  `[defs of X in files without type information: name-based]`. Without facts it is as before.
- `callers`: `path:line: enclosingFunction  line text` (`(package level)` at package level); with several definitions,
  `matched N definitions:` and the definition locations under the header. Without `include_tests` and no result, it suggests
  including tests. `callees`: targets inside the module as `path:start  name  (called at line N)`, outside as
  `packagepath.Name  (called at …)`, in order of first call line. Both are capped at 200 lines. Without facts they return an error
  (`no type information for this language (Go only); use refs`, or while preparing or after failure
  `no type information yet (Go index <status>); use refs`).
- `resolved` goes into result entries as `"types"` or `"name"` (`refs`/`defs` are `types` when they use facts, otherwise `name`).
  So the result keys of `RepoTools.run` grow, and it also appears in `retrieval_served.results[]` events and the MCP server log.
  Failed calls do not have it.
- Notice lines of the name-based fallback: `[type information for Go is still being prepared: results are name-based]` (in
  progress), `[no type information for Go (failed|timeout|unavailable): results are name-based]`. A reading process that never
  attempted extraction writes nothing.
- Citation checks: like `defs`, `callers`/`callees` count no file as provided, create no line ranges and leave `mentioned_in`
  empty. For `refs`, the type-checked lines are the shown lines.
- Prompt: the retrieval guide gets a description of `callers`/`callees` with example requests, and the sentence "a Go result is
  type-checked only when its first line contains `type-checked`". `RETRIEVAL_PROMPT_VERSION` is `2026-10-12.p2.6`. The MCP tools
  are `repo_callers` (`symbol`, `glob`, `include_tests`) and `repo_callees` (`symbol`), appended to the tool list.

**Map**

- With facts, Go declarations that have a definition in the facts are ranked by the number of distinct referencing files outside
  the declaring file that are not noise paths (the rule excluding names of length 2 or less does not apply, since there is type
  information). If the declaring file is a noise path, the rank is 0. `MAP_VERSION` is 3.
- The map of a directory with Go files gets one line under the header: `[Go files ranked by type-checked references (go1.26.8)]`
  when facts were used, otherwise the name-based notice line above. Maps of repositories without Go modules are as before.

**Installation and CI**

- `scripts/install-agent-service.sh` cross-builds `tools/goindex` the same way as the agent (`GOOS=linux GOARCH=<hub>
  CGO_ENABLED=0`), uploads it to `~/.local/libexec/guardian/goindex` under a temporary name and swaps it in with `mv` (755). This step
  is an optional feature, so like the workspace clone and project sync steps it warns on failure and the install continues.
  `GUARDIAN_GOINDEX_*` was added to the `agent.env` example.
- `scripts/ci.sh go` adds gofmt, `go vet`, `go test -race` and linux arm64/amd64 builds for `tools/goindex`.

**Tests**

- `tools/goindex/index_test.go` (14 Go tests): same name in two packages, methods and functions, interface calls, test files,
  partial results of packages with type errors, readable files only, external call targets, enclosing functions, grouped
  declarations, forced environment, timeout exit code and more.
- `cli/llm-agent/test_go_facts.py`: fact lookup, retention count, the extraction flow with a fake helper (hit, different go version,
  different helper version, partial module failure, failure, no retry, process group kill on timeout, cancellation, no helper or
  go, cleanup failure, shared concurrent run, build cache cleanup, no secrets in the environment), waiting and fallback results,
  reading processes, the output of each op, the map, engine events and snapshot, plus an integration test that builds the real
  helper with `go build` and indexes a temporary repository (skipped without `go`) and the Go version of decoy 1.
- Existing tests whose expectations changed on purpose: `test_evidence.py` (prompt version), `test_llm_runner.py` and
  `test_repo_mcp.py` (two tools added to the tool list), `test_repo_index_ops.py` (`resolved` key in `defs` results).

**Measurements** (this repository at HEAD `f86f986`, one `apps/agent` module, this Mac arm64 / Go 1.26.8, fresh build cache)

| Item | Value |
|---|---|
| Extraction time, first (empty `GOCACHE`) | 5.7 s (helper 5.2 s) |
| Extraction time, build cache only (no facts file) | 1.0 s (helper 0.33 s, the rest is creating and removing the worktree) |
| Same sha reused (facts file hit) | 0.01 s |
| Facts JSON | 792,364 bytes (727 definitions + 258 external call targets, 4,911 references) |
| `GOCACHE` | about 125 MiB (130,634,186 bytes), mostly standard library compile output |
| Peak memory (max RSS of the helper and its children) | 333 MB first, 41 MB with a cache |
| Map generation (with facts, index in memory) | 0.036 s, 32,480 bytes |

On the hub (arm64, slower CPU) the first run may take longer. It must be measured again on the hub at the first deploy.

`callers` (`debateHub.followEvents`):

```
[callers of debateHub.followEvents: type-checked (go1.26.8); a call through an interface is listed under the interface method]
apps/agent/debate_events.go:137: debateHub.debateEvents  h.followEvents(w, r, id, after)
```

`callees` (`debateHub.startDebate`, first part):

```
[callees of debateHub.startDebate: type-checked (go1.26.8); standard library and dependency targets are shown as package.Name]
encoding/json.Decoder.Decode  (called at line 544)
net/http.MaxBytesReader  (called at line 544)
encoding/json.NewDecoder  (called at line 544)
errors.As  (called at line 546)
apps/agent/debate_handler.go:330  errJSON  (called at lines 547, 550, 554, 558, 603, 610, 620)
apps/agent/debate_handler.go:492  debateRequest.validate  (called at line 553)
apps/agent/debate_handler.go:632  debateHub.publishedRefusal  (called at line 557)
apps/agent/debate_handler.go:536  newJobID  (called at line 563)
...
apps/agent/debate_handler.go:659  debateHub.launch  (called at line 616)
apps/agent/main.go:21  jsonResponse  (called at line 626)
```

**Known limits**

- The Go build cache is `code-index/.gocache`. `default_cache_dir` strips a leading `.` from the project id, so it never overlaps
  any project's index cache directory.
- A symbol cannot be qualified by package (no `a.Handle` syntax). When several definitions share the name, `refs`/`callers` merge
  the results of all of them and list the packages in the header. Decoy 1's expectation is met as "lines of the uncalled one do
  not appear".
- Cancellation and `kill -9`: when the engine is killed, the worktree entry stays in `.git/worktrees/`. The next extraction deletes
  the temporary directory after an hour and `git worktree prune` reclaims the entry.

## 2a. Repository map

Built deterministically from the index (same sha and version give the same text).

```
--- Repository map (sha 3eda3e5, 479 files, ranked by references) ---
cli/llm-agent/
  debate_engine.py  [1702 lines] DebateEngine, DebateEngine.run, DebateEngine._call, retrieval_enabled, …
  repo_tools.py     [538 lines]  RepoTools, RepoTools.run, provided_paths, parse_repo_blocks, …
apps/agent/
  main.go           […] main, guard, …
docs/design/  (32 files)
```

- For each file, up to 6 declarations in order of **reverse reference count** (the number of other files where the name appears;
  distinct files since P2.1). Files without declarations show only path and line count.
- Non-code files in the same directory (documents, images) fold into `(N files)`.
- First prompt: the map replaces the current file list. The size cap is `GUARDIAN_REPO_MAP_BYTES` (default 32 KB). Over it, symbols
  of files with fewer reverse references go first; if still over, deep directories are folded. Folded parts say to expand them with
  the `map` op.
  This cap is a prompt size rule, separate from the retrieval budgets (`GUARDIAN_RETRIEVAL_*`) set to unlimited on 2026-10-09.
- A debate with reference code paths (`pack.snapshot["paths"]` not empty) gets no list, as today. Instead it gets only the map's
  root summary (one line per directory).
- `GUARDIAN_REPO_MAP=0` returns to the old file list (for comparison experiments).

### Implementation notes (P2)

Implementation: `cli/llm-agent/repo_map.py` (`build_map`, `root_summary`, `repo_map_cap`, `repo_map_enabled`, `MAP_VERSION`).
Decisions that differ from the design or were not in it.

- **`INDEX_VERSION` stays 1.** Line counts were needed, so the blob cache JSON gained `lines`. P1 cache files without `lines` fail
  the format check and are parsed again (without a version change). The cache JSON holds no source body, only the declaration's
  first line (`signature`, up to 200 characters), identifier occurrences and the line count.
- **Definition of folding non-code files.** Code files are those whose extension `code_index.language` has a parser for (.go .py
  .ts .tsx .js .jsx .mjs .cjs); other readable files are non-code. In a directory with code files they count as one line
  `  (+N non-code files)`; a directory with only non-code files is one line `dir/  (N files)`. N is the number of files directly in
  that directory. Code files over 1 MB that are not indexed appear as `[large file, not indexed]`.
- **Reverse reference count.** (Changed in P2.1: distinct files, noise paths excluded; see "Implementation notes (P2.1)" below.)
  The sum of the "number of lines" in files other than the declaring file where the declared identifier (the last component of the
  qualified name) appears. Ties break by qualified name; ranking between files by the maximum over their declarations, then path.
  Files without declarations are not ranked.
- **Order of cap handling.** After dropping symbols (lowest-ranked files first, the file keeps only `…`), if still over, the
  deepest directories fold one level at a time into `dir/  (N files, folded)`. If there is room left after folding, symbols of
  top-ranked files come back (the implementation picks the largest K that fits the cap at each step). When symbols are reduced or
  folded, a final line (about 110 bytes) says to expand with the `map` op. So when the gain of reducing is smaller than this line,
  dropping symbols makes it larger and one more level folds. The smallest form is the single line `(all N files, folded)`; if the
  cap is smaller than that, it is passed as is. At every step every readable file appears either in a list or in a count.
- **The `map` op.** `depth` D treats the requested `path` as level 0, shows levels up to D-1 in detail, and counts directories at
  level D and below as `dir/  (N files, folded)` (N is everything below). Like `list`, `depth` is clamped to 1–4 and non-integers
  default to 2. The size cap `GUARDIAN_REPO_MAP_BYTES` applies only to the first prompt; the `map` op only gets the normal result
  limit (`max_bytes`). The file count in the header is all readable files (not the number of indexed code files).
- **Root summary.** A debate with reference code paths gets, under `--- Repository summary (sha …, N files; …) ---`, one line per
  top-level directory `name/  N files; top symbols: a, b, c` (`./` for files directly under the root). No size cap (one line per
  top-level directory). The code bundle's own `Repository file list` stays.
- **The index is built at debate start.** With the map on, `_setup_retrieval` builds the index right away (it does not wait for
  `defs`/`refs`). If building the index or map fails, only a warning is printed, it falls back to the old file list and the debate
  continues.
- **Records.** The thread's `context_snapshot.index` is `{sha, version, files, symbols, prompt_version, map_version}` (`files` is the
  number of indexed code files); with the map off the index is not built, so the key is absent. `debate_started.data.context` gained
  `map_bytes` (bytes of map or summary put into the prompt, 0 when off), `prompt_version` (`RETRIEVAL_PROMPT_VERSION`, bumped when
  the retrieval prompt text changes) and `map_version` (`MAP_VERSION`, null when off). Reflected in the event table
  (`debate-agent-retrieval.md`).
- The per-sha JSON and the 20-sha retention were not built in P2 either (the map is built in memory on every call).

Measurements (this repository at HEAD `f2ef643`, 494 readable files, 299 indexed code files, 3,637 declarations, temporary cache
directory):

| Item | Value |
|---|---|
| Full map without a cap | 32,115 bytes |
| Map with the 32 KB cap | 32,115 bytes (within the cap, same as the full map, 0 folds) |
| Cap 16 KB / 8 KB / 4 KB | 16,316 / 8,185 / 1,591 bytes (12 directories folded at 8 KB, at 4 KB only top-level directories in detail) |
| Root summary | 541 bytes |
| Build time (with index) | cold 0.83 s, warm (blob cache) 0.10 s, map only with the index in memory 0.007 s |
| `map` op default (root, depth 2) | 1,591 bytes |

- This repository's full map is just under 32 KB, so the default cap does not apply. Folding only happens in larger repositories.
- Observation: reverse reference counts are name-based, so local constants of TSX demo files (`row`, `item`, `col`) and story names
  rise to the top. Ranking quality is looked at in evaluation 1.

### Implementation notes (P2.1, robustness)

W2, W4, W4b and W5 of the architecture review (`docs/analysis/2026-10-12-retrieval-architecture-review.md`), evaluation 1's map
ranking noise and cache lifetime went in together. The text agents see changes, so this went in after evaluation 1; `INDEX_VERSION`
became 2 and `MAP_VERSION` 2.

- **Marking silent false negatives (W2).** The last line of `defs`/`refs` results gets
  `[index coverage: N files failed to parse, M files over 1 MB not indexed, K files with this identifier capped at 5000 occurrences — results may be incomplete; use grep for these: 경로, …]`
  (`code_index.coverage_note`). Items with a count of 0 are left out; if all three are 0 there is no line. With a `glob` in the
  request only files matching it count. At most 10 paths, then `(+N more)`. It is also added to "no references/definitions"
  results (where a false negative is most dangerous). The cap only applies to `refs` and looks at one identifier, so the count is
  "files where that identifier hit the cap" (not stored separately in the cache; 5000 occurrence lines in a file counts as hitting
  the cap). The `map` op and the first prompt map put the same line right under the header (counting only files of the requested
  subtree, included in the cap calculation). Paths on this line are ignored by `provided_paths`/`mentioned_in` (they are neither
  code the agent saw nor paths mentioned in the body).
- **Cache content (W4).** Before storing, a declaration's `signature` has string literal contents replaced with `"…"` (Python
  quotes, triple quotes, `r`/`b`/`f`/`u` prefixes; Go double quotes, runes, backticks; TS quotes, backticks). An unclosed literal is
  masked to the end of the line. Trailing comments stay. So signatures in `defs` output may be masked, while `read` shows the real
  code. The `outline` that code context uses does not change: Go and Python outlines do not use the cache and are built from the
  original lines, and the TS outline (`ts_outline`) is built from the original lines (only the declarations for the cache are
  masked). `INDEX_VERSION` was bumped so older cache files that still contain literals are parsed again, and cleanup
  (`prune_cache`) deletes cache files of other versions immediately regardless of age. `RETRIEVAL_PROMPT_VERSION` became
  `2026-10-12.p2.1`, since the text agents see (map, end notices) changed.
- **Reading lines for `refs` (W5).** Line contents are read through `RepoTools._read_blobs` (`git cat-file --batch`) 25 files at a
  time instead of one `git cat-file` per file. The result text is the same.
- **Secret filter (W4b).** `code_context.is_secret` matches `SECRET_PATTERNS` against every component of the path (directories
  included), case-insensitively, and blocks `.ssh` `.gnupg` `.aws` `.docker` `.kube` as directories only (everything below is
  secret). Content is not inspected. Callers are the code bundle, `RepoTools.readable_paths`/`_excluded` and the edit block in
  `mcp_client`; the edit block looks at paths relative to the workspace (resolved paths), so a directory name above the workspace
  (`~/secrets-projects/…`) does not block every file. Of the 503 tracked files at this repository's HEAD (`c677431`), none is newly
  excluded from reading (`*secret*` and `*credential*` names were already excluded by the file name check).
- **The `retrieval_warning` event.** When the bytes served in one retrieval step (one repo block round or one MCP call) exceed
  `GUARDIAN_RETRIEVAL_WARN_BYTES` (default 256 KiB, 0 turns it off; first 2 MiB, lowered after the context compaction incident in
  hub wiki writing), `{step, bytes, threshold, via}` is emitted right after `retrieval_served`. No limit is imposed (budgets stay
  unlimited). The dashboard does not show it: the Agents monitor skips unknown event types (the reducer `default` in
  `agentMonitor.ts`).
- **Map ranking (evaluation 1 observation 1).** The reverse reference count is the *number of distinct files* where the name
  appears (declaring file excluded). References from story, preview or test paths (`*.stories.*`, `previews/`, `.design-sync/`,
  `__tests__/`, `*_test.go`, `test_*.py`, `*.test.ts(x)`/`.js(x)`) do not count. Identifiers of length 2 or less are not ranked.
  Declarations inside the same path set are not ranked either (the preview constant `nodes` being used in another file does not
  make it an important symbol). Declarations never referenced from another file are unranked and come after ranked declarations
  in qualified name order. In this repository the effect where `actions, nodes, commands`, `row, item, col` and story names of
  `.design-sync/previews/*.tsx` rose to the top disappears.
- **Cache lifetime.** `code_index.prune_cache` implements the lifetime rules of the "Cache" section above. It deletes only
  `blobs/*/*.json` and `.tmp-*` older than an hour, together with empty shard directories.
- **Tests.** `test_code_index.py` (masking, version, lifetime), `test_repo_index_ops.py` (every case of the end notice, batch
  reads), `test_repo_map.py` (ranking, map notice), `test_code_context.py` and `test_patch_ops.py` (secret paths),
  `test_db_backup.py` (backup exclusion), `test_retrieval_robustness.py` (warning event).

### Implementation notes (P2.5, evidence)

Line-level citation checks and the optional `findings` block (`cli/llm-agent/evidence.py`). The design and all rules are in
"Implementation notes (P2.5, evidence)" of `docs/design/debate-agent-retrieval.md`; only the points related to this document are
noted here.

- **`defs`/`map`/`list` are still not code evidence.** Their results create no shown line ranges, so citing `path:7` after seeing
  only `path:6-8` from `defs` is asked again as code never received. `refs`/`grep` count only the matching lines as shown ranges
  (lines of another file with a `Handle` of the same name do not qualify). So W3, where one `refs` call made 200 files citable, is
  gone.
- **No new `INDEX_VERSION`.** The index cache does not change. `RETRIEVAL_PROMPT_VERSION` becomes `2026-10-12.p2.5`.
- **P2.6's `callers`/`callees`** are ops that create no shown lines, as now (not counted as evidence). The P3 wiki is the same: wiki
  text is not shown lines.
- **Evaluation 2**: the `unverified` count now includes line-level mismatches, and `retrieval.findings` gives the findings
  submission rate and mismatch count per provider.

## Layer 2. Code wiki

### Pages

A page covers one directory (`cli/llm-agent`) or a named feature (`feature:debate-retrieval`).
(2026-10-18: feature page ids are `feature-<document name>`, and there are four page kinds: `dir`, `overview`, `group`, `feature`.
See `code-wiki-coverage.md`.)

Coverage extension (a feature page per design document, splitting large directories, file view; all implemented):
`code-wiki-coverage.md`.

Pages are stored in the hub data directory at `<GUARDIAN_DATA_DIR>/code-wiki/<project>/pages/<page id>.md` (nothing is written to
the target project repository; "Decisions" 1 below).

```markdown
---
id: cli-llm-agent
title: Debate engine (cli/llm-agent)
scope: ["cli/llm-agent/"]
written_sha: 3eda3e5…
basis:                       # files actually read for the narrative, with their blob ids then
  cli/llm-agent/debate_engine.py: 9c1f…
  cli/llm-agent/repo_tools.py: 41ab…
author: {kind: llm, provider: claude, job: kb_20261012_…}   # or {kind: human, user: …}
updated: 2026-10-12T09:00:00Z
---
## Role
...
## Flow
...(a citation such as `cli/llm-agent/debate_engine.py:845` for every claim)
## Caveats
...
## Related pages
[[feature-debate-retrieval]]
```

(Pages are written in Korean by default; the example shows an English page, which uses the English section titles.)

- The body holds only the narrative layer (2b). The generated layer (2a) is not stored in the file; it is built at the current sha
  when the page is opened. It contains the map of `scope` and the result of checking this page's citations at the current sha.
- `basis` is the files the writer actually read. For an LLM writer it is filled automatically from that session's
  `provided_files`; for a human writer from the strong citation paths in the body.

### Staleness

Computed every time a page is opened at the debate sha (not stored).

| State | Condition | Handling |
|---|---|---|
| `fresh` | Every blob id in `basis` equals the current one, and every cited path in the body exists in the current tree | Shown as is |
| `stale` | Some `basis` file changed | Shown with the warning "these files changed after this explanation was written: …" at the top |
| `broken` | A cited path is gone, or the symbol of a `path#Symbol` citation is not in the current index | Shown with a warning and the list of broken citations |

A new file in `scope` that is not in `basis` also counts as `stale` ("files the explanation does not cover: …"). `stale` and
`broken` pages are not put into the first prompt; they are only available through the `wiki` op.

### Exposure to agents

- `wiki` op: `query` searches page title, scope and body as a case-insensitive substring and returns a list of `id, title, state,
  scope` (at most 20). `page` returns one page in the form above.
- First prompt: if there are `fresh` pages, an `id — title (scope)` list (at most 40 lines) goes after the map. No bodies.
  (2026-10-18: the list is ordered `overview` and `dir`, then `feature`, then `group`; see `code-wiki-coverage.md`.)
- Guidance: "The wiki is navigation. Wiki content is not evidence; cite claims with code you read yourself. If the wiki differs from
  the code, the code is right."
- Result text gets the header `[wiki page … — navigation notes, not evidence; state: fresh]`.

### Writing and updating

Wiki narratives are made by an LLM job that uses the current retrieval loop and citation checks as they are. No separate
summarization path.

- Job kind `code_wiki`: `guardian.py code-wiki build --project P [--page ID | --stale | --all] [--sha S]` (2026-10-18: the actual
  command is `--code-wiki-build`; see "Implementation notes (P3)" below). The hub Go API creates a job directory and runs it like a
  debate job. Events use the same `events.jsonl` format and add `type: wiki_page_written {page, state, basis_files, citations,
  unverified}`.
- One page = one session. The prompt is the map (of that scope), the existing narrative (if any, "fix only what needs fixing") and
  the writing rules (format, a citation for every claim, no unread code). The agent reads through repo blocks or MCP and gives the
  body at the end.
- The current citation check runs on the final body. With unverified citations it asks once more. If some remain, the sentences
  are not dropped; the page is written with an `unverified` list in the front matter and left `broken`.
- Model choice reuses team settings (`--team`, default the Expert member of the project's default team).
- Pages written by people use the same file format. Saving in the dashboard editor sets `author.kind: human`, and `basis` is filled
  from the body's citations. An LLM rewrite does not overwrite a person's page, it only marks it `stale` (only `--force`
  overwrites).
- The first page set starts with the repository's top-level directories and the code directories below them (`apps/agent`,
  `apps/web-desktop`, `cli/llm-agent` and so on), 10 to 20 of them. `feature:` pages are made by people. (2026-10-18: the default
  set now also makes a feature page `feature-<document name>` per design document; A in `code-wiki-coverage.md`.)
- The wiki is not fixed automatically when a debate ends. Press "rewrite stale pages" in the dashboard or run CLI `--stale`. There
  is no scheduled automatic run (decision 3).

### Records

- The thread's `context_snapshot` gets `index: {"sha", "version", "files", "symbols"}` and `wiki: {"shown": [id], "states": {id: state}}`.
- `debate_started.data.context` gets `map_bytes`, `wiki_pages` and `wiki_stale`.
- The transcript's `retrievals[]` stays as is, and new ops use the same entry shape.

### Implementation notes (P3)

Implementation: `cli/llm-agent/code_wiki.py` (front matter, storage, staleness, generated layer, the body of the wiki op),
`wiki_build.py` (the writing job), `wiki_ops.py` (JSON commands for P4 to call), and the wiring in `repo_tools.py`, `repo_mcp.py`,
`llm_runner.py`, `debate_engine.py` and `guardian.py`. Decisions that differ from the design or were not in it.

**Storage and format**

- The location is as designed, `<GUARDIAN_DATA_DIR>/code-wiki/<project>/pages/<id>.md` (the project id is cleaned with the same rule
  as the code index cache). Page id `^[a-z0-9][a-z0-9-]{0,63}$`, body cap 256 KB (UTF-8 bytes), title one line of 200 characters,
  20 scope entries, 400 basis entries. Writes go to a temporary file in the same directory, then `fsync` and `os.replace`
  (atomic); files 0600, directories we create (`code-wiki`, `<project>`, `pages`) 0700. Reads open with `O_NOFOLLOW` (symbolic
  links refused) and check the file size cap and "front matter id = file name". Unreadable files appear in the list's `errors` as
  `{id, error}`, and the rest keep working.
- **The front matter is a restricted YAML read and written without PyYAML** (defined by the header comment of `code_wiki.py`). A line
  is `key: value`, where the value is a one-line JSON value (string, number, list, object) or a bare token of only
  `A-Za-z0-9_.@/+:-` (JSON if it reads as a number, `true`, `false` or `null`, otherwise a string). Only `basis:` is followed by
  indented lines `  "path": "blob id"`, a block mapping of string pairs. Whole-line `#` comments and blank lines are ignored;
  anything else (single-quoted strings, inline comments, duplicate keys, no closing `---`) is a `FrontMatterError`. Writing quotes
  every string as JSON so values contain no newlines, and lines are split on `"\n"` only (safe for characters such as ` `). Even
  if the body has a `---` line, the front matter ends at the first closing `---`.
- `prompt_version` (the writer prompt version `wiki_build.WIKI_PROMPT_VERSION`, an empty string for pages by people) and
  `unverified` (unverified citations left after the retry when writing) were added to the designed fields. `author` is
  `{kind: llm, provider, job}` (`job` is the thread id of the writing job, `wiki_<time>_<page id>`) or `{kind: human, user}`.

**Staleness** (`code_wiki.page_state(page, tools)`, computed per sha, not stored)

- The "new file" of `stale` is a readable file in scope that **was not in the tree at `written_sha`** and is not in `basis`. `basis`
  is "files read", which does not include every file of the scope (the agent reads only some), so plain "not in basis" would make a
  page stale from the start. So the tree at `written_sha` is read with `git ls-tree`, and only files missing then count. When that
  commit is not available (gc and so on), the new file check is skipped with the reason in `notes` (with
  `written_sha == current sha` there are no new files).
- A `basis` file that became unreadable or disappeared is `removed`; a different blob id is `changed`; both are `stale`.
- `broken` means (a) a path the body cites does not exist, (b) the symbol of `path#Symbol` is in none of the code index
  (`find_defs`), the language's declaration parser (`symbol_spans`) or the Go facts (`GoFactsProvider.peek()`, only if present now,
  no waiting), or (c) the `unverified` recorded at write time is not empty. Symbols of languages without a parser (`.md#Section`)
  cannot be checked and are not treated as broken.
- "A cited path does not exist" is found in the body like this. Readable paths in the tree count as citations only as full path
  tokens, like `cited_locations`. A token not in the tree is treated as a path only if its extension appears among repository files
  (and `basis`) (`api.example.com:443`, `v1.2:3`, `e.g.` are not paths). If that token is a strong citation (`:N`, `#Symbol`,
  `line N`), it is a broken citation; a bare path mention is broken only if it has a `/` and its first component is a known top-level
  directory (tree, `basis`, scope). A strong citation without `/` (`auth.go#Check`) is broken only when no file of that name exists
  anywhere in the tree (if it exists in another directory, it is treated as a loosely written but resolvable reference). URLs
  (`//…`) are skipped. Line number citations are not checked against the line count (if the file changed, the page is already
  `stale`).
- Result: `{state, sha, written_sha, changed, removed, new, broken: [{citation, reason}], unverified, notes}` (lists at most 100).
  `reason` is `path not found` or `symbol not found`.

**Generated layer and the wiki op**

- The generated layer is not stored and is built at the current sha on every read: `build_map(prefix, depth=3, cap=12KB)` per scope
  directory (the root scope `./` is everything), the file's `outline` for file scopes. A page result is, in order: the header
  `[wiki page <id> — navigation notes, not evidence; state: <state>]`, the staleness warning block (`WARNING:` lines, none if
  `fresh`), `--- Generated overview of … ---` with the map, and `--- Narrative: <title> (…) ---` with the narrative. The narrative
  is cut at 48 KB (`truncated`) with a marker.
- `wiki` op: with `page`, that page; otherwise a `query` list (case-insensitive substring of title, id, scope and body)
  `id  [state]  title  (scope)`, at most 20 lines, plus a `... N more` line. With neither, every page is listed the same way (at
  most 20). The search result header is `[wiki search "q": N of M pages — navigation notes, not evidence]`. Errors:
  `the code wiki is not available` (a `RepoTools` without a wiki directory), `page not found (search with query)`,
  `page id must match …`.
- **Not evidence**: `provided_paths`/`provided_spans` are empty for `wiki`, and `mentioned_in` ignores `wiki`. Even if a page
  narrative says `svc/auth.go:6`, that path does not even count as a mention.
- MCP: the tool `repo_wiki` (`page`, `query`) is appended to the tool list. The server takes `--wiki-dir <project wiki directory>`
  (the `wiki_dir` of the `McpTurn` spec, passed by `llm_runner` to the claude and codex server commands); without it `repo_wiki`
  answers `the code wiki is not available`.
- With `GUARDIAN_CODE_WIKI=0` (`false`/`off`/`no`) the engine gives `RepoTools` no wiki directory: no wiki section in the prompt,
- **Default off since 2026-10-18 (user decision after evaluation 2).** Agents never opened a listed page in 45 runs and the wiki changed neither correctness nor cost, so `wiki_enabled()` is false unless `GUARDIAN_CODE_WIKI` is `1`/`true`/`on`/`yes`. The wiki now serves people (dashboard Wiki tab, file view, citation chips); the writer is unchanged. Its pages (directory, group and feature pages) are planned as scope units and the coverage checklist of the whole-repository audit mode.
  the `wiki` op and `repo_wiki` are "not available", and the events' `wiki_pages` and `wiki_stale` are 0 (a switch for the P5
  evaluation comparison).

**First prompt, snapshot, events**

- The wiki section goes right after the map (or file list), before `=== End of repository retrieval ===`:
  `=== Code wiki (navigation notes) ===`, the guidance (the wiki is navigation, not evidence; do not cite it, cite code you read
  yourself; if the wiki differs from the code, the code is right; open with `{"op": "wiki", "page": "<id>"}` or
  `mcp__repo__repo_wiki` and search with `query`), the `fresh` pages as `id — title (scope)` lines (at most 40 lines; title cut at
  120 characters and scope at 160, joined into one line), `... N more fresh pages` beyond 40 lines, if there are stale pages
  `N more pages are stale or broken: a wiki search lists them with their state; …`, and `=== End of code wiki ===`. **With no
  `fresh` page the section is absent and the prompt is byte for byte the same as p2.6** (`test_code_wiki.WikiPromptTests` pins the
  sha256 computed at p2.6 for three inputs of `_retrieval_intro`, and checks that the engine's first prompt does not contain the
  word `wiki`). So agents of a project with only stale pages do not learn about the `wiki` op from the prompt (it is visible in the
  MCP tool list).
- `RETRIEVAL_PROMPT_VERSION = "2026-10-12.p3"`. If computing wiki states fails, the debate continues (warning only, no section).
- The thread's `context_snapshot.wiki = {"shown": [id], "states": {id: state}}` exists only when there is at least one page.
  `debate_started.data.context.wiki_pages` is the project's total page count, and `wiki_stale` the number not `fresh` at that sha
  (always present, 0 when none). Reflected in the event table and "Implementation notes (P3)" of
  `docs/design/debate-agent-retrieval.md`.

**Writing job** (`wiki_build.WikiBuilder`, manual runs only)

- Command: `python3 cli/llm-agent/guardian.py --code-wiki-build --project P [--page ID ... | --stale | --all] [--sha S] [--team T] [--force] [--language L] [--json] [--events-file F] [--result-file F]`.
  Instead of the designed `code-wiki build` subcommand it follows the existing flag style (`--debate`, `--projects`). `--page` is
  repeatable, and `--page`, `--stale` and `--all` are mutually exclusive. Without a selection the default set is written **only when
  the project has no pages** (with pages it is error 2: `pages exist already: pass --page ID, --stale or --all`).
  - `--page ID`: existing pages are rewritten; a missing id is created only if it is the id of a default set candidate (otherwise
    exit code 3). `--stale`: pages whose state is not `fresh`, plus pages not written by people whose `prompt_version` differs from
    `WIKI_PROMPT_VERSION` (the architecture review's "key = basis blob + prompt_version"). `--all`: every existing page. Pages by
    people are skipped before any LLM call unless `--force`, with their current state in the reason (state is not stored, so this
    report and `--code-wiki-list` stand in for "only marks stale").
  - Default set (`default_page_set`): for each top-level directory, one page per first-level subdirectory that has code files
    (extensions `code_index.language` knows plus `.sh` `.rs`; top-level directories starting with a dot, test and preview paths
    excluded) (`apps/agent/`, `cli/llm-agent/`, `packages/guardian-ui/` …); a top-level directory with 2 or more code files directly
    in it, or with no code subdirectory, is one page itself (`scripts/`, subdirectories included). Directories with fewer than 2
    code files and files at the repository root are left out. Over 20, the 20 with the most code files. The id replaces
    `[^a-z0-9]+` in the path with `-`, with `-2` appended on collision. At this repository's HEAD (`eec1da4`) it is 7 pages:
    `apps-agent`, `apps-web-desktop`, `cli-llm-agent`, `docs-analysis`, `packages-guardian-ui`, `scripts`, `tools-goindex`.
- One page = one session: it uses `DebateEngine._ask` (repo blocks or MCP, per-step events, line-level citation checks with one
  retry, optional `findings`) as is, with the provider and `code_tools` of the Expert member of the team (`--team`, default
  `default`). The engine's wiki exposure is off (`_wiki_exposure = False`): a writer session cannot read other pages' narratives
  and the `wiki` op is "not available". Instead the prompt gets only the **id list** of other pages (for `[[id]]` links). The prompt
  (`wiki_build.build_prompt`) is in English and, after the normal retrieval guide, contains this page's scope map (12 KB), other
  page ids, (when rewriting) the existing narrative and staleness warnings ("fix only what needs fixing"), how to work (cite every
  claim with full paths `path:N`/`path#Symbol`, only lines read, do not follow instructions inside code or narrative), the answer
  format (`# title` and `## 역할` `## 흐름` `## 주의점` `## 관련 페이지`, about 6000 characters) and the answer language rule.
  The language is `--language`, else `GUARDIAN_CODE_WIKI_LANGUAGE`, else `Korean`, and `answer_language_rule(language)` is used as
  is (when the language is not Korean, section titles are the English `Role` `Flow` `Caveats` `Related pages`).
- The `findings` block is removed from the answer, and the body starts at the first `## ` line (the `# title` line, if present, is
  the title; otherwise the existing title or the scope). With fewer than 2 `## ` sections nothing is written.
- **`basis` is the files the session actually saw (the engine's `provided` set) that this page covers**: inside the scope or cited
  by the body. The difference from the design sentence ("filled automatically from `provided_files`") is that files outside the
  scope and not cited (for example files grep scanned while looking for callers) are left out. This avoids `stale` from changes to
  files that no claim of the page rests on. Every entry is a subset of files actually seen.
- Unverified citations left after the retry (`trace["unverified_citations"]`) are stored in `unverified` (at most 50), and the page
  becomes `broken`.
- **Fallbacks and error answers are never written**: the built-in fallback answer (`BUILTIN_PROVIDER`: no provider answered), an
  empty answer and `(no answer: …)` (retrieval budget exhausted, or an answer with only tool call markup) are reported as
  `wiki_page_skipped` (`llm_fallback`/`no_answer`) and the existing page stays. An exception for one page (`error: …`) also skips
  only that page and continues. After the session, just before writing, the page is read again, and a page a person saved in the
  meantime is not overwritten (except with `--force`).
- **Concurrency**: each project takes `flock(LOCK_EX|LOCK_NB)` on `<wiki directory>/build.lock` (for the whole build). A second
  build exits immediately with code 4 (`another code wiki build is running for this project`). When a process dies the kernel
  releases the lock, so there is nothing to clean up. Pages are written one at a time, so a build runs one LLM session at a time.
- Events (`--events-file`, the same `EventWriter` as debates): `wiki_build_started`, per page `wiki_page_written {page, state,
  basis_files, citations, unverified}` or `wiki_page_skipped {page, reason}`, `wiki_build_finished {written, skipped, failed}`.
  During a session `agent_started`, `retrieval_*`, `citation_check`, `evidence_check` and `agent_finished` appear in the same shape
  as in debates (the thread id per page is `wiki_<time>_<page id>`; sessions are kept in `--sessions` and can be opened with
  `--attach`). Failures before the start (project, team or sha errors, selection errors, the lock) produce no events.
- Result (`--result-file`, `--json`): `{project, sha, team, language, force, pages: [id], written: [{page, state, basis_files,
  citations, unverified}], skipped: [{page, reason}], failed}` (`--json` prepends `"ok": true`). Exit codes: 0 ran, 1 nothing
  written and failures (`failed` > 0), 2 invalid input (project, team, sha, selection, `GUARDIAN_RETRIEVAL=0`), 3 unknown page id,
  4 already building. With `--json`, errors before the start also print `{"ok": false, "error": …}` to stdout.
- **The writer session's role and first prompt** (corrected after a hub failure, `WIKI_PROMPT_VERSION` 2026-10-12.wiki2):
  `llm_runner._build_prompt` put the role line "expert Linux System & SSH Security Engineer" in front of every session call and
  again on every resumed turn. Writer sessions replace that line with `wiki_build.WRITER_SYSTEM` (a codebase documentation writer,
  in English) (`run_session_prompt(system=...)`) and send resumed turns without a preamble. The team Expert's persona prompt is not
  used; only the provider order and `code_tools` are. The protocol guide in the first prompt uses `root_summary` instead of the full
  repository map (the page scope map goes into the task separately): the hub input of 50,624 bytes becomes about 19 KB for this
  repository's `cli-llm-agent`. `--code-wiki-build ... --dry-run [--json]` shows the composition of each page's first prompt
  (protocol, guide, scope_map, task, total bytes) without calling an LLM.
- **Cause of the session context loss** (confirmed on the hub, `WIKI_PROMPT_VERSION` 2026-10-12.wiki3): the 14 repo blocks of the
  first answer, 70 requests together, came back as one 722 KB message, and the claude CLI compacted the session automatically
  (`compact_boundary` in the session record, "Conversation compacted"). After compaction the model lost the task and what it had
  read. User decision (2026-10-12): no step cap for wiki writing either. Instead `WRITER_SYSTEM` got the advice "read in small
  batches, outline large files first", and the `retrieval_warning` threshold shared with debates was lowered from 2 MiB to 256 KiB
  so such steps show up as events.
  - **Changed (2026-10-13, jev decision):** the same incident happened again on the feature page
    `feature-debate-code-index-wiki` (first step 3.1 MB; after compaction the session lost the task and was skipped with an answer
    without sections). So writer sessions alone get a **per-step served bytes cap**: during a build `wiki_build.writer_limits()` sets
    `GUARDIAN_RETRIEVAL_STEP_BYTES` to `GUARDIAN_CODE_WIKI_STEP_BYTES` (default 512 KiB, 0 turns it off; an explicitly set smaller
    value stays) and restores it afterwards. Overflowing requests come back as `deferred` through the existing mechanism, to be sent
    again in the next step (blocks and MCP alike). Total reading stays unlimited, and debate sessions are not affected. Introducing
    the cap and its value (512 KiB, confidence 0.75) were decided with jev as the user instructed.
  - **Changed (2026-10-16, `WIKI_PROMPT_VERSION` 2026-10-16.wiki6):** the root cause was that the role and task were only in the
    first user message. In claude sessions, `WRITER_SYSTEM`, the retrieval protocol, the root summary and the language rule are now
    in the system prompt (`--append-system-prompt`, the same text on every call) and survive compaction. The first user message is
    only the page task (`build_prompt`). The task itself gets summarized on compaction, but in page writing the role, protocol,
    language and citation rules remain, so the model can read again and answer. Other providers' first messages are unchanged.
    Details in "System prompt and compaction" of `debate-agent-retrieval.md`.

**Read and edit commands** (`wiki_ops.py`, called by P4 through `guardian.py`). All print JSON to stdout (`--json` is only accepted)
and use the same exit codes as `config_ops` (0 success, 2 invalid input or a project/sha that cannot be opened, 3 unknown page,
4 build conflict). States are relative to `--sha S` (default the HEAD of the project's workspace clone). The error body is
`{"ok": false, "error": "…"}`.

| Command | Input | Output |
|---|---|---|
| `--code-wiki-list --project P [--sha S]` | | `{"ok", "project", "sha", "pages": [{"id", "title", "scope": [..], "author": {..}, "updated", "state"}], "errors": [{"id", "error"}]}` |
| `--code-wiki-show ID --project P [--sha S]` | | `{"ok", "project", "sha", "page": {"id", "title", "scope", "author", "updated", "written_sha", "prompt_version", "basis": {path: blob}, "unverified": [..]}, "narrative": "<body>", "state", "staleness": {"state", "sha", "written_sha", "changed": [..], "removed": [..], "new": [..], "broken": [{"citation", "reason"}], "unverified": [..], "notes": [..]}, "generated": "<scope map text>"}` (text for people when called without `--json`) |
| `--code-wiki-save --project P [--sha S] [--actor U]` | stdin JSON `{"id", "title", "scope": [..], "body"}` (other keys refused) | `{"ok", "project", "sha", "page": {…same meta as show}, "state", "staleness": {…}}`. The engine rewrites the front matter: `author {kind: human, user: --actor or the OS user}`, `written_sha` = the checked sha, `basis` = current blobs of the readable files the body cites strongly, `prompt_version` "", `unverified` []. Scope entries must be readable files or directories (directories are stored with a trailing `/`, `.` and `./` mean the whole repository); input up to about 320 KB, body up to 256 KB |
| `--code-wiki-delete ID --project P` | | `{"ok", "project", "deleted": ID}`, exit code 3 if missing |
| `--code-wiki-build …` | "Writing job" above | The result JSON above |

(2026-10-18: list entries and the `page` meta gained `kind`, the `page` meta also `docs`, and `--code-wiki-build` takes `--kind`,
`--regroup` and `--dry-run`. The file view commands `--code-file-list`, `--code-file-show` and `--code-file-read` live in
`file_view.py`. See `code-wiki-coverage.md`.)

**When P4 starts writing jobs**: a writing job is not a debate job. Do not use the Go hub's debate concurrency limit
(`MaxRunning`); give the `code_wiki` job kind a separate slot (1 in total). Per-project deduplication is already done by Python's
`build.lock` (exit code 4 → HTTP 409), so Go only manages the slot. Like a debate, create a job directory and run with
`--events-file <job>/events.jsonl --result-file <job>/result.json --json`; a page a person saves from the dashboard (`PUT`) replaces
the same file atomically, so it does not break even when it overlaps a build (the build re-checks for a person's page just before
writing). The event consumer (Agents monitor) skips unknown types, so `wiki_*` is fine, but the job has no `debate_started`, so
values the monitor shows in the job header (project, page list) must be read from `wiki_build_started`.

**Known limits and open issues**

- `fresh` only means "the basis blobs are unchanged and the cited paths and symbols exist", not that the narrative is right.
  Changes in files outside basis are not seen (only new files in scope count as `stale`), and the lines a line number citation
  points at are not checked (if that file changed, the page is already `stale`).
- `path#Symbol` checks have the limits of the layout parser (P1 implementation notes). Declarations the parser misses (TS class
  members and so on) can make a page `broken` with `symbol not found`.
- Rewriting a `fresh` LLM page with `--page`, without a person editing it, always calls the LLM (skipping on a cache hit exists
  only for `--stale`).
- The LLM cost and time of writer sessions are unlimited like debates (only the `GUARDIAN_RETRIEVAL_*` budget if set). Running the
  20-page default set at once means 20 sessions, so the first run is better done a few pages at a time with `--page`.
- At the first deploy, the real provider answer format (section title compliance, citation check pass rate) must be checked on the
  hub with `--code-wiki-build --page cli-llm-agent`. Tests at this stage use only a fake runner.

**Tests**

- `test_code_wiki.py` (about 80): front matter (round trip, bare tokens and comments, invalid input, field validation), storage
  (permissions, atomic replace and keeping the original on failure, id escape attempts, symbolic links, size and id mismatch, the
  lock), staleness (fresh, blob change, deletion, new files versus existing and out-of-scope files, a missing written commit, a
  moved symbol, unverified citations, Python methods, languages without a parser, path citation rules, Go facts), the wiki op
  (header, generated layer, warning block, search, caps, errors, no file, scope or mention, MCP tool, process and command
  arguments), the first prompt (no wiki = same bytes as p2.6, only stale pages, list and guidance, 40-line cap, switch, wiki error
  isolation), **decoy check 2** (below), the writing job (success, events, prompt content, wiki exposure off, unverified citations
  left, resolved by the retry, basis rule, fallback, malformed and empty answers, a person's page, a person saving during the
  session, the rewrite prompt for stale pages, the lock, a vanished scope, invalid input, an old commit), CLI (save, list, show,
  delete, state per sha, exit codes for invalid input, build and its exit codes). `test_db_backup.py` got a test that `code-wiki/`
  stays in the tarball.
- **Decoy check 2 (deterministic)**: build a repository where a page says "authentication happens in `svc/auth.go#Check`
  (`svc/auth.go:6`)" but `Check` has moved to `svc/guard.go` in the code. (1) The page is `broken`
  (`svc/auth.go#Check (symbol not found)`), not listed in the first prompt, and `wiki_pages` and `wiki_stale` are 1 and 1.
  (2) When the agent opens the page with the `wiki` op, the result shows `state: broken` and the list of broken citations. (3) A
  final answer that cites `svc/auth.go:6` after seeing only the wiki is caught with `citation_check.unverified == ["svc/auth.go"]`
  and asked once more; the transcript's wiki entry has `paths` `[]` and the thread snapshot has no `provided_files`/`provided_spans`.
  (4) The same answer passes after `svc/auth.go` was `read`.
- Existing tests whose expectations changed on purpose: `test_evidence.py` and `test_go_facts.py` (prompt version
  `2026-10-12.p3`), `test_llm_runner.py` and `test_repo_mcp.py` (`repo_wiki` at the end of the tool list).
- Short citations (P2.7): citations a writer answer gives as `guardian.py:755` or `(line 510)` are resolved to one file with the same
  check as debates and verified by the P2.5 line rules (rules in "Short citations (P2.7)" of `debate-agent-retrieval.md`). Resolved
  files go into `wiki_page_written.citations`; unresolved ones stay in the page's `unverified` as strings. The writer prompt was
  changed to allow only full paths, and `WIKI_PROMPT_VERSION` is `2026-10-13.wiki4`. The staleness check's `broken_citations`
  re-checks only full path citations.

## Go API and dashboard

| Method and path | Content |
|---|---|
| `GET /api/projects/{id}/wiki` | Page list (`id, title, scope, author, updated, state`). `state` is relative to the HEAD of the project's workspace clone |
| `GET /api/projects/{id}/wiki/{page}` | One page (narrative, generated layer, staleness details) |
| `PUT /api/projects/{id}/wiki/{page}` | Saves a person's edit. Body cap 256 KB; the engine rewrites the front matter |
| `POST /api/projects/{id}/wiki/build` | Starts a `code_wiki` job (`{"pages": [...]}` or `{"stale": true}`) |

- Authentication is the global `withSecurity(withAuth(mux))` in `main.go` (`h.guard` only checks that the CLI exists). Page ids are
  restricted to `^[a-z0-9][a-z0-9-]{0,63}$` (so they cannot leave the directory).
- Dashboard: a `Wiki` tab on the project screen with a list (state chips), a page view (narrative and generated layer apart),
  editing and "rewrite stale". Writing jobs appear in the Agents monitor like debates. (2026-10-18: the tab lives in the section
  switch of the Debates page; see "Implementation notes (P4)" below.)
- The file view routes `GET /api/projects/{id}/files`, `.../files/show?path=` and `.../files/lines?path=&start=&end=` are in
  `apps/agent/file_handler.go` (C in `code-wiki-coverage.md`).

### Implementation notes (P4)

Implementation: Go `apps/agent/wiki_handler.go` (routes, validation, starting writing jobs), `debate_handler.go` (job `kind`,
concurrency counting, completion handling), dashboard `apps/web-desktop/src/pages/wiki/` (`WikiTab`, `WikiPageView`,
`WikiEditDialog`), `lib/wiki.ts`, `lib/agentMonitor.ts` and `pages/agents/*` (job monitor), `api.ts` and `types.ts`. Decisions that
differ from the design table or were not in it.

**Routes** (all behind `h.guard`; project id the existing `idPattern`, page id `^[a-z0-9][a-z0-9-]{0,63}$`, `sha` `contextRefRe` and
not starting with `-`. All called with `--json` and single `--flag=value` argv entries. 2026-10-18: authentication is done by the
global `withAuth`, and `h.guard` only checks that the CLI exists)

| Route | CLI | Notes |
|---|---|---|
| `GET /api/projects/{id}/wiki[?sha=]` | `--code-wiki-list --project=ID [--sha=S]` | CLI time limit 120 s (the first call builds the code index) |
| `GET /api/projects/{id}/wiki/{page}[?sha=]` | `--code-wiki-show=PAGE --project=ID` | The response is the CLI JSON as is (`narrative`, `generated`, `staleness`) |
| `PUT /api/projects/{id}/wiki/{page}` | `--code-wiki-save --project=ID --actor=<actorFor>` | Body `{title, scope, body[, id]}`. Request cap 320 KB (the CLI input cap, 413 above it), body text 256 KB (UTF-8 bytes, 413 above it). An `id` must equal the path value (otherwise 400); without it the engine fills it in. A new id creates the page |
| `DELETE /api/projects/{id}/wiki/{page}` | `--code-wiki-delete=PAGE --project=ID` | |
| `POST /api/projects/{id}/wiki/build` | `--code-wiki-build --project=ID --json [--page=P ...\|--stale\|--all] [--force]` | "Writing job" below |

- Exit code mapping is the same as the existing `respondConfig`: 0 → 200 (CLI JSON), 2 → 400, 3 → 404, 4 → 409, anything else → 500.
  503 without the CLI (`guard`). The actor (`--actor`) is `actorFor(RemoteAddr)` (`dashboard@<host>`) like other decision routes.
  The hub has no user session name, so this is used instead of a "session user".
- **`GUARDIAN_UI_ACTIONS=off` does not apply to the wiki routes.** That switch blocks decision routes such as execute, reject,
  close, publish and workspace reset; it does not block saving personas, teams and projects or starting debates. Wiki edits and
  writing jobs were judged to be of the latter kind (pinned by a test).
- Route collisions: `POST …/wiki/build` differs in method from `PUT/GET/DELETE …/wiki/{page}`, so they do not overlap. A page with
  the id `build` can still be handled with `GET/PUT/DELETE`.

**Writing job** (`code_wiki`)

- Request `{"pages": [id…]}` | `{"stale": true}` | `{"all": true}` with an optional `"force"`. Two or more together are 400; nothing,
  or an empty body, means the CLI's default page set (allowed only for a project without pages; with pages the CLI exits 2 → 400).
  Unknown fields are 400. `pages` holds at most 50. (2026-10-18: an optional field `kind` (`dir`|`overview`|`group`|`feature`) was
  added and is passed as `--kind`; it cannot be combined with `pages`.)
- It uses the same `<GUARDIAN_DATA_DIR>/debate-jobs/<dbt_…>/` layout as debates (`meta.json`, `output.log`, `events.jsonl`,
  `result.json`) and `launch`. `meta.json` has `kind: "code_wiki"`, `project` and `task` (an English description such as
  `code wiki build: stale pages`); `team`, `thread_id` and `rounds` are empty. So `GET /api/debates/{id}`, `…/events` (polling and
  follow), the list and cancellation work as they are.
- **Own slot**: running `code_wiki` jobs do not count toward `MaxRunning` (the debate limit), and debates are not affected by
  writing jobs. There is 1 slot in total, changed with `GUARDIAN_MAX_WIKI_BUILDS`. If the same project already has a running
  writing job it is 409; if the slots are full it is 429 (the same code as the debate limit). The final per-project guard is the
  CLI's `build.lock`.
- **The CLI's immediate refusals are returned synchronously**: the job is asynchronous, but exit codes 2, 3 and 4 (invalid input,
  unknown page, already building) come right after the start. The route waits `WikiStartGrace` (default 3 s) for the process to end
  with one of those codes, then answers 400, 404 or 409 with the CLI's `{"ok": false, "error"}` text and deletes the job directory
  (failed jobs do not stay in the list). If it does not end in that time, the answer is 202 with the job summary. If a slow Python
  start makes the refusal late, the job stays in the list as `failed` (`code wiki build exited with code 4: …`).
- It uses the same time limit (`GUARDIAN_DEBATE_TIMEOUT`, default 1 hour) and the same cancellation (`DELETE /api/debates/{id}` →
  SIGKILL of the process group, status `cancelled`). Pages already written stay. On an agent restart it becomes `interrupted`
  (error text "agent restarted while the code wiki build was running").
- **`GET /api/debates` behaviour**: writing jobs go into the same list with `kind: "code_wiki"`. The agent monitor is the debate
  console's list and detail (`#/debates`), so leaving them out of the list would hide them from the monitor too. The debate console
  shows them with a "Code wiki build" chip, and the overview screen's "Recent debates" leaves them out. The list cap (50) is shared.

**Dashboard location and screens**

- Location: Wiki in the section switch at the top of the Debates page (`Debates | Wiki`). Paths are `#/debates/wiki` (list) and
  `#/debates/wiki/<page id>` (open a page). The project uses the same project selector as Debates. There is no separate "project
  screen", so it lives here where the project selector is. The 5-second list polling is done by `DebatesPage` regardless of the
  section.
- Wiki list (`WikiTab`): the top card has project selection, `Reload`, `New page` (a new page by hand), `Rewrite stale (n)`,
  `Rewrite all` (confirmation dialog), and `Write the first pages` (default set) when there are no pages. Below on the left is the
  page list (title, state chip FRESH/STALE/BROKEN, id, author `claude (LLM)`/`alice (person)`, update time) with the checked commit
  and unreadable page errors. On the right is the open page, or "Select a page to read it." when none. While a writing job of this
  project runs, the build buttons are disabled and a link to the monitor is shown. When that job ends the list is reloaded.
- Page view (`WikiPageView`): a header (state chip, title, id, author, update, written commit and checked commit, scope chips) with
  `Rewrite`, `Edit` and `Delete`. When stale, a warning box lists changed, removed and new files, broken citations (with reasons),
  unverified citations and notes. Below that the **narrative** (preformatted text with a caption naming the author) and the
  **generated layer** ("built from the code when opened, not stored", a collapsed box) are separate. The dashboard has no Markdown
  renderer, so the narrative is shown as source (no dependency added). (2026-10-18: since 2026-10-13 the narrative is rendered as
  Markdown with citations as chips that open the file view; `WikiNarrative.tsx`, `components/MarkdownDoc.tsx`.) `Delete` uses
  `ConfirmModal`; `Rewrite` on a person's page sends `force: true` after confirmation.
- Edit (`WikiEditDialog`): id (new pages only), title, scope (one per line), a body text area with a size display. The client checks
  id format, title, empty body and 256 KB first, and hub errors are shown inside the dialog. Saving makes the author a person, and
  basis is filled from the body's strong citations (P3).
- Narrow screens: like Debates, either the list or the detail (`#/debates/wiki/<id>` shows the detail and "← Back to pages").
- Agent monitor (`#/debates/<job id>`): a "Code wiki build" chip on list rows. A writing job shows the monitor even after it ends,
  instead of the result screen (debate verdict). The header has the chip and an "Open the wiki" link, the cancel dialog uses writing
  job wording, and the result card shows "Pages (n/m)" (per page FRESH/STALE/BROKEN, SKIPPED, not yet done). The single lane is
  the writer session's Expert. The timeline shows `wiki_build_started` ("Code wiki build started"), `wiki_page_written` ("Page
  written", state chip, files read and citation count, unverified citation warning), `wiki_page_skipped` (warning, existing page
  kept), `wiki_build_finished` ("N written"), and also, as in debates, `retrieval_warning` (warning box), `evidence_check` (one line
  when consistent, a warning and list on mismatch), `compiler_index` (state chip and package, definition and reference counts) and
  `compiler_index_warning`. The job status is set from `wiki_build_finished` (`failed` if no page was written and there were
  failures, otherwise `completed`; skips alone are not an error).

**Known limits**

- The screen copy is English like the rest of the dashboard (the dashboard has no Korean copy). Labels are in `WIKI_JOB_LABEL` of
  `lib/wiki.ts` and in each component. (2026-10-18: dashboard copy is now Korean by default with English as an option. Wiki copy
  lives in `src/i18n/ko/wiki.ts` and `src/i18n/en/wiki.ts`, and `WIKI_JOB_LABEL` is `tr.wiki.jobLabel`. So the English labels above
  are those of the English screen. See `dashboard-i18n.md`.)
- The list and page view do not choose a base sha (the HEAD of the project's workspace clone). The `sha` query exists only in the
  API; the screens do not use it.
- During `WikiStartGrace`, which waits for a writing job's refusal, the request stays open (default 3 s). A second request for the
  same project in that window gets 409 right away from the slot check.
- No screen check on the real hub yet (provider answer format, how long pages look). Tests use only a fake CLI and a mock API.

## Phases

| Phase | Scope | Done when |
|---|---|---|
| P1 | `code_index.py` (three language parsers, blob cache), `RepoTools` blob ids, TS/TSX support in `outline` and `path#Symbol`, `defs`/`refs` ops, MCP tools, prompt guidance | Parser unit tests (regular and irregular examples per language), existing `test_code_select.py` and `test_repo_tools.py` pass, decoy check 1 |
| P2 | Repository map, first prompt replacement, `map` op, `GUARDIAN_REPO_MAP` | Map determinism test (same sha → same bytes), 32 KB reduction test, evaluation 1 |
| P2.1 | Robustness: index coverage notices for `refs`/`defs`/map (W2), literal masking in cache signatures and `INDEX_VERSION` 2 (W4), batch reads for `refs` (W5), secret filter on the whole path (W4b), `retrieval_warning` event, map ranking (distinct files, noise paths excluded), cache cleanup and backup exclusion | Tests for every end notice case, no literals in cache `signature`, `config/secrets/x.yaml` unreadable, map determinism at the same sha |
| P2.5 | Evidence: line-range citation checks (`GUARDIAN_CITATION_SLACK`, `provided_spans`, MCP log `spans`), optional `findings` block comparison and the `evidence_check` event, findings reports per provider, prompt version `2026-10-12.p2.5` | Catches the same-name `Handle` decoy; shown lines, slack, range and symbol rules; file-level compatibility for threads before P2.5; fake citations mismatch; the judge format is not disturbed by the findings block |
| P2.6 | Go compiler-assisted index: `tools/goindex`, temporary worktree and cleanup, facts JSON, type-checked `refs`/`defs`, `callers`/`callees` ops and MCP tools, map ranking | "Verification (P2.6)" above, 0 worktrees and temporary directories after cleanup |
| P3 | Wiki storage format, staleness, `wiki` op, `code_wiki` job | Staleness tests (blob change, path deletion, symbol deletion, new file), citation check wiring tests, decoy check 2 |
| P4 | Go API, dashboard Wiki tab | API tests, web build (implemented, "Implementation notes (P4)" above) |
| P5 | Evaluation 2 and default decisions | Done (2026-10-17): the map and wiki exposure both stay on by default. `docs/analysis/2026-10-13-retrieval-eval2/results.md` |

## Verification

### Decoy checks

- **Decoy 1 (relation lookup):** put the same name `Handle` in different Go and TS files of a test repository, with only one real
  call relation. When asked "where is X called", check that the agent confirms `refs` results with `read` and does not cite the
  same-name one as a caller.
- **Decoy 2 (stale wiki):** create a state where a wiki page says "authentication happens in `auth.go#Check`" but the code has
  moved elsewhere (`broken`). Check that the agent does not cite the wiki sentence as evidence and looks for the code again, and
  that the citation check catches a `path:N` written from the wiki alone.

### Evaluation

Run 12 fixed questions at the same sha and compare. Three configurations with the same team: baseline (`GUARDIAN_REPO_MAP=0`, no
wiki), P1+P2, P1+P2+P3.

- Question types: 3 API and handler lookups, 3 call path traces, 3 TS/TSX screen behaviours, 3 bug fixes (including judge actions)
- Metrics (all read from existing records):
  - steps and bytes in `debate_finished.data.retrieval`
  - the `unverified` count of `citation_check`
  - `action_repair` occurrences and final `INVALID` count
  - jev verdict
  - answer accuracy (an answer location set by a person per question)
- Decision rule: if bytes drop and accuracy and jev stay equal or improve, the feature is turned on by default. If accuracy gets
  worse, the default stays off.

## Decisions (2026-10-12, all default options)

1. **Wiki storage location: the hub data directory** (`<GUARDIAN_DATA_DIR>/code-wiki/<project>/`). It does not touch the target
   repository, and no per-project write permission is needed.
   The alternative considered was committing to `docs/wiki/` in the target repository. That would give sha pinning and git review,
   but writes would have to go through the publish flow and files would appear in the user's project.
2. **TS/TSX parser: keep the standard library principle** (layout parser). Missing code not formatted by prettier and nested
   declarations (functions inside components) is accepted. No optional dependency such as tree-sitter.
3. **Wiki updates: manual only.** Rewrites only through the dashboard button or CLI `--stale`. No scheduled automatic updates (LLM
   cost).
4. **Map cap: 32 KB by default** (`GUARDIAN_REPO_MAP_BYTES`). It is a prompt size rule, separate from the unlimited retrieval
   budget. The full map size is measured in P2.
5. **Personal wiki (`llm-wikis`): separate.** The `wiki` MCP on this Mac and the hub wiki are not connected, with no export or sync.
6. **P2.6: compiler-assisted index, Go first** (added 2026-10-12). The debate engine keeps the standard-library-only principle;
   only the fact extraction is done by the Go toolchain already on the hub and a standard library helper (`goindex`). Source stays
   in git, and the worktree and artifacts used for extraction are deleted every time. TS is deferred to a separate decision because
   of the install script risk.

## What this design does not do

- Embeddings and vector search (a semantic search op `search`) stay a separate experiment. If they come in, they follow the same rule
  as the layer 2 navigation aids that only give candidate locations (not evidence).
- No LSP server. Go gets exact references from one-off type checking in P2.6; `refs` for other languages is name-based and says so
  in the result.
- Wiki narratives are not used for the judge's action checks (`ContextPack.saw`). The judge builds patches only from source it read,
  as today.
