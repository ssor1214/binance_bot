"""[2026-08-19] 라이브 early_entry_spike 라벨을 공개 아카이브 체결데이터로 독립 검증한다.

**배경**
원장 772건에서 early_entry_spike=True 거래가 승률 74.5%/+1.887, False가 65.8%/-15.516으로
갈렸고 walk-forward 양쪽 창에서 모두 플러스였다. 이 태그를 비중 가중에 쓰려면 먼저
"태그 자체가 정확한가"를 확인해야 한다. 라이브 태그는 ws_trade_worker가 계산해 상태파일로
넘긴 값이라, 워커 지연/재시작/구독누락이 있으면 조용히 틀릴 수 있다.

**방법**
data.binance.vision(정적 CDN, API키/서명 불필요, fapi weight 미적용 - 2026-08-11 IP밴
사고 경로와 무관)에서 aggTrades를 받아, 실제 프로덕션 함수 detect_volume_spike()를
그대로 호출해 진입 시점의 스파이크 여부를 복원하고 원장 라벨과 비교한다. 근사식을 새로
쓰지 않는 이유는 재현이 아니라 검증이 목적이기 때문이다.

**알려진 비교 한계 (해석 시 반드시 감안)**
1) 구독 상한: SPIKE_ENTRY_MAX_SYMBOLS=20 이라 유동성 상위 20개만 워커가 구독한다. 그
   밖의 심볼은 실제 스파이크가 있어도 라이브 라벨이 항상 False다 -> 이 방향의 불일치
   (live=False / archive=True)는 "틀린 라벨"이 아니라 "미구독"일 수 있다.
2) 평가 시점 차이: 라이브는 스캔 루프에서 신호확정 시 판정하고, 원장의 entered_at은
   체결 완료 시각이다. 지정가 대기(LIMIT_ENTRY_WAIT_SEC)와 스캔 소요를 합치면 수초~수십초
   앞선다. 그래서 여러 오프셋으로 평가해 일치율이 가장 높은 지점을 함께 보고한다.
3) 아카이브 게시 지연: 최근 1~2일치는 아직 없다.
4) UTC 자정 경계: baseline 창(300초)이 전날로 넘어가는 거래는 전날 파일이 없으면 제외한다.
"""
import argparse
import bisect
import calendar
import io
import json
import os
import sys
import time
import zipfile
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bot.config import Config
from bot.ws_trade_client import TradeTick, TradeTickCache, detect_volume_spike

ARCHIVE_DIR = os.path.join("archive", "binance_vision", "aggTrades")


def load_ledger(path):
    out = []
    with io.open(path, encoding="utf-8") as fh:
        for line in fh:
            try:
                t = json.loads(line)
            except Exception:
                continue
            if t.get("origin") != "bot":
                continue
            if t.get("early_entry_spike") is None or not t.get("entered_at"):
                continue
            out.append(t)
    return out


def extract_ticks(zip_path, symbol, windows_ms):
    """windows_ms: 정렬/병합된 [(lo_ms, hi_ms), ...]. 해당 구간 체결만 뽑는다(메모리 절약).

    aggTrades CSV 컬럼:
      agg_trade_id, price, quantity, first_trade_id, last_trade_id, transact_time, is_buyer_maker
    """
    if not windows_ms:
        return []
    lo_all = windows_ms[0][0]
    hi_all = windows_ms[-1][1]
    ticks = []
    with zipfile.ZipFile(zip_path) as z:
        name = z.infolist()[0].filename
        with z.open(name) as fh:
            idx = 0
            first = True
            for raw in io.TextIOWrapper(fh, encoding="utf-8"):
                if first:
                    first = False
                    if raw.startswith("agg_trade_id"):
                        continue
                parts = raw.rstrip("\n").split(",")
                if len(parts) < 7:
                    continue
                try:
                    ts = int(parts[5])
                except ValueError:
                    continue
                if ts < lo_all:
                    continue
                if ts > hi_all:
                    break
                while idx < len(windows_ms) and ts > windows_ms[idx][1]:
                    idx += 1
                if idx >= len(windows_ms):
                    break
                if ts < windows_ms[idx][0]:
                    continue
                try:
                    price = float(parts[1])
                    qty = float(parts[2])
                except ValueError:
                    continue
                ticks.append(TradeTick(symbol=symbol, price=price, quantity=qty,
                                       event_time_ms=ts, trade_time_ms=ts,
                                       is_buyer_maker=(parts[6].strip().lower() == "true")))
    return ticks


