"""[2026-08-14] 트레일링스탑 giveback(반납비율) 분석 + 대안 파라미터 시뮬레이션.

1) giveback_raw.json (peak ROE vs 실제 exit ROE)의 반납비율 분포 통계.
2) 실제 청산 로직(offline_backtest.exit_decision, arm/peak 갱신 로직)을 그대로 재사용해서
   entry~exit(+recovery 여유) 구간의 1분봉 위에서 대안 trail_drawdown_pct/take_profit_min을
   재현 시뮬레이션 — lookahead 없이 캔들 순서대로 진행.
"""
from __future__ import annotations
import json
import sys
from pathlib import Path
from statistics import mean, median

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from offline_backtest import Candle, Position, Settings, exit_decision  # noqa: E402

RAW_PATH = Path(__file__).resolve().parent / "giveback_raw.json"
KLINES_CACHE = Path(__file__).resolve().parent / "giveback_klines_cache.json"


def load_raw():
    return json.loads(RAW_PATH.read_text(encoding="utf-8"))


def print_giveback_stats(rows):
    print("=== Peak ROE 대비 실제 확정 ROE 반납비율(giveback) 분석 ===")
    print(f"표본수: {len(rows)}")

    givebacks = []
    for r in rows:
        peak = r["peak_roe"]
        exitr = r["exit_roe"]
        if peak <= 0:
            continue
        gb = (peak - exitr) / peak
        givebacks.append(gb)

    givebacks.sort()
    print(f"평균 giveback: {mean(givebacks)*100:.1f}%")
    print(f"중앙값 giveback: {median(givebacks)*100:.1f}%")
    print(f"평균 peak ROE: {mean(r['peak_roe'] for r in rows):.2f}%")
    print(f"평균 exit ROE: {mean(r['exit_roe'] for r in rows):.2f}%")
    print(f"평균 (peak-exit) %p: {mean(r['peak_roe']-r['exit_roe'] for r in rows):.2f}%p")

    buckets = [(0, 0.1), (0.1, 0.25), (0.25, 0.4), (0.4, 0.6), (0.6, 1.0), (1.0, 999)]
    for lo, hi in buckets:
        c = sum(1 for g in givebacks if lo <= g < hi)
        print(f"  giveback {lo*100:.0f}~{hi*100:.0f}%: {c}건 ({c/len(givebacks)*100:.1f}%)")

    # peak_roe == exit_roe (즉시 하드캡/이례적 케이스) 별도 표시
    zero_gb = sum(1 for g in givebacks if g < 0.001)
    print(f"거의 반납 없음(<0.1%): {zero_gb}건 ({zero_gb/len(givebacks)*100:.1f}%)")
    huge_gb = sum(1 for g in givebacks if g > 0.5)
    print(f"50% 이상 반납: {huge_gb}건 ({huge_gb/len(givebacks)*100:.1f}%)")

    # 절대 %p 반납이 큰 케이스(예: 3%p 이상) 개수
    big_abs = [r for r in rows if r["peak_roe"] - r["exit_roe"] >= 3.0]
    print(f"절대 3%p 이상 반납: {len(big_abs)}건 ({len(big_abs)/len(rows)*100:.1f}%)")

    return givebacks


def fetch_or_load_klines(ex, symbol, start_sec, end_sec):
    cache = {}
    if KLINES_CACHE.exists():
        cache = json.loads(KLINES_CACHE.read_text(encoding="utf-8"))
    key = f"{symbol}_{int(start_sec)}_{int(end_sec)}"
    if key in cache:
        return cache[key]
    raw = ex.client.futures_klines(
        symbol=symbol, interval="1m",
        startTime=int(start_sec * 1000) - 5000, endTime=int(end_sec * 1000) + 5000,
        limit=1000,
    )
    cache[key] = raw
    KLINES_CACHE.write_text(json.dumps(cache), encoding="utf-8")
    return raw


