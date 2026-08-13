"""[2026-08-13 사용자요청] "오늘 수익률/기준자산을 우리가 개선한 시점(예: 진입범위필터
수정한 12:17)으로 리셋해줘" — 재시작/날짜변경 시 자동으로 '지금'을 기준으로 삼는 기본
동작으로는 과거 특정 시점을 기준자산으로 못 잡는다. logs/daily_baseline_override.json을
통한 수동 오버라이드를 검증한다."""
import json
import tempfile
import unittest
from datetime import date
from pathlib import Path

from bot.main import load_daily_baseline_override


class DailyBaselineOverrideTests(unittest.TestCase):
    def test_missing_file_returns_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "no_such_file.json"
            result = load_daily_baseline_override(date(2026, 8, 13), 50.0, path=path)
        self.assertIsNone(result)

    def test_date_mismatch_returns_none_so_normal_autoreset_wins(self):
        """다음날이 되면 어제자 오버라이드 파일이 남아있어도 자동으로 무시돼야 한다 —
        안 그러면 매일 어제 기준으로 계속 잘못 계산됨."""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "override.json"
            path.write_text(json.dumps({
                "date": "2026-08-13", "start_balance": 12.41, "bot_pnl_start": -38.11,
            }), encoding="utf-8")
            result = load_daily_baseline_override(date(2026, 8, 14), 50.0, path=path)
        self.assertIsNone(result)

    def test_matching_date_applies_override_values(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "override.json"
            path.write_text(json.dumps({
                "date": "2026-08-13", "start_balance": 12.41, "bot_pnl_start": -38.11,
                "next_threshold": 50,
            }), encoding="utf-8")
            result = load_daily_baseline_override(date(2026, 8, 13), 999.0, path=path)
        self.assertIsNotNone(result)
        self.assertEqual(result["date"], date(2026, 8, 13))
        self.assertAlmostEqual(result["start_balance"], 12.41)
        self.assertAlmostEqual(result["bot_pnl_start"], -38.11)
        self.assertAlmostEqual(result["next_threshold"], 50.0)
        self.assertEqual(result["checkpoint_count"], 0)
        self.assertFalse(result["loss_limit_triggered"])

    def test_missing_next_threshold_falls_back_to_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "override.json"
            path.write_text(json.dumps({
                "date": "2026-08-13", "start_balance": 12.41, "bot_pnl_start": -38.11,
            }), encoding="utf-8")
            result = load_daily_baseline_override(date(2026, 8, 13), 50.0, path=path)
        self.assertAlmostEqual(result["next_threshold"], 50.0)

    def test_missing_required_field_returns_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "override.json"
            path.write_text(json.dumps({"date": "2026-08-13", "start_balance": 12.41}), encoding="utf-8")
            result = load_daily_baseline_override(date(2026, 8, 13), 50.0, path=path)
        self.assertIsNone(result)

    def test_malformed_json_returns_none_instead_of_crashing(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "override.json"
            path.write_text("{not valid json", encoding="utf-8")
            result = load_daily_baseline_override(date(2026, 8, 13), 50.0, path=path)
        self.assertIsNone(result)


if __name__ == "__main__":
    unittest.main()
