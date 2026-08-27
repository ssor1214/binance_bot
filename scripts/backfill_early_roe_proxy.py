"""과거 거래의 "진입 직후 ROE"를 1분봉 종가로 복원해 조기청산 신호를 검증한다.

배경: roe_at_30s 관측 필드는 2026-08-18 14:57 배포 이후 거래에만 있다. 실측만으로는
표본이 시간당 10건씩밖에 안 쌓여 판정이 늦다. 그래서 과거로 확장한다.

대리지표: **진입이 속한 1분봉의 종가 시점 ROE**.
  진입 시각이 분 안에서 어디냐에 따라 진입 후 0~60초 사이 값이 된다.
  실측(진입 시각의 분내 위치 중앙 31초)이라 종가까지 평균 29초 -> "약 30초"와 거의 맞는다.

**정확도 검증 완료(실측 47건 대조):**
  복원값 - 실측 = 중앙 -0.05%p / 평균 -0.08%p (편향 거의 없음)
  판정 일치율: 임계 0.0 -> 89.4% / -0.5 -> 83.0% / -0.3 -> 78.7%
  고점 ROE 복원(중앙 +1.56%p 과대, 일치율 68.8%로 사용 불가 판정)과 결과가 다른 이유는
  고점은 극값이라 1분봉 고가가 "봇이 폴링으로 잡을 수 없었던 순간꼬리"를 포함하는 반면,
  이 지표는 **시점값**이라 1분봉 종가가 확정된 실제 가격이기 때문이다.

한계(결론에 반드시 병기할 것):
  - 일치율 79~89%라 10~20%는 틀린다. 탐지율 추정에 그만큼 오차가 붙는다.
  - 진입 시각이 분 초반이면 종가까지 55초, 후반이면 5초라 거래마다 시점이 다르다.
  - 정확도 자체가 실측 47건으로 잰 값이라 그것도 표본이 작다.
  -> 방향 판단에는 쓰되, 실측 표본과 **함께** 보고 둘이 어긋나면 실측을 우선한다.

실행:
  python scripts/backfill_early_roe_proxy.py --hours 72
  python scripts/backfill_early_roe_proxy.py --hours 168 --limit 500
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
CACHE = ROOT / "logs" / "early_roe_proxy_cache.json"
PNL_KEY = "net_realized_usdt"
THROTTLE_SEC = 0.4
BAD_PEAK = 1.5
DETECT_MIN = 60.0
FALSE_MAX = 20.0


def margin_of(t):
    return (t.get("quantity") or 0.0) * (t.get("entry_price") or 0.0) / (t.get("leverage") or 4.0)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--hours", type=float, default=72.0)
    ap.add_argument("--limit", type=int, default=0, help="대상 거래 수 상한(최근순)")
    args = ap.parse_args()

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
    start = time.time() - args.hours * 3600
    pool = [t for t in rows
            if t.get("origin") == "bot" and t.get(PNL_KEY) is not None
            and t.get("max_favorable_roe") is not None
            and t.get("entered_at") and (t.get("exited_at") or 0) >= start
            and margin_of(t) > 0]
    pool.sort(key=lambda t: t["entered_at"], reverse=True)
    if args.limit:
        pool = pool[:args.limit]
    print("대상 %d건 (최근 %.0f시간)" % (len(pool), args.hours))
    if not pool:
        return

    cache = {}
    if CACHE.exists():
        try:
            cache = json.loads(CACHE.read_text(encoding="utf-8"))
        except Exception:
            cache = {}

    from bot.config import Config
    from bot.exchange import Exchange
    ex = Exchange(Config())

    by_symbol = defaultdict(list)
    for t in pool:
        by_symbol[t["symbol"]].append(t)
    print("심볼 %d개, 스로틀 %.1f초" % (len(by_symbol), THROTTLE_SEC))

    joined = []
    for i, (sym, ts) in enumerate(sorted(by_symbol.items()), 1):
        lo = int(min(t["entered_at"] for t in ts) * 1000) - 120000
        hi = int(max(t["entered_at"] for t in ts) * 1000) + 180000
        key = "%s:%d:%d" % (sym, lo // 60000, hi // 60000)
        closes = cache.get(key)
        if closes is None:
            closes = {}
            cursor = lo
            while cursor < hi:
                try:
                    kl = ex.client.futures_klines(symbol=sym, interval="1m",
                                                  startTime=cursor, endTime=hi, limit=1000)
                except Exception as e:
                    print("  %s 조회 실패: %s" % (sym, e))
                    break
                time.sleep(THROTTLE_SEC)  # IP밴 방지
                if not kl:
                    break
                for k in kl:
                    closes[str(int(k[0]))] = float(k[4])
                last = int(kl[-1][0])
                if len(kl) < 1000 or last <= cursor:
                    break
                cursor = last + 60000
            cache[key] = closes
        for t in ts:
            em = int(t["entered_at"] * 1000) // 60000 * 60000
            c = closes.get(str(em))
            if c is None:
                continue
            move = (c / t["entry_price"] - 1) * (1 if t["side"] == "LONG" else -1)
            joined.append((t, move * (t.get("leverage") or 4.0) * 100))
        if i % 20 == 0:
            print("  %d/%d 심볼 (%d건)" % (i, len(by_symbol), len(joined)), flush=True)
            CACHE.write_text(json.dumps(cache), encoding="utf-8")
    CACHE.write_text(json.dumps(cache), encoding="utf-8")
    print("복원 %d건" % len(joined))
    print()

    bad = [(t, p) for t, p in joined if t["max_favorable_roe"] < BAD_PEAK]
    good = [(t, p) for t, p in joined if t["max_favorable_roe"] >= BAD_PEAK]
    base = sum(t[PNL_KEY] for t, _ in joined)
    print("불량(고점<%.1f%%) %d건(%.1f%%) 승률 %.1f%% / 정상 %d건 승률 %.1f%%"
          % (BAD_PEAK, len(bad), 100 * len(bad) / len(joined),
             100 * len([1 for t, _ in bad if t[PNL_KEY] > 0]) / len(bad) if bad else 0,
             len(good), 100 * len([1 for t, _ in good if t[PNL_KEY] > 0]) / len(good) if good else 0))
    print("기준 순익 %+.3f USDT" % base)
    if bad and good:
        print("불량 복원ROE 중앙 %+.2f%% / 정상 %+.2f%%"
              % (statistics.median([p for _, p in bad]), statistics.median([p for _, p in good])))
    print()

    print("%-8s %8s %8s %10s %12s %12s" % ("임계", "탐지율", "오탐률", "자를건수", "예상순익", "개선"))
    for th in (0.0, -0.3, -0.5, -1.0, -1.5, -2.0, -3.0):
        cut = [(t, p) for t, p in joined if p <= th]
        if not cut:
            continue
        hit = [1 for t, _ in cut if t["max_favorable_roe"] < BAD_PEAK]
        detect = 100 * len(hit) / len(bad) if bad else 0.0
        false = 100 * (len(cut) - len(hit)) / len(good) if good else 0.0
        newp = sum((p / 100.0 * margin_of(t)) if p <= th else t[PNL_KEY] for t, p in joined)
        ok = detect >= DETECT_MIN and false <= FALSE_MAX and newp > base
        print("%-8.1f %7.1f%% %7.1f%% %10d %+12.3f %+12.3f%s"
              % (th, detect, false, len(cut), newp, newp - base, "  <-- 기준 충족" if ok else ""))
    print()
    print("[한계] 복원값은 실측 대비 판정 일치율 79~89%. 방향 판단용이며 실측과 함께 볼 것.")


if __name__ == "__main__":
    main()
