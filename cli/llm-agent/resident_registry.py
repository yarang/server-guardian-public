"""Declarations of resident agents and the operator's registration checks (resident-agents.md, M1).

The declaration file <GUARDIAN_DATA_DIR>/resident-agents.json names the agents; it does not make
one usable (binding is M2). add_agent re-runs the design's checks (name, node and protocol 3, the
M0 provider table, the character, the peers, the node's memory budget) and refuses with a reason
each time one fails. A hand-edited entry that fails the static rules is skipped on load, its
problem is returned (and written as an event when a store exists) and the other entries keep
running. The file is written atomically and stamped with `modified`.

A declaration may instead say `tools: "open"` (resident-open-tools.md O2): the agent then keeps its
built-in tools and runs inside the node's sandbox, so add_agent also checks the provider (claude
or agy only), a name the sandbox user gsa-<name> can carry, the node's sandbox probe verdict at
exactly this release's sandbox version, and the open memory figure. The key is not written for
closed agents, so existing declaration files stay byte-identical.

Reads create nothing: list_agents() and load_declarations() on an empty data dir leave it empty.
"""

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import mail_store
import sandbox_info

DECLARATION_FORMAT = 1
RESERVED_NAMES = ("operator", "hub")
PROTOCOL_MIN = 3
# C8 of the O2 shared contract + PD1 (resident-codex-and-policy.md): providers allowed with
# tools: "open" (codex stays resident: false for closed tools, open_resident: true for open).
OPEN_PROVIDERS = ("claude", "agy", "codex")
SANDBOX_REPROBE = "re-run scripts/probe-sandbox.sh"


class RegistrationError(ValueError):
    """A registration check refused the change; the message is the reason shown to the operator."""


def declaration_path() -> Path:
    return mail_store.data_dir() / "resident-agents.json"


def providers_table_path() -> Path:
    configured = os.environ.get("GUARDIAN_RESIDENT_PROVIDERS")
    if configured:
        return Path(configured).expanduser()
    return Path(__file__).resolve().parents[2] / "configs" / "resident-providers.json"


def requirements_path() -> Path:
    return Path(__file__).resolve().parents[2] / "configs" / "worker-requirements.json"


def load_table(table: Any = None) -> Dict[str, Any]:
    """The M0 table: a dict or Path passed straight through (tests), else the repository file."""
    if isinstance(table, dict):
        return table
    path = Path(table) if table is not None else providers_table_path()
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RegistrationError(f"cannot read the resident provider table {path}: {exc}") from exc


def _entry_address(entry: Dict[str, Any]) -> str:
    return f"{entry['name']}@{entry['node']}"


def _effective_peers(entry: Dict[str, Any], addresses: Sequence[str]) -> List[str]:
    peers = entry.get("peers")
    if peers is None:
        return [a for a in addresses if a != entry["address"]]
    return list(peers)


def _resolve_peer(name: str, known: Dict[str, List[str]]) -> str:
    """A full address passes as is; a bare name must name exactly one declared agent."""
    if "@" in name:
        if name not in {a for addresses in known.values() for a in addresses}:
            raise RegistrationError(f"peer {name} is not a declared agent")
        return name
    matches = known.get(name) or []
    if not matches:
        raise RegistrationError(f"peer {name} is not a declared agent")
    if len(matches) > 1:
        raise RegistrationError(f"ambiguous peer {name}: {', '.join(sorted(matches))}")
    return matches[0]


