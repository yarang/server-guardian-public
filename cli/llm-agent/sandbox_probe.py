#!/usr/bin/env python3
"""Evaluation of the sandbox probe lines (docs/design/resident-open-tools.md, O1).

  sandbox_probe.py evaluate    key=value lines on stdin -> Korean summary on stdout; exit 0 the
                               sandbox holds, 3 a check failed, 1 the probe did not finish
  sandbox_probe.py verdict     key=value lines on stdin -> the JSON --record stores on the hub, with
                               today's UTC date

scripts/probe-sandbox.sh collects the lines on the node and pipes them here, so this module runs on
the admin Mac and imports nothing from the repository (standard library only). The failure rules are
the probe table of resident-open-tools.md; the node-side scripts are scripts/lib/sandbox-probe-*.
"""

import json
import sys
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

UNIX_ALLOW_EXACT = {"/run/systemd/journal/socket", "/run/systemd/journal/stdout",
                    "/run/systemd/journal/dev-log", "/dev/log"}
UNIX_ALLOW_PREFIX = ("/run/systemd/resolve/", "/run/systemd/userdb/",
                     "/var/lib/guardian-sandbox/sock/probe/")
NODE_MS_MAX = 2000


def parse_lines(text: str) -> Dict[str, str]:
    """key=value lines; the last value wins, lines without = are ignored."""
    kv: Dict[str, str] = {}
    for line in text.splitlines():
        key, sep, value = line.partition("=")
        if sep:
            kv[key] = value
    return kv


def indexed(kv: Dict[str, str], prefix: str) -> List[Tuple[str, Dict[str, str]]]:
    """[(index, {name/state/target...}, ...)] from keys like path.3.name, sorted by index."""
    found: Dict[int, Dict[str, str]] = {}
    for key, value in kv.items():
        parts = key.split(".")
        if len(parts) == 3 and parts[0] == prefix and parts[1].isdigit():
            found.setdefault(int(parts[1]), {})[parts[2]] = value
    return [(index, found[index]) for index in sorted(found)]


def _int(kv: Dict[str, str], key: str) -> Optional[int]:
    value = kv.get(key, "")
    if value.isdigit():
        return int(value)
    return None


def _unix_allowed(path: str) -> bool:
    return path in UNIX_ALLOW_EXACT or any(path.startswith(p) for p in UNIX_ALLOW_PREFIX)


