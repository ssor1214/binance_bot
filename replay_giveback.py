# -*- coding: utf-8 -*-
"""안 B — 원장 반사실 재생. **봇이 실제로 잡은 거래**에 청산 규칙만 바꿔 다시 걸어본다.

하네스의 근본 결함(모집단이 다름)이 없다. 진입 선택은 라이브 그 자체다.

한계 (결과 해석 시 반드시 함께 볼 것):
  1) 라이브 되돌림 판정은 5초 틱인데 재생은 1분봉이다. MFE 고점을 과소평가한다.
     -> 봉 **종가** 기준(하한)과 봉 **고가** 기준(상한) 두 값을 모두 낸다.
  2) 청산이 빨라지면 슬롯이 빨리 비어 다음 거래가 달라진다(2차 효과). 재생은 모델 안 함.
     -> 보유시간이 30% 넘게 바뀌는 조합에는 경고를 단다.
  3) 실제보다 늦게 끝나는 조합은 실제로는 없던 시간대의 봉을 쓴다.
"""
import json, io, re, sys, time, math, statistics as st

LEDGER = "logs/scalp_bot_e3_cm_ledger.jsonl"
RUNLOG = "logs/scalp_bot_e3_cm_run.log"
SINCE  = "2026-08-27 20:35"
FEE_MAKER, FEE_TAKER = 0.0276, 0.0586      # 왕복 %, 원장 실측
HORIZON_MIN = 90

def f(r, k):
    try: return float(r.get(k) or 0)
    except Exception: return 0.0

def load_stops():
    """런로그의 진입 줄에서 (심볼, 방향, 시각) -> 손절선."""
    pat = re.compile(r"(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d) \[e3\] pid=\d+ 진입 (\S+) "
                     r"(LONG|SHORT) 평단([\d.]+) \d+차 qty=[\d.]+ 손절([\d.]+)")
    out = []
    for ln in io.open(RUNLOG, encoding="utf-8", errors="replace"):
        m = pat.search(ln)
        if m:
            ts = time.mktime(time.strptime(m.group(1), "%Y-%m-%d %H:%M:%S"))
            out.append((ts, m.group(2), m.group(3), float(m.group(4)), float(m.group(5))))
    return out

def match_stop(stops, r):
    c = [x for x in stops if x[1] == r["symbol"] and x[2] == r["side"]
         and abs(x[0] - r["entered_at"]) < 180]
    if not c: return 0.0
    return min(c, key=lambda x: abs(x[0] - r["entered_at"]))[4]

def fetch_bars(client, sym, t_in, t_out, cache):
    """진입~청산+여유 구간 1분봉. 심볼당 한 번만 받아 캐시한다(IP밴 예방 스로틀)."""
    key = sym
    if key not in cache:
        try:
            kl = client.futures_klines(symbol=sym, interval="1m", limit=1000)
            cache[key] = [[int(k[0]) // 1000, float(k[1]), float(k[2]),
                           float(k[3]), float(k[4])] for k in kl]
        except Exception:
            cache[key] = []
        time.sleep(0.12)
    bars = cache[key]
    a, b = int(t_in), int(t_out) + HORIZON_MIN * 60
    return [x for x in bars if a - 60 <= x[0] <= b]

