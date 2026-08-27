"""[2026-08-14] 트레일링스탑 giveback(반납비율) 분석용 데이터 수집 스크립트.
- logs/trade_ledger.jsonl 에서 origin=bot, exit_reason in (TAKE_PROFIT, TAKE_PROFIT_MOMENTUM_LOCK)
  거래를 모두 뽑아 실제 1분봉으로 entry~exit 구간의 peak ROE(레버리지 반영)를 계산한다.
- 읽기전용 REST(futures_klines)만 사용, 0.3초 스로틀로 IP밴 방지.
- 결과를 giveback_raw.json 으로 캐싱해서 재실행시 이미 받은 심볼/구간은 재조회하지 않는다.
"""
from __future__ import annotations
import json
import time
from pathlib import Path

from bot.config import Config
from bot.exchange import Exchange

LEDGER_PATH = Path("logs/trade_ledger.jsonl")
OUT_PATH = Path("archive/scratch_scripts/giveback_raw.json")


def load_tp_trades() -> list[dict]:
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
            if r.get("exit_reason") not in ("TAKE_PROFIT", "TAKE_PROFIT_MOMENTUM_LOCK"):
                continue
            rows.append(r)
    rows.sort(key=lambda r: r["entered_at"])
    return rows


def fetch_klines(ex: Exchange, symbol: str, start_sec: float, end_sec: float):
    raw = ex.client.futures_klines(
        symbol=symbol, interval="1m",
        startTime=int(start_sec * 1000) - 5000, endTime=int(end_sec * 1000) + 5000,
        limit=1000,
    )
    return raw


def analyze_trade(ex: Exchange, trade: dict) -> dict | None:
    symbol = trade["symbol"]
    side = trade["side"]
    entry_price = trade["entry_price"]
    exit_price = trade["exit_price"]
    entered_at = trade["entered_at"]
    exited_at = trade["exited_at"]
    leverage = trade.get("leverage") or 1

    if exited_at <= entered_at:
        return None

    try:
        raw = fetch_klines(ex, symbol, entered_at, exited_at)
    except Exception as e:
        print(f"  fetch 실패 {symbol}: {e}")
        return None
    if not raw:
        return None

    # 캔들: [open_time, open, high, low, close, ...]
    hold = [k for k in raw if entered_at * 1000 <= k[6] and k[0] <= exited_at * 1000]
    if not hold:
        return None

    highs = [float(k[2]) for k in hold]
    lows = [float(k[3]) for k in hold]

    if side == "LONG":
        peak_price = max(highs)
        peak_pnl_pct = (peak_price / entry_price - 1) * 100
    else:
        peak_price = min(lows)
        peak_pnl_pct = (entry_price / peak_price - 1) * 100

    peak_roe = peak_pnl_pct * leverage
    exit_pnl_pct = ((exit_price / entry_price - 1) if side == "LONG" else (entry_price / exit_price - 1)) * 100
    exit_roe = exit_pnl_pct * leverage

    return {
        "symbol": symbol, "side": side, "entry_price": entry_price, "exit_price": exit_price,
        "entered_at": entered_at, "exited_at": exited_at, "leverage": leverage,
        "exit_reason": trade.get("exit_reason"),
        "peak_price": peak_price, "peak_roe": peak_roe, "exit_roe": exit_roe,
        "held_min": (exited_at - entered_at) / 60,
        "num_candles": len(hold),
    }


def main():
    cfg = Config()
    ex = Exchange(cfg)

    trades = load_tp_trades()
    print(f"TAKE_PROFIT 계열 거래 {len(trades)}건")

    cache = {}
    if OUT_PATH.exists():
        cache = {r["_key"]: r for r in json.loads(OUT_PATH.read_text(encoding="utf-8"))}
        print(f"기존 캐시 {len(cache)}건 로드")

    results = list(cache.values())
    done_keys = set(cache.keys())

    for i, trade in enumerate(trades):
        key = f"{trade['symbol']}_{trade['entered_at']}"
        if key in done_keys:
            continue
        r = analyze_trade(ex, trade)
        if r:
            r["_key"] = key
            results.append(r)
        if (i + 1) % 20 == 0:
            print(f"  진행 {i+1}/{len(trades)}...")
            OUT_PATH.write_text(json.dumps(results, ensure_ascii=False, indent=1), encoding="utf-8")
        time.sleep(0.3)

    OUT_PATH.write_text(json.dumps(results, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"완료 — {len(results)}건 저장: {OUT_PATH}")


if __name__ == "__main__":
    main()
