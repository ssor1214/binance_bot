"""원칙 0(CM) 기반 지정가 TP — 목표선 계산과 무효/폴백 판정."""
import importlib.util
import sys
from pathlib import Path

import pandas as pd

try:
    import pytest
except ImportError:                          # pytest 미설치 환경에서도 직접 실행 가능
    class _P:
        @staticmethod
        def approx(v, rel=1e-9):
            class _A:
                def __eq__(_s, o): return abs(o - v) <= max(1e-9, abs(v) * 1e-9)
            return _A()
    pytest = _P()

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
_spec = importlib.util.spec_from_file_location("e3", ROOT / "scripts" / "scalp_bot_e3.py")
e3 = importlib.util.module_from_spec(_spec)
sys.modules["e3"] = e3
_spec.loader.exec_module(e3)


def _bars(closes, highs=None, lows=None):
    n = len(closes)
    return pd.DataFrame({
        "open_time": list(range(n)),
        "open": closes,
        "high": highs or [c * 1.001 for c in closes],
        "low": lows or [c * 0.999 for c in closes],
        "close": closes,
        "volume": [1.0] * n,
    })


def test_cm_target_is_swing_extreme_of_current_leg():
    """상승 추세에서 CM 목표선은 그 추세 구간의 최고가여야 한다."""
    closes = [100 + i * 0.5 for i in range(60)]
    highs = [c * 1.002 for c in closes]
    highs[55] = 200.0                      # 추세 안의 뾰족한 고점
    ind = e3.indicators(_bars(closes, highs=highs))
    assert ind is not None
    assert ind["cm_tp_long"] == pytest.approx(200.0)


def test_pullback_places_tp_below_the_extreme():
    """극값에 그대로 걸면 체결이 안 된다 — 0.2% 앞당겨야 한다."""
    ind = {"cm_tp_long": 200.0, "cm_tp_short": 0.0}
    assert e3.cm_tp_price(ind, 100.0, "LONG", 0.2) == pytest.approx(200.0 * 0.998)


def test_short_pullback_is_upward():
    ind = {"cm_tp_long": 0.0, "cm_tp_short": 50.0}
    assert e3.cm_tp_price(ind, 100.0, "SHORT", 0.2) == pytest.approx(50.0 * 1.002)


def test_invalid_when_entry_already_past_target():
    """진입가가 이미 목표를 지났으면 0 — 호출부가 볼밴/RR 로 폴백한다."""
    ind = {"cm_tp_long": 100.0, "cm_tp_short": 0.0}
    assert e3.cm_tp_price(ind, 100.5, "LONG", 0.2) == 0.0
    ind2 = {"cm_tp_long": 0.0, "cm_tp_short": 100.0}
    assert e3.cm_tp_price(ind2, 99.5, "SHORT", 0.2) == 0.0


def test_invalid_when_pullback_swallows_the_edge():
    """앞당김이 커서 목표가 진입가 아래로 내려가면 무효여야 한다(주문 방향 뒤집힘 방지)."""
    ind = {"cm_tp_long": 100.0, "cm_tp_short": 0.0}
    assert e3.cm_tp_price(ind, 99.9, "LONG", 1.0) == 0.0


def test_no_target_returns_zero():
    assert e3.cm_tp_price({"cm_tp_long": 0.0}, 100.0, "LONG", 0.2) == 0.0
    assert e3.cm_tp_price({"cm_tp_long": 100.0}, 0.0, "LONG", 0.2) == 0.0


class _Ex:
    def __init__(self): self.placed, self.cancelled = [], []
    def close_limit_position(self, sym, side, qty, price):
        self.placed.append((sym, side, qty, price)); return {"orderId": 777}
    def cancel_regular_order(self, sym, oid): self.cancelled.append((sym, oid))


def test_sync_tp_limit_replaces_old_order():
    """수량이 바뀌는 추가 진입에서 기존 주문을 안 지우면 고아 reduceOnly 가 남는다."""
    ex = _Ex()
    pos = e3.Pos("X", "LONG", [100.0], 5.0, 0.0, 5, tp_limit_price=110.0, tp_order_id=1)
    e3.sync_tp_limit(ex, pos, False, lambda m: None)
    assert ex.cancelled == [("X", 1)]
    assert ex.placed == [("X", "LONG", 5.0, 110.0)]
    assert pos.tp_order_id == 777


def test_cancel_tp_limit_clears_id():
    ex = _Ex()
    pos = e3.Pos("X", "LONG", [100.0], 5.0, 0.0, 5, tp_order_id=9)
    e3.cancel_tp_limit(ex, pos, False)
    assert ex.cancelled == [("X", 9)] and pos.tp_order_id == 0


def test_no_order_when_price_missing():
    """TP 가격이 없으면 아무 주문도 내지 않는다(폴백 경로)."""
    ex = _Ex()
    pos = e3.Pos("X", "LONG", [100.0], 5.0, 0.0, 5, tp_limit_price=0.0)
    e3.sync_tp_limit(ex, pos, False, lambda m: None)
    assert ex.placed == []


