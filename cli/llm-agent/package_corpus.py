"""Seeded-defect corpus for the package action gates (fleet-provisioning.md, P5).

Ten healthy package actions and 30 planted defects (five classes of six): mixed-OS fleets, protected removals, cascade
removals, stale simulations and missing sudo. Every scenario carries the host facts, the roles, the simulation the host
printed when the action was proposed and, for the stale class, what the host says when the action is about to run.
`decide()` runs the gates in the order the hub does: protection and `ops_packages.check` (validation), `assess` over the
simulation (measurement), then validation, measurement and `ops_actions.drift` again at run time. No model, no host, no
package manager runs; nothing is read from or written to the data directory.

`measure()` reports recall per class (a defect counts as caught when the gate blocks it, or rates it high where that is
the defence, and says why in the words the class expects) and the false-block rate: healthy actions that are blocked or
rated high without cause. Only a change to the healthy base counts, so a gate that already refuses the base can never
make a defect look caught.

    python3 package_corpus.py [--json] [--targets] [--min-recall CLASS=0.8 ...] [--max-false-block 0.0]

Standard library only.
"""
import argparse
import json
import os
import re
import sys
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from typing import Any, Dict, Iterator, List, Optional, Sequence, Tuple

import ops_actions
import ops_packages as pk
import package_roles

INSTALL, REMOVE = pk.INSTALL, pk.REMOVE
# What the operator has enabled: the managers that have never run on a real host are opt-in (UNVERIFIED).
ALLOW_UNVERIFIED = "bun,zypper,apk,pacman"

# Facts as ops_facts.parse_host_facts returns them, with the owner fields the derived protection reads.
def host(os_id: str, managers: Dict[str, str], os_family: str = "linux", **kw: Any) -> Dict[str, Any]:
    base: Dict[str, Any] = {"os_family": os_family, "os_id": os_id, "os_version": "1", "arch": "arm64", "init": "systemd",
                            "root": False, "sudo_nopasswd": True, "complete": True, "managers": managers,
                            "services": [], "ports": [], "ports_owner_visibility": "full"}
    base.update(kw)
    return base


UBUNTU = host("ubuntu", {"apt-get": "/usr/bin/apt-get", "npm": "/usr/bin/npm"})
FEDORA = host("fedora", {"dnf": "/usr/bin/dnf"})
SUSE = host("opensuse-leap", {"zypper": "/usr/bin/zypper"})
ALPINE = host("alpine", {"apk": "/sbin/apk"}, root=True, sudo_nopasswd=False)
ARCH = host("arch", {"pacman": "/usr/bin/pacman"})
MAC = host("macos", {"brew": "/opt/homebrew/bin/brew", "npm": "/opt/homebrew/bin/npm"}, os_family="macos", sudo_nopasswd=False)
NO_SUDO: Dict[str, Any] = {"root": False, "sudo_nopasswd": False}
NGINX = {"unit": "nginx.service", "package": "nginx"}


def sections(installed: str = "", simulation: str = "", code: int = 0, services: str = "") -> str:
    return f"@@installed\n{installed}\n@@simulation\n{simulation}\n@@exit {code}\n@@services\n{services}\n"


APT_INSTALL = ("Inst libhwloc15 (2.10.0-1build1 Ubuntu:24.04/noble [arm64])\n"
               "Inst htop (3.3.0-4build1 Ubuntu:24.04/noble [arm64])\n")
