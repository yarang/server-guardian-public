# Hub backup and restore

English | [한국어](backup-restore.ko.md)

How the state of the hub (`site-a-arm1`) is backed up every day, and how to restore it when the hub disk is lost. The
backup is made by `server-guardian-db-backup.timer` (daily at 03:30 UTC, random delay of up to 15 minutes), which runs
`cli/llm-agent/db_backup.py --keep 14`. The hub's `server-guardian-ops-watch.timer` (nightly ops watch) runs earlier,
at 02:30 UTC, so a debate it starts is in that night's backup. Every command in this document runs on the hub (or on
the node that keeps the copies). Practise a restore in a temporary directory first, not on the real hub (drill
procedure at the end).

## What is backed up

Two kinds of files accumulate in `~/.guardian/data/backups/`, 14 of each (`--keep`).

| File | Contents |
|---|---|
| `guardian_decisions-<date_time>.db` | The decision DB. A consistent copy made with SQLite's online backup API |
| `guardian_data-<date_time>.tar.gz` | `data/` = all of `~/.guardian/data` (`servers.json` inventory, teams, personas, projects, debate job records, jev ADR DB, code wiki pages `code-wiki/`, audit log `audit/`, ops watch state `ops-watch/`, ...), `fleet/` = `~/.ssh/guardian_fleet.conf`, `~/.ssh/guardian_known_hosts` (when present) |

The backup service reads `~/.guardian/backup.env`, which the hub install (`scripts/install-agent-service.sh`) writes:
- `GUARDIAN_BACKUP_REPLICA`: the `backup_replica` of the hub entry in the hub inventory `~/.guardian/data/servers.json`.
  Only with `--offline` is the local snapshot `configs/servers.json` read instead.
- `GUARDIAN_DATA_DIR`, `GUARDIAN_DB_PATH`: copied from `~/.guardian/agent.env` when set there. The backup service
  cannot read `agent.env` because of its sandbox; without this copy a hub with a custom location would back up the
  default one.

With `GUARDIAN_BACKUP_REPLICA` set, every new file is also copied to `<replica>:~/.guardian/hub-backups/`, which also
keeps only 14. The copy uses the fleet key through the ssh-agent (`guardian-fleet-agent.service`).

Not in the backup:
- **Private keys and secrets**: `/etc/guardian/fleet_key` (fleet private key), private keys in `~/.ssh`,
  `~/.guardian/agent.env` (AGENT_TOKEN), `~/.guardian/llm.env` (LLM credentials). The service sandbox
  (`InaccessiblePaths`) blocks them, and the code reads only the two allowlisted files (`guardian_fleet.conf`,
  `guardian_known_hosts`).
- `backups/` itself, `code-index/` (code index cache, rebuilt on demand and pruned by
  `GUARDIAN_CODE_INDEX_MAX_AGE_DAYS`/`GUARDIAN_CODE_INDEX_MAX_MB`), every `*.log` (`debate-jobs/*/output.log` is the
  largest), `*.partial` files being written, and the live files of the decision DB (`guardian_decisions.db`, `-wal`,
  `-shm`, `-journal`). The consistent `.db` copy above replaces the DB.
- The installed releases (`~/.guardian/releases`) and code repositories (`~/server-guardian`, workspace clones):
  installed and fetched again (redeploy, git).

Unlike `code-index/`, `code-wiki/` (code wiki pages, `~/.guardian/data/code-wiki/<project>/pages/*.md`) **is backed
up**. Pages edited by people and narratives that LLM sessions took time to write cannot be regenerated, or only at a
cost. The `build.lock` in the same directory is an empty file marking a write in progress and is harmless to restore
(the lock is held by a process, not by the file's contents). For the same reason the node lock files of ops actions
(`ops-locks/<host>.lock`) are harmless to restore.

Note: the tarball packs files as they are while the hub runs. JSON that a debate is writing, or the jev DB, may rarely
be caught midway, so go through the checks after a restore (step 5 below). The tarball holds the inventory and jev
data, so its mode is `600`, and the node that keeps the copies also keeps `~/.guardian/hub-backups` at `700`.

## Restore procedure

### 0. Preparation

Pick the backup to restore. If the hub disk is still there, use `~/.guardian/data/backups/`; if it was lost, fetch from
the replica node.

```bash
# Fetch the latest two files from the replica node (on a rebuilt hub, run this on the new hub)
REPLICA=<backup_replica value>
ssh "$REPLICA" 'ls -1 ~/.guardian/hub-backups | tail -n 4'
mkdir -p ~/restore && cd ~/restore
scp "$REPLICA:.guardian/hub-backups/guardian_decisions-<date_time>.db" .
scp "$REPLICA:.guardian/hub-backups/guardian_data-<date_time>.tar.gz" .
```

If the hub itself was rebuilt, first install the hub from the Mac (the hub has no inventory copy yet, so only this
first install uses `--offline`). `--offline` reads the Mac's local snapshot `configs/servers.json` (git-ignored, fetched
earlier with `scripts/pull-inventory.sh`), so that file must exist. The restore comes after this install, so the
initial `servers.json` the install creates is replaced by the backup in step 3. Moving the hub to a new id also needs
`--offline`, because the old hub's inventory cannot confirm the role.

