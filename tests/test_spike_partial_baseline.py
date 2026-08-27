"""[2026-08-19 실거래 라벨 검증으로 발견] baseline이 덜 찼을 때 스파이크가 오탐되던 버그.

`detect_volume_spike()`는 baseline 평균을 낼 때
    baseline_avg = total_quote_volume / (baseline_window_sec / spike_window_sec)
로 계산했는데, 분모(=30)가 "실제로 캐시에 들어있는 시간"과 무관하게 고정이었다. 캐시가
덜 차 있으면 분자만 작아지고 분모는 그대로라 평균이 과소평가되고 ratio가 폭증한다.

체결이 완전히 균일해서 스파이크가 전혀 없는 시세(기대 ratio=1.0)에서 실측:
    캐시 충전  10초 -> ratio 30.00  is_spike=True
    캐시 충전  60초 -> ratio  5.00  is_spike=True
    캐시 충전 120초 -> ratio  2.50  is_spike=False
    캐시 충전 300초 -> ratio  1.00  is_spike=False  <- 정답
즉 임계 3.0 기준으로 **캐시가 100초 미만이면 무조건 스파이크**로 찍힌다.

이게 라이브에서 상시 발생하고 있었다. 체결 워커가 "응답불능" 판정으로 시간당 수십 회
재시작하면서 TradeTickCache가 매번 비워져 300초 baseline이 채워질 틈이 없었다.

독립 검증(2026-08-19): 바이낸스 공개 아카이브(data.binance.vision, fapi 미사용)에서
08-17 8심볼 aggTrades를 받아 이 함수로 진입 시점을 복원해 원장 early_entry_spike 라벨과
대조한 결과 —
  - 라이브가 True로 찍은 45건 중 38~44건이 실제로는 스파이크가 아니었다
    (ACEUSDT는 라이브 7건이 전부 오탐, 아카이브 기준 True 0건)
  - 평가 오프셋을 0 ~ -600초로 훑어도 일치율이 71%를 못 넘었다 -> "판정 지연"이 아니라
    "오탐"이다
이 라벨은 관측 전용(SPIKE_ENTRY_AGGRESSIVE_FILL=false)이라 매매에는 영향이 없었지만,
원장 분석에서 "spike=True 거래가 승률 74.5%"라는 잘못된 신호를 만들어냈다.

수정: 기존 docstring의 "baseline이 비어있으면 보수적으로 False" 원칙을 부분충전까지
확장한다. 관측 구간이 baseline_window_sec * min_baseline_coverage(기본 0.8)에 못 미치면
판단하지 않고 is_spike=False + insufficient_baseline=True를 돌려준다.
"""
import unittest

from bot.ws_trade_client import TradeTick, TradeTickCache, detect_volume_spike


def _fill_uniform(cache, symbol, span_sec, now_ms, per_sec=10, quote_per_tick=100.0):
    """스파이크가 전혀 없는 완전 균일 체결. 기대 ratio는 항상 1.0이다."""
    for s in range(span_sec):
        t_ms = now_ms - (span_sec - s) * 1000
        for i in range(per_sec):
            cache.append(TradeTick(symbol=symbol, price=1.0, quantity=quote_per_tick,
                                   event_time_ms=t_ms + i * 10, trade_time_ms=t_ms + i * 10,
                                   is_buyer_maker=False))


