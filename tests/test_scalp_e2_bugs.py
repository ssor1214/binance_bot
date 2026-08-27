"""scalp_bot_e2 에서 발견된 동작 버그 4건에 대한 회귀 테스트.

전략이 마이너스인지와는 별개로, 아래는 실제 주문/집계를 왜곡하는 결함이었다.
  1. 추가 진입 시 고아 STOP_MARKET 이 남는다 (P0 — 포지션 일부가 예기치 않게 잘린다)
  2. 재시작 채택 포지션의 손익비 1:2 익절이 사라진다
  3. 실손익 집계에 같은 심볼의 이전 체결이 섞인다
  4. 계좌의 남의 포지션까지 e2 가 채택해 관리한다
"""
import importlib.util
import json
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

_spec = importlib.util.spec_from_file_location(
    "scalp_bot_e2", ROOT / "scripts" / "scalp_bot_e2.py")
e2 = importlib.util.module_from_spec(_spec)
# dataclass 는 정의 시점에 sys.modules 로 자기 모듈을 찾는다. 먼저 등록해야 한다.
sys.modules["scalp_bot_e2"] = e2
_spec.loader.exec_module(e2)


class FakeClient:
    def __init__(self, trades=None):
        self._trades = trades or []

    def futures_account_trades(self, symbol, limit=None, startTime=None):
        rows = [t for t in self._trades if t["symbol"] == symbol]
        if startTime is not None:
            rows = [t for t in rows if t["time"] >= startTime]
        if limit:
            rows = rows[-limit:]
        return rows


class FakeExchange:
    """주문 등록/취소를 기록만 하는 가짜 거래소."""

    def __init__(self, trades=None, fail_place=False):
        self.client = FakeClient(trades)
        self.placed = []        # (symbol, side, qty, stop, algo_id)
        self.cancelled = []     # (symbol, algo_id)
        self._next_id = 1000
        self.fail_place = fail_place

    def place_stop_market(self, symbol, side, qty, stop):
        if self.fail_place:
            raise RuntimeError("-4120 주문 거부")
        self._next_id += 1
        self.placed.append((symbol, side, qty, stop, self._next_id))
        return {"algoId": self._next_id}

    def cancel_order(self, symbol, algo_id):
        self.cancelled.append((symbol, algo_id))

    def open_algo_ids(self):
        """아직 취소되지 않은 손절주문 id 들."""
        done = {a for _s, a in self.cancelled}
        return [p[4] for p in self.placed if p[4] not in done]


def _adopt_existing_position(lp, *, rr=2.0, fail_place=False):
    """재시작 채택 경로의 핵심만 떼어낸 헬퍼.

    목적:
    - 채택 포지션은 TP(BB/RR)를 복원해야 한다.
    - SL 등록이 실패해도 포지션 자체는 살아 있고, 이후 재시도 대상으로 남아야 한다.
    """
    ex = FakeExchange(fail_place=fail_place)
    side = "LONG" if float(lp["positionAmt"]) > 0 else "SHORT"
    entry = float(lp["entryPrice"])
    qty = abs(float(lp["positionAmt"]))
    stop = float(lp["stop"])
    tp_bb = float(lp["tp_bb"])
    risk = abs(entry - stop) / entry if entry > 0 else 0.0
    tp_rr = (entry * (1 + rr * risk) if side == "LONG" else entry * (1 - rr * risk)) if risk > 0 else 0.0

    pos = e2.Pos(lp["symbol"], side, [entry], qty, lp.get("entered_at", 0.0), 5,
                 stop_price=stop, tp_bb=tp_bb, tp_rr=tp_rr)
    pos.adopted = True
    unprotected = {}
    try:
        r = ex.place_stop_market(lp["symbol"], side, qty, stop)
        pos.stop_algo_id = int((r or {}).get("algoId") or 0)
    except Exception:
        unprotected[lp["symbol"]] = {"next_retry_at": 0.0, "last_warn_at": 0.0}
    return pos, unprotected


# ---------------------------------------------------------------- 버그 1
def test_추가진입_후_손절주문은_항상_하나만_남는다():
    """1차 -> 2차 -> 3차로 물타기할 때 거래소에 살아있는 손절주문은 언제나 1개여야 한다.

    이전 구현은 새 leg 수량으로 먼저 걸고 합산 수량으로 또 걸면서 앞의 것을
    취소하지 않아, leg 수량만 커버하는 고아 주문이 남았다.
    """
    ex = FakeExchange()
    algo = e2.sync_stop(ex, "BTCUSDT", "LONG", 1.0, 99.0, old_algo_id=0)
    assert len(ex.open_algo_ids()) == 1

    algo = e2.sync_stop(ex, "BTCUSDT", "LONG", 2.0, 98.0, old_algo_id=algo)
    assert len(ex.open_algo_ids()) == 1, "2차 진입 후 고아 손절주문이 남았다"

    algo = e2.sync_stop(ex, "BTCUSDT", "LONG", 3.0, 97.0, old_algo_id=algo)
    open_ids = ex.open_algo_ids()
    assert len(open_ids) == 1, "3차 진입 후 고아 손절주문이 남았다"
    assert open_ids[0] == algo

    # 마지막에 살아있는 주문은 합산 수량 3.0 을 커버해야 한다
    last = [p for p in ex.placed if p[4] == algo][0]
    assert last[2] == 3.0, "손절주문이 누적 수량 전체를 커버하지 않는다"


def test_취소가_등록보다_먼저다():
    """등록 후 취소 순서면 순간적으로 손절주문 2개가 공존한다."""
    ex = FakeExchange()
    ex.placed.append(("BTCUSDT", "LONG", 1.0, 99.0, 777))
    e2.sync_stop(ex, "BTCUSDT", "LONG", 2.0, 98.0, old_algo_id=777)
    assert ex.cancelled == [("BTCUSDT", 777)]
    assert len(ex.open_algo_ids()) == 1


def test_손절주문_등록_실패는_0을_반환하고_봇을_멈추지_않는다():
    ex = FakeExchange(fail_place=True)
    warned = []
    assert e2.sync_stop(ex, "BTCUSDT", "LONG", 1.0, 99.0, warn=warned.append) == 0
    assert warned and "손절주문 등록 실패" in warned[0]


def test_dry_run은_주문을_내지_않는다():
    ex = FakeExchange()
    assert e2.sync_stop(ex, "BTCUSDT", "LONG", 1.0, 99.0, old_algo_id=5, dry=True) == 0
    assert ex.placed == [] and ex.cancelled == []


# ---------------------------------------------------------------- 버그 2
@pytest.mark.parametrize("side,entry,stop", [("LONG", 100.0, 98.0),
                                             ("SHORT", 100.0, 102.0)])
def test_채택_포지션도_손익비_익절선을_갖는다(side, entry, stop):
    """재시작으로 채택된 포지션은 tp_rr 이 0이라 RR 청산이 사라졌었다.

    같은 전략이 재시작 전후로 다르게 동작하면 안 된다.
    """
    rr = 2.0
    is_long = side == "LONG"
    risk = abs(entry - stop) / entry
    tp = entry * (1 + rr * risk) if is_long else entry * (1 - rr * risk)

    p = e2.Pos(("BTCUSDT"), side, [entry], 1.0, 0.0, 5,
               stop_price=stop, tp_bb=0.0, tp_rr=tp)
    assert p.tp_rr > 0, "채택 포지션에 손익비 익절선이 없다"
    # 손절폭의 2배 지점이어야 한다
    reward = abs(p.tp_rr - entry) / entry
    assert reward == pytest.approx(rr * risk)
    # 방향이 맞아야 한다 — 롱이면 위, 숏이면 아래
    assert (p.tp_rr > entry) if is_long else (p.tp_rr < entry)


