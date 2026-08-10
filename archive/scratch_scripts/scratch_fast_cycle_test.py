"""[2026-08-11 사용자요청] "10분에 3슬롯이 1~6사이클 돌 수 있게" — 신호기준을 더 오픈마인드로
완화했을 때 실제로 회전율/수익성이 어떻게 바뀌는지 재검증. 슬롯매매(물타기 없음, 마진
13.33%, TP 3.0%, SL 6.0%) 설정을 기준선으로 삼고, 진입기준 완화/익절폭 단축을 각각 및
동시에 적용해서 비교한다. 실 API 호출 없음(scratch_klines_v4.json만 사용)."""
from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import offline_backtest as ob

DATA_PATH = Path("scratch_klines_v4.json")

BASELINE = ob.Settings(
    margin_fraction=0.1333,
    average_down=False,
    take_profit_roe_pct=3.0,
    stop_roe_pct=6.0,
    trailing_drawdown_roe_pct=1.0,
)

VARIANTS = {
    "기준선(현재 슬롯매매)": BASELINE,
    "A. 진입기준 완화만": replace(
        BASELINE,
        candle_change_pct=0.20, volume_ratio=1.5, taker_ratio=0.52,
        adx_threshold=15.0, short_candle_change_pct=0.25, short_volume_ratio=2.0,
        short_taker_buy_ratio_max=0.48, short_adx_threshold=18.0,
    ),
    "B. 빠른익절만(TP1.5%)": replace(BASELINE, take_profit_roe_pct=1.5, trailing_drawdown_roe_pct=0.5),
    "C. 진입완화+빠른익절 동시": replace(
        BASELINE,
        candle_change_pct=0.20, volume_ratio=1.5, taker_ratio=0.52,
        adx_threshold=15.0, short_candle_change_pct=0.25, short_volume_ratio=2.0,
        short_taker_buy_ratio_max=0.48, short_adx_threshold=18.0,
        take_profit_roe_pct=1.5, trailing_drawdown_roe_pct=0.5,
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
        print(f"거래수={a['trades']} (시간당 {trades_per_hour:.2f}건) 승률={a['win_rate']*100:.1f}% "
              f"순손익={a['net_pnl']:+.3f} 손익비={a['profit_factor']} 기대값/건={a['expectancy']:+.4f} "
              f"평균보유={a['average_holding_minutes']:.1f}분 최대낙폭={m['max_drawdown_usdt']:.3f}USDT")


if __name__ == "__main__":
    main()
