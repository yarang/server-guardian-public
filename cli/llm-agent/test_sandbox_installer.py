"""Tests for the sandbox installer's O3 fixes (docs/design/resident-open-tools.md, O1-O3).

The first real open-agent turns on a node hit three defects the ssh-run O1 probe could not see:
systemd resolves --chdir before the launcher creates ~/work (the unit dies with status 200/CHDIR),
the worker unit's NoNewPrivileges=yes blocks the daemon's sudo to the wrapper, and a running systemd
user manager keeps the supplementary groups it started with. These tests lock the repository side of
the fixes by string checks on the scripts (running them needs root and systemd on a node):
the drop-in file's exact content, the work-dir step of ensure_agent, --no-restart parsing in both
scripts and the shipping of the drop-in. The sandbox version hash must stay the sha256 of exactly
the four files of test_sandbox_wrapper.py.SandboxVersion; the drop-in ships beside them, unhashed.

The 2026-11-19 defect pair is locked here as well: a reinstall without --agent dropped every
agent an earlier run had created from installed.json while their users stayed (the worker then
refused each of their turns with sandbox_agent_missing), and --remove --agent NAME deleted the
user but left NAME listed. AgentList checks both directions: existing_agents itself (run under
bash with a stubbed getent) and the do_install/do_remove wiring around it."""

import re
import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
DROPIN = ROOT / "deploy" / "worker" / "sandbox-sudo.conf"
INSTALLER = ROOT / "scripts" / "lib" / "sandbox-remote-install.sh"
ADMIN = ROOT / "scripts" / "install-sandbox.sh"

# test_sandbox_wrapper.py.SandboxVersion.FILES: the version hash covers exactly these.
HASHED_FILES = ("guardian-sandbox-run.sh", "limits.conf", "guardian-sandbox.sudoers",
                "guardian-sandbox-launch.py")


@unittest.skipUnless(INSTALLER.exists(), "the installer not found")
class WorkDir(unittest.TestCase):
    def ensure_agent_body(self):
        text = INSTALLER.read_text(encoding="utf-8")
        return text[text.index("ensure_agent()"):text.index("write_installed_json()")]

    def test_ensure_agent_creates_the_agent_owned_work_dir(self):
        # Defect 1: the daemon starts every turn with --chdir /home/gsa-<name>/work and systemd
        # resolves it before the launcher's ensure_dir, so the installer must leave it behind.
        body = self.ensure_agent_body()
        for needle in ('work="/home/${user}/work"',
                       'install -d -o "${user}" -g "${user}" -m 0700 "${work}"',
                       'chown "${user}:${user}" "${work}"',
                       'chmod 0700 "${work}"'):
            self.assertIn(needle, body, needle)

    def test_an_existing_work_dir_is_kept_but_links_and_files_refused(self):
        body = self.ensure_agent_body()
        self.assertIn('[ -L "${work}" ]', body)
        self.assertIn('[ -e "${work}" ] && [ ! -d "${work}" ]', body)
        self.assertNotIn('rm -rf "${work}"', body)  # the directory is never wiped
        self.assertIn('"/home/${user}/out"', body)
        self.assertIn('"/home/${user}/.local/bin"', body)


