"""[2026-08-11 사용자요청] "5분 강제청산(순환매매) + 7슬롯 + 슬롯당 15%"를 진짜 신호로직으로
검증. 이전 테스트들과 달리 심볼별 독립실행이 아니라, 40개 심볼이 공유 슬롯(7개)을 실제로
경쟁 배분하는 포트폴리오 단위 백테스트다 — offline_backtest.run_backtest()의 핵심 로직
(Position/_fill/_close/pending)을 그대로 재사용하되, signal() 대신 매 캔들마다 각 심볼의
scan_entry_candidate()를 호출하고 점수(score)순으로 정렬해 남은 슬롯만큼만 채운다(라이브의
select_and_enter_best_candidates와 동일한 원리). 청산은 5분 강제청산을 추가 적용.
실 API 호출 없음."""
from __future__ import annotations

from collections import defaultdict
from pathlib import Path

import offline_backtest as ob

from bot.config import Config
from bot.main import compute_leverage, scan_entry_candidate
from scratch_full_pipeline_backtest import FakeExchange, load_raw

DATA_PATH = Path("scratch_klines_v4.json")
MAX_HOLD_MIN = 5


def run_portfolio_backtest(data, cfg, fake_ex, settings, max_hold_min=None):
    by_time = defaultdict(dict)
    for symbol, candles in data.items():
        for candle in candles:
            by_time[candle.timestamp][symbol] = candle

    histories = defaultdict(list)
    positions: dict[str, ob.Position] = {}
    balance, ledger, curve = settings.starting_balance, [], []
    occupancy = []  # [2026-08-11 사용자요청] 매 틱마다 슬롯 점유 개수 기록(4개 이상 항상 차있는지 확인용)

    # [2026-08-11 사용자요청] 중간 진행상황을 볼 수 있게 진행률 로그 추가(1000틱마다, flush).
    import sys
    sorted_ts = sorted(by_time)
    total_ts = len(sorted_ts)
    for i, timestamp in enumerate(sorted_ts):
        if i % 1000 == 0:
            print(f"[진행] {i}/{total_ts} ({i/total_ts*100:.1f}%) 거래={len(ledger)}건 잔고={balance:.2f}", file=sys.stderr, flush=True)
        candles = by_time[timestamp]

        # 1) 보유 포지션 청산 체크(익절/손절/트레일링 + 5분 강제청산)
        for symbol in list(positions.keys()):
            if symbol not in candles:
                continue
            pos = positions[symbol]
            candle = candles[symbol]
            decision = ob.exit_decision(pos, candle, settings)
            if not decision and max_hold_min is not None:
                if (candle.timestamp - pos.entry_time) / 60000 >= max_hold_min:
                    decision = (candle.close, "time_stop")
            if decision:
                price, reason = decision
                item, balance = ob._close(pos, price, timestamp, reason, settings, balance)
                ledger.append(item)
                del positions[symbol]
            else:
                favorable = candle.high if pos.side == "LONG" else candle.low
                pos.peak_price = max(pos.peak_price, favorable) if pos.side == "LONG" else min(pos.peak_price, favorable)
                roe = ((pos.peak_price / pos.entry_price - 1) * (1 if pos.side == "LONG" else -1) * settings.leverage * 100)
                if roe >= settings.take_profit_roe_pct:
                    pos.trailing_armed = True

        # 2) 이번 캔들에서 신규 후보를 스캔(슬롯이 남아있는 심볼만) -> 점수순 정렬 -> 남은
        #    슬롯만큼만 다음봉 시가에 진입 예약(pending). 라이브의 "이번 주기 후보를 모아
        #    점수순으로 채운다"는 원리를 그대로 반영.
        free_slots = settings.max_positions - len(positions)
        if free_slots > 0:
            candidates = []
            for symbol, candle in candles.items():
                if symbol in positions:
                    continue
                histories[symbol].append(candle)
                if len(histories[symbol]) < 60:  # scan_entry_candidate 내부 지표 워밍업 최소치
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
                entry = ob._fill(candle.close, side, True, settings.slippage_bps)  # 이번 캔들 종가 근사(다음틱 시가 대용)
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

        occupancy.append(len(positions))
        equity = balance + sum(
            p.margin + (candles.get(s, histories[s][-1]).close - p.entry_price) * p.quantity * (1 if p.side == "LONG" else -1)
            for s, p in positions.items()
        )
        curve.append({"timestamp": timestamp, "equity": equity})

    for symbol, pos in list(positions.items()):
        item, balance = ob._close(pos, histories[symbol][-1].close, histories[symbol][-1].timestamp, "end_of_data", settings, balance)
        ledger.append(item)

    return {"ledger": ledger, "final_balance": balance, "equity_curve": curve, "occupancy": occupancy}


