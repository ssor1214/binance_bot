"""심볼 손실 블락 규칙을 원장으로 역시뮬레이션한다 (2026-08-18 사용자요청).

배경: "한 코인에 2회 연속 손절당하면 일정 시간 그 심볼 매매를 막으면 승률이 오르지 않을까,
숏으로 2번 졌으면 숏만 막고 롱은 열어두면 어떨까"라는 제안을 검증하기 위해 만들었다.
현재 라이브는 `SYMBOL_BLACKLIST_LOSS_THRESHOLD=1` / `SYMBOL_BLACKLIST_COOLDOWN_MIN=10`
(방향 구분 없음)이다.

이 스크립트가 답하는 것:
  1. 방향별 블락(숏 2연패 -> 숏만 차단) vs 방향무관(롱숏 둘 다 차단) 중 무엇이 나은가
  2. 차단 시간 30 / 60 / 120분 중 어디가 나은가
  3. 연속 손실 2회 vs 3회 중 무엇이 나은가

**중요한 한계 — 결과를 곧이곧대로 믿지 말 것:**
- 차단된 거래가 "그냥 사라진다"고 계산한다. 실제로는 슬롯이 비어 다른 심볼이 대신 들어갈 수
  있으므로 대체 진입의 이득/손해가 반영되지 않는다(보수적 방향).
- offline_backtest.py 엔진에는 심볼 블랙리스트 모델이 없어 공식 백테스트로 교차검증할 수 없다.
  이 원장 시뮬이 유일한 근거다.
- 구간을 3등분해 함께 출력한다. **전체 합계만 보고 판단하지 말 것.** 실측(8/15~18)에서
  개선의 대부분이 손실이 컸던 1/3 구간에 몰렸고, 성적이 좋았던 2/3 구간에서는 모든 조합이
  마이너스였다. 이 규칙은 수익 개선책이 아니라 나쁜 국면의 손실 방어책이다.

실행:
  python scripts/simulate_symbol_block_rules.py                 # 최근 72시간
  python scripts/simulate_symbol_block_rules.py --hours 24
  python scripts/simulate_symbol_block_rules.py --since "2026-08-17 01:49:05"

주의: 집계 전에 반드시 `scripts/reconcile_realized_pnl.py --hours N --write`로 실현손익을
보정할 것. 미보정 거래는 추정치 편향이 있어 제외한다.
"""
from __future__ import annotations

import argparse
import json
import time
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LEDGER = ROOT / "logs" / "trade_ledger.jsonl"
PNL_KEY = "net_realized_usdt"


