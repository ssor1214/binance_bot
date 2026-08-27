"""[2026-08-12 사용자요청] "자산이 5불만 남으면 거래 자체를 멈춰줘" — 저잔고 복구모드
(기본 17달러 미만에서도 고확률 후보는 계속 진입)와 별개로, 절대 하한선 밑이면 예외
없이 신규 진입을 완전히 차단하는 is_critical_balance_stop()/select_and_enter_best_candidates()
연동을 검증한다. 실 API 호출 없음."""
import unittest
from unittest.mock import patch

from bot.config import Config
from bot.main import is_critical_balance_stop, select_and_enter_best_candidates
from bot.position_manager import PositionManager


class FakeTelegram:
    trading_paused = False

    def __init__(self):
        self.sent = []
        self.notified = None

    def send(self, text, reply_markup=None):
        self.sent.append(text)

    def notify_scan_candidates(self, candidates, slots):
        self.notified = (candidates, slots)


class CriticalBalanceStopTests(unittest.TestCase):
    def test_stops_below_threshold(self):
        cfg = Config()
        cfg.critical_balance_stop_usdt = 5.0
        self.assertTrue(is_critical_balance_stop(5.0, cfg))
        self.assertTrue(is_critical_balance_stop(4.99, cfg))
        self.assertFalse(is_critical_balance_stop(5.01, cfg))

    def test_disabled_when_zero(self):
        cfg = Config()
        cfg.critical_balance_stop_usdt = 0.0
        self.assertFalse(is_critical_balance_stop(0.0, cfg))
        self.assertFalse(is_critical_balance_stop(1.0, cfg))

    def test_default_is_5_usdt(self):
        """[2026-08-15 사용자요청] "자산 보호모드 해제해줘 지금" — 총자산이 4.64USDT로 절대
        하한선(5.0)에 걸려 신규진입이 전면 차단된 상태에서, 사용자가 즉시 해제를 요청해
        .env의 CRITICAL_BALANCE_STOP_USDT를 5.0->0(비활성화)으로 변경함. 이 테스트는 원래
        .env 실제값(=사실상 "기본값")이 5.0이길 기대했는데, 그 실제값 자체가 바뀌었으므로
        기대값도 함께 갱신. 다른 테스트들은 전부 cfg.critical_balance_stop_usdt를 명시적으로
        오버라이드해서 이 변경과 무관하게 그대로 통과함(기능 자체는 안 건드림). 원복시 5.0으로."""
        cfg = Config()
        self.assertEqual(cfg.critical_balance_stop_usdt, 0.0)

    def test_blocks_entry_even_when_low_balance_recovery_would_normally_allow_it(self):
        """저잔고 복구모드(17달러 미만 고확률 후보 허용)조차 절대 하한선 밑에서는 무시돼야 한다."""
        cfg = Config()
        cfg.low_balance_new_entry_pause_threshold = 25.0
        cfg.low_balance_recovery_enabled = True
        cfg.low_balance_recovery_min_probability = 0.0  # 뭐든 통과시키는 조건
        cfg.low_balance_recovery_min_score = 0.0
        cfg.critical_balance_stop_usdt = 5.0
        pm = PositionManager(cfg)
        tg = FakeTelegram()

        strong_candidate = {"symbol": "BTCUSDT", "signal": "LONG", "probability": 0.99, "score": 0.99}

        with patch("bot.main.execute_entry", side_effect=AssertionError("절대 하한선 밑에서는 진입하면 안 된다")):
            select_and_enter_best_candidates(None, pm, cfg, tg, total_balance=4.5, candidates=[strong_candidate])

        self.assertIsNone(tg.notified)  # 후보 스캔 알림도 안 나가야 함(완전 차단)

    def test_sends_telegram_notification_once_on_transition_into_stop(self):
        cfg = Config()
        cfg.critical_balance_stop_usdt = 5.0
        pm = PositionManager(cfg)
        tg = FakeTelegram()

        select_and_enter_best_candidates(None, pm, cfg, tg, total_balance=4.0, candidates=[])
        select_and_enter_best_candidates(None, pm, cfg, tg, total_balance=3.5, candidates=[])
        select_and_enter_best_candidates(None, pm, cfg, tg, total_balance=4.9, candidates=[])

        self.assertEqual(len(tg.sent), 1)  # 계속 하한선 밑이어도 반복 알림 없음
        self.assertIn("자산보호모드", tg.sent[0])

    def test_sends_telegram_notification_once_on_recovery(self):
        cfg = Config()
        cfg.critical_balance_stop_usdt = 5.0
        pm = PositionManager(cfg)
        tg = FakeTelegram()

        select_and_enter_best_candidates(None, pm, cfg, tg, total_balance=4.0, candidates=[])  # 진입
        select_and_enter_best_candidates(None, pm, cfg, tg, total_balance=10.0, candidates=[])  # 회복
        select_and_enter_best_candidates(None, pm, cfg, tg, total_balance=12.0, candidates=[])  # 계속 정상

        self.assertEqual(len(tg.sent), 2)
        self.assertIn("자산보호모드", tg.sent[0])
        self.assertIn("해제", tg.sent[1])

    def test_existing_positions_unaffected_flag_only_blocks_new_entries(self):
        """이 기능은 select_and_enter_best_candidates(신규 진입 경로)에만 걸리고, 기존 포지션
        평가(PositionManager.evaluate 등)는 별도 경로라 이 테스트 대상이 아님을 문서화한다."""
        cfg = Config()
        cfg.critical_balance_stop_usdt = 5.0
        pm = PositionManager(cfg)
        pm.track("BTCUSDT", "LONG", entry_price=100.0, quantity=1.0, leverage=1.0)

        self.assertTrue(is_critical_balance_stop(4.0, cfg))
        self.assertIn("BTCUSDT", pm.positions)  # 하한선과 무관하게 기존 포지션은 그대로 추적됨


if __name__ == "__main__":
    unittest.main()