def summarize(result, label):
    ledger = result["ledger"]
    if not ledger:
        print(f"=== {label} === 거래 없음")
        return
    wins = [r for r in ledger if r["net_pnl"] > 0]
    losses = [r for r in ledger if r["net_pnl"] <= 0]
    net = sum(r["net_pnl"] for r in ledger)
    gross_profit = sum(r["net_pnl"] for r in wins)
    gross_loss = abs(sum(r["net_pnl"] for r in losses))
    pf = gross_profit / gross_loss if gross_loss else float("inf")
    avg_hold = sum(r["holding_minutes"] for r in ledger) / len(ledger)
    peak, max_dd = 0.0, 0.0
    for p in result["equity_curve"]:
        peak = max(peak, p["equity"])
        max_dd = max(max_dd, peak - p["equity"])
    start_equity = result["equity_curve"][0]["equity"] if result["equity_curve"] else 1
    print(f"=== {label} ===")
    print(f"거래수={len(ledger)} 승률={len(wins)/len(ledger)*100:.1f}% 순손익={net:+.3f} "
          f"손익비={pf:.2f} 평균보유={avg_hold:.1f}분 최종잔고={result['final_balance']:.2f} "
          f"최대낙폭={max_dd:.2f}({max_dd/start_equity*100:.1f}%)")
    occ = result.get("occupancy") or []
    if occ:
        avg_occ = sum(occ) / len(occ)
        pct_ge4 = sum(1 for o in occ if o >= 4) / len(occ) * 100
        print(f"평균 슬롯점유={avg_occ:.2f}개 / 4개 이상 차있던 비율={pct_ge4:.1f}%")


def main():
    cfg = Config()
    raw = load_raw(DATA_PATH)  # fake_ex는 전체 이력을 그대로 갖고 있어야 함(지표 워밍업용)
    fake_ex = FakeExchange(raw)
    data_candles, _ = ob.load_data(DATA_PATH)

    # [2026-08-11 사용자요청] "더 가볍게" — 기간과 심볼 수를 더 줄인다(fake_ex의 워밍업용
    # 전체이력은 그대로 유지, lookahead 없음). 매 틱마다 scan_entry_candidate가 심볼 수만큼
    # 반복 호출되는 구조라 심볼 수가 체감 속도에 가장 크게 영향을 준다.
    RECENT_MINUTES = 720  # 약 반나절
    MAX_SYMBOLS = 20
    data_candles = {
        symbol: candles[-RECENT_MINUTES:]
        for symbol, candles in list(data_candles.items())[:MAX_SYMBOLS]
    }

    # [2026-08-11 사용자요청 재실행] 슬롯이 더 잘 채워지도록 라이브에 이미 적용한 완화값
    # (확률기준 0.58/0.63)을 그대로 쓴다 — cfg는 Config()가 .env를 그대로 로드하므로 이미
    # 반영돼 있음. 슬롯당 마진을 총시드의 10% 고정, 손절은 라이브 값(-6% ROE, 유예없는
    # 즉시컷) 그대로.
    print(f"진입확률기준: LONG={cfg.min_entry_probability} SHORT={cfg.short_min_entry_probability}")
    leverage_est = compute_leverage(0.8, cfg)
    settings = ob.Settings(
        margin_fraction=0.16, average_down=False, leverage=float(leverage_est),
        stop_roe_pct=cfg.stop_loss_pct, take_profit_roe_pct=cfg.take_profit_min,
        trailing_drawdown_roe_pct=cfg.trail_drawdown_pct, hard_take_profit_roe_pct=cfg.take_profit_hard_cap,
        max_positions=7,
    )

    result = run_portfolio_backtest(data_candles, cfg, fake_ex, settings, max_hold_min=MAX_HOLD_MIN)
    summarize(result, "7슬롯 16%(라이브와 동일) + 5분 강제청산(순환매매) + 완화된 진입확률, 진짜 신호로직, 포트폴리오 공유슬롯")


if __name__ == "__main__":
    main()