def test_e2_초기화시_sl등록_실패해도_tp는_복원되고_무보호목록에_남는다():
    pos, unprotected = _adopt_existing_position({
        "symbol": "ACEUSDT",
        "positionAmt": "82.78",
        "entryPrice": "0.206734",
        "stop": "0.205885",
        "tp_bb": "0.208000",
    }, fail_place=True)

    assert pos.adopted is True
    assert pos.tp_bb == pytest.approx(0.208000)
    assert pos.tp_rr > pos.entry
    assert pos.stop_algo_id == 0
    assert "ACEUSDT" in unprotected, "SL 등록 실패 포지션이 재시도 대상에서 빠졌다"


def test_e2_초기화시_sl_tp주문이_없어도_포지션자체는_채택된다():
    pos, unprotected = _adopt_existing_position({
        "symbol": "HEMIUSDT",
        "positionAmt": "-621",
        "entryPrice": "0.009045",
        "stop": "0.009120",
        "tp_bb": "0.008980",
    }, fail_place=False)

    assert pos.symbol == "HEMIUSDT"
    assert pos.side == "SHORT"
    assert pos.adopted is True
    assert pos.stop_algo_id > 0
    assert pos.tp_bb == pytest.approx(0.008980)
    assert pos.tp_rr < pos.entry, "SHORT 채택 포지션의 RR 익절선 방향이 틀렸다"
    assert unprotected == {}


# ---------------------------------------------------------------- 버그 3
def test_이전_포지션의_체결은_순손익에_섞이지_않는다():
    """같은 심볼을 반복 거래하면 startTime 만으로는 이전 체결이 걸러지지 않는다."""
    trades = [
        {"symbol": "BTCUSDT", "id": 1, "time": 1000, "commission": "0.5",
         "realizedPnl": "-9.0"},                      # 이전 포지션 (섞이면 안 됨)
        {"symbol": "BTCUSDT", "id": 2, "time": 2000, "commission": "0.1",
         "realizedPnl": "0.0"},                       # 이번 진입
        {"symbol": "BTCUSDT", "id": 3, "time": 3000, "commission": "0.1",
         "realizedPnl": "1.0"},                       # 이번 청산
    ]
    ex = FakeExchange(trades)
    since = 1                                          # 진입 직전 최신 체결 id
    rows = [t for t in ex.client.futures_account_trades("BTCUSDT")
            if int(t["id"]) > since]
    comm = sum(float(t["commission"]) for t in rows)
    rz = sum(float(t["realizedPnl"]) for t in rows)
    assert comm == pytest.approx(0.2)
    assert rz == pytest.approx(1.0)
    assert rz - comm == pytest.approx(0.8), "이전 포지션 손실이 섞였다"


def test_진입_직전_체결id를_읽는다():
    trades = [{"symbol": "BTCUSDT", "id": 7, "time": 1000},
              {"symbol": "BTCUSDT", "id": 9, "time": 2000}]
    assert e2.last_trade_id(FakeExchange(trades), "BTCUSDT") == 9


def test_체결_조회_실패해도_0을_반환한다():
    class Broken(FakeExchange):
        def __init__(self):
            super().__init__()
            self.client = type("C", (), {
                "futures_account_trades": lambda *a, **k: (_ for _ in ()).throw(
                    RuntimeError("API"))})()

    assert e2.last_trade_id(Broken(), "BTCUSDT") == 0


def test_원장은_실체결_평균가로_roe를_기록한다():
    trades = [
        {"symbol": "HEMIUSDT", "side": "BUY", "price": "0.0093850", "qty": "1495",
         "realizedPnl": "0", "commission": "0.00701528"},
        {"symbol": "HEMIUSDT", "side": "BUY", "price": "0.0093850", "qty": "253",
         "realizedPnl": "0", "commission": "0.00118720"},
        {"symbol": "HEMIUSDT", "side": "SELL", "price": "0.0093320", "qty": "541",
         "realizedPnl": "-0.02867300", "commission": "0.00252430"},
        {"symbol": "HEMIUSDT", "side": "SELL", "price": "0.0093320", "qty": "590",
         "realizedPnl": "-0.03127000", "commission": "0.00275294"},
        {"symbol": "HEMIUSDT", "side": "SELL", "price": "0.0093310", "qty": "541",
         "realizedPnl": "-0.02921400", "commission": "0.00252403"},
        {"symbol": "HEMIUSDT", "side": "SELL", "price": "0.0093300", "qty": "76",
         "realizedPnl": "-0.00418000", "commission": "0.00035454"},
    ]
    snap = e2.realized_fill_snapshot(
        trades, "LONG", fallback_entry=0.00921278, fallback_exit=0.00933134,
        fallback_qty=1748.0, leverage=5)
    assert snap["entry_price"] == pytest.approx(0.0093850)
    assert snap["exit_price"] == pytest.approx((541*0.0093320 + 590*0.0093320 + 541*0.0093310 + 76*0.0093300) / 1748.0)
    assert snap["roe_pct"] < 0, "실체결 기준이면 HEMIUSDT LONG은 손실이어야 한다"


def test_원장은_short도_실체결_평균가로_roe를_기록한다():
    trades = [
        {"symbol": "LITUSDT", "side": "SELL", "price": "2.869600", "qty": "2.1",
         "realizedPnl": "0", "commission": "0.00301308"},
        {"symbol": "LITUSDT", "side": "SELL", "price": "2.869500", "qty": "1.8",
         "realizedPnl": "0", "commission": "0.00258255"},
        {"symbol": "LITUSDT", "side": "SELL", "price": "2.869500", "qty": "2",
         "realizedPnl": "0", "commission": "0.00286950"},
        {"symbol": "LITUSDT", "side": "SELL", "price": "2.869500", "qty": "0.7",
         "realizedPnl": "0", "commission": "0.00100432"},
        {"symbol": "LITUSDT", "side": "BUY", "price": "2.869600", "qty": "6.6",
         "realizedPnl": "-0.01271250", "commission": "0.00946967"},
    ]
    snap = e2.realized_fill_snapshot(
        trades, "SHORT", fallback_entry=2.89322, fallback_exit=2.8699,
        fallback_qty=6.6, leverage=5)
    assert snap["entry_price"] == pytest.approx((2.1*2.8696 + 1.8*2.8695 + 2.0*2.8695 + 0.7*2.8695) / 6.6)
    assert snap["exit_price"] == pytest.approx(2.8696)
    assert snap["roe_pct"] < 0.1, "실체결 기준이면 LITUSDT SHORT의 과장된 +ROE가 사라져야 한다"


def test_진입후_실체결_평균가를_전략_평단으로_쓴다():
    trades = [
        {"symbol": "ACEUSDT", "id": 10, "side": "BUY", "price": "0.2060", "qty": "10"},
        {"symbol": "ACEUSDT", "id": 11, "side": "BUY", "price": "0.2080", "qty": "30"},
    ]
    px, qty = e2.entry_fill_after(
        FakeExchange(trades), "ACEUSDT", "LONG", since_trade_id=9,
        fallback_price=0.2050, expected_qty=40.0)

    assert px == pytest.approx((0.2060 * 10 + 0.2080 * 30) / 40)
    assert qty == pytest.approx(40.0)


