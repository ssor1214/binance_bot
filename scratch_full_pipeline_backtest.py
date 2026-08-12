"""[2026-08-11 사용자요청] "간소화하지 말고" — offline_backtest.py의 단순화된 signal() 대신,
라이브가 실제로 쓰는 bot/main.py의 scan_entry_candidate() 전체 파이프라인(확률점수+BTC정렬+
고래성감지+즉시모멘텀+체결방향+MTF추세동의+종합점수 게이트)을 그대로 호출해서 신호를
만든다. 청산(TP/SL/트레일링)은 offline_backtest.py의 검증된 pending/다음봉시가 진입 +
exit_decision 엔진을 그대로 재사용한다(신규 재작성 없음, 이미 검증된 부분은 안 건드림).

FakeExchange: 실 API 호출 없이 scratch_klines_v4.json의 1분봉을 "현재 시점까지만" 잘라서
제공한다(미래데이터 유출 방지). get_klines(interval=...)는 1분봉을 해당 간격으로 리샘플링.
get_funding_rate_pct는 데이터가 없어 예외를 던져 whale_signal=False로 안전하게 폴백시킨다
(scan_entry_candidate 자체가 이 실패를 이미 흡수하도록 설계돼 있음 — 코드 그대로 재사용).

한계(명시): (1) 펀딩비/오더북 스프레드 데이터 없음 — 관련 필터는 그레이스풀하게 스킵됨(실제
코드의 예외처리 경로를 그대로 타므로 동작은 안전하지만 그 필터가 실제로 걸러줄 신호는
못 걸러짐). (2) total_balance를 시작자본 고정값으로 근사(계좌 성장에 따른 compute_min_
confirmations/사이징 변화는 반영 안 됨). (3) confirm_two_candle은 라이브 설정
(REQUIRE_TWO_CANDLE_CONFIRMATION=False)과 동일하게 비활성 상태로 둠(라이브와 일치)."""
from __future__ import annotations

import sys
from dataclasses import replace
from pathlib import Path

import pandas as pd

import offline_backtest as ob

from bot.config import Config
from bot.main import compute_leverage, scan_entry_candidate

DATA_PATH = Path("scratch_klines_v4.json")

INTERVAL_MINUTES = {"1m": 1, "3m": 3, "5m": 5, "15m": 15, "30m": 30, "1h": 60, "4h": 240, "1d": 1440}

KLINE_COLUMNS = [
    "open_time", "open", "high", "low", "close", "volume",
    "close_time", "quote_asset_volume", "num_trades",
    "taker_buy_base", "taker_buy_quote", "ignore",
]


class FakeExchange:
    """scan_entry_candidate()가 기대하는 최소 인터페이스만 흉내낸다. 미래데이터 유출 방지를
    위해 set_now(symbol, ts)로 매번 "지금 시점"을 갱신한 뒤에만 get_klines를 호출해야 한다."""

    def __init__(self, raw_by_symbol: dict[str, list]):
        self._raw = raw_by_symbol  # symbol -> 1분봉 raw row 리스트(시간순 정렬됨)
        self._cursor = {}  # symbol -> 현재 시점까지 보이는 1분봉 raw row 개수

    def set_now(self, symbol: str, ts: int) -> None:
        rows = self._raw.get(symbol, [])
        # ts 이하인 마지막 인덱스+1(=exclusive 상한)을 캐싱 없이 매번 선형탐색하기엔 비싸므로
        # 이분탐색으로 찾는다.
        import bisect
        idx = bisect.bisect_right([r[0] for r in rows], ts)
        self._cursor[symbol] = idx

    def _visible_rows(self, symbol: str) -> list:
        idx = self._cursor.get(symbol, 0)
        return self._raw.get(symbol, [])[:idx]

    def get_klines(self, symbol: str, limit: int = 99, interval: str | None = None) -> pd.DataFrame:
        rows_1m = self._visible_rows(symbol)
        step = INTERVAL_MINUTES.get(interval or "1m", 1)
        if step <= 1:
            resampled = rows_1m[-limit:]
        else:
            # step개씩 묶어서 OHLCV 집계(마지막에 step개 안 채워진 미완성 버킷은 제외 —
            # 실제 get_klines가 미완성 캔들을 제외하는 것과 동일한 원칙).
            n_complete = len(rows_1m) // step
            resampled = []
            for b in range(n_complete):
                bucket = rows_1m[b * step:(b + 1) * step]
                o = bucket[0][1]
                h = max(float(r[2]) for r in bucket)
                l = min(float(r[3]) for r in bucket)
                c = bucket[-1][4]
                vol = sum(float(r[5]) for r in bucket)
                qvol = sum(float(r[7]) for r in bucket)
                ntr = sum(float(r[8]) for r in bucket)
                tb_base = sum(float(r[9]) for r in bucket)
                tb_quote = sum(float(r[10]) for r in bucket)
                resampled.append([bucket[0][0], o, h, l, c, vol, bucket[-1][6], qvol, ntr, tb_base, tb_quote, 0])
            resampled = resampled[-limit:]
        if not resampled:
            return pd.DataFrame(columns=KLINE_COLUMNS)
        df = pd.DataFrame(resampled, columns=KLINE_COLUMNS)
        for col in ("open", "high", "low", "close", "volume", "taker_buy_base"):
            df[col] = df[col].astype(float)
        df["open_time"] = pd.to_datetime(df["open_time"], unit="ms")
        return df.reset_index(drop=True)

    def get_mark_price(self, symbol: str) -> float:
        rows = self._visible_rows(symbol)
        return float(rows[-1][4]) if rows else 0.0

    def get_funding_rate_pct(self, symbol: str) -> float:
        raise RuntimeError("백테스트: 펀딩비 이력 없음")  # scan_entry_candidate가 이미 안전하게 흡수함


