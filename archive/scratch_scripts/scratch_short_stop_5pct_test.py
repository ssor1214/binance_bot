"""[2026-08-13] SHORT_STOP_LOSS_PCT 8.0 -> 5.0 검증 (실제 1분봉 이벤트 리플레이).

방법:
  - logs/trade_ledger.jsonl 에서 origin=bot, side=SHORT 진입 건 전체 추출
  - 각 건에 대해 진입~진입+최대 3시간 구간의 실제 1분봉을 조회(읽기전용 REST, 스로틀)
  - 실제 가격 경로 위에서 stop_price = entry_price * (1 + pct/100/leverage) (SHORT)
  - 8.0%와 5.0% 각각에 대해 high가 그 선에 처음 도달하는 시각을 찾음
  - 원래 실제 청산(exited_at, exit_reason, pnl)과 비교해서 "가상 손절이 실제 청산보다 먼저
    도달했는지" 판정. 먼저 도달하면 그 시점에 손절가로 청산된 것으로 간주(실제 pnl 대체).
    가상 손절 도달 전에 실제로 청산됐다면 실제 결과를 그대로 사용(둘 다 baseline/variant 동일).
  - lookahead 없음: 각 트레이드마다 그 트레이드의 실제 캔들 경로만 사용, 미래 정보 사용 안함.

주의: 같은 라이브 API 키로 REST 반복호출 -> 스로틀(sleep) 필수, IP밴 재발방지.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

import pandas as pd

from bot.config import Config
from bot.exchange import Exchange

LEDGER_PATH = Path("logs/trade_ledger.jsonl")
CACHE_PATH = Path("scratch_short_klines_cache.json")
MAX_WINDOW_SEC = 3 * 3600  # 진입 후 최대 3시간까지만 조회(대부분 44분 이내 종료)


def load_short_trades() -> list[dict]:
    rows = []
    with open(LEDGER_PATH, encoding="utf-8", errors="ignore") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except Exception:
                continue
            if r.get("origin", "bot") != "bot":
                continue
            if r.get("side") != "SHORT":
                continue
            rows.append(r)
    rows.sort(key=lambda r: r["entered_at"])
    return rows


def fetch_klines(ex: Exchange, symbol: str, start_sec: float, end_sec: float) -> pd.DataFrame:
    raw = ex.client.futures_klines(
        symbol=symbol, interval="1m",
        startTime=int(start_sec * 1000), endTime=int(end_sec * 1000), limit=1000,
    )
    df = pd.DataFrame(raw, columns=[
        "open_time", "open", "high", "low", "close", "volume",
        "close_time", "quote_asset_volume", "num_trades",
        "taker_buy_base", "taker_buy_quote", "ignore",
    ])
    for col in ("open", "high", "low", "close", "volume"):
        df[col] = df[col].astype(float)
    df["open_time_sec"] = df["open_time"] / 1000.0
    return df


def simulate_stop(df: pd.DataFrame, entry_price: float, entered_at: float, stop_pct: float, leverage: float):
    """SHORT 포지션에서 stop_pct(ROE%) 손절선에 처음 도달하는 시각/가격을 찾는다.
    lookahead 없음: entered_at 이후 캔들만 순서대로 확인."""
    stop_price = entry_price * (1 + stop_pct / 100.0 / leverage)
    hit = df[(df["open_time_sec"] >= entered_at) & (df["high"] >= stop_price)]
    if hit.empty:
        return None
    row = hit.iloc[0]
    return {"hit_time": row["open_time_sec"] + 60, "stop_price": stop_price}


def main():
    cfg = Config()
    ex = Exchange(cfg)

    trades = load_short_trades()
    print(f"SHORT bot 진입 {len(trades)}건 로드됨. 캔들 조회 시작...")

    cache = {}
    if CACHE_PATH.exists():
        try:
            cache = json.loads(CACHE_PATH.read_text(encoding="utf-8"))
        except Exception:
            cache = {}

    results = []
    n_fetched = 0
    for i, t in enumerate(trades):
        symbol = t["symbol"]
        entry_price = t["entry_price"]
        leverage = t.get("leverage") or 1
        entered_at = t["entered_at"]
        exited_at = t["exited_at"]
        actual_reason = t.get("exit_reason")
        actual_pnl_usdt = t.get("estimated_pnl_usdt", 0) or 0
        actual_pnl_pct = t.get("estimated_pnl_pct", 0) or 0

        window_end = min(entered_at + MAX_WINDOW_SEC, entered_at + max(exited_at - entered_at, 0) + 600)
        window_end = max(window_end, exited_at + 120)
        key = f"{symbol}_{int(entered_at)}"

        if key in cache:
            raw = cache[key]
            df = pd.DataFrame(raw)
        else:
            try:
                df = fetch_klines(ex, symbol, entered_at - 60, window_end)
            except Exception as e:
                print(f"  [스킵] {symbol} @ {entered_at}: {e}")
                continue
            n_fetched += 1
            cache[key] = df[["open_time_sec", "high", "low", "close"]].to_dict("records")
            time.sleep(0.2)
            if n_fetched % 50 == 0:
                CACHE_PATH.write_text(json.dumps(cache), encoding="utf-8")
                print(f"  진행 {i+1}/{len(trades)} (캔들조회 {n_fetched}건, 캐시저장)")

        if df.empty:
            continue

        hit8 = simulate_stop(df, entry_price, entered_at, 8.0, leverage)
        hit5 = simulate_stop(df, entry_price, entered_at, 5.0, leverage)

        results.append({
            "symbol": symbol, "entered_at": entered_at, "exited_at": exited_at,
            "entry_price": entry_price, "leverage": leverage,
            "actual_reason": actual_reason, "actual_pnl_usdt": actual_pnl_usdt,
            "actual_pnl_pct": actual_pnl_pct, "quantity": t.get("quantity"),
            "hit8": hit8, "hit5": hit5,
        })

    CACHE_PATH.write_text(json.dumps(cache), encoding="utf-8")
    print(f"분석 완료: {len(results)}건 (신규조회 {n_fetched}건)")

    Path("scratch_short_stop_results.json").write_text(
        json.dumps(results, default=str), encoding="utf-8"
    )


if __name__ == "__main__":
    main()
