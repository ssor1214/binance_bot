"""e2 전용 원장 분석.

실행:
  .venv312\Scripts\python.exe scripts/analyze_e2_ledger.py
  .venv312\Scripts\python.exe scripts/analyze_e2_ledger.py --live-only
  .venv312\Scripts\python.exe scripts/analyze_e2_ledger.py --since "2026-08-20 18:18"
"""
from __future__ import annotations

import argparse
import json
from collections import defaultdict
from datetime import datetime
from pathlib import Path


LEDGER = Path(__file__).resolve().parent.parent / "logs" / "scalp_bot_e2_ledger.jsonl"


def load_rows() -> list[dict]:
    rows: list[dict] = []
    with LEDGER.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            rows.append(json.loads(line))
    rows.sort(key=lambda r: float(r.get("entered_at") or 0))
    return rows


def filter_rows(rows: list[dict], live_only: bool, since: str | None) -> list[dict]:
    out = rows
    if live_only:
        out = [r for r in out if not r.get("dry_run")]
    if since:
        cutoff = datetime.strptime(since, "%Y-%m-%d %H:%M").timestamp()
        out = [r for r in out if float(r.get("entered_at") or 0) >= cutoff]
    return out


def summarize(rows: list[dict], label: str) -> None:
    print(f"\n=== {label} ===")
    if not rows:
        print("거래 없음")
        return
    net = sum(float(r.get("real_net", 0) or 0) for r in rows)
    nom = sum(float(r.get("nominal", 0) or 0) for r in rows)
    wins = sum(1 for r in rows if float(r.get("real_net", 0) or 0) > 0)
    print(
        f"거래수={len(rows)} 승률={wins / len(rows) * 100:.1f}% "
        f"누적순익={net:+.4f}USDT 명목당={net / max(nom, 1e-9) * 100:+.4f}%"
    )


def summarize_group(rows: list[dict], key_name: str) -> None:
    grouped: dict[object, list[dict]] = defaultdict(list)
    for row in rows:
        grouped[row.get(key_name)].append(row)
    print(f"\n=== {key_name}별 ===")
    for key in sorted(grouped, key=lambda x: (x is None, x)):
        subset = grouped[key]
        net = sum(float(r.get("real_net", 0) or 0) for r in subset)
        nom = sum(float(r.get("nominal", 0) or 0) for r in subset)
        wins = sum(1 for r in subset if float(r.get("real_net", 0) or 0) > 0)
        print(
            f"{key}: 거래수={len(subset):>2} 승률={wins / len(subset) * 100:>5.1f}% "
            f"순익={net:+.4f} 명목당={net / max(nom, 1e-9) * 100:+.4f}%"
        )


def print_timeline(rows: list[dict]) -> None:
    print("\n=== 최근 거래 타임라인 ===")
    for row in rows[-20:]:
        entered = datetime.fromtimestamp(float(row["entered_at"])).strftime("%H:%M:%S")
        dur = float(row["exited_at"]) - float(row["entered_at"])
        print(
            f"{entered} {row['symbol']} {row['side']} "
            f"{row['legs']}차 {row['exit_reason']} dur={dur:.0f}s "
            f"net={float(row['real_net']):+.4f}"
        )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live-only", action="store_true", help="실주문 표본만 본다")
    parser.add_argument("--since", type=str, default=None, help='이 시각 이후만 본다. 예: "2026-08-20 18:18"')
    args = parser.parse_args()

    rows = filter_rows(load_rows(), live_only=args.live_only, since=args.since)
    summarize(rows, "전체")
    summarize_group(rows, "legs")
    summarize_group(rows, "exit_reason")

    combo: dict[tuple[object, object], list[dict]] = defaultdict(list)
    for row in rows:
        combo[(row.get("exit_reason"), row.get("legs"))].append(row)
    print("\n=== 청산사유 x 차수 ===")
    for key in sorted(combo):
        subset = combo[key]
        net = sum(float(r.get("real_net", 0) or 0) for r in subset)
        nom = sum(float(r.get("nominal", 0) or 0) for r in subset)
        wins = sum(1 for r in subset if float(r.get("real_net", 0) or 0) > 0)
        print(
            f"{key[0]} / {key[1]}차: 거래수={len(subset):>2} 승률={wins / len(subset) * 100:>5.1f}% "
            f"순익={net:+.4f} 명목당={net / max(nom, 1e-9) * 100:+.4f}%"
        )

    print_timeline(rows)


if __name__ == "__main__":
    main()
