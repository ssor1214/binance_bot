"""BTC 단기 모멘텀 역행 필터 가설 검증 (2026-08-14 야간 검증 요청)
- logs/trade_ledger.jsonl 의 origin=bot 진입 전체(최근 ~4.4일)를 사용.
- BTCUSDT 1분봉을 전체 구간에 대해 한 번만 배치 조회(1000개씩) 해서 캐시 -> REST 호출 최소화.
- 각 진입 entered_at 직전 N분(3/5분) BTC 수익률을 계산.
- LONG인데 BTC가 -X% 이상 하락, SHORT인데 BTC가 +X% 이상 상승 -> "역행" 신호로 정의.
- 이 그룹을 스킵했을 때 / 50% 비중 축소했을 때 baseline 대비 승률·순손익이 어떻게 바뀌는지 계산.
"""
import json
import time
from pathlib import Path

from bot.config import Config
from bot.exchange import Exchange

LEDGER_PATH = Path("logs/trade_ledger.jsonl")
CACHE_PATH = Path("scratch_btc_1m_cache.json")


def load_bot_entries():
    rows = []
    with open(LEDGER_PATH, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except Exception:
                continue
            if r.get("origin", "bot") != "bot":
                continue
            rows.append(r)
    rows.sort(key=lambda r: r["entered_at"])
    return rows


def fetch_btc_klines(ex, start_sec, end_sec):
    """1000개(분)씩 배치로 BTCUSDT 1m 클라인을 조회, dict[open_time_sec]->close 로 반환."""
    out = {}
    cur = start_sec
    batch_ms = 1000 * 60  # 1000 minutes per call
    calls = 0
    while cur < end_sec:
        chunk_end = min(cur + batch_ms, end_sec)
        raw = ex.client.futures_klines(
            symbol="BTCUSDT", interval="1m",
            startTime=int(cur * 1000), endTime=int(chunk_end * 1000), limit=1000,
        )
        calls += 1
        for k in raw:
            open_time_sec = k[0] / 1000
            close = float(k[4])
            out[open_time_sec] = close
        if not raw:
            cur = chunk_end
        else:
            last_open = raw[-1][0] / 1000
            cur = last_open + 60
        time.sleep(0.25)  # 레이트리밋 여유 (IP밴 재발방지)
    print(f"BTC 클라인 조회 완료: {calls}회 호출, {len(out)}개 캔들")
    return out


def get_close_at_or_before(btc_map, ts, sorted_times):
    import bisect
    idx = bisect.bisect_right(sorted_times, ts) - 1
    if idx < 0:
        return None
    return btc_map[sorted_times[idx]]


def main():
    cfg = Config()
    ex = Exchange(cfg)

    entries = load_bot_entries()
    print(f"origin=bot 진입 {len(entries)}건 로드")

    min_ts = min(r["entered_at"] for r in entries) - 600
    max_ts = max(r["entered_at"] for r in entries) + 60

    if CACHE_PATH.exists():
        btc_map = {float(k): v for k, v in json.loads(CACHE_PATH.read_text()).items()}
        print(f"캐시 사용: {len(btc_map)}개 캔들")
    else:
        btc_map = fetch_btc_klines(ex, min_ts, max_ts)
        CACHE_PATH.write_text(json.dumps(btc_map))

    sorted_times = sorted(btc_map.keys())

    # 각 진입건에 대해 3분/5분 BTC 모멘텀 계산
    enriched = []
    for r in entries:
        entered_at = r["entered_at"]
        side = r["side"]
        pnl = r.get("estimated_pnl_usdt", 0) or 0
        c0 = get_close_at_or_before(btc_map, entered_at, sorted_times)
        results = {}
        for mins in (3, 5):
            c_past = get_close_at_or_before(btc_map, entered_at - mins * 60, sorted_times)
            if c0 is None or c_past is None or c_past == 0:
                results[mins] = None
            else:
                results[mins] = (c0 / c_past - 1) * 100
        enriched.append({
            "symbol": r["symbol"], "side": side, "pnl": pnl,
            "entered_at": entered_at, "mom3": results[3], "mom5": results[5],
        })

    valid = [e for e in enriched if e["mom3"] is not None and e["mom5"] is not None]
    print(f"BTC 모멘텀 계산 가능 {len(valid)}/{len(enriched)}건")

    def baseline_stats(rows):
        n = len(rows)
        wins = [r for r in rows if r["pnl"] > 0]
        losses = [r for r in rows if r["pnl"] <= 0]
        win_rate = len(wins) / n * 100 if n else 0
        net_pnl = sum(r["pnl"] for r in rows)
        avg_win = sum(r["pnl"] for r in wins) / len(wins) if wins else 0
        avg_loss = sum(r["pnl"] for r in losses) / len(losses) if losses else 0
        pf = (sum(r["pnl"] for r in wins) / abs(sum(r["pnl"] for r in losses))) if losses and sum(r["pnl"] for r in losses) != 0 else float("inf")
        return dict(n=n, win_rate=win_rate, net_pnl=net_pnl, avg_win=avg_win, avg_loss=avg_loss, pf=pf)

    print("\n=== BASELINE (필터 없음, 전체 origin=bot 진입) ===")
    base = baseline_stats(valid)
    print(f"n={base['n']} win_rate={base['win_rate']:.1f}% net_pnl={base['net_pnl']:+.3f} "
          f"avg_win={base['avg_win']:+.3f} avg_loss={base['avg_loss']:+.3f} pf={base['pf']:.2f}")

    thresholds = [-0.10, -0.15, -0.20]
    windows = [3, 5]

    print("\n=== 역행 그룹 자체 분석 (가설: 역행그룹이 baseline보다 나쁜가?) ===")
    for mins in windows:
        key = f"mom{mins}"
        for th in thresholds:
            flagged = []
            for r in valid:
                mom = r[key]
                if r["side"] == "LONG" and mom <= th:
                    flagged.append(r)
                elif r["side"] == "SHORT" and mom >= -th:
                    flagged.append(r)
            not_flagged = [r for r in valid if r not in flagged]
            if not flagged:
                print(f"[{mins}분, th={th}%] 걸린 건수 0 — 평가 불가")
                continue
            fstats = baseline_stats(flagged)
            nstats = baseline_stats(not_flagged)
            pct_flagged = len(flagged) / len(valid) * 100
            print(f"[{mins}분, th={th}%] 걸림 {len(flagged)}건({pct_flagged:.1f}%) "
                  f"승률={fstats['win_rate']:.1f}% net={fstats['net_pnl']:+.3f} pf={fstats['pf']:.2f} "
                  f"  |  안걸림 {len(not_flagged)}건 승률={nstats['win_rate']:.1f}% net={nstats['net_pnl']:+.3f}")

    print("\n=== 스킵 시뮬레이션 (걸린 건 제거) vs 50% 축소 시뮬레이션 ===")
    for mins in windows:
        key = f"mom{mins}"
        for th in thresholds:
            flagged_mask = []
            for r in valid:
                mom = r[key]
                is_flagged = (r["side"] == "LONG" and mom <= th) or (r["side"] == "SHORT" and mom >= -th)
                flagged_mask.append(is_flagged)

            skip_rows = [r for r, f in zip(valid, flagged_mask) if not f]
            skip_stats = baseline_stats(skip_rows)

            half_rows = []
            for r, f in zip(valid, flagged_mask):
                rr = dict(r)
                if f:
                    rr["pnl"] = rr["pnl"] * 0.5
                half_rows.append(rr)
            half_stats = baseline_stats(half_rows)

            pct_flagged = sum(flagged_mask) / len(valid) * 100
            print(f"[{mins}분, th={th}%] 걸림비율={pct_flagged:.1f}%")
            print(f"  SKIP : n={skip_stats['n']} win_rate={skip_stats['win_rate']:.1f}% "
                  f"net={skip_stats['net_pnl']:+.3f} (baseline net={base['net_pnl']:+.3f}, "
                  f"delta={skip_stats['net_pnl']-base['net_pnl']:+.3f})")
            print(f"  HALF : n={half_stats['n']} win_rate={half_stats['win_rate']:.1f}% "
                  f"net={half_stats['net_pnl']:+.3f} (delta={half_stats['net_pnl']-base['net_pnl']:+.3f})")

    # 오늘 09:26~09:36 사고 구간 상세 출력
    print("\n=== 2026-08-14 09:26~09:36 사고구간 4건 상세 ===")
    for r in valid:
        lt = time.localtime(r["entered_at"])
        if time.strftime("%Y-%m-%d", lt) == "2026-08-14" and "09:2" <= time.strftime("%H:%M", lt) <= "09:40":
            print(f"{time.strftime('%H:%M:%S', lt)} {r['symbol']} {r['side']} pnl={r['pnl']:+.3f} "
                  f"mom3={r['mom3']:+.3f}% mom5={r['mom5']:+.3f}%")


if __name__ == "__main__":
    main()
