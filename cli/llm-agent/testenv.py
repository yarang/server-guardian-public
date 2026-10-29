"""
Test isolation shared by the test modules: keeps test runs out of the real data directory.

Debate tests that give the engine a git workspace build the symbol index, whose cache defaults to
<GUARDIAN_DATA_DIR>/code-index. Without isolation a test run (also the hub project's own unittest
command) would write cache files into ~/.guardian/data, next to the production decision DB.

The same workspaces can hold a go.mod (this repository does), and a goindex helper on the machine (the hub has one)
would then start a real extraction. isolate_data_dir() therefore also points GUARDIAN_GOINDEX_BIN at a file that does
not exist; tests of the extraction itself set it to their own fake or built helper.
"""

import os
import unittest
from unittest.mock import patch
from tempfile import TemporaryDirectory
from typing import List, Optional, Tuple

_stack: List[Tuple[TemporaryDirectory, Optional[str], Optional[str]]] = []


def isolate_data_dir() -> str:
    """Points GUARDIAN_DATA_DIR at a fresh temporary directory until restore_data_dir(); returns its path."""
    tmp = TemporaryDirectory(prefix="guardian-test-data-")
    _stack.append((tmp, os.environ.get("GUARDIAN_DATA_DIR"), os.environ.get("GUARDIAN_GOINDEX_BIN")))
    os.environ["GUARDIAN_DATA_DIR"] = tmp.name
    os.environ["GUARDIAN_GOINDEX_BIN"] = os.path.join(tmp.name, "no-goindex-helper")
    return tmp.name


def restore_data_dir() -> None:
    """Undoes the latest isolate_data_dir(): restores the previous value and removes the directory."""
    if not _stack:
        return
    tmp, previous, previous_helper = _stack.pop()
    for name, value in (("GUARDIAN_DATA_DIR", previous), ("GUARDIAN_GOINDEX_BIN", previous_helper)):
        if value is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = value
    tmp.cleanup()


def set_auto_apply(case: unittest.TestCase, value: str = "off") -> None:
    """Sets GUARDIAN_AUTO_APPLY for one test case (reverted at cleanup).

    Engine tests that check the PROPOSED -> approval -> EXECUTED flow turn auto-apply off; without it the
    engine runs the judge's code changes itself (action_ops.auto_run_actions).
    """
    env = patch.dict(os.environ, {"GUARDIAN_AUTO_APPLY": value})
    env.start()
    case.addCleanup(env.stop)
