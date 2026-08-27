"""[2026-08-19] S1 2차 — aggTrades 초단위 체결흐름 피처로 3분/5분 지평 예측력 측정.

**배경**
1차(1분봉 + 25초 오더북 + 5분 지표, 20심볼x7일 20만샘플)에서 7개 피처가 전부 미달했다.
최대가 vol_ratio +0.0082%로 합격선(왕복 수수료 명목당 0.0576%)의 14%였다.
1분봉 taker_buy_ratio는 -0.0035%로 사실상 0이었는데, 1분 단위로 뭉개면 체결 흐름의
미세 구조가 사라진다는 가설이 남는다. 그래서 체결 원본으로 10/30/60초 창을 만든다.

**데이터** data.binance.vision aggTrades (정적 CDN, API키/서명 불필요, fapi weight 미적용).
2026-08-11 IP밴 사고 경로(fapi REST 반복호출)와 무관하다.
컬럼: agg_trade_id,price,quantity,first_trade_id,last_trade_id,transact_time,is_buyer_maker
  is_buyer_maker=true  -> 매수자가 메이커 = **테이커가 매도** (공격적 매도)
  is_buyer_maker=false -> 테이커가 매수 (공격적 매수)

**lookahead 차단**
- 피처는 각 분봉 마감시각 t 이하 체결만 사용.
- 목표는 t+1분 시가 진입 -> t+H분 종가 청산. 진입가는 klines의 시가를 쓴다.
- 1차에서 metrics의 create_time이 구간 '시작'이라 미래정보가 섞였던 사고가 있었다
  (taker_ls edge +0.457% -> 지연 적용시 +0.0198%). aggTrades는 체결 시각이 확정값이라
  같은 문제가 없지만, 창 경계는 항상 t 이하로만 잡는다.

**합격선** 명목당 gross > 0.0576% (왕복 수수료). 못 넘으면 그 피처는 버린다.

실행:
  python scripts/s1_aggtrade_features.py --symbols ACEUSDT,GPSUSDT --days 2026-08-16,2026-08-17
"""
import argparse
import io
import os
import sys
import time
import urllib.error
import urllib.request
import zipfile
from collections import defaultdict

BASE = "https://data.binance.vision/data/futures/um/daily"
CACHE_AGG = os.path.join("archive", "binance_vision", "aggTrades")
CACHE_KL = os.path.join("archive", "binance_vision", "s1")
FEE = 0.0576

MAJORS = {"BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT", "DOGEUSDT"}
MIDS = {"LINKUSDT", "AVAXUSDT", "SUIUSDT", "WLDUSDT", "ADAUSDT", "AAVEUSDT"}
ALTS = {"ACEUSDT", "GPSUSDT", "AIOUSDT", "BTWUSDT", "CYSUSDT", "TUTUSDT",
        "HUSDT", "PORTALUSDT", "VELVETUSDT", "BEATUSDT"}


def _dl(url, dest):
    if os.path.exists(dest) and os.path.getsize(dest) > 0:
        return dest
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    try:
        with urllib.request.urlopen(url, timeout=300) as r, open(dest + ".part", "wb") as fh:
            while True:
                b = r.read(1 << 18)
                if not b:
                    break
                fh.write(b)
        os.replace(dest + ".part", dest)
        time.sleep(1.0)
        return dest
    except Exception:
        if os.path.exists(dest + ".part"):
            os.remove(dest + ".part")
        return None


def get_agg(symbol, day):
    name = "%s-aggTrades-%s.zip" % (symbol, day)
    return _dl("%s/aggTrades/%s/%s" % (BASE, symbol, name),
               os.path.join(CACHE_AGG, symbol, name))


# [2026-08-19] 표본 확대(20심볼x7일)시 aggTrades 총량이 5~10GB로 추정되는데 디스크 여유가
# 11GB뿐(92% 사용)이라 전량 캐시가 위험하다. --stream이면 처리 직후 파일을 지워 peak 디스크를
# 파일 1개(최대 약 130MB) 수준으로 묶는다. 이미 받아둔 파일은 지우지 않는다.
STREAM = False
_PREEXISTING = set()


def get_kl(symbol, day):
    name = "%s-1m-%s.zip" % (symbol, day)
    return _dl("%s/klines/%s/1m/%s" % (BASE, symbol, name),
               os.path.join(CACHE_KL, name))


def load_klines(path):
    if not path:
        return []
    out = []
    with zipfile.ZipFile(path) as z:
        with z.open(z.infolist()[0].filename) as fh:
            first = True
            for raw in io.TextIOWrapper(fh, encoding="utf-8"):
                if first:
                    first = False
                    if raw[0].isalpha():
                        continue
                r = raw.split(",")
                try:
                    out.append({"t": int(r[0]), "o": float(r[1]), "c": float(r[4])})
                except (ValueError, IndexError):
                    continue
    out.sort(key=lambda x: x["t"])
    return out


