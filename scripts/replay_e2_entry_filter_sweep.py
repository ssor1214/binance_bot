"""e2 entry-filter sweep on local 1m archive.

Purpose:
- Compare baseline vs A1/A2/B1/B2 with the same replay engine.
- Use local archive/binance_vision/s1 data only.

Notes:
- This is a local-archive replay, not the exact live-aligned 85-symbol engine Claude used.
- It is still useful for relative comparison across the same engine.

Experiments:
- baseline: min-risk 0.35
- A1: min-risk 0.40
- A2: min-risk 0.45
- B1: EMA-gap floor 0.05%
- B2: EMA25 slope floor 0.03%
"""
from __future__ import annotations

import json
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from zipfile import ZipFile

import pandas as pd


DATA_DIR = Path(__file__).resolve().parent.parent / "archive" / "binance_vision" / "s1"
OUT_PATH = Path(__file__).resolve().parent.parent / "archive" / "scratch_scripts" / "e2_entry_filter_sweep.json"
ROUND_TRIP_FEE_RATE = 0.001002


@dataclass
class Pending:
    side: str
    legs: list[float] = field(default_factory=list)
    done: int = 0
    since_idx: int = 0


@dataclass
class Position:
    symbol: str
    side: str
    legs: list[float]
    entry_idx: int
    stop_price: float
    tp_bb: float
    tp_rr: float

    @property
    def entry(self) -> float:
        return sum(self.legs) / len(self.legs)


def load_symbol_frames() -> dict[str, pd.DataFrame]:
    by_symbol: dict[str, list[tuple[str, Path]]] = defaultdict(list)
    for path in DATA_DIR.glob("*-1m-2026-08-*.zip"):
        if path.stem.count("-1m-") != 1:
            continue
        symbol, day = path.stem.split("-1m-")
        by_symbol[symbol].append((day, path))

    out: dict[str, pd.DataFrame] = {}
    for symbol, items in sorted(by_symbol.items()):
        frames: list[pd.DataFrame] = []
        for _day, path in sorted(items):
            with ZipFile(path) as zf:
                with zf.open(zf.namelist()[0]) as fp:
                    df = pd.read_csv(fp)
            for col in ("open", "high", "low", "close"):
                df[col] = df[col].astype(float)
            df["open_time"] = pd.to_datetime(df["open_time"], unit="ms", utc=True)
            frames.append(df[["open_time", "open", "high", "low", "close"]])
        out[symbol] = pd.concat(frames, ignore_index=True).sort_values("open_time").reset_index(drop=True)
    return out


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
    return out


def trade_net_pct(side: str, entry: float, exit_price: float) -> float:
    gross = (exit_price / entry - 1.0) if side == "LONG" else (entry / exit_price - 1.0)
    return (gross - ROUND_TRIP_FEE_RATE) * 100.0


def _passes_filters(row, side: str, close: float, cfg: dict) -> bool:
    if cfg.get("ema_gap_floor_pct", 0.0) > 0:
        gap_floor = cfg["ema_gap_floor_pct"] / 100.0
        gaps = [
            abs(float(row["e5"]) - float(row["e10"])) / close,
            abs(float(row["e10"]) - float(row["e15"])) / close,
            abs(float(row["e15"]) - float(row["e25"])) / close,
        ]
        if any(g < gap_floor for g in gaps):
            return False
    if cfg.get("ema25_slope_floor_pct", 0.0) > 0:
        prev = float(row["e25_prev"]) if pd.notna(row["e25_prev"]) else None
        if prev is None:
            return False
        slope = (float(row["e25"]) - prev) / close
        floor = cfg["ema25_slope_floor_pct"] / 100.0
        if side == "LONG":
            if slope < floor:
                return False
        else:
            if slope > -floor:
                return False
    return True


