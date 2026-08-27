"""과거 거래의 고점 ROE(max_favorable_roe)를 1분봉으로 복원한다.

목적: 무장선(TAKE_PROFIT_MIN) 조정 판단의 표본을 늘린다. 관측 필드는 2026-08-17 17:55
배포분부터만 있어서 그 이전 거래는 "고점이 어디였나"를 알 수 없다. 1분봉 고가/저가로
되살리면 수백 건 규모로 볼 수 있다.

중요한 한계 — 이 값은 **상한(upper bound)**이다:
  - 1분봉 high/low는 그 1분 안의 극값이고, 봇은 약 5초 주기 폴링으로 mark price를 본다.
    따라서 봇이 실제로 관측했을 값보다 크거나 같다.
  - 1분봉은 last price, 봇 판단은 mark price 기준이라 미세한 차이가 있다.
그래서 --validate 모드로 **관측 필드가 있는 거래에 대해 실측값과 대조**해 편차를 먼저 재고,
편차가 크면 이 대리지표로 결론을 내면 안 된다.

lookahead 우려 없음: 매매 판단에 쓰는 게 아니라 이미 끝난 거래를 사후 계측할 뿐이다.

실행:
  python scripts/backfill_peak_roe_from_klines.py --validate
      관측 필드가 있는 거래에서 1분봉 복원값 vs 실측값 대조 (먼저 이걸 돌릴 것)
  python scripts/backfill_peak_roe_from_klines.py --since "2026-08-16 00:00:00" --arm-sweep
      과거 구간 복원 후 무장선 스윕

주의: REST 스로틀 0.4초 고정(이 저장소는 무스로틀 반복호출로 실제 IP밴을 겪었다).
캐시를 남겨 재실행 시 재조회하지 않는다.
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
LEDGER = ROOT / "logs" / "trade_ledger.jsonl"
CACHE = ROOT / "logs" / "peak_roe_klines_cache.json"
THROTTLE_SEC = 0.4
GIVEBACK_PCT = 0.91  # 무장 성공 거래의 실측 반납폭 중앙값(%p)


def load_ledger() -> list[dict]:
    rows = []
    with open(LEDGER, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return rows


def margin_of(t: dict) -> float:
    return (t.get("quantity") or 0.0) * (t.get("entry_price") or 0.0) / (t.get("leverage") or 4.0)


def realized_roe(t: dict) -> float | None:
    m = margin_of(t)
    pnl = t.get("net_realized_usdt")
    if pnl is None or m <= 0:
        return None
    return pnl / m * 100.0


def arm_threshold(t: dict) -> float:
    snap = t.get("config_snapshot") or {}
    key = "short_take_profit_min" if t.get("side") == "SHORT" else "take_profit_min"
    return float(snap.get(key) or snap.get("take_profit_min") or 3.0)


def fetch_klines(ex, symbol: str, start_ms: int, end_ms: int, cache: dict) -> list[list]:
    """심볼별로 필요한 구간을 한 번에 받아 캐시한다(거래마다 조회하면 호출수가 폭발한다)."""
    key = "%s:%d:%d" % (symbol, start_ms // 60000, end_ms // 60000)
    if key in cache:
        return cache[key]
    out: list[list] = []
    cursor = start_ms
    while cursor < end_ms:
        try:
            rows = ex.client.futures_klines(
                symbol=symbol, interval="1m", startTime=cursor, endTime=end_ms, limit=1000
            )
        except Exception as e:
            print("  klines 실패 %s: %s" % (symbol, e))
            break
        time.sleep(THROTTLE_SEC)  # IP밴 방지
        if not rows:
            break
        out.extend(rows)
        last = int(rows[-1][0])
        if len(rows) < 1000 or last <= cursor:
            break
        cursor = last + 60000
    cache[key] = out
    return out


def peak_roe_from_klines(t: dict, candles: list[list]) -> float | None:
    """진입~청산 구간의 1분봉에서 유리한 방향 극값으로 고점 ROE를 계산한다."""
    entry = t.get("entry_price") or 0.0
    lev = t.get("leverage") or 4.0
    a, b = (t.get("entered_at") or 0) * 1000, (t.get("exited_at") or 0) * 1000
    if entry <= 0 or a <= 0 or b <= a:
        return None
    best = None
    for c in candles:
        ts = int(c[0])
        if ts + 60000 < a or ts > b:
            continue
        high, low = float(c[2]), float(c[3])
        px = high if t.get("side") == "LONG" else low
        move = (px / entry - 1.0) * (1 if t.get("side") == "LONG" else -1)
        roe = move * lev * 100.0
        if best is None or roe > best:
            best = roe
    return best


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--validate", action="store_true",
                    help="관측 필드가 있는 거래에서 복원값 vs 실측값을 대조한다")
    ap.add_argument("--since", type=str, default="", help='"YYYY-MM-DD HH:MM:SS"')
    ap.add_argument("--arm-sweep", action="store_true", help="무장선 스윕 결과 출력")
    ap.add_argument("--limit", type=int, default=0, help="심볼 수 상한(시험 실행용)")
    args = ap.parse_args()

    from bot.config import Config
    from bot.exchange import Exchange
    ex = Exchange(Config())

    rows = load_ledger()
    pool = [t for t in rows
            if t.get("origin") == "bot"
            and t.get("net_realized_usdt") is not None
            and t.get("entered_at") and t.get("exited_at")]

    if args.validate:
        pool = [t for t in pool if t.get("max_favorable_roe") is not None]
        print("검증 대상(관측 필드 보유) %d건" % len(pool))
    else:
        if args.since:
            start = time.mktime(time.strptime(args.since, "%Y-%m-%d %H:%M:%S"))
            pool = [t for t in pool if t["exited_at"] >= start]
        print("복원 대상 %d건" % len(pool))
    if not pool:
        print("대상 없음")
        return

    by_symbol: dict[str, list[dict]] = defaultdict(list)
    for t in pool:
        by_symbol[t["symbol"]].append(t)
    symbols = sorted(by_symbol)
    if args.limit:
        symbols = symbols[:args.limit]
    print("심볼 %d개, 스로틀 %.1f초" % (len(symbols), THROTTLE_SEC))

    cache = {}
    if CACHE.exists():
        try:
            cache = json.loads(CACHE.read_text(encoding="utf-8"))
            print("캐시 %d구간 재사용" % len(cache))
        except Exception:
            cache = {}

    restored = []
    for i, sym in enumerate(symbols, 1):
        ts = by_symbol[sym]
        lo = int(min(t["entered_at"] for t in ts) * 1000) - 120000
        hi = int(max(t["exited_at"] for t in ts) * 1000) + 120000
        candles = fetch_klines(ex, sym, lo, hi, cache)
        for t in ts:
            pk = peak_roe_from_klines(t, candles)
            if pk is not None:
                restored.append((t, pk))
        if i % 10 == 0:
            print("  %d/%d 심볼 (%d건 복원)" % (i, len(symbols), len(restored)), flush=True)
            CACHE.write_text(json.dumps(cache), encoding="utf-8")  # 중간 저장
    CACHE.write_text(json.dumps(cache), encoding="utf-8")
    print("복원 완료 %d건" % len(restored))
    print()

    if args.validate:
        diffs = [pk - t["max_favorable_roe"] for t, pk in restored]
        print("[검증] 1분봉 복원값 - 실측 관측값 (양수면 복원값이 더 큼 = 상한 성격)")
        print("  n=%d  중앙 %+.2f%%p  평균 %+.2f%%p  표준편차 %.2f" % (
            len(diffs), statistics.median(diffs), statistics.mean(diffs),
            statistics.pstdev(diffs) if len(diffs) > 1 else 0.0))
        print("  복원값이 더 작은 건(있으면 안 됨) %d건" % len([d for d in diffs if d < -0.05]))
        # 무장 여부 판정이 일치하는지가 실제로 중요한 부분이다
        agree = 0
        for t, pk in restored:
            thr = arm_threshold(t)
            if ((pk >= thr) == ((t["max_favorable_roe"] or 0) >= thr)):
                agree += 1
        print("  무장선 도달 여부 판정 일치율 %.1f%% (%d/%d)"
              % (100 * agree / len(restored), agree, len(restored)))
        print()
        print("  [해석] 일치율이 낮으면 이 대리지표로 무장률을 논하면 안 된다.")
        return

    if args.arm_sweep:
        cur_armed = len([t for t, _ in restored if t.get("armed_at")])
        print("[무장선 스윕] 표본 %d건" % len(restored))
        print("%-8s %10s %10s %16s" % ("무장선", "신규무장", "무장률", "순개선추정(ROE합)"))
        for new in (3.0, 2.5, 2.0, 1.5):
            cand = [(t, pk) for t, pk in restored
                    if not t.get("armed_at") and arm_threshold(t) > new and pk >= new]
            gain = 0.0
            for t, pk in cand:
                r = realized_roe(t)
                if r is None:
                    continue
                gain += max(0.0, pk - GIVEBACK_PCT) - r
            armed = cur_armed + len(cand)
            print("%-8.1f %10d %9.1f%% %15.2f%%" % (new, len(cand), 100 * armed / len(restored), gain))
        print()
        print("  주의: 복원 고점은 상한이므로 개선 추정도 낙관 방향으로 치우친다.")
        print("  반납폭은 무장 성공 거래의 실측 중앙값 %.2f%%p를 적용했다." % GIVEBACK_PCT)


if __name__ == "__main__":
    main()
