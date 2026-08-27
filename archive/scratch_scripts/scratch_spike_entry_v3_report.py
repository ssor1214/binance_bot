"""[2026-08-16] Spike-based EARLY-ENTRY TRIGGER (not gate) design v3 backtest report.

Design under test (bot/main.py scan_entry_candidate, cfg.spike_entry_enabled):
  - Spike present (tick-volume spike detected via spike_based_entry_signal): enter
    EARLIER than the 1m-candle-confirmed baseline entry (at the spike tick time/price).
  - Spike absent: fall through UNCHANGED to the existing 1m-signal / next-candle-open
    fill path (baseline). This is the key difference from the earlier v2 experiment,
    which *gated* entries on spike presence (blocking non-spike trades entirely and
    inflating apparent win rate by discarding ~56% of real signals). v3 keeps every
    trade the baseline would have taken; it only ever swaps entry timing/price for
    the subset that had a spike.

No new network calls: reuses archive/scratch_scripts/scratch_spike_entry_results_20260815.json,
which was produced by scratch_spike_entry_v2_resolution_unified.py. That file already
re-simulates BOTH the baseline (1m-signal, next-1m-candle-open fill, walked forward with
the unmodified offline_backtest.exit_decision()) and, for the 17/45 samples where a
volume spike (detect_volume_spike, unmodified) was found before the real entry time, a
variant exit resimulated from the spike tick price/time -- same exit_decision(), same
resolution, no lookahead (spike search only looks backward from entered_at).

This script only re-aggregates that already-computed, leak-free data into the
baseline-vs-variant comparison required for the v3 sign-off decision. Trade count is
enforced identical (45 == 45) by construction: variant always keeps the baseline row
for the 28 non-spike samples untouched.
"""
from __future__ import annotations

import json
from pathlib import Path

RESULTS_PATH = Path(__file__).resolve().parent / "scratch_spike_entry_results_20260815.json"


def summarize(pnls: list[float], helds: list[float], label: str) -> dict:
    n = len(pnls)
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    win_rate = len(wins) / n * 100
    gross_win = sum(wins)
    gross_loss = -sum(losses) if losses else 0.0
    profit_factor = (gross_win / gross_loss) if gross_loss > 0 else float("inf")
    expectancy = sum(pnls) / n
    avg_held = sum(helds) / n
    # sequential equity curve (pnl%, unlevered/unweighted, comparison-only) -> max drawdown
    equity, peak, max_dd = 0.0, 0.0, 0.0
    for p in pnls:
        equity += p
        peak = max(peak, equity)
        max_dd = min(max_dd, equity - peak)
    result = dict(
        label=label, n=n, win_rate=win_rate, profit_factor=profit_factor,
        expectancy=expectancy, avg_held_sec=avg_held, sum_pnl=sum(pnls), max_dd=max_dd,
    )
    print(f"--- {label} ---")
    print(
        f"trades={n}  winrate={win_rate:.1f}%  PF={profit_factor:.2f}  "
        f"expectancy={expectancy:+.3f}%/trade  avg_held={avg_held:.1f}s  "
        f"sum_pnl={sum(pnls):+.2f}%  maxDD={max_dd:.2f}%"
    )
    return result


def main() -> None:
    data = json.loads(RESULTS_PATH.read_text(encoding="utf-8"))

    baseline_pnls = [r["baseline_pnl_pct_estimate"] for r in data]
    baseline_helds = [r["baseline_held_sec"] for r in data]

    variant_pnls, variant_helds = [], []
    early_entry_count = 0
    for r in data:
        if r.get("spike_found") and "variant_exit" in r:
            variant_pnls.append(r["variant_exit"]["pnl_roe_pct"])
            variant_helds.append(r["variant_exit"]["held_sec"])
            early_entry_count += 1
        else:
            # unchanged path: identical to baseline row (no spike -> no behavior change)
            variant_pnls.append(r["baseline_pnl_pct_estimate"])
            variant_helds.append(r["baseline_held_sec"])

    print(f"total samples: {len(data)}")
    print(f"early-entry (spike-triggered) trades: {early_entry_count} / {len(data)}")
    print(f"trade count check: baseline={len(baseline_pnls)} variant={len(variant_pnls)} "
          f"{'MATCH (no frequency loss)' if len(baseline_pnls) == len(variant_pnls) else 'MISMATCH!'}")
    print()

    b = summarize(baseline_pnls, baseline_helds, "BASELINE (existing: 1m-signal, next-candle-open fill)")
    v = summarize(variant_pnls, variant_helds, "VARIANT (spike->early entry trigger, no-spike->unchanged)")

    print()
    wins_needed = 2
    wins = 0
    for k, better in [("win_rate", v["win_rate"] > b["win_rate"]),
                       ("profit_factor", v["profit_factor"] > b["profit_factor"]),
                       ("sum_pnl", v["sum_pnl"] > b["sum_pnl"])]:
        print(f"{k}: baseline vs variant -> variant better: {better}")
        wins += int(better)

    freq_ok = len(baseline_pnls) == len(variant_pnls)
    verdict = "APPLY (권장)" if freq_ok and wins >= wins_needed else "HOLD (보류)"
    print(f"\nVerdict: {verdict}  (freq_preserved={freq_ok}, metrics_won={wins}/3)")


if __name__ == "__main__":
    main()
