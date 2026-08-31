"""상장폐지 심볼의 봉 데이터 수집기 — 생존편향 크기 측정용.

REST `/fapi/v1/klines` 는 **현재 상장된 심볼만** 응답한다. 그래서 edge_fetch.py 로
만든 표본에는 기간 중 사라진 심볼이 통째로 빠져 있고, 이것이
HANDOFF_2026-08-31 7~8장의 마지막 미해결 관문이다.

바이낸스 공개 데이터 덤프(data.binance.vision)에는 **상장폐지 심볼도 남아 있다.**
여기서 월별 klines zip 을 받아 edge_lab 이 읽는 npz 로 합친다.

- 전부 공개 CDN 이다. 인증 0회 / API 키 미사용 / fapi 레이트리밋과 무관.
- 월별 파일은 그 달이 끝난 뒤 올라온다. 진행 중인 달은 없으므로 창을 그에 맞춰 자른다.
"""
import argparse
import io
import json
import pathlib
import sys
import time
import urllib.request
import zipfile

import numpy as np

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
ROOT = pathlib.Path(__file__).resolve().parent.parent
MON = "https://data.binance.vision/data/futures/um/monthly/klines/{s}/{i}/{s}-{i}-{m}.zip"


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


def grab(sym, interval, month, sleep):
    url = MON.format(s=sym, i=interval, m=month)
    try:
        with urllib.request.urlopen(url, timeout=40) as r:
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
                    continue          # 일부 파일에 헤더 줄이 있다
                try:
                    rows.append([float(p[0]), float(p[1]), float(p[2]),
                                 float(p[3]), float(p[4]), float(p[5])])
                except ValueError:
                    continue
    return rows


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--symbols-json", default="scratch_delisted_in_window.json")
    p.add_argument("--interval", default="1h")
    p.add_argument("--start", default="2026-02")
    p.add_argument("--end", default="2026-07")
    p.add_argument("--sleep", type=float, default=0.05)
    p.add_argument("--min-bars", type=int, default=200)
    p.add_argument("--out", default="scratch_edge_delisted.npz")
    a = p.parse_args()

    syms = json.load(open(ROOT / a.symbols_json))
    ms = months(a.start, a.end)
    print(f"{len(syms)}심볼 x {len(ms)}개월 ({ms[0]}~{ms[-1]}) {a.interval}")

    arrs, ok = {}, []
    for i, s in enumerate(syms, 1):
        rows = []
        for m in ms:
            rows += grab(s, a.interval, m, a.sleep)
        if len(rows) < a.min_bars:
            print(f"[{i}/{len(syms)}] {s} SKIP (n={len(rows)})", flush=True)
            continue
        d = {r[0]: r for r in rows}
        rows = [d[t] for t in sorted(d)]
        arrs[f"{s}|{a.interval}"] = np.asarray(rows, dtype=np.float64)
        # 4시간봉은 이 용도(EMA200 HTF 필터)로만 쓰이는데 상폐 심볼엔 굳이 필요없다.
        # edge_lab 이 없으면 htf 를 NaN 으로 두도록 빈 배열을 넣는다.
        arrs[f"{s}|4h"] = np.zeros((0, 6), dtype=np.float64)
        ok.append(s)
        print(f"[{i}/{len(syms)}] {s} n={len(rows)}", flush=True)

    arrs["__symbols__"] = np.asarray(ok)
    arrs["__meta__"] = np.asarray([a.interval, "delisted", str(len(ok)), str(time.time())])
    out = ROOT / a.out
    np.savez_compressed(out, **arrs)
    print(f"saved {out} symbols={len(ok)}")


if __name__ == "__main__":
    main()