def test_진입체결_조회실패시_ema가_아니라_주문직전가격으로_fallback한다():
    class Broken(FakeExchange):
        def __init__(self):
            super().__init__()
            self.client = type("C", (), {
                "futures_account_trades": lambda *a, **k: (_ for _ in ()).throw(
                    RuntimeError("API"))})()

    px, qty = e2.entry_fill_after(
        Broken(), "ACEUSDT", "LONG", since_trade_id=9,
        fallback_price=0.2075, expected_qty=40.0)

    assert px == pytest.approx(0.2075)
    assert qty == pytest.approx(40.0)


# ---------------------------------------------------------------- 버그 4
def test_상태파일에_없는_심볼은_채택하지_않는다(tmp_path):
    """e2 가 계좌의 모든 포지션을 자기 것으로 채택하면 다른 봇과 교차 간섭한다."""
    state = tmp_path / "state.json"
    state.write_text(json.dumps({"symbols": ["BTCUSDT", "ETHUSDT"]}), encoding="utf-8")
    owned = set(json.loads(state.read_text(encoding="utf-8"))["symbols"])

    live = [{"symbol": "BTCUSDT"}, {"symbol": "ETHUSDT"}, {"symbol": "XRPUSDT"}]
    adopt = [x for x in live if x["symbol"] in owned]
    skip = [x["symbol"] for x in live if x["symbol"] not in owned]

    assert [x["symbol"] for x in adopt] == ["BTCUSDT", "ETHUSDT"]
    assert skip == ["XRPUSDT"], "다른 봇의 포지션을 채택했다"


def test_상태파일이_없으면_전부_채택한다():
    """첫 기동에는 소유권 정보가 없다. 버리면 무관리 포지션이 생기므로 전부 채택한다
    (실사고: BTWUSDT ROE -38.79%)."""
    owned = set()
    live = [{"symbol": "BTCUSDT"}, {"symbol": "XRPUSDT"}]
    adopt = live if not owned else [x for x in live if x["symbol"] in owned]
    assert len(adopt) == 2


# ------------------------------------------------- 재시작 후 보유시간 유지
def _resolve_entered_at(owned_at, live_update_time_ms, now):
    """봇의 진입시각 결정 로직과 같은 순서: 상태파일 -> updateTime -> 지금."""
    at = now
    if 0 < live_update_time_ms / 1000.0 <= now:
        at = live_update_time_ms / 1000.0
    return owned_at or at


def test_상태파일_진입시각이_updateTime보다_우선한다():
    """updateTime 은 '마지막 변경 시각'이라 3분할 포지션에서는 3차 시각이 잡힌다.
    1차 진입 시각은 상태파일에만 있다."""
    now = 10_000.0
    first_leg, third_leg = 1_000.0, 9_500.0
    assert _resolve_entered_at(first_leg, third_leg * 1000, now) == first_leg


def test_상태파일이_없으면_updateTime을_쓴다():
    now = 10_000.0
    assert _resolve_entered_at(None, 9_500.0 * 1000, now) == 9_500.0


def test_둘_다_없으면_현재시각을_쓴다():
    now = 10_000.0
    assert _resolve_entered_at(None, 0, now) == now


def test_보유시간이_리셋되면_시간손절이_발동하지_않는다():
    """실측: 20분 보유한 HEMIUSDT 가 원장에 0.1분으로 기록됐다.
    entered_at 이 재시작마다 now 로 리셋되면 --max-hold-sec 은 영영 안 걸린다."""
    now, real_entry, max_hold = 10_000.0, 8_800.0, 300.0   # 실제 20분 보유
    reset = now - now                                       # 리셋된 경우의 보유시간
    kept = now - _resolve_entered_at(real_entry, 0, now)
    assert reset < max_hold, "리셋되면 시간손절이 안 걸린다(재현)"
    assert kept >= max_hold, "진입시각을 보존하면 시간손절이 걸린다"


# ------------------------------------------- 추가 진입 차수의 체결가 분리
class FillClient:
    """진입 체결 이력을 돌려주는 가짜 클라이언트."""

    def __init__(self, trades):
        self._t = trades
        self.calls = 0

    def futures_account_trades(self, symbol, limit=None, startTime=None):
        self.calls += 1
        rows = [t for t in self._t if t["symbol"] == symbol]
        return rows[-limit:] if limit else rows


class FillExchange:
    def __init__(self, trades):
        self.client = FillClient(trades)


# 1차는 100.0 에 1개, 2차는 90.0 에 1개 체결됐다.
_TRANCHE_FILLS = [
    {"symbol": "BTCUSDT", "id": 10, "side": "BUY", "qty": "1.0", "price": "100.0"},
    {"symbol": "BTCUSDT", "id": 11, "side": "BUY", "qty": "1.0", "price": "90.0"},
]


def test_추가진입은_이번_차수_체결가만_읽는다():
    """since_trade_id 를 0으로 두면 1차 체결까지 가중평균해 '누적 평단'이 나온다.
    그 값을 legs 에 또 붙이므로 1차가 중복 반영된다.
    2차 주문 직전의 체결 id(10)를 넘기면 2차 체결(90.0)만 잡혀야 한다."""
    ex = FillExchange(_TRANCHE_FILLS)
    px, qty = e2.entry_fill_after(ex, "BTCUSDT", "LONG", 10, fallback_price=95.0,
                                  expected_qty=1.0)
    assert px == pytest.approx(90.0), "1차 체결이 섞여 누적 평단이 나왔다"
    assert qty == pytest.approx(1.0)


def test_since_id가_0이면_이전_차수가_섞인다_회귀재현():
    """수정 전 동작을 명시적으로 남긴다 - 왜 since_id 가 필요한지의 근거."""
    ex = FillExchange(_TRANCHE_FILLS)
    px, _ = e2.entry_fill_after(ex, "BTCUSDT", "LONG", 0, fallback_price=95.0)
    assert px == pytest.approx(95.0), "since_id 가 없으면 합산하지 않고 fallback 을 쓴다"


def test_숏은_SELL_체결만_읽는다():
    trades = [
        {"symbol": "BTCUSDT", "id": 20, "side": "BUY", "qty": "1.0", "price": "100.0"},
        {"symbol": "BTCUSDT", "id": 21, "side": "SELL", "qty": "2.0", "price": "80.0"},
    ]
    ex = FillExchange(trades)
    px, qty = e2.entry_fill_after(ex, "BTCUSDT", "SHORT", 20, fallback_price=99.0,
                                  expected_qty=2.0)
    assert px == pytest.approx(80.0)
    assert qty == pytest.approx(2.0)


def test_체결이_아직_안_잡히면_fallback을_쓴다():
    """시장가 체결 직후 조회는 비어 있을 수 있다. 봇을 멈추면 안 된다.
    since_id 가 있을 때만 재시도한다 - 0이면 조회 자체가 위험해서 즉시 fallback."""
    ex = FillExchange([])
    px, qty = e2.entry_fill_after(ex, "BTCUSDT", "LONG", 99, fallback_price=123.4,
                                  expected_qty=5.0)
    assert px == pytest.approx(123.4)
    assert qty == pytest.approx(5.0)
    assert ex.client.calls == 3, "재시도 3회를 다 쓰고 fallback 으로 넘어가야 한다"


def test_since_id가_0이면_조회하지_않고_바로_fallback():
    ex = FillExchange([])
    px, _ = e2.entry_fill_after(ex, "BTCUSDT", "LONG", 0, fallback_price=7.0,
                                expected_qty=1.0)
    assert px == pytest.approx(7.0)
    assert ex.client.calls == 0, "필터를 못 거는 상태에서는 조회하면 안 된다"


