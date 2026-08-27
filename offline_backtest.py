"""Offline-only 1 minute kline backtester.

This module deliberately has no Binance, HTTP, WebSocket, dotenv, or trading-code
imports.  It reads a single local JSON kline snapshot and writes local reports.
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import socket
import sys
import http.client
import urllib.request
from collections import defaultdict
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

OFFLINE_ERROR = "Offline backtest: network access is disabled"


def disable_network() -> None:
    """Fail closed for direct sockets and already-loaded HTTP clients.

    No network package is imported here.  Patching an already loaded requests
    module also makes accidental direct request entry points fail immediately.
    """
    def blocked(*_args: Any, **_kwargs: Any) -> None:
        raise RuntimeError(OFFLINE_ERROR)

    socket.socket = blocked  # type: ignore[assignment]
    socket.create_connection = blocked  # type: ignore[assignment]
    socket.getaddrinfo = blocked  # type: ignore[assignment]
    http.client.HTTPConnection.connect = blocked  # type: ignore[assignment]
    http.client.HTTPSConnection.connect = blocked  # type: ignore[assignment]
    urllib.request.urlopen = blocked  # type: ignore[assignment]
    requests_module = sys.modules.get("requests")
    if requests_module is not None:
        requests_module.request = blocked
        session = getattr(requests_module, "Session", None)
        if session is not None:
            session.request = blocked


@dataclass
class Candle:
    timestamp: int
    open: float
    high: float
    low: float
    close: float
    volume: float
    quote_volume: float
    taker_buy_volume: float


@dataclass
class Settings:
    starting_balance: float = 38.0
    leverage: float = 4.0
    max_positions: int = 3
    margin_fraction: float = 0.20
    long_margin_fraction_mult: float = 1.0
    short_margin_fraction_mult: float = 1.0
    fee_rate: float = 0.0004
    slippage_bps: float = 5.0
    funding_rate_8h: float = 0.0
    stop_roe_pct: float = 5.0
    short_stop_roe_pct: float = 5.0
    take_profit_roe_pct: float = 3.0
    hard_take_profit_roe_pct: float = 7.0
    trailing_drawdown_roe_pct: float = 1.0
    warmup: int = 60
    volume_ratio: float = 2.0
    candle_change_pct: float = 0.35
    taker_ratio: float = 0.55
    adx_threshold: float = 20.0
    min_avg_quote_volume: float = 150.0
    min_atr_vs_stop_ratio: float = 0.8
    max_atr_vs_stop_ratio: float = 4.0
    limit_entry_pullback_bps: float = 0.0
    trade_sides: str = "both"
    short_volume_ratio: float = 2.6
    short_candle_change_pct: float = 0.45
    short_taker_buy_ratio_max: float = 0.43
    short_adx_threshold: float = 24.0
    short_rsi_max: float = 45.0
    short_max_lower_wick_body_ratio: float = 0.0
    short_max_close_from_low_pct: float = 0.0
    long_max_upper_wick_body_ratio: float = 0.0
    long_max_close_from_high_pct: float = 0.0
    average_down: bool = False
    average_down_trigger_ratio: float = 0.5
    average_down_size_ratio: float = 0.5


@dataclass
class Position:
    symbol: str
    side: str
    entry_time: int
    entry_price: float
    quantity: float
    margin: float
    entry_fee: float
    peak_price: float
    trailing_armed: bool = False
    average_down_done: bool = False
    funding: float = 0.0
    max_adverse_roe: float = 0.0
    max_favorable_roe: float = 0.0


def _adverse_roe(pos: Position, candle: Candle, settings: Settings) -> float:
    adverse_price = candle.low if pos.side == "LONG" else candle.high
    return (adverse_price / pos.entry_price - 1) * (1 if pos.side == "LONG" else -1) * settings.leverage * 100


def _favorable_roe(pos: Position, candle: Candle, settings: Settings) -> float:
    favorable_price = candle.high if pos.side == "LONG" else candle.low
    return (favorable_price / pos.entry_price - 1) * (1 if pos.side == "LONG" else -1) * settings.leverage * 100


def _ema(values: list[float], period: int) -> float:
    result = values[0]
    alpha = 2.0 / (period + 1)
    for value in values[1:]:
        result += alpha * (value - result)
    return result


def _rsi(closes: list[float], period: int = 14) -> float:
    if len(closes) <= period:
        return 50.0
    changes = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    gains = [max(x, 0.0) for x in changes[-period:]]
    losses = [max(-x, 0.0) for x in changes[-period:]]
    avg_loss = sum(losses) / period
    return 100.0 if avg_loss == 0 else 100 - (100 / (1 + (sum(gains) / period) / avg_loss))


def _adx(candles: list[Candle], period: int = 14) -> float:
    if len(candles) <= period:
        return 0.0
    plus, minus, tr = [], [], []
    for prev, curr in zip(candles[-period - 1:-1], candles[-period:]):
        up, down = curr.high - prev.high, prev.low - curr.low
        plus.append(up if up > down and up > 0 else 0.0)
        minus.append(down if down > up and down > 0 else 0.0)
        tr.append(max(curr.high - curr.low, abs(curr.high - prev.close), abs(curr.low - prev.close)))
    atr = sum(tr) / period
    if atr == 0:
        return 0.0
    pdi, mdi = 100 * sum(plus) / period / atr, 100 * sum(minus) / period / atr
    return 0.0 if pdi + mdi == 0 else 100 * abs(pdi - mdi) / (pdi + mdi)


def _atr(candles: list[Candle], period: int = 14) -> float:
    if len(candles) <= period:
        return 0.0
    tr = []
    for prev, curr in zip(candles[-period - 1:-1], candles[-period:]):
        tr.append(max(curr.high - curr.low, abs(curr.high - prev.close), abs(curr.low - prev.close)))
    return sum(tr) / period if tr else 0.0


def _passes_whipsaw_filter(history: list[Candle], side: str, settings: Settings) -> bool:
    current = history[-1]
    if current.close <= 0:
        return False
    atr_pct = (_atr(history) / current.close) * 100
    stop_roe = settings.short_stop_roe_pct if side == "SHORT" and settings.short_stop_roe_pct > 0 else settings.stop_roe_pct
    stop_dist_pct = stop_roe / settings.leverage
    if settings.min_atr_vs_stop_ratio > 0 and atr_pct < stop_dist_pct * settings.min_atr_vs_stop_ratio:
        return False
    if settings.max_atr_vs_stop_ratio > 0 and atr_pct > stop_dist_pct * settings.max_atr_vs_stop_ratio:
        return False
    return True


def _passes_short_scalp_reversal_filter(candle: Candle, settings: Settings) -> bool:
    """Reject SHORT scalps that already bounced hard within the signal candle.

    This uses only the already-closed 1m candle.  A long lower wick or a close far
    above the low means sellers pushed down but buyers absorbed/reversed it; that
    is the exact whipsaw shape seen in the recent live SHORT losses.
    """
    body = abs(candle.close - candle.open)
    lower_wick = min(candle.open, candle.close) - candle.low
    if settings.short_max_lower_wick_body_ratio > 0:
        if body <= 0 or lower_wick / body > settings.short_max_lower_wick_body_ratio:
            return False
    if settings.short_max_close_from_low_pct > 0 and candle.low > 0:
        close_from_low_pct = (candle.close / candle.low - 1) * 100
        if close_from_low_pct > settings.short_max_close_from_low_pct:
            return False
    return True


def _passes_long_scalp_reversal_filter(candle: Candle, settings: Settings) -> bool:
    """Reject LONG scalps that already rejected hard within the signal candle.

    This uses only the already-closed 1m candle.  A long upper wick or a close far
    below the high means buyers pushed up but sellers absorbed/reversed it; that
    is the LONG-side analogue of the recent SHORT lower-wick whipsaw filter.
    """
    body = abs(candle.close - candle.open)
    upper_wick = candle.high - max(candle.open, candle.close)
    if settings.long_max_upper_wick_body_ratio > 0:
        if body <= 0 or upper_wick / body > settings.long_max_upper_wick_body_ratio:
            return False
    if settings.long_max_close_from_high_pct > 0 and candle.high > 0:
        close_from_high_pct = (candle.high / candle.close - 1) * 100
        if close_from_high_pct > settings.long_max_close_from_high_pct:
            return False
    return True


def signal(history: list[Candle], settings: Settings) -> str | None:
    """Uses only the passed, already-closed candles; never an unobserved candle."""
    if len(history) < settings.warmup:
        return None
    window = history[-max(settings.warmup, 80):]
    current = window[-1]
    closes = [c.close for c in window]
    fast, slow = _ema(closes[-20:], 20), _ema(closes[-50:], 50)
    macd = _ema(closes[-12:], 12) - _ema(closes[-26:], 26)
    macd_series = [_ema(closes[max(0, i - 25):i + 1], min(26, i + 1)) - _ema(closes[max(0, i - 11):i + 1], min(12, i + 1)) for i in range(len(closes) - 9, len(closes))]
    macd_signal = _ema(macd_series, len(macd_series))
    volume_ma = sum(c.volume for c in window[-20:]) / 20
    quote_volume_ma = sum(c.quote_volume for c in window[-20:]) / 20
    change = (current.close / current.open - 1) * 100 if current.open else 0.0
    taker = current.taker_buy_volume / current.volume if current.volume else 0.5
    adx = _adx(window)
    rsi = _rsi(closes)
    long_allowed = settings.trade_sides in ("both", "long-only")
    short_allowed = settings.trade_sides in ("both", "short-only")
    long_ok = (long_allowed and quote_volume_ma >= settings.min_avg_quote_volume and change >= settings.candle_change_pct and current.volume >= volume_ma * settings.volume_ratio
               and taker >= settings.taker_ratio and fast > slow and macd >= macd_signal
               and rsi >= 50 and adx >= settings.adx_threshold)
    short_ok = (short_allowed and quote_volume_ma >= settings.min_avg_quote_volume and change <= -settings.short_candle_change_pct
                and current.volume >= volume_ma * settings.short_volume_ratio and taker <= settings.short_taker_buy_ratio_max
                and fast < slow and macd <= macd_signal and rsi <= settings.short_rsi_max
                and adx >= settings.short_adx_threshold)
    if short_ok and not _passes_short_scalp_reversal_filter(current, settings):
        short_ok = False
    if long_ok and not _passes_long_scalp_reversal_filter(current, settings):
        long_ok = False
    side = "LONG" if long_ok else "SHORT" if short_ok else None
    if side and not _passes_whipsaw_filter(window, side, settings):
        return None
    return side


def load_data(path: Path) -> tuple[dict[str, list[Candle]], dict[str, Any]]:
    if path.name != "scratch_klines_v4.json":
        raise ValueError("Offline backtest accepts only scratch_klines_v4.json")
    raw = json.loads(path.read_text(encoding="utf-8"))
    data, issues = {}, {"duplicate_candles": {}, "missing_minutes": {}, "invalid_candles": {}, "time_misalignment": []}
    for symbol, rows in raw.items():
        seen, candles, duplicates, invalid = set(), [], 0, 0
        for row in rows:
            try:
                ts = int(row[0])
                if ts in seen:
                    duplicates += 1; continue
                seen.add(ts)
                c = Candle(ts, *map(float, (row[1], row[2], row[3], row[4], row[5], row[7], row[9])))
                if c.low <= 0 or c.high < c.low or not (c.low <= c.open <= c.high and c.low <= c.close <= c.high):
                    invalid += 1; continue
                candles.append(c)
            except (IndexError, TypeError, ValueError):
                invalid += 1
        candles.sort(key=lambda c: c.timestamp)
        gaps = sum(max(0, (b.timestamp - a.timestamp) // 60000 - 1) for a, b in zip(candles, candles[1:]))
        data[symbol] = candles
        if duplicates: issues["duplicate_candles"][symbol] = duplicates
        if invalid: issues["invalid_candles"][symbol] = invalid
        if gaps: issues["missing_minutes"][symbol] = gaps
    starts = {s: cs[0].timestamp for s, cs in data.items() if cs}
    if len(set(starts.values())) > 1: issues["time_misalignment"].append({"starts": starts})
    return data, issues


def _fill(price: float, side: str, entering: bool, bps: float) -> float:
    adverse_buy = (side == "LONG") == entering
    return price * (1 + (bps / 10000 if adverse_buy else -bps / 10000))


def _entry_limit_price(open_price: float, side: str, settings: Settings) -> float:
    if settings.limit_entry_pullback_bps <= 0:
        return open_price
    if side == "LONG":
        return open_price * (1 - settings.limit_entry_pullback_bps / 10000)
    return open_price * (1 + settings.limit_entry_pullback_bps / 10000)


def _entry_limit_filled(candle: Candle, side: str, limit_price: float) -> bool:
    if side == "LONG":
        return candle.low <= limit_price
    return candle.high >= limit_price


def exit_decision(pos: Position, candle: Candle, settings: Settings) -> tuple[float, str] | None:
    """Check levels known before this candle; ambiguous outcomes are adverse-first."""
    stop = pos.entry_price * (1 - settings.stop_roe_pct / 100 / settings.leverage if pos.side == "LONG" else 1 + settings.stop_roe_pct / 100 / settings.leverage)
    target = pos.entry_price * (1 + settings.hard_take_profit_roe_pct / 100 / settings.leverage if pos.side == "LONG" else 1 - settings.hard_take_profit_roe_pct / 100 / settings.leverage)
    stop_hit = candle.low <= stop if pos.side == "LONG" else candle.high >= stop
    target_hit = candle.high >= target if pos.side == "LONG" else candle.low <= target
    if stop_hit:
        return stop, "stop_loss"
    if pos.trailing_armed:
        trail = pos.peak_price * (1 - settings.trailing_drawdown_roe_pct / 100 / settings.leverage if pos.side == "LONG" else 1 + settings.trailing_drawdown_roe_pct / 100 / settings.leverage)
        trail_hit = candle.low <= trail if pos.side == "LONG" else candle.high >= trail
        if trail_hit:
            return trail, "trailing_stop"
    if target_hit:
        return target, "hard_take_profit"
    return None


def _average_down(pos: Position, candle: Candle, settings: Settings, balance: float) -> float:
    """Add once at a pre-known adverse level. Disabled unless explicitly opted in."""
    if not settings.average_down or pos.average_down_done:
        return balance
    trigger_roe = settings.stop_roe_pct * settings.average_down_trigger_ratio
    trigger = pos.entry_price * (1 - trigger_roe / 100 / settings.leverage if pos.side == "LONG" else 1 + trigger_roe / 100 / settings.leverage)
    hit = candle.low <= trigger if pos.side == "LONG" else candle.high >= trigger
    add_margin = min(pos.margin * settings.average_down_size_ratio, max(0.0, balance))
    if not hit or add_margin <= 0:
        return balance
    fill = _fill(trigger, pos.side, True, settings.slippage_bps)
    add_qty = add_margin * settings.leverage / fill
    fee = fill * add_qty * settings.fee_rate
    if add_margin + fee > balance:
        return balance
    old_notional = pos.entry_price * pos.quantity
    pos.quantity += add_qty
    pos.entry_price = (old_notional + fill * add_qty) / pos.quantity
    pos.margin += add_margin
    pos.entry_fee += fee
    pos.average_down_done = True
    pos.trailing_armed = False
    pos.peak_price = pos.entry_price
    return balance - add_margin - fee


def _close(pos: Position, price: float, timestamp: int, reason: str, settings: Settings, balance: float) -> tuple[dict[str, Any], float]:
    fill = _fill(price, pos.side, False, settings.slippage_bps)
    gross = (fill - pos.entry_price) * pos.quantity * (1 if pos.side == "LONG" else -1)
    exit_fee = fill * pos.quantity * settings.fee_rate
    total_fee = pos.entry_fee + exit_fee
    slippage = abs(pos.entry_price - (pos.entry_price / (1 + settings.slippage_bps / 10000 if pos.side == "LONG" else 1 - settings.slippage_bps / 10000))) * pos.quantity + abs(price - fill) * pos.quantity
    net = gross - exit_fee - pos.funding  # entry fee already deducted at entry
    pre_cost_gross = gross + slippage
    ledger = {"symbol": pos.symbol, "side": pos.side, "entry_time": pos.entry_time, "exit_time": timestamp,
              "entry_price": pos.entry_price, "exit_price": fill, "quantity": pos.quantity, "reason": reason,
              "gross_pnl": pre_cost_gross, "fee": total_fee, "slippage": slippage, "funding": pos.funding,
              "net_pnl": pre_cost_gross - total_fee - slippage - pos.funding, "holding_minutes": (timestamp - pos.entry_time) / 60000,
              "max_adverse_roe": pos.max_adverse_roe, "max_favorable_roe": pos.max_favorable_roe}
    return ledger, balance + pos.margin + net


def run_backtest(data: dict[str, list[Candle]], settings: Settings) -> dict[str, Any]:
    by_time = defaultdict(dict)
    for symbol, candles in data.items():
        for candle in candles: by_time[candle.timestamp][symbol] = candle
    histories = defaultdict(list); positions: dict[str, Position] = {}; pending: dict[str, str] = {}
    balance, ledger, curve = settings.starting_balance, [], []
    unfilled_entries = 0
    for timestamp in sorted(by_time):
        candles = by_time[timestamp]
        for symbol in sorted(candles):
            candle = candles[symbol]
            pos = positions.get(symbol)
            if pos:
                pos.max_adverse_roe = min(pos.max_adverse_roe, _adverse_roe(pos, candle, settings))
                pos.max_favorable_roe = max(pos.max_favorable_roe, _favorable_roe(pos, candle, settings))
                # Conservative: if stop and target are both reachable, stop wins.
                decision = exit_decision(pos, candle, settings)
                if decision:
                    price, reason = decision
                    item, balance = _close(pos, price, timestamp, reason, settings, balance); ledger.append(item); del positions[symbol]
                else:
                    balance = _average_down(pos, candle, settings, balance)
                    # The high/low becomes known only after this candle closes;
                    # it can arm/update a trail for a subsequent candle only.
                    favorable = candle.high if pos.side == "LONG" else candle.low
                    pos.peak_price = max(pos.peak_price, favorable) if pos.side == "LONG" else min(pos.peak_price, favorable)
                    roe = ((pos.peak_price / pos.entry_price - 1) * (1 if pos.side == "LONG" else -1) * settings.leverage * 100)
                    if roe >= settings.take_profit_roe_pct: pos.trailing_armed = True
                    if symbol in positions:
                        positions[symbol].funding += positions[symbol].entry_price * positions[symbol].quantity * settings.funding_rate_8h / 480
            if symbol in pending and symbol not in positions and len(positions) < settings.max_positions:
                side = pending.pop(symbol)
                limit_price = _entry_limit_price(candle.open, side, settings)
                if not _entry_limit_filled(candle, side, limit_price):
                    unfilled_entries += 1
                    histories[symbol].append(candle)
                    continue
                entry = _fill(limit_price, side, True, settings.slippage_bps)
                side_margin_mult = settings.long_margin_fraction_mult if side == "LONG" else settings.short_margin_fraction_mult
                margin = balance * settings.margin_fraction * side_margin_mult
                if margin > 0:
                    qty = margin * settings.leverage / entry; fee = entry * qty * settings.fee_rate
                    balance -= margin + fee
                    pos = Position(symbol, side, timestamp, entry, qty, margin, fee, entry)
                    pos.max_adverse_roe = min(pos.max_adverse_roe, _adverse_roe(pos, candle, settings))
                    pos.max_favorable_roe = max(pos.max_favorable_roe, _favorable_roe(pos, candle, settings))
                    positions[symbol] = pos
            histories[symbol].append(candle)
            if symbol not in positions and symbol not in pending and len(positions) + len(pending) < settings.max_positions:
                side = signal(histories[symbol], settings)
                if side: pending[symbol] = side
        equity = balance + sum(p.margin + (candles.get(s, histories[s][-1]).close - p.entry_price) * p.quantity * (1 if p.side == "LONG" else -1) for s, p in positions.items())
        curve.append({"timestamp": timestamp, "equity": equity})
    for symbol, pos in list(positions.items()):
        item, balance = _close(pos, histories[symbol][-1].close, histories[symbol][-1].timestamp, "end_of_data", settings, balance); ledger.append(item)
    return {"ledger": ledger, "final_balance": balance, "equity_curve": curve, "unfilled_entries": unfilled_entries}


def metrics(result: dict[str, Any], validation_start: int) -> dict[str, Any]:
    ledger = result["ledger"]
    def summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
        wins = [r for r in rows if r["net_pnl"] > 0]; losses = [r for r in rows if r["net_pnl"] <= 0]
        gross_profit, gross_loss = sum(r["net_pnl"] for r in wins), abs(sum(r["net_pnl"] for r in losses))
        early_losses = [r for r in losses if r["holding_minutes"] <= 3]
        adverse_losses = [r for r in losses if r.get("max_adverse_roe", 0.0) <= -3.0]
        streak = current = 0
        for r in rows:
            current = current + 1 if r["net_pnl"] <= 0 else 0; streak = max(streak, current)
        return {"trades": len(rows), "wins": len(wins), "losses": len(losses), "win_rate": len(wins) / len(rows) if rows else 0,
                "gross_pnl": sum(r["gross_pnl"] for r in rows), "fee": sum(r["fee"] for r in rows), "slippage": sum(r["slippage"] for r in rows), "funding": sum(r["funding"] for r in rows), "net_pnl": sum(r["net_pnl"] for r in rows),
                "average_win": sum(r["net_pnl"] for r in wins) / len(wins) if wins else 0, "average_loss": sum(r["net_pnl"] for r in losses) / len(losses) if losses else 0,
                "average_max_adverse_roe": sum(r.get("max_adverse_roe", 0.0) for r in rows) / len(rows) if rows else 0,
                "early_loss_rate": len(early_losses) / len(rows) if rows else 0,
                "adverse_loss_rate": len(adverse_losses) / len(rows) if rows else 0,
                "profit_factor": gross_profit / gross_loss if gross_loss else None, "expectancy": sum(r["net_pnl"] for r in rows) / len(rows) if rows else 0,
                "max_consecutive_losses": streak, "average_holding_minutes": sum(r["holding_minutes"] for r in rows) / len(rows) if rows else 0}
    peak, max_dd = -math.inf, 0.0
    for point in result["equity_curve"]:
        peak = max(peak, point["equity"]); max_dd = max(max_dd, peak - point["equity"])
    groups = {"by_symbol": {}, "by_side": {}, "by_hour_utc": {}}
    for key, selector in (("by_symbol", lambda r: r["symbol"]), ("by_side", lambda r: r["side"]), ("by_hour_utc", lambda r: str(datetime.fromtimestamp(r["entry_time"] / 1000, timezone.utc).hour))):
        bucket = defaultdict(list)
        for row in ledger: bucket[selector(row)].append(row)
        groups[key] = {name: summary(rows) for name, rows in bucket.items()}
    return {"all": summary(ledger), "exploration_first_3_days": summary([r for r in ledger if r["entry_time"] < validation_start]), "validation_last_2_days": summary([r for r in ledger if r["entry_time"] >= validation_start]), "max_drawdown_usdt": max_dd, "max_drawdown_pct_of_start": max_dd / result["equity_curve"][0]["equity"] if result["equity_curve"] else 0.0, "unfilled_entries": result.get("unfilled_entries", 0), **groups}


def write_reports(output: Path, payload: dict[str, Any]) -> tuple[Path, Path, Path]:
    output.mkdir(parents=True, exist_ok=True); stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    json_path, md_path, csv_path = output / f"{stamp}.json", output / f"{stamp}.md", output / f"{stamp}_ledger.csv"
    json_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    with csv_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(payload["ledger"][0]) if payload["ledger"] else ["symbol", "side"]); writer.writeheader(); writer.writerows(payload["ledger"])
    m = payload["metrics"]
    md_path.write_text("# Offline backtest report\n\n> **Warning:** 1-minute OHLCV has no tick order. Same-candle protective outcomes are resolved adverse-first.\n\n"
                       "## OFFLINE MODE — no API/network access\n\n"
                       f"- Source: `{payload['data_file']}` (local only)\n- Final balance: {payload['result']['final_balance']:.4f} USDT\n- 38→100 reached: {any(x['equity'] >= 100 for x in payload['result']['equity_curve'])}\n- No optimization was performed; first 3 days are exploration and final 2 days fixed validation. Validation may use older candles only for indicator warm-up; no validation outcome changes parameters.\n\n"
                       f"## Assumptions\n\n```json\n{json.dumps(payload['settings'], indent=2)}\n```\n\n## Metrics\n\n```json\n{json.dumps(m, indent=2)}\n```\n\n## Data quality\n\n```json\n{json.dumps(payload['data_quality'], indent=2)}\n```\n\n"
                       "## Limitations\n\n- One-minute OHLCV does not reveal tick order, gaps, queue position, or partial fills. Newly observed highs/lows affect trailing only from the next candle.\n- Five days is a small, regime-specific sample; correlated symbols are not independent samples.\n- Fees and slippage are assumptions. Funding defaults to zero. Liquidations, exchange filters, latency, outages, and rejected orders are not modeled.\n- The 3/2-day split blocks tuning inside this runner, but repeated manual parameter choices can still overfit the holdout.\n", encoding="utf-8")
    return json_path, md_path, csv_path


def main() -> None:
    parser = argparse.ArgumentParser(description="Offline-only local kline backtest")
    parser.add_argument("--data", required=True); parser.add_argument("--output", default="backtest_results")
    parser.add_argument("--fee-rate", type=float, default=0.0004); parser.add_argument("--slippage-bps", type=float, default=5.0)
    parser.add_argument("--long-margin-fraction-mult", type=float, default=1.0)
    parser.add_argument("--short-margin-fraction-mult", type=float, default=1.0)
    parser.add_argument("--min-atr-vs-stop-ratio", type=float, default=0.8)
    parser.add_argument("--max-atr-vs-stop-ratio", type=float, default=4.0)
    parser.add_argument("--short-stop-roe-pct", type=float, default=5.0)
    # [2026-08-19] 손절폭 검증용. 기존엔 Settings 기본값 5.0으로 고정돼 CLI로 못 바꿨다.
    # 라이브 .env는 STOP_LOSS_PCT=6.0이라 기본값과 어긋나 있었다.
    parser.add_argument("--stop-roe-pct", type=float, default=5.0)
    # [2026-08-19] 보유시간 연장 검증용. 무장선/트레일폭/하드캡 모두 CLI 노출이 없었다.
    parser.add_argument("--take-profit-roe-pct", type=float, default=3.0)
    parser.add_argument("--hard-take-profit-roe-pct", type=float, default=7.0)
    parser.add_argument("--trailing-drawdown-roe-pct", type=float, default=1.0)
    parser.add_argument("--funding-rate-8h", type=float, default=0.0); parser.add_argument("--average-down", action="store_true")
    parser.add_argument("--trade-sides", choices=("both", "long-only", "short-only"), default="both")
    parser.add_argument("--short-volume-ratio", type=float, default=2.6)
    parser.add_argument("--limit-entry-pullback-bps", type=float, default=0.0)
    parser.add_argument("--short-candle-change-pct", type=float, default=0.45)
    parser.add_argument("--short-taker-buy-ratio-max", type=float, default=0.43)
    parser.add_argument("--short-adx-threshold", type=float, default=24.0)
    parser.add_argument("--short-rsi-max", type=float, default=45.0)
    parser.add_argument("--short-max-lower-wick-body-ratio", type=float, default=0.0)
    parser.add_argument("--short-max-close-from-low-pct", type=float, default=0.0)
    parser.add_argument("--long-max-upper-wick-body-ratio", type=float, default=0.0)
    parser.add_argument("--long-max-close-from-high-pct", type=float, default=0.0)
    args = parser.parse_args(); disable_network()
    # Windows legacy consoles otherwise cannot render the mandated em dash.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    print("OFFLINE MODE — no API/network access")
    settings = Settings(stop_roe_pct=args.stop_roe_pct,
                        take_profit_roe_pct=args.take_profit_roe_pct,
                        hard_take_profit_roe_pct=args.hard_take_profit_roe_pct,
                        trailing_drawdown_roe_pct=args.trailing_drawdown_roe_pct, fee_rate=args.fee_rate, slippage_bps=args.slippage_bps, funding_rate_8h=args.funding_rate_8h,
                        long_margin_fraction_mult=args.long_margin_fraction_mult,
                        short_margin_fraction_mult=args.short_margin_fraction_mult,
                        min_atr_vs_stop_ratio=args.min_atr_vs_stop_ratio,
                        max_atr_vs_stop_ratio=args.max_atr_vs_stop_ratio,
                        short_stop_roe_pct=args.short_stop_roe_pct,
                        trade_sides=args.trade_sides, short_volume_ratio=args.short_volume_ratio,
                        limit_entry_pullback_bps=args.limit_entry_pullback_bps,
                        short_candle_change_pct=args.short_candle_change_pct,
                        short_taker_buy_ratio_max=args.short_taker_buy_ratio_max,
                        short_max_lower_wick_body_ratio=args.short_max_lower_wick_body_ratio,
                        short_max_close_from_low_pct=args.short_max_close_from_low_pct,
                        long_max_upper_wick_body_ratio=args.long_max_upper_wick_body_ratio,
                        long_max_close_from_high_pct=args.long_max_close_from_high_pct,
                        short_adx_threshold=args.short_adx_threshold, short_rsi_max=args.short_rsi_max,
                        average_down=args.average_down)
    data, quality = load_data(Path(args.data)); result = run_backtest(data, settings)
    first = min(c.timestamp for candles in data.values() for c in candles); payload = {"data_file": str(Path(args.data)), "settings": asdict(settings), "data_quality": quality, "result": result, "ledger": result["ledger"], "metrics": metrics(result, first + 3 * 86400000)}
    paths = write_reports(Path(args.output), payload); print("Saved:", *(str(p) for p in paths))


if __name__ == "__main__":
    main()