APT_REMOVE = "Remv htop [3.3.0-4build1]\nRemv libhwloc15 [2.10.0-1build1]\n"
APT_REMOVE_SSH = "Remv openssh-server [1:9.6p1-3ubuntu13]\nRemv ubuntu-server [1.539]\n"
APT_REMOVE_SIX = "".join(f"Remv libfoo{n} [1.{n}]\n" for n in range(6))
APT_MISSING = "E: Unable to locate package htop\n"
APT_INSTALLED = "htop 3.3.0-4build1 ii \nlibhwloc15 2.10.0-1build1 ii \n"
DNF_INSTALL = """Dependencies resolved.
================================================================================
 Package           Architecture   Version               Repository         Size
================================================================================
Installing:
 htop              x86_64         3.3.0-1.fc40          fedora            180 k
Installing dependencies:
 hwloc-libs        x86_64         2.10.0-1.fc40         fedora            200 k

Transaction Summary
================================================================================
Install  2 Packages

Operation aborted.
"""
DNF_REMOVE = """Dependencies resolved.
================================================================================
 Package           Architecture   Version               Repository         Size
================================================================================
Removing:
 htop              x86_64         3.3.0-1.fc40          @fedora           400 k

Transaction Summary
================================================================================
Remove  1 Package

Operation aborted.
"""
DNF_REMOVE_SYSTEMD = DNF_REMOVE.replace("\nTransaction Summary", """Removing dependent packages:
 systemd-resolved  x86_64         255.4-1.fc40          @fedora           800 k

Transaction Summary""")
DNF_INSTALLED = "htop 3.3.0-1.fc40\n"
BREW_INSTALL = "==> Would install 1 formula:\ntree\n==> Would install 1 dependency:\nlibfoo 1.2\n"
NPM_INSTALL = "add cowsay 1.6.0\nadd emoji-regex 8.0.0\n"
NPM_INSTALL_REMOVES = NPM_INSTALL + "remove old-dep 1.0.0\n"
APK_INSTALL = "(1/2) Installing libhwloc (2.10.0-r0)\n(2/2) Installing htop (3.3.0-r0)\nOK: 12 MiB in 30 packages\n"
PACMAN_INSTALL = "hwloc 2.10.0-1\nhtop 3.3.0-1\n"
PACMAN_REMOVE_BLOCKED = ("error: failed to prepare transaction (could not satisfy dependencies)\n"
                         ":: mpv: removing ffmpeg breaks dependency 'ffmpeg'\n")


@dataclass(frozen=True)
class Scenario:
    """One package action as the gates see it. `later_*` is the host at run time (None: unchanged since the proposal)."""
    ident: str
    facts: Optional[Dict[str, Any]]
    tool: str
    args: Dict[str, Any]
    simulation: str
    roles: Tuple[str, ...] = ()
    project_extra: Tuple[str, ...] = ()
    allow_unverified: str = ALLOW_UNVERIFIED
    later_facts: Optional[Dict[str, Any]] = None
    later_roles: Optional[Tuple[str, ...]] = None
    later_simulation: Optional[str] = None
    risk: str = "medium"  # what the healthy action is rated


def act(manager: str, *names: str, **extra: Any) -> Dict[str, Any]:
    return {"target_host": "node-1", "manager": manager, "packages": [{"name": n} for n in names], **extra}


HEALTHY: Dict[str, Scenario] = {s.ident: s for s in [
    Scenario("apt-install", UBUNTU, INSTALL, act("auto", "htop"), sections("", APT_INSTALL)),
    Scenario("dnf-install", FEDORA, INSTALL, act("dnf", "htop"), sections("", DNF_INSTALL, 1)),
    Scenario("apt-remove", UBUNTU, REMOVE, act("apt", "htop"), sections(APT_INSTALLED, APT_REMOVE)),
    Scenario("dnf-remove", FEDORA, REMOVE, act("auto", "htop"), sections(DNF_INSTALLED, DNF_REMOVE, 1)),
    Scenario("brew-install", MAC, INSTALL, act("brew", "tree"), sections("", BREW_INSTALL)),
    Scenario("npm-install-no-sudo", host("ubuntu", {"apt-get": "/usr/bin/apt-get", "npm": "/usr/bin/npm"}, **NO_SUDO),
             INSTALL, act("npm", "cowsay"), sections("", NPM_INSTALL)),
    Scenario("apk-install", ALPINE, INSTALL, act("apk", "htop"), sections("", APK_INSTALL)),
    Scenario("pacman-install", ARCH, INSTALL, act("pacman", "htop"), sections("", PACMAN_INSTALL)),
    # a web host with nginx running: removing something unrelated must not be refused
    Scenario("apt-remove-on-web-host", host("ubuntu", UBUNTU["managers"], services=[NGINX],
                                            ports=[{"listen": "0.0.0.0:80", "process": "nginx", "package": "nginx"}]),
             REMOVE, act("apt", "htop"), sections(APT_INSTALLED, APT_REMOVE, services="nginx.service\n"), roles=("web",)),
    # unchanged between proposal and run: the drift check passes it
    Scenario("apt-install-rechecked", UBUNTU, INSTALL, act("apt", "htop"), sections("", APT_INSTALL),
             later_simulation=sections("", APT_INSTALL)),
]}


@dataclass(frozen=True)
class Defect:
    """A change to one healthy scenario. `changes` are Scenario fields; `how` is "block" (the action must be refused,
    `needle` names why) or "high" (it must stay possible but be rated high)."""
    ident: str
    cls: str
    base: str
    changes: Dict[str, Any] = field(default_factory=dict)
    needle: str = ""
    how: str = "block"


def later(**kw: Any) -> Dict[str, Any]:
    return kw


