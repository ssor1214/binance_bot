"""[2026-08-17 실거래 사고] 포지션 없는 심볼에 남은 조건부 주문이 신규 진입을 즉시 청산.

사고 경위:
- 사용자 보고: "진입하자마자 1초도 안 돼서 자꾸 매도된다"
- 거래소 조회 결과 TRAILING_STOP_MARKET 4건이 고아로 남아 있었다
  (BTWUSDT/AIOUSDT/CYSUSDT/SPORTFUNUSDT — 전부 포지션 없음, 전부 reduceOnly)
- reduceOnly 주문은 포지션이 없을 땐 잠자고 있다가, 그 심볼에 새로 진입하는 순간
  즉시 발동해 방금 만든 포지션을 청산한다
- 실측 일치: CYSUSDT 고아 주문 14:12 생성 → 15:28:28 신규 진입 → 15:29:44 "거래소 직접 종료"

원인:
- 재시작 시 STOP_MARKET은 "거래소에서 발견해 채택"하는 경로가 있지만
  TRAILING_STOP_MARKET에는 없어서, 재시작마다 새로 등록하며 옛 주문이 남는다
- 고아 생성시각이 재시작 직전과 정확히 맞물린다(12:32:21/13:45:35/14:35:55 vs
  재시작 12:32:39/13:49:08/14:38:21)
"""
import unittest
from unittest.mock import MagicMock

from bot.config import Config
from bot.exchange import Exchange


def _ex_with_orders(orders):
    ex = Exchange.__new__(Exchange)  # __init__(API 연결) 우회
    ex.client = MagicMock()
    ex.cfg = Config()
    ex.client.futures_get_open_algo_orders.return_value = orders
    return ex


class OrphanCleanupTests(unittest.TestCase):
    def test_cancels_orders_for_symbols_without_position(self):
        ex = _ex_with_orders([
            {"symbol": "BTWUSDT", "algoId": 1, "orderType": "TRAILING_STOP_MARKET"},
            {"symbol": "GPSUSDT", "algoId": 2, "orderType": "STOP_MARKET"},
        ])
        cancelled = ex.cancel_orphan_algo_orders(held_symbols={"GPSUSDT"})
        self.assertEqual([o["symbol"] for o in cancelled], ["BTWUSDT"])
        ex.client.futures_cancel_algo_order.assert_called_once_with(symbol="BTWUSDT", algoId=1)

    def test_keeps_orders_for_held_symbols(self):
        """보유 중인 포지션의 보호주문까지 지우면 무보호 상태가 된다 — 절대 건드리면 안 된다."""
        ex = _ex_with_orders([
            {"symbol": "AKEUSDT", "algoId": 1, "orderType": "STOP_MARKET"},
            {"symbol": "AKEUSDT", "algoId": 2, "orderType": "TRAILING_STOP_MARKET"},
        ])
        cancelled = ex.cancel_orphan_algo_orders(held_symbols={"AKEUSDT"})
        self.assertEqual(cancelled, [])
        ex.client.futures_cancel_algo_order.assert_not_called()

    def test_cancel_failure_does_not_abort_remaining(self):
        """한 건 실패가 나머지 정리를 막으면 안 된다."""
        ex = _ex_with_orders([
            {"symbol": "AAAUSDT", "algoId": 1, "orderType": "TRAILING_STOP_MARKET"},
            {"symbol": "BBBUSDT", "algoId": 2, "orderType": "TRAILING_STOP_MARKET"},
        ])
        ex.client.futures_cancel_algo_order.side_effect = [RuntimeError("boom"), None]
        cancelled = ex.cancel_orphan_algo_orders(held_symbols=set())
        self.assertEqual([o["symbol"] for o in cancelled], ["BBBUSDT"])

    def test_query_failure_returns_empty_and_does_not_raise(self):
        """조회 실패가 기동을 막으면 안 된다(관측/정리 경로는 매매를 멈추면 안 됨)."""
        ex = _ex_with_orders([])
        ex.client.futures_get_open_algo_orders.side_effect = RuntimeError("api down")
        self.assertEqual(ex.cancel_orphan_algo_orders(held_symbols=set()), [])

    def test_no_orders_is_noop(self):
        ex = _ex_with_orders([])
        self.assertEqual(ex.cancel_orphan_algo_orders(held_symbols={"AKEUSDT"}), [])
        ex.client.futures_cancel_algo_order.assert_not_called()


