# -*- coding: utf-8 -*-
"""손익비 개선 방향 탐색 — 실제로 잡은 거래에 손절폭/익절상한만 바꿔 재생.

하네스가 아니라 라이브 원장이 모집단이므로 진입 선택 편향이 없다.
한계: 1분봉이라 봉 안의 선착 순서를 못 가린다 -> 같은 봉이면 **SL 우선**(보수적).
"""
import json, io, re, sys, time, math, statistics as st
LEDGER="logs/scalp_bot_e3_cm_ledger.jsonl"; RUNLOG="logs/scalp_bot_e3_cm_run.log"
SINCE="2026-08-27 20:00"; LEV=5; HORIZON=180
FEE_TP, FEE_SL = 0.0276, 0.0586
def f(r,k):
    try: return float(r.get(k) or 0)
    except Exception: return 0.0

def load():
    from bot.config import Config
    from bot.exchange import Exchange
    ex=Exchange(Config())
    rows=[r for r in (json.loads(l) for l in io.open(LEDGER,encoding="utf-8") if l.strip()) if r.get("exited_at")]
    t0=time.mktime(time.strptime(SINCE,"%Y-%m-%d %H:%M"))
    v=[r for r in rows if r["exited_at"]>=t0 and abs(f(r,"real_net"))>1e-9 and f(r,"entry_price")>0]
    cache={}; out=[]
    for r in v:
        s=r["symbol"]
        if s not in cache:
            try:
                kl=ex.client.futures_klines(symbol=s,interval="1m",limit=1000)
                cache[s]=[[int(k[0])//1000,float(k[1]),float(k[2]),float(k[3]),float(k[4])] for k in kl]
            except Exception: cache[s]=[]
            time.sleep(0.12)
        b=[x for x in cache[s] if int(r["entered_at"])-60<=x[0]<=int(r["exited_at"])+HORIZON*60]
        if len(b)>=3: out.append((r,b))
    return out

def replay(r,bars,tp_roe,sl_roe,gb_arm=0.0,gb_frac=0.0,basis="high"):
    """gb_arm>0 이면 되돌림 청산도 건다.
    basis: MFE 갱신 기준. 'close'=봉 종가(하한) / 'high'=봉 고저(상한, 라이브 5초틱에 가깝다)
    우선순위: 거래소 주문(SL/TP)이 봉 안에서 먼저, 되돌림은 봉 종가로 판정(폴링 재현).
    """
    e=f(r,"entry_price"); L=r["side"]=="LONG"
    tp=e*(1+tp_roe/LEV/100) if L else e*(1-tp_roe/LEV/100)
    sl=e*(1-sl_roe/LEV/100) if L else e*(1+sl_roe/LEV/100)
    peak=0.0
    for t,o,hi,lo,c in bars:
        if t<int(r["entered_at"])-60: continue
        if (lo<=sl) if L else (hi>=sl): return -sl_roe/LEV-FEE_SL,"SL"
        if (hi>=tp) if L else (lo<=tp): return  tp_roe/LEV-FEE_TP,"TP"
        pk = hi if L else lo
        roe_pk=((pk/e-1) if L else (1-pk/e))*LEV*100
        roe_c =((c /e-1) if L else (1-c /e))*LEV*100
        peak=max(peak, roe_pk if basis=="high" else roe_c)
        if gb_arm>0 and peak>=gb_arm and roe_c<=peak*(1-gb_frac):
            return roe_c/LEV-FEE_TP,"GB"      # 지정가 시도 -> maker
    c=bars[-1][4]
    return ((c/e-1) if L else (1-c/e))*100-FEE_SL,"TIME"

if __name__=="__main__":
    data=load()
    print(f"재생 대상 {len(data)}건 (20:00 이후 원장)")
    act=sum(f(r,"real_net")/f(r,"nominal")*100 for r,_ in data if f(r,"nominal")>0)
    print(f"actual: per-trade {act/len(data):+.4f}%   (current: TP4.3 / SL8.0 / giveback OFF)")
    print("MFE 기준 = 봉 고저(상한, 라이브 5초틱에 가까움)")
    print(f"{'익절':>5}{'손절':>5}{'되돌림arm':>10}{'frac':>6}{'건당%':>10}{'t':>7}{'승률':>7}{'TP':>5}{'SL':>5}{'GB':>5}{'만료':>5}")
    print("-"*72)
    for tp_roe in (4.3, 8.0):
        for gb in ((0,0),(2.0,0.4),(3.0,0.4),(4.0,0.4),(4.0,0.3),(5.0,0.4)):
            res=[];why=[]
            for r,b in data:
                g,w=replay(r,b,tp_roe,8.0,gb[0],gb[1]); res.append(g); why.append(w)
            m=sum(res)/len(res); sd=st.pstdev(res)
            t=m/(sd/math.sqrt(len(res))) if sd>0 else 0
            n=len(res)
            tag="" if gb[0] else "  <- 되돌림 없음"
            print(f"{tp_roe:>5.1f}{8.0:>5.1f}{gb[0]:>10.1f}{gb[1]:>6.1f}{m:>+10.4f}{t:>+7.2f}"
                  f"{sum(1 for x in res if x>0)/n*100:>6.0f}%{why.count('TP')/n*100:>4.0f}%"
                  f"{why.count('SL')/n*100:>4.0f}%{why.count('GB')/n*100:>4.0f}%{why.count('TIME')/n*100:>4.0f}%{tag}")
        print()
