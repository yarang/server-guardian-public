# Design Reconciliation & Operational Hardening Specification (design-reconciliation.md)

English | [한국어](design-reconciliation.ko.md)

Status: Final (2026-11-01)  
Target Documents: `docs/design/*.md`, `docs/analysis/2026-10-12-procedure-review.md`, `CLAUDE.md`, `agent.md`  
Related Documents: [server-ops-project.md](server-ops-project.md), [fleet-provisioning.md](fleet-provisioning.md), [batch-execution.md](batch-execution.md), [design-project.md](design-project.md), [repo-audit-mode.md](repo-audit-mode.md)

---

## 1. Overview & Objectives

Server Guardian's 23 design documents have evolved rapidly as features were incrementally introduced. Over time, structural inconsistencies and technical debt have accumulated:
1. Contradictions and premise drift between legacy and newer design documents (Specification Drift).
2. Security boundary erosion introduced by batch execution and ad-hoc free script policies.
3. Unresolved high-severity operational flaws (e.g. `--rotate` node disconnection, hub loopback SSH privilege escalation).
4. Unverified package adapters (`dnf`, `zypper`, `apk`, `pacman`) and unfinished roadmap items left in limbo.
5. Degradation of the Single Source of Truth due to mixing initial proposals with point-in-time implementation notes.

This specification defines the formal engineering resolutions to these five structural problems and establishes strict document governance rules.

---

## 2. Single Source of Truth & Specification Harmonization

### 2.1 Re-establishing the Scope of Configuration Management
* **Problem**: `server-ops-project.md` Chapter 2 defined "Configuration management is a non-goal", whereas `fleet-provisioning.md` Chapter 2 explicitly reversed this to "Package installation and removal are in scope".
* **Resolution**:
  - Formally update the scope of `server-ops-project.md`:
    - **In-Scope**: OS and runtime package lifecycle management (`ops:package_install`, `ops:package_remove`), service state control (`ops:restart_service`), safe disk cleanup (`ops:docker_prune`, `ops:journal_vacuum`, `ops:apt_clean`).
    - **Out-of-Scope**: General configuration management such as arbitrary template deployment, OS user account creation, and global firewall rule configuration (Ansible/Puppet style).

### 2.2 Eliminating Fictitious Module References (`ops_mcp.py`)
* **Problem**: `server-ops-project.md` described `ops_mcp.py` and `OPS_TOOLS`, which were never implemented and instead consolidated into `SERVER_TOOLS` in `ops_actions.py` and the Go Hub API. This misled subsequent design teams (such as `design-project.md`).
* **Resolution**:
  - Remove all references to `ops_mcp.py` across `server-ops-project.md` and `agent.md`. The official architecture is formalized as `cli/llm-agent/ops_actions.py` interfacing with the Go Hub HTTP API (`apps/agent/`).

### 2.3 Structural Capability Model for Project Kinds
* **Problem**: The legacy boolean check `(name in SERVER_TOOLS) == (self.kind == "ops")` in `mcp_client.py` broke when introducing `kind: "design"`, inadvertently exposing code modification tools to non-repository projects.
* **Resolution**:
  - Replace ad-hoc boolean branches with a centralized `KIND_CAPABILITIES` table shared across `project_registry.py` and `mcp_client.py`:
    ```python
    KIND_CAPABILITIES = {
        "code": {
            "has_workspace": True,
            "allowed_tools": {"read_file", "apply_patch", "run_tests", "list_dir", "grep_search", "find_by_name"},
            "auto_apply_allowed": True,
        },
        "ops": {
            "has_workspace": False,
            "allowed_tools": {"ops:package_install", "ops:package_remove", "ops:restart_service",
                              "ops:docker_prune", "ops:journal_vacuum", "ops:apt_clean",
                              "ops:run_command_set", "ops:run_script"},
            "auto_apply_allowed": False,
        },
        "design": {
            "has_workspace": False,
            "allowed_tools": {"write_artifact"},
            "auto_apply_allowed": False,
        }
    }
    ```

---

## 3. Security Boundary Re-hardening

### 3.1 Restoring Human Approval for Ad-hoc Scripts (`ops:run_script`)
* **Problem**: `batch-execution.md` F1 allowed unattended execution of ad-hoc scripts via critic voting on `script_groups`, despite acknowledging that deny-list static filters (`_BYPASS_PATTERNS`) are heuristic tripwires rather than security boundaries.
* **Resolution**:
  1. **Prohibit Unattended Ad-hoc Scripts**: `ops:run_script` must never be automatically executed via `policy.batch.auto` under any project or host group. It **strictly requires explicit human approval** (`--yes` or UI confirmation).
  2. **Unattended Execution Limited to Confirmed Command Sets**: Automated batch execution is exclusively restricted to pre-audited, digest-signed command sets (`ops:run_command_set` with `.confirmed.json`).
  3. Retire `policy.batch.scripts: "vote"`; only `"approve"` (mandatory human approval) remains valid.

