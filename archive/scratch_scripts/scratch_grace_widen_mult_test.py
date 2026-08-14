"""[2026-08-14] STOP_LOSS_GRACE_WIDEN_MULT 2.5(baseline, live) vs 2.0 vs 1.5 검증.

방법: offline_backtest.py의 합성 스윕엔진은 쓰지 않는다(이 검증은 "진입 후 유예기간 중
손절폭"만 좁히는 국소적 변경이라 실제 체결 로직 전체를 다시 도는 게 오히려 부정확 —
scripts/postmortem.py와 동일 계열로 실제 1분봉 위에서 counterfactual 재구성한다).

핵심 통찰: STOP_LOSS_GRACE_WIDEN_MULT는 entered_at 기준 STOP_LOSS_GRACE_SEC(180초) 동안만
손절폭을 넓힌다. 180초가 지나면 baseline/variant 모두 동일한 base stop_loss_pct로 수렴하므로,
counterfactual 재구성에는 진입 후 최대 180초 구간의 1분봉만 있으면 충분하다(그 이후 구간은
mult 값과 무관하게 동일하므로 실제 결과를 그대로 쓴다).

각 거래에 대해:
  1. entered_at ~ entered_at+180s(clip to exited_at) 구간의 실제 1분봉(low/high)을 조회.
  2. side별 stop 가격 = entry_price * (1 - base_stop*mult/100/leverage) [LONG]
                        entry_price * (1 + base_stop*mult/100/leverage) [SHORT]
  3. 각 mult(2.5/2.0/1.5)에 대해 그 구간에서 stop가 처음 닿는 시각(t_cross)을 찾는다.
  4. t_cross가 실제 청산시각(exited_at)보다 먼저면 -> variant는 그 시점에 stop으로 청산된 것으로
     재구성(pnl 재계산). 아니면 -> variant 결과 = 실제 결과 그대로(그 mult에서는 grace 구간 중
     stop에 안 닿았으므로 실제 청산 로직이 그대로 적용됐을 것).

pnl 공식은 ledger 실측값 역산으로 검증한 것:
  pnl_usdt = qty*(exit-entry)*sign - entry_notional*fee_rate_roundtrip   (sign: LONG=+1, SHORT=-1)

읽기전용 REST만 사용(futures_klines, startTime/endTime), 매 호출 사이 sleep으로 스로틀.
라이브 설정/코드는 건드리지 않는다.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

import pandas as pd

from bot.config import Config
from bot.exchange import Exchange

LEDGER_PATH = Path("logs/trade_ledger.jsonl")
GRACE_SEC = 180.0
MULTS = [2.5, 2.0, 1.5]  # 2.5 = baseline(live)
SLEEP_BETWEEN = 0.25


def load_bot_trades() -> list[dict]:
    rows = []
    with open(LEDGER_PATH, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except Exception:
                continue
            if r.get("origin", "bot") != "bot":
                continue
            if not r.get("entered_at") or not r.get("exited_at"):
                continue
            rows.append(r)
    rows.sort(key=lambda r: r["entered_at"])
    return rows


def fetch_grace_window_klines(ex: Exchange, symbol: str, entered_at: float, window_end: float) -> pd.DataFrame | None:
    try:
        raw = ex.client.futures_klines(
            symbol=symbol, interval="1m",
            startTime=int((entered_at - 5) * 1000), endTime=int((window_end + 5) * 1000), limit=10,
        )
    except Exception:
        return None
    if not raw:
        return None
    df = pd.DataFrame(raw, columns=[
        "open_time", "open", "high", "low", "close", "volume",
        "close_time", "quote_asset_volume", "num_trades",
        "taker_buy_base", "taker_buy_quote", "ignore",
    ])
    for col in ("open", "high", "low", "close"):
        df[col] = df[col].astype(float)
    df["open_time_sec"] = df["open_time"] / 1000.0
    df["close_time_sec"] = df["close_time"] / 1000.0
    return df


def pnl_usdt(side: str, entry: float, exit_price: float, qty: float, fee_rt: float) -> float:
    sign = 1.0 if side == "LONG" else -1.0
    notional = entry * qty
    return qty * (exit_price - entry) * sign - notional * fee_rt


def stop_price_for(side: str, entry: float, base_stop_pct: float, mult: float, leverage: float) -> float:
    widened = base_stop_pct * mult
    if side == "LONG":
        return entry * (1 - widened / 100 / max(leverage, 1e-9))
    return entry * (1 + widened / 100 / max(leverage, 1e-9))


def find_stop_cross(df: pd.DataFrame, side: str, stop_price: float, entered_at: float, grace_end: float, exited_at: float):
    """grace 구간(entered_at~min(grace_end, exited_at)) 안에서 stop_price에 처음 닿는 캔들의
    (닿는시각, 체결가=stop_price) 반환. 없으면 None."""
    cutoff = min(grace_end, exited_at)
    for _, row in df.iterrows():
        if row["open_time_sec"] < entered_at:
            continue
        if row["open_time_sec"] >= cutoff:
            break
        if side == "LONG":
            if row["low"] <= stop_price:
                cross_t = min(row["close_time_sec"], cutoff)
                return cross_t, stop_price
        else:
            if row["high"] >= stop_price:
                cross_t = min(row["close_time_sec"], cutoff)
                return cross_t, stop_price
    return None


def main():
    cfg = Config()
    ex = Exchange(cfg)

    trades = load_bot_trades()
    print(f"bot origin 거래 {len(trades)}건 로드 (범위 {time.strftime('%Y-%m-%d %H:%M', time.localtime(trades[0]['entered_at']))} ~ "
          f"{time.strftime('%Y-%m-%d %H:%M', time.localtime(trades[-1]['entered_at']))})")

    results = []  # per trade: {mult: {"exit_price":..., "pnl":..., "cut_early": bool, "cut_time":...}}
    skipped = 0
    for i, t in enumerate(trades):
        entered_at = t["entered_at"]
        exited_at = t["exited_at"]
        side = t["side"]
        entry = t["entry_price"]
        qty = t["quantity"]
        leverage = t.get("leverage") or 1.0
        base_stop = (t.get("config_snapshot") or {}).get("stop_loss_pct", cfg.stop_loss_pct)
        fee_rt = (t.get("config_snapshot") or {}).get("fee_rate_roundtrip", cfg.fee_rate_roundtrip)
        actual_pnl = t.get("estimated_pnl_usdt", 0.0) or 0.0

        grace_end = entered_at + GRACE_SEC
        window_end = min(grace_end, exited_at)
        if window_end <= entered_at:
            skipped += 1
            continue

        df = fetch_grace_window_klines(ex, t["symbol"], entered_at, window_end)
        time.sleep(SLEEP_BETWEEN)
        if df is None or df.empty:
            skipped += 1
            continue

        per_mult = {}
        for mult in MULTS:
            sp = stop_price_for(side, entry, base_stop, mult, leverage)
            cross = find_stop_cross(df, side, sp, entered_at, grace_end, exited_at)
            if cross is not None:
                cross_t, cross_price = cross
                variant_pnl = pnl_usdt(side, entry, cross_price, qty, fee_rt)
                per_mult[mult] = {
                    "cut_early": True, "cut_time": cross_t, "exit_price": cross_price,
                    "pnl": variant_pnl,
                }
            else:
                per_mult[mult] = {
                    "cut_early": False, "cut_time": None, "exit_price": t["exit_price"],
                    "pnl": actual_pnl,
                }

        results.append({
            "symbol": t["symbol"], "side": side, "entered_at": entered_at, "exited_at": exited_at,
            "held_seconds": t.get("held_seconds"), "exit_reason": t.get("exit_reason"),
            "actual_pnl": actual_pnl, "per_mult": per_mult,
        })

        if (i + 1) % 100 == 0:
            print(f"  진행 {i+1}/{len(trades)} (skip {skipped})...")

    print(f"완료: 분석가능 {len(results)}건, skip {skipped}건")

    with open("scratch_grace_widen_mult_result.json", "w", encoding="utf-8") as f:
        json.dump(results, f)
    print("원자료 저장: scratch_grace_widen_mult_result.json")

    # ---- 요약 리포트 ----
    baseline_total = sum(r["actual_pnl"] for r in results)
    baseline_wins = sum(1 for r in results if r["actual_pnl"] > 0)
    n = len(results)
    print(f"\n=== baseline(2.5, 실측) === n={n} 순손익={baseline_total:+.3f} USDT 승률={baseline_wins/n*100:.1f}%")

    for mult in MULTS:
        if mult == 2.5:
            continue
        total = sum(r["per_mult"][mult]["pnl"] for r in results)
        wins = sum(1 for r in results if r["per_mult"][mult]["pnl"] > 0)
        n_cut = sum(1 for r in results if r["per_mult"][mult]["cut_early"])
        # 조기컷 중 원래보다 손실이 작아진(=도움된) 건 vs 원래 이득/작은손실이었는데 조기컷으로 악화된(=해된) 건
        helped = []
        hurt = []
        for r in results:
            pm = r["per_mult"][mult]
            if not pm["cut_early"]:
                continue
            delta = pm["pnl"] - r["actual_pnl"]  # variant - baseline
            if delta > 1e-9:
                helped.append((r, delta))  # 조기컷이 결과를 개선(원래 더 나쁜 손실이었는데 일찍 끊음)
            elif delta < -1e-9:
                hurt.append((r, delta))  # 조기컷이 결과를 악화(원래 이익/작은손실이었는데 일찍 끊겨 손해)

        print(f"\n=== mult={mult} === n={n} 순손익={total:+.3f} USDT (Δ{total-baseline_total:+.3f}) "
              f"승률={wins/n*100:.1f}% 조기컷건수={n_cut}")
        print(f"  조기컷 중 개선(더 일찍 잘려 손실 축소): {len(helped)}건, 합계 Δ={sum(d for _, d in helped):+.3f} USDT")
        print(f"  조기컷 중 악화(살아날 수 있었는데 일찍 잘림): {len(hurt)}건, 합계 Δ={sum(d for _, d in hurt):+.3f} USDT")
        if hurt:
            hurt_sorted = sorted(hurt, key=lambda x: x[1])[:10]
            print("  악화 상위 10건:")
            for r, delta in hurt_sorted:
                print(f"    {r['symbol']} {r['side']} entered={time.strftime('%m-%d %H:%M', time.localtime(r['entered_at']))} "
                      f"actual_pnl={r['actual_pnl']:+.3f} exit_reason={r['exit_reason']} held={r['held_seconds']:.0f}s "
                      f"variant_pnl={r['per_mult'][mult]['pnl']:+.3f} Δ={delta:+.3f}")
        if helped:
            helped_sorted = sorted(helped, key=lambda x: -x[1])[:10]
            print("  개선 상위 10건:")
            for r, delta in helped_sorted:
                print(f"    {r['symbol']} {r['side']} entered={time.strftime('%m-%d %H:%M', time.localtime(r['entered_at']))} "
                      f"actual_pnl={r['actual_pnl']:+.3f} exit_reason={r['exit_reason']} held={r['held_seconds']:.0f}s "
                      f"variant_pnl={r['per_mult'][mult]['pnl']:+.3f} Δ={delta:+.3f}")


if __name__ == "__main__":
    main()
