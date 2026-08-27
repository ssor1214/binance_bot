# -*- coding: utf-8 -*-
"""전환봉 필터가 **눌림 체결률**에 미치는 영향.

봇은 신호가 나면 EMA5 되돌림을 기다렸다 지정가로 들어간다. TTL 안에 안 오면 포기한다.
전환 직후는 가격이 빨리 움직여 되돌림이 안 올 수 있고, 그러면 신호가 많아도
실제 진입은 안 늘어난다. 실효 공급 = 신호수 x 체결률.
"""
import json, sys, statistics as st
sys.path.insert(0, ".")
from scripts.scalp_bot_e3 import _series_ma, ema_last
SP = sys.argv[1]
TTL_SEC = 60          # 라이브 실측 45~79초

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

def scan(raw, flip_max=None):
    sig = fill = 0
    for sym, bars in raw.items():
        b3 = resample(bars, 3)
        if len(b3) < 60: continue
        close=[x[4] for x in b3]; vol=[x[5] for x in b3]
        hs=_series_ma(close,vol,20,4,7,second=False)
        idx={x[0]:k for k,x in enumerate(bars)}
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
            if side is None or (side=="LONG")!=h4[i]: continue
            if flip_max is not None:
                age=0; j=i
                while j>0:
                    pu=(hs[j-1]>=hs[j-3]) if (j>=3 and hs[j-1] and hs[j-3]) else None
                    if pu is None or pu!=up: break
                    age+=1; j-=1
                    if age>flip_max: break
                if age>flip_max: continue
            sig+=1
            ema5=ema_last(close[:i+1],5)
            k0=idx.get(b3[i+1][0])
            if k0 is None: continue
            for jj in range(k0, min(k0+TTL_SEC//60+1, len(bars))):
                if (bars[jj][3]<=ema5) if side=="LONG" else (bars[jj][2]>=ema5):
                    fill+=1; break
    return sig, fill

if __name__=="__main__":
    RAW=json.load(open(SP+"/klines_1m.json"))
    hrs=72.0
    print("="*84)
    print(f"[눌림 체결률] TTL {TTL_SEC}초 안에 EMA5 를 터치하는가 (85심볼 x 3일)")
    print("="*84)
    print(f"{'조건':<22}{'신호':>8}{'체결':>8}{'체결률':>9}{'실효공급/h':>12}{'봇18/h 대비':>12}")
    for nm,fm in (("현행 (전환필터 없음)",None),("전환 후 2봉 이내",2)):
        s,f=scan(RAW,fm)
        eff=f/hrs
        print(f"{nm:<22}{s:>8}{f:>8}{f/s*100:>8.1f}%{eff:>12.0f}{eff/18:>11.1f}x")
