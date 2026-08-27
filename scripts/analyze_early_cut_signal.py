"""진입 후 30/60초 ROE로 "불량 거래"를 판별할 수 있는지 측정한다 (2026-08-18).

배경: 관측 202건에서 고점 ROE가 1.5%에 못 미친 거래 70건(34.7%)이 승률 11.4%,
손실 대부분을 만든다. 나머지 132건은 플러스라 이 34%만 없으면 흑자다
(완벽히 걸러낼 경우 -2.882 -> +7.291).
그런데 **진입 시점 피처로는 구분이 안 된다** — 확률/우선순위/강도/speed/total_score/
mtf_agree/btc_mult 9개 대조에서 8개가 z<2로 무의미했다(확률은 불량 0.9420 vs 정상 0.9409).
그래서 진입 **후** 짧은 구간에서 판별 가능한지를 잰다. roe_at_30s / roe_at_60s는 그 측정용
관측 필드이며 **청산 판단에는 쓰지 않는다**(2026-08-18 14:57 배포).

판정 기준(2026-08-18 사용자합의로 확정, 사후에 바꾸지 말 것):
  **실측 roe_at_30s 100건 단일 기준.**
  - 탐지율 >= 60% AND 오탐률 <= 20% AND 순익 개선 > 0 -> 조기청산 규칙 검증 단계로
  - 그 외 -> "진입 후에도 판별 불가"로 이 방향을 닫는다(그것도 결론이다)

  당초 "과거 복원 500건"을 함께 보기로 했으나 **구조적으로 불가**해 철회했다.
  복원 대상은 max_favorable_roe(불량/정상 라벨)가 있는 거래로 한정되는데, 그 필드도
  2026-08-18 배포 이후에만 있어 72시간을 열어도 246건뿐이었다. 500건을 채우려면
  max_favorable_roe까지 1분봉으로 복원해야 하는데 그건 이미 사용 불가 판정
  (실측 대비 중앙 +1.56%p 과대, 판정 일치율 68.8%)이 난 지표라 라벨 자체가 오염된다.

  복원 246건 결과는 **참고로만** 볼 것(scripts/backfill_early_roe_proxy.py):
    임계 0.0 -> 탐지 66.7% / 오탐 44.4% / 순익 +1.916
    임계 -1.0 -> 탐지 35.7% / 오탐 13.6% / 순익 +1.295
  실측(44건, 임계 0.0에서 탐지 53.3% / 순익 -0.027)과 방향이 어긋난다. 실측을 우선한다.
  **복원의 순익 개선폭은 신뢰하지 말 것** — 판정 일치율은 쟀지만 순익 재계산의 정확도는
  검증하지 않았다. 자른 시점(1분봉 종가) ROE가 실제 청산가보다 유리하게 잡혔을 수 있다.

  표본 100건이면 불량이 약 34건이라 탐지율 신뢰구간은 +-8.2%p다. "40% vs 60%" 구분에는
  충분하지만(2.33 표준오차), 순익 개선폭은 소수의 큰 거래에 좌우돼 불안정하다는 점은
  감안할 것 — 실제로 22건 +0.119 -> 34건 -0.251 -> 44건 +0.008로 부호가 두 번 뒤집혔다.

주의: 2026-08-17에 "무조건 120/180초 후 컷"을 검증했다가 승률 -11~12%p로 기각한 이력이 있다.
그때는 측정 없이 잘랐고 이번엔 판별 가능성부터 잰다. 이 스크립트가 기준을 넘겨도
곧바로 적용하지 말고 offline_backtest 또는 별도 역시뮬로 한 번 더 검증할 것.

실행:
  python scripts/analyze_early_cut_signal.py
  python scripts/analyze_early_cut_signal.py --field roe_at_60s
"""
from __future__ import annotations

import argparse
import json
import statistics
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LEDGER = ROOT / "logs" / "trade_ledger.jsonl"
PNL_KEY = "net_realized_usdt"
BAD_PEAK = 1.5      # 고점 ROE가 이 값 미만이면 "불량"
MIN_SAMPLE = 100    # 판정에 필요한 표본
DETECT_MIN = 60.0   # 탐지율 기준(%)
FALSE_MAX = 20.0    # 오탐률 기준(%)


