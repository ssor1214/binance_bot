"""공개 1분봉으로 120초/180초 micro-scalp 조기청산을 근사 리플레이한다.

trade_ledger의 엔트리/실제 종료 기록과 Binance Futures 공개 1분봉을 결합해,
"보유가 target_sec를 넘으면 그 시점의 1분봉 close로 청산했다"는 가정을 비교한다.

주의:
- 엔트리 체결가는 원장 entry_price를 사용한다(actual_fill_entry_price는 현재 비어 있음).
- 조기청산 가격은 tick이 아니라 1분봉 close 근사다.
- 따라서 완전한 체결 재현은 아니고, oracle보다는 현실적이지만 여전히 근사치다.

실행:
  python scripts/replay_micro_scalp_klines.py --days 3
  python scripts/replay_micro_scalp_klines.py --days 5 --targets 120 180
"""
from __future__ import annotations

import argparse
import json
import math
import time
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Iterable

import requests


ROOT = Path(__file__).resolve().parent.parent
LEDGER_PATH = ROOT / "logs" / "trade_ledger.jsonl"
CACHE_DIR = ROOT / "scratch_kline_cache" / "micro_scalp_replay"
BASE_URL = "https://fapi.binance.com/fapi/v1/klines"
KLINE_LIMIT = 1500
# REST 스로틀(초). 캐시 히트면 호출이 없으므로 실제 대기는 신규 구간에만 발생한다.
THROTTLE_SEC = 0.4


@dataclass
class VariantResult:
    name: str
    trades: int
    wins: int
    pnl: float
    max_loss_streak: int
    long_trades: int = 0
    long_wins: int = 0
    long_pnl: float = 0.0
    short_trades: int = 0
    short_wins: int = 0
    short_pnl: float = 0.0

    @property
    def win_rate(self) -> float:
        return (self.wins / self.trades * 100.0) if self.trades else 0.0

    @property
    def long_win_rate(self) -> float:
        return (self.long_wins / self.long_trades * 100.0) if self.long_trades else 0.0

    @property
    def short_win_rate(self) -> float:
        return (self.short_wins / self.short_trades * 100.0) if self.short_trades else 0.0


def load_rows(path: Path) -> list[dict]:
    rows: list[dict] = []
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


def ceil_minute_ms(ts_sec: float) -> int:
    return int(math.ceil(ts_sec / 60.0) * 60_000)


def floor_minute_ms(ts_sec: float) -> int:
    return int(math.floor(ts_sec / 60.0) * 60_000)


def cache_path(symbol: str, start_ms: int, end_ms: int) -> Path:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    return CACHE_DIR / f"{symbol}_{start_ms}_{end_ms}.json"


def fetch_klines(symbol: str, start_ms: int, end_ms: int) -> list[list]:
    path = cache_path(symbol, start_ms, end_ms)
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))

    out: list[list] = []
    cursor = start_ms
    session = requests.Session()
    headers = {"User-Agent": "micro-scalp-replay/1.0"}
    while cursor <= end_ms:
        params = {
            "symbol": symbol,
            "interval": "1m",
            "startTime": cursor,
            "endTime": end_ms,
            "limit": KLINE_LIMIT,
        }
        resp = session.get(BASE_URL, params=params, headers=headers, timeout=20)
        resp.raise_for_status()
        chunk = resp.json()
        if not chunk:
            break
        out.extend(chunk)
        last_open = int(chunk[-1][0])
        cursor = last_open + 60_000
        if len(chunk) < KLINE_LIMIT:
            break
        # [2026-08-17 추가] REST 스로틀. 이 저장소에서 klines/aggTrades 무스로틀 반복호출로
        # 실제 IP밴(약 10분 매매중단)이 발생한 이력이 있다 — 페이지마다 반드시 쉬어준다.
        time.sleep(THROTTLE_SEC)

    path.write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")
    return out


def build_symbol_kline_map(rows: list[dict], max_target_sec: int) -> dict[str, dict[int, float]]:
    grouped: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        grouped[str(row["symbol"])].append(row)

    out: dict[str, dict[int, float]] = {}
    for symbol, symbol_rows in grouped.items():
        start_ms = min(floor_minute_ms(float(r["entered_at"])) for r in symbol_rows)
        end_ms = max(ceil_minute_ms(float(r["entered_at"]) + max_target_sec + 60.0) for r in symbol_rows)
        klines = fetch_klines(symbol, start_ms, end_ms)
        time.sleep(THROTTLE_SEC)  # 심볼 간에도 쉬어준다(IP밴 방지)
        price_map: dict[int, float] = {}
        for k in klines:
            open_time = int(k[0])
            close_price = float(k[4])
            price_map[open_time] = close_price
        out[symbol] = price_map
    return out


def pnl_usdt(side: str, entry_price: float, exit_price: float, qty: float) -> float:
    if side == "LONG":
        return (exit_price - entry_price) * qty
    return (entry_price - exit_price) * qty


