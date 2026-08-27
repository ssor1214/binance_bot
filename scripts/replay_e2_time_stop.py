"""e2 원장 전용 시간 기반 강제청산 리플레이.

목적:
- logs/scalp_bot_e2_ledger.jsonl 실주문 원장을 읽는다.
- 지정한 초 문턱(기본 180/240/300초)보다 오래 간 거래에 대해
  "그 시점이 포함된 1분봉 종가로 강제청산했다"는 가정으로 재계산한다.
- 실제 결과와 시뮬레이션 결과를 나란히 비교한다.

주의:
- 분봉 종가 기반 근사다. 실제 체결가와 다를 수 있다.
- n초 시점 '이후' 캔들은 보지 않는다.
- 원장에 있는 거래만 대상으로 하므로, 열린 포지션은 반영하지 않는다.

실행:
  .venv312\\Scripts\\python.exe scripts\\replay_e2_time_stop.py
  .venv312\\Scripts\\python.exe scripts\\replay_e2_time_stop.py --thresholds 180 240 300
  .venv312\\Scripts\\python.exe scripts\\replay_e2_time_stop.py --since "2026-08-20 18:00"
"""
from __future__ import annotations

import argparse
import json
import time
from collections import defaultdict
from datetime import datetime
from pathlib import Path

import requests


LEDGER_PATH = Path(__file__).resolve().parent.parent / "logs" / "scalp_bot_e2_ledger.jsonl"
OUT_PATH = Path(__file__).resolve().parent.parent / "archive" / "scratch_scripts" / "e2_time_stop_replay_detail.json"
KLINES_URL = "https://fapi.binance.com/fapi/v1/klines"


def load_rows(live_only: bool = True, since: str | None = None) -> list[dict]:
    rows: list[dict] = []
    with LEDGER_PATH.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            if live_only and row.get("dry_run"):
                continue
            if row.get("origin") != "scalp_bot_e2":
                continue
            rows.append(row)
    if since:
        cutoff = datetime.strptime(since, "%Y-%m-%d %H:%M").timestamp()
        rows = [r for r in rows if float(r["entered_at"]) >= cutoff]
    rows.sort(key=lambda r: float(r["entered_at"]))
    return rows


def fetch_klines(symbol: str, start_sec: float, end_sec: float) -> list[list]:
    params = {
        "symbol": symbol,
        "interval": "1m",
        "startTime": int(start_sec * 1000),
        "endTime": int(end_sec * 1000),
        "limit": 20,
    }
    resp = requests.get(KLINES_URL, params=params, timeout=15)
    resp.raise_for_status()
    return resp.json()


def price_at_second(raw_klines: list[list], target_sec: float) -> float | None:
    """target_sec 시점을 포함하는 마지막 1분봉의 종가를 반환."""
    target_ms = int(target_sec * 1000)
    candidate = None
    for k in raw_klines:
        open_ms = int(k[0])
        if open_ms <= target_ms:
            candidate = k
        else:
            break
    return float(candidate[4]) if candidate else None


def simulated_realized(side: str, entry_price: float, exit_price: float, qty: float) -> float:
    if side == "LONG":
        return (exit_price - entry_price) * qty
    return (entry_price - exit_price) * qty


def replay(rows: list[dict], thresholds: list[int]) -> dict[int, list[dict]]:
    cache: dict[tuple[str, int], list[list] | None] = {}
    results: dict[int, list[dict]] = {th: [] for th in thresholds}
    max_th = max(thresholds)

    for row in rows:
        symbol = row["symbol"]
        entered_at = float(row["entered_at"])
        key = (symbol, int(entered_at))
        if key not in cache:
            try:
                cache[key] = fetch_klines(symbol, entered_at - 60, entered_at + max_th + 120)
            except Exception:
                cache[key] = None
            time.sleep(0.2)
        raw = cache[key]
        hold_sec = float(row["exited_at"]) - entered_at
        actual_net = float(row.get("real_net", 0) or 0)
        actual_reason = row.get("exit_reason")
        qty = float(row["quantity"])
        entry_price = float(row["entry_price"])
        side = row["side"]
        commission = float(row.get("real_commission", 0) or 0)

        for th in thresholds:
            detail = {
                "symbol": symbol,
                "side": side,
                "entered_at": entered_at,
                "held_sec_actual": hold_sec,
                "legs": row.get("legs"),
                "actual_exit_reason": actual_reason,
                "actual_net": actual_net,
                "threshold_sec": th,
            }
            if hold_sec <= th or not raw:
                detail["mode"] = "actual"
                detail["sim_net"] = actual_net
                detail["delta_vs_actual"] = 0.0
                results[th].append(detail)
                continue

            sim_exit_price = price_at_second(raw, entered_at + th)
            if sim_exit_price is None:
                detail["mode"] = "missing_kline"
                detail["sim_net"] = actual_net
                detail["delta_vs_actual"] = 0.0
                results[th].append(detail)
                continue

            sim_realized = simulated_realized(side, entry_price, sim_exit_price, qty)
            # 보수적으로 현재 원장에 기록된 commission을 그대로 쓴다.
            sim_net = sim_realized - commission
            detail["mode"] = "sim_timeout"
            detail["sim_exit_price"] = sim_exit_price
            detail["sim_net"] = sim_net
            detail["delta_vs_actual"] = sim_net - actual_net
            results[th].append(detail)
    return results


def print_summary(results: dict[int, list[dict]]) -> None:
    for th in sorted(results):
        rows = results[th]
        if not rows:
            continue
        base = sum(r["actual_net"] for r in rows)
        sim = sum(r["sim_net"] for r in rows)
        sim_count = sum(1 for r in rows if r["mode"] == "sim_timeout")
        base_wins = sum(1 for r in rows if r["actual_net"] > 0)
        sim_wins = sum(1 for r in rows if r["sim_net"] > 0)
        print(f"=== {th}초 강제청산 ===")
        print(f"대상 {len(rows)}건, 실제 타임스톱 적용 {sim_count}건")
        print(f"실제 총순익 {base:+.4f} / 승률 {base_wins / len(rows) * 100:.1f}%")
        print(f"시뮬 총순익 {sim:+.4f} / 승률 {sim_wins / len(rows) * 100:.1f}%")
        print(f"차이(sim-actual) {sim - base:+.4f}")

        grouped: dict[tuple[object, object], list[dict]] = defaultdict(list)
        for row in rows:
            grouped[(row["actual_exit_reason"], row["legs"])].append(row)
        print("청산사유 x 차수")
        for key in sorted(grouped):
            subset = grouped[key]
            delta = sum(r["delta_vs_actual"] for r in subset)
            print(f"  {key[0]} / {key[1]}차: {len(subset)}건 delta={delta:+.4f}")
        print()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--thresholds", nargs="+", type=int, default=[180, 240, 300])
    parser.add_argument("--since", type=str, default=None, help='이 시각 이후 거래만. 예: "2026-08-20 18:00"')
    args = parser.parse_args()

    rows = load_rows(live_only=True, since=args.since)
    print(f"대상 거래 {len(rows)}건")
    if not rows:
        return
    results = replay(rows, args.thresholds)
    print_summary(results)
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps({str(k): v for k, v in results.items()}, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"상세 저장: {OUT_PATH}")


if __name__ == "__main__":
    main()
