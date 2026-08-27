"""[2026-08-19] S1 신호 재설계 오프라인 리서치 하네스.

**왜 필요한가**
V2 재배포 이후 629건 실측:
    명목당 gross +0.0024%  /  수수료 -0.0576%  /  net -0.0544%
현 PUMP_SIGNAL은 매매 우위가 수수료의 1/24이라 어떤 파라미터 조정으로도 못 넘는다.
따라서 재설계의 유일한 합격선은 **명목당 gross > 0.0576%** (왕복 수수료)다.

**데이터 출처 — API 미사용**
data.binance.vision(정적 CDN, API키/서명 불필요, fapi weight 미적용). 2026-08-11에
라이브 봇과 같은 키로 REST를 난사해 IP밴을 당한 이력이 있어 그 경로를 피한다.
  klines/1m     : 가격/거래량 (기준 시계열, 목표수익 계산용)
  bookDepth     : ±1~5% 구간 호가 깊이(약 25초 간격) -> 오더북 불균형
  metrics       : 5분 간격 OI / 상위트레이더 롱숏비 / 테이커 매수비 -> 수급

**측정 대상**
현 전략이 못 하는 것은 "진입 후 유리한 방향으로 갈 거래를 고르는 것"이다(고점 1.5% 도달
67.8%, 나머지가 손실 전량). 그래서 여기서는 진입 규칙을 만들지 않고, **각 피처가 향후
2~5분 수익률을 예측하는가**만 잰다. 예측력이 수수료 선을 못 넘으면 그 피처는 버린다.

**절대 하지 않는 것 (lookahead 차단)**
- 피처는 t 시점까지의 데이터만 사용. 목표는 t 이후 구간에서만 계산한다.
- bookDepth/metrics는 타임스탬프가 t 이하인 마지막 관측만 쓴다(미래 스냅샷 금지).

실행:
  python scripts/s1_signal_research.py --symbols ACEUSDT,GPSUSDT --days 2026-08-16,2026-08-17
"""
import argparse
import io
import json
import os
import sys
import time
import urllib.error
import urllib.request
import zipfile
from bisect import bisect_right
from collections import defaultdict

BASE = "https://data.binance.vision/data/futures/um/daily"
CACHE = os.path.join("archive", "binance_vision", "s1")
FEE_ROUNDTRIP_PCT = 0.0576  # 실측 명목당 왕복 수수료

# [2026-08-19 lookahead 발견/수정] metrics의 create_time은 5분 구간의 **시작**이다.
# 처음엔 "create_time <= 현재시각"이면 과거 데이터라고 보고 그대로 썼는데, 그러면 그 값이
# 앞으로 일어날 5분의 체결 흐름을 담고 있어 미래정보가 된다.
# 실측 진단(6심볼 3444표본): taker_ls와 '직전 5분 수익률' 상관 0.057 vs '이후 5분' 0.260.
# 6심볼 전부 이후 구간 상관이 3~6배 커서 구간 시작 표기가 확정됐다.
# 이 버그가 있을 때 taker_ls의 상하위 10% 스프레드가 +0.457%(수수료선의 8배)로 나왔고,
# 지연을 걸자 +0.0198%로 23분의 1이 되면서 5분위 단조성도 사라졌다.
# -> metrics는 반드시 create_time + METRICS_LAG_MS 이후에만 사용한다.
METRICS_LAG_MS = 5 * 60 * 1000


