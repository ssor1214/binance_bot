"""[분석전용] TP/SL 유예(defer) 재검증용 1분봉 수집 스크립트.
공개 klines 엔드포인트만 사용 (API 키 불필요), 0.4초 스로틀 + 20콜마다 추가 휴식.
IP밴 재발 방지 최우선. 라이브 코드는 절대 건드리지 않음 - 순수 조회/캐시 저장.

거래원장(logs/trade_ledger.jsonl)의 origin=bot, exit_reason in
{TAKE_PROFIT, TAKE_PROFIT_MOMENTUM_LOCK, STOP_LOSS, EXTERNAL_CLOSE_LOSS} 거래들에 대해
심볼별로 필요한 시간창을 병합해서 최소 호출 수로 1분봉을 받아 로컬 캐시에 저장한다.
"""
import json
import time
import requests
from pathlib import Path
from collections import defaultdict

BASE = "https://fapi.binance.com/fapi/v1/klines"
LEDGER = Path("logs/trade_ledger.jsonl")
CACHE_PATH = Path("archive/scratch_scripts/scratch_tp_sl_defer_recheck_klines_cache.json")

TP_REASONS = {"TAKE_PROFIT", "TAKE_PROFIT_MOMENTUM_LOCK"}
SL_REASONS = {"STOP_LOSS", "EXTERNAL_CLOSE_LOSS"}

PRE_SL_SEC = 90 * 60     # SL side: indicator warmup lookback before trigger
POST_SL_SEC = 70         # SL side: small buffer after trigger for defer simulation
POST_TP_SEC = 130        # TP side: lookahead for defer simulation


def load_ledger():
    recs = []
    with open(LEDGER, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            recs.append(json.loads(line))
    return recs


def merge_windows(windows):
    """windows: list of (start,end) epoch seconds -> merged, sorted, non-overlapping list."""
    if not windows:
        return []
    windows = sorted(windows)
    merged = [list(windows[0])]
    for s, e in windows[1:]:
        if s <= merged[-1][1] + 5:  # small gap tolerance to merge adjacent windows
            merged[-1][1] = max(merged[-1][1], e)
        else:
            merged.append([s, e])
    return [tuple(w) for w in merged]


def fetch_klines(symbol, start_sec, end_sec):
    start_ms = int(start_sec * 1000)
    end_ms = int(end_sec * 1000)
    out = []
    cursor = start_ms
    while cursor < end_ms:
        r = requests.get(BASE, params={
            "symbol": symbol, "interval": "1m",
            "startTime": cursor, "endTime": end_ms, "limit": 1000,
        }, timeout=10)
        r.raise_for_status()
        kl = r.json()
        if not kl:
            break
        out.extend(kl)
        last_open = kl[-1][0]
        if last_open <= cursor:
            break
        cursor = last_open + 60_000
        if len(kl) < 1000:
            break
        time.sleep(0.4)
    return out


def main():
    recs = load_ledger()
    bot_recs = [r for r in recs if r.get("origin") == "bot"]

    by_symbol_windows = defaultdict(list)
    for r in bot_recs:
        reason = r.get("exit_reason")
        exited_at = r.get("exited_at")
        entered_at = r.get("entered_at")
        if exited_at is None:
            continue
        if reason in TP_REASONS:
            by_symbol_windows[r["symbol"]].append((exited_at - 5, exited_at + POST_TP_SEC))
        elif reason in SL_REASONS:
            # [fix] indicator warmup (EMA/MACD/RSI, ~61 candles) needs lookback *before* the
            # trigger regardless of how long the position was held - bounding this by
            # entered_at (as an earlier version of this script did) starves quick trades
            # (SL median hold ~1.8min) of enough pre-trigger history and silently disables
            # the recovery-signal check for the majority of cases. Always use the full
            # PRE_SL_SEC lookback.
            lookback_start = exited_at - PRE_SL_SEC
            by_symbol_windows[r["symbol"]].append((lookback_start, exited_at + POST_SL_SEC))

    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    cache = {}
    if CACHE_PATH.exists():
        cache = json.loads(CACHE_PATH.read_text(encoding="utf-8"))

    total_calls = 0
    symbols = sorted(by_symbol_windows.keys())
    print(f"symbols to fetch: {len(symbols)}, total case-windows: {sum(len(v) for v in by_symbol_windows.values())}")

    for si, symbol in enumerate(symbols):
        merged = merge_windows(by_symbol_windows[symbol])
        sym_cache = cache.setdefault(symbol, {})
        for (s, e) in merged:
            key = f"{int(s)}_{int(e)}"
            if key in sym_cache:
                continue
            try:
                kl = fetch_klines(symbol, s, e)
                sym_cache[key] = kl
                total_calls += 1
                print(f"[{si+1}/{len(symbols)}] {symbol} {key} -> {len(kl)} candles")
            except Exception as ex:
                print(f"[{symbol}] fetch fail {key}: {ex}")
            time.sleep(0.4)
            if total_calls % 20 == 0 and total_calls > 0:
                time.sleep(3)
        # periodic save so partial progress survives interruption
        CACHE_PATH.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")

    CACHE_PATH.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")
    print(f"done. total merged-window calls executed: {total_calls}")


if __name__ == "__main__":
    main()
