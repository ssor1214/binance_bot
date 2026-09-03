"""E7 independent runner scaffold.

The runner is intentionally dry-run only until order lifecycle tests pass.
It owns e7_state.json and e7_ledger.jsonl and never imports e3.
"""
from __future__ import annotations
import argparse, json, time
from pathlib import Path
from scalp_bot_e7 import E7Config, e7_candidate, confirm_candidate, position_size
from e7_telegram import E7Telegram

ROOT=Path(__file__).resolve().parent.parent
STATE=ROOT/'state'/'e7_state.json'; LEDGER=ROOT/'logs'/'e7_ledger.jsonl'
MAX_SLOTS=8

def min_margin_for_balance(balance: float) -> float:
    """User-defined per-slot minimum margin tiers."""
    if balance < 50.0: return 4.0
    if balance < 100.0: return 25.0
    if balance < 300.0: return 45.0
    return 45.0

def slot_budget(balance: float, open_slots: int = 0) -> tuple[int, float]:
    """Return capacity and per-slot minimum without reserving impossible slots."""
    minimum=min_margin_for_balance(balance)
    available=max(0.0, balance * 0.90)
    capacity=min(MAX_SLOTS, max(0, int(available // minimum)))
    return max(0, capacity-open_slots), minimum

def collect_1m_candles(exchange, symbol: str, limit: int = 100):
    """Read-only 1m candles; discard the unfinished current candle."""
    df=exchange.get_klines(symbol, limit=max(60, limit), interval='1m')
    return [{'open_time':str(r['open_time']), 'open':float(r['open']),
             'high':float(r['high']), 'low':float(r['low']),
             'close':float(r['close']), 'volume':float(r['volume'])}
            for _,r in df.iloc[:-1].iterrows()]

def load_state():
    if not STATE.exists(): return {'version':1,'positions':{},'paused':False}
    return json.loads(STATE.read_text(encoding='utf-8'))

def save_state(s):
    STATE.parent.mkdir(parents=True,exist_ok=True); tmp=STATE.with_suffix('.tmp')
    tmp.write_text(json.dumps(s,ensure_ascii=False,indent=2),encoding='utf-8'); tmp.replace(STATE)

def append_ledger(row):
    LEDGER.parent.mkdir(parents=True,exist_ok=True)
    with LEDGER.open('a',encoding='utf-8') as f: f.write(json.dumps(row,ensure_ascii=False)+'\n')

def record_scan(symbol, candles):
    append_ledger({'event':'SCAN','strategy':'E7','symbol':symbol,
                   'bars':len(candles),'ts':time.time(),'dry_run':True})

def validate_order(symbol, price, qty, min_notional, step_size):
    if price <= 0 or qty <= 0: return False,'invalid price/quantity'
    if price*qty < min_notional: return False,f'min_notional {price*qty:.8f} < {min_notional}'
    if step_size > 0 and abs(round(qty/step_size)*step_size-qty) > step_size*1e-6: return False,'quantity step mismatch'
    return True,'ok'

def expire_position(state, symbol, now=None):
    p=state.get('positions',{}).get(symbol)
    if not p: return False
    now=now or time.time()
    if now-float(p['entered_at']) >= 900:
        append_ledger({'event':'TIME_STOP','symbol':symbol,'side':p['side'],'ts':now,'dry_run':True})
        del state['positions'][symbol]; save_state(state); return True
    return False

def main():
    ap=argparse.ArgumentParser(description='E7 independent Binance runner (dry-run)')
    ap.add_argument('--dry-run',action='store_true',default=True); ap.add_argument('--balance',type=float,default=4.0)
    ap.add_argument('--leverage',type=int,default=3); ap.add_argument('--telegram',action='store_true'); args=ap.parse_args()
    if args.leverage != 3: raise SystemExit('E7 requires leverage=3')
    state=load_state(); save_state(state)
    free_slots, minimum=slot_budget(args.balance, len(state.get('positions',{})))
    print('E7 runner ready: 1m collection/order lifecycle is not enabled until exchange safety tests pass.')
    print(f'state={STATE} ledger={LEDGER} dry_run=True balance={args.balance:.4f} leverage=3x')
    print(f'max_slots={MAX_SLOTS} available_slots={free_slots} min_margin={minimum:.2f} USDT')
    if args.telegram:
        tg=E7Telegram()
        if not tg.enabled: raise SystemExit('Telegram credentials missing: TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID')
        tg.send('상태 초기화 완료. /e7_status /e7_pause /e7_resume')
        tg.run()

if __name__=='__main__': main()
