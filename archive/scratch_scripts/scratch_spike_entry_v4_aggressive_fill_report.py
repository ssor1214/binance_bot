"""[2026-08-16] v4 step 2/2: aggregate step-1 fill-probability data
(scratch_spike_entry_v4_fill_probability_fetch.py output) against the existing
scratch_spike_entry_results_20260815.json to compare:

  BASELINE = today's LIVE mechanism exactly as configured in .env
    (LIMIT_ENTRY_ENABLED=true, LIMIT_ENTRY_PULLBACK_PCT=0.0 -> passive order resting at
    the touch price at signal time, LIMIT_ENTRY_WAIT_SEC=10s, cancel-and-skip on timeout).
    For the 17 early_entry_spike-tagged trades, a trade is INCLUDED only if the tick-level
    fill-probability proxy (step 1) shows the market traded through the resting price
    within the 10s window; the 3 that don't are excluded entirely (real missed trades,
    not counted as losses -- they never happened under the live mechanism).

  VARIANT = same 45-trade set, but candidate["early_entry_spike"]==True trades now use
    the aggressive fill this session added to bot/main.py: place_entry_order(...,
    aggressive=True) crosses the spread (LONG buys at ask / SHORT sells at bid) instead of
    resting passively, so it captures the same fill price/time already recorded as
    baseline_entry_price/entered_at in the original ledger for ALL 17 (the 3 that were
    "missed" under the passive path are recovered).

No lookahead: reuses only already-decided real trade prices/exits/times; no future
information used. Non-spike-tagged 28 trades are byte-identical in both paths.
"""
from __future__ import annotations

import json
from pathlib import Path

RESULTS_PATH = Path(__file__).resolve().parent / "scratch_spike_entry_results_20260815.json"
FILLPROB_PATH = Path(
    r"C:\Users\lg\AppData\Local\Temp\claude\c--Users-lg-Desktop-binance-futures-bot"
    r"\0dad9f52-9f21-4dc5-ad53-9d30561acc41\scratchpad\spike_v4_fill_probability.json"
)


def summarize(rows: list[dict], label: str) -> dict:
    pnls = [r["baseline_pnl_pct_estimate"] for r in rows]
    helds = [r["baseline_held_sec"] for r in rows]
    n = len(pnls)
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    win_rate = len(wins) / n * 100
    gross_win = sum(wins)
    gross_loss = -sum(losses) if losses else 0.0
    pf = gross_win / gross_loss if gross_loss > 0 else float("inf")
    expectancy = sum(pnls) / n
    avg_held = sum(helds) / n
    equity, peak, max_dd = 0.0, 0.0, 0.0
    for p in pnls:
        equity += p
        peak = max(peak, equity)
        max_dd = min(max_dd, equity - peak)
    result = dict(label=label, n=n, win_rate=win_rate, pf=pf, expectancy=expectancy,
                  avg_held=avg_held, sum_pnl=sum(pnls), max_dd=max_dd)
    print(f"--- {label} ---")
    print(f"trades={n}  winrate={win_rate:.1f}%  PF={pf:.2f}  expectancy={expectancy:+.3f}%/trade  "
          f"avg_held={avg_held:.1f}s  sum_pnl={sum(pnls):+.2f}%  maxDD={max_dd:.2f}%")
    return result


def main() -> None:
    data = json.loads(RESULTS_PATH.read_text(encoding="utf-8"))
    fillprob = json.loads(FILLPROB_PATH.read_text(encoding="utf-8"))
    fillmap = {(r["symbol"], r["entered_at"]): r for r in fillprob}

    baseline_rows, variant_rows, missed = [], [], []
    for r in data:
        if not r.get("spike_found"):
            baseline_rows.append(r)
            variant_rows.append(r)
            continue
        fp = fillmap.get((r["symbol"], r["entered_at"]))
        variant_rows.append(r)
        if fp and fp.get("passive_would_fill"):
            baseline_rows.append(r)
        else:
            missed.append(r)

    spike_n = sum(1 for r in data if r.get("spike_found"))
    print(f"total samples: {len(data)}  early_entry_spike-tagged: {spike_n}")
    print(f"of those, missed under today's live passive fill (pullback=0%, 10s wait): {len(missed)}")
    for r in missed:
        print(f"  MISSED (rescued by aggressive fill): {r['symbol']} {r['side']} "
              f"would-have-been pnl={r['baseline_pnl_pct_estimate']:+.2f}% ({r['baseline_exit_reason']})")
    print()

    b = summarize(baseline_rows, "BASELINE (live today: passive limit, pullback=0%, 10s wait, misses excluded)")
    v = summarize(variant_rows, "VARIANT (spike-tagged -> aggressive spread-crossing fill)")

    print()
    print(f"trade count: baseline={b['n']}  variant={v['n']}  "
          f"delta={v['n']-b['n']:+d} (rescued from being silently skipped)")
    for k in ("win_rate", "pf", "sum_pnl", "expectancy"):
        print(f"{k}: baseline={b[k]:.3f}  variant={v[k]:.3f}  "
              f"{'variant better' if v[k] > b[k] else 'baseline better/equal'}")


if __name__ == "__main__":
    main()
