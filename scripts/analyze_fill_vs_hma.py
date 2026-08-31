"""체결이 실제로 '눌림' 자리에서 이뤄졌는가 — 원장 x 3분봉 패널 대조.

배경: e3 는 진입 **판정**을 추격으로 하고(`ma_up and close > HullMA20`) **체결**은
EMA5 눌림 대기로 한다. 분해 측정에서 부호를 뒤집는 것이 바로 그 `close > HullMA20`
조건이었으므로(가격위치만 -0.0071 vs 눌림 +0.0075), "체결이 그 좋은 쪽에 들어가는가"
가 실전 손익의 갈림길이 된다. 그런데 **측정의 눌림(close < HullMA20, 3분봉)과 체결의
눌림(EMA5 터치, 1분봉)이 같은 것인지는 재본 적이 없었다.** 이 스크립트가 그걸 잰다.

주의:
  - `entered_at` 은 실제 체결 시각이 아니다(CLAUDE.md 원칙 0 보강 5). 왕복 수수료율이
    정상인 건(=발견 지연 <=5초)으로 좁혀서 봐야 한다. 아래 clean 이 그것이다.
  - 3분봉 패널에 없는 심볼은 제외된다(실측 478/640).
"""
import json, sys, pathlib, collections
import numpy as np
sys.path.insert(0, str(pathlib.Path('scripts').resolve()))
sys.stdout.reconfigure(encoding='utf-8', errors='replace')
from edge_lab import load_panel, build_indicators

P = load_panel(['scratch_edge_3m_30d.npz'], '3m')
I = build_indicators(P)
t = np.asarray(P['t']).astype(np.int64).ravel()
if t[0] > 1e11: t = t // 1000
syms = list(P['syms']); col = {s: j for j, s in enumerate(syms)}
hma = I['hma']; hup = I['hma_up']

rows = [json.loads(l) for l in open('logs/scalp_bot_e3_cm_ledger.jsonl', encoding='utf-8') if l.strip()]
def rate(x): return x['real_commission']/x['nominal']*100
def tgt(x): return 0.04 if x['exit_reason'] in ('TP_LIMIT','GIVEBACK_LIMIT') else 0.07
clean = [x for x in rows if rate(x) >= tgt(x)*0.7]      # entered_at 신뢰 가능(지연<=5s)

def classify(rs, lab):
    g = collections.defaultdict(lambda: [0, 0.0])
    miss = 0
    for x in rs:
        j = col.get(x['symbol'])
        if j is None: miss += 1; continue
        i = int(np.searchsorted(t, int(x['entered_at']), side='right')) - 1
        i -= 1                                           # 마지막 '마감된' 봉
        if i < 1 or i >= len(t): miss += 1; continue
        h = hma[i, j]; u = hup[i, j]
        if not np.isfinite(h) or not np.isfinite(u): miss += 1; continue
        L = x['side'] == 'LONG'
        below = x['entry_price'] < h                     # 롱 기준 '아래'
        pull = below if L else (not below)               # 눌림 = 롱이면 HMA 아래
        dir_ok = (u == 1) if L else (u == 0)
        k = ('눌림' if pull else '추격') + ('/방향정합' if dir_ok else '/방향역행')
        g[k][0] += 1; g[k][1] += x['real_net']
    n = sum(v[0] for v in g.values())
    print(f'--- {lab}  (대조 {n}건 / 제외 {miss}건)')
    for k in sorted(g, key=lambda k: -g[k][0]):
        c, s = g[k]
        print(f'    {k:<14} n={c:4d} ({100*c/n:4.1f}%)  net {s:+8.2f}  건당 {s/c:+.4f}')
    pull_n = sum(v[0] for k, v in g.items() if k.startswith('눌림'))
    print(f'    => 눌림 자리 체결 비율 {100*pull_n/n:.1f}%')

classify(clean, '두다리 640건(entered_at 신뢰)')
print()
classify(rows, '전체 1017건(참고 — entered_at 지연 오염)')
