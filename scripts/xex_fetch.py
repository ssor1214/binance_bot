"""거래소 간 가격차 수집기 — 공개 엔드포인트 전용(인증 0회).

같은 무기한 선물의 1분봉 종가를 여러 거래소에서 받아 같은 시간축에 올린다.
차익 측정([xex_lab.py](xex_lab.py))의 입력이다.

* 전부 공개 REST 다. API 키를 쓰지 않고 주문 기능이 없다.
* 라이브 봇과 무관하므로 IP 밴 위험이 없다(CLAUDE.md 판정 시 참고 참조).
* 심볼 명명이 거래소마다 다르다: Binance `BTCUSDT` / Bybit `BTCUSDT` /
  OKX `BTC-USDT-SWAP`. 아래 `to_native` 가 변환한다.
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

VENUES = {
    # name: (url_builder, parser)  — 전부 1분봉, 종가와 open_time(ms) 만 쓴다
    "binance": lambda s, st, lim:
        f"https://fapi.binance.com/fapi/v1/klines?symbol={s}&interval=1m&startTime={st}&limit={lim}",
    "bybit": lambda s, st, lim:
        f"https://api.bybit.com/v5/market/kline?category=linear&symbol={s}&interval=1&start={st}&limit={min(lim,1000)}",
    "okx": lambda s, st, lim:
        f"https://www.okx.com/api/v5/market/history-candles?instId={s}&bar=1m&before={st}&limit={min(lim,100)}",
}


def to_native(sym, venue):
    if venue in ("binance", "bybit"):
        return sym
    if venue == "okx":
        base = sym[:-4] if sym.endswith("USDT") else sym
        return f"{base}-USDT-SWAP"
    return sym


def get(url, tries=3):
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "edge-research/1.0"})
            with urllib.request.urlopen(req, timeout=20) as r:
                return json.loads(r.read().decode())
        except Exception as e:
            if i == tries - 1:
                return {"__err__": str(e)}
            time.sleep(1.0 + i)
    return None


def parse(venue, js):
    """-> [(open_time_ms, close)] 오름차순."""
    out = []
    if not isinstance(js, dict) and venue == "binance":
        for k in js:
            out.append((int(k[0]), float(k[4])))
        return out
    if venue == "binance":
        return out
    if venue == "bybit":
        for k in (js.get("result") or {}).get("list") or []:
            out.append((int(k[0]), float(k[4])))
        return sorted(out)
    if venue == "okx":
        for k in js.get("data") or []:
            out.append((int(k[0]), float(k[4])))
        return sorted(out)
    return out


def fetch_symbol(sym, venue, start_ms, end_ms, sleep):
    """1분봉을 페이지네이션으로 모은다."""
    rows, cur, calls = [], start_ms, 0
    while cur < end_ms and calls < 400:
        url = VENUES[venue](to_native(sym, venue), cur, 1000)
        js = get(url)
        calls += 1
        if js is None or (isinstance(js, dict) and "__err__" in js):
            break
        got = parse(venue, js)
        if not got:
            break
        got = [g for g in got if start_ms <= g[0] < end_ms]
        if not got:
            break
        rows.extend(got)
        nxt = max(g[0] for g in got) + 60_000
        if nxt <= cur:
            break
        cur = nxt
        time.sleep(sleep)
    d = {}
    for t, c in rows:
        d[t] = c
    return sorted(d.items()), calls


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--venues", default="binance,bybit")
    p.add_argument("--symbols", default="", help="비우면 ws_worker_cache.json 상위 N")
    p.add_argument("--top", type=int, default=25)
    p.add_argument("--days", type=float, default=7.0)
    p.add_argument("--sleep", type=float, default=0.25)
    p.add_argument("--out", default="scratch_xex_1m.npz")
    a = p.parse_args()

    if a.symbols:
        syms = [s.strip().upper() for s in a.symbols.split(",") if s.strip()]
    else:
        cache = json.load(open(ROOT / "logs/ws_worker_cache.json", encoding="utf-8"))
        syms = [s for s in (cache.get("symbols") or cache.get("universe") or [])][:a.top]
    venues = [v.strip() for v in a.venues.split(",") if v.strip() in VENUES]
    end_ms = int(time.time() // 60 * 60 * 1000)
    start_ms = end_ms - int(a.days * 86400_000)
    print(f"{len(syms)}심볼 x {len(venues)}거래소 / {a.days}일 / 1분봉")

    store, total = {}, 0
    for i, s in enumerate(syms, 1):
        ok = True
        got = {}
        for v in venues:
            rows, calls = fetch_symbol(s, v, start_ms, end_ms, a.sleep)
            total += calls
            if len(rows) < 100:
                ok = False
                break
            got[v] = rows
        if not ok:
            print(f"[{i}/{len(syms)}] {s} SKIP (일부 거래소에 없음)")
            continue
        for v, rows in got.items():
            store[f"{s}|{v}"] = np.array(rows, dtype=np.float64)
        print(f"[{i}/{len(syms)}] {s} " + " ".join(f"{v}={len(got[v])}" for v in venues)
              + f" calls={total}")
    if not store:
        raise SystemExit("[중단] 수집된 심볼 없음")
    store["__meta__"] = np.array([",".join(venues), str(a.days), str(len(syms))])
    np.savez_compressed(ROOT / a.out, **store)
    print(f"saved {ROOT / a.out}  pairs={len(store) - 1}  TOTAL_CALLS={total}")


if __name__ == "__main__":
    main()
