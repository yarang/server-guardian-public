#!/usr/bin/env bash
# Runs ON the worker host (scripts/install-worker.sh feeds it to `ssh <host> bash -s -- <action> ...`).
#
#   install <max_jobs>   swap in the files unpacked in ~/.guardian/worker/.incoming, install the user unit, restart
#   restore              put ~/.guardian/worker/previous back and restart (after a failed acceptance check)
#   stop                 stop and disable the unit, keep the files (first install that failed its check)
#   uninstall            stop the unit, remove the unit and ~/.guardian/worker
#
# Prints "ok: ..." / "needs: ..." lines; exit 0 = done, 1 = failed, 3 = installed but linger is missing (the
# worker would die with the last login, so the caller keeps the host out of the inventory).

set -euo pipefail

WORKER_HOME="${HOME}/.guardian/worker"
UNIT_DIR="${HOME}/.config/systemd/user"
UNIT="guardian-worker.service"
# Must list the basenames of deploy/worker/files.txt (cli/llm-agent/test_worker_install.py checks it).
CODE=(
    code_context.py code_index.py code_select.py code_wiki.py env_policy.py evidence.py go_facts.py
    go_ops.py llm_runner.py mailbox_mcp.py mcp_stdio.py remote_agents.py repo_map.py repo_mcp.py
    repo_snapshot.py repo_tools.py resident_driver.py resident_node.py resident_open.py
    resident_repo_mcp.py resident_tasks.py worker.py
)

export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
user_ctl() { systemctl --user "$@"; }

# The CLIs the worker runs are found with shutil.which, so the unit's PATH holds their directories.
build_path() {
    local dirs=() seen=":" dir cli p
    for cli in claude codex agy; do
        p="$(PATH="${HOME}/.local/bin:${PATH}" command -v "${cli}" 2>/dev/null || true)"
        [ -n "${p}" ] && dirs+=("$(dirname "${p}")")
    done
    dirs+=("${HOME}/.local/bin" /usr/local/bin /usr/bin /bin)
    local out=""
    for dir in "${dirs[@]}"; do
        case "${seen}" in *":${dir}:"*) continue ;; esac
        case "${dir}" in *[\|\&\\\ ]*) echo "needs: directory with a space or special character on PATH: ${dir}" >&2; exit 1 ;; esac
        seen="${seen}${dir}:"
        out="${out:+${out}:}${dir}"
    done
    printf '%s' "${out}"
}

ensure_linger() {
    local me; me="$(id -un)"
    [ "$(loginctl show-user "${me}" -p Linger 2>/dev/null | cut -d= -f2)" = yes ] && return 0
    loginctl enable-linger "${me}" 2>/dev/null || sudo -n loginctl enable-linger "${me}" 2>/dev/null || true
    [ "$(loginctl show-user "${me}" -p Linger 2>/dev/null | cut -d= -f2)" = yes ]
}

# Syntax-check the code files in a directory. A fresh upload must hold the whole set; a backup (previous/) may come
# from an older release with a smaller set, so it needs only worker.py and remote_agents.py.
verify_files() {
    local dir="$1" mode="${2:-all}" f present=()
    for f in "${CODE[@]}"; do
        if [ -f "${dir}/${f}" ]; then
            present+=("${f}")
        elif [ "${mode}" = all ] || [ "${f}" = worker.py ] || [ "${f}" = remote_agents.py ]; then
            echo "needs: ${dir}/${f} is missing" >&2; return 1
        fi
    done
    python3 - "${dir}" "${present[@]}" <<'PY'
import ast, os, sys
base = sys.argv[1]
for name in sys.argv[2:]:
    with open(os.path.join(base, name), encoding="utf-8") as handle:
        ast.parse(handle.read(), name)
PY
}

case "${1:-}" in
install)
    jobs="${2:?max_jobs}"
    case "${jobs}" in ''|*[!0-9]*) echo "needs: max_jobs must be a number" >&2; exit 1 ;; esac
    incoming="${WORKER_HOME}/.incoming"
    verify_files "${incoming}"
    [ -f "${incoming}/${UNIT}" ] || { echo "needs: ${UNIT} is missing in the upload" >&2; exit 1; }
    path_value="$(build_path)"
    mkdir -p "${WORKER_HOME}/previous" "${UNIT_DIR}"
    chmod 700 "${WORKER_HOME}"
    # Keep the running version for a rollback, then replace file by file (rename = atomic per file).
    for f in "${CODE[@]}"; do
        [ -f "${WORKER_HOME}/${f}" ] && cp -p "${WORKER_HOME}/${f}" "${WORKER_HOME}/previous/${f}"
    done
    [ -f "${WORKER_HOME}/REVISION" ] && cp -p "${WORKER_HOME}/REVISION" "${WORKER_HOME}/previous/REVISION"
    for f in "${CODE[@]}" REVISION; do
        [ -f "${incoming}/${f}" ] && mv -f "${incoming}/${f}" "${WORKER_HOME}/${f}"
    done
    sed -e "s|@PATH@|${path_value}|" -e "s|@MAX_JOBS@|${jobs}|" "${incoming}/${UNIT}" > "${UNIT_DIR}/${UNIT}.tmp"
    mv -f "${UNIT_DIR}/${UNIT}.tmp" "${UNIT_DIR}/${UNIT}"
    rm -rf "${incoming}"
    if ! ensure_linger; then
        echo "needs: linger is off, so the worker would stop at the last logout. Run once on this host: sudo loginctl enable-linger $(id -un)"
        exit 3
    fi
    user_ctl daemon-reload
    user_ctl enable "${UNIT}" >/dev/null 2>&1
    user_ctl restart "${UNIT}"
    echo "ok: unit installed and restarted (PATH ${path_value})"
    ;;
restore)
    [ -d "${WORKER_HOME}/previous" ] || { echo "needs: no previous version to restore" >&2; exit 1; }
    verify_files "${WORKER_HOME}/previous" backup
    for f in "${CODE[@]}" REVISION; do
        [ -f "${WORKER_HOME}/previous/${f}" ] && cp -p "${WORKER_HOME}/previous/${f}" "${WORKER_HOME}/${f}"
    done
    user_ctl restart "${UNIT}"
    echo "ok: previous version restored"
    ;;
stop)
    user_ctl disable --now "${UNIT}" >/dev/null 2>&1 || true
    echo "ok: unit stopped and disabled (files kept)"
    ;;
uninstall)
    user_ctl disable --now "${UNIT}" >/dev/null 2>&1 || true
    rm -f "${UNIT_DIR}/${UNIT}"
    user_ctl daemon-reload >/dev/null 2>&1 || true
    # Only our own directory, and only when it looks like one (never a symlink somewhere else).
    if [ -d "${WORKER_HOME}" ] && [ ! -L "${WORKER_HOME}" ] && [ -f "${WORKER_HOME}/worker.py" ]; then
        rm -rf "${WORKER_HOME}"
        echo "ok: unit and ${WORKER_HOME} removed (llm.env and the CLI logins stay)"
    else
        echo "ok: unit removed; ${WORKER_HOME} was not an install directory, left alone"
    fi
    ;;
*)
    echo "usage: bash -s -- install <max_jobs> | restore | stop | uninstall" >&2
    exit 2
    ;;
esac