def _entry_problem(entry: Any, table: Dict[str, Any]) -> Optional[str]:
    """The static rules a loaded entry must pass; the peer resolution happens in a second pass."""
    if not isinstance(entry, dict):
        return "entry is not an object"
    name, node = entry.get("name"), entry.get("node")
    if not isinstance(name, str) or not mail_store.NAME_RE.fullmatch(name):
        return f"bad name {name!r}"
    if name in RESERVED_NAMES:
        return f"name {name} is reserved"
    if not isinstance(node, str) or not mail_store.NODE_RE.fullmatch(node):
        return f"bad node {node!r}"
    tools = entry.get("tools")
    if tools not in (None, "closed", "open"):
        return f"bad tools {tools!r}: use closed or open"
    provider = table.get("providers", {}).get(entry.get("provider"))
    if tools == "open":
        # The static open rules (the inventory verdict and the budget stay with add_agent; the
        # relay re-checks the verdict on every agents frame it builds).
        if entry.get("provider") not in OPEN_PROVIDERS:
            return f"provider {entry.get('provider')!r} cannot run with open tools (claude, agy or codex only)"
        if not sandbox_info.SANDBOX_NAME_RE.fullmatch(name):
            return f"bad name {name!r} for an open agent: use ^[a-z][a-z0-9]{{0,15}}$"
        if not isinstance(provider, dict) or (provider.get("resident") is not True
                                              and provider.get("open_resident") is not True):
            return f"provider {entry.get('provider')!r} is not allowed as an open resident agent (M0 table)"
        if provider.get("resident") is True:
            driver = entry.get("driver") or provider.get("default_driver")
            driver_entry = (provider.get("drivers") or {}).get(driver)
            if not isinstance(driver_entry, dict) or driver_entry.get("status") != "pass":
                status = driver_entry.get("status") if isinstance(driver_entry, dict) else "absent"
                return f"driver {driver!r} for {entry.get('provider')} is {status} in the M0 table"
        else:
            allowed_driver = provider.get("open_driver") or "headless"
            driver = entry.get("driver") or allowed_driver
            if driver != allowed_driver:
                return f"driver {driver!r} for open {entry.get('provider')} must be {allowed_driver!r}"
    else:
        if not isinstance(provider, dict) or provider.get("resident") is not True:
            return f"provider {entry.get('provider')!r} is not allowed as a resident agent (M0 table)"
        driver = entry.get("driver") or provider.get("default_driver")
        driver_entry = (provider.get("drivers") or {}).get(driver)
        if not isinstance(driver_entry, dict) or driver_entry.get("status") != "pass":
            status = driver_entry.get("status") if isinstance(driver_entry, dict) else "absent"
            return f"driver {driver!r} for {entry.get('provider')} is {status} in the M0 table"
    if entry.get("peers") is not None and not isinstance(entry.get("peers"), list):
        return "peers must be a list of addresses"
    return None


def load_declarations(path: Optional[Path] = None,
                      table: Any = None) -> Tuple[List[Dict[str, Any]], List[str]]:
    """The valid entries (peers resolved to full addresses) and the problems of the skipped ones."""
    path = path or declaration_path()
    if not path.is_file():
        return [], []
    table = load_table(table)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return [], [f"{path.name}: cannot read ({exc})"]
    if not isinstance(data, dict) or data.get("format") != DECLARATION_FORMAT or \
            not isinstance(data.get("agents"), list):
        return [], [f"{path.name}: expected format {DECLARATION_FORMAT} with an agents list"]
    problems: List[str] = []
    checked: List[Dict[str, Any]] = []
    seen: Dict[str, List[str]] = {}
    for raw in data["agents"]:
        problem = _entry_problem(raw, table)
        if problem is None and _entry_address(raw) in {a for v in seen.values() for a in v}:
            problem = f"duplicate agent {_entry_address(raw)}"
        if problem:
            problems.append(f"{path.name}: {problem}")
            continue
        entry = dict(raw)
        entry["address"] = _entry_address(entry)
        checked.append(entry)
        seen.setdefault(entry["name"], []).append(entry["address"])
    entries: List[Dict[str, Any]] = []
    for entry in checked:
        try:
            peers = entry.get("peers")
            if peers is not None:
                entry["peers"] = [_resolve_peer(peer, seen) for peer in peers]
        except RegistrationError as exc:
            problems.append(f"{path.name}: {entry['address']}: {exc}")
            continue
        entries.append(entry)
    _log_problems(problems)
    return entries, problems


def _log_problems(problems: List[str]) -> None:
    """A skipped entry is named in an event when a store exists; none is created just for that."""
    if problems and mail_store.default_store_path().exists():
        mail_store.MailStore().note("declaration_problem", detail={"problems": problems})


def _write_declarations(entries: List[Dict[str, Any]], path: Optional[Path] = None) -> Path:
    """Writes the file atomically (a temp file in the same directory, then os.replace)."""
    path = path or declaration_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"format": DECLARATION_FORMAT,
               "modified": mail_store.format_ts(datetime.now(timezone.utc)),
               "agents": [{k: v for k, v in entry.items() if k != "address"} for entry in entries]}
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(tmp, path)
    return path


def _announce_peers(store: mail_store.MailStore,
                    changes: Dict[str, Tuple[List[str], List[str]]]) -> None:
    """One hub `peers_changed` notice per agent whose effective peer list changed (D9)."""
    for address, added_removed in changes.items():
        added = sorted(added_removed[0])
        removed = sorted(added_removed[1])
        body = ", ".join([*[f"added: {a}" for a in added], *[f"removed: {r}" for r in removed]])
        store.send("hub", address, "status", "peers_changed", body, op_id=None)


