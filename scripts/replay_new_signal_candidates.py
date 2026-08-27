"""새 스캘핑 신호 후보 1차 비교.

목적:
- e2 파라미터 튜닝이 아니라, 다른 시장 현상에 수수료 전 우위가 있는지 빠르게 본다.
- 로컬 archive/binance_vision/s1 1분봉만 사용한다. REST 호출 없음.

공통 체결 모델:
- 신호봉 종가 진입
- 1R 손절, 2R 익절, 30분 타임아웃
- 같은 봉에서 SL/TP가 모두 닿으면 보수적으로 SL 우선
- 왕복 수수료 0.1002% 차감
"""
from __future__ import annotations

import json
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from zipfile import ZipFile

import pandas as pd


ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "archive" / "binance_vision" / "s1"
OUT_PATH = ROOT / "archive" / "scratch_scripts" / "new_signal_candidates.json"
ROUND_TRIP_FEE_RATE = 0.001002
MAX_HOLD_BARS = 30
RR = 2.0
MIN_RISK_PCT = 0.15
MAX_RISK_PCT = 1.80


@dataclass
class Signal:
    name: str
    symbol: str
    side: str
    idx: int
    entry: float
    stop: float
    reason: str


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
        for col in ("open", "high", "low", "close", "volume"):
            if col in df:
                df[col] = df[col].astype(float)
        df["open_time"] = pd.to_datetime(df["open_time"], unit="ms", utc=True)
        keep = ["open_time", "open", "high", "low", "close"]
        if "volume" in df:
            keep.append("volume")
        frames.append(df[keep])
    out = pd.concat(frames, ignore_index=True).sort_values("open_time").reset_index(drop=True)
    if "volume" not in out:
        out["volume"] = 0.0
    return out


