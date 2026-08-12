"""[2026-08-12 사용자요청] SOFT_STOP_MIN_LOSS_ROE/EARLY_EXIT_MIN_LOSS_ROE 1.5->2.5 변경
효과를 1분봉으로 재현. offline_backtest.py엔 이 로직이 없어(라이브 전용) 대신 실제 봇의
PositionManager.evaluate()/check_early_exit()/check_hourly_soft_stop()를 그대로
재사용해서, 과거 실제로 SOFT_STOP/EARLY_EXIT로 청산됐던 포지션들을 새 기준(2.5%)으로
다시 관리했다면 어떤 결과였을지 1분봉 실데이터로 재현한다. 읽기전용 REST만 사용."""
import json
import time
from pathlib import Path

import pandas as pd

from bot.config import Config
from bot.exchange import Exchange
from bot.position_manager import PositionManager
from bot.main import check_early_exit, check_hourly_soft_stop
from scratch_trade_postmortem import fetch_historical_klines

LEDGER_PATH = Path("logs/trade_ledger.jsonl")
SINCE = time.mktime(time.strptime("2026-08-11 20:38:06", "%Y-%m-%d %H:%M:%S"))
REPLAY_WINDOW_MIN = 30  # 원래 청산 시점부터 이만큼 더 데이터를 갖고 재현


class ReplayExchange:
    """1분봉 데이터를 시점별로 흉내내는 최소 FakeExchange. get_mark_price/get_klines만
    구현(check_early_exit/check_hourly_soft_stop/evaluate에 필요한 전부)."""

    def __init__(self, df_1m):
        self.df_1m = df_1m.reset_index(drop=True)
        self.now_idx = 0

    def set_now(self, idx):
        self.now_idx = idx

    def get_mark_price(self, symbol):
        return float(self.df_1m.iloc[self.now_idx]["close"])

    def get_klines(self, symbol, limit=99, interval=None):
        visible = self.df_1m.iloc[:self.now_idx + 1]
        if interval and interval != "1m":
            step = {"5m": 5, "15m": 15, "1h": 60}.get(interval, 1)
            n_complete = len(visible) // step
            if n_complete == 0:
                return visible.iloc[0:0]
            rows = []
            for b in range(n_complete):
                bucket = visible.iloc[b * step:(b + 1) * step]
                rows.append({
                    "open_time": bucket.iloc[0]["open_time"], "open": bucket.iloc[0]["open"],
                    "high": bucket["high"].max(), "low": bucket["low"].min(), "close": bucket.iloc[-1]["close"],
                    "volume": bucket["volume"].sum(), "taker_buy_base": bucket["taker_buy_base"].sum(),
                })
            return pd.DataFrame(rows).iloc[-limit:].reset_index(drop=True)
        return visible.iloc[-limit:].reset_index(drop=True)


def load_trades():
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
            if r["entered_at"] < SINCE:
                continue
            if r.get("exit_reason") not in ("SOFT_STOP", "EARLY_EXIT"):
                continue
            rows.append(r)
    return rows


def replay_one(ex_real, trade, cfg):
    symbol, side = trade["symbol"], trade["side"]
    entry_price, entered_at, exited_at = trade["entry_price"], trade["entered_at"], trade["exited_at"]
    leverage = trade.get("leverage", 4)
    try:
        # 지표 계산(add_indicators)에 필요한 워밍업(~60개 이상)을 확보하려고 진입 120분
        # 전부터 데이터를 가져온다 — 재현 시작점(entry_idx)은 아래에서 정확히 진입시각으로 맞춤.
        df = fetch_historical_klines(ex_real, symbol, entered_at - 120 * 60, exited_at + REPLAY_WINDOW_MIN * 60)
    except Exception:
        return None
    if df.empty:
        return None

    replay_ex = ReplayExchange(df)
    pm = PositionManager(cfg)
    pm.track(symbol, side, entry_price=entry_price, quantity=1.0, leverage=leverage)
    hourly_state = {}

    entry_idx = df[df["open_time"] >= pd.to_datetime(entered_at, unit="s")].index
    if len(entry_idx) == 0:
        return None
    start_i = entry_idx[0]

    for i in range(start_i, len(df)):
        replay_ex.set_now(i)
        mark_price = float(df.iloc[i]["close"])
        decision = pm.evaluate(symbol, mark_price)
        if decision:
            return {"symbol": symbol, "side": side, "outcome": decision, "orig_reason": trade.get("exit_reason"),
                    "orig_pnl": trade.get("estimated_pnl_usdt"), "new_pnl_pct": _pnl_pct(entry_price, mark_price, side)}
        if check_early_exit(replay_ex, pm, cfg, symbol):
            return {"symbol": symbol, "side": side, "outcome": "EARLY_EXIT", "orig_reason": trade.get("exit_reason"),
                    "orig_pnl": trade.get("estimated_pnl_usdt"), "new_pnl_pct": _pnl_pct(entry_price, mark_price, side)}
        if check_hourly_soft_stop(replay_ex, cfg, pm, symbol, hourly_state):
            return {"symbol": symbol, "side": side, "outcome": "SOFT_STOP", "orig_reason": trade.get("exit_reason"),
                    "orig_pnl": trade.get("estimated_pnl_usdt"), "new_pnl_pct": _pnl_pct(entry_price, mark_price, side)}
    # 재현 구간 끝까지 안 닫히면 마지막 가격 기준으로 보류중 표시
    last_price = float(df.iloc[-1]["close"])
    return {"symbol": symbol, "side": side, "outcome": "STILL_OPEN", "orig_reason": trade.get("exit_reason"),
            "orig_pnl": trade.get("estimated_pnl_usdt"), "new_pnl_pct": _pnl_pct(entry_price, last_price, side)}


def _pnl_pct(entry, mark, side):
    return ((mark / entry - 1) * 100) if side == "LONG" else ((entry / mark - 1) * 100)


def main():
    cfg_new = Config()  # .env의 새 기준(2.5/2.5) 그대로 사용
    cfg_new.force_profit_exit_max_hold_min = 0  # 순환매매 강제익절은 이 재현에서 제외(별개 로직)
    ex_real = Exchange(cfg_new)

    trades = load_trades()
    print(f"SOFT_STOP/EARLY_EXIT {len(trades)}건 재현 시작(새 기준 2.5%)...")
    results = []
    for i, t in enumerate(trades):
        r = replay_one(ex_real, t, cfg_new)
        if r:
            results.append(r)
        time.sleep(0.1)

    orig_total = sum(r["orig_pnl"] for r in results)
    new_total_pct = sum(r["new_pnl_pct"] for r in results)
    lines = [f"=== 재현 결과 ({len(results)}건) ===",
             f"기존(1.5% 기준) 실현손익 합계: {orig_total:+.3f} USDT",
             f"새 기준(2.5%)으로 재관리했을 때 pnl% 합계: {new_total_pct:+.2f}%p (USDT 환산 아님, 참고용)",
             ""]
    for r in results:
        lines.append(f"{r['symbol']} {r['side']} 기존청산={r['orig_reason']}(pnl={r['orig_pnl']:+.3f}) "
                      f"-> 새기준 재현결과={r['outcome']}(pnl%={r['new_pnl_pct']:+.2f}%)")
    text = "\n".join(lines)
    Path("soft_early_threshold_backtest_result.txt").write_text(text, encoding="utf-8")
    print("완료")


if __name__ == "__main__":
    main()
