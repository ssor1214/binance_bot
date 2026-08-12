"""[2026-08-11 사용자요청] "볼린저 스퀴즈+가짜이탈반등(False Breakout)+5분봉 추세필터"
전략을 저희 공식 백테스트 엔진(pending/다음봉시가 진입)으로 재검증. 신호 로직만 이식하고
사이징/체결은 offline_backtest.py의 정상 로직을 그대로 쓴다. 실 API 호출 없음."""
from __future__ import annotations

from pathlib import Path

import offline_backtest as ob

DATA_PATH = Path("scratch_klines_v4.json")
WINDOW = 450  # 5분봉 EMA20 워밍업(약 100개 5분봉=500분) 확보용


def _bb(closes, period=20):
    w = closes[-period:]
    mid = sum(w) / period
    std = (sum((c - mid) ** 2 for c in w) / period) ** 0.5
    upper, lower = mid + std * 2, mid - std * 2
    width = (upper - lower) / mid if mid else 0.0
    return width, upper, lower


def strategy_signal(history, settings):
    if len(history) < WINDOW:
        return None
    window = history[-WINDOW:]
    closes = [c.close for c in window]
    current = window[-1]

    width_now, bb_upper, bb_lower = _bb(closes, 20)

    widths_30 = []
    for k in range(30):
        idx = len(closes) - 30 + k
        if idx < 20:
            continue
        w, _, _ = _bb(closes[:idx + 1], 20)
        widths_30.append(w)
    if not widths_30:
        return None
    is_squeeze = width_now <= min(widths_30) * 1.1

    is_false_breakout = current.low < bb_lower and current.close > bb_lower

    bars_5m = []
    bucket = []
    for c in window:
        bucket.append(c)
        if len(bucket) == 5:
            bars_5m.append(bucket[-1].close)
            bucket = []
    if len(bars_5m) < 20:
        return None
    ema20_5m = ob._ema(bars_5m[-20:], 20)
    is_bullish_5m = bars_5m[-1] > ema20_5m

    if is_bullish_5m and is_squeeze and is_false_breakout:
        return "LONG"
    return None


def main():
    data, _ = ob.load_data(DATA_PATH)
    total_hours = (max(c[-1].timestamp for c in data.values()) - min(c[0].timestamp for c in data.values())) / 3_600_000

    settings = ob.Settings(
        leverage=3.0, margin_fraction=0.10, average_down=False,
        stop_roe_pct=0.5 * 3.0, take_profit_roe_pct=0.4 * 3.0, hard_take_profit_roe_pct=0.4 * 3.0,
        trailing_drawdown_roe_pct=0.4 * 3.0, trade_sides="long-only",
        fee_rate=-0.0002,  # 메이커 페이백 가정 반영
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
