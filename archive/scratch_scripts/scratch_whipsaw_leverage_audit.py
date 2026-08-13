"""[감사 검증] passes_whipsaw_volatility_filter의 하드코딩 4.0 -> 실제(신호강도 기반)
레버리지 수정이 실제 진입 결정과 손익에 어떤 영향을 주는지 offline_backtest.py의
공식 pending 메커니즘(다음 캔들 시가 체결)으로 검증한다.

baseline: whipsaw 필터가 항상 leverage=4.0으로 stop_dist_pct를 계산 (수정 전 버그 재현)
variant : whipsaw 필터가 신호강도 기반 실제 레버리지(leverage_min~leverage_max, round)로 계산 (수정 후)

두 변형 모두 포지션 손익 계산(마진/수량/스탑/트레일링 등)은 offline_backtest.py 기존 방식
그대로 settings.leverage(고정값)를 사용한다 — 이 백테스터 자체가 동적 레버리지 경제성을
모델링하지 않기 때문(라이브 코드와 다른 기존 한계이며 이번 수정 범위 밖). 이번 검증은
"whipsaw 필터의 진입 게이트 판정"에만 초점을 맞춘다.

REST 호출 없음, 로컬 scratch_klines_v4.json만 사용.
"""
import copy
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import offline_backtest as ob

ob.disable_network()

LEVERAGE_MIN = 3
LEVERAGE_MAX = 5


def _strength(window: list[ob.Candle]) -> float:
    """bot/strategy.py signal_strength()와 동일한 공식 (rsi, macd 기반)."""
    closes = [c.close for c in window]
    rsi = ob._rsi(closes)
    macd = ob._ema(closes[-12:], 12) - ob._ema(closes[-26:], 26)
    macd_series = [
        ob._ema(closes[max(0, i - 25):i + 1], min(26, i + 1)) - ob._ema(closes[max(0, i - 11):i + 1], min(12, i + 1))
        for i in range(len(closes) - 9, len(closes))
    ]
    macd_signal = ob._ema(macd_series, len(macd_series))
    current = window[-1]
    rsi_dist = abs(rsi - 50) / 50
    rsi_score = min(rsi_dist / 0.4, 1.0)
    macd_hist = abs(macd - macd_signal)
    macd_score = min(macd_hist / (current.close * 0.001), 1.0) if current.close > 0 else 0.0
    return round((rsi_score + macd_score) / 2, 4)


def _compute_leverage(strength: float) -> float:
    span = LEVERAGE_MAX - LEVERAGE_MIN
    return round(LEVERAGE_MIN + strength * span)


def make_signal_variant(use_dynamic_leverage: bool):
    """ob.signal()을 감싸서 whipsaw 필터 판정에 쓰는 레버리지만 바꾼 버전을 만든다.
    원본 signal() 로직(진입조건, wick필터 등)은 그대로 재사용하고, 마지막
    _passes_whipsaw_filter 호출부만 대체한다."""
    baseline_atr = ob._atr

    def patched_signal(history: list, settings: ob.Settings) -> str | None:
        if len(history) < settings.warmup:
            return None
        window = history[-max(settings.warmup, 80):]
        current = window[-1]
        closes = [c.close for c in window]
        fast, slow = ob._ema(closes[-20:], 20), ob._ema(closes[-50:], 50)
        macd = ob._ema(closes[-12:], 12) - ob._ema(closes[-26:], 26)
        macd_series = [
            ob._ema(closes[max(0, i - 25):i + 1], min(26, i + 1)) - ob._ema(closes[max(0, i - 11):i + 1], min(12, i + 1))
            for i in range(len(closes) - 9, len(closes))
        ]
        macd_signal = ob._ema(macd_series, len(macd_series))
        volume_ma = sum(c.volume for c in window[-20:]) / 20
        quote_volume_ma = sum(c.quote_volume for c in window[-20:]) / 20
        change = (current.close / current.open - 1) * 100 if current.open else 0.0
        taker = current.taker_buy_volume / current.volume if current.volume else 0.5
        adx = ob._adx(window)
        rsi = ob._rsi(closes)
        long_allowed = settings.trade_sides in ("both", "long-only")
        short_allowed = settings.trade_sides in ("both", "short-only")
        long_ok = (long_allowed and quote_volume_ma >= settings.min_avg_quote_volume and change >= settings.candle_change_pct and current.volume >= volume_ma * settings.volume_ratio
                   and taker >= settings.taker_ratio and fast > slow and macd >= macd_signal
                   and rsi >= 50 and adx >= settings.adx_threshold)
        short_ok = (short_allowed and quote_volume_ma >= settings.min_avg_quote_volume and change <= -settings.short_candle_change_pct
                    and current.volume >= volume_ma * settings.short_volume_ratio and taker <= settings.short_taker_buy_ratio_max
                    and fast < slow and macd <= macd_signal and rsi <= settings.short_rsi_max
                    and adx >= settings.short_adx_threshold)
        if short_ok and not ob._passes_short_scalp_reversal_filter(current, settings):
            short_ok = False
        if long_ok and not ob._passes_long_scalp_reversal_filter(current, settings):
            long_ok = False
        side = "LONG" if long_ok else "SHORT" if short_ok else None
        if side is None:
            return None

        # --- whipsaw 필터: 여기만 baseline(4.0 고정) vs variant(동적 레버리지) 차이 ---
        if current.close <= 0:
            return None
        atr_pct = (baseline_atr(window) / current.close) * 100
        stop_roe = settings.short_stop_roe_pct if side == "SHORT" and settings.short_stop_roe_pct > 0 else settings.stop_roe_pct
        if use_dynamic_leverage:
            strength = _strength(window)
            leverage = _compute_leverage(strength)
        else:
            leverage = 4.0
        stop_dist_pct = stop_roe / max(leverage, 1.0)
        if settings.min_atr_vs_stop_ratio > 0 and atr_pct < stop_dist_pct * settings.min_atr_vs_stop_ratio:
            return None
        if settings.max_atr_vs_stop_ratio > 0 and atr_pct > stop_dist_pct * settings.max_atr_vs_stop_ratio:
            return None
        return side

    return patched_signal


