"""최근 후보 생성 퍼널(JSONL)을 집계해 어느 단계에서 거래가 줄어드는지 본다.

실행:
  python scripts/analyze_entry_funnel.py
  python scripts/analyze_entry_funnel.py --since "2026-08-17 10:00"
  python scripts/analyze_entry_funnel.py --minutes 30
"""
from __future__ import annotations

import argparse
import json
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
FUNNEL_PATH = ROOT / "logs" / "entry_funnel.jsonl"


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
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return rows


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--since", type=str, default=None, help="이 시각 이후만 집계 (예: '2026-08-17 10:00')")
    parser.add_argument("--minutes", type=int, default=None, help="최근 N분만 집계")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    rows = load_rows(FUNNEL_PATH)
    if not rows:
        print(f"퍼널 표본 파일이 없거나 비어 있습니다: {FUNNEL_PATH}")
        return

    for row in rows:
        row["recorded_dt"] = datetime.fromtimestamp(float(row["recorded_at"]))

    if args.since:
        cutoff = datetime.strptime(args.since, "%Y-%m-%d %H:%M")
        rows = [r for r in rows if r["recorded_dt"] >= cutoff]
    elif args.minutes is not None:
        end_dt = max(r["recorded_dt"] for r in rows)
        cutoff = end_dt - timedelta(minutes=args.minutes)
        rows = [r for r in rows if r["recorded_dt"] >= cutoff]

    if not rows:
        print("조건에 맞는 퍼널 표본이 없습니다.")
        return

    stage_counts = Counter(r.get("stage", "unknown") for r in rows)
    candidate_rows = [r for r in rows if r.get("stage") == "candidate"]
    side_counts = Counter(r.get("side", "UNKNOWN") for r in candidate_rows)
    reject_counts = Counter()
    for r in rows:
        stage = r.get("stage", "unknown")
        if stage != "candidate":
            reject_counts[stage] += 1

    start_dt = min(r["recorded_dt"] for r in rows)
    end_dt = max(r["recorded_dt"] for r in rows)
    total = len(rows)
    candidates = len(candidate_rows)

    print("=== 후보 생성 퍼널 ===")
    print(f"기간: {start_dt:%Y-%m-%d %H:%M:%S} ~ {end_dt:%Y-%m-%d %H:%M:%S}")
    print(f"총 이벤트={total} 최종 후보={candidates}")

    print("\n[단계별 건수]")
    for stage, count in stage_counts.most_common():
        print(f"{stage:22s} {count:6d}")

    print("\n[탈락 사유 TOP]")
    for stage, count in reject_counts.most_common():
        print(f"{stage:22s} {count:6d}")

    print("\n[최종 후보 방향]")
    for side, count in side_counts.most_common():
        print(f"{side:22s} {count:6d}")

    print("\n[최근 후보 20건]")
    recent_candidates = sorted(candidate_rows, key=lambda r: r["recorded_at"])[-20:]
    for row in recent_candidates:
        detail = row.get("detail", {}) or {}
        print(
            f"{row['recorded_dt']:%H:%M:%S} {row.get('symbol','?'):12s} {row.get('side','?'):5s} "
            f"prob={float(row.get('probability', 0.0)):.2f} "
            f"priority={float(detail.get('entry_priority', 0.0)):.2f} "
            f"mtf={int(detail.get('mtf_agree', 0))}/{int(detail.get('mtf_total', 0))}"
        )


if __name__ == "__main__":
    main()
