"""[2026-08-25] 1회 진입 증거금 고정 모드.

베팅액 고정은 전략 검증의 전제다. 실측상 오늘 평균 증거금이 2.39 -> 9.75 -> 17.10으로
커지면서 같은 ROE 손실의 금액이 10배가 됐고, 승률이 떨어지는 구간에서 베팅액만 커졌다.
"""
import unittest

from bot.config import Config
from bot.main import compute_position_size


class _Ex:
    def round_quantity(self, symbol, qty, price=None, max_notional=None):
        return qty


def _cfg(fixed=11.0):
    cfg = Config()
    cfg.fixed_entry_margin_usdt = fixed
    return cfg


def _run(cfg, balance, price=100.0, ratio=0.15, leverage=4):
    return compute_position_size(
        balance, "TESTUSDT", _Ex(), price, ratio, leverage, 10.0,
        fixed_entry_margin_usdt=cfg.fixed_entry_margin_usdt,
    )


class FixedEntryMarginTests(unittest.TestCase):
    def test_margin_is_exactly_fixed_regardless_of_balance(self):
        """잔고가 달라져도 증거금은 11 USDT로 같아야 한다 — 이게 고정의 핵심."""
        for balance in (100.0, 265.0, 1000.0):
            qty = _run(_cfg(), balance)
            margin = qty * 100.0 / 4  # notional / leverage
            self.assertAlmostEqual(margin, 11.0, places=6, msg=f"balance={balance}")

    def test_ratio_is_ignored(self):
        """비율 사이징과 방어배율이 무시되는지 — ratio를 바꿔도 결과가 같아야 한다."""
        a = _run(_cfg(), 265.0, ratio=0.02)
        b = _run(_cfg(), 265.0, ratio=0.15)
        self.assertAlmostEqual(a, b, places=6)

    def test_skips_entry_when_balance_cannot_cover(self):
        """가용잔고가 고정 증거금도 못 대면 진입을 생략한다(0 반환)."""
        self.assertEqual(_run(_cfg(), 5.0), 0.0)

    def test_zero_disables_the_mode(self):
        """0이면 기존 비율 사이징으로 복귀한다(원복 경로)."""
        qty = _run(_cfg(fixed=0.0), 265.0, ratio=0.15)
        margin = qty * 100.0 / 4
        self.assertGreater(margin, 11.0)

    def test_code_default_is_off(self):
        import inspect
        self.assertIn(
            'fixed_entry_margin_usdt: float = _float("FIXED_ENTRY_MARGIN_USDT", 0.0)',
            inspect.getsource(Config),
        )


if __name__ == "__main__":
    unittest.main()


class AffordabilityUsesFirstTrancheTests(unittest.TestCase):
    """[2026-08-25 버그수정] 가용잔고 검사는 '지금 실제로 넣는 금액' 기준이어야 한다.

    순방향 분할이 켜져 있으면 1차는 총액의 40%만 쓰는데, 예전엔 총액 전부를 요구했다.
    11만 쓰면서 27.5가 없으면 진입을 생략 — 문턱이 실제 필요액의 2.5배였고,
    슬롯이 차고 평가손실이 생기면 거래가 통째로 멈추는 경로였다(원칙 1 손해).
    """

    TOTAL = 27.5
    FIRST = 0.4          # 1차 = 11.0 USDT

    def _run(self, balance, first_ratio):
        return compute_position_size(
            balance, "TESTUSDT", _Ex(), 100.0, 0.15, 4, 10.0,
            fixed_entry_margin_usdt=self.TOTAL,
            fixed_entry_first_ratio=first_ratio,
        )

    def test_enters_when_first_tranche_is_affordable(self):
        """가용 12 USDT — 총액 27.5는 못 대지만 1차 11은 댈 수 있으므로 진입해야 한다."""
        self.assertGreater(self._run(12.0, self.FIRST), 0.0)

    def test_skips_when_even_first_tranche_is_unaffordable(self):
        """1차 11도 못 대면 그때는 생략이 맞다."""
        self.assertEqual(self._run(9.0, self.FIRST), 0.0)

    def test_old_behavior_would_have_blocked(self):
        """분할이 꺼져 있으면(first_ratio=1.0) 총액 기준이라 가용 12로는 진입 못 한다 —
        이게 수정 전 동작이고, 분할이 켜진 상태에서도 그렇게 굴었던 게 버그였다."""
        self.assertEqual(self._run(12.0, 1.0), 0.0)

    def test_quantity_is_planned_total_when_affordable(self):
        """자금이 충분하면 수량은 계획 총액 기준 — 1차로 자르는 건 execute_entry가 한다."""
        qty = self._run(200.0, self.FIRST)
        self.assertAlmostEqual(qty * 100.0 / 4, self.TOTAL, places=6)

    def test_quantity_is_clamped_to_available(self):
        """가용잔고가 총액보다 적으면 수량이 가용 한도로 클램프돼야 한다(폴백 경로 안전)."""
        qty = self._run(12.0, self.FIRST)
        self.assertLessEqual(qty * 100.0 / 4, 12.0 + 1e-9)


class NoRecurrenceInvariantTests(unittest.TestCase):
    """[2026-08-25 재발방지] 이 버그의 뿌리는 사이징 판단이 두 함수에 나뉘어 있다는 점이다.

    compute_position_size가 '계획 총액'으로 감당 여부를 판정하고, execute_entry가 그걸
    1차 비율로 자른다. 두 곳의 기준이 어긋나면 "실제로는 감당 가능한데 진입이 막히는"
    상태가 조용히 생긴다. 아래 불변식으로 그 어긋남을 고정한다.
    """

    def test_invariant_if_first_tranche_affordable_then_entry_is_possible(self):
        """1차 몫을 감당할 수 있으면 수량이 0이 아니어야 한다 — 이게 이번 버그의 핵심 불변식."""
        total, first_ratio, lev, price = 27.5, 0.4, 4, 100.0
        first_margin = total * first_ratio
        for balance in (first_margin, first_margin + 0.5, first_margin * 2, total, total * 10):
            qty = compute_position_size(
                balance, "TESTUSDT", _Ex(), price, 0.15, lev, 10.0,
                fixed_entry_margin_usdt=total, fixed_entry_first_ratio=first_ratio,
            )
            self.assertGreater(qty, 0.0, msg=f"balance={balance} 에서 진입이 막혔다")

    def test_invariant_returned_quantity_never_exceeds_available(self):
        """돌려준 수량이 가용잔고를 넘으면 안 된다 — 넘으면 주문이 거래소에서 거부된다."""
        total, first_ratio, lev, price = 27.5, 0.4, 4, 100.0
        for balance in (11.0, 15.0, 27.5, 100.0):
            qty = compute_position_size(
                balance, "TESTUSDT", _Ex(), price, 0.15, lev, 10.0,
                fixed_entry_margin_usdt=total, fixed_entry_first_ratio=first_ratio,
            )
            self.assertLessEqual(qty * price / lev, balance + 1e-9, msg=f"balance={balance}")

    def test_gate_uses_first_ratio_in_source(self):
        """소스 수준 고정 — 감당 판정이 '1차 비율'을 반영하는지. 여기가 풀리면 버그가 재발한다."""
        import inspect

        from bot.main import compute_position_size as fn

        src = inspect.getsource(fn)
        self.assertIn("required_notional = fixed_margin * first_ratio * leverage", src)
        self.assertIn("planned_notional = min(fixed_margin * leverage, max_notional)", src)