DEFECTS: List[Defect] = [
    # mixed-OS fleets: the model proposes a manager the host does not have, or one the operator has not enabled
    Defect("mos-apt-on-fedora", "mixed-os", "dnf-install", {"args": act("apt", "htop")}, "apt is not installed"),
    Defect("mos-brew-on-linux", "mixed-os", "apt-install", {"args": act("brew", "tree")}, "brew is not installed"),
    Defect("mos-dnf-on-mac", "mixed-os", "brew-install", {"args": act("dnf", "tree")}, "dnf is not installed"),
    Defect("mos-unverified-apk", "mixed-os", "apk-install", {"allow_unverified": ""}, "not been verified"),
    Defect("mos-brew-as-root", "mixed-os", "brew-install", {"facts": {**MAC, "root": True}}, "does not run as root"),
    Defect("mos-purge-on-dnf", "mixed-os", "dnf-remove", {"args": act("dnf", "htop", purge=True)}, "purge applies to apt only"),
    # protected removals: the package is protected by the built-in list, a role, what runs, or the project
    Defect("prot-builtin-ssh", "protected", "apt-remove", {"args": act("apt", "openssh-server")}, "is protected"),
    Defect("prot-role-web", "protected", "apt-remove", {"args": act("apt", "nginx"), "roles": ("web",)}, "role web"),
    Defect("prot-running-service", "protected", "apt-remove",
           {"facts": host("ubuntu", UBUNTU["managers"], services=[{"unit": "redis-server.service", "package": "redis-server"}]),
            "args": act("apt", "redis-server")}, "running redis-server.service"),
    Defect("prot-listening-port", "protected", "apt-remove",
           {"facts": host("ubuntu", UBUNTU["managers"],
                          ports=[{"listen": "0.0.0.0:5432", "process": "postgres", "package": "postgresql-16"}]),
            "args": act("apt", "postgresql-16")}, "listening on 0.0.0.0:5432"),
    Defect("prot-project-policy", "protected", "apt-remove", {"project_extra": ("htop",)}, "project policy"),
    Defect("prot-blind-spot", "protected", "apt-remove",
           {"facts": host("ubuntu", UBUNTU["managers"], ports=[{"listen": "0.0.0.0:8443", "process": "unknown"}])},
           how="high"),
    # cascade removals: the simulation shows what else would go
    Defect("casc-dragged-ssh", "cascade", "apt-remove",
           {"args": act("apt", "ubuntu-server"), "simulation": sections("ubuntu-server 1.539 ii", APT_REMOVE_SSH)},
           "would remove protected packages"),
    Defect("casc-dnf-systemd", "cascade", "dnf-remove", {"simulation": sections(DNF_INSTALLED, DNF_REMOVE_SYSTEMD, 1)},
           "would remove protected packages"),
    Defect("casc-project-protected", "cascade", "apt-remove", {"project_extra": ("libhwloc15",)},
           "would remove protected packages"),
    Defect("casc-pacman-breaks-dependency", "cascade", "pacman-install",
           {"tool": REMOVE, "simulation": sections("htop 3.3.0-1", PACMAN_REMOVE_BLOCKED, 1)}, "could not satisfy dependencies"),
    Defect("casc-many-removals", "cascade", "apt-remove", {"simulation": sections(APT_INSTALLED, APT_REMOVE_SIX)}, how="high"),
    Defect("casc-install-removes", "cascade", "apt-install",
           {"simulation": sections("", APT_INSTALL + "Remv libold [1.0]\n")}, how="high"),
    # stale simulations: the host changed between the proposal and the run
    Defect("stale-new-removal", "stale", "apt-install-rechecked",
           {"later_simulation": sections("", APT_INSTALL + "Remv libold [1.0]\n")}, "also act on remove:libold"),
    Defect("stale-now-protected", "stale", "apt-remove",
           {"later_simulation": sections(APT_INSTALLED, APT_REMOVE + "Remv openssh-server [1:9.6p1]\n")},
           "would remove protected packages"),
    Defect("stale-package-gone", "stale", "apt-remove", {"later_simulation": sections("", APT_REMOVE)}, "is installed on this host"),
    Defect("stale-role-added", "stale", "apt-remove",
           {"args": act("apt", "nginx"), "simulation": sections("nginx 1.24.0-2 ii", "Remv nginx [1.24.0-2]\n"),
            "later_simulation": sections("nginx 1.24.0-2 ii", "Remv nginx [1.24.0-2]\n"), "later_roles": ("web",)}, "role web"),
    Defect("stale-service-started", "stale", "apt-remove",
           {"later_facts": host("ubuntu", UBUNTU["managers"], services=[{"unit": "htop.service", "package": "htop"}])},
           "running htop.service"),
    Defect("stale-simulation-fails", "stale", "apt-install-rechecked", {"later_simulation": sections("", APT_MISSING, 100)},
           "Unable to locate package"),
    # missing sudo: the login is neither root nor allowed passwordless sudo
    Defect("sudo-apt-install", "sudo", "apt-install", {"facts": host("ubuntu", UBUNTU["managers"], **NO_SUDO)}, "sudo_required"),
    Defect("sudo-dnf-remove", "sudo", "dnf-remove", {"facts": host("fedora", FEDORA["managers"], **NO_SUDO)}, "sudo_required"),
    Defect("sudo-pacman-install", "sudo", "pacman-install", {"facts": host("arch", ARCH["managers"], **NO_SUDO)}, "sudo_required"),
    Defect("sudo-zypper-install", "sudo", "apt-install",
           {"facts": host("opensuse-leap", SUSE["managers"], **NO_SUDO), "args": act("zypper", "htop")}, "sudo_required"),
    Defect("sudo-no-facts", "sudo", "apt-install", {"facts": None}, "host facts are not available"),
    Defect("sudo-lost-before-run", "sudo", "apt-install-rechecked",
           {"later_facts": host("ubuntu", UBUNTU["managers"], **NO_SUDO)}, "sudo_required"),
]