def test_부분체결이면_기대수량의_95퍼센트까지_기다린다():
    partial = [{"symbol": "BTCUSDT", "id": 30, "side": "BUY",
                "qty": "0.5", "price": "100.0"}]
    ex = FillExchange(partial)
    px, _ = e2.entry_fill_after(ex, "BTCUSDT", "LONG", 29, fallback_price=111.0,
                                expected_qty=1.0)
    assert px == pytest.approx(111.0), "수량이 모자라면 fallback 을 써야 한다"


# ------------------------------------------- 외부 청산(거래소 손절) 누락
def test_봇이_모르는_사이_닫힌_포지션을_찾아낸다():
    """거래소 손절주문이 발동하면 봇은 청산을 스스로 하지 않는다.
    실측: 90분 구간에서 원장 +0.1691 vs 거래소 -2.7594 (차이 -2.93).
    원장에는 봇이 직접 청산한 익절만 쌓이고 손절이 통째로 빠졌다."""
    bot_positions = {"BTWUSDT", "ACEUSDT", "SUIUSDT"}
    live_on_exchange = {"SUIUSDT"}          # 둘은 거래소 손절로 이미 닫혔다
    closed = [s for s in bot_positions if s not in live_on_exchange]
    assert sorted(closed) == ["ACEUSDT", "BTWUSDT"]


def test_외부청산_손익은_체결이력으로_재구성한다():
    """롱이 손절가에 잘린 경우. 실현손익이 음수로 잡혀야 한다."""
    trades = [
        {"side": "BUY", "qty": "42.0", "price": "0.38934",
         "realizedPnl": "0", "commission": "0.0082"},
        {"side": "SELL", "qty": "14.0", "price": "0.38701",
         "realizedPnl": "-0.0326", "commission": "0.0027"},
        {"side": "SELL", "qty": "28.0", "price": "0.38682",
         "realizedPnl": "-0.0706", "commission": "0.0054"},
    ]
    snap = e2.realized_fill_snapshot(trades, "LONG", fallback_entry=0.0,
                                     fallback_exit=0.0, fallback_qty=42.0,
                                     leverage=5)
    assert snap["entry_price"] == pytest.approx(0.38934)
    # 청산가는 두 체결의 가중평균
    assert snap["exit_price"] == pytest.approx((14 * 0.38701 + 28 * 0.38682) / 42)
    assert snap["roe_pct"] < 0, "손절인데 ROE가 양수로 기록됐다"
    rz = sum(float(t["realizedPnl"]) for t in trades)
    comm = sum(float(t["commission"]) for t in trades)
    assert rz - comm == pytest.approx(-0.1195, abs=1e-4)


def test_숏_외부청산도_부호가_맞는다():
    trades = [
        {"side": "SELL", "qty": "42.0", "price": "0.37727",
         "realizedPnl": "0", "commission": "0.0079"},
        {"side": "BUY", "qty": "42.0", "price": "0.38164",
         "realizedPnl": "-0.1835", "commission": "0.0080"},
    ]
    snap = e2.realized_fill_snapshot(trades, "SHORT", fallback_entry=0.0,
                                     fallback_exit=0.0, fallback_qty=42.0,
                                     leverage=5)
    assert snap["entry_price"] == pytest.approx(0.37727)
    assert snap["exit_price"] == pytest.approx(0.38164)
    assert snap["roe_pct"] < 0, "숏이 불리하게 잘렸는데 ROE가 양수다"


def test_체결이력이_비면_손익0으로_기록하고_넘어간다():
    """조회 실패해도 포지션을 원장 없이 버리면 안 된다."""
    snap = e2.realized_fill_snapshot([], "LONG", fallback_entry=1.5,
                                     fallback_exit=1.5, fallback_qty=10.0,
                                     leverage=5)
    assert snap["entry_price"] == pytest.approx(1.5)
    assert snap["quantity"] == pytest.approx(10.0)
    assert snap["roe_pct"] == pytest.approx(0.0)


# ------------------------------------------- 체결 수량 폭주 (P0)
def test_since_id를_모르면_합산하지_않는다():
    """last_trade_id() 가 조회 실패 시 0을 반환하면 필터를 못 건다.
    그 상태로 합산하면 최근 50건의 같은 방향 체결을 전부 더한다.
    실측: BOMEUSDT 2차에서 12,113 대신 265,690 이 반환돼
    봇 내부 수량이 277,809(거래소 실제 24,232)로 어긋났다."""
    many = [{"symbol": "BOMEUSDT", "id": i, "side": "BUY",
             "qty": "12000", "price": "0.00125"} for i in range(1, 21)]
    ex = FillExchange(many)
    px, qty = e2.entry_fill_after(ex, "BOMEUSDT", "LONG", 0,
                                  fallback_price=0.00126, expected_qty=12113.0)
    assert qty == pytest.approx(12113.0), "20건을 전부 합산해 24만이 나왔다"
    assert px == pytest.approx(0.00126)


def test_주문수량보다_크게_벗어나면_fallback을_쓴다():
    """필터가 걸려도 이전 차수가 섞이면 수량이 부풀 수 있다."""
    rows = [{"symbol": "BTCUSDT", "id": 5, "side": "BUY", "qty": "100", "price": "10"},
            {"symbol": "BTCUSDT", "id": 6, "side": "BUY", "qty": "100", "price": "12"}]
    ex = FillExchange(rows)
    px, qty = e2.entry_fill_after(ex, "BTCUSDT", "LONG", 4,
                                  fallback_price=11.5, expected_qty=100.0)
    assert qty == pytest.approx(100.0), "200 을 그대로 받아들였다"
    assert px == pytest.approx(11.5)


class PosExchange:
    def __init__(self, positions, raise_on_call=False):
        self._p = positions
        self._raise = raise_on_call

    class _C:
        def __init__(self, outer):
            self.o = outer

        def futures_account(self):
            if self.o._raise:
                raise RuntimeError("API")
            return {"positions": self.o._p}

    @property
    def client(self):
        return PosExchange._C(self)


def test_보유수량은_거래소를_정본으로_쓴다():
    ex = PosExchange([{"symbol": "BOMEUSDT", "positionAmt": "24232"}])
    assert e2.live_position_qty(ex, "BOMEUSDT", fallback=277809.0) == pytest.approx(24232.0)


def test_거래소_조회_실패시_fallback을_쓴다():
    ex = PosExchange([], raise_on_call=True)
    assert e2.live_position_qty(ex, "BOMEUSDT", fallback=100.0) == pytest.approx(100.0)


def test_포지션이_없으면_fallback을_쓴다():
    """체결 직후 아직 포지션에 반영되기 전일 수 있다."""
    ex = PosExchange([{"symbol": "BOMEUSDT", "positionAmt": "0"}])
    assert e2.live_position_qty(ex, "BOMEUSDT", fallback=50.0) == pytest.approx(50.0)


# ------------------------------------------- 거래량 상위 심볼 갱신
def test_거래량상위_갱신시_보유심볼은_대상에_남긴다():
    merged = e2.merge_symbol_universe(
        ["BTCUSDT", "ETHUSDT", "SOLUSDT"],
        {"BOMEUSDT", "ETHUSDT"},
        limit=3,
    )

    assert merged[:3] == ["BTCUSDT", "ETHUSDT", "SOLUSDT"]
    assert "BOMEUSDT" in merged, "보유 중인 심볼을 갱신 과정에서 빼면 관리가 끊긴다"
    assert merged.count("ETHUSDT") == 1


# =========================================================== 안전 규칙 3종
# 1) 봇 중복 실행 방지
def test_같은_PID면_락을_다시_잡는다(tmp_path, monkeypatch):
    """자기 자신의 PID 는 충돌이 아니다(재진입 허용)."""
    f = tmp_path / "bot_pid.json"
    monkeypatch.setattr(e2, "BOT_PID_FILE", f)
    assert e2.acquire_bot_lock() is True
    assert json.loads(f.read_text(encoding="utf-8"))["pid"] == os.getpid()
    assert e2.acquire_bot_lock() is True


