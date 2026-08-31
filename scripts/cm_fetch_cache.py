"""CM 백테스트용 봉 캐시 수집기 (공개 엔드포인트 전용).

- 인증 엔드포인트 호출 없음 (API 키 미사용) -> 라이브 봇 키와 겹치지 않는다.
- 호출 간 0.25초 sleep, 총 호출수를 로그로 남긴다.
- 3분봉 7일 + 4시간봉 300개 (EMA200 HTF 필터용)
"""
import json, time, sys, urllib.request, pathlib
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

BASE = "https://fapi.binance.com/fapi/v1/klines"
OUT = pathlib.Path(__file__).resolve().parent.parent / "scratch_cm_klines.json"
SLEEP = 0.25
calls = 0


def get(symbol, interval, limit, end=None):
    global calls
    url = f"{BASE}?symbol={symbol}&interval={interval}&limit={limit}"
    if end:
        url += f"&endTime={end}"
    for attempt in range(5):
        try:
            with urllib.request.urlopen(url, timeout=20) as r:
                calls += 1
                time.sleep(SLEEP)
                return json.loads(r.read())
        except Exception as e:
            print(f"  retry {symbol} {interval}: {e}", flush=True)
            time.sleep(2 + 3 * attempt)
    return []


def rows(k):
    # [open_time_ms, open, high, low, close, volume]
    return [[int(x[0]), float(x[1]), float(x[2]), float(x[3]), float(x[4]), float(x[5])] for x in k]


def fetch_3m(symbol, want):
    out, end = [], None
    while len(out) < want:
        k = get(symbol, "3m", 1500, end)
        if not k:
            break
        r = rows(k)
        out = r + out
        end = r[0][0] - 1
        if len(r) < 1500:
            break
    # 중복 제거 + 정렬
    d = {x[0]: x for x in out}
    return [d[t] for t in sorted(d)]


def main():
    syms = [s for s in json.load(open("logs/ws_worker_cache.json", encoding="utf-8"))["rows_by_symbol"]
            if s.isascii() and s.endswith("USDT")]
    days = float(sys.argv[1]) if len(sys.argv) > 1 else 8.0
    want = int(days * 24 * 20)  # 3분봉 하루 480개
    cache = {"fetched_at": time.time(), "days": days, "bars3m": {}, "bars4h": {}}
    for i, s in enumerate(syms, 1):
        b3 = fetch_3m(s, want)
        b4 = rows(get(s, "4h", 300))
        if len(b3) < 500 or len(b4) < 210:
            print(f"[{i}/{len(syms)}] {s} SKIP (3m={len(b3)} 4h={len(b4)})", flush=True)
            continue
        cache["bars3m"][s] = b3
        cache["bars4h"][s] = b4
        print(f"[{i}/{len(syms)}] {s} 3m={len(b3)} 4h={len(b4)} calls={calls}", flush=True)
    cache["total_calls"] = calls
    OUT.write_text(json.dumps(cache), encoding="utf-8")
    print(f"saved {OUT} symbols={len(cache['bars3m'])} TOTAL_CALLS={calls}")


if __name__ == "__main__":
    main()