def replay_cut(rows: list[dict], price_maps: dict[str, dict[int, float]], target_sec: int, long_only: bool = False) -> list[dict]:
    out: list[dict] = []
    for row in rows:
        if long_only and row.get("side") != "LONG":
            continue
        held = float(row.get("held_seconds", 0.0))
        if held <= target_sec:
            out.append(row)
            continue

        symbol = str(row["symbol"])
        target_ms = ceil_minute_ms(float(row["entered_at"]) + target_sec)
        price = price_maps.get(symbol, {}).get(target_ms)
        if price is None:
            # 데이터가 없으면 보수적으로 원래 거래를 유지
            out.append(row)
            continue

        entry_price = float(row.get("entry_price", 0.0))
        quantity = abs(float(row.get("quantity", 0.0)))
        side = str(row.get("side", "LONG"))
        new_row = dict(row)
        new_row["estimated_pnl_usdt"] = pnl_usdt(side, entry_price, price, quantity)
        if entry_price > 0:
            if side == "LONG":
                new_row["estimated_pnl_pct"] = (price - entry_price) / entry_price * 100.0
            else:
                new_row["estimated_pnl_pct"] = (entry_price - price) / entry_price * 100.0
        new_row["held_seconds"] = float(target_sec)
        new_row["exit_reason"] = f"MICRO_SCALP_{target_sec}S"
        new_row["exit_price"] = price
        out.append(new_row)
    return out


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


def calc_stats(rows: Iterable[dict], name: str) -> VariantResult:
    wins = 0
    pnl = 0.0
    max_loss_streak = 0
    loss_streak = 0
    count = 0
    long_trades = 0
    long_wins = 0
    long_pnl = 0.0
    short_trades = 0
    short_wins = 0
    short_pnl = 0.0
    for row in rows:
        count += 1
        trade_pnl = float(row.get("estimated_pnl_usdt", 0.0))
        side = str(row.get("side", "LONG"))
        pnl += trade_pnl
        if side == "LONG":
            long_trades += 1
            long_pnl += trade_pnl
        else:
            short_trades += 1
            short_pnl += trade_pnl
        if trade_pnl > 0:
            wins += 1
            if side == "LONG":
                long_wins += 1
            else:
                short_wins += 1
            loss_streak = 0
        else:
            loss_streak += 1
            max_loss_streak = max(max_loss_streak, loss_streak)
    return VariantResult(
        name=name,
        trades=count,
        wins=wins,
        pnl=pnl,
        max_loss_streak=max_loss_streak,
        long_trades=long_trades,
        long_wins=long_wins,
        long_pnl=long_pnl,
        short_trades=short_trades,
        short_wins=short_wins,
        short_pnl=short_pnl,
    )


def print_table(title: str, results: list[VariantResult], hours: float) -> None:
    print(f"\n=== {title} ===")
    print(
        f"{'variant':28s} {'trades':>7s} {'/h':>6s} {'win%':>7s} {'pnl':>11s} "
        f"{'L':>5s} {'Lwin%':>7s} {'Lpnl':>11s} {'S':>5s} {'Swin%':>7s} {'Spnl':>11s} {'maxLS':>7s}"
    )
    for r in results:
        trades_per_hour = (r.trades / hours) if hours > 0 else 0.0
        print(
            f"{r.name:28s} {r.trades:7d} {trades_per_hour:6.2f} {r.win_rate:7.1f} {r.pnl:+11.4f} "
            f"{r.long_trades:5d} {r.long_win_rate:7.1f} {r.long_pnl:+11.4f} "
            f"{r.short_trades:5d} {r.short_win_rate:7.1f} {r.short_pnl:+11.4f} {r.max_loss_streak:7d}"
        )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--days", type=int, default=5)
    parser.add_argument("--since", type=str, default=None,
                        help='"YYYY-MM-DD HH:MM" 이후 진입만 집계(--days보다 우선). '
                             '설정 변경 전후를 나눠 비교할 때 사용')
    parser.add_argument("--targets", nargs="*", type=int, default=[120, 180])
    args = parser.parse_args()

    rows = load_rows(LEDGER_PATH)
    if not rows:
        print("trade_ledger 데이터가 없습니다.")
        return

    end_dt = rows[-1]["entered_dt"]
    if args.since:
        cutoff = datetime.strptime(args.since, "%Y-%m-%d %H:%M")
    else:
        cutoff = end_dt - timedelta(days=args.days)
    subset = [r for r in rows if r["entered_dt"] >= cutoff]
    max_target = max(args.targets) if args.targets else 180
    price_maps = build_symbol_kline_map(subset, max_target)

    results = [calc_stats(subset, "actual_2")]
    loss_block_rows = causal_same_symbol_loss_block(subset, block_minutes=30.0)
    fast_reentry_block_rows = causal_same_symbol_fast_reentry_block(subset, minutes=15.0)
    results.append(calc_stats(loss_block_rows, "loss_block_30m"))
    results.append(calc_stats(fast_reentry_block_rows, "fast_reentry_block_15m"))
    for target in args.targets:
        results.append(calc_stats(replay_cut(subset, price_maps, target_sec=target, long_only=False), f"cut_{target}s_all"))
        results.append(calc_stats(replay_cut(subset, price_maps, target_sec=target, long_only=True), f"cut_{target}s_long"))
        results.append(
            calc_stats(
                replay_cut(loss_block_rows, price_maps, target_sec=target, long_only=False),
                f"loss_block_30m+cut_{target}s",
            )
        )
        results.append(
            calc_stats(
                replay_cut(fast_reentry_block_rows, price_maps, target_sec=target, long_only=False),
                f"fast_reentry_15m+cut_{target}s",
            )
        )
    print_table(
        f"1분봉 근사 리플레이 ({cutoff:%Y-%m-%d %H:%M} ~ {end_dt:%Y-%m-%d %H:%M})",
        results,
        max((end_dt - cutoff).total_seconds() / 3600.0, 1e-9),
    )


if __name__ == "__main__":
    main()