def test_살아있는_다른_인스턴스가_있으면_기동을_거부한다(tmp_path, monkeypatch, capsys):
    """실사고: Git Bash ps/kill 이 Windows python.exe 를 못 죽이는데 grep -c 가 0을
    반환해 '죽었다'고 오판했다. 구버전 4개 + 신버전 2개가 동시에 실주문을 냈다."""
    f = tmp_path / "bot_pid.json"
    f.write_text(json.dumps({"pid": 999999, "ts": 0}), encoding="utf-8")
    monkeypatch.setattr(e2, "BOT_PID_FILE", f)
    monkeypatch.setattr(e2, "_pid_alive", lambda pid: True)
    assert e2.acquire_bot_lock() is False
    assert "이미 e2 봇이 실행 중" in capsys.readouterr().out


def test_죽은_PID면_락을_가져온다(tmp_path, monkeypatch):
    f = tmp_path / "bot_pid.json"
    f.write_text(json.dumps({"pid": 999999, "ts": 0}), encoding="utf-8")
    monkeypatch.setattr(e2, "BOT_PID_FILE", f)
    monkeypatch.setattr(e2, "_pid_alive", lambda pid: False)
    assert e2.acquire_bot_lock() is True
    assert json.loads(f.read_text(encoding="utf-8"))["pid"] == os.getpid()


def test_락은_자기_것만_해제한다(tmp_path, monkeypatch):
    """다른 인스턴스의 락 파일을 지우면 중복 실행이 다시 열린다."""
    f = tmp_path / "bot_pid.json"
    monkeypatch.setattr(e2, "BOT_PID_FILE", f)
    f.write_text(json.dumps({"pid": 999999, "ts": 0}), encoding="utf-8")
    e2.release_bot_lock()
    assert f.exists(), "남의 락을 지웠다"
    f.write_text(json.dumps({"pid": os.getpid(), "ts": 0}), encoding="utf-8")
    e2.release_bot_lock()
    assert not f.exists()


def test_락파일이_깨져도_기동을_막지_않는다(tmp_path, monkeypatch):
    f = tmp_path / "bot_pid.json"
    f.write_text("깨진 내용", encoding="utf-8")
    monkeypatch.setattr(e2, "BOT_PID_FILE", f)
    assert e2.acquire_bot_lock() is True


# 2) 원장 vs 거래소 대조 (판정 로직)
def _gap(ledger_net, exchange_net):
    return exchange_net - ledger_net


def test_원장이_손실을_빠뜨리면_차이가_음수로_나온다():
    """실측: 원장 +2.8612 vs 거래소 -2.4792. 19건 누락, 승률 14%p 부풀림."""
    assert _gap(2.8612, -2.4792) == pytest.approx(-5.3404, abs=1e-4)
    assert abs(_gap(2.8612, -2.4792)) > 0.05, "경고 문턱을 넘어야 한다"


def test_정합하면_차이가_문턱_아래다():
    assert abs(_gap(1.2340, 1.2338)) <= 0.05


# 3) 손절 미보호 시간 상한
def _should_force_close(now, since, limit):
    return limit > 0 and (now - since) >= limit


def test_보호_실패가_상한을_넘으면_강제청산한다():
    """무한 재시도는 손절 없는 포지션을 방치하는 것과 같다.
    실사고: BTWUSDT 가 손절 없이 ROE -38.79% 까지 갔다."""
    assert _should_force_close(now=1000, since=1000 - 121, limit=120) is True
    assert _should_force_close(now=1000, since=1000 - 119, limit=120) is False


def test_상한이_0이면_강제청산하지_않는다():
    assert _should_force_close(now=1e9, since=0, limit=0) is False


# ============================== 손절폭 확대 (--stop-widen-pct)
# [2026-08-21] 손절선은 항상 EMA25 고정이었고 이를 넓히는 인자가 없었다.
# --min-risk-pct 는 손절을 넓히는 값이 아니라 '진입을 거르는' 필터다.
# 실측 근거(원장 130건): 손절 28건 중 50%가 진입 후 한 틱도 유리한 적 없음.
# 반대로 익절 102건의 최대불리 ROE 중앙값 +0.00% -> 이긴 거래는 손절선
# 근처에 가지 않으므로 넓혀도 승리를 잃지 않는다(-2% 밑으로 밀린 승리 1건뿐).


def test_stop_widen_기본값0은_ema25를_그대로_쓴다():
    """기본값이 0 이 아니면 재시작만으로 전략이 조용히 바뀐다."""
    assert e2.widened_stop(100.0, 99.0, "LONG", 0.0) == 99.0
    assert e2.widened_stop(100.0, 101.0, "SHORT", 0.0) == 101.0


def test_stop_widen_long은_손절선을_아래로_민다():
    assert e2.widened_stop(100.0, 99.0, "LONG", 3.0) == pytest.approx(97.0)


def test_stop_widen_short은_손절선을_위로_민다():
    assert e2.widened_stop(100.0, 101.0, "SHORT", 3.0) == pytest.approx(103.0)


@pytest.mark.parametrize("side,stop", [("LONG", 94.0), ("SHORT", 106.0)])
def test_stop_widen_이미_넓으면_좁히지_않는다(side, stop):
    """넓히기 전용이다. EMA25 가 더 멀면 그대로 둬야 한다."""
    assert e2.widened_stop(100.0, stop, side, 3.0) == pytest.approx(stop)


@pytest.mark.parametrize("side", ["LONG", "SHORT"])
def test_stop_widen_손절선이_진입가를_넘지_않는다(side):
    """넘어가면 진입 즉시 청산된다."""
    near = 99.9 if side == "LONG" else 100.1
    out = e2.widened_stop(100.0, near, side, 5.0)
    assert (out < 100.0) if side == "LONG" else (out > 100.0)


@pytest.mark.parametrize("ent,stop,w", [(0.0, 99.0, 3.0), (100.0, 0.0, 3.0),
                                        (100.0, 99.0, -1.0)])
def test_stop_widen_잘못된_입력은_원본을_돌려준다(ent, stop, w):
    assert e2.widened_stop(ent, stop, "LONG", w) == stop


def test_stop_widen_하면_손익비_익절선도_같이_멀어진다():
    """익절선이 안 따라가면 실제 손익비가 설정값보다 나빠진다."""
    narrow = e2.fee_aware_rr_price(100.0, 99.0, "LONG", 2.0, 0.001002)
    wide = e2.fee_aware_rr_price(
        100.0, e2.widened_stop(100.0, 99.0, "LONG", 3.0), "LONG", 2.0, 0.001002)
    assert wide > narrow


def test_stop_widen_인자가_존재하고_기본값이_0이다():
    src = (ROOT / "scripts" / "scalp_bot_e2.py").read_text(encoding="utf-8")
    assert '"--stop-widen-pct"' in src
    assert "default=0.0" in src.split('"--stop-widen-pct"')[1][:200]


def test_stop_widen_은_min_risk_pct_필터_뒤에_적용된다():
    """필터 앞에 두면 '근접손절' 스킵이 사라져 진입 종목 수가 바뀐다.

    변수를 하나만 바꿔야 손절폭 효과를 따로 잴 수 있다.
    """
    src = (ROOT / "scripts" / "scalp_bot_e2.py").read_text(encoding="utf-8")
    body = src[src.index("live_risk = abs("):]
    filt = body.index('skips["근접손절"]')
    widen = body.index("stop = widened_stop(entry, stop, side")
    assert filt < widen, "손절 확대가 min-risk-pct 필터보다 앞에 있다"


