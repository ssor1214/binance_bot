"""E7-only Telegram command surface; no shared e3 state."""
from __future__ import annotations
import json, time
import os, requests
from pathlib import Path
ROOT=Path(__file__).resolve().parent.parent; STATE=ROOT/'state'/'e7_state.json'

def read_state():
    if not STATE.exists(): return {'version':1,'positions':{},'paused':False}
    return json.loads(STATE.read_text(encoding='utf-8'))

def write_state(s):
    STATE.parent.mkdir(parents=True,exist_ok=True); tmp=STATE.with_suffix('.tmp')
    tmp.write_text(json.dumps(s,ensure_ascii=False,indent=2),encoding='utf-8'); tmp.replace(STATE)

def handle(command: str) -> str:
    s=read_state(); c=command.strip().lower()
    if c in ('/e7_pause','e7_pause'): s['paused']=True; write_state(s); return 'E7 신규진입 일시정지'
    if c in ('/e7_resume','e7_resume'): s['paused']=False; write_state(s); return 'E7 신규진입 재개'
    if c in ('/e7_status','e7_status'): return f"E7 paused={s.get('paused',False)} positions={len(s.get('positions',{}))}"
    return '사용법: /e7_status /e7_pause /e7_resume'

class E7Telegram:
    """E7-only polling client. It never imports or mutates the legacy bot state."""
    def __init__(self, token=None, chat_id=None):
        self.token=token or os.getenv('TELEGRAM_BOT_TOKEN','')
        self.chat_id=str(chat_id or os.getenv('TELEGRAM_CHAT_ID',''))
        self.offset=0
        self.enabled=bool(self.token and self.chat_id)

    def send(self, text):
        if not self.enabled: return False
        r=requests.post(f'https://api.telegram.org/bot{self.token}/sendMessage',
                        json={'chat_id':self.chat_id,'text':f'[E7] {text}'},timeout=10)
        return r.ok

    def poll_once(self):
        if not self.enabled: return 0
        r=requests.get(f'https://api.telegram.org/bot{self.token}/getUpdates',
                       params={'offset':self.offset,'timeout':1},timeout=5)
        n=0
        for u in r.json().get('result',[]):
            self.offset=int(u['update_id'])+1; msg=u.get('message',{})
            if str(msg.get('chat',{}).get('id')) != self.chat_id: continue
            answer=handle(msg.get('text','')); self.send(answer); n+=1
        return n

    def run(self, interval=2.0):
        self.send('텔레그램 연결됨 (dry-run 전용)')
        while True:
            try: self.poll_once()
            except Exception as exc: self.send(f'polling 오류: {exc}')
            time.sleep(interval)
