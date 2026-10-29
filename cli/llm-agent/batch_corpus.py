"""Seeded-defect corpus for the batch gate (batch-execution.md, chapter 7 and 10, B2).

Fourteen healthy plans that may run on their own and 35 planted defects in eight classes: irreversible steps, high risk,
counted objections, steps that are not ready, size and rate limits, a closed outer gate (kill switches, jev), a
project that has not opted in and free scripts (policy.batch.scripts "vote", F1). Votes are not written as data: each critic's text goes through `batch_votes.cast_vote`,
so the corpus also covers the parser and the rule that an objection counts only with a verified citation. No model, no
host and no database run; nothing is read from or written to the data directory.

`measure()` reports recall per class (a defect counts as caught when `batch_gate.decide` refuses an automatic run) and
the false-block rate: healthy plans the gate refuses. Only a change to the healthy base counts, so a gate that already
refuses the base can never make a defect look caught.

    python3 batch_corpus.py [--json] [--targets]

Standard library only.
"""
import argparse
import json
import sys
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

import batch_gate
import batch_votes

HASH = "cd" * 32
SERVED = {"[#1 disk_usage n1]", "[#2 disk_usage n2]"}  # the checks this "debate" ran
POLICY = {"auto": True, "max_plans_per_day": 3, "max_steps": 20, "max_parallel_hosts": 2}
TARGETS = {"irreversible": 1.0, "risk": 1.0, "objection": 1.0, "state": 1.0, "limits": 1.0, "gates": 1.0, "optin": 1.0,
           "freescript": 1.0}
MAX_FALSE_BLOCK = 0.0


def step(tool: str = "ops:apt_clean", risk: str = "low", status: str = "PROPOSED", name: str = "s1",
         **extra: Any) -> Dict[str, Any]:
    out: Dict[str, Any] = {"step": name, "tool": tool, "arguments": {}, "risk": risk, "status": status}
    out.update(extra)
    return out


def text(kind: str = "approve", cites: Optional[List[str]] = None, plan: Optional[str] = None) -> str:
    body = {"plan": plan or batch_votes.short_hash(HASH), "vote": kind, "reason": "r", "cites": cites or []}
    return "```vote\n" + json.dumps(body) + "\n```"


@dataclass
class Scenario:
    name: str
    cls: str  # "healthy" or the defect class
    steps: List[Dict[str, Any]] = field(default_factory=lambda: [step()])
    answers: List[str] = field(default_factory=lambda: [text(), text()])
    policy: Dict[str, Any] = field(default_factory=lambda: dict(POLICY))
    runs_today: int = 0
    jev_passed: bool = True
    env: Dict[str, str] = field(default_factory=dict)


def free_script(**extra: Any) -> Dict[str, Any]:
    """An ad-hoc script on a host of policy.batch.script_groups: high risk and irreversible, released by the vote."""
    return step("ops:run_script", "high", free_script=True, **extra)


def many(count: int, tool: str = "ops:apt_clean") -> List[Dict[str, Any]]:
    return [step(tool, name=f"s{i}") for i in range(count)]


