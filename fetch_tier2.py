"""86~150위 심볼 3일치 1분봉. 스로틀 필수(IP밴 사고 이력)."""
import json, time, os, sys
from bot.config import Config
from bot.exchange import Exchange
SP = sys.argv[1]
ex = Exchange(Config()); c = ex.client
syms = ex.get_active_usdt_perpetual_symbols(limit=150)
tier2 = syms[85:150]
json.dump({"top85": syms[:85], "tier2": tier2}, open(SP + "/symbol_tiers.json", "w"))
print(f"2군 {len(tier2)}개", flush=True)
end = int(time.time() * 1000); start = end - 3 * 86400 * 1000
out = {}
for i, s in enumerate(tier2):
    bars = []; cur = start
    try:
        while cur < end:
            kl = c.futures_klines(symbol=s, interval="1m", startTime=cur, limit=1000)
            if not kl: break
            bars += [[int(k[0]), float(k[1]), float(k[2]), float(k[3]), float(k[4]), float(k[5])] for k in kl]
            nxt = int(kl[-1][0]) + 60000
            if nxt <= cur: break
            cur = nxt
            time.sleep(0.25)
    except Exception as e:
        print(f"  {s} 실패 {e}", flush=True)
    if bars: out[s] = bars
json.dump(out, open(SP + "/klines_1m_tier2.json", "w"))
print(f"저장 {len(out)}심볼 {sum(len(v) for v in out.values())}봉", flush=True)
