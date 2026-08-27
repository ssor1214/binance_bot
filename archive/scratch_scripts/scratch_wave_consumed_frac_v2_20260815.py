"""파동 소진율(consumed_frac) vs 승률/ROE 상관관계 재검증 (2026-08-15, v2, LONG+SHORT 통합).

## 배경
- 8/13 세션: SHORT PUMP_SIGNAL 150건 표본 -> "후반부(파동 많이 소진) 진입일수록 승률 나쁨"
  (후반부 37.3% vs 초중반 44.4%). 이때 사용한 정확한 consumed_frac 계산 코드는 세션 종료 후
  커밋되지 않아(gitignore 대상 scratch 파일) *유실*됨. git log --all --diff-filter=A로도
  찾지 못했고, 메모리(.claude/projects/.../memory/*.md)에도 원 수식이 남아있지 않음.
  -> 이번 세션에서 archive/scratch_scripts/scratch_short_retracement_test.py 를 확인했으나,
     그 스크립트 자체가 "1단계: 파동 소진율 재구성(과거회고식 프록시 정의)"라고 명시하며
     8/13 원 정의를 재현한 것이 아니라 별도로 근사한 것임을 스스로 밝히고 있음(주석 189행).
     즉 8/13 원 정의는 최종적으로 복구 불가 -> 이 스크립트에서 **새 정의를 확정**하고
     이 주석에 영구 기록한다. 다음 세션은 이 정의를 그대로 재사용할 것.

## consumed_frac 정의 (이번 세션 확정, v2)
신호캔들 인덱스를 sig_idx, 신호캔들 직전 L=10개 캔들 + 신호캔들 자신을 "국소 파동 윈도우"로 본다.
  window = candles[sig_idx-10 : sig_idx+1]   (신호캔들 포함, 총 최대 11개 캔들)
  swing_high = max(c.high for c in window)
  swing_low  = min(c.low  for c in window)
  swing_range = swing_high - swing_low
  close_sig = candles[sig_idx].close  (신호캔들 "종가" — lookahead 없음, 신호캔들까지의 정보만 사용)

방향에 따라 "그 국소 파동에서 신호캔실이 얼마나 늦게(소진된 뒤) 진입하는지"를 다음과 같이 정의:
  - SHORT (상승 펌프를 페이드하는 진입): 되돌림이 이미 얼마나 진행됐는지를 측정.
      consumed_frac = (swing_high - close_sig) / swing_range
      0 = 고점 근처에서 바로 진입(되돌림 없음, "이른" 진입)
      1 = 고점에서 저점까지 완전히 되돌린 뒤 진입("늦은" 추격 숏)
  - LONG (상승 펌프에 편승하는 진입): 그 상승폭을 얼마나 이미 다 따라잡은 뒤 진입하는지 측정.
      consumed_frac = (close_sig - swing_low) / swing_range
      0 = 저점 근처에서 바로 진입("이른" 진입)
      1 = 고점 근처까지 이미 오른 뒤 진입("늦은" 추격 롱)

두 정의 모두 "얼마나 그 국소 파동을 다 써버린(소진한) 뒤에 진입하는가"를 0~1로 통일해서 나타내며,
값이 클수록 "추격 진입"(파동이 이미 많이 진행된 뒤 뒤늦게 올라탐)을 의미한다.
swing_range <= 0(캔들이 다 동일가 등 이상치)인 경우는 표본에서 제외.

## 승패 판정
lookahead-free 재시뮬레이션(offline_backtest.exit_decision) 대신, **실제 봇이 체결/청산한 결과**
(logs/trade_ledger.jsonl의 estimated_pnl_pct)를 그대로 사용한다. 이는 실거래 결과이므로 그 자체로
lookahead bias가 없다(체결/청산 모두 과거 실제 이벤트). 이 방식이 이번 분석의 핵심 방법론 변경점:
"소진율과 봇이 실제로 낸 손익의 상관관계"를 직접 보는 것이 목적이므로 별도 청산 로직 재시뮬레이션은
불필요하고 오히려 실제 봇 동작과 괴리를 만들 수 있어 배제한다.

## 데이터
- logs/trade_ledger.jsonl, origin=bot, entry_reason=PUMP_SIGNAL, entered_at >= 2026-08-13 00:00 UTC
- LONG/SHORT 모두 포함, 방향별로 분리 집계
- 1분봉은 바이낸스 공개 klines 엔드포인트(API 키 불필요)에서 심볼별로 필요한 구간만 묶어서 조회.
  호출 간 0.3초 스로틀. 418/429 발생 시 장시간 대기(IP밴 방지, backtest-ip-ban-incident 참고).
"""
from __future__ import annotations

