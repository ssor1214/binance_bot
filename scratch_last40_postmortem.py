import json, time
from pathlib import Path
from bot.config import Config
from bot.exchange import Exchange
from scratch_trade_postmortem import fetch_historical_klines

cfg = Config()
ex = Exchange(cfg)

rows=[]
with open('logs/trade_ledger.jsonl', encoding='utf-8') as f:
    for line in f:
        line=line.strip()
        if not line: continue
        try: rows.append(json.loads(line))
        except: pass
bot_rows=[r for r in rows if r.get('origin','bot')=='bot']
since = time.time() - 2400
losses = [r for r in bot_rows if r['entered_at']>=since and (r.get('estimated_pnl_usdt',0) or 0)<0]

import pandas as pd
out=[]
for t in losses:
    symbol, side = t['symbol'], t['side']
    entry_price, exited_at = t['entry_price'], t['exited_at']
    try:
        df = fetch_historical_klines(ex, symbol, exited_at, exited_at + 900)
    except Exception:
        continue
    after = df[df['open_time'] > pd.to_datetime(exited_at, unit='s')].head(15)
    recovered=False; move=0.0
    if not after.empty:
        if side=='LONG':
            best=after['high'].max(); recovered = best>entry_price; move=(best/entry_price-1)*100
        else:
            best=after['low'].min(); recovered = best<entry_price; move=(entry_price/best-1)*100
    out.append(f"{symbol} {side} pnl={t.get('estimated_pnl_usdt'):+.3f} reason={t.get('exit_reason')} 회복={'Y' if recovered else 'N'} 추가이동={move:+.2f}%")

Path('last40_postmortem.txt').write_text('\n'.join(out), encoding='utf-8')
print('\n'.join(out))
