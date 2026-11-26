#!/usr/bin/env bash
# ==============================================================================
# Server Guardian - write (or remove) an open agent's credential on a node
# (docs/design/resident-open-tools.md, OD3; docs/design/resident-codex-and-policy.md, PD2)
#
# Providers:
#   claude (default): pipes one long-lived token (`claude setup-token`) into
#     /home/gsa-<agent>/.guardian/credentials/claude-oauth-token (0600, owned by gsa-<agent>).
#   codex (--provider codex): either pipes a dedicated auth.json JSON object on stdin into
#     /home/gsa-<agent>/.codex/auth.json (0600, owned by gsa-<agent>), or runs interactive
#     `codex login --device-auth` as gsa-<agent> on the target node (--login).
#
# Secrets never travel in arguments, environment variables, temp files on this machine, or logs.
#
# Usage:
#   claude setup-token | scripts/sandbox-credential.sh <host> <agent>
#   scripts/sandbox-credential.sh <host> <agent> --remove
#   scripts/sandbox-credential.sh <host> <agent> --provider codex --login
#   cat auth.json | scripts/sandbox-credential.sh <host> <agent> --provider codex
#   scripts/sandbox-credential.sh <host> <agent> --provider codex --remove
#
# Exit: 0 written (or removed), 1 the ssh or remote write failed, 2 misuse or bad input.
# ==============================================================================

set -euo pipefail

AGENT_NAME_RE='^[a-z][a-z0-9]{0,15}$'

HOST=""
AGENT=""
PROVIDER="claude"
REMOVE=0
LOGIN=0
while [ $# -gt 0 ]; do
    case "$1" in
        --provider)
            shift
            PROVIDER="${1:-}"
            case "${PROVIDER}" in
                claude|codex) ;;
                *) echo "unsupported --provider (use claude or codex): ${PROVIDER}" >&2; exit 2 ;;
            esac
            ;;
        --remove) REMOVE=1 ;;
        --login) LOGIN=1 ;;
        -h|--help) sed -n '2,23p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
        -*) echo "unknown option: $1" >&2; exit 2 ;;
        *)
            if [ -z "${HOST}" ]; then HOST="$1"
            elif [ -z "${AGENT}" ]; then AGENT="$1"
            else echo "usage: scripts/sandbox-credential.sh <host> <agent> [--provider claude|codex] [--login|--remove]" >&2; exit 2
            fi ;;
    esac
    shift
done
[ -n "${HOST}" ] && [ -n "${AGENT}" ] || {
    echo "usage: scripts/sandbox-credential.sh <host> <agent> [--provider claude|codex] [--login|--remove]" >&2
    exit 2
}
[[ "${AGENT}" =~ ${AGENT_NAME_RE} ]] || {
    echo "bad agent name (must match ${AGENT_NAME_RE}): ${AGENT}" >&2
    exit 2
}
if [ "${LOGIN}" = 1 ] && [ "${PROVIDER}" != "codex" ]; then
    echo "--login is supported only with --provider codex" >&2
    exit 2
fi
if [ "${LOGIN}" = 1 ] && [ "${REMOVE}" = 1 ]; then
    echo "--login and --remove cannot be combined" >&2
    exit 2
fi

remote_write_claude="$(cat <<'REMOTE'
set -euo pipefail
agent="$1"
user="gsa-${agent}"
guard="/home/${user}/.guardian"
creds="${guard}/credentials"
getent passwd "${user}" >/dev/null 2>&1 || { echo "credential: no user ${user} on this host" >&2; exit 1; }
install -d -o "${user}" -g "${user}" -m 0700 "${guard}" "${creds}"
token="$(cat)"
[ -n "${token}" ] || { echo "credential: empty token on stdin" >&2; exit 1; }
case "${token}" in
    *[[:space:]]*) echo "credential: the token must not contain whitespace" >&2; exit 1 ;;
esac
printf '%s\n' "${token}" | runuser -u "${user}" -- sh -c "umask 077; cat > '${creds}/claude-oauth-token.new'"
mv -f "${creds}/claude-oauth-token.new" "${creds}/claude-oauth-token"
echo "credential written for ${agent}"
REMOTE
)"

remote_remove_claude="$(cat <<'REMOTE'
set -euo pipefail
agent="$1"
user="gsa-${agent}"
creds="/home/${user}/.guardian/credentials"
getent passwd "${user}" >/dev/null 2>&1 || { echo "credential: no user ${user} on this host" >&2; exit 1; }
rm -f "${creds}/claude-oauth-token" "${creds}/claude-oauth-token.new"
echo "credential removed for ${agent}"
REMOTE
)"

remote_write_codex="$(cat <<'REMOTE'
set -euo pipefail
agent="$1"
user="gsa-${agent}"
cdir="/home/${user}/.codex"
getent passwd "${user}" >/dev/null 2>&1 || { echo "credential: no user ${user} on this host" >&2; exit 1; }
install -d -o "${user}" -g "${user}" -m 0700 "${cdir}"
runuser -u "${user}" -- python3 -c '
import json, os, sys
raw = sys.stdin.read()
obj = json.loads(raw)
if not isinstance(obj, dict) or not obj:
    raise SystemExit("credential: auth.json must be a non-empty JSON object")
