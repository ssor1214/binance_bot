"""[2026-08-16] v4 step 1/2: fetch tick data to test whether the EXISTING live passive
limit order (LIMIT_ENTRY_ENABLED=true, LIMIT_ENTRY_PULLBACK_PCT=0.0 -> order price == best
bid/ask at signal time, LIMIT_ENTRY_WAIT_SEC=10s) would actually have FILLED within its
10s window for the 17 early_entry_spike-tagged trades from
scratch_spike_entry_results_20260815.json, versus the new aggressive (spread-crossing)
fill this session adds to bot/main.py place_entry_order()/execute_entry() for
candidate["early_entry_spike"]==True.

We don't have historical order-book (bid/ask) snapshots, only trade ticks, so we use a
conservative, clearly-labeled proxy: pullback_pct=0 means the passive order sits exactly
at the last traded price at signal time (baseline_entry_price). We treat that passive
order as FILLED at time t if any subsequent trade tick within
[entered_at, entered_at+LIMIT_ENTRY_WAIT_SEC] trades AT OR THROUGH that price in the
direction that would execute a resting order (LONG: a sell trade prints at or below
baseline_entry_price; SHORT: a buy trade prints at or above baseline_entry_price). This is
a proxy, not a guarantee (real fills also need queue priority), so results are reported as
an upper bound on the passive path's real fill rate, and the honest limitation is called
out.

IP-ban precaution: 17 symbols only (one 12s window per symbol), single page virtually
always covers 12s of aggTrades for these thin/mid-liquidity symbols, 0.8s sleep between
every HTTP call, and an extra 2.5s rest every 5 calls (well under the 20-call threshold
noted in the incident writeup, applied conservatively). No re-use of the live bot's API
key -- separate throttled REST calls to the public fapi endpoint only.
"""
from __future__ import annotations

import json
import time
import urllib.request
from pathlib import Path

RESULTS_PATH = Path(r"c:\Users\lg\Desktop\binance-futures-bot\archive\scratch_scripts\scratch_spike_entry_results_20260815.json")
OUT_PATH = Path(r"C:\Users\lg\AppData\Local\Temp\claude\c--Users-lg-Desktop-binance-futures-bot\0dad9f52-9f21-4dc5-ad53-9d30561acc41\scratchpad\spike_v4_fill_probability.json")

BASE_URL = "https://fapi.binance.com/fapi/v1/aggTrades"
SLEEP_SEC = 0.8
LIMIT_ENTRY_WAIT_SEC = 10.0


def fetch_agg_trades(symbol: str, start_ms: int, end_ms: int, max_pages: int = 3) -> list:
    out = []
    cur_start = start_ms
    for _ in range(max_pages):
        url = f"{BASE_URL}?symbol={symbol}&startTime={cur_start}&endTime={end_ms}&limit=1000"
        req = urllib.request.Request(url, headers={"User-Agent": "scratch-backtest-v4/1.0"})
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = json.loads(resp.read())
        except Exception as e:
            print(f"  fetch error {symbol}: {e}")
            time.sleep(SLEEP_SEC)
            break
        time.sleep(SLEEP_SEC)
        if not data:
            break
        out.extend(data)
        last_t = data[-1]["T"]
        if last_t >= end_ms or len(data) < 1000:
            break
        cur_start = last_t + 1
    return out


def main() -> None:
    data = json.loads(RESULTS_PATH.read_text(encoding="utf-8"))
    spikes = [r for r in data if r.get("spike_found")]
    print(f"{len(spikes)} early_entry_spike-tagged trades to check")

    results = []
    for i, r in enumerate(spikes):
        symbol = r["symbol"]
        side = r["side"]
        entered_ms = int(r["entered_at"] * 1000)
        entry_price = r["baseline_entry_price"]
        window_end_ms = entered_ms + int(LIMIT_ENTRY_WAIT_SEC * 1000)

        print(f"[{i+1}/{len(spikes)}] {symbol} {side} entered={entered_ms}")
        raw = fetch_agg_trades(symbol, entered_ms, window_end_ms, max_pages=3)

        row = {"symbol": symbol, "side": side, "entered_at": r["entered_at"],
               "baseline_entry_price": entry_price, "tick_count": len(raw)}
        if not raw:
            row["passive_fill_checkable"] = False
            row["passive_would_fill"] = None
        else:
            row["passive_fill_checkable"] = True
            fill_time_ms = None
            for d in raw:
                p = float(d["p"])
                t = int(d["T"])
                if side == "LONG" and p <= entry_price:
                    fill_time_ms = t
                    break
                if side == "SHORT" and p >= entry_price:
                    fill_time_ms = t
                    break
            row["passive_would_fill"] = fill_time_ms is not None
            if fill_time_ms is not None:
                row["passive_fill_delay_sec"] = (fill_time_ms - entered_ms) / 1000
        results.append(row)

        if (i + 1) % 5 == 0:
            print("  ...resting 2.5s (throttle precaution)")
            time.sleep(2.5)

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(results, indent=2), encoding="utf-8")
    print("Saved:", OUT_PATH)


if __name__ == "__main__":
    main()