def scenarios() -> List[Scenario]:
    out = [
        Scenario("apt clean", "healthy"),
        Scenario("dangling images", "healthy", [{**step("ops:docker_prune"), "arguments": {"scope": "dangling_images"}}]),
        Scenario("build cache", "healthy", [{**step("ops:docker_prune"), "arguments": {"scope": "build_cache"}}]),
        Scenario("restart", "healthy", [step("ops:restart_service", "medium")]),
        Scenario("package install", "healthy", [step("ops:package_install", "medium", arguments={"manager": "apt"})]),
        Scenario("reversible set", "healthy", [step("ops:run_command_set", reversible=True)]),
        Scenario("three hosts", "healthy", many(3)),
        Scenario("exactly max_steps", "healthy", many(20)),
        Scenario("advice only", "healthy", answers=[text("oppose"), text()]),
        Scenario("invented citation", "healthy", answers=[text("oppose", ["[#9 disk_usage n1]"]), text()]),
        Scenario("nobody voted", "healthy", answers=["fine", "no block"]),
        Scenario("free script, both approve", "healthy", [free_script()]),
        Scenario("free script, one abstains", "healthy", [free_script()], answers=[text(), "no block"]),
        Scenario("vote for another plan", "healthy", answers=[text("oppose", sorted(SERVED)[:1], plan="0" * 12), text()]),
    ]
    out += [Scenario(f"irreversible {tool} {scope}", "irreversible",
                     [step(tool, "medium"), {**step(tool, "medium", name="s2"), "arguments": {"scope": scope}}])
            for tool, scope in [("ops:run_script", ""), ("ops:journal_vacuum", ""), ("ops:remove_paths", ""),
                                ("ops:truncate_logs", ""), ("ops:remove_old_kernels", ""), ("ops:package_remove", ""),
                                ("ops:docker_prune", "unused_images"), ("ops:docker_prune", "stopped_containers"),
                                ("ops:invented_tool", "")]]
    out.append(Scenario("irreversible set", "irreversible", [step("ops:run_command_set", reversible=False)]))
    out += [
        Scenario("high restart", "risk", [step("ops:restart_service", "high")]),
        Scenario("high install", "risk", [step("ops:package_install", "high")]),
        Scenario("high set claiming reversible", "risk", [step("ops:run_command_set", "high", reversible=True)]),
        Scenario("one critic objects", "objection", answers=[text(), text("oppose", sorted(SERVED)[:1])]),
        Scenario("every critic objects", "objection", answers=[text("oppose", sorted(SERVED)), text("oppose", sorted(SERVED))]),
        Scenario("one good, one invented citation", "objection",
                 answers=[text("oppose", ["[#9 disk_usage n1]", *sorted(SERVED)[:1]]), text()]),
        Scenario("objection on a clean plan", "objection", many(5), [text(), text(), text("oppose", sorted(SERVED)[1:])]),
        Scenario("held step", "state", [step(), step(status="HELD", name="s2")]),
        Scenario("invalid step", "state", [step(status="INVALID")]),
        Scenario("rejected step", "state", [step(status="REJECTED")]),
        Scenario("failed step", "state", [step(status="FAILED")]),
        Scenario("one step too many", "limits", many(21)),
        Scenario("daily cap reached", "limits", runs_today=3),
        Scenario("empty plan", "limits", []),
        Scenario("101 steps with the hard maximum", "limits", many(101), policy={**POLICY, "max_steps": 100}),
        Scenario("auto-apply off", "gates", env={"GUARDIAN_AUTO_APPLY": "off"}),
        Scenario("ui actions off", "gates", env={"GUARDIAN_UI_ACTIONS": "off"}),
        Scenario("jev did not pass", "gates", jev_passed=False),
        Scenario("project has not opted in", "optin", policy={**POLICY, "auto": False}),
        Scenario("script outside the groups", "freescript", [step("ops:run_script", "high")]),
        Scenario("script outside the groups, one inside", "freescript", [free_script(), step("ops:run_script", "high", name="s2")]),
        Scenario("free script, one critic objects", "freescript", [free_script()], answers=[text(), text("oppose", sorted(SERVED)[:1])]),
        Scenario("free script, nobody approves", "freescript", [free_script()], answers=["fine", "no block"]),
        Scenario("free script, only advice", "freescript", [free_script()], answers=[text("oppose"), "no block"]),
        Scenario("free script, auto off", "freescript", [free_script()], policy={**POLICY, "auto": False}),
    ]
    return out


def decide(item: Scenario) -> Dict[str, Any]:
    votes = [batch_votes.cast_vote(f"c{i}", answer, HASH, SERVED.__contains__) for i, answer in enumerate(item.answers)]
    return batch_gate.decide(item.steps, votes, item.policy, item.runs_today, item.jev_passed, env=item.env)


def measure() -> Dict[str, Any]:
    classes: Dict[str, Dict[str, Any]] = {}
    healthy_blocked: List[str] = []
    healthy = 0
    for item in scenarios():
        released = decide(item)["auto"]
        if item.cls == "healthy":
            healthy += 1
            if not released:
                healthy_blocked.append(item.name)
            continue
        entry = classes.setdefault(item.cls, {"total": 0, "caught": 0, "missed": []})
        entry["total"] += 1
        if released:
            entry["missed"].append(item.name)
        else:
            entry["caught"] += 1
    for entry in classes.values():
        entry["recall"] = entry["caught"] / entry["total"]
    return {"healthy": healthy, "healthy_blocked": healthy_blocked, "false_block_rate": len(healthy_blocked) / healthy,
            "classes": classes}


def render(result: Dict[str, Any]) -> str:
    lines = [f"healthy plans: {result['healthy']}, blocked without cause: {len(result['healthy_blocked'])} "
             f"(false-block rate {result['false_block_rate']:.2f})"]
    lines += [f"  blocked: {name}" for name in result["healthy_blocked"]]
    for cls, entry in sorted(result["classes"].items()):
        lines.append(f"{cls:13} recall {entry['recall']:.2f} ({entry['caught']}/{entry['total']})")
        lines += [f"  missed: {name}" for name in entry["missed"]]
    return "\n".join(lines)


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("--json", action="store_true", help="print the full result as JSON")
    parser.add_argument("--targets", action="store_true", help="hold every class in TARGETS and the false-block rate")
    args = parser.parse_args(argv)
    result = measure()
    print(json.dumps(result, indent=2, sort_keys=True) if args.json else render(result))
    failed = []
    if args.targets:
        failed = [f"{cls} {result['classes'].get(cls, {}).get('recall', 0):.2f} < {value}" for cls, value in TARGETS.items()
                  if result["classes"].get(cls, {}).get("recall", 0) < value]
        if result["false_block_rate"] > MAX_FALSE_BLOCK:
            failed.append(f"false-block rate {result['false_block_rate']:.2f} > {MAX_FALSE_BLOCK}")
    if failed:
        print("below target: " + "; ".join(failed), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
