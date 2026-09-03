"""e6 shadow fills: signal/virtual entry only, never submits an order."""
from __future__ import annotations
import json, math, time
from pathlib import Path
import numpy as np

ROOT = Path(__file__).resolve().parent.parent
CACHE = ROOT / "logs" / "ws_worker_cache.json"
OUT = ROOT / "logs" / "e6_shadow_fills.jsonl"
REENTRY = 0.002
TP_FRAC = 0.42
SL_PCT = 0.006
seen = set()
active = []

def mark(symbol, fallback):
    # REST read only; failure falls back to the latest closed candle.
    try:
        import sys
        sys.path.insert(0, str(ROOT / "scripts"))
        from scalp_bot_e3 import Config, Exchange
        return float(Exchange(Config()).get_mark_price(symbol))
    except Exception:
        return fallback

def main():
    while True:
        try:
            data = json.loads(CACHE.read_text(encoding="utf-8"))
            rows = data.get("rows_by_symbol", {})
            now = time.time()
            for sym, raw in rows.items():
                if len(raw) < 25: continue
                key = (sym, int(raw[-1].get("open_time", 0)))
                if key in seen: continue
                seen.add(key)
                c = np.array([float(x["close"]) for x in raw[-1440:]])
                h = float(raw[-1]["high"]); l = float(raw[-1]["low"]); close = float(raw[-1]["close"])
                mid = float(c[-20:].mean()); sd = float(c[-20:].std(ddof=0))
                lo, hi = mid - 2*sd, mid + 2*sd
                if not (lo > 0 and hi > 0): continue
                widths = []
                for k in range(20, len(c) + 1):
                    w = c[k-20:k]
                    m = float(w.mean())
                    if m > 0: widths.append(float(4*w.std(ddof=0)/m))
                if len(widths) < 20: continue
                rank = sum(x < widths[-1] for x in widths[:-1]) / max(1, len(widths)-1)
                if not (0.80 <= rank < 1.00): continue
                side = "LONG" if l <= lo and close >= lo*(1+REENTRY) else ("SHORT" if h >= hi and close <= hi*(1-REENTRY) else "")
                if not side: continue
                ent = lo if side == "LONG" else hi
                tp = ent + (mid-ent)*TP_FRAC
                sl = ent*(1-SL_PCT) if side == "LONG" else ent*(1+SL_PCT)
                item = {"symbol":sym,"side":side,"signal_at":now,"bar":key[1],"entry":ent,"tp":tp,"sl":sl,"p5":None,"p15":None,"status":"OPEN"}
                item["mark_entry"] = mark(sym, close)
                active.append(item)
            for item in list(active):
                age = time.time()-item["signal_at"]
                if item["p5"] is None and age >= 5: item["p5"] = mark(item["symbol"], item["entry"])
                if item["p15"] is None and age >= 15: item["p15"] = mark(item["symbol"], item["entry"])
                if item["p15"] is not None and age >= 15:
                    item["status"] = "OBSERVED"
                    with OUT.open("a", encoding="utf-8") as f: f.write(json.dumps(item, ensure_ascii=False)+"\n")
                    active.remove(item)
            time.sleep(5)
        except Exception:
            time.sleep(5)
if __name__ == "__main__": main()