class StartupWiringTests(unittest.TestCase):
    def test_cleanup_runs_after_position_sync(self):
        """held_symbols가 정확하려면 포지션 동기화 뒤에 돌아야 한다."""
        import inspect
        from bot import main as bot_main
        src = inspect.getsource(bot_main.main)
        sync_at = src.index('"기존 포지션 동기화"')
        clean_at = src.index('"고아 조건부주문 정리"')
        self.assertLess(sync_at, clean_at)

    def test_uses_tracked_positions_as_held_symbols(self):
        import inspect
        from bot import main as bot_main
        src = inspect.getsource(bot_main.main)
        self.assertIn("held = set(pm.positions.keys())", src)
        self.assertIn("ex.cancel_orphan_algo_orders(held)", src)


if __name__ == "__main__":
    unittest.main()


class RestartAdoptionTests(unittest.TestCase):
    """[사용자 지적] "매일 재부팅/재시작하는데 재시작 시 뭔가 남으면 또 버그 아니냐"

    맞다. 재시작이 이 버그의 트리거였다. 3중으로 막는다:
      1) 기동 시 1회 고아 청소 (main() 시작 단계)
      2) 재시작 복원 포지션이 기존 TRAILING_STOP_MARKET을 '채택'해 소유권 회복
         → 청산 시 함께 취소되므로 애초에 고아가 안 생긴다 (근본 수정)
      3) reconcile 매 주기 고아 청소 (운영 중 새로 생기는 경우 대비)
    """

    def test_trailing_adoption_wired_in_reconcile(self):
        """근본 수정: STOP_MARKET처럼 TRAILING도 채택해야 청산 시 취소된다."""
        import inspect
        from bot import main as bot_main
        src = inspect.getsource(bot_main.reconcile_positions)
        self.assertIn('o.get("orderType") == "TRAILING_STOP_MARKET"', src)
        # [2026-08-17 QA 보완] next()로 첫 건만 채택하던 것을 matches 리스트 방식으로 바꿨다
        # (side 검증 + 중복 취소를 위해). 상세는 TrailingAdoptionQualityTests 참고.
        self.assertIn('pos.trailing_order_id = int(keep["algoId"])', src)
        self.assertIn("소유권 복원", src)

    def test_adoption_runs_before_stop_market_block(self):
        """STOP_MARKET 블록은 채택 시 continue로 빠져나가므로, 트레일링 채택이 먼저여야
        둘 다 처리된다(순서가 뒤바뀌면 트레일링 채택이 영원히 스킵된다)."""
        import inspect
        from bot import main as bot_main
        src = inspect.getsource(bot_main.reconcile_positions)
        adopt_at = src.index('o.get("orderType") == "TRAILING_STOP_MARKET"')
        stop_at = src.index("missing = pos.stop_order_id is None")
        self.assertLess(adopt_at, stop_at)

    def test_runtime_cleanup_wired_in_reconcile(self):
        """기동 시 청소만으로는 부족 — 운영 중 포지션이 닫히며 트레일링만 남는 사례가 실제로 발생."""
        import inspect
        from bot import main as bot_main
        src = inspect.getsource(bot_main.reconcile_positions)
        self.assertIn("held_symbols = set(pm.positions.keys())", src)
        self.assertIn("포지션 없는 고아 조건부주문 취소", src)

    def test_runtime_cleanup_reuses_existing_query(self):
        """이미 조회한 live_algo_orders를 재사용해야 한다 — 매 주기 추가 API 호출은 부담이고
        이 저장소는 과거 REST 과다호출로 실제 IP밴을 겪었다."""
        import inspect
        from bot import main as bot_main
        src = inspect.getsource(bot_main.reconcile_positions)
        cleanup_at = src.index("held_symbols = set(pm.positions.keys())")
        query_at = src.index("live_algo_orders = ex.get_open_algo_orders()")
        self.assertLess(query_at, cleanup_at, "청소는 기존 조회 결과를 재사용해야 한다")


