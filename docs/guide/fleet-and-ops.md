# Fleet Management and Ops Projects

English | [한국어](fleet-and-ops.ko.md)

Back to [User Guide](README.md).

This module covers managing your node inventory, node groups, remote diagnostics, SSH audits, ops projects (`kind:
"ops"`), typed cleanup actions, nightly ops watch, package provisioning, and batch execution.

## 1. Node inventory and registration

The source of truth for the fleet inventory is `~/.guardian/data/servers.json` on the hub (`GUARDIAN_SERVERS_FILE`).
`configs/servers.json` in your local checkout is only a git-ignored snapshot for `--offline` admin runs.

- **Add or edit nodes**: use **Nodes > Register node** (`#/fleet/nodes/new`) in the dashboard (reads the hub's SSH
  config and probes the node) or edit `~/.guardian/data/servers.json` on the hub.
- **Mesh join (tailnet)**: pending tailnet nodes appear in **Nodes** (`#/fleet/nodes`) where you can approve or remove
  them. See [headscale operations](../operations/headscale.md).
- **Sync local snapshot**: pull the hub's live inventory to your workstation with:
  ```bash
  scripts/pull-inventory.sh
  ```

## 2. Node groups

Nodes can belong to multiple user-defined groups stored in the hub decision DB. Membership is computed as:

`members = picked nodes + nodes matching any rule − excluded nodes`

Conditions inside one rule are ANDed; multiple rules are ORed. Supported rule fields: `tier`, `arch`,
`guardian_role`, `role` (`eq` / `neq`), `tailscale_tags`, `cli_support` (`contains` / `not_contains`), and `name`
(glob such as `site-b-*`).

```bash
# List groups
python3 cli/llm-agent/guardian.py --groups

# Create or update a dynamic group
echo '{"name":"Tier-1 ARM","rules":[{"conditions":[{"field":"tier","op":"eq","value":"Tier-1"},{"field":"arch","op":"eq","value":"aarch64"}]}]}' \
  | python3 cli/llm-agent/guardian.py --group-save --json

# Delete a group
python3 cli/llm-agent/guardian.py --group-delete tier-1-arm --json
```

Anywhere `--target` is accepted, Guardian resolves `local` / `all`, then a node id/name/hostname, then a group id/name
(case-insensitive).

## 3. Fleet diagnosis, remote commands, and SSH audit

```bash
# Parallel diagnosis of the whole fleet, one node, or a node group
python3 cli/llm-agent/guardian.py --fleet
python3 cli/llm-agent/guardian.py --fleet --target site-b-cluster

# Run a command in parallel across nodes
python3 cli/llm-agent/guardian.py --exec "uname -a" --target all
python3 cli/llm-agent/guardian.py --exec "df -h /" --target tier-1-arm

# Per-node SSH security audit (SEC-SSH-001 to SEC-SSH-005, cached 60 s)
python3 cli/llm-agent/guardian.py --ssh-audit
python3 cli/llm-agent/guardian.py --ssh-audit --target site-b-cluster --refresh --json
```

- Every command executed through `/api/exec`, `/api/exec/remote`, or `/api/fleet/exec` is logged to
  `~/.guardian/data/audit/exec.jsonl` (viewable via `GET /api/audit`).
- SSH audit statuses: `ok` (effective `sshd -T` with root/sudo), `partial` (read `sshd_config` files without root),
  `unreachable` (SSH failed/timed out), `error`. Only `authorized_keys` fingerprints, comments, and options are
  returned—never public key bodies.

## 4. Ops projects and the cleanup action catalog

An ops project (`"kind": "ops"`) targets nodes or node groups rather than a Git repository. The built-in project
`fleet` (`configs/projects/fleet.json`) covers every non-hub node with team `fleet-ops`, the full cleanup catalog, no
automatic runs, and nightly watch enabled.