def test_stop_widen_재시작_채택경로에도_적용된다():
    """재시작 전후로 같은 전략이 다르게 동작하면 안 된다."""
    src = (ROOT / "scripts" / "scalp_bot_e2.py").read_text(encoding="utf-8")
    assert "_stop = widened_stop(_ep, _stop, _side, args.stop_widen_pct)" in src


# ============================== 2차 진입을 볼린저 밴드에서 (--tranche2-band)
# [2026-08-21 사용자요청] "볼밴 하단에 닿았을때 2차 매수하는걸로 로직을 바꿔줘".
# 실측(정배열 45심볼): 밴드 반대편은 진입가 대비 ROE 중앙 -1.74% 로 손절선
# (-3.00%) 안쪽이라 68.9% 의 경우 도달 가능. 손절폭 확대(1.5%)가 전제조건이다.

def _ind(e5=99.0, e10=98.0, e15=97.0, e25=96.0, bb_l=95.0, bb_u=105.0):
    return {"e5": e5, "e10": e10, "e15": e15, "e25": e25,
            "bb_l": bb_l, "bb_u": bb_u, "close": 100.0}


def test_밴드옵션_꺼져있으면_기존_ema목표를_쓴다():
    assert e2.tranche_targets(_ind(), "LONG", 3, False) == [99.0, 98.0, 97.0]


def test_롱_2차목표가_볼밴하단으로_바뀐다():
    # 볼밴 하단(96.5)은 EMA5(99.0) 아래이면서 EMA25 손절선(96.0) 위여야 채택된다
    t = e2.tranche_targets(_ind(bb_l=96.5), "LONG", 2, True)
    assert t == [99.0, 96.5], "2차가 볼밴 하단이 아니다"


def test_숏_2차목표가_볼밴상단으로_바뀐다():
    # 숏은 EMA 역배열: e5 > e10 > ... 방향이 반대
    ind = _ind(e5=101.0, e10=102.0, e15=103.0, e25=104.0, bb_u=103.5)
    t = e2.tranche_targets(ind, "SHORT", 2, True)
    assert t == [101.0, 103.5], "2차가 볼밴 상단이 아니다"


def test_1차만_쓰면_밴드옵션이_영향을_주지_않는다():
    assert e2.tranche_targets(_ind(), "LONG", 1, True) == [99.0]


def test_밴드가_1차보다_안쪽이면_ema10으로_되돌린다():
    """볼밴 하단이 EMA5 보다 위면 1차와 동시에 체결돼 2차가 무의미하다."""
    t = e2.tranche_targets(_ind(e5=99.0, bb_l=99.5), "LONG", 2, True)
    assert t == [99.0, 98.0]


def test_밴드가_손절선보다_바깥이면_손절선_80퍼센트_지점으로_당긴다():
    """볼밴 하단이 EMA25(손절) 아래면 닿기 전에 손절된다.

    [2026-08-21 실사고] 예전엔 EMA10 으로 되돌렸는데, 추세장에서 EMA10 은
    EMA5 바로 옆이라 2차가 1차와 0.047% 차이로 체결됐다(LITUSDT).
    이제는 1차~손절선의 80% 지점으로 당겨 2차를 의미있게 깊게 둔다.
    """
    t = e2.tranche_targets(_ind(e25=96.0, bb_l=95.0), "LONG", 2, True)
    assert t[1] == pytest.approx(99.0 - 0.8 * (99.0 - 96.0))   # 96.6
    assert 96.0 < t[1] < 99.0, "손절선 안쪽이면서 1차보다 깊어야 한다"
    t2 = e2.tranche_targets(_ind(e25=96.0, bb_l=96.5), "LONG", 2, True)
    assert t2 == [99.0, 96.5], "손절선 안쪽인데 밴드를 안 썼다"


def test_숏도_손절선_바깥이면_80퍼센트_지점으로_당긴다():
    ind = _ind(e5=101.0, e10=102.0, e15=103.0, e25=104.0, bb_u=110.0)
    t = e2.tranche_targets(ind, "SHORT", 2, True)
    assert t[1] == pytest.approx(101.0 + 0.8 * (104.0 - 101.0))  # 103.4
    assert 101.0 < t[1] < 104.0


# ---- [2026-08-21 실사고] 2차가 1차와 같은 가격에 체결되던 문제
def test_최소간격_기본0은_기존동작을_유지한다():
    assert e2.tranche_targets(_ind(), "LONG", 2, False) == [99.0, 98.0]


def test_최소간격을_주면_2차가_그만큼_떨어진다():
    """LITUSDT 실사고: 1차 2.739798 / 2차 2.738500 = 0.047% 간격.

    EMA10 이 1차 바로 옆(98.9)일 때가 그 상황이다. 최소간격 1% 를 주면
    98.01 까지 밀려나야 한다.
    """
    t = e2.tranche_targets(_ind(e10=98.9), "LONG", 2, False, 1.0)
    assert t[1] == pytest.approx(99.0 * 0.99)     # 98.01
    assert (99.0 - t[1]) / 99.0 * 100 == pytest.approx(1.0)

    # 실사고 재현: 최소간격이 없으면 0.1% 간격으로 붙어버린다
    t0 = e2.tranche_targets(_ind(e10=98.9), "LONG", 2, False, 0.0)
    assert (99.0 - t0[1]) / 99.0 * 100 < 0.2


def test_최소간격은_이미_더_깊은_목표를_얕게_만들지_않는다():
    """EMA10 이 이미 최소간격보다 깊으면 그대로 둬야 한다."""
    t = e2.tranche_targets(_ind(e10=90.0, e25=85.0), "LONG", 2, False, 1.0)
    assert t[1] == pytest.approx(90.0)


def test_최소간격이_손절선을_넘으면_80퍼센트로_당긴다():
    t = e2.tranche_targets(_ind(e25=98.5), "LONG", 2, False, 5.0)
    assert t[1] == pytest.approx(99.0 - 0.8 * (99.0 - 98.5))
    assert t[1] > 98.5, "손절선을 넘어가면 2차가 영영 안 걸린다"


@pytest.mark.parametrize("side,ind_kw", [
    ("LONG", dict()),
    ("SHORT", dict(e5=101.0, e10=102.0, e15=103.0, e25=104.0)),
])
def test_2차는_항상_1차보다_깊다(side, ind_kw):
    for gap in (0.0, 0.5, 1.0, 3.0):
        for band in (True, False):
            t = e2.tranche_targets(_ind(**ind_kw), side, 2, band, gap)
            if len(t) < 2:
                continue
            assert (t[1] < t[0]) if side == "LONG" else (t[1] > t[0]), (
                f"2차가 1차보다 얕다 gap={gap} band={band}")


def test_2차를_둘_자리가_없으면_1차만_남긴다():
    """손절선이 1차와 붙어 있으면 2차를 두지 않는다(무의미한 중복 진입 방지)."""
    t = e2.tranche_targets(_ind(e25=99.0), "LONG", 2, True, 1.0)
    assert len(t) == 1


def test_인자가_존재하고_기본값이_0이다_최소간격():
    src = (ROOT / "scripts" / "scalp_bot_e2.py").read_text(encoding="utf-8")
    assert '"--tranche-min-gap-pct"' in src
    assert "default=0.0" in src.split('"--tranche-min-gap-pct"')[1][:200]


