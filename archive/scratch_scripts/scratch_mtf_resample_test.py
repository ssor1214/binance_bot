"""Concept test: does generating signals off 3m/5m/15m resampled candles instead of
1m improve win rate, using the SAME offline_backtest.py signal()/run_backtest()
machinery this project already treats as its validated proxy engine?

IMPORTANT CAVEATS (read before trusting numbers):
1. This uses offline_backtest.py's own simplified signal() (EMA20/50 trend + MACD +
   RSI + ADX + volume/taker spike), NOT bot/strategy.py's live production
   generate_signal_with_probability. The live mtf_trend_alignment() gate additionally
   requires network calls (ex.get_klines for 5m/15m/1h/4h) and is ALREADY a mandatory
   hard filter (agree/total >= mtf_min_agree_ratio, default 0.75) on every single live
   entry since the very first tracked commit -- confirmed via `git log -S` and
   bot/main.py:2411-2417. That means the exact hypothesis under test ("does requiring
   higher-timeframe alignment improve entries") is already baked into every live trade;
   there is no historical "misaligned" trade population left to compare against
   (mtf_trend_alignment's agree/total is also never logged per executed trade, only for
   REJECTED candidates, so a clean post-hoc split by ratio is not reconstructable from
   logs without new data collection / re-fetching historical klines, which risks the
   IP-ban failure mode already documented).
2. Because of (1), this script instead answers a narrower, still-useful question via a
   clean, no-lookahead simulation: if the ENTIRE signal-detection cadence (not just an
   alignment filter) moves from 1m candles to slower N-minute candles, does directional
   accuracy improve. This is the fallback/plan-2 approach from the task brief.
3. TP/SL/trailing are scaled by timeframe with a simple multiplier (roughly
   sqrt(timeframe) volatility scaling) -- an approximation, not a tuned optimum for each
   timeframe. Absolute trade counts on 5-day data at 5m/15m are inherently small.
4. Resampling and warmup/pending-fill still go through the official run_backtest()
   pending-dict (next-bar-open fill) -- no custom immediate-fill loop.
"""
import sys
sys.path.insert(0, r"C:\Users\lg\Desktop\binance-futures-bot")
from pathlib import Path
from collections import defaultdict
import math
import offline_backtest as ob

ob.disable_network()

data_1m, quality = ob.load_data(Path("scratch_klines_v4.json"))
first_ts = min(c.timestamp for candles in data_1m.values() for c in candles)
last_ts = max(c.timestamp for candles in data_1m.values() for c in candles)
total_hours = (last_ts - first_ts) / 3600000


def resample(candles_1m, n_minutes):
    """Aggregate consecutive 1m candles into n-minute OHLCV bars, bucketed by
    floor(timestamp / (n*60000)). Only emits a bar once all its 1m children are
    present in-order (no partial/current bar, no lookahead)."""
    bucket_ms = n_minutes * 60000
    buckets = defaultdict(list)
    for c in candles_1m:
        buckets[c.timestamp - (c.timestamp % bucket_ms)].append(c)
    out = []
    for bucket_start in sorted(buckets):
        rows = sorted(buckets[bucket_start], key=lambda c: c.timestamp)
        if len(rows) != n_minutes:
            continue  # incomplete bucket (gap or edge of dataset) -- skip, don't guess
        out.append(ob.Candle(
            timestamp=bucket_start,
            open=rows[0].open, high=max(r.high for r in rows), low=min(r.low for r in rows),
            close=rows[-1].close, volume=sum(r.volume for r in rows),
            quote_volume=sum(r.quote_volume for r in rows),
            taker_buy_volume=sum(r.taker_buy_volume for r in rows),
        ))
    return out


resampled = {
    "1m (baseline)": data_1m,
    "3m": {s: resample(cs, 3) for s, cs in data_1m.items()},
    "5m": {s: resample(cs, 5) for s, cs in data_1m.items()},
    "15m": {s: resample(cs, 15) for s, cs in data_1m.items()},
}

