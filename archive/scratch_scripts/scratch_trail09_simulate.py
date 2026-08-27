"""[2026-08-14] TRAIL_DRAWDOWN_PCT 1.3(baseline, 현재 라이브) vs 0.9(제안값) 전체모집단 검증.

- 대상: logs/trade_ledger.jsonl origin=bot 전체 거래(exit_reason 무관, 약 1222건).
- offline_backtest.exit_decision()을 그대로 재사용해 entry~exit+20분 구간의 실제 1분봉
  경로 위에서 캔들 순서대로(lookahead 없이) 시퀀셜 재현 청산 시뮬레이션.
- 각 거래의 실제 config_snapshot(stop_loss_pct, take_profit_min/short_take_profit_min,
  take_profit_hard_cap, trail_drawdown_pct, leverage)을 그대로 사용 — 임의 고정값 사용 안 함.
- baseline은 config_snapshot의 실제 trail_drawdown_pct를 그대로 사용(원 라이브값 재현),
  variant는 trail_drawdown_pct만 0.9로 교체.
"""
from __future__ import annotations
import json
import sys
from pathlib import Path
from statistics import mean

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from offline_backtest import Candle, Position, Settings, exit_decision  # noqa: E402

BASE = Path(__file__).resolve().parent
RAW_PATH = BASE / "trail09_all_trades_raw.json"
CACHE_PATH = BASE / "giveback_klines_cache.json"

FEE_RATE_ROUNDTRIP_DEFAULT = 0.001  # config_snapshot에 있으면 그 값 사용


def load_rows():
    rows = json.loads(RAW_PATH.read_text(encoding="utf-8"))
    cache = json.loads(CACHE_PATH.read_text(encoding="utf-8"))
    for r in rows:
        r["_klines"] = cache.get(r["_cache_key"]) or []
    return rows


def make_settings(cfg_snap, leverage, trail_override=None, side="LONG"):
    trail = trail_override if trail_override is not None else cfg_snap.get("trail_drawdown_pct", 1.3)
    tp = cfg_snap.get("short_take_profit_min", 4.0) if side == "SHORT" else cfg_snap.get("take_profit_min", 3.0)
    return Settings(
        leverage=leverage,
        stop_roe_pct=cfg_snap.get("stop_loss_pct", 3.5),
        short_stop_roe_pct=cfg_snap.get("stop_loss_pct", 3.5),
        take_profit_roe_pct=tp,
        hard_take_profit_roe_pct=cfg_snap.get("take_profit_hard_cap", 20.0),
        trailing_drawdown_roe_pct=trail,
    )


