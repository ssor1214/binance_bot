"""micro-scalp 태그를 과거 로그로 **소급 재구성**해서 태그 O/X 성과를 비교한다.

문제: logs/micro_scalp_candidates.jsonl은 MICRO_SCALP_* 설정이 추가된 2026-08-17 12:32부터만
쌓인다. 그 전 구간(V2 도입 01:49~)은 태그 기록이 없어 표본이 얇다.

해결: 태그 조건은 후보 로그에 남는 값만으로 판정 가능하다.
  probability >= MICRO_SCALP_MIN_PROBABILITY (0.88)
  entry_priority >= MICRO_SCALP_MIN_ENTRY_PRIORITY (0.80)
  signal == LONG (MICRO_SCALP_LONG_ONLY=true)
bot.log의 "이번 주기 진입 후보 N개 (상위: [(SYM, prob, priority), ...])" 줄에서 이 값을 복원해
실제 진입(trade_ledger)과 매칭하면, 과거 거래에도 같은 기준의 태그를 붙일 수 있다.

**한계(반드시 감안할 것)**
- `btc_momentum_opposes=false` 조건은 후보 로그에 안 남아 판정에서 제외했다. 즉 실시간 태그보다
  약간 느슨하다(실시간 태그의 상위집합).
- 후보 로그는 상위 5개만 출력하므로, 6번째 이하 후보로 진입한 거래는 태그 판정이 불가하다
  (unknown으로 분류하고 비교에서 제외한다).
- 따라서 이 결과는 실시간 태그 집계와 정확히 같지 않다. 방향성 확인용이다.

실행:
  python scripts/backfill_micro_scalp_tags.py --since "2026-08-17 01:49"
  python scripts/backfill_micro_scalp_tags.py --since "2026-08-16 00:00" --hourly
  python scripts/backfill_micro_scalp_tags.py --since "2026-08-17 01:49" --prob-min 0.86 --prio-min 0.78
"""
from __future__ import annotations

import argparse
import ast
import json
import re
import statistics
from collections import defaultdict
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LOG_PATHS = [ROOT / "logs" / "bot.log.2", ROOT / "logs" / "bot.log.1", ROOT / "logs" / "bot.log"]
LEDGER = ROOT / "logs" / "trade_ledger.jsonl"

TS_RE = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),\d+")
CAND_RE = re.compile(r"이번 주기 진입 후보 \d+개 \(상위: (\[.*?\]), 확률/score순")
MATCH_WINDOW_SEC = 180.0