def _peer_diff(before: Dict[str, List[str]], after: Dict[str, List[str]],
               changed: str) -> Dict[str, Tuple[List[str], List[str]]]:
    """Which other agents' effective lists changed; the changed agent itself is not told (it gets
    the start card, or is gone)."""
    updates: Dict[str, Tuple[List[str], List[str]]] = {}
    for address, peers in after.items():
        if address == changed:
            continue
        gained = [p for p in peers if p not in before.get(address, [])]
        lost = [p for p in before.get(address, []) if p not in peers]
        if gained or lost:
            updates[address] = (gained, lost)
    return updates


def _node_entry(node: str, servers: Optional[Sequence[Dict[str, Any]]]):
    if servers is None:
        servers, error, _ = _load_servers()
        if error:
            raise RegistrationError(error)
    for entry in servers or []:
        if isinstance(entry, dict) and entry.get("id") == node:
            return entry
    raise RegistrationError(f"node {node} is not in the inventory")


def _load_servers():
    import group_manager
    return group_manager.load_servers()


def _check_node(node: str, provider: str, servers: Optional[Sequence[Dict[str, Any]]]) \
        -> Dict[str, Any]:
    entry = _node_entry(node, servers)
    if entry.get("worker") is not True:
        raise RegistrationError(f"node {node} is not a worker host (worker is not true)")
    capable = entry.get("worker_capable")
    if not isinstance(capable, dict) or capable.get("passed") is not True:
        raise RegistrationError(f"node {node}: the last worker probe did not pass")
    install = entry.get("worker_install")
    protocol = install.get("protocol") if isinstance(install, dict) else None
    if isinstance(protocol, bool) or not isinstance(protocol, int) or protocol < PROTOCOL_MIN:
        raise RegistrationError("node has no protocol 3 worker (install the M2 worker)")
    if provider not in (capable.get("clis") or []):
        raise RegistrationError(f"provider {provider} is not installed on {node}")
    return entry


def _check_driver(provider: str, driver: Optional[str], table: Dict[str, Any],
                  tools: str = "closed") -> str:
    entry = table.get("providers", {}).get(provider)
    if tools == "open":
        if not isinstance(entry, dict) or (entry.get("resident") is not True
                                           and entry.get("open_resident") is not True):
            raise RegistrationError(f"provider {provider} is not allowed as a resident agent (M0 table)")
        if entry.get("resident") is not True:
            allowed = entry.get("open_driver") or "headless"
            chosen = driver or allowed
            if chosen != allowed:
                raise RegistrationError(f"driver {chosen!r} for open {provider} must be {allowed!r}")
            return str(chosen)
    elif not isinstance(entry, dict) or entry.get("resident") is not True:
        raise RegistrationError(f"provider {provider} is not allowed as a resident agent (M0 table)")
    chosen = driver or entry.get("default_driver")
    drivers = entry.get("drivers") or {}
    driver_entry = drivers.get(chosen) if isinstance(chosen, str) else None
    if not isinstance(driver_entry, dict) or driver_entry.get("status") != "pass":
        status = driver_entry.get("status") if isinstance(driver_entry, dict) else "absent"
        raise RegistrationError(f"driver {chosen!r} for {provider} is {status} in the M0 table")
    return str(chosen)


def inventory_sandbox(node: str, servers: Optional[Sequence[Dict[str, Any]]] = None) \
        -> Optional[Dict[str, Any]]:
    """The node's `resident_sandbox` probe verdict (worker_probe.record-sandbox writes it), or None
    when the node or the verdict is absent. The relay re-reads it while it decides open agents, so
    it goes through the same loader as the registration instead of a second one."""
    try:
        entry = _node_entry(node, servers)
    except RegistrationError:
        return None
    verdict = entry.get("resident_sandbox")
    return verdict if isinstance(verdict, dict) else None


def _sandbox_version() -> str:
    """This release's sandbox version (sandbox_info, C1). A release tree without the sandbox files
    cannot vouch for any node, so it cannot register an open agent at all."""
    try:
        return sandbox_info.sandbox_version()
    except sandbox_info.SandboxVersionError as exc:
        raise RegistrationError(f"cannot compute this release's sandbox version: {exc}") from exc


