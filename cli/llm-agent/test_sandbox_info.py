"""sandbox_info: the release's sandbox version (O2 shared contract C1) and the open-agent name
rule (C8). The version runs against a temp tree, because the launcher file of the four arrives
with another brief and the checkout may not hold it yet."""

import hashlib
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Dict, Optional

import sandbox_info


def make_tree(directory: str, contents: Optional[Dict[str, bytes]] = None) -> Path:
    """A root holding the four sandbox files; `contents` overrides single files' bytes."""
    files: Dict[str, bytes] = {relative: f"# {relative}\n".encode("utf-8")
                               for relative in sandbox_info.SANDBOX_FILES}
    files.update(contents or {})
    root = Path(directory)
    for relative, data in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    return root


class SandboxVersion(unittest.TestCase):
    def test_equals_the_sha256_of_the_four_files_in_order(self):
        with TemporaryDirectory() as tmp:
            root = make_tree(tmp)
            digest = hashlib.sha256()
            for relative in sandbox_info.SANDBOX_FILES:
                digest.update((root / relative).read_bytes())
            self.assertEqual(sandbox_info.sandbox_version(root), digest.hexdigest()[:12])

    def test_the_files_are_the_contracts_four_in_its_order(self):
        self.assertEqual(sandbox_info.SANDBOX_FILES, (
            "deploy/sandbox/guardian-sandbox-run.sh",
            "deploy/sandbox/limits.conf",
            "deploy/sandbox/guardian-sandbox.sudoers",
            "deploy/sandbox/guardian-sandbox-launch.py",
        ))

    def test_a_change_of_any_file_changes_the_version(self):
        with TemporaryDirectory() as tmp:
            root = make_tree(tmp)
            before = sandbox_info.sandbox_version(root)
            for relative in sandbox_info.SANDBOX_FILES:
                path = root / relative
                path.write_bytes(path.read_bytes() + b"# changed\n")
                after = sandbox_info.sandbox_version(root)
                self.assertNotEqual(after, before, f"{relative} did not change the version")
                before = after

    def test_a_missing_file_raises_naming_it(self):
        with TemporaryDirectory() as tmp:
            root = make_tree(tmp)
            (root / "deploy/sandbox" / "limits.conf").unlink()
            with self.assertRaises(sandbox_info.SandboxVersionError) as caught:
                sandbox_info.sandbox_version(root)
            self.assertIn("limits.conf", str(caught.exception))

    def test_the_default_root_is_this_repository(self):
        # The default root is the module's repository root: with the launcher still missing the
        # call raises naming a repository path; once it ships the explicit root agrees.
        explicit = Path(__file__).resolve().parents[2]
        try:
            version = sandbox_info.sandbox_version()
        except sandbox_info.SandboxVersionError as exc:
            self.assertIn("deploy/sandbox", str(exc))
        else:
            self.assertEqual(version, sandbox_info.sandbox_version(explicit))


class SandboxNameRule(unittest.TestCase):
    """C8: the resident name is the Unix user gsa-<name>, so it is stricter than a mail name."""

    def test_the_shape(self):
        for good in ("a", "claude", "a1b2c3d4", "x" * 16):
            self.assertIsNotNone(sandbox_info.SANDBOX_NAME_RE.fullmatch(good), good)
        for bad in ("", "Claude", "1abc", "agent-one", "a b", "x" * 17, "claude\n", "-abc"):
            self.assertIsNone(sandbox_info.SANDBOX_NAME_RE.fullmatch(bad), bad)


if __name__ == "__main__":
    unittest.main()
