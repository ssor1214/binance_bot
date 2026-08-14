import json

with open("scratch_mtf_replay_results.json", encoding="utf-8") as f:
    data = json.load(f)

data.sort(key=lambda r: r["entered_at"])
print(f"total entries: {len(data)}")
print(f"span: {(data[-1]['entered_at'] - data[0]['entered_at']) / 3600:.1f} hours")

thresholds = [0.0, 0.5, 0.75, 0.80, 0.85, 0.90, 1.0]


def stats(rows, total_hours):
    n = len(rows)
    if n == 0:
        return dict(n=0)
    wins = [r for r in rows if r["pnl_usdt"] is not None and r["pnl_usdt"] > 0]
    losses = [r for r in rows if r["pnl_usdt"] is not None and r["pnl_usdt"] <= 0]
    win_rate = len(wins) / n * 100
    gross_win = sum(r["pnl_usdt"] for r in wins)
    gross_loss = -sum(r["pnl_usdt"] for r in losses)
    pf = gross_win / gross_loss if gross_loss > 0 else float("inf")
    net = sum(r["pnl_usdt"] for r in rows if r["pnl_usdt"] is not None)
    ev = net / n
    avg_hold = sum(r["held_seconds"] for r in rows if r["held_seconds"] is not None) / n
    per_hour = n / total_hours
    return dict(n=n, win_rate=win_rate, pf=pf, net=net, ev=ev, avg_hold=avg_hold, per_hour=per_hour)


total_hours = (data[-1]["entered_at"] - data[0]["entered_at"]) / 3600

print(f"\n{'thr':>5} | {'n':>5} | {'trades/hr':>9} | {'win%':>6} | {'PF':>6} | {'net USDT':>9} | {'EV/trade':>9} | {'avg_hold(s)':>11}")
print("-" * 90)
for thr in thresholds:
    rows = [r for r in data if r["ratio"] >= thr - 1e-9]
    s = stats(rows, total_hours)
    if s["n"] == 0:
        print(f"{thr:>5} | no trades")
        continue
    print(f"{thr:>5} | {s['n']:>5} | {s['per_hour']:>9.2f} | {s['win_rate']:>6.2f} | {s['pf']:>6.2f} | {s['net']:>9.2f} | {s['ev']:>9.4f} | {s['avg_hold']:>11.1f}")

print("\n=== held-out: last 48h ===")
cutoff = data[-1]["entered_at"] - 48 * 3600
recent = [r for r in data if r["entered_at"] >= cutoff]
recent_hours = (recent[-1]["entered_at"] - recent[0]["entered_at"]) / 3600
print(f"recent entries: {len(recent)}, span {recent_hours:.1f}h")
print(f"\n{'thr':>5} | {'n':>5} | {'trades/hr':>9} | {'win%':>6} | {'PF':>6} | {'net USDT':>9} | {'EV/trade':>9} | {'avg_hold(s)':>11}")
print("-" * 90)
for thr in thresholds:
    rows = [r for r in recent if r["ratio"] >= thr - 1e-9]
    s = stats(rows, recent_hours)
    if s["n"] == 0:
        print(f"{thr:>5} | no trades")
        continue
    print(f"{thr:>5} | {s['n']:>5} | {s['per_hour']:>9.2f} | {s['win_rate']:>6.2f} | {s['pf']:>6.2f} | {s['net']:>9.2f} | {s['ev']:>9.4f} | {s['avg_hold']:>11.1f}")

print("\n=== held-out: last 24h ===")
cutoff24 = data[-1]["entered_at"] - 24 * 3600
recent24 = [r for r in data if r["entered_at"] >= cutoff24]
recent24_hours = (recent24[-1]["entered_at"] - recent24[0]["entered_at"]) / 3600
print(f"recent24 entries: {len(recent24)}, span {recent24_hours:.1f}h")
print(f"\n{'thr':>5} | {'n':>5} | {'trades/hr':>9} | {'win%':>6} | {'PF':>6} | {'net USDT':>9} | {'EV/trade':>9} | {'avg_hold(s)':>11}")
print("-" * 90)
for thr in thresholds:
    rows = [r for r in recent24 if r["ratio"] >= thr - 1e-9]
    s = stats(rows, recent24_hours)
    if s["n"] == 0:
        print(f"{thr:>5} | no trades")
        continue
    print(f"{thr:>5} | {s['n']:>5} | {s['per_hour']:>9.2f} | {s['win_rate']:>6.2f} | {s['pf']:>6.2f} | {s['net']:>9.2f} | {s['ev']:>9.4f} | {s['avg_hold']:>11.1f}")

# ratio distribution
from collections import Counter
print("\nratio distribution (agree/total):")
c = Counter(round(r["ratio"], 2) for r in data)
for k in sorted(c):
    print(f"  {k}: {c[k]}")
