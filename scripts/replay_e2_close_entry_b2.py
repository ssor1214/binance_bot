"""Reconstruct e2 close-entry baseline and B2 on local 1m archive.

Model:
- Alignment: EMA5 > EMA10 > EMA15 > EMA25 (or reverse for short)
- During a bar, if price touches EMA5/10/15, mark pending legs
- Entry happens at that bar close price ("봉마감 진입")
- Apply live-like guards on the actual entry price:
  - skip if already beyond stop
  - skip if TP band already passed
  - skip if risk < min-risk-pct
- Exit priority: STOP_EMA25 -> BB -> RR

This is a reconstruction from handoff docs, not Claude's original script.
Use for relative confirmation only.
"""
from __future__ import annotations

import json
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from zipfile import ZipFile

import pandas as pd


DATA_DIR = Path(__file__).resolve().parent.parent / "archive" / "binance_vision" / "s1"
OUT_PATH = Path(__file__).resolve().parent.parent / "archive" / "scratch_scripts" / "e2_close_entry_b2.json"
ROUND_TRIP_FEE_RATE = 0.001002


@dataclass
class Pending:
    side: str
    touched: int = 0
    done: int = 0
    since_idx: int = 0


@dataclass
class Position:
    symbol: str
    side: str
    entries: list[float] = field(default_factory=list)
    entry_idx: int = 0
    stop_price: float = 0.0
    tp_bb: float = 0.0
    tp_rr: float = 0.0
    max_favorable_roe: float = 0.0
    max_adverse_roe: float = 0.0
    early_favorable_180: float = 0.0

    @property
    def entry(self) -> float:
        return sum(self.entries) / len(self.entries)


def iter_symbol_paths() -> list[tuple[str, list[Path]]]:
    by_symbol: dict[str, list[tuple[str, Path]]] = defaultdict(list)
    for path in DATA_DIR.glob("*-1m-2026-08-*.zip"):
        if path.stem.count("-1m-") != 1:
            continue
        symbol, day = path.stem.split("-1m-")
        by_symbol[symbol].append((day, path))
    return [(sym, [p for _, p in sorted(items)]) for sym, items in sorted(by_symbol.items())]


def load_symbol_frame(paths: list[Path]) -> pd.DataFrame:
    frames: list[pd.DataFrame] = []
    for path in paths:
        with ZipFile(path) as zf:
            with zf.open(zf.namelist()[0]) as fp:
                df = pd.read_csv(fp)
        for col in ("open", "high", "low", "close"):
            df[col] = df[col].astype(float)
        df["open_time"] = pd.to_datetime(df["open_time"], unit="ms", utc=True)
        frames.append(df[["open_time", "open", "high", "low", "close"]])
    return pd.concat(frames, ignore_index=True).sort_values("open_time").reset_index(drop=True)


def add_indicators(df: pd.DataFrame, stop_ema: int = 25) -> pd.DataFrame:
    out = df.copy()
    close = out["close"]
    out["e5"] = close.ewm(span=5, adjust=False).mean()
    out["e10"] = close.ewm(span=10, adjust=False).mean()
    out["e15"] = close.ewm(span=15, adjust=False).mean()
    out["e25"] = close.ewm(span=stop_ema, adjust=False).mean()
    out["e25_prev"] = out["e25"].shift(1)
    mu = close.rolling(20).mean()
    sd = close.rolling(20).std(ddof=0)
    out["bb_u"] = mu + 2 * sd
    out["bb_l"] = mu - 2 * sd
    out["bb_width_pct"] = ((out["bb_u"] - out["bb_l"]) / close) * 100.0
    return out


def net_pct(side: str, entry: float, exit_price: float) -> float:
    gross = (exit_price / entry - 1.0) if side == "LONG" else (entry / exit_price - 1.0)
    return (gross - ROUND_TRIP_FEE_RATE) * 100.0


def passes_b2(row, side: str, close: float, slope_floor_pct: float) -> bool:
    if slope_floor_pct <= 0:
        return True
    if pd.isna(row["e25_prev"]):
        return False
    slope = (float(row["e25"]) - float(row["e25_prev"])) / close
    floor = slope_floor_pct / 100.0
    return slope >= floor if side == "LONG" else slope <= -floor