def margin_of(t: dict) -> float:
    return (t.get("quantity") or 0.0) * (t.get("entry_price") or 0.0) / (t.get("leverage") or 4.0)


def pnl_of(t: dict) -> float:
    v = t.get(PNL_KEY)
    return v if v is not None else (t.get("estimated_pnl_usdt") or 0.0)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--field", default="roe_at_30s", choices=("roe_at_30s", "roe_at_60s"))
    args = ap.parse_args()
    fld = args.field

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
    pool = [t for t in rows
            if t.get("origin") == "bot"
            and t.get(fld) is not None
            and t.get("max_favorable_roe") is not None
            and margin_of(t) > 0]

    print("%s 보유 거래 %d건 (판정선 %d건)" % (fld, len(pool), MIN_SAMPLE))
    if len(pool) < 20:
        print("표본 부족 — 20건 미만이라 아무 계산도 하지 않는다.")
        return

    bad = [t for t in pool if t["max_favorable_roe"] < BAD_PEAK]
    good = [t for t in pool if t["max_favorable_roe"] >= BAD_PEAK]
    base = sum(pnl_of(t) for t in pool)
    print("  불량(고점<%.1f%%) %d건(%.1f%%) 승률 %.1f%% / 정상 %d건 승률 %.1f%%"
          % (BAD_PEAK, len(bad), 100 * len(bad) / len(pool),
             100 * len([t for t in bad if pnl_of(t) > 0]) / len(bad) if bad else 0,
             len(good), 100 * len([t for t in good if pnl_of(t) > 0]) / len(good) if good else 0))
    print("  기준 순익 %+.3f USDT" % base)
    if bad:
        print("  불량 %s 중앙 %+.2f%% / 정상 %s 중앙 %+.2f%%"
              % (fld, statistics.median([t[fld] for t in bad]),
                 fld, statistics.median([t[fld] for t in good]) if good else 0.0))
    print()

    print("%-8s %8s %8s %10s %12s %12s" % ("임계", "탐지율", "오탐률", "자를건수", "예상순익", "개선"))
    verdict = None
    for th in (0.0, -0.3, -0.5, -1.0, -1.5, -2.0, -3.0):
        cut = [t for t in pool if t[fld] <= th]
        if not cut:
            continue
        hit = [t for t in cut if t["max_favorable_roe"] < BAD_PEAK]
        detect = 100 * len(hit) / len(bad) if bad else 0.0
        false = 100 * (len(cut) - len(hit)) / len(good) if good else 0.0
        # 자르면 그 시점 ROE가 실현된다고 본다(수수료는 이미 pnl에 반영된 수준으로 근사)
        newp = 0.0
        for t in pool:
            newp += (t[fld] / 100.0 * margin_of(t)) if t[fld] <= th else pnl_of(t)
        ok = (detect >= DETECT_MIN and false <= FALSE_MAX and newp > base)
        print("%-8.1f %7.1f%% %7.1f%% %10d %+12.3f %+12.3f%s"
              % (th, detect, false, len(cut), newp, newp - base, "  <-- 기준 충족" if ok else ""))
        if ok and (verdict is None or newp > verdict[1]):
            verdict = (th, newp, detect, false)
    print()

    if len(pool) < MIN_SAMPLE:
        print("[판정 보류] 표본 %d건 < %d건. 계속 수집할 것." % (len(pool), MIN_SAMPLE))
        return
    if verdict:
        print("[기준 충족] 임계 %.1f%%: 탐지 %.1f%% / 오탐 %.1f%% / 순익 %+.3f"
              % (verdict[0], verdict[2], verdict[3], verdict[1]))
        print("  -> 곧바로 적용하지 말 것. 별도 역시뮬/백테스트로 한 번 더 검증한 뒤 제안한다.")
    else:
        print("[기준 미달] 탐지 %.0f%% 이상 & 오탐 %.0f%% 이하를 만족하는 임계가 없다."
              % (DETECT_MIN, FALSE_MAX))
        print("  -> '진입 후에도 판별 불가'가 결론이다. 이 방향을 닫고 국면 대응으로 전환할 것.")


if __name__ == "__main__":
    main()