class PartialBaselineDoesNotFakeSpikeTest(unittest.TestCase):
    NOW_MS = 1_700_000_000_000

    def test_short_cache_uniform_tape_is_not_a_spike(self):
        """옛 코드에서는 10/30/60초 충전 모두 is_spike=True가 나왔다(ratio 30/10/5)."""
        for span in (10, 30, 60, 100):
            with self.subTest(span_sec=span):
                cache = TradeTickCache(max_ticks_per_symbol=100000)
                _fill_uniform(cache, "AAAUSDT", span, self.NOW_MS)
                r = detect_volume_spike(cache, "AAAUSDT", spike_multiplier=3.0,
                                        spike_window_sec=10, baseline_window_sec=300,
                                        now_ms=self.NOW_MS)
                self.assertFalse(
                    r["is_spike"],
                    "균일 체결인데 캐시 %d초 충전만으로 스파이크 판정이 났다(ratio=%.2f)"
                    % (span, r["ratio"]),
                )
                self.assertTrue(r["insufficient_baseline"])

    def test_full_baseline_uniform_tape_is_not_a_spike(self):
        """정상 경로 회귀 확인 - 300초가 다 차면 기존과 동일하게 ratio 1.0."""
        cache = TradeTickCache(max_ticks_per_symbol=100000)
        _fill_uniform(cache, "AAAUSDT", 300, self.NOW_MS)
        r = detect_volume_spike(cache, "AAAUSDT", spike_multiplier=3.0,
                                spike_window_sec=10, baseline_window_sec=300,
                                now_ms=self.NOW_MS)
        self.assertFalse(r["is_spike"])
        self.assertFalse(r["insufficient_baseline"])
        self.assertAlmostEqual(r["ratio"], 1.0, places=2)

    def test_real_spike_still_detected_when_baseline_is_full(self):
        """진짜 스파이크는 그대로 잡아야 한다 - 이 수정이 기능을 죽이면 안 된다."""
        cache = TradeTickCache(max_ticks_per_symbol=100000)
        _fill_uniform(cache, "AAAUSDT", 300, self.NOW_MS)
        cache.append(TradeTick(symbol="AAAUSDT", price=1.0, quantity=50000,
                               event_time_ms=self.NOW_MS - 500, trade_time_ms=self.NOW_MS - 500,
                               is_buyer_maker=False))
        r = detect_volume_spike(cache, "AAAUSDT", spike_multiplier=3.0,
                                spike_window_sec=10, baseline_window_sec=300,
                                now_ms=self.NOW_MS)
        self.assertTrue(r["is_spike"])
        self.assertGreaterEqual(r["ratio"], 3.0)

    def test_coverage_threshold_is_configurable(self):
        """min_baseline_coverage=0 이면 옛 동작(부분충전도 판정)으로 되돌릴 수 있다."""
        cache = TradeTickCache(max_ticks_per_symbol=100000)
        _fill_uniform(cache, "AAAUSDT", 30, self.NOW_MS)
        r = detect_volume_spike(cache, "AAAUSDT", spike_multiplier=3.0,
                                spike_window_sec=10, baseline_window_sec=300,
                                now_ms=self.NOW_MS, min_baseline_coverage=0.0)
        self.assertTrue(r["is_spike"], "옛 동작 재현용 이스케이프 해치가 동작해야 한다")
        self.assertFalse(r["insufficient_baseline"])

    def test_empty_cache_unchanged(self):
        """데이터가 아예 없는 기존 경로는 그대로여야 한다."""
        cache = TradeTickCache()
        r = detect_volume_spike(cache, "NOPE")
        self.assertFalse(r["is_spike"])
        self.assertEqual(r["ratio"], 0.0)


class WorkerPassesConfigTest(unittest.TestCase):
    """[2026-08-19 함께 발견] 워커가 detect_volume_spike를 기본값으로 호출해서 .env의
    SPIKE_ENTRY_MULTIPLIER/WINDOW/BASELINE이 워커에는 전혀 반영되지 않고 있었다.
    현재 설정이 마침 기본값과 같아 실해는 없었으나 잠재 버그라 함께 고쳤다."""

    def test_dump_status_passes_cfg_values(self):
        import inspect
        from bot import ws_trade_worker
        src = inspect.getsource(ws_trade_worker.dump_status)
        self.assertIn("cfg.spike_entry_multiplier", src)
        self.assertIn("cfg.spike_entry_window_sec", src)
        self.assertIn("cfg.spike_entry_baseline_sec", src)


if __name__ == "__main__":
    unittest.main()
