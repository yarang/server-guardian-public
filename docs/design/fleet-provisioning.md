# Fleet provisioning: planned package install and removal

English | [한국어](fleet-provisioning.ko.md)

Status: proposed (2026-10-19; scope decisions taken by the operator the same day, chapter 2); P1-P6 built and deployed 2026-10-20 as `6b24987`. Related:
[server-ops-project.md](server-ops-project.md) (ops projects, the action catalog this extends),
[multi-perspective.md](multi-perspective.md), [mesh-join.md](mesh-join.md).

## 1. Problem

The operator wants to give the LLM an order ("install nginx on the web nodes, remove the old Node from the build
nodes"), have it read the order against the real state of each node, make an install plan, and carry that plan out.
In the `fleet` project this does not work. Causes, found in the code and in debate `thread_20261019_025735_911ea1`:

| # | Where it stops | Evidence |
|---|---|---|
| 1 | No install or remove action exists. | `ops_actions.ACTIONS` has seven cleanup actions. The only other tool is `jev_mcp:server_remediation`, a free shell line (risk high, no preview, no verify) that the judge refuses. |
| 2 | The debate agents cannot see what a plan needs. | The ten checks are Linux and disk oriented (`df -x`, `free`, `journalctl`, apt cache). Nothing reports OS, package manager, installed packages, sudo, or what runs on the node. The inventory has `arch` only. On a macOS node most checks failed. |
| 3 | The unit is "one action on one node". | There are no per-node steps, no dependencies between steps, no stop rule, and the approval screen works action by action. |
| 4 | The team is built for cleanup. | The `ops_sre` prompt says "least destructive first, prefer a catalog cleanup action". With no matching action the judge ends with "no automated actions, an operator does it by hand". |
| 5 | The automatic path is closed on purpose. | `policy.auto_run` takes only low-risk catalog actions; installs are at least medium. |

`server-ops-project.md` chapter 2 lists "not a replacement for configuration management tools" as a non-goal. That
chapter is amended by this document (chapter 2, decision 1).

## 2. Decisions (operator, 2026-10-19)

1. **Scope.** Installing and removing packages on fleet nodes is in scope. The non-goal in
   `server-ops-project.md` now reads "no general configuration management: files, templates, users and
   service configuration are not managed; packages are".
2. **Protection.** Install is not limited by an allow list. Removal and side effects are limited by a **protected
   list**: a built-in list that cannot be lifted, the project's additions, and packages protected **by the role of
   the node** (chapter 6).
3. **Package tools.** Beyond apt and brew: other Linux managers and language-level tools (npm, bun, ...).
   The list with phases is chapter 4.
4. **sudo.** Used only where the node allows it without a password (`sudo -n`). A node without it is not
   changed; the plan reports why (chapter 5.4).

Standing decisions that stay: the Python engine is standard library only; nothing is installed on fleet nodes (the hub
reaches them over SSH); no free shell line is added for installs; humans approve (no `auto_run` for packages).

## 3. Design in one picture

```
order (text)
  -> Expert reads node facts and package info (read-only checks)      chapter 5
  -> Expert writes a plan: steps per node, order, stop rules          chapter 7
  -> validator (code, no model): scope, manager, sudo, protected list chapter 6
  -> previews by simulation, stored with the plan
  -> person approves the plan (one screen, every step listed)
  -> runner: per node in order, drift check, run, verify              chapter 8
```

## 4. Package tools

"Phase" is the implementation phase of chapter 10. Every tool is an adapter with the same five functions
(`facts`, `info`, `simulate`, `argv`, `query`); no adapter builds a shell string.

| Tool | Runs on | What it manages | Privilege | Preview | Phase |
|---|---|---|---|---|---|
| apt (`apt-get`, `dpkg`) | Debian, Ubuntu | system packages | `sudo -n` | simulated (`apt-get -s`) | P2 |
| dnf (`yum` as an alias) | RHEL, Fedora, Rocky, Alma, Amazon Linux | system packages | `sudo -n` | simulated (`--assumeno`) | P2 |
| brew | macOS, Linuxbrew | formulae, casks | user, never root | simulated for install (`--dry-run`); removal lists dependents (`brew uses --installed`) | P2 |
| npm (global) | any with Node | global CLI packages | user, or `sudo -n` when the prefix is root-owned | simulated (`--dry-run`) | P2 |
| bun (global) | any with Bun | global CLI packages | user | listed only (no dry run) | P2 |
| zypper | SUSE | system packages | `sudo -n` | simulated (`--dry-run`) | P2b |
| apk | Alpine | system packages | root or `sudo -n` | simulated (`--simulate`) | P2b |
| pacman | Arch | system packages | `sudo -n` | printed (`-Sp`, `-Rp`) | P2b |
| pnpm, yarn (global) | any with Node | global CLI packages | user | listed only | P2b |
| pipx, uv tool | any with Python | Python command-line apps (not `pip` into the system Python: PEP 668) | user | listed only | P2b |
| snap | Ubuntu | snaps | `sudo -n` | listed only | later |
| flatpak | desktop Linux | apps | user or `sudo -n` | listed only | later |
| nix (`nix profile`) | any with Nix | profile packages | user | simulated | later |
| cargo install, go install, gem, composer global | any with the toolchain | built from source or per language | user | listed only | later (compiles code on the node) |
| mas, winget, choco, scoop | macOS App Store, Windows | - | - | - | not planned (needs a GUI login, or the fleet has no Windows) |
| docker images, compose | any with Docker | services, not packages | - | - | out of scope (a separate service deployment design) |

"Listed only" means the preview shows the current state and the exact command, not a simulation. Such steps are
marked, are never part of a plan that `auto_run` could take, and raise the step's risk by one level.

(Operator wrote "burn"; read as **bun**. Correct this chapter if something else was meant.)

## 5. Seeing the node: facts and package info

### 5.1 New read-only checks (`ops_checks.py`)

| Check | Arguments | Result |
|---|---|---|
| `host_facts` | `host` | `os_family` (linux, macos), `os_id`, `os_version`, `arch`, `init` (systemd, launchd, other), package managers found (`command -v`), `sudo_nopasswd` (`sudo -n true`), logged-in users, enabled and running services with the package that owns each (`dpkg -S`, `rpm -qf`, `brew services list`), listening ports with the owning process and package |
| `package_info` | `host`, `manager`, `name` | whether the name exists in the manager's index, candidate version, installed version, dependencies, and for removal the packages that depend on it |

Both are audited like every check (`source=ops_check`). The output is data, never instructions (same rule as chapter 5.2
of `server-ops-project.md`). The existing checks get macOS variants (`df -h -I`, `ps -Ao pid,pcpu,rss,comm -r`,
`vm_stat`, `launchctl list`, `tmutil listlocalsnapshots /`), and `uptime_load` stops using a Linux marker file as
"reboot pending" on a node where it cannot know.

### 5.2 Facts cache

`host_facts` results are stored in `<data dir>/ops-facts/<host>.json` with a time stamp. The validator
(chapter 6) uses facts at most 15 minutes old and runs the check again otherwise. The cache is production state:
tests use `testenv.isolate_data_dir()`.

### 5.3 Inventory

The inventory entry may carry `ops_roles` (a list of role names, chapter 6.2). `servers.json` stays a local
snapshot; `servers.example.json` shows the field.

### 5.4 sudo

A system manager step needs `sudo_nopasswd: true` or a root login. Without it the validator marks the step
`INVALID: sudo_required` and the plan cannot be approved for that node; it is not tried. Other nodes of the same plan
are not affected. User-level tools (brew, bun, pipx) run without sudo; npm uses `sudo -n` only when the prefix is
root-owned and facts say sudo works.

## 6. Protection

A removal or install is refused (`INVALID`, with the package named) when it would remove a protected package, either
directly or as a side effect shown in the simulation (autoremove, dependency, conflict).

### 6.1 Built-in list (cannot be lifted by a project)

Per manager family, matched by name or glob, read from `ops_packages.py`:

- Reaching and controlling the node: the ssh server and client, `sudo`, `tailscale*`, `headscale*`, `systemd*`,
  `dbus*`, `launchd`-owned system packages on macOS, the network manager in use.
- The running kernel and its modules, boot loaders, `libc*`, `glibc`, the package manager itself and its libraries
  (`apt`, `dpkg`, `rpm`, `dnf`, `brew`), the init system.
- What Guardian itself needs on nodes it reaches: the interpreters the check commands use (`coreutils`, `findutils`,
  `procps`, `bash`/`zsh`, `python3` on Debian-family nodes where `apt` depends on it).
- Language runtimes behind other managed tools: removing `nodejs` is refused while a protected global npm package
  is installed.

### 6.2 Role-based protection

Protection follows what the node does, from two sources that are added together:

1. **Declared roles.** `ops_roles` on the inventory entry (for example `web`, `database`, `gitea`, `headscale`,
   `llm_cli_host`, `build`, `monitoring`) maps through `configs/package-roles.json` to protected package patterns per
   manager, e.g. role `llm_cli_host` protects the Node runtime and the global CLI packages that the hub's debate
   agents run through; role `gitea` protects `gitea` and its database client. The file is part of the repository
   and validated by `scripts/ci.sh config`.
2. **Derived from what runs.** From `host_facts`: the package that owns every enabled or running service, and
   every package that owns a process with a listening port, is protected on that node. A package you installed
   an hour ago and started is protected the moment it runs.

A project adds more with `policy.protected_packages` (it can only add, like `deny_paths`).

The protected list for a plan is computed once by the validator, shown on the approval screen per node, and recorded
with the plan, so a later change of roles does not change what was approved.

### 6.3 Install bundles

Roles (6.2) are a veto: they say what may not be removed. They install nothing. A **bundle** is the opposite and
separate thing: a named, exact list of packages per manager that the operator or the judge may *propose* for
installation. Two files, because the two lists want opposite shapes (a veto is safer wide, a proposal is safer exact),
and because a role that also installed would turn "protect more" into "install more".

`configs/package-bundles.json`: `{bundle: {description, reviewed, notes, packages: {manager: {required, optional}}}}`.

- `required`: what the bundle does not work without. `optional`: extras that are left out unless asked for by name.
  A partial install is therefore the default, and nothing is installed by "having a role".
- A required package may be skipped (`skip`) but the expansion reports it; an optional one may be added by name. A name
  the bundle does not offer is refused.
- A bundle is not a permission. `package_bundles.expand(name, manager, optional, skip, installed)` yields plain names
  for `ops:package_install`, minus what the node already has; each one passes the validator, simulation, protected list
  and drift check like a package typed by hand, and a plan step per package (or per group) keeps step-level approval.
- `notes` are one-line facts the approval screen and the judge must not lose (for example that `dnf` needs
  `postgresql-setup --initdb`, which package actions do not do).
- **Versions: the distribution decides, a bundle pins only where the name carries a version** (decided 2026-10-19).
  "LTS" is not one concept: PostgreSQL has none (a major version is supported for about five years), nginx has
  stable/mainline branches, Node.js and MariaDB have LTS lines. On apt and dnf the package names are unversioned and the
  version is the one the node's OS release ships, so keeping the OS on an LTS release keeps the service there; the
  install simulation already shows the candidate version on the approval screen. Where the name itself carries a major
  version (brew `postgresql@17`), the bundle is the policy. No vendor repository (PGDG, nginx.org) is ever added by a
  package action: that would add a signing key and a source outside the validated path. A service with a real LTS line
  gets the line in the bundle name (for example `nodejs_lts`) and one note saying which line it is.
- `reviewed` (required, `YYYY-MM-DD`) is the day a person last checked the names and versions of the bundle. A bundle
  older than 180 days (`GUARDIAN_BUNDLE_MAX_AGE_DAYS`) is listed under `stale` by `--package-bundles` and warned about by
  `scripts/ci.sh config`; it is not an error and still expands, because a stale list is a prompt to look, not a reason
  to stop an install. The file never stores end-of-life dates: a wrong date nobody notices is worse than none.
- The first bundles are `nginx` and `postgresql` (apt, dnf, brew). The package lists are a first set the operator
  still has to confirm.

## 7. Actions and plan

### 7.1 New catalog actions (`ops_actions.py`)

| Tool | Arguments | Risk | Preview | Run | Verify |
|---|---|---|---|---|---|
| `ops:package_install` | `target_host`, `manager` (name or `auto`), `packages` (1-20 of `name`, optional `version`) | medium; high when the step is "listed only" or the simulation adds a service unit | simulation: packages that would be added, upgraded or removed | adapter `argv`, non-interactive | each package present at the version; services active before are still active; protected packages still present |
| `ops:package_remove` | `target_host`, `manager`, `packages` (1-20), `purge?` | medium; high when more than 5 packages go or a service unit goes | simulation or dependents list | adapter `argv` | each package absent; same service and protected checks |

Rules for both:

- Names are checked per manager with a regular expression (npm accepts `@scope/name`). Versions are exact, not ranges.
- The source is always the manager's configured registry. A URL, a git reference, a tarball or file path, `--registry`,
  and a brew `owner/tap/formula` name are refused (taps only when `policy.allowed_taps` names them).
- npm and bun run package lifecycle scripts as usual (operator decision, 2026-10-19: no `--ignore-scripts` by
  default). `ignore_scripts: true` installs without them. Scripts run arbitrary code, which is why the source is
  restricted to the registry (above) and why bun stays high risk.
- `manager: "auto"` is resolved by the validator from facts to the node's native system manager and is stored
  resolved. If none is found the step is `INVALID`.
- Drift: the simulation is computed again right before running; a package that was not in the stored preview, a
  changed version, or a longer removal list sets `STALE`.
- `package_upgrade` is not part of this design.
- Both actions go through `/api/exec/remote` with the audit labels `source: action`, `op`, `project`, `plan_id`.

### 7.2 The plan

The judge's decision carries, next to `actions`, a `plan`:

```json
{"plan": {"goal": "nginx on the web nodes",
          "steps": [
            {"id": "s1", "host": "node-1", "tool": "ops:package_install", "args": {"manager": "auto", "packages": [{"name": "nginx"}]}},
            {"id": "s2", "host": "node-1", "tool": "ops:restart_service", "args": {"unit": "nginx.service"}, "requires": ["s1"]},
            {"id": "s3", "host": "node-2", "tool": "ops:package_install", "args": {"manager": "auto", "packages": [{"name": "nginx"}]},
             "on_failure": "continue"}]}}
```

- Steps are ordinary `decision_actions` rows (state `PROPOSED`) with three added columns: `plan_id`, `step`, `requires`
  (JSON). Everything that exists for actions (preview, `decided_by`, audit, `STALE`) applies per step.
- `requires` is a dependency inside one plan. A step runs after all it requires are `EXECUTED` with `verify.ok`.
- `on_failure` per step: `stop` (default: the rest of that node's steps become `SKIPPED`, a new state; other nodes
  continue) or `continue`. A plan-wide `stop_plan` is also allowed.
- Limits: 100 steps per plan, 20 per node, no cycles, only nodes in the project's resolved targets.
- No automatic rollback. When a step fails, the runner stops that node and proposes the inverse steps as new
  `PROPOSED` actions for a person to approve.

### 7.3 Validator (code, no model)

Runs when the decision is stored and again when the plan is opened for approval:

target in scope; manager present in fresh facts; sudo available where needed; names and versions well formed; protected
list (chapter 6) against the simulation; graph without cycles; limits. Each failure marks the step `INVALID` with a
one-line reason that the debate agents see in the next round.

## 8. Running a plan

```
approve plan (all steps, one decision record per step, decided_by the person)
  per node, in plan order, nodes in parallel (at most 2 at a time):
    wait for requires -> drift check -> run -> verify -> record
```

- The per-node action lock of `server-ops-project.md` 7.4 stays: one step per node at a time.
- `policy.auto_run` does not apply to packages. A plan is always approved by a person.
- The dashboard shows the plan as a table by node, with steps, protected packages, simulation result and risk. New
  strings go into both catalogs (`ko`, `en`).

## 9. Team and prompts

New team `fleet-provision` (category `operations`), project `fleet` `allowed_actions` gains the two tools and
`fleet-ops` keeps cleanup only, so existing behaviour does not change.

- Expert `ops_provisioner`: reads the order, calls `host_facts` and `package_info` for every named node, then writes
  node by node; package names differ by family (`apache2` vs `httpd`), so it checks each name with `package_info`.
- Critics: `ops_safety_critic` (reused) and a new `ops_dependency_critic` (protected packages, cascade, mixed
  fleets, order of steps).
- Judge `ops_provision_judge`: writes the plan JSON of 7.2.
- `RETRIEVAL_PROMPT_VERSION` is bumped.

Which team an order goes to stays the operator's choice in the debate dialog. Choosing the team from the order text
is not done in this design.

## 10. Phases

| Phase | Content | Needs a model to test |
|---|---|---|
| P1 (done, deployed 2026-10-20) | `host_facts`, `package_info` (apt, dnf, brew, npm), macOS variants of existing checks, facts cache, `ops_roles` field | no |
| P2 (done, deployed 2026-10-20) | `ops_packages.py` adapters (apt, dnf, brew, npm, bun), `ops:package_install`, `ops:package_remove`, built-in protected list, validator for single actions | no |
| P2b (done, deployed 2026-10-20) | zypper, apk, pacman, pnpm, yarn, pipx, uv tool | no |
| P3 (done, deployed 2026-10-20) | plan object, columns, validator, plan runner, `SKIPPED`, dashboard plan view | no |
| P4 (done, deployed 2026-10-20) | `configs/package-roles.json`, derived protection, `policy.protected_packages`, dashboard role editor | no |
| P4b (done, deployed 2026-10-20) | `configs/package-bundles.json`, `package_bundles.py` (`nginx`, `postgresql`), `--package-bundles`, CI check; the judge uses them from P6 | no |
| P5 (done, deployed 2026-10-20) | seeded corpus (`package_corpus.py`, like `artifact_corpus.py`): mixed-OS fleets, protected removals, cascade removals, stale simulations, missing sudo; recall and false-block rate | no |
| P6 (built, trial not run) | team `fleet-provision`, three personas, `bundles` check, plan rules in the judge prompt, `RETRIEVAL_PROMPT_VERSION` `p16`; the trial debate on an evaluation sandbox (`~/guardian-eval`) before any production node is still to do | yes |

Each phase records its deviations in a "구현 메모" subsection here. Tests never touch the real data dir and never
call a real package manager: adapters are tested through their `argv` and by parsing recorded outputs.

### 10.1 Implementation notes

**P1 (2026-10-19; code in `ops_facts.py`, wired in `ops_checks.py`; deployed 2026-10-20).**

- The audit label of the new checks is `source=ops_check` like every other check; chapter 5.1 first said `ops_mcp`,
  which is the label of the MCP server-inspection path, not of the `ops` block.
- `host_facts` is one POSIX `sh` script printing `key: value` lines (no python3 or jq on the node), parsed on the hub by
  `parse_host_facts`. It adds the usual install directories (`/opt/homebrew/bin`, linuxbrew, `~/.bun/bin`, nvm node) to
  `PATH` first, because a non-login ssh session on macOS has none of them. Its last line `facts_complete: yes` is
  the completeness marker: a run without it, with exit status other than 0, or cut at 64 KiB is never cached.
- Owners of listening ports need root or passwordless sudo. The script uses `sudo -n` when it works and prints
  `ports_owner_visibility: full|partial`; an owner it could not see is `unknown`, never empty. The derived protection
  of P4 must read `partial` and `unknown` as "cannot rule out", not as "nothing to protect" (done: "Blind spots" in the P4 notes).
- `host_facts` and `package_info` have their own time limits (60 s); service owners cost one `dpkg -S`/`rpm -qf` each and
  the service list is cut at 60.
- The macOS variants are selected on the node with `case "$(uname -s)" in Darwin) ...`, because a builder sees only its
  arguments and the facts of a node may not exist yet. They were run on a Mac; the Linux branches are the old commands
  unchanged. `uptime_load` now answers `reboot pending: unknown` where no marker exists (no dpkg, no `needs-restarting`),
  instead of "no reboot required".
- `package_info` knows apt, dnf, brew and npm. Names are matched against a pattern per manager (no leading `-`, no
  version, URL, path, tap or scope other than npm's `@scope/`). dnf runs with `-C` (cache only). The bun query command
  is not encoded yet: it is verified on a node that has bun when the bun adapter is written (P2). The dnf commands are
  covered by their command text only; they have not run on a real RPM node.
- The cache is `<data dir>/ops-facts/<host>.json`, written atomically (temp file and `os.replace`), node ids checked
  against a file-name pattern; `load_facts(host, max_age=900)` is what P2 and P3 call.
- `ops_roles` is an example field in `configs/servers.example.json`. P1 left it unread (the hub agent and `ops_targets`
  ignored unknown inventory fields); P4 reads it, see the P4 notes.
- `RETRIEVAL_PROMPT_VERSION` is now `2026-10-19.p15` (the ops protocol text changed).

**P2 (2026-10-19; code in `ops_packages.py`, wired into `ops_actions.py`, `mcp_client.py`, `action_ops.py`, `project_registry.py`; deployed 2026-10-20).**

- Package tools are opt-in per project. The default `allowed_actions` of an ops project is the server tools **minus**
  `ops:package_install` and `ops:package_remove`; a project lists them in `policy.allowed_actions`. `auto_run` still
  takes low-risk actions only, so packages (medium or high) never run unattended. The builtin `fleet` project lists them
  since P6, but only a judge whose team and persona also list them sees them (see the P6 note); the prompt version is
  `2026-10-19.p16`.
- Node facts are read **live when the cache is missing or older than 900 s** (`ops_actions.host_facts`, audit label
  `source=ops_check`) and only complete facts are stored. The judge therefore does not have to call `host_facts` first,
  and the action is validated again at approval time against fresh facts. Without usable facts the action is refused.
- The validator is split: `shape` (manager-independent: argument names, 1 to 20 packages, plain names, no duplicates,
  versions only on installs) runs in `ops_actions.validate`; `check` (needs facts: `auto` resolution, manager present,
  root or passwordless sudo for apt/dnf as `sudo_required`, brew never as root, `purge` apt only, `ignore_scripts`
  npm/bun only, per-manager name and version patterns, built-in protected list for removals plus
  `policy.protected_packages`) runs in `MCPClient.validate` and returns the arguments with the manager resolved.
- The simulation can refuse: `assess` returns a `refusal` when a removal would also remove a protected package or (brew)
  when installed formulae depend on the package. At proposal this stores the action as `INVALID`; at run time the
  measurement makes `execute` return `FAILED` without running, and `drift` reports it as stale ("debate again on the
  current state"). An install whose simulation removes something is `high` risk.
- Risk is `medium` by default; `high` for bun, more than 5 removals (requested or simulated) and
  installs that remove packages. npm and bun run package scripts unless `ignore_scripts` is set (operator decision
  2026-10-19; the first draft had scripts off by default and `allow_scripts` to turn them on).
- Preview and verification: the measurement has four sections (installed versions, simulation, exit status, running
  services) and is parsed before the 4000-character cut. Verification succeeds only when the exit status is 0, every
  package is (or is no longer) installed at the requested version, and no service that ran before has stopped.
- Sources of the output formats: npm (`--dry-run` `add/change/remove` lines, `ls -g --json`) and brew (`--dry-run`,
  `name old -> new` upgrades, `brew uses`) were run on a Mac. apt was then run on an Ubuntu 24.04 x86_64 fleet node the operator allowed for tests (2026-10-19): live facts, install
  and unknown-package simulations parsed as expected, a protected removal was refused, and a real `execute` of
  `ops:package_install` then `ops:package_remove` of `tree` ended `SUCCESS` with verification `ok` and left the node as
  it was. **dnf (transaction table, wrapped names, exit status 1 after `--assumeno`) is written from the tool's
  documented format and has never run on a real node**; a dnf node run was a precondition for P6 until the operator decided on 2026-10-19 to start with apt nodes only (see the P6 note).
- **bun is unverified.** Its adapter exists but is refused unless `GUARDIAN_PACKAGES_ALLOW_UNVERIFIED` lists `bun` in the
  hub environment, because no node with bun was available to check its commands and output.
- While probing brew on the Mac without `HOMEBREW_NO_AUTO_UPDATE=1` the taps of that Mac were updated once (harmless,
  and the production preamble sets the variable). URLs, git specs, tarballs, taps and version ranges are refused.

**P2b (2026-10-19; same files as P2, plus the tables in `ops_packages.py`; deployed 2026-10-20).**

- Managers added: zypper, apk, pacman (system, root or passwordless `sudo -n`) and pnpm, yarn, pipx, uv (user level).
  `auto` resolution on a Linux node looks for them after apt and dnf. `ops_facts.package_info` was **not** extended: it
  still knows apt, dnf, brew and npm only.
- **Verified live** (2026-10-19, on a node the operator allowed for tests, in scratch directories that were removed
  afterwards; the node is not named here): pnpm 9, yarn 1.22, pipx 1.18 and uv 0.13 install, list and remove. **Not
  verified on any real node: zypper, apk, pacman and bun** (no node, no container runtime). Their adapters are written
  from the documented formats and are refused unless `GUARDIAN_PACKAGES_ALLOW_UNVERIFIED` lists them. dnf is still unrun.
- Findings from the live runs that changed the design:
  - `yarn global list` hides packages without a binary, so the installed version is read from
    `$(yarn global dir)/node_modules/<name>/package.json`. Only yarn 1.x has `global`; the command exits 3 with a clear
    message on other versions.
  - pnpm needs `PNPM_HOME` and a matching `PATH` before any global command; the preamble sets both per OS.
  - `pipx` and `uv tool` take **one package per action** (each creates its own environment; a second name would hide
    which one failed). `pipx install` of an installed package is a no-op, so verification compares the version.
    Python names are compared in PEP 503 normalized form. Version pins use `==`.
- Listed-only managers (bun, pnpm, yarn, pipx, uv) have no dry run: the preview is the request plus the exact command and
  the risk is `high`. zypper (`--dry-run`), apk (`--simulate`) and pacman (`-S/-R --print --print-format "%n %v"`) are
  simulated; for these the simulation runs under the privilege prefix too.
- pacman: no `-y` (a database refresh without a full upgrade is a partial upgrade) and no version pinning. zypper exit
  codes 102 and 103 (reboot or restart hint) count as success on a real run. A protected package in a simulated removal
  is a `refusal`, as for the other managers.
- `ignore_scripts` is accepted for npm, bun, pnpm and yarn and refused for the rest. Scripts still run by default.
  The reading of "no default `--ignore-scripts`" as opt-in is the assistant's; the operator has not confirmed it.

**P3 (2026-10-19; `plan_ops.py`, `plan_handler.go`, `PlanDialog.tsx` / `PlansPanel.tsx`; deployed 2026-10-20).**

- **Storage.** Table `decision_plans` (id, thread_id, round_num, project_id, goal, stop_plan, created_at) and four
  columns on `decision_actions` (`plan_id`, `step`, `requires`, `on_failure`); old databases migrate in place. Plan id is
  `plan_<thread>_r<round>`. Steps are ordinary action rows, so the approvals list, audit, `decided_by` and the jev gate see
  them like any action.
- **Two levels of validation.** A structural error (shape, duplicate step id, a cycle, more than 100 steps or 20 per node)
  refuses the whole plan and nothing is stored. A step that does not validate on its node is stored as `INVALID` with the
  reason; the plan stays reviewable, which departs from 7.3 ("marks the step INVALID") only in that the plan can still run:
  an `INVALID` step counts as a failure, so its dependants are `SKIPPED`.
- **Previews.** A step is measured when the plan is stored. A step whose `requires` have not run yet cannot be measured
  honestly (the node will differ), so its preview is stored as `deferred` and measured right before it runs; the drift
  check then has nothing to compare, and only the refusal rules apply. The dashboard says so.
- **Semantics of failure.** `FAILED`, `STALE` and `INVALID` are failures; `REJECTED` (a person's choice) and `SKIPPED` (a
  consequence) are not. `on_failure: stop` (default) skips the rest of that node; `continue` keeps going; `stop_plan`
  skips every step that has not started, on every node. A step is skipped when anything it requires failed or was
  skipped. **There is no rollback and inverse steps are not proposed** (7.2 mentions them; not built, the open point in
  chapter 11 stays).
- **Running.** Nodes in parallel (2 at a time), one step per node (the per-node lock of server-ops 7.4); a busy node is
  waited for 120 s, then the step fails with `not_run` (it never started). The plan itself is locked, so one run at a time.
  Interrupted runs resume: only `PROPOSED` steps start, finished ones keep their state. `policy.auto_run` never runs plan
  steps. A plan step cannot be run or rejected alone (`CONFLICT`, dashboard hides the button); a `HELD` step blocks the
  plan until `--jev-recheck`.
- **CLI.** `--plans`, `--plan-show`, `--plan-run ID --yes`, `--plan-reject` (all `--json`; exit 2/3/4 map to 400/404/409).
- **Hub.** `GET /api/plans[?thread=&project=]`, `GET /api/plans/{id}`, `POST /api/plans/{id}/run` (body
  `{"confirm_id": "<plan id>"}`) and `/reject`. A plan can run for hours, so the run is detached from the request: the hub
  answers `202 {"started": true}` if it is still running after 3 s and the dashboard polls the plan every 3 s; a run that
  ends within the grace answers with the result. One in-process run per plan (409 on a second). `GUARDIAN_UI_ACTIONS=off`
  blocks run and reject, not the reads.
- **Dashboard.** Plans panel above the approvals list (hidden when the project has none); `#/approvals/plan_...` opens the
  plan dialog: steps by node, simulation, requires / on-failure, run (checkbox for low and medium risk, the plan id typed
  for the rest) and reject. The action dialog of a plan step explains that it runs with the plan and links to it.
- **Not done / known gaps.** The audit log does not carry a `plan_id` label (steps are labelled as single actions). The
  fail-stale live check (`failStaleAction`) does not know about plan runs; a late result is recorded as `late_result`.
  Prompts did not mention `plan` before P6; the provisioning judge writes one since (see the P6 note).

**P4 (2026-10-19; code in `package_roles.py` and `configs/package-roles.json`, wired into `ops_packages.py`, `ops_actions.py`, `mcp_client.py`, `ops_targets.py`, `project_registry.py`; Go and dashboard; deployed 2026-10-20).**

- **Four sources, added together.** The built-in `ops_packages.PROTECTED` list (cannot be lifted), the roles of the node
  (`ops_roles` -> `configs/package-roles.json`), what runs on the node (derived from the facts) and
  `policy.protected_packages` of the project (can only add). Roles ship with `web`, `database`, `gitea`, `headscale`,
  `llm_cli_host`, `build` and `monitoring`; the package lists are a first set the operator still has to confirm.
- **Recomputed, not stored.** The design said the protected set is computed once. It is computed at every validation
  instead: when the action is proposed (`validate`), when the preview is measured (`ops_preview`) and right before it runs
  (`execute_action`), from the facts cache, the inventory and the policy of that moment. A role added or a service
  started after the proposal therefore still blocks the run. The preview carries `protected` ("pattern (source)" lines)
  so the approval screen can say why; the dashboard does not show that field yet.
- **Unknown role refuses.** A role in `ops_roles` that the file does not define refuses every package action on that
  node until the inventory is fixed: a misspelt role would otherwise protect nothing without anyone noticing. An
  unreadable or invalid roles file refuses package actions everywhere.
- **Derived protection is narrow.** It takes the owner package of every running service and the owner of every
  listening port, and it applies to the system managers of the OS family only (apt/dnf/... on Linux), never to npm or
  brew. Names that are not plain literals, and `unknown`, are skipped.
- **Blind spots (closes the gap P1 left).** The Linux `host_facts` script resolves the listening binary of each port to
  its package (`ss -p` pid, `/proc/<pid>/exe`, `dpkg -S` / `rpm -qf`) and prints `port: ADDR proc=NAME pkg=PKG`, so a
  process called `postgres` protects `postgresql-16`; facts stored in the old format (no `pkg=`) fall back to the
  process name. `package_roles.blind_spots()` names what the protected list cannot see: a port with no owner and a
  `ports_owner_visibility` other than `full`. The reasons travel as `blind` in the measured preview, and
  `ops_packages.risk()` rates a **removal** on such a node `high` (installs are not held back; the dashboard shows the
  reasons next to the simulation, in the action dialog and in the plan view). Two traps found by running the script on
  a real node: a socket-activated service (sshd, rpcbind) is listed under pid 1, whose package is the init system and says
  nothing about the service, so such a port is left `unknown` (and therefore a blind spot) instead of protecting
  `systemd`; and `/proc/<pid>/exe` of a binary replaced by an upgrade ends in ` (deleted)`, which is stripped before
  the package lookup. A service without a package is not a blind spot: it is listed by name. It is still a rating, not
  a refusal: a person approves the removal.
- **Project additions apply to every manager.** `policy.protected_packages` is stored under the `"*"` key; a pattern is a
  package-name fnmatch (letters, digits, `. _ + @ / -`, `*` and `?`, at least one literal character, up to 100).
- **Inventory.** `ops_roles` is now read: `ops_targets.resolve` returns `roles`, `host_roles()` lists them. The hub agent
  validates it (`metaRoleRe`, 1-32 lowercase characters) and keeps it in `FleetNode`/node metadata; `GET
  /api/package-roles` serves `guardian.py --package-roles` (roles, descriptions and patterns) to the dashboard.
- **Dashboard.** The node form has a "Package roles" list field with the known roles as a hint; the ops project form has
  a protected packages textarea. Both catalogs (`ko`, `en`) carry the strings.
- **CI.** `scripts/ci.sh config` runs `guardian.py --package-roles` and fails on an invalid roles file.
- **Not done.** `preview.protected` is not shown in the dashboard step details; `protected` patterns for managers a role
  does not list are simply absent (a role lists the managers its authors thought of).

**P4b (2026-10-19; code in `package_bundles.py` and `configs/package-bundles.json`; deployed 2026-10-20).**

- Chapter 6.3 is new: install bundles are a separate file from the roles on purpose (veto versus proposal). Nothing in P4
  changes; a role still installs nothing.
- `guardian.py --package-bundles` lists and validates the file (JSON, exit 2 when invalid), and `scripts/ci.sh config`
  fails on an invalid file. Package names are checked with `ops_packages.NAME_PATTERNS` of each manager, so a name the
  validator would refuse cannot sit in a bundle. A test hands every expanded bundle to `MCPClient.validate` on matching
  facts (apt, dnf, brew), and checks that the role `web` / `database` protects the required packages of `nginx` /
  `postgresql`.
- **Wired to the judge in P6.** The read-only check `bundles` lists them and expands one for a manager (see the P6
  note). There is no `GET /api/package-bundles` and no dashboard view.
- `postgresql@17` is the brew formula (checked against a local brew); a newer major version needs a new entry.
- **`reviewed` and the version policy (2026-10-19, same phase).** Every bundle has a required `reviewed` date; `expand`
  returns it, and `--package-bundles` adds `max_age_days` and `stale` (oldest first). CI only warns. Both bundles carry a
  note that apt and dnf follow the distribution's version.

**P5 (2026-10-19; code in `package_corpus.py`, tests in `test_package_corpus.py`; deployed 2026-10-20).**

- 10 healthy actions and 30 planted defects, six in each class: mixed-OS fleets (a manager the node lacks, an
  unverified manager the operator has not enabled, brew as root, `purge` outside apt), protected removals (built-in list,
  role, running service, listening port, project policy, and the blind-spot rating), cascade removals (a dependency that
  is protected, a project-protected one, a failed pacman transaction, more than five removals, an install that removes),
  stale simulations (a new removal, a package that became protected by a service or a role, the package gone, a failing
  simulation) and missing sudo (four managers, no facts, sudo lost between proposal and run).
- `decide()` calls the same functions as the hub in the same order: `package_roles.protection`, `ops_packages.check`,
  `ops_packages.assess`, `ops_packages.risk`, and again at the run with `ops_actions.drift`. No model, no node and no
  package manager runs, and the data directory is not touched. `GUARDIAN_PACKAGES_ALLOW_UNVERIFIED` is set per scenario
  and restored.
- A defect counts as caught only when the gate refuses it for the reason its class expects (`needle`), or, for the
  defences that are a rating and not a refusal, when the action stays possible but is rated `high` and its healthy base
  was not. A base that is already refused never makes a defect look caught. The false-block rate is the share of
  healthy actions that are refused or rated differently: 0.00.
- First measurement: recall 1.00 in every class (30 of 30), false-block rate 0.00. The first run scored 28 of 30: both
  misses were faults of the scenarios (a pacman error line that carries no `error:` marker; a role scenario whose package
  was not installed), not of a gate; they were corrected before the number above. `python3 package_corpus.py --targets`
  holds every class to 1.00 and the false-block rate to 0.00; CI runs it as part of the unit tests.
- **What the number does not say.** The simulations are the recorded formats (apt, npm, brew) and the documented ones
  (dnf, zypper, apk, pacman); a format that a real node prints differently is the corpus's blind spot, which is why the
  dnf node run is not done; the operator accepted that on 2026-10-19 and limits use to apt nodes. The corpus measures the gates, not whether a judge proposes good plans.

**P6 (2026-10-19; built and deployed 2026-10-20 as `6b24987`, trial debate not run).**

- **Team `fleet-provision`** (category `operations`, 2 rounds): expert `ops_provisioner`, critics `ops_provision_safety_critic` and
  `ops_dependency_critic`, judge `ops_provision_judge`. Its `allowed_tools` are `ops:package_install`,
  `ops:package_remove` and `ops:restart_service`; the persona `mcp_capabilities` list the same three.
- **Three gates must agree.** The judge gets a tool only if the team list, the persona and the project's
  `policy.allowed_actions` all name it (`team_manager.judge_tools`, then `MCPClient.project_tools`). The builtin `fleet`
  project now allows the two package tools, but `fleet-ops` (persona `ops_judge`) still gets none, and a new ops project
  (default policy without them) leaves the provisioning judge only the restart. Tests cover all three cases.
- **Prompt gate.** `DebateEngine` sets `provisioning` when the judge's tools include a package tool. Only then do
  `ops_checks.protocol_text(True)` (the `bundles` check and the "Package orders" rules: `host_facts`, then `package_info`
  per host, prefer a bundle, cite `[#n check host]`) and `JUDGE_PLAN_RULES` (orders go into a `plan` key of the same JSON
  block, not into `actions`) reach the debate agents. `ops_checks.protocol_text()` without the flag is byte-identical to the
  version before P6, so the `fleet-ops` prompts did not change; `RETRIEVAL_PROMPT_VERSION` is `2026-10-19.p16` because the
  provisioning prompts are new.
- **`bundles` check.** Hub-local, no host: no arguments lists the bundles, `name` and `manager` together expand one
  (`package_bundles.expand`). Calls are served without a host, so a citation of it verifies with any host token. For a
  lineup without package tools the check is unknown.
- **Tests** (`test_provisioning.py`, 13): the tool gate in the three cases, the team files, the check and its errors, the
  example plan of the prompt through `parse_plan` and `normalize_plan`, and the prompt gate through a full engine run with a
  fake runner.
- **Not done.** The trial debate on `~/guardian-eval` was not run: it uses the Codex quota shared with production, so it
  needs the operator's go-ahead. No hub team other than
  `fleet-provision` can propose packages. Deployed 2026-10-20 (`6b24987`). The `--ignore-scripts` reading and the bundle composition (optional
  extras, `postgresql@17`) still wait for the operator.
- **Decision (2026-10-19, operator): apt nodes only, no dnf verification.** There is no dnf test node, so `fleet-provision`
  is used on apt nodes first and dnf nodes stay unverified. Nothing in the code enforces this (dnf is not in
  `ops_packages.UNVERIFIED`); the safeguards are that package actions are never auto-run (medium or high risk) and that
  the operator reads the preview of every step before approving. Adding `dnf` to `UNVERIFIED` would make the hub refuse
  dnf nodes unless `GUARDIAN_PACKAGES_ALLOW_UNVERIFIED` lists it; that is the fail-closed option if wanted.
- **First real use and a correction (2026-10-19, operator feedback: too defensive).** The order was "check the timezones
  and packages, let's manage the servers" (`thread_20261019_142607_2ed0f3`); the result was APPROVED with no actions and five
  `constraints` that nobody had asked for ("do not change the 5 hosts until the user picks a zone", "UTC is not the
  default", "restart one at a time", "pilot on one host", "do not touch owner packages"). Causes: the safety critic was
  only told to find the safer alternative, so every objection pushed toward restraint; the judge adopted the objections
  as binding constraints ("both critiques accepted"); and the expert's open question (the target zone) became a hold.
  Part of the outcome was right (the order asked to *check*, and timezone changes are outside the tools of the team), but
  the judge hid that behind "hold" instead of saying "this tool cannot do it, run this command". Changes (personas only,
  `RETRIEVAL_PROMPT_VERSION` unchanged, `fleet-ops` unchanged): a provisioning-only `ops_provision_safety_critic` (the
  reused `ops_safety_critic` stays with `fleet-ops`); both critics mark each objection `[차단]` (a quoted, concrete harm)
  or `[권고]` and may not set preconditions the order did not ask for; the judge puts only `[차단]` into the plan, writes
  advice as checks, invents no prohibitions, and states what the tools cannot do with the command to run by hand; the
  expert gives one question with a recommended default. Not measured on a model yet: the next real order is the test.

## 11. Open points

- Role names and their package lists (6.2) start small; the operator confirms the first set.
- Whether "listed only" tools (bun, pipx) should be allowed at all before P6, given the weaker preview.
- Whether a failed install should offer an automatic inverse step without approval (decided no for now).
- `bun` is assumed for "burn" (chapter 4).
