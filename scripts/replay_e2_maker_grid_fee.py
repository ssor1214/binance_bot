"""e2를 maker 우선 체결 구조로 바꿨을 때의 1차 오프라인 비교.

목적:
- 현행 e2는 진입/청산을 대부분 시장가(taker)로 처리해 왕복 수수료 부담이 크다.
- 신호 자체를 바꾸지 않고, 진입을 maker 지정가로 기다리면 수수료 절감과 체결률 저하 중
  어느 쪽이 더 큰지 본다.

모델:
- e2의 정배열, EMA5/10/15 터치, 봉마감 진입, 라이브 가드는 유지한다.
- baseline은 봉마감가로 즉시 진입, entry/exit 모두 taker 수수료.
- maker_entry는 봉마감가보다 유리한 지정가를 걸고 wait_bars 안에 터치되면 진입한다.
- maker_entry_tp는 진입은 maker, BB/RR 익절도 maker, STOP은 taker로 계산한다.

주의:
- 1분봉 기반이라 실제 호가/큐 우선순위/부분체결은 알 수 없다.
- 체결된 거래만의 손익과, 미체결로 줄어든 거래량을 보는 1차 비교다.
"""
from __future__ import annotations

import json
import sys
import argparse
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from scripts.replay_e2_close_entry_b2 import (
    add_indicators,
    iter_symbol_paths,
    load_symbol_frame,
    passes_b2,
    passes_bb_width,
    passes_close_location,
    passes_gap,
)


OUT_PATH = ROOT / "archive" / "scratch_scripts" / "e2_maker_grid_fee.json"
TAKER_FEE = 0.000501
MAKER_FEE = 0.000200


@dataclass
class PendingSignal:
    side: str
    touched: int = 0
    done: int = 0
    since_idx: int = 0


@dataclass
class LimitOrder:
    side: str
    entries_count: int
    limit_price: float
    signal_idx: int
    expiry_idx: int
    stop_price: float
    tp_bb: float
    rr: float


@dataclass
class Position:
    symbol: str
    side: str
    entries: list[float] = field(default_factory=list)
    entry_idx: int = 0
    stop_price: float = 0.0
    tp_bb: float = 0.0
    tp_rr: float = 0.0

    @property
    def entry(self) -> float:
        return sum(self.entries) / len(self.entries)


def fee_rate(entry_liq: str, exit_liq: str) -> float:
    return (MAKER_FEE if entry_liq == "maker" else TAKER_FEE) + (
        MAKER_FEE if exit_liq == "maker" else TAKER_FEE
    )


def net_pct(side: str, entry: float, exit_price: float, entry_liq: str, exit_liq: str) -> float:
    gross = (exit_price / entry - 1.0) if side == "LONG" else (entry / exit_price - 1.0)
    return (gross - fee_rate(entry_liq, exit_liq)) * 100.0


def limit_price_for(side: str, close: float, improve_bps: float) -> float:
    offset = improve_bps / 10000.0
    return close * (1.0 - offset) if side == "LONG" else close * (1.0 + offset)


def touched_limit(row, side: str, price: float) -> bool:
    return float(row["low"]) <= price if side == "LONG" else float(row["high"]) >= price