def passes_gap(row, close: float, gap_floor_pct: float) -> bool:
    if gap_floor_pct <= 0:
        return True
    floor = gap_floor_pct / 100.0
    gaps = [
        abs(float(row["e5"]) - float(row["e10"])) / close,
        abs(float(row["e10"]) - float(row["e15"])) / close,
        abs(float(row["e15"]) - float(row["e25"])) / close,
    ]
    return all(g >= floor for g in gaps)


def passes_close_location(row, side: str, clv_floor: float) -> bool:
    if clv_floor <= 0:
        return True
    high = float(row["high"])
    low = float(row["low"])
    close = float(row["close"])
    span = high - low
    if span <= 0:
        return False
    clv = (close - low) / span
    return clv >= clv_floor if side == "LONG" else clv <= (1.0 - clv_floor)


def passes_bb_width(row, bb_width_floor_pct: float) -> bool:
    if bb_width_floor_pct <= 0:
        return True
    width = float(row.get("bb_width_pct", 0.0) or 0.0)
    return width >= bb_width_floor_pct


def replay_symbol(
    symbol: str,
    df: pd.DataFrame,
    min_risk_pct: float,
    slope_floor_pct: float,
    tranches: int = 3,
    rr: float = 2.0,
    stop_ema: int = 25,
    gap_floor_pct: float = 0.0,
    keep_pending_after_entry: bool = True,
    clv_floor: float = 0.0,
    bb_width_floor_pct: float = 0.0,
) -> list[dict]:
    rows = add_indicators(df, stop_ema=stop_ema)
    pending: Pending | None = None
    pos: Position | None = None
    trades: list[dict] = []
    warmup = 30

    for i in range(warmup, len(rows)):
        row = rows.iloc[i]
        if pd.isna(row["bb_u"]) or pd.isna(row["e25"]):
            continue

        if pos is not None:
            if pos.side == "LONG":
                favorable_roe = (float(row["high"]) / pos.entry - 1.0) * 100.0
                adverse_roe = (float(row["low"]) / pos.entry - 1.0) * 100.0
            else:
                favorable_roe = (pos.entry / float(row["low"]) - 1.0) * 100.0
                adverse_roe = (pos.entry / float(row["high"]) - 1.0) * 100.0
            pos.max_favorable_roe = max(pos.max_favorable_roe, favorable_roe)
            pos.max_adverse_roe = min(pos.max_adverse_roe, adverse_roe)
            if (i - pos.entry_idx) * 60.0 <= 180.0:
                pos.early_favorable_180 = max(pos.early_favorable_180, favorable_roe)
            exit_reason = None
            exit_price = None
            if pos.side == "LONG":
                if row["low"] <= pos.stop_price:
                    exit_reason = "STOP_EMA25"
                    exit_price = pos.stop_price
                elif pos.tp_bb and row["high"] >= pos.tp_bb:
                    exit_reason = "BB"
                    exit_price = pos.tp_bb
                elif pos.tp_rr and row["high"] >= pos.tp_rr:
                    exit_reason = "RR"
                    exit_price = pos.tp_rr
            else:
                if row["high"] >= pos.stop_price:
                    exit_reason = "STOP_EMA25"
                    exit_price = pos.stop_price
                elif pos.tp_bb and row["low"] <= pos.tp_bb:
                    exit_reason = "BB"
                    exit_price = pos.tp_bb
                elif pos.tp_rr and row["low"] <= pos.tp_rr:
                    exit_reason = "RR"
                    exit_price = pos.tp_rr
            if exit_reason:
                trades.append({
                    "symbol": pos.symbol,
                    "side": pos.side,
                    "legs": len(pos.entries),
                    "exit_reason": exit_reason,
                    "net_pct": net_pct(pos.side, pos.entry, float(exit_price)),
                    "hold_sec": (i - pos.entry_idx) * 60.0,
                    "max_favorable_roe": pos.max_favorable_roe,
                    "max_adverse_roe": pos.max_adverse_roe,
                    "early_favorable_180": pos.early_favorable_180,
                })
                pos = None
                pending = None

        e5, e10, e15, e25 = (float(row["e5"]), float(row["e10"]), float(row["e15"]), float(row["e25"]))
        is_long = e5 > e10 > e15 > e25
        is_short = e5 < e10 < e15 < e25
        if not (is_long or is_short):
            pending = None
            continue
        side = "LONG" if is_long else "SHORT"
        close = float(row["close"])
        if not passes_b2(row, side, close, slope_floor_pct):
            pending = None
            continue
        if not passes_gap(row, close, gap_floor_pct):
            pending = None
            continue
        if not passes_close_location(row, side, clv_floor):
            pending = None
            continue
        if not passes_bb_width(row, bb_width_floor_pct):
            pending = None
            continue

        if pending is None:
            pending = Pending(side=side, since_idx=i)
        if pending.side != side or i - pending.since_idx > 60:
            pending = None
            continue

        targets = [e5, e10, e15][:tranches]
        lo, hi = float(row["low"]), float(row["high"])
        while pending.touched < len(targets):
            tgt = targets[pending.touched]
            touched = (lo <= tgt) if side == "LONG" else (hi >= tgt)
            if not touched:
                break
            pending.touched += 1

        if pending.touched == pending.done:
            continue

        entry = close
        stop = e25
        tp_bb = float(row["bb_u"] if side == "LONG" else row["bb_l"])
        if (side == "LONG" and entry <= stop) or (side == "SHORT" and entry >= stop):
            pending = None
            continue
        if tp_bb and ((entry >= tp_bb) if side == "LONG" else (entry <= tp_bb)):
            pending = None
            continue
        risk = abs(entry - stop) / entry
        if risk * 100.0 < min_risk_pct:
            pending = None
            continue

        if pos is not None and pos.side == side:
            new_entries = pending.touched - pending.done
            pos.entries.extend([entry] * new_entries)
            pos.stop_price = stop
            pos.tp_bb = tp_bb
            new_risk = abs(pos.entry - stop) / pos.entry if pos.entry else 0.0
            pos.tp_rr = (pos.entry * (1.0 + rr * new_risk) if side == "LONG" else pos.entry * (1.0 - rr * new_risk)) if new_risk > 0 else 0.0
            pending.done = pending.touched
            if not keep_pending_after_entry:
                pending = None
            continue

        pos = Position(
            symbol=symbol,
            side=side,
            entries=[entry] * pending.touched,
            entry_idx=i,
            stop_price=stop,
            tp_bb=tp_bb,
            tp_rr=(entry * (1.0 + rr * risk) if side == "LONG" else entry * (1.0 - rr * risk)),
        )
        pending.done = pending.touched
        if not keep_pending_after_entry:
            pending = None
    return trades


