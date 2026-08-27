"""SHORT PUMP_SIGNAL: (1) 파동 소진율 재검증(확대 표본), (2) 되돌림대기 필터 4단계 백테스트.
import io as _io_reconf

- 데이터 소스: 실 1분봉(공개 klines 엔드포인트, API 키 불필요) — logs/trade_ledger.jsonl의
  SHORT PUMP_SIGNAL 진입 545건 주변만 필요한 구간을 심볼별로 묶어서 조회(약 150콜 예상).
  각 호출 사이 0.4초 스로틀. 라이브 봇과 동일 IP이므로 반드시 호출수 최소화.
- 청산 로직은 offline_backtest.py의 exit_decision()을 그대로 재사용(커스텀 청산 로직 금지 원칙 준수).
- lookahead 없음: 되돌림 임계값은 신호캔들 종가 이후로 순차적으로(시간순) 도달 여부만 확인.
"""
from __future__ import annotations

import io
import json
import time
import sys
from dataclasses import replace
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))
from offline_backtest import Candle, Position, Settings, exit_decision  # noqa: E402

LEDGER = Path("logs/trade_ledger.jsonl")
BASE_URL = "https://fapi.binance.com/fapi/v1/klines"
THROTTLE_SEC = 0.4
BEFORE_MIN = 20
AFTER_MIN = 90
MAX_CHUNK_MIN = 1500
RETRACE_LEVELS = [0.05, 0.10, 0.15, 0.20]
MAX_WAIT_MIN = 60  # 되돌림 대기 최대 시간(초과 시 스킵 처리)


def load_trades() -> list[dict]:
    trades = []
    with io.open(LEDGER, encoding="utf-8", errors="replace") as f:
        for line in f:
            try:
                d = json.loads(line)
            except Exception:
                continue
            if d.get("origin") == "bot" and d.get("entry_reason") == "PUMP_SIGNAL" and d.get("side") == "SHORT":
                trades.append(d)
    return trades


def fetch_klines(symbol: str, start_ms: int, end_ms: int) -> list[list]:
    params = {"symbol": symbol, "interval": "1m", "startTime": start_ms, "endTime": end_ms, "limit": 1500}
    for attempt in range(4):
        try:
            resp = requests.get(BASE_URL, params=params, timeout=15)
        except Exception as e:
            print(f"  [warn] {symbol} network error: {e}, retry")
            time.sleep(2 * (attempt + 1))
            continue
        if resp.status_code == 200:
            return resp.json()
        if resp.status_code in (418, 429):
            wait = int(resp.headers.get("Retry-After", 30))
            print(f"  [BAN RISK] {symbol} status={resp.status_code}, sleeping {wait}s")
            time.sleep(wait + 1)
            continue
        print(f"  [warn] {symbol} status={resp.status_code}: {resp.text[:200]}")
        time.sleep(1)
    return []


def fetch_symbol_candles(symbol: str, entered_ats: list[float]) -> list[Candle]:
    ts_sorted = sorted(entered_ats)
    span_start = ts_sorted[0] - BEFORE_MIN * 60
    span_end = ts_sorted[-1] + AFTER_MIN * 60
    all_rows: list[list] = []
    cur = span_start
    while cur < span_end:
        chunk_end = min(cur + MAX_CHUNK_MIN * 60, span_end)
        rows = fetch_klines(symbol, int(cur * 1000), int(chunk_end * 1000))
        all_rows.extend(rows)
        time.sleep(THROTTLE_SEC)
        cur = chunk_end
    candles = []
    seen = set()
    for row in all_rows:
        ts = int(row[0])
        if ts in seen:
            continue
        seen.add(ts)
        try:
            c = Candle(ts, float(row[1]), float(row[2]), float(row[3]), float(row[4]), float(row[5]), float(row[7]), float(row[9]))
        except (IndexError, ValueError, TypeError):
            continue
        if c.low <= 0 or c.high < c.low:
            continue
        candles.append(c)
    candles.sort(key=lambda c: c.timestamp)
    return candles