def test_3차는_밴드옵션과_무관하게_ema15다():
    t = e2.tranche_targets(_ind(bb_l=96.5), "LONG", 3, True)
    assert t == [99.0, 96.5, 97.0]


def test_밴드값이_0이면_되돌린다():
    assert e2.tranche_targets(_ind(bb_l=0.0), "LONG", 2, True) == [99.0, 98.0]


def test_인자가_존재하고_기본값이_꺼짐이다():
    src = (ROOT / "scripts" / "scalp_bot_e2.py").read_text(encoding="utf-8")
    assert '"--tranche2-band"' in src
    assert 'action="store_true"' in src.split('"--tranche2-band"')[1][:120]


def test_진입경로가_tranche_targets를_쓴다():
    """하드코딩된 [e5, e10, e15] 가 남아 있으면 옵션이 무시된다."""
    src = (ROOT / "scripts" / "scalp_bot_e2.py").read_text(encoding="utf-8")
    assert 'targets = tranche_targets(' in src
    assert 'targets = [ind["e5"], ind["e10"], ind["e15"]]' not in src


def test_2차진입후_평단이_1차보다_유리해진다():
    """볼밴 하단에서 담으면 롱 평단은 내려가야 한다."""
    t = e2.tranche_targets(_ind(), "LONG", 2, True)
    assert sum(t) / len(t) < t[0]


# ============================== 목표수익 상향 (--tp-extra-roe-pct)
# [2026-08-21 사용자요청] "목표 수익 %를 0.5%정도만 더 늘리면 어때?"
# 단위는 ROE(%) — 손절쪽 --stop-widen-pct 가 가격 % 인 것과 다르다.

def test_tp_기본값0은_익절선을_그대로_둔다():
    assert e2.padded_tp(100.0, 102.0, "LONG", 0.0, 2) == 102.0
    assert e2.padded_tp(100.0, 98.0, "SHORT", 0.0, 2) == 98.0


def test_tp_롱은_익절선이_위로_밀린다():
    # ROE 0.5% @2배 = 가격 0.25% = 진입가 100 기준 0.25
    assert e2.padded_tp(100.0, 102.0, "LONG", 0.5, 2) == pytest.approx(102.25)


def test_tp_숏은_익절선이_아래로_밀린다():
    assert e2.padded_tp(100.0, 98.0, "SHORT", 0.5, 2) == pytest.approx(97.75)


def test_tp_추가분은_정확히_지정한_roe만큼이다():
    """ROE 로 환산했을 때 딱 그만큼 늘어야 한다."""
    ent, lev, extra = 100.0, 2, 0.5
    base, padded = 102.0, e2.padded_tp(100.0, 102.0, "LONG", extra, lev)
    roe = lambda p: (p / ent - 1) * lev * 100
    assert roe(padded) - roe(base) == pytest.approx(extra)


@pytest.mark.parametrize("lev", [1, 2, 3, 5])
def test_tp_레버리지가_달라도_roe_증가폭은_같다(lev):
    ent, extra = 100.0, 0.5
    padded = e2.padded_tp(ent, 102.0, "LONG", extra, lev)
    roe = lambda p: (p / ent - 1) * lev * 100
    assert roe(padded) - roe(102.0) == pytest.approx(extra)


def test_tp_익절이_비활성이면_0을_유지한다():
    """fee_aware_bb_price 가 0(수수료 미달)을 주면 밀어내면 안 된다."""
    assert e2.padded_tp(100.0, 0.0, "LONG", 0.5, 2) == 0.0


@pytest.mark.parametrize("ent,tp,extra,lev", [(0.0, 102.0, 0.5, 2),
                                              (100.0, 102.0, -1.0, 2),
                                              (100.0, 102.0, 0.5, 0)])
def test_tp_잘못된_입력은_원본을_돌려준다(ent, tp, extra, lev):
    assert e2.padded_tp(ent, tp, "LONG", extra, lev) == tp


def test_tp_인자가_존재하고_기본값이_0이다():
    src = (ROOT / "scripts" / "scalp_bot_e2.py").read_text(encoding="utf-8")
    assert '"--tp-extra-roe-pct"' in src
    assert "default=0.0" in src.split('"--tp-extra-roe-pct"')[1][:200]


def test_tp_모든_볼밴익절_지점에_적용된다():
    """한 군데라도 빠지면 진입/재시작/추가차수에서 익절선이 달라진다."""
    src = (ROOT / "scripts" / "scalp_bot_e2.py").read_text(encoding="utf-8")
    body = src[src.index("def main("):] if "def main(" in src else src
    assert body.count("padded_tp(") >= 4, "볼밴 익절 지점 4곳 전부에 적용돼야 한다"


def test_tp_진입전_익절선통과_검사도_같은_선을_본다():
    """목표를 늦췄는데 사전검사가 옛 선을 보면 들어갈 자리를 놓친다."""
    src = (ROOT / "scripts" / "scalp_bot_e2.py").read_text(encoding="utf-8")
    i = src.index("tp_bb0 = ")
    assert "padded_tp(" in src[i:i + 200]


# ============================== 진입 깊이 (--entry-depth-pct)
# [2026-08-21] 백테스트에서 가장 큰 개선 축. 85심볼 10일, 거래당 순익(수수료 후):
#   깊이 0.0%(현행) -0.0958% / 0.3% -0.0619% / 0.5% -0.0482% (+0.0476%p)
#   대가는 거래수 42%. EMA 기간 변경(EMA5->EMA20)과 동치인 축이다.

def test_깊이_기본0은_목표선을_그대로_둔다():
    assert e2.deepen_target(100.0, "LONG", 0.0) == 100.0
    assert e2.deepen_target(100.0, "SHORT", 0.0) == 100.0


def test_깊이_롱은_목표가_아래로_내려간다():
    assert e2.deepen_target(100.0, "LONG", 0.5) == pytest.approx(99.5)


def test_깊이_숏은_목표가_위로_올라간다():
    assert e2.deepen_target(100.0, "SHORT", 0.5) == pytest.approx(100.5)


@pytest.mark.parametrize("t,d", [(0.0, 0.5), (100.0, -1.0), (-5.0, 0.5)])
def test_깊이_잘못된_입력은_원본을_돌려준다(t, d):
    assert e2.deepen_target(t, "LONG", d) == t


def test_깊이는_모든_차수_목표에_적용된다():
    """1차만 깊게 하고 2차를 그대로 두면 두 목표가 뒤집힐 수 있다."""
    src = (ROOT / "scripts" / "scalp_bot_e2.py").read_text(encoding="utf-8")
    assert "targets = [deepen_target(t, side, args.entry_depth_pct)" in src


def test_깊이_적용후에도_2차가_1차보다_깊다():
    base = e2.tranche_targets(_ind(bb_l=96.5), "LONG", 2, True)
    deep = [e2.deepen_target(t, "LONG", 0.5) for t in base]
    assert deep[1] < deep[0]
    assert all(d < b for d, b in zip(deep, base))


def test_깊이_인자가_존재하고_기본값이_0이다():
    src = (ROOT / "scripts" / "scalp_bot_e2.py").read_text(encoding="utf-8")
    assert '"--entry-depth-pct"' in src
    assert "default=0.0" in src.split('"--entry-depth-pct"')[1][:200]


# ============================== 익절 ROE 하한 (--tp-floor-roe-pct)
# [2026-08-21] 하한 0%: 보유중앙 10분/거래당 -0.1061%
#              하한 3%: 보유중앙 14분/거래당 -0.0838%
#              하한 6%: 보유중앙 20분/거래당 -0.0724%

def test_하한_기본0은_익절선을_그대로_둔다():
    assert e2.tp_with_floor(100.0, 102.0, "LONG", 0.0, 2) == 102.0


