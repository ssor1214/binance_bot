"""종이 검증(forward test) — 횡단면 모멘텀 20봉 / 24시간 보유.

HANDOFF_2026-08-31 12장에서 처음으로 세 관문을 넘은 후보를 **안 본 데이터**로 검증한다.
과거 4.1년은 전부 in-sample 이다(그 데이터를 보면서 신호를 골랐다). 남은 진짜 검증은
과거에 없고 앞으로 나올 데이터뿐이며, 그건 자산이 아니라 시간만 있으면 된다.

## 규칙 (2026-08-31 동결 — 이 파일의 커밋 시각이 동결 증거다)

    유니버스 : 스프레드 0.010% 이하 유동 19심볼 (`--universe`)
    신호     : 20시간 수익률의 횡단면 상대강도(전체 중앙값 차감)
               상위 20% 롱 / 하위 20% 숏
    진입     : 신호봉 **다음 봉 시가**        (lookahead 없음)
    청산     : 진입 24봉 뒤 **종가**
    표본추출 : 매일 00:00 UTC 봉 마감 시 1회 (stride=24 와 동일, 보유구간 겹침 없음)
    변형     : all19 = 19심볼 전부 / core13 = 2022-07 부터 존재한 13심볼만

**이 규칙을 나중에 바꾸지 않는다.** 바꾸면 그 시점부터 다시 out-of-sample 이 아니다.

## 왜 상주 프로세스가 아닌가

봉 데이터는 나중에도 그대로 받을 수 있으므로, 신호를 실시간으로 잡아둘 필요가 없다.
**규칙을 동결한 시각**만 증명되면 되고 그건 커밋이 해준다. 그래서 이 스크립트는
아무 때나 돌려도 `--freeze` 이후 전 구간을 재구성해 같은 결과를 낸다(멱등).
상주 프로세스가 죽어서 표본이 비는 사고가 구조적으로 불가능하다.

## 판정 기준 (미리 못 박는다)

    채택: 독립 관측 **60일 이상** 누적 시
          건당평균·심볼중앙값 모두 순엣지(비용·펀딩 차감 후) > 0,
          시각t·심볼t 모두 2 이상, 롱/숏 양쪽 모두 양수
    기각: 위 중 하나라도 미달. 특히 중앙값이 0 아래로 내려가면 즉시 기각.

핸드오프 12장에는 처음에 "60건 이상"으로 적었는데 **그건 잘못된 기준이다.**
하루 5건이면 12일 만에 채워지고 그건 독립 관측 12개일 뿐이다. 같은 문서가
"시각 클러스터 t 로 판정한다"고 해놓고 건수로 세면 모순이다. **60일로 고쳤다.**
"""
import argparse
import datetime as dt
import json
import pathlib
import statistics as stat
import sys
import time
import urllib.request

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
ROOT = pathlib.Path(__file__).resolve().parent.parent
LEDGER = ROOT / "logs" / "paper_xsmom.jsonl"
BASE = "https://fapi.binance.com/fapi/v1/klines"

UNIVERSE19 = ("BTCUSDT,ETHUSDT,ZECUSDT,HYPEUSDT,BNBUSDT,LITUSDT,1000PEPEUSDT,TRXUSDT,"
              "BCHUSDT,TAOUSDT,XLMUSDT,XMRUSDT,ENAUSDT,XRPUSDT,COTIUSDT,AAVEUSDT,"
              "LINKUSDT,HUMAUSDT,SOLUSDT")
CORE13 = ("BTCUSDT,ETHUSDT,ZECUSDT,BNBUSDT,TRXUSDT,BCHUSDT,XLMUSDT,XMRUSDT,XRPUSDT,"
          "COTIUSDT,AAVEUSDT,LINKUSDT,SOLUSDT")
LOOKBACK = 20
HOLD = 24
FEE_ROUNDTRIP = 0.04          # 메이커 진입/청산 가정 (0.02 x 2)
SPREAD_DEFAULT = 0.01         # 유동 심볼 실측 중앙값 근사. --spread-from 으로 갱신 가능
HOUR_UTC = 0                  # 매일 이 시각 봉 마감으로 신호를 만든다


def get(url):
    for attempt in range(5):
        try:
            with urllib.request.urlopen(url, timeout=25) as r:
                time.sleep(0.25)
                return json.loads(r.read())
        except Exception as e:
            print(f"  retry: {e}", flush=True)
            time.sleep(2 + 3 * attempt)
    return []


