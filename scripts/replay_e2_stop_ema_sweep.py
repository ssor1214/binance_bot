"""e2 손절 기준 EMA 스윕 리플레이.

로컬 archive/binance_vision/s1 의 1분봉 zip을 읽어 e2 규칙을 오프라인 재현한다.
질문: EMA25 대신 EMA20/30/40으로 손절 기준선을 바꾸면 나아지는가?

주의:
- 현재 저장소에 남아 있는 것은 1분봉 아카이브이므로, 이전 -0.1988% 실측처럼
  "진입 지연 2초 + 실제 체결가"까지는 완전 재현하지 못한다.
- 대신 e2 본체의 규칙(정배열, EMA5/10/15 분할 진입, stop EMA 손절, BB/RR 익절,
  min-risk 필터, same-bar 우선순위)을 그대로 맞춘 비교용 스윕이다.

실행:
  .venv312\\Scripts\\python.exe scripts\\replay_e2_stop_ema_sweep.py
  .venv312\\Scripts\\python.exe scripts\\replay_e2_stop_ema_sweep.py --stop-emas 20 25 30 40
"""
from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from zipfile import ZipFile

import pandas as pd


DATA_DIR = Path(__file__).resolve().parent.parent / "archive" / "binance_vision" / "s1"
OUT_PATH = Path(__file__).resolve().parent.parent / "archive" / "scratch_scripts" / "e2_stop_ema_sweep.json"
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
    exit_idx: int | None = None
    stop_price: float = 0.0
    tp_bb: float = 0.0
    tp_rr: float = 0.0

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
        merged = pd.concat(frames, ignore_index=True).sort_values("open_time").reset_index(drop=True)
        out[symbol] = merged
    return out


def add_indicators(df: pd.DataFrame, stop_ema: int) -> pd.DataFrame:
    out = df.copy()
    close = out["close"]
    out["e5"] = close.ewm(span=5, adjust=False).mean()
    out["e10"] = close.ewm(span=10, adjust=False).mean()
    out["e15"] = close.ewm(span=15, adjust=False).mean()
    out["estop"] = close.ewm(span=stop_ema, adjust=False).mean()
    mu = close.rolling(20).mean()
    sd = close.rolling(20).std(ddof=0)
    out["bb_u"] = mu + 2 * sd
    out["bb_l"] = mu - 2 * sd
    return out


def trade_net_pct(side: str, entry: float, exit_price: float) -> float:
    gross = (exit_price / entry - 1.0) if side == "LONG" else (entry / exit_price - 1.0)
    return gross - ROUND_TRIP_FEE_RATE


def replay_symbol(df: pd.DataFrame, symbol: str, stop_ema: int, tranches: int, rr: float, min_risk_pct: float) -> list[dict]:
    rows = add_indicators(df, stop_ema)
    pending: Pending | None = None
    pos: Position | None = None
    trades: list[dict] = []
    targets_cols = ["e5", "e10", "e15"][:tranches]
    warmup = max(30, stop_ema + 2)

    for i in range(warmup, len(rows)):
        row = rows.iloc[i]
        if pd.isna(row["bb_u"]) or pd.isna(row["estop"]):
            continue

        # exit first, same as live loop
        if pos is not None:
            exit_reason = None
            exit_price = None
            if pos.side == "LONG":
                if row["low"] <= pos.stop_price:
                    exit_reason = "STOP_EMA"
                    exit_price = pos.stop_price
                elif pos.tp_bb and row["high"] >= pos.tp_bb:
                    exit_reason = "BB"
                    exit_price = pos.tp_bb
                elif pos.tp_rr and row["high"] >= pos.tp_rr:
                    exit_reason = "RR"
                    exit_price = pos.tp_rr
            else:
                if row["high"] >= pos.stop_price:
                    exit_reason = "STOP_EMA"
                    exit_price = pos.stop_price
                elif pos.tp_bb and row["low"] <= pos.tp_bb:
                    exit_reason = "BB"
                    exit_price = pos.tp_bb
                elif pos.tp_rr and row["low"] <= pos.tp_rr:
                    exit_reason = "RR"
                    exit_price = pos.tp_rr
            if exit_reason:
                net_pct = trade_net_pct(pos.side, pos.entry, float(exit_price))
                trades.append({
                    "symbol": symbol,
                    "side": pos.side,
                    "legs": len(pos.legs),
                    "entry": pos.entry,
                    "exit": float(exit_price),
                    "entry_time": rows.iloc[pos.entry_idx]["open_time"].isoformat(),
                    "exit_time": row["open_time"].isoformat(),
                    "hold_sec": (i - pos.entry_idx) * 60.0,
                    "exit_reason": exit_reason,
                    "net_pct": net_pct * 100.0,
                })
                pos = None
                pending = None

        e5, e10, e15, estop = float(row["e5"]), float(row["e10"]), float(row["e15"]), float(row["estop"])
        is_long = e5 > e10 > e15 > estop
        is_short = e5 < e10 < e15 < estop
        if not (is_long or is_short):
            pending = None
            continue

        side = "LONG" if is_long else "SHORT"
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
        stop = estop
        risk = abs(entry - stop) / entry
        if risk * 100.0 < min_risk_pct:
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
            pos.tp_rr = (pos.entry * (1.0 + rr * new_risk) if is_long else pos.entry * (1.0 - rr * new_risk)) if rr and new_risk > 0 else 0.0
            pending.done = len(pending.legs)
            continue

        if pos is None:
            pos = Position(
                symbol=symbol,
                side=side,
                legs=list(pending.legs),
                entry_idx=i,
                stop_price=stop,
                tp_bb=tp_bb,
                tp_rr=(entry * (1.0 + rr * risk) if is_long else entry * (1.0 - rr * risk)) if rr else 0.0,
            )
            pending.done = len(pending.legs)

    return trades


