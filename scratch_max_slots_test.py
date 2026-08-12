"""[2026-08-11 사용자요청] "슬롯당 13.33% 유지하고 최대슬롯 3->7개로 확대(=총노출 40%->93%,
사실상 100% 투입)" 검증. 저희 실제 전략(offline_backtest.py 기본 signal(), 펌프모멘텀)
그대로 사용, 슬롯수만 바꾼다. 실 API 호출 없음."""
from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import offline_backtest as ob

DATA_PATH = Path("scratch_klines_v4.json")

BASELINE = ob.Settings(
    margin_fraction=0.1333, average_down=False,
    stop_roe_pct=6.0, take_profit_roe_pct=3.0, trailing_drawdown_roe_pct=0.75,
    hard_take_profit_roe_pct=4.5, max_positions=3,
)

VARIANTS = {
    "현재(3슬롯, 총노출 최대 40%)": BASELINE,
    "5슬롯(총노출 최대 67%)": replace(BASELINE, max_positions=5),
    "7슬롯(총노출 최대 93%, 사실상 100% 투입)": replace(BASELINE, max_positions=7),
}


def main():
    data, _ = ob.load_data(DATA_PATH)
    total_hours = (max(c[-1].timestamp for c in data.values()) - min(c[0].timestamp for c in data.values())) / 3_600_000

    for label, settings in VARIANTS.items():
        result = ob.run_backtest(data, settings)
        m = ob.metrics(result, validation_start=result["equity_curve"][-1]["timestamp"] + 1)
        a = m["all"]
        dd_pct = m["max_drawdown_pct_of_start"] * 100
        print(f"=== {label} ===")
        print(f"거래수={a['trades']} (시간당 {a['trades']/total_hours:.2f}건) 승률={a['win_rate']*100:.1f}% "
              f"순손익={a['net_pnl']:+.3f} 손익비={a['profit_factor']} 기대값/건={a['expectancy']:+.4f} "
              f"최대낙폭={m['max_drawdown_usdt']:.3f}USDT(자산대비 {dd_pct:.1f}%)")


if __name__ == "__main__":
    main()
