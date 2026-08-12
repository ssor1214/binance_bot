"""[2026-08-12 사용자요청] SOFT_STOP/EARLY_EXIT이 과민반응하는지 실제 차트로 재확인.
offline_backtest.py엔 이 로직이 없어(라이브 전용) 백테스트 대신 실거래 사후검증으로
진행. 읽기전용 REST만 사용, 라이브 영향 없음."""
import json
import time
from pathlib import Path

from bot.config import Config
from bot.exchange import Exchange
from scratch_trade_postmortem import fetch_historical_klines

LEDGER_PATH = Path("logs/trade_ledger.jsonl")
SINCE = time.mktime(time.strptime("2026-08-11 20:38:06", "%Y-%m-%d %H:%M:%S"))
RECOVERY_WINDOW_MIN = 15


def load_trades(reasons):
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
            if r["entered_at"] < SINCE:
                continue
            if r.get("exit_reason") not in reasons:
                continue
            rows.append(r)
    rows.sort(key=lambda r: r["entered_at"])
    return rows


def analyze(ex, trade):
    import pandas as pd
    symbol, side = trade["symbol"], trade["side"]
    entry_price, exited_at = trade["entry_price"], trade["exited_at"]
    try:
        df = fetch_historical_klines(ex, symbol, exited_at, exited_at + RECOVERY_WINDOW_MIN * 60 + 60)
    except Exception:
        return None
    if df.empty:
        return None
    after = df[df["open_time"] > pd.to_datetime(exited_at, unit="s")].head(RECOVERY_WINDOW_MIN)
    recovered = False
    max_further_move_pct = 0.0
    if not after.empty:
        if side == "LONG":
            best = after["high"].max()
            recovered = best > entry_price
            max_further_move_pct = (best / entry_price - 1) * 100
        else:
            best = after["low"].min()
            recovered = best < entry_price
            max_further_move_pct = (entry_price / best - 1) * 100
    return {
        "symbol": symbol, "side": side, "pnl": trade.get("estimated_pnl_usdt", 0),
        "reason": trade.get("exit_reason"), "recovered": recovered,
        "further_move_pct": max_further_move_pct,
    }


def main():
    cfg = Config()
    ex = Exchange(cfg)
    trades = load_trades({"SOFT_STOP", "EARLY_EXIT"})
    print(f"SOFT_STOP/EARLY_EXIT {len(trades)}건 분석 시작...")
    results = []
    for i, t in enumerate(trades):
        r = analyze(ex, t)
        if r:
            results.append(r)
        time.sleep(0.15)

    with open("soft_early_postmortem_result.txt", "w", encoding="utf-8") as f:
        for reason in ("SOFT_STOP", "EARLY_EXIT"):
            rs = [r for r in results if r["reason"] == reason]
            if not rs:
                f.write(f"{reason}: 데이터 없음\n\n")
                continue
            recovered = [r for r in rs if r["recovered"]]
            avg_further = sum(r["further_move_pct"] for r in rs) / len(rs)
            f.write(f"=== {reason} ({len(rs)}건) ===\n")
            f.write(f"청산후 {RECOVERY_WINDOW_MIN}분내 회복: {len(recovered)}/{len(rs)}건 ({len(recovered)/len(rs)*100:.1f}%)\n")
            f.write(f"평균 추가유리이동폭: {avg_further:+.2f}%\n")
            for r in rs:
                f.write(f"  {r['symbol']} {r['side']} pnl={r['pnl']:+.3f} 회복={'Y' if r['recovered'] else 'N'} 추가이동={r['further_move_pct']:+.2f}%\n")
            f.write("\n")
    print("완료 — soft_early_postmortem_result.txt")


if __name__ == "__main__":
    main()
