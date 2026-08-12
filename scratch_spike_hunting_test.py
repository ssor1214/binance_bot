"""[2026-08-11 사용자요청] "거래량 스파이크(20봉평균 3배) + 가격 0.2% 급등" 즉시 시장가
진입 전략 재검증. 원본이 이미 다음봉시가 진입 구조라 신호로직만 이식(offline_backtest.py).
실 API 호출 없음."""
from __future__ import annotations

from pathlib import Path

import offline_backtest as ob

DATA_PATH = Path("scratch_klines_v4.json")
WINDOW = 25


def strategy_signal(history, settings):
    if len(history) < WINDOW:
        return None
    window = history[-WINDOW:]
    current = window[-1]
    prev = window[-2]

    vol_ma = sum(c.volume for c in window[-21:-1]) / 20  # current 제외 직전 20봉 평균
    is_volume_spike = current.volume >= vol_ma * 3.0
    is_price_pump = current.close > prev.close * 1.002

    if is_volume_spike and is_price_pump:
        return "LONG"
    return None


def main():
    data, _ = ob.load_data(DATA_PATH)
    total_hours = (max(c[-1].timestamp for c in data.values()) - min(c[0].timestamp for c in data.values())) / 3_600_000

    settings = ob.Settings(
        leverage=3.0, margin_fraction=0.10, average_down=False,
        stop_roe_pct=0.4 * 3.0, take_profit_roe_pct=0.4 * 3.0, hard_take_profit_roe_pct=0.4 * 3.0,
        trailing_drawdown_roe_pct=0.4 * 3.0, trade_sides="long-only", fee_rate=0.0004,
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
