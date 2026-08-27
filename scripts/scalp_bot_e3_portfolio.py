from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bot.config import Config
from bot.exchange import Exchange
from bot.grid_chart import render_grid_chart, slot_payload
from bot.grid_e3 import GridState
from scripts.scalp_bot_e3 import (
    Tg,
    acquire_bot_lock,
    expand_range_cycle,
    release_bot_lock,
    configure_symbol,
    ensure_grid_orders,
    fetch_open_regular_orders,
    flatten_position,
    handle_global_stop,
    log_event,
    make_cycle_state,
    process_dry_fills,
    process_live_fills,
    reconcile_cycle_state_with_exchange,
    parse_symbol_csv,
    rank_candidate_symbols,
    try_place_limit_order,
)

VERSION = "e3_portfolio"
ROOT = Path(__file__).resolve().parent.parent
LOG_DIR = ROOT / "logs"
STATE = LOG_DIR / f"{VERSION}_state.json"


@dataclass
class SlotState:
    slot_id: int
    capital_ratio: float
    state: dict | None = None
    strategy_id: str = ""
    width_pct: float = 10.0
    grid_count: int = 5
    last_switch_at: float = 0.0
    last_score: float = 0.0
    paused: bool = False


@dataclass
class PortfolioState:
    wallet_balance_start: float
    slots: list[SlotState] = field(default_factory=list)
    candidate_snapshot: list[dict] = field(default_factory=list)
    legacy_positions: list[dict] = field(default_factory=list)
    started_at: float = field(default_factory=time.time)
    loss_pause_until: float = 0.0
    loss_guard_last_trigger_at: float = 0.0


def save_portfolio_state(portfolio: PortfolioState) -> None:
    STATE.write_text(json.dumps(asdict(portfolio), ensure_ascii=False, indent=2), encoding="utf-8")


def load_portfolio_state() -> PortfolioState | None:
    if not STATE.exists():
        return None
    try:
        data = json.loads(STATE.read_text(encoding="utf-8"))
    except Exception:
        return None
    slots = [SlotState(**slot) for slot in data.get("slots", [])]
    return PortfolioState(
        wallet_balance_start=float(data["wallet_balance_start"]),
        slots=slots,
        candidate_snapshot=list(data.get("candidate_snapshot", [])),
        legacy_positions=list(data.get("legacy_positions", [])),
        started_at=float(data.get("started_at", time.time())),
        loss_pause_until=float(data.get("loss_pause_until", 0.0) or 0.0),
        loss_guard_last_trigger_at=float(data.get("loss_guard_last_trigger_at", 0.0) or 0.0),
    )


def cycle_state_to_dict(state) -> dict:
    return asdict(state)


def cycle_state_from_dict(payload: dict):
    from scripts.scalp_bot_e3 import CycleState

    return CycleState(**payload)


def reconcile_portfolio_with_exchange(ex: Exchange, portfolio: PortfolioState) -> PortfolioState:
    open_orders = list_account_open_orders(ex)
    open_positions = list_account_open_positions(ex)
    orders_by_symbol: dict[str, list[dict]] = {}
    for order in open_orders:
        sym = str(order.get("symbol") or "")
        if not sym:
            continue
        orders_by_symbol.setdefault(sym, []).append(order)
    position_symbols = {
        str(row.get("symbol") or "")
        for row in open_positions
        if row.get("symbol")
    }

    for slot in portfolio.slots:
        if not slot.state:
            continue
        sym = str(slot.state.get("symbol") or "")
        if not sym:
            slot.state = None
            slot.strategy_id = ""
            continue
        has_orders = bool(orders_by_symbol.get(sym))
        has_position = sym in position_symbols
        if has_orders or has_position:
            continue
        log_event(
            "PORTFOLIO_RESUME_CLEAR",
            slot_id=slot.slot_id,
            symbol=sym,
            reason="exchange_empty",
        )
        slot.state = None
        slot.strategy_id = ""
        slot.width_pct = 10.0
        slot.grid_count = 5
        slot.last_score = 0.0
        slot.last_switch_at = 0.0
    return portfolio


def pick_symbols(candidates: list[dict], n: int) -> list[dict]:
    out: list[dict] = []
    used: set[str] = set()
    for row in candidates:
        sym = row["symbol"]
        if sym in used:
            continue
        used.add(sym)
        out.append(row)
        if len(out) >= n:
            break
    return out


def build_strategy_profiles(mode: str) -> list[dict]:
    if mode == "fast":
        return [
            {"id": "speed_6x4", "width_pct": 6.0, "grid_count": 4},
            # [2026-08-21] 체결이 너무 느려 격자를 촘촘히 깐 프로파일을 추가한다.
            # 실측: 격자 4칸이면 간격 4.26%, 회전 중앙 270분(4.5시간)이었다.
            # 칸당 명목에 여유가 있었다(10.09 vs 최소 5.0).
            # 격자를 늘리는 것은 총 명목이 그대로라 재고 위험을 키우지 않는다.
            # 범위 이동이나 폭 축소와 달리 내려갈 때 사 모으는 총량이 같다.
            #   간격 = 폭 x 2 / (격자수 - 1)
            {"id": "rapid_5x6", "width_pct": 5.0, "grid_count": 6},   # 2.00%
            {"id": "rapid_6x7", "width_pct": 6.0, "grid_count": 7},   # 2.00%
            {"id": "quick_6x6", "width_pct": 6.0, "grid_count": 6},   # 2.40%
            {"id": "quick_8x7", "width_pct": 8.0, "grid_count": 7},   # 2.67%
            {"id": "turbo_8x3", "width_pct": 8.0, "grid_count": 3},
            {"id": "fast_8x4", "width_pct": 8.0, "grid_count": 4},
            {"id": "fast_8x5", "width_pct": 8.0, "grid_count": 5},
            {"id": "base_10x4", "width_pct": 10.0, "grid_count": 4},
        ]
    return [
        {"id": "base_10x5", "width_pct": 10.0, "grid_count": 5},
    ]


def estimate_cycle_profit_usdt(price: float, width_pct: float, grid_count: int, qty_per_rung: float) -> float:
    if price <= 0 or grid_count < 2 or qty_per_rung <= 0:
        return 0.0
    low = price * (1.0 - width_pct / 100.0)
    high = price * (1.0 + width_pct / 100.0)
    gap = (high - low) / (grid_count - 1)
    return max(0.0, gap * qty_per_rung)