def replay_symbol(
    symbol: str,
    df: pd.DataFrame,
    mode: str,
    improve_bps: float,
    wait_bars: int,
    min_risk_pct: float = 0.35,
    slope_floor_pct: float = 0.0,
    tranches: int = 3,
    rr: float = 2.0,
    stop_ema: int = 25,
) -> tuple[list[dict], dict]:
    rows = add_indicators(df, stop_ema=stop_ema)
    pending: PendingSignal | None = None
    order: LimitOrder | None = None
    pos: Position | None = None
    trades: list[dict] = []
    stats = {"signals": 0, "limit_orders": 0, "filled_orders": 0, "expired_orders": 0}

    for i in range(30, len(rows)):
        row = rows.iloc[i]
        if pd.isna(row["bb_u"]) or pd.isna(row["e25"]):
            continue

        if order is not None:
            if i > order.expiry_idx:
                stats["expired_orders"] += 1
                order = None
            elif touched_limit(row, order.side, order.limit_price):
                risk = abs(order.limit_price - order.stop_price) / order.limit_price
                pos = Position(
                    symbol=symbol,
                    side=order.side,
                    entries=[order.limit_price] * order.entries_count,
                    entry_idx=i,
                    stop_price=order.stop_price,
                    tp_bb=order.tp_bb,
                    tp_rr=(order.limit_price * (1.0 + order.rr * risk)
                           if order.side == "LONG"
                           else order.limit_price * (1.0 - order.rr * risk)),
                )
                stats["filled_orders"] += 1
                order = None
                pending = None

        if pos is not None:
            exit_reason = None
            exit_price = None
            if pos.side == "LONG":
                if row["low"] <= pos.stop_price:
                    exit_reason, exit_price = "STOP_EMA25", pos.stop_price
                elif pos.tp_bb and row["high"] >= pos.tp_bb:
                    exit_reason, exit_price = "BB", pos.tp_bb
                elif pos.tp_rr and row["high"] >= pos.tp_rr:
                    exit_reason, exit_price = "RR", pos.tp_rr
            else:
                if row["high"] >= pos.stop_price:
                    exit_reason, exit_price = "STOP_EMA25", pos.stop_price
                elif pos.tp_bb and row["low"] <= pos.tp_bb:
                    exit_reason, exit_price = "BB", pos.tp_bb
                elif pos.tp_rr and row["low"] <= pos.tp_rr:
                    exit_reason, exit_price = "RR", pos.tp_rr
            if exit_reason:
                exit_liq = "maker" if mode == "maker_entry_tp" and exit_reason in ("BB", "RR") else "taker"
                trades.append({
                    "symbol": symbol,
                    "side": pos.side,
                    "legs": len(pos.entries),
                    "exit_reason": exit_reason,
                    "hold_sec": (i - pos.entry_idx) * 60.0,
                    "net_pct": net_pct(pos.side, pos.entry, float(exit_price), "maker" if mode != "baseline" else "taker", exit_liq),
                })
                pos = None
                pending = None

        if pos is not None or order is not None:
            continue

        e5, e10, e15, e25 = (float(row["e5"]), float(row["e10"]), float(row["e15"]), float(row["e25"]))
        is_long = e5 > e10 > e15 > e25
        is_short = e5 < e10 < e15 < e25
        if not (is_long or is_short):
            pending = None
            continue
        side = "LONG" if is_long else "SHORT"
        close = float(row["close"])
        if not passes_b2(row, side, close, slope_floor_pct):
            pending = None
            continue
        if not passes_gap(row, close, 0.0):
            pending = None
            continue
        if not passes_close_location(row, side, 0.0):
            pending = None
            continue
        if not passes_bb_width(row, 0.0):
            pending = None
            continue

        if pending is None:
            pending = PendingSignal(side=side, since_idx=i)
        if pending.side != side or i - pending.since_idx > 60:
            pending = None
            continue

        targets = [e5, e10, e15][:tranches]
        lo, hi = float(row["low"]), float(row["high"])
        while pending.touched < len(targets):
            tgt = targets[pending.touched]
            if not ((lo <= tgt) if side == "LONG" else (hi >= tgt)):
                break
            pending.touched += 1
        if pending.touched == pending.done:
            continue

        entry = close if mode == "baseline" else limit_price_for(side, close, improve_bps)
        stop = e25
        tp_bb = float(row["bb_u"] if side == "LONG" else row["bb_l"])
        if (side == "LONG" and entry <= stop) or (side == "SHORT" and entry >= stop):
            pending = None
            continue
        if tp_bb and ((entry >= tp_bb) if side == "LONG" else (entry <= tp_bb)):
            pending = None
            continue
        risk = abs(entry - stop) / entry
        if risk * 100.0 < min_risk_pct:
            pending = None
            continue

        stats["signals"] += 1
        entries_count = pending.touched - pending.done
        if mode == "baseline":
            pos = Position(
                symbol=symbol,
                side=side,
                entries=[entry] * entries_count,
                entry_idx=i,
                stop_price=stop,
                tp_bb=tp_bb,
                tp_rr=(entry * (1.0 + rr * risk) if side == "LONG" else entry * (1.0 - rr * risk)),
            )
            pending.done = pending.touched
        else:
            order = LimitOrder(
                side=side,
                entries_count=entries_count,
                limit_price=entry,
                signal_idx=i,
                expiry_idx=i + wait_bars,
                stop_price=stop,
                tp_bb=tp_bb,
                rr=rr,
            )
            stats["limit_orders"] += 1
            pending.done = pending.touched
    return trades, stats