target = sys.argv[1]
tmp = target + ".new"
fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
try:
    with os.fdopen(fd, "w", encoding="utf-8") as h:
        h.write(raw if raw.endswith("\n") else raw + "\n")
    os.chmod(tmp, 0o600)
    os.replace(tmp, target)
except BaseException:
    try:
        os.unlink(tmp)
    except OSError:
        pass
    raise
' "${cdir}/auth.json"
echo "codex credential written for ${agent}"
REMOTE
)"

remote_remove_codex="$(cat <<'REMOTE'
set -euo pipefail
agent="$1"
user="gsa-${agent}"
cdir="/home/${user}/.codex"
getent passwd "${user}" >/dev/null 2>&1 || { echo "credential: no user ${user} on this host" >&2; exit 1; }
rm -f "${cdir}/auth.json" "${cdir}/auth.json.new"
echo "codex credential removed for ${agent}"
REMOTE
)"

remote_login_codex="$(cat <<'REMOTE'
set -euo pipefail
agent="$1"
user="gsa-${agent}"
home="/home/${user}"
cdir="${home}/.codex"
getent passwd "${user}" >/dev/null 2>&1 || { echo "credential: no user ${user} on this host" >&2; exit 1; }
bin="$(find /usr/local/lib/guardian-sandbox/cli -maxdepth 2 -path '*/codex-*/codex' -type f | sort | tail -n 1 || true)"
[ -n "${bin}" ] && [ -x "${bin}" ] || { echo "credential: no sandbox codex binary installed under /usr/local/lib/guardian-sandbox/cli" >&2; exit 1; }
install -d -o "${user}" -g "${user}" -m 0700 "${cdir}"
runuser -u "${user}" -- env -i HOME="${home}" CODEX_HOME="${cdir}" PATH=/usr/local/bin:/usr/bin:/bin LANG=C.UTF-8 \
    "${bin}" login --device-auth
chmod 0600 "${cdir}/auth.json"
chown "${user}:${user}" "${cdir}/auth.json"
echo "codex login complete for ${agent}"
REMOTE
)"

run_remote() {  # run_remote SCRIPT_B64
    local b64="$1"
    ssh -o BatchMode=yes -o ConnectTimeout=10 -- "${HOST}" \
        "sudo -n bash -c \"\$(printf %s ${b64} | base64 -d)\" bash ${AGENT}"
}

if [ "${REMOVE}" = 1 ]; then
    if [ "${PROVIDER}" = "codex" ]; then
        run_remote "$(printf '%s' "${remote_remove_codex}" | base64 | tr -d '\n')" </dev/null
    else
        run_remote "$(printf '%s' "${remote_remove_claude}" | base64 | tr -d '\n')" </dev/null
    fi
    exit 0
fi

if [ "${LOGIN}" = 1 ]; then
    b64="$(printf '%s' "${remote_login_codex}" | base64 | tr -d '\n')"
    ssh -t -o ConnectTimeout=10 -- "${HOST}" \
        "sudo -n bash -c \"\$(printf %s ${b64} | base64 -d)\" bash ${AGENT}"
    exit 0
fi

if [ "${PROVIDER}" = "codex" ]; then
    if [ -t 0 ]; then
        echo "refusing a terminal stdin: pipe auth.json or pass --login ($0 <host> <agent> --provider codex --login)" >&2
        exit 2
    fi
    input="$(cat)"
    python3 -c 'import json, sys; obj = json.loads(sys.stdin.read()); assert isinstance(obj, dict) and obj' <<<"${input}" >/dev/null 2>&1 || {
        echo "invalid codex auth.json input (must be a non-empty JSON object)" >&2
        exit 2
    }
    printf '%s\n' "${input}" | run_remote "$(printf '%s' "${remote_write_codex}" | base64 | tr -d '\n')"
    exit 0
fi

if [ -t 0 ]; then
    echo "refusing a terminal stdin: pipe the token instead (claude setup-token | $0 <host> <agent>)" >&2
    exit 2
fi
input="$(cat)"
[ -n "${input//[[:space:]]/}" ] || { echo "empty token input" >&2; exit 2; }
token="$(printf '%s\n' "${input}" | awk 'NF { if (seen) exit 1; print; seen = 1 }')" || {
    echo "the token input must be exactly one non-empty line" >&2
    exit 2
}
[ -n "${token}" ] || { echo "empty token input" >&2; exit 2; }
case "${token}" in
    *[[:space:]]*) echo "the token must not contain whitespace" >&2; exit 2 ;;
esac

printf '%s\n' "${token}" | run_remote "$(printf '%s' "${remote_write_claude}" | base64 | tr -d '\n')"
