"""Fine grid around the promising region found in scratch_rr_sweep.py: wider/equal
hard_take_profit_roe_pct combined with tighter trailing_drawdown_roe_pct.
Same methodology/baseline mapping as scratch_rr_sweep.py -- see that file's docstring.
"""
import sys
sys.path.insert(0, r"C:\Users\lg\Desktop\binance-futures-bot")
from pathlib import Path
import offline_backtest as ob

ob.disable_network()
data, quality = ob.load_data(Path("scratch_klines_v4.json"))
first_ts = min(c.timestamp for candles in data.values() for c in candles)
last_ts = max(c.timestamp for candles in data.values() for c in candles)
total_hours = (last_ts - first_ts) / 3600000

caps = [18.0, 20.0, 24.0, 28.0]
trails = [0.6, 0.8, 1.0, 1.3]

print(f"{'cap':>5} | {'trail':>5} | {'trades':>6} | {'tr/hr':>6} | {'win%':>6} | {'PF':>6} | {'EV/tr':>8} | {'net_pnl':>9} | {'max_dd':>8} | {'fin_bal':>9}")
print("-" * 100)
rows = []
for cap in caps:
    for trail in trails:
        settings = ob.Settings(stop_roe_pct=6.0, short_stop_roe_pct=6.0, take_profit_roe_pct=3.0,
                                hard_take_profit_roe_pct=cap, trailing_drawdown_roe_pct=trail)
        result = ob.run_backtest(data, settings)
        m = ob.metrics(result, first_ts + 3 * 86400000)
        a = m["all"]
        pf = a["profit_factor"]
        pf_s = f"{pf:.2f}" if pf is not None else "inf"
        print(f"{cap:>5.1f} | {trail:>5.2f} | {a['trades']:>6} | {a['trades']/total_hours:>6.2f} | {a['win_rate']*100:>5.1f}% | {pf_s:>6} | {a['expectancy']:>8.4f} | {a['net_pnl']:>9.3f} | {m['max_drawdown_usdt']:>8.3f} | {result['final_balance']:>9.3f}")
        rows.append((cap, trail, a['net_pnl'], pf))

best = max(rows, key=lambda r: r[2])
print(f"\nBest by net_pnl: cap={best[0]}, trail={best[1]}, net_pnl={best[2]:.3f}, PF={best[3]}")