During an ops debate, debate agents inspect nodes through fixed read-only ```` ```ops ```` checks (`disk_usage`,
`largest_dirs`, `largest_files`, `docker_usage`, `journal_usage`, `package_cache`, `service_status`, `top_processes`,
`uptime_load`, `headscale_status`, etc.). Every check is audited as `source=ops_check` and verified by citation.

Instead of arbitrary shell commands, the Judge proposes typed actions from the cleanup catalog:

| Action | Risk | What it does |
|---|---|---|
| `ops:docker_prune` | Low / Medium | Reclaim unused Docker images/containers/cache by scope |
| `ops:journal_vacuum` | Low | Vacuum `systemd-journald` logs by size or age |
| `ops:apt_clean` | Low | Clear `apt` package cache |
| `ops:remove_paths` | Medium | Remove specific non-protected files/directories |
| `ops:truncate_logs` | Medium | Truncate oversized log files in place |
| `ops:remove_old_kernels` | Medium | Remove old unused Linux kernels |
| `ops:restart_service` | Medium | Restart a named systemd service |
| `jev_mcp:server_remediation` | High | Free-form fallback command; always requires human approval |

Safety lifecycle for every catalog action:
1. **Deny list**: protected system paths, home directories, `.ssh`, and secret-looking paths are always refused
   (`policy.deny_paths` can only add restrictions).
2. **Preview**: measured when proposed so you see what will be deleted or restarted.
3. **Drift check**: re-measured right before execution; if size grew by more than 50% and more than 256 MiB (or new
   items appeared), the action becomes `STALE` and does not run.
4. **Verify**: re-measured after execution (`before`, `after`, `freed`, `ok`). Only one action runs per node at a time.
5. **`policy.auto_run`**: off by default. Projects may opt in specific low-risk actions (`docker_prune` low scope,
   `journal_vacuum`, `apt_clean`) with a daily cap (`max_auto_runs_per_day`, 1–100). Auto-run executes only when the
   `jev` gate verdict is `PASSED` (`decided_by: auto:<project>`).

## 5. Nightly ops watch

`server-guardian-ops-watch.timer` runs daily at `02:30 UTC`:

```bash
# Dry-run report without starting debates
python3 cli/llm-agent/guardian.py --ops-watch --dry-run

# Run watch for a specific ops project
python3 cli/llm-agent/guardian.py --ops-watch fleet
```

For each ops project with `policy.watch`, Guardian checks disk/inode usage (default threshold `85%`) and journal size
(default `3G`) on every target node and starts one ops debate for new findings (with a 7-day repeat guard per finding).

## 6. Fleet provisioning and batch execution

### Provisioning (`fleet-provision` team)
Ops projects that opt into `ops:package_install` / `ops:package_remove` can plan package changes across nodes using
the `fleet-provision` team:
- Built-in package protection (`configs/package-roles.json` + node `ops_roles` + `policy.protected_packages`) blocks
  removing critical packages and resolves listening ports to their owning package.
- Install bundles (`configs/package-bundles.json`, such as `nginx`, `postgresql`) supply vetted package lists.
- Multi-node proposals are stored as an ordered **plan** and reviewed in **Approvals** or via CLI:
  ```bash
  python3 cli/llm-agent/guardian.py --plans [<thread-id>]
  python3 cli/llm-agent/guardian.py --plan-show <plan-id>
  python3 cli/llm-agent/guardian.py --plan-run <plan-id>
  python3 cli/llm-agent/guardian.py --plan-reject <plan-id>
  ```

### Command sets and batch execution
Reusable command sets live in `configs/command-sets/` (`health-report`, `service-status`, `refresh-package-index`) or
are saved from **Nodes > Command sets**:

```bash
python3 cli/llm-agent/guardian.py --command-sets
python3 cli/llm-agent/guardian.py --command-set-confirm <id>
```

Projects that opt into `ops:run_command_set` / `ops:run_script` evaluate multi-node plans through critic **votes** and
the **batch gate**:
- Operator-saved command sets require explicit text confirmation (`--command-set-confirm` or dashboard confirm) before
  unattended batch execution.
- Free scripts (`ops:run_script`) always wait for human approval by default (`policy.batch.scripts: "approve"`). Even
  when an operator opts a non-hub group into `"vote"` mode with `batch.auto`, any critic objection holds the plan for a
  person, and bypass shapes (`eval`, piping into a shell) are rejected at proposal time.
