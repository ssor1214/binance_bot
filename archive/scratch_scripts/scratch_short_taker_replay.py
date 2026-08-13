"""[2026-08-13] SHORT 전용 taker imbalance 임계값 검증 — 실거래 이벤트 리플레이.

offline_backtest.py의 합성 스윕엔진이 아니라, logs/trade_ledger.jsonl의 실제 SHORT
진입 건들을 과거 1분봉으로 재구성해서, 진입 당시 신호캔들의 taker_buy_ratio를 그대로
가져와 "이 임계값이었다면 이 진입이 실제로 통과했을지"를 실측 재생한다.

REST는 읽기전용 futures_klines만 사용, 라이브 봇과 같은 API 키로 스로틀(0.2s)해서
IP밴 사고 재발을 막는다.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

import pandas as pd

from bot.config import Config
from bot.exchange import Exchange

LEDGER_PATH = Path("logs/trade_ledger.jsonl")


def load_short_trades() -> list[dict]:
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
        startTime=int(start_sec * 1000), endTime=int(end_sec * 1000), limit=10,
    )
    df = pd.DataFrame(raw, columns=[
        "open_time", "open", "high", "low", "close", "volume",
        "close_time", "quote_asset_volume", "num_trades",
        "taker_buy_base", "taker_buy_quote", "ignore",
    ])
    for col in ("open", "high", "low", "close", "volume", "taker_buy_base"):
        df[col] = df[col].astype(float)
    df["open_time_ms"] = df["open_time"].astype(float)
    return df


def signal_candle_ratio(ex: Exchange, trade: dict) -> float | None:
    """진입은 신호캔들 '다음 캔들 시가'에 체결되므로(lookahead 방지), entered_at보다
    약 0~2분 전에 마감한 신호캔들을 찾아 taker_buy_ratio를 계산한다."""
    symbol = trade["symbol"]
    entered_at = trade["entered_at"]
    try:
        df = fetch_klines(ex, symbol, entered_at - 240, entered_at + 5)
    except Exception:
        return None
    if df.empty:
        return None
    entered_ms = entered_at * 1000
    df = df.sort_values("open_time_ms").reset_index(drop=True)
    # entered_at이 속한(체결된) 캔들 = fill 캔들(다음 캔들 시가 체결이므로 신호캔들이 아님).
    # 그 fill 캔들 "바로 이전" 캔들이 실제로 신호를 발생시킨 캔들이다.
    fill_idx = None
    for i in range(len(df) - 1):
        if df.loc[i, "open_time_ms"] <= entered_ms < df.loc[i + 1, "open_time_ms"]:
            fill_idx = i
            break
    if fill_idx is None:
        # entered_at이 마지막 캔들 구간에 들어간 경우
        cand = df[df["open_time_ms"] <= entered_ms]
        if cand.empty:
            return None
        fill_idx = cand.index[-1]
    signal_idx = fill_idx - 1
    if signal_idx < 0:
        return None
    signal = df.loc[signal_idx]
    if signal["volume"] <= 0:
        return None
    return float(signal["taker_buy_base"] / signal["volume"])


def main():
    cfg = Config()
    ex = Exchange(cfg)

    trades = load_short_trades()
    print(f"SHORT 거래 {len(trades)}건 리플레이 시작...")

    results = []
    for i, t in enumerate(trades):
        ratio = signal_candle_ratio(ex, t)
        if ratio is not None:
            results.append({
                "symbol": t["symbol"], "ratio": ratio,
                "pnl": t.get("estimated_pnl_usdt", 0) or 0,
                "entered_at": t["entered_at"],
            })
        if (i + 1) % 50 == 0:
            print(f"  진행 {i+1}/{len(trades)}...")
        time.sleep(0.2)

    print(f"복원 성공 {len(results)}/{len(trades)}건")

    with open("scratch_short_taker_replay_result.json", "w", encoding="utf-8") as f:
        json.dump(results, f)

    thresholds = {
        "baseline(0.50, live 현재)": 0.50,
        "0.43": 0.43,
        "0.40": 0.40,
        "0.38": 0.38,
    }

    print(f"\n{'임계값':<22}{'통과건수':>8}{'승수':>6}{'패수':>6}{'승률':>8}{'순손익':>10}{'PF':>8}{'건당EV':>10}")
    for name, cap in thresholds.items():
        passed = [r for r in results if r["ratio"] <= cap]
        wins = [r for r in passed if r["pnl"] > 0]
        losses = [r for r in passed if r["pnl"] <= 0]
        net = sum(r["pnl"] for r in passed)
        gross_win = sum(r["pnl"] for r in wins)
        gross_loss = -sum(r["pnl"] for r in losses)
        pf = (gross_win / gross_loss) if gross_loss > 0 else float("inf")
        wr = (len(wins) / len(passed) * 100) if passed else 0.0
        ev = (net / len(passed)) if passed else 0.0
        print(f"{name:<22}{len(passed):>8}{len(wins):>6}{len(losses):>6}{wr:>7.1f}%{net:>10.3f}{pf:>8.2f}{ev:>10.4f}")


if __name__ == "__main__":
    main()
