"""[2026-08-14] TRAIL_DRAWDOWN_PCT 1.3(baseline)->0.9(제안) 전체모집단 검증용
klines 수집 스크립트.

logs/trade_ledger.jsonl 의 origin=bot 전체 거래(약 1222건, exit_reason 무관)에 대해
entry~exit+20분 구간 1분봉을 수집한다. 기존 giveback_klines_cache.json을 그대로
재사용(캐시 키가 동일한 symbol_entered_exited+20min 포맷이면 히트)하고, 부족분만
0.25~0.35초 스로틀로 신규 수집한다. 읽기전용 공개 klines 엔드포인트만 사용.
"""
from __future__ import annotations
import json
import random
import time
from pathlib import Path

LEDGER_PATH = Path("logs/trade_ledger.jsonl")
CACHE_PATH = Path(__file__).resolve().parent / "giveback_klines_cache.json"
OUT_RAW_PATH = Path(__file__).resolve().parent / "trail09_all_trades_raw.json"


def load_all_bot_trades():
    rows = []
    with open(LEDGER_PATH, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except Exception:
                continue
            if r.get("origin") != "bot":
                continue
            if r.get("exited_at") is None or r.get("entered_at") is None:
                continue
            if r["exited_at"] <= r["entered_at"]:
                continue
            rows.append(r)
    rows.sort(key=lambda r: r["entered_at"])
    for r in rows:
        r["_key"] = f"{r['symbol']}_{r['entered_at']}"
    return rows


def main():
    from bot.config import Config
    from bot.exchange import Exchange

    cfg = Config()
    ex = Exchange(cfg)

    rows = load_all_bot_trades()
    print(f"origin=bot 전체 거래: {len(rows)}건")

    cache = {}
    if CACHE_PATH.exists():
        cache = json.loads(CACHE_PATH.read_text(encoding="utf-8"))
        print(f"기존 klines 캐시: {len(cache)}건")

    fetch_count = 0
    for i, r in enumerate(rows):
        symbol = r["symbol"]
        start_sec = r["entered_at"]
        end_sec = r["exited_at"] + 20 * 60
        cache_key = f"{symbol}_{int(start_sec)}_{int(end_sec)}"
        if cache_key not in cache:
            try:
                raw = ex.client.futures_klines(
                    symbol=symbol, interval="1m",
                    startTime=int(start_sec * 1000) - 5000, endTime=int(end_sec * 1000) + 5000,
                    limit=1000,
                )
                cache[cache_key] = raw
                fetch_count += 1
            except Exception as e:
                print(f"  fetch 실패 {symbol} @ {r['entered_at']}: {e}")
                cache[cache_key] = []
            time.sleep(0.25 + random.random() * 0.1)
            if fetch_count % 30 == 0 and fetch_count > 0:
                print(f"  신규수집 {fetch_count}건 진행, 전체 {i+1}/{len(rows)}")
                CACHE_PATH.write_text(json.dumps(cache), encoding="utf-8")
        r["_cache_key"] = cache_key

    CACHE_PATH.write_text(json.dumps(cache), encoding="utf-8")
    OUT_RAW_PATH.write_text(json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"완료 — 신규수집 {fetch_count}건, 전체 캐시 {len(cache)}건")
    print(f"거래 메타 저장: {OUT_RAW_PATH}")


if __name__ == "__main__":
    main()