def simulate_variant(rows, klines_by_trade, leverage_default, settings: Settings):
    """실제 청산 로직(exit_decision)을 그대로 재사용해, 각 거래의 entry_price/side/leverage로
    구성한 Position을 만들고 실제 klines 경로를 캔들 순서대로 흘려서 새 파라미터 기준
    (target/trailing/stop 우선순위 exit_decision 그대로) 청산 시점/가격을 재현한다.
    STOP_LOSS로 재현되는 경우도 있을 수 있음(원본은 TP였지만 더 타이트한 트레일링이면
    peak 도달 전 stop 걸릴 가능성 낮음 — stop_roe_pct는 원래 설정 그대로 사용)."""
    exit_rois = []
    reasons = {"stop_loss": 0, "trailing_stop": 0, "hard_take_profit": 0, "none(unresolved)": 0}
    for r in rows:
        key = r["_key"]
        candles = klines_by_trade.get(key)
        if not candles:
            continue
        leverage = r.get("leverage") or leverage_default
        s = Settings(
            leverage=leverage,
            stop_roe_pct=settings.stop_roe_pct,
            short_stop_roe_pct=settings.stop_roe_pct,
            take_profit_roe_pct=(settings.short_take_profit_roe_pct if r["side"] == "SHORT" else settings.take_profit_roe_pct),
            hard_take_profit_roe_pct=settings.hard_take_profit_roe_pct,
            trailing_drawdown_roe_pct=settings.trailing_drawdown_roe_pct,
        )
        pos = Position(
            symbol=r["symbol"], side=r["side"], entry_time=0, entry_price=r["entry_price"],
            quantity=1.0, margin=1.0, entry_fee=0.0, peak_price=r["entry_price"],
        )
        resolved = None
        for k in candles:
            c = Candle(timestamp=k[0], open=float(k[1]), high=float(k[2]), low=float(k[3]),
                       close=float(k[4]), volume=float(k[5]), quote_volume=0.0, taker_buy_volume=0.0)
            # arm/peak 갱신 (offline_backtest.run_backtest 내부 로직과 동일한 순서로 재현)
            favorable = c.high if pos.side == "LONG" else c.low
            pos.peak_price = max(pos.peak_price, favorable) if pos.side == "LONG" else min(pos.peak_price, favorable)
            roe = ((pos.peak_price / pos.entry_price - 1) * (1 if pos.side == "LONG" else -1) * leverage * 100)
            if roe >= s.take_profit_roe_pct:
                pos.trailing_armed = True

            decision = exit_decision(pos, c, s)
            if decision is not None:
                price, reason = decision
                exit_roe = ((price / pos.entry_price - 1) * (1 if pos.side == "LONG" else -1) * leverage * 100)
                resolved = (exit_roe, reason)
                break
        if resolved is None:
            reasons["none(unresolved)"] += 1
            continue
        exit_rois.append(resolved[0])
        reasons[resolved[1]] = reasons.get(resolved[1], 0) + 1
    return exit_rois, reasons


