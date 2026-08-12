"""[2026-08-11 사용자요청] 슬롯 3->4개 + 슬롯당비중 소폭 확대(13.33%->16%) 시 승률 영향 검증.
가벼운 signal()로 빠르게(초 단위) 확인 — 백그라운드에 이미 무거운 작업들이 돌고 있어서
가벼운 엔진 재사용."""
from dataclasses import replace
from pathlib import Path
import offline_backtest as ob

data, _ = ob.load_data(Path("scratch_klines_v4.json"))
total_hours = (max(c[-1].timestamp for c in data.values()) - min(c[0].timestamp for c in data.values())) / 3_600_000

BASELINE = ob.Settings(margin_fraction=0.1333, average_down=False, stop_roe_pct=6.0,
                        take_profit_roe_pct=3.0, trailing_drawdown_roe_pct=0.75,
                        hard_take_profit_roe_pct=4.5, max_positions=3)

VARIANTS = {
    "현재(3슬롯 13.33%, 총노출40%)": BASELINE,
    "4슬롯 13.33%(총노출53%, 비중 그대로)": replace(BASELINE, max_positions=4),
    "4슬롯 16%(총노출64%, 비중도 확대)": replace(BASELINE, max_positions=4, margin_fraction=0.16),
}

for label, settings in VARIANTS.items():
    result = ob.run_backtest(data, settings)
    m = ob.metrics(result, validation_start=result["equity_curve"][-1]["timestamp"] + 1)
    a = m["all"]
    print(f"=== {label} ===")
    print(f"거래수={a['trades']} (시간당 {a['trades']/total_hours:.2f}건) 승률={a['win_rate']*100:.1f}% "
          f"손익비={a['profit_factor']} 순손익={a['net_pnl']:+.3f} 최대낙폭={m['max_drawdown_pct_of_start']*100:.1f}%")
