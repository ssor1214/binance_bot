import re, json
from datetime import datetime

FILES = ["logs/bot.log.5","logs/bot.log.4","logs/bot.log.3","logs/bot.log.2","logs/bot.log.1","logs/bot.log"]

cand_re = re.compile(
    r"^(?P<ts>[\d\-]+ [\d:,]+) \[INFO\] bot\.main: 이번 주기 진입 후보 (?P<n>\d+)개 \(상위: \[(?P<items>.*)\], 확률/score순 정렬\)"
)
item_re = re.compile(r"\('(?P<sym>\w+)', (?:np\.float64\()?(?P<prob>[0-9.]+)\)?, (?:np\.float64\()?(?P<score>[0-9.]+)\)?\)")

lowbal_pass_re = re.compile(
    r"^(?P<ts>[\d\-]+ [\d:,]+) \[ERROR\] bot\.main: 총자산 (?P<bal>[0-9.]+) USDT 저잔고 복구모드 — 후보 (?P<n>\d+)개 중 고확률 (?P<m>\d+)개만 최대 (?P<slots>\d+)개 진입 허용"
)
lowbal_block_re = re.compile(
    r"^(?P<ts>[\d\-]+ [\d:,]+) \[ERROR\] bot\.main: 총자산 (?P<bal>[0-9.]+) USDT 저잔고 복구모드 — 후보 (?P<n>\d+)개 중 고확률 기준\(확률 (?P<minp>[0-9.]+), score (?P<mins>[0-9.]+)\) 통과 없음"
)
entered_re = re.compile(r"^(?P<ts>[\d\-]+ [\d:,]+) \[INFO\] bot\.main: \[(?P<sym>\w+)\] 저잔고 복구모드 — 고확률 후보만 진입")

records = []
pending_candidates = None
pending_line_ts = None

for fn in FILES:
    try:
        with open(fn, encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
    except FileNotFoundError:
        continue
    for i, line in enumerate(lines):
        m = cand_re.match(line)
        if m:
            items = item_re.findall(m.group("items"))
            pending_candidates = [(s, float(p), float(sc)) for s, p, sc in items]
            pending_line_ts = m.group("ts")
            continue
        m2 = lowbal_block_re.match(line)
        if m2 and pending_candidates is not None:
            records.append({
                "ts": m2.group("ts"), "bal": float(m2.group("bal")),
                "min_p": float(m2.group("minp")), "min_s": float(m2.group("mins")),
                "candidates": pending_candidates, "mode": "all_blocked",
            })
            pending_candidates = None
            continue
        m3 = lowbal_pass_re.match(line)
        if m3 and pending_candidates is not None:
            records.append({
                "ts": m3.group("ts"), "bal": float(m3.group("bal")),
                "n": int(m3.group("n")), "m": int(m3.group("m")),
                "candidates": pending_candidates, "mode": "partial",
            })
            pending_candidates = None
            continue

print(f"total low-balance-mode cycles captured: {len(records)}")

# min prob threshold used historically 0.80, min score 0.68 (per config default, may vary if set)
default_min_p = 0.80
default_min_s = 0.68

blocked_candidates = []  # candidates that were present but didn't pass score/prob gate
for r in records:
    minp = r.get("min_p", default_min_p)
    mins = r.get("min_s", default_min_s)
    for sym, prob, score in r["candidates"]:
        passed = prob >= minp and score >= mins
        if not passed:
            blocked_candidates.append({
                "ts": r["ts"], "symbol": sym, "prob": prob, "score": score,
                "min_p": minp, "min_s": mins, "bal": r["bal"],
                "reason": "prob" if prob < minp else "score",
            })

print(f"total candidate instances (in low-bal cycles): {sum(len(r['candidates']) for r in records)}")
print(f"blocked candidate instances: {len(blocked_candidates)}")

score_only_blocked = [b for b in blocked_candidates if b["reason"] == "score" and b["prob"] >= b["min_p"]]
print(f"blocked ONLY by score (prob was fine): {len(score_only_blocked)}")

with open("scratch_blocked_candidates.json", "w", encoding="utf-8") as f:
    json.dump(blocked_candidates, f, indent=2, ensure_ascii=False)

# distribution of scores among score-only-blocked
import collections
buckets = collections.Counter()
for b in score_only_blocked:
    s = b["score"]
    if s >= 0.65: buckets["0.65-0.68"] += 1
    elif s >= 0.60: buckets["0.60-0.65"] += 1
    elif s >= 0.55: buckets["0.55-0.60"] += 1
    elif s >= 0.50: buckets["0.50-0.55"] += 1
    else: buckets["<0.50"] += 1
print("score buckets (score-only-blocked):", dict(buckets))

for b in score_only_blocked[:60]:
    print(b["ts"], b["symbol"], "prob=%.2f"%b["prob"], "score=%.2f"%b["score"])
