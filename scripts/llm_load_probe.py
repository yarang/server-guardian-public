#!/usr/bin/env python3
"""
R0 of docs/design/remote-llm-hosts.md: is the hub loaded, and are the LLM CLI processes the main share of that load?

Runs on the hub, standard library only. It reads /proc, the debate events and the inventory; it writes only into --out
(default ~/guardian-eval/r0, an evaluation directory, never the production data dir) and spends no model quota.

    llm_load_probe.py sample  [--out DIR] [--interval 5] [--duration 48h]
    llm_load_probe.py report  [--out DIR] [--json]
    llm_load_probe.py history [--data-dir DIR] [--days 30] [--json]
    llm_load_probe.py hosts   [--inventory FILE] [--json]

sample   appends one JSON line per interval to DIR/samples-<UTC date>.jsonl: hub CPU (/proc/stat), memory, PSI, and the
         CPU ticks and RSS of four process classes:
           llm        an LLM CLI (claude, codex, agy) started by the engine: what remote hosts would take over
           retrieval  repo_mcp.py and its children: stays on the hub (it reads the code)
           engine     guardian.py and every other child of it: stays on the hub
           other      everything else (Gitea, the Go agent, an operator's own claude session, ...)
         A process is classed by the first of itself and its ancestors that is one of the above. CPU of a child that
         exited between two samples reaches its parent's cutime/cstime; the part not seen before is credited to the
         exited children's class when they all had the same one, else to the parent's.
report   summarises the samples and prints the R0 verdict (DECISION_RULE below, fixed before the data is read).
history  agent turns from <data dir>/debate-jobs/*/events.jsonl (agent_finished: duration, provider, fallback) and how
         many ran at the same time.
hosts    CLI versions and capacity of the Tier-1 hosts of the inventory, over the hub's fleet SSH (read-only commands).
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import re
import signal
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Optional, Tuple

CLASSES = ("llm", "retrieval", "engine", "other")
LLM_NAMES = {"claude", "codex", "agy"}
INTERPRETERS = {"node", "nodejs", "bun", "python", "python3", "sh", "bash"}
ENGINE_SCRIPTS = {"guardian.py"}
RETRIEVAL_SCRIPTS = {"repo_mcp.py"}
# Same as llm_runner.CLAUDE_RECORDED_PROMPT_VERSION (test_llm_load_probe.py checks it): older claude is not usable.
CLAUDE_MIN_VERSION = (2, 1, 265)

# The R0 decision rule (docs/design/remote-llm-hosts.md, "R0 procedure"). Go needs pressure AND share.
DECISION_RULE: Dict[str, float] = {
    "min_busy_samples": 360,     # at least 30 minutes of debates at the default 5 s interval
    "cpu_util_p95": 0.70,        # pressure: hub CPU busy at the 95th percentile of busy samples
    "mem_available_min": 0.15,   # pressure: lowest MemAvailable as a fraction of MemTotal
    "psi_cpu_some_p95": 20.0,    # pressure: /proc/pressure/cpu some avg10, 95th percentile
    "psi_mem_some_p95": 10.0,    # pressure: /proc/pressure/memory some avg10, 95th percentile
    "llm_cpu_share": 0.50,       # share: llm ticks / hub busy ticks over the busy samples
    "llm_mem_share_p95": 0.50,   # share: llm RSS / used memory (total - available), 95th percentile
}


# --------------------------------------------------------------------------- /proc


@dataclass
class Proc:
    pid: int
    ppid: int
    comm: str
    argv: List[str]
    start: int          # starttime in clock ticks since boot: tells a reused pid apart
    self_ticks: int     # utime + stime
    child_ticks: int    # cutime + cstime (reaped children)
    rss: int            # bytes


def parse_stat(text: str) -> Tuple[str, List[str]]:
    """(comm, fields from field 3 on) of a /proc/<pid>/stat line; comm may hold spaces and parentheses."""
    head, rest = text.rsplit(")", 1)
    return head.split("(", 1)[1], rest.split()


def read_procs(root: Path = Path("/proc"), page_size: Optional[int] = None) -> Dict[int, Proc]:
    page = page_size or os.sysconf("SC_PAGE_SIZE")
    procs: Dict[int, Proc] = {}
    for entry in root.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            comm, f = parse_stat((entry / "stat").read_text(encoding="utf-8", errors="replace"))
            raw = (entry / "cmdline").read_bytes()
        except (OSError, IndexError, ValueError):
            continue  # the process exited while we read it
        argv = [a.decode("utf-8", "replace") for a in raw.split(b"\0") if a]
        try:
            procs[int(entry.name)] = Proc(
                pid=int(entry.name), ppid=int(f[1]), comm=comm, argv=argv, start=int(f[19]),
                self_ticks=int(f[11]) + int(f[12]), child_ticks=int(f[13]) + int(f[14]), rss=int(f[21]) * page)
        except (IndexError, ValueError):
            continue
    return procs


def _stem(path: str) -> str:
    name = os.path.basename(path)
    for ext in (".js", ".mjs", ".cjs"):
        if name.endswith(ext):
            return name[: -len(ext)]
    return name


def own_role(p: Proc) -> Optional[str]:
    """llm / retrieval / engine when the process itself is one, else None."""
    args = p.argv or [p.comm]
    first = [os.path.basename(a) for a in args[:3]]
    if any(name in RETRIEVAL_SCRIPTS for name in first):
        return "retrieval"
    names = {_stem(args[0]), p.comm}
    if _stem(args[0]) in INTERPRETERS and len(args) > 1:
        names.add(_stem(args[1]))
    if names & LLM_NAMES or any("claude-code" in a or "@openai/codex" in a for a in args[:2]):
        return "llm"
    if any(name in ENGINE_SCRIPTS for name in first):
        return "engine"
    return None


def classify(procs: Dict[int, Proc]) -> Dict[int, str]:
    """Class of every process. An LLM CLI without an engine ancestor (an operator's own session) is "other"."""
    roles = {pid: own_role(p) for pid, p in procs.items()}
    out: Dict[int, str] = {}
    for pid in procs:
        chain: List[int] = []
        cur: Optional[int] = pid
        while cur in procs and len(chain) < 64:
            chain.append(cur)
            cur = procs[cur].ppid
        found = next((roles[c] for c in chain if roles[c]), None)
        if found == "llm":
            first_llm = next(i for i, c in enumerate(chain) if roles[c])
            if not any(roles[c] == "engine" for c in chain[first_llm + 1:]):
                found = "other"
        out[pid] = found or "other"
    return out


@dataclass
class Tracker:
    """Turns two /proc readings into the CPU ticks each class used between them."""

    prev: Dict[int, Proc] = field(default_factory=dict)
    prev_class: Dict[int, str] = field(default_factory=dict)

    def step(self, procs: Dict[int, Proc], classes: Dict[int, str]) -> Dict[str, int]:
        ticks = {c: 0 for c in CLASSES}
        same = {pid for pid, p in procs.items() if pid in self.prev and self.prev[pid].start == p.start}
        gone = [pid for pid in self.prev if pid not in same]
        # What the vanished children had used up to the last sample was counted then; parent cutime now holds all of it.
        seen_by_parent: Dict[int, int] = {}
        gone_classes: Dict[int, set] = {}
        for pid in gone:
            old = self.prev[pid]
            seen_by_parent[old.ppid] = seen_by_parent.get(old.ppid, 0) + old.self_ticks + old.child_ticks
            gone_classes.setdefault(old.ppid, set()).add(self.prev_class.get(pid, "other"))
        for pid, p in procs.items():
            cls = classes.get(pid, "other")
            if pid in same:
                old = self.prev[pid]
                d_self, d_child = p.self_ticks - old.self_ticks, p.child_ticks - old.child_ticks
            else:
                d_self, d_child = p.self_ticks, p.child_ticks
            ticks[cls] += max(0, d_self)
            residual = max(0, d_child - (seen_by_parent.get(pid, 0) if pid in same else 0))
            kinds = gone_classes.get(pid, set()) if pid in same else set()
            ticks[next(iter(kinds)) if len(kinds) == 1 else cls] += residual
        self.prev, self.prev_class = procs, classes
        return ticks


def read_cpu(root: Path = Path("/proc")) -> Tuple[int, int]:
    """(busy, total) clock ticks of all CPUs since boot."""
    values = [int(v) for v in (root / "stat").read_text().splitlines()[0].split()[1:9]]
    total = sum(values)
    return total - values[3] - values[4], total


def read_meminfo(root: Path = Path("/proc")) -> Dict[str, int]:
    out: Dict[str, int] = {}
    for line in (root / "meminfo").read_text().splitlines():
        key, _, rest = line.partition(":")
        if key in ("MemTotal", "MemAvailable", "SwapTotal", "SwapFree"):
            out[key] = int(rest.split()[0]) * 1024
    return out


def read_psi(root: Path = Path("/proc")) -> Dict[str, Optional[float]]:
    """some avg10 of cpu and memory, full avg10 of memory; None where the kernel has no PSI."""
    out: Dict[str, Optional[float]] = {"cpu_some": None, "mem_some": None, "mem_full": None}
    for name, prefix in (("cpu", "cpu"), ("memory", "mem")):
        try:
            text = (root / "pressure" / name).read_text()
        except OSError:
            continue
        for line in text.splitlines():
            kind, _, rest = line.partition(" ")
            m = re.search(r"avg10=([0-9.]+)", rest)
            if m and kind in ("some", "full"):
                out[f"{prefix}_{kind}"] = float(m.group(1))
    out.pop("cpu_full", None)
    return out


def take_sample(root: Path, tracker: Tracker, last_cpu: Tuple[int, int], dt: float) -> Tuple[Dict[str, Any], Tuple[int, int]]:
    procs = read_procs(root)
    classes = classify(procs)
    ticks = tracker.step(procs, classes)
    cpu = read_cpu(root)
    mem = read_meminfo(root)
    per: Dict[str, Dict[str, int]] = {c: {"ticks": ticks[c], "rss": 0, "procs": 0} for c in CLASSES}
    for pid, p in procs.items():
        per[classes[pid]]["rss"] += p.rss
        per[classes[pid]]["procs"] += 1
    # One turn is one top LLM process (claude's node, codex's wrapper); its own children are not counted again.
    llm_roots = sum(1 for pid, p in procs.items()
                    if classes[pid] == "llm" and classes.get(p.ppid) != "llm")
    sample = {
        "ts": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "dt": round(dt, 2),
        "cpu": {"busy": cpu[0] - last_cpu[0], "total": cpu[1] - last_cpu[1]},
        "ncpu": os.cpu_count(),
        "mem": {"total": mem.get("MemTotal", 0), "available": mem.get("MemAvailable", 0),
                "swap_used": mem.get("SwapTotal", 0) - mem.get("SwapFree", 0)},
        "psi": read_psi(root),
        "classes": per,
        "llm_turns": llm_roots,
    }
    return sample, cpu


def parse_duration(text: str) -> float:
    m = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*([smhd]?)\s*", text)
    if not m:
        raise argparse.ArgumentTypeError(f"not a duration: {text!r} (examples: 3600, 30m, 48h, 7d)")
    return float(m.group(1)) * {"": 1, "s": 1, "m": 60, "h": 3600, "d": 86400}[m.group(2)]


def run_sample(out: Path, interval: float, duration: float, root: Path = Path("/proc")) -> int:
    out.mkdir(parents=True, exist_ok=True)
    stop = {"now": False}

    def _stop(*_: Any) -> None:
        stop["now"] = True

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    tracker = Tracker()
    procs = read_procs(root)
    tracker.step(procs, classify(procs))  # baseline: the first reading writes nothing
    last_cpu, last_t = read_cpu(root), time.monotonic()
    end = last_t + duration if duration > 0 else float("inf")
    written = 0
    print(f"sampling every {interval:g}s into {out}/samples-<date>.jsonl until "
          f"{'stopped' if duration <= 0 else f'{duration / 3600:.1f} h from now'}", flush=True)
    while not stop["now"] and time.monotonic() < end:
        time.sleep(max(0.0, interval - (time.monotonic() - last_t)))
        now = time.monotonic()
        sample, last_cpu = take_sample(root, tracker, last_cpu, now - last_t)
        last_t = now
        path = out / f"samples-{sample['ts'][:10]}.jsonl"
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(sample, separators=(",", ":")) + "\n")
        written += 1
    print(f"wrote {written} samples", flush=True)
    return 0


