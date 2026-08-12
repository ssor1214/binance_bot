"""[2026-08-11 사용자요청] "물타기 다시 켜기" 검증 — 진짜 신호로직 + 포트폴리오 공유슬롯
기반. 현재 라이브(4슬롯, 슬롯당16%, 물타기없음)와 공정 비교를 위해, 물타기 버전은 총노출을
동일하게(16%) 맞춘다: 초기진입 8% + 손절폭 50%지점에서 물타기 1회 8% 추가 = 총 16%.
offline_backtest._average_down()(기존 검증된 단일-트랜치 물타기 로직)을 그대로 재사용,
scratch_7slot_5min_portfolio_test.py의 포트폴리오 루프에 물타기 단계만 추가한다.
실 API 호출 없음."""
from __future__ import annotations

from collections import defaultdict
from pathlib import Path

import offline_backtest as ob

from bot.config import Config
from bot.main import compute_leverage, scan_entry_candidate
from scratch_full_pipeline_backtest import FakeExchange, load_raw
from scratch_7slot_5min_portfolio_test import summarize

DATA_PATH = Path("scratch_klines_v4.json")


def run_portfolio_backtest_with_average_down(data, cfg, fake_ex, settings):
    by_time = defaultdict(dict)
    for symbol, candles in data.items():
        for candle in candles:
            by_time[candle.timestamp][symbol] = candle

    histories = defaultdict(list)
    positions: dict[str, ob.Position] = {}
    balance, ledger, curve = settings.starting_balance, [], []

    for timestamp in sorted(by_time):
        candles = by_time[timestamp]

        for symbol in list(positions.keys()):
            if symbol not in candles:
                continue
            pos = positions[symbol]
            candle = candles[symbol]
            decision = ob.exit_decision(pos, candle, settings)
            if decision:
                price, reason = decision
                item, balance = ob._close(pos, price, timestamp, reason, settings, balance)
                ledger.append(item)
                del positions[symbol]
            else:
                balance = ob._average_down(pos, candle, settings, balance)  # 물타기 체크(설정상 1회만)
                favorable = candle.high if pos.side == "LONG" else candle.low
                pos.peak_price = max(pos.peak_price, favorable) if pos.side == "LONG" else min(pos.peak_price, favorable)
                roe = ((pos.peak_price / pos.entry_price - 1) * (1 if pos.side == "LONG" else -1) * settings.leverage * 100)
                if roe >= settings.take_profit_roe_pct:
                    pos.trailing_armed = True

        free_slots = settings.max_positions - len(positions)
        if free_slots > 0:
            candidates = []
            for symbol, candle in candles.items():
                if symbol in positions:
                    continue
                histories[symbol].append(candle)
                if len(histories[symbol]) < 60:
                    continue
                fake_ex.set_now(symbol, timestamp)
                try:
                    cand = scan_entry_candidate(fake_ex, cfg, symbol, balance)
                except Exception:
                    cand = None
                if cand:
                    candidates.append(cand)
            candidates.sort(key=lambda c: c["score"], reverse=True)
            for cand in candidates[:free_slots]:
                symbol = cand["symbol"]
                side = cand["signal"]
                candle = candles[symbol]
                entry = ob._fill(candle.close, side, True, settings.slippage_bps)
                margin = balance * settings.margin_fraction
                if margin <= 0:
                    continue
                qty = margin * settings.leverage / entry
                fee = entry * qty * settings.fee_rate
                balance -= margin + fee
                positions[symbol] = ob.Position(symbol, side, timestamp, entry, qty, margin, fee, entry)
        else:
            for symbol, candle in candles.items():
                if symbol not in positions:
                    histories[symbol].append(candle)

        equity = balance + sum(
            p.margin + (candles.get(s, histories[s][-1]).close - p.entry_price) * p.quantity * (1 if p.side == "LONG" else -1)
            for s, p in positions.items()
        )
        curve.append({"timestamp": timestamp, "equity": equity})

    for symbol, pos in list(positions.items()):
        item, balance = ob._close(pos, histories[symbol][-1].close, histories[symbol][-1].timestamp, "end_of_data", settings, balance)
        ledger.append(item)

    return {"ledger": ledger, "final_balance": balance, "equity_curve": curve}


def main():
    cfg = Config()
    raw = load_raw(DATA_PATH)
    fake_ex = FakeExchange(raw)
    data_candles, _ = ob.load_data(DATA_PATH)

    leverage_est = compute_leverage(0.8, cfg)
    settings = ob.Settings(
        margin_fraction=0.08,  # 초기진입 8% (물타기로 8% 더 추가 가능하게 절반만 먼저)
        average_down=True, average_down_trigger_ratio=0.5, average_down_size_ratio=1.0,  # 손절폭 50%지점서 초기margin과 동일액(8%) 추가 -> 총 16%
        leverage=float(leverage_est),
        stop_roe_pct=cfg.stop_loss_pct, take_profit_roe_pct=cfg.take_profit_min,
        trailing_drawdown_roe_pct=cfg.trail_drawdown_pct, hard_take_profit_roe_pct=cfg.take_profit_hard_cap,
        max_positions=4,  # 현재 라이브와 동일 슬롯수
    )

    result = run_portfolio_backtest_with_average_down(data_candles, cfg, fake_ex, settings)
    summarize(result, "물타기 재활성화(초기8%+물타기8%=총16%, 4슬롯), 진짜 신호로직")


if __name__ == "__main__":
    main()
