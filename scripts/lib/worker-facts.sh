#!/usr/bin/env bash
# Facts a host reports for the worker capability probe (docs/design/remote-agents.md). Run on the host through
# `ssh <host> bash -s < worker-facts.sh`; read-only, prints `key=value` lines that worker_probe.parse_facts reads.
# Nothing here writes outside a temp name, starts a service or sends a prompt.

export PATH="$HOME/.local/bin:$PATH"
echo "hostname_short=$(hostname -s 2>/dev/null || hostname)"
echo "arch=$(uname -m)"
echo "cpu_count=$(getconf _NPROCESSORS_ONLN 2>/dev/null || echo unknown)"
if [ -r /proc/meminfo ]; then
    awk '/^MemTotal:/ {print "mem_total_kib=" $2} /^MemAvailable:/ {print "mem_available_kib=" $2}' /proc/meminfo
fi
if command -v python3 >/dev/null 2>&1; then
    echo "python3_version=$(python3 -c 'import sys; print("%d.%d.%d" % sys.version_info[:3])' 2>/dev/null)"
fi
if command -v git >/dev/null 2>&1; then
    echo "git_version=$(git --version 2>/dev/null | awk '{print $3}')"
fi
echo "disk_free_kib=$(df -Pk "$HOME" 2>/dev/null | awk 'NR==2 {print $4}')"
echo "systemd_user=$(command -v systemctl >/dev/null 2>&1 && systemctl --user is-system-running >/dev/null 2>&1 && echo yes || echo no)"
echo "linger=$(loginctl show-user "$(id -un)" -p Linger 2>/dev/null | cut -d= -f2 | tr '[:upper:]' '[:lower:]')"
echo "sudo_nopass=$(sudo -n true >/dev/null 2>&1 && echo yes || echo no)"
for cli in claude codex agy; do
    path="$(command -v "${cli}" 2>/dev/null || true)"
    [ -n "${path}" ] && echo "cli_path.${cli}=${path}"
done
exit 0