# baseline live-approximate settings (established in this project's prior scratch tests)
BASE = dict(
    warmup=60, volume_ratio=2.0, candle_change_pct=0.35, taker_ratio=0.55,
    adx_threshold=20.0, short_volume_ratio=2.6, short_candle_change_pct=0.45,
    short_taker_buy_ratio_max=0.43, short_adx_threshold=24.0, short_rsi_max=45.0,
    stop_roe_pct=5.0, short_stop_roe_pct=5.0, take_profit_roe_pct=3.0,
    hard_take_profit_roe_pct=7.0, trailing_drawdown_roe_pct=1.0,
)

# Timeframe-scaled variants: warmup shrinks (fewer bars available on 5-day window),
# TP/SL widen roughly with sqrt(timeframe minutes) as a volatility-scaling proxy.
TF_MULT = {"1m (baseline)": 1.0, "3m": math.sqrt(3), "5m": math.sqrt(5), "15m": math.sqrt(15)}
TF_WARMUP = {"1m (baseline)": 60, "3m": 60, "5m": 50, "15m": 30}

results = {}
for name, ds in resampled.items():
    mult = TF_MULT[name]
    kwargs = dict(BASE)
    kwargs["warmup"] = TF_WARMUP[name]
    kwargs["stop_roe_pct"] = BASE["stop_roe_pct"] * mult
    kwargs["short_stop_roe_pct"] = BASE["short_stop_roe_pct"] * mult
    kwargs["take_profit_roe_pct"] = BASE["take_profit_roe_pct"] * mult
    kwargs["hard_take_profit_roe_pct"] = BASE["hard_take_profit_roe_pct"] * mult
    kwargs["trailing_drawdown_roe_pct"] = BASE["trailing_drawdown_roe_pct"] * mult
    settings = ob.Settings(**kwargs)
    n_bars = sum(len(cs) for cs in ds.values())
    result = ob.run_backtest(ds, settings)
    m = ob.metrics(result, first_ts + 3 * 86400000)
    results[name] = (result, m, settings, n_bars)

def pf_str(pf):
    return f"{pf:.2f}" if pf is not None else "inf"

print("=== ALL (5-day, resampled signal cadence) ===")
print(f"{'timeframe':>14} | {'bars':>6} | {'trades':>6} | {'tr/hr':>6} | {'win%':>6} | {'PF':>6} | {'EV/tr':>8} | {'avg_hold_m':>10} | {'net_pnl':>9} | {'max_dd':>7}")
print("-" * 120)
for name in resampled:
    result, m, s, n_bars = results[name]
    a = m["all"]
    print(f"{name:>14} | {n_bars:>6} | {a['trades']:>6} | {a['trades']/total_hours:>6.2f} | {a['win_rate']*100:>5.1f}% | {pf_str(a['profit_factor']):>6} | {a['expectancy']:>8.4f} | {a['average_holding_minutes']:>10.1f} | {a['net_pnl']:>9.3f} | {m['max_drawdown_usdt']:>7.3f}")

print()
print("=== By side (LONG/SHORT) -- all ===")
for name in resampled:
    result, m, s, n_bars = results[name]
    print(f"-- {name} --")
    for side in ("LONG", "SHORT"):
        rows = m["by_side"].get(side)
        if not rows:
            print(f"   {side}: no trades")
            continue
        print(f"   {side}: trades={rows['trades']:>4} win%={rows['win_rate']*100:>5.1f} PF={pf_str(rows['profit_factor']):>6} EV/tr={rows['expectancy']:>8.4f} net_pnl={rows['net_pnl']:>9.3f}")

out_dir = Path("backtest_results/mtf_resample_test")
out_dir.mkdir(parents=True, exist_ok=True)
import json
summary = {name: {"n_bars": n_bars, "metrics": m} for name, (_, m, _, n_bars) in results.items()}
(out_dir / "summary.json").write_text(json.dumps(summary, indent=2, default=str), encoding="utf-8")
print(f"\nWritten: {out_dir / 'summary.json'}")