def klines(sym, start_ms, limit=1500):
    out, cur = [], start_ms
    while True:
        k = get(f"{BASE}?symbol={sym}&interval=1h&startTime={int(cur)}&limit={limit}")
        if not k:
            break
        out += [[int(x[0]), float(x[1]), float(x[4])] for x in k]   # t, open, close
        if len(k) < limit:
            break
        cur = out[-1][0] + 1
    d = {r[0]: r for r in out}
    return [d[t] for t in sorted(d)]


def spreads_from_obook(path):
    """기록기 산출물에서 심볼별 스프레드 중앙값(%)을 뽑는다."""
    acc = {}
    p = pathlib.Path(path)
    if not p.exists():
        return {}
    with open(p, encoding="utf-8") as f:
        next(f, None)
        for line in f:
            c = line.rstrip("\n").split(",")
            if len(c) < 6:
                continue
            try:
                b, a = float(c[2]), float(c[4])
            except ValueError:
                continue
            if b > 0 and a > b:
                acc.setdefault(c[1], []).append((a - b) / ((a + b) / 2) * 100)
    return {s: stat.median(v) for s, v in acc.items() if len(v) > 50}


def build(bars, uni, syms, freeze_ms, spread):
    """동결 시각 이후의 각 신호일에 대해 신호와 (성숙했다면) 결과를 만든다.

    **순위·중앙값·분위·드리프트 기준선은 항상 `uni`(19심볼) 전체로 낸다.**
    신호를 내보내는 대상만 `syms` 로 좁힌다. 이 둘을 같이 좁히면 횡단면 순위가
    달라져 전혀 다른 신호가 되고, 기준선까지 좁으면 9장의 생존편향과 같은 경로로
    편향이 생긴다. (초판에서 이 둘을 syms 로 계산하는 버그가 있었다.)
    """
    times = sorted({t for s in uni for t, _, _ in bars.get(s, [])})
    idx = {s: {t: i for i, (t, _, _) in enumerate(bars.get(s, []))} for s in uni}
    rows = []
    for t in times:
        d = dt.datetime.fromtimestamp(t / 1000, dt.timezone.utc)
        if d.hour != HOUR_UTC or t < freeze_ms:
            continue
        rel = {}
        for s in uni:
            b, i = bars.get(s, []), idx[s].get(t)
            if i is None or i < LOOKBACK:
                continue
            c0, c1 = b[i - LOOKBACK][2], b[i][2]
            if c0 > 0:
                rel[s] = c1 / c0 - 1.0
        if len(rel) < 8:
            continue
        med = stat.median(rel.values())
        r = {s: v - med for s, v in rel.items()}
        vals = sorted(r.values())
        lo = vals[max(0, int(len(vals) * 0.2) - 1)]
        hi = vals[min(len(vals) - 1, int(len(vals) * 0.8))]

        # 같은 시각 유니버스 전체의 선행수익률 평균 = 드리프트 기준선
        fwd = {}
        for s in uni:
            b, i = bars.get(s, []), idx[s].get(t)
            if i is None or i + 1 + HOLD >= len(b):
                continue
            e, x = b[i + 1][1], b[i + 1 + HOLD][2]
            if e > 0:
                fwd[s] = (x / e - 1.0) * 100.0
        mkt = stat.fmean(fwd.values()) if fwd else None

        for s, v in r.items():
            side = 1 if v >= hi else (-1 if v <= lo else 0)
            if side == 0 or s not in syms:
                continue
            b, i = bars[s], idx[s][t]
            cost = FEE_ROUNDTRIP + spread.get(s, SPREAD_DEFAULT)
            row = {"signal_ts": t, "date": d.strftime("%Y-%m-%d"), "symbol": s,
                   "side": "LONG" if side > 0 else "SHORT", "rel": round(v * 100, 4),
                   "cost_pct": round(cost, 4), "matured": False}
            if s in fwd and mkt is not None:
                gross = side * fwd[s]
                neutral = side * (fwd[s] - mkt)
                row.update(matured=True,
                           entry=b[i + 1][1], exit=b[i + 1 + HOLD][2],
                           gross_pct=round(gross, 4),
                           neutral_pct=round(neutral, 4),
                           net_pct=round(neutral - cost, 4))
            rows.append(row)
    return rows


