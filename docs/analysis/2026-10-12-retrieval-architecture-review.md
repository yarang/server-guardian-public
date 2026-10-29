# Review of a code reading architecture change: the Agentic MapReduce proposal (2026-10-12)

English | [한국어](2026-10-12-retrieval-architecture-review.ko.md)

> Point-in-time record from 2026-10-12; the code has changed since.

## Background

The proposal the user shared presents "deterministic indexing → sharding → agent Map → cross-validating Reduce" as the direction the industry is converging on. It is a multi-layer (L0-L4) structure with a graph DB and a vector index as shared state, a task queue and several worker servers.
As evidence it cites Cognition's security scan, Cursor's planner–worker, Aider RepoMap, RepoGraph, CodexGraph, LocAgent, Agent4cs, Meta-RAG and Code Broker.
A team of four reviewed whether to apply this proposal to server-guardian.

| Role | Perspective |
|---|---|
| Designer | The best way forward for this system |
| Critic | Attacks the proposal and the current plan equally; security |
| Operations | The real operating conditions and costs of the hub and fleet |
| Judge | Decides after checking the three opinions and the points of contention against the code |

The original texts of the sources the proposal cites (a ZenML summary page, arXiv numbers) were not checked in this review. The figures (reading and search are 56.2% of tool-use turns and 46.5% of tokens) come from a different task: a security scan of a whole codebase.

## Conclusion

Keep tool-based debate exploration on a single hub and continue P1 → P2 → P3. The proposal's distributed structure is not adopted (graph DB, vector index, queue, multi-server workers, tree-sitter and SCIP).
All three roles reached the same conclusion, and the facts support it.

- The repository is small (479 files, 4.2MB).
- The LLMs are subscription CLIs, so more workers share the same account's rate limit.
- The hub has 2 OCPU and 12GB and also runs Gitea and other apps.
- The user decided to use only the standard library.
- The bottleneck is not throughput but over-reading that comes from "not knowing where to read" (1,301 items, 5.6MB in one step).

Everything taken from the proposal is implemented lightly inside the current structure.

- Deterministic index exposed over MCP: done in P1 (`defs`/`refs`, `repo_defs`/`repo_refs`)
- Repository map: P2 (in progress)
- Separate verification step: deterministic citation matching, not an LLM (P2.5)
- Job key: `prompt_version`/`map_version` metadata (P2)
- Compiler-assisted index and `callers`/`callees` ops: starting with Go, type-checked with the hub's Go toolchain (P2.6, instead of a standard-library import graph by user decision on 2026-10-12)
- MapReduce, selector and sharding: as an audit job on the hub after P3 (user decision 2)

All the P1 defects the critic found were confirmed as fact (see "Fact check" below). But none of them justifies MapReduce, and they are fixed within the current design.

## Decision table

| Item | Decision | Phase | Reason |
|---|---|---|---|
| Selector, shards, Map/Reduce | Added to the roadmap (user decision 2). As an audit job on the hub after P3 | Audit job | Q&A and fix tasks have no target set to shard |
| planner–worker hierarchy | Not adopted | – | The roles are already strict; it only adds CLI calls |
| Graph DB, Cypher, vector index | Not adopted | – | More daemons, no gain at 4MB, breaks the standard-library principle |
| Index exposed over MCP | Done | P1 | `repo_defs`/`repo_refs` |
| tree-sitter, SCIP, LSP | Not adopted | – | User decision |
| Bottom-up summaries | Kept as the P3 wiki. Flat structure, not used as evidence | P3 | Stacking summaries stacks staleness too |
| Static analysis in judge verification | Later. Condition: in type D tasks, syntax or type errors cause `action_repair`/`INVALID` | After P3 | It belongs to patch verification, not exploration |
| Queue and tier-1 host workers | Not adopted. Revisit if operating conditions arise | – | Shared quota, per-host auth, code deployment, no leases or retries |
| Dependency community shards, halo, unresolved references | Later (with the audit job) | Audit job | – |
| Job key (sha, kind, prompt_version) | Now. Metadata only | P2 commit | Makes evaluations comparable with each other |
| Enforced JSON output schema | Partly adopted: optional block `findings[]` | P2.5 | Prose is the default. Record how often agents produce the block |
| L4 verifier | Adopted as deterministic citation matching (not an LLM) | P2.5 | jev already does LLM verification |
| Measure coverage against an independent ground truth | Adopted as a principle | Evaluation 2 (E1) | Measuring against the selector itself is circular |
| W1 `refs` is name-based | Go compiler-assisted index (type-checked `refs`, `callers`/`callees`); new ops do not count as evidence | P2.6 | Same-name collisions (`run`, `Close`) |
| W2 silent false negatives | Show the counts of parse failures, files over 1MB and limits reached at the end of `refs`/`defs` results | P2.1 (after evaluation 1) | It changes the text the agents see, so not during evaluation 1 |
| W3 file-level citation verification | A strong citation passes only if it is within ±K lines of lines the agent saw, or the agent `read` that file | P2.5 | One `refs` call makes 200 files citable |
| W4 cache stores the first line of declarations | Fix the docstring now. Masking string literals in value declarations and bumping `INDEX_VERSION` in P2.1 | Now / P2.1 | – |
| W4b `is_secret` checks only the file name | Check the full path and directory names (content check later) | P2.1 (needs user approval) | It changes which files the agents can read |
| W5 per-file `git cat-file` in `refs` | Change to batch reading | P2.1 | Performance |
| Evaluation questions E1-E3, 3 repetitions | Adopted | Evaluation 2 | Evaluation 1 cannot measure completeness, variance or false claims |
| Operations: cache cleanup, exclude from backup | Adopted | P3 | It can be rebuilt |
| Operations: dedicated slot for wiki jobs | Reflected in the P3 design | P3 | `MaxRunning` contention |
| Operations: hard cap per step | No cap. `retrieval_warning` event for steps over 2MB (256KB since 2026-10-12) (user decision 1; only the threshold was lowered after the wiki compaction incident) | P2.1 | On 2026-10-09 the user chose unlimited |

