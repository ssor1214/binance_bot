"""[2026-08-11 사용자요청] 공유받은 "EMA20/60+볼린저하단+RSI30-45+ATR필터" 롱전용 스캘핑
전략을 저희 공식 백테스트 엔진(offline_backtest.py, pending/다음봉시가 진입)으로 재검증.
원본 스크립트는 같은 캔들 종가로 즉시 체결(lookahead bias)하는 결함이 있어 그대로 안 쓰고,
신호 로직만 이식해서 ob.signal()을 몽키패치한다. 실 API 호출 없음."""
from __future__ import annotations

from pathlib import Path

import offline_backtest as ob

DATA_PATH = Path("scratch_klines_v4.json")
WARMUP = 80  # BB(20)+ATR_MA(50) 워밍업을 넉넉히 커버


def strategy_signal(history, settings):
    """원본 스크립트의 check_entry_signal()을 그대로 이식(롱 전용)."""
    if len(history) < WARMUP:
        return None
    window = history[-WARMUP:]
    closes = [c.close for c in window]
    current = window[-1]

    ema20 = ob._ema(closes[-20:], 20)
    ema60 = ob._ema(closes[-60:], 60) if len(closes) >= 60 else ob._ema(closes, len(closes))
    is_uptrend = ema20 > ema60

    bb_window = closes[-20:]
    bb_mid = sum(bb_window) / 20
    bb_std = (sum((c - bb_mid) ** 2 for c in bb_window) / 20) ** 0.5
    bb_lower = bb_mid - bb_std * 2
    is_near_lower_bb = current.close <= bb_lower * 1.002

    rsi = ob._rsi(closes, 14)
    is_rsi_valid = 30 <= rsi <= 45

    trs = []
    for i in range(1, len(window)):
        prev_close = window[i - 1].close
        hi, lo = window[i].high, window[i].low
        trs.append(max(hi - lo, abs(hi - prev_close), abs(lo - prev_close)))
    atr = sum(trs[-14:]) / 14 if len(trs) >= 14 else None
    atr_ma = sum(trs[-50:]) / min(50, len(trs)) if trs else None
    is_volatility_safe = atr is not None and atr_ma is not None and atr < atr_ma * 2.5

    if is_uptrend and is_near_lower_bb and is_rsi_valid and is_volatility_safe:
        return "LONG"
    return None


def main():
    data, _ = ob.load_data(DATA_PATH)
    # 원본 스크립트 파라미터: TP+0.7%, SL-0.6%(코인가격 기준), 레버리지 3배, 3슬롯,
    # 슬롯당 10%. 저희 슬롯매매(마진13.33%, 물타기 없음)와는 다르므로, "원본 그대로"와
    # "저희 슬롯매매 사이징"을 나란히 비교한다.
    settings_original_sizing = ob.Settings(
        leverage=3.0, margin_fraction=0.10, average_down=False,
        stop_roe_pct=0.6 * 3.0, take_profit_roe_pct=0.7 * 3.0, hard_take_profit_roe_pct=0.7 * 3.0,
        trailing_drawdown_roe_pct=0.7 * 3.0,  # 트레일링 없이 하드TP로만 확정(원본과 동일하게)
        trade_sides="long-only",
    )
    settings_slot_sizing = ob.Settings(
        leverage=4.0, margin_fraction=0.1333, average_down=False,
        stop_roe_pct=6.0, take_profit_roe_pct=3.0, hard_take_profit_roe_pct=4.5,
        trailing_drawdown_roe_pct=0.75, trade_sides="long-only",
    )

    baseline_signal = ob.signal
    ob.signal = strategy_signal
    try:
        for label, settings in (
            ("원본 파라미터(TP0.7%/SL0.6%/레버리지3배)", settings_original_sizing),
            ("저희 슬롯매매 사이징(TP3%/SL6%/레버리지4배/마진13.33%)", settings_slot_sizing),
        ):
            result = ob.run_backtest(data, settings)
            m = ob.metrics(result, validation_start=result["equity_curve"][-1]["timestamp"] + 1)
            a = m["all"]
            total_hours = (max(c[-1].timestamp for c in data.values()) - min(c[0].timestamp for c in data.values())) / 3_600_000
            print(f"=== {label} ===")
            print(f"거래수={a['trades']} (시간당 {a['trades']/total_hours:.2f}건) 승률={a['win_rate']*100:.1f}% "
                  f"순손익={a['net_pnl']:+.3f} 손익비={a['profit_factor']} 기대값/건={a['expectancy']:+.4f} "
                  f"평균보유={a['average_holding_minutes']:.1f}분 최대낙폭={m['max_drawdown_usdt']:.3f}USDT")
    finally:
        ob.signal = baseline_signal


if __name__ == "__main__":
    main()