def second_buckets(path, symbol):
    """체결을 1초 버킷으로 집계. {sec: [buy_qv, sell_qv, n, max_qv]}"""
    b = defaultdict(lambda: [0.0, 0.0, 0, 0.0])
    if not path:
        return b
    with zipfile.ZipFile(path) as z:
        with z.open(z.infolist()[0].filename) as fh:
            first = True
            for raw in io.TextIOWrapper(fh, encoding="utf-8"):
                if first:
                    first = False
                    if raw[0] == "a":
                        continue
                p = raw.split(",")
                if len(p) < 7:
                    continue
                try:
                    qv = float(p[1]) * float(p[2])
                    s = int(p[5]) // 1000
                except ValueError:
                    continue
                e = b[s]
                # is_buyer_maker=true -> 테이커 매도
                if p[6][0] in ("t", "T"):
                    e[1] += qv
                else:
                    e[0] += qv
                e[2] += 1
                if qv > e[3]:
                    e[3] = qv
    return b


def window(buckets, end_sec, span):
    buy = sell = 0.0
    n = 0
    mx = 0.0
    for s in range(end_sec - span + 1, end_sec + 1):
        e = buckets.get(s)
        if not e:
            continue
        buy += e[0]; sell += e[1]; n += e[2]
        if e[3] > mx:
            mx = e[3]
    return buy, sell, n, mx


def build(symbol, day, horizons):
    kl = load_klines(get_kl(symbol, day))
    if len(kl) < 40:
        return []
    agg = get_agg(symbol, day)
    bk = second_buckets(agg, symbol)
    if STREAM and agg and agg not in _PREEXISTING:
        try:
            os.remove(agg)
        except OSError:
            pass
    if not bk:
        return []
    idx = {k["t"]: i for i, k in enumerate(kl)}
    rows = []
    maxh = max(horizons)
    for i in range(20, len(kl) - maxh - 1):
        k = kl[i]
        end_sec = (k["t"] + 60000) // 1000 - 1  # 이 분봉 마지막 초 (t 이하만 사용)
        f = {}
        ok = True
        for span, tag in ((10, "10s"), (30, "30s"), (60, "60s")):
            buy, sell, n, mx = window(bk, end_sec, span)
            tot = buy + sell
            if tot <= 0:
                ok = False
                break
            f["imb_" + tag] = (buy - sell) / tot
            f["cnt_" + tag] = n
            f["big_" + tag] = mx / tot
        if not ok:
            continue
        b6, s6, n6, _ = window(bk, end_sec, 60)
        b300, s300, n300, _ = window(bk, end_sec, 300)
        if (b300 + s300) <= 0 or n300 <= 0:
            continue
        f["qv_accel"] = (b6 + s6) / ((b300 + s300) / 5.0)
        f["cnt_accel"] = n6 / (n300 / 5.0)
        f["imb_shift"] = f["imb_10s"] - f["imb_60s"]
        entry = kl[i + 1]["o"]
        if not entry:
            continue
        for h in horizons:
            f["_fwd%d" % h] = (kl[i + h]["c"] / entry - 1) * 100
        f["_sym"] = symbol
        f["_day"] = day
        rows.append(f)
    return rows


def decile(rows, feat, hkey, lab):
    x = [r for r in rows if feat in r and hkey in r]
    if len(x) < 300:
        print("    %-14s 표본부족(%d)" % (lab, len(x)))
        return
    x.sort(key=lambda r: r[feat])
    n = len(x) // 10
    lo = sum(r[hkey] for r in x[:n]) / n
    hi = sum(r[hkey] for r in x[-n:]) / n
    e = (hi - lo) / 2.0
    print("    %-14s n=%6d 하위 %+.4f%% 상위 %+.4f%% edge %+.4f%%%s"
          % (lab, len(x), lo, hi, e, "  <-- 통과" if abs(e) > FEE else ""))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", required=True)
    ap.add_argument("--days", required=True)
    ap.add_argument("--horizons", default="3,5")
    ap.add_argument("--stream", action="store_true",
                    help="처리 직후 aggTrades 파일 삭제(디스크 절약)")
    args = ap.parse_args()
    global STREAM
    STREAM = args.stream
    for root, _, files in os.walk(CACHE_AGG):
        for f in files:
            _PREEXISTING.add(os.path.join(root, f))
    syms = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    days = [d.strip() for d in args.days.split(",") if d.strip()]
    hs = [int(x) for x in args.horizons.split(",")]
    rows = []
    t0 = time.time()
    for s in syms:
        for d in days:
            r = build(s, d, hs)
            rows += r
            print("  %-12s %s  %5d 샘플  (누적 %d, %.0fs)"
                  % (s, d, len(r), len(rows), time.time() - t0), flush=True)
    if not rows:
        print("샘플 없음")
        return 1
    feats = ["imb_10s", "imb_30s", "imb_60s", "imb_shift", "qv_accel", "cnt_accel",
             "big_10s", "big_60s", "cnt_10s"]
    print("\n총 %d 샘플 / 합격선 명목당 %.4f%%" % (len(rows), FEE))
    for h in hs:
        print("\n=== 지평 %d분 ===" % h)
        for f in feats:
            decile(rows, f, "_fwd%d" % h, f)
    for h in hs:
        v = sorted(abs(r["_fwd%d" % h]) for r in rows)
        print("\n지평 %d분 절대변동 중앙 %.4f%%" % (h, v[len(v) // 2]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
