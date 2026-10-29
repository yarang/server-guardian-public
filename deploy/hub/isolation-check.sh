#!/usr/bin/env bash
# ==============================================================================
# Server Guardian - Check the agent's isolation on the hub (deploy/hub/install.sh runs it after a start)
#
# Runs inside the agent's own mount namespace, as ubuntu with the agent's no_new_privs flag: the key must be
# unreadable to plain reads, sudo must match the hub sudo setting (HUB_SUDO=on|off), and fleet SSH must work.
# Prints one line per check; a failed check carries ❌.
# ==============================================================================
# First fleet host other than the hub itself.
fleet="$(awk -v self="$(hostname)" '/^Host /&&$2!=self&&$2!="*"{print $2; exit}' ~/.ssh/guardian_fleet.conf 2>/dev/null || true)"
[ -n "${fleet}" ] || fleet="$(awk -v self="$(hostname)" '/# BEGIN guardian-hub-fleet/{f=1} f&&/^Host /&&$2!=self{print $2; exit}' ~/.ssh/config)"
pid="$(systemctl show -p MainPID --value server-guardian-agent.service)"
nnp=""; grep -q "^NoNewPrivs:[[:space:]]*1" "/proc/${pid}/status" && nnp="--no-new-privs"
run() { sudo -n nsenter -t "${pid}" -m -- setpriv --reuid ubuntu --regid ubuntu --init-groups ${nnp} \
    env HOME=/home/ubuntu /bin/bash -c "$1" </dev/null 2>/dev/null; }
run "cat /etc/guardian/fleet_key >/dev/null" && echo "  ❌ 키 파일을 읽을 수 있음" || echo "  ✅ 키 파일 읽기 차단 (일반 읽기)"
if run "sudo -n true"; then
    if [ "${HUB_SUDO}" = on ]; then echo "  ✅ sudo 허용 (--hub-sudo)"; else echo "  ❌ sudo 가능"; fi
else
    if [ "${HUB_SUDO}" = on ]; then echo "  ❌ sudo 실패 (--hub-sudo인데 막힘)"; else echo "  ✅ sudo 차단"; fi
fi
if [ -n "${fleet}" ] && run "ssh -o BatchMode=yes -o ConnectTimeout=8 -o LogLevel=ERROR ${fleet} true"; then
    echo "  ✅ fleet SSH (${fleet}) 동작"
else
    echo "  ❌ fleet SSH (${fleet:-대상 없음}) 실패"
fi
echo "  systemd 보안 점수: $(systemd-analyze security server-guardian-agent.service 2>/dev/null | tail -n 1 | sed "s/.*exposure level for [^:]*: //")"
