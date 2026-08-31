"""리샘플 중복으로 생기는 '신호봉 1개 지연'의 비용을 측정한다.

WS 캐시가 이미 3분봉인데 resample_bars(df, 3) 을 또 걸면 마지막 봉이 버려진다.
그래서 봇은 방금 마감한 봉이 아니라 그 직전 봉으로 신호를 판정한다.
  정상: 봉 k 마감 -> 봉 k+1 시가 진입
  현행: 봉 k 마감 -> 봇은 k-1 까지만 봄 -> 실제로는 k+1 시가에 k-1 신호로 진입
"""
import json, math, sys, statistics as st
import importlib.util
spec = importlib.util.spec_from_file_location('e3', 'scripts/scalp_bot_e3.py')
e3 = importlib.util.module_from_spec(spec); sys.modules['e3'] = e3
_a = sys.argv; sys.argv = ['x']; spec.loader.exec_module(e3); sys.argv = _a

D = json.load(open('scratch_cm_klines.json', encoding='utf-8'))
B3, B4 = D['bars3m'], D['bars4h']
SYMS = sorted(B3)
HOR = [5, 10, 20]

def htf_series(sym):
    rows = B4[sym]; ema = None; a = 2.0 / 201.0; out = []
    for r in rows:
        c = r[4]; ema = c if ema is None else a * c + (1 - a) * ema
        out.append((r[0], c > ema))
    return out

def htf_at(s, ts):
    lo, hi, res = 0, len(s) - 1, None
    while lo <= hi:
        m = (lo + hi) // 2
        if s[m][0] <= ts: res = s[m][1]; lo = m + 1
        else: hi = m - 1
    return res

MKT = {}
def mkt(h):
    if h in MKT: return MKT[h]
    n = min(len(B3[s]) for s in SYMS)
    acc = [0.0] * n; cnt = [0] * n
    for s in SYMS:
        q = B3[s]
        for j in range(n - h):
            acc[j] += (q[j + h][4] / q[j][1] - 1.0) * 100.0; cnt[j] += 1
    MKT[h] = [acc[j] / cnt[j] if cnt[j] else 0.0 for j in range(n)]
    return MKT[h]

def run(delay):
    """delay=0 정상(신호봉 다음 봉 진입), delay=1 현행(한 봉 더 늦게 진입)."""
    sig = []
    for sym in SYMS:
        rows = B3[sym]
        cl = [r[4] for r in rows]; vo = [r[5] for r in rows]
        hs = e3._series_ma(cl, vo, 20, 4, 7, second=False)
        hu = htf_series(sym)
        for i in range(30, len(rows) - max(HOR) - 2 - delay):
            if hs[i] is None or hs[i - 2] is None: continue
            up = hs[i] >= hs[i - 2]
            if not (cl[i] > hs[i] if up else cl[i] < hs[i]): continue
            age, k = 0, i
            while k - 2 >= 0 and (hs[k] >= hs[k - 2]) == up: age += 1; k -= 1
            if age - 1 > 5: continue                     # flip<=5 (라이브)
            h4 = htf_at(hu, rows[i][0])
            if h4 is None or h4 != up: continue
            j = i + 1 + delay                            # 진입 봉
            sig.append((sym, j, up, rows[j][1]))
    return sig

def rep(name, sig):
    cols = []
    for h in HOR:
        mm = mkt(h); raw, adj = [], []
        for sym, j, up, px in sig:
            q = B3[sym]
            if j + h >= len(q) or j >= len(mm): continue
            r = (q[j + h][4] / px - 1.0) * 100.0
            r = r if up else -r
            raw.append(r); adj.append(r - (mm[j] if up else -mm[j]))
        def t(v): return st.mean(v)/(st.stdev(v)/math.sqrt(len(v))) if len(v) > 2 and st.stdev(v) else 0.0
        cols.append((st.mean(raw), t(raw), st.mean(adj), t(adj)))
    print(f'{name:30s} n={len(sig):6d} | ' + ' | '.join(
        f'{h*3:>2d}분 원시{c[0]:+.4f} 중립{c[2]:+.4f}(t{c[3]:+.1f})' for h, c in zip(HOR, cols)))

print('=== 신호봉 1개 지연의 비용 (CM+HTF+flip<=5, 드리프트 중립) ===')
rep('정상: 신호봉 다음 봉 진입', run(0))
rep('현행: 한 봉 더 늦게 진입', run(1))
