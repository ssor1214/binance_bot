"""micro-scalp tag-only 후보가 "메인 레인과 얼마나 겹치는지"와 "태그 유무로 성과가
갈리는지"를 집계한다.

기존 analyze_micro_scalp_candidates.py는 후보 자체(개수/분포)만 본다. 이 스크립트는
logs/micro_scalp_candidates.jsonl 과 logs/trade_ledger.jsonl 을 대조해서 인수인계 문서가
요구한 다음 두 질문에 답한다:

  1) 이 레인이 "새 기회"를 만드는가, 아니면 메인이 이미 잡는 걸 중복 태깅하는가?
     → 겹침률(overlap rate)
  2) 태그가 붙은 거래가 안 붙은 거래보다 실제로 잘 되는가?
     → 태그/비태그 성과 비교(승률·순익·건당손익·보유시간)

겹침 판정: 같은 심볼 + 같은 방향 + 후보 기록시각과 진입시각 차이가 --window 초 이내.
후보 태깅은 진입 직전에 일어나므로 기본 180초면 충분하지만, 스캔 주기가 바뀌면 조정할 것.

실행:
  python scripts/analyze_micro_scalp_overlap.py
  python scripts/analyze_micro_scalp_overlap.py --minutes 180
  python scripts/analyze_micro_scalp_overlap.py --since "2026-08-17 12:00" --window 240
  python scripts/analyze_micro_scalp_overlap.py --json      # 기계가 읽을 형태로

[주의] 표본이 작을 때는 겹침률이 몇 건 차이로 크게 흔들린다. 이 스크립트는 표본 수를
항상 같이 출력하니, 결론을 내기 전에 n을 먼저 볼 것.
"""
from __future__ import annotations

import argparse
import json
import statistics
import time
from collections import Counter
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CAND_PATH = ROOT / "logs" / "micro_scalp_candidates.jsonl"
LEDGER_PATH = ROOT / "logs" / "trade_ledger.jsonl"


def load_jsonl(path: Path) -> list[dict]:
    rows: list[dict] = []
    if not path.exists():
        return rows
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return rows


def pnl(trade: dict) -> float:
    return trade.get("estimated_pnl_usdt") or 0.0


def summarize(trades: list[dict]) -> dict:
    n = len(trades)
    if not n:
        return {"n": 0}
    wins = [t for t in trades if pnl(t) > 0]
    total = sum(pnl(t) for t in trades)
    held = [t.get("held_seconds") or 0 for t in trades]
    return {
        "n": n,
        "win_rate": len(wins) / n * 100,
        "net_usdt": total,
        "per_trade_usdt": total / n,
        "median_held_sec": statistics.median(held) if held else 0,
    }


