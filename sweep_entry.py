# -*- coding: utf-8 -*-
"""원칙 0(CM) 진입 파라미터 스윕 + 메인봇 뼈대(볼밴+EMA+RSI) 비교.

**청산 규칙을 일절 쓰지 않는다.** 신호 시점부터 N분 뒤 종가까지의 선행수익률만 본다.
그래야 청산규칙과의 동어반복(=승률 지표 함정)이 생기지 않는다.
진입 시점은 pending 방식과 같게 **신호 봉 다음 봉 시가**로 잡는다(lookahead 차단).
"""
import json, sys, math, statistics as st
import pandas as pd
sys.path.insert(0, ".")
from scripts.scalp_bot_e3 import (_series_ma, CMUltimateMASettings, ema_last)

SP = sys.argv[1]
RAW = json.load(open(SP + "/klines_1m.json"))
HOR = (3, 5, 10, 15, 30)

def resample(bars, m):
    """벽시계 경계로 묶고 미완성 묶음은 버린다(봇의 resample_bars 와 동일 원칙)."""
    if m <= 1:
        return bars
    out = []; cur = None
    for t, o, h, l, c, v in bars:
        b = (t // 60000) // m * m * 60000
        if cur is None or cur[0] != b:
            if cur is not None and cur[6] == m:
                out.append(cur[:6])
            cur = [b, o, h, l, c, v, 1]
        else:
            cur[2] = max(cur[2], h); cur[3] = min(cur[3], l); cur[4] = c
            cur[5] += v; cur[6] += 1
    if cur is not None and cur[6] == m:
        out.append(cur[:6])
    return out

def cm_series(bars, length, atype, smoothe=2):
    """봉마다 ma_up / ma_down 을 만든다(cm_ultimate_ma_mtf_v2 의 핵심 판정과 동일)."""
    close = [b[4] for b in bars]; vol = [b[5] for b in bars]
    avg = _series_ma(close, vol, length, atype, 7, second=False)
    up, dn = [], []
    for i in range(len(bars)):
        j = i - smoothe
        if j < 0 or avg[i] is None or avg[j] is None:
            up.append(False); dn.append(False); continue
        up.append(avg[i] >= avg[j]); dn.append(avg[i] < avg[j])
    return avg, up, dn

def fwd_all(m1, t_entry_ms, e, side):
    """진입 시각(1분봉 기준)부터 각 horizon 분 뒤 종가까지의 수익률.

    [수정] 종전엔 **신호봉 단위**로 horizon 을 나눠서 3분봉에서 3분과 5분이
    똑같이 '1봉 뒤'가 됐다(5//3==1). 1분봉으로 재야 구간이 구분된다.
    """
    out = {}
    idx = m1.get("idx"); bars = m1["bars"]
    k = idx.get(t_entry_ms)
    if k is None or e <= 0:
        return out
    for h in HOR:
        j = k + h
        if j >= len(bars):
            continue
        c = bars[j][4]
        out[h] = ((c / e - 1) if side == "LONG" else (1 - c / e)) * 100
    return out

def tstat(g):
    if len(g) < 20: return 0.0, 0.0
    m = sum(g) / len(g); sd = st.pstdev(g)
    return m, (m / (sd / math.sqrt(len(g))) if sd > 0 else 0.0)

def report(name, res):
    line = f"{name:<34}"
    for h in HOR:
        g = res.get(h, [])
        if len(g) < 20:
            line += f"{'-':>16}"; continue
        m, t = tstat(g)
        line += f"{m:>+8.4f}%(t{t:>+5.2f})"
    print(line + f"   n={len(res.get(HOR[0],[]))}")

# ---------------------------------------------------------------- 1) CM 파라미터 스윕
def run_cm(tf, length, atype, pull_max=None, htf=None):
    """htf: None=필터없음, True=4시간 EMA200 정배열 일치만."""
    res = {h: [] for h in HOR}
    for sym, bars in RAW.items():
        bb = resample(bars, tf)
        if len(bb) < 60:
            continue
        m1 = {"bars": bars, "idx": {b[0]: k for k, b in enumerate(bars)}}
        avg, up, dn = cm_series(bb, length, atype)
        # 4시간 EMA200 정배열(같은 1분봉으로 240분 리샘플 후 EMA200)
        h4 = None
        if htf is not None:
            hb = resample(bars, 240)
            if len(hb) < 5:
                continue
            hc = [b[4] for b in hb]
            h4 = [None] * len(bb)
            k = 0
            for i, b in enumerate(bb):
                while k + 1 < len(hb) and hb[k + 1][0] <= b[0]:
                    k += 1
                seg = hc[:k + 1]
                h4[i] = (seg[-1] > ema_last(seg, min(200, len(seg)))) if len(seg) >= 3 else None
        for i in range(40, len(bb) - 1):
            c = bb[i][4]; a = avg[i]
            if a is None or a <= 0:
                continue
            side = "LONG" if (up[i] and c > a) else "SHORT" if (dn[i] and c < a) else None
            if side is None:
                continue
            if h4 is not None and h4[i] is not None:
                if (side == "LONG") != h4[i]:
                    continue
            if pull_max is not None:
                # 눌림 깊이: 진입가(다음 봉 시가)가 HullMA 에서 얼마나 떨어져 있나
                e = bb[i + 1][1]
                d = abs(e - a) / a * 100
                if d > pull_max:
                    continue
            e = bb[i + 1][1]
            for h, r in fwd_all(m1, bb[i + 1][0], e, side).items():
                res[h].append(r)
    return res

# ---------------------------------------------------------------- 3) 메인봇 뼈대
def run_bb_ema_rsi(tf, need_ema=True, need_rsi=True):
    res = {h: [] for h in HOR}
    for sym, bars in RAW.items():
        bb = resample(bars, tf)
        if len(bb) < 60:
            continue
        m1 = {"bars": bars, "idx": {b[0]: k for k, b in enumerate(bars)}}
        c = [b[4] for b in bb]
        for i in range(40, len(bb) - 1):
            w = c[i - 19:i + 1]
            mu = sum(w) / 20
            sd = (sum((x - mu) ** 2 for x in w) / 20) ** 0.5
            up_, lo_ = mu + 2 * sd, mu - 2 * sd
            side = "LONG" if c[i] <= lo_ else "SHORT" if c[i] >= up_ else None
            if side is None:
                continue
            if need_ema:
                # [수정] 종전엔 "종가가 EMA25 위"를 요구했는데, 볼밴 하단 터치와
                # 동시에 성립하는 일이 거의 없어 표본이 0 이 됐다(하단밴드 < MA20 ~ EMA25).
                # 메인 봇의 '방향일치'는 **EMA25 기울기**(추세 방향)를 뜻한다.
                e_now = ema_last(c[:i + 1], 25)
                e_prev = ema_last(c[:i - 2], 25) if i >= 30 else e_now
                if (side == "LONG") != (e_now > e_prev):
                    continue
            if need_rsi:
                g = l = 0.0
                for k in range(i - 13, i + 1):
                    d = c[k] - c[k - 1]
                    g += max(d, 0); l += max(-d, 0)
                rsi = 100 if l == 0 else 100 - 100 / (1 + (g / 14) / (l / 14))
                if side == "LONG" and rsi > 35: continue
                if side == "SHORT" and rsi < 65: continue
            e = bb[i + 1][1]
            for h, r in fwd_all(m1, bb[i + 1][0], e, side).items():
                res[h].append(r)
    return res

if __name__ == "__main__":
    hdr = f"{'설정':<34}" + "".join(f"{str(h)+'분':>16}" for h in HOR)
    print("=" * 116); print("[1] 원칙 0 (CM) 파라미터 스윕 — 신호봉 다음 시가 진입, 선행수익률"); print(hdr); print("-"*116)
    base = None
    for tf in (3, 5, 15):
        for ln in (10, 20, 40):
            r = run_cm(tf, ln, 4)
            report(f"CM HullMA{ln} / {tf}분봉", r)
            if tf == 3 and ln == 20: base = r
    print()
    print("현재값(3분/HullMA20) 대비 변형:"); print("-"*116)
    report("  atype=1 (SMA, 어제까지 값)", run_cm(3, 20, 1))
    report("  + 4h EMA200 정배열 필터", run_cm(3, 20, 4, htf=True))
    report("  + 눌림 상한 0.5%", run_cm(3, 20, 4, pull_max=0.5))
    report("  + 눌림 상한 0.2%", run_cm(3, 20, 4, pull_max=0.2))
    print()
    print("=" * 116); print("[3] 메인 봇 뼈대 (볼밴+EMA+RSI) 비교"); print(hdr); print("-"*116)
    report("볼밴+EMA25+RSI (방향일치)", run_bb_ema_rsi(3, True, True))
    report("볼밴+EMA25", run_bb_ema_rsi(3, True, False))
    report("볼밴 단독", run_bb_ema_rsi(3, False, False))
    report("볼밴+EMA25+RSI (15분봉)", run_bb_ema_rsi(15, True, True))