def score_profile_candidate(row: dict, width_pct: float, grid_count: int) -> dict:
    gap_pct = (width_pct * 2.0) / max(grid_count - 1, 1)
    turnover_ratio = row["mean_abs_1m_pct"] / max(gap_pct, 1e-9)
    cycle_profit_est = estimate_cycle_profit_usdt(
        row["price"], width_pct, grid_count, row["qty_per_rung"]
    )
    ret60 = abs(float(row.get("ret_60_pct", 0.0) or 0.0))
    ret180 = abs(float(row.get("ret_180_pct", 0.0) or 0.0))
    ret24h = abs(float(row.get("ret_24h_pct", 0.0) or 0.0))
    pump_penalty = (
        max(0.0, ret60 - 6.0) * 2.0
        + max(0.0, ret180 - 15.0) * 0.8
        + max(0.0, ret24h - 10.0) * 2.2
    )
    row = dict(row)
    row["strategy_id"] = f"{int(width_pct)}x{grid_count}"
    row["profile_width_pct"] = width_pct
    row["profile_grid_count"] = grid_count
    row["gap_pct"] = round(gap_pct, 4)
    row["turnover_ratio"] = round(turnover_ratio, 4)
    row["cycle_profit_est"] = round(cycle_profit_est, 6)
    row["pump_penalty"] = round(pump_penalty, 4)
    row["profile_score"] = round(
        row["score"] + cycle_profit_est * 4.0 + turnover_ratio * 40.0 - pump_penalty,
        4,
    )
    return row


def select_profile_candidates(
    ex: Exchange,
    wallet_balance: float,
    leverage: int,
    capital_usage: float,
    candidate_limit: int,
    top_n: int,
    exclude_symbols: set[str],
    min_width_pct: float,
    min_mean_abs_1m_pct: float,
    max_spread_pct: float,
    max_abs_ret_24h_pct: float,
    max_abs_ret_60_pct: float,
    max_abs_ret_180_pct: float,
    profiles: list[dict],
) -> list[dict]:
    rows: list[dict] = []
    for profile in profiles:
        base_rows = rank_candidate_symbols(
            ex,
            wallet_balance,
            leverage,
            profile["grid_count"],
            capital_usage,
            candidate_limit=candidate_limit,
            top_n=top_n,
            exclude_symbols=exclude_symbols,
            min_width_pct=min_width_pct,
            min_mean_abs_1m_pct=min_mean_abs_1m_pct,
            max_spread_pct=max_spread_pct,
            max_abs_ret_24h_pct=max_abs_ret_24h_pct,
        )
        for row in base_rows:
            if abs(float(row.get("ret_24h_pct", 0.0) or 0.0)) > max_abs_ret_24h_pct:
                continue
            if abs(float(row.get("ret_60_pct", 0.0) or 0.0)) > max_abs_ret_60_pct:
                continue
            if abs(float(row.get("ret_180_pct", 0.0) or 0.0)) > max_abs_ret_180_pct:
                continue
            rows.append(
                score_profile_candidate(
                    row,
                    width_pct=profile["width_pct"],
                    grid_count=profile["grid_count"],
                )
            )
    rows.sort(key=lambda x: x["profile_score"], reverse=True)
    return rows


def filter_profiles_for_wallet(wallet_balance: float, profiles: list[dict]) -> list[dict]:
    # [2026-08-21] 소액 지갑에서도 촘촘한 격자를 허용한다.
    # 기존에는 4칸(간격 4~5.3%)만 허용돼 회전 중앙이 270분이었다.
    # 칸당 명목이 최소주문(5 USDT)을 넘는지는 make_cycle_state 가 따로 막으므로,
    # 여기서는 후보만 넓히고 실제 가능 여부는 그쪽 판정에 맡긴다.
    if wallet_balance < 250.0:
        allowed = {"speed_6x4", "fast_8x4",
                   "rapid_5x6", "rapid_6x7", "quick_6x6", "quick_8x7"}
    elif wallet_balance < 300.0:
        allowed = {"speed_6x4", "fast_8x4",
                   "rapid_5x6", "rapid_6x7", "quick_6x6", "quick_8x7"}
    else:
        allowed = {"fast_8x4", "speed_6x4", "turbo_8x3", "fast_8x5", "base_10x4"}
    out = [p for p in profiles if p["id"] in allowed]
    return out or list(profiles)


def slot_summary(slot: SlotState) -> str:
    """한 줄에 슬롯 상태를 담는다.

    [2026-08-21] 기존에는 ratio/score/switches 만 있어 격자가 어떤 상태인지
    전혀 알 수 없었다. 보유·대기 칸수와 격자 실현을 넣는다.
    """
    if not slot.state:
        return f"slot{slot.slot_id} · 비어 있음"
    st = slot.state
    levels = st.get("levels") or []
    held = len(st.get("held_buy_rungs") or [])
    nb = len(st.get("buy_orders") or {})
    ns = len(st.get("sell_orders") or {})
    realized = float(st.get("realized_grid_profit_est", 0.0) or 0.0)
    return (
        f"slot{slot.slot_id} 배정 {st['symbol']} {slot.strategy_id or '-'}"
        f"  보유 {held}/{max(len(levels) - 1, 0)}칸"
        f"  대기 매수{nb} 매도{ns}"
        f"  격자실현 {realized:+.4f}"
        f"  재센터 {st.get('reset_count', 0)}회  점수 {slot.last_score:.1f}"
    )


def _grid_stats(since: float = 0.0) -> dict:
    """원장에서 격자 짝(매수체결 -> 매도체결)을 복원해 실적을 낸다.

    [2026-08-21] 브리핑에 체류시간·승률·순익을 넣기 위해 추가.
    격자는 사는 자리와 파는 자리가 정해져 있어 짝이 맞으면 항상 이익이지만,
    수수료를 빼면 손실이 될 수 있으므로 승률을 따로 센다.
    """
    from scripts.scalp_bot_e3 import LEDGER
    if not LEDGER.exists():
        return {}
    rows = []
    try:
        for line in LEDGER.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                r = json.loads(line)
            except Exception:
                continue
            if r.get("ts", 0) >= since:
                rows.append(r)
    except Exception:
        return {}
    rows.sort(key=lambda r: r.get("ts", 0))

    open_buy: dict = {}
    pairs = []
    n_buy = n_sell = 0
    for r in rows:
        kind, sym = r.get("kind"), r.get("symbol")
        if kind == "BUY_FILLED":
            n_buy += 1
            open_buy.setdefault((sym, r.get("rung")), []).append(r)
        elif kind == "SELL_FILLED":
            n_sell += 1
            br = r.get("paired_buy_rung")
            if br is None:
                br = (r.get("rung") or 1) - 1
            q = open_buy.get((sym, br))
            if q:
                b = q.pop(0)
                gain = (float(r.get("price", 0)) - float(b.get("price", 0))) \
                    * float(r.get("quantity", 0) or b.get("quantity", 0) or 0)
                pairs.append((r["ts"] - b["ts"], gain))
    return {
        "n_buy": n_buy,
        "n_sell": n_sell,
        "open_rungs": sum(len(v) for v in open_buy.values()),
        "pairs": pairs,
        "switches": sum(1 for r in rows if r.get("kind") == "PORTFOLIO_SWITCH"),
        "expands": sum(1 for r in rows if r.get("kind") in ("RANGE_EXPAND", "RECENTER")),
        "failed": sum(1 for r in rows if r.get("kind") == "PLACE_ORDER_FAILED"),
    }