import io
import json
import math
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import requests

LEDGER = Path("logs/trade_ledger.jsonl")
BASE_URL = "https://fapi.binance.com/fapi/v1/klines"
THROTTLE_SEC = 0.3
BEFORE_MIN = 20   # 신호캔들 앞쪽 여유(윈도우 L=10 커버 + 여유)
LOOKBACK_L = 10
CUTOFF_TS = 1786579200.0  # 2026-08-13 00:00:00 UTC


@dataclass
class Candle:
    ts: int
    open: float
    high: float
    low: float
    close: float


def load_trades() -> list[dict]:
    trades = []
    with io.open(LEDGER, encoding="utf-8", errors="replace") as f:
        for line in f:
            try:
                d = json.loads(line)
            except Exception:
                continue
            if (
                d.get("origin") == "bot"
                and d.get("entry_reason") == "PUMP_SIGNAL"
                and d.get("side") in ("LONG", "SHORT")
                and d.get("entered_at", 0) >= CUTOFF_TS
            ):
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
    span_end = ts_sorted[-1] + 60
    all_rows: list[list] = []
    cur = span_start
    while cur < span_end:
        chunk_end = min(cur + 1500 * 60, span_end)
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
            c = Candle(ts, float(row[1]), float(row[2]), float(row[3]), float(row[4]))
        except (IndexError, ValueError, TypeError):
            continue
        if c.low <= 0 or c.high < c.low:
            continue
        candles.append(c)
    candles.sort(key=lambda c: c.ts)
    return candles


def find_signal_index(candles: list[Candle], entered_at: float) -> int | None:
    """마지막으로 '완전히 닫힌' 캔들(신호캔들) 인덱스 = close_time <= entered_at."""
    entered_ms = entered_at * 1000
    best = None
    for i, c in enumerate(candles):
        if c.ts + 60000 <= entered_ms:
            best = i
        else:
            break
    return best


