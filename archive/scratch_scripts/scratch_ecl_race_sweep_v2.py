"""[2026-08-15] EXCHANGE_TRAILING_BACKSTOP_MULTIPLIER 재검증 v2 — 촘촘한 그리드 + 큰 표본.

원본 archive/scratch_scripts/scratch_ecl_race_sweep.py 로직(라이브 실제 주문 공식 그대로:
callback_rate = trail_drawdown_pct * multiplier / leverage, 하드 stop 우선순위, ROE 기준 activation/stop)을
그대로 재사용하고 다음만 확장한다:
  - 그리드: 1.0 / 1.2 / 1.5 / 1.8 / 2.0(현재 라이브) / 2.5 / 3.0
  - 표본: trade_ledger.jsonl 전체에서 origin=bot, exited_at >= 1755133770(원복 이후) 전부
  - armed 서브셋뿐 아니라 "전체 거래" 관점 기대값/승률/손익비도 산출:
      armed 거래는 카운터팩추얼(재구성된 청산가), non-armed 거래는 실측 net pnl(net_pnl 필드,
      없으면 config_snapshot 기반 fee 차감 근사)을 그대로 사용해 포트폴리오 합산.

lookahead 없음(이미 발생한 실 캔들 경로만 사용). REST 호출 0.4s 스로틀. 라이브 코드/설정 변경 없음.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from statistics import mean, median

CACHE_PATH = Path("archive/scratch_scripts/giveback_klines_cache.json")
LEDGER_PATH = Path("logs/trade_ledger.jsonl")
OUT_RAW = Path("archive/scratch_scripts/ecl_race_sweep_v2_raw.json")
OUT_SUMMARY = Path("archive/scratch_scripts/ecl_race_sweep_v2_summary.json")

MULT_VARIANTS = [1.0, 1.2, 1.5, 1.8, 2.0, 2.5, 3.0]
RECOVERY_WINDOW_MIN = 30
CUTOFF_EXITED_AT = 1755133770  # 원복 이후 전체


def load_bot_trades_since(cutoff: float) -> list[dict]:
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
            if r.get("exited_at", 0) < cutoff:
                continue
            rows.append(r)
    rows.sort(key=lambda r: r["entered_at"])
    return rows


def cache_key(symbol: str, entered_at: float, exited_at: float) -> str:
    return f"{symbol}:{int(entered_at)}:{int(exited_at) + RECOVERY_WINDOW_MIN * 60}"


def fetch_missing(trades: list[dict], cache: dict) -> dict:
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
        if (i + 1) % 25 == 0:
            print(f"  진행 {i+1}/{len(missing)}")
            CACHE_PATH.write_text(json.dumps(cache), encoding="utf-8")
        time.sleep(0.4)
    CACHE_PATH.write_text(json.dumps(cache), encoding="utf-8")
    return cache


def simulate_trade(t: dict, klines: list, multiplier: float) -> dict:
    """Reconstruct exchange TRAILING_STOP_MARKET race vs bot hard stop floor.

    Same priority as offline_backtest.exit_decision: hard stop floor checked
    before trailing each candle. Uses each trade's own config_snapshot.
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
    fee_rt = snap.get("fee_rate_roundtrip", 0.001) * 100

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

    callback_rate = trail_pct * multiplier / leverage

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


def actual_net_pnl_pct(t: dict) -> float | None:
    """실측 net pnl%(가격 기준, ROE 아님) — non-armed 거래의 포트폴리오 합산용."""
    entry = t.get("entry_price")
    exit_p = t.get("exit_price")
    side = t.get("side")
    if not entry or not exit_p or not side:
        return None
    snap = t.get("config_snapshot") or {}
    fee_rt = snap.get("fee_rate_roundtrip", 0.001) * 100
    if side == "LONG":
        raw = (exit_p / entry - 1) * 100
    else:
        raw = (entry / exit_p - 1) * 100
    return raw - fee_rt


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

    # 전체 포트폴리오 관점: armed는 카운터팩추얼 net_pnl_pct, non-armed는 실측 net pnl 사용
    portfolio_pnls = []
    for r in rows:
        if r["armed"]:
            portfolio_pnls.append(r["net_pnl_pct"])
        else:
            v = r.get("actual_net_pnl_pct")
            if v is not None:
                portfolio_pnls.append(v)
    n_portfolio = len(portfolio_pnls)
    p_wins = [v for v in portfolio_pnls if v > 0]
    p_losses = [v for v in portfolio_pnls if v <= 0]
    p_win_rate = len(p_wins) / n_portfolio * 100 if n_portfolio else 0
    p_gross_win = sum(p_wins)
    p_gross_loss = abs(sum(p_losses))
    p_pf = p_gross_win / p_gross_loss if p_gross_loss else None
    p_expectancy = sum(portfolio_pnls) / n_portfolio if n_portfolio else 0

    return {
        "n_armed": n_armed, "n_all_trades": all_trades_n,
        "armed_share_of_all_pct": n_armed / all_trades_n * 100 if all_trades_n else 0,
        "trailing_hit_n": len(trailing_hits),
        "trailing_hit_share_of_armed_pct": len(trailing_hits) / n_armed * 100 if n_armed else 0,
        "stop_floor_hit_n": len(stop_floor_hits),
        "censored_n": len(censored),
        "win_rate_armed_pct": win_rate,
        "profit_factor_armed": pf,
        "expectancy_pct_armed": expectancy,
        "median_reaction_window_min": median(r["reaction_window_min"] for r in trailing_hits) if trailing_hits else None,
        "mean_reaction_window_min": mean(r["reaction_window_min"] for r in trailing_hits) if trailing_hits else None,
        # 전체 포트폴리오(armed 카운터팩추얼 + non-armed 실측)
        "n_portfolio": n_portfolio,
        "win_rate_portfolio_pct": p_win_rate,
        "profit_factor_portfolio": p_pf,
        "expectancy_pct_portfolio": p_expectancy,
    }


def main():
    trades = load_bot_trades_since(CUTOFF_EXITED_AT)
    print(f"원복 이후(exited_at>={CUTOFF_EXITED_AT}) bot 거래 {len(trades)}건 로드 "
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
                rows.append({"armed": False, "reason": "no_klines",
                             "actual_net_pnl_pct": actual_net_pnl_pct(t),
                             "symbol": t["symbol"], "entered_at": t["entered_at"]})
                continue
            r = simulate_trade(t, klines, mult)
            r["symbol"] = t["symbol"]
            r["entered_at"] = t["entered_at"]
            r["actual_exit_reason"] = t.get("exit_reason")
            if not r["armed"]:
                r["actual_net_pnl_pct"] = actual_net_pnl_pct(t)
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

    from collections import Counter
    reason_counts = Counter(t.get("exit_reason") for t in trades)
    ecl_n = reason_counts.get("EXTERNAL_CLOSE_LOSS", 0)
    print(f"\n참고: 실제 라이브 EXTERNAL_CLOSE_LOSS {ecl_n}/{len(trades)} "
          f"({ecl_n/len(trades)*100:.1f}%)")


if __name__ == "__main__":
    main()
