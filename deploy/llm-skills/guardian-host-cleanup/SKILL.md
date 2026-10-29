---
name: guardian-host-cleanup
description: >-
  Safe cleanup of THIS node with Server Guardian's cleanup catalog: journal vacuum, apt cache, dangling docker images
  or build cache, old kernels, emptying log files, removing named paths. Use it only when the operator asks to free
  space or clean this node ("이 노드 정리", "디스크 비워줘", "journal 줄여줘", "apt 캐시 삭제"). Every action is
  measured before and after and needs the operator's explicit yes.
---

# Guardian host cleanup

Clean this node only, one action at a time, with the operator's confirmation for each. Fleet-wide cleanup is not
done here: on the hub use `guardian-fleet-ops`, so the actions are debated, audited and measured by Guardian.

## Rules (never break them)

- Only the actions below. No other `rm`, no package removal other than old kernels, no config edits.
- Never touch: `/`, `/etc`, `/boot`, `/usr`, `/bin`, `/sbin`, `/lib*`, `/var/lib` (docker data only through docker),
  `/var/backups`, `/proc`, `/sys`, `/dev`, `/run`, `/snap`, `/home/<user>` or `/root` themselves, and any path with a
  component `.ssh`, `.gnupg`, `.aws`, `.docker`, `.kube`, `.claude`, `.codex`, `.gemini`, `.guardian`, `.config`,
  `.local`, `server-guardian`, `guardian-workspace`, `guardian-eval` or `gitea`, or a secret-looking file
  (`*.enc`, `.env`, `*.key`, `*.pem`, `id_*`, `*credential*`, `*secret*`).
- Paths must be plain absolute paths shown by a check: no globs, no `..`, never across a mount point.
- Use `sudo -n` (never a password prompt). If it fails, report that the node has no passwordless sudo and stop.
- Before each action: measure, show what will be deleted and how much, ask "실행할까요?" and wait for a clear yes.
- After each action: measure again and report before -> after -> freed.

## Actions

| Action | Measure | Run |
|---|---|---|
| Journal down to a size (>= 100M) | `journalctl --disk-usage` | `sudo -n journalctl --vacuum-size=<500M\|1G\|...>` |
| apt package cache | `sudo -n du -sk /var/cache/apt/archives` | `sudo -n apt-get clean` |
| Dangling docker images | `docker images -f dangling=true` | `docker image prune -f` (or `sudo -n docker ...`) |
| Docker build cache | `docker system df` (Build Cache row) | `docker builder prune -f` |
| Old kernels (keep the running and the newest) | `uname -r` and the installed `linux-image-*` / `-headers-*` / `-modules-*` | `sudo -n apt-get purge -y -- <the old packages by name>` |
| Empty log files under /var/log | `sudo -n stat -c '%s %n' -- <files>` | `sudo -n truncate -s 0 -- <files>` (regular files only, not symlinks) |
| Remove named paths | `sudo -n du -sk -- <paths>` | refuse a mount point (`mountpoint -q`), then `sudo -n rm -rf --one-file-system -- <paths>` |

Keep audit and login history the operator may need: prefer a journal size that keeps recent weeks (1G or more on a
busy node) over the smallest one.

## Report

A short table: action, before, after, freed, and anything that failed with its error.
