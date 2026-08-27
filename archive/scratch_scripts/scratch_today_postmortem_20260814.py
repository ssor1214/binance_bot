"""[2026-08-15 사용자요청] 8/14(KST) 하루 실거래 전체 복기.
읽기 전용 REST(get historical klines)만 사용, 주문/포지션 변경 없음, 라이브 코드 수정 없음.
IP밴 방지: 0.25초 스로틀.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

from bot.config import Config
from bot.exchange import Exchange

LEDGER_PATH = Path("logs/trade_ledger.jsonl")
START = 1786633200.0          # 2026-08-14 00:00 KST (UTC epoch)
END = START + 25 * 3600       # ~2026-08-15 01:00 KST
RECOVERY_WINDOW_MIN = 30
LOSS_REASONS = {"STOP_LOSS", "EXTERNAL_CLOSE_LOSS", "SOFT_STOP", "EARLY_EXIT"}


def load_today() -> list[dict]:
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
            if not (START <= r["entered_at"] <= END):
                continue
            rows.append(r)
    rows.sort(key=lambda r: r["entered_at"])
    return rows


def fetch_klines(ex: Exchange, symbol: str, start_sec: float, end_sec: float):
    import pandas as pd
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
    df["open_time"] = pd.to_datetime(df["open_time"], unit="ms")
    return df


def analyze_loss(ex: Exchange, trade: dict) -> dict | None:
    import pandas as pd
    symbol = trade["symbol"]
    side = trade["side"]
    entry_price = trade["entry_price"]
    entered_at = trade["entered_at"]
    exited_at = trade["exited_at"]
    stop_loss_pct = trade.get("config_snapshot", {}).get("stop_loss_pct", 6.0)

    try:
        df = fetch_klines(ex, symbol, entered_at - 120, exited_at + RECOVERY_WINDOW_MIN * 60 + 60)
    except Exception as e:
        return {"symbol": symbol, "error": str(e)}
    if df.empty:
        return None

    hold_df = df[(df["open_time"] >= pd.to_datetime(entered_at, unit="s")) &
                 (df["open_time"] <= pd.to_datetime(exited_at, unit="s"))]
    after_df = df[df["open_time"] > pd.to_datetime(exited_at, unit="s")].head(RECOVERY_WINDOW_MIN)
    if hold_df.empty:
        return None

    if side == "LONG":
        worst_price = hold_df["low"].min()
        mae_pct = (worst_price / entry_price - 1) * 100
    else:
        worst_price = hold_df["high"].max()
        mae_pct = (entry_price / worst_price - 1) * 100

    # 방향판단: 손절이 "진입방향 자체가 틀렸다"(추세 지속 역행) vs "노이즈에 흔들렸다"(짧게 찔렀다 회복)
    # 판정기준: 청산후 회복시각(최초로 진입가 이상/이하 도달)까지 걸린 시간
    recovered = False
    recovery_min = None
    max_favorable_after_pct = None
    if not after_df.empty:
        if side == "LONG":
            best_after = after_df["high"].max()
            recovered = best_after > entry_price
            max_favorable_after_pct = (best_after / trade["exit_price"] - 1) * 100
            for _, row in after_df.iterrows():
                if row["high"] > entry_price:
                    recovery_min = (row["open_time"] - pd.to_datetime(exited_at, unit="s")).total_seconds() / 60
                    break
        else:
            best_after = after_df["low"].min()
            recovered = best_after < entry_price
            max_favorable_after_pct = (trade["exit_price"] / best_after - 1) * 100
            for _, row in after_df.iterrows():
                if row["low"] < entry_price:
                    recovery_min = (row["open_time"] - pd.to_datetime(exited_at, unit="s")).total_seconds() / 60
                    break

    return {
        "symbol": symbol, "side": side, "reason": trade.get("exit_reason"),
        "pnl": trade.get("estimated_pnl_usdt", 0),
        "held_min": (exited_at - entered_at) / 60,
        "mae_pct": mae_pct, "stop_loss_pct": stop_loss_pct,
        "mae_vs_stop_ratio": abs(mae_pct) / stop_loss_pct if stop_loss_pct else None,
        "recovered_after_exit": recovered, "recovery_min": recovery_min,
        "max_favorable_after_pct": max_favorable_after_pct,
    }


def analyze_profit(ex: Exchange, trade: dict) -> dict | None:
    import pandas as pd
    symbol = trade["symbol"]
    side = trade["side"]
    exit_price = trade["exit_price"]
    entered_at = trade["entered_at"]
    exited_at = trade["exited_at"]

    try:
        df = fetch_klines(ex, symbol, exited_at - 30, exited_at + RECOVERY_WINDOW_MIN * 60 + 60)
    except Exception as e:
        return {"symbol": symbol, "error": str(e)}
    if df.empty:
        return None

    after_df = df[df["open_time"] > pd.to_datetime(exited_at, unit="s")].head(RECOVERY_WINDOW_MIN)
    if after_df.empty:
        return None

    if side == "LONG":
        best_after = after_df["high"].max()
        worst_after = after_df["low"].min()
        extra_upside_pct = (best_after / exit_price - 1) * 100
        giveback_risk_pct = (exit_price / worst_after - 1) * 100  # 양수면 더 버텼으면 반납/역전됐을 폭
    else:
        best_after = after_df["low"].min()
        worst_after = after_df["high"].max()
        extra_upside_pct = (exit_price / best_after - 1) * 100
        giveback_risk_pct = (worst_after / exit_price - 1) * 100

    return {
        "symbol": symbol, "side": side, "reason": trade.get("exit_reason"),
        "pnl": trade.get("estimated_pnl_usdt", 0),
        "held_min": (exited_at - entered_at) / 60,
        "extra_upside_pct": extra_upside_pct,
        "giveback_risk_pct": giveback_risk_pct,
    }


def main():
    cfg = Config()
    ex = Exchange(cfg)

    rows = load_today()
    print(f"오늘(2026-08-14 KST) 전체 거래 {len(rows)}건")

    wins = [r for r in rows if (r.get("estimated_pnl_usdt") or 0) > 0]
    losses = [r for r in rows if (r.get("estimated_pnl_usdt") or 0) <= 0]
    total_pnl = sum(r.get("estimated_pnl_usdt") or 0 for r in rows)
    span_h = (rows[-1]["exited_at"] - rows[0]["entered_at"]) / 3600 if rows else 1
    print(f"승 {len(wins)} 패 {len(losses)} 승률 {len(wins)/len(rows)*100:.1f}% "
          f"순손익 {total_pnl:+.3f} USDT 시간당 {len(rows)/span_h:.1f}건")

    loss_trades = [r for r in rows if r.get("exit_reason") in LOSS_REASONS]
    profit_trades = [r for r in rows if r.get("exit_reason") not in LOSS_REASONS
                      and (r.get("estimated_pnl_usdt") or 0) > 0]

    print(f"손실성 청산(STOP_LOSS/EXTERNAL_CLOSE_LOSS/SOFT_STOP) {len(loss_trades)}건 분석...")
    loss_results = []
    for i, t in enumerate(loss_trades):
        r = analyze_loss(ex, t)
        if r and "error" not in r:
            loss_results.append(r)
        if (i + 1) % 10 == 0:
            print(f"  손실 {i+1}/{len(loss_trades)}")
        time.sleep(0.25)

    print(f"수익 청산 {len(profit_trades)}건 분석...")
    profit_results = []
    for i, t in enumerate(profit_trades):
        r = analyze_profit(ex, t)
        if r and "error" not in r:
            profit_results.append(r)
        if (i + 1) % 20 == 0:
            print(f"  수익 {i+1}/{len(profit_trades)}")
        time.sleep(0.25)

    out = Path("archive/scratch_scripts/scratch_today_postmortem_result.json")
    out.write_text(json.dumps({
        "summary": {
            "n_total": len(rows), "n_win": len(wins), "n_loss": len(losses),
            "total_pnl": total_pnl, "trades_per_hour": len(rows) / span_h,
        },
        "loss_results": loss_results,
        "profit_results": profit_results,
    }, indent=2, default=lambda o: bool(o) if hasattr(o, "item") else str(o)), encoding="utf-8")
    print("완료 —", out)


if __name__ == "__main__":
    main()
