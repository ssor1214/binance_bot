"""펀딩비 이력 수집기 (공개 엔드포인트 전용).

8~24시간 보유 전략에서 펀딩비는 무시할 수 없다. 8시간마다(일부 심볼은 4시간마다)
정산되므로 24시간 보유면 3회, 즉 엣지와 같은 자릿수가 될 수 있다.

- `/fapi/v1/fundingRate` 는 **공개** 엔드포인트다. 인증 0회 / API 키 미사용.
- 심볼당 limit=1000 (180일 x 8시간 = 540건이면 1~2회 호출로 끝난다).
- 부호 규약: fundingRate > 0 이면 **롱이 숏에게 준다**. 따라서
  롱의 펀딩손익 = -rate, 숏의 펀딩손익 = +rate.
"""
import argparse
import json
import pathlib
import sys
import time
import urllib.request

import numpy as np

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
ROOT = pathlib.Path(__file__).resolve().parent.parent
BASE = "https://fapi.binance.com/fapi/v1/fundingRate"
calls = 0


def get(symbol, start, sleep):
    global calls
    url = f"{BASE}?symbol={symbol}&limit=1000"
    if start:
        url += f"&startTime={int(start)}"
    for attempt in range(5):
        try:
            with urllib.request.urlopen(url, timeout=25) as r:
                calls += 1
                time.sleep(sleep)
                return json.loads(r.read())
        except Exception as e:
            print(f"  retry {symbol}: {e}", flush=True)
            time.sleep(3 + 4 * attempt)
    return []


def fetch(symbol, start, sleep):
    """startTime 부터 앞으로 훑는다. 1000건씩이라 180일이면 보통 1회."""
    out, cur = [], start
    while True:
        k = get(symbol, cur, sleep)
        if not k:
            break
        rows = [[float(x["fundingTime"]), float(x["fundingRate"])] for x in k]
        out += rows
        if len(k) < 1000:
            break
        cur = rows[-1][0] + 1
    d = {r[0]: r for r in out}
    return [d[t] for t in sorted(d)]


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--from-cache", default="scratch_edge_1h_180d.npz",
                   help="이 npz 의 심볼/기간에 맞춰 받는다")
    p.add_argument("--interval", default="1h")
    p.add_argument("--sleep", type=float, default=0.35)
    p.add_argument("--out", default="scratch_funding.npz")
    a = p.parse_args()

    z = np.load(ROOT / a.from_cache, allow_pickle=True)
    syms = [str(x) for x in z["__symbols__"]]
    start = min(float(z[f"{s}|{a.interval}"][0, 0]) for s in syms) - 86400_000

    arrs, tot = {}, 0
    for i, s in enumerate(syms, 1):
        rows = fetch(s, start, a.sleep)
        if not rows:
            print(f"[{i}/{len(syms)}] {s} 펀딩 이력 없음", flush=True)
            continue
        arrs[f"{s}|funding"] = np.asarray(rows, dtype=np.float64)
        tot += len(rows)
        span_h = (rows[-1][0] - rows[0][0]) / 3600_000 / max(len(rows) - 1, 1)
        print(f"[{i}/{len(syms)}] {s} n={len(rows)} 주기~{span_h:.1f}h "
              f"평균{np.mean([r[1] for r in rows]) * 100:+.4f}% calls={calls}", flush=True)
    arrs["__symbols__"] = np.asarray([s for s in syms if f"{s}|funding" in arrs])
    out = ROOT / a.out
    np.savez_compressed(out, **arrs)
    print(f"saved {out} symbols={len(arrs) - 1} rows={tot} TOTAL_CALLS={calls}")


if __name__ == "__main__":
    main()
