import json
from datetime import datetime, timedelta
import time

with open("scratch_blocked_candidates.json", encoding="utf-8") as f:
    blocked = json.load(f)

# dedupe: group by symbol, collapse entries within 5 min of each other into one "opportunity"
# keep the earliest ts and max score/prob seen in the group
def parse_ts(s):
    return datetime.strptime(s.split(",")[0], "%Y-%m-%d %H:%M:%S")

blocked.sort(key=lambda b: (b["symbol"], parse_ts(b["ts"])))
groups = []
cur = None
for b in blocked:
    t = parse_ts(b["ts"])
    if cur and cur["symbol"] == b["symbol"] and (t - cur["last_ts"]).total_seconds() <= 300:
        cur["last_ts"] = t
        cur["count"] += 1
        cur["max_score"] = max(cur["max_score"], b["score"])
        cur["max_prob"] = max(cur["max_prob"], b["prob"])
    else:
        if cur:
            groups.append(cur)
        cur = {"symbol": b["symbol"], "first_ts": t, "last_ts": t, "count": 1,
               "max_score": b["score"], "max_prob": b["prob"], "min_score": b["score"]}
if cur:
    groups.append(cur)

print(f"deduped opportunities (score-blocked): {len(groups)}")

# load ledger
ledger = []
with open("logs/trade_ledger.jsonl", encoding="utf-8") as f:
    for line in f:
        line = line.strip()
        if not line:
            continue
        try:
            ledger.append(json.loads(line))
        except Exception:
            pass

print(f"ledger entries: {len(ledger)}")

matched = []
unmatched = []
for g in groups:
    sym = g["symbol"]
    win_start = g["first_ts"] - timedelta(minutes=15)
    win_end = g["last_ts"] + timedelta(minutes=90)  # entry could happen later once slot frees / balance recovers
    best = None
    for t in ledger:
        if t["symbol"] != sym:
            continue
        ent = datetime.fromtimestamp(t["entered_at"])
        if win_start <= ent <= win_end:
            if best is None or abs((ent - g["first_ts"]).total_seconds()) < abs((datetime.fromtimestamp(best["entered_at"]) - g["first_ts"]).total_seconds()):
                best = t
    if best:
        matched.append({**g, "first_ts": str(g["first_ts"]), "last_ts": str(g["last_ts"]), "ledger": best})
    else:
        unmatched.append({**g, "first_ts": str(g["first_ts"]), "last_ts": str(g["last_ts"])})

print(f"matched to a real ledger trade (same symbol, +/-window): {len(matched)}")
print(f"unmatched (no nearby real trade for that symbol): {len(unmatched)}")

with open("scratch_matched.json", "w", encoding="utf-8") as f:
    json.dump(matched, f, indent=2, ensure_ascii=False)
with open("scratch_unmatched.json", "w", encoding="utf-8") as f:
    json.dump(unmatched, f, indent=2, ensure_ascii=False)

wins = [m for m in matched if m["ledger"]["estimated_pnl_usdt"] > 0]
losses = [m for m in matched if m["ledger"]["estimated_pnl_usdt"] <= 0]
print(f"matched wins: {len(wins)}, losses: {len(losses)}, winrate: {len(wins)/len(matched)*100:.1f}%" if matched else "no matches")
total_pnl = sum(m["ledger"]["estimated_pnl_usdt"] for m in matched)
print(f"total pnl of matched trades (USDT, at their actual position size): {total_pnl:.2f}")
avg_pnl = total_pnl / len(matched) if matched else 0
print(f"avg pnl per matched trade: {avg_pnl:.3f}")