def test_failed_placement_does_not_leave_stale_id():
    """등록 실패 시 id 가 남으면 나중에 엉뚱한 주문을 취소한다."""
    class _Bad(_Ex):
        def close_limit_position(self, *a): raise RuntimeError("boom")
    pos = e3.Pos("X", "LONG", [100.0], 5.0, 0.0, 5, tp_limit_price=110.0, tp_order_id=3)
    msgs = []
    e3.sync_tp_limit(_Bad(), pos, False, msgs.append)
    assert pos.tp_order_id == 0
    assert any("폴링 익절로 대체" in m for m in msgs)



def test_cap_stop_roe_pulls_in_absurd_stop():
    """TRUMPUSDT 실사례: 손절폭 ROE 139% 가 그대로 나갔다."""
    assert e3.cap_stop_roe(1.824602, 2.332063, "SHORT", 5, 5) == pytest.approx(1.824602 * 1.01)


def test_cap_stop_roe_leaves_tight_stop_alone():
    """상한은 넓은 손절만 당긴다. 좁은 손절은 건드리지 않는다(하한이 아니다)."""
    assert e3.cap_stop_roe(0.147310, 0.147936, "SHORT", 5, 5) == 0.147936
    assert e3.cap_stop_roe(100.0, 99.5, "LONG", 5, 5) == 99.5


def test_cap_stop_roe_long_side():
    assert e3.cap_stop_roe(100.0, 90.0, "LONG", 5, 5) == pytest.approx(99.0)


def test_cap_stop_roe_disabled_when_zero():
    assert e3.cap_stop_roe(100.0, 90.0, "LONG", 5, 0) == 90.0
    assert e3.cap_stop_roe(100.0, 90.0, "LONG", 0, 5) == 90.0


def test_cap_stop_roe_never_flips_side():
    """상한을 씌워도 손절선이 진입가 반대편으로 넘어가면 안 된다."""
    for side, stop in (("LONG", 50.0), ("SHORT", 200.0)):
        out = e3.cap_stop_roe(100.0, stop, side, 5, 5)
        assert e3.stop_is_sane(100.0, out, side)


class _StopEx:
    """place_stop_market 이 -2021 로 거부하는 거래소."""
    def __init__(self, msg): self.msg = msg
    def cancel_order(self, *a): pass
    def place_stop_market(self, *a): raise RuntimeError(self.msg)


def test_minus2021_raises_breach_not_silent_zero():
    """-2021 은 '이미 손절선을 지났다'는 뜻 - 조용히 0을 돌려주면 안 된다."""
    for msg in ("APIError(code=-2021): Order would immediately trigger.",
                "Order would immediately trigger"):
        try:
            e3.sync_stop(_StopEx(msg), "X", "LONG", 1.0, 99.0, 0, False, lambda m: None)
        except e3.StopAlreadyBreached as ex:
            assert ex.symbol == "X"
        else:
            raise AssertionError(f"StopAlreadyBreached 가 발생해야 한다: {msg}")


def test_other_stop_errors_still_return_zero():
    """다른 실패는 종전대로 0을 돌려주고 재등록 경로(unprotected)로 간다."""
    msgs = []
    out = e3.sync_stop(_StopEx("APIError(code=-1111): Precision"), "X", "LONG",
                       1.0, 99.0, 0, False, msgs.append)
    assert out == 0
    assert any("폴링 손절만 남음" in m for m in msgs)


def test_dry_run_never_raises():
    assert e3.sync_stop(_StopEx("-2021"), "X", "LONG", 1.0, 99.0, 0, True, None) == 0


def _pos(side):
    return e3.Pos("S", side, [1.0], 1.0, 0.0, 5)


def test_same_side_counts_open_positions():
    pos = {"A": _pos("SHORT"), "B": _pos("SHORT"), "C": _pos("LONG")}
    assert e3.same_side_count(pos, {}, "SHORT") == 2
    assert e3.same_side_count(pos, {}, "LONG") == 1


def test_same_side_counts_unfilled_entry_orders():
    """미체결 주문을 안 세면 한 사이클에 5개가 동시에 나가 편중이 그대로 생긴다."""
    pos = {"A": _pos("SHORT")}
    orders = {"B": {"side": "SHORT"}, "C": {"side": "SHORT"}, "D": {"side": "LONG"}}
    assert e3.same_side_count(pos, orders, "SHORT") == 3
    assert e3.same_side_count(pos, orders, "LONG") == 1


def test_same_side_empty():
    assert e3.same_side_count({}, {}, "LONG") == 0


def test_same_side_gate_threshold():
    """상한 3이면 2개까지는 통과, 3개째부터 막힌다."""
    pos = {f"S{i}": _pos("SHORT") for i in range(2)}
    assert e3.same_side_count(pos, {}, "SHORT") < 3        # 통과
    pos["S2"] = _pos("SHORT")
    assert e3.same_side_count(pos, {}, "SHORT") >= 3       # 차단
    assert e3.same_side_count(pos, {}, "LONG") < 3         # 반대방향은 영향 없음