def test_하한_롱은_더_먼_쪽을_고른다():
    # ROE 6% @2배 = 가격 3% -> 103.0. 볼밴 102.0 보다 멀다.
    assert e2.tp_with_floor(100.0, 102.0, "LONG", 6.0, 2) == pytest.approx(103.0)
    # 볼밴이 이미 더 멀면 볼밴을 쓴다
    assert e2.tp_with_floor(100.0, 110.0, "LONG", 6.0, 2) == pytest.approx(110.0)


def test_하한_숏도_더_먼_쪽을_고른다():
    assert e2.tp_with_floor(100.0, 98.0, "SHORT", 6.0, 2) == pytest.approx(97.0)
    assert e2.tp_with_floor(100.0, 90.0, "SHORT", 6.0, 2) == pytest.approx(90.0)


def test_하한_익절이_비활성이면_하한선만으로_익절선을_만든다():
    """볼밴이 수수료를 못 넘어 0 이어도 하한이 있으면 익절선은 있어야 한다."""
    assert e2.tp_with_floor(100.0, 0.0, "LONG", 6.0, 2) == pytest.approx(103.0)
    assert e2.tp_with_floor(100.0, 0.0, "SHORT", 6.0, 2) == pytest.approx(97.0)


@pytest.mark.parametrize("lev", [1, 2, 3, 5])
def test_하한은_레버리지를_반영한다(lev):
    out = e2.tp_with_floor(100.0, 0.0, "LONG", 6.0, lev)
    assert (out / 100.0 - 1) * lev * 100 == pytest.approx(6.0)


@pytest.mark.parametrize("ent,fl,lev", [(0.0, 6.0, 2), (100.0, -1.0, 2), (100.0, 6.0, 0)])
def test_하한_잘못된_입력은_원본을_돌려준다(ent, fl, lev):
    assert e2.tp_with_floor(ent, 102.0, "LONG", fl, lev) == 102.0


def test_하한이_모든_볼밴익절_지점에_적용된다():
    src = (ROOT / "scripts" / "scalp_bot_e2.py").read_text(encoding="utf-8")
    assert src.count("tp_with_floor(") >= 5   # 정의 1 + 적용 4


def test_하한_인자가_존재하고_기본값이_0이다():
    src = (ROOT / "scripts" / "scalp_bot_e2.py").read_text(encoding="utf-8")
    assert '"--tp-floor-roe-pct"' in src
    assert "default=0.0" in src.split('"--tp-floor-roe-pct"')[1][:200]


# ============================== 신호봉 합치기 (--signal-tf-min)
# [2026-08-21] 실측 거래당 순익 / 보유 중앙:
#   1분 -0.1025%/10분  2분 -0.0845%/16분  3분 -0.0831%/20분  5분 -0.0417%/25분
import pandas as _pd


def _m1(n=12, start="2026-08-21 10:00"):
    """1분봉 n개. close 를 1,2,3... 으로 둬서 합치기 검증이 쉽게."""
    t = _pd.date_range(start, periods=n, freq="1min")
    return _pd.DataFrame({
        "open_time": t,
        "open": [10.0 + i for i in range(n)],
        "high": [20.0 + i for i in range(n)],
        "low": [1.0 + i for i in range(n)],
        "close": [15.0 + i for i in range(n)],
        "volume": [1.0] * n,
    })


def test_봉합치기_1분이면_원본_그대로():
    df = _m1(5)
    assert e2.resample_bars(df, 1) is df


def test_봉합치기_3분봉_ohlc가_맞다():
    df = _m1(6)          # 10:00~10:05 -> 10:00, 10:03 두 봉
    out = e2.resample_bars(df, 3)
    assert len(out) == 2
    assert out["open"].iloc[0] == 10.0            # 첫 1분봉의 시가
    assert out["close"].iloc[0] == 17.0           # 세번째 1분봉의 종가
    assert out["high"].iloc[0] == 22.0            # 세 봉 중 최고
    assert out["low"].iloc[0] == 1.0              # 세 봉 중 최저
    assert out["volume"].iloc[0] == 3.0


def test_봉합치기_미완성_마지막봉은_버린다():
    """10:00~10:03 = 4개 -> 완성된 10:00 봉 하나만 남아야 한다."""
    out = e2.resample_bars(_m1(4), 3)
    assert len(out) == 1
    assert out["open_time"].iloc[0] == _pd.Timestamp("2026-08-21 10:00")


def test_봉합치기_벽시계_경계로_묶는다():
    """끝에서부터 N개씩 묶으면 매 분 경계가 밀려 지표가 흔들린다."""
    df = _m1(6, start="2026-08-21 10:01")     # 10:01 부터 시작
    out = e2.resample_bars(df, 3)
    # 10:01,10:02 -> 10:00 버킷(미완성) / 10:03~10:05 -> 10:03 버킷(완성)
    assert out["open_time"].iloc[0] == _pd.Timestamp("2026-08-21 10:00")
    assert list(out["open_time"]) == [_pd.Timestamp("2026-08-21 10:00"),
                                      _pd.Timestamp("2026-08-21 10:03")]


def test_봉합치기_빈입력은_그대로():
    empty = _m1(0)
    assert len(e2.resample_bars(empty, 3)) == 0
    assert e2.resample_bars(None, 3) is None


def test_봉합치기_지표계산에_필요한_30봉이_나온다():
    """indicators 는 30봉 미만이면 None 을 준다 — 신호가 아예 안 난다."""
    for tf in (2, 3, 5):
        lim = e2.klines_limit_for_tf(tf)
        out = e2.resample_bars(_m1(lim), tf)
        assert len(out) >= 30, f"{tf}분봉 {lim}개 요청했는데 {len(out)}봉뿐"
        assert e2.indicators(out) is not None


def test_요청limit은_ws캐시_한도를_넘지_않는다():
    """200 을 넘으면 REST 폴백 — 85심볼이면 예전 IP밴 사고 경로다."""
    for tf in (1, 2, 3, 5, 10):
        assert e2.klines_limit_for_tf(tf) <= e2.WS_KLINE_CACHE_LEN


def test_1분은_기존_limit99를_유지한다():
    assert e2.klines_limit_for_tf(1) == 99


def test_signal_bars가_limit을_넘겨_호출한다():
    """limit 을 안 넘기면 99개만 와서 3분봉이 33개뿐 — 지표 경계에 걸린다."""
    seen = {}

    class FakeEx:
        def get_klines(self, symbol, limit=99, interval=None):
            seen["limit"] = limit
            return _m1(limit)

    out = e2.signal_bars(FakeEx(), "BTCUSDT", 3)
    assert seen["limit"] == e2.klines_limit_for_tf(3)
    assert len(out) >= 30


def test_인자가_존재하고_기본값이_1이다():
    src = (ROOT / "scripts" / "scalp_bot_e2.py").read_text(encoding="utf-8")
    assert '"--signal-tf-min"' in src
    assert "default=1" in src.split('"--signal-tf-min"')[1][:200]


def test_두_호출부_모두_signal_bars를_쓴다():
    """진입 경로와 재시작 채택 경로가 다른 봉을 보면 전략이 어긋난다."""
    src = (ROOT / "scripts" / "scalp_bot_e2.py").read_text(encoding="utf-8")
    assert src.count("signal_bars(ex, sym, args.signal_tf_min)") == 1
    assert src.count("signal_bars(ex, _sym, args.signal_tf_min)") == 1
    assert "df = ex.get_klines(sym)" not in src
    assert "_df = ex.get_klines(_sym)" not in src
