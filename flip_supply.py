# -*- coding: utf-8 -*-
"""전환봉 필터가 **거래수**에 미치는 영향을 과거 데이터로 잰다(원칙 1 판정).

핵심 질문: 봇의 진입 속도를 정하는 것이 '신호 공급'인가 '슬롯 회전'인가.
  - 공급 제약이면 신호가 1/4 이 되면 거래도 1/4 이 된다 -> 원칙 1 위배
  - 회전 제약이면 신호가 줄어도 슬롯이 비는 만큼 채우므로 거래수는 거의 유지된다

라이브 실측 funnel 과 대조해 판정한다.
"""
import json, sys, time, statistics as st
sys.path.insert(0, ".")
from scripts.scalp_bot_e3 import _series_ma, ema_last
SP = sys.argv[1]

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

def count(raw, flip_max=None):
    """시간대(3분봉 시각)별 신호 수. 4h 정합만 센다(현 라이브 조건)."""
    per_bar = {}
    for sym, bars in raw.items():
        b3 = resample(bars, 3)
        if len(b3) < 60: continue
        close=[x[4] for x in b3]; vol=[x[5] for x in b3]
        hs=_series_ma(close,vol,20,4,7,second=False)
        hb=resample(bars,240)
        if len(hb)<5: continue
        hc=[x[4] for x in hb]; h4=[None]*len(b3); k=0
        for i,x in enumerate(b3):
            while k+1<len(hb) and hb[k+1][0]<=x[0]: k+=1
            seg=hc[:k+1]
            h4[i]=(seg[-1]>ema_last(seg,min(200,len(seg)))) if len(seg)>=3 else None
        ups=[(hs[i]>=hs[i-2]) if (i>=2 and hs[i] and hs[i-2]) else None for i in range(len(b3))]
        for i in range(40,len(b3)-1):
            a=hs[i]
            if a is None or a<=0 or ups[i] is None or h4[i] is None: continue
            up=ups[i]; c=close[i]
            side="LONG" if (up and c>a) else "SHORT" if (not up and c<a) else None
            if side is None: continue
            if (side=="LONG")!=h4[i]: continue
            if flip_max is not None:
                age=0; j=i
                while j>0:
                    pu=(hs[j-1]>=hs[j-3]) if (j>=3 and hs[j-1] and hs[j-3]) else None
                    if pu is None or pu!=up: break
                    age+=1; j-=1
                    if age>flip_max: break
                if age>flip_max: continue
            per_bar[b3[i][0]] = per_bar.get(b3[i][0],0)+1
    return per_bar

if __name__=="__main__":
    RAW=json.load(open(SP+"/klines_1m.json"))
    base=count(RAW,None); flt=count(RAW,2)
    hrs=(max(base)-min(base))/1000/3600
    print("="*80)
    print("[전환봉 필터의 거래수 영향] 85심볼 x 3일, 4h 정합 신호만")
    print("="*80)
    print(f"{'조건':<22}{'총신호':>9}{'시간당':>9}{'봇소화(18/h)':>14}{'소화율':>9}")
    for nm,d in (("현행 (전환필터 없음)",base),("전환 후 2봉 이내",flt)):
        n=sum(d.values())
        print(f"{nm:<22}{n:>9}{n/hrs:>9.0f}{18:>14}{18/(n/hrs)*100:>8.2f}%")
    print()
    # 3분봉마다 동시에 몇 개가 나오는가 = 한 사이클에 고를 수 있는 후보 수
    for nm,d in (("현행",base),("전환2봉",flt)):
        v=sorted(d.values())
        z=len([1 for x in d.values() if x==0])
        print(f"{nm:<10} 3분봉당 신호수  중앙 {st.median(v):>4.0f}  p10 {v[len(v)//10]:>3}  "
              f"최소 {v[0]:>3}   신호 0개인 봉 {z}개")
