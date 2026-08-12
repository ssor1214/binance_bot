"""[2026-08-12 사용자요청, 코덱스 백테스트 이어서 진행] 코덱스가 offline_backtest.py에서
검증한 "LONG 고점밀림(상단꼬리/고점대비 종가) 필터"는 완전 차단(D열 표 기준 "보류" 판정,
거래수 616->188건으로 과도하게 감소)만 테스트됐다. 코덱스 자신의 추천(D열)은 "차단이
아니라 비중축소/후순위"였는데 그건 아직 검증 안 됨 — 이 스크립트가 그 검증을 담당한다.

offline_backtest.py를 수정하지 않고(코덱스가 계속 작업 중이라 충돌 방지) import해서 그
검증된 signal()/exit_decision()/_fill() 등을 그대로 재사용하되, run_backtest()만 이
파일 안에 별도로 복제해서 "고점밀림 조건 걸리면 차단 대신 비중만 축소"로 동작을 바꾼다.
공식 pending(다음봉 시가/지정가) 진입 메커니즘은 동일하게 유지 — lookahead bias 없음."""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

import offline_backtest as ob

DATA_PATH = Path("scratch_klines_v4.json")


def signal_with_pullback_flag(history: list, settings) -> tuple[str | None, bool]:
    """ob.signal()과 동일하게 판단하되, LONG 신호가 "고점밀림 필터" 때문에 걸렸는지 여부를
    같이 반환한다(soft 모드에서 완전 차단 대신 비중축소로 쓰기 위함)."""
    if len(history) < settings.warmup:
        return None, False
    window = history[-max(settings.warmup, 80):]
    current = window[-1]
    closes = [c.close for c in window]
    fast, slow = ob._ema(closes[-20:], 20), ob._ema(closes[-50:], 50)
    macd = ob._ema(closes[-12:], 12) - ob._ema(closes[-26:], 26)
    macd_series = [ob._ema(closes[max(0, i - 25):i + 1], min(26, i + 1)) - ob._ema(closes[max(0, i - 11):i + 1], min(12, i + 1)) for i in range(len(closes) - 9, len(closes))]
    macd_signal = ob._ema(macd_series, len(macd_series))
    volume_ma = sum(c.volume for c in window[-20:]) / 20
    quote_volume_ma = sum(c.quote_volume for c in window[-20:]) / 20
    change = (current.close / current.open - 1) * 100 if current.open else 0.0
    taker = current.taker_buy_volume / current.volume if current.volume else 0.5
    adx = ob._adx(window)
    rsi = ob._rsi(closes)
    long_allowed = settings.trade_sides in ("both", "long-only")
    short_allowed = settings.trade_sides in ("both", "short-only")
    long_ok = (long_allowed and quote_volume_ma >= settings.min_avg_quote_volume and change >= settings.candle_change_pct and current.volume >= volume_ma * settings.volume_ratio
               and taker >= settings.taker_ratio and fast > slow and macd >= macd_signal
               and rsi >= 50 and adx >= settings.adx_threshold)
    short_ok = (short_allowed and quote_volume_ma >= settings.min_avg_quote_volume and change <= -settings.short_candle_change_pct
                and current.volume >= volume_ma * settings.short_volume_ratio and taker <= settings.short_taker_buy_ratio_max
                and fast < slow and macd <= macd_signal and rsi <= settings.short_rsi_max
                and adx >= settings.short_adx_threshold)
    if short_ok and not ob._passes_short_scalp_reversal_filter(current, settings):
        short_ok = False
    pullback_flagged = False
    if long_ok and not ob._passes_long_scalp_reversal_filter(current, settings):
        long_ok = False
        pullback_flagged = True  # 완전 차단 대신 비중축소 후보로 고려
    side = "LONG" if long_ok else "SHORT" if short_ok else None
    if side and not ob._passes_whipsaw_filter(window, side, settings):
        return None, False
    if side is None and pullback_flagged and settings.long_pullback_soft_mode:
        return "LONG", True  # soft 모드: 차단하지 않고 비중축소 후보로 통과시킴
    return side, False