def simulate_trade(r, trail_override=None):
    """exit_decision을 그대로 사용해 시퀀셜 재현. run_backtest 내부와 동일한 순서로
    peak/trailing_armed 갱신 후 exit_decision 호출."""
    candles_raw = r["_klines"]
    if not candles_raw:
        return None
    entered_at_ms = r["entered_at"] * 1000
    hold = [k for k in candles_raw if k[0] >= entered_at_ms]
    if not hold:
        return None
    leverage = r.get("leverage") or 4
    cfg_snap = r.get("config_snapshot") or {}
    settings = make_settings(cfg_snap, leverage, trail_override=trail_override, side=r["side"])
    fee_rt = cfg_snap.get("fee_rate_roundtrip", FEE_RATE_ROUNDTRIP_DEFAULT)

    pos = Position(symbol=r["symbol"], side=r["side"], entry_time=hold[0][0],
                    entry_price=r["entry_price"], quantity=1.0, margin=1.0, entry_fee=0.0,
                    peak_price=r["entry_price"])
    resolved = None
    for k in hold:
        c = Candle(timestamp=k[0], open=float(k[1]), high=float(k[2]), low=float(k[3]),
                   close=float(k[4]), volume=float(k[5]), quote_volume=0.0, taker_buy_volume=0.0)
        decision = exit_decision(pos, c, settings)
        if decision is not None:
            price, reason = decision
            resolved = (price, reason, c.timestamp)
            break
        # run_backtest 순서: exit_decision 먼저 체크(직전 캔들까지 armed 상태 기준),
        # 그 다음에야 이번 캔들 high/low로 peak/trailing_armed 갱신.
        favorable = c.high if pos.side == "LONG" else c.low
        pos.peak_price = max(pos.peak_price, favorable) if pos.side == "LONG" else min(pos.peak_price, favorable)
        roe = ((pos.peak_price / pos.entry_price - 1) * (1 if pos.side == "LONG" else -1) * leverage * 100)
        if roe >= settings.take_profit_roe_pct:
            pos.trailing_armed = True

    if resolved is None:
        # 데이터 구간(20분 여유) 내에 stop/trail/target 어느 것도 안 걸림 -> 구간 마지막 종가로 강제청산
        last = hold[-1]
        price = float(last[4])
        resolved = (price, "unresolved_end_of_window", last[0])

    exit_price, reason, exit_ts = resolved
    exit_roe_gross = ((exit_price / r["entry_price"] - 1) * (1 if r["side"] == "LONG" else -1) * leverage * 100)
    exit_roe_net = exit_roe_gross - fee_rt * leverage * 100  # fee_rate_roundtrip은 notional 기준 비율
    return {
        "symbol": r["symbol"], "side": r["side"], "leverage": leverage,
        "exit_reason_sim": reason, "exit_roe_gross": exit_roe_gross, "exit_roe_net": exit_roe_net,
        "held_min_sim": (exit_ts - entered_at_ms) / 60000,
        "actual_exit_reason": r.get("exit_reason"), "actual_pnl_pct_roe": r.get("estimated_pnl_pct"),
    }


def summarize(results, label):
    n = len(results)
    if n == 0:
        print(f"{label}: 표본 없음")
        return
    wins = [x for x in results if x["exit_roe_net"] > 0]
    losses = [x for x in results if x["exit_roe_net"] <= 0]
    win_rate = len(wins) / n * 100
    gross_profit = sum(x["exit_roe_net"] for x in wins)
    gross_loss = abs(sum(x["exit_roe_net"] for x in losses))
    pf = gross_profit / gross_loss if gross_loss else float("inf")
    net_roe_sum = sum(x["exit_roe_net"] for x in results)
    avg_held = mean(x["held_min_sim"] for x in results)
    from collections import Counter
    reason_counts = Counter(x["exit_reason_sim"] for x in results)
    print(f"\n=== {label} (n={n}) ===")
    print(f"  승률: {win_rate:.1f}%  (승 {len(wins)} / 패 {len(losses)})")
    print(f"  순ROE합: {net_roe_sum:.1f}%p   PF: {pf:.3f}")
    print(f"  평균보유(분, 시뮬 청산기준): {avg_held:.1f}")
    print(f"  exit_reason 분해: {dict(reason_counts)}")
    return {"n": n, "win_rate": win_rate, "net_roe_sum": net_roe_sum, "pf": pf,
            "avg_held": avg_held, "reasons": dict(reason_counts), "wins": len(wins), "losses": len(losses)}


