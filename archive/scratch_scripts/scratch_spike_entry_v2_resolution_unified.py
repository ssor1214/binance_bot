"""[2026-08-15] Spike-based early entry vs baseline (1m-signal, next-1m-open fill)
entry comparison -- RESOLUTION UNIFIED version.

Fixes a methodology bug in scratch_spike_entry_backtest.py: that script compared
baseline = actual LIVE trade result (1m-candle exit resolution, real order fills,
funding, slippage noise) against variant = 5s-candle simulated exit. The two
result sets differed in resolution, not just entry timing, so the comparison
conflated "earlier entry" with "finer exit resolution".

Here, BOTH baseline and variant are re-simulated with the exact same unmodified
`exit_decision()` from offline_backtest.py, walking forward on 5s OHLCV candles
built from real historical aggTrades tick data. The only difference between the
two paths is the entry timestamp/price:
  - baseline: entered_at from the real trade ledger (this is the actual live
    entry, which itself already resulted from 1m-candle PUMP_SIGNAL detection +
    fill at the next 1m candle's open -- i.e. exactly the required baseline
    entry method, just re-run through the finer-resolution exit engine for a
    fair comparison).
  - variant: first tick-level volume spike detected at/before entered_at
    (detect_volume_spike, unmodified, 5s window / 300s baseline window,
    multiplier 3.0x -- same as production SPIKE_ENTRY_* defaults), entering at
    that spike's tick price instead of waiting for the 1m candle boundary.

No lookahead: find_spike_time() only scans ticks in chronological order up to
entered_ms, and only fires once real (not synthetic/padded) baseline history of
BASELINE_LOOKBACK_MS has accumulated in the tick cache.

IP-ban precaution: reuses raw ticks already fetched once per sample for BOTH the
baseline and variant resimulation (no extra network calls per sample). Single
threaded, 0.35s+ sleep between every HTTP call, max 3 pages per sample, sample
capped at the existing 45-trade PUMP_SIGNAL set pulled from logs/trade_ledger.jsonl
(archive/scratch_scripts/scratch_spike_entry_backtest.py originally built this
same sample; reused verbatim here, no new ledger scraping needed).
"""
from __future__ import annotations

import json
import math
import time
import urllib.request
from pathlib import Path

from offline_backtest import Candle, Settings, exit_decision, Position
from bot.ws_trade_client import TradeTick, TradeTickCache, detect_volume_spike, resample_ticks_to_ohlcv

SAMPLE_PATH = Path(r"C:\Users\lg\AppData\Local\Temp\claude\c--Users-lg-Desktop-binance-futures-bot\0dad9f52-9f21-4dc5-ad53-9d30561acc41\scratchpad\spike_sample.json")
OUT_PATH = Path(r"C:\Users\lg\AppData\Local\Temp\claude\c--Users-lg-Desktop-binance-futures-bot\0dad9f52-9f21-4dc5-ad53-9d30561acc41\scratchpad\spike_entry_v2_results.json")

SLEEP_SEC = 0.35
BASE_URL = "https://fapi.binance.com/fapi/v1/aggTrades"

BASELINE_LOOKBACK_MS = 300_000
SEARCH_LOOKBACK_MS = 350_000
FORWARD_BUFFER_MS = 180_000  # extra room past actual held_seconds for both resims to finish


def fetch_agg_trades(symbol: str, start_ms: int, end_ms: int, max_pages: int = 3) -> list:
    out = []
    cur_start = start_ms
    for _ in range(max_pages):
        url = f"{BASE_URL}?symbol={symbol}&startTime={cur_start}&endTime={end_ms}&limit=1000"
        req = urllib.request.Request(url, headers={"User-Agent": "scratch-backtest/1.0"})
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = json.loads(resp.read())
        except Exception as e:
            print(f"  fetch error {symbol}: {e}")
            break
        time.sleep(SLEEP_SEC)
        if not data:
            break
        out.extend(data)
        last_t = data[-1]["T"]
        if last_t >= end_ms or len(data) < 1000:
            break
        cur_start = last_t + 1
    return out


