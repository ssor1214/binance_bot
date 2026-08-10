import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import run_forever


class SupervisorHeartbeatTests(unittest.TestCase):
    def test_stale_heartbeat_from_previous_process_does_not_kill_new_process(self):
        with tempfile.TemporaryDirectory() as tmp:
            heartbeat_path = Path(tmp) / "heartbeat.txt"
            heartbeat_path.write_text("1000", encoding="utf-8")
            with patch.object(run_forever, "HEARTBEAT_PATH", heartbeat_path):
                age = run_forever.heartbeat_age_sec(process_started_at=3500, now=3501)
        self.assertEqual(age, 1)

    def test_new_process_heartbeat_becomes_the_freshest_timestamp(self):
        with tempfile.TemporaryDirectory() as tmp:
            heartbeat_path = Path(tmp) / "heartbeat.txt"
            heartbeat_path.write_text("3510", encoding="utf-8")
            with patch.object(run_forever, "HEARTBEAT_PATH", heartbeat_path):
                age = run_forever.heartbeat_age_sec(process_started_at=3500, now=3515)
        self.assertEqual(age, 5)

    def test_missing_heartbeat_uses_process_start_as_initial_grace(self):
        with tempfile.TemporaryDirectory() as tmp:
            heartbeat_path = Path(tmp) / "missing.txt"
            with patch.object(run_forever, "HEARTBEAT_PATH", heartbeat_path):
                age = run_forever.heartbeat_age_sec(process_started_at=3500, now=3799)
        self.assertEqual(age, 299)


if __name__ == "__main__":
    unittest.main()
