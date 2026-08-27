"""트레일링 무장률과 반납폭을 원장의 관측 필드로 집계한다.

배경(2026-08-17): 실현손익 보정 후 436건 재집계 결과, 순익 전체가 "봇 트레일링이 무장한
69건(+8.99USDT, 승률 98.6%)"에서 나오고 무장 실패 367건이 -16.76USDT였다. 무장률을 올리는
것이 손익비 개선의 핵심 후보인데, 당시엔 무장 여부를 bot.log 문자열로 역산할 수밖에 없어
수치 신뢰도가 낮았다. bot/position_manager.py에 관측 필드를 넣었으므로 이제 원장만으로 집계한다.

[2026-08-18 지표 전환] 무장률을 주지표에서 내리고 **고점 ROE 분포**를 앞에 둔다. 이유:
  1. "무장 성공 승률 100%"는 발견이 아니라 정의상 당연하다. 무장 조건이 ROE >= take_profit_min
     (롱3.0/숏4.0)이고 확정은 고점 대비 TRAIL_DRAWDOWN 1.3%p이므로, 3.0%에서 무장해 1.3%p를
     반납해도 1.7%가 남는다. 질 수가 없는 구조다(실측 무장 시점 ROE 최소 3.01%, 30건 전승).
  2. 무장률은 take_profit_min에 종속돼 그 값을 바꾸면 시계열이 끊긴다. 롱/숏이 다른 임계를
     쓰는 것도 섞인다.
  3. 시간대별로 무장률과 고점 ROE 중앙값의 상관이 r=0.302로 약하다. 무장률은 "3% 선을
     넘었나"만 세는 이진 지표라 그 아래 분포를 전부 버린다.
  실측(178건): 순익 기여가 가장 큰 구간은 고점 >= 1.5%(+6.598)이며 >= 3.0%(무장선)는
  +4.522로 오히려 낮다. 무장선이 너무 좁게 잘라 좋은 거래를 놓치고 있다는 뜻이다.
  -> 주지표: **고점 ROE 중앙값**, **고점 >= 1.5% 비율**. 무장률은 참고로만 남긴다.
  무장 실패 원인 분해는 A안(백스탑 완화) 재론 방지용으로 계속 출력한다.

핵심 질문 세 가지:
  1. 무장률은 실제로 몇 %인가 (로그 역산 추정치는 16%였다)
  2. 무장 실패 거래 중 "가격은 무장선을 넘겼는데 봇이 못 잡은" 비율은 몇 %인가
     -> max_favorable_roe >= take_profit_min 인데 armed_at 이 없는 거래
     -> 이 비율이 높으면 거래소 백스탑/폴링이 원인(A안), 낮으면 그냥 가격이 안 온 것(B안 무의미)
  3. 반납폭(max_favorable_roe - 실현 ROE)이 얼마나 되는가

실행:
  python scripts/analyze_arming_rate.py                 # 관측 필드가 있는 거래 전체
  python scripts/analyze_arming_rate.py --hours 24
  python scripts/analyze_arming_rate.py --since "2026-08-17 18:00:00"

주의: 관측 필드는 2026-08-17 배포 이후 청산된 거래에만 있다. 그 이전 거래는 전부
"필드 없음"으로 제외되며, 표본이 20건 미만이면 결론을 내지 않는다(저장소 규칙).
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
LEDGER = ROOT / "logs" / "trade_ledger.jsonl"
MIN_SAMPLE = 20


def realized_roe(t: dict) -> float | None:
    """실현 ROE(%). 실현손익 보정값이 있으면 그걸 쓰고, 없으면 추정치로 폴백한다."""
    margin = (t.get("quantity") or 0.0) * (t.get("entry_price") or 0.0) / (t.get("leverage") or 1.0)
    if margin <= 0:
        return None
    pnl = t.get("net_realized_usdt")
    if pnl is None:
        pnl = t.get("estimated_pnl_usdt")
    if pnl is None:
        return None
    return pnl / margin * 100.0


def arm_threshold(t: dict) -> float:
    """이 거래에 적용된 무장선(진입 시점 스냅샷 기준 — 그 뒤 설정을 바꿔도 소급되지 않는다)."""
    snap = t.get("config_snapshot") or {}
    key = "short_take_profit_min" if t.get("side") == "SHORT" else "take_profit_min"
    return float(snap.get(key) or snap.get("take_profit_min") or 0.0)


def fmt(rows: list[dict], label: str) -> str:
    if not rows:
        return "  %-22s n=0" % label
    pnl = [t.get("net_realized_usdt") if t.get("net_realized_usdt") is not None
           else t.get("estimated_pnl_usdt") or 0.0 for t in rows]
    wins = [p for p in pnl if p > 0]
    roes = [r for r in (realized_roe(t) for t in rows) if r is not None]
    return ("  %-22s n=%3d  승률 %5.1f%%  순익 %+8.3f USDT  평균ROE %+6.2f%%"
            % (label, len(rows), 100 * len(wins) / len(rows), sum(pnl),
               statistics.mean(roes) if roes else 0.0))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--hours", type=float, default=0.0, help="최근 N시간만")
    ap.add_argument("--since", type=str, default="", help='"YYYY-MM-DD HH:MM:SS" 이후')
    args = ap.parse_args()

    start = 0.0
    if args.hours:
        start = time.time() - args.hours * 3600
    if args.since:
        start = max(start, time.mktime(time.strptime(args.since, "%Y-%m-%d %H:%M:%S")))

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

    # 관측 필드가 있는 거래만. evaluate_calls는 배포 이후 거래에만 존재한다.
    pool = [t for t in rows
            if t.get("origin") == "bot"
            and t.get("evaluate_calls") is not None
            and (t.get("exited_at") or 0) >= start]

    print("관측 필드 보유 거래 %d건 (전체 원장 %d건)" % (len(pool), len(rows)))
    if not pool:
        print("아직 표본이 없다 — 배포 이후 청산된 거래가 쌓여야 집계된다.")
        return
    span = max(t["exited_at"] for t in pool) - min(t["exited_at"] for t in pool)
    print("구간 %s ~ %s (%.2f시간)" % (
        time.strftime("%m-%d %H:%M", time.localtime(min(t["exited_at"] for t in pool))),
        time.strftime("%m-%d %H:%M", time.localtime(max(t["exited_at"] for t in pool))),
        span / 3600))
    print()

    # [2026-08-18] 주지표: 고점 ROE 분포. 임계값에 묶이지 않고 진입 품질을 직접 보여준다.
    peaks = sorted(t["max_favorable_roe"] for t in pool if t.get("max_favorable_roe") is not None)
    if peaks:
        median_peak = statistics.median(peaks)
        print("[주지표] 고점 ROE 중앙값 %+.2f%%   (분포 p25 %+.2f%% / p75 %+.2f%%)"
              % (median_peak, peaks[len(peaks) // 4], peaks[len(peaks) * 3 // 4]))
        for th in (1.0, 1.5, 2.0):
            hit = [t for t in pool if (t.get("max_favorable_roe") or 0) >= th]
            if not hit:
                continue
            wins = [t for t in hit if (t.get("net_realized_usdt") if t.get("net_realized_usdt") is not None
                                       else t.get("estimated_pnl_usdt") or 0) > 0]
            pnl = sum(t.get("net_realized_usdt") if t.get("net_realized_usdt") is not None
                      else t.get("estimated_pnl_usdt") or 0 for t in hit)
            mark = "  <- 주지표" if abs(th - 1.5) < 1e-9 else ""
            print("  고점 >= %.1f%%: %3d건 (%4.1f%%)  승률 %5.1f%%  순익 %+7.3f%s"
                  % (th, len(hit), 100 * len(hit) / len(pool),
                     100 * len(wins) / len(hit), pnl, mark))
        print()

    armed = [t for t in pool if t.get("armed_at")]
    unarmed = [t for t in pool if not t.get("armed_at")]
    print("[참고] 무장률 %.1f%% (%d/%d) — 정의상 승률 100%%에 가까우므로 성과 지표로 쓰지 말 것" % (100 * len(armed) / len(pool), len(armed), len(pool)))
    print(fmt(armed, "무장 성공"))
    print(fmt(unarmed, "무장 실패"))
    print()

    # 무장 실패 원인 분해: 가격이 안 왔나, 봇이 못 잡았나
    missed = [t for t in unarmed
              if (t.get("max_favorable_roe") or 0.0) >= arm_threshold(t) > 0]
    never = [t for t in unarmed if t not in missed]
    print("[2] 무장 실패 %d건의 원인 분해" % len(unarmed))
    if unarmed:
        print("  가격이 무장선까지 안 옴        %3d건 (%.1f%%)"
              % (len(never), 100 * len(never) / len(unarmed)))
        print("  무장선은 넘겼는데 못 잡음      %3d건 (%.1f%%)  <- 거래소 선청산/폴링 지연 후보"
              % (len(missed), 100 * len(missed) / len(unarmed)))
    print(fmt(missed, "  그중 실적"))
    if missed:
        print("  이 거래들의 관측 고점 ROE 중앙 %.2f%% / 실현 ROE 중앙 %.2f%%"
              % (statistics.median([t["max_favorable_roe"] for t in missed]),
                 statistics.median([r for r in (realized_roe(t) for t in missed) if r is not None] or [0])))
        print("  청산사유 %s" % Counter(t.get("exit_reason") for t in missed).most_common(5))
        print("  폴링 횟수 중앙 %d회 (보유 중앙 %.0f초)"
              % (statistics.median([t["evaluate_calls"] for t in missed]),
                 statistics.median([t.get("held_seconds") or 0 for t in missed])))
    print()

    print("[3] 반납폭 (관측 고점 ROE - 실현 ROE)")
    for label, group in (("무장 성공", armed), ("무장 실패", unarmed)):
        gb = [t["max_favorable_roe"] - r for t in group
              if (r := realized_roe(t)) is not None and t.get("max_favorable_roe") is not None]
        if gb:
            print("  %-10s n=%3d  중앙 %+.2f%%p  평균 %+.2f%%p  최대 %+.2f%%p"
                  % (label, len(gb), statistics.median(gb), statistics.mean(gb), max(gb)))
    print()

    if len(pool) < MIN_SAMPLE:
        print("[결론 보류] 표본 %d건 < %d건. 더 쌓인 뒤 다시 볼 것." % (len(pool), MIN_SAMPLE))
    else:
        print("[판정 기준]")
        print("  - '무장선 넘겼는데 못 잡음'이 무장 실패의 30%% 이상이면 A안(거래소 백스탑 완화)에 근거가 생긴다.")
        print("  - 대부분이 '가격이 안 옴'이면 무장선을 낮추는 B안도 A안도 효과가 없다. 진입 품질 문제다.")


if __name__ == "__main__":
    main()