def summarize(trades: list[dict], stats: dict, base_trades: int | None = None) -> dict:
    n = len(trades)
    total = sum(t["net_pct"] for t in trades)
    wins = sum(1 for t in trades if t["net_pct"] > 0)
    reasons = Counter(t["exit_reason"] for t in trades)
    holds = pd.Series([t["hold_sec"] for t in trades]) if trades else pd.Series(dtype=float)
    return {
        "trades": n,
        "trade_fill_vs_base_pct": n / base_trades * 100.0 if base_trades else 100.0,
        "avg_pct": total / n if n else 0.0,
        "sum_pct": total,
        "win_rate": wins / n * 100.0 if n else 0.0,
        "median_hold_sec": float(holds.median()) if n else 0.0,
        "signals": stats.get("signals", 0),
        "limit_orders": stats.get("limit_orders", 0),
        "filled_orders": stats.get("filled_orders", 0),
        "expired_orders": stats.get("expired_orders", 0),
        "limit_fill_rate_pct": stats.get("filled_orders", 0) / max(stats.get("limit_orders", 0), 1) * 100.0,
        "exit_reason_counts": dict(reasons),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--max-symbols", type=int, default=20)
    args = parser.parse_args()
    variants = [
        ("baseline", "baseline", 0.0, 0),
        ("maker_entry_0bps_wait1", "maker_entry", 0.0, 1),
        ("maker_entry_1bps_wait1", "maker_entry", 1.0, 1),
        ("maker_entry_2bps_wait1", "maker_entry", 2.0, 1),
        ("maker_entry_1bps_wait3", "maker_entry", 1.0, 3),
        ("maker_entry_tp_0bps_wait1", "maker_entry_tp", 0.0, 1),
        ("maker_entry_tp_1bps_wait1", "maker_entry_tp", 1.0, 1),
        ("maker_entry_tp_1bps_wait3", "maker_entry_tp", 1.0, 3),
    ]
    symbol_paths = iter_symbol_paths()[: args.max_symbols if args.max_symbols > 0 else None]
    print(f"symbols={len(symbol_paths)}")
    trades_by_variant: dict[str, list[dict]] = {name: [] for name, *_ in variants}
    stats_by_variant: dict[str, dict[str, int]] = {name: defaultdict(int) for name, *_ in variants}
    for symbol, paths in symbol_paths:
        df = load_symbol_frame(paths)
        for name, mode, improve_bps, wait_bars in variants:
            ts, st = replay_symbol(symbol, df, mode=mode, improve_bps=improve_bps, wait_bars=wait_bars)
            trades_by_variant[name].extend(ts)
            for k, v in st.items():
                stats_by_variant[name][k] += int(v)

    all_results: dict[str, dict] = {}
    baseline_trades = len(trades_by_variant["baseline"])
    for name, *_ in variants:
        trades = trades_by_variant[name]
        merged_stats = stats_by_variant[name]
        res = summarize(trades, merged_stats, baseline_trades)
        all_results[name] = res
        print(
            f"{name:28s} trades={res['trades']:6d} fill_vs_base={res['trade_fill_vs_base_pct']:5.1f}% "
            f"fill={res['limit_fill_rate_pct']:5.1f}% avg={res['avg_pct']:+.4f}% "
            f"sum={res['sum_pct']:+.1f}% win={res['win_rate']:5.1f}% hold={res['median_hold_sec']:4.0f}s "
            f"reasons={res['exit_reason_counts']}"
        )
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(all_results, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"saved={OUT_PATH}")


if __name__ == "__main__":
    main()
