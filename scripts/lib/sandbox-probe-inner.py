#!/usr/bin/env python3
"""The sandbox probe's inner script (docs/design/resident-open-tools.md, O1).

Runs INSIDE the sandboxed unit as gsa-probe; the remote script copies it to /home/gsa-probe/inner.py
because that home is 0700. Modes:

  checks <base64 JSON>   visibility, writes, tcp, dns, egress, unix and abstract sockets, own and
                         foreign sockets, processes, persistence attempts
  hog                    allocate 16 MiB blocks until MemoryError / the memory cap kills us (4 GiB cap)
  forks                  fork sleepers until TasksMax refuses (2000 children cap)
  workload               run the cli/llm-agent unittest suite as scripts/ci.sh does (no LLM
                         provider, throwaway data dir; loopback-server modules left out) and
                         report the cgroup memory peak

Prints key=value lines only, never file contents; every network call has a 3 s timeout. Standard
library only, like everything in this repository's Python.
"""

import base64
import concurrent.futures
import errno
import grp
import json
import os
import pwd
import signal
import socket
import ssl
import stat
import subprocess
import sys
import threading
import time

TIMEOUT = 3.0
TCP_WORKERS = 24  # threads count against the probe unit's TasksMax (64)


def emit(key, value):
    # Flushed per line: a unit killed at RuntimeMaxSec still leaves what it found so far.
    print("%s=%s" % (key, str(value).replace("\n", " ")), flush=True)


def err_name(exc):
    return errno.errorcode.get(exc.errno) or type(exc).__name__


# --- identity and privilege -----------------------------------------------------------------------

def identity():
    entry = pwd.getpwuid(os.getuid())
    emit("id.uid", os.getuid())
    emit("id.user", entry.pw_name)
    emit("id.groups", " ".join(group_name(g) for g in os.getgroups()))


def group_name(gid):
    try:
        return grp.getgrgid(gid).gr_name
    except KeyError:
        return str(gid)