def load_raw(path: Path) -> dict[str, list]:
    import json
    raw = json.loads(path.read_text(encoding="utf-8"))
    return {s: sorted(rows, key=lambda r: r[0]) for s, rows in raw.items()}


def run_variant(label, cfg, fake_ex, symbols, data_candles, settings, extra_exit_decision=None):
    baseline_signal = ob.signal
    baseline_exit = ob.exit_decision
    approx_balance = settings.starting_balance

    # [한계 명시] offline_backtest.signal(history, settings)는 심볼 정보를 안 받는다.
    # run_backtest를 심볼마다 따로(단일심볼 dict) 실행하는 방식으로 우회했는데, 이러면
    # 실제 라이브의 "전체 40심볼 중 상위 3개만 슬롯 배정"이라는 경쟁 배분이 반영되지
    # 않고, 각 심볼이 독립적으로 신호만 나면 진입한다 — 즉 여기 나오는 거래수는
    # "슬롯 제한이 없다면 나올 수 있는 상한"이고, 실제 3슬롯 경쟁하에서는 이보다
    # 적게 나온다. 승률/손익비/평균보유 같은 질적 지표는 이 제약과 무관해서 그대로 유효하다.
    if extra_exit_decision:
        ob.exit_decision = extra_exit_decision

    total_trades_all = []
    for symbol in symbols:
        def make_signal_for_symbol(sym):
            def sig(history, _settings):
                ts = history[-1].timestamp
                fake_ex.set_now(sym, ts)
                try:
                    candidate = scan_entry_candidate(fake_ex, cfg, sym, approx_balance)
                except Exception:
                    return None
                if not candidate:
                    return None
                return candidate["signal"]
            return sig

        ob.signal = make_signal_for_symbol(symbol)
        result = ob.run_backtest({symbol: data_candles[symbol]}, settings)
        total_trades_all.extend(result["ledger"])

    ob.signal = baseline_signal
    ob.exit_decision = baseline_exit

    if not total_trades_all:
        print(f"=== {label} === 거래 없음")
        return
    wins = [r for r in total_trades_all if r["net_pnl"] > 0]
    losses = [r for r in total_trades_all if r["net_pnl"] <= 0]
    net = sum(r["net_pnl"] for r in total_trades_all)
    gross_profit = sum(r["net_pnl"] for r in wins)
    gross_loss = abs(sum(r["net_pnl"] for r in losses))
    pf = gross_profit / gross_loss if gross_loss else float("inf")
    avg_hold = sum(r["holding_minutes"] for r in total_trades_all) / len(total_trades_all)
    print(f"=== {label} ===")
    print(f"거래수={len(total_trades_all)} 승률={len(wins)/len(total_trades_all)*100:.1f}% "
          f"순손익={net:+.3f} 손익비={pf:.2f} 평균보유={avg_hold:.1f}분")


def main():
    cfg = Config()  # 라이브 .env 그대로 로드(슬롯매매 실제 설정)
    raw = load_raw(DATA_PATH)
    symbols = list(raw.keys())
    fake_ex = FakeExchange(raw)

    data_candles, _ = ob.load_data(DATA_PATH)

    leverage_est = compute_leverage(0.8, cfg)  # 대표 강도값으로 근사 레버리지 산출
    base_settings = ob.Settings(
        margin_fraction=cfg.position_size_min, average_down=False, leverage=float(leverage_est),
        stop_roe_pct=cfg.stop_loss_pct, take_profit_roe_pct=cfg.take_profit_min,
        trailing_drawdown_roe_pct=cfg.trail_drawdown_pct, hard_take_profit_roe_pct=cfg.take_profit_hard_cap,
    )

    run_variant("① 기준(현재 슬롯매매, 실제 신호로직)", cfg, fake_ex, symbols, data_candles, base_settings)

    def make_time_capped(max_hold_min, orig):
        def capped(pos, candle, settings):
            d = orig(pos, candle, settings)
            if d:
                return d
            if (candle.timestamp - pos.entry_time) / 60000 >= max_hold_min:
                return candle.close, "time_stop"
            return None
        return capped

    orig_exit = ob.exit_decision
    run_variant("② 15분 강제청산(실제 신호로직)", cfg, fake_ex, symbols, data_candles, base_settings,
                extra_exit_decision=make_time_capped(15, orig_exit))
    run_variant("③ 5분 강제청산(실제 신호로직)", cfg, fake_ex, symbols, data_candles, base_settings,
                extra_exit_decision=make_time_capped(5, orig_exit))


if __name__ == "__main__":
    main()