def summarize(result: dict) -> dict:
    """offline_backtest.metrics()의 all-요약을 그대로 재사용한다 (lookahead 없는 공식 지표)."""
    m = ob.metrics(result, validation_start=0)["all"]
    return {
        "trades": m["trades"],
        "win_rate_pct": round(m["win_rate"] * 100, 2),
        "profit_factor": round(m["profit_factor"], 3) if m["profit_factor"] is not None else None,
        "expectancy_usdt": round(m["expectancy"], 4),
        "avg_hold_min": round(m["average_holding_minutes"], 2),
        "net_pnl_usdt": round(m["net_pnl"], 3),
        "unfilled_entries": result.get("unfilled_entries", 0),
    }


def max_drawdown_pct(result: dict) -> float:
    peak, max_dd_usdt = float("-inf"), 0.0
    for point in result["equity_curve"]:
        peak = max(peak, point["equity"])
        max_dd_usdt = max(max_dd_usdt, peak - point["equity"])
    start_equity = result["equity_curve"][0]["equity"] if result["equity_curve"] else 0.0
    return round(max_dd_usdt / start_equity * 100, 2) if start_equity else 0.0


def main():
    data_path = Path(__file__).resolve().parent / "scratch_klines_v4.json"
    data, meta = ob.load_data(data_path)
    total_hours = None
    all_ts = [c.timestamp for candles in data.values() for c in candles]
    if all_ts:
        total_hours = (max(all_ts) - min(all_ts)) / 3_600_000

    settings = ob.Settings()  # 라이브 기본값 (bot/config.py 확인 결과와 동일하게 유지)

    baseline_signal = ob.signal
    try:
        ob.signal = make_signal_variant(use_dynamic_leverage=False)
        baseline_result = ob.run_backtest(data, settings)

        ob.signal = make_signal_variant(use_dynamic_leverage=True)
        variant_result = ob.run_backtest(data, settings)
    finally:
        ob.signal = baseline_signal  # 원상복구

    base_summary = summarize(baseline_result)
    var_summary = summarize(variant_result)

    base_summary["max_drawdown_pct"] = max_drawdown_pct(baseline_result)
    var_summary["max_drawdown_pct"] = max_drawdown_pct(variant_result)
    if total_hours:
        base_summary["trades_per_hour_x3slots"] = round(base_summary["trades"] / total_hours, 3)
        var_summary["trades_per_hour_x3slots"] = round(var_summary["trades"] / total_hours, 3)

    print("=== baseline (whipsaw filter: hardcoded leverage=4.0, 버그 재현) ===")
    print(json.dumps(base_summary, indent=2, ensure_ascii=False))
    print()
    print("=== variant (whipsaw filter: 신호강도 기반 실제 레버리지, 수정본) ===")
    print(json.dumps(var_summary, indent=2, ensure_ascii=False))

    # 진입 판정이 실제로 갈린 건수 세보기 (동일 신호 후보 중 필터 통과여부가 달라진 것)
    base_symbols_times = {(t["symbol"], t["entry_time"]) for t in baseline_result["ledger"]}
    var_symbols_times = {(t["symbol"], t["entry_time"]) for t in variant_result["ledger"]}
    only_baseline = base_symbols_times - var_symbols_times
    only_variant = var_symbols_times - base_symbols_times
    print()
    print(f"baseline에만 있던 진입(수정으로 사라짐): {len(only_baseline)}건")
    print(f"variant에만 있던 진입(수정으로 새로 생김): {len(only_variant)}건")


if __name__ == "__main__":
    main()
