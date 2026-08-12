"""[2026-08-11 사용자요청] "순환매매 전체 복기" — 손실쪽은 이미 봤으니 승리거래도 재분석해서
"더 크게 먹을 수 있었는데 너무 빨리 잠갔는지"(MFE 대비 실현손익 비율)를 확인한다.
읽기전용 REST만 사용, 라이브 영향 없음."""
from __future__ import annotations

import json
import time
from pathlib import Path

from bot.config import Config
from bot.exchange import Exchange
from scratch_trade_postmortem import fetch_historical_klines, ROTATION_START

LEDGER_PATH = Path("logs/trade_ledger.jsonl")


def load_winning_trades() -> list[dict]:
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
            if r["entered_at"] < ROTATION_START:
                continue
            pnl = r.get("estimated_pnl_usdt", 0) or 0
            if pnl <= 0:
                continue
            rows.append(r)
    rows.sort(key=lambda r: r["entered_at"])
    return rows


def analyze_win(ex: Exchange, trade: dict) -> dict | None:
    import pandas as pd
    symbol = trade["symbol"]
    side = trade["side"]
    entry_price = trade["entry_price"]
    entered_at = trade["entered_at"]
    exited_at = trade["exited_at"]
    realized_pct = trade.get("estimated_pnl_pct", 0) or 0

    try:
        df = fetch_historical_klines(ex, symbol, entered_at, exited_at + 15 * 60)
    except Exception:
        return None
    if df.empty:
        return None

    hold_and_after = df[df["open_time"] >= pd.to_datetime(entered_at, unit="s")]
    if hold_and_after.empty:
        return None

    if side == "LONG":
        best_price = hold_and_after["high"].max()
        mfe_pct = (best_price / entry_price - 1) * 100
    else:
        best_price = hold_and_after["low"].min()
        mfe_pct = (entry_price / best_price - 1) * 100

    capture_ratio = (realized_pct / mfe_pct * 100) if mfe_pct > 0 else None
    return {
        "symbol": symbol, "side": side, "pnl_pct": realized_pct,
        "held_min": (exited_at - entered_at) / 60, "reason": trade.get("exit_reason"),
        "mfe_pct": mfe_pct, "capture_ratio": capture_ratio,
    }


def main():
    cfg = Config()
    ex = Exchange(cfg)
    wins = load_winning_trades()
    print(f"순환매매 익절거래 {len(wins)}건 분석 시작...")

    results = []
    for i, trade in enumerate(wins):
        r = analyze_win(ex, trade)
        if r:
            results.append(r)
        if (i + 1) % 10 == 0:
            print(f"  진행 {i+1}/{len(wins)}...")
        time.sleep(0.15)

    with open("trade_postmortem_wins_result.txt", "w", encoding="utf-8") as f:
        f.write(f"=== 순환매매 익절거래 복기 ({len(results)}건 분석) ===\n\n")
        valid = [r for r in results if r["capture_ratio"] is not None]
        avg_capture = sum(r["capture_ratio"] for r in valid) / len(valid) if valid else 0
        f.write(f"평균 캡처비율(실현수익/최대favorable폭): {avg_capture:.1f}%\n")
        under50 = [r for r in valid if r["capture_ratio"] < 50]
        f.write(f"캡처비율 50% 미만(=가능했던 수익의 절반도 못 먹음): {len(under50)}/{len(valid)}건 ({len(under50)/len(valid)*100:.1f}%)\n\n")
        f.write("=== 상세 ===\n")
        for r in results:
            cr = f"{r['capture_ratio']:.0f}%" if r["capture_ratio"] is not None else "N/A"
            f.write(f"{r['symbol']} {r['side']} pnl%={r['pnl_pct']:+.2f} reason={r['reason']} "
                     f"held={r['held_min']:.2f}min MFE={r['mfe_pct']:+.2f}% 캡처={cr}\n")

    print("완료 — trade_postmortem_wins_result.txt 저장됨")


if __name__ == "__main__":
    main()