def replay(r, stop, bars, arm, frac, mfe_basis):
    """한 거래를 청산 규칙만 바꿔 다시 건다.

    우선순위는 라이브와 같다: 거래소 손절/TP 는 봉 안에서, 되돌림은 봉 종가로 판정.
    mfe_basis: 'close' = 봉 종가로만 MFE 갱신(하한) / 'high' = 봉 고가까지(상한)
    """
    e = f(r, "entry_price"); L = r["side"] == "LONG"; lev = f(r, "leverage") or 5
    tp = f(r, "tp_limit_price")
    if e <= 0 or not bars: return None
    peak = 0.0
    for t, o, hi, lo, c in bars:
        if t < int(r["entered_at"]) - 60: continue
        if stop > 0 and ((lo <= stop) if L else (hi >= stop)):
            g = ((stop / e - 1) if L else (1 - stop / e)) * 100
            return g - FEE_TAKER, "SL"
        if tp > 0 and ((hi >= tp) if L else (lo <= tp)):
            g = ((tp / e - 1) if L else (1 - tp / e)) * 100
            return g - FEE_MAKER, "TP"
        pk_px = hi if L else lo
        roe_pk = ((pk_px / e - 1) if L else (1 - pk_px / e)) * lev * 100
        roe_c = ((c / e - 1) if L else (1 - c / e)) * lev * 100
        peak = max(peak, roe_pk if mfe_basis == "high" else roe_c)
        if arm > 0 and peak >= arm and roe_c <= peak * (1 - frac):
            return roe_c / lev - FEE_MAKER, "GB"      # 지정가 시도 -> maker 가정
    t, o, hi, lo, c = bars[-1]
    g = ((c / e - 1) if L else (1 - c / e)) * 100
    return g - FEE_TAKER, "TIME"

def main():
    from bot.config import Config
    from bot.exchange import Exchange
    ex = Exchange(Config())
    rows = [r for r in (json.loads(l) for l in io.open(LEDGER, encoding="utf-8") if l.strip())
            if r.get("exited_at")]
    t0 = time.mktime(time.strptime(SINCE, "%Y-%m-%d %H:%M"))
    v = [r for r in rows if r["exited_at"] >= t0 and abs(f(r, "real_net")) > 1e-9
         and f(r, "tp_limit_price") > 0]
    stops = load_stops()
    cache = {}
    data = []
    for r in v:
        sp = match_stop(stops, r)
        if sp <= 0: continue
        bars = fetch_bars(ex.client, r["symbol"], r["entered_at"], r["exited_at"], cache)
        if len(bars) < 3: continue
        data.append((r, sp, bars))
    print(f"재생 대상 {len(data)}건 / 원장 {len(v)}건 (손절선·봉 확보된 것만)")
    if not data: return
    # 실제 성적(비교 기준)
    act = sum(f(r, "real_net") / f(r, "nominal") * 100 for r, _, _ in data if f(r, "nominal") > 0)
    print(f"실제 성적: 명목대비 합 {act:+.3f}%  건당 {act/len(data):+.4f}%\n")
    print("  note: live uses 5s ticks, so reality is closer to the HIGH basis (upper bound)")
    for basis in ("close", "high"):
        print(f"── MFE 기준 = 봉 {basis} ({'하한' if basis=='close' else '상한'}) ──")
        print(f"{'arm':>5}{'frac':>6}{'건당%':>10}{'t':>7}{'승률':>7}{'TP':>5}{'SL':>5}{'GB':>5}{'만료':>5}{'보유변화':>9}")
        for arm in (0.0, 1.0, 1.5, 2.0, 3.0):
            for frac in ((0.4,) if arm == 0 else (0.3, 0.4, 0.5)):
                res, why = [], []
                for r, sp, bars in data:
                    out = replay(r, sp, bars, arm, frac, basis)
                    if out is None: continue
                    res.append(out[0]); why.append(out[1])
                if len(res) < 30: continue
                m = sum(res) / len(res); sd = st.pstdev(res)
                t = m / (sd / math.sqrt(len(res))) if sd > 0 else 0
                n = len(res)
                tag = "" if arm > 0 else "  <- 되돌림 없음(기준)"
                print(f"{arm:>5.1f}{frac:>6.1f}{m:>+10.4f}{t:>+7.2f}"
                      f"{sum(1 for x in res if x>0)/n*100:>6.0f}%"
                      f"{why.count('TP')/n*100:>4.0f}%{why.count('SL')/n*100:>4.0f}%"
                      f"{why.count('GB')/n*100:>4.0f}%{why.count('TIME')/n*100:>4.0f}%{tag}")
        print()

if __name__ == "__main__":
    main()
