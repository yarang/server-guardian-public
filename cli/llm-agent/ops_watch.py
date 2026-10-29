"""
Nightly watch of ops projects (docs/design/server-ops-project.md, P6).

For every ops project whose policy has a "watch" block, the read-only checks disk_usage and journal_usage run on
each target host. A filesystem above disk_percent or inode_percent, or a journal above journal_max, is a finding.
When there are findings, ONE ops debate is started through the hub API (POST /api/debates), so it shows up in the
dashboard like any other and only proposes actions: nothing here changes a host. auto_run, if the project has it,
applies to that debate's proposals exactly as to any other.

The same finding (host, kind, mount) does not start another debate for REPEAT_DAYS, so a full disk nobody cleans
raises one debate a week instead of one a night. The state lives in <data dir>/ops-watch/<project>.json.

    "policy": {"watch": {"disk_percent": 85, "inode_percent": 85, "journal_max": "3G", "headscale": true}}

With "headscale": true the check headscale_status also runs on every target that has headscale (the tailnet control
server; hosts without it are skipped). Its findings: the service is not active, the TLS certificate ends within
CERT_DAYS, a headscale* backup timer's last run failed or is older than BACKUP_MAX_HOURS, or a tagged node (a server,
not a person's device) has been offline for OFFLINE_MINUTES. The debate only reports on them: restarting headscale
is refused by ops:restart_service, and its configuration, ACL and nodes are changed by a person.

Run by guardian.py --ops-watch (the hub's server-guardian-ops-watch.timer), --dry-run reports without starting.
"""

import json
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import ops_actions
from ops_checks import OpsChecks

REPEAT_DAYS = 7
WATCH_KEYS = {"disk_percent", "inode_percent", "journal_max", "headscale"}
DEFAULT_WATCH = {"disk_percent": 85, "inode_percent": 85, "journal_max": "3G"}
JOURNAL_MAX = re.compile(r"^([1-9][0-9]{0,4})([MG])$")
# Filesystems a full disk cannot be cleaned on (read-only images, firmware variables, the boot EFI partition).
SKIP_FSTYPES = {"squashfs", "efivarfs", "vfat", "iso9660"}
CERT_DAYS = 21
BACKUP_MAX_HOURS = 48
OFFLINE_MINUTES = 60
HEADSCALE_MISSING = "headscale is not installed on this host"


def parse_watch(raw: Any) -> Optional[Dict[str, Any]]:
    """Validates policy.watch; None when absent. Raises ValueError."""
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ValueError("watch must be an object with disk_percent, inode_percent and journal_max")
    unknown = sorted(set(raw) - WATCH_KEYS)
    if unknown:
        raise ValueError(f"watch: unknown keys {unknown}; known: {sorted(WATCH_KEYS)}")
    watch = {**DEFAULT_WATCH, **raw}
    for key in ("disk_percent", "inode_percent"):
        value = watch[key]
        if isinstance(value, bool) or not isinstance(value, int) or not 50 <= value <= 99:
            raise ValueError(f"watch.{key} must be an integer from 50 to 99")
    size = str(watch["journal_max"]).strip().upper()
    if not JOURNAL_MAX.fullmatch(size):
        raise ValueError("watch.journal_max must be a size such as 500M or 3G")
    watch["journal_max"] = size
    if not isinstance(watch.get("headscale", False), bool):
        raise ValueError("watch.headscale must be true or false")
    return watch


def _size_bytes(size: str) -> int:
    number, unit = JOURNAL_MAX.fullmatch(size).groups()
    return int(number) * 1024 ** (2 if unit == "M" else 3)


def parse_df(text: str) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """(filesystems, inode lines) from the disk_usage check: df -PT, then '# inodes' and df -Pi."""
    space, inodes, part = [], [], "space"
    for line in (text or "").splitlines():
        if line.startswith("# inodes"):
            part = "inodes"
            continue
        fields = line.split()
        if not fields or fields[0] == "Filesystem":
            continue
        if part == "space" and len(fields) >= 7 and fields[5].endswith("%") and fields[5][:-1].isdigit():
            space.append({"fs": fields[0], "type": fields[1], "percent": int(fields[5][:-1]), "mount": fields[6]})
        elif part == "inodes" and len(fields) >= 6 and fields[4].endswith("%") and fields[4][:-1].isdigit():
            inodes.append({"fs": fields[0], "percent": int(fields[4][:-1]), "mount": fields[5]})
    return space, inodes