class FailSafeSourceOfTruthTests(unittest.TestCase):
    """[QA 재점검으로 발견] 고아 판정 근거를 pm.positions 하나에만 두면,
    거래소엔 포지션이 있는데 봇이 추적을 놓친 경우(track 실패/레이스) **그 포지션의
    보호주문을 지워 무보호 상태**로 만든다. 실거래에서 가장 위험한 실패 모드다.
    둘 중 하나라도 '포지션 있음'이면 건드리지 않아야 한다."""

    def test_reconcile_uses_union_of_pm_and_exchange(self):
        import inspect
        from bot import main as bot_main
        src = inspect.getsource(bot_main.reconcile_positions)
        self.assertIn("set(pm.positions.keys()) | set(live_positions.keys())", src)

    def test_startup_uses_union_of_pm_and_exchange(self):
        import inspect
        from bot import main as bot_main
        src = inspect.getsource(bot_main.main)
        self.assertIn('held |= {p["symbol"] for p in ex.get_open_positions()}', src)

    def test_startup_skips_cleanup_when_position_query_fails(self):
        """거래소 조회가 실패하면 held가 불완전해진다 — 그 상태로 취소하면 위험하므로
        아예 건너뛰어야 한다(안전 우선)."""
        import inspect
        from bot import main as bot_main
        src = inspect.getsource(bot_main.main)
        self.assertIn("안전하게 이번 정리는 건너뜀", src)

    def test_protective_orders_kept_when_only_exchange_knows_position(self):
        """pm이 놓친 포지션이라도 거래소가 알고 있으면 주문을 유지해야 한다."""
        ex = _ex_with_orders([
            {"symbol": "GHOSTUSDT", "algoId": 1, "orderType": "STOP_MARKET"},
        ])
        # 거래소 기준 보유 심볼로 넘기면 취소되지 않아야 한다
        cancelled = ex.cancel_orphan_algo_orders(held_symbols={"GHOSTUSDT"})
        self.assertEqual(cancelled, [])
        ex.client.futures_cancel_algo_order.assert_not_called()


class BlacklistPersistenceCoverageTests(unittest.TestCase):
    """블락을 설정하는 지점들이 실제로 저장 경로를 타는지 확인.
    설정만 하고 _save_stats()가 안 불리면 영속화 수정이 무의미해진다."""

    def test_blacklist_writes_and_save_share_one_method(self):
        import inspect
        from bot.position_manager import PositionManager
        src = inspect.getsource(PositionManager._record_result_locked)
        self.assertIn("self.symbol_blacklist_until[symbol]", src)
        self.assertIn("self._save_stats()", src)
        # 저장이 블락 설정보다 뒤에 와야 값이 반영된다
        self.assertLess(src.index("self.symbol_blacklist_until[symbol]"), src.rindex("self._save_stats()"))


class TrailingAdoptionQualityTests(unittest.TestCase):
    """[QA 재점검] 채택 로직 자체의 결함 두 가지를 막는다.

    1) side 미검증: 보호주문은 포지션 반대편(LONG->SELL, SHORT->BUY)이어야 한다.
       방향이 다른 주문을 채택하면 엉뚱한 주문에 소유권을 잡고 진짜 보호주문이 고아가 된다.
    2) 중복 미처리: 같은 심볼에 트레일링이 2개 이상 남을 수 있다(스윙 확장 재등록 시 실제
       발생 — CHIPUSDT 15:52 콜백0.49% + 15:53 콜백1.95%). 하나만 채택하면 나머지가 고아가 된다.
    """

    def _src(self):
        import inspect
        from bot import main as bot_main
        return inspect.getsource(bot_main.reconcile_positions)

    def test_side_is_verified(self):
        src = self._src()
        self.assertIn('expected_side = "SELL" if pos.side == "LONG" else "BUY"', src)
        self.assertIn('o.get("side") == expected_side', src)

    def test_duplicates_are_cancelled_not_left(self):
        src = self._src()
        self.assertIn("중복 TRAILING_STOP_MARKET", src)
        self.assertIn("for dup in matches[:-1]:", src)

    def test_keeps_most_recent_order(self):
        """스윙 확장 등 최신 의도를 반영한 주문을 남겨야 한다."""
        src = self._src()
        self.assertIn('matches.sort(key=lambda o: int(o.get("updateTime") or o.get("createTime") or 0))', src)
        self.assertIn("keep = matches[-1]", src)

    def test_duplicate_cancel_failure_does_not_abort(self):
        src = self._src()
        self.assertIn("중복 트레일링 취소 실패", src)