def merge_windows(windows):
    merged = []
    for lo, hi in sorted(windows):
        if merged and lo <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], hi))
        else:
            merged.append((lo, hi))
    return merged


def scan_archive(archive_dir):
    avail = defaultdict(set)
    if not os.path.isdir(archive_dir):
        return avail
    for sym in sorted(os.listdir(archive_dir)):
        d = os.path.join(archive_dir, sym)
        if not os.path.isdir(d):
            continue
        for f in os.listdir(d):
            if f.endswith(".zip") and "-aggTrades-" in f:
                avail[sym].add(f.split("-aggTrades-")[1][:-4])
    return avail


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ledger", default=os.path.join("logs", "trade_ledger.jsonl"))
    ap.add_argument("--archive", default=ARCHIVE_DIR)
    ap.add_argument("--offsets", default="0,-5,-10,-20,-30,-45",
                    help="entered_at 기준 평가시점 오프셋(초). 라이브는 체결보다 앞서 판정한다.")
    args = ap.parse_args()

    cfg = Config()
    baseline = float(cfg.spike_entry_baseline_sec)
    offsets = [float(x) for x in args.offsets.split(",")]

    avail = scan_archive(args.archive)
    if not avail:
        print("아카이브가 비어 있다. scripts/fetch_binance_archive.py 를 먼저 실행할 것.")
        return 1
    print("아카이브 보유: " + ", ".join("%s(%s)" % (s, ",".join(sorted(v))) for s, v in sorted(avail.items())))
    print("스파이크 설정: window=%.0fs baseline=%.0fs multiplier=%.1f  구독상한=%d심볼"
          % (cfg.spike_entry_window_sec, baseline, cfg.spike_entry_multiplier, cfg.spike_entry_max_symbols))

    trades = load_ledger(args.ledger)
    targets = []
    for t in trades:
        day = time.strftime("%Y-%m-%d", time.gmtime(t["entered_at"]))
        if t["symbol"] in avail and day in avail[t["symbol"]]:
            targets.append(t)
    print("대상 거래 %d건 (라벨 보유 전체 %d건 중)" % (len(targets), len(trades)))
    if not targets:
        return 1

    by_key = defaultdict(list)
    for t in targets:
        by_key[(t["symbol"], time.strftime("%Y-%m-%d", time.gmtime(t["entered_at"])))].append(t)

    results = []
    skipped_midnight = 0
    for (symbol, day), group in sorted(by_key.items()):
        zp = os.path.join(args.archive, symbol, "%s-aggTrades-%s.zip" % (symbol, day))
        day_start_ms = calendar.timegm(time.strptime(day, "%Y-%m-%d")) * 1000
        evals = []
        for t in group:
            base_ms = int(t["entered_at"] * 1000)
            lo = base_ms + int(min(offsets) * 1000) - int(baseline * 1000)
            if lo < day_start_ms:
                skipped_midnight += 1
                continue
            evals.append((t, base_ms, lo, base_ms))
        if not evals:
            print("  %s %s: 자정경계로 전건 제외" % (symbol, day))
            continue
        merged = merge_windows([(lo, hi) for _, _, lo, hi in evals])
        t0 = time.time()
        ticks = extract_ticks(zp, symbol, merged)
        # [2026-08-19 하네스 버그 수정 - lookahead] TradeTickCache.get_recent()는 하한
        # (trade_time >= now - lookback)만 보고 상한이 없다. 라이브에서는 캐시에 과거 틱만
        # 들어오므로 문제가 없지만, 백테스트에서 하루치를 한 캐시에 다 넣으면 "최근 10초"가
        # 평가시점 이후의 미래 체결까지 삼켜서 ratio가 baseline 창 개수(30)에 붙어버린다.
        # 실제로 첫 실행에서 96%가 스파이크로 나왔고 ratio 최대가 29.88이었다(=30 근사).
        # 그래서 평가 시점마다 now_ms 이하 틱만 잘라 새 캐시를 만든다.
        tick_times = [tk.trade_time_ms for tk in ticks]
        print("  %s %s: 거래 %d건 / 창 %d개 / 체결 %d개 (%.1fs)"
              % (symbol, day, len(evals), len(merged), len(ticks), time.time() - t0))
        for t, base_ms, _, _ in evals:
            row = {"symbol": symbol, "day": day, "entered_at": t["entered_at"],
                   "live": bool(t["early_entry_spike"]),
                   "net": t.get("net_realized_usdt")}
            for off in offsets:
                now_ms = base_ms + int(off * 1000)
                lo_i = bisect.bisect_left(tick_times, now_ms - int(baseline * 1000))
                hi_i = bisect.bisect_right(tick_times, now_ms)
                sub = ticks[lo_i:hi_i]
                cache = TradeTickCache(max_ticks_per_symbol=max(len(sub), 1))
                for tk in sub:
                    cache.append(tk)
                r = detect_volume_spike(cache, symbol,
                                        spike_multiplier=cfg.spike_entry_multiplier,
                                        spike_window_sec=cfg.spike_entry_window_sec,
                                        baseline_window_sec=baseline,
                                        now_ms=now_ms)
                row["off%+d" % int(off)] = bool(r["is_spike"])
                row["ratio%+d" % int(off)] = round(float(r["ratio"]), 3)
            results.append(row)

    if not results:
        print("평가된 거래가 없다.")
        return 1
    if skipped_midnight:
        print("자정경계 제외 %d건" % skipped_midnight)

    live_true = sum(1 for r in results if r["live"])
    print("\n=== 라벨 일치율 (n=%d, 라이브 True %d건 / False %d건) ==="
          % (len(results), live_true, len(results) - live_true))
    print("%-8s %9s %14s %14s %10s" % ("오프셋", "일치율", "live T/arch F", "live F/arch T", "arch True"))
    for off in offsets:
        k = "off%+d" % int(off)
        agree = sum(1 for r in results if r["live"] == r[k])
        tf = sum(1 for r in results if r["live"] and not r[k])
        ft = sum(1 for r in results if (not r["live"]) and r[k])
        at = sum(1 for r in results if r[k])
        print("%-8s %8.1f%% %14d %14d %10d" % ("%+ds" % int(off), 100.0 * agree / len(results), tf, ft, at))

    print("\n=== 심볼별 (오프셋 0 기준) ===")
    per = defaultdict(lambda: [0, 0, 0, 0])  # n, agree, liveT, archT
    for r in results:
        p = per[r["symbol"]]
        p[0] += 1
        p[1] += 1 if r["live"] == r["off+0"] else 0
        p[2] += 1 if r["live"] else 0
        p[3] += 1 if r["off+0"] else 0
    for sym, (n, ag, lt, at) in sorted(per.items()):
        print("  %-13s n=%3d 일치 %5.1f%%  live True %2d  archive True %2d" % (sym, n, 100.0 * ag / n, lt, at))

    out = os.path.join("archive", "scratch_scripts", "spike_label_validation.json")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with io.open(out, "w", encoding="utf-8") as fh:
        json.dump(results, fh, ensure_ascii=False, indent=1)
    print("\n상세 저장: %s" % out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
