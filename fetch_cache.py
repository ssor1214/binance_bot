"""CM 파라미터 스윕용 1분봉 캐시. 라이브와 같은 API 키이므로 반드시 스로틀."""
import json, time, os, sys
from bot.config import Config
from bot.exchange import Exchange
SP = sys.argv[1]; DAYS = float(sys.argv[2]) if len(sys.argv) > 2 else 3.0
ex = Exchange(Config()); c = ex.client
syms = ex.get_active_usdt_perpetual_symbols()[:85]
print(f"심볼 {len(syms)}개 / {DAYS}일치 1분봉", flush=True)
end = int(time.time() * 1000)
start = end - int(DAYS * 86400 * 1000)
out = {}
for i, s in enumerate(syms):
    bars = []; cur = start
    try:
        while cur < end:
            kl = c.futures_klines(symbol=s, interval="1m", startTime=cur, limit=1000)
            if not kl: break
            bars += [[int(k[0]), float(k[1]), float(k[2]), float(k[3]), float(k[4]), float(k[5])] for k in kl]
            nxt = int(kl[-1][0]) + 60000
            if nxt <= cur: break
            cur = nxt
            time.sleep(0.25)          # 스로틀 — IP밴 사고(2026-08-13) 재발 방지
    except Exception as e:
        print(f"  {s} 실패 {e}", flush=True)
    if bars: out[s] = bars
    if (i + 1) % 10 == 0:
        print(f"  {i+1}/{len(syms)} ({sum(len(v) for v in out.values())}봉)", flush=True)
p = os.path.join(SP, "klines_1m.json")
json.dump(out, open(p, "w"))
print(f"저장 {p} — {len(out)}심볼 {sum(len(v) for v in out.values())}봉", flush=True)
