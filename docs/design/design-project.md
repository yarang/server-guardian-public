# Design, document and image artifact project (`kind: "design"`): review

English | [한국어](design-project.ko.md)

Status: review result (2026-10-17). Decisions 1-7 were confirmed with the recommended defaults and P1 is implemented (not deployed; see "Implementation notes (P1)" below). Decisions 8, 10, 11 and 12 were settled on 2026-10-18; decision 9 was reopened the same day because its premise was wrong (see "Decision record"). P2 and decision 12 steps 0-1 are implemented (not deployed; see "Implementation notes (P2)"). P3 is implemented (not deployed; see "Implementation notes (P3)").
Method: a review team of five (engine fit, validation and quality, storage/viewing/download, security and counter-arguments, capability survey) each read the code and documents and filed a report.
This document is the synthesis. Items whose source is a web document are marked "unverified"; some code facts (`mcp_client.py:148`, `--tools ""`,
the absence of `ops_mcp.py`, the scope of the CSP) were re-checked directly by the synthesizer.

## Question and conclusion

Question: can the latest Claude prompting techniques turn this CLI from a coding-only tool into a project type that produces design, document and image
artifacts? How does the user view and download the results?

**Conclusion: possible under conditions. The answer is split by scope.**

| Artifact | Verdict | Condition |
|---|---|---|
| Markdown documents, design docs | Possible | A `code` project can already write them to `docs/` with `apply_patch`. The gain of a new kind is projects without a repository and versioning |
| Mermaid, SVG diagrams | Possible | The engine extracts them from the response text. The dashboard already has a render path (`MarkdownDoc.tsx`) |
| Single-file HTML/CSS mockups | Conditional | Security isolation (see "Security") comes first. Viewed in the user's browser, no server-side rendering |
| PNG, PDF | Conditional, later | Converting in the **user's browser** (SVG→canvas→PNG, print→PDF) without a server renderer is **the synthesizer's proposal and was not checked by the review team (unverified)**. Server-side rendering needs a decision |
| DOCX, PPTX, XLSX | Conditional, later | With the standard library alone, OOXML has to be written by hand; otherwise an external library or Skill is needed (decision required) |
| Direct raster image generation | **Not possible with Claude** | Claude understands images but does not generate them (official FAQ, capability survey). codex `image_generation` rests on secondary sources only, is unverified, and is switched off in this repository. Images have to be "SVG drawn as code" or an external image model |

What "prompting techniques alone" can do ends with the text family (Markdown, Mermaid, SVG, HTML). Beyond that, **runtime environment decisions**
(renderer, library, external model) are needed, and those touch the standing decisions in CLAUDE.md (standard library only, no queue or workers, one hub).

## What is reused and what blocks in the current structure

- Reused: Expert, Critic panel and Judge structure, events (`events.jsonl`), sessions, teams and personas (`configs/personas/*.json`), the jev gate
  (code lines are omitted when `code_context=None`). The debate engine itself is not coupled to code.
- Blocker 1, git assumptions: workspace lock, base SHA, publish, sync, wiki, reset. `kind: "ops"` already created projects without `repo` and `workspace`,
  so `_parse_ops` (`project_registry.py:183-225`) is the template.
- Blocker 2, limits of the precedent: the ops branch is the boolean `is_ops`. `(name in SERVER_TOOLS) == (self.kind == "ops")` at `mcp_client.py:148` is
  only correct with two kinds; adding `design` would leave the code tools (`apply_patch` and others) open. It has to become a `kind → tool set` table (`KIND_TOOLS`).
  `Project.is_ops` also doubles as "no workspace", so that meaning is split off as `has_workspace`.
- Blocker 3, the rest of ops is not implemented: `ops_mcp.py` and `OPS_TOOLS` exist only in the design document (the code has the single `SERVER_TOOLS`). "Follow the ops precedent" only goes as far as P1 (kind validation).
- Blocker 4, agents cannot create files: claude runs with `--tools ""` (`llm_runner.py:357`, `:545`). Agents cannot see render results either.
  Artifacts arrive as response text and the engine writes them. This structure is a security advantage, so it is not changed in the first phase.
- Blocker 5, validation: the citation check in `evidence.py` only proves "this quote is on this line". It works as is for factual claims about code, but cannot
  prove that a design is good. The jev gate sees only the proposal, critique and action summary, within 4,500 characters. In evaluation 1 the jev PASSED count of the same 12
  questions moved from 7 to 10, so it is not used as a quality metric.

## Decisions (user confirmation needed, recommended default)

