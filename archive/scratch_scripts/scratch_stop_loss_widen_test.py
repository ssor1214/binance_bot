"""Compare STOP_LOSS_PCT (stop_roe_pct) baseline (6.0, live) vs 8.0/10.0/12.0.

Uses the official offline_backtest.run_backtest pending-fill mechanism
(next-candle-open fills). No custom entry loop. No signal/exit monkeypatch
needed here since we only vary a Settings field.
"""
import sys
sys.path.insert(0, r"C:\Users\lg\Desktop\binance-futures-bot")
from pathlib import Path
from dataclasses import asdict
import offline_backtest as ob

ob.disable_network()

data, quality = ob.load_data(Path("scratch_klines_v4.json"))
first_ts = min(c.timestamp for candles in data.values() for c in candles)

variants = [6.0, 8.0, 10.0, 12.0]
results = {}
for sl in variants:
    settings = ob.Settings(stop_roe_pct=sl)
    result = ob.run_backtest(data, settings)
    m = ob.metrics(result, first_ts + 3 * 86400000)
    results[sl] = (result, m)

print(f"{'STOP_LOSS_PCT':>14} | {'trades':>7} | {'trades/hr':>9} | {'win_rate':>8} | {'PF':>6} | {'EV/trade':>9} | {'avg_hold_m':>10} | {'net_pnl':>9} | {'max_dd':>8} | {'final_bal':>9}")
print("-" * 120)
total_hours = (max(c.timestamp for candles in data.values() for c in candles) - first_ts) / 3600000
for sl in variants:
    result, m = results[sl]
    a = m["all"]
    pf = a["profit_factor"]
    pf_s = f"{pf:.2f}" if pf is not None else "inf"
    print(f"{sl:>14.1f} | {a['trades']:>7} | {a['trades']/total_hours:>9.2f} | {a['win_rate']*100:>7.1f}% | {pf_s:>6} | {a['expectancy']:>9.4f} | {a['average_holding_minutes']:>10.1f} | {a['net_pnl']:>9.3f} | {m['max_drawdown_usdt']:>8.3f} | {result['final_balance']:>9.3f}")

print()
print("=== Validation-only (last 2 days, held-out) ===")
print(f"{'STOP_LOSS_PCT':>14} | {'trades':>7} | {'win_rate':>8} | {'PF':>6} | {'EV/trade':>9} | {'net_pnl':>9}")
print("-" * 70)
for sl in variants:
    result, m = results[sl]
    v = m["validation_last_2_days"]
    pf = v["profit_factor"]
    pf_s = f"{pf:.2f}" if pf is not None else "inf"
    print(f"{sl:>14.1f} | {v['trades']:>7} | {v['win_rate']*100:>7.1f}% | {pf_s:>6} | {v['expectancy']:>9.4f} | {v['net_pnl']:>9.3f}")

print()
print("=== Loss-side detail (avg loss size, adverse-loss-rate) ===")
print(f"{'STOP_LOSS_PCT':>14} | {'avg_win':>8} | {'avg_loss':>9} | {'adverse_loss_rate(>=3%)':>24}")
for sl in variants:
    result, m = results[sl]
    a = m["all"]
    print(f"{sl:>14.1f} | {a['average_win']:>8.4f} | {a['average_loss']:>9.4f} | {a['adverse_loss_rate']*100:>23.1f}%")

# Save baseline vs 8.0/10.0/12.0 full JSON for the record.
out_dir = Path("backtest_results/stop_loss_widen_test")
out_dir.mkdir(parents=True, exist_ok=True)
import json
for sl in variants:
    result, m = results[sl]
    payload = {"data_file": "scratch_klines_v4.json", "settings": asdict(ob.Settings(stop_roe_pct=sl)), "result": {k: v for k, v in result.items() if k != "ledger"}, "metrics": m}
    (out_dir / f"sl_{sl:.1f}.json").write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
print("\nSaved detail JSONs to", out_dir)