def main():
    rows = load_raw()
    givebacks = print_giveback_stats(rows)

    print("\n=== 대안 파라미터 재현 시뮬레이션 준비: klines 재사용(entry~exit+여유) ===")
    from bot.config import Config
    from bot.exchange import Exchange
    import time

    cfg = Config()
    ex = Exchange(cfg)

    klines_by_trade = {}
    n = len(rows)
    for i, r in enumerate(rows):
        key = r["_key"]
        # exit 이후 20분 더 확보 — 더 넓은 트레일링폭이 원래 exit 시점 이후까지 안 끊기고
        # 더 버텼을 경우를 재현하기 위함
        end_sec = r["exited_at"] + 20 * 60
        raw = fetch_or_load_klines(ex, r["symbol"], r["entered_at"], end_sec)
        hold = [k for k in raw if k[0] >= r["entered_at"] * 1000]
        klines_by_trade[key] = hold
        if (i + 1) % 50 == 0:
            print(f"  klines 준비 {i+1}/{n}")
        time.sleep(0.02)  # 대부분 캐시 히트라 거의 API 호출 없음

    # baseline: 라이브 설정
    baseline_settings = Settings(
        stop_roe_pct=999,  # 아래에서 개별 지정
    )

    def make_settings(trail_dd, tp_min_long, tp_min_short, hard_cap, stop_roe):
        s = Settings()
        s.trailing_drawdown_roe_pct = trail_dd
        s.take_profit_roe_pct = tp_min_long
        s.short_take_profit_roe_pct = tp_min_short  # not a real field; injected below
        s.hard_take_profit_roe_pct = hard_cap
        s.stop_roe_pct = stop_roe
        s.short_stop_roe_pct = stop_roe
        return s

    # Settings dataclass has no short_take_profit_roe_pct field; add dynamically via simple namespace
    class VariantCfg:
        def __init__(self, trail_dd, tp_min_long, tp_min_short, hard_cap, stop_roe):
            self.trailing_drawdown_roe_pct = trail_dd
            self.take_profit_roe_pct = tp_min_long
            self.short_take_profit_roe_pct = tp_min_short
            self.hard_take_profit_roe_pct = hard_cap
            self.stop_roe_pct = stop_roe

    # 실거래 stop_roe_pct는 config.py stop_loss_pct 계열 — .env 기준 대략 3~4% 구간(early-hold widened).
    # 여기선 시뮬레이션 목적상 baseline stop을 넉넉히(6%) 잡아 "TP/트레일링 로직 자체"의 효과만
    # 비교한다(실제 STOP_LOSS는 원 거래에서 이미 배제된 표본이므로 손절 재현 오염을 최소화).
    STOP_ROE_FOR_SIM = 6.0

    variants = {
        "baseline (TRAIL=1.3)": VariantCfg(1.3, 3.0, 4.0, 20.0, STOP_ROE_FOR_SIM),
        "TRAIL=0.5": VariantCfg(0.5, 3.0, 4.0, 20.0, STOP_ROE_FOR_SIM),
        "TRAIL=0.7": VariantCfg(0.7, 3.0, 4.0, 20.0, STOP_ROE_FOR_SIM),
        "TRAIL=0.9": VariantCfg(0.9, 3.0, 4.0, 20.0, STOP_ROE_FOR_SIM),
        "TRAIL=1.1": VariantCfg(1.1, 3.0, 4.0, 20.0, STOP_ROE_FOR_SIM),
        "TRAIL=2.0": VariantCfg(2.0, 3.0, 4.0, 20.0, STOP_ROE_FOR_SIM),
    }

    print("\n=== 변형별 결과 (재현 시뮬레이션, 동일 실제 가격경로) ===")
    header = f"{'variant':40s} {'n':>5s} {'avg_exit_roe':>13s} {'median_exit_roe':>16s} {'stop_loss건':>10s} {'trailing건':>10s} {'hardcap건':>10s} {'미확정':>6s}"
    print(header)
    for name, s in variants.items():
        exit_rois, reasons = simulate_variant(rows, klines_by_trade, cfg.leverage if hasattr(cfg, "leverage") else 5, s)
        avg = mean(exit_rois) if exit_rois else float("nan")
        med = median(exit_rois) if exit_rois else float("nan")
        print(f"{name:40s} {len(exit_rois):>5d} {avg:>13.3f} {med:>16.3f} "
              f"{reasons.get('stop_loss',0):>10d} {reasons.get('trailing_stop',0):>10d} "
              f"{reasons.get('hard_take_profit',0):>10d} {reasons.get('none(unresolved)',0):>6d}")

    # baseline과 실제 라이브 청산 exit_roe(원 데이터) 비교
    actual_avg = mean(r["exit_roe"] for r in rows)
    print(f"\n[참고] 실제 라이브 청산 평균 exit ROE(TP 계열, {len(rows)}건): {actual_avg:.3f}%")


if __name__ == "__main__":
    main()
