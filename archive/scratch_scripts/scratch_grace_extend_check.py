"""[분석전용] 진입유예(STOP_LOSS_GRACE_SEC) 180초 -> 250초 연장 검증.

질문: 유예만료 직후(170~200초)에 손절된 거래들이, 70초를 더 줬다면 회복했을까?
방법: 각 거래의 청산시각 이후 실제 1분봉을 조회해, 유예를 250초까지 줬다면
      (a) 그 사이 넓힌 손절선(15% ROE)에 닿았는지  (b) 250초 시점 ROE가 얼마였는지 비교.
lookahead 없음(이미 발생한 실제 캔들만 사용). 공개 klines, 0.4초 스로틀(IP밴 방지).
"""
import json, time, datetime, requests

BASE = "https://fapi.binance.com/fapi/v1/klines"
GRACE_OLD, GRACE_NEW = 180.0, 250.0

recs = []
with open('logs/trade_ledger.jsonl', encoding='utf-8') as f:
    for line in f:
        try: recs.append(json.loads(line))
        except Exception: pass

cutR = time.mktime(time.strptime('2026-08-14 12:09:30', '%Y-%m-%d %H:%M:%S'))
targets = [t for t in recs
           if t.get('origin') == 'bot' and t.get('exit_reason') == 'STOP_LOSS'
           and (t.get('exited_at') or 0) >= cutR
           and 170 <= t.get('held_seconds', 0) <= 200]
print(f"대상: 유예만료 직후 손절 {len(targets)}건\n")

def fetch(symbol, start_s, end_s):
    r = requests.get(BASE, params={"symbol": symbol, "interval": "1m",
                                    "startTime": int(start_s*1000), "endTime": int(end_s*1000),
                                    "limit": 20}, timeout=10)
    r.raise_for_status()
    return r.json()

improved = worsened = same = hit_wide = 0
deltas = []
for t in targets:
    sym, side = t['symbol'], t['side']
    entry, lev = t['entry_price'], t.get('leverage') or 1
    entered, exited = t['entered_at'], t['exited_at']
    # 연장 구간: 실제 청산시각 ~ 진입후 250초
    ext_end = entered + GRACE_NEW
    if ext_end <= exited:
        same += 1
        continue
    try:
        kl = fetch(sym, exited - 60, ext_end + 60)
    except Exception as e:
        print(f"  {sym}: 조회실패 {e}"); time.sleep(0.5); continue
    time.sleep(0.4)
    if not kl:
        continue

    def roe(px):
        chg = (px - entry) / entry * 100 if side == 'LONG' else (entry - px) / entry * 100
        return chg * lev

    wide_stop_roe = -15.0  # 6% * 2.5 (유예 중 기준)
    worst = None; final_px = None
    for k in kl:
        ot = k[0] / 1000
        if ot < exited - 60 or ot > ext_end:
            continue
        hi, lo, cl = float(k[2]), float(k[3]), float(k[4])
        adverse = lo if side == 'LONG' else hi
        r = roe(adverse)
        worst = r if worst is None else min(worst, r)
        final_px = cl
    if final_px is None:
        continue
    old_roe = (t.get('estimated_pnl_pct') or 0) * lev
    new_roe = wide_stop_roe if (worst is not None and worst <= wide_stop_roe) else roe(final_px)
    if worst is not None and worst <= wide_stop_roe:
        hit_wide += 1
    d = new_roe - old_roe
    deltas.append(d)
    if d > 0.3: improved += 1
    elif d < -0.3: worsened += 1
    else: same += 1
    print(f"  {sym:11s} {side:5s} 기존ROE={old_roe:+7.2f}% -> 250초연장시={new_roe:+7.2f}%  Δ={d:+7.2f}%p"
          + ("  [연장중 넓은손절 도달]" if (worst is not None and worst <= wide_stop_roe) else ""))

print()
print(f"개선 {improved}건 / 악화 {worsened}건 / 변화없음 {same}건")
if deltas:
    print(f"평균 Δ {sum(deltas)/len(deltas):+.2f}%p, 중앙값 {sorted(deltas)[len(deltas)//2]:+.2f}%p")
    print(f"연장 중 넓은손절(15% ROE)에 닿은 건수: {hit_wide}")
