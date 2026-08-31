"""상장폐지 심볼의 펀딩 이력 수집기 (공개 덤프).

REST `/fapi/v1/fundingRate` 는 상폐 심볼을 응답하지 않는다. edge_delisted.py 와 같은
경로로 data.binance.vision 의 월별 fundingRate zip 을 받아 edge_lab 이 읽는 형식
(`SYM|funding` = [fundingTime_ms, rate])으로 저장한다.

**커버리지가 완전하지 않다.** 상폐 145심볼 중 펀딩 덤프가 있는 것은 59개(41%)뿐이고,
그 가용성 자체가 생존과 상관돼 있을 수 있다. 리포트에 반드시 명시할 것.
"""
import argparse, io, json, pathlib, sys, time, urllib.request, zipfile
import numpy as np

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
ROOT = pathlib.Path(__file__).resolve().parent.parent
URL = "https://data.binance.vision/data/futures/um/monthly/fundingRate/{s}/{s}-fundingRate-{m}.zip"


def months(start, end):
    y, m = int(start[:4]), int(start[5:7])
    ey, em = int(end[:4]), int(end[5:7])
    out = []
    while (y, m) <= (ey, em):
        out.append(f"{y:04d}-{m:02d}")
        m += 1
        if m == 13:
            y, m = y + 1, 1
    return out


def grab(sym, month, sleep):
    try:
        with urllib.request.urlopen(URL.format(s=sym, m=month), timeout=40) as r:
            data = r.read()
    except Exception:
        return []
    time.sleep(sleep)
    rows = []
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        with z.open(z.namelist()[0]) as f:
            for line in io.TextIOWrapper(f, encoding="utf-8"):
                p = line.strip().split(",")
                if not p or not p[0] or p[0][0] not in "0123456789":
                    continue
                try:
                    rows.append([float(p[0]), float(p[-1])])
                except ValueError:
                    continue
    return rows


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--symbols-json", default="scratch_delisted_coverage.json")
    p.add_argument("--key", default="funding")
    p.add_argument("--start", default="2026-02")
    p.add_argument("--end", default="2026-07")
    p.add_argument("--sleep", type=float, default=0.05)
    p.add_argument("--out", default="scratch_funding_delisted.npz")
    a = p.parse_args()

    src = json.load(open(ROOT / a.symbols_json))
    syms = src[a.key] if isinstance(src, dict) else src
    ms = months(a.start, a.end)
    arrs, ok = {}, []
    for i, s in enumerate(syms, 1):
        rows = []
        for m in ms:
            rows += grab(s, m, a.sleep)
        if len(rows) < 20:
            print(f"[{i}/{len(syms)}] {s} SKIP (n={len(rows)})", flush=True)
            continue
        d = {r[0]: r for r in rows}
        arrs[f"{s}|funding"] = np.asarray([d[t] for t in sorted(d)], dtype=np.float64)
        ok.append(s)
        print(f"[{i}/{len(syms)}] {s} n={len(d)}", flush=True)
    arrs["__symbols__"] = np.asarray(ok)
    np.savez_compressed(ROOT / a.out, **arrs)
    print(f"saved {ROOT / a.out} symbols={len(ok)}")


if __name__ == "__main__":
    main()
