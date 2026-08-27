"""Entry-only diagnostics for e2.

Purpose:
- Compare initial deploy-style e2 entries without SL/TP exits.
- Measure future excursion quality after entry at fixed horizons.

This is a diagnostic script, not a PnL backtest.
"""
from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path

import pandas as pd

from scripts.replay_e2_close_entry_b2 import add_indicators, iter_symbol_paths, load_symbol_frame


OUT_PATH = Path(__file__).resolve().parent.parent / "archive" / "scratch_scripts" / "e2_entry_only_diagnostics.json"


def entry_diag_for_symbol(
    symbol: str,
    df: pd.DataFrame,
    min_risk_pct: float,
    tranches: int = 3,
    keep_pending_after_entry: bool = True,
) -> list[dict]:
    rows = add_indicators(df, stop_ema=25)
    pending = None
    active = None
    diags: list[dict] = []
    warmup = 30

    for i in range(warmup, len(rows)):
        row = rows.iloc[i]
        if pd.isna(row["bb_u"]) or pd.isna(row["e25"]):
            continue

        e5, e10, e15, e25 = (float(row["e5"]), float(row["e10"]), float(row["e15"]), float(row["e25"]))
        is_long = e5 > e10 > e15 > e25
        is_short = e5 < e10 < e15 < e25
        if not (is_long or is_short):
            pending = None
            if active is None:
                continue
        side = "LONG" if is_long else "SHORT"
        close = float(row["close"])

        if active is None:
            if pending is None:
                pending = {"side": side, "touched": 0, "done": 0, "since_idx": i}
            if pending["side"] != side or i - pending["since_idx"] > 60:
                pending = None
                continue

            targets = [e5, e10, e15][:tranches]
            lo, hi = float(row["low"]), float(row["high"])
            while pending["touched"] < len(targets):
                tgt = targets[pending["touched"]]
                touched = (lo <= tgt) if side == "LONG" else (hi >= tgt)
                if not touched:
                    break
                pending["touched"] += 1

            if pending["touched"] == pending["done"]:
                continue

            entry = close
            stop = e25
            if (side == "LONG" and entry <= stop) or (side == "SHORT" and entry >= stop):
                pending = None
                continue
            risk = abs(entry - stop) / entry
            if risk * 100.0 < min_risk_pct:
                pending = None
                continue

            active = {
                "symbol": symbol,
                "side": side,
                "legs": pending["touched"],
                "entry_idx": i,
                "entry": entry,
            }
            pending["done"] = pending["touched"]
            if not keep_pending_after_entry:
                pending = None
            continue

        if active["side"] != side:
            active = None
            pending = None
            continue

        if keep_pending_after_entry and pending is not None:
            targets = [e5, e10, e15][:tranches]
            lo, hi = float(row["low"]), float(row["high"])
            while pending["touched"] < len(targets):
                tgt = targets[pending["touched"]]
                touched = (lo <= tgt) if side == "LONG" else (hi >= tgt)
                if not touched:
                    break
                pending["touched"] += 1
            if pending["touched"] > active["legs"]:
                active["legs"] = pending["touched"]
                pending["done"] = pending["touched"]

        age_sec = (i - active["entry_idx"]) * 60.0
        if age_sec < 300.0:
            continue

        window = rows.iloc[active["entry_idx"] : i + 1]
        entry = active["entry"]
        if active["side"] == "LONG":
            fav_180 = (float(window.iloc[:4]["high"].max()) / entry - 1.0) * 100.0
            adv_180 = (float(window.iloc[:4]["low"].min()) / entry - 1.0) * 100.0
            fav_300 = (float(window["high"].max()) / entry - 1.0) * 100.0
            adv_300 = (float(window["low"].min()) / entry - 1.0) * 100.0
        else:
            fav_180 = (entry / float(window.iloc[:4]["low"].min()) - 1.0) * 100.0
            adv_180 = (entry / float(window.iloc[:4]["high"].max()) - 1.0) * 100.0
            fav_300 = (entry / float(window["low"].min()) - 1.0) * 100.0
            adv_300 = (entry / float(window["high"].max()) - 1.0) * 100.0
        diags.append({
            "symbol": active["symbol"],
            "side": active["side"],
            "legs": active["legs"],
            "fav_180": fav_180,
            "adv_180": adv_180,
            "fav_300": fav_300,
            "adv_300": adv_300,
        })
        active = None
        if not keep_pending_after_entry:
            pending = None
    return diags


def summarize(diags: list[dict]) -> dict:
    n = len(diags)
    if not n:
        return {"trades": 0}
    out = {
        "trades": n,
        "legs_ge2_share": sum(1 for d in diags if d["legs"] >= 2) / n * 100.0,
        "legs_ge3_share": sum(1 for d in diags if d["legs"] >= 3) / n * 100.0,
    }
    for horizon in ("180", "300"):
        fav_key = f"fav_{horizon}"
        adv_key = f"adv_{horizon}"
        favs = [d[fav_key] for d in diags]
        advs = [d[adv_key] for d in diags]
        out[f"fav_{horizon}_avg"] = sum(favs) / n
        out[f"adv_{horizon}_avg"] = sum(advs) / n
        out[f"fav_{horizon}_gt_0.10"] = sum(1 for v in favs if v > 0.10) / n * 100.0
        out[f"fav_{horizon}_gt_0.20"] = sum(1 for v in favs if v > 0.20) / n * 100.0
    return out


def run_variant(name: str, min_risk_pct: float, keep_pending_after_entry: bool) -> dict:
    diags = []
    for symbol, paths in iter_symbol_paths():
        df = load_symbol_frame(paths)
        diags.extend(entry_diag_for_symbol(
            symbol,
            df,
            min_risk_pct=min_risk_pct,
            tranches=3,
            keep_pending_after_entry=keep_pending_after_entry,
        ))
    return summarize(diags)


def main() -> None:
    variants = {
        "initial_entry_only": run_variant("initial_entry_only", min_risk_pct=0.0, keep_pending_after_entry=True),
        "initial_entry_only_bug5": run_variant("initial_entry_only_bug5", min_risk_pct=0.0, keep_pending_after_entry=False),
        "current_entry_only": run_variant("current_entry_only", min_risk_pct=0.35, keep_pending_after_entry=True),
    }
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(variants, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(variants, ensure_ascii=False, indent=2))
    print(f\"saved={OUT_PATH}\")


if __name__ == "__main__":
    main()