def tstats(vals_by_time, vals_by_sym):
    def t(vs):
        if len(vs) < 5:
            return float("nan")
        sd = stat.stdev(vs)
        return stat.fmean(vs) / (sd / len(vs) ** 0.5) if sd > 0 else float("nan")
    return t(list(vals_by_time)), t(list(vals_by_sym))


def report(rows):
    m = [r for r in rows if r.get("matured")]
    print(f"\n{'=' * 74}")
    print(f"신호 {len(rows)}건 (성숙 {len(m)}건 / 미성숙 {len(rows) - len(m)}건)")
    if not m:
        print("아직 성숙한 신호가 없다. 24시간 뒤부터 채점된다.")
        return
    days = sorted({r["date"] for r in m})
    print(f"독립 관측일 {len(days)}일 ({days[0]} ~ {days[-1]})   목표 60일")
    for label, key in (("드리프트 중립", "neutral_pct"), ("비용·차감 후 순", "net_pct")):
        v = [r[key] for r in m]
        by_t = [stat.fmean([r[key] for r in m if r["date"] == d]) for d in days]
        syms = sorted({r["symbol"] for r in m})
        by_s = [stat.fmean([r[key] for r in m if r["symbol"] == s]) for s in syms
                if sum(1 for r in m if r["symbol"] == s) >= 3]
        tt, ts = tstats(by_t, by_s)
        med = stat.median(by_s) if by_s else float("nan")
        print(f"  {label:14} 건당 {stat.fmean(v):+.4f}% | 심볼중앙값 {med:+.4f}% "
              f"(시각t {tt:+.1f} / 심볼t {ts:+.1f})")
    for side in ("LONG", "SHORT"):
        v = [r["net_pct"] for r in m if r["side"] == side]
        if v:
            print(f"  {side:14} 건당 {stat.fmean(v):+.4f}%  n={len(v)}")
    net = [r["net_pct"] for r in m]
    win = sum(1 for x in net if x > 0)
    print(f"  승률 {win / len(net) * 100:.1f}%  |  누적 {sum(net):+.2f}%p")
    print(f"{'=' * 74}")
    print("판정: 60일 이상에서 순엣지의 건당평균·심볼중앙값 모두 > 0 이고")
    print("      시각t·심볼t 모두 2 이상이며 롱/숏 양쪽 양수여야 채택.")
    print("      중앙값이 0 아래로 내려가면 즉시 기각. 기준을 나중에 바꾸지 않는다.")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--variant", choices=["all19", "core13"], default="core13")
    p.add_argument("--freeze", default="2026-08-31",
                   help="이 날짜부터의 봉만 쓴다. 규칙 동결일이며 바꾸지 않는다.")
    p.add_argument("--spread-from", default="", help="obook CSV 로 심볼별 비용 갱신")
    p.add_argument("--ledger", default=str(LEDGER))
    a = p.parse_args()

    syms = (UNIVERSE19 if a.variant == "all19" else CORE13).split(",")
    uni = UNIVERSE19.split(",")          # 순위·기준선은 항상 19심볼로 낸다
    freeze = dt.datetime.strptime(a.freeze, "%Y-%m-%d").replace(
        tzinfo=dt.timezone.utc)
    freeze_ms = int(freeze.timestamp() * 1000)
    # 룩백 워밍업만큼 앞에서부터 받는다
    start = freeze_ms - (LOOKBACK + 4) * 3600_000

    spread = spreads_from_obook(a.spread_from) if a.spread_from else {}
    if spread:
        print(f"스프레드 실측 반영: {len(spread)}심볼 "
              f"(중앙 {stat.median(spread.values()):.4f}%)")

    print(f"변형 {a.variant} / 신호심볼 {len(syms)} / 순위기준 {len(uni)} / 동결 {a.freeze}")
    bars = {}
    for s in uni:
        bars[s] = klines(s, start)
    have = sum(1 for s in uni if len(bars[s]) > LOOKBACK)
    print(f"봉 수신: {have}/{len(uni)}심볼, 최대 {max(len(v) for v in bars.values())}봉")

    rows = build(bars, uni, syms, freeze_ms, spread)
    for r in rows:
        r["variant"] = a.variant
    path = pathlib.Path(a.ledger)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    print(f"원장 갱신: {path} ({len(rows)}행, 멱등 — 매번 전체 재구성)")
    report(rows)


if __name__ == "__main__":
    main()