def test_pullback_depth_basic():
    # 분모는 진입가다. 100.5 기준 0.5 차이 -> 0.4975%
    assert abs(e3.pullback_depth_pct(100.5, 100.0) - 0.5 / 100.5 * 100) < 1e-9
    assert abs(e3.pullback_depth_pct(99.5, 100.0) - 0.5 / 99.5 * 100) < 1e-9


def test_pullback_depth_is_direction_agnostic():
    """롱(HullMA 위)이든 숏(아래)이든 '벌어진 거리'만 본다."""
    assert e3.pullback_depth_pct(101.0, 100.0) > 0
    assert e3.pullback_depth_pct(99.0, 100.0) > 0


def test_pullback_depth_guards_bad_input():
    assert e3.pullback_depth_pct(0.0, 100.0) == 0.0
    assert e3.pullback_depth_pct(100.0, 0.0) == 0.0


def test_pullback_gate_threshold():
    """상한 0.5% 기준: 0.4%는 통과, 0.6%는 차단."""
    assert e3.pullback_depth_pct(100.4, 100.0) <= 0.5
    assert e3.pullback_depth_pct(100.6, 100.0) > 0.5


def test_tp_cap_pulls_far_target_in():
    """목표 ROE 10%(가격 2%)가 상한 6%(가격 1.2%)로 당겨져야 한다."""
    ind = {"cm_tp_long": 102.0, "cm_tp_short": 0.0}
    out = e3.cm_tp_price(ind, 100.0, "LONG", 0.0, 5, 6.0)
    assert out == pytest.approx(101.2)


def test_tp_cap_short_side():
    ind = {"cm_tp_long": 0.0, "cm_tp_short": 98.0}
    out = e3.cm_tp_price(ind, 100.0, "SHORT", 0.0, 5, 6.0)
    assert out == pytest.approx(98.8)


def test_tp_cap_leaves_near_target_alone():
    """상한은 먼 목표만 당긴다. 이미 가까운 목표는 그대로 둔다."""
    ind = {"cm_tp_long": 100.5, "cm_tp_short": 0.0}
    assert e3.cm_tp_price(ind, 100.0, "LONG", 0.0, 5, 6.0) == pytest.approx(100.5)


def test_tp_cap_disabled_when_zero():
    ind = {"cm_tp_long": 102.0, "cm_tp_short": 0.0}
    assert e3.cm_tp_price(ind, 100.0, "LONG", 0.0, 5, 0.0) == pytest.approx(102.0)


def test_pullback_applied_before_cap():
    """앞당김 0.5% 를 먼저 적용하고, 그래도 멀면 상한으로 끊는다."""
    ind = {"cm_tp_long": 110.0, "cm_tp_short": 0.0}
    # 110 * 0.995 = 109.45 -> ROE 47% -> 상한 6% -> 101.2
    assert e3.cm_tp_price(ind, 100.0, "LONG", 0.5, 5, 6.0) == pytest.approx(101.2)


def test_tp_cap_still_returns_zero_when_invalid():
    """상한을 씌워도 목표가 진입가를 못 넘으면 무효(폴백)여야 한다."""
    ind = {"cm_tp_long": 100.0, "cm_tp_short": 0.0}
    assert e3.cm_tp_price(ind, 100.5, "LONG", 0.5, 5, 6.0) == 0.0


def test_beat_updates_heartbeat():
    e3.beat("unit-test")
    assert e3._HEARTBEAT["phase"] == "unit-test"
    assert e3._HEARTBEAT["ts"] > 0


def test_watchdog_disabled_when_zero():
    """0이면 스레드를 띄우지 않는다(테스트/드라이런에서 프로세스를 죽이면 안 된다)."""
    import threading
    before = threading.active_count()
    e3.start_main_loop_watchdog(0)
    assert threading.active_count() == before


def test_watchdog_starts_daemon_thread():
    import threading, time as _t
    e3.beat("alive")
    e3.start_main_loop_watchdog(9999)          # 절대 발동하지 않는 임계값
    _t.sleep(0.05)
    ts = [x for x in threading.enumerate() if x.name == "main-loop-watchdog"]
    assert ts, "워치독 스레드가 떠야 한다"
    assert ts[0].daemon, "데몬이어야 메인 종료를 막지 않는다"

if __name__ == "__main__":
    fails = 0
    for _n, _f in sorted(list(globals().items())):
        if _n.startswith("test_") and callable(_f):
            try:
                _f()
                print("  PASS  " + _n)
            except Exception as _e:
                fails += 1
                print("  FAIL  " + _n + ": " + str(_e))
    print("")
    print(("실패 " + str(fails) + "건") if fails else "전부 통과")
    raise SystemExit(1 if fails else 0)
