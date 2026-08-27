"""tag-only micro-scalp 후보 로그를 집계한다.

실행:
  python scripts/analyze_micro_scalp_candidates.py
  python scripts/analyze_micro_scalp_candidates.py --since "2026-08-17 12:00"
  python scripts/analyze_micro_scalp_candidates.py --minutes 60
"""
from __future__ import annotations

import argparse
import json
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
PATH = ROOT / "logs" / "micro_scalp_candidates.jsonl"


def load_rows(path: Path) -> list[dict]:
    if not path.exists():
        return []
    rows: list[dict] = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            row["recorded_dt"] = datetime.fromtimestamp(float(row["recorded_at"]))
            rows.append(row)
    rows.sort(key=lambda r: float(r.get("recorded_at", 0.0)))
    return rows


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--since", type=str, default=None)
    parser.add_argument("--minutes", type=int, default=None)
    args = parser.parse_args()

    rows = load_rows(PATH)
    if not rows:
        print(f"micro-scalp 후보 파일이 없거나 비어 있습니다: {PATH}")
        return

    if args.since:
        cutoff = datetime.strptime(args.since, "%Y-%m-%d %H:%M")
        rows = [r for r in rows if r["recorded_dt"] >= cutoff]
    elif args.minutes is not None:
        end_dt = rows[-1]["recorded_dt"]
        cutoff = end_dt - timedelta(minutes=args.minutes)
        rows = [r for r in rows if r["recorded_dt"] >= cutoff]

    if not rows:
        print("조건에 맞는 micro-scalp 후보가 없습니다.")
        return

    print("=== micro-scalp 후보 집계 ===")
    print(f"기간: {rows[0]['recorded_dt']:%Y-%m-%d %H:%M:%S} ~ {rows[-1]['recorded_dt']:%Y-%m-%d %H:%M:%S}")
    print(f"후보 수={len(rows)}")

    print("\n[방향별]")
    for side, count in Counter(r.get("signal", "UNKNOWN") for r in rows).most_common():
        print(f"{side:12s} {count:5d}")

    print("\n[사유별]")
    for reason, count in Counter(r.get("micro_scalp_reason", "") for r in rows).most_common():
        print(f"{reason:32s} {count:5d}")

    print("\n[최근 후보 20건]")
    for row in rows[-20:]:
        print(
            f"{row['recorded_dt']:%H:%M:%S} {row.get('symbol','?'):12s} {row.get('signal','?'):5s} "
            f"prob={float(row.get('probability', 0.0)):.2f} "
            f"priority={float(row.get('entry_priority', 0.0)):.2f} "
            f"reason={row.get('micro_scalp_reason','')}"
        )


if __name__ == "__main__":
    main()
