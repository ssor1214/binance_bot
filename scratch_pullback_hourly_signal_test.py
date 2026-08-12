"""[2026-08-12 사용자요청] "노이즈 필터가 얼마나 거래 시그널/실제 거래수를 줄이는지
시간대별로" — signal_with_pullback_flag()를 계측해서 매 캔들마다 (1) LONG 조건 자체가
맞았는지(필터 적용 전 원시 시그널), (2) 그중 고점밀림 필터에 걸려 차단됐는지를 시간대별로
집계한다. 실제 체결 거래수는 offline_backtest.run_backtest()로 별도 확인. 공식 pending
메커니즘 그대로, lookahead 없음."""
from __future__ import annotations

from collections import defaultdict
from pathlib import Path

import offline_backtest as ob

DATA_PATH = Path("scratch_klines_v4.json")


def signal_instrumented(history: list, settings, stats: dict, hour_key: int) -> str | None:
    """ob.signal()과 동일 로직이되, LONG 원시조건 충족여부/필터차단여부를 hour_key
    버킷에 집계한다."""
    if len(history) < settings.warmup:
        return None
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
    long_raw_ok = (long_allowed and quote_volume_ma >= settings.min_avg_quote_volume and change >= settings.candle_change_pct and current.volume >= volume_ma * settings.volume_ratio
                   and taker >= settings.taker_ratio and fast > slow and macd >= macd_signal
                   and rsi >= 50 and adx >= settings.adx_threshold)
    short_ok = (short_allowed and quote_volume_ma >= settings.min_avg_quote_volume and change <= -settings.short_candle_change_pct
                and current.volume >= volume_ma * settings.short_volume_ratio and taker <= settings.short_taker_buy_ratio_max
                and fast < slow and macd <= macd_signal and rsi <= settings.short_rsi_max
                and adx >= settings.short_adx_threshold)
    if short_ok and not ob._passes_short_scalp_reversal_filter(current, settings):
        short_ok = False

    long_ok = long_raw_ok
    if long_raw_ok:
        stats[hour_key]["long_raw_signal"] += 1
        if not ob._passes_long_scalp_reversal_filter(current, settings):
            long_ok = False
            stats[hour_key]["long_blocked_by_pullback"] += 1

    side = "LONG" if long_ok else "SHORT" if short_ok else None
    if side and not ob._passes_whipsaw_filter(window, side, settings):
        return None
    if side == "LONG":
        stats[hour_key]["long_signal_passed"] += 1
    return side


def run_instrumented(data: dict, settings) -> tuple[dict, dict]:
    """ob.run_backtest()과 동일 흐름이되 signal_instrumented로 시간대별 시그널 통계도 같이 낸다."""
    by_time = defaultdict(dict)
    for symbol, candles in data.items():
        for candle in candles:
            by_time[candle.timestamp][symbol] = candle
    histories = defaultdict(list)
    positions: dict = {}
    pending: dict[str, str] = {}
    balance, ledger = settings.starting_balance, []
    stats = defaultdict(lambda: defaultdict(int))
    trade_hour_counts = defaultdict(lambda: defaultdict(int))  # hour -> side -> filled count

    for timestamp in sorted(by_time):
        hour_key = timestamp // 3600
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
                side = pending.pop(symbol)
                limit_price = ob._entry_limit_price(candle.open, side, settings)
                if ob._entry_limit_filled(candle, side, limit_price):
                    entry = ob._fill(limit_price, side, True, settings.slippage_bps)
                    side_margin_mult = settings.long_margin_fraction_mult if side == "LONG" else settings.short_margin_fraction_mult
                    margin = balance * settings.margin_fraction * side_margin_mult
                    if margin > 0:
                        qty = margin * settings.leverage / entry
                        fee = entry * qty * settings.fee_rate
                        balance -= margin + fee
                        pos = ob.Position(symbol, side, timestamp, entry, qty, margin, fee, entry)
                        pos.max_adverse_roe = min(pos.max_adverse_roe, ob._adverse_roe(pos, candle, settings))
                        pos.max_favorable_roe = max(pos.max_favorable_roe, ob._favorable_roe(pos, candle, settings))
                        positions[symbol] = pos
                        trade_hour_counts[hour_key][side] += 1
                else:
                    histories[symbol].append(candle)
                    continue
            histories[symbol].append(candle)
            if symbol not in positions and symbol not in pending and len(positions) + len(pending) < settings.max_positions:
                side = signal_instrumented(histories[symbol], settings, stats, hour_key)
                if side:
                    pending[symbol] = side

    for symbol, pos in list(positions.items()):
        item, balance = ob._close(pos, histories[symbol][-1].close, histories[symbol][-1].timestamp, "end_of_data", settings, balance)
        ledger.append(item)
    return dict(stats), dict(trade_hour_counts)


def main():
    data, _ = ob.load_data(DATA_PATH)
    settings = ob.Settings(
        starting_balance=38.0, leverage=4.0, max_positions=3, margin_fraction=0.20,
        short_candle_change_pct=0.45, short_volume_ratio=2.6, short_taker_buy_ratio_max=0.43,
        short_adx_threshold=24.0, short_max_close_from_low_pct=0.5,
        long_max_close_from_high_pct=0.8, long_max_upper_wick_body_ratio=2.0,
    )
    stats, trade_hour_counts = run_instrumented(data, settings)

    hours = sorted(set(stats.keys()) | set(trade_hour_counts.keys()))
    lines = ["시간대(순번) | LONG원시시그널 | 필터차단 | 필터통과(=진입시도) | 실제체결(LONG)"]
    total_raw, total_blocked, total_passed, total_filled = 0, 0, 0, 0
    for i, h in enumerate(hours):
        raw = stats.get(h, {}).get("long_raw_signal", 0)
        blocked = stats.get(h, {}).get("long_blocked_by_pullback", 0)
        passed = stats.get(h, {}).get("long_signal_passed", 0)
        filled = trade_hour_counts.get(h, {}).get("LONG", 0)
        total_raw += raw; total_blocked += blocked; total_passed += passed; total_filled += filled
        lines.append(f"{i+1}시간째 | {raw} | {blocked} | {passed} | {filled}")
    lines.append("---")
    lines.append(f"합계 | 원시시그널={total_raw} | 차단={total_blocked}({total_blocked/total_raw*100 if total_raw else 0:.1f}%) | 통과={total_passed} | 실제체결={total_filled}")

    output = "\n".join(lines)
    print(output)
    Path("pullback_hourly_signal_result.txt").write_text(output, encoding="utf-8")


if __name__ == "__main__":
    main()