def _base_rules(kv: Dict[str, str], failures: List[str], warnings: List[str]) -> None:
    """Identity, visibility, writes, the network deny list, sockets and persistence."""
    if kv.get("sudo") != "refused":
        failures.append("sudo -n true: %s (expected refused)" % kv.get("sudo", "missing"))
    groups = kv.get("id.groups", "gsa-probe").split()
    if [g for g in groups if g != "gsa-probe"]:
        failures.append("id.groups: %s (only gsa-probe allowed)" % " ".join(groups))
    for _, row in indexed(kv, "path"):
        if row.get("state") not in ("hidden", "denied"):
            failures.append("%s is %s (expected hidden or denied)" % (row.get("name", "?"), row.get("state")))
    if kv.get("write.home") != "ok":
        failures.append("write.home: %s (the agent's own home must be writable)" % kv.get("write.home", "missing"))
    # Since O2 the daemon owns the sock dir (2750): the agent may connect but never write there.
    if kv.get("write.own_sock_dir") in (None, "ok"):
        failures.append("write.own_sock_dir: %s (the agent's own sock dir must be read-only)"
                        % kv.get("write.own_sock_dir", "missing"))
    for _, row in indexed(kv, "write"):
        if row.get("state") == "ok":
            failures.append("%s is writable (expected refused)" % row.get("name", "?"))
    for _, row in indexed(kv, "tcp"):
        if row.get("state") == "connected":
            failures.append("tcp %s connected (every probed target must be denied)" % row.get("target", "?"))
    if kv.get("dns") != "ok":
        failures.append("dns: %s (the resolver stub must work)" % kv.get("dns", "missing"))
    egress = {host: kv.get("egress.%s" % host) for host in ("api.anthropic.com", "api.openai.com")}
    if all(value != "ok" for value in egress.values()):
        failures.append("egress: no provider API reachable (%s)" % ", ".join("%s=%s" % it for it in egress.items()))
    elif any(value != "ok" for value in egress.values()):
        warnings.append("egress: %s" % ", ".join("%s=%s" % it for it in egress.items() if it[1] != "ok"))
    for _, row in indexed(kv, "unix"):
        if row.get("state") == "connected" and not _unix_allowed(row.get("path", "")):
            failures.append("unix %s connected (not on the allow list)" % row.get("path", "?"))
    for _, row in indexed(kv, "abstract"):
        if row.get("state") == "connected":
            warnings.append("abstract %s connected (cannot hide these without a network namespace)"
                            % row.get("name", "?"))
    if kv.get("own_sock") != "ok":
        failures.append("own_sock: %s (the agent's own mailbox socket must answer)" % kv.get("own_sock", "missing"))
    if kv.get("other_sock") == "connected":
        failures.append("other_sock connected (another agent's socket must be unreachable)")
    if kv.get("worker_sock") == "connected":
        failures.append("worker_sock connected (the node user's worker socket must be unreachable)")
    if kv.get("proc.foreign_visible", "0") != "0":
        failures.append("proc.foreign_visible: %s (ProtectProc=invisible should hide other users)"
                        % kv.get("proc.foreign_visible"))
    if kv.get("persist.leftover", "0") != "0":
        failures.append("persist.leftover: %s (a process outlived the unit)" % kv.get("persist.leftover"))
    if kv.get("persist.user_unit") == "started":
        failures.append("persist.user_unit started (a user unit escaped the sandbox)")
    if kv.get("persist.crontab") == "available":
        warnings.append("persist.crontab available (cron works inside the sandbox)")
    # O2: the launcher inside the unit and the wrapper's --stop (lines absent when an older
    # probe ran without them; the rules then say nothing, like the stress lines).
    if "launcher.exit" in kv or "launcher.result" in kv:
        if kv.get("launcher.exit") == "3" and kv.get("launcher.result") in ("no_credential", "cli_missing"):
            pass  # either one proves the launcher runs inside the unit
        else:
            failures.append("launcher: exit %s, %s (expected exit 3 with no_credential or cli_missing)"
                            % (kv.get("launcher.exit", "missing"), kv.get("launcher.result", "missing")))
    if "stop.exit" in kv or "stop.active" in kv:
        if kv.get("stop.exit") != "0" or kv.get("stop.active") != "none":
            failures.append("stop: exit %s, active %s (--stop must end every gsa-probe unit)"
                            % (kv.get("stop.exit", "missing"), kv.get("stop.active", "missing")))
        elif kv.get("stop.started") == "0":
            failures.append("stop: the sleep-60 turn never became active, so nothing was stopped")
    # O3's daemon view (lines absent when an older probe ran without them): every agent's work dir
    # must exist before a turn can start (systemd resolves --chdir before the launcher runs), and a
    # running worker unit must have NoNewPrivs 0 (the drop-in) and every gsa group of the node user
    # (the user-manager restart); a stopped worker is only a warning.
    for key, value in sorted(kv.items()):
        if key.startswith("daemon.workdir.") and value != "ok":
            agent = key[len("daemon.workdir."):]
            failures.append("%s %s (run scripts/install-sandbox.sh again: it must leave "
                            "/home/gsa-%s/work as the agent's 0700 directory)" % (key, value, agent))
    if "daemon.worker" in kv:
        if kv.get("daemon.worker") == "stopped":
            warnings.append("daemon.worker stopped: guardian-worker.service is not running, so its "
                            "NoNewPrivs and gsa groups were not checked")
        else:
            if kv.get("daemon.worker_nnp") != "0":
                failures.append("daemon.worker_nnp: %s (the worker unit still blocks sudo; install "
                                "deploy/worker/sandbox-sudo.conf with scripts/install-sandbox.sh and "
                                "let it restart the user manager)"
                                % kv.get("daemon.worker_nnp", "missing"))
            if kv.get("daemon.worker_groups") != "ok":
                failures.append("daemon.worker_groups: %s (restart the node user's user manager so "
                                "the worker gets every gsa group: systemctl restart user@<uid>.service)"
                                % kv.get("daemon.worker_groups", "missing"))