def run_backtest_soft_pullback(data: dict, settings) -> dict:
    """ob.run_backtest()을 그대로 복제하되, LONG 고점밀림 후보는 차단 대신
    settings.long_pullback_size_mult 비율로 비중을 줄여서 진입시킨다."""
    by_time = defaultdict(dict)
    for symbol, candles in data.items():
        for candle in candles:
            by_time[candle.timestamp][symbol] = candle
    histories = defaultdict(list)
    positions: dict = {}
    pending: dict[str, tuple[str, bool]] = {}  # symbol -> (side, is_pullback_flagged)
    balance, ledger, curve = settings.starting_balance, [], []
    unfilled_entries = 0

    for timestamp in sorted(by_time):
        candles = by_time[timestamp]
        for symbol in sorted(candles):
            candle = candles[symbol]
            pos = positions.get(symbol)
            if pos:
                pos.max_adverse_roe = min(pos.max_adverse_roe, ob._adverse_roe(pos, candle, settings))
                pos.max_favorable_roe = max(pos.max_favorable_roe, ob._favorable_roe(pos, candle, settings))
                decision = ob.exit_decision(pos, candle, settings)
                if decision:
                    price, reason = decision
                    item, balance = ob._close(pos, price, timestamp, reason, settings, balance)
                    ledger.append(item)
                    del positions[symbol]
                else:
                    balance = ob._average_down(pos, candle, settings, balance)
                    favorable = candle.high if pos.side == "LONG" else candle.low
                    pos.peak_price = max(pos.peak_price, favorable) if pos.side == "LONG" else min(pos.peak_price, favorable)
                    roe = ((pos.peak_price / pos.entry_price - 1) * (1 if pos.side == "LONG" else -1) * settings.leverage * 100)
                    if roe >= settings.take_profit_roe_pct:
                        pos.trailing_armed = True
                    if symbol in positions:
                        positions[symbol].funding += positions[symbol].entry_price * positions[symbol].quantity * settings.funding_rate_8h / 480
            if symbol in pending and symbol not in positions and len(positions) < settings.max_positions:
                side, is_pullback = pending.pop(symbol)
                limit_price = ob._entry_limit_price(candle.open, side, settings)
                if not ob._entry_limit_filled(candle, side, limit_price):
                    unfilled_entries += 1
                    histories[symbol].append(candle)
                    continue
                entry = ob._fill(limit_price, side, True, settings.slippage_bps)
                side_margin_mult = settings.long_margin_fraction_mult if side == "LONG" else settings.short_margin_fraction_mult
                if is_pullback:
                    side_margin_mult *= settings.long_pullback_size_mult
                margin = balance * settings.margin_fraction * side_margin_mult
                if margin > 0:
                    qty = margin * settings.leverage / entry
                    fee = entry * qty * settings.fee_rate
                    balance -= margin + fee
                    pos = ob.Position(symbol, side, timestamp, entry, qty, margin, fee, entry)
                    pos.max_adverse_roe = min(pos.max_adverse_roe, ob._adverse_roe(pos, candle, settings))
                    pos.max_favorable_roe = max(pos.max_favorable_roe, ob._favorable_roe(pos, candle, settings))
                    positions[symbol] = pos
            histories[symbol].append(candle)
            if symbol not in positions and symbol not in pending and len(positions) + len(pending) < settings.max_positions:
                side, is_pullback = signal_with_pullback_flag(histories[symbol], settings)
                if side:
                    pending[symbol] = (side, is_pullback)
        equity = balance + sum(
            p.margin + (candles.get(s, histories[s][-1]).close - p.entry_price) * p.quantity * (1 if p.side == "LONG" else -1)
            for s, p in positions.items()
        )
        curve.append({"timestamp": timestamp, "equity": equity})

    for symbol, pos in list(positions.items()):
        item, balance = ob._close(pos, histories[symbol][-1].close, histories[symbol][-1].timestamp, "end_of_data", settings, balance)
        ledger.append(item)
    return {"ledger": ledger, "final_balance": balance, "equity_curve": curve, "unfilled_entries": unfilled_entries}


def summarize(result: dict, label: str) -> str:
    ledger = result["ledger"]
    if not ledger:
        return f"=== {label} === 거래 없음"
    wins = [r for r in ledger if r["net_pnl"] > 0]
    losses = [r for r in ledger if r["net_pnl"] <= 0]
    net = sum(r["net_pnl"] for r in ledger)
    gross_profit = sum(r["net_pnl"] for r in wins)
    gross_loss = abs(sum(r["net_pnl"] for r in losses))
    pf = gross_profit / gross_loss if gross_loss else float("inf")
    long_rows = [r for r in ledger if r["side"] == "LONG"]
    long_wins = [r for r in long_rows if r["net_pnl"] > 0]
    long_net = sum(r["net_pnl"] for r in long_rows)
    lines = [
        f"=== {label} ===",
        f"거래수={len(ledger)} 승률={len(wins)/len(ledger)*100:.2f}% 순손익={net:+.4f} 손익비={pf:.3f}",
        f"LONG: {len(long_rows)}건 승률={len(long_wins)/len(long_rows)*100:.2f}% 순손익={long_net:+.4f}" if long_rows else "LONG: 거래없음",
        f"최종잔고={result['final_balance']:.4f} 미체결={result['unfilled_entries']}건",
    ]
    return "\n".join(lines)


def main():
    data, _ = ob.load_data(DATA_PATH)

    base_kwargs = dict(
        starting_balance=38.0, leverage=4.0, max_positions=3, margin_fraction=0.20,
        short_candle_change_pct=0.45, short_volume_ratio=2.6, short_taker_buy_ratio_max=0.43,
        short_adx_threshold=24.0,
        # 코덱스 검증된 SHORT 휩쏘 0.5% 필터는 그대로 유지(이미 개선 확인됨)
        short_max_close_from_low_pct=0.5,
    )

    results_text = []

    # 베이스라인(LONG 고점밀림 필터 없음)
    s_baseline = ob.Settings(**base_kwargs, long_max_close_from_high_pct=0.0, long_max_upper_wick_body_ratio=0.0)
    r = ob.run_backtest(data, s_baseline)
    results_text.append(summarize(r, "베이스라인(LONG 고점밀림 필터 없음)"))

    # 완전 차단(코덱스가 이미 검증한 것, 재확인용)
    s_block = ob.Settings(**base_kwargs, long_max_close_from_high_pct=0.8, long_max_upper_wick_body_ratio=2.0)
    r = ob.run_backtest(data, s_block)
    results_text.append(summarize(r, "LONG 고점밀림 0.8% 완전차단(코덱스 재확인)"))

    # 비중축소 변형들
    for mult in (0.6, 0.5, 0.4):
        s_soft = ob.Settings(
            **base_kwargs, long_max_close_from_high_pct=0.8, long_max_upper_wick_body_ratio=2.0,
        )
        s_soft.long_pullback_soft_mode = True
        s_soft.long_pullback_size_mult = mult
        r = run_backtest_soft_pullback(data, s_soft)
        results_text.append(summarize(r, f"LONG 고점밀림 0.8% 비중축소(x{mult})"))

    output = "\n\n".join(results_text)
    print(output)
    Path("long_pullback_sizing_result.txt").write_text(output, encoding="utf-8")


if __name__ == "__main__":
    main()
