---
name: guardian-host-check
description: >-
  Read-only health and disk check of THIS node, the same checks Server Guardian's ops debates use. Use it when the
  operator asks how this node is doing, why its disk is full, what takes space, or before any cleanup
  ("이 노드 점검", "디스크 확인", "용량 왜 찼어", "노드 상태"). It changes nothing.
---

# Guardian host check (read-only)

Look at this node only. Run the commands below, read their output, and report facts with the numbers you saw.
Never change anything in this skill: no deletion, no restart, no package or config change. If the operator wants
something cleaned, finish the report and then use the `guardian-host-cleanup` skill.

## Checks

Run what the question needs; for "how is this node" run 1-4 and 8. The commands are for Debian and Ubuntu (GNU tools,
dpkg): look at `uname -s` and `/etc/os-release` first, and on another system (macOS, a dnf or zypper distribution)
use its own equivalent of a read-only check or say that the check does not apply. Never change anything to make a
check work.

1. Disk and inodes of the real filesystems:
   `df -PT -x tmpfs -x devtmpfs -x squashfs -x overlay; df -Pi -x tmpfs -x devtmpfs -x squashfs -x overlay`
2. Largest directories (narrow the path; a whole `/` walk can take minutes):
   `du -x -k -d 1 /var 2>/dev/null | sort -rn | head -20` (then go one level deeper where it is large)
3. Journal and logs: `journalctl --disk-usage; du -x -k -d 1 /var/log 2>/dev/null | sort -rn | head -20`
4. Package cache and kernels: `du -sk /var/cache/apt/archives 2>/dev/null; uname -r;
   dpkg-query -W -f '${Package} ${Status}\n' 'linux-image-*' 2>/dev/null | grep ' installed$'`
5. Docker, when installed: `docker system df` (or `sudo -n docker system df`)
6. Large files under a path: `find <path> -xdev -type f -size +100M -printf '%s\t%TY-%Tm-%Td\t%p\n' 2>/dev/null | sort -rn | head -30`
7. Services: `systemctl --failed --no-legend --plain --no-pager`
8. Load and memory: `uptime; nproc; free -m; [ -f /var/run/reboot-required ] && echo "reboot required"`

## Report

- Lead with the answer (for example: `/` is 91% full, journal 4.0G is most of it).
- One line per finding with the number and the command it came from.
- Do not print the contents of files under ~/.ssh, ~/.guardian, credential or key files, or any `.env` file.
- End with what could be cleaned safely (journal vacuum, apt cache, dangling docker images, old kernels) and how much it
  would free, but do not do it here.
