# -*- coding: utf-8 -*-
"""수수료 차감 후 '순익' 기준 평가. 승률은 참고로만 낸다.

사용자 지시: "승률보다 순익이 좋아야 한다".
승률은 청산 규칙과 동어반복이 되므로 판정 기준으로 쓰지 않는다.
여기서는 청산 규칙을 넣지 않고, 신호 시점부터 고정 보유(15분) 후 종가 청산을
가정해 **명목 대비 순익률**을 낸다. 실제 봇은 TP/SL 을 쓰지만, 진입 품질 비교에는
같은 청산 조건을 양쪽에 똑같이 적용하는 것이 공정하다.
"""
import json, sys, math, statistics as st
sys.path.insert(0, ".")
from scripts.scalp_bot_e3 import _series_ma, ema_last

SP = sys.argv[1]
HOLD = 15                      # 분. 스윕에서 엣지가 가장 컸던 구간
LEV = 5
FEE_ROUNDTRIP = 0.0276 + 0.0586   # 실측 왕복(진입 maker + 청산 taker 최악) %/명목
FEE_BEST = 0.0276 * 2             # 양쪽 다 지정가로 체결된 경우

def resample(bars, m):
    if m <= 1: return bars
    out = []; cur = None
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

def cm_updn(bars, length, atype, smoothe=2):
    close = [b[4] for b in bars]; vol = [b[5] for b in bars]
    avg = _series_ma(close, vol, length, atype, 7, second=False)
    up, dn = [], []
    for i in range(len(bars)):
        j = i - smoothe
        ok = j >= 0 and avg[i] is not None and avg[j] is not None
        up.append(ok and avg[i] >= avg[j]); dn.append(ok and avg[i] < avg[j])
    return avg, up, dn

def htf_flags(bars, bb):
    hb = resample(bars, 240)
    if len(hb) < 5: return None
    hc = [b[4] for b in hb]; out = [None] * len(bb); k = 0
    for i, b in enumerate(bb):
        while k + 1 < len(hb) and hb[k + 1][0] <= b[0]: k += 1
        seg = hc[:k + 1]
        out[i] = (seg[-1] > ema_last(seg, min(200, len(seg)))) if len(seg) >= 3 else None
    return out

def run(raw, use_htf, tf=3, length=20, atype=4):
    rs = []
    for sym, bars in raw.items():
        bb = resample(bars, tf)
        if len(bb) < 60: continue
        idx = {b[0]: k for k, b in enumerate(bars)}
        avg, up, dn = cm_updn(bb, length, atype)
        h4 = htf_flags(bars, bb) if use_htf else None
        for i in range(40, len(bb) - 1):
            a = avg[i]; c = bb[i][4]
            if a is None or a <= 0: continue
            side = "LONG" if (up[i] and c > a) else "SHORT" if (dn[i] and c < a) else None
            if side is None: continue
            if h4 is not None:
                if h4[i] is None or (side == "LONG") != h4[i]: continue
            e = bb[i + 1][1]; k = idx.get(bb[i + 1][0])
            if k is None or e <= 0: continue
            j = k + HOLD
            if j >= len(bars): continue
            g = ((bars[j][4] / e - 1) if side == "LONG" else (1 - bars[j][4] / e)) * 100
            rs.append(g)
    return rs

def show(name, g):
    if len(g) < 50:
        print(f"{name:<30} 표본부족 n={len(g)}"); return
    m = sum(g) / len(g); sd = st.pstdev(g); t = m / (sd / math.sqrt(len(g)))
    n_worst = m - FEE_ROUNDTRIP
    n_best = m - FEE_BEST
    print(f"{name:<30}{len(g):>8}{m:>+10.4f}%{t:>+8.2f}"
          f"{n_worst:>+11.4f}%{n_best:>+11.4f}%{sum(1 for x in g if x>0)/len(g)*100:>8.1f}%")

if __name__ == "__main__":
    T1 = json.load(open(SP + "/klines_1m.json"))
    import os
    hdr = (f"{'구성':<30}{'건수':>8}{'총수익률':>11}{'t':>8}"
           f"{'순익(최악)':>11}{'순익(최선)':>11}{'승률':>9}")
    print("=" * 96)
    print(f"[보유 {HOLD}분 고정 청산 / 수수료 차감 후 '순익' 기준]")
    print(f"  최악 = 진입 maker + 청산 taker  왕복 {FEE_ROUNDTRIP:.4f}%")
    print(f"  최선 = 양쪽 다 지정가 체결      왕복 {FEE_BEST:.4f}%")
    print("=" * 96); print(hdr); print("-" * 96)
    show("1군(1~85위) 필터없음", run(T1, False))
    show("1군(1~85위) + 4h필터", run(T1, True))
    p2 = SP + "/klines_1m_tier2.json"
    if os.path.exists(p2) and os.path.getsize(p2) > 100:
        T2 = json.load(open(p2))
        print("-" * 96)
        show("2군(86~150위) 필터없음", run(T2, False))
        show("2군(86~150위) + 4h필터", run(T2, True))
        print("-" * 96)
        both = dict(T1); both.update(T2)
        show("1+2군 통합 + 4h필터", run(both, True))
    else:
        print("\n(2군 데이터 아직 없음)")