def fmt(s: dict) -> str:
    if not s.get("n"):
        return "표본 없음"
    return (
        f"{s['n']}건 승률 {s['win_rate']:.1f}% 순익 {s['net_usdt']:+.3f} USDT "
        f"(건당 {s['per_trade_usdt']:+.4f}, 보유중앙값 {s['median_held_sec']:.0f}초)"
    )


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--minutes", type=float, default=None, help="최근 N분만 집계")
    ap.add_argument("--since", type=str, default=None, help='"YYYY-MM-DD HH:MM" 이후만 집계')
    ap.add_argument("--window", type=float, default=180.0,
                    help="후보-진입을 같은 기회로 볼 시간창(초). 기본 180")
    ap.add_argument("--json", action="store_true", help="JSON으로 출력")
    args = ap.parse_args()

    cands = load_jsonl(CAND_PATH)
    if not cands:
        print(f"후보 로그가 비어 있음: {CAND_PATH}")
        print("-> SPIKE/MICRO_SCALP 설정이 켜져 있는지, 워커가 살아있는지 확인할 것")
        return

    now = time.time()
    start = min(c.get("recorded_at", now) for c in cands)
    if args.since:
        start = max(start, datetime.strptime(args.since, "%Y-%m-%d %H:%M").timestamp())
    if args.minutes:
        start = max(start, now - args.minutes * 60)

    cands = [c for c in cands if (c.get("recorded_at") or 0) >= start]
    if not cands:
        print("해당 구간에 후보 없음")
        return

    # 진입시각 기준으로 봇 거래를 모은다(후보 태깅은 진입 직전이라 window만큼 여유를 둔다).
    ledger = load_jsonl(LEDGER_PATH)
    bots = [
        t for t in ledger
        if t.get("origin") == "bot" and (t.get("entered_at") or 0) >= start - args.window
    ]

    matched: list[tuple[dict, dict]] = []
    unmatched: list[dict] = []
    used_ids: set[int] = set()
    for c in cands:
        hit = None
        for t in bots:
            if id(t) in used_ids:
                continue
            if t.get("symbol") != c.get("symbol") or t.get("side") != c.get("signal"):
                continue
            if abs((t.get("entered_at") or 0) - (c.get("recorded_at") or 0)) <= args.window:
                hit = t
                used_ids.add(id(t))
                break
        if hit:
            matched.append((c, hit))
        else:
            unmatched.append(c)

    tagged_trades = [t for _, t in matched]
    untagged_trades = [t for t in bots if id(t) not in used_ids and (t.get("exited_at") or 0)]

    elapsed_h = max((now - start) / 3600, 1e-9)
    longs = sum(1 for c in cands if c.get("signal") == "LONG")
    mtf22 = sum(1 for c in cands if (c.get("micro_scalp_detail") or {}).get("agree") == 2)

    result = {
        "window_start": datetime.fromtimestamp(start).strftime("%Y-%m-%d %H:%M:%S"),
        "elapsed_hours": round(elapsed_h, 2),
        "match_window_sec": args.window,
        "candidates": {
            "n": len(cands),
            "per_hour": round(len(cands) / elapsed_h, 2),
            "long_pct": round(longs / len(cands) * 100, 1),
            "mtf_2of2_pct": round(mtf22 / len(cands) * 100, 1),
            "median_probability": round(statistics.median(c.get("probability", 0) for c in cands), 4),
            "median_entry_priority": round(statistics.median(c.get("entry_priority", 0) for c in cands), 4),
            "symbols": dict(Counter(c.get("symbol") for c in cands).most_common()),
        },
        "overlap": {
            "matched": len(matched),
            "unmatched": len(unmatched),
            "overlap_rate_pct": round(len(matched) / len(cands) * 100, 1),
        },
        "tagged_performance": summarize(tagged_trades),
        "untagged_performance": summarize(untagged_trades),
    }

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return

    c = result["candidates"]
    o = result["overlap"]
    print(f"수집 시작 {result['window_start']} / 경과 {result['elapsed_hours']}시간 "
          f"/ 매칭창 {args.window:.0f}초")
    print()
    print(f"[후보] {c['n']}건 / 시간당 {c['per_hour']}건 / 롱 {c['long_pct']}% "
          f"/ MTF2-2 {c['mtf_2of2_pct']}% / prob중앙 {c['median_probability']} "
          f"/ priority중앙 {c['median_entry_priority']}")
    print(f"       심볼: {c['symbols']}")
    print()
    print(f"[겹침] 메인도 진입 {o['matched']}건 = {o['overlap_rate_pct']}% "
          f"/ 메인 미진입 {o['unmatched']}건")
    print("       * 겹침이 높을수록 '새 기회'가 아니라 '메인이 잡는 것의 중복 태깅'에 가깝다")
    print()
    print(f"[성과] 태그 있음: {fmt(result['tagged_performance'])}")
    print(f"       태그 없음: {fmt(result['untagged_performance'])}")
    print()
    if matched:
        print("겹친 거래 내역:")
        for cand, t in matched:
            print("  %s %-11s %-24s %+.3f%% %+.3fU held=%.0fs" % (
                time.strftime("%H:%M:%S", time.localtime(cand["recorded_at"])),
                cand.get("symbol"), t.get("exit_reason"),
                t.get("estimated_pnl_pct") or 0, pnl(t), t.get("held_seconds") or 0,
            ))
    if unmatched:
        print()
        print("미겹침 후보(메인이 잡지 않은 기회):")
        for cand in unmatched:
            d = cand.get("micro_scalp_detail") or {}
            print("  %s %-11s prob=%.3f priority=%.3f mtf=%s/%s" % (
                time.strftime("%H:%M:%S", time.localtime(cand["recorded_at"])),
                cand.get("symbol"), cand.get("probability", 0),
                cand.get("entry_priority", 0), d.get("agree"), d.get("total"),
            ))
    print()
    n_small = min(len(matched), len(untagged_trades))
    if n_small < 30:
        # 이모지는 Windows cp949 콘솔에서 UnicodeEncodeError를 일으키므로 쓰지 않는다.
        print(f"[주의] 표본 부족 - 비교군 최소 {n_small}건. 30건 이상 쌓인 뒤 결론 낼 것.")


if __name__ == "__main__":
    main()