def main():
    rows = load_rows()
    print(f"전체 거래(origin=bot): {len(rows)}건")
    no_klines = sum(1 for r in rows if not r["_klines"])
    print(f"klines 없음(제외 대상): {no_klines}건")

    baseline_results, variant_results = [], []
    for r in rows:
        b = simulate_trade(r, trail_override=None)  # config_snapshot 실제 trail_drawdown_pct 그대로
        v = simulate_trade(r, trail_override=0.9)
        if b:
            baseline_results.append(b)
        if v:
            variant_results.append(v)

    b_stats = summarize(baseline_results, "baseline (config_snapshot 실제 trail_drawdown_pct, 라이브값 재현)")
    v_stats = summarize(variant_results, "variant (TRAIL_DRAWDOWN_PCT=0.9로 강제)")

    # 재현 정확도: 실제 라이브 exit_reason 대비 baseline 시뮬 exit_reason 매치율
    print("\n=== 재현 정확도(실제 라이브 vs baseline 시뮬) ===")
    reason_map = {
        "stop_loss": "STOP_LOSS", "trailing_stop": {"TAKE_PROFIT", "TAKE_PROFIT_MOMENTUM_LOCK"},
        "hard_take_profit": {"TAKE_PROFIT", "TAKE_PROFIT_MOMENTUM_LOCK"},
    }
    match = 0
    comparable = 0
    for x in baseline_results:
        actual = x["actual_exit_reason"]
        sim = x["exit_reason_sim"]
        if actual in ("EARLY_EXIT", "SOFT_STOP", "EXTERNAL_CLOSE_LOSS", "EXTERNAL_CLOSE_PROFIT", "FUNDING_FORCE_CLOSE"):
            continue  # exit_decision이 모델링하지 않는 별도 로직 경로 -> 비교 대상에서 제외, 별도 카운트
        comparable += 1
        expected = reason_map.get(sim)
        if isinstance(expected, set):
            if actual in expected:
                match += 1
        elif actual == expected:
            match += 1
    print(f"  exit_decision 모델링 대상(STOP_LOSS/TAKE_PROFIT계열) 실제거래: {comparable}건")
    print(f"  baseline 시뮬 exit_reason 일치: {match}/{comparable} ({match/comparable*100:.1f}%)" if comparable else "  비교 대상 없음")

    other_reason_n = sum(1 for x in baseline_results if x["actual_exit_reason"] in
                          ("EARLY_EXIT", "SOFT_STOP", "EXTERNAL_CLOSE_LOSS", "EXTERNAL_CLOSE_PROFIT", "FUNDING_FORCE_CLOSE"))
    print(f"  exit_decision 미모델링(EARLY_EXIT/SOFT_STOP/EXTERNAL_CLOSE/FUNDING) 실제거래: {other_reason_n}건"
          f" ({other_reason_n/len(baseline_results)*100:.1f}% of {len(baseline_results)})")

    # 실제 라이브 순ROE합/승률 (참고)
    actual_wins = sum(1 for r in rows if (r.get("estimated_pnl_usdt") or 0) > 0)
    actual_win_rate = actual_wins / len(rows) * 100
    actual_pnl_sum = sum(r.get("estimated_pnl_usdt") or 0 for r in rows)
    actual_roe_sum = sum(r.get("estimated_pnl_pct") or 0 for r in rows)
    print(f"\n=== 참고: 실제 라이브 기록 그대로(1222건) ===")
    print(f"  실제 승률: {actual_win_rate:.1f}%  실제 순PnL합(USDT): {actual_pnl_sum:.2f}  실제 순ROE합: {actual_roe_sum:.1f}%p")

    # 시간당 거래수(실측)
    import datetime
    ts_sorted = sorted(r["entered_at"] for r in rows)
    span_hours = (ts_sorted[-1] - ts_sorted[0]) / 3600
    print(f"\n=== 실측 시간당 거래수 ===")
    print(f"  기간: {datetime.datetime.utcfromtimestamp(ts_sorted[0])} ~ {datetime.datetime.utcfromtimestamp(ts_sorted[-1])} UTC ({span_hours:.1f}시간)")
    print(f"  거래수/시간: {len(rows)/span_hours:.2f}건/h")

    out = {
        "baseline": b_stats, "variant_0_9": v_stats,
        "reproduction_match": match, "reproduction_comparable": comparable,
        "other_reason_n": other_reason_n,
        "actual_win_rate": actual_win_rate, "actual_pnl_sum": actual_pnl_sum, "actual_roe_sum": actual_roe_sum,
        "trades_per_hour_actual": len(rows) / span_hours,
    }
    (BASE / "trail09_summary.json").write_text(json.dumps(out, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\n요약 저장: {BASE / 'trail09_summary.json'}")


if __name__ == "__main__":
    main()
