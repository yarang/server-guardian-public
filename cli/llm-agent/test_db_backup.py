import datetime
import os
import sqlite3
import tarfile
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import db_backup
from db_manager import DecisionDBManager, decisions_dir, default_db_path


class DataPathTests(unittest.TestCase):
    def test_db_lives_in_data_dir_outside_the_repo(self) -> None:
        with TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"GUARDIAN_DATA_DIR": tmp}):
                os.environ.pop("GUARDIAN_DB_PATH", None)
                self.assertEqual(default_db_path(), Path(tmp) / "guardian_decisions.db")
                self.assertEqual(decisions_dir(), Path(tmp) / "decisions")
                self.assertTrue(decisions_dir().is_dir())

    def test_db_path_override(self) -> None:
        with TemporaryDirectory() as tmp:
            target = Path(tmp) / "nested" / "x.db"
            with patch.dict(os.environ, {"GUARDIAN_DB_PATH": str(target)}):
                db = DecisionDBManager()
            self.assertEqual(db.db_path, target)
            self.assertTrue(target.exists())


class BackupTests(unittest.TestCase):
    def test_backup_copies_rows_and_prunes(self) -> None:
        with TemporaryDirectory() as tmp:
            source = Path(tmp) / "live.db"
            db = DecisionDBManager(source)
            db.save_agent_session("t1", "Critic Agent", "claude", "s-1")
            dest = Path(tmp) / "backups"
            base = datetime.datetime(2026, 10, 3, 12, 0, 0)
            for i in range(4):
                db_backup.backup_database(source, dest, keep=3, now=base + datetime.timedelta(days=i))

            files = sorted(p.name for p in dest.glob("*.db"))
            self.assertEqual(files, [
                "guardian_decisions-20261004_120000.db",
                "guardian_decisions-20261005_120000.db",
                "guardian_decisions-20261006_120000.db",
            ])
            self.assertFalse(list(dest.glob("*.partial")))
            newest = dest / files[-1]
            self.assertEqual(oct(newest.stat().st_mode & 0o777), "0o600")
            with sqlite3.connect(newest) as conn:
                self.assertEqual(conn.execute("select session_id from agent_sessions").fetchone()[0], "s-1")

    @patch("db_backup.subprocess.run")
    def test_replicate_copies_then_prunes_remote(self, run) -> None:
        backup = Path("/tmp/guardian_decisions-20261003_120000.db")
        db_backup.replicate_backup(backup, "site-yd-arm1", keep=14)

        calls = [c.args[0] for c in run.call_args_list]
        self.assertEqual(calls[0][0], "ssh")
        self.assertEqual(calls[1][0], "scp")
        self.assertEqual(calls[1][-1], "site-yd-arm1:.guardian/hub-backups/guardian_decisions-20261003_120000.db")
        self.assertIn("head -n -14", calls[2][-1])
        self.assertTrue(all("BatchMode=yes" in c for c in calls))

    @patch("db_backup.replicate_backup", side_effect=OSError("ssh down"))
    def test_replication_failure_is_reported(self, _rep) -> None:
        with TemporaryDirectory() as tmp:
            source = Path(tmp) / "live.db"
            DecisionDBManager(source)
            env = {"GUARDIAN_DB_PATH": str(source), "GUARDIAN_DATA_DIR": tmp}
            with patch.dict(os.environ, env):
                self.assertEqual(db_backup.main(["--replicate", "somewhere"]), 2)
                self.assertEqual(len(list((Path(tmp) / "backups").glob("*.db"))), 1)

    def test_missing_source_fails(self) -> None:
        with TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"GUARDIAN_DB_PATH": str(Path(tmp) / "none.db"), "GUARDIAN_DATA_DIR": tmp}):
                self.assertEqual(db_backup.main([]), 1)


def make_data_dir(root: Path) -> Path:
    data = root / "data"
    (data / "backups").mkdir(parents=True)
    (data / "backups" / "guardian_decisions-20260101_000000.db").write_text("old backup")
    (data / "debate-jobs" / "job1").mkdir(parents=True)
    (data / "debate-jobs" / "job1" / "output.log").write_text("x" * 1000)
    (data / "debate-jobs" / "job1" / "job.json").write_text("{}")
    (data / "teams").mkdir()
    (data / "teams" / "cross.json").write_text("{}")
    (data / "servers.json").write_text("[]")
    DecisionDBManager(data / "guardian_decisions.db")
    return data


