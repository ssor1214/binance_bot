"""[2026-08-17 실거래 점검으로 발견] 심볼 블락이 재시작 때마다 사라지던 결함.

`symbol_blacklist_until`(손실 후 재진입 차단 / 익절 후 쿨다운)은 인메모리 dict로만 있고
`_save_stats`/`_load_stats` 어디에도 없었다. 블락은 최대 `symbol_blacklist_cooldown_min`
(기본 60분)까지 유지돼야 하는데, 재시작 간격이 그보다 짧으면 차단이 무력화된다.
실측: 2026-08-17에 30~50분 간격으로 4회 재시작했다.

참고로 `symbol_loss_timestamps`는 "창이 30분으로 짧아 영속화하지 않는다"는 판단이 주석에
명시돼 있다. 블락(60분)은 그 근거가 적용되지 않으므로 이번에 영속화 대상에 넣었다.
"""
import json
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from bot.config import Config
from bot.position_manager import PositionManager


class BlacklistPersistenceTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        self._tmp = tempfile.TemporaryDirectory()
        self.stats_path = Path(self._tmp.name) / "stats.json"
        self._patcher = patch("bot.position_manager.STATS_FILE", self.stats_path)
        self._patcher.start()

    def tearDown(self):
        self._patcher.stop()
        self._tmp.cleanup()

    def test_blacklist_saved_and_restored(self):
        pm = PositionManager(Config())
        until = time.time() + 1800  # 30분 뒤까지 블락
        pm.symbol_blacklist_until["BTCUSDT"] = until
        pm.symbol_loss_streak["BTCUSDT"] = 2
        pm._save_stats()

        restored = PositionManager(Config())
        self.assertIn("BTCUSDT", restored.symbol_blacklist_until,
                      "재시작해도 심볼 블락이 살아 있어야 한다")
        self.assertAlmostEqual(restored.symbol_blacklist_until["BTCUSDT"], until, places=3)
        self.assertEqual(restored.symbol_loss_streak.get("BTCUSDT"), 2)

    def test_expired_entries_dropped_on_load(self):
        """만료된 블락을 되살리면 멀쩡한 심볼이 계속 막히고 파일도 무한정 커진다."""
        pm = PositionManager(Config())
        pm.symbol_blacklist_until["OLDUSDT"] = time.time() - 10  # 이미 만료
        pm.symbol_blacklist_until["LIVEUSDT"] = time.time() + 600
        pm._save_stats()

        restored = PositionManager(Config())
        self.assertNotIn("OLDUSDT", restored.symbol_blacklist_until)
        self.assertIn("LIVEUSDT", restored.symbol_blacklist_until)

    def test_blocked_symbol_still_blocked_after_restart(self):
        """실제 차단 판정(is_symbol_blocked 계열)이 재시작 후에도 동작해야 의미가 있다."""
        cfg = Config()
        pm = PositionManager(cfg)
        pm.symbol_blacklist_until["BTCUSDT"] = time.time() + 600
        pm._save_stats()

        restored = PositionManager(cfg)
        until = restored.symbol_blacklist_until.get("BTCUSDT", 0)
        self.assertGreater(until, time.time(), "복원된 블락이 아직 유효해야 한다")

    def test_missing_keys_do_not_break_load(self):
        """이 필드가 없던 시절의 기존 stats 파일과도 호환돼야 한다(하위호환)."""
        self.stats_path.write_text(json.dumps({
            "win_streak": 3, "total_trades": 10, "wins": 6, "losses": 4,
        }), encoding="utf-8")
        pm = PositionManager(Config())
        self.assertEqual(pm.win_streak, 3)
        self.assertEqual(pm.symbol_blacklist_until, {})
        self.assertEqual(pm.symbol_loss_streak, {})

    def test_corrupt_values_are_ignored(self):
        self.stats_path.write_text(json.dumps({
            "symbol_blacklist_until": {"BADUSDT": "not-a-number", "OKUSDT": time.time() + 300},
            "symbol_loss_streak": {"BADUSDT": None, "OKUSDT": 2},
        }), encoding="utf-8")
        pm = PositionManager(Config())
        self.assertNotIn("BADUSDT", pm.symbol_blacklist_until)
        self.assertIn("OKUSDT", pm.symbol_blacklist_until)
        self.assertEqual(pm.symbol_loss_streak.get("OKUSDT"), 2)


class SaveShapeTests(unittest.TestCase):
    def test_payload_includes_new_fields(self):
        import inspect
        src = inspect.getsource(PositionManager._save_stats)
        self.assertIn('"symbol_blacklist_until"', src)
        self.assertIn('"symbol_loss_streak"', src)


if __name__ == "__main__":
    unittest.main()