def _check_open(name: str, node: str, provider: str, server: Dict[str, Any]) -> None:
    """The preconditions of a declaration with tools: "open" (O2): claude or agy, a name the
    sandbox user can carry, and the node's sandbox probe passed for exactly this release's sandbox
    version (a verdict for another version is stale)."""
    if provider not in OPEN_PROVIDERS:
        raise RegistrationError(f"provider {provider} cannot run with open tools (claude, agy or codex only)")
    if not sandbox_info.SANDBOX_NAME_RE.fullmatch(name):
        raise RegistrationError(f"bad name {name!r} for an open agent: use ^[a-z][a-z0-9]{{0,15}}$")
    expected = _sandbox_version()
    verdict = server.get("resident_sandbox")
    if not isinstance(verdict, dict) or verdict.get("passed") is not True \
            or not isinstance(verdict.get("version"), str) or not verdict.get("version"):
        raise RegistrationError(f"node {node}: no passed sandbox probe on record; "
                                f"{SANDBOX_REPROBE} {node} --record")
    if verdict.get("version") != expected:
        raise RegistrationError(f"node {node}: the sandbox verdict is for version "
                                f"{verdict.get('version')}, this release ships {expected}; "
                                f"{SANDBOX_REPROBE} {node} --record")


def _check_budget(node: str, server: Dict[str, Any], existing: Sequence[Dict[str, Any]],
                  tools: str = "closed") -> None:
    capable = server["worker_capable"]
    free = capable.get("memory_free_mib")
    if isinstance(free, bool) or not isinstance(free, int):
        raise RegistrationError(f"node {node} has no memory figure recorded; re-probe the node")
    try:
        requirements = json.loads(requirements_path().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RegistrationError(f"cannot read {requirements_path()}: {exc}") from exc
    # The resident budget figures live with the worker requirements (install step 2, M2a), not in the M0 table.
    per_instance, per_turn = requirements.get("resident_memory_mib"), requirements.get("resident_turn_memory_mib")
    if not all(isinstance(v, int) and not isinstance(v, bool) for v in (per_instance, per_turn)):
        raise RegistrationError("the worker requirements have no resident budget figure")
    # An open instance counts its own figure (MemoryMax of deploy/sandbox/guardian-sandbox-run.sh,
    # resident_open_memory_mib) instead, so the figure is only required when an open instance is
    # involved: closed registrations work with the requirements they already had.
    per_open = requirements.get("resident_open_memory_mib")
    instances = [entry for entry in existing if entry.get("node") == node]
    if (tools == "open" or any(entry.get("tools") == "open" for entry in instances)) \
            and (not isinstance(per_open, int) or isinstance(per_open, bool)):
        raise RegistrationError("the worker requirements have no open resident budget figure "
                                "(resident_open_memory_mib)")
    install = server.get("worker_install") or {}
    max_jobs = int(capable.get("max_jobs") or install.get("max_jobs")
                   or requirements["default_max_jobs"])

    def cost(entry: Dict[str, Any]) -> int:
        return per_open if entry.get("tools") == "open" else per_instance

    resident = sum(cost(entry) for entry in instances) + (per_open if tools == "open" else per_instance)
    needed = max_jobs * requirements["free_memory_mib_per_job"] + resident + per_turn
    if free < needed:
        raise RegistrationError(f"node {node} budget: {free} MiB free, {needed} MiB needed "
                                f"({max_jobs} jobs, {len(instances) + 1} resident instances, one resident turn)")


def add_agent(name: str, node: str, provider: str, *, character: Optional[str] = None,
              driver: Optional[str] = None, peers: Optional[Sequence[str]] = None,
              servers: Optional[Sequence[Dict[str, Any]]] = None, personas: Any = None,
              table: Any = None, tools: str = "closed") -> Dict[str, Any]:
    """Runs the registration checks, writes the declaration atomically, upserts the agent into the
    store and sends a `peers_changed` notice to every other agent that gained the new peer.
    `tools` is "closed" (the default; the key is then not written, so closed declarations stay
    byte-identical) or "open", which also runs the O2 preconditions (claude or agy, a sandbox-safe
    name, the node's sandbox probe passed at this release's sandbox version, the open budget)."""
    if tools not in ("closed", "open"):
        raise RegistrationError(f"bad tools {tools!r}: use closed or open")
    if not isinstance(name, str) or not mail_store.NAME_RE.fullmatch(name):
        raise RegistrationError(f"bad name {name!r}: use ^[a-z][a-z0-9-]{{0,31}}$")
    if name in RESERVED_NAMES:
        raise RegistrationError(f"name {name} is reserved")
    table = load_table(table)
    entries, _ = load_declarations(table=table)
    if any(entry["name"] == name and entry["node"] == node for entry in entries):
        raise RegistrationError(f"agent {name}@{node} is already declared")
    server = _check_node(node, provider, servers)
    chosen_driver = _check_driver(provider, driver, table, tools=tools)
    if tools == "open":
        _check_open(name, node, provider, server)
    if character is not None:
        from character import is_character
        manager = personas if personas is not None else _persona_manager()
        persona = manager.get_persona(character) if manager is not None else None
        if persona is None or not is_character(persona):
            raise RegistrationError(f"character {character} does not exist or is not a format 2 "
                                    f"character file")
    entry: Dict[str, Any] = {"name": name, "node": node, "provider": provider,
                             "driver": chosen_driver}
    if tools == "open":
        entry["tools"] = "open"
    if character is not None:
        entry["character"] = character
    if peers is not None:
        known: Dict[str, List[str]] = {}
        for other in entries:
            known.setdefault(other["name"], []).append(other["address"])
        known.setdefault(name, []).append(f"{name}@{node}")
        entry["peers"] = [_resolve_peer(peer, known) for peer in peers]
    _check_budget(node, server, entries, tools=tools)

    addresses = [e["address"] for e in entries]
    before = {e["address"]: _effective_peers(e, addresses) for e in entries}
    address = f"{name}@{node}"
    full = {**entry, "address": address}
    path = _write_declarations([*entries, entry])
    store = mail_store.MailStore()
    store.upsert_agent(full)
    detail: Dict[str, Any] = {"provider": provider, "driver": chosen_driver, "file": str(path)}
    if tools == "open":
        detail["tools"] = "open"
    store.note("agent_added", agent=address, detail=detail)
    all_addresses = [*addresses, address]
    # Every existing agent whose peers are implicit (all others) gains the new one.
    after = {e["address"]: _effective_peers(e, all_addresses) for e in [*entries, full]}
    _announce_peers(store, _peer_diff(before, after, address))
    return entry


def _persona_manager():
    from persona_manager import PersonaManager
    return PersonaManager()


def remove_agent(address: str) -> Dict[str, Any]:
    """The design's order: retiring, tasks released, mail undeliverable, then the file (the
    supervisor shutdown and the spool cleanup are M2/M3, left as an event), then peer notices."""
    entries, _ = load_declarations()
    entry = next((e for e in entries if e["address"] == address), None)
    if entry is None:
        raise RegistrationError(f"no resident agent {address}")
    addresses = [e["address"] for e in entries]
    before = {e["address"]: _effective_peers(e, addresses) for e in entries}
    store_exists = mail_store.default_store_path().exists()
    if store_exists:
        store = mail_store.MailStore()
        store.set_retiring(address)  # (1) new mail is refused with agent_retired
        store.release_agent_tasks(address, "agent_retired")  # (2)
        store.mark_undeliverable(address)  # (3) each agent sender gets one hub notice
        # (4) the supervisor sends shutdown and stops the CLI, (5) the daemon removes the spool,
        # the sockets and the isolated working directory: both are M2/M3, not built yet.
        store.note("retire_pending_node", agent=address,
                   detail={"shutdown": "M3 supervisor", "spool_cleanup": "M2 daemon"})
    remaining: List[Dict[str, Any]] = []
    for other in entries:
        if other["address"] == address:
            continue
        if address in (other.get("peers") or []):
            other["peers"] = [p for p in other["peers"] if p != address]
        remaining.append(other)
    _write_declarations(remaining)
    if store_exists:
        after = {e["address"]: _effective_peers(e, [e["address"] for e in remaining])
                 for e in remaining}
        _announce_peers(mail_store.MailStore(), _peer_diff(before, after, address))
    return {"ok": True, "removed": address}


def list_agents() -> Tuple[List[Dict[str, Any]], List[str]]:
    """Declarations joined with the store's state and epoch when the store exists; creates nothing."""
    entries, problems = load_declarations()
    rows = {row["address"]: row for row in mail_store.MailStore().agent_rows()}
    agents = []
    for entry in entries:
        row = rows.get(entry["address"])
        agents.append({**entry,
                       "state": row["state"] if row else None,
                       "epoch": row["epoch"] if row else None,
                       "session_id": row["session_id"] if row else None})
    return agents, problems