def _stress_rules(kv: Dict[str, str], failures: List[str], warnings: List[str]) -> None:
    """The cap runs: only applied when the stress steps ran. A cap counts only when it was seen:
    the hog ends in MemoryError or is killed (non-zero exit that is not the wrapper's refusal 64),
    the forks stop at a refused fork."""
    hog_exit = kv.get("hog.exit", "")
    if kv.get("hog.result") == "not_capped":
        failures.append("hog reached 4 GiB: MemoryMax did not cap it")
    elif kv.get("hog.result") != "memory_error" and hog_exit in ("", "0", "64"):
        failures.append("hog: no memory cap observed (exit %s)" % (hog_exit or "missing"))
    if kv.get("hog.leftover", "0") != "0":
        failures.append("hog.leftover: %s" % kv.get("hog.leftover"))
    if (_int(kv, "hog.node_ms") or 0) > NODE_MS_MAX:
        failures.append("hog.node_ms: %s (the node went unresponsive)" % kv.get("hog.node_ms"))
    if kv.get("forks.result") == "not_capped":
        failures.append("forks reached 2000 children: TasksMax did not cap it")
    elif "forks.capped_at" not in kv:
        failures.append("forks: no task cap observed (exit %s)" % (kv.get("forks.exit") or "missing"))
    if kv.get("forks.leftover", "0") != "0":
        failures.append("forks.leftover: %s" % kv.get("forks.leftover"))
    if (_int(kv, "forks.node_ms") or 0) > NODE_MS_MAX:
        failures.append("forks.node_ms: %s (the node went unresponsive)" % kv.get("forks.node_ms"))
    capped_at = _int(kv, "forks.capped_at")
    limit = _int(kv, "limits.PROBE_TASKS_MAX")
    if capped_at is not None and limit is not None and capped_at > limit:
        failures.append("forks.capped_at %d > PROBE_TASKS_MAX %d" % (capped_at, limit))
    elif capped_at is not None and limit is None:
        warnings.append("limits.PROBE_TASKS_MAX missing: cannot compare forks.capped_at")


def evaluate(kv: Dict[str, str]) -> Dict[str, Any]:
    """The verdict: {passed, finished, failures, warnings, memory_peak_mib, version, stress}."""
    failures: List[str] = []
    warnings: List[str] = []
    finished = kv.get("probe.done") == "1" and kv.get("checks.done") == "1"
    stress = kv.get("stress") or ""
    _base_rules(kv, failures, warnings)
    if stress not in ("skipped_busy", "skipped_flag"):
        _stress_rules(kv, failures, warnings)
    if not finished:
        failures.append("probe did not finish")
    peak = _int(kv, "workload.memory_peak_mib")
    return {"passed": finished and not failures, "finished": finished, "failures": failures,
            "warnings": warnings, "memory_peak_mib": peak,
            "version": kv.get("sandbox_version") or None, "stress": stress,
            "workload_rc": kv.get("workload.rc")}


def format_summary(verdict: Dict[str, Any]) -> str:
    """The Korean table scripts/probe-sandbox.sh prints."""
    lines = []
    if verdict["passed"]:
        lines.append("✅ sandbox 판정: 통과")
    else:
        lines.append("❌ sandbox 판정: 실패")
    lines.append("  완료: %s | 스트레스: %s | 버전: %s" % (
        "예" if verdict["finished"] else "아니요", verdict.get("stress") or "?", verdict.get("version") or "?"))
    lines.append("  실패 %d건 / 경고 %d건" % (len(verdict["failures"]), len(verdict["warnings"])))
    for failure in verdict["failures"]:
        lines.append("  ❌ %s" % failure)
    for warning in verdict["warnings"]:
        lines.append("  ⚠️  %s" % warning)
    peak = verdict.get("memory_peak_mib")
    lines.append("  워크로드 메모리 피크: %s" % ("%d MiB" % peak if peak is not None else "측정 안 됨"))
    if verdict.get("workload_rc") is not None:
        lines.append("  워크로드 종료 코드: %s" % verdict["workload_rc"])
    return "\n".join(lines)


def verdict_record(verdict: Dict[str, Any], today: str) -> Dict[str, Any]:
    """The shape --record stores as the inventory's `resident_sandbox` (worker_probe.record_sandbox)."""
    return {"version": verdict.get("version"), "passed": bool(verdict.get("passed")),
            "checked": today, "stress": verdict.get("stress"),
            "memory_peak_mib": verdict.get("memory_peak_mib"),
            "failures": list(verdict.get("failures") or []),
            "warnings": list(verdict.get("warnings") or [])}


def main(argv: List[str]) -> int:
    if argv == ["evaluate"]:
        verdict = evaluate(parse_lines(sys.stdin.read()))
        print(format_summary(verdict))
        if not verdict["finished"]:
            return 1
        return 0 if verdict["passed"] else 3
    if argv == ["verdict"]:
        verdict = evaluate(parse_lines(sys.stdin.read()))
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        print(json.dumps(verdict_record(verdict, today), ensure_ascii=False))
        return 0
    raise SystemExit(__doc__)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