def fetch(kind, symbol, day, sub=""):
    name = "%s-%s-%s.zip" % (symbol, kind if not sub else sub, day)
    if kind == "klines":
        url = "%s/klines/%s/1m/%s" % (BASE, symbol, name)
    else:
        url = "%s/%s/%s/%s" % (BASE, kind, symbol, name)
    os.makedirs(CACHE, exist_ok=True)
    dest = os.path.join(CACHE, name)
    if os.path.exists(dest) and os.path.getsize(dest) > 0:
        return dest
    try:
        with urllib.request.urlopen(url, timeout=120) as r, open(dest + ".part", "wb") as fh:
            while True:
                b = r.read(1 << 16)
                if not b:
                    break
                fh.write(b)
        os.replace(dest + ".part", dest)
        time.sleep(1.0)
        return dest
    except urllib.error.HTTPError as e:
        if os.path.exists(dest + ".part"):
            os.remove(dest + ".part")
        return None if e.code == 404 else None
    except Exception:
        if os.path.exists(dest + ".part"):
            os.remove(dest + ".part")
        return None


def read_csv(path, has_header_guess):
    if not path:
        return []
    out = []
    with zipfile.ZipFile(path) as z:
        n = z.infolist()[0].filename
        with z.open(n) as fh:
            first = True
            for raw in io.TextIOWrapper(fh, encoding="utf-8"):
                if first:
                    first = False
                    if raw[0].isalpha():
                        continue
                out.append(raw.rstrip("\n").split(","))
    return out


def to_ms(s):
    return int(time.mktime(time.strptime(s, "%Y-%m-%d %H:%M:%S")) * 1000) - time.timezone * 1000


def load_klines(symbol, day):
    rows = read_csv(fetch("klines", symbol, day, sub="1m"), True)
    out = []
    for r in rows:
        try:
            out.append({"t": int(r[0]), "o": float(r[1]), "h": float(r[2]), "l": float(r[3]),
                        "c": float(r[4]), "v": float(r[5]), "qv": float(r[7]),
                        "tbv": float(r[10])})
        except (ValueError, IndexError):
            continue
    out.sort(key=lambda x: x["t"])
    return out


def load_bookdepth(symbol, day):
    """timestamp,percentage,depth,notional -> 스냅샷별 {pct: notional}"""
    rows = read_csv(fetch("bookDepth", symbol, day), True)
    snaps = defaultdict(dict)
    for r in rows:
        try:
            snaps[to_ms(r[0])][float(r[1])] = float(r[3])
        except (ValueError, IndexError):
            continue
    return sorted(snaps.items())


def load_metrics(symbol, day):
    rows = read_csv(fetch("metrics", symbol, day), True)
    out = []
    for r in rows:
        try:
            out.append((to_ms(r[0]), {
                "oi": float(r[2]), "oi_val": float(r[3]),
                "tt_cnt_ls": float(r[4]), "tt_sum_ls": float(r[5]),
                "cnt_ls": float(r[6]), "taker_ls": float(r[7]),
            }))
        except (ValueError, IndexError):
            continue
    out.sort()
    return out


def last_at_or_before(seq, t):
    """seq: [(ts, payload)] 정렬됨. t 이하 마지막 항목 (lookahead 차단)."""
    i = bisect_right([x[0] for x in seq], t) - 1
    return seq[i][1] if i >= 0 else None


