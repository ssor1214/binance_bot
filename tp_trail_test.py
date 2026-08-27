# -*- coding: utf-8 -*-
"""익절선을 매 신호봉마다 CM 최대익절선으로 재계산하되 **바깥으로만** 옮긴다.

손절은 고정(8% ROE) — 손절을 따라 옮기면 '무손절'에 수렴하고, 그건 오늘
"60분 창에서만 좋아 보이는 함정"으로 이미 기각됐다.
익절은 반대다: 추세가 자라면 CM 목표가 멀어지므로 따라가면 더 먹을 수 있다.
**진입가 쪽으로 당기지 않는다** — 당기면 되돌림 청산과 같아지고 그건 네 번 기각됐다.
"""
import json, sys, math, statistics as st
sys.path.insert(0, ".")
from scripts.scalp_bot_e3 import _series_ma, ema_last
SP=sys.argv[1]; LEV=5; HOR=180
FEE_TP, FEE_SL = 0.0276, 0.0586
PULL=0.5; WIN=41; PB_WAIT=60

def resample(bars,m):
    out,cur=[],None
    for t,o,h,l,c,v in bars:
        b=(t//60000)//m*m*60000
        if cur is None or cur[0]!=b:
            if cur is not None and cur[6]==m: out.append(cur[:6])
            cur=[b,o,h,l,c,v,1]
        else:
            cur[2]=max(cur[2],h);cur[3]=min(cur[3],l);cur[4]=c;cur[5]+=v;cur[6]+=1
    if cur is not None and cur[6]==m: out.append(cur[:6])
    return out

def leg_start(hs,i,up,sm=2,floor_i=0):
    k=i
    while k>max(sm+1,floor_i):
        a,b=hs[k-1],hs[k-1-sm]
        if a is None or b is None: break
        if (a>=b) if up else (a<b): k-=1
        else: break
    return max(floor_i,k)

def cap(entry,px,side,mx):
    if mx<=0: return px
    roe=((px/entry-1) if side=="LONG" else (1-px/entry))*LEV*100
    if roe<=mx: return px
    d=mx/100/LEV
    return entry*(1+d) if side=="LONG" else entry*(1-d)

def cm_target(b3,i,up,side):
    """i 봉 기준 CM 최대익절선(레그 극값) - pullback."""
    w0=max(0,i-(WIN-1))
    hs=CACHE_HS[id(b3)]
    ls=leg_start(hs,i,up,floor_i=w0)
    seg=b3[ls:i+1]
    tgt=max(x[2] for x in seg) if side=="LONG" else min(x[3] for x in seg)
    return tgt*(1-PULL/100) if side=="LONG" else tgt*(1+PULL/100)

CACHE_HS={}
def run(raw,tp_max,trail,flip_max=5,sl_roe=8.0):
    res=[];why=[]
    for sym,bars in raw.items():
        b3=resample(bars,3)
        if len(b3)<60: continue
        close=[x[4] for x in b3]; vol=[x[5] for x in b3]
        hs=_series_ma(close,vol,20,4,7,second=False); CACHE_HS[id(b3)]=hs
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
            age=0;j=i
            while j>0:
                pu=(hs[j-1]>=hs[j-3]) if (j>=3 and hs[j-1] and hs[j-3]) else None
                if pu is None or pu!=up: break
                age+=1;j-=1
                if age>flip_max: break
            if age>flip_max: continue
            wi=max(0,i-(WIN-1))
            ema5=ema_last(close[wi:i+1],5)
            k0=idx.get(b3[i+1][0])
            if k0 is None: continue
            entry=0.0;k_in=None
            for jj in range(k0,min(k0+PB_WAIT,len(bars))):
                if (bars[jj][3]<=ema5) if side=="LONG" else (bars[jj][2]>=ema5):
                    entry=ema5;k_in=jj;break
            if entry<=0: continue
            t_fill=bars[k_in][0]; i2=i
            while i2+1<len(b3) and b3[i2+1][0]<=t_fill: i2+=1
            if hs[i2] is None: continue
            tp=cap(entry,cm_target(b3,i2,up,side),side,tp_max)
            if (tp<=entry) if side=="LONG" else (tp>=entry): continue
            sl=entry*(1-sl_roe/100/LEV) if side=="LONG" else entry*(1+sl_roe/100/LEV)
            L=side=="LONG"; i3=i2; done=None
            for jj in range(k_in,min(k_in+HOR,len(bars))):
                t,o,hi,lo,cc=bars[jj][:5]
                if (lo<=sl) if L else (hi>=sl): done=(-sl_roe/LEV-FEE_SL,"SL"); break
                if (hi>=tp) if L else (lo<=tp):
                    done=(((tp/entry-1) if L else (1-tp/entry))*100-FEE_TP,"TP"); break
                if trail:
                    while i3+1<len(b3) and b3[i3+1][0]<=t:
                        i3+=1
                        if hs[i3] is None: continue
                        nt=cap(entry,cm_target(b3,i3,up,side),side,tp_max)
                        if (nt>tp) if L else (0<nt<tp): tp=nt      # 바깥으로만
            if done is None:
                cc=bars[min(k_in+HOR,len(bars))-1][4]
                done=(((cc/entry-1) if L else (1-cc/entry))*100-FEE_SL,"TIME")
            res.append(done[0]); why.append(done[1])
    return res,why

def rep(nm,res,why):
    if len(res)<50: print(f"{nm:<30}표본부족 {len(res)}"); return
    m=sum(res)/len(res); sd=st.pstdev(res); t=m/(sd/math.sqrt(len(res))); n=len(res)
    print(f"{nm:<30}{n:>7}{m:>+10.4f}%{t:>+8.2f}{sum(1 for x in res if x>0)/n*100:>7.1f}%"
          f"{why.count('TP')/n*100:>6.0f}%{why.count('SL')/n*100:>6.0f}%{why.count('TIME')/n*100:>6.0f}%")

if __name__=="__main__":
    RAW=json.load(open(SP+"/klines_1m.json"))
    print("="*90)
    print("[익절선 추적 검토] 손절 고정 8% / 전환필터 5봉 / 4h필터 / 최대보유 180분")
    print("="*90)
    print(f"{'구성':<30}{'건수':>7}{'건당%':>10}{'t':>8}{'승률':>8}{'TP':>6}{'SL':>6}{'만료':>6}")
    print("-"*90)
    for tp_max in (4.3, 8.0):
        for trail in (False, True):
            r,w=run(RAW,tp_max,trail)
            rep(f"익절상한 {tp_max} / 추적 {'ON ' if trail else 'OFF'}",r,w)