def find_signal_index(candles: list[Candle], entered_at: float) -> int | None:
    """마지막으로 '완전히 닫힌' 캔들(신호캔들) 인덱스 = close_time <= entered_at."""
    entered_ms = entered_at * 1000
    best = None
    for i, c in enumerate(candles):
        if c.timestamp + 60000 <= entered_ms:
            best = i
        else:
            break
    return best


def settings_for(cfg: dict) -> Settings:
    return Settings(
        leverage=float(cfg.get("leverage", 4)),
        stop_roe_pct=float(cfg.get("stop_loss_pct", 3.5)),
        take_profit_roe_pct=float(cfg.get("short_take_profit_min", cfg.get("take_profit_min", 3.0))),
        hard_take_profit_roe_pct=float(cfg.get("take_profit_hard_cap", 7.0)),
        trailing_drawdown_roe_pct=float(cfg.get("trail_drawdown_pct", 1.0)),
    )


def simulate_exit(candles: list[Candle], start_idx: int, entry_price: float, settings: Settings) -> dict | None:
    """entry_price로 SHORT 진입, start_idx부터 순차적으로 exit_decision 적용."""
    pos = Position("X", "SHORT", candles[start_idx].timestamp, entry_price, 1.0, 1.0, 0.0, entry_price)
    for i in range(start_idx, len(candles)):
        c = candles[i]
        decision = exit_decision(pos, c, settings)
        if decision:
            price, reason = decision
            pnl_pct = (entry_price - price) / entry_price * 100
            roe_pct = pnl_pct * settings.leverage
            return {"exit_price": price, "reason": reason, "held_min": (c.timestamp - pos.entry_time) / 60000,
                    "roe_pct": roe_pct}
        favorable = c.low
        pos.peak_price = min(pos.peak_price, favorable)
        roe = (pos.peak_price / entry_price - 1) * -1 * settings.leverage * 100
        if roe >= settings.take_profit_roe_pct:
            pos.trailing_armed = True
    return None  # 윈도우 내 미해소(censored)


