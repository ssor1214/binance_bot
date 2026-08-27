"""최근 거래 원장에서 micro-scalp 승/패 패턴을 분리해 본다.

짧게 먹힌 승리와 오래 끌다 깨진 손실을 분리해서,
후보를 더 짧게 운용하는 전략 설계 근거로 사용한다.

실행:
  python scripts/analyze_micro_scalp_patterns.py
  python scripts/analyze_micro_scalp_patterns.py --since "2026-08-17 00:00"
  python scripts/analyze_micro_scalp_patterns.py --win-max-hold-sec 180 --loss-min-hold-sec 180
"""
from __future__ import annotations

import argparse
import json
from collections import Counter
from datetime import datetime
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
LEDGER_PATH = ROOT / "logs" / "trade_ledger.jsonl"


def load_rows(path: Path) -> list[dict]:
    rows: list[dict] = []
    if not path.exists():
        return rows
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if row.get("origin") != "bot":
                continue
            row["entered_dt"] = datetime.fromtimestamp(float(row["entered_at"]))
            rows.append(row)
    rows.sort(key=lambda r: r["entered_at"])
    return rows


def summarize_group(rows: list[dict], title: str) -> None:
    print(f"\n=== {title} ===")
    if not rows:
        print("대상 거래 없음")
        return

    wins = [r for r in rows if float(r.get("estimated_pnl_usdt", 0.0)) > 0]
    total_pnl = sum(float(r.get("estimated_pnl_usdt", 0.0)) for r in rows)
    avg_hold = sum(float(r.get("held_seconds", 0.0)) for r in rows) / len(rows)
    print(
        f"거래수={len(rows)} 승률={len(wins)/len(rows)*100:.1f}% "
        f"누적손익={total_pnl:+.4f}USDT 평균보유={avg_hold:.1f}초"
    )

    print("\n[방향별]")
    by_side = Counter(r.get("side", "UNKNOWN") for r in rows)
    for side, count in by_side.items():
        subset = [r for r in rows if r.get("side") == side]
        side_wins = sum(1 for r in subset if float(r.get("estimated_pnl_usdt", 0.0)) > 0)
        side_pnl = sum(float(r.get("estimated_pnl_usdt", 0.0)) for r in subset)
        print(f"{side:8s} 거래수={count:3d} 승률={side_wins/count*100:.1f}% 손익={side_pnl:+.4f}USDT")

    print("\n[청산사유]")
    for reason, count in Counter(r.get("exit_reason", "UNKNOWN") for r in rows).most_common():
        print(f"{reason:28s} {count:3d}")

    print("\n[최근 15건]")
    for row in rows[-15:]:
        print(
            f"{row['entered_dt']:%m-%d %H:%M:%S} {row.get('symbol','?'):12s} {row.get('side','?'):5s} "
            f"pnl={float(row.get('estimated_pnl_usdt',0.0)):+.4f} held={float(row.get('held_seconds',0.0)):.1f}s "
            f"exit={row.get('exit_reason','?')} spike={bool(row.get('early_entry_spike'))} "
            f"widened={bool(row.get('stop_loss_widened'))} sl={float(row.get('applied_stop_loss_pct',0.0)):.1f}"
        )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--since", type=str, default=None, help="이 시각 이후 거래만 (예: '2026-08-17 00:00')")
    parser.add_argument("--win-max-hold-sec", type=float, default=180.0)
    parser.add_argument("--loss-min-hold-sec", type=float, default=180.0)
    args = parser.parse_args()

    rows = load_rows(LEDGER_PATH)
    if args.since:
        cutoff = datetime.strptime(args.since, "%Y-%m-%d %H:%M")
        rows = [r for r in rows if r["entered_dt"] >= cutoff]

    if not rows:
        print("대상 거래가 없습니다.")
        return

    short_winners = [
        r for r in rows
        if float(r.get("estimated_pnl_usdt", 0.0)) > 0
        and float(r.get("held_seconds", 0.0)) <= args.win_max_hold_sec
    ]
    long_losers = [
        r for r in rows
        if float(r.get("estimated_pnl_usdt", 0.0)) <= 0
        and float(r.get("held_seconds", 0.0)) >= args.loss_min_hold_sec
    ]

    print("=== Hold Bucket Summary ===")
    buckets = [
        ("<=60s", 0, 60),
        ("60-180s", 60, 180),
        ("180-300s", 180, 300),
        ("300s+", 300, 10**12),
    ]
    for label, lo, hi in buckets:
        bucket_rows = [r for r in rows if lo < float(r.get("held_seconds", 0.0)) <= hi]
        if not bucket_rows:
            continue
        wins = sum(1 for r in bucket_rows if float(r.get("estimated_pnl_usdt", 0.0)) > 0)
        pnl = sum(float(r.get("estimated_pnl_usdt", 0.0)) for r in bucket_rows)
        print(f"{label:8s} 거래수={len(bucket_rows):3d} 승률={wins/len(bucket_rows)*100:.1f}% 손익={pnl:+.4f}USDT")

    summarize_group(short_winners, f"짧게 먹힌 승리 (<= {args.win_max_hold_sec:.0f}s)")
    summarize_group(long_losers, f"길게 끌다 깨진 손실 (>= {args.loss_min_hold_sec:.0f}s)")


if __name__ == "__main__":
    main()
