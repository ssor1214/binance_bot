"""엣지 측정용 봉 캐시 수집기 (공개 엔드포인트 전용, npz 저장).

cm_fetch_cache.py 의 후속. 두 가지가 다르다.
  1) **장기간**을 받는다. 9.4일 한 국면짜리 캐시로는 "국면이 바뀌면 다른 결론인가"에
     답할 수 없다는 것이 HANDOFF_2026-08-28 의 명시된 한계였다.
  2) JSON 대신 **npz(float64 배열)** 로 저장한다. 30일 x 84심볼 3분봉은 JSON 이면
     100MB 넘고 파이썬 객체로 올리면 GB 단위가 된다. npz 는 ~90MB / 로드 즉시.

안전장치 (backtest-ip-ban-incident 재발 방지):
  - 인증 엔드포인트 호출 0회, API 키 미사용.
  - klines 는 호출당 weight 10, 분당 한도 2400. SLEEP 0.35 -> 약 1,700/분으로 여유를 둔다.
  - 라이브 봇이 떠 있으면 그쪽 REST 와 합산되므로, 실행 전 프로세스 유무를 확인할 것.
"""
import json, time, sys, argparse, pathlib, urllib.request
import numpy as np

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
ROOT = pathlib.Path(__file__).resolve().parent.parent
BASE = "https://fapi.binance.com/fapi/v1/klines"
calls = 0


def get(symbol, interval, limit, end, sleep):
    global calls
    url = f"{BASE}?symbol={symbol}&interval={interval}&limit={limit}"
    if end:
        url += f"&endTime={end}"
    for attempt in range(5):
        try:
            with urllib.request.urlopen(url, timeout=25) as r:
                calls += 1
                time.sleep(sleep)
                return json.loads(r.read())
        except Exception as e:
            print(f"  retry {symbol} {interval}: {e}", flush=True)
            time.sleep(3 + 4 * attempt)
    return []


def rows(k, with_buyvol=False):
    """바이낸스 kline 응답 -> 행렬. with_buyvol=True 면 컬럼 6 에
    taker_buy_base_volume(원본 인덱스 9)을 추가한다 — 매수/매도 체결량 분리용."""
    if with_buyvol:
        return [[float(x[0]), float(x[1]), float(x[2]), float(x[3]), float(x[4]),
                 float(x[5]), float(x[9])] for x in k]
    return [[float(x[0]), float(x[1]), float(x[2]), float(x[3]), float(x[4]), float(x[5])] for x in k]


def fetch_back(symbol, interval, want, sleep, with_buyvol=False):
    """endTime 을 뒤로 밀며 want 개가 찰 때까지 과거로 긁는다."""
    out, end = [], None
    while len(out) < want:
        k = get(symbol, interval, 1500, end, sleep)
        if not k:
            break
        r = rows(k, with_buyvol=with_buyvol)
        out = r + out
        end = int(r[0][0]) - 1
        if len(r) < 1500:
            break
    d = {x[0]: x for x in out}
    return [d[t] for t in sorted(d)]


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--days", type=float, default=30.0)
    p.add_argument("--interval", default="3m")
    p.add_argument("--sleep", type=float, default=0.35)
    p.add_argument("--out", default="scratch_edge_3m_30d.npz")
    p.add_argument("--symbols", default="")
    p.add_argument("--symbols-file", default="", help="한 줄/쉼표 구분 심볼 목록 파일")
    p.add_argument("--min-frac", type=float, default=0.9,
                   help="요청 봉수 대비 이 비율 미만이면 건너뛴다")
    p.add_argument("--with-buyvol", action="store_true",
                   help="컬럼 6 에 taker_buy_base_volume 추가(매수/매도 체결량 분리용)")
    a = p.parse_args()

    if a.symbols_file:
        raw = pathlib.Path(a.symbols_file).read_text(encoding="utf-8")
        syms = [x.strip().upper() for x in raw.replace(",", " ").split() if x.strip()]
    elif a.symbols:
        syms = [s.strip().upper() for s in a.symbols.split(",") if s.strip()]
    else:
        cache = json.load(open(ROOT / "logs/ws_worker_cache.json", encoding="utf-8"))
        syms = [s for s in cache["rows_by_symbol"] if s.isascii() and s.endswith("USDT")]
    per_day = 1440 / int(a.interval.rstrip("mh")) / (60 if a.interval.endswith("h") else 1)
    want = int(a.days * per_day)
    # 4시간 EMA200 필터용 상위봉: 200개 워밍업 + 기간
    want4h = int(a.days * 6) + 260

    arrs, ok = {}, []
    for i, s in enumerate(syms, 1):
        b = fetch_back(s, a.interval, want, a.sleep, with_buyvol=a.with_buyvol)
        b4 = fetch_back(s, "4h", want4h, a.sleep)
        if len(b) < want * a.min_frac or len(b4) < 260:
            print(f"[{i}/{len(syms)}] {s} SKIP (n={len(b)} 4h={len(b4)})", flush=True)
            continue
        arrs[f"{s}|{a.interval}"] = np.asarray(b, dtype=np.float64)
        arrs[f"{s}|4h"] = np.asarray(b4, dtype=np.float64)
        ok.append(s)
        print(f"[{i}/{len(syms)}] {s} n={len(b)} 4h={len(b4)} calls={calls}", flush=True)

    arrs["__symbols__"] = np.asarray(ok)
    arrs["__meta__"] = np.asarray([a.interval, str(a.days), str(calls), str(time.time())])
    out = ROOT / a.out
    np.savez_compressed(out, **arrs)
    print(f"saved {out} symbols={len(ok)} TOTAL_CALLS={calls}")


if __name__ == "__main__":
    main()