def replay_symbol(df: pd.DataFrame, symbol: str, cfg: dict) -> list[dict]:
    rows = add_indicators(df, 25)
    pending: Pending | None = None
    pos: Position | None = None
    trades: list[dict] = []
    targets_cols = ["e5", "e10", "e15"][: cfg["tranches"]]
    warmup = 30

    for i in range(warmup, len(rows)):
        row = rows.iloc[i]
        if pd.isna(row["bb_u"]) or pd.isna(row["e25"]) or pd.isna(row["e25_prev"]):
            continue

        if pos is not None:
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
                    "symbol": symbol,
                    "side": pos.side,
                    "legs": len(pos.legs),
                    "exit_reason": exit_reason,
                    "net_pct": trade_net_pct(pos.side, pos.entry, float(exit_price)),
                    "hold_sec": (i - pos.entry_idx) * 60.0,
                })
                pos = None
                pending = None

        e5 = float(row["e5"])
        e10 = float(row["e10"])
        e15 = float(row["e15"])
        e25 = float(row["e25"])
        is_long = e5 > e10 > e15 > e25
        is_short = e5 < e10 < e15 < e25
        if not (is_long or is_short):
            pending = None
            continue

        side = "LONG" if is_long else "SHORT"
        close = float(row["close"])
        if not _passes_filters(row, side, close, cfg):
            pending = None
            continue

        if pending is None:
            pending = Pending(side=side, since_idx=i)
        if pending.side != side or i - pending.since_idx > 60:
            pending = None
            continue

        lo = float(row["low"])
        hi = float(row["high"])
        targets = [float(row[c]) for c in targets_cols]
        k = len(pending.legs)
        if k < len(targets):
            tgt = targets[k]
            if (lo <= tgt) if is_long else (hi >= tgt):
                pending.legs.append(tgt)
        if len(pending.legs) == pending.done:
            continue

        entry = sum(pending.legs) / len(pending.legs)
        stop = e25
        # Guard: signals already beyond stop are invalid.
        if (side == "LONG" and entry <= stop) or (side == "SHORT" and entry >= stop):
            pending = None
            continue
        risk = abs(entry - stop) / entry
        if risk * 100.0 < cfg["min_risk_pct"]:
            pending = None
            continue
        tp_bb = float(row["bb_u"] if is_long else row["bb_l"])
        if tp_bb and ((entry >= tp_bb) if is_long else (entry <= tp_bb)):
            pending = None
            continue

        if pos is not None and pos.side == side:
            pos.legs = list(pending.legs)
            pos.stop_price = stop
            pos.tp_bb = tp_bb
            new_risk = abs(pos.entry - stop) / pos.entry if pos.entry else 0.0
            pos.tp_rr = (pos.entry * (1.0 + cfg["rr"] * new_risk) if is_long else pos.entry * (1.0 - cfg["rr"] * new_risk)) if new_risk > 0 else 0.0
            pending.done = len(pending.legs)
            continue

        pos = Position(
            symbol=symbol,
            side=side,
            legs=list(pending.legs),
            entry_idx=i,
            stop_price=stop,
            tp_bb=tp_bb,
            tp_rr=(entry * (1.0 + cfg["rr"] * risk) if is_long else entry * (1.0 - cfg["rr"] * risk)),
        )
        pending.done = len(pending.legs)
    return trades


def summarize(trades: list[dict]) -> dict:
    n = len(trades)
    wins = sum(1 for t in trades if t["net_pct"] > 0)
    net = sum(t["net_pct"] for t in trades)
    avg = net / n if n else 0.0
    stop_n = sum(1 for t in trades if t["exit_reason"] == "STOP_EMA25")
    hold_med = float(pd.Series([t["hold_sec"] for t in trades]).median()) if n else 0.0
    by_reason = Counter(t["exit_reason"] for t in trades)
    by_reason_avg = {}
    for key in by_reason:
        subset = [t["net_pct"] for t in trades if t["exit_reason"] == key]
        by_reason_avg[key] = sum(subset) / len(subset)
    return {
        "trades": n,
        "win_rate": (wins / n * 100.0) if n else 0.0,
        "avg_pct": avg,
        "stop_share": (stop_n / n * 100.0) if n else 0.0,
        "hold_med": hold_med,
        "exit_reason_counts": dict(by_reason),
        "exit_reason_avg_pct": by_reason_avg,
    }


def main() -> None:
    configs = [
        {"name": "baseline", "min_risk_pct": 0.35, "ema_gap_floor_pct": 0.0, "ema25_slope_floor_pct": 0.0, "tranches": 3, "rr": 2.0},
        {"name": "A1_min_risk_0.40", "min_risk_pct": 0.40, "ema_gap_floor_pct": 0.0, "ema25_slope_floor_pct": 0.0, "tranches": 3, "rr": 2.0},
        {"name": "A2_min_risk_0.45", "min_risk_pct": 0.45, "ema_gap_floor_pct": 0.0, "ema25_slope_floor_pct": 0.0, "tranches": 3, "rr": 2.0},
        {"name": "B1_gap_floor_0.05", "min_risk_pct": 0.35, "ema_gap_floor_pct": 0.05, "ema25_slope_floor_pct": 0.0, "tranches": 3, "rr": 2.0},
        {"name": "B2_e25_slope_0.03", "min_risk_pct": 0.35, "ema_gap_floor_pct": 0.0, "ema25_slope_floor_pct": 0.03, "tranches": 3, "rr": 2.0},
    ]
    symbol_frames = load_symbol_frames()
    print(f"대상 심볼 {len(symbol_frames)}개")
    results = {}
    for cfg in configs:
        trades: list[dict] = []
        for symbol, frame in symbol_frames.items():
            trades.extend(replay_symbol(frame, symbol, cfg))
        stats = summarize(trades)
        results[cfg["name"]] = stats
        print(
            f"{cfg['name']:18s} trades={stats['trades']:6d} avg={stats['avg_pct']:+.4f}% "
            f"win={stats['win_rate']:.1f}% stop={stats['stop_share']:.1f}% hold_med={stats['hold_med']:.0f}s"
        )
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"상세 저장: {OUT_PATH}")


if __name__ == "__main__":
    main()