def make_ssh_dir(root: Path) -> Path:
    ssh = root / "ssh"
    ssh.mkdir()
    (ssh / "guardian_fleet.conf").write_text("Host a\n")
    (ssh / "guardian_known_hosts").write_text("a ssh-ed25519 AAAA\n")
    (ssh / "guardian_fleet").write_text("PRIVATE KEY")
    (ssh / "id_ed25519").write_text("PRIVATE KEY")
    (ssh / "authorized_keys").write_text("ssh-ed25519 AAAA")
    return ssh


class DataBackupTests(unittest.TestCase):
    def test_tarball_has_data_and_fleet_files_without_excluded_or_private_files(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            data = make_data_dir(root)
            ssh = make_ssh_dir(root)
            dest = data / "backups"
            target = db_backup.backup_data(
                data, dest, keep=5, ssh_dir=ssh, skip=[data / "guardian_decisions.db"],
                now=datetime.datetime(2026, 10, 3, 12, 0, 0),
            )

            self.assertEqual(target.name, "guardian_data-20261003_120000.tar.gz")
            self.assertEqual(oct(target.stat().st_mode & 0o777), "0o600")
            self.assertFalse(list(dest.glob("*.partial")))
            with tarfile.open(target) as tar:
                names = set(tar.getnames())
            self.assertIn("data/servers.json", names)
            self.assertIn("data/teams/cross.json", names)
            self.assertIn("data/debate-jobs/job1/job.json", names)
            self.assertIn("fleet/guardian_fleet.conf", names)
            self.assertIn("fleet/guardian_known_hosts", names)
            self.assertNotIn("data/guardian_decisions.db", names)
            self.assertFalse([n for n in names if n.startswith("data/backups")])
            self.assertFalse([n for n in names if n.endswith("output.log")])
            self.assertFalse([n for n in names if "id_ed25519" in n or n.endswith("guardian_fleet") or "authorized_keys" in n])

    def test_code_index_cache_is_left_out_of_the_tarball(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            data = make_data_dir(root)
            blob = data / "code-index" / "proj" / "blobs" / "ab" / ("ab" + "0" * 38 + ".json")
            blob.parent.mkdir(parents=True)
            blob.write_text("{}")
            (data / "code-index-notes.json").write_text("{}")  # only the directory named code-index is a cache
            target = db_backup.backup_data(data, data / "backups", keep=5, ssh_dir=root / "none",
                                           now=datetime.datetime(2026, 10, 3, 12, 0, 0))
            with tarfile.open(target) as tar:
                names = set(tar.getnames())
            self.assertFalse([n for n in names if n.startswith("data/code-index/") or n == "data/code-index"])
            self.assertIn("data/code-index-notes.json", names)
            self.assertIn("data/servers.json", names)

    def test_design_artifacts_stay_but_their_derived_cache_is_left_out(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            data = make_data_dir(root)
            version = data / "design-artifacts" / "arch" / "thread_1" / "v1"
            version.mkdir(parents=True)
            (version / "a.md").write_text("# A\n", encoding="utf-8")
            (version / "manifest.json").write_text("{}", encoding="utf-8")
            cache = data / "design-artifacts" / "arch" / ".cache"
            cache.mkdir()
            (cache / "preview.png").write_bytes(b"png")
            other = data / "teams" / ".cache"  # only the artifact store has this rule
            other.mkdir()
            (other / "keep.json").write_text("{}", encoding="utf-8")
            target = db_backup.backup_data(data, data / "backups", keep=5, ssh_dir=root / "none",
                                           now=datetime.datetime(2026, 10, 3, 12, 0, 0))
            with tarfile.open(target) as tar:
                names = set(tar.getnames())
            self.assertIn("data/design-artifacts/arch/thread_1/v1/a.md", names)
            self.assertIn("data/design-artifacts/arch/thread_1/v1/manifest.json", names)
            self.assertFalse([n for n in names if ".cache" in n and n.startswith("data/design-artifacts")])
            self.assertIn("data/teams/.cache/keep.json", names)

    def test_code_wiki_pages_are_user_content_and_stay_in_the_tarball(self) -> None:
        """Unlike code-index/ (a cache), code-wiki/ holds pages people edit and sessions took time to write."""
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            data = make_data_dir(root)
            page = data / "code-wiki" / "proj" / "pages" / "cli-llm-agent.md"
            page.parent.mkdir(parents=True)
            page.write_text("---\nid: \"cli-llm-agent\"\n---\n## 역할\n", encoding="utf-8")
            (data / "code-wiki" / "proj" / "build.lock").write_text("123\n")
            target = db_backup.backup_data(data, data / "backups", keep=5, ssh_dir=root / "none",
                                           now=datetime.datetime(2026, 10, 3, 12, 0, 0))
            with tarfile.open(target) as tar:
                names = set(tar.getnames())
                content = tar.extractfile("data/code-wiki/proj/pages/cli-llm-agent.md").read().decode("utf-8")
            self.assertIn("data/code-wiki/proj/pages/cli-llm-agent.md", names)
            self.assertIn("## 역할", content)

    def test_missing_ssh_files_are_fine_and_old_tarballs_are_pruned(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            data = make_data_dir(root)
            dest = root / "out"
            base = datetime.datetime(2026, 10, 3, 12, 0, 0)
            for i in range(4):
                db_backup.backup_data(data, dest, keep=2, ssh_dir=root / "no-ssh", now=base + datetime.timedelta(days=i))
            files = sorted(p.name for p in dest.glob("guardian_data-*.tar.gz"))
            self.assertEqual(files, ["guardian_data-20261005_120000.tar.gz", "guardian_data-20261006_120000.tar.gz"])
            with tarfile.open(dest / files[-1]) as tar:
                self.assertFalse([n for n in tar.getnames() if n.startswith("fleet/")])

    def test_data_dir_backup_does_not_prune_db_copies(self) -> None:
        with TemporaryDirectory() as tmp:
            data = make_data_dir(Path(tmp))
            dest = data / "backups"
            db_backup.backup_data(data, dest, keep=1)
            self.assertTrue((dest / "guardian_decisions-20260101_000000.db").exists())

    def test_missing_data_dir_fails(self) -> None:
        with TemporaryDirectory() as tmp:
            with self.assertRaises(FileNotFoundError):
                db_backup.backup_data(Path(tmp) / "none", Path(tmp) / "out", keep=1)

    @patch("db_backup.subprocess.run")
    def test_replicate_data_tarball_prunes_by_its_own_pattern(self, run) -> None:
        db_backup.replicate_backup(Path("/tmp/guardian_data-20261003_120000.tar.gz"), "hub-replica", keep=7,
                                   pattern=db_backup.DATA_PATTERN)
        calls = [c.args[0] for c in run.call_args_list]
        self.assertEqual(calls[1][-1], "hub-replica:.guardian/hub-backups/guardian_data-20261003_120000.tar.gz")
        self.assertIn("ls -1 guardian_data-*.tar.gz", calls[2][-1])
        self.assertIn("head -n -7", calls[2][-1])

    @patch("db_backup.replicate_backup")
    def test_main_writes_db_and_data_backups_and_replicates_both(self, rep) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            data = make_data_dir(root)
            ssh = make_ssh_dir(root)
            with patch.dict(os.environ, {"GUARDIAN_DATA_DIR": str(data)}):
                os.environ.pop("GUARDIAN_DB_PATH", None)
                self.assertEqual(db_backup.main(["--replicate", "spare", "--ssh-dir", str(ssh)]), 0)
            self.assertEqual(len(list((data / "backups").glob("guardian_decisions-*.db"))), 2)
            tarballs = list((data / "backups").glob("guardian_data-*.tar.gz"))
            self.assertEqual(len(tarballs), 1)
            with tarfile.open(tarballs[0]) as tar:
                self.assertNotIn("data/guardian_decisions.db", tar.getnames())
            sent = sorted(c.args[0].name.split("-")[0] for c in rep.call_args_list)
            self.assertEqual(sent, ["guardian_data", "guardian_decisions"])

    def test_no_data_flag_skips_the_tarball(self) -> None:
        with TemporaryDirectory() as tmp:
            data = make_data_dir(Path(tmp))
            with patch.dict(os.environ, {"GUARDIAN_DATA_DIR": str(data)}):
                os.environ.pop("GUARDIAN_DB_PATH", None)
                self.assertEqual(db_backup.main(["--no-data"]), 0)
            self.assertFalse(list((data / "backups").glob("guardian_data-*")))

    @patch("db_backup.backup_data", side_effect=OSError("disk full"))
    def test_data_backup_failure_has_its_own_exit_status(self, _bd) -> None:
        with TemporaryDirectory() as tmp:
            data = make_data_dir(Path(tmp))
            with patch.dict(os.environ, {"GUARDIAN_DATA_DIR": str(data)}):
                os.environ.pop("GUARDIAN_DB_PATH", None)
                self.assertEqual(db_backup.main([]), 3)
            self.assertEqual(len(list((data / "backups").glob("guardian_decisions-*.db"))), 2)


if __name__ == "__main__":
    unittest.main()
