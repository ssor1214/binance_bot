"""[2026-08-14] 보유시간 문턱(N분) 강제청산 시뮬레이션.
- 대상: held_min >= 10분인 실거래(79건, baseline 총PnL -14.04)
- 방법: 각 거래의 진입시각~진입+20분 구간 1분봉을 fapi.binance.com 공개 klines 엔드포인트로
  (읽기전용, API키 불필요) 가져와서, N분 시점 캔들의 '종가'로 강제청산했다고 가정.
  N분 이후 캔들은 절대 보지 않음(lookahead 없음).
- IP밴 방지: 라이브 봇의 Exchange/API키 재사용 안 함. 심볼당 1회 호출, 호출간 0.25초 슬립.
"""
from __future__ import annotations

import json
import time
import requests
from pathlib import Path

LEDGER_PATH = Path("logs/trade_ledger.jsonl")
KLINES_URL = "https://fapi.binance.com/fapi/v1/klines"
FEE_RATE_ROUNDTRIP = 0.001  # ledger config_snapshot과 동일 가정


def load_target_trades(min_held_min: float = 10.0) -> list[dict]:
    rows = []
    with open(LEDGER_PATH, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except Exception:
                continue
            if r.get("origin", "bot") != "bot":
                continue
            if r.get("held_seconds") is None:
                continue
            held_min = r["held_seconds"] / 60.0
            if held_min < min_held_min:
                continue
            r["held_min"] = held_min
            rows.append(r)
    return rows


def fetch_klines(symbol: str, start_sec: float, end_sec: float):
    params = {
        "symbol": symbol,
        "interval": "1m",
        "startTime": int(start_sec * 1000),
        "endTime": int(end_sec * 1000),
        "limit": 40,
    }
    resp = requests.get(KLINES_URL, params=params, timeout=10)
    resp.raise_for_status()
    return resp.json()


def price_at_minute(raw_klines, entered_at: float, n_min: float):
    """entered_at 이후 n_min분 시점을 포함하는 캔들의 종가를 반환. n_min 이후 캔들은 사용 안 함."""
    target_ms = int((entered_at + n_min * 60) * 1000)
    candidate = None
    for k in raw_klines:
        open_ms = k[0]
        if open_ms <= target_ms:
            candidate = k
        else:
            break
    if candidate is None:
        return None
    return float(candidate[4])  # close


def simulate_pnl(side: str, entry_price: float, exit_price: float, leverage: int) -> float:
    if side == "LONG":
        raw_pct = (exit_price / entry_price - 1)
    else:
        raw_pct = (entry_price / exit_price - 1)
    pnl_pct = raw_pct * leverage - FEE_RATE_ROUNDTRIP  # 수수료 대략 반영(왕복 0.1%)
    return pnl_pct


def main():
    thresholds_to_test = [10, 15]
    trades = load_target_trades(min_held_min=10.0)
    print(f"대상 거래(held>=10min): {len(trades)}건\n")

    cache = {}  # (symbol, entered_at) -> raw klines
    results = {n: [] for n in thresholds_to_test}
    fetch_fail = 0

    for i, t in enumerate(trades):
        symbol = t["symbol"]
        entered_at = t["entered_at"]
        key = (symbol, entered_at)
        if key not in cache:
            try:
                raw = fetch_klines(symbol, entered_at - 60, entered_at + 22 * 60)
                cache[key] = raw
            except Exception as e:
                cache[key] = None
                fetch_fail += 1
            time.sleep(0.25)
        raw = cache[key]
        if not raw:
            continue

        for n in thresholds_to_test:
            if t["held_min"] <= n:
                # 실제로 N분 전에 이미 청산됐으면 시뮬레이션 대상 아님(실제 결과 그대로 사용)
                results[n].append({
                    "symbol": symbol, "mode": "actual(already closed before N)",
                    "sim_pnl_usdt": t.get("estimated_pnl_usdt") or 0,
                    "actual_pnl_usdt": t.get("estimated_pnl_usdt") or 0,
                })
                continue
            px = price_at_minute(raw, entered_at, n)
            if px is None:
                continue
            entry_price = t["entry_price"]
            leverage = t.get("leverage") or 4
            sim_pct = simulate_pnl(t["side"], entry_price, px, leverage)
            # notional 추정: quantity * entry_price / leverage 는 증거금. usdt pnl = margin * pnl_pct?
            # ledger의 estimated_pnl_usdt와 estimated_pnl_pct 관계로 margin 역산
            actual_pnl_pct = (t.get("estimated_pnl_pct") or 0) / 100.0
            actual_pnl_usdt = t.get("estimated_pnl_usdt") or 0
            if actual_pnl_pct != 0:
                margin = actual_pnl_usdt / actual_pnl_pct
            else:
                margin = t["quantity"] * entry_price / leverage
            sim_pnl_usdt = margin * sim_pct
            results[n].append({
                "symbol": symbol, "mode": "sim_timeout",
                "sim_pnl_usdt": sim_pnl_usdt,
                "actual_pnl_usdt": actual_pnl_usdt,
                "sim_price": px, "actual_exit_reason": t.get("exit_reason"),
                "held_min_actual": t["held_min"],
            })

    print(f"klines 조회 실패: {fetch_fail}건\n")

    for n in thresholds_to_test:
        rs = results[n]
        if not rs:
            continue
        baseline_total = sum(r["actual_pnl_usdt"] for r in rs)
        sim_total = sum(r["sim_pnl_usdt"] for r in rs)
        sim_wins = len([r for r in rs if r["sim_pnl_usdt"] > 0])
        actual_wins = len([r for r in rs if r["actual_pnl_usdt"] > 0])
        n_simulated = len([r for r in rs if r["mode"] == "sim_timeout"])
        print(f"=== N={n}분 강제청산 시뮬레이션 (대상 {len(rs)}건, 실제타임아웃적용 {n_simulated}건) ===")
        print(f"  baseline(실제) 총PnL: {baseline_total:+.2f}  승수 {actual_wins}/{len(rs)} ({actual_wins/len(rs)*100:.1f}%)")
        print(f"  N분강제청산 총PnL:   {sim_total:+.2f}  승수 {sim_wins}/{len(rs)} ({sim_wins/len(rs)*100:.1f}%)")
        print(f"  차이(sim - baseline): {sim_total - baseline_total:+.2f}\n")

    # 상세 저장
    out_path = Path("archive/scratch_scripts/holding_time_timeout_sim_detail.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump({str(n): results[n] for n in thresholds_to_test}, f, ensure_ascii=False, indent=2)
    print(f"상세결과 저장: {out_path}")


if __name__ == "__main__":
    main()