def summarize(trades: list[dict]) -> dict:
    n = len(trades)
    wins = sum(1 for t in trades if t["net_pct"] > 0)
    total = sum(t["net_pct"] for t in trades)
    stop_n = sum(1 for t in trades if t["exit_reason"] == "STOP_EMA25")
    by_reason = Counter(t["exit_reason"] for t in trades)
    reason_avg = {}
    for key in by_reason:
        vals = [t["net_pct"] for t in trades if t["exit_reason"] == key]
        reason_avg[key] = sum(vals) / len(vals)
    return {
        "trades": n,
        "avg_pct": total / n if n else 0.0,
        "win_rate": wins / n * 100.0 if n else 0.0,
        "stop_share": stop_n / n * 100.0 if n else 0.0,
        "hold_med": float(pd.Series([t["hold_sec"] for t in trades]).median()) if n else 0.0,
        "exit_reason_counts": dict(by_reason),
        "exit_reason_avg_pct": reason_avg,
    }


def main() -> None:
    variants = [
        ("baseline_close", 0.35, 0.0, 3, 25, 0.0),
        ("B2_close_e25_slope_0.03", 0.35, 0.03, 3, 25, 0.0),
    ]
    results = {}
    symbol_paths = iter_symbol_paths()
    print(f"대상 심볼 {len(symbol_paths)}개")
    for name, min_risk_pct, slope_floor_pct, tranches, stop_ema, gap_floor_pct in variants:
        trades: list[dict] = []
        for _symbol, paths in symbol_paths:
            df = load_symbol_frame(paths)
            trades.extend(replay_symbol(
                _symbol,
                df,
                min_risk_pct=min_risk_pct,
                slope_floor_pct=slope_floor_pct,
                tranches=tranches,
                stop_ema=stop_ema,
                gap_floor_pct=gap_floor_pct,
            ))
        stats = summarize(trades)
        results[name] = stats
        print(
            f"{name:24s} trades={stats['trades']:6d} avg={stats['avg_pct']:+.4f}% "
            f"win={stats['win_rate']:.1f}% stop={stats['stop_share']:.1f}% hold_med={stats['hold_med']:.0f}s"
        )
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"상세 저장: {OUT_PATH}")


if __name__ == "__main__":
    main()
