"""원칙 0 대조: HullMA20 을 3분봉(현행)과 15분봉(원칙 0)으로 계산했을 때의
방향성 엣지를 같은 기준(드리프트 중립)으로 비교한다.

신호는 봉 마감으로 확정하고, 선행수익률은 신호봉 다음 봉 시가부터 N분 뒤 종가까지.
lookahead 없음.
"""
import json, math, sys, statistics as st
from collections import defaultdict
sys.path.insert(0, 'scripts')
import importlib.util
spec = importlib.util.spec_from_file_location('e3', 'scripts/scalp_bot_e3.py')
e3 = importlib.util.module_from_spec(spec); sys.modules['e3'] = e3
_argv = sys.argv; sys.argv = ['x']; spec.loader.exec_module(e3); sys.argv = _argv

D = json.load(open('scratch_cm_klines.json', encoding='utf-8'))
B3, B4 = D['bars3m'], D['bars4h']
SYMS = sorted(B3)

def agg(rows, k):
    """3분봉 k개를 벽시계 경계로 묶는다(resample_bars 와 같은 원칙)."""
    if k == 1:
        return rows
    out, cur, span = [], None, k * 180000
    for r in rows:
        b = r[0] - (r[0] % span)
        if cur is None or cur[0] != b:
            if cur is not None:
                out.append(cur)
            cur = [b, r[1], r[2], r[3], r[4], r[5], 1]
        else:
            cur[2] = max(cur[2], r[2]); cur[3] = min(cur[3], r[3])
            cur[4] = r[4]; cur[5] += r[5]; cur[6] += 1
    if cur is not None and cur[6] == k:      # 미완성 묶음 버림
        out.append(cur)
    return [r[:6] for r in out]

def htf_up(sym):
    """4h 종가 > EMA200 을 시각별로. (ts, bool) 오름차순."""
    rows = B4[sym]
    cl = [r[4] for r in rows]
    ema, a, out = None, 2.0 / 201.0, []
    for i, c in enumerate(cl):
        ema = c if ema is None else a * c + (1 - a) * ema
        out.append((rows[i][0], c > ema))
    return out

def htf_at(series, ts):
    lo, hi, res = 0, len(series) - 1, None
    while lo <= hi:
        m = (lo + hi) // 2
        if series[m][0] <= ts:
            res = series[m][1]; lo = m + 1
        else:
            hi = m - 1
    return res

# 신호 수집: (심볼, 진입시각ts, 방향) -> 3분봉 인덱스 기준 선행수익률
HOR = [5, 10, 20]            # 3분봉 개수 = 15분/30분/60분
def collect(tf_bars, flip_max):
    """tf_bars: 신호를 계산할 묶음 크기(1=3분, 5=15분)."""
    sig = []
    for sym in SYMS:
        rows3 = B3[sym]
        bars = agg(rows3, tf_bars)
        if len(bars) < 60:
            continue
        cl = [r[4] for r in bars]; vol = [r[5] for r in bars]
        hs = e3._series_ma(cl, vol, 20, 4, 7, second=False)   # HullMA20
        hu = htf_up(sym)
        # 3분봉 시각 -> 인덱스
        idx3 = {r[0]: i for i, r in enumerate(rows3)}
        for i in range(30, len(bars) - 1):
            if hs[i] is None or hs[i - 2] is None:
                continue
            up = hs[i] >= hs[i - 2]
            if not (cl[i] > hs[i] if up else cl[i] < hs[i]):
                continue                       # close vs out1 (봇과 동일)
            # 전환 경과
            if flip_max >= 0:
                age, k = 0, i
                while k - 2 >= 0 and (hs[k] >= hs[k - 2]) == up:
                    age += 1; k -= 1
                if age - 1 > flip_max:
                    continue
            # 4h 정합
            h = htf_at(hu, bars[i][0])
            if h is None or h != up:
                continue
            # 진입: 신호봉 마감 다음 3분봉 시가
            nxt = bars[i][0] + tf_bars * 180000
            j = idx3.get(nxt)
            if j is None or j + max(HOR) >= len(rows3):
                continue
            sig.append((sym, j, up, rows3[j][1]))
    return sig

MKT = {}
def market_mean(h):
    """진입봉 인덱스 j 에서 h봉 뒤까지, 전 심볼 등가중 평균 수익률."""
    if h in MKT:
        return MKT[h]
    n = min(len(B3[s]) for s in SYMS)
    acc = [0.0] * n; cnt = [0] * n
    for s in SYMS:
        q = B3[s]
        for j in range(n - h):
            acc[j] += (q[j + h][4] / q[j][1] - 1.0) * 100.0
            cnt[j] += 1
    MKT[h] = [acc[j] / cnt[j] if cnt[j] else 0.0 for j in range(n)]
    return MKT[h]

def report(name, sig):
    if not sig:
        print(f'{name}: 신호 0'); return
    cols = []
    for h in HOR:
        mm = market_mean(h)
        raw, adj = [], []
        for sym, j, up, px in sig:
            q = B3[sym]
            if j + h >= len(q) or j >= len(mm):
                continue
            r = (q[j + h][4] / px - 1.0) * 100.0
            r = r if up else -r
            m = mm[j] if up else -mm[j]
            raw.append(r); adj.append(r - m)
        def t(v):
            return st.mean(v) / (st.stdev(v) / math.sqrt(len(v))) if len(v) > 2 and st.stdev(v) else 0.0
        cols.append((st.mean(raw), t(raw), st.mean(adj), t(adj)))
    print(f'{name:32s} n={len(sig):6d} | ' + ' | '.join(
        f'{h*3:>2d}분 원시{c[0]:+.4f}(t{c[1]:+.1f}) 중립{c[2]:+.4f}(t{c[3]:+.1f})'
        for h, c in zip(HOR, cols)))

print('=== HullMA20 계산 시간대 대조 (4h EMA200 정합 필터 공통 적용) ===')
report('3분봉 HullMA20 flip<=5 [현행]', collect(1, 5))
report('3분봉 HullMA20 flip 무제한', collect(1, -1))
report('15분봉 HullMA20 flip<=5 [원칙0]', collect(5, 5))
report('15분봉 HullMA20 flip 무제한', collect(5, -1))
