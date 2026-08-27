import inspect
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bot.config import Config
from bot.main import classify_micro_scalp_candidate, record_micro_scalp_candidate


class MicroScalpTaggingTests(unittest.TestCase):
    def test_classify_micro_scalp_candidate_accepts_strong_long(self):
        cfg = Config()
        cfg.micro_scalp_enabled = True
        cfg.micro_scalp_long_only = True
        cfg.micro_scalp_allow_short = False
        cfg.micro_scalp_min_probability = 0.88
        cfg.micro_scalp_min_entry_priority = 0.80
        cfg.micro_scalp_require_btc_opposes_false = True
        candidate = {
            "signal": "LONG",
            "probability": 0.92,
            "entry_priority": 0.84,
            "score": 0.83,
            "chase_entry": False,
            "btc_momentum_opposes": False,
        }
        ok, reason, detail = classify_micro_scalp_candidate(candidate, cfg, agree=1, total=2)
        self.assertTrue(ok)
        self.assertEqual(reason, "high_confidence_long_fast_lane")
        self.assertEqual(detail["agree"], 1)

    def test_classify_micro_scalp_candidate_rejects_short_when_long_only(self):
        cfg = Config()
        cfg.micro_scalp_enabled = True
        cfg.micro_scalp_long_only = True
        cfg.micro_scalp_allow_short = False
        candidate = {
            "signal": "SHORT",
            "probability": 0.95,
            "entry_priority": 0.90,
            "score": 0.88,
            "chase_entry": False,
            "btc_momentum_opposes": False,
        }
        ok, reason, _ = classify_micro_scalp_candidate(candidate, cfg, agree=2, total=2)
        self.assertFalse(ok)
        self.assertEqual(reason, "")

    def test_record_micro_scalp_candidate_writes_jsonl(self):
        import json

        candidate = {
            "symbol": "AIOUSDT",
            "signal": "LONG",
            "probability": 0.91,
            "entry_priority": 0.83,
            "score": 0.82,
            "strength": 0.75,
            "speed": 0.66,
            "micro_scalp_candidate": True,
            "micro_scalp_reason": "high_confidence_long_fast_lane",
            "micro_scalp_lane": "micro_scalp",
            "micro_scalp_tag_version": "2026-08-17-v1",
            "btc_mult": 1.12,
            "chase_entry": False,
            "btc_momentum_opposes": False,
            "early_entry_spike": False,
            "micro_scalp_detail": {"agree": 1, "total": 2},
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "micro_scalp_candidates.jsonl"
            record_micro_scalp_candidate(candidate, path=path)
            rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["symbol"], "AIOUSDT")
        self.assertEqual(rows[0]["micro_scalp_reason"], "high_confidence_long_fast_lane")


class MicroScalpSourceWiringTests(unittest.TestCase):
    def test_scan_entry_candidate_carries_micro_scalp_keys(self):
        from bot import main as bot_main

        src = inspect.getsource(bot_main.scan_entry_candidate)
        self.assertIn('"micro_scalp_candidate": micro_scalp_candidate,', src)
        self.assertIn('record_micro_scalp_candidate(candidate)', src)
        self.assertIn('micro_scalp 후보 태그 감지', src)

    def test_scan_entry_candidate_promotes_micro_scalp_lane_when_live_enabled(self):
        from bot import main as bot_main

        src = inspect.getsource(bot_main.scan_entry_candidate)
        self.assertIn('if micro_scalp_candidate and not getattr(cfg, "micro_scalp_tag_only", True):', src)
        self.assertIn('"entry_lane": entry_lane,', src)


if __name__ == "__main__":
    unittest.main()
