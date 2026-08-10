"""[2026-08-11 사용자요청] "단기 회전매매" 재정의 테스트 — 목표: 5분 내 순환(이상적), 최대
15분 내 강제 청산. offline_backtest.exit_decision을 몽키패치해서 "보유 15분 초과 시
그 캔들 종가로 강제청산(time_stop)"을 추가하고, TP/트레일링폭을 여러 단계로 좁혀가며
회전율(평균보유시간)과 수익성을 같이 스윕한다. 실 API 호출 없음."""
from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import offline_backtest as ob

DATA_PATH = Path("scratch_klines_v4.json")
MAX_HOLD_MIN = 5

BASELINE = ob.Settings(
    margin_fraction=0.1333,
    average_down=False,
    stop_roe_pct=6.0,
)

VARIANTS = {
    "현재 슬롯매매 설정(TP3.0%, 트레일0.75%) + 5분 강제청산": replace(BASELINE, take_profit_roe_pct=3.0, trailing_drawdown_roe_pct=0.75, hard_take_profit_roe_pct=4.5),
}

_orig_exit_decision = ob.exit_decision


def make_time_capped_exit(max_hold_min):
    def capped(pos, candle, settings):
        decision = _orig_exit_decision(pos, candle, settings)
        if decision:
            return decision
        held_min = (candle.timestamp - pos.entry_time) / 60000
        if held_min >= max_hold_min:
            # [강제청산] 15분 넘도록 익절/손절 어느쪽도 안 걸렸으면 그 캔들 종가로 정리한다
            # (스캘핑/회전매매는 무기한 보유가 실패로 간주됨 — 슬롯을 계속 묶어두면 안 됨).
            return candle.close, "time_stop"
        return None
    return capped


def main():
    data, _ = ob.load_data(DATA_PATH)
    total_hours = (max(c[-1].timestamp for c in data.values()) - min(c[0].timestamp for c in data.values())) / 3_600_000

    ob.exit_decision = make_time_capped_exit(MAX_HOLD_MIN)
    try:
        for label, settings in VARIANTS.items():
            result = ob.run_backtest(data, settings)
            m = ob.metrics(result, validation_start=result["equity_curve"][-1]["timestamp"] + 1)
            a = m["all"]
            time_stops = sum(1 for r in result["ledger"] if r["reason"] == "time_stop")
            trades_per_hour = a["trades"] / total_hours if total_hours else 0
            print(f"=== {label} (15분 강제청산 적용) ===")
            print(f"거래수={a['trades']} (시간당 {trades_per_hour:.2f}건, 5일누적) 승률={a['win_rate']*100:.1f}% "
                  f"순손익={a['net_pnl']:+.3f} 손익비={a['profit_factor']} 기대값/건={a['expectancy']:+.4f} "
                  f"평균보유={a['average_holding_minutes']:.1f}분 15분강제청산건수={time_stops}({time_stops/a['trades']*100:.0f}%) "
                  f"최대낙폭={m['max_drawdown_usdt']:.3f}USDT")
    finally:
        ob.exit_decision = _orig_exit_decision


if __name__ == "__main__":
    main()