def build(symbol, days, horizon_min):
    kl, bd, mt = [], [], []
    for day in days:
        kl += load_klines(symbol, day)
        bd += load_bookdepth(symbol, day)
        mt += load_metrics(symbol, day)
    bd.sort(); mt.sort()
    if len(kl) < horizon_min + 30:
        return []
    rows = []
    for i in range(20, len(kl) - horizon_min):
        k = kl[i]
        t = k["t"] + 59999  # 이 봉이 확정되는 시각까지만 정보를 쓴다
        d = last_at_or_before(bd, t)
        m = last_at_or_before(mt, t - METRICS_LAG_MS)  # lookahead 차단(상단 주석 참고)
        if not d:
            continue
        bid1 = d.get(-1.0); ask1 = d.get(1.0)
        bid5 = sum(v for p, v in d.items() if p < 0); ask5 = sum(v for p, v in d.items() if p > 0)
        if not bid1 or not ask1 or not bid5 or not ask5:
            continue
        prev = kl[i - 1]
        win = kl[max(0, i - 19):i + 1]
        vol_ma = sum(x["qv"] for x in win) / len(win)
        f = {
            "depth_imb_1": (bid1 - ask1) / (bid1 + ask1),
            "depth_imb_5": (bid5 - ask5) / (bid5 + ask5),
            "taker_buy_ratio": (k["tbv"] / k["qv"]) if k["qv"] else 0.5,
            "vol_ratio": (k["qv"] / vol_ma) if vol_ma else 1.0,
            "ret_1m": (k["c"] / prev["c"] - 1) if prev["c"] else 0.0,
        }
        if m:
            f["taker_ls"] = m["taker_ls"]
            f["tt_sum_ls"] = m["tt_sum_ls"]
        # 목표: 다음 봉 시가에 진입해 horizon_min 뒤 종가까지 (lookahead 없음)
        entry = kl[i + 1]["o"]
        exitp = kl[i + horizon_min]["c"]
        if not entry:
            continue
        f["_fwd_pct"] = (exitp / entry - 1) * 100
        f["_hi_pct"] = (max(x["h"] for x in kl[i + 1:i + 1 + horizon_min]) / entry - 1) * 100
        f["_lo_pct"] = (min(x["l"] for x in kl[i + 1:i + 1 + horizon_min]) / entry - 1) * 100
        f["_symbol"] = symbol
        rows.append(f)
    return rows


def decile_report(rows, feat, horizon_min):
    x = [r for r in rows if feat in r]
    if len(x) < 200:
        print("  %-18s 표본부족(%d)" % (feat, len(x)))
        return
    x.sort(key=lambda r: r[feat])
    n = len(x) // 10
    lo = x[:n]; hi = x[-n:]
    fl = sum(r["_fwd_pct"] for r in lo) / len(lo)
    fh = sum(r["_fwd_pct"] for r in hi) / len(hi)
    # 상위 10%를 롱, 하위 10%를 숏으로 잡았을 때 명목당 gross
    edge = (fh - fl) / 2.0
    mark = " <-- 수수료선 통과" if abs(edge) > FEE_ROUNDTRIP_PCT else ""
    print("  %-18s n=%5d  하위10%% %+.4f%%  상위10%% %+.4f%%  스프레드/2 %+.4f%%%s"
          % (feat, len(x), fl, fh, edge, mark))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", required=True)
    ap.add_argument("--days", required=True)
    ap.add_argument("--horizon", type=int, default=3, help="목표 보유 분(기본 3분)")
    args = ap.parse_args()
    syms = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    days = [d.strip() for d in args.days.split(",") if d.strip()]
    allrows = []
    for s in syms:
        r = build(s, days, args.horizon)
        print("%-12s %5d 샘플" % (s, len(r)), flush=True)
        allrows += r
    if not allrows:
        print("샘플 없음"); return 1
    print("\n총 %d 샘플 / 목표구간 %d분 / 합격선 명목당 %.4f%%" % (len(allrows), args.horizon, FEE_ROUNDTRIP_PCT))
    print("\n=== 피처별 상하위 10%% 향후수익률 스프레드 ===")
    for f in ("depth_imb_1", "depth_imb_5", "taker_buy_ratio", "vol_ratio", "ret_1m", "taker_ls", "tt_sum_ls"):
        decile_report(allrows, f, args.horizon)
    fwd = sorted(r["_fwd_pct"] for r in allrows)
    print("\n참고: 무조건 롱 평균 %+.4f%% / 절대변동 중앙 %.4f%%"
          % (sum(fwd) / len(fwd), sorted(abs(v) for v in fwd)[len(fwd) // 2]))
    out = os.path.join("archive", "scratch_scripts", "s1_research_rows.json")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with io.open(out, "w", encoding="utf-8") as fh:
        json.dump(allrows[:5000], fh)
    print("샘플 저장: %s" % out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