def summarize(trades: list[dict]) -> dict:
    n = len(trades)
    wins = sum(1 for t in trades if t["net_pct"] > 0)
    net = sum(t["net_pct"] for t in trades)
    avg = net / n if n else 0.0
    hold_avg = sum(t["hold_sec"] for t in trades) / n if n else 0.0
    hold_med = pd.Series([t["hold_sec"] for t in trades]).median() if n else 0.0
    by_reason = Counter(t["exit_reason"] for t in trades)
    by_reason_avg = {}
    for key in by_reason:
        subset = [t["net_pct"] for t in trades if t["exit_reason"] == key]
        by_reason_avg[key] = sum(subset) / len(subset)
    by_side = {}
    for side in ("LONG", "SHORT"):
        subset = [t for t in trades if t["side"] == side]
        sn = len(subset)
        sw = sum(1 for t in subset if t["net_pct"] > 0)
        snet = sum(t["net_pct"] for t in subset)
        by_side[side] = {
            "n": sn,
            "win_rate": (sw / sn * 100.0) if sn else 0.0,
            "net_pct_sum": snet,
            "avg_pct": (snet / sn) if sn else 0.0,
        }
    return {
        "trades": n,
        "win_rate": (wins / n * 100.0) if n else 0.0,
        "net_pct_sum": net,
        "avg_pct": avg,
        "avg_hold_sec": hold_avg,
        "median_hold_sec": float(hold_med),
        "exit_reason_counts": dict(by_reason),
        "exit_reason_avg_pct": by_reason_avg,
        "by_side": by_side,
    }


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--stop-emas", nargs="+", type=int, default=[20, 25, 30, 40])
    p.add_argument("--tranches", type=int, default=3)
    p.add_argument("--rr", type=float, default=2.0)
    p.add_argument("--min-risk-pct", type=float, default=0.35)
    args = p.parse_args()

    symbol_frames = load_symbol_frames()
    if not symbol_frames:
        raise SystemExit("1분봉 zip 데이터가 없습니다.")

    print(f"대상 심볼 {len(symbol_frames)}개")
    results: dict[str, dict] = {}
    for stop_ema in args.stop_emas:
        trades: list[dict] = []
        for symbol, frame in symbol_frames.items():
            trades.extend(replay_symbol(frame, symbol, stop_ema, args.tranches, args.rr, args.min_risk_pct))
        stats = summarize(trades)
        results[str(stop_ema)] = stats
        reasons = ", ".join(
            f"{k} {v}건 avg{stats['exit_reason_avg_pct'][k]:+.4f}%"
            for k, v in sorted(stats["exit_reason_counts"].items())
        )
        print(
            f"EMA{stop_ema:02d}  trades={stats['trades']}  win={stats['win_rate']:.1f}%  "
            f"avg={stats['avg_pct']:+.4f}%  total={stats['net_pct_sum']:+.1f}%  "
            f"hold_med={stats['median_hold_sec']:.0f}s  hold_avg={stats['avg_hold_sec']:.1f}s"
        )
        print(f"  LONG {stats['by_side']['LONG']['n']}건 {stats['by_side']['LONG']['win_rate']:.1f}% {stats['by_side']['LONG']['avg_pct']:+.4f}%")
        print(f"  SHORT {stats['by_side']['SHORT']['n']}건 {stats['by_side']['SHORT']['win_rate']:.1f}% {stats['by_side']['SHORT']['avg_pct']:+.4f}%")
        print(f"  exits {reasons}")

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"상세 저장: {OUT_PATH}")


if __name__ == "__main__":
    main()