def parse_candidates(since_ts: float) -> list[dict]:
    """후보 로그에서 (시각, 심볼, 확률, 우선순위)를 뽑는다."""
    out: list[dict] = []
    for path in LOG_PATHS:
        if not path.exists():
            continue
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                m0 = TS_RE.match(line)
                if not m0:
                    continue
                ts = datetime.strptime(m0.group(1), "%Y-%m-%d %H:%M:%S").timestamp()
                if ts < since_ts:
                    continue
                m = CAND_RE.search(line)
                if not m:
                    continue
                raw = re.sub(r"np\.float64\(([-\d.eE]+)\)", r"\1", m.group(1))
                try:
                    items = ast.literal_eval(raw)
                except (ValueError, SyntaxError):
                    continue
                for it in items:
                    if isinstance(it, (list, tuple)) and len(it) >= 3:
                        try:
                            out.append({"ts": ts, "symbol": str(it[0]),
                                        "probability": float(it[1]), "entry_priority": float(it[2])})
                        except (TypeError, ValueError):
                            continue
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", required=True, help='"YYYY-MM-DD HH:MM"')
    ap.add_argument("--prob-min", type=float, default=None, help="기본은 .env의 MICRO_SCALP_MIN_PROBABILITY")
    ap.add_argument("--prio-min", type=float, default=None, help="기본은 .env의 MICRO_SCALP_MIN_ENTRY_PRIORITY")
    ap.add_argument("--hourly", action="store_true", help="정각 시간별로도 나눠서 출력")
    args = ap.parse_args()

    import sys
    sys.path.insert(0, str(ROOT))
    from bot.config import Config
    cfg = Config()
    prob_min = args.prob_min if args.prob_min is not None else cfg.micro_scalp_min_probability
    prio_min = args.prio_min if args.prio_min is not None else cfg.micro_scalp_min_entry_priority

    since_ts = datetime.strptime(args.since, "%Y-%m-%d %H:%M").timestamp()
    cands = parse_candidates(since_ts)

    trades = []
    with open(LEDGER, encoding="utf-8") as f:
        for line in f:
            try:
                t = json.loads(line)
            except json.JSONDecodeError:
                continue
            if t.get("origin") == "bot" and (t.get("exited_at") or 0) >= since_ts:
                trades.append(t)

    # 진입 시각 근처의 후보 기록에서 확률/우선순위를 찾아 태그 판정
    by_symbol: dict[str, list[dict]] = defaultdict(list)
    for c in cands:
        by_symbol[c["symbol"]].append(c)

    tagged, untagged, unknown = [], [], []
    for t in trades:
        ent = t.get("entered_at") or 0
        best = None
        for c in by_symbol.get(t.get("symbol", ""), []):
            if abs(c["ts"] - ent) <= MATCH_WINDOW_SEC:
                if best is None or abs(c["ts"] - ent) < abs(best["ts"] - ent):
                    best = c
        if best is None:
            unknown.append(t)
            continue
        t["_prob"], t["_prio"] = best["probability"], best["entry_priority"]
        is_tag = (best["probability"] >= prob_min
                  and best["entry_priority"] >= prio_min
                  and t.get("side") == "LONG")
        (tagged if is_tag else untagged).append(t)

    def pnl(x):
        return x.get("estimated_pnl_usdt") or 0.0

    def summarize(g, label):
        if not g:
            return f"{label}: 표본 없음"
        w = sum(1 for x in g if pnl(x) > 0)
        tot = sum(pnl(x) for x in g)
        held = statistics.median(x.get("held_seconds") or 0 for x in g)
        return (f"{label}: {len(g)}건 승률 {w/len(g)*100:.1f}% 순익 {tot:+.3f} "
                f"건당 {tot/len(g):+.4f} 보유중앙값 {held:.0f}초")

    print(f"기준: {args.since} 이후 / 태그조건 prob>={prob_min} priority>={prio_min} LONG only")
    print(f"      (btc_momentum_opposes 조건은 로그에 없어 제외 - 실시간 태그의 상위집합)")
    print()
    print(f"봇 거래 {len(trades)}건 중 태그판정 가능 {len(tagged)+len(untagged)}건 "
          f"/ 판정불가 {len(unknown)}건(후보로그 상위5개 밖)")
    print()
    print("  " + summarize(tagged, "태그 O"))
    print("  " + summarize(untagged, "태그 X"))

    if args.hourly:
        print()
        print("| 시각 | 태그O 건수 | 태그O 승률 | 태그O 건당 | 태그X 건수 | 태그X 승률 | 태그X 건당 |")
        print("|---|---|---|---|---|---|---|")
        buckets: dict[float, dict[str, list]] = defaultdict(lambda: {"t": [], "u": []})
        for g, key in ((tagged, "t"), (untagged, "u")):
            for x in g:
                lt = datetime.fromtimestamp(x["exited_at"]).replace(minute=0, second=0, microsecond=0)
                buckets[lt.timestamp()][key].append(x)
        for k in sorted(buckets):
            b = buckets[k]
            def cell(g):
                if not g:
                    return "0 | - | -"
                w = sum(1 for x in g if pnl(x) > 0)
                tot = sum(pnl(x) for x in g)
                return f"{len(g)} | {w/len(g)*100:.1f}% | {tot/len(g):+.4f}"
            print(f"| {datetime.fromtimestamp(k):%H}시 | {cell(b['t'])} | {cell(b['u'])} |")

    n_small = min(len(tagged), len(untagged))
    print()
    if n_small < 30:
        print(f"[주의] 표본 부족 - 비교군 최소 {n_small}건. 30건 이상에서 판단할 것.")


if __name__ == "__main__":
    main()