def add_features(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    c = out["close"]
    h = out["high"]
    l = out["low"]
    out["ema20"] = c.ewm(span=20, adjust=False).mean()
    out["ema50"] = c.ewm(span=50, adjust=False).mean()
    out["atr14"] = (h - l).rolling(14).mean()
    out["vol_ma20"] = out["volume"].rolling(20).mean()
    out["hi20_prev"] = h.shift(1).rolling(20).max()
    out["lo20_prev"] = l.shift(1).rolling(20).min()
    out["hi10_prev"] = h.shift(1).rolling(10).max()
    out["lo10_prev"] = l.shift(1).rolling(10).min()
    return out


def risk_ok(entry: float, stop: float) -> bool:
    if entry <= 0 or stop <= 0:
        return False
    risk_pct = abs(entry - stop) / entry * 100.0
    return MIN_RISK_PCT <= risk_pct <= MAX_RISK_PCT


def make_signal(name: str, symbol: str, side: str, i: int, entry: float,
                stop: float, reason: str) -> Signal | None:
    if side == "LONG" and stop >= entry:
        return None
    if side == "SHORT" and stop <= entry:
        return None
    if not risk_ok(entry, stop):
        return None
    return Signal(name, symbol, side, i, entry, stop, reason)


def signals_for_row(rows: pd.DataFrame, symbol: str, i: int) -> list[Signal]:
    row = rows.iloc[i]
    prev = rows.iloc[i - 1]
    out: list[Signal] = []
    entry = float(row["close"])
    atr = float(row["atr14"] or 0.0)
    vol_ma = float(row["vol_ma20"] or 0.0)
    vol_ok = vol_ma <= 0 or float(row["volume"]) >= vol_ma * 1.2

    hi20 = float(row["hi20_prev"])
    lo20 = float(row["lo20_prev"])
    prev_hi20 = float(prev["hi20_prev"])
    prev_lo20 = float(prev["lo20_prev"])
    hi10 = float(row["hi10_prev"])
    lo10 = float(row["lo10_prev"])

    # 1) 변동성 돌파 후 되돌림: 직전 봉이 20봉 고저를 돌파했고, 이번 봉이 그 선을 재확인 후 회복.
    if float(prev["close"]) > prev_hi20 and float(row["low"]) <= prev_hi20 < entry:
        stop = min(float(row["low"]), prev_hi20 - atr * 0.25)
        sig = make_signal("breakout_retest", symbol, "LONG", i, entry, stop, "prev_high_retest")
        if sig:
            out.append(sig)
    if float(prev["close"]) < prev_lo20 and float(row["high"]) >= prev_lo20 > entry:
        stop = max(float(row["high"]), prev_lo20 + atr * 0.25)
        sig = make_signal("breakout_retest", symbol, "SHORT", i, entry, stop, "prev_low_retest")
        if sig:
            out.append(sig)

    # 2) 유동성 회수 후 반전: 20봉 고저를 찌르고 다시 범위 안으로 닫힘.
    if float(row["low"]) < lo20 < entry:
        stop = float(row["low"])
        sig = make_signal("liquidity_sweep_reversal", symbol, "LONG", i, entry, stop, "low_sweep_reclaim")
        if sig:
            out.append(sig)
    if float(row["high"]) > hi20 > entry:
        stop = float(row["high"])
        sig = make_signal("liquidity_sweep_reversal", symbol, "SHORT", i, entry, stop, "high_sweep_reject")
        if sig:
            out.append(sig)

    # 3) 강한 추세 지속형: EMA20/50 정렬 + 10봉 고저 돌파 + 평균 이상 거래량.
    if vol_ok and float(row["ema20"]) > float(row["ema50"]) and entry > hi10:
        stop = max(float(row["ema20"]), entry - atr * 1.2)
        sig = make_signal("trend_continuation", symbol, "LONG", i, entry, stop, "ema_trend_high_break")
        if sig:
            out.append(sig)
    if vol_ok and float(row["ema20"]) < float(row["ema50"]) and entry < lo10:
        stop = min(float(row["ema20"]), entry + atr * 1.2)
        sig = make_signal("trend_continuation", symbol, "SHORT", i, entry, stop, "ema_trend_low_break")
        if sig:
            out.append(sig)
    return out


def exit_trade(rows: pd.DataFrame, sig: Signal) -> dict:
    risk = abs(sig.entry - sig.stop)
    tp = sig.entry + risk * RR if sig.side == "LONG" else sig.entry - risk * RR
    end = min(len(rows) - 1, sig.idx + MAX_HOLD_BARS)
    exit_price = float(rows.iloc[end]["close"])
    exit_reason = "TIMEOUT"
    exit_idx = end
    for j in range(sig.idx + 1, end + 1):
        row = rows.iloc[j]
        if sig.side == "LONG":
            if float(row["low"]) <= sig.stop:
                exit_price = sig.stop
                exit_reason = "SL"
                exit_idx = j
                break
            if float(row["high"]) >= tp:
                exit_price = tp
                exit_reason = "TP2R"
                exit_idx = j
                break
        else:
            if float(row["high"]) >= sig.stop:
                exit_price = sig.stop
                exit_reason = "SL"
                exit_idx = j
                break
            if float(row["low"]) <= tp:
                exit_price = tp
                exit_reason = "TP2R"
                exit_idx = j
                break

    gross = (exit_price / sig.entry - 1.0) if sig.side == "LONG" else (sig.entry / exit_price - 1.0)
    return {
        "strategy": sig.name,
        "symbol": sig.symbol,
        "side": sig.side,
        "entry_time": rows.iloc[sig.idx]["open_time"].isoformat(),
        "exit_time": rows.iloc[exit_idx]["open_time"].isoformat(),
        "entry": sig.entry,
        "stop": sig.stop,
        "exit": exit_price,
        "exit_reason": exit_reason,
        "gross_pct": gross * 100.0,
        "net_pct": (gross - ROUND_TRIP_FEE_RATE) * 100.0,
        "hold_sec": (exit_idx - sig.idx) * 60.0,
        "reason": sig.reason,
    }


def replay_symbol(symbol: str, df: pd.DataFrame) -> list[dict]:
    rows = add_features(df)
    trades: list[dict] = []
    next_free: dict[str, int] = defaultdict(int)
    for i in range(60, len(rows) - 2):
        row = rows.iloc[i]
        if pd.isna(row["hi20_prev"]) or pd.isna(row["atr14"]):
            continue
        for sig in signals_for_row(rows, symbol, i):
            if i < next_free[sig.name]:
                continue
            tr = exit_trade(rows, sig)
            trades.append(tr)
            next_free[sig.name] = i + 3
    return trades


def summarize(trades: list[dict]) -> dict:
    n = len(trades)
    wins = sum(1 for t in trades if t["net_pct"] > 0)
    gross_sum = sum(t["gross_pct"] for t in trades)
    net_sum = sum(t["net_pct"] for t in trades)
    reasons = Counter(t["exit_reason"] for t in trades)
    sides = {}
    for side in ("LONG", "SHORT"):
        ss = [t for t in trades if t["side"] == side]
        sn = len(ss)
        sides[side] = {
            "trades": sn,
            "win_rate": sum(1 for t in ss if t["net_pct"] > 0) / sn * 100.0 if sn else 0.0,
            "gross_avg_pct": sum(t["gross_pct"] for t in ss) / sn if sn else 0.0,
            "net_avg_pct": sum(t["net_pct"] for t in ss) / sn if sn else 0.0,
        }
    return {
        "trades": n,
        "win_rate": wins / n * 100.0 if n else 0.0,
        "gross_avg_pct": gross_sum / n if n else 0.0,
        "net_avg_pct": net_sum / n if n else 0.0,
        "net_sum_pct": net_sum,
        "median_hold_sec": float(pd.Series([t["hold_sec"] for t in trades]).median()) if n else 0.0,
        "exit_reason_counts": dict(reasons),
        "by_side": sides,
    }


def main() -> None:
    all_trades: list[dict] = []
    paths = iter_symbol_paths()
    print(f"대상 심볼 {len(paths)}개")
    for idx, (symbol, spaths) in enumerate(paths, 1):
        df = load_symbol_frame(spaths)
        all_trades.extend(replay_symbol(symbol, df))
        if idx % 20 == 0:
            print(f"  진행 {idx}/{len(paths)}")

    by_strategy: dict[str, list[dict]] = defaultdict(list)
    for tr in all_trades:
        by_strategy[tr["strategy"]].append(tr)

    results = {
        "settings": {
            "round_trip_fee_rate": ROUND_TRIP_FEE_RATE,
            "rr": RR,
            "max_hold_bars": MAX_HOLD_BARS,
            "min_risk_pct": MIN_RISK_PCT,
            "max_risk_pct": MAX_RISK_PCT,
            "data_dir": str(DATA_DIR),
        },
        "summary": {},
        "trades_sample": all_trades[:200],
    }
    print("\nstrategy,trades,win,gross_avg,net_avg,net_sum,hold_med,exits")
    for name in ("breakout_retest", "liquidity_sweep_reversal", "trend_continuation"):
        stats = summarize(by_strategy[name])
        results["summary"][name] = stats
        exits = " ".join(f"{k}:{v}" for k, v in sorted(stats["exit_reason_counts"].items()))
        print(
            f"{name},{stats['trades']},{stats['win_rate']:.1f},"
            f"{stats['gross_avg_pct']:+.4f},{stats['net_avg_pct']:+.4f},"
            f"{stats['net_sum_pct']:+.1f},{stats['median_hold_sec']:.0f},{exits}"
        )
        for side in ("LONG", "SHORT"):
            ss = stats["by_side"][side]
            print(
                f"  {side}: {ss['trades']}건 win{ss['win_rate']:.1f}% "
                f"gross{ss['gross_avg_pct']:+.4f}% net{ss['net_avg_pct']:+.4f}%"
            )

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n상세 저장: {OUT_PATH}")


if __name__ == "__main__":
    main()
