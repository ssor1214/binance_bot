"""e2 원장으로 심볼 단기 밴 규칙을 사후 비교한다.

주의:
- 원장 기반 사후분석이다. 밴으로 스킵된 거래 이후의 실제 시장/슬롯 변화는 재현하지 못한다.
- 따라서 "그 거래를 안 했으면 확정 손익/명목/건수가 어떻게 바뀌었나"를 보는 1차 추정이다.
"""
from __future__ import annotations

import argparse
import json
from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
LEDGER = ROOT / "logs" / "scalp_bot_e2_ledger.jsonl"


@dataclass(frozen=True)
class Variant:
    name: str
    loss_streak: int = 0
    window_losses: int = 0
    window_sec: float = 0.0
    stop_weight: int = 1
    ban_sec: float = 0.0


VARIANTS = [
    Variant("base_no_ban"),
    Variant("loss1_ban20m", loss_streak=1, ban_sec=20 * 60),
    Variant("loss1_ban60m", loss_streak=1, ban_sec=60 * 60),
    Variant("streak2_ban20m", loss_streak=2, ban_sec=20 * 60),
    Variant("streak2_ban30m", loss_streak=2, ban_sec=30 * 60),
    Variant("streak2_ban60m", loss_streak=2, ban_sec=60 * 60),
    Variant("loss2_30m_ban30m", window_losses=2, window_sec=30 * 60, ban_sec=30 * 60),
    Variant("loss2_30m_ban60m", window_losses=2, window_sec=30 * 60, ban_sec=60 * 60),
    Variant("stop_weight2_streak2_ban30m", loss_streak=2, stop_weight=2, ban_sec=30 * 60),
]


def load_rows() -> list[dict]:
    rows = []
    if not LEDGER.exists():
        return rows
    for line in LEDGER.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        r = json.loads(line)
        if r.get("dry_run"):
            continue
        rows.append(r)
    rows.sort(key=lambda r: (float(r.get("entered_at") or 0.0), float(r.get("exited_at") or 0.0)))
    return rows


def is_loss(r: dict) -> bool:
    return float(r.get("real_net", 0.0) or 0.0) < 0


def loss_weight(r: dict, stop_weight: int) -> int:
    if not is_loss(r):
        return 0
    reason = str(r.get("exit_reason") or "")
    if reason in ("STOP_EXCHANGE", "STOP_EMA25", "STOP_EMA"):
        return stop_weight
    return 1


def apply_variant(rows: list[dict], variant: Variant) -> tuple[list[dict], list[dict]]:
    if variant.name == "base_no_ban":
        return list(rows), []

    ban_until: dict[str, float] = defaultdict(float)
    streak_score: dict[str, int] = defaultdict(int)
    loss_times: dict[str, deque[tuple[float, int]]] = defaultdict(deque)
    kept = []
    skipped = []

    for r in rows:
        sym = str(r.get("symbol"))
        ent = float(r.get("entered_at") or 0.0)
        ext = float(r.get("exited_at") or ent)
        if ent < ban_until[sym]:
            skipped.append(r)
            continue

        kept.append(r)
        w = loss_weight(r, variant.stop_weight)
        if w <= 0:
            streak_score[sym] = 0
            loss_times[sym].clear()
            continue

        if variant.loss_streak:
            streak_score[sym] += w
            if streak_score[sym] >= variant.loss_streak:
                ban_until[sym] = max(ban_until[sym], ext + variant.ban_sec)
                streak_score[sym] = 0

        if variant.window_losses:
            q = loss_times[sym]
            q.append((ext, w))
            while q and ext - q[0][0] > variant.window_sec:
                q.popleft()
            if sum(x[1] for x in q) >= variant.window_losses:
                ban_until[sym] = max(ban_until[sym], ext + variant.ban_sec)
                q.clear()
    return kept, skipped


def summarize(rows: list[dict], base_rows: list[dict] | None = None) -> dict:
    n = len(rows)
    wins = sum(1 for r in rows if float(r.get("real_net", 0.0) or 0.0) > 0)
    net = sum(float(r.get("real_net", 0.0) or 0.0) for r in rows)
    nominal = sum(float(r.get("nominal", 0.0) or 0.0) for r in rows)
    start = min((float(r.get("entered_at") or 0.0) for r in base_rows or rows), default=0.0)
    end = max((float(r.get("exited_at") or 0.0) for r in base_rows or rows), default=start)
    hours = max((end - start) / 3600.0, 1e-9)
    return {
        "trades": n,
        "win_rate": wins / n * 100.0 if n else 0.0,
        "net": net,
        "nominal": nominal,
        "net_per_nominal_pct": net / nominal * 100.0 if nominal else 0.0,
        "trades_per_hour": n / hours,
        "nominal_per_hour": nominal / hours,
    }


def run_set(rows: list[dict], label: str) -> list[dict]:
    base_stats = summarize(rows)
    out = []
    for v in VARIANTS:
        kept, skipped = apply_variant(rows, v)
        st = summarize(kept, rows)
        skipped_nom = sum(float(r.get("nominal", 0.0) or 0.0) for r in skipped)
        out.append({
            "label": label,
            "variant": v.name,
            **st,
            "skipped": len(skipped),
            "trade_reduction_pct": (len(skipped) / max(len(rows), 1) * 100.0),
            "nominal_reduction_pct": (skipped_nom / max(base_stats["nominal"], 1e-9) * 100.0),
            "net_delta": st["net"] - base_stats["net"],
        })
    return out


def print_table(rows: list[dict]) -> None:
    print("set,variant,trades,skip,trade_cut,nominal_cut,trades_hr,nominal_hr,win,net,delta,net_nom")
    for r in rows:
        print(
            f"{r['label']},{r['variant']},{r['trades']},{r['skipped']},"
            f"{r['trade_reduction_pct']:.1f}%,{r['nominal_reduction_pct']:.1f}%,"
            f"{r['trades_per_hour']:.2f},{r['nominal_per_hour']:.2f},"
            f"{r['win_rate']:.1f}%,{r['net']:+.4f},{r['net_delta']:+.4f},"
            f"{r['net_per_nominal_pct']:+.3f}%"
        )


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--out", type=Path, default=ROOT / "archive" / "scratch_scripts" / "e2_symbol_ban_analysis.json")
    args = p.parse_args()

    rows = load_rows()
    if not rows:
        raise SystemExit("ledger rows not found")

    live_only = [r for r in rows if not r.get("reconstructed")]
    since_23 = [
        r for r in rows
        if datetime.fromtimestamp(float(r.get("exited_at") or 0.0)).hour >= 23
    ]

    result = []
    result.extend(run_set(rows, "all_actual"))
    result.extend(run_set(since_23, "23h_actual"))
    result.extend(run_set(live_only, "post_restart_live"))

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print_table(result)
    print(f"saved={args.out}")


if __name__ == "__main__":
    main()
