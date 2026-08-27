import json, time, urllib.request, urllib.parse

START_MS = 1786290309000 - 3600*1000  # pad 1h before earliest entry
END_MS = 1786793020000 + 60*1000

OUT = "archive/scratch_scripts/scratch_btc_1m_klines.json"

def fetch_klines(start_ms, end_ms):
    url = "https://fapi.binance.com/fapi/v1/klines?" + urllib.parse.urlencode({
        "symbol": "BTCUSDT", "interval": "1m", "startTime": start_ms, "endTime": end_ms, "limit": 1500
    })
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read().decode())

all_kl = []
cur = START_MS
call_count = 0
while cur < END_MS:
    kl = fetch_klines(cur, END_MS)
    if not kl:
        break
    all_kl.extend(kl)
    last_open = kl[-1][0]
    if last_open <= cur:
        break
    cur = last_open + 60_000
    call_count += 1
    time.sleep(0.4)
    if call_count % 20 == 0:
        print(f"calls={call_count}, cur={cur}, sleeping extra 5s")
        time.sleep(5)

print("total klines fetched:", len(all_kl))
with open(OUT, "w") as f:
    json.dump(all_kl, f)
print("saved to", OUT)
