"""V2 재배포 이후 1시간 단위 성과표 (2026-08-18 사용자요청).

"코덱스 v2 재배포 이후 한시간단위로 전체 거래수/승률/순익, 롱·숏 거래수/승률/순익을
요약하지 않고" 매시 브리핑에 넣기 위한 스크립트다. 매번 즉석 코드로 뽑으면 기준이
흔들리므로 고정한다.

기준 시각: V2 재배포 = 2026-08-17 01:49:05 (기본값, --since로 변경 가능)
집계: origin=bot만, net_realized_usdt 기준. 미보정 거래는 제외하고 건수를 병기한다.
      실행 전에 `scripts/reconcile_realized_pnl.py --hours N --write`로 보정할 것.

주의: 시간이 지날수록 행이 계속 늘어난다. 요약하지 말라는 요청이라 전 구간을 출력한다.
      필요하면 --since로 시작점을 옮긴다.
"""
from __future__ import annotations

import argparse
import json
import time
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LEDGER = ROOT / "logs" / "trade_ledger.jsonl"
PNL_KEY = "net_realized_usdt"
V2_DEPLOY = "2026-08-17 01:49:05"


def stats(rows: list[dict]):
    if not rows:
        return (0, None, 0.0)
    wins = [t for t in rows if t[PNL_KEY] > 0]
    return (len(rows), 100 * len(wins) / len(rows), sum(t[PNL_KEY] for t in rows))


def fmt_wr(v) -> str:
    return "-" if v is None else "%.1f%%" % v


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", default=V2_DEPLOY, help='"YYYY-MM-DD HH:MM:SS"')
    ap.add_argument("--markdown", action="store_true", help="마크다운 표로 출력")
    args = ap.parse_args()
    start = time.mktime(time.strptime(args.since, "%Y-%m-%d %H:%M:%S"))

    rows = []
    with open(LEDGER, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    pool_all = [t for t in rows if t.get("origin") == "bot" and (t.get("exited_at") or 0) >= start]
    pool = [t for t in pool_all if t.get(PNL_KEY) is not None]
    if not pool:
        print("대상 거래 없음")
        return

    buckets: dict[tuple, list[dict]] = defaultdict(list)
    for t in pool:
        lt = time.localtime(t["exited_at"])
        buckets[(lt.tm_mon, lt.tm_mday, lt.tm_hour)].append(t)

    elapsed_h = (time.time() - start) / 3600
    print("기준 %s 이후  총 %d건 (실현손익 보정 %d건 / 미보정 %d건 제외)"
          % (args.since, len(pool_all), len(pool), len(pool_all) - len(pool)))
    print("경과 %.2f시간 / 시간당 %.2f건" % (elapsed_h, len(pool) / elapsed_h if elapsed_h else 0))
    print()

    if args.markdown:
        print("| 시각 | 거래 | 승률 | 순익 | 롱 | 롱승률 | 롱순익 | 숏 | 숏승률 | 숏순익 | 누적 |")
        print("|---|---|---|---|---|---|---|---|---|---|---|")
    else:
        print("%-12s %5s %8s %9s | %4s %8s %9s | %4s %8s %9s | %9s"
              % ("시각", "거래", "승률", "순익", "롱", "롱승률", "롱순익", "숏", "숏승률", "숏순익", "누적"))

    cum = 0.0
    for key in sorted(buckets):
        x = buckets[key]
        n, wr, pnl = stats(x)
        cum += pnl
        ln, lwr, lp = stats([t for t in x if t.get("side") == "LONG"])
        sn, swr, sp = stats([t for t in x if t.get("side") == "SHORT"])
        label = "%02d/%02d %02d시" % (key[0], key[1], key[2])
        if args.markdown:
            print("| %s | %d | %s | %+.3f | %d | %s | %+.3f | %d | %s | %+.3f | %+.3f |"
                  % (label, n, fmt_wr(wr), pnl, ln, fmt_wr(lwr), lp, sn, fmt_wr(swr), sp, cum))
        else:
            print("%-12s %5d %8s %+9.3f | %4d %8s %+9.3f | %4d %8s %+9.3f | %+9.3f"
                  % (label, n, fmt_wr(wr), pnl, ln, fmt_wr(lwr), lp, sn, fmt_wr(swr), sp, cum))

    n, wr, pnl = stats(pool)
    ln, lwr, lp = stats([t for t in pool if t.get("side") == "LONG"])
    sn, swr, sp = stats([t for t in pool if t.get("side") == "SHORT"])
    if args.markdown:
        print("| **합계** | **%d** | **%s** | **%+.3f** | **%d** | **%s** | **%+.3f** | **%d** | **%s** | **%+.3f** | |"
              % (n, fmt_wr(wr), pnl, ln, fmt_wr(lwr), lp, sn, fmt_wr(swr), sp))
    else:
        print("%-12s %5d %8s %+9.3f | %4d %8s %+9.3f | %4d %8s %+9.3f |"
              % ("합계", n, fmt_wr(wr), pnl, ln, fmt_wr(lwr), lp, sn, fmt_wr(swr), sp))


if __name__ == "__main__":
    main()
