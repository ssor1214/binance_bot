"""MTF_MIN_AGREE_RATIO 임계값 검증 - 실제 trade_ledger.jsonl 리플레이 기반.
lookahead 없음: 각 진입 시점(entered_at) 이전에 '이미 닫힌' 캔들만 사용해서
bot/strategy.py의 mtf_trend_alignment와 동일한 EMA20/50 fast/slow 계산으로
agree/total을 재구성한다.

REST 호출은 심볼당 타임프레임당 1회로 최소화하고(endTime로 과거 구간 커버),
호출 사이 sleep으로 스로틀한다. 주문/계좌 API는 전혀 호출하지 않는다(읽기전용
공개 마켓데이터 futures_klines만 사용).
"""
import json
import os
import time
import sys
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from dotenv import load_dotenv
load_dotenv()

from binance.client import Client

API_KEY = os.getenv("BINANCE_API_KEY", "")
API_SECRET = os.getenv("BINANCE_API_SECRET", "")

EMA_FAST = 20
EMA_SLOW = 50
TIMEFRAMES = ["5m", "15m", "1h", "4h"]
LEDGER_PATH = "logs/trade_ledger.jsonl"
THROTTLE_SEC = 0.35

client = Client(API_KEY, API_SECRET)


def load_bot_entries():
    rows = []
    with open(LEDGER_PATH, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except Exception:
                continue
            if d.get("origin") != "bot":
                continue
            if not d.get("entered_at"):
                continue
            rows.append(d)
    rows.sort(key=lambda r: r["entered_at"])
    return rows


def ema(values, span):
    """단순 EWM(adjust=False), pandas와 동일 로직."""
    alpha = 2.0 / (span + 1.0)
    e = values[0]
    for v in values[1:]:
        e = alpha * v + (1 - alpha) * e
    return e


def fetch_klines_for_symbol(symbol, end_time_ms, interval, limit=1500):
    for attempt in range(3):
        try:
            raw = client.futures_klines(symbol=symbol, interval=interval, endTime=end_time_ms, limit=limit)
            return raw
        except Exception as e:
            print(f"  [retry {attempt}] {symbol} {interval}: {e}")
            time.sleep(2.0)
    return []


def main():
    entries = load_bot_entries()
    print(f"총 bot 진입 {len(entries)}건")

    by_symbol = defaultdict(list)
    for e in entries:
        by_symbol[e["symbol"]].append(e)

    symbols = sorted(by_symbol.keys())
    print(f"고유 심볼 {len(symbols)}개, 예상 REST 호출수 {len(symbols) * len(TIMEFRAMES)}건")

    # symbol -> interval -> list of (close_time_ms, close_price)
    kline_cache = {}

    call_count = 0
    for si, symbol in enumerate(symbols):
        max_entry_ts_ms = int(max(e["entered_at"] for e in by_symbol[symbol]) * 1000)
        kline_cache[symbol] = {}
        for interval in TIMEFRAMES:
            raw = fetch_klines_for_symbol(symbol, max_entry_ts_ms, interval)
            call_count += 1
            time.sleep(THROTTLE_SEC)
            closes = []
            for k in raw:
                close_time_ms = int(k[6])
                close_price = float(k[4])
                closes.append((close_time_ms, close_price))
            kline_cache[symbol][interval] = closes
        if (si + 1) % 10 == 0:
            print(f"  진행 {si+1}/{len(symbols)} 심볼, REST 호출 {call_count}건 완료")

    print(f"REST 호출 총 {call_count}건 완료. 이제 각 진입 재구성 중...")

    results = []  # per entry: dict with agree, total, ratio, pnl_pct, entered_at, symbol
    skipped_insufficient = 0
    for e in entries:
        symbol = e["symbol"]
        side = e["side"]
        entered_ms = int(e["entered_at"] * 1000)
        agree = 0
        total = 0
        for interval in TIMEFRAMES:
            closes = kline_cache[symbol].get(interval, [])
            # 진입 시점 이전에 '이미 닫힌' 캔들만 사용 (close_time < entered_ms)
            usable = [c for (ct, c) in closes if ct < entered_ms]
            window = max(EMA_SLOW + 10, 60)
            if len(usable) < EMA_SLOW + 2:
                continue
            usable_win = usable[-window:]
            fast = ema(usable_win, EMA_FAST)
            slow = ema(usable_win, EMA_SLOW)
            total += 1
            if side == "LONG" and fast > slow:
                agree += 1
            elif side == "SHORT" and fast < slow:
                agree += 1
        if total == 0:
            skipped_insufficient += 1
            continue
        ratio = agree / total
        results.append({
            "symbol": symbol,
            "side": side,
            "entered_at": e["entered_at"],
            "agree": agree,
            "total": total,
            "ratio": ratio,
            "pnl_pct": e.get("estimated_pnl_pct"),
            "pnl_usdt": e.get("estimated_pnl_usdt"),
            "held_seconds": e.get("held_seconds"),
        })

    print(f"재구성 완료: {len(results)}건 (판단불가 스킵 {skipped_insufficient}건)")

    out_path = "scratch_mtf_replay_results.json"
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(results, f)
    print(f"결과 저장: {out_path}")


if __name__ == "__main__":
    main()