@unittest.skipUnless(DROPIN.exists() and INSTALLER.exists(), "drop-in or installer not found")
class Dropin(unittest.TestCase):
    def test_file_sets_exactly_no_new_privileges_under_service(self):
        # Defect 2: the drop-in must say nothing else, so it can never widen the worker unit.
        lines = [line.strip() for line in DROPIN.read_text(encoding="utf-8").splitlines()]
        body = [line for line in lines if line and not line.startswith("#")]
        self.assertEqual(body, ["[Service]", "NoNewPrivileges=no"])

    def test_installs_into_the_node_user_unit_tree_without_symlinked_components(self):
        text = INSTALLER.read_text(encoding="utf-8")
        body = text[text.index("install_dropin()"):text.index("# O3: a running systemd user manager")]
        for needle in ("guardian-worker.service.d", 'dst="${dir}/sandbox-sudo.conf"',
                       "for comp in .config systemd user guardian-worker.service.d",
                       'rm -f "${tmp}"',
                       'install -o "${NODE_USER}" -g "${group}" -m 0644 "${src}" "${tmp}"',
                       'cmp -s "${src}" "${dst}"'):
            self.assertIn(needle, body, needle)
        self.assertGreaterEqual(body.count('[ -L "${cur}" ]'), 1)

    def test_whole_remove_deletes_it_but_single_agent_remove_keeps_it(self):
        text = INSTALLER.read_text(encoding="utf-8")
        body = text[text.index("do_remove()"):]
        self.assertIn('dropin="${dir}/sandbox-sudo.conf"', body)
        self.assertIn('rm -f "${dropin}"', body)
        self.assertIn('rmdir "${dir}"', body)  # only when it is then empty
        single = body[:body.index("    while IFS= read -r user")]
        self.assertNotIn("sandbox-sudo.conf", single)  # --remove --agent leaves the drop-in


@unittest.skipUnless(INSTALLER.exists(), "the installer not found")
class AgentList(unittest.TestCase):
    def existing_agents_body(self):
        text = INSTALLER.read_text(encoding="utf-8")
        return text[text.index("existing_agents()"):text.index("do_install()")]

    def test_existing_agents_lists_only_valid_sandbox_agent_names(self):
        # Defect 1: the collector must see exactly the users the whole-remove branch removes,
        # minus the names the wrapper refuses (gsa-Bad!) and the users that are not ours
        # (gsa-other, ubuntu). Run for real under bash with a stubbed getent.
        if not shutil.which("bash"):
            self.skipTest("bash not available")
        text = INSTALLER.read_text(encoding="utf-8")
        # the filter must be the script's own name rule, not a copy of it
        name_re = re.search(r"^AGENT_NAME_RE='[^']*'$", text, re.MULTILINE)
        self.assertIsNotNone(name_re, "AGENT_NAME_RE not found in the installer")
        passwd = "\n".join([
            "gsa-coder:x:9001:9001:guardian sandbox agent coder:/home/gsa-coder:/usr/sbin/nologin",
            "gsa-probe:x:9002:9002:guardian sandbox agent probe:/home/gsa-probe:/usr/sbin/nologin",
            "gsa-other:x:9003:9003:something else:/home/gsa-other:/bin/bash",
            "ubuntu:x:1000:1000:Ubuntu:/home/ubuntu:/bin/bash",
            "gsa-Bad!:x:9004:9004:guardian sandbox agent Bad!:/home/gsa-Bad!:/bin/bash",
        ])
        script = (
            "getent() { printf '%s\\n' '" + passwd.replace("'", "'\\''") + "'; }\n"
            + name_re.group(0) + "\n"
            + self.existing_agents_body()
            + "existing_agents\n"
        )
        done = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=60)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(done.stdout, "coder\nprobe\n")

    def test_do_install_keeps_and_relists_the_agents_already_on_the_node(self):
        # Defect 1: reinstalling without --agent must not drop the earlier agents from
        # installed.json, so the collected set feeds both ensure_agent and write_installed_json.
        text = INSTALLER.read_text(encoding="utf-8")
        body = text[text.index("do_install()"):text.index("do_remove()")]
        self.assertIn('existing="$(existing_agents)"', body)
        self.assertIn('for name in probe probe2 ${AGENTS[@]+"${AGENTS[@]}"} ${existing}', body)
        self.assertIn('for name in ${agents[@]+"${agents[@]}"}; do\n        ensure_agent "${name}"',
                      body)
        self.assertIn('write_installed_json ${agents[@]+"${agents[@]}"} -- ${copied[@]+"${copied[@]}"',
                      body)
        self.assertIn("기존 에이전트 유지", body)

    def test_single_agent_remove_drops_the_name_from_installed_json(self):
        # Defect 2: --remove --agent NAME removed the user but left NAME in installed.json; the
        # rewrite must happen before the branch returns, and only the agents field may change.
        text = INSTALLER.read_text(encoding="utf-8")
        body = text[text.index("do_remove()"):]
        single = body[:body.index("    while IFS= read -r user")]
        ret = single.index("return 0")
        self.assertIn("${ETC_DIR}/installed.json", single[:ret])
        self.assertIn("python3 -I", single[:ret])
        self.assertIn('install_mv 0644 "${json_tmp}" "${ETC_DIR}/installed.json"', single[:ret])

    def test_single_agent_remove_keep_set(self):
        # The keep set of --remove --agent, run under bash with a stubbed existing_agents (one
        # name per line, as the real one prints): removed names leave even when their user
        # survived a failed userdel, probe and probe2 stay unless removed and really gone.
        if not shutil.which("bash"):
            self.skipTest("bash not available")
        text = INSTALLER.read_text(encoding="utf-8")
        body = text[text.index("do_remove()"):]
        start = body.index('        present="$(existing_agents')
        block = body[start:body.index("\n        done\n", start) + len("\n        done\n")]
        cases = [
            ("coder", "probe probe2 coder", "probe probe2"),
            ("coder", "probe probe2", "probe probe2"),
            ("probe", "probe2", "probe2"),
            ("probe", "probe probe2", "probe probe2"),
            ("coder x", "probe probe2 x", "probe probe2"),
        ]
        for removed, present, want in cases:
            script = (
                "set -u\nAGENTS=(" + removed + ")\nkeep=''\n"
                "existing_agents() { printf '%s\\n' " + present + "; }\n"
                + block + 'printf "%s" "${keep}"\n'
            )
            done = subprocess.run(["bash", "-c", script], capture_output=True, text=True,
                                  timeout=60)
            self.assertEqual(done.returncode, 0, done.stderr)
            self.assertEqual(done.stdout, want, (removed, present))


