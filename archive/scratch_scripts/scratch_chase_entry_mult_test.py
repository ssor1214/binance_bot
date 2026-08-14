"""[2026-08-14] CHASE_ENTRY_RANGE_MULT 임계값(3.0 -> 2.0/1.5/1.2) 검증.

방법: logs/trade_ledger.jsonl 의 origin=bot 최근 5일 진입 전체를 추출, 심볼별로
15m klines(bot/main.py의 scan_entry_candidate 가 실제로 쓰는 기본 interval=15m)를
REST 배치조회(읽기전용, 스로틀)한 뒤 bot/indicators.add_indicators 로 ATR14를 복원한다.

각 진입 시점에서 "이미 닫힌 마지막 15m 캔들"(신호캔들)의 range(high-low)와 ATR을 구해
bot/main.py 의 실제 로직과 동일하게
    chase_entry = atr>0 and candle_range >= atr * mult
을 재현한다. mult in {3.0(baseline), 2.0, 1.5, 1.2} 각각에 대해:
  - chase_entry 로 판정된 진입은 실제 라이브 비중(ratio)의 90%로 축소됐다고 가정하고
    pnl_usdt 를 0.9배로 스케일해서 순손익을 재계산 (현재 로직 그대로 축소, 스킵 아님).
  - 걸린 진입의 승/패 비율, 걸린 비율(%)도 함께 계산.

lookahead 없음(진입 시각 이전에 이미 close_time <= entered_at 인 캔들만 사용).
읽기전용 REST만 사용, 라이브 주문/설정 변경 없음. 스로틀 0.25s.
"""
from __future__ import annotations

import sys
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import json
import time
from pathlib import Path

import pandas as pd

from bot.config import Config
from bot.exchange import Exchange
from bot.indicators import add_indicators

LEDGER = Path("logs/trade_ledger.jsonl")
DAYS = 5
THROTTLE_SEC = 0.25
INTERVAL = "15m"
INTERVAL_SEC = 15 * 60
CHASE_SIZE_MULT = 0.90  # cfg.chase_entry_size_mult 라이브값
CANDIDATE_MULTS = [3.0, 2.0, 1.5, 1.2]


def load_trades():
    now = time.time()
    cutoff = now - DAYS * 86400
    rows = []
    with open(LEDGER, encoding="utf-8") as f:
        for line in f:
            try:
                d = json.loads(line)
            except Exception:
                continue
            if d.get("origin") != "bot":
                continue
            if d.get("entered_at", 0) < cutoff:
                continue
            if not str(d.get("symbol", "")).isascii():
                continue
            rows.append(d)
    return rows


def fetch_klines(ex: Exchange, symbol: str, start_sec: float, end_sec: float) -> pd.DataFrame:
    all_raw = []
    cur_start = int(start_sec * 1000)
    end_ms = int(end_sec * 1000)
    while cur_start < end_ms:
        raw = ex.client.futures_klines(
            symbol=symbol, interval=INTERVAL,
            startTime=cur_start, endTime=end_ms, limit=1000,
        )
        time.sleep(THROTTLE_SEC)
        if not raw:
            break
        all_raw.extend(raw)
        last_open = raw[-1][0]
        if len(raw) < 1000:
            break
        cur_start = last_open + INTERVAL_SEC * 1000
    if not all_raw:
        return pd.DataFrame()
    df = pd.DataFrame(all_raw, columns=[
        "open_time", "open", "high", "low", "close", "volume",
        "close_time", "quote_asset_volume", "num_trades",
        "taker_buy_base", "taker_buy_quote", "ignore",
    ])
    df = df.drop_duplicates(subset="open_time").reset_index(drop=True)
    for col in ("open", "high", "low", "close", "volume", "taker_buy_base"):
        df[col] = df[col].astype(float)
    df["open_time_ms"] = df["open_time"].astype(float)
    df["close_time_ms"] = df["close_time"].astype(float)
    return df


