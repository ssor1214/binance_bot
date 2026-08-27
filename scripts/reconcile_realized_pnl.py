"""거래원장의 추정손익(estimated_pnl_usdt)을 거래소 실현손익과 대조/보정한다.

문제(2026-08-17 실측으로 발견):
  원장의 `estimated_pnl_usdt`는 **봇이 폴링으로 청산을 인지한 시점의 mark price** 기반
  추정치다. 거래소가 먼저 청산하고 봇이 최대 5초 뒤 발견하면, 그 사이 가격이 움직여
  실제 체결가와 다른 값이 기록된다. 급락 중에는 실제보다 나쁘게 기록되는 편향이 생긴다.

  실측(최근 3시간 40건): 봇 추정 합계 -1.8981 vs 실제 실현 -1.0431 → **0.855 USDT 과대손실**.
  16개 심볼 중 14개에서 실제가 더 좋았고, CHIPUSDT/HUSDT는 **부호까지 반대**였다.
  (CHIPUSDT 봇 -0.108 vs 실제 +0.060)

  원장 스키마 주석에도 "정확한 값은 알 수 있을 때만 채운다"고 예고돼 있었으나,
  편향의 크기가 실측된 적이 없었다. 승률/손익 통계가 전부 이 추정치 기반이므로 영향이 크다.

이 스크립트는 `futures_income_history`의 REALIZED_PNL/COMMISSION/FUNDING_FEE를 가져와
원장 각 거래에 다음 필드를 채운다(기존 값은 덮어쓰지 않는다):
  realized_pnl_usdt       실제 실현손익(수수료 제외)
  commission_usdt         실제 수수료(음수)
  funding_fee_usdt        펀딩비
  net_realized_usdt       실현손익 + 수수료 + 펀딩비 (= 실제 잔고 증감)
  pnl_estimate_error      net_realized_usdt - estimated_pnl_usdt (양수면 봇이 과소평가)

매칭: 같은 심볼에서 청산시각(exited_at) 기준 ±window초 안의 income 레코드를 모은다.
부분체결로 한 청산에 여러 레코드가 생기므로 합산한다. 이미 다른 거래에 배정된 레코드는
재사용하지 않는다(이중 계상 방지).

실행:
  python scripts/reconcile_realized_pnl.py --hours 24            # 조회만(리포트)
  python scripts/reconcile_realized_pnl.py --hours 24 --write    # 원장에 기록
  python scripts/reconcile_realized_pnl.py --hours 6 --write --json

주의:
- 바이낸스 income history는 조회 구간/건수 제한이 있어 --hours를 너무 크게 잡으면 누락된다.
  기본 24시간, 최대 168시간(7일). 구간을 6시간씩 끊어 페이지네이션한다.
- REST 스로틀 0.4초 (이 저장소는 무스로틀 반복호출로 실제 IP밴을 겪었다).
- --write는 원장을 다시 쓴다. 실행 전 자동으로 .bak 백업을 만든다.
"""
from __future__ import annotations

import argparse
import json
import shutil
import statistics
import sys
import time
from collections import defaultdict
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
LEDGER = ROOT / "logs" / "trade_ledger.jsonl"
THROTTLE_SEC = 0.4
CHUNK_HOURS = 6
MATCH_WINDOW_SEC = 90.0
INCOME_TYPES = ("REALIZED_PNL", "COMMISSION", "FUNDING_FEE")


