"""[2026-08-10 사용자요청] trade_ledger.jsonl을 pandas로 방향별(LONG/SHORT) 정밀 분석하는
재사용 스크립트. 이번 세션 내내 스크래치 스크립트로 반복했던 분석(승률/손익/손익비/시간대
별 추이)을 한 번에 실행할 수 있게 정리한 것 — CSV가 아니라 JSONL을 쓰는 이유는
`bot/trade_ledger.py`/`bot/position_manager.py` 문서화 참고(스키마가 계속 늘어나도
마이그레이션 없이 호환됨).

실행: python scripts/analyze_trade_ledger.py [--since "2026-08-10 14:31"] [--bot-only] [--external-close-loss-report]
실 API를 호출하지 않고 로컬 logs/trade_ledger.jsonl만 읽는다."""
from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path

import pandas as pd

LEDGER_PATH = Path(__file__).resolve().parent.parent / "logs" / "trade_ledger.jsonl"


def load_ledger(path: Path = LEDGER_PATH) -> pd.DataFrame:
    rows = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    df = pd.DataFrame(rows)
    if not df.empty:
        df["entered_dt"] = pd.to_datetime(df["entered_at"], unit="s")
        df["exited_dt"] = pd.to_datetime(df["exited_at"], unit="s")
    return df


def summarize(df: pd.DataFrame, label: str = "전체") -> None:
    if df.empty:
        print(f"=== {label}: 거래 없음 ===")
        return
    wins = df[df["estimated_pnl_usdt"] > 0]
    losses = df[df["estimated_pnl_usdt"] <= 0]
    win_rate = len(wins) / len(df) * 100
    avg_win = wins["estimated_pnl_usdt"].mean() if len(wins) else 0.0
    avg_loss = losses["estimated_pnl_usdt"].mean() if len(losses) else 0.0
    profit_factor = (wins["estimated_pnl_usdt"].sum() / abs(losses["estimated_pnl_usdt"].sum())
                      if len(losses) and losses["estimated_pnl_usdt"].sum() != 0 else float("inf"))
    print(f"=== {label} ===")
    print(f"거래수={len(df)} 승률={win_rate:.1f}% 누적손익={df['estimated_pnl_usdt'].sum():+.3f}USDT "
          f"평균이익={avg_win:.4f} 평균손실={avg_loss:.4f} 손익비={profit_factor:.2f}")


def print_external_close_loss_report(df: pd.DataFrame) -> None:
    """EXTERNAL_CLOSE_LOSS만 분리해, 사후분류 손실의 패턴을 빠르게 복기한다."""
    subset = df[df["exit_reason"] == "EXTERNAL_CLOSE_LOSS"].copy()
    if subset.empty:
        print("\n=== EXTERNAL_CLOSE_LOSS 전용 리포트 ===")
        print("대상 거래가 없습니다.")
        return

    print("\n=== EXTERNAL_CLOSE_LOSS 전용 리포트 ===")
    print(
        f"거래수={len(subset)} 누적손익={subset['estimated_pnl_usdt'].sum():+.3f}USDT "
        f"평균손익={subset['estimated_pnl_usdt'].mean():.4f}"
    )

    print("\n[방향별]")
    by_side = subset.groupby("side").agg(
        거래수=("estimated_pnl_usdt", "count"),
        손익=("estimated_pnl_usdt", "sum"),
        평균=("estimated_pnl_usdt", "mean"),
    )
    print(by_side.to_string())

    print("\n[레버리지별]")
    by_leverage = subset.groupby("leverage").agg(
        거래수=("estimated_pnl_usdt", "count"),
        손익=("estimated_pnl_usdt", "sum"),
        평균=("estimated_pnl_usdt", "mean"),
    ).sort_index()
    print(by_leverage.to_string())

    if "protection_state" in subset.columns:
        print("\n[보호상태별]")
        prot = subset.groupby(subset["protection_state"].fillna("UNKNOWN")).agg(
            거래수=("estimated_pnl_usdt", "count"),
            손익=("estimated_pnl_usdt", "sum"),
            평균=("estimated_pnl_usdt", "mean"),
        ).sort_values("손익")
        print(prot.to_string())

    print("\n[손실 심볼 TOP10]")
    worst = subset.groupby("symbol").agg(
        거래수=("estimated_pnl_usdt", "count"),
        손익=("estimated_pnl_usdt", "sum"),
        평균=("estimated_pnl_usdt", "mean"),
    ).sort_values("손익").head(10)
    print(worst.to_string())

    print("\n[최근 샘플 10건]")
    cols = [c for c in [
        "entered_dt", "symbol", "side", "estimated_pnl_usdt", "leverage",
        "protection_state", "stop_loss_widened", "applied_stop_loss_pct",
        "sl_defer_used", "sl_defer_active",
    ] if c in subset.columns]
    print(subset.sort_values("entered_dt")[cols].tail(10).to_string(index=False))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--since", type=str, default=None, help="이 시각 이후 거래만(예: '2026-08-10 14:31')")
    parser.add_argument("--bot-only", action="store_true", help="봇이 직접 진입한 거래만(수동 진입 제외)")
    parser.add_argument("--external-close-loss-report", action="store_true", help="EXTERNAL_CLOSE_LOSS 전용 복기 리포트 추가")
    args = parser.parse_args()

    df = load_ledger()
    if df.empty:
        print("거래 기록이 없습니다.")
        return

    if args.bot_only:
        df = df[df["origin"] == "bot"]
    if args.since:
        cutoff = datetime.strptime(args.since, "%Y-%m-%d %H:%M")
        df = df[df["entered_dt"] >= cutoff]

    summarize(df, "전체")
    for side in ("LONG", "SHORT"):
        summarize(df[df["side"] == side], side)

    print("\n=== 청산사유 분포 ===")
    print(df["exit_reason"].value_counts().to_string())

    print("\n=== 손실 코인 TOP5 ===")
    worst = df.groupby("symbol")["estimated_pnl_usdt"].sum().sort_values().head(5)
    print(worst.to_string())

    print("\n=== 시간대별(1시간 단위) 추이 ===")
    hourly = df.set_index("entered_dt").resample("1h").agg(
        거래수=("estimated_pnl_usdt", "count"),
        손익=("estimated_pnl_usdt", "sum"),
    )
    print(hourly.to_string())

    if args.external_close_loss_report:
        print_external_close_loss_report(df)


if __name__ == "__main__":
    main()