def simulate_retracement_entry(candles: list[Candle], sig_idx: int, threshold_pct: float, settings: Settings) -> dict:
    signal_low = candles[sig_idx].low
    retrace_price = signal_low * (1 + threshold_pct / 100)
    deadline_ts = candles[sig_idx].timestamp + 60000 + MAX_WAIT_MIN * 60000
    for i in range(sig_idx + 1, len(candles)):
        c = candles[i]
        if c.timestamp > deadline_ts:
            return {"status": "skip_timeout"}
        if c.high >= retrace_price:
            exit_res = simulate_exit(candles, i, retrace_price, settings)
            if exit_res is None:
                return {"status": "skip_unresolved_after_entry"}
            return {"status": "entered", "entry_price": retrace_price, "wait_min": (c.timestamp - candles[sig_idx].timestamp) / 60000, **exit_res}
    return {"status": "skip_no_data"}


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    trades = load_trades()
    print(f"loaded {len(trades)} SHORT PUMP_SIGNAL trades")
    by_symbol: dict[str, list[dict]] = {}
    for t in trades:
        by_symbol.setdefault(t["symbol"], []).append(t)

    results = {"baseline": [], **{lvl: [] for lvl in RETRACE_LEVELS}}
    consumed_frac_records = []  # (consumed_frac, win, roe_pct)

    n_symbols = len(by_symbol)
    for si, (symbol, tlist) in enumerate(sorted(by_symbol.items()), 1):
        print(f"[{si}/{n_symbols}] {symbol}: fetching klines for {len(tlist)} trades...")
        candles = fetch_symbol_candles(symbol, [t["entered_at"] for t in tlist])
        if not candles:
            print(f"  [skip] no candles for {symbol}")
            continue
        for t in tlist:
            entered_at = t["entered_at"]
            entry_price = t["entry_price"]
            cfg = t.get("config_snapshot") or {}
            settings = settings_for(cfg)
            sig_idx = find_signal_index(candles, entered_at)
            if sig_idx is None or sig_idx < 1:
                continue

            # --- baseline: 실제 기록된 entry_price로 즉시 진입 (다음 캔들부터 시뮬레이션) ---
            base = simulate_exit(candles, sig_idx + 1, entry_price, settings)
            if base is not None:
                results["baseline"].append({**base, "symbol": symbol})

            # --- consumed_frac 재구성 (1단계, 과거회고식 프록시 정의 — 본문 주석 참고) ---
            lookback = candles[max(0, sig_idx - 10):sig_idx]
            if lookback:
                swing_high = max(c.high for c in lookback)
                swing_low = min(min(c.low for c in lookback), candles[sig_idx].low)
                if swing_high > swing_low:
                    consumed_frac = (swing_high - candles[sig_idx].low) / (swing_high - swing_low)
                    if base is not None:
                        consumed_frac_records.append((consumed_frac, base["roe_pct"] > 0, base["roe_pct"]))

            # --- 2단계: 되돌림대기 4단계 ---
            for lvl in RETRACE_LEVELS:
                r = simulate_retracement_entry(candles, sig_idx, lvl, settings)
                r["symbol"] = symbol
                results[lvl].append(r)

    out_path = Path("scratch_short_retracement_results.json")
    out_path.write_text(json.dumps({"results": {str(k): v for k, v in results.items()},
                                     "consumed_frac_records": consumed_frac_records}, indent=2), encoding="utf-8")
    print(f"saved raw results to {out_path}")

    # ---- 리포트 ----
    print("\n=== 1단계: 파동 소진율(consumed_frac) 재검증 (표본 확대) ===")
    hi = [r for r in consumed_frac_records if r[0] >= 0.70]
    lo = [r for r in consumed_frac_records if r[0] < 0.70]
    def winrate(rows):
        return sum(1 for r in rows if r[1]) / len(rows) if rows else float("nan")
    def avg_roe(rows):
        return sum(r[2] for r in rows) / len(rows) if rows else float("nan")
    print(f"n={len(consumed_frac_records)} (baseline exit 확정된 것만)")
    print(f"  후반부(>=70%): n={len(hi)} 승률={winrate(hi)*100:.1f}% 평균ROE={avg_roe(hi):.2f}%")
    print(f"  초중반(<70%):  n={len(lo)} 승률={winrate(lo)*100:.1f}% 평균ROE={avg_roe(lo):.2f}%")

    print("\n=== 2단계: 되돌림대기 필터 비교 (baseline vs 4단계) ===")
    header = f"{'구간':>14} | {'n(진입)':>8} | {'스킵률':>8} | {'승률':>7} | {'PF':>6} | {'순ROE합':>9} | {'평균보유(분)':>10}"
    print(header)
    print("-" * len(header))

    def summarize(rows, total_signals):
        entered = [r for r in rows if r.get("status", "entered") == "entered" or "reason" in r]
        wins = [r for r in entered if r["roe_pct"] > 0]
        losses = [r for r in entered if r["roe_pct"] <= 0]
        gp = sum(r["roe_pct"] for r in wins)
        gl = abs(sum(r["roe_pct"] for r in losses))
        pf = gp / gl if gl > 0 else float("inf")
        wr = len(wins) / len(entered) * 100 if entered else float("nan")
        net = sum(r["roe_pct"] for r in entered)
        avg_hold = sum(r["held_min"] for r in entered) / len(entered) if entered else float("nan")
        skip_rate = (total_signals - len(entered)) / total_signals * 100 if total_signals else float("nan")
        return len(entered), skip_rate, wr, pf, net, avg_hold

    total_signals = len(results["baseline"])
    n, skip, wr, pf, net, hold = summarize(results["baseline"], len(results["baseline"]))
    print(f"{'baseline(즉시)':>14} | {n:>8} | {skip:>7.1f}% | {wr:>6.1f}% | {pf:>6.2f} | {net:>9.2f} | {hold:>10.1f}")
    for lvl in RETRACE_LEVELS:
        n, skip, wr, pf, net, hold = summarize(results[lvl], total_signals)
        print(f"{str(lvl)+'% 되돌림':>14} | {n:>8} | {skip:>7.1f}% | {wr:>6.1f}% | {pf:>6.2f} | {net:>9.2f} | {hold:>10.1f}")


if __name__ == "__main__":
    main()