TARGETS: Dict[str, float] = {"mixed-os": 1.0, "protected": 1.0, "cascade": 1.0, "stale": 1.0, "sudo": 1.0}
MAX_FALSE_BLOCK = 0.0


def apply(defect: Defect) -> Scenario:
    unknown = set(defect.changes) - set(Scenario.__dataclass_fields__)
    if unknown:
        raise ValueError(f"{defect.ident}: unknown scenario field(s) {sorted(unknown)}")
    base = HEALTHY[defect.base]
    changed = replace(base, ident=defect.ident, **defect.changes)
    if changed == replace(base, ident=defect.ident):
        raise ValueError(f"{defect.ident}: changes nothing in {defect.base}")
    return changed


@dataclass(frozen=True)
class Outcome:
    blocked: bool
    stage: str = ""   # validate, measure or stale
    reason: str = ""
    risk: str = "medium"


@contextmanager
def _unverified(allowed: str) -> Iterator[None]:
    old = os.environ.get("GUARDIAN_PACKAGES_ALLOW_UNVERIFIED")
    os.environ["GUARDIAN_PACKAGES_ALLOW_UNVERIFIED"] = allowed
    try:
        yield
    finally:
        if old is None:
            os.environ.pop("GUARDIAN_PACKAGES_ALLOW_UNVERIFIED", None)
        else:
            os.environ["GUARDIAN_PACKAGES_ALLOW_UNVERIFIED"] = old


def _gates(sc: Scenario, facts: Optional[Dict[str, Any]], roles: Sequence[str], out: str) -> Tuple[Optional[Outcome], Dict[str, Any], Dict[str, Any]]:
    """Validation and measurement against one state of the host. (blocking outcome or None, arguments, measurement)."""
    try:
        protection = package_roles.protection(roles, facts, sc.project_extra)
        if protection.unknown_roles:
            return Outcome(True, "validate", f"unknown roles {protection.unknown_roles}"), {}, {}
        args = pk.check(sc.tool, sc.args, facts, protection)
    except ValueError as exc:
        return Outcome(True, "validate", str(exc)), {}, {}
    found = pk.assess(sc.tool, args, out, protection)
    found["protected"] = protection.describe(args["manager"])
    blind = protection.blind_for(args["manager"])
    if blind:
        found["blind"] = blind
    if found.get("error"):
        return Outcome(True, "measure", str(found["error"])), args, found
    if found.get("refusal"):
        return Outcome(True, "measure", str(found["refusal"])), args, found
    return None, args, found


def decide(sc: Scenario) -> Outcome:
    """What the hub does with the action: refuse it at validation or measurement, rate it, and re-check it at the run."""
    with _unverified(sc.allow_unverified):
        blocked, args, stored = _gates(sc, sc.facts, sc.roles, sc.simulation)
        if blocked:
            return blocked
        rating = pk.risk(sc.tool, args, stored)
        if sc.later_facts is None and sc.later_roles is None and sc.later_simulation is None:
            return Outcome(False, risk=rating)
        facts = sc.later_facts if sc.later_facts is not None else sc.facts
        roles = sc.later_roles if sc.later_roles is not None else sc.roles
        out = sc.later_simulation if sc.later_simulation is not None else sc.simulation
        blocked, _, now = _gates(sc, facts, roles, out)
        if blocked:
            return replace(blocked, stage="stale")
        why = ops_actions.drift(stored, now)
        if why:
            return Outcome(True, "stale", why)
        return Outcome(False, risk=rating)


