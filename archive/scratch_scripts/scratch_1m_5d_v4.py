import os, sys, time, json, io
sys.path.insert(0, os.getcwd())
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
from bot.config import Config
from binance.client import Client

cfg = Config()
client = Client(cfg.api_key, cfg.api_secret)
try:
    server_time = client.futures_time()['serverTime']
    client.timestamp_offset = server_time - int(time.time()*1000)
except Exception:
    pass

tickers = client.futures_ticker()
usdt_perp = [t for t in tickers if t['symbol'].endswith('USDT')]
ranked = sorted(usdt_perp, key=lambda t: float(t['quoteVolume']), reverse=True)
top_syms = [t['symbol'] for t in ranked[:40]]
print(f"대상 심볼: {len(top_syms)}개")

now_ms = int(time.time() * 1000)
start_ms = now_ms - 5*24*3600*1000

klines_data = {}
fail = 0
call_count = 0
aborted = False
for i, sym in enumerate(top_syms):
    if aborted:
        break
    all_kl = []
    cursor = start_ms
    try:
        while cursor < now_ms:
            kl = client.futures_klines(symbol=sym, interval='1m', startTime=cursor, limit=1000)
            call_count += 1
            time.sleep(0.3)
            if call_count % 20 == 0:
                time.sleep(2.0)
            if not kl:
                break
            all_kl.extend(kl)
            last_open = kl[-1][0]
            if last_open <= cursor:
                break
            cursor = last_open + 60000
            if len(kl) < 1000:
                break
        if len(all_kl) >= 500:
            klines_data[sym] = all_kl
    except Exception as e:
        msg = str(e)
        if "Way too many requests" in msg or "banned" in msg.lower():
            print(f"경고: 밴 조짐 감지({sym}) — 즉시 중단: {msg}")
            aborted = True
            break
        fail += 1
    if (i+1) % 10 == 0:
        print(f"진행 {i+1}/{len(top_syms)}")

print(f"수집완료: {len(klines_data)}개 (실패 {fail}, 호출 {call_count}, 중단={aborted})")
with open('scratch_klines_v4.json', 'w') as f:
    json.dump(klines_data, f)