def findings_for(host: str, disk: Optional[Dict[str, Any]], journal: Optional[Dict[str, Any]],
                 watch: Dict[str, Any]) -> List[Dict[str, Any]]:
    """The thresholds a host is over, from its disk_usage and journal_usage results (None when the check failed)."""
    found: List[Dict[str, Any]] = []
    if disk and disk.get("ok"):
        space, inodes = parse_df(disk["text"])
        types = {s["fs"]: s["type"] for s in space}
        for s in space:
            if s["type"] not in SKIP_FSTYPES and s["percent"] >= watch["disk_percent"]:
                found.append({"host": host, "kind": "disk", "mount": s["mount"], "value": f"{s['percent']}%",
                              "threshold": f"{watch['disk_percent']}%", "call": disk["call"]})
        for i in inodes:
            if types.get(i["fs"]) not in SKIP_FSTYPES and i["percent"] >= watch["inode_percent"]:
                found.append({"host": host, "kind": "inodes", "mount": i["mount"], "value": f"{i['percent']}%",
                              "threshold": f"{watch['inode_percent']}%", "call": disk["call"]})
    if journal and journal.get("ok") and "take up" in journal["text"]:
        used = ops_actions.parse_size(journal["text"].split("take up", 1)[1])
        if used is not None and used > _size_bytes(watch["journal_max"]):
            found.append({"host": host, "kind": "journal", "mount": "/var/log/journal", "value": ops_actions.human(used),
                          "threshold": watch["journal_max"], "call": journal["call"]})
    return found


def _utc(text: str, fmt: str) -> Optional[datetime]:
    try:
        return datetime.strptime(text.strip(), fmt).replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def headscale_findings(host: str, result: Optional[Dict[str, Any]], now: float) -> List[Dict[str, Any]]:
    """Findings from a headscale_status result (none when the check failed or the host has no headscale)."""
    if not result or not result.get("ok") or HEADSCALE_MISSING in result["text"]:
        return []
    found: List[Dict[str, Any]] = []
    call = result["call"]
    clock = datetime.fromtimestamp(now, timezone.utc)

    def add(kind: str, mount: str, value: str, threshold: str) -> None:
        found.append({"host": host, "kind": kind, "mount": mount, "value": value, "threshold": threshold,
                      "call": call})

    for line in result["text"].splitlines():
        key, _, rest = line.partition(": ")
        if key == "service" and rest.strip() != "active":
            add("headscale_service", "headscale.service", rest.strip() or "unknown", "active")
        elif key == "certificate_not_after":
            end = _utc(rest.replace("GMT", ""), "%b %d %H:%M:%S %Y")
            if end is not None and (end - clock).days < CERT_DAYS:
                add("headscale_cert", "tls", f"{(end - clock).days} days", f"{CERT_DAYS} days")
        elif key == "backup":
            unit, _, tail = rest.partition(" ")
            state, _, stamp = tail.partition(" ")
            ran = _utc(" ".join(stamp.split()[1:3]), "%Y-%m-%d %H:%M:%S") if stamp.strip() else None
            if state != "success":
                add("headscale_backup", unit, state or "unknown", "success")
            elif ran is None or clock - ran > timedelta(hours=BACKUP_MAX_HOURS):
                add("headscale_backup", unit, f"last run {stamp.strip() or 'never'}", f"{BACKUP_MAX_HOURS}h")
        elif line.startswith("node\t"):
            fields = line.split("\t")
            if len(fields) < 6 or fields[4] != "offline" or not fields[3].startswith("tag:"):
                continue
            seen = _utc(fields[5], "%Y-%m-%dT%H:%MZ")
            if seen is None or clock - seen > timedelta(minutes=OFFLINE_MINUTES):
                add("mesh_offline", fields[1], f"offline since {fields[5]}", f"{OFFLINE_MINUTES} min")
    return found


def fingerprint(f: Dict[str, Any]) -> str:
    return f"{f['host']}|{f['kind']}|{f['mount']}"