def caught(defect: Defect, outcome: Outcome, base: Outcome) -> bool:
    """A defect is caught when the gate does what the defect's class expects, for the reason it expects."""
    if defect.how == "high":
        return not outcome.blocked and outcome.risk == "high" and base.risk != "high"
    return outcome.blocked and not base.blocked and bool(defect.needle) and defect.needle.lower() in outcome.reason.lower()


def measure() -> Dict[str, Any]:
    baseline = {name: decide(sc) for name, sc in HEALTHY.items()}
    wrong = sorted(name for name, out in baseline.items() if out.blocked or out.risk != HEALTHY[name].risk)
    false_blocked = sorted(name for name, out in baseline.items() if out.blocked)
    classes: Dict[str, Dict[str, Any]] = {}
    for defect in DEFECTS:
        outcome = decide(apply(defect))
        row = classes.setdefault(defect.cls, {"defects": 0, "blocked": 0, "caught": 0, "missed": [], "wrong_reason": []})
        row["defects"] += 1
        row["blocked"] += outcome.blocked or (defect.how == "high" and outcome.risk == "high")
        ok = caught(defect, outcome, baseline[defect.base])
        row["caught"] += ok
        if not ok:
            row["missed"].append(defect.ident)
            if outcome.blocked:
                row["wrong_reason"].append(f"{defect.ident}: {outcome.stage}: {outcome.reason[:80]}")
    total = sum(r["caught"] for r in classes.values())
    return {
        "healthy": len(HEALTHY),
        "false_blocked": false_blocked,
        "false_block_rate": len(false_blocked) / len(HEALTHY),
        "healthy_wrong": wrong,
        "defects": len(DEFECTS),
        "recall": total / len(DEFECTS),
        "classes": {cls: dict(row, recall=row["caught"] / row["defects"]) for cls, row in classes.items()},
    }


def render(result: Dict[str, Any]) -> str:
    lines = [f"healthy actions: {result['healthy']}  false-block rate {result['false_block_rate']:.2f} "
             f"(blocked: {', '.join(result['false_blocked']) or 'none'}; off-rating: "
             f"{', '.join(n for n in result['healthy_wrong'] if n not in result['false_blocked']) or 'none'})",
             f"defects: {result['defects']}  recall {result['recall']:.2f}", ""]
    lines.append(f"{'class':<12}{'recall':>8}{'caught':>8}  missed")
    for cls, row in result["classes"].items():
        lines.append(f"{cls:<12}{row['recall']:>8.2f}{row['caught']:>5}/{row['defects']:<3} {', '.join(row['missed']) or '-'}")
        for item in row["wrong_reason"]:
            lines.append(f"{'':<12}blocked for another reason: {item}")
    return "\n".join(lines)


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("--json", action="store_true", help="print the full result as JSON")
    parser.add_argument("--targets", action="store_true", help="hold every class in TARGETS and the false-block rate to their P5 targets")
    parser.add_argument("--min-recall", action="append", default=[], metavar="CLASS=VALUE",
                        help="exit 1 when a class stays below the value (repeatable)")
    parser.add_argument("--max-false-block", type=float, default=None, metavar="RATE",
                        help="exit 1 when more healthy actions than this share are blocked")
    args = parser.parse_args(argv)
    result = measure()
    print(json.dumps(result, indent=2, sort_keys=True) if args.json else render(result))
    failed = []
    for item in [*(f"{cls}={value}" for cls, value in TARGETS.items() if args.targets), *args.min_recall]:
        cls, _, value = item.partition("=")
        if cls not in result["classes"] or not re.fullmatch(r"[0-9.]+", value):
            parser.error(f"--min-recall expects CLASS=VALUE with a known class, got {item!r}")
        if result["classes"][cls]["recall"] < float(value):
            failed.append(f"{cls} {result['classes'][cls]['recall']:.2f} < {value}")
    limit = MAX_FALSE_BLOCK if args.targets and args.max_false_block is None else args.max_false_block
    if limit is not None and result["false_block_rate"] > limit:
        failed.append(f"false-block rate {result['false_block_rate']:.2f} > {limit}")
    if failed:
        print("below target: " + "; ".join(failed), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
