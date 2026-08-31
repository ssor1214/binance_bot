"""미결제약정(OI)·포지셔닝 지표 수집기 (공개 덤프).

klines 에 없는 정보를 쓰는 마지막 축이다. data.binance.vision 의 daily/metrics 는
5분 간격으로 다음을 준다.

    sum_open_interest              미결제약정 수량
    sum_open_interest_value        미결제약정 명목가
    count_toptrader_long_short_ratio   상위트레이더 계정수 롱/숏비
    sum_toptrader_long_short_ratio     상위트레이더 포지션 롱/숏비
    count_long_short_ratio             전체 계정수 롱/숏비
    sum_taker_long_short_vol_ratio     taker 매수/매도 거래량비

**커버리지 주의**: 상장폐지 심볼 145개 중 metrics 가 남아 있는 것은 70개(48%)뿐이고,
그 가용성 자체가 생존과 상관돼 있을 수 있다. 생존편향이 완전히 걷히지 않는다.

봉 시각에 맞춰 리샘플할 때 **그 봉 시각 이전의 마지막 관측**만 쓴다(lookahead 없음).
"""
import argparse
import concurrent.futures as cf
import datetime as dt
import io
import json
import pathlib
import sys
import urllib.request
import zipfile

import numpy as np

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
ROOT = pathlib.Path(__file__).resolve().parent.parent
URL = "https://data.binance.vision/data/futures/um/daily/metrics/{s}/{s}-metrics-{d}.zip"
COLS = ["oi", "oi_val", "tt_cnt", "tt_sum", "acct", "taker"]


def days(start, end):
    a = dt.date.fromisoformat(start)
    b = dt.date.fromisoformat(end)
    out = []
    while a <= b:
        out.append(a.isoformat())
        a += dt.timedelta(days=1)
    return out


def grab(sym, day):
    try:
        with urllib.request.urlopen(URL.format(s=sym, d=day), timeout=30) as r:
            data = r.read()
    except Exception:
        return []
    rows = []
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            with z.open(z.namelist()[0]) as f:
                for line in io.TextIOWrapper(f, encoding="utf-8"):
                    p = line.strip().split(",")
                    if len(p) < 8 or not p[0][:1].isdigit():
                        continue
                    try:
                        t = dt.datetime.strptime(p[0], "%Y-%m-%d %H:%M:%S").replace(
                            tzinfo=dt.timezone.utc).timestamp() * 1000
                        rows.append([t] + [float(x) for x in p[2:8]])
                    except ValueError:
                        continue
    except zipfile.BadZipFile:
        return []
    return rows


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--symbols-json", default="")
    p.add_argument("--from-cache", default="scratch_edge_1h_180d.npz")
    p.add_argument("--extra-json", default="scratch_delisted_coverage.json")
    p.add_argument("--start", default="2026-04-01")
    p.add_argument("--end", default="2026-07-31")
    p.add_argument("--workers", type=int, default=6)
    p.add_argument("--out", default="scratch_metrics.npz")
    a = p.parse_args()

    if a.symbols_json:
        syms = json.load(open(ROOT / a.symbols_json))
    else:
        z = np.load(ROOT / a.from_cache, allow_pickle=True)
        syms = [str(x) for x in z["__symbols__"]]
        if a.extra_json:
            extra = json.load(open(ROOT / a.extra_json))
            syms += [s for s in extra.get("metrics", []) if s not in syms]
    ds = days(a.start, a.end)
    print(f"{len(syms)}심볼 x {len(ds)}일 = {len(syms) * len(ds)}파일, 워커 {a.workers}")

    arrs, ok = {}, []
    with cf.ThreadPoolExecutor(a.workers) as ex:
        for i, s in enumerate(syms, 1):
            rows = []
            for r in ex.map(lambda d: grab(s, d), ds):
                rows += r
            if len(rows) < 500:
                print(f"[{i}/{len(syms)}] {s} SKIP (n={len(rows)})", flush=True)
                continue
            d = {r[0]: r for r in rows}
            arrs[f"{s}|metrics"] = np.asarray([d[t] for t in sorted(d)], dtype=np.float64)
            ok.append(s)
            if i % 10 == 0 or i == len(syms):
                print(f"[{i}/{len(syms)}] {s} n={len(d)}", flush=True)

    arrs["__symbols__"] = np.asarray(ok)
    arrs["__cols__"] = np.asarray(COLS)
    np.savez_compressed(ROOT / a.out, **arrs)
    print(f"saved {ROOT / a.out} symbols={len(ok)}")


if __name__ == "__main__":
    main()
