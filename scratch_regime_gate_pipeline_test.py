"""[2026-08-11 사용자요청] "휩쏘 회피" B안 — BTC 자체 변동성(5분봉 ATR%)이 최근 100개
5분봉 대비 하위 30퍼센타일(상대적 저변동성=횡보 의심 구간)이면 신규 진입을 시장 전체적으로
차단하는 레짐게이트를 추가해서, 어제 검증한 "간소화 안 한" 진짜 파이프라인
(scan_entry_candidate 전체)으로 재검증한다. 고정 임계값 대신 BTC 자신의 최근 분포 대비
상대값을 쓰므로 과최적화 위험이 낮다.

scratch_full_pipeline_backtest.py의 FakeExchange/run_variant을 그대로 재사용(신규 재작성
없음). 실 API 호출 없음."""
from __future__ import annotations

import bisect
from pathlib import Path

import offline_backtest as ob

from bot.config import Config
from bot.main import compute_leverage, scan_entry_candidate
from scratch_full_pipeline_backtest import FakeExchange, load_raw, run_variant

DATA_PATH = Path("scratch_klines_v4.json")
LOOKBACK_5M_BARS = 100
CHOPPY_PERCENTILE = 0.30  # 최근 100개 5분봉 ATR% 중 하위 30%면 "횡보"


def build_choppy_series(raw_btc: list, lookback: int = LOOKBACK_5M_BARS, percentile: float = CHOPPY_PERCENTILE) -> dict[int, bool]:
    """BTC 1분봉 -> 5분봉 ATR%를 시점순으로 계산하며, 각 5분봉 종료 시점마다 "그 시점까지만
    보이는" 과거 lookback개 ATR%의 하위 percentile 이하인지 표시한다(미래데이터 유출 없음)."""
    rows = sorted(raw_btc, key=lambda r: r[0])
    bars_5m = []
    bucket = []
    for r in rows:
        bucket.append(r)
        if len(bucket) == 5:
            o = float(bucket[0][1])
            h = max(float(x[2]) for x in bucket)
            l = min(float(x[3]) for x in bucket)
            c = float(bucket[-1][4])
            bars_5m.append((bucket[-1][0], h, l, c))
            bucket = []

    choppy: dict[int, bool] = {}
    trs: list[float] = []
    atr_history: list[float] = []
    prev_close = None
    for ts, h, l, c in bars_5m:
        tr = (h - l) if prev_close is None else max(h - l, abs(h - prev_close), abs(l - prev_close))
        trs.append(tr)
        atr14 = sum(trs[-14:]) / min(14, len(trs))
        atr_pct = (atr14 / c * 100) if c else 0.0
        # 판단은 "이번 값을 포함하기 전" 과거 lookback개 분포 기준으로 한다(자기 자신을 분포에
        # 넣으면 판단이 왜곡될 수 있음 — 상대적으로 낮은지는 과거 대비로만 본다).
        past_window = atr_history[-lookback:]
        if len(past_window) >= 20:
            sorted_w = sorted(past_window)
            idx = max(0, int(len(sorted_w) * percentile) - 1)
            threshold = sorted_w[idx]
            choppy[ts] = atr_pct <= threshold
        else:
            choppy[ts] = False  # 워밍업 구간은 게이트 없이 통과
        atr_history.append(atr_pct)
        prev_close = c
    return choppy


def make_regime_gated_signal_for_symbol(sym, cfg, fake_ex, approx_balance, choppy_ts_sorted, choppy_map):
    def sig(history, _settings):
        ts = history[-1].timestamp
        # 이 1분봉 시각 기준으로 "가장 최근에 확정된" 5분봉의 횡보여부를 찾는다.
        idx = bisect.bisect_right(choppy_ts_sorted, ts) - 1
        if idx >= 0 and choppy_map[choppy_ts_sorted[idx]]:
            return None  # 시장 전체 레짐이 횡보로 판단되면 이 심볼 신호와 무관하게 진입 차단
        fake_ex.set_now(sym, ts)
        try:
            candidate = scan_entry_candidate(fake_ex, cfg, sym, approx_balance)
        except Exception:
            return None
        if not candidate:
            return None
        return candidate["signal"]
    return sig


def run_regime_gated_variant(label, cfg, fake_ex, symbols, data_candles, settings, choppy_map):
    choppy_ts_sorted = sorted(choppy_map.keys())
    baseline_signal = ob.signal
    total_trades_all = []
    for symbol in symbols:
        ob.signal = make_regime_gated_signal_for_symbol(symbol, cfg, fake_ex, settings.starting_balance, choppy_ts_sorted, choppy_map)
        result = ob.run_backtest({symbol: data_candles[symbol]}, settings)
        total_trades_all.extend(result["ledger"])
    ob.signal = baseline_signal

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
    cfg = Config()
    raw = load_raw(DATA_PATH)
    symbols = list(raw.keys())
    fake_ex = FakeExchange(raw)
    data_candles, _ = ob.load_data(DATA_PATH)

    leverage_est = compute_leverage(0.8, cfg)
    base_settings = ob.Settings(
        margin_fraction=cfg.position_size_min, average_down=False, leverage=float(leverage_est),
        stop_roe_pct=cfg.stop_loss_pct, take_profit_roe_pct=cfg.take_profit_min,
        trailing_drawdown_roe_pct=cfg.trail_drawdown_pct, hard_take_profit_roe_pct=cfg.take_profit_hard_cap,
    )

    choppy_map = build_choppy_series(raw["BTCUSDT"])
    choppy_count = sum(1 for v in choppy_map.values() if v)
    print(f"BTC 5분봉 {len(choppy_map)}개 중 '횡보'로 판정된 비율: {choppy_count/len(choppy_map)*100:.1f}%")

    run_variant("① 기준(레짐게이트 없음, 실제 신호로직) — 참고용 재실행", cfg, fake_ex, symbols, data_candles, base_settings)
    run_regime_gated_variant("④ BTC 레짐게이트(하위30% ATR시 진입차단) 적용", cfg, fake_ex, symbols, data_candles, base_settings, choppy_map)


if __name__ == "__main__":
    main()
