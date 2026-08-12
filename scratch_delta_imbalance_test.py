"""[2026-08-11 사용자요청] "델타(체결 매수/매도 불균형) 기반 진입" 전략 재검증.
원본 스크립트는 종가 위치(close-location)로 매수/매도 비율을 "추정"하지만, 저희 캔들
데이터엔 바이낸스가 실제로 주는 taker_buy_volume(진짜 시장가 매수 체결량)이 이미 있어서
그걸로 더 정확하게 검증한다. EMA50 추세필터 + delta_ratio>=0.4 + 5봉 delta_ma>0.1 조건을
실제 taker 데이터 기준으로 환산해서 이식(offline_backtest.py, 다음봉시가 진입). 실 API 호출 없음."""
from __future__ import annotations

from pathlib import Path

import offline_backtest as ob

DATA_PATH = Path("scratch_klines_v4.json")
WINDOW = 60


def _delta_ratio(candle):
    """원본의 delta_ratio(-1~1, 매수우위 양수)를 실제 taker_buy_volume으로 계산.
    delta_ratio = (buy-sell)/volume = 2*(buy/volume) - 1."""
    if candle.volume <= 0:
        return 0.0
    return 2.0 * (candle.taker_buy_volume / candle.volume) - 1.0


def strategy_signal(history, settings):
    if len(history) < WINDOW:
        return None
    window = history[-WINDOW:]
    closes = [c.close for c in window]
    current = window[-1]

    ema50 = ob._ema(closes[-50:], 50)
    is_uptrend = current.close > ema50

    is_delta_surged = _delta_ratio(current) >= 0.4
    delta_ma = sum(_delta_ratio(c) for c in window[-5:]) / 5
    is_delta_sustained = delta_ma > 0.1

    if is_uptrend and is_delta_surged and is_delta_sustained:
        return "LONG"
    return None


def main():
    data, _ = ob.load_data(DATA_PATH)
    total_hours = (max(c[-1].timestamp for c in data.values()) - min(c[0].timestamp for c in data.values())) / 3_600_000

    settings = ob.Settings(
        leverage=3.0, margin_fraction=0.10, average_down=False,
        stop_roe_pct=0.5 * 3.0, take_profit_roe_pct=0.6 * 3.0, hard_take_profit_roe_pct=0.6 * 3.0,
        trailing_drawdown_roe_pct=0.6 * 3.0, trade_sides="long-only",
        fee_rate=0.0002,
    )

    baseline_signal = ob.signal
    ob.signal = strategy_signal
    try:
        result = ob.run_backtest(data, settings)
        m = ob.metrics(result, validation_start=result["equity_curve"][-1]["timestamp"] + 1)
        a = m["all"]
        print(f"거래수={a['trades']} (시간당 {a['trades']/total_hours:.2f}건) 승률={a['win_rate']*100:.1f}% "
              f"순손익={a['net_pnl']:+.3f} 손익비={a['profit_factor']} 기대값/건={a['expectancy']:+.4f} "
              f"평균보유={a['average_holding_minutes']:.1f}분 최대낙폭={m['max_drawdown_usdt']:.3f}USDT")
    finally:
        ob.signal = baseline_signal


if __name__ == "__main__":
    main()
