import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bot.main import record_entry_funnel_event


class EntryFunnelLoggingTests(unittest.TestCase):
    def test_record_entry_funnel_event_writes_jsonl_row(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "entry_funnel.jsonl"
            record_entry_funnel_event(
                "AIOUSDT",
                "probability",
                side="LONG",
                probability=0.82,
                detail={"required_probability": 0.85},
                path=path,
            )
            rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["symbol"], "AIOUSDT")
        self.assertEqual(rows[0]["stage"], "probability")
        self.assertEqual(rows[0]["side"], "LONG")
        self.assertAlmostEqual(rows[0]["probability"], 0.82)
        self.assertAlmostEqual(rows[0]["detail"]["required_probability"], 0.85)


if __name__ == "__main__":
    unittest.main()
