"""[분석전용] TP/SL 유예(defer) 재검증 - 사후 가격경로 시뮬레이션.
scratch_tp_sl_defer_fetch.py가 만든 로컬 klines 캐시만 사용, 추가 API 호출 없음.
bot/strategy.py의 실제 함수(pnl_pct, detect_reversal, is_momentum_continuing)를 그대로
재사용해 라이브 로직과 판단 기준을 일치시킨다. lookahead 방지: 각 시점의 판단은 그
시점까지의 데이터만 사용(add_indicators는 causal transform이라 이후 데이터가 껴있어도
과거 행 값에 영향 없음 - 단 우리는 안전하게 매 시점마다 그 시점까지 슬라이스해서 계산).
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import pandas as pd

from bot.config import Config
from bot.indicators import add_indicators
from bot.strategy import pnl_pct, detect_reversal

LEDGER = Path("logs/trade_ledger.jsonl")
CACHE_PATH = Path("archive/scratch_scripts/scratch_tp_sl_defer_recheck_klines_cache.json")
OUT_PATH = Path("archive/scratch_scripts/scratch_tp_sl_defer_recheck_results.json")

TP_REASONS = {"TAKE_PROFIT", "TAKE_PROFIT_MOMENTUM_LOCK"}
SL_REASONS = {"STOP_LOSS", "EXTERNAL_CLOSE_LOSS"}

cfg = Config()


def load_ledger():
    recs = []
    with open(LEDGER, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                recs.append(json.loads(line))
    return recs


def is_ascii_symbol(sym):
    return all(ord(c) < 128 for c in sym)


def klines_to_df(kl):
    cols = ["open_time", "open", "high", "low", "close", "volume", "close_time",
            "quote_volume", "trades", "taker_buy_base", "taker_buy_quote", "ignore"]
    df = pd.DataFrame(kl, columns=cols)
    for c in ["open", "high", "low", "close", "volume", "taker_buy_base"]:
        df[c] = df[c].astype(float)
    df["open_time"] = df["open_time"].astype(float) / 1000.0  # -> seconds
    df = df.sort_values("open_time").drop_duplicates("open_time").reset_index(drop=True)
    return df


def build_symbol_df(cache, symbol):
    """Merge all cached windows for a symbol into one sorted, deduped 1m-kline DataFrame."""
    sym_cache = cache.get(symbol, {})
    all_kl = []
    for kl in sym_cache.values():
        all_kl.extend(kl)
    if not all_kl:
        return None
    return klines_to_df(all_kl)


def price_at(df, t):
    """Interpolated close price at epoch-seconds t, using the 1m candle containing t.
    If t falls in a gap (no candle covers it, e.g. thin liquidity/API gap), fall back to
    nearest available candle close (still causal - never uses a candle starting after t
    except as last-resort fallback when no covering/prior candle exists)."""
    # exact/covering candle
    row = df[(df["open_time"] <= t) & (df["open_time"] + 60 > t)]
    if not row.empty:
        r = row.iloc[0]
        frac = (t - r["open_time"]) / 60.0
        return r["open"] + (r["close"] - r["open"]) * frac
    # [gap fix] exchange/API data can have short gaps (e.g. thin liquidity minute skipped).
    # Reconstructing a *historical* price at t from its two nearest real neighbours is not
    # lookahead bias (we are not making a decision using future info - we are just
    # reconstructing what the price actually was for retrospective comparison). A naive
    # "always use last known price, however stale" fallback caused a severe bug (APRUSDT
    # case: stale price held for 30s then a real candle 43s later showed the real level,
    # producing a fabricated ~-885 ROE swing). Bound both sides by max_gap and prefer
    # interpolation between the two nearest real candles when both are within range.
    max_gap = 65.0
    prior = df[df["open_time"] + 60 <= t]
    nxt = df[df["open_time"] > t]
    prior_gap = (t - (prior.iloc[-1]["open_time"] + 60)) if not prior.empty else None
    nxt_gap = (nxt.iloc[0]["open_time"] - t) if not nxt.empty else None
    if prior_gap is not None and prior_gap > max_gap:
        prior = None
    if nxt_gap is not None and nxt_gap > max_gap:
        nxt = None
    if prior is not None and not prior.empty and nxt is not None and not nxt.empty:
        p_close_t = prior.iloc[-1]["open_time"] + 60
        p_close = prior.iloc[-1]["close"]
        n_open_t = nxt.iloc[0]["open_time"]
        n_open = nxt.iloc[0]["open"]
        span = n_open_t - p_close_t
        if span <= 0:
            return n_open
        frac = (t - p_close_t) / span
        return p_close + (n_open - p_close) * frac
    if prior is not None and not prior.empty:
        return prior.iloc[-1]["close"]
    if nxt is not None and not nxt.empty:
        return nxt.iloc[0]["open"]
    return None


def df_up_to(df, t):
    """Rows with open_time <= t (causal slice) for indicator computation."""
    return df[df["open_time"] <= t].reset_index(drop=True)


def opposite(side):
    return "SHORT" if side == "LONG" else "LONG"


def simulate_tp(rec, df):
    entry_price = rec["entry_price"]
    side = rec["side"]
    leverage = rec["leverage"]
    exited_at = rec["exited_at"]
    exit_price = rec["exit_price"]

    baseline_roe = pnl_pct(entry_price, exit_price, side) * leverage

    result = {
        "symbol": rec["symbol"], "side": side, "exited_at": exited_at,
        "baseline_roe": baseline_roe, "exit_reason": rec["exit_reason"],
        "held_seconds": rec["held_seconds"],
    }

    horizons = [30, 60, 90, 120]
    for h in horizons:
        p = price_at(df, exited_at + h)
        if p is None:
            result[f"naive_roe_{h}s"] = None
            continue
        roe_h = pnl_pct(entry_price, p, side) * leverage
        result[f"naive_roe_{h}s"] = roe_h

    # causal trailing-floor variant: walk forward at 15s resolution, track running peak
    # roe from the defer-start point, exit as soon as peak-roe giveback >= floor (this
    # trade's own trail_drawdown_pct from its live config snapshot), else exit at cap(120s).
    floor_giveback = rec.get("config_snapshot", {}).get("trail_drawdown_pct", 1.0) or 1.0
    cap = 120
    step = 15
    t = exited_at
    peak_roe = baseline_roe
    exit_roe = None
    exit_t_offset = None
    tt = 0
    while tt <= cap:
        p = price_at(df, exited_at + tt)
        if p is None:
            tt += step
            continue
        roe_now = pnl_pct(entry_price, p, side) * leverage
        if roe_now > peak_roe:
            peak_roe = roe_now
        if peak_roe - roe_now >= floor_giveback:
            exit_roe = roe_now
            exit_t_offset = tt
            break
        tt += step
    if exit_roe is None:
        p = price_at(df, exited_at + cap)
        if p is not None:
            exit_roe = pnl_pct(entry_price, p, side) * leverage
            exit_t_offset = cap
    result["floor_variant_roe"] = exit_roe
    result["floor_variant_exit_offset_s"] = exit_t_offset
    result["floor_variant_giveback_used"] = floor_giveback
    return result


def simulate_sl(rec, df, recovery_min_votes=2):
    entry_price = rec["entry_price"]
    side = rec["side"]
    leverage = rec["leverage"]
    exited_at = rec["exited_at"]
    exit_price = rec["exit_price"]

    baseline_roe = pnl_pct(entry_price, exit_price, side) * leverage

    df_pre = df_up_to(df, exited_at)
    recovery_signal = False
    votes_note = None
    warmup = max(cfg.ema_slow, cfg.macd_slow, cfg.bb_period, cfg.atr_period,
                 cfg.adx_period, cfg.volume_ma_period, cfg.stoch_rsi_period, cfg.rsi_period) + cfg.macd_signal + 2
    if len(df_pre) >= warmup:
        try:
            df_ind = add_indicators(df_pre, cfg)
            # recovery signal: "against opposite(side)" == "favorable to side"
            recovery_signal = detect_reversal(df_ind, cfg, opposite(side), min_votes=recovery_min_votes)
        except Exception as ex:
            votes_note = f"indicator_fail:{ex}"
    else:
        votes_note = f"insufficient_warmup({len(df_pre)}<{warmup})"

    result = {
        "symbol": rec["symbol"], "side": side, "exited_at": exited_at,
        "baseline_roe": baseline_roe, "exit_reason": rec["exit_reason"],
        "held_seconds": rec["held_seconds"], "recovery_signal": bool(recovery_signal),
        "warmup_note": votes_note,
    }

    # unconditional defer (no signal gating) - contrast with old "time-based forced close"
    # finding, kept for comparison only
    for defer_sec, extra_cap in [(30, None), (60, None), (60, 1.0), (60, 1.5)]:
        # extra_cap: secondary hard-stop, additional adverse ROE beyond baseline before
        # bailing early during the wait (causal - only uses info up to that point)
        tt = 0
        step = 10
        exit_roe = None
        exit_off = None
        while tt <= defer_sec:
            p = price_at(df, exited_at + tt)
            if p is None:
                tt += step
                continue
            roe_now = pnl_pct(entry_price, p, side) * leverage
            if extra_cap is not None and (baseline_roe - roe_now) >= extra_cap:
                exit_roe = roe_now
                exit_off = tt
                break
            tt += step
        if exit_roe is None:
            p = price_at(df, exited_at + defer_sec)
            if p is not None:
                exit_roe = pnl_pct(entry_price, p, side) * leverage
                exit_off = defer_sec
        key = f"defer_{defer_sec}s" + (f"_cap{extra_cap}" if extra_cap is not None else "")
        result[f"{key}_roe"] = exit_roe
        result[f"{key}_offset_s"] = exit_off
    return result


def main():
    recs = load_ledger()
    bot_recs = [r for r in recs if r.get("origin") == "bot" and is_ascii_symbol(r.get("symbol", ""))]

    cache = json.loads(CACHE_PATH.read_text(encoding="utf-8"))

    symbol_dfs = {}
    for sym in set(r["symbol"] for r in bot_recs if r["exit_reason"] in TP_REASONS | SL_REASONS):
        symbol_dfs[sym] = build_symbol_df(cache, sym)

    tp_results = []
    sl_results = []
    skipped = {"tp_no_data": 0, "sl_no_data": 0}

    for r in bot_recs:
        reason = r.get("exit_reason")
        if reason in TP_REASONS:
            df = symbol_dfs.get(r["symbol"])
            if df is None or df.empty:
                skipped["tp_no_data"] += 1
                continue
            tp_results.append(simulate_tp(r, df))
        elif reason in SL_REASONS:
            df = symbol_dfs.get(r["symbol"])
            if df is None or df.empty:
                skipped["sl_no_data"] += 1
                continue
            sl_results.append(simulate_sl(r, df))

    out = {
        "meta": {
            "n_tp_cases": len(tp_results), "n_sl_cases": len(sl_results),
            "skipped": skipped,
            "n_excluded_nonascii_symbol": sum(
                1 for r in recs if r.get("origin") == "bot" and not is_ascii_symbol(r.get("symbol", ""))
                and r.get("exit_reason") in TP_REASONS | SL_REASONS
            ),
        },
        "tp_cases": tp_results,
        "sl_cases": sl_results,
    }
    OUT_PATH.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    print("wrote", OUT_PATH, out["meta"])


if __name__ == "__main__":
    main()