## Fact check (as of P1 commit `98e0be7`)

| Point | Result | Evidence |
|---|---|---|
| (a) Docstring saying the cache does not store source text | Not true. It stores the first line of a declaration as `signature` (200 characters), and module-level assignments (`API_KEY = "..."`) are declarations too. The same content already reaches the LLM through grep and read, but a copy that is never cleaned up stays on disk | `code_index.py:30-31`, `:63-65`, `:106-111`, `:452-474` |
| (b) Whole files from grep/`refs` results count as "shown" | True. `refs` returns at most 200 lines. grep was the same before P1 | `repo_tools.py:173-176`, `:46`, `:574-577`, `debate_engine.py:1443-1444`, `:1148-1149` |
| (c) Silent false negatives in `refs` | True. Parse failures, files over 1MB and the 5000-line limit per identifier are not reported | `repo_tools.py:572-573`, `code_index.py:524-526`, `:487-489`, `:51`, `:70` |
| (d) `is_secret` checks only the file name | True | `code_context.py:27`, `:115-117`, `repo_tools.py:245-247` |
| (e) Critics run in parallel | True (only for a panel). Expert and Judge run sequentially | `debate_engine.py:759-763` |

## Revised roadmap

1. **Evaluation 1 is frozen.**
   - The `map` setup is used exactly as in the P2 commit. Only metadata the agents do not see (`prompt_version`/`map_version`) and the docstring fix go in with it.
   - The criterion is the rule written in the plan in advance. The designer's "30% fewer bytes" criterion is not added afterwards; it becomes a goal for evaluation 2.
2. **P2.1, robustness:** W2, W4 (string masking and `INDEX_VERSION` bump), W5, W4b (if the user approves)
   - Exit condition: every case of the end-of-result notice has a test.
   - Exit condition: no string literals remain in the cache's `signature`.
   - Exit condition: `config/secrets/x.yaml` cannot be read.
3. **P2.5, evidence:** line-span citation verification and `findings[]` citation matching. Event `evidence_check{checked, mismatched}`
   - Exit condition: it catches the same-name `Handle` decoy.
   - Exit condition: it reports, per provider, the share of answers that produced `findings[]`.
4. **P2.6, Go compiler-assisted index:** extract facts with `go list -export` and `go/types` in a temporary worktree, then delete the worktree and outputs. `callers`/`callees` do not count as evidence. The design is "Layer 1 extension" in `docs/design/debate-code-index-wiki.md`.
   - Exit condition: ground-truth agreement on B1-B3 and E2 is equal to or better than P2.1.
5. **P3, wiki (as designed):** the key is the basis blob and `prompt_version`. It has a dedicated job slot, cleans up its cache, and never writes a page from a fallback answer.
   - Exit condition: it passes decoy 2, and no citation written from the wiki alone remains.
6. **Evaluation 2:** add E1-E3 to the 12 questions and repeat each 3 times. Record tokens per step, rate-limit hits and the number of unverified citations.
   - E1: every HTTP route in `apps/agent` and its auth wrapper. Measured by precision and recall against a ground truth built with grep.
   - E2: every path from the Go agent into Python
   - E3: questions about features that do not exist (measures false claims)
7. **Audit job (user decision 2):** start the design after P3. It runs sequentially on the hub with checkpoints by job key. Coverage is measured against an independent ground truth (the E1 method), not against the selector. The E1 results of evaluation 2 are an input to the design.
8. **Only if the condition arises:** static analysis in judge verification

## User decisions (2026-10-12)

| # | Question | Team's recommended default | Decision |
|---|---|---|---|
| 1 | Should there be a hard cap per retrieval step? | Keep unlimited. Emit only a `retrieval_warning` event when a step exceeds 2MB | As recommended. Warning event added in P2.1 |
| 2 | Is a whole-repository audit/enumeration mode needed? | Not now. Keep the Q&A and fix tasks and decide again after seeing the E1 results | **Added to the roadmap.** Design a single-process audit job on the hub after P3 (roadmap 7) |
| 3 | Should the secret filter be widened to the full path and directory names (content later)? | Add path and directory checks in P2.1; defer the content check | As recommended (P2.1) |
| 4 | Should the code index cache be excluded from the nightly backup and old entries cleaned up? | Yes (it can be rebuilt) | As recommended (P3) |
