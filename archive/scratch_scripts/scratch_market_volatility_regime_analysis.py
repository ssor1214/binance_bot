import json, math
from collections import defaultdict

with open("logs/trade_ledger.jsonl", encoding="utf-8") as f:
    trades = []
    for line in f:
        try:
            d = json.loads(line)
        except Exception:
            continue
        if d.get("origin") == "bot" and d.get("entry_reason") == "PUMP_SIGNAL":
            if d.get("estimated_pnl_pct") is None:
                continue
            trades.append(d)

print("total PUMP_SIGNAL trades w/ pnl:", len(trades))

with open("archive/scratch_scripts/scratch_btc_1m_klines.json") as f:
    kl = json.load(f)

# kl: [open_time, open, high, low, close, volume, close_time, ...]
kl_by_time = {}
for row in kl:
    ot = int(row[0]) // 1000  # seconds
    o, h, l, c = float(row[1]), float(row[2]), float(row[3]), float(row[4])
    kl_by_time[ot] = (o, h, l, c)

sorted_times = sorted(kl_by_time.keys())

def range_pct_before(entry_ts, window_minutes):
    """Use only candles with open_time < entry_ts (strictly before, no lookahead)."""
    entry_min = int(entry_ts // 60) * 60
    # candles fully closed before entry minute
    end = entry_min  # exclusive of current forming candle
    start = end - window_minutes * 60
    vals = []
    for t in range(start, end, 60):
        if t in kl_by_time:
            o, h, l, c = kl_by_time[t]
            if o > 0:
                vals.append((h - l) / o * 100.0)
    if len(vals) < window_minutes * 0.8:  # require at least 80% coverage
        return None
    return sum(vals) / len(vals)

for window in (30, 60):
    key = f"vol_{window}"
    for t in trades:
        t[key] = range_pct_before(t["entered_at"], window)

def stats(subset):
    n = len(subset)
    if n == 0:
        return None
    wins = [t for t in subset if t["estimated_pnl_pct"] > 0]
    losses = [t for t in subset if t["estimated_pnl_pct"] <= 0]
    win_rate = len(wins) / n * 100
    gross_win = sum(t["estimated_pnl_pct"] for t in wins)
    gross_loss = -sum(t["estimated_pnl_pct"] for t in losses)
    pf = gross_win / gross_loss if gross_loss > 0 else float("inf")
    avg_roe = sum(t["estimated_pnl_pct"] for t in subset) / n
    return dict(n=n, win_rate=win_rate, pf=pf, avg_roe=avg_roe)

def z_test_two_prop(n1, w1, n2, w2):
    p1, p2 = w1/n1, w2/n2
    p_pool = (w1+w2)/(n1+n2)
    se = math.sqrt(p_pool*(1-p_pool)*(1/n1+1/n2))
    if se == 0:
        return None
    z = (p1-p2)/se
    return z

for window in (30, 60):
    key = f"vol_{window}"
    valid = [t for t in trades if t.get(key) is not None]
    print(f"\n=== window={window}min, valid sample={len(valid)} (of {len(trades)}) ===")
    valid_sorted = sorted(valid, key=lambda t: t[key])
    n = len(valid_sorted)
    q1 = valid_sorted[: n // 4]
    q2 = valid_sorted[n // 4: n // 2]
    q3 = valid_sorted[n // 2: 3 * n // 4]
    q4 = valid_sorted[3 * n // 4:]
    half_low = valid_sorted[: n // 2]
    half_high = valid_sorted[n // 2:]

    for label, sub in [("Q1(lowest vol)", q1), ("Q2", q2), ("Q3", q3), ("Q4(highest vol)", q4)]:
        s = stats(sub)
        vol_range = (sub[0][key], sub[-1][key]) if sub else (None, None)
        print(f"{label}: n={s['n']} win_rate={s['win_rate']:.1f}% pf={s['pf']:.2f} avg_roe={s['avg_roe']:.3f}% vol_range={vol_range[0]:.3f}-{vol_range[1]:.3f}%")

    s_low = stats(half_low)
    s_high = stats(half_high)
    print(f"HALF-LOW: n={s_low['n']} win_rate={s_low['win_rate']:.1f}% pf={s_low['pf']:.2f} avg_roe={s_low['avg_roe']:.3f}%")
    print(f"HALF-HIGH: n={s_high['n']} win_rate={s_high['win_rate']:.1f}% pf={s_high['pf']:.2f} avg_roe={s_high['avg_roe']:.3f}%")

    w_low = sum(1 for t in half_low if t["estimated_pnl_pct"] > 0)
    w_high = sum(1 for t in half_high if t["estimated_pnl_pct"] > 0)
    z = z_test_two_prop(len(half_low), w_low, len(half_high), w_high)
    print(f"z-test (low vs high win rate): z={z:.3f}" if z is not None else "z-test: n/a")

    # correlation (pearson) between vol and pnl_pct
    xs = [t[key] for t in valid]
    ys = [t["estimated_pnl_pct"] for t in valid]
    mean_x = sum(xs)/len(xs)
    mean_y = sum(ys)/len(ys)
    cov = sum((x-mean_x)*(y-mean_y) for x,y in zip(xs,ys))
    sx = math.sqrt(sum((x-mean_x)**2 for x in xs))
    sy = math.sqrt(sum((y-mean_y)**2 for y in ys))
    r = cov/(sx*sy) if sx>0 and sy>0 else None
    print(f"pearson r (vol vs pnl_pct): {r:.4f}" if r is not None else "r: n/a")