def load(start_ts: float) -> list[dict]:
    rows = []
    with open(LEDGER, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                t = json.loads(line)
            except json.JSONDecodeError:
                continue
            if (t.get("origin") == "bot" and t.get(PNL_KEY) is not None
                    and t.get("entered_at") and (t.get("exited_at") or 0) >= start_ts):
                rows.append(t)
    rows.sort(key=lambda t: t["entered_at"])
    return rows


def simulate(pool: list[dict], direction_aware: bool, loss_streak: int, block_min: float):
    """규칙이 있었다면 차단됐을 거래를 골라낸다.

    스트릭은 심볼(+방향)별로 세고, 승리하면 리셋한다. 라이브 PositionManager의
    symbol_loss_streak 동작과 같은 방식이다(차단 판정은 진입 시각 기준).
    """
    by_symbol: dict[str, list[dict]] = defaultdict(list)
    for t in pool:
        by_symbol[t["symbol"]].append(t)

    blocked_ids = set()
    for trades in by_symbol.values():
        trades.sort(key=lambda t: t["entered_at"])
        streak: dict[str, int] = defaultdict(int)
        blocked_until: dict[str, float] = defaultdict(float)
        for t in trades:
            key = t["side"] if direction_aware else "ANY"
            if t["entered_at"] < blocked_until[key]:
                blocked_ids.add(id(t))
                continue  # 차단됐으므로 스트릭도 갱신하지 않는다(진입 자체가 없었을 것)
            if t[PNL_KEY] <= 0:
                streak[key] += 1
                if streak[key] >= loss_streak:
                    blocked_until[key] = t["exited_at"] + block_min * 60
                    streak[key] = 0
            else:
                streak[key] = 0

    blocked = [t for t in pool if id(t) in blocked_ids]
    kept = [t for t in pool if id(t) not in blocked_ids]
    return blocked, kept


def stats(rows: list[dict]):
    if not rows:
        return (0, 0.0, 0.0)
    wins = [t for t in rows if t[PNL_KEY] > 0]
    return (len(rows), 100 * len(wins) / len(rows), sum(t[PNL_KEY] for t in rows))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--hours", type=float, default=72.0)
    ap.add_argument("--since", type=str, default="")
    args = ap.parse_args()

    start = time.time() - args.hours * 3600
    if args.since:
        start = max(start, time.mktime(time.strptime(args.since, "%Y-%m-%d %H:%M:%S")))

    pool = load(start)
    if len(pool) < 20:
        print("표본 %d건 — 20건 미만이라 결론을 내지 않는다." % len(pool))
        return

    fmt = lambda ts: time.strftime("%m-%d %H:%M", time.localtime(ts))
    n, wr, pnl = stats(pool)
    print("표본 %d건  %s ~ %s" % (n, fmt(pool[0]["entered_at"]), fmt(pool[-1]["exited_at"])))
    print("기준(규칙 없음): 승률 %.2f%%  순익 %+.3f USDT" % (wr, pnl))

    third = len(pool) // 3
    segments = [("1/3", pool[:third]), ("2/3", pool[third:2 * third]), ("3/3", pool[2 * third:])]
    print("구간: " + " / ".join(
        "%s %s~%s(%+.2f)" % (name, fmt(s[0]["entered_at"]), fmt(s[-1]["entered_at"]), stats(s)[2])
        for name, s in segments))
    print()

    header = "%-22s %6s %8s %10s %10s %9s %9s %9s"
    print(header % ("규칙", "차단", "차단승률", "차단손익", "전체개선", "1/3", "2/3", "3/3"))
    best = None
    for loss_streak in (2, 3):
        for block_min in (30, 60, 120):
            for aware in (True, False):
                blocked, kept = simulate(pool, aware, loss_streak, block_min)
                nb, wb, pb = stats(blocked)
                improve = stats(kept)[2] - pnl
                seg_impr = []
                for _, seg in segments:
                    _, seg_kept = simulate(seg, aware, loss_streak, block_min)
                    seg_impr.append(stats(seg_kept)[2] - stats(seg)[2])
                label = "%d연패 %3d분 %s" % (loss_streak, block_min, "방향별" if aware else "롱숏둘다")
                print(header % (label, nb, "%.1f%%" % wb, "%+.3f" % pb, "%+.3f" % improve,
                                "%+.3f" % seg_impr[0], "%+.3f" % seg_impr[1], "%+.3f" % seg_impr[2]))
                # 모든 구간에서 플러스인 조합만 후보로 본다(한 구간 몰림 방지)
                if min(seg_impr) > 0 and (best is None or improve > best[1]):
                    best = (label, improve, nb)
    print()
    print("현행(1연패 10분 롱숏둘다) 참고:")
    blocked, kept = simulate(pool, False, 1, 10)
    nb, wb, pb = stats(blocked)
    print("  차단 %d건 승률 %.1f%% 손익 %+.3f -> 개선 %+.3f" % (nb, wb, pb, stats(kept)[2] - pnl))
    print()
    if best:
        print("[모든 구간에서 플러스인 최선 조합] %s  개선 %+.3f (차단 %d건)" % best)
    else:
        print("[주의] 세 구간 모두에서 플러스인 조합이 없다 — 어떤 규칙도 국면을 탄다는 뜻이다.")
    print("  차단 비율이 높을수록 거래 기회가 줄어든다. 개선폭만 보고 고르지 말 것.")


if __name__ == "__main__":
    main()