```bash
scripts/install-agent-service.sh site-a-arm1 --offline
```

On a new hub this install behaves as follows. The order is **install (--offline) -> restore -> `setup-hub-ssh.sh` ->
install again**.
- The hub has no fleet key, so the install creates a new one at `/etc/guardian/fleet_key`
  (`deploy/hub/protect-fleet-key.sh`).
- That key is not registered on any node yet, so fleet SSH fails in the isolation check at the end of the install and
  the install ends with exit code 1. Until step 4 this is **the expected result**.
- After step 4 registers the key on the nodes with `setup-hub-ssh.sh`, running the install once more passes the
  isolation check.

### 1. Stop the hub agent and the nightly watch

Keep the hub agent and debate jobs from writing files during the restore. The ops watch service needs the hub agent
(`Wants=`), so when its timer fires it can start the hub agent again and start a debate. Stop the timer too.

```bash
sudo systemctl stop server-guardian-ops-watch.timer
sudo systemctl stop server-guardian-agent
# Check that no debate process is still running
pgrep -af guardian.py || echo "none"
```

### 2. Restore the decision DB

Do not delete the current files; move them aside. `-wal`/`-shm` belong to the previous DB, so move them too.

```bash
cd ~/.guardian/data
stamp=$(date +%Y%m%d_%H%M%S)
mkdir -p pre-restore-$stamp
mv guardian_decisions.db guardian_decisions.db-wal guardian_decisions.db-shm pre-restore-$stamp/ 2>/dev/null || true
cp ~/restore/guardian_decisions-<date_time>.db guardian_decisions.db
chmod 600 guardian_decisions.db
python3 -c "import sqlite3; print(sqlite3.connect('guardian_decisions.db').execute('PRAGMA integrity_check').fetchone()[0])"   # must print ok (the hub has no sqlite3 command)
```

On a hub that moved the DB with `GUARDIAN_DB_PATH`, copy it to that path (check `~/.guardian/agent.env`).

### 3. Restore the data directory

The tarball has two parts, `data/` and `fleet/`. Unpack it in a temporary location first, look at it, and copy only
what you need into place.

```bash
cd ~/restore && mkdir -p x && tar -xzf guardian_data-<date_time>.tar.gz -C x
ls x/data x/fleet
# The DB was restored in step 2 and is not in the tarball. Overwrite the rest (inventory, teams, personas, projects, debate records, jev).
# Skip audit/: the hub's live audit log (audit/exec.jsonl) is newer than the backup and must not be overwritten.
(cd x/data && tar -cf - --exclude=./audit .) | tar -xf - -C ~/.guardian/data
chmod 700 ~/.guardian/data
# Keep the backup's audit log alongside; merge it by hand only if the live log is empty or lost.
[ ! -d x/data/audit ] || cp -a x/data/audit ~/restore/audit-from-backup
```

Do not restore the audit log over the live file. That would erase the audit records written after the backup.

Only overwritten files change; files created after the backup stay. To go back to the backup state completely, empty
`~/.guardian/data` except `backups/`, `pre-restore-*` and the restored DB, then copy.

`ops-watch/` also goes back to the backup state, so a finding that already started a debate after the backup may start
another debate at the next nightly watch.

### 4. Restore the fleet SSH files

```bash
install -m 600 ~/restore/x/fleet/guardian_fleet.conf ~/.ssh/guardian_fleet.conf
install -m 600 ~/restore/x/fleet/guardian_known_hosts ~/.ssh/guardian_known_hosts   # only if in the tarball
touch ~/.ssh/config && chmod 600 ~/.ssh/config   # a new hub may not have the file
grep -q 'Include ~/.ssh/guardian_fleet.conf' ~/.ssh/config || sed -i '1i Include ~/.ssh/guardian_fleet.conf' ~/.ssh/config
```

`~/.ssh/known_hosts` is not in the backup. Nodes registered from the dashboard have their host keys pinned in
`guardian_known_hosts` and come back with it, but nodes migrated by script (`accept-new`) trust their host key anew on
the first connection after the hub is rebuilt (TOFU).

