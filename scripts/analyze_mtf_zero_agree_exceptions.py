"""0/2 MTF 예외 진입 표본(JSONL)과 trade_ledger를 느슨하게 매칭해 손익을 집계한다.

실행:
  python scripts/analyze_mtf_zero_agree_exceptions.py
  python scripts/analyze_mtf_zero_agree_exceptions.py --since "2026-08-17 09:32"
  python scripts/analyze_mtf_zero_agree_exceptions.py --window-sec 21600
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path

import pandas as pd


ROOT = Path(__file__).resolve().parent.parent
SAMPLES_PATH = ROOT / "logs" / "mtf_zero_agree_exceptions.jsonl"
LEDGER_PATH = ROOT / "logs" / "trade_ledger.jsonl"


def load_jsonl(path: Path) -> pd.DataFrame:
    rows = []
    if not path.exists():
        return pd.DataFrame()
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return pd.DataFrame(rows)


def load_samples(path: Path = SAMPLES_PATH) -> pd.DataFrame:
    df = load_jsonl(path)
    if df.empty:
        return df
    df["recorded_dt"] = pd.to_datetime(df["recorded_at"], unit="s")
    return df.sort_values("recorded_at").reset_index(drop=True)


def load_ledger(path: Path = LEDGER_PATH) -> pd.DataFrame:
    df = load_jsonl(path)
    if df.empty:
        return df
    df["entered_dt"] = pd.to_datetime(df["entered_at"], unit="s")
    df["exited_dt"] = pd.to_datetime(df["exited_at"], unit="s")
    return df.sort_values("entered_at").reset_index(drop=True)


def match_samples_to_trades(samples: pd.DataFrame, ledger: pd.DataFrame, window_sec: int) -> pd.DataFrame:
    if samples.empty:
        return samples.copy()

    if ledger.empty:
        out = samples.copy()
        out["matched"] = False
        return out

    ledger = ledger.copy()
    ledger["origin"] = ledger["origin"].fillna("")
    ledger = ledger[ledger["origin"] == "bot"].sort_values("entered_at").reset_index(drop=True)
    used_trade_ids: set[int] = set()
    matched_rows: list[dict] = []

    for sample in samples.to_dict("records"):
        subset = ledger[
            (ledger["symbol"] == sample["symbol"])
            & (ledger["side"] == sample["side"])
            & (ledger["entered_at"] >= float(sample["recorded_at"]) - float(window_sec))
            & (ledger["entered_at"] <= float(sample["recorded_at"]) + float(window_sec))
        ].copy()
        subset["trade_id"] = subset.index
        subset = subset[~subset["trade_id"].isin(used_trade_ids)]
        if subset.empty:
            row = dict(sample)
            row["matched"] = False
            matched_rows.append(row)
            continue

        subset["delta_sec"] = (subset["entered_at"] - float(sample["recorded_at"])).abs()
        best = subset.sort_values(["delta_sec", "entered_at"]).iloc[0]
        used_trade_ids.add(int(best["trade_id"]))

        row = dict(sample)
        row.update({
            "matched": True,
            "trade_entered_at": float(best["entered_at"]),
            "trade_exited_at": float(best["exited_at"]),
            "trade_entered_dt": best["entered_dt"],
            "trade_exited_dt": best["exited_dt"],
            "delta_sec": float(best["delta_sec"]),
            "estimated_pnl_usdt": float(best["estimated_pnl_usdt"]),
            "estimated_pnl_pct": float(best["estimated_pnl_pct"]),
            "exit_reason": best["exit_reason"],
            "leverage": float(best["leverage"]),
            "held_seconds": float(best["held_seconds"]),
        })
        matched_rows.append(row)

    return pd.DataFrame(matched_rows)


def summarize(samples: pd.DataFrame) -> None:
    print("=== 0/2 MTF 예외 진입 표본 ===")
    print(f"기록 표본 수={len(samples)}")
    if samples.empty:
        return

    matched = samples[samples["matched"] == True].copy()
    unmatched = samples[samples["matched"] != True].copy()
    print(f"매칭 성공={len(matched)} 미매칭={len(unmatched)}")

    if matched.empty:
        return

    wins = matched[matched["estimated_pnl_usdt"] > 0]
    win_rate = len(wins) / len(matched) * 100
    print(
        f"손익 집계 대상={len(matched)} 승률={win_rate:.1f}% "
        f"누적손익={matched['estimated_pnl_usdt'].sum():+.4f}USDT "
        f"평균손익={matched['estimated_pnl_usdt'].mean():+.4f}USDT"
    )

    print("\n[방향별]")
    by_side = matched.groupby("side").agg(
        표본수=("estimated_pnl_usdt", "count"),
        승률=("estimated_pnl_usdt", lambda s: (s.gt(0).mean() * 100.0)),
        손익=("estimated_pnl_usdt", "sum"),
        평균=("estimated_pnl_usdt", "mean"),
    )
    print(by_side.to_string(float_format=lambda v: f"{v:.4f}"))

    print("\n[최근 표본]")
    cols = [
        "recorded_dt",
        "symbol",
        "side",
        "probability",
        "entry_priority",
        "estimated_pnl_usdt",
        "estimated_pnl_pct",
        "exit_reason",
        "delta_sec",
    ]
    present_cols = [c for c in cols if c in matched.columns]
    print(matched.sort_values("recorded_at")[present_cols].tail(20).to_string(index=False))

    if not unmatched.empty:
        print("\n[미매칭 표본]")
        cols = ["recorded_dt", "symbol", "side", "probability", "entry_priority"]
        present_cols = [c for c in cols if c in unmatched.columns]
        print(unmatched.sort_values("recorded_at")[present_cols].tail(20).to_string(index=False))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--since", type=str, default=None, help="이 시각 이후 표본만(예: '2026-08-17 09:32')")
    parser.add_argument("--window-sec", type=int, default=7200, help="trade_ledger 매칭 허용 시간창(초)")
    args = parser.parse_args()

    samples = load_samples()
    if samples.empty:
        print(f"표본 파일이 없거나 비어 있습니다: {SAMPLES_PATH}")
        return

    if args.since:
        cutoff = datetime.strptime(args.since, "%Y-%m-%d %H:%M")
        samples = samples[samples["recorded_dt"] >= cutoff]

    ledger = load_ledger()
    matched = match_samples_to_trades(samples, ledger, window_sec=args.window_sec)
    summarize(matched)


if __name__ == "__main__":
    main()
