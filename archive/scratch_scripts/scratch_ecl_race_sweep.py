"""[2026-08-15] EXTERNAL_CLOSE_LOSS 대응: 거래소측 트레일링 백스탑 폭 vs 봇 개입기회 검증.

스윕 1: EXCHANGE_TRAILING_BACKSTOP_MULTIPLIER (1.5 / 2.0(현재) / 2.5 / 3.0)
  - 실거래 300건의 실제 1분봉 경로 위에서, "거래소에 실제로 걸리는 TRAILING_STOP_MARKET"을
    callback_rate = trail_drawdown_pct(각 거래의 config_snapshot 실측값) * multiplier / leverage
    로 재구성해 armed(=take_profit_min ROE 도달) 이후 러닝피크 대비 되돌림 트리거를 재현한다.
  - 봇 자체 하드 손절(stop_loss_pct)이 트레일링보다 먼저 걸리면 그쪽 우선(같은 순서로 실제
    exit_decision과 동일 로직: stop 우선 확인).
  - armed 안 된(트레일링 자체가 걸릴 일이 없었던) 거래는 실제 라이브 청산가/손익을 그대로 사용
    (multiplier 변경이 영향을 줄 수 없는 구간이므로 카운터팩추얼 대상에서 제외, 포트폴리오
    합산에는 실측값 포함).

스윕 2: POSITION_CHECK_INTERVAL_SEC (2 / 3 / 5(현재) / 8초)
  - 1분봉 해상도로는 초단위 체크주기 차이를 직접 재현할 수 없다(같은 1분봉 안에서 어느
    시점에 몇 초 간격으로 체크했는지는 데이터에 없음). 대신 "피크 형성 후 실제로 거래소
    트레일링이 발동하기까지 걸린 시간(반응 가능 창)"을 계측해, 2~8초 후보가 모두 이 창에
    비해 훨씬 촘촘한지(=체크주기 자체는 병목이 아닐 가능성)를 정량적으로 보여준다.
  - 결론은 정성적 트레이드오프(짧을수록 API 호출량 증가)를 곁들여 제시한다.

lookahead 없음: 이미 발생한 실제 캔들 경로만 사용. 원본 exit_decision 로직을 그대로 재사용
(stop 우선순위 등 offline_backtest.exit_decision과 동일 순서). 라이브 코드/설정 변경 없음.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from statistics import mean, median

CACHE_PATH = Path("archive/scratch_scripts/giveback_klines_cache.json")
LEDGER_PATH = Path("logs/trade_ledger.jsonl")
OUT_RAW = Path("archive/scratch_scripts/ecl_race_sweep_raw.json")
OUT_SUMMARY = Path("archive/scratch_scripts/ecl_race_sweep_summary.json")

MULT_VARIANTS = [1.5, 2.0, 2.5, 3.0]
RECOVERY_WINDOW_MIN = 30  # 청산 이후 여유 관측창(censored 판정용)


def load_last_n_bot_trades(n: int = 300) -> list[dict]:
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
            if r.get("origin") != "bot":
                continue
            rows.append(r)
    rows.sort(key=lambda r: r["entered_at"])
    return rows[-n:]


def cache_key(symbol: str, entered_at: float, exited_at: float) -> str:
    return f"{symbol}:{int(entered_at)}:{int(exited_at) + RECOVERY_WINDOW_MIN * 60}"


def fetch_missing(trades: list[dict], cache: dict) -> dict:
    """Fetch only klines not already present in cache, throttled >=0.4s/call."""
    missing = []
    for t in trades:
        key = cache_key(t["symbol"], t["entered_at"], t["exited_at"])
        if key not in cache:
            missing.append((t, key))
    print(f"cache 히트: {len(trades) - len(missing)}/{len(trades)}건, 신규 REST 필요: {len(missing)}건")
    if not missing:
        return cache
    from bot.config import Config
    from bot.exchange import Exchange
    cfg = Config()
    ex = Exchange(cfg)
    for i, (t, key) in enumerate(missing):
        try:
            raw = ex.client.futures_klines(
                symbol=t["symbol"], interval="1m",
                startTime=int((t["entered_at"] - 30) * 1000),
                endTime=int((t["exited_at"] + RECOVERY_WINDOW_MIN * 60) * 1000),
                limit=1000,
            )
            cache[key] = raw
        except Exception as e:
            print(f"  [skip] {t['symbol']} fetch 실패: {e}")
        if (i + 1) % 10 == 0:
            print(f"  진행 {i+1}/{len(missing)}")
        time.sleep(0.4)
    CACHE_PATH.write_text(json.dumps(cache), encoding="utf-8")
    return cache


def simulate_trade(t: dict, klines: list, multiplier: float) -> dict:
    """Reconstruct exchange TRAILING_STOP_MARKET race vs bot hard stop floor.

    Mirrors offline_backtest.exit_decision priority: hard stop floor checked
    before trailing on each candle. Uses each trade's own config_snapshot
    (trail_drawdown_pct, stop_loss_pct, take_profit_min, leverage) — no
    fixed assumptions substituted for missing fields except documented
    live defaults (bot/config.py).
    """
    side = t["side"]
    entry = t["entry_price"]
    entered_at = t["entered_at"]
    exited_at = t["exited_at"]
    snap = t.get("config_snapshot") or {}
    leverage = t.get("leverage") or 4
    trail_pct = snap.get("trail_drawdown_pct", 0.5)
    take_profit_min = snap.get("short_take_profit_min" if side == "SHORT" else "take_profit_min",
                                0.3 if side == "SHORT" else 3.0)
    stop_loss_pct = snap.get("stop_loss_pct", 5.0)
    fee_rt = snap.get("fee_rate_roundtrip", 0.001) * 100  # raw price %, roundtrip

    # [정정] 실제 거래소 STOP_MARKET은 bot/main.py:2320-2324(compute_stop_loss_pct 사용부)
    # 기준으로 ROE% 기준(stop_loss_pct/leverage)이다. bot/strategy.py의 stop_loss_price()는
    # 별도 폴백/레거시 경로라 실제 거래소 주문 폭과 다르므로 여기서는 쓰지 않는다.
    if side == "LONG":
        activation_price = entry * (1 + take_profit_min / 100 / leverage)
        stop_price = entry * (1 - stop_loss_pct / 100 / leverage)
    else:
        activation_price = entry * (1 - take_profit_min / 100 / leverage)
        stop_price = entry * (1 + stop_loss_pct / 100 / leverage)

    cand = [k for k in klines if k[0] >= (entered_at - 1) * 1000]
    if not cand:
        return {"armed": False, "reason": "no_klines"}

    real_cand = [k for k in cand if k[0] <= (exited_at + 1) * 1000]
    armed_idx = None
    for i, k in enumerate(real_cand):
        hi, lo = float(k[2]), float(k[3])
        if side == "LONG" and hi >= activation_price:
            armed_idx = i
            break
        if side == "SHORT" and lo <= activation_price:
            armed_idx = i
            break
    if armed_idx is None:
        return {"armed": False, "reason": "never_armed"}

    callback_rate = trail_pct * multiplier / leverage  # matches bot/main.py formula exactly

    peak = activation_price
    peak_ts = cand[armed_idx][0]
    exit_price = exit_kind = exit_ts = None
    for i in range(armed_idx, len(cand)):
        k = cand[i]
        hi, lo = float(k[2]), float(k[3])
        if side == "LONG":
            if hi > peak:
                peak, peak_ts = hi, k[0]
            trail_trigger = peak * (1 - callback_rate / 100)
            if lo <= stop_price:
                exit_price, exit_kind, exit_ts = stop_price, "STOP_LOSS_FLOOR", k[0]
                break
            if lo <= trail_trigger:
                exit_price, exit_kind, exit_ts = trail_trigger, "TRAILING", k[0]
                break
        else:
            if lo < peak:
                peak, peak_ts = lo, k[0]
            trail_trigger = peak * (1 + callback_rate / 100)
            if hi >= stop_price:
                exit_price, exit_kind, exit_ts = stop_price, "STOP_LOSS_FLOOR", k[0]
                break
            if hi >= trail_trigger:
                exit_price, exit_kind, exit_ts = trail_trigger, "TRAILING", k[0]
                break

    censored = False
    if exit_price is None:
        exit_price = float(cand[-1][4])
        exit_kind = "CENSORED"
        exit_ts = cand[-1][0]
        censored = True

    if side == "LONG":
        raw_pnl_pct = (exit_price / entry - 1) * 100
    else:
        raw_pnl_pct = (entry / exit_price - 1) * 100
    net_pnl_pct = raw_pnl_pct - fee_rt

    reaction_window_min = (exit_ts - peak_ts) / 60000.0

    return {
        "armed": True, "exit_kind": exit_kind, "exit_price": exit_price,
        "net_pnl_pct": net_pnl_pct, "censored": censored,
        "held_min": (exit_ts / 1000 - entered_at) / 60,
        "reaction_window_min": reaction_window_min,
        "leverage": leverage,
    }


def summarize(rows: list[dict], all_trades_n: int) -> dict:
    armed = [r for r in rows if r["armed"]]
    n_armed = len(armed)
    trailing_hits = [r for r in armed if r["exit_kind"] == "TRAILING"]
    stop_floor_hits = [r for r in armed if r["exit_kind"] == "STOP_LOSS_FLOOR"]
    censored = [r for r in armed if r["censored"]]
    wins = [r for r in armed if r["net_pnl_pct"] > 0]
    losses = [r for r in armed if r["net_pnl_pct"] <= 0]
    win_rate = len(wins) / n_armed * 100 if n_armed else 0
    gross_win = sum(r["net_pnl_pct"] for r in wins)
    gross_loss = abs(sum(r["net_pnl_pct"] for r in losses))
    pf = gross_win / gross_loss if gross_loss else None
    expectancy = sum(r["net_pnl_pct"] for r in armed) / n_armed if n_armed else 0
    trailing_loss_pnls = [r["net_pnl_pct"] for r in trailing_hits if r["net_pnl_pct"] <= 0]
    return {
        "n_armed": n_armed, "n_all_trades": all_trades_n,
        "armed_share_of_all_pct": n_armed / all_trades_n * 100 if all_trades_n else 0,
        "trailing_hit_n": len(trailing_hits),
        "trailing_hit_share_of_armed_pct": len(trailing_hits) / n_armed * 100 if n_armed else 0,
        "trailing_hit_share_of_all_pct": len(trailing_hits) / all_trades_n * 100 if all_trades_n else 0,
        "stop_floor_hit_n": len(stop_floor_hits),
        "censored_n": len(censored),
        "win_rate_armed_pct": win_rate,
        "profit_factor_armed": pf,
        "expectancy_pct_armed": expectancy,
        "avg_trailing_loss_pct": mean(trailing_loss_pnls) if trailing_loss_pnls else None,
        "median_reaction_window_min": median(r["reaction_window_min"] for r in trailing_hits) if trailing_hits else None,
        "mean_reaction_window_min": mean(r["reaction_window_min"] for r in trailing_hits) if trailing_hits else None,
    }


def main():
    trades = load_last_n_bot_trades(300)
    print(f"최근 bot 거래 {len(trades)}건 로드 "
          f"(기간 {trades[0]['entered_at']:.0f} ~ {trades[-1]['entered_at']:.0f})")

    cache = {}
    if CACHE_PATH.exists():
        cache = json.loads(CACHE_PATH.read_text(encoding="utf-8"))
    cache = fetch_missing(trades, cache)

    raw_out = {}
    summary = {}
    for mult in MULT_VARIANTS:
        rows = []
        for t in trades:
            key = cache_key(t["symbol"], t["entered_at"], t["exited_at"])
            klines = cache.get(key)
            if not klines:
                rows.append({"armed": False, "reason": "no_klines"})
                continue
            r = simulate_trade(t, klines, mult)
            r["symbol"] = t["symbol"]
            r["entered_at"] = t["entered_at"]
            r["actual_exit_reason"] = t.get("exit_reason")
            rows.append(r)
        raw_out[str(mult)] = rows
        s = summarize(rows, len(trades))
        summary[str(mult)] = s
        print(f"\n=== multiplier={mult} ===")
        for k, v in s.items():
            print(f"  {k}: {v}")

    OUT_RAW.write_text(json.dumps(raw_out, ensure_ascii=False), encoding="utf-8")
    OUT_SUMMARY.write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\n저장: {OUT_RAW}, {OUT_SUMMARY}")

    # 실제 라이브 EXTERNAL_CLOSE_LOSS 비중(참고)
    from collections import Counter
    reason_counts = Counter(t.get("exit_reason") for t in trades)
    ecl_n = reason_counts.get("EXTERNAL_CLOSE_LOSS", 0)
    print(f"\n참고: 실제 라이브 EXTERNAL_CLOSE_LOSS {ecl_n}/{len(trades)} "
          f"({ecl_n/len(trades)*100:.1f}%)")


if __name__ == "__main__":
    main()