The fleet **private key is not in the backup.** If the key survived (`/etc/guardian/fleet_key`,
`guardian-fleet-agent.service`), keep using it. If the hub was rebuilt and has no key, register one on the nodes from
the Mac with `setup-hub-ssh.sh`. On every node that script removes the old lines with the `guardian-hub-fleet` comment
and adds **the current hub key**, so no separate `--revoke` is needed after a reinstall. (If you run it anyway,
`--revoke` removes only lines whose key body matches. Its default target is the hub's current key, so it would remove
the new key. To revoke the old key, name it: `--revoke --key <old public key line|file|SHA256:..>`.)

```bash
scripts/setup-hub-ssh.sh          # issue, register and verify the new hub key (reads the hub inventory)
scripts/install-agent-service.sh site-a-arm1 --offline   # run again to pass the isolation check
```

To change the key on purpose, use `scripts/setup-hub-ssh.sh --rotate` (`--dry-run` first). It adds the new key to
every node, switches the hub key only after a login with the new key works, and removes the old key by its body only on
nodes where that login was confirmed. If anything fails, it removes the new key again and leaves the old key in place.

### 5. Start the hub agent and check

```bash
sudo systemctl start server-guardian-agent
systemctl is-active server-guardian-agent
sudo systemctl start server-guardian-ops-watch.timer
set -a; . ~/.guardian/agent.env; set +a
curl -s -H "Authorization: Bearer $AGENT_TOKEN" http://127.0.0.1:9870/api/status | head -c 300; echo
```

Checklist:
- The dashboard shows the restored nodes (Fleet), the same list as the hub inventory:
  `jq length ~/.guardian/data/servers.json`.
- Earlier debate threads and actions are there: `cd ~/.guardian/current && python3 cli/llm-agent/guardian.py --actions`
  and `--sessions`.
- Node groups, teams and projects are there: `--groups`, `--teams`, `--projects`.
- Code wiki pages are there: `cd ~/.guardian/current && python3 cli/llm-agent/guardian.py --code-wiki-list --project server-guardian`
  (JSON; `state` is `fresh`/`stale`/`broken` against the HEAD of the workspace clone). All wiki commands are in
  "Implementation notes (P3)" of [debate-code-index-wiki.md](../design/debate-code-index-wiki.md).
- Fleet SSH works: `ssh -o BatchMode=yes <node id> hostname`, and the SSH audit on the dashboard.
- Both timers are scheduled again: `systemctl list-timers 'server-guardian-*' --no-pager`.
- The backup runs again: `sudo systemctl start server-guardian-db-backup.service && ls -l ~/.guardian/data/backups | tail -n 4`.
- Check the nightly watch with a dry run (starts no debate): `cd ~/.guardian/current/cli/llm-agent && set -a && . ~/.guardian/agent.env && set +a && python3 guardian.py --ops-watch --dry-run`.

If something is wrong, go back to the previous DB kept in `~/.guardian/data/pre-restore-<stamp>/` (stop the hub agent and
reverse step 2).

The Mac's `configs/servers.json` is a git-ignored local snapshot, so after the restore fetch the hub inventory again
with `scripts/pull-inventory.sh` (the next `--offline` install uses it).

## Restore drill (without touching the real hub)

Once a quarter, check in a temporary directory that the latest copy restores.

```bash
d=$(mktemp -d) && cd "$d"
scp "$REPLICA:.guardian/hub-backups/guardian_decisions-<latest>.db" .
scp "$REPLICA:.guardian/hub-backups/guardian_data-<latest>.tar.gz" .
for f in guardian_decisions-*.db; do python3 -c "import sqlite3,sys; c=sqlite3.connect(sys.argv[1]); print(sys.argv[1], c.execute('PRAGMA integrity_check').fetchone()[0], c.execute('SELECT count(*) FROM agent_sessions').fetchone()[0])" "$f"; done
mkdir x && tar -xzf guardian_data-*.tar.gz -C x && ls x/data x/fleet && jq length x/data/servers.json
tar -tzf guardian_data-*.tar.gz | grep -Ei 'fleet_key|id_(rsa|ed25519)|\.log$' && echo "contains forbidden files" || echo "no secrets or logs"
```

All three must pass: integrity `ok`, the inventory and team files unpack, no secrets or logs.
Record the drill date and result as one line at the end of this document.

## Failure alerts

The exit codes of `db_backup.py` are `1` DB backup failed, `2` copy to the replica failed, `3` data tarball failed (the
first failure wins; the remaining steps are still attempted). systemd records the failed service, so check with
`systemctl --failed` and `journalctl -u server-guardian-db-backup.service`. There is no automatic alert yet.
