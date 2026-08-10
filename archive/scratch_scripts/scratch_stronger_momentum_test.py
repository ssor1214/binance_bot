"""[2026-08-11 사용자요청] "익절폭(TP)은 유지하면서 회전속도만 높이는 방법" 검증.
가설: 더 강한 모멘텀(캔들 변동폭/거래량/체결비율 더 엄격)의 신호만 받으면, 같은 TP까지
도달하는 시간이 짧아져서(=평균보유시간 감소) TP를 깎지 않고도 회전율이 오를 것이다.
TP/트레일링/손절은 전부 고정(현재 슬롯매매와 동일), 진입기준만 단계적으로 더 엄격하게.
실 API 호출 없음."""
from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import offline_backtest as ob

DATA_PATH = Path("scratch_klines_v4.json")

FIXED_EXIT = dict(
    margin_fraction=0.1333, average_down=False,
    stop_roe_pct=6.0, take_profit_roe_pct=3.0, trailing_drawdown_roe_pct=0.75, hard_take_profit_roe_pct=4.5,
)

BASELINE = ob.Settings(**FIXED_EXIT)

VARIANTS = {
    "기준(현재 슬롯매매 진입기준)": BASELINE,
    "강한모멘텀만(변동폭/거래량 1.5배)": replace(
        BASELINE,
        candle_change_pct=0.35 * 1.5, volume_ratio=2.0 * 1.5, taker_ratio=0.60,
        short_candle_change_pct=0.45 * 1.5, short_volume_ratio=2.6 * 1.5, short_taker_buy_ratio_max=0.38,
    ),
    "매우강한모멘텀만(변동폭/거래량 2배)": replace(
        BASELINE,
        candle_change_pct=0.35 * 2.0, volume_ratio=2.0 * 2.0, taker_ratio=0.65,
        short_candle_change_pct=0.45 * 2.0, short_volume_ratio=2.6 * 2.0, short_taker_buy_ratio_max=0.33,
    ),
}


def main():
    data, _ = ob.load_data(DATA_PATH)
    total_hours = (max(c[-1].timestamp for c in data.values()) - min(c[0].timestamp for c in data.values())) / 3_600_000

    for label, settings in VARIANTS.items():
        result = ob.run_backtest(data, settings)
        m = ob.metrics(result, validation_start=result["equity_curve"][-1]["timestamp"] + 1)
        a = m["all"]
        trades_per_hour = a["trades"] / total_hours if total_hours else 0
        print(f"=== {label} ===")
        print(f"거래수={a['trades']} (시간당 {trades_per_hour:.2f}건, 5일누적) 승률={a['win_rate']*100:.1f}% "
              f"순손익={a['net_pnl']:+.3f} 손익비={a['profit_factor']} 기대값/건={a['expectancy']:+.4f} "
              f"평균보유={a['average_holding_minutes']:.1f}분 최대낙폭={m['max_drawdown_usdt']:.3f}USDT")


if __name__ == "__main__":
    main()