# --------------------------------------------------------------------------- report


def percentile(values: List[float], q: float) -> Optional[float]:
    if not values:
        return None
    ordered = sorted(values)
    k = (len(ordered) - 1) * q
    lo = int(k)
    hi = min(lo + 1, len(ordered) - 1)
    return ordered[lo] + (ordered[hi] - ordered[lo]) * (k - lo)


def load_samples(out: Path) -> List[Dict[str, Any]]:
    samples: List[Dict[str, Any]] = []
    for path in sorted(out.glob("samples-*.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                samples.append(json.loads(line))
            except ValueError:
                continue  # a line cut by a stop in the middle of a write
    return samples


def summarise(samples: List[Dict[str, Any]], rule: Dict[str, float] = DECISION_RULE) -> Dict[str, Any]:
    # Busy: a turn was running at the sample, or one used CPU in the interval before it (it exited in between).
    busy = [s for s in samples
            if (s.get("llm_turns", 0) > 0 or s["classes"]["llm"]["ticks"] > 0) and s["cpu"]["total"] > 0]
    result: Dict[str, Any] = {"samples": len(samples), "busy_samples": len(busy), "rule": dict(rule)}
    if samples:
        result["span"] = [samples[0]["ts"], samples[-1]["ts"]]
    if not busy:
        result["verdict"] = "insufficient"
        result["reasons"] = ["no sample with a running LLM CLI turn"]
        return result
    util = [s["cpu"]["busy"] / s["cpu"]["total"] for s in busy]
    hub_busy = sum(s["cpu"]["busy"] for s in busy)
    shares = {c: (sum(s["classes"][c]["ticks"] for s in busy) / hub_busy if hub_busy else 0.0) for c in CLASSES}
    shares["unattributed"] = max(0.0, 1.0 - sum(shares.values()))
    used = [max(1, s["mem"]["total"] - s["mem"]["available"]) for s in busy]
    mem_share = {c: [s["classes"][c]["rss"] / u for s, u in zip(busy, used)] for c in CLASSES}
    avail_frac = [s["mem"]["available"] / s["mem"]["total"] for s in samples if s["mem"]["total"]]
    psi = {k: [s["psi"][k] for s in busy if s.get("psi", {}).get(k) is not None] for k in ("cpu_some", "mem_some", "mem_full")}
    result.update({
        "cpu_util": {"p50": percentile(util, 0.5), "p95": percentile(util, 0.95), "max": max(util)},
        "cpu_share": shares,
        "mem_share_p95": {c: percentile(v, 0.95) for c, v in mem_share.items()},
        "llm_rss_max": max(s["classes"]["llm"]["rss"] for s in busy),
        "mem_available_min": min(avail_frac) if avail_frac else None,
        "psi_p95": {k: percentile(v, 0.95) for k, v in psi.items()},
        "llm_turns": {"p50": percentile([s["llm_turns"] for s in busy], 0.5),
                      "p95": percentile([s["llm_turns"] for s in busy], 0.95),
                      "max": max(s["llm_turns"] for s in busy)},
    })
    result.update(verdict(result, rule))
    return result


def verdict(r: Dict[str, Any], rule: Dict[str, float]) -> Dict[str, Any]:
    if r["busy_samples"] < rule["min_busy_samples"]:
        return {"verdict": "insufficient",
                "reasons": [f"{r['busy_samples']} busy samples, the rule needs {int(rule['min_busy_samples'])}"]}
    pressure: List[str] = []
    if r["cpu_util"]["p95"] >= rule["cpu_util_p95"]:
        pressure.append(f"CPU p95 {r['cpu_util']['p95']:.0%} >= {rule['cpu_util_p95']:.0%}")
    if r["mem_available_min"] is not None and r["mem_available_min"] < rule["mem_available_min"]:
        pressure.append(f"MemAvailable min {r['mem_available_min']:.0%} < {rule['mem_available_min']:.0%}")
    for key, limit in (("cpu_some", "psi_cpu_some_p95"), ("mem_some", "psi_mem_some_p95")):
        value = r["psi_p95"].get(key)
        if value is not None and value >= rule[limit]:
            pressure.append(f"PSI {key} p95 {value:.1f} >= {rule[limit]:g}")
    share: List[str] = []
    if r["cpu_share"]["llm"] >= rule["llm_cpu_share"]:
        share.append(f"LLM CLI CPU share {r['cpu_share']['llm']:.0%} >= {rule['llm_cpu_share']:.0%}")
    mem_p95 = r["mem_share_p95"]["llm"]
    if mem_p95 is not None and mem_p95 >= rule["llm_mem_share_p95"]:
        share.append(f"LLM CLI memory share p95 {mem_p95:.0%} >= {rule['llm_mem_share_p95']:.0%}")
    if pressure and share:
        return {"verdict": "go", "reasons": pressure + share}
    if not pressure:
        return {"verdict": "not-needed", "reasons": ["no pressure criterion met"] + share}
    return {"verdict": "elsewhere", "reasons": pressure + ["the LLM CLIs are not the main share: remote hosts would not relieve it"]}


def _pct(value: Optional[float]) -> str:
    return "-" if value is None else f"{value:.0%}"


def print_report(r: Dict[str, Any]) -> None:
    print(f"samples {r['samples']}, busy (an LLM CLI turn ran) {r['busy_samples']}"
          + (f", {r['span'][0]} .. {r['span'][1]}" if r.get("span") else ""))
    if "cpu_util" in r:
        u = r["cpu_util"]
        print(f"hub CPU while busy: p50 {_pct(u['p50'])}, p95 {_pct(u['p95'])}, max {_pct(u['max'])}")
        print("CPU share while busy: " + ", ".join(f"{k} {_pct(v)}" for k, v in r["cpu_share"].items()))
        print("memory share p95 (of used): " + ", ".join(f"{k} {_pct(v)}" for k, v in r["mem_share_p95"].items()))
        print(f"LLM CLI RSS max {r['llm_rss_max'] / 2**20:.0f} MiB, MemAvailable min {_pct(r['mem_available_min'])}")
        print("PSI p95: " + ", ".join(f"{k} {'-' if v is None else f'{v:.1f}'}" for k, v in r["psi_p95"].items()))
        t = r["llm_turns"]
        print(f"concurrent LLM CLI turns: p50 {t['p50']:g}, p95 {t['p95']:g}, max {t['max']}")
    print(f"verdict: {r['verdict']}")
    for reason in r.get("reasons", []):
        print(f"  - {reason}")


# --------------------------------------------------------------------------- history


def _parse_ts(text: str) -> float:
    return datetime.datetime.strptime(text[:19], "%Y-%m-%dT%H:%M:%S").replace(
        tzinfo=datetime.timezone.utc).timestamp() + (float("0" + text[19:23]) if text[19:20] == "." else 0.0)


def iter_turns(jobs_dir: Path, since: float) -> Iterator[Dict[str, Any]]:
    """agent_finished and debate_started events of every job (lines are filtered before they are parsed)."""
    for events in sorted(jobs_dir.glob("*/events.jsonl")):
        try:
            handle = events.open(encoding="utf-8", errors="replace")
        except OSError:
            continue
        with handle:
            for line in handle:
                if '"agent_finished"' not in line and '"debate_started"' not in line:
                    continue
                try:
                    event = json.loads(line)
                    ts = _parse_ts(str(event["ts"]))
                except (ValueError, KeyError):
                    continue
                if ts >= since:
                    yield {"job": events.parent.name, "ts": ts, "type": event.get("type"), "data": event.get("data") or {}}


def summarise_history(events: Iterable[Dict[str, Any]]) -> Dict[str, Any]:
    turns: List[Tuple[float, float]] = []
    providers: Dict[str, int] = {}
    durations: List[float] = []
    debates = fallbacks = 0
    for e in events:
        if e["type"] == "debate_started":
            debates += 1
            continue
        if e["type"] != "agent_finished":
            continue
        if e["data"].get("fallback"):
            fallbacks += 1
            continue
        duration = float(e["data"].get("duration_ms") or 0) / 1000
        turns.append((e["ts"] - duration, e["ts"]))
        durations.append(duration)
        name = str(e["data"].get("provider") or "?")
        providers[name] = providers.get(name, 0) + 1
    edges = sorted([(s, 1) for s, _ in turns] + [(t, -1) for _, t in turns])
    level, peak, last = 0, 0, None
    at_level: Dict[int, float] = {}
    for moment, change in edges:
        if last is not None and level > 0:
            at_level[level] = at_level.get(level, 0.0) + moment - last
        level += change
        peak = max(peak, level)
        last = moment
    busy = sum(at_level.values())
    return {
        "debates": debates, "turns": len(turns), "fallback_turns": fallbacks, "providers": providers,
        "turn_seconds": {"p50": percentile(durations, 0.5), "p95": percentile(durations, 0.95),
                         "max": max(durations) if durations else None},
        "agent_hours": sum(durations) / 3600, "busy_hours": busy / 3600, "peak_concurrency": peak,
        "time_at_concurrency": {str(k): (v / busy if busy else 0.0) for k, v in sorted(at_level.items())},
    }


def print_history(h: Dict[str, Any], days: float) -> None:
    print(f"last {days:g} days: {h['debates']} debates, {h['turns']} CLI turns (+{h['fallback_turns']} built-in fallbacks)")
    print("providers: " + (", ".join(f"{k} {v}" for k, v in sorted(h["providers"].items())) or "-"))
    t = h["turn_seconds"]
    if t["p50"] is not None:
        print(f"turn duration: p50 {t['p50']:.0f}s, p95 {t['p95']:.0f}s, max {t['max']:.0f}s")
    print(f"agent time {h['agent_hours']:.1f} h over {h['busy_hours']:.1f} h with at least one turn running; "
          f"peak {h['peak_concurrency']} at once")
    print("share of that time by concurrent turns: "
          + (", ".join(f"{k}: {v:.0%}" for k, v in h["time_at_concurrency"].items()) or "-"))


# --------------------------------------------------------------------------- hosts

# Read-only: versions, CPU count, memory, load, free disk in $HOME. Nothing under ~/.claude, ~/.codex or ~/.guardian
# is opened (credentials stay untouched).
REMOTE_PROBE = r"""
PATH="$HOME/.local/bin:$HOME/.npm-global/bin:$HOME/.bun/bin:/usr/local/bin:$PATH"
for c in claude codex agy; do
  if command -v "$c" >/dev/null 2>&1; then
    v=$(timeout 20 "$c" --version 2>/dev/null </dev/null | head -n 1 | tr -d '\r')
    echo "cli_$c=${v:-unknown}"
  else
    echo "cli_$c="
  fi
done
echo "arch=$(uname -m)"
echo "ncpu=$(nproc 2>/dev/null)"
awk '/^MemTotal:/{print "mem_total_kb="$2} /^MemAvailable:/{print "mem_available_kb="$2}' /proc/meminfo
echo "loadavg=$(cut -d' ' -f1-3 /proc/loadavg)"
echo "disk_free_kb=$(df -Pk "$HOME" | awk 'NR==2{print $4}')"
"""


def parse_probe(text: str) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for line in text.splitlines():
        key, sep, value = line.partition("=")
        if sep and re.fullmatch(r"[a-z_]+", key):
            out[key] = value.strip()
    return out


def version_tuple(text: str) -> Optional[Tuple[int, ...]]:
    m = re.search(r"(\d+)\.(\d+)\.(\d+)", text or "")
    return tuple(int(g) for g in m.groups()) if m else None


def tier1_hosts(inventory: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [s for s in inventory if str(s.get("tier", "")).lower() == "tier-1" or s.get("guardian_role") == "hub"]


def probe_host(entry: Dict[str, Any], timeout: float = 60) -> Dict[str, Any]:
    hub = entry.get("guardian_role") == "hub"
    argv = ["sh", "-s"] if hub else ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", str(entry["id"]), "sh -s"]
    row: Dict[str, Any] = {"id": entry["id"], "hub": hub, "llm_slots": entry.get("llm_slots")}
    try:
        proc = subprocess.run(argv, input=REMOTE_PROBE, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {**row, "error": f"{type(exc).__name__}: {exc}"[:200]}
    if proc.returncode != 0 and not proc.stdout:
        return {**row, "error": (proc.stderr.strip() or f"exit {proc.returncode}")[-200:]}
    facts = parse_probe(proc.stdout)
    claude = version_tuple(facts.get("cli_claude", ""))
    row.update(facts)
    row["claude_usable"] = bool(claude and claude >= CLAUDE_MIN_VERSION)
    return row


def print_hosts(rows: List[Dict[str, Any]]) -> None:
    header = f"{'host':<16} {'arch':<8} {'cpu':>3} {'mem GB':>7} {'free GB':>7} {'load':<15} claude / codex / agy"
    print(header)
    for r in rows:
        name = r["id"] + (" (hub)" if r["hub"] else "")
        if "error" in r:
            print(f"{name:<16} unreachable: {r['error']}")
            continue
        total = int(r.get("mem_total_kb") or 0) / 2**20
        avail = int(r.get("mem_available_kb") or 0) / 2**20
        clis = " / ".join(r.get(f"cli_{c}") or "-" for c in ("claude", "codex", "agy"))
        old = "" if r["claude_usable"] or not r.get("cli_claude") else "  (claude too old for the runner)"
        print(f"{name:<16} {r.get('arch', '?'):<8} {r.get('ncpu', '?'):>3} {total:>7.1f} {avail:>7.1f} "
              f"{r.get('loadavg', '?'):<15} {clis}{old}")


# --------------------------------------------------------------------------- main


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="R0 of docs/design/remote-llm-hosts.md (read-only measurements)")
    sub = parser.add_subparsers(dest="cmd", required=True)
    default_out = Path("~/guardian-eval/r0").expanduser()
    p = sub.add_parser("sample", help="sample /proc into DIR/samples-<date>.jsonl")
    p.add_argument("--out", type=Path, default=default_out)
    p.add_argument("--interval", type=float, default=5.0)
    p.add_argument("--duration", type=parse_duration, default=parse_duration("48h"), help="0 = until stopped")
    p = sub.add_parser("report", help="summary of the samples and the R0 verdict")
    p.add_argument("--out", type=Path, default=default_out)
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("history", help="agent turns and their concurrency from the debate events")
    p.add_argument("--data-dir", type=Path,
                   default=Path(os.environ.get("GUARDIAN_DATA_DIR", "~/.guardian/data")).expanduser())
    p.add_argument("--days", type=float, default=30.0)
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("hosts", help="CLI versions and capacity of the Tier-1 hosts")
    p.add_argument("--inventory", type=Path,
                   default=Path(os.environ.get("GUARDIAN_SERVERS_FILE", "~/.guardian/data/servers.json")).expanduser())
    p.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    if args.cmd == "sample":
        if args.interval < 1:
            parser.error("--interval must be at least 1 second")
        return run_sample(args.out.expanduser(), args.interval, args.duration)
    if args.cmd == "report":
        result = summarise(load_samples(args.out.expanduser()))
        print(json.dumps(result, indent=2)) if args.json else print_report(result)
        return 0
    if args.cmd == "history":
        jobs = args.data_dir.expanduser() / "debate-jobs"
        if not jobs.is_dir():
            print(f"no debate-jobs directory under {args.data_dir}", file=sys.stderr)
            return 1
        result = summarise_history(iter_turns(jobs, time.time() - args.days * 86400))
        print(json.dumps(result, indent=2)) if args.json else print_history(result, args.days)
        return 0
    inventory = json.loads(args.inventory.expanduser().read_text(encoding="utf-8"))
    rows = [probe_host(entry) for entry in tier1_hosts(inventory)]
    print(json.dumps(rows, indent=2)) if args.json else print_hosts(rows)
    return 0


if __name__ == "__main__":
    sys.exit(main())