def main():
    cfg = Config()
    trades = load_trades()
    print(f"loaded {len(trades)} bot entries (last {DAYS}d)")

    by_symbol: dict[str, list[dict]] = {}
    for t in trades:
        by_symbol.setdefault(t["symbol"], []).append(t)
    print(f"{len(by_symbol)} unique symbols -> fetching {INTERVAL} klines per symbol (throttled)")

    ex = Exchange(cfg)

    warmup_bars_needed = max(
        cfg.ema_slow, cfg.macd_slow, cfg.bb_period, cfg.atr_period,
        cfg.adx_period, cfg.volume_ma_period, cfg.stoch_rsi_period, cfg.rsi_period,
    ) + cfg.macd_signal + 10
    warmup_sec = warmup_bars_needed * INTERVAL_SEC

    now = time.time()
    global_start_default = min(t["entered_at"] for t in trades) - warmup_sec - 3600

    results = []
    skipped_symbols = []

    for i, (symbol, sym_trades) in enumerate(sorted(by_symbol.items())):
        sym_min_entry = min(t["entered_at"] for t in sym_trades)
        start = sym_min_entry - warmup_sec - 3600
        try:
            df = fetch_klines(ex, symbol, start, now)
            if df.empty or len(df) < warmup_bars_needed + 5:
                skipped_symbols.append((symbol, "insufficient_klines", len(df)))
                continue
            df_ind = add_indicators(df, cfg)
        except Exception as e:
            skipped_symbols.append((symbol, str(e), 0))
            continue

        for t in sym_trades:
            entered_at = t["entered_at"]
            entered_ms = entered_at * 1000
            closed = df_ind[df_ind["close_time_ms"] <= entered_ms]
            if closed.empty:
                continue
            curr = closed.iloc[-1]
            if pd.isna(curr.get("atr")):
                continue
            atr_value = float(curr["atr"] or 0.0)
            candle_range = float(curr["high"]) - float(curr["low"])
            ratio = (candle_range / atr_value) if atr_value > 0 else float("nan")

            results.append({
                "symbol": symbol,
                "side": t["side"],
                "entered_at": float(entered_at),
                "entered_iso": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(entered_at)),
                "pnl_usdt": t.get("estimated_pnl_usdt"),
                "atr": atr_value,
                "candle_range": candle_range,
                "ratio": float(ratio),
                "exit_reason": t.get("exit_reason"),
            })
        print(f"[{i+1}/{len(by_symbol)}] {symbol}: {len(sym_trades)} trades processed, klines={len(df)}")

    print(f"\nmatched {len(results)} / {len(trades)} trades with valid ATR data")
    if skipped_symbols:
        print(f"skipped {len(skipped_symbols)} symbols:")
        for s in skipped_symbols[:20]:
            print("  ", s)

    out_path = Path("scratch_chase_entry_mult_results.json")
    out_path.write_text(json.dumps(results, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"saved raw results -> {out_path}")

    # ---- report ----
    pnl_key = "pnl_usdt"
    valid = [r for r in results if r.get(pnl_key) is not None and not pd.isna(r["ratio"])]
    total_pnl_baseline = sum(r[pnl_key] for r in valid)
    wins = [r for r in valid if r[pnl_key] > 0]
    winrate = len(wins) / len(valid) * 100 if valid else 0

    lines = []
    lines.append(f"=== CHASE_ENTRY_RANGE_MULT 임계값 검증 (실 라이브값=3.0, chase_size_mult={CHASE_SIZE_MULT}) ===\n")
    lines.append(f"총 {len(trades)}건 중 ATR 계산가능 {len(valid)}건\n")
    lines.append(f"실제 라이브 순손익(현재 이미 3.0에서 축소 적용된 상태) = {total_pnl_baseline:+.3f} USDT, "
                 f"승률={winrate:.1f}% (승{len(wins)}/패{len(valid)-len(wins)})\n\n")

    for mult in CANDIDATE_MULTS:
        hit = [r for r in valid if r["ratio"] >= mult]
        miss = [r for r in valid if r["ratio"] < mult]
        hit_wins = [r for r in hit if r[pnl_key] > 0]
        hit_losses = [r for r in hit if r[pnl_key] <= 0]
        hit_pnl = sum(r[pnl_key] for r in hit)
        hit_winrate = len(hit_wins) / len(hit) * 100 if hit else 0
        pct_hit = len(hit) / len(valid) * 100 if valid else 0

        # 현재 로직: baseline(3.0)에서 이미 hit된 건은 실적립된 pnl이 0.9배 축소된 값.
        # mult가 3.0이면 그대로 baseline. mult < 3.0이면 (3.0에서는 안 걸렸지만 새 mult에서
        # 걸리는) 추가 대상들의 pnl을 "원래(비축소) 값"으로 역산 후 0.9배 추가 축소해서
        # 순손익 변화를 재계산해야 정확하다. 여기서는 근사: 이미 축소된 상태의 pnl은 유지하고,
        # baseline에서 안 걸렸던(ratio<3.0) 대상이 새 mult에서 걸리면 추가로 0.9배 재축소한다.
        baseline_hit_symbols = {(r["symbol"], r["entered_at"]) for r in valid if r["ratio"] >= 3.0}
        newly_hit = [r for r in hit if (r["symbol"], r["entered_at"]) not in baseline_hit_symbols]
        already_hit_in_baseline = [r for r in hit if (r["symbol"], r["entered_at"]) in baseline_hit_symbols]

        adj_pnl = sum(r[pnl_key] for r in already_hit_in_baseline)  # 이미 축소된 값 그대로
        adj_pnl += sum(r[pnl_key] * CHASE_SIZE_MULT for r in newly_hit)  # 추가축소
        adj_pnl += sum(r[pnl_key] for r in miss)  # 안 걸린 건 원래 라이브 그대로(축소 없음, ratio<mult)
        # 주의: mult>3.0(해당없음, 후보에 없음)인 경우는 미고려

        lines.append(
            f"mult={mult}:\n"
            f"  걸린 진입(비중90%): {len(hit)}건 ({pct_hit:.1f}%) 승{len(hit_wins)}/패{len(hit_losses)} "
            f"승률={hit_winrate:.1f}% 걸린분pnl(축소전 실측합)={hit_pnl:+.3f}\n"
            f"  조정후 전체 순손익(신규 걸린 건만 추가 0.9배 축소 적용) = {adj_pnl:+.3f} USDT "
            f"(baseline 대비 {adj_pnl - total_pnl_baseline:+.3f})\n\n"
        )

    with open("scratch_chase_entry_mult_report.txt", "w", encoding="utf-8") as f:
        f.writelines(lines)
    print("".join(lines))


if __name__ == "__main__":
    main()