def sudo_state():
    try:
        proc = subprocess.run(["sudo", "-n", "true"], capture_output=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return "refused"  # a missing binary or a hang both mean: no sudo here
    return "allowed" if proc.returncode == 0 else "refused"


# --- path visibility and writes -------------------------------------------------------------------

def path_state(path):
    try:
        st = os.stat(path)
    except FileNotFoundError:
        return "hidden"
    except NotADirectoryError:
        return "hidden"
    except PermissionError:
        return "denied"
    except OSError:
        return "hidden"
    if stat.S_ISDIR(st.st_mode):
        try:
            os.listdir(path)
            return "listable"
        except OSError:
            return "denied"
    try:
        with open(path, "rb") as handle:
            handle.read(1)  # never read beyond one byte: only whether we could matters
        return "readable"
    except PermissionError:
        return "denied"
    except OSError:
        return "hidden"


def path_checks(payload):
    home = payload["node_home"]
    paths = [home, home + "/.guardian/llm.env", home + "/.ssh", home + "/.claude", home + "/.codex",
             home + "/.config", payload["worker_sock"], "/home/gsa-probe2", "/home/gsa-probe2/canary",
             "/var/lib/guardian-sandbox/sock/probe2", "/root", "/etc/sudoers.d"]
    for i, path in enumerate(paths):
        emit("path.%d.name" % i, path)
        emit("path.%d.state" % i, path_state(path))


def write_state(directory):
    tmp = os.path.join(directory, ".guardian-probe-write-%d" % os.getpid())
    try:
        with open(tmp, "wb") as handle:
            handle.write(b"x")
        os.unlink(tmp)
        return "ok"
    except OSError as exc:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        return err_name(exc)


def write_checks(payload):
    emit("write.home", write_state(os.environ.get("HOME", "/home/gsa-probe")))
    emit("write.own_sock_dir", write_state("/var/lib/guardian-sandbox/sock/probe"))
    dirs = ["/etc", "/usr/local", "/var/lib", "/opt", "/srv", "/var/lib/guardian-sandbox/sock",
            payload["node_home"]]
    for i, directory in enumerate(dirs):
        emit("write.%d.name" % i, directory)
        emit("write.%d.state" % i, write_state(directory))
    # PrivateTmp makes /tmp and /var/tmp private per unit; just record that writes work there.
    emit("write.tmp", "private_ok" if write_state("/tmp") == "ok" else write_state("/tmp"))
    emit("write.var_tmp", "private_ok" if write_state("/var/tmp") == "ok" else write_state("/var/tmp"))


# --- network --------------------------------------------------------------------------------------

def tcp_state(addr, port):
    family = socket.AF_INET6 if ":" in addr else socket.AF_INET
    try:
        with socket.socket(family, socket.SOCK_STREAM) as sock:
            sock.settimeout(TIMEOUT)
            sock.connect((addr, port))
        return "connected"
    except PermissionError:
        return "denied"
    except ConnectionRefusedError:
        return "refused"
    except socket.timeout:
        return "timeout"
    except OSError as exc:
        if exc.errno in (errno.ENETUNREACH, errno.EHOSTUNREACH):
            return "unreachable"
        return err_name(exc)


def tcp_targets(payload):
    targets = []
    for entry in payload.get("listeners") or []:
        port = entry.get("port")
        if not isinstance(port, int):
            continue
        for addr in ("127.0.0.1", "127.0.0.2", "0.0.0.0", "::1", "::", "::ffff:127.0.0.1"):
            targets.append((addr, port))
        for addr in payload.get("own_addrs") or []:
            targets.append((addr, port))
    if payload.get("hub_ip"):
        targets.append((payload["hub_ip"], 22))
    if payload.get("peer_ip"):
        targets.append((payload["peer_ip"], 22))
    targets.append(("169.254.169.254", 80))
    # The resolver stub is allowed by design: port 53 on 127.0.0.53/127.0.0.54 is not a must-deny.
    targets = [t for t in targets if not (t[1] == 53 and t[0] in ("127.0.0.53", "127.0.0.54"))]
    seen, out = set(), []
    for target in targets:
        if target not in seen:
            seen.add(target)
            out.append(target)
    return out


def tcp_checks(payload):
    # Denied ranges drop packets, so most targets wait the full timeout: probe them in parallel
    # (a node with a dozen listeners has over a hundred targets) and report in target order.
    targets = tcp_targets(payload)
    with concurrent.futures.ThreadPoolExecutor(max_workers=TCP_WORKERS) as pool:
        states = list(pool.map(lambda target: tcp_state(*target), targets))
    for i, ((addr, port), state) in enumerate(zip(targets, states)):
        emit("tcp.%d.target" % i, "%s:%d" % (addr, port))
        emit("tcp.%d.state" % i, state)


def dns_state():
    result = {"value": "timeout"}

    def resolve():
        try:
            socket.getaddrinfo("api.anthropic.com", 443, socket.AF_UNSPEC, socket.SOCK_STREAM)
            result["value"] = "ok"
        except OSError as exc:
            result["value"] = err_name(exc)

    thread = threading.Thread(target=resolve, daemon=True)
    thread.start()
    thread.join(TIMEOUT)
    return result["value"]


def egress_state(host):
    try:
        with socket.create_connection((host, 443), timeout=TIMEOUT) as raw:
            context = ssl.create_default_context()
            with context.wrap_socket(raw, server_hostname=host):
                return "ok"
    except OSError as exc:
        return err_name(exc)


# --- unix sockets ---------------------------------------------------------------------------------

def walk_one_fs(root):
    out, stack = [], [root]
    try:
        root_dev = os.stat(root).st_dev
    except OSError:
        return out
    while stack:
        directory = stack.pop()
        try:
            entries = list(os.scandir(directory))
        except OSError:
            continue
        for entry in entries:
            try:
                st = entry.stat(follow_symlinks=False)
                if stat.S_ISDIR(st.st_mode):
                    if st.st_dev == root_dev:  # never cross devices: /run, /dev, /proc are their own
                        stack.append(entry.path)
                elif stat.S_ISSOCK(st.st_mode):
                    out.append(entry.path)
                elif stat.S_ISLNK(st.st_mode):
                    try:
                        if stat.S_ISSOCK(os.stat(entry.path).st_mode):
                            out.append(entry.path)
                    except OSError:
                        pass
            except OSError:
                continue
    return out


def unix_state(path):
    for kind in (socket.SOCK_STREAM, socket.SOCK_DGRAM):
        last = ""
        try:
            sock = socket.socket(socket.AF_UNIX, kind)
            sock.settimeout(TIMEOUT)
            try:
                sock.connect(path)
            finally:
                sock.close()
            return "connected"
        except OSError as exc:
            last = err_name(exc)
            if exc.errno == errno.EPROTOTYPE:
                continue  # a datagram socket: retry with SOCK_DGRAM
            if exc.errno in (errno.EACCES, errno.EPERM):
                return "denied"
            if exc.errno == errno.ENOENT:
                return "hidden"
            if isinstance(exc, ConnectionRefusedError):
                return "refused"
            return last
    return last


def unix_socket_checks():
    paths = []
    for root in ("/", "/run", "/dev"):
        paths.extend(walk_one_fs(root))
    for i, path in enumerate(sorted(paths)):
        emit("unix.%d.path" % i, path)
        emit("unix.%d.state" % i, unix_state(path))


def abstract_checks(cap=200):
    try:
        with open("/proc/net/unix", encoding="utf-8", errors="replace") as handle:
            lines = handle.read().splitlines()
    except OSError:
        return
    names, truncated = [], False
    for line in lines[1:]:
        parts = line.split(None, 7)
        if len(parts) < 8 or not parts[7].startswith("@"):
            continue
        if len(names) >= cap:
            truncated = True
            break
        names.append(parts[7])
    for i, name in enumerate(names):
        emit("abstract.%d.name" % i, name)
        emit("abstract.%d.state" % i, unix_state("\0" + name[1:]))
    if truncated:
        emit("abstract.truncated", 1)


def own_sock_check(path):
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(TIMEOUT)
            sock.connect(path)
            sock.sendall(b'{"op":"ping"}\n')
            reply = sock.recv(4096)
        return "ok" if b'"ok"' in reply and b"true" in reply else "bad_reply"
    except OSError as exc:
        return err_name(exc)


# --- processes and persistence --------------------------------------------------------------------

def foreign_process_count():
    me, count = os.getuid(), 0
    try:
        names = os.listdir("/proc")
    except OSError:
        return -1
    for name in names:
        if not name.isdigit():
            continue
        try:
            if os.stat("/proc/" + name).st_uid != me:
                count += 1
        except OSError:
            continue
    return count


def persist_daemon():
    try:
        pid = os.fork()
    except OSError as exc:
        return err_name(exc)
    if pid == 0:
        try:
            if os.fork() != 0:
                os._exit(0)  # the middle child dies, the grandchild is reparented
            os.setsid()
            devnull = os.open(os.devnull, os.O_RDWR)
            for fd in (0, 1, 2):
                os.dup2(devnull, fd)
            os.close(devnull)
            os.closerange(3, 64)
            time.sleep(600)
        except BaseException:
            pass
        os._exit(0)
    os.waitpid(pid, 0)
    return "spawned"  # it must not outlive the unit (the remote script checks with pgrep)


def run_quiet(argv, timeout):
    try:
        proc = subprocess.run(argv, capture_output=True, timeout=timeout)
    except FileNotFoundError:
        return "absent"
    except subprocess.TimeoutExpired:
        return "refused"
    return proc.returncode


def run_checks_mode(payload):
    identity()
    emit("sudo", sudo_state())
    path_checks(payload)
    write_checks(payload)
    tcp_checks(payload)
    emit("dns", dns_state())
    for host in ("api.anthropic.com", "api.openai.com"):
        emit("egress.%s" % host, egress_state(host))
    unix_socket_checks()
    abstract_checks()
    emit("own_sock", own_sock_check(payload["own_sock"]))
    emit("other_sock", unix_state(payload["other_sock"]))
    emit("worker_sock", unix_state(payload["worker_sock"]))
    emit("proc.foreign_visible", foreign_process_count())
    emit("persist.daemon", persist_daemon())
    rc = run_quiet(["systemd-run", "--user", "--quiet", "true"], 10)
    emit("persist.user_unit", "started" if rc == 0 else "refused")
    rc = run_quiet(["crontab", "-l"], 10)
    emit("persist.crontab", "available" if rc == 0 else ("absent" if rc == "absent" else "refused"))
    emit("checks.done", 1)


# --- stress modes ---------------------------------------------------------------------------------

def hog_mode():
    chunks, total_mib = [], 0
    try:
        while total_mib < 4 * 1024:
            chunk = bytearray(16 * 1024 * 1024)
            for offset in range(0, len(chunk), 4096):  # touch every page, or nothing is charged
                chunk[offset] = 1
            chunks.append(chunk)
            total_mib += 16
    except MemoryError:
        emit("hog.result", "memory_error")
        emit("hog.mib", total_mib)
        return
    emit("hog.result", "not_capped")  # reached 4 GiB: the cap did not hold


def forks_mode():
    pids, capped_at, err = [], None, ""
    try:
        while len(pids) < 2000:
            pid = os.fork()
            if pid == 0:
                time.sleep(60)
                os._exit(0)
            pids.append(pid)
    except OSError as exc:
        capped_at, err = len(pids), err_name(exc)
    for pid in pids:
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    for pid in pids:
        try:
            os.waitpid(pid, 0)
        except ChildProcessError:
            pass
    if capped_at is not None:
        emit("forks.capped_at", capped_at)
        emit("forks.errno", err)
    else:
        emit("forks.result", "not_capped")  # 2000 children: the cap did not hold


def memory_peak_mib():
    try:
        with open("/proc/self/cgroup", encoding="utf-8") as handle:
            for line in handle:
                if line.startswith("0::"):
                    path = line.split(":", 2)[2].strip()
                    with open("/sys/fs/cgroup" + path + "/memory.peak", encoding="utf-8") as peak:
                        return int(peak.read().strip()) // (1024 * 1024)
    except (OSError, ValueError):
        pass
    return "unavailable"


# Test modules that serve a fake API on 127.0.0.1. The sandbox drops loopback packets, so each
# request waits for the connect timeout: they test the host, not the turn, and are left out.
LOOPBACK_MODULES = ("test_exaone_provider",)


def workload_env():
    """The CI environment of scripts/ci.sh: no real LLM provider, a throwaway data dir."""
    data_dir = os.path.join(os.path.expanduser("~"), "work", "data")
    os.makedirs(data_dir, exist_ok=True)
    env = dict(os.environ)
    env.update({"GUARDIAN_DATA_DIR": data_dir, "GUARDIAN_LLM_PROVIDERS": "none"})
    return env


def workload_modules():
    names = sorted(name[:-3] for name in os.listdir(".")
                   if name.startswith("test_") and name.endswith(".py"))
    return [name for name in names if name not in LOOPBACK_MODULES]


def workload_mode():
    start = time.monotonic()
    emit("workload.skipped", ",".join(LOOPBACK_MODULES))
    try:
        proc = subprocess.run([sys.executable, "-m", "unittest", *workload_modules()],
                              capture_output=True, text=True, timeout=1500, env=workload_env())
        rc, out = proc.returncode, (proc.stdout or "") + (proc.stderr or "")
    except subprocess.TimeoutExpired:
        rc, out = "timeout", ""
    tail = ""
    for line in reversed(out.splitlines()):
        if line.strip():
            tail = line.strip()
            break
    emit("workload.rc", rc)
    emit("workload.seconds", round(time.monotonic() - start))
    emit("workload.tail", tail.replace(" ", "_")[:120])
    emit("workload.memory_peak_mib", memory_peak_mib())


def main(argv):
    if len(argv) >= 2 and argv[0] == "checks":
        payload = json.loads(base64.b64decode(argv[1]))
        run_checks_mode(payload)
        return 0
    if argv[:1] == ["hog"]:
        hog_mode()
        return 0
    if argv[:1] == ["forks"]:
        forks_mode()
        return 0
    if argv[:1] == ["workload"]:
        workload_mode()
        return 0
    print("usage: sandbox-probe-inner.py checks <base64 JSON> | hog | forks | workload", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