@unittest.skipUnless(INSTALLER.exists() and ADMIN.exists(), "installer scripts not found")
class NoRestart(unittest.TestCase):
    def test_parsed_in_both_scripts(self):
        # Defect 3: the flag must reach the remote installer, which owns the restart decision.
        self.assertIn("--no-restart) NO_RESTART=1", INSTALLER.read_text(encoding="utf-8"))
        self.assertIn("--no-restart) NORESTART=1", ADMIN.read_text(encoding="utf-8"))
        forwarded = ADMIN.read_text(encoding="utf-8")
        self.assertIn('remote_args+=" --no-restart"', forwarded)

    def test_restart_only_when_a_group_or_the_dropin_demands_it(self):
        text = INSTALLER.read_text(encoding="utf-8")
        body = text[text.index("sync_user_manager()"):text.index("remove_agent()")]
        for needle in ('systemctl is-active --quiet "user@${uid}.service"',
                       '[ "${missing}" = 1 ] || [ "${DROPIN_CHANGED}" = 1 ] || return 0',
                       'systemctl restart "user@${uid}.service"',
                       'systemctl --user -M "${NODE_USER}@" daemon-reload'):
            self.assertIn(needle, body, needle)


@unittest.skipUnless(ADMIN.exists(), "install-sandbox.sh not found")
class Shipping(unittest.TestCase):
    def test_install_sandbox_ships_and_passes_the_dropin(self):
        text = ADMIN.read_text(encoding="utf-8")
        self.assertIn("SHIP_PATHS=(deploy/sandbox deploy/worker/sandbox-sudo.conf "
                      "scripts/lib/sandbox-remote-install.sh)", text)
        self.assertIn('--dropin \\"\\$d/deploy/worker/sandbox-sudo.conf\\"', text)


@unittest.skipUnless((ROOT / "deploy" / "sandbox").is_dir(), "deploy/sandbox not found")
class VersionHashUnchanged(unittest.TestCase):
    def test_deploy_sandbox_holds_exactly_the_four_hashed_files(self):
        # The drop-in must never drift into the hashed set (test_sandbox_wrapper.py locks the
        # formula; this locks the directory), or nodes and hub would disagree on the version.
        names = sorted(p.name for p in (ROOT / "deploy" / "sandbox").iterdir())
        self.assertEqual(names, sorted(HASHED_FILES))


if __name__ == "__main__":
    unittest.main()