def compute_consumed_frac(candles: list[Candle], sig_idx: int, side: str) -> float | None:
    lo = max(0, sig_idx - LOOKBACK_L)
    window = candles[lo:sig_idx + 1]
    if len(window) < 3:
        return None
    swing_high = max(c.high for c in window)
    swing_low = min(c.low for c in window)
    rng = swing_high - swing_low
    if rng <= 0:
        return None
    close_sig = candles[sig_idx].close
    if side == "SHORT":
        frac = (swing_high - close_sig) / rng
    else:
        frac = (close_sig - swing_low) / rng
    return max(0.0, min(1.0, frac))


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    trades = load_trades()
    print(f"loaded {len(trades)} PUMP_SIGNAL trades (LONG+SHORT, since 8/13 00:00 UTC)")
    by_symbol: dict[str, list[dict]] = {}
    for t in trades:
        by_symbol.setdefault(t["symbol"], []).append(t)
    print(f"{len(by_symbol)} distinct symbols")

    records = []  # dict(side, consumed_frac, win, roe_pct, symbol, entered_at)
    n_symbols = len(by_symbol)
    for si, (symbol, tlist) in enumerate(sorted(by_symbol.items()), 1):
        print(f"[{si}/{n_symbols}] {symbol}: fetching klines for {len(tlist)} trades...")
        candles = fetch_symbol_candles(symbol, [t["entered_at"] for t in tlist])
        if not candles:
            print(f"  [skip] no candles for {symbol}")
            continue
        for t in tlist:
            sig_idx = find_signal_index(candles, t["entered_at"])
            if sig_idx is None or sig_idx < 3:
                continue
            frac = compute_consumed_frac(candles, sig_idx, t["side"])
            if frac is None:
                continue
            roe = t.get("estimated_pnl_pct")
            if roe is None:
                continue
            records.append({
                "side": t["side"],
                "symbol": symbol,
                "consumed_frac": frac,
                "win": roe > 0,
                "roe_pct": roe,
                "entered_at": t["entered_at"],
            })

    out_path = Path("scratch_wave_consumed_frac_v2_records.json")
    out_path.write_text(json.dumps(records, indent=2), encoding="utf-8")
    print(f"\nsaved {len(records)} records to {out_path}")

    def winrate(rows):
        return sum(1 for r in rows if r["win"]) / len(rows) if rows else float("nan")

    def avg_roe(rows):
        return sum(r["roe_pct"] for r in rows) / len(rows) if rows else float("nan")

    def z_test_two_proportions(rows_a, rows_b):
        n1, n2 = len(rows_a), len(rows_b)
        if n1 == 0 or n2 == 0:
            return float("nan")
        p1 = winrate(rows_a)
        p2 = winrate(rows_b)
        p_pool = (sum(1 for r in rows_a if r["win"]) + sum(1 for r in rows_b if r["win"])) / (n1 + n2)
        se = math.sqrt(p_pool * (1 - p_pool) * (1 / n1 + 1 / n2))
        if se == 0:
            return float("nan")
        return (p1 - p2) / se

    for side in ("LONG", "SHORT"):
        rows = [r for r in records if r["side"] == side]
        print(f"\n=== {side} (n={len(rows)}) ===")
        if not rows:
            continue
        # 4분위
        bins = [(0.0, 0.25), (0.25, 0.5), (0.5, 0.75), (0.75, 1.01)]
        print(f"{'구간':>12} | {'n':>5} | {'승률':>7} | {'평균ROE%':>9}")
        for lo, hi in bins:
            b = [r for r in rows if lo <= r["consumed_frac"] < hi]
            print(f"{f'[{lo:.2f},{hi:.2f})':>12} | {len(b):>5} | {winrate(b)*100:>6.1f}% | {avg_roe(b):>8.2f}")
        # 2분위 (초중반 vs 후반부, 8/13-8/14 리포트와 비교 가능하도록)
        hi_half = [r for r in rows if r["consumed_frac"] >= 0.5]
        lo_half = [r for r in rows if r["consumed_frac"] < 0.5]
        print(f"\n  2분위: 후반부(>=0.5) n={len(hi_half)} 승률={winrate(hi_half)*100:.1f}% 평균ROE={avg_roe(hi_half):.2f}%")
        print(f"          초중반(<0.5)  n={len(lo_half)} 승률={winrate(lo_half)*100:.1f}% 평균ROE={avg_roe(lo_half):.2f}%")
        z = z_test_two_proportions(hi_half, lo_half)
        print(f"  z-stat(후반부 vs 초중반 승률차) = {z:.2f}  (|z|>=1.96 => p<0.05 유의)")

        # 상관계수(피어슨)
        if len(rows) > 2:
            xs = [r["consumed_frac"] for r in rows]
            ys = [1.0 if r["win"] else 0.0 for r in rows]
            mx = sum(xs) / len(xs)
            my = sum(ys) / len(ys)
            cov = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
            sx = math.sqrt(sum((x - mx) ** 2 for x in xs))
            sy = math.sqrt(sum((y - my) ** 2 for y in ys))
            corr = cov / (sx * sy) if sx > 0 and sy > 0 else float("nan")
            print(f"  피어슨 상관계수(consumed_frac vs win) = {corr:.3f}")


if __name__ == "__main__":
    main()
