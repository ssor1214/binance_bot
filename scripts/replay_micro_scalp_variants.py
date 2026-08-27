"""trade_ledger 기반으로 2번(현재) vs 여러 micro-scalp 변형을 리플레이 비교한다.

중요:
- 이 스크립트는 원장(JSONL)만 사용한다.
- 따라서 intratrade 가격경로를 모르는 한계가 있어, 일부 버전은 "causal replay",
  일부는 "oracle upper bound"로 구분해서 봐야 한다.

실행:
  python scripts/replay_micro_scalp_variants.py
  python scripts/replay_micro_scalp_variants.py --days 3
  python scripts/replay_micro_scalp_variants.py --days 5 7
"""
from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
LEDGER_PATH = ROOT / "logs" / "trade_ledger.jsonl"


@dataclass
class VariantResult:
    name: str
    kind: str
    trades: int
    wins: int
    pnl: float
    max_loss_streak: int

    @property
    def win_rate(self) -> float:
        return (self.wins / self.trades * 100.0) if self.trades else 0.0


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
    rows.sort(key=lambda r: float(r.get("entered_at", 0.0)))
    return rows


def calc_stats(rows: list[dict], name: str, kind: str) -> VariantResult:
    wins = 0
    pnl = 0.0
    max_loss_streak = 0
    loss_streak = 0
    for row in rows:
        trade_pnl = float(row.get("estimated_pnl_usdt", 0.0))
        pnl += trade_pnl
        if trade_pnl > 0:
            wins += 1
            loss_streak = 0
        else:
            loss_streak += 1
            if loss_streak > max_loss_streak:
                max_loss_streak = loss_streak
    return VariantResult(
        name=name,
        kind=kind,
        trades=len(rows),
        wins=wins,
        pnl=pnl,
        max_loss_streak=max_loss_streak,
    )


def actual(rows: list[dict]) -> list[dict]:
    return list(rows)


def long_only(rows: list[dict]) -> list[dict]:
    return [r for r in rows if r.get("side") == "LONG"]


def causal_same_symbol_loss_block(rows: list[dict], block_minutes: float = 30.0) -> list[dict]:
    out: list[dict] = []
    blocked_until: dict[tuple[str, str], float] = {}
    for row in rows:
        key = (str(row.get("symbol", "")), str(row.get("side", "")))
        entered_at = float(row.get("entered_at", 0.0))
        if entered_at < blocked_until.get(key, 0.0):
            continue
        out.append(row)
        if float(row.get("estimated_pnl_usdt", 0.0)) <= 0:
            blocked_until[key] = float(row.get("exited_at", entered_at)) + block_minutes * 60.0
    return out


def causal_same_symbol_fast_reentry_block(rows: list[dict], minutes: float = 15.0) -> list[dict]:
    out: list[dict] = []
    last_exit_at: dict[tuple[str, str], float] = {}
    for row in rows:
        key = (str(row.get("symbol", "")), str(row.get("side", "")))
        entered_at = float(row.get("entered_at", 0.0))
        last_exit = last_exit_at.get(key)
        if last_exit is not None and entered_at - last_exit < minutes * 60.0:
            continue
        out.append(row)
        last_exit_at[key] = float(row.get("exited_at", entered_at))
    return out


def causal_long_only_loss_block(rows: list[dict]) -> list[dict]:
    return causal_same_symbol_loss_block(long_only(rows), block_minutes=30.0)


def oracle_hold_le(rows: list[dict], max_hold_sec: float) -> list[dict]:
    return [r for r in rows if float(r.get("held_seconds", 0.0)) <= max_hold_sec]


VARIANTS = [
    ("actual_2", "baseline", actual),
    ("long_only", "causal", long_only),
    ("same_symbol_loss_block_30m", "causal", causal_same_symbol_loss_block),
    ("same_symbol_fast_reentry_block_15m", "causal", causal_same_symbol_fast_reentry_block),
    ("long_only_loss_block_30m", "causal", causal_long_only_loss_block),
    ("oracle_hold_le_180s", "oracle", lambda rows: oracle_hold_le(rows, 180.0)),
    ("oracle_hold_le_120s", "oracle", lambda rows: oracle_hold_le(rows, 120.0)),
]


def print_table(title: str, results: list[VariantResult]) -> None:
    print(f"\n=== {title} ===")
    print(f"{'variant':32s} {'kind':9s} {'trades':>7s} {'win%':>7s} {'pnl':>11s} {'maxLS':>7s}")
    for r in results:
        print(
            f"{r.name:32s} {r.kind:9s} {r.trades:7d} {r.win_rate:7.1f} "
            f"{r.pnl:+11.4f} {r.max_loss_streak:7d}"
        )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--days", nargs="*", type=int, default=[3, 5, 7], help="비교할 lookback day 목록")
    args = parser.parse_args()

    rows = load_rows(LEDGER_PATH)
    if not rows:
        print("trade_ledger 데이터가 없습니다.")
        return

    end_dt = rows[-1]["entered_dt"]
    for days in args.days:
        cutoff = end_dt - timedelta(days=days)
        subset = [r for r in rows if r["entered_dt"] >= cutoff]
        results = [calc_stats(fn(subset), name, kind) for name, kind, fn in VARIANTS]
        print_table(f"최근 {days}일 ({cutoff:%Y-%m-%d %H:%M} ~ {end_dt:%Y-%m-%d %H:%M})", results)


if __name__ == "__main__":
    main()
