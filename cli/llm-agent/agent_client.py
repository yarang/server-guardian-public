"""
Client for communicating with the Go Node Agent REST API.
Uses Python standard libraries (urllib.request, json) for zero-dependency execution.
"""

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional


def agent_token() -> Optional[str]:
    """AGENT_TOKEN from the environment, else from the agent service env file on this host."""
    token = os.environ.get("AGENT_TOKEN")
    if token:
        return token
    env_file = Path(os.environ.get("GUARDIAN_AGENT_ENV", "~/.guardian/agent.env")).expanduser()
    try:
        for line in env_file.read_text(encoding="utf-8").splitlines():
            key, sep, value = line.strip().partition("=")
            if sep and key.strip() == "AGENT_TOKEN" and value.strip():
                return value.strip().strip("'\"")
    except OSError:
        pass
    return None


class NodeAgentClient:
    def __init__(self, base_url: str = "http://localhost:9870", token: Optional[str] = None):
        self.base_url = base_url.rstrip("/")
        self.token = token if token is not None else agent_token()

    def _authorize(self, req: urllib.request.Request) -> None:
        if self.token:
            req.add_header("Authorization", f"Bearer {self.token}")

    def _fetch_json(
        self, endpoint: str, method: str = "GET", data: Optional[Dict[str, Any]] = None, timeout: int = 5,
        labels: Optional[Dict[str, str]] = None,
    ) -> Any:
        """labels ({"source", "op", "project"}) become X-Guardian-* headers that the hub writes into its audit log."""
        url = f"{self.base_url}{endpoint}"
        req = urllib.request.Request(url, method=method)
        req.add_header("Content-Type", "application/json")
        for name, value in (labels or {}).items():
            if value:
                req.add_header(f"X-Guardian-{name.capitalize()}", str(value))
        self._authorize(req)

        payload = None
        if data is not None:
            payload = json.dumps(data).encode("utf-8")

        try:
            with urllib.request.urlopen(req, data=payload, timeout=timeout) as res:
                body = res.read().decode("utf-8")
                return json.loads(body)
        except urllib.error.HTTPError as e:
            err_body = e.read().decode("utf-8")
            raise RuntimeError(f"Agent API {endpoint} returned HTTP {e.code}: {err_body}") from e
        except Exception as e:
            raise ConnectionError(f"Failed to connect to Node Agent at {url}: {e}") from e

    def get_status(self) -> Dict[str, Any]:
        return self._fetch_json("/api/status")

    def get_metrics(self) -> Dict[str, Any]:
        return self._fetch_json("/api/metrics")

    def get_ssh_config(self) -> Dict[str, Any]:
        return self._fetch_json("/api/ssh/config")

    def get_ssh_security(self) -> Dict[str, Any]:
        return self._fetch_json("/api/ssh/security")

    def get_authorized_keys(self) -> Dict[str, Any]:
        return self._fetch_json("/api/ssh/keys")

    def get_nodes(self) -> Dict[str, Any]:
        """Fetch the registered fleet inventory."""
        return self._fetch_json("/api/nodes")

    def execute_command(self, command: str, timeout_sec: int = 10,
                        labels: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
        """Execute sandbox command on local host via Go Agent."""
        return self._fetch_json(
            "/api/exec", method="POST", data={"command": command, "timeout_sec": timeout_sec}, timeout=timeout_sec + 2,
            labels=labels,
        )

    def execute_remote_ssh(self, target_host: str, command: str, timeout_sec: int = 10,
                           labels: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
        """Execute remote command on a fleet host via SSH."""
        return self._fetch_json(
            "/api/exec/remote",
            method="POST",
            data={"target_host": target_host, "command": command, "timeout_sec": timeout_sec},
            timeout=timeout_sec + 2,
            labels=labels,
        )

    def start_debate(self, task: str, project: str) -> Dict[str, Any]:
        """Starts a debate job on the hub (POST /api/debates), as the dashboard does; returns the job."""
        return self._fetch_json("/api/debates", method="POST", data={"task": task, "project": project}, timeout=30)

    def get_groups(self) -> Dict[str, Any]:
        """User-defined node groups with their resolved member ids (hub /api/groups)."""
        return self._fetch_json("/api/groups", timeout=35)

    def diagnose_fleet(self, targets: Optional[List[str]] = None) -> Dict[str, Any]:
        """Parallel diagnosis across every node, or only the given node ids."""
        query = ""
        if targets:
            query = "?" + urllib.parse.urlencode({"targets": ",".join(targets)})
        return self._fetch_json(f"/api/fleet/diagnose{query}", method="GET", timeout=15)

    def fleet_ssh_audit(self, targets: Optional[List[str]] = None, refresh: bool = False) -> Dict[str, Any]:
        """Per-node sshd audit across every node, or only the given node ids (GET /api/fleet/ssh).

        The hub caches results for 60s per target set; refresh=True asks it to probe again."""
        params: Dict[str, str] = {}
        if targets:
            params["targets"] = ",".join(targets)
        if refresh:
            params["refresh"] = "1"
        query = "?" + urllib.parse.urlencode(params) if params else ""
        # Up to 8 nodes at a time with a 20s cap each: a slow fleet can take about a minute.
        return self._fetch_json(f"/api/fleet/ssh{query}", method="GET", timeout=90)

    def execute_fleet(self, command: str, timeout_sec: int = 10, targets: Optional[List[str]] = None) -> Dict[str, Any]:
        """Execute a command in parallel across every node, or only the given node ids."""
        data: Dict[str, Any] = {"command": command, "timeout_sec": timeout_sec}
        if targets:
            data["targets"] = list(targets)
        return self._fetch_json("/api/fleet/exec", method="POST", data=data, timeout=timeout_sec + 5)

    def stream_fleet_exec(self, command: str, targets: Optional[List[str]] = None):
        """Stream execution events across the fleet (SSE over a POST; the command stays out of the URL)."""
        body: Dict[str, Any] = {"command": command}
        if targets:
            body["targets"] = list(targets)
        url = f"{self.base_url}/api/fleet/exec/stream"
        req = urllib.request.Request(url, data=json.dumps(body).encode("utf-8"), method="POST")
        req.add_header("Content-Type", "application/json")
        req.add_header("Accept", "text/event-stream")
        self._authorize(req)
        try:
            resp = urllib.request.urlopen(req)
        except urllib.error.HTTPError as e:
            err_body = e.read().decode("utf-8")
            raise RuntimeError(f"Agent API /api/fleet/exec/stream returned HTTP {e.code}: {err_body}") from e
        with resp:
            for line in resp:
                decoded = line.decode("utf-8").strip()
                if decoded.startswith("data:"):
                    payload = decoded[5:].strip()
                    if payload:
                        try:
                            yield json.loads(payload)
                        except Exception:
                            pass