def task_text(project_id: str, findings: List[Dict[str, Any]]) -> str:
    """The debate's task: Korean like the dashboard, so the agents answer in Korean."""
    labels = {"disk": "디스크 사용률", "inodes": "inode 사용률", "journal": "journal 크기",
              "headscale_service": "headscale 서비스 상태", "headscale_cert": "headscale 인증서 남은 기간",
              "headscale_backup": "headscale 백업", "mesh_offline": "메시 노드 오프라인"}
    lines = [f"야간 점검에서 {project_id} 프로젝트의 서버가 임계값을 넘었다. 아래 서버만 대상으로 원인을 점검하고, "
             "안전하게 정리할 수 있는 곳을 찾아 정리 조치를 제안하라. 가능하면 ops: 정리 조치를 사용하고, 점검 결과를 "
             "근거로 삼아라. 다른 서버는 건드리지 마라.", ""]
    for f in findings:
        lines.append(f"- {f['host']}: {labels[f['kind']]} {f['value']} (임계값 {f['threshold']}, {f['mount']})")
    if any(f["kind"].startswith(("headscale_", "mesh_")) for f in findings):
        lines += ["", "headscale 항목은 headscale_status 점검으로 원인을 확인해 보고하라. headscale 서비스 재시작, 설정·ACL "
                  "변경, 노드 등록·삭제는 조치로 제안하지 말고 사람이 할 일로 적어라. 오프라인 노드는 그 노드를 대상으로 "
                  "점검하지 말고(대상 서버가 아닐 수 있다) 상태만 보고하라."]
    return "\n".join(lines)


def _state_path(data_dir: Path, project_id: str) -> Path:
    return data_dir / "ops-watch" / f"{project_id}.json"


def _load_state(path: Path) -> Dict[str, float]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {k: float(v) for k, v in data.items() if isinstance(v, (int, float))} if isinstance(data, dict) else {}


def watch_project(project: Any, agent: Any, data_dir: Path, dry_run: bool = False,
                  now: Optional[float] = None) -> Dict[str, Any]:
    """Checks one ops project's targets and starts a debate for new findings. Returns a report."""
    now = time.time() if now is None else now
    watch = (project.policy or {}).get("watch")
    report: Dict[str, Any] = {"project": project.id, "checked": [], "unreachable": [], "findings": [], "new": [],
                              "debate": None}
    if not watch:
        report["skipped"] = "the project has no policy.watch"
        return report
    checks = OpsChecks(project, agent=agent)
    for host in checks.resolved["ids"]:
        disk = checks.run({"op": "disk_usage", "host": host})
        journal = checks.run({"op": "journal_usage", "host": host})
        if not disk["ok"] and not journal["ok"]:
            report["unreachable"].append(host)
            continue
        report["checked"].append(host)
        report["findings"].extend(findings_for(host, disk, journal, watch))
        if watch.get("headscale"):
            report["findings"].extend(headscale_findings(host, checks.run({"op": "headscale_status", "host": host}), now))
    path = _state_path(data_dir, project.id)
    state = _load_state(path)
    recent = now - REPEAT_DAYS * 86400
    report["new"] = [f for f in report["findings"] if state.get(fingerprint(f), 0) < recent]
    if not report["new"] or dry_run:
        return report
    report["debate"] = agent.start_debate(task_text(project.id, report["new"]), project.id)
    state = {k: v for k, v in state.items() if v >= recent}
    state.update({fingerprint(f): now for f in report["new"]})
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)
    return report


def watch_all(manager: Any, agent: Any, data_dir: Path, project_id: Optional[str] = None,
              dry_run: bool = False) -> List[Dict[str, Any]]:
    """Runs watch_project for one project, or for every valid ops project with a watch policy."""
    from project_registry import ProjectValidationError

    if project_id:
        return [watch_project(manager.load(project_id), agent, data_dir, dry_run)]
    reports = []
    for raw in manager.list_projects():
        if raw.get("kind") != "ops" or not (raw.get("policy") or {}).get("watch"):
            continue
        try:
            project = manager.load(str(raw.get("id")))
        except ProjectValidationError as exc:
            reports.append({"project": raw.get("id"), "error": str(exc)})
            continue
        reports.append(watch_project(project, agent, data_dir, dry_run))
    return reports
