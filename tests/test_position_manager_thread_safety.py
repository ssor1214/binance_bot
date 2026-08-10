"""[2026-08-10 사용자요청] 텔레그램 폴링 스레드(수동 /close 등)와 메인 루프가 같은
PositionManager 인스턴스의 record_result()를 동시에 호출할 수 있다 — threading.Lock으로
보호되는지 실제 스레드를 띄워 검증한다. 실 API를 절대 호출하지 않는다."""
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from bot.config import Config
from bot.position_manager import PositionManager


def cfg() -> Config:
    c = Config()
    c.symbol_blacklist_loss_threshold = 999
    c.symbol_blacklist_min_loss_streak = 999
    c.slippage_quarantine_multiplier = 999.0  # 이 테스트에서 격리 로직 부작용 방지
    return c


class PositionManagerThreadSafetyTests(unittest.TestCase):
    def make_manager(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        stats_path = Path(self.tmp.name) / ".bot_stats.json"
        patcher = patch("bot.position_manager.STATS_FILE", stats_path)
        patcher.start()
        self.addCleanup(patcher.stop)
        return PositionManager(cfg())

    def test_concurrent_record_result_calls_do_not_lose_updates(self):
        """[핵심] 여러 스레드(메인루프 역할 + 텔레그램폴링 역할 흉내)가 동시에
        record_result()를 호출해도 total_trades/wins 카운트가 정확히 맞아야 한다 —
        Lock이 없으면 read-modify-write 경합으로 일부 증가분이 누락될 수 있다."""
        pm = self.make_manager()
        N_THREADS = 8
        CALLS_PER_THREAD = 50

        def worker(thread_id):
            for i in range(CALLS_PER_THREAD):
                pm.record_result(f"SYM{thread_id}_{i}", 1.0, 0.1, side="LONG")

        threads = [threading.Thread(target=worker, args=(t,)) for t in range(N_THREADS)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        expected = N_THREADS * CALLS_PER_THREAD
        self.assertEqual(pm.total_trades, expected)
        self.assertEqual(pm.wins, expected)
        self.assertEqual(pm.long_wins, expected)

    def test_lock_exists_and_is_a_real_lock(self):
        pm = self.make_manager()
        self.assertTrue(hasattr(pm, "_lock"))
        self.assertTrue(pm._lock.acquire(blocking=False))
        pm._lock.release()


if __name__ == "__main__":
    unittest.main()
