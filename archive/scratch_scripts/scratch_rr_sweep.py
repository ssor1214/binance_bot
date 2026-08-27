"""R:R (stop_roe_pct : hard_take_profit_roe_pct) sweep vs current live baseline.

Live .env baseline mapped to offline_backtest.Settings:
  STOP_LOSS_PCT=6.0          -> stop_roe_pct=6.0
  SHORT_STOP_LOSS_PCT=0      -> short_stop_roe_pct=6.0 (0 = use common value)
  TAKE_PROFIT_MIN=3.0        -> take_profit_roe_pct=3.0 (trailing-arm threshold)
  TAKE_PROFIT_HARD_CAP=20.0  -> hard_take_profit_roe_pct=20.0 (TAKE_PROFIT_MAX is
                                 also 20.0 live, so MAX and HARD_CAP coincide right now)
  TRAIL_DRAWDOWN_PCT=1.3     -> trailing_drawdown_roe_pct=1.3

NOTE (methodology limitation, disclosed): offline_backtest.py's exit_decision() is a
SIMPLIFIED model of the live exit stack. It only has one stop level, one trailing-arm
threshold, one trailing callback, and one hard cap -- it does NOT model live-only
mechanics such as SHORT_TAKE_PROFIT_MIN(4.0, separate from LONG's 3.0), SOFT_STOP,
STOP_LOSS_GRACE_SEC/WIDEN, TIME_STOP, FORCE_PROFIT_EXIT, or average-down. So absolute
win-rate/PF numbers will differ from live, but the *relative* comparison between R:R
variants under the same simplified model is still informative for "is a wider/narrower
TP structurally better", which is what was asked here.

Uses the official offline_backtest.run_backtest pending-fill mechanism (next-candle-open
fill). No custom entry loop. No signal/exit monkeypatch.
"""
import sys
sys.path.insert(0, r"C:\Users\lg\Desktop\binance-futures-bot")
from pathlib import Path
from dataclasses import asdict
import json
import offline_backtest as ob

ob.disable_network()

data, quality = ob.load_data(Path("scratch_klines_v4.json"))
first_ts = min(c.timestamp for candles in data.values() for c in candles)
last_ts = max(c.timestamp for candles in data.values() for c in candles)
total_hours = (last_ts - first_ts) / 3600000

# (label, stop_roe_pct, short_stop_roe_pct, take_profit_roe_pct(trail-arm), hard_take_profit_roe_pct, trailing_drawdown_roe_pct)
variants = [
    ("baseline(live) 1:3.33", 6.0, 6.0, 3.0, 20.0, 1.3),
    ("RR 1:1.0  (cap=6)",      6.0, 6.0, 2.0, 6.0, 1.0),
    ("RR 1:1.5  (cap=9)",      6.0, 6.0, 2.5, 9.0, 1.1),
    ("RR 1:2.0  (cap=12)",     6.0, 6.0, 3.0, 12.0, 1.3),
    ("RR 1:2.5  (cap=15)",     6.0, 6.0, 3.0, 15.0, 1.3),
    ("RR 1:3.0  (cap=18)",     6.0, 6.0, 3.0, 18.0, 1.3),
    ("RR 1:4.0  (cap=24)",     6.0, 6.0, 3.0, 24.0, 1.3),
    ("baseline cap, tighter trail(0.8)", 6.0, 6.0, 3.0, 20.0, 0.8),
]

results = {}
for label, stop, short_stop, arm, cap, trail in variants:
    settings = ob.Settings(stop_roe_pct=stop, short_stop_roe_pct=short_stop,
                            take_profit_roe_pct=arm, hard_take_profit_roe_pct=cap,
                            trailing_drawdown_roe_pct=trail)
    result = ob.run_backtest(data, settings)
    m = ob.metrics(result, first_ts + 3 * 86400000)
    results[label] = (settings, result, m)

print(f"Data: {len(data)} symbols, {total_hours:.1f}h span, {first_ts} - {last_ts}")
print()
print(f"{'variant':>34} | {'trades':>7} | {'tr/hr':>6} | {'win%':>6} | {'PF':>6} | {'EV/tr':>8} | {'avg_hold_m':>10} | {'net_pnl':>9} | {'max_dd':>8} | {'fin_bal':>9}")
print("-" * 140)
for label, stop, short_stop, arm, cap, trail in variants:
    settings, result, m = results[label]
    a = m["all"]
    pf = a["profit_factor"]
    pf_s = f"{pf:.2f}" if pf is not None else "inf"
    print(f"{label:>34} | {a['trades']:>7} | {a['trades']/total_hours:>6.2f} | {a['win_rate']*100:>5.1f}% | {pf_s:>6} | {a['expectancy']:>8.4f} | {a['average_holding_minutes']:>10.1f} | {a['net_pnl']:>9.3f} | {m['max_drawdown_usdt']:>8.3f} | {result['final_balance']:>9.3f}")

print()
print("=== Validation-only (last 2 days, held-out) ===")
print(f"{'variant':>34} | {'trades':>7} | {'win%':>6} | {'PF':>6} | {'EV/tr':>8} | {'net_pnl':>9}")
print("-" * 90)
for label, stop, short_stop, arm, cap, trail in variants:
    settings, result, m = results[label]
    v = m["validation_last_2_days"]
    pf = v["profit_factor"]
    pf_s = f"{pf:.2f}" if pf is not None else "inf"
    print(f"{label:>34} | {v['trades']:>7} | {v['win_rate']*100:>5.1f}% | {pf_s:>6} | {v['expectancy']:>8.4f} | {v['net_pnl']:>9.3f}")

print()
print("=== By side (LONG vs SHORT), all period ===")
print(f"{'variant':>34} | {'side':>5} | {'trades':>6} | {'win%':>6} | {'PF':>6} | {'net_pnl':>9}")
for label, stop, short_stop, arm, cap, trail in variants:
    settings, result, m = results[label]
    for side in ("LONG", "SHORT"):
        s = m["by_side"].get(side)
        if not s:
            continue
        pf = s["profit_factor"]
        pf_s = f"{pf:.2f}" if pf is not None else "inf"
        print(f"{label:>34} | {side:>5} | {s['trades']:>6} | {s['win_rate']*100:>5.1f}% | {pf_s:>6} | {s['net_pnl']:>9.3f}")

out_dir = Path("backtest_results/rr_sweep")
out_dir.mkdir(parents=True, exist_ok=True)
for label, stop, short_stop, arm, cap, trail in variants:
    settings, result, m = results[label]
    safe = label.replace(" ", "_").replace(":", "").replace("(", "").replace(")", "").replace(".", "p").replace("=", "").replace(",", "")
    payload = {"data_file": "scratch_klines_v4.json", "settings": asdict(settings),
               "result": {k: v for k, v in result.items() if k != "ledger"}, "metrics": m}
    (out_dir / f"{safe}.json").write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
print("\nSaved detail JSONs to", out_dir)