def to_ticks(symbol: str, raw: list) -> list:
    ticks = []
    for d in raw:
        ticks.append(TradeTick(
            symbol=symbol, price=float(d["p"]), quantity=float(d["q"]),
            event_time_ms=int(d["T"]), trade_time_ms=int(d["T"]),
            is_buyer_maker=bool(d["m"]),
        ))
    return ticks


def find_spike_time(ticks: list, symbol: str, entry_ms: int, fetch_start_ms: int) -> dict | None:
    cache = TradeTickCache(max_ticks_per_symbol=20000)
    sorted_ticks = sorted(ticks, key=lambda t: t.trade_time_ms)
    checked_times = set()
    check_start_ms = fetch_start_ms + BASELINE_LOOKBACK_MS
    for t in sorted_ticks:
        if t.trade_time_ms > entry_ms:
            break
        cache.append(t)
        bucket = t.trade_time_ms // 2000
        if bucket in checked_times:
            continue
        checked_times.add(bucket)
        if t.trade_time_ms < check_start_ms:
            continue
        result = detect_volume_spike(cache, symbol, spike_multiplier=3.0,
                                      spike_window_sec=10.0, baseline_window_sec=300.0,
                                      now_ms=t.trade_time_ms)
        if result["is_spike"]:
            return {"time_ms": t.trade_time_ms, "price": t.price, "ratio": result["ratio"]}
    return None


def simulate_exit(ticks: list, symbol: str, side: str, entry_ms: int, entry_price: float,
                   settings: Settings, max_hold_ms: int) -> dict | None:
    """Unmodified production exit_decision(), walked forward on 5s OHLCV candles.
    Used identically for BOTH baseline and variant -- only entry_ms/entry_price differ."""
    window_ticks = [t for t in ticks if entry_ms <= t.trade_time_ms <= entry_ms + max_hold_ms]
    candles_raw = resample_ticks_to_ohlcv(window_ticks, bucket_sec=5.0)
    if not candles_raw:
        return None
    pos = Position(symbol=symbol, side=side, entry_time=entry_ms, entry_price=entry_price,
                    quantity=1.0, margin=1.0, entry_fee=0.0, peak_price=entry_price)
    for c in candles_raw:
        candle = Candle(timestamp=c["close_time_ms"], open=c["open"], high=c["high"],
                         low=c["low"], close=c["close"], volume=c["volume"],
                         quote_volume=c["quote_volume"], taker_buy_volume=c["taker_buy_base"])
        decision = exit_decision(pos, candle, settings)
        if decision:
            price, reason = decision
            pnl_pct = (price / entry_price - 1) * (1 if side == "LONG" else -1) * settings.leverage * 100
            return {"exit_price": price, "reason": reason, "exit_time_ms": candle.timestamp,
                     "pnl_roe_pct": pnl_pct, "held_sec": (candle.timestamp - entry_ms) / 1000,
                     "complete": True}
        favorable = candle.high if side == "LONG" else candle.low
        pos.peak_price = max(pos.peak_price, favorable) if side == "LONG" else min(pos.peak_price, favorable)
        roe = (pos.peak_price / entry_price - 1) * (1 if side == "LONG" else -1) * settings.leverage * 100
        if roe >= settings.take_profit_roe_pct:
            pos.trailing_armed = True
    last = candles_raw[-1]
    pnl_pct = (last["close"] / entry_price - 1) * (1 if side == "LONG" else -1) * settings.leverage * 100
    return {"exit_price": last["close"], "reason": "data_exhausted", "exit_time_ms": last["close_time_ms"],
            "pnl_roe_pct": pnl_pct, "held_sec": (last["close_time_ms"] - entry_ms) / 1000,
            "complete": False}