| # | Item | Recommended default | Alternatives |
|---|---|---|---|
| 1 | Scope (first phase) | **Markdown, Mermaid, SVG**, reusing the existing render paths | Up to HTML/PDF/PNG (isolation design first), up to DOCX/PPTX |
| 2 | Kind structure | A single `kind: "design"`, types as an allow list in `artifacts.types` | One kind per type; `offers`-style branching doubles |
| 3 | Where to render | **The user's browser**. The hub does not render | Install rsvg/Chromium on the hub (resource and SSRF risk, needs `systemd-run` limits), a separate host (changes the "one hub" decision) |
| 4 | Images | First phase: **SVG drawn as code** only. PNG via browser conversion is tried in P4 (synthesizer's proposal, unverified: whether the conversion works and the PDF constraint below are checked in P4) | Connect an external image model (credentials, cost, terms), enable codex `image_generation` |
| 5 | Writing and approval | New tool `write_artifact`, **manual approval by default**. Not added to `FILE_TOOLS` or `WRITE_TOOLS` | Auto-apply when jev passes (`GUARDIAN_AUTO_APPLY_ARTIFACTS`, off by default, reconsidered after evaluation) |
| 6 | Storage | `<DATA>/design-artifacts/<project>/<thread>/vN/`, immutable versions, `manifest.json`, **included in the backup** | Store in a work clone (git) and publish; images bloat the repository |
| 7 | Agent write permission | **None** (keep `--tools ""`). The engine extracts from the response and writes | Allow temp-directory writes in an artifact mode (`acceptEdits`); needs extension, size and path checks |
| 8 | Runner | Artifact generation is **claude only** | Allow codex/agy. Both have a known hole that lets them read `agent.env` and `llm.env` (stated in `env_policy.py`) |
| 9 | When to start | **After re-running evaluation 2** (once the cause of the claude CLI failures is understood) | Start P1 now. P1 does not use retrieval and is independent of evaluation 2, but fallback reliability is shared |
| 10 | Location | Add to server-guardian as a `kind` | **0. Build nothing**: documents and Mermaid can already be written to `docs/` by a `code` project through `apply_patch` (`JUDGE_DIAGRAM_HINT` allows mermaid). The gains of a new kind are repository-less projects, versioning and a download screen; if those are not needed, it is the cheapest option. Others: a separate project, or the Claude app's Artifacts/Docs (isolation and rendering already solved) |
| 11 | Version retention | Only the latest version of a thread is kept without a time limit; older versions are deleted after `artifacts.retention_days` (default 30, set per project, 0 = keep). No quota; one per-file safety check | Keep every version forever (cheap for text, but binaries in P4 change that); keep approved versions forever |
| 12 | Thread token cleanup | A cleanup step for earlier rounds, deterministic first (program), an optional light LLM digest only if still too large | LLM digest first (costs usage and weakens cache stability); no cleanup |

### Decision record (2026-10-18)

Decisions 8-10 were put to JEV (`jev_ask`, model jev-1.13.0) with a written summary of the context; JEV scores a framing the author wrote, so it is a signal, not a check of the facts.

| # | Result | Probability | Grade | Status |
|---|---|---|---|---|
| 8 | claude only (the audit mode restricts its Experts for the same `agent.env`/`llm.env` reason) | 1.00 | act | **Settled** |
| 9 | ~~P2 starts after evaluation 2 is re-run~~ (asked with a wrong premise, see the correction below) | 0.95 | act | **Reopened** |
| 10 | Keep `kind: "design"` in server-guardian (alternatives: freeze at P1 and use `code` projects 0.09, Claude app Artifacts 0.08) | 0.81 | review | **Settled**: the user confirmed option A after the review-grade result |

Decisions 11 and 12 were settled by the user on 2026-10-18 and were not put to JEV.

| # | Result | Status |
|---|---|---|
| 11 | Versions that are not the latest of their thread are deleted after 30 days, or after the project's `artifacts.retention_days`. The latest version is never deleted. A deleted version keeps its `manifest.json` entry (sha256, size, deletion date) as a tombstone, so an older `[artifact:name@v3 §heading]` citation reads "expired", not "not found". No size quota. One safety check stays in L1: a text file above 2 MiB fails validation and the reason shows on the approval screen. The prune runs in an existing nightly job, not a new timer | **Settled** (user). The retention gate of decision 9 is met |
| 12 | The thread gets a cleanup step against wasted tokens. Order: (0) measure, (1) deterministic compaction, (2) optional light-LLM digest, off by default | **Settled** (user). Engine-wide, not design-only; implemented as its own task |

Decision 12 in detail (code facts checked on 2026-10-18):

- `_format_history` (`debate_engine.py:730`) joins every earlier round verbatim. It is sent only to agents that start a fresh CLI session (`_compose`, `new_session`), that is when a thread is continued and a session is lost or a fallback changes the provider. Resumed sessions already hold the text, so the cleanup only pays off in those cases.
- Retrieval results are about 90-105 KB per debate (evaluation 2 `metrics.jsonl`, about 20 steps) and stay in the session. The cleanup does not touch them.
- Nothing measures tokens today: `metrics.jsonl` has bytes only, and `prompt_bytes` of the first turn is an event. Step 0 adds `history_bytes` per turn and the thresholds come from that data. If the history share is small, step 1 may not be worth building.
- Step 1 is a stdlib module (`thread_digest.py`, `DIGEST_VERSION`, prompt version bump): the latest round in full; earlier rounds reduced to the Judge verdict, decision items, `findings`, Critic blockers and the first heading or paragraph of the proposal; long code fences replaced by `[code omitted: path:lines]`; repeated quotes of the proposal removed. The same input gives the same output (stable prompt cache prefix, golden-transcript tests, no usage-limit exposure). A digest is navigation only, never evidence: a citation passes only if those lines were shown to the session.
- Step 2 summarizes once, only when the history after step 1 is still above a threshold (for example 24 KiB), and stores the result in the thread. It pays off only with three or more fresh sessions (Expert, Critic panel, Judge). Whether `llm_runner.py` can choose a lighter model is unverified and must be checked first.

**Correction (2026-10-18).** The premise given to JEV for decision 9 was wrong. The "107 of 135 runs replaced by the built-in answer" figure belongs only to the first run of evaluation 2 (2026-10-12), which `plan.md` of that evaluation already declares invalid. The re-run of 2026-10-16/17 kept 135 runs with 0 fallbacks (6 attempts were discarded and repeated 30 minutes later, caused by the claude usage limit) and its results were committed on 2026-10-17 (`0b8588c`). So the gate "evaluation 2 is valid again" was already met when this document was written, and nothing needs to be re-run. Asked again with the corrected facts, JEV chose "start P2 once the retention policy and size limits are decided" (0.83, review grade) and rated another re-run unnecessary (yes 0.13). The remaining reliability point is that the usage limit still causes occasional fallbacks (about 6 in 135 attempts), which P2's rule "fallbacks and rate limits do not count as iterations, the run stops as `deferred`" covers. The user has to confirm the new decision 9.

JEV also rated deploying P1 (together with the audit A1/A2 changes) before any run against a real model on the hub as unlikely to be right (yes 0.23).

## Design summary

### Data model

```json
{
  "id": "arch-docs", "kind": "design", "name": "...", "description": "...",
  "artifacts": {"types": ["markdown", "mermaid", "svg"], "max_bytes": 262144, "reference_repo": null},
  "review": {"rubric": ["consistency", "missing requirements", "diagram accuracy"]},
  "policy": {"allowed_actions": ["jev_mcp:write_artifact"]}
}
```

- `kind` is `code | ops | design`. `repo`, `workspace`, `tests`, `context`, `wiki` and `targets` are rejected for design, and code rejects `artifacts`.
- `artifacts.types` must not be empty and is a subset of `ARTIFACT_TYPES`. `png`, `jpeg` and `html` are rejected until decisions 3 and 4 and the security design are done (after P4).
- `required_sections` (optional, empty by default) maps a Markdown artifact name to headings it must contain, e.g. `{"decisions.md": ["Context", "Decision", "Consequences"]}`; a missing heading is an L2 error (see the seeded-defect corpus).
- `reference_repo` is a reserved field that only accepts `null` for now (like `runbook` in ops). Judge tools = (team ∩ persona) ∩ kind ∩ `policy.allowed_actions`.

### Tools and team

- Add `jev_mcp:write_artifact` (risk medium) to `TOOLS`. Render tools are added after decision 3 is settled.
- The implementation is a new `artifact_ops.py` (version store, per-type validators, lock `artifacts/<project>/.lock`). The git concept is replaced by `base_version` (`artifact@v3`).
- The personas are code-biased ("principal developer", "senior code reviewer"), so `design_expert`, `design_critic` (rubric based), `design_judge` and the team `design-review` are new.
  The Expert prompt is nearly generic; what has to change are the Judge rules (`JUDGE_PATCH_RULES`: file:line, "checks in the code"). Split them into a judge profile
  and bump `RETRIEVAL_PROMPT_VERSION`. A test keeps the code prompt byte-identical.
- Mapping of prompting techniques (capability survey): spec first (the Expert writes purpose, audience, structure and constraints before producing; the Critic reviews against that spec), structured output (JSON contract for the judge,
  a response that ended on `max_tokens` counts as failed), higher effort for design critique and verdict (invalidates the cache, a cost), render-critique loop (validation L4 below, later).

### Where recent prompting techniques fit (capability survey, original sources not re-checked)

| Technique | Where it plugs in | Notes |
|---|---|---|
| Spec first | The Expert writes purpose, audience, structure and constraints before producing. The Critic reviews against this spec, not the artifact | Long input first, question last. Matches the current static-front/dynamic-back layout |
| Structured output | The Judge's JSON contract | Less thinking can reduce accuracy, so add a "think enough before answering" sentence or raise effort. A response that ended on `max_tokens` is a failure and retried even if the JSON is valid |
| Higher effort | Design critique and verdict | Currently `--effort medium`. Changing it invalidates the prompt cache. `xhigh` and above needs "stop and report when done" |
| Document/design Skills | Team guidance | Custom Skills in `.claude/skills/` also load with `-p`. Whether the hub syncs document Skills is unverified, so we do not rely on it |
| Role separation | The existing Expert/Critic/Judge already is this structure | `--agents` dynamic subagents are unnecessary |
| Render-critique loop (vision) | Critic (later, P5) | A new capability not in the repository. Needs an image delivery path and a renderer first |

### Validation layers (within the standard library)

| Layer | Check | Feasible |
|---|---|---|
| L1 structure and security | SVG `xml.etree` parsing (DOCTYPE/ENTITY rejected up front), script, foreignObject, `on*`, external href forbidden, size cap, JSON, Markdown fences | Yes |
| L2 references and accessibility | Relative links, duplicate ids, `alt`, heading order, Mermaid node definition/reference, hex colour contrast (WCAG approximation), required sections, terminology | Yes. Contrast and terminology are warnings only |
| L3 render success and Mermaid syntax | A real parser/renderer | No. Needs an external binary (decision 3) |
| L4 visual critique | The Critic sees the rendered image | No. There is no image delivery path (runners take text on stdin). Whether the claude CLI accepts MCP image results needs a bait experiment |

Principle: deterministic checks block, the Critic advises, a human approves last. The engine forces non-approval on L1 failures and security failures, and the Judge cannot waive them.
Factual claims about code are checked with the existing `findings` and `check_findings`; design intent is recorded as "not verifiable". Evidence notation is `[artifact:<name>@v<n> §heading]`
and follows the "only versions that were shown count" principle.

Improvement loop: one initial generation plus at most two revisions. Early exit on L1 and L2 passing with zero Critic blockers, two consecutive rounds without improvement, or budget exhausted.
Fallback responses and rate limits do not count as iterations; the run stops as `deferred`. The 2-5 minutes per iteration is an **estimate** from the measured code debate (about 283 s).

### Relation to auto-apply

`is_auto_approvable` looks only at the tool name. If the artifact kind reused `apply_patch` or `execute_code_action`, `auto:engine` would write without a human.
So (1) `write_artifact` stays outside `FILE_TOOLS`, (2) `is_auto_approvable` gets a kind check, (3) writes go only to version directories and never overwrite,
(4) a test pins it: even with jev PASSED and `GUARDIAN_AUTO_APPLY` on, `auto_run_actions` returns an empty list for design (the same shape as audit mode's "no action rows" test).

## Viewing and download

### Storage

Built in a temporary directory under `<DATA>/design-artifacts/<project>/<thread>/vN/`, then renamed (the `code_wiki.py` pattern). `manifest.json` goes through `os.replace`.
File entries are `{path, sha256, bytes, mime, kind, stage, agent, title, preview_of}`. File names match `^[a-z0-9][a-z0-9._-]{0,80}$`, no subdirectories,
and only files listed in `manifest.files` are served. The MIME type comes from an extension allow list in Go, not from the manifest.
Limits (decision 11): no quota. The only check is the L1 safety limit of 2 MiB per text file (`GUARDIAN_ARTIFACT_MAX_FILE_BYTES`), reported as a validation failure. Binary limits are decided together with P4. Versions that are not the latest of their thread are pruned after `artifacts.retention_days` (default 30, 0 = keep), and the latest version is never pruned.
The backup includes them (`_is_excluded` in `db_backup.py` includes new directories automatically). Derived previews live in `.cache/`, and an exclusion rule is added for it.

### Viewing

| Format | How |
|---|---|
| md, mermaid (.mmd), svg | Reuse `MarkdownDoc` and `sanitizeSvg`. **SVG written directly by the LLM is shown only through `<img>`, never inline** |
| png (after P4) | `<img>` (same-origin URL, cookie auth, CSP `'self'` is enough) |
| pdf (after P4) | Same-origin `<iframe>`. If the `CSP: sandbox` header from the security section is put on every artifact, Chrome's built-in PDF viewer is reported to refuse sandboxed documents (synthesizer's note, unverified). Either put the header on html/svg only or make "PDF preview under the sandbox header" a P4 check item |
| html | Sanitized view by default. The original goes into a `sandbox=""` iframe (no scripts). If decision 1 excludes HTML, this is P4 |
| pptx, docx | No preview. If a PDF/PNG is linked through `preview_of`, show that; otherwise a file card and download only |

The UI is an "Artifacts" tab at the level of the wiki tab, plus an entry point next to the debate result's `ReportButton`. List → pick a version → file cards (type chip, size, stage) → detail.
Korean copy is split into `ko/artifacts.ts`; mobile uses `useNarrow` for one column (iframe 100% width at 70vh, 44px download button).

### Download and authentication

- The hub dashboard (same origin) authenticates with an HttpOnly session cookie (`guardian_session`, SameSite=Strict). The cookie passes without `Authorization`
  (`authorized` in `auth.go`), so `<a href="…/files/<name>?download=1">` works without headers. No blob conversion is needed and large files stream.
  Checked in code by the synthesizer: `crossSiteWithoutOrigin` in `withSecurity` (`security.go:160`) rejects only when `Sec-Fetch-Site` is `cross-site` or `same-site`.
  A link click inside the dashboard (`same-origin`) and typing in the address bar or opening a new tab directly (`none`) pass, and because the cookie is `SameSite=Strict` a link from another site carries no cookie and gets 401.
  An actual browser download test has not been done yet (the end condition of P3).
- Forcing an attachment with `?download=1` means that even opened directly in a new tab the file is saved, not executed. This is the default safe path for HTML artifacts.
- Only Tauri and cross-origin use `authFetch` (Bearer) to fetch and save. The existing `save_download` takes strings only, allows only `.md` and caps at 16 MiB,
  so a binary `save_download_bytes` is needed (Rust change, decision required).
- Response headers: `Content-Disposition` via `mime.FormatMediaType("attachment", …)` for UTF-8 file names, `ETag` is the sha256, `nosniff`, `Cache-Control: private, no-cache`.
  `http.ServeContent` handles Range and If-None-Match. The whole bundle is an `archive/zip` stream including `manifest.json`, already compressed formats are stored.
- Artifact routes are not added to the `?access_token=` query path (`streamPaths`), because the token would stay in the URL. Views and downloads record only the name in `recordAudit`.

| Path (GET) | Response |
|---|---|
| `/api/projects/{id}/artifacts` | Latest version summary per thread |
| `/api/threads/{thread}/artifacts` | Version list |
| `/api/threads/{thread}/artifacts/v{n}` | Manifest |
| `/api/threads/{thread}/artifacts/v{n}/files/{name}` | File bytes, attachment with `?download=1` |
| `/api/threads/{thread}/artifacts/v{n}/bundle.zip` | zip stream |

These are read-only paths that read the disk directly without going through the CLI (`openRegular` O_NOFOLLOW plus a size cap, like `debate_report.go`). Events carry metadata only:
`artifact_created {version, path, kind, mime, bytes, sha256, stage}`, `artifact_version_finished`, `artifact_skipped {path, reason}`, `artifact_iteration`.

## Security (blocking items)

Serving LLM-made HTML/SVG from the dashboard's origin with scripts enabled is stored XSS. A script that sends `fetch('/api/exec', {credentials:'include'})`
passes both `withSecurity` (same-origin) and `withAuth` (cookie) and becomes a shell with the hub user's rights. `dashboardCSP` is attached only to UI responses in `ui.go`
(`setUISecurityHeaders`), and under `/api` only `nosniff` is set, so artifact routes carry no CSP. The minimum safe design:

1. Fixed headers on artifact responses: `Content-Security-Policy: sandbox; default-src 'none'; img-src data:; style-src 'unsafe-inline'; font-src data:; frame-ancestors 'self'`,
   `X-Content-Type-Options: nosniff`, `Referrer-Policy: no-referrer`. Content-Type is fixed by an extension table.
2. On screen, SVG is shown through `<img>` and HTML through `<iframe sandbox="">`. Never grant `allow-scripts`, and never `allow-same-origin`.
   The header's `sandbox` covers opening the URL directly in a new tab. Use both.
3. Server-side sanitizing is not used as a security boundary. A separate port gains nothing, since cookies are not scoped by port.
4. A `blob:` iframe and `srcdoc` without sandbox inherit the dashboard origin. Do not add `blob:` to the CSP; use cookie-based same-origin URLs.
5. `sanitizeSvg` turns on `ADD_TAGS: ['foreignObject']`. Allow it only for SVG produced by mermaid, not for SVG the LLM wrote directly.
6. Scan for known token patterns (such as the AGENT_TOKEN value) before storing. `is_secret` looks only at path names. Archives are neither accepted nor unpacked, so there is no zip slip.

A hub-side render (option C of decision 3) is not recommended. Headless Chromium uses 300-800 MB at a time (estimate), competes with Gitea on 2 OCPU, and the HTML being rendered
can reach `file://~/.guardian/agent.env`, the OCI metadata service and `127.0.0.1:9870`. Without a queue there is no good way to limit concurrency either.

## Phases

| Phase | Content | End condition |
|---|---|---|
| D0 | Confirm the decisions above, check the hub's claude version (below) | User record |
| P1 | `design` kind validation, `has_workspace`, `KIND_TOOLS`, judge profile, personas and team, reject git features (sync, publish, wiki, reset), read-only critique with the artifact text attached to the task | A design debate is recorded and no code context is attached. Code prompt byte-identical. `ci.sh python` passes |
| P2 | `artifact_ops.py`, `write_artifact`, L1 and L2 validators, version store, `base_version` stale check, manual approval screen | Proposal → approval → new version recorded. Test with zero auto-applies. Seeded-defect recall at or above the target set in D0 |
| P3 | Go read endpoints (list, file, zip), security headers, audit, backup rules, dashboard "Artifacts" tab (md, svg, mermaid), events | 401 without a cookie, 200 with Bearer and 206 for Range, 404 for path escape and symlinks, downloaded file sha256 equals the manifest, `ci.sh web` and `go` pass |
| P4 (conditional) | Isolated HTML view, allow png/pdf formats and previews, browser PNG/PDF conversion (unverified proposal), Tauri `save_download_bytes`, text improvement loop | Security header tests, check PDF preview under the sandbox header and whether `print()` works (content inside a `sandbox=""` iframe cannot be printed by the parent, so a printed PDF may apply only to Markdown and mermaid rendered by the dashboard itself), gain over `single` in evaluation A |
| P5 (conditional) | Server render, visual critique, DOCX/PPTX, image model | Only after decisions 3 and 4 change |

Evaluation: the deterministic layer plants 30 defects in 10 healthy artifacts (broken SVG, low contrast, dead links, missing required sections, false claims about code) and measures recall and precision.
The quality layer uses `single`, `loop-det`, `loop-text`, `loop-vision` and a best-of-N control with the same budget, 15 questions × 3 runs, graded blind (20% human re-grading).
The rules of evaluation 2 (runs with a fallback are moved to `.fallback-<k>` and re-run, the denominator counts valid runs only) apply unchanged.

### Implementation notes (P1)

Implemented 2026-10-17. The user confirmed decisions 1-7 with the recommended defaults and told us to start P1. Deviations from the design:

- **Start time**: decision 9's "after re-running evaluation 2" was not followed; the user told us to start first. Evaluation 2 had in fact already been re-run validly on 2026-10-16/17 (see the correction in the decision record).
- **`write_artifact` is not in P1**: it is not in `TOOLS`. So `policy.allowed_actions` has to be empty, and giving `["jev_mcp:write_artifact"]` as in the example above is rejected (allowed in P2 together with the tool). `KIND_TOOLS["design"]` (`DESIGN_TOOLS`) is the empty set.
- **`KIND_TOOLS`**: the table in `mcp_client.py` splits the tool list by kind (ops = `SERVER_TOOLS`, design = `DESIGN_TOOLS`, code = everything else). Without a project (kind `None`) the existing behaviour of offering everything is kept. Because the judge's tools are empty, the action list of a design debate is always empty, and an action an agent proposes anyway is recorded as INVALID (the `validate` message was not changed, so the "belongs to code projects" wording appears).
- **`has_workspace`**: kept separate from `is_ops`. `clone_path` raises `NoWorkspace` for `NO_WORKSPACE_KINDS` (ops, design), and sync, publish, wiki and `sync_projects("all")` reject or skip through this property. The ops target summary (`config_ops`) still uses `is_ops`.
- **Auto-apply allow list**: `action_ops.AUTO_APPLY_KINDS = (None, "code", "ops")`. The check uses the client of the project the action belongs to (`_client(mcp, action)`), so design is skipped in the recheck path (`--jev-recheck`) that uses the `Workspaces` resolver too (security review finding: the resolver has no `kind`, so the first implementation passed on that path). The debate engine also does not call `_auto_apply` when `project.is_design`. It is an allow list rather than a deny list, so when more kinds are added later the default stays off.
- **Artifact types**: `artifacts.types` accepts only `markdown`, `mermaid` and `svg`. `html`, `png`, `jpeg` and `pdf` are rejected with a "later phase" error. `artifacts.max_bytes` is validated to 1..1 MiB but not enforced in P1 (enforcement comes with `write_artifact` in P2). It is left out of the prompt so agents are not told about a limit that is not kept. The real cap is the Go handler's task length limit.
- **Review criteria (`review.rubric`)**: the default is three English items (`consistency`, `missing requirements`, `diagram accuracy`), at most 10, and each item must be one line (printable characters only). `design_prompt_text` appends them to the end of the task.
- **Debate path**: design projects call `_setup_retrieval(None, None, ..., repository=False)` to switch retrieval off, and the team's code context setting is ignored with a warning. `context_paths` is rejected. If no team is given and the thread has no stored team, `design-review` is used (personas `design_expert`, `design_critic`, `design_judge`, `allowed_tools: []`). The judge prompt bases its verdict on "passages of the received artifact" instead of tools, and the action rules were changed for design.
- **Prompt version**: `RETRIEVAL_PROMPT_VERSION` went from `2026-10-16.p6` to `2026-10-17.p7`. The value is recorded only in the context snapshot, so no cache is invalidated. Four existing tests that pinned the version (`test_evidence`, `test_system_prompt`, `test_go_facts`, `test_code_wiki`) were fixed along with it.
- **Code and ops prompt identity**: nine debate calls (every field of agents and judge) were captured before and after the change and compared with no difference (thread ids normalized).
- **Dashboard**: the Go agent does not look at the project `kind`; the CLI rejects. On the web, `lib/project.ts` gains `hasNoRepository` (ops or design), which hides the reference-code field of the debate start form and the wiki tab of the debates page and stops sending them. The reason is a problem from code review: the form first sent the team's default context paths as `context_paths`, so design debates were rejected with "drop the context paths". `'design'` was also added to `kind` in `types.ts`. Other screens (project list card, file view, sync button) were not touched and the behaviour for design projects was not checked on screen. A dedicated view is handled in P3.

### Implementation notes (P2)

Implemented 2026-10-18, not deployed. The user told us to start P2 and the token cleanup after decisions 11 and 12. Deviations from the design:

- **Store layout**: `<GUARDIAN_DATA_DIR>/design-artifacts/<project>/<thread>/vN/`, each version a full snapshot plus `manifest.json` (`manifest_version`, `version`, `parent`, `written`, `created_at`, and per file `path`, `sha256`, `bytes`, `mime`, `kind`, `stage`, `agent`, `title`). A write goes to a `.tmp-v*` directory under a per-project `flock` and appears with one `rename`; the manifest is written with `os.replace`. Files are mode 600, directories 700. Backup needs no rule: the nightly tarball includes the directory and there is no cache inside it.
- **Tool**: `jev_mcp:write_artifact(name, content, base_version?, title?)`, risk medium, outside `FILE_TOOLS` and `WRITE_TOOLS`, so it never auto-applies (`AUTO_APPLY_KINDS` is unchanged, the engine does not call `_auto_apply` for design, and a test pins zero auto-runs). `KIND_TOOLS["design"]` is `{write_artifact}`.
- **Tool gating**: `policy.allowed_actions` of a design project defaults to `["jev_mcp:write_artifact"]`; an explicit `[]` switches the tool off (the judge then gets the P1 "no tools" prompt); any other name is refused at save time. `MCPClient.project_tools` now narrows by this policy for ops and design alike. `team_manager.judge_tools` treats an empty persona list as "all", so the project's kind and policy are the real limit, not the persona.
- **`base_version`**: `artifact@vN`, required once the thread has a version, omitted for the first. A stale value is a repairable proposal error (the judge gets the repair message with the error, which names the current version; the prompt itself lists the files), is checked again at approval (`stale_reason`), and once more under the lock at write time (`STALE`, recorded as FAILED by `_execute`). Link checks against the thread's other files run at write time only (`known`); at proposal time the file set may still change.
- **Validators** are as designed for L1 and L2 with these differences: heading order is a warning, an image without alt text is an error, SVG forbids every `href` that is not `#id`, and Mermaid is checked by diagram type, forbidden directives and bracket balance, not by a parser. Secret shapes (private key block, AWS key id, GitHub, `sk-`, Slack, JWT) are refused in all three types. Contrast and terminology checks are **deferred**: they were warnings only and need a colour model and a glossary source that P2 does not have.
- **Limits**: 2 MiB per file (`GUARDIAN_ARTIFACT_MAX_FILE_BYTES`) and the project's `artifacts.max_bytes`, now enforced (P1 left it unenforced). `artifacts.retention_days` is validated 0..3650 (default 30, 0 keeps everything).
- **Retention**: `guardian.py --artifacts-prune` (`--dry-run` lists) expires non-latest versions older than the project's days and keeps the manifest as a tombstone (`expired`, `expired_at`); stale `.tmp-v*` directories older than one hour are removed. A directory whose project is gone or is not a design project is left alone. It runs as `ExecStartPre=-` of `server-guardian-ops-watch.service` (02:30 UTC), as decision 11 required, so there is no new timer and a failure does not skip the watch. An expired version is not carried into the next one.
- **Approval screen**: `describe_action` shows the arguments, the first 60 lines of the content, the target `artifact@vN` and the L1/L2 findings.
- **Prompt version**: `RETRIEVAL_PROMPT_VERSION` is `2026-10-18.p14` (judge design tool rules, and the thread digest below).
- **Not done**: the iteration loop, the Critic's use of stored versions, `[artifact:...]` citation checks, and the Go endpoints and the dashboard tab (P3). The seeded-defect corpus was built afterwards (see "Seeded-defect corpus" below); the P2 end condition "recall at or above the target" depends on the targets recorded in that section (set 2026-10-18, awaiting confirmation of the sections number). The other end conditions are met by `test_artifact_ops.py`.
- **Hardening after the security review** (same day, before the first commit). The review reproduced bypasses of the first validators; the fixes and what changed for the judge:
  - Markdown: raw HTML is refused except attribute-less inert tags (`br`, `kbd`, `sup`, `details`, ...); entity-encoded and whitespace-split `javascript:`/`data:` targets are decoded before the check; fences follow CommonMark (at most three spaces of indent, no backtick in a backtick fence's info string), so an indented fence no longer hides live text. Inline code spans are skipped by a linear scanner.
  - Mermaid: `click`/`callback`/`link` are refused after a `;` as well as at a line start; `%%{...}` init directives, front matter `config:` and `securityLevel` are refused anywhere; HTML in labels follows the Markdown tag rule.
  - SVG: an element allow list (no animation elements, no `foreignObject`), every element and attribute must be in the SVG, xlink or xml namespace, the root needs `xmlns`, `href` may only be `#id`, `xml:base` is refused, CSS (style elements and attributes) is decoded (escapes, comments, case) before `@import`, `image-set(`, `expression(` and non-`#` `url()` are looked for, and the whole text of a style element is scanned (text after a child element was skipped before).
  - All kinds: control and bidirectional-override characters are refused (the approval screen also prints them as U+FFFD); more credential shapes (fine-grained GitHub tokens, Google and Tailscale keys, `sk_live_`) are matched, also after entity decoding and in the title; text above `artifacts.max_bytes` or the safety limit is refused before any scan or parse; names and versions are matched with `fullmatch` (a trailing newline passed `$`); a manifest path that is not a plain artifact name is dropped when the manifest is read, so a tampered manifest cannot make `read`, `write` or `prune` touch a file outside the version; symlinked project and thread directories are skipped by the prune.
  - The approval screen validates with the project's own types and size limit, the same as the write.
  - Consequence for the judge: an artifact that used raw HTML in Markdown, SVG animation or a namespace-less `<svg>` is now refused at proposal time and the judge repairs it like any invalid proposal.

### Implementation notes (P3)

Implemented 2026-10-18, not deployed. Deviations from the design:

- **Routes** are the five GET routes above, registered in `apps/agent/artifact_handler.go` without `h.guard` (they read the disk directly). Go 1.22 patterns cannot put a literal prefix in front of a wildcard, so the version is a whole `{version}` segment parsed as `v[1-9][0-9]{0,8}`; the URLs are the same.
- **Layered reads.** Every directory level (data root, `design-artifacts`, project, thread, version) must be a real directory and every file is opened with `openRegular` (O_NOFOLLOW, regular files only), so a symlink anywhere answers 404. Only names that the manifest lists and that match the extension table (`.md`, `.mmd`, `.svg`) are served, and the MIME type is forced from that table, never taken from the manifest. A manifest must say it is the version in its path. Thread ids are limited to 80 characters (what `safe_id` writes).
- **The content is checked against the manifest on every read**: size cap 2 MiB, then length and sha256. A mismatch is a 409 (`artifact content changed`) and the bundle verifies all files before it starts streaming, so a tampered store never yields a partial zip with a valid-looking manifest. This costs one hash per view; files are small.
- **Fixed headers** on every file: `Content-Security-Policy: sandbox; default-src 'none'; img-src data:; style-src 'unsafe-inline'; font-src data:; frame-ancestors 'self'`, `X-Content-Type-Options: nosniff`, `Referrer-Policy: no-referrer`, `Cache-Control: private, no-cache`, `ETag` = sha256. Range and conditional requests go through `http.ServeContent`. `?download=1` sends `Content-Disposition: attachment`. The routes are not in `streamPaths`, so `?access_token=` is ignored (401 without a cookie or Bearer). The audit line records `artifact <name>@v<n>` or `artifact bundle @v<n>` and nothing from the content.
- **Expired versions** stay listed (summary with `expired_at`, manifest with `expired` files) and their files answer 404 instead of content.
- **Backup**: `db_backup._is_excluded` leaves out any `.cache` directory below `design-artifacts/` (derived previews); the artifacts themselves are backed up. `teams/.cache` is unaffected.
- **Dashboard**: `#/debates/artifacts[/<thread>[/v<n>[/<file>]]]`, offered as a second tab (Debates | Artifacts) only for `kind: "design"` projects, which have no Wiki tab. List → version chips (tombstones dimmed) → file cards (kind chip, size, stage, agent, short sha256) → viewer. Markdown goes through `MarkdownDoc`; `.mmd` is wrapped in a mermaid fence that is longer than any backtick run in the source; **SVG is shown only as `<img src="data:image/svg+xml;base64,…">`** built from the fetched text (no inline markup, no `blob:`, nothing added to the CSP). Copy is in `i18n/{ko,en}/artifacts.ts` plus `debate.page.tabArtifacts`.
- **Download** does not use the `<a href="…?download=1">` path of the design. The dashboard fetches the text with `authFetch` (cookie or Bearer, one code path, no token in a URL) and saves it through a Blob link; files are at most 2 MiB so streaming is not needed. The Tauri shell still has only `save_download` (Markdown), so other kinds show a notice there until `save_download_bytes` exists (P4). The `?download=1` header itself is covered by the Go tests.
- **Events are not implemented** (`artifact_created`, `artifact_version_finished`, `artifact_skipped`). `write_artifact` runs inside `action_ops._execute`, an approved-action CLI run outside any job's `events.jsonl`, so there is no event sink to write to. The audit line and the manifest are the record. Adding events needs a job-independent sink; not done.
- **Entry point**: `pages/debate/ArtifactsLink.tsx` puts a "View artifacts" link (`#/debates/artifacts/<thread>`) next to `ReportButton` in `JobResult` and `RunDetail`, for finished debates only. It is shown only when the debate belongs to the selected project and that project is `kind: "design"`, because the Artifacts tab exists only for the selected design project; a job without a project counts as the default code project. The link does not check whether the thread has stored versions; an empty thread shows the tab's own empty state.
- **Not done**: the real-browser download check (a click in the dashboard, then comparing the saved file's sha256 with the manifest). The Go tests cover 401 without a cookie, 200 with Bearer, 206 for Range, 404 for path escape and symlinks, and the sha256 of the served and zipped bytes; `ci.sh web` and `go` pass. The browser check stays **open** as an end condition of P3.

### Seeded-defect corpus (P2 evaluation)

Built 2026-10-18 in `cli/llm-agent/artifact_corpus.py` (test: `test_artifact_corpus.py`, run it with `python3 artifact_corpus.py [--json] [--min-recall CLASS=VALUE]`). It needs no model: the deterministic layer is the object of the measurement, and the quality layer (`single`, `loop-*`) is a separate evaluation.

- **Corpus**: 10 healthy artifacts (4 Markdown, 3 SVG, 3 Mermaid; they must pass with no error and no warning) and 30 defects, six in each of five classes, every one a single exact replacement in a healthy artifact. Only findings that the defect adds to its healthy base count, so a warning the base already has never makes a defect look detected.
- **Metrics**: recall is defects whose added findings include one that names the planted class (a pattern per class); precision is those detections divided by all flagged defects plus flagged healthy artifacts. A flag with no matching finding is listed as `misflagged`.
- **Result** (after the two detectors below): healthy artifacts flagged 0, recall 18/30 = 0.60, precision 1.00.

| Class | Detector | Recall | P2 target |
|---|---|---|---|
| broken SVG | L1/L2 SVG checks | 6/6 | 1.0 |
| dead links | L2 relative link and image-target check | 6/6 | 1.0 |
| missing required sections | L2 `artifacts.required_sections` (opt-in) | 6/6 | 1.0 |
| low contrast | none (deferred, see P2 notes) | 0/6 | none until the detector exists |
| false claims about code | none by design: `findings` and `check_findings` of the debate, needs a model | 0/6 | not a deterministic target |

- **Baseline** (first run of the corpus, before the detectors): recall 10/30 = 0.33. Dead links were 4/6 because only a missing alt text was checked on a Markdown image, and missing sections were 0/6 because the project had no section list.
- **Two detectors added the same day**, after the targets were chosen with the user:
  - **Image targets**: a relative Markdown image target that is not an artifact of the thread is an L2 warning (`image 'x', which is not an artifact of this thread`), by the same rule as links (schemes, `#`, `/` and `./` forms are skipped; skipped as a whole when the thread is not known). A target in a directory (`figures/a.svg`) is unknown because artifact names are flat.
  - **Required sections**: the project setting `artifacts.required_sections` maps a Markdown artifact name to the headings it must contain (any level, case and spacing ignored, a heading inside a code fence does not count). A missing heading is an L2 **error**, so the write is refused and the judge repairs it like any invalid proposal. It is **empty by default**: no project is affected until it lists files. `check_arguments` looks up the file being written, so only that file's list applies. Validation is in `project_registry._parse_required_sections` (Markdown names only, 1 to 32 printable headings per file).
- **Targets** (`TARGETS` in `artifact_corpus.py`, held by `python3 artifact_corpus.py --targets` and by `test_artifact_corpus.py`): 1.0 for broken SVG, dead links and missing sections. The sections target was proposed together with the detector and **still needs the user's confirmation**: 1.0 is reachable only because the corpus names its required headings itself; a real project's list decides what is found.
- **Not targeted**: low contrast stays at 0/6 until a colour-pair detector exists (deferred, see the P2 notes). False claims are excluded from the deterministic target; their six defects are the input for a separate measurement through the debate's `findings`, which needs a model and a stored code reference and was **not run**.
- **P2 end condition**: the first two parts are met; the third (seeded-defect recall at or above the target) is met for the three deterministic classes once the user confirms the numbers above. It is not met for contrast and false claims by design of the targets, not by accident.

### Implementation notes (decision 12, steps 0 and 1)

Implemented 2026-10-18, not deployed. Engine-wide, not design-only.

- **Step 0**: `agent_started` events carry `history_bytes`, the size of the replayed earlier rounds in that prompt (0 for a resumed session). Look at it in `events.jsonl` before tuning the threshold.
- **Step 1**: `thread_digest.py` (`DIGEST_VERSION = 1`) replaces `_format_history` for fresh sessions. Below 16 KiB (`GUARDIAN_THREAD_DIGEST_MIN_BYTES`) the replay is verbatim. Above it the latest round stays whole and every earlier entry keeps its head (Expert 1200, Critic 800, Judge 800 characters, an open code fence is closed, a Judge entry keeps its last `VERDICT:` line) followed by `[N more characters condensed]`. A header says the text is navigation only. `GUARDIAN_THREAD_DIGEST=off` restores the full replay. The result is used only if it is shorter than the full text.
- **Deviation**: the plan listed verdict, decision items, `findings`, Critic blockers and the proposal's first heading, and removal of long code fences and repeated quotes. The implementation is a plain head cut per entry, because the transcript is free text and a structural digest would depend on the persona format. The safeguards asked for ("안정장치") are the navigation-only header, the line-span rule of `evidence.py` (unchanged: a citation passes only if those lines were shown), the closed fence, the size check and the off switch. A structural digest can replace the cut later by bumping `DIGEST_VERSION`.
- **Step 2** (light-LLM digest) is not implemented; whether `llm_runner.py` can choose a lighter model is still unverified.

## Risks

| Level | Item | Mitigation |
|---|---|---|
| CRITICAL | Running LLM HTML on the same origin (XSS becomes `/api/exec`) | Headers and sandbox from the security section. Header tests are part of P3's end condition |
| CRITICAL | Rendering LLM HTML on the hub (file reads, SSRF) | No server-side render (decision 3) |
| HIGH | design silently inherits code tools and auto-apply | `KIND_TOOLS`, kind check in `is_auto_approvable`, pinned by tests |
| HIGH | codex/agy read `agent.env` and `llm.env` and leak them into artifacts | claude only (decision 8), token scan before storing |
| HIGH | Usage-limit fallbacks replace agent answers (the first run of evaluation 2 lost 107 of 135 runs; the valid re-run had 6 discarded attempts in 135) | Fallbacks do not count as iterations, the run stops as `deferred`; the evaluation rule of discarding and repeating applies |
| MEDIUM | Accumulating artifacts grow the backup tarball and disk | Retention pruning of non-latest versions (decision 11), L1 per-file safety limit |
| MEDIUM | Exfiltration through external URLs | CSP `img-src data:`, `default-src 'none'` |
| MEDIUM | Time and subscription quota consumed by the iteration loop | At most 2 revisions, `deferred` on rate limit |
| LOW | Path escape, symlinks, missing download audit | Server-generated file names, `openRegular`, `recordAudit` |

The counter-arguments are recorded too. (a) The identity of this product is server guardianship and operations debates, and ops P2-P6 and audit mode come first on the roadmap. (b) For documents and diagrams,
the Claude app's Artifacts/Docs already solve isolation and rendering. That is why narrowing the first scope to Markdown, Mermaid and SVG is recommended.

## Where the reviews disagreed, and what is unverified

- Go changes: the engine reviewer said "no Go change" (`newCmd` passes `--project=` through), the storage reviewer said five read endpoints are needed. Both are right. Starting a debate needs no Go change;
  viewing and downloading artifacts needs new Go handlers. The engine reviewer also pointed out that chapter 8 of the server-ops design ("409 in Go") differs from the actual implementation (rejected in the Python CLI).
  The design document was not corrected.
- The hub's claude version: an old document (`debate-code-context.md`) says 2.1.138, and flags were verified on this Mac's 2.1.289. The hub's current version is **unverified**.
  Recent flags such as `--json-schema` and `acceptEdits` are used only after checking the hub version (the runner has `parse_claude_version` feature detection).
- codex `image_generation` and agy's image generation: could not be confirmed in official documentation (unverified).
- Whether document Skills (pdf, xlsx) sync on the hub through the claude.ai login: official documents disagree, and it is unverified under the hub's minimal environment variable setup.
  The rule against copying credentials between nodes stands, so we do not rely on syncing.
- The time and memory figures (2-5 minutes per iteration, Chromium 300-800 MB) are estimates, not measurements.
- The prompting techniques of the capability survey (Sonnet 5.5 guide's thinking phrases, the effect of the crop tool) are an agent's summary of the official documents it read; the synthesizer did not reopen the originals.

## Items that need a user decision

1. Confirm the corrected decision 9: the retention gate is met (decision 11), so start P2 now, or wait for something else (for example the audit A4 comparison and deployment).
2. Whether to add `save_download_bytes` (a Rust change) to Tauri. Only needed from P4.
3. How far binaries go into the backup and their limits. Only needed from P4.

## Decision record (2026-10-23): the standard-library limit is lifted for document formats

The operator lifted the "Python engine stays standard library only" limit (CLAUDE.md, standing decisions) so that DOCX and the other
document formats of the table above (PPTX, XLSX) can be produced with a library instead of hand-written OOXML. Read narrowly: the
decision was taken while asking about DOCX, so it covers document artifact formats; the other exclusions (tree-sitter, graph DB, vector
index, task queue, fleet workers) stand until the operator says otherwise. Nothing is built yet. Open before P4 starts:

1. Which library (for example `python-docx`) and whether the engine imports it lazily, so a missing package fails only the DOCX tool.
2. How the dependency reaches the hub: the release tree assembled by `scripts/install-agent-service.sh` has no Python environment today.
3. How a DOCX is previewed: the file-card-only plan above stays unless a PDF/PNG preview is linked through `preview_of`.
