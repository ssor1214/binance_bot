"""[2026-08-19] 바이낸스 공개 데이터 아카이브(data.binance.vision)에서 USDT-M 선물
aggTrades 일별 파일을 내려받아 로컬에 캐시한다.

**왜 fapi가 아니라 아카이브인가**
2026-08-11에 라이브 봇과 같은 API 키로 futures_aggregate_trades() 등을 반복 호출하다
-1003(Way too many requests)로 IP 밴을 당한 사고가 있었다. data.binance.vision은 S3/CDN
정적 파일 서버라 fapi.binance.com과 인프라가 완전히 분리돼 있고, API 키도 서명도 필요
없으며 REST weight 카운터에 잡히지 않는다 - 즉 라이브 봇의 API 예산을 전혀 쓰지 않는다.
그래도 예의상 요청 간 스로틀(기본 1.0초)을 둔다.

**무엇에 쓰는가**
detect_volume_spike()는 "최근 10초 체결대금 합 / 300초 baseline 평균 >= 3.0"만 보므로
체결 원본(aggTrades)만 있으면 라이브 판정을 그대로 재현할 수 있다. 1분봉으로는 10초
창을 만들 수 없어 근사가 불가능해서 aggTrades를 쓴다(선물 um 아카이브에는 1초봉 없음).

이미 받은 파일은 건너뛴다(재개 가능). 아카이브는 하루이틀 지연 게시되므로 최근 날짜는
404가 정상이며, 그 경우 건너뛰고 계속 진행한다.
"""
import argparse
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import date, timedelta

BASE = "https://data.binance.vision/data/futures/um/daily/aggTrades"
DEFAULT_OUT = os.path.join("archive", "binance_vision", "aggTrades")


def daterange(start: date, end: date):
    d = start
    while d <= end:
        yield d
        d += timedelta(days=1)


def fetch_one(symbol: str, d: date, out_dir: str, throttle: float, timeout: float) -> str:
    name = "%s-aggTrades-%s.zip" % (symbol, d.isoformat())
    dest_dir = os.path.join(out_dir, symbol)
    os.makedirs(dest_dir, exist_ok=True)
    dest = os.path.join(dest_dir, name)
    if os.path.exists(dest) and os.path.getsize(dest) > 0:
        return "cached"
    url = "%s/%s/%s" % (BASE, symbol, name)
    tmp = dest + ".part"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp, open(tmp, "wb") as fh:
            while True:
                chunk = resp.read(1 << 16)
                if not chunk:
                    break
                fh.write(chunk)
        os.replace(tmp, dest)
        return "ok"
    except urllib.error.HTTPError as e:
        if os.path.exists(tmp):
            os.remove(tmp)
        # 아카이브 미게시(최근 날짜) 또는 해당 심볼 미상장 구간
        return "missing" if e.code == 404 else "http%d" % e.code
    except Exception as e:
        if os.path.exists(tmp):
            os.remove(tmp)
        return "err:%s" % type(e).__name__
    finally:
        time.sleep(throttle)


def main():
    ap = argparse.ArgumentParser(description="Binance public archive aggTrades fetcher (no API key, no fapi)")
    ap.add_argument("--symbols", required=True, help="comma separated, e.g. ACEUSDT,BEATUSDT")
    ap.add_argument("--start", required=True, help="YYYY-MM-DD (UTC)")
    ap.add_argument("--end", required=True, help="YYYY-MM-DD (UTC)")
    ap.add_argument("--out", default=DEFAULT_OUT)
    ap.add_argument("--throttle", type=float, default=1.0, help="seconds between requests")
    ap.add_argument("--timeout", type=float, default=120.0)
    args = ap.parse_args()

    symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    start = date.fromisoformat(args.start)
    end = date.fromisoformat(args.end)
    days = list(daterange(start, end))
    total = len(symbols) * len(days)
    print("target %d symbols x %d days = %d files -> %s" % (len(symbols), len(days), total, args.out))
    print("source: %s (public archive, no API key, not fapi)" % BASE)

    counts = {}
    done = 0
    bytes_total = 0
    for symbol in symbols:
        for d in days:
            status = fetch_one(symbol, d, args.out, args.throttle, args.timeout)
            counts[status] = counts.get(status, 0) + 1
            done += 1
            if status in ("ok", "cached"):
                p = os.path.join(args.out, symbol, "%s-aggTrades-%s.zip" % (symbol, d.isoformat()))
                try:
                    bytes_total += os.path.getsize(p)
                except OSError:
                    pass
            if done % 10 == 0 or done == total:
                print("  %d/%d  %s  (%.1f MB)" % (done, total, counts, bytes_total / 1e6))
    print("done:", counts, "총 %.1f MB" % (bytes_total / 1e6))
    return 0


if __name__ == "__main__":
    sys.exit(main())
