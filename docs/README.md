# Documentation

English | [한국어](README.ko.md)

Every document is in English (`name.md`, the main version) and Korean (`name.ko.md`). The project overview is
the repository [README](../README.md); the detailed system specification is [agent.md](../agent.md).

## Terms

- [Glossary: one word for one thing (node, execution host, critic, action, ...)](glossary.md)

## Design

- [Code wiki coverage: design document links, splitting large scopes, file view (draft)](design/code-wiki-coverage.md)
- [Wiki viewer: an index to start from, and moving between pages while reading (approved 2026-10-27; V0-V3 deployed 2026-10-28, V4 modes deployed 2026-10-28)](design/wiki-viewer.md)
- [Web dashboard languages (message catalogs)](design/dashboard-i18n.md)
- [Dashboard React migration and design system (design)](design/dashboard-react-migration.md)
- [Dashboard design language: warm paper light theme after the Notion analysis](design/dashboard-notion-theme.md)
- [Code retrieval for debate agents and the Agents monitor (design)](design/debate-agent-retrieval.md)
- [Code reading for debate agents (design)](design/debate-code-context.md)
- [Symbol index, repository map and code wiki for debate agents (design)](design/debate-code-index-wiki.md)
- [Judge actions: the partial edit tool (design)](design/debate-patch-tool.md)
- [Debate team UI contract (Python CLI ↔ Go hub API ↔ dashboard)](design/debate-teams-api.md)
- [Debate teams (lineups) design](design/debate-teams.md)
- [Per-node SSH security audit (fleet SSH audit)](design/fleet-ssh-audit.md)
- [User-defined node groups (Python CLI ↔ Go hub API ↔ dashboard)](design/node-groups.md)
- [Node registration through the hub's SSH config](design/node-registration.md)
- [Publishing a thread's changes](design/publish-thread.md)
- [Whole-repository audit mode (property audit, findings only, design agreed; A1 and A2 implemented)](design/repo-audit-mode.md)
- [Multi-perspective debates: independent proposals and blind critique (decisions made, nothing implemented)](design/multi-perspective.md)
- [Design, document and image artifact project (`kind: "design"`, review; P1 implemented)](design/design-project.md)
- [Project results tab (reports and artifacts of every project kind; deployed)](design/project-results.md)
- [Desktop export folder (save reports and artifacts into a chosen folder, manual and automatic; approved, nothing built)](design/desktop-export.md)
- [Office export: DOCX and PPTX from reports, diagrams for every project (D2 DOCX deployed 2026-10-28; D3 PPTX and report "Save .mmd" deployed 2026-10-28 as `b50af88`; D1 rendering deployed; report snapshot "Keep as document" built 2026-10-28, deployed as `f6497bd`)](design/office-export.md)
- [Install tracks: developer and operator (design agreed)](design/install-tracks.md)
- [Mesh join (approve and remove tailnet nodes from the dashboard)](design/mesh-join.md)
- [Node operations project (ops project without a repository, node inspection MCP, cleanup action catalog)](design/server-ops-project.md)
- [Fleet provisioning (planned package install and removal: host facts, package tools, protection, plans)](design/fleet-provisioning.md)
- [Batch execution (proposed: command sets and scripts on many hosts, critics vote, a person decides only on opposition)](design/batch-execution.md)
- [Web dashboard UI flow redesign](design/ui-flow-redesign.md)

## Operations

- [2026-10-05 fleet sshd hardening (MaxAuthTries, PermitRootLogin)](operations/2026-10-05-fleet-sshd-hardening.md)
- [Hub backup and restore](operations/backup-restore.md)
- [headscale operations (tailnet control server, emergency access, adding a node)](operations/headscale.md)

## Analyses and evaluations (point-in-time records)

- [Server Guardian system analysis report (2026-10-04)](analysis/2026-10-04-system-analysis.md)
- [APIs that do not work (2026-10-12)](analysis/2026-10-12-non-working-api.md)
- [Procedure and UI consistency review report (2026-10-12)](analysis/2026-10-12-procedure-review.md)
- [Phase 1 - Debate / Decision / Action lifecycle review](analysis/2026-10-12-procedure-review/phase1-debate.md)
- [Phase 1 - Development workflow review (server-guardian)](analysis/2026-10-12-procedure-review/phase1-dev.md)
- [Phase 1 - Server / fleet management review (ops analyst)](analysis/2026-10-12-procedure-review/phase1-ops.md)
- [UI review: apps/web-desktop (phase 1, read-only)](analysis/2026-10-12-procedure-review/phase1-ui.md)
- [Phase 3 - Debate / Decision / Action re-review](analysis/2026-10-12-procedure-review/phase3-debate.md)
- [Phase 3 - Development workflow re-review (5aa8e2e..52fbfe2)](analysis/2026-10-12-procedure-review/phase3-dev.md)
- [Phase 3 - Server / fleet management re-review (ops analyst)](analysis/2026-10-12-procedure-review/phase3-ops.md)
- [UI re-review, phase 3 (apps/web-desktop at 52fbfe2, read-only)](analysis/2026-10-12-procedure-review/phase3-ui.md)
- [Phase 4 - Debate / Decision / Action re-review](analysis/2026-10-12-procedure-review/phase4-debate.md)
- [Phase 4 - Development workflow re-review (52fbfe2..66cf41e)](analysis/2026-10-12-procedure-review/phase4-dev.md)
- [Phase 4 - Server / fleet management re-review (ops analyst)](analysis/2026-10-12-procedure-review/phase4-ops.md)
- [UI re-review, phase 4 (apps/web-desktop at 66cf41e, read-only)](analysis/2026-10-12-procedure-review/phase4-ui.md)
- [Phase 5 - Debate / Decision / Action re-review](analysis/2026-10-12-procedure-review/phase5-debate.md)
- [Phase 5 - Development workflow re-review (66cf41e..b82310c)](analysis/2026-10-12-procedure-review/phase5-dev.md)
- [Phase 5 - Server / fleet management re-review (ops analyst)](analysis/2026-10-12-procedure-review/phase5-ops.md)
- [UI re-review, phase 5 (apps/web-desktop at 7783afa, read-only)](analysis/2026-10-12-procedure-review/phase5-ui.md)
- [Review of a code reading architecture change: the Agentic MapReduce proposal (2026-10-12)](analysis/2026-10-12-retrieval-architecture-review.md)
- [Retrieval evaluation 1: symbol index and repository map (plan)](analysis/2026-10-12-retrieval-eval/plan.md)
- [Retrieval evaluation 1 results (2026-10-12)](analysis/2026-10-12-retrieval-eval/results.md)
- [Eval 2 ground truth (server-guardian @ b82310c)](analysis/2026-10-13-retrieval-eval2/ground-truth-e.md)
- [Retrieval evaluation 2 (plan)](analysis/2026-10-13-retrieval-eval2/plan.md)
- [Retrieval evaluation 2 results (re-run, 2026-10-16~17)](analysis/2026-10-13-retrieval-eval2/results.md)
- [Audit mode A4, first validation run (2026-10-19)](analysis/2026-10-19-audit-a4/results.md)
- [Audit mode A4, second validation run: large scope (2026-10-19)](analysis/2026-10-19-audit-a4b/results.md)
- [Multi-draft evaluation results (2026-10-19)](analysis/2026-10-19-multi-draft-eval/results.md)