def fetch_income(ex, start_ms: int, end_ms: int) -> list[dict]:
    """구간을 CHUNK_HOURS씩 끊어 income history를 모은다(건수 제한 회피)."""
    out: list[dict] = []
    cursor = start_ms
    step = CHUNK_HOURS * 3600 * 1000
    while cursor < end_ms:
        chunk_end = min(cursor + step, end_ms)
        try:
            rows = ex.client.futures_income_history(
                startTime=cursor, endTime=chunk_end, limit=1000
            )
        except Exception as e:
            print(f"  income 조회 실패 {datetime.fromtimestamp(cursor/1000):%m-%d %H:%M}: {e}")
            rows = []
        out.extend(rows)
        time.sleep(THROTTLE_SEC)  # IP밴 방지
        cursor = chunk_end
    return out


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


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--hours", type=float, default=24.0, help="최근 N시간 (최대 168)")
    ap.add_argument("--window", type=float, default=MATCH_WINDOW_SEC,
                    help="청산시각 ±N초 안의 income을 같은 거래로 본다")
    ap.add_argument("--write", action="store_true", help="원장에 실제값을 기록(백업 후)")
    ap.add_argument("--json", action="store_true", help="요약을 JSON으로 출력")
    args = ap.parse_args()
    hours = min(args.hours, 168.0)

    from bot.config import Config
    from bot.exchange import Exchange
    ex = Exchange(Config())

    now = time.time()
    start_ts = now - hours * 3600
    print(f"income history 조회: 최근 {hours:.0f}시간 ({CHUNK_HOURS}시간씩 분할, {THROTTLE_SEC}초 스로틀)")
    income = fetch_income(ex, int(start_ts * 1000), int(now * 1000))
    print(f"  수신 {len(income)}건")

    # 심볼별 시각순 정리
    by_symbol: dict[str, list[dict]] = defaultdict(list)
    for r in income:
        if r.get("incomeType") not in INCOME_TYPES:
            continue
        sym = r.get("symbol") or ""
        if not sym:
            continue
        by_symbol[sym].append({
            "ts": float(r["time"]) / 1000.0,
            "type": r["incomeType"],
            "income": float(r["income"]),
        })
    for v in by_symbol.values():
        v.sort(key=lambda x: x["ts"])

    ledger = load_ledger()
    targets = [t for t in ledger if (t.get("exited_at") or 0) >= start_ts]
    targets.sort(key=lambda t: t["exited_at"])
    print(f"원장 대상 거래 {len(targets)}건")

    used: set[int] = set()
    matched = 0
    errors: list[float] = []
    for t in targets:
        sym = t.get("symbol")
        ex_at = t.get("exited_at") or 0
        pool = by_symbol.get(sym, [])
        realized = commission = funding = 0.0
        hit = False
        for i, rec in enumerate(pool):
            key = id(rec)
            if key in used:
                continue
            if abs(rec["ts"] - ex_at) > args.window:
                continue
            used.add(key)
            hit = True
            if rec["type"] == "REALIZED_PNL":
                realized += rec["income"]
            elif rec["type"] == "COMMISSION":
                commission += rec["income"]
            else:
                funding += rec["income"]
        if not hit:
            continue
        matched += 1
        net = realized + commission + funding
        est = t.get("estimated_pnl_usdt") or 0.0
        t["realized_pnl_usdt"] = round(realized, 8)
        t["commission_usdt"] = round(commission, 8)
        t["funding_fee_usdt"] = round(funding, 8)
        t["net_realized_usdt"] = round(net, 8)
        t["pnl_estimate_error"] = round(net - est, 8)
        errors.append(net - est)

    est_sum = sum(t.get("estimated_pnl_usdt") or 0 for t in targets)
    net_sum = sum(t.get("net_realized_usdt") or 0 for t in targets if "net_realized_usdt" in t)
    real_sum = sum(t.get("realized_pnl_usdt") or 0 for t in targets if "realized_pnl_usdt" in t)
    comm_sum = sum(t.get("commission_usdt") or 0 for t in targets if "commission_usdt" in t)

    def winrate(key):
        g = [t for t in targets if key in t or key == "estimated_pnl_usdt"]
        g = [t for t in g if t.get(key) is not None]
        if not g:
            return 0, 0.0
        w = sum(1 for t in g if (t.get(key) or 0) > 0)
        return len(g), w / len(g) * 100

    n_est, wr_est = winrate("estimated_pnl_usdt")
    n_net, wr_net = winrate("net_realized_usdt")

    summary = {
        "hours": hours,
        "ledger_trades": len(targets),
        "matched": matched,
        "unmatched": len(targets) - matched,
        "estimated_sum": round(est_sum, 5),
        "realized_sum": round(real_sum, 5),
        "commission_sum": round(comm_sum, 5),
        "net_realized_sum": round(net_sum, 5),
        "total_error": round(net_sum - sum(
            t.get("estimated_pnl_usdt") or 0 for t in targets if "net_realized_usdt" in t), 5),
        "winrate_estimated": round(wr_est, 2),
        "winrate_net_realized": round(wr_net, 2),
        "sign_flips": sum(
            1 for t in targets if "net_realized_usdt" in t
            and ((t.get("estimated_pnl_usdt") or 0) < 0) != ((t["net_realized_usdt"]) < 0)
        ),
    }

    if args.json:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    else:
        print()
        print(f"매칭 {matched}건 / 미매칭 {len(targets) - matched}건")
        print(f"  봇 추정 합계     {summary['estimated_sum']:+.5f} USDT")
        print(f"  실제 실현손익    {summary['realized_sum']:+.5f} USDT")
        print(f"  실제 수수료      {summary['commission_sum']:+.5f} USDT")
        print(f"  실제 순증감      {summary['net_realized_sum']:+.5f} USDT  <- 잔고에 반영되는 값")
        print(f"  추정 오차 합계   {summary['total_error']:+.5f} USDT (양수면 봇이 손실을 과대기록)")
        print()
        print(f"  승률(추정 기준)  {summary['winrate_estimated']:.1f}%  (n={n_est})")
        print(f"  승률(실제 기준)  {summary['winrate_net_realized']:.1f}%  (n={n_net})")
        print(f"  부호가 뒤바뀐 거래 {summary['sign_flips']}건")
        if errors:
            print()
            print(f"  건당 오차: 평균 {statistics.mean(errors):+.5f} / 중앙값 {statistics.median(errors):+.5f} "
                  f"/ 최대 {max(errors):+.5f} / 최소 {min(errors):+.5f}")
        if summary["unmatched"]:
            print()
            print("  [주의] 미매칭 거래는 income 조회 구간 밖이거나 수동 청산일 수 있다.")

    if args.write:
        backup = LEDGER.with_suffix(".jsonl.bak")
        shutil.copy2(LEDGER, backup)
        print()
        print(f"백업 생성: {backup}")
        with open(LEDGER, "w", encoding="utf-8") as f:
            for t in ledger:
                f.write(json.dumps(t, ensure_ascii=False) + "\n")
        print(f"원장 갱신 완료: {matched}건에 실제값 기록")
        # 콘솔이 cp949라 em dash 등은 UnicodeEncodeError를 낸다. 출력 문자열엔 쓰지 않는다.
        print("  (estimated_pnl_usdt는 보존 - 추정치와 실제값을 나란히 비교할 수 있다)")


if __name__ == "__main__":
    main()