def summarize_portfolio(portfolio: PortfolioState, wallet_balance: float) -> str:
    """브리핑 본문.

    [2026-08-21 사용자요청] 슬롯 체류시간 / 순익 / 승률 /
    자산 증감액과 전체 대비 % 를 담는다.
    """
    base = portfolio.wallet_balance_start or 1.0
    pnl = wallet_balance - portfolio.wallet_balance_start
    pct = pnl / base * 100.0
    lines = [
        f"[e3 브리핑] {time.strftime('%m/%d %H:%M')}",
        f"자산 {wallet_balance:.4f} USDT  ({pnl:+.4f}, {pct:+.2f}%)",
        f"기준 {portfolio.wallet_balance_start:.4f} USDT",
    ]

    st_ = _grid_stats(portfolio.started_at)
    if st_:
        pairs = st_.get("pairs") or []
        if pairs:
            durs = sorted(d / 60.0 for d, _g in pairs)
            wins = sum(1 for _d, g in pairs if g > 0)
            gains = sum(g for _d, g in pairs)
            mid = durs[len(durs) // 2]
            lines += [
                "── 완결 격자 ──",
                f"  {len(pairs)}회  승률 {wins / len(pairs) * 100:.0f}%"
                f"  격자순익 {gains:+.4f} USDT",
                f"  체류 중앙 {mid:.0f}분  (최단 {durs[0]:.0f} / 최장 {durs[-1]:.0f})",
            ]
        else:
            lines.append("── 완결 격자 없음 (매수 후 매도 대기 중) ──")
        lines.append(
            f"  체결 매수{st_.get('n_buy', 0)} 매도{st_.get('n_sell', 0)}"
            f"  미청산 칸 {st_.get('open_rungs', 0)}"
            f"  교체 {st_.get('switches', 0)}회"
            f"  확장 {st_.get('expands', 0)}회"
            + (f"  주문실패 {st_['failed']}" if st_.get("failed") else "")
        )

    lines.append("── 슬롯 ──")
    lines += ["  " + slot_summary(slot) for slot in portfolio.slots]
    lines.append("")
    lines.append("※ 격자순익은 맞물린 짝만 센 값입니다."
                 " 보유 중인 칸의 평가손익은 '자산' 쪽에 반영됩니다.")
    return "\n".join(lines)



def portfolio_chart(ex: Exchange, portfolio: PortfolioState, bars: int = 120) -> bytes:
    """슬롯별 격자 상태를 한 장의 PNG 로 만든다."""
    payloads = []
    for slot in portfolio.slots:
        if not slot.state:
            continue
        sym = slot.state["symbol"]
        try:
            mark = ex.get_mark_price(sym)
        except Exception:
            mark = 0.0
        prices = []
        try:
            df = ex.get_klines(sym)
            prices = [float(x) for x in df["close"].tolist()[-bars:]]
        except Exception:
            prices = []
        levels = slot.state.get("levels") or []
        held = len(slot.state.get("held_buy_rungs") or [])
        title = (f"slot{slot.slot_id} {sym}  보유 {held}/{max(len(levels) - 1, 0)}칸"
                 f"  현재 {mark:.6g}")
        payloads.append(slot_payload(title, slot.state, mark, prices))
    return render_grid_chart(payloads)


def list_account_open_orders(ex: Exchange) -> list[dict]:
    try:
        return list(ex.client.futures_get_open_orders())
    except Exception:
        return []


def list_account_open_positions(ex: Exchange) -> list[dict]:
    try:
        rows = list(ex.client.futures_position_information())
    except Exception:
        return []
    out = []
    for row in rows:
        try:
            amt = float(row.get("positionAmt", 0) or 0)
        except (TypeError, ValueError):
            continue
        if abs(amt) > 0:
            out.append(row)
    return out


def assert_clean_account_or_raise(ex: Exchange) -> None:
    orders = list_account_open_orders(ex)
    positions = list_account_open_positions(ex)
    if not orders and not positions:
        return
    order_symbols = sorted({str(o.get("symbol", "")) for o in orders if o.get("symbol")})
    pos_symbols = sorted({str(p.get("symbol", "")) for p in positions if p.get("symbol")})
    raise RuntimeError(
        "포트폴리오 fresh start 차단: 기존 주문/포지션이 남아 있습니다. "
        f"open_orders={order_symbols} open_positions={pos_symbols}. "
        "--resume 으로 재개하거나 먼저 수동 정리하십시오."
    )


def snapshot_legacy_positions(ex: Exchange) -> list[dict]:
    out: list[dict] = []
    for row in list_account_open_positions(ex):
        try:
            amt = float(row.get("positionAmt", 0) or 0)
            entry_price = float(row.get("entryPrice", 0) or 0)
            break_even_price = float(row.get("breakEvenPrice", 0) or 0)
        except (TypeError, ValueError):
            continue
        if abs(amt) <= 0 or entry_price <= 0:
            continue
        out.append({
            "symbol": str(row["symbol"]),
            "side": "LONG" if amt > 0 else "SHORT",
            "amount": abs(amt),
            "entry_price": entry_price,
            "break_even_price": break_even_price if break_even_price > 0 else entry_price,
        })
    return out


def cancel_all_account_open_orders(ex: Exchange) -> list[str]:
    cancelled: list[str] = []
    for order in list_account_open_orders(ex):
        try:
            symbol = str(order["symbol"])
            ex.cancel_regular_order(symbol, int(order["orderId"]))
            cancelled.append(symbol)
        except Exception:
            continue
    return cancelled


def adopt_legacy_positions(ex: Exchange) -> list[dict]:
    cancelled_symbols = cancel_all_account_open_orders(ex)
    legacy = snapshot_legacy_positions(ex)
    if cancelled_symbols or legacy:
        log_event(
            "LEGACY_ADOPT",
            cancelled_symbols=sorted(set(cancelled_symbols)),
            legacy_symbols=[p["symbol"] for p in legacy],
        )
    return legacy


def close_legacy_positions_on_profit(ex: Exchange, legacy_positions: list[dict]) -> tuple[list[dict], list[dict]]:
    remaining: list[dict] = []
    alerts: list[dict] = []
    if hasattr(ex, "get_open_positions"):
        live_positions = ex.get_open_positions()
    elif hasattr(ex, "get_all_positions"):
        live_positions = ex.get_all_positions()
    else:
        live_positions = []
    live_by_symbol = {
        p["symbol"]: p for p in live_positions
        if abs(float(p.get("amount", 0) or 0)) > 0
    }
    for legacy in legacy_positions:
        live = live_by_symbol.get(legacy["symbol"])
        if not live:
            continue
        mark_price = float(live.get("mark_price", 0) or 0)
        trigger = float(legacy.get("break_even_price") or legacy.get("entry_price") or 0)
        side = str(legacy["side"])
        profitable = mark_price >= trigger if side == "LONG" else mark_price <= trigger
        if profitable:
            try:
                ex.close_market_position(legacy["symbol"], side, abs(float(live["amount"])))
                log_event(
                    "LEGACY_FLAT",
                    symbol=legacy["symbol"],
                    side=side,
                    mark_price=mark_price,
                    trigger_price=trigger,
                )
                alerts.append({
                    "symbol": legacy["symbol"],
                    "side": side,
                    "mark_price": mark_price,
                    "trigger_price": trigger,
                })
                continue
            except Exception:
                pass
        remaining.append(legacy)
    return remaining, alerts


def fetch_recent_income_events(ex: Exchange, hours: float) -> list[dict]:
    """거래소 원본 income history에서 최근 순손익 이벤트를 읽는다.

    손실 리밋은 로컬 원장보다 거래소 REALIZED_PNL/COMMISSION/FUNDING_FEE가 맞다.
    앱 강제 재시작 후에도 거래소 기록을 다시 읽으면 같은 리밋을 복원할 수 있다.
    """
    if hours <= 0:
        return []
    end_ms = int(time.time() * 1000)
    start_ms = int((time.time() - hours * 3600.0) * 1000)
    try:
        rows = ex.client.futures_income_history(startTime=start_ms, endTime=end_ms, limit=1000)
    except Exception:
        return []
    out: list[dict] = []
    for row in rows:
        typ = str(row.get("incomeType") or "")
        if typ not in {"REALIZED_PNL", "COMMISSION", "FUNDING_FEE"}:
            continue
        try:
            income = float(row.get("income") or 0.0)
            ts = float(row["time"]) / 1000.0
        except (TypeError, ValueError, KeyError):
            continue
        if income == 0:
            continue
        out.append({
            "ts": ts,
            "symbol": str(row.get("symbol") or ""),
            "type": typ,
            "income": income,
        })
    out.sort(key=lambda r: r["ts"])
    return out


def active_slot_symbols(portfolio: PortfolioState) -> set[str]:
    symbols = {
        str((slot.state or {}).get("symbol") or "")
        for slot in portfolio.slots
        if slot.state
    }
    symbols.discard("")
    return symbols


def filter_legacy_positions(legacy_positions: list[dict], slot_symbols: set[str]) -> list[dict]:
    return [
        p for p in legacy_positions
        if str(p.get("symbol") or "") not in slot_symbols
    ]


def rolling_income_sum(events: list[dict], now: float, window_sec: float) -> float:
    cutoff = now - window_sec
    return sum(float(e.get("income", 0.0) or 0.0) for e in events if float(e.get("ts", 0.0) or 0.0) >= cutoff)


def evaluate_loss_guard(
    events: list[dict],
    now: float,
    guard_30m_usdt: float,
    guard_60m_usdt: float,
) -> dict | None:
    """손실 리밋 발동 여부를 계산한다. threshold는 양수 USDT로 받는다."""
    checks = [
        ("30m", 30 * 60.0, guard_30m_usdt),
        ("60m", 60 * 60.0, guard_60m_usdt),
    ]
    triggered = []
    for label, window_sec, limit in checks:
        if limit <= 0:
            continue
        net = rolling_income_sum(events, now, window_sec)
        if net <= -abs(limit):
            triggered.append({
                "window": label,
                "window_sec": window_sec,
                "net": net,
                "limit": -abs(limit),
            })
    if not triggered:
        return None
    triggered.sort(key=lambda x: x["net"])
    return triggered[0]


def cancel_slot_buy_orders(ex: Exchange, slot: SlotState) -> int:
    if not slot.state:
        return 0
    state = cycle_state_from_dict(slot.state)
    cancelled = 0
    for payload in list(state.buy_orders.values()):
        try:
            ex.cancel_regular_order(state.symbol, int(payload["order_id"]))
            cancelled += 1
        except Exception:
            pass
    state.buy_orders = {}
    slot.state = cycle_state_to_dict(state)
    return cancelled


def apply_loss_pause(
    ex: Exchange,
    portfolio: PortfolioState,
    trigger: dict,
    pause_sec: float,
    now: float,
    live: bool,
) -> int:
    """손실 리밋 발동 시 신규 매수만 막는다. 보유 SELL/포지션은 유지한다."""
    portfolio.loss_pause_until = max(portfolio.loss_pause_until, now + pause_sec)
    portfolio.loss_guard_last_trigger_at = now
    cancelled = 0
    if live:
        for slot in portfolio.slots:
            cancelled += cancel_slot_buy_orders(ex, slot)
    log_event(
        "PORTFOLIO_LOSS_PAUSE",
        window=trigger["window"],
        net=trigger["net"],
        limit=trigger["limit"],
        pause_sec=pause_sec,
        cancelled_buy_orders=cancelled,
        live=live,
    )
    return cancelled


def refresh_loss_pause(portfolio: PortfolioState, now: float) -> bool:
    paused = portfolio.loss_pause_until > now
    if not paused and portfolio.loss_pause_until:
        portfolio.loss_pause_until = 0.0
    return paused


def monitor_stale_slot_grid(
    ex: Exchange,
    slot: SlotState,
    now: float,
    stale_buy_sec: float,
    live: bool,
) -> dict:
    """불필요한 미청산칸을 정리한다.

    - 거래소에 없는 주문/중복/격자 밖 주문은 reconcile 쪽에서 정리한다.
    - 오래된 미체결 BUY만 취소한다.
    - 보유칸과 대응 SELL은 유지/복구 대상으로 보고 손실 시장가 청산하지 않는다.
    """
    result = {"cancelled_stale_buys": 0, "held_without_sell": 0}
    if not slot.state:
        return result
    current = cycle_state_from_dict(slot.state)
    if live:
        current = reconcile_cycle_state_with_exchange(
            ex, current, cancel_duplicates=True, cancel_stale=True
        )
    if stale_buy_sec > 0 and current.buy_orders:
        last_fill_at = current.last_fill_at or current.started_at
        if now - last_fill_at >= stale_buy_sec and not current.held_buy_rungs and not current.sell_orders:
            for payload in list(current.buy_orders.values()):
                try:
                    if live:
                        ex.cancel_regular_order(current.symbol, int(payload["order_id"]))
                    result["cancelled_stale_buys"] += 1
                except Exception:
                    pass
            current.buy_orders = {}
            log_event(
                "PORTFOLIO_STALE_BUY_CLEAR",
                slot_id=slot.slot_id,
                symbol=current.symbol,
                stale_sec=now - last_fill_at,
                cancelled=result["cancelled_stale_buys"],
                live=live,
            )
    missing_sells = max(0, len(current.held_buy_rungs) - len(current.sell_orders))
    result["held_without_sell"] = missing_sells
    slot.state = cycle_state_to_dict(current)
    return result


def repair_orphan_position_held_rung(ex: Exchange, state) -> bool:
    """포지션은 있는데 상태/SELL이 사라진 경우 보유 rung을 복원한다."""
    if state.held_buy_rungs or state.sell_orders:
        return False
    pos = ex.get_position(state.symbol)
    if not pos or abs(float(pos.get("amount", 0.0) or 0.0)) <= 0:
        return False
    entry = float(pos.get("entry_price", 0.0) or 0.0)
    buy_levels = list(enumerate(state.levels[:-1]))
    if not buy_levels:
        return False
    rung, _level = min(buy_levels, key=lambda item: abs(float(item[1]) - entry))
    state.held_buy_rungs = [int(rung)]
    state.buy_orders.pop(str(rung), None)
    log_event(
        "PORTFOLIO_ORPHAN_POSITION_REPAIRED",
        symbol=state.symbol,
        entry_price=entry,
        restored_buy_rung=int(rung),
        amount=float(pos.get("amount", 0.0) or 0.0),
    )
    return True


def start_slot(
    ex: Exchange,
    symbol: str,
    wallet_balance: float,
    width_pct: float,
    grid_count: int,
    leverage: int,
    capital_ratio: float,
):
    configure_symbol(ex, symbol, leverage)
    state = make_cycle_state(
        ex,
        symbol,
        ex.get_mark_price(symbol),
        wallet_balance,
        width_pct,
        grid_count,
        leverage,
        capital_ratio,
    )
    state.last_fill_at = state.started_at
    return state


def maybe_rotate_slot(
    ex: Exchange,
    slot: SlotState,
    candidates: list[dict],
    wallet_balance: float,
    leverage: int,
    min_switch_interval_sec: float,
    switch_score_delta: float,
    idle_switch_sec: float,
    live: bool,
    taken_symbols: set[str] | None = None,
) -> None:
    """taken_symbols: 다른 슬롯이 이미 쓰는 심볼. 후보에서 제외한다.

    [2026-08-21 실사고] 슬롯마다 이 함수를 따로 부르는데 후보 목록이 같아서
    두 슬롯이 동시에 같은 심볼(BEATUSDT)로 교체됐다. 결과:
      - 같은 칸을 두 슬롯이 각자 매수 -> 의도의 2배 재고
      - 한 슬롯이 교체하며 flatten_position 을 부르면 다른 슬롯 포지션까지 청산
      - 40분 만에 -2.91 USDT, 손실컷(7%) 발동으로 봇 정지
    """
    if not slot.state:
        return
    if taken_symbols:
        candidates = [c for c in candidates
                      if str(c.get("symbol")) not in taken_symbols]
    current = cycle_state_from_dict(slot.state)
    now = time.time()
    mark_price = ex.get_mark_price(current.symbol)
    in_range = GridState(current.levels, set(current.held_buy_rungs)).in_range(mark_price)
    current_row = next(
        (
            c for c in candidates
            if c["symbol"] == current.symbol
            and c["strategy_id"] == (slot.strategy_id or f"{int(slot.width_pct)}x{slot.grid_count}")
        ),
        None,
    )
    current_score = current_row["profile_score"] if current_row else slot.last_score
    best = next(
        (
            c for c in candidates
            if c["symbol"] != current.symbol or c["strategy_id"] != slot.strategy_id
        ),
        None,
    )
    if not in_range:
        expanded = expand_range_cycle(ex, current, mark_price)
        slot.state = cycle_state_to_dict(expanded)
        slot.last_score = current_score
        log_event(
            "PORTFOLIO_RANGE_EXPAND",
            slot_id=slot.slot_id,
            symbol=current.symbol,
            strategy_id=slot.strategy_id,
            mark_price=mark_price,
        )
        return

    should_switch = False
    if best and now - slot.last_switch_at >= min_switch_interval_sec:
        should_switch = best["profile_score"] >= current_score + switch_score_delta
    last_fill_at = current.last_fill_at or current.started_at
    idle_sec = max(0.0, now - last_fill_at)
    idle_orders_only = bool(current.buy_orders) and not current.sell_orders and not current.held_buy_rungs
    if best and idle_orders_only and idle_sec >= idle_switch_sec:
        should_switch = best["profile_score"] >= current_score + switch_score_delta
        if should_switch:
            log_event(
                "PORTFOLIO_IDLE_SWITCH_READY",
                slot_id=slot.slot_id,
                symbol=current.symbol,
                idle_sec=idle_sec,
                current_score=current_score,
                best_symbol=best["symbol"],
                best_strategy=best["strategy_id"],
                best_score=best["profile_score"],
            )
    if not should_switch or not best:
        slot.last_score = current_score
        return

    # Cleanly wind down current slot before reassigning the budget.
    for payload in list(current.buy_orders.values()):
        ex.cancel_regular_order(current.symbol, int(payload["order_id"]))
    for payload in list(current.sell_orders.values()):
        ex.cancel_regular_order(current.symbol, int(payload["order_id"]))
    if live:
        flatten_position(ex, current.symbol)
    new_state = start_slot(
        ex,
        best["symbol"],
        wallet_balance,
        best["profile_width_pct"],
        best["profile_grid_count"],
        leverage,
        slot.capital_ratio,
    )
    slot.state = cycle_state_to_dict(new_state)
    slot.strategy_id = best["strategy_id"]
    slot.width_pct = best["profile_width_pct"]
    slot.grid_count = best["profile_grid_count"]
    slot.last_score = best["profile_score"]
    slot.last_switch_at = now
    log_event(
        "PORTFOLIO_SWITCH",
        slot_id=slot.slot_id,
        old_symbol=current.symbol,
        new_symbol=best["symbol"],
        old_score=current_score,
        new_score=best["profile_score"],
        new_strategy=best["strategy_id"],
        reason="idle" if idle_orders_only and idle_sec >= idle_switch_sec else "score",
        live=live,
    )


def main() -> int:
    ap = argparse.ArgumentParser(description="E3 portfolio grid bot")
    ap.add_argument("--slots", type=int, default=2)
    ap.add_argument("--width-pct", type=float, default=10.0)
    ap.add_argument("--grid-count", type=int, default=5)
    ap.add_argument("--strategy-mode", choices=["fixed", "fast"], default="fast")
    ap.add_argument("--leverage", type=int, default=2)
    ap.add_argument("--capital-usage", type=float, default=1.0)
    ap.add_argument("--candidate-limit", type=int, default=100)
    ap.add_argument("--top-candidates", type=int, default=12)
    ap.add_argument("--min-width-pct", type=float, default=4.0)
    ap.add_argument("--min-mean-abs-1m-pct", type=float, default=0.08)
    ap.add_argument("--max-spread-pct", type=float, default=0.12)
    ap.add_argument("--max-abs-ret-24h-pct", type=float, default=15.0)
    ap.add_argument("--max-abs-ret-60-pct", type=float, default=12.0)
    ap.add_argument("--max-abs-ret-180-pct", type=float, default=30.0)
    ap.add_argument("--poll-sec", type=float, default=5.0)
    ap.add_argument("--score-eval-sec", type=float, default=300.0)
    ap.add_argument("--min-switch-interval-sec", type=float, default=1200.0)
    ap.add_argument("--switch-score-delta", type=float, default=15.0)
    ap.add_argument("--idle-switch-sec", type=float, default=1800.0)
    ap.add_argument("--max-loss-pct", type=float, default=7.0)
    ap.add_argument("--loss-guard-30m-usdt", type=float, default=0.30,
                    help="최근 30분 거래소 순손익이 -N USDT 이하이면 신규 매수를 일시 중단")
    ap.add_argument("--loss-guard-60m-usdt", type=float, default=0.60,
                    help="최근 60분 거래소 순손익이 -N USDT 이하이면 신규 매수를 일시 중단")
    ap.add_argument("--loss-guard-pause-sec", type=float, default=3600.0,
                    help="rolling loss guard 발동 후 신규 매수 중단 시간")
    ap.add_argument("--stale-buy-clear-sec", type=float, default=1800.0,
                    help="보유/매도 없이 오래 남은 미체결 BUY 취소 시간. 0이면 비활성")
    ap.add_argument("--exclude-symbols", type=str, default="XRPUSDT")
    ap.add_argument("--minutes", type=float, default=0.0)
    ap.add_argument("--dry-run", action="store_true")
    # [2026-08-21] --resume 을 기본값으로 바꾼다.
    # 실사고: 재시작 때 --resume 을 빼먹으면 adopt_legacy_positions() 가
    # 계좌의 미체결 주문을 전부 취소한다. 그 주문들이 곧 격자다.
    # 하룻밤에 LEGACY_ADOPT 가 22번 찍혔고, 그때마다 격자가 통째로 지워졌다.
    # 매수 39건 대 매도 3건(완결률 7.7%)이 그 결과다.
    # 새로 시작하려면 --fresh-start 를 명시해야 한다.
    ap.add_argument("--resume", action="store_true", default=True,
                    help="(기본값) 기존 격자를 이어받는다")
    ap.add_argument("--fresh-start", dest="resume", action="store_false",
                    help="기존 주문을 취소하고 처음부터 시작한다")
    ap.add_argument("--no-telegram", action="store_true")
    ap.add_argument("--i-understand-grid-risks", action="store_true")
    # [2026-08-21] 브리핑의 손익을 '지금 배포 시점' 기준으로 다시 잡는다.
    # --resume 은 이전 기준을 이어받는데, 배포가 바뀌면 그 기준이 의미를 잃는다.
    ap.add_argument("--reset-baseline", action="store_true",
                    help="재개하더라도 자산 기준시점을 현재 잔고로 리셋한다")
    # 0 이 아니면 그 값을 기준 자산으로 못박는다(수동 지정).
    ap.add_argument("--baseline-balance", type=float, default=0.0)
    args = ap.parse_args()

    if not args.dry_run and not args.i_understand_grid_risks:
        print("실주문은 --i-understand-grid-risks 가 필요합니다.", flush=True)
        return 2
    if args.slots < 1 or args.slots > 3:
        print("slots 는 1~3만 허용합니다.", flush=True)
        return 2
    if args.leverage != 2:
        print("e3 포트폴리오는 격리 2배 고정입니다.", flush=True)
        return 2
    if args.capital_usage <= 0 or args.capital_usage > 1.0:
        print("capital-usage 는 0 초과 1.0 이하만 허용합니다.", flush=True)
        return 2
    if not acquire_bot_lock():
        return 2

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[
            logging.FileHandler(LOG_DIR / f"{VERSION}_run.log", encoding="utf-8"),
            logging.StreamHandler(sys.stdout),
        ],
    )

    cfg = Config()
    ex = Exchange(cfg)
    tg = None if args.no_telegram else Tg(cfg)
    exclude_symbols = parse_symbol_csv(args.exclude_symbols)
    profiles = build_strategy_profiles(args.strategy_mode)
    portfolio = load_portfolio_state() if args.resume else None
    wallet = ex.get_total_margin_balance()
    if wallet <= 0:
        print("총 자산이 0 이하입니다.", flush=True)
        return 2
    if portfolio is None:
        ratios = [1.0 / args.slots] * args.slots
        ratios[-1] += 1.0 - sum(ratios)
        portfolio = PortfolioState(
            wallet_balance_start=wallet,
            slots=[SlotState(slot_id=i + 1, capital_ratio=ratios[i]) for i in range(args.slots)],
        )
        portfolio.legacy_positions = adopt_legacy_positions(ex)
        if tg and tg.enabled and portfolio.legacy_positions:
            syms = ",".join(p["symbol"] for p in portfolio.legacy_positions)
            tg.send(f"[e3 legacy] 기존 포지션 {syms} 은 손익분기 이상 오면 즉시 정리합니다. 기존 예약은 취소했습니다.")
    else:
        portfolio = reconcile_portfolio_with_exchange(ex, portfolio)
        if not portfolio.legacy_positions:
            portfolio.legacy_positions = filter_legacy_positions(
                snapshot_legacy_positions(ex),
                active_slot_symbols(portfolio),
            )
        else:
            portfolio.legacy_positions = filter_legacy_positions(
                portfolio.legacy_positions,
                active_slot_symbols(portfolio),
            )
        if tg and tg.enabled:
            empty_slots = [str(slot.slot_id) for slot in portfolio.slots if slot.state is None]
            if empty_slots:
                tg.send(f"[e3 resume 대조] 거래소에 없는 stale 슬롯 {','.join(empty_slots)} 를 비웠습니다.")

    # [2026-08-21] 자산 기준시점 리셋.
    # 브리핑의 '시작 -> 현재' 는 배포 시점 기준이어야 읽는 의미가 있다.
    prev_baseline = portfolio.wallet_balance_start
    if args.baseline_balance > 0:
        portfolio.wallet_balance_start = args.baseline_balance
    elif args.reset_baseline or not args.resume:
        portfolio.wallet_balance_start = wallet
    deadline = time.time() + args.minutes * 60.0 if args.minutes > 0 else None
    # [2026-08-21] 매시 정각/30분 자동 브리핑용. 기동 직후 곧바로 한 번
    # 보내지 않도록 현재 슬롯으로 초기화한다.
    # [2026-08-21] 재시작을 텔레그램으로 알린다.
    # 프로세스가 조용히 재기동되면 사용자가 알 방법이 없고,
    # 브리핑 숫자가 갑자기 바뀐 이유도 설명되지 않는다.
    if tg and tg.enabled:
        _held = sum(len((sl.state or {}).get("held_buy_rungs") or [])
                    for sl in portfolio.slots)
        _syms = ", ".join((sl.state or {}).get("symbol", "-")
                          for sl in portfolio.slots if sl.state) or "없음"
        _mode = "재개(--resume)" if args.resume else "신규 시작"
        _base = (f"기준자산 {prev_baseline:.4f} -> {portfolio.wallet_balance_start:.4f} (리셋)"
                 if abs(prev_baseline - portfolio.wallet_balance_start) > 1e-9
                 else f"기준자산 {portfolio.wallet_balance_start:.4f} (유지)")
        try:
            tg.menu()
        except Exception:
            pass
        tg.send(
            f"[e3 재시작] {time.strftime('%Y-%m-%d %H:%M:%S')}\n"
            f"{_mode} / 슬롯 {args.slots} / 레버리지 {args.leverage}x\n"
            f"현재 자산 {wallet:.4f} USDT\n"
            f"{_base}\n"
            f"슬롯 심볼: {_syms}\n"
            f"이어받은 보유 칸 {_held}개\n"
            f"손실컷 {args.max_loss_pct}% / 자본사용 {args.capital_usage}"
        )

    _lt0 = time.localtime()
    last_brief_slot = (_lt0.tm_hour, 0 if _lt0.tm_min < 30 else 30)
    next_eval = 0.0
    if tg and tg.enabled:
        tg.poll()
        tg.menu()

    try:
        while True:
            wallet = ex.get_total_margin_balance()
            portfolio.legacy_positions, legacy_alerts = close_legacy_positions_on_profit(ex, portfolio.legacy_positions)
            if tg and tg.enabled:
                for alert in legacy_alerts:
                    tg.send(
                        f"[e3 legacy 정리] {alert['symbol']} {alert['side']} "
                        f"mark={alert['mark_price']:.6g} trigger={alert['trigger_price']:.6g}"
                    )
            if wallet <= portfolio.wallet_balance_start * (1.0 - args.max_loss_pct / 100.0):
                for slot in portfolio.slots:
                    if not slot.state:
                        continue
                    current = cycle_state_from_dict(slot.state)
                    handle_global_stop(
                        ex, current, current.symbol, wallet, args.max_loss_pct, live=not args.dry_run
                    )
                save_portfolio_state(portfolio)
                return 0

            now_ts = time.time()
            loss_paused = refresh_loss_pause(portfolio, now_ts)
            max_guard_hours = max(
                1.0,
                args.loss_guard_30m_usdt > 0 and 0.5 or 0.0,
                args.loss_guard_60m_usdt > 0 and 1.0 or 0.0,
            )
            if args.loss_guard_pause_sec > 0 and (args.loss_guard_30m_usdt > 0 or args.loss_guard_60m_usdt > 0):
                events = fetch_recent_income_events(ex, max_guard_hours)
                trigger = evaluate_loss_guard(
                    events,
                    now_ts,
                    args.loss_guard_30m_usdt,
                    args.loss_guard_60m_usdt,
                )
                if trigger and now_ts - portfolio.loss_guard_last_trigger_at >= 60.0:
                    cancelled = apply_loss_pause(
                        ex,
                        portfolio,
                        trigger,
                        args.loss_guard_pause_sec,
                        now_ts,
                        live=not args.dry_run,
                    )
                    loss_paused = True
                    if tg and tg.enabled:
                        tg.send(
                            f"[e3 손실 리밋] {trigger['window']} 순손익 {trigger['net']:+.4f} USDT "
                            f"(한도 {trigger['limit']:+.4f})\n"
                            f"신규 매수 {args.loss_guard_pause_sec / 60:.0f}분 중단 / "
                            f"미체결 BUY {cancelled}개 취소\n"
                            f"보유칸 SELL은 유지합니다."
                        )

            if time.time() >= next_eval:
                active_profiles = filter_profiles_for_wallet(wallet, profiles)
                blocked_symbols = exclude_symbols | {p["symbol"] for p in portfolio.legacy_positions}
                if not loss_paused:
                    portfolio.candidate_snapshot = select_profile_candidates(
                        ex,
                        wallet,
                        args.leverage,
                        args.capital_usage / args.slots,
                        candidate_limit=args.candidate_limit,
                        top_n=args.top_candidates,
                        exclude_symbols=blocked_symbols,
                        min_width_pct=args.min_width_pct,
                        min_mean_abs_1m_pct=args.min_mean_abs_1m_pct,
                        max_spread_pct=args.max_spread_pct,
                        max_abs_ret_24h_pct=args.max_abs_ret_24h_pct,
                        max_abs_ret_60_pct=args.max_abs_ret_60_pct,
                        max_abs_ret_180_pct=args.max_abs_ret_180_pct,
                        profiles=active_profiles,
                    )
                # [2026-08-21] 이미 슬롯이 잡고 있는 심볼은 후보에서 뺀다.
                # pick_symbols 는 한 번의 선택 안에서만 중복을 막으므로,
                # 빈 슬롯이 다른 슬롯과 같은 심볼을 받는 경로가 열려 있었다.
                held_syms = {
                    str((sl.state or {}).get("symbol") or "")
                    for sl in portfolio.slots if sl.state
                }
                held_syms.discard("")
                free_pool = [c for c in portfolio.candidate_snapshot
                             if str(c.get("symbol")) not in held_syms]
                selected = pick_symbols(free_pool, args.slots)
                sel_i = 0
                for idx, slot in enumerate(portfolio.slots):
                    if loss_paused:
                        continue
                    if slot.state is None:
                        if sel_i >= len(selected):
                            continue
                        idx = sel_i
                        sel_i += 1
                        new_state = start_slot(
                            ex,
                            selected[idx]["symbol"],
                            wallet,
                            selected[idx]["profile_width_pct"],
                            selected[idx]["profile_grid_count"],
                            args.leverage,
                            slot.capital_ratio * args.capital_usage,
                        )
                        slot.state = cycle_state_to_dict(new_state)
                        slot.strategy_id = selected[idx]["strategy_id"]
                        slot.width_pct = selected[idx]["profile_width_pct"]
                        slot.grid_count = selected[idx]["profile_grid_count"]
                        slot.last_score = selected[idx]["profile_score"]
                        slot.last_switch_at = time.time()
                    else:
                        # 다른 슬롯이 이미 잡고 있는 심볼은 후보에서 뺀다.
                        # 안 그러면 두 슬롯이 같은 심볼로 몰려 재고가 2배가 되고
                        # 한쪽의 flatten 이 다른 쪽 포지션까지 닫는다.
                        others = {
                            str((o.state or {}).get("symbol") or "")
                            for o in portfolio.slots
                            if o is not slot and o.state
                        }
                        others.discard("")
                        maybe_rotate_slot(
                            ex,
                            slot,
                            portfolio.candidate_snapshot,
                            wallet,
                            args.leverage,
                            args.min_switch_interval_sec,
                            args.switch_score_delta,
                            args.idle_switch_sec,
                            live=not args.dry_run,
                            taken_symbols=others,
                        )
                next_eval = time.time() + args.score_eval_sec

            for slot in portfolio.slots:
                if not slot.state:
                    continue
                current = cycle_state_from_dict(slot.state)
                mark = ex.get_mark_price(current.symbol)
                if not args.dry_run:
                    current = reconcile_cycle_state_with_exchange(
                        ex, current, cancel_duplicates=False, cancel_stale=False
                    )
                    repair_orphan_position_held_rung(ex, current)
                grid = GridState(current.levels, set(current.held_buy_rungs))
                if not grid.in_range(mark):
                    # 보유칸이 없으면 회전/범위확장은 평가 사이클에 맡긴다.
                    # 보유칸이 있으면 범위 밖이어도 reduceOnly SELL 재등록은 계속해야 한다.
                    if not current.held_buy_rungs:
                        slot.state = cycle_state_to_dict(current)
                        continue
                if args.dry_run:
                    process_dry_fills(current, grid, mark)
                else:
                    process_live_fills(ex, current, grid)
                    # [2026-08-21 사용자요청] "수동 매매나 대기중인걸 취소할 수도 있다".
                    # 체결 반영 뒤 거래소의 실제 미체결로 슬롯 상태를 다시 맞춘다.
                    # 이게 없으면 사용자가 취소한 주문을 봇이 '살아 있다'고 믿어
                    # 그 칸을 영영 다시 채우지 않는다(격자에 구멍이 남는다).
                    #
                    # cancel_duplicates / cancel_stale 은 반드시 False 로 둔다.
                    # True 면 격자에 없는 주문을 취소하는데, 사용자가 같은 심볼에
                    # 직접 낸 수동 주문이 거기 해당해 임의로 취소해 버린다.
                    grid = GridState(current.levels, set(current.held_buy_rungs))
                if not loss_paused:
                    ensure_grid_orders(ex, current, grid, mark, live=not args.dry_run)
                else:
                    for sell_rung in grid.eligible_sell_rungs(mark):
                        key = str(sell_rung)
                        if key in current.sell_orders:
                            continue
                        price = current.levels[sell_rung]
                        if args.dry_run:
                            order_id = -(1000 + sell_rung)
                        else:
                            order_id = try_place_limit_order(
                                ex, current.symbol, "SHORT", current.qty_per_rung, price, reduce_only=True
                            )
                            if order_id is None:
                                continue
                        current.sell_orders[key] = {
                            "order_id": order_id,
                            "rung": sell_rung,
                            "price": price,
                            "quantity": current.qty_per_rung,
                        }
                        log_event("PLACE_SELL", symbol=current.symbol, rung=sell_rung, price=price, quantity=current.qty_per_rung, live=not args.dry_run)
                slot.state = cycle_state_to_dict(current)
                stale = monitor_stale_slot_grid(
                    ex,
                    slot,
                    time.time(),
                    args.stale_buy_clear_sec,
                    live=not args.dry_run,
                )
                if tg and tg.enabled and stale.get("cancelled_stale_buys"):
                    tg.send(
                        f"[e3 stale 정리] slot{slot.slot_id} "
                        f"오래된 미체결 BUY {stale['cancelled_stale_buys']}개 취소"
                    )

            save_portfolio_state(portfolio)

            # [2026-08-21] 매시 정각과 30분에 자동 브리핑. 버튼을 누르지 않아도
            # 상태가 남도록 한다. 차트까지 같이 보낸다.
            _lt = time.localtime()
            _slot_key = (_lt.tm_hour, 0 if _lt.tm_min < 30 else 30)
            if tg and tg.enabled and _slot_key != last_brief_slot:
                last_brief_slot = _slot_key
                try:
                    tg.send_photo(portfolio_chart(ex, portfolio),
                                  summarize_portfolio(portfolio, wallet))
                except Exception:
                    tg.send(summarize_portfolio(portfolio, wallet))

            if tg and tg.enabled:
                for _cq, action in tg.poll():
                    if action == "status":
                        tg.send(summarize_portfolio(portfolio, wallet))
                    elif action == "brief":
                        tg.send(summarize_portfolio(portfolio, wallet))
                    elif action == "gridview":
                        try:
                            tg.send_photo(
                                portfolio_chart(ex, portfolio),
                                summarize_portfolio(portfolio, wallet))
                        except Exception as exc:
                            tg.send(f"[e3] 격자 차트 생성 실패: {exc}")
            if deadline and time.time() >= deadline:
                return 0
            time.sleep(args.poll_sec)
    finally:
        release_bot_lock()


if __name__ == "__main__":
    raise SystemExit(main())