### 3.2 Critic Voting Quorum & Fail-Closed Enforcement
* **Problem**: In `batch-execution.md` Section 5.3, missing votes count as abstentions which are counted as approvals (`abstain -> approve`). A systemic LLM outage or timeout could result in zero critics voting while opening the auto-execution gate.
* **Resolution**:
  1. **Enforce Quorum**: A gate release requires explicit `approve` votes from at least 2 critics (or 1 critic if the lineup seats only 1).
  2. Abstentions due to timeouts, API limits, or parsing failures are never counted as approvals. Insufficient quorum yields `auto: false` (reason: `insufficient_quorum`), deferring the plan to human review.

---

## 4. High-Severity Operational Fixes (OPS Hardening)

### 4.1 Fleet SSH Key Rotation Specification (`OPS-P5-1`, `OPS-P5-4`)
* **Problem**: `--rotate` targeted only 100.64.* subnets, disconnecting dashboard-registered nodes; staged private keys were stored in `~/.guardian-rotate.*` with normal user permissions; old keys remained indefinitely in `/etc/guardian/fleet_key.old-*`.
* **Resolution**:
  1. **Exhaustive Inventory Resolution**: Target hosts must encompass all active nodes in `servers.json` and `guardian_fleet.conf`; arbitrary subnet filtering is prohibited.
  2. **Runtime Memory Isolation**: Staging keys are generated solely under `/run/guardian-fleet/` (mode 0700, owner `root:guardian-fleet`).
  3. **Three-Phase Atomic Rollout**:
     - Phase 1: Deploy new public keys to all fleet nodes and test SSH connectivity across 100% of nodes.
     - Phase 2: Atomically swap `/etc/guardian/fleet_key` on the hub only after full verification. If any node fails, abort and purge the new key.
     - Phase 3: Remove old public keys from all nodes and shred (`shred -u`) the old private key.

### 4.2 Eliminating Hub Loopback Privilege Escalation (`OPS-P5-2`)
* **Problem**: The hub guard blocked only fleet routes, but the hub still held the fleet key in its own `authorized_keys`, allowing `/api/exec` or compromised subprocesses to run `ssh localhost sudo`.
* **Resolution**:
  1. Purge the fleet key from the hub's own `authorized_keys` and prevent its re-insertion during installer runs.
  2. Enforce loopback SSH blocking in `/etc/ssh/sshd_config`:
     ```
     Match Address 127.0.0.1,::1,<Hub-Tailscale-IP>
         DenyUsers ubuntu
     ```
     (All local hub operations must route through the internal `executeRaw` runner, subject to `GUARDIAN_HUB_SUDO`).

---

## 5. Lifecycle Management for Unverified Adapters & Roadmaps

### 5.1 Isolating Unverified Package Managers (`ops_packages.py`)
* **Specification**:
  1. Besides `GUARDIAN_PACKAGES_ALLOW_UNVERIFIED`, package managers lacking physical test hosts (`dnf`, `zypper`, `apk`, `pacman`, `bun`) remain rejected as `INVALID` at proposal time unless explicitly enabled in `policy.allowed_package_managers`.
  2. Establish a containerized harness (`systemd-nspawn` or Docker) on the hub to validate simulations and real execution outputs prior to promoting any manager to verified status.

### 5.2 Standardizing Design Lifecycle Metadata
Every design document header must declare one of four standardized statuses:
* `PROPOSED`: Proposal under community/team review.
* `ACCEPTED`: Approved design currently in active implementation.
* `IMPLEMENTED`: Verified production architecture.
* `DEPRECATED`: Superseded or retired design (with a pointer to the successor document).

---

## 6. Document Governance & Bilingual Synchronization

1. **Single Source of Truth**:
   - The body of each design document reflects only the currently active specification.
   - Chronological implementation notes, phase logs, and historical review findings must be partitioned into `## Appendix: Implementation & Decision History`.
2. **Automated Bilingual Verification**:
   - Add `scripts/check-docs-sync.py` to `scripts/ci.sh config` to verify structural parity (heading hierarchy, code blocks, metadata status) between `*.md` and `*.ko.md`.
