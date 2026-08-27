"""[2026-08-15] Resume of scratch_spike_entry_v2_resolution_unified.py.
Processes remaining sample trades (index 15..44) not yet covered in
spike_entry_v2_results.json (which already has 15 processed, 4 complete pairs).
Appends new rows to the same output file. Reuses all logic verbatim from the
v2 script (find_spike_time / simulate_exit / Settings construction) -- only
adds: resume-from-index, more conservative throttling (0.5-1.0s per call,
extra 2-3s rest every 20 calls), and immediate abort on 429/418/ban signals.
"""
from __future__ import annotations

import json
import time
import urllib.request
import urllib.error
from pathlib import Path

from offline_backtest import Candle, Settings, exit_decision, Position
from bot.ws_trade_client import TradeTick, TradeTickCache, detect_volume_spike, resample_ticks_to_ohlcv

SAMPLE_PATH = Path(r"C:\Users\lg\AppData\Local\Temp\claude\c--Users-lg-Desktop-binance-futures-bot\0dad9f52-9f21-4dc5-ad53-9d30561acc41\scratchpad\spike_sample.json")
OUT_PATH = Path(r"C:\Users\lg\AppData\Local\Temp\claude\c--Users-lg-Desktop-binance-futures-bot\0dad9f52-9f21-4dc5-ad53-9d30561acc41\scratchpad\spike_entry_v2_results.json")

SLEEP_SEC = 0.7
CALL_COUNT_REST_EVERY = 20
CALL_COUNT_REST_SEC = 2.5
BASE_URL = "https://fapi.binance.com/fapi/v1/aggTrades"

BASELINE_LOOKBACK_MS = 300_000
SEARCH_LOOKBACK_MS = 350_000
FORWARD_BUFFER_MS = 180_000

call_count = 0
aborted = False


def fetch_agg_trades(symbol: str, start_ms: int, end_ms: int, max_pages: int = 8) -> list:
    global call_count, aborted
    out = []
    cur_start = start_ms
    for _ in range(max_pages):
        if aborted:
            break
        url = f"{BASE_URL}?symbol={symbol}&startTime={cur_start}&endTime={end_ms}&limit=1000"
        req = urllib.request.Request(url, headers={"User-Agent": "scratch-backtest/1.0"})
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = json.loads(resp.read())
        except urllib.error.HTTPError as e:
            if e.code in (418, 429):
                print(f"  !!! BAN SIGNAL {e.code} on {symbol} -- ABORTING IMMEDIATELY !!!")
                aborted = True
            else:
                print(f"  fetch HTTPError {symbol}: {e}")
            break
        except Exception as e:
            msg = str(e)
            if "banned" in msg.lower() or "418" in msg or "429" in msg or "too many" in msg.lower():
                print(f"  !!! BAN SIGNAL in exception on {symbol}: {msg} -- ABORTING !!!")
                aborted = True
            else:
                print(f"  fetch error {symbol}: {e}")
            break
        call_count += 1
        time.sleep(SLEEP_SEC)
        if call_count % CALL_COUNT_REST_EVERY == 0:
            print(f"  ...{call_count} calls done, resting {CALL_COUNT_REST_SEC}s")
            time.sleep(CALL_COUNT_REST_SEC)
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
    global aborted
    sample = json.loads(SAMPLE_PATH.read_text(encoding="utf-8"))
    existing = json.loads(OUT_PATH.read_text(encoding="utf-8")) if OUT_PATH.exists() else []
    already_done = len(existing)
    print(f"Resuming from index {already_done} (already have {already_done} rows in output)")

    def complete_pairs_count(rows):
        n = 0
        for r in rows:
            if r.get("spike_found") and r.get("baseline_exit_5s", {}).get("complete") and \
               r.get("variant_exit_5s", {}).get("complete"):
                n += 1
        return n

    results = list(existing)
    target_pairs = 20
    for i in range(already_done, len(sample)):
        if aborted:
            print("ABORTED due to ban signal -- stopping loop.")
            break
        pairs_so_far = complete_pairs_count(results)
        if pairs_so_far >= target_pairs:
            print(f"Reached target of {target_pairs} complete pairs -- stopping.")
            break
        trade = sample[i]
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
        print(f"[{i+1}/{len(sample)}] pairs_so_far={pairs_so_far} {symbol} {side} entered={entered_ms}")
        raw_pre = fetch_agg_trades(symbol, fetch_start, entered_ms, max_pages=4)
        if aborted:
            break
        raw_post = fetch_agg_trades(symbol, entered_ms + 1, fetch_end, max_pages=8)
        if aborted:
            break
        raw = raw_pre + raw_post
        if not raw or not raw_post:
            print("  insufficient data, skipping")
            continue
        ticks = to_ticks(symbol, raw)

        baseline_max_hold = held_ms + FORWARD_BUFFER_MS
        baseline_exit = simulate_exit(ticks, symbol, side, entered_ms, trade["entry_price"],
                                       settings, baseline_max_hold)

        row = {
            "symbol": symbol, "side": side, "entered_at": trade["entered_at"],
            "baseline_entry_price": trade["entry_price"],
            "baseline_exit_5s": baseline_exit,
            "live_ledger_pnl_pct_estimate": trade["estimated_pnl_pct"],
            "live_ledger_held_sec": trade["held_seconds"],
            "tick_count": len(ticks),
        }

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
        # save incrementally so partial progress survives an abort
        OUT_PATH.write_text(json.dumps(results, indent=2), encoding="utf-8")

    final_pairs = complete_pairs_count(results)
    print(f"Done. Total rows={len(results)}, complete pairs={final_pairs}, aborted={aborted}")
    print("Saved:", OUT_PATH)


if __name__ == "__main__":
    main()
