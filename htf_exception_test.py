# -*- coding: utf-8 -*-
"""4시간 필터 예외 검토 — "HullMA 방향 전환 직후"는 역행이어도 받을 것인가.

계기: MOVRUSDT 16:18 에 CM SHORT 가 켜지고 직후 두 봉에서 -10% 가 났는데,
4시간 EMA200 이 위에 있어(상승국면) 역행으로 차단됐다. 큰 하락은 대개 상승추세가
꺾이는 자리에서 시작하므로, **전환 봉만 예외로 받자**는 가설.

측정: 3일 x 85심볼, 신호봉 다음 시가 진입, 보유 15분 고정, 수수료 차감 후 순익.
청산 규칙을 쓰지 않으므로 승률 지표의 동어반복 함정이 없다.
"""
import json, sys, math, statistics as st
sys.path.insert(0, ".")
from scripts.scalp_bot_e3 import _series_ma, ema_last

SP = sys.argv[1]
LEV, HOLD = 5, 15
FEE_WORST, FEE_BEST = 0.0862, 0.0552

def resample(bars, m):
    out, cur = [], None
    for t, o, h, l, c, v in bars:
        b = (t // 60000) // m * m * 60000
        if cur is None or cur[0] != b:
            if cur is not None and cur[6] == m: out.append(cur[:6])
            cur = [b, o, h, l, c, v, 1]
        else:
            cur[2] = max(cur[2], h); cur[3] = min(cur[3], l); cur[4] = c
            cur[5] += v; cur[6] += 1
    if cur is not None and cur[6] == m: out.append(cur[:6])
    return out

def run(raw):
    """(그룹명 -> 수익률 리스트). 그룹은 4h 정합/역행 x 전환 후 경과봉."""
    g = {}
    for sym, bars in raw.items():
        b3 = resample(bars, 3)
        if len(b3) < 60: continue
        close = [x[4] for x in b3]; vol = [x[5] for x in b3]
        hs = _series_ma(close, vol, 20, 4, 7, second=False)
        idx = {x[0]: k for k, x in enumerate(bars)}
        hb = resample(bars, 240)
        if len(hb) < 5: continue
        hc = [x[4] for x in hb]
        h4 = [None] * len(b3); k = 0
        for i, x in enumerate(b3):
            while k + 1 < len(hb) and hb[k + 1][0] <= x[0]: k += 1
            seg = hc[:k + 1]
            h4[i] = (seg[-1] > ema_last(seg, min(200, len(seg)))) if len(seg) >= 3 else None
        ups = [None] * len(b3)
        for i in range(len(b3)):
            ups[i] = (hs[i] >= hs[i - 2]) if (i >= 2 and hs[i] and hs[i - 2]) else None
        for i in range(40, len(b3) - 1):
            a = hs[i]
            if a is None or a <= 0 or ups[i] is None or h4[i] is None: continue
            up = ups[i]
            c = close[i]
            side = "LONG" if (up and c > a) else "SHORT" if (not up and c < a) else None
            if side is None: continue
            aligned = (side == "LONG") == h4[i]
            # 전환 후 경과 봉수: ups 가 현재 값으로 바뀐 뒤 몇 봉째인가
            age = 0
            j = i
            while j > 0 and ups[j - 1] == up:
                age += 1; j -= 1
                if age > 9: break
            e = b3[i + 1][1]; kk = idx.get(b3[i + 1][0])
            if kk is None or e <= 0: continue
            jj = kk + HOLD
            if jj >= len(bars): continue
            r = ((bars[jj][4] / e - 1) if side == "LONG" else (1 - bars[jj][4] / e)) * 100
            base = "정합" if aligned else "역행"
            key = f"{base}/전환후{age}봉" if age <= 8 else f"{base}/9봉이상"
            g.setdefault(key, []).append(r)
            g.setdefault("정합" if aligned else "역행", []).append(r)
    return g

def show(nm, v):
    if len(v) < 100:
        print(f"{nm:<24}{len(v):>8}  표본부족"); return
    m = sum(v) / len(v); sd = st.pstdev(v)
    t = m / (sd / math.sqrt(len(v)))
    print(f"{nm:<24}{len(v):>8}{m:>+10.4f}%{t:>+8.2f}"
          f"{m-FEE_WORST:>+11.4f}%{m-FEE_BEST:>+11.4f}%"
          f"{sum(1 for x in v if x>0)/len(v)*100:>8.1f}%")

if __name__ == "__main__":
    RAW = json.load(open(SP + "/klines_1m.json"))
    g = run(RAW)
    print("=" * 92)
    print(f"[4시간 필터 예외 검토] 보유 {HOLD}분 고정 / 수수료 차감 후 순익")
    print("=" * 92)
    print(f"{'그룹':<24}{'건수':>8}{'총수익률':>10}{'t':>8}{'순익(최악)':>12}{'순익(최선)':>12}{'승률':>9}")
    print("-" * 92)
    for k in ("정합", "역행"):
        show(k, g.get(k, []))
    print("-" * 92)
    import re as _re
    def _ord(k):
        m=_re.search(r"전환후(\d+)봉",k)
        return (0 if k.startswith("정합") else 1, int(m.group(1)) if m else 99)
    print("[정합] 전환 후 경과봉별 — 누적 진입가능 건수도 함께")
    cum=0
    for k in sorted((x for x in g if x.startswith("정합/")), key=_ord):
        show(k, g[k]); cum+=len(g[k])
    print()
    print("[누적] 전환 후 N봉 이내까지 받으면")
    print(f"{'조건':<24}{'건수':>8}{'총수익률':>10}{'t':>8}{'순익(최악)':>12}{'순익(최선)':>12}{'승률':>9}")
    acc=[]
    for n in range(0,7):
        acc += g.get(f"정합/전환후{n}봉",[])
        show(f"  전환 후 {n}봉 이내", acc)
    show("  전체(제한없음)", g.get("정합",[]))
