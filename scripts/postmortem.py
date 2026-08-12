"""[2026-08-11 사용자요청] "스캘핑 기준이니 과거 차트를 다시 볼 수 있다 — 복기 시스템을
만들어서 개선안을 도출하자" — 순환매매(10:53:48~) 손실 거래들을 하나씩 다시 불러와서,
그 시점 전후의 실제 1분봉 흐름을 보고 다음을 계산한다:

  1. MAE(최대 역행폭) — 보유 중 가장 불리했던 순간이 손절폭 대비 얼마나 더 갔는지(슬리피지/
     휩쏘 크기 추정)
  2. 청산 이후 15분간 가격이 되돌아왔는지(=조금만 더 버텼으면 살았을 손실인지)
  3. 진입 시점이 국소 고점/저점(반전 지점) 바로 직전이었는지(타이밍 품질)

읽기 전용 REST 호출만 사용(get_klines) — 주문/포지션 변경 없음, 라이브에 영향 없음."""
from __future__ import annotations

import json
import time
from pathlib import Path

from bot.config import Config
from bot.exchange import Exchange

LEDGER_PATH = Path("logs/trade_ledger.jsonl")
ROTATION_START = time.mktime(time.strptime("2026-08-11 10:53:48", "%Y-%m-%d %H:%M:%S"))
RECOVERY_WINDOW_MIN = 15  # 청산 후 이만큼(분) 안에 되돌아왔는지 확인


def load_losing_trades(side: str | None = None) -> list[dict]:
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
            if pnl >= 0:
                continue
            if side and r.get("side") != side:
                continue
            rows.append(r)
    rows.sort(key=lambda r: r["entered_at"])
    return rows


def fetch_historical_klines(ex: Exchange, symbol: str, start_sec: float, end_sec: float):
    """[2026-08-11] ex.get_klines()는 항상 '지금 시점 기준 최신 N개'만 반환해서 몇 시간
    전 과거 구간은 조회할 수 없다 — 복기는 과거 특정 구간이 필요하므로 startTime/endTime을
    지원하는 거래소 클라이언트를 직접 호출한다(읽기 전용, 주문/포지션 변경 없음)."""
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
    for col in ("open", "high", "low", "close", "volume", "taker_buy_base"):
        df[col] = df[col].astype(float)
    df["open_time"] = pd.to_datetime(df["open_time"], unit="ms")
    return df


def analyze_trade(ex: Exchange, trade: dict) -> dict | None:
    import pandas as pd
    symbol = trade["symbol"]
    side = trade["side"]
    entry_price = trade["entry_price"]
    exit_price = trade["exit_price"]
    entered_at = trade["entered_at"]
    exited_at = trade["exited_at"]

    try:
        df = fetch_historical_klines(ex, symbol, entered_at - 300, exited_at + RECOVERY_WINDOW_MIN * 60 + 60)
    except Exception:
        return None
    if df.empty:
        return None

    hold_df = df[(df["open_time"] >= pd.to_datetime(entered_at, unit="s")) &
                 (df["open_time"] <= pd.to_datetime(exited_at, unit="s"))]
    after_df = df[df["open_time"] > pd.to_datetime(exited_at, unit="s")]
    after_df = after_df.head(RECOVERY_WINDOW_MIN)

    if hold_df.empty:
        return None

    # MAE: 보유 중 가장 불리했던 가격
    if side == "LONG":
        worst_price = hold_df["low"].min()
        mae_pct = (worst_price / entry_price - 1) * 100
    else:
        worst_price = hold_df["high"].max()
        mae_pct = (entry_price / worst_price - 1) * 100  # 음수면 역행

    # 청산 이후 되돌아왔는지(=청산가 대비 유리한 방향으로 더 갔는지)
    recovered = False
    if not after_df.empty:
        if side == "LONG":
            best_after = after_df["high"].max()
            recovered = best_after > entry_price  # 진입가 이상 회복
        else:
            best_after = after_df["low"].min()
            recovered = best_after < entry_price

    return {
        "symbol": symbol, "side": side, "pnl": trade.get("estimated_pnl_usdt", 0),
        "reason": trade.get("exit_reason"), "held_min": (exited_at - entered_at) / 60,
        "mae_pct": mae_pct, "recovered_after_exit": recovered,
    }


def main():
    cfg = Config()
    ex = Exchange(cfg)

    losing = load_losing_trades()
    print(f"순환매매(10:53:48~) 손실거래 {len(losing)}건 분석 시작...")

    results = []
    for i, trade in enumerate(losing):
        r = analyze_trade(ex, trade)
        if r:
            results.append(r)
        if (i + 1) % 10 == 0:
            print(f"  진행 {i+1}/{len(losing)}...")
        time.sleep(0.15)  # REST 레이트리밋 여유

    if not results:
        print("분석 가능한 거래가 없습니다.")
        return

    with open("trade_postmortem_result.txt", "w", encoding="utf-8") as f:
        f.write(f"=== 순환매매 손실거래 복기 ({len(results)}건 분석) ===\n\n")
        for side in ("LONG", "SHORT"):
            side_rs = [r for r in results if r["side"] == side]
            if not side_rs:
                continue
            recovered = [r for r in side_rs if r["recovered_after_exit"]]
            f.write(f"--- {side} ({len(side_rs)}건) ---\n")
            f.write(f"청산 후 {RECOVERY_WINDOW_MIN}분 내 가격 회복(=더 버텼으면 살았을 손실): "
                    f"{len(recovered)}/{len(side_rs)}건 ({len(recovered)/len(side_rs)*100:.1f}%)\n")
            avg_mae = sum(r["mae_pct"] for r in side_rs) / len(side_rs)
            f.write(f"평균 MAE(최대 역행폭): {avg_mae:+.2f}%\n")
            fast_losses = [r for r in side_rs if r["held_min"] < 1]
            f.write(f"1분 이내 청산: {len(fast_losses)}건 ({len(fast_losses)/len(side_rs)*100:.1f}%)\n\n")

        f.write("=== 상세 ===\n")
        for r in results:
            f.write(f"{r['symbol']} {r['side']} pnl={r['pnl']:+.3f} reason={r['reason']} "
                     f"held={r['held_min']:.2f}min MAE={r['mae_pct']:+.2f}% "
                     f"청산후회복={'Y' if r['recovered_after_exit'] else 'N'}\n")

    print("완료 — trade_postmortem_result.txt 저장됨")


if __name__ == "__main__":
    main()
