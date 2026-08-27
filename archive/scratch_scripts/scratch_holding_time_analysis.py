"""[2026-08-14 사용자요청] logs/trade_ledger.jsonl(origin=bot) 전체를 이용해 보유시간과
승률/손익의 관계를 분석. 순수 데이터 집계만 — 커스텀 진입로직/라이브 코드 수정 없음.
"""
from __future__ import annotations

import json
import statistics
from pathlib import Path
from collections import defaultdict, Counter

LEDGER_PATH = Path("logs/trade_ledger.jsonl")

BUCKETS = [
    (0, 2, "0-2min"),
    (2, 5, "2-5min"),
    (5, 10, "5-10min"),
    (10, 20, "10-20min"),
    (20, 40, "20-40min"),
    (40, float("inf"), "40min+"),
]


def load_trades() -> list[dict]:
    rows = []
    with open(LEDGER_PATH, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except Exception:
                continue
            if r.get("origin", "bot") != "bot":
                continue
            if r.get("held_seconds") is None:
                continue
            r["held_min"] = r["held_seconds"] / 60.0
            rows.append(r)
    return rows


def bucket_of(held_min: float) -> str:
    for lo, hi, name in BUCKETS:
        if lo <= held_min < hi:
            return name
    return BUCKETS[-1][2]


def pct(x):
    return f"{x*100:.1f}%"


def main():
    trades = load_trades()
    print(f"총 분석대상: {len(trades)}건\n")

    # === 1. 보유시간 구간별 집계 ===
    print("=== 1. 보유시간 구간별 승률/손익 ===")
    header = f"{'구간':>10} | {'거래수':>6} | {'승률':>7} | {'평균순ROE%':>10} | {'평균PnL(USDT)':>13} | {'총PnL':>9}"
    print(header)
    print("-" * len(header))
    bucket_rows = defaultdict(list)
    for r in trades:
        bucket_rows[bucket_of(r["held_min"])].append(r)

    for lo, hi, name in BUCKETS:
        rs = bucket_rows.get(name, [])
        if not rs:
            print(f"{name:>10} | {0:>6} | {'-':>7} | {'-':>10} | {'-':>13} | {'-':>9}")
            continue
        wins = [r for r in rs if (r.get("estimated_pnl_usdt") or 0) > 0]
        wr = len(wins) / len(rs)
        avg_roe = statistics.mean(r.get("estimated_pnl_pct") or 0 for r in rs)
        avg_pnl = statistics.mean(r.get("estimated_pnl_usdt") or 0 for r in rs)
        total_pnl = sum(r.get("estimated_pnl_usdt") or 0 for r in rs)
        print(f"{name:>10} | {len(rs):>6} | {pct(wr):>7} | {avg_roe:>+10.2f} | {avg_pnl:>+13.4f} | {total_pnl:>+9.2f}")

    # === 2. exit_reason별 보유시간 분포 ===
    print("\n=== 2. 청산사유별 보유시간 분포 ===")
    header2 = f"{'exit_reason':>26} | {'건수':>5} | {'승률':>7} | {'평균보유(분)':>10} | {'중앙값(분)':>9} | {'p90(분)':>8} | {'총PnL':>9}"
    print(header2)
    print("-" * len(header2))
    by_reason = defaultdict(list)
    for r in trades:
        by_reason[r.get("exit_reason") or "UNKNOWN"].append(r)
    for reason, rs in sorted(by_reason.items(), key=lambda kv: -len(kv[1])):
        holds = sorted(r["held_min"] for r in rs)
        wins = [r for r in rs if (r.get("estimated_pnl_usdt") or 0) > 0]
        wr = len(wins) / len(rs)
        avg_hold = statistics.mean(holds)
        med_hold = statistics.median(holds)
        p90 = holds[int(len(holds) * 0.9)] if len(holds) > 1 else holds[0]
        total_pnl = sum(r.get("estimated_pnl_usdt") or 0 for r in rs)
        print(f"{reason:>26} | {len(rs):>5} | {pct(wr):>7} | {avg_hold:>10.2f} | {med_hold:>9.2f} | {p90:>8.2f} | {total_pnl:>+9.2f}")

    # === 3. 세밀한(1분 단위) 문턱 탐색: N분 이상 보유 시 승률 ===
    print("\n=== 3. 'N분 이상 보유' 누적 승률 문턱 탐색 (1분 단위, N=1..60) ===")
    print(f"{'N(분)':>6} | {'N분이상 거래수':>13} | {'N분이상 승률':>12} | {'N분미만 승률':>12} | {'N분이상 총PnL':>13}")
    thresholds = list(range(1, 61))
    best = None
    for n in thresholds:
        over = [r for r in trades if r["held_min"] >= n]
        under = [r for r in trades if r["held_min"] < n]
        if len(over) < 15 or len(under) < 15:
            continue
        wr_over = len([r for r in over if (r.get("estimated_pnl_usdt") or 0) > 0]) / len(over)
        wr_under = len([r for r in under if (r.get("estimated_pnl_usdt") or 0) > 0]) / len(under)
        total_over = sum(r.get("estimated_pnl_usdt") or 0 for r in over)
        if n % 2 == 0 or n <= 15:
            print(f"{n:>6} | {len(over):>13} | {pct(wr_over):>12} | {pct(wr_under):>12} | {total_over:>+13.2f}")
        gap = wr_under - wr_over
        if best is None or gap > best[0]:
            best = (gap, n, wr_over, wr_under, len(over))

    if best:
        gap, n, wr_over, wr_under, cnt = best
        print(f"\n가장 큰 승률 갭 지점: N={n}분 (미만 승률 {pct(wr_under)} vs 이상 승률 {pct(wr_over)}, 갭 {pct(gap)}, N분이상 표본 {cnt}건)")

    return trades, bucket_rows, by_reason


if __name__ == "__main__":
    main()