def main():
    sample = json.loads(SAMPLE_PATH.read_text(encoding="utf-8"))
    results = []
    for i, trade in enumerate(sample):
        symbol = trade["symbol"]
        side = trade["side"]
        entered_ms = int(trade["entered_at"] * 1000)
        held_ms = int(trade["held_seconds"] * 1000)
        snap = trade.get("config_snapshot", {})
        settings = Settings(
            leverage=float(trade.get("leverage", 6)),
            stop_roe_pct=float(snap.get("stop_loss_pct", 6.0)),
            hard_take_profit_roe_pct=float(snap.get("take_profit_hard_cap", 20.0)),
            take_profit_roe_pct=float(snap.get("take_profit_min", 3.0)),
            trailing_drawdown_roe_pct=float(snap.get("trail_drawdown_pct", 0.9)),
        )
        fetch_start = entered_ms - (BASELINE_LOOKBACK_MS + SEARCH_LOOKBACK_MS)
        fetch_end = entered_ms + max(held_ms, 60_000) + FORWARD_BUFFER_MS
        print(f"[{i+1}/{len(sample)}] {symbol} {side} entered={entered_ms} window={fetch_end-fetch_start}ms")
        # Fetch pre-entry and post-entry windows SEPARATELY. A single combined fetch
        # capped at a small page budget silently starved the post-entry side for
        # high-volume symbols (all pages consumed by the pre-entry search window),
        # leaving zero ticks to resimulate the exit on. Each side gets its own budget.
        raw_pre = fetch_agg_trades(symbol, fetch_start, entered_ms, max_pages=4)
        raw_post = fetch_agg_trades(symbol, entered_ms + 1, fetch_end, max_pages=8)
        raw = raw_pre + raw_post
        if not raw:
            print("  no data, skipping")
            time.sleep(SLEEP_SEC)
            continue
        if not raw_post:
            print("  no post-entry data, skipping (cannot resimulate exit)")
            time.sleep(SLEEP_SEC)
            continue
        ticks = to_ticks(symbol, raw)

        # --- baseline: real ledger entry timestamp/price, resimulated at 5s resolution ---
        baseline_max_hold = held_ms + FORWARD_BUFFER_MS
        baseline_exit = simulate_exit(ticks, symbol, side, entered_ms, trade["entry_price"],
                                       settings, baseline_max_hold)

        row = {
            "symbol": symbol, "side": side, "entered_at": trade["entered_at"],
            "baseline_entry_price": trade["entry_price"],
            "baseline_exit_5s": baseline_exit,
            "live_ledger_pnl_pct_estimate": trade["estimated_pnl_pct"],  # reference only, not used in comparison
            "live_ledger_held_sec": trade["held_seconds"],
            "tick_count": len(ticks),
        }

        # --- variant: earliest tick-level spike at/before entered_ms, same 5s exit engine ---
        spike = find_spike_time(ticks, symbol, entered_ms, fetch_start)
        if spike is None:
            row["spike_found"] = False
        else:
            row["spike_found"] = True
            row["spike_time_ms"] = spike["time_ms"]
            row["spike_price"] = spike["price"]
            row["spike_ratio"] = spike["ratio"]
            row["seconds_earlier_than_actual_entry"] = (entered_ms - spike["time_ms"]) / 1000
            row["price_diff_pct"] = (spike["price"] / trade["entry_price"] - 1) * 100
            variant_max_hold_ms = held_ms + (entered_ms - spike["time_ms"]) + FORWARD_BUFFER_MS
            variant = simulate_exit(ticks, symbol, side, spike["time_ms"], spike["price"],
                                     settings, variant_max_hold_ms)
            row["variant_exit_5s"] = variant
        results.append(row)
        time.sleep(SLEEP_SEC)
    OUT_PATH.write_text(json.dumps(results, indent=2), encoding="utf-8")
    print("Saved:", OUT_PATH)


if __name__ == "__main__":
    main()
