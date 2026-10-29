"""The host that runs the engine, and so every agent's LLM CLI, is recorded in the run's first event."""
import os
import unittest
from unittest import mock

import debate_events


class EngineHostTests(unittest.TestCase):
    def test_label_from_the_environment_wins_over_the_hostname(self) -> None:
        with mock.patch.dict(os.environ, {"GUARDIAN_HOST_LABEL": "  hub-1  "}):
            self.assertEqual(debate_events.engine_host(), "hub-1")

    def test_hostname_is_the_default(self) -> None:
        env = {k: v for k, v in os.environ.items() if k != "GUARDIAN_HOST_LABEL"}
        with mock.patch.dict(os.environ, env, clear=True), mock.patch("socket.gethostname", return_value="node-1"):
            self.assertEqual(debate_events.engine_host(), "node-1")

    def test_an_unreadable_hostname_gives_an_empty_value_and_long_labels_are_cut(self) -> None:
        env = {k: v for k, v in os.environ.items() if k != "GUARDIAN_HOST_LABEL"}
        with mock.patch.dict(os.environ, env, clear=True), mock.patch("socket.gethostname", side_effect=OSError):
            self.assertEqual(debate_events.engine_host(), "")
        with mock.patch.dict(os.environ, {"GUARDIAN_HOST_LABEL": "h" * 200}):
            self.assertEqual(len(debate_events.engine_host()), 80)


if __name__ == "__main__":
    unittest.main()
