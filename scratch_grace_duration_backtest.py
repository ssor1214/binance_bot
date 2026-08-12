"""[2026-08-12 사용자요청] "PROMUSDT처럼 유예기간(45초) 끝난 뒤 정상폭 손절에 뚫린 경우가
있는지, 유예기간을 늘리면(90/120/180초) 살아남았을지" 1분봉으로 가볍게 확인.
실 손절/청산 트레이드를 재시뮬레이션하지 않고, 각 캔들 시점마다 "그때 적용됐을 손절폭
(유예중=넓음/유예종료=원래폭)"과 실제 역행폭(MAE)을 비교해서 "몇 초째에 실제로 뚫렸는지"만
가볍게 계산한다. 읽기전용 REST만 사용."""
import json
import time
from pathlib import Path

import pandas as pd

from bot.config import Config
from bot.exchange import Exchange
from scratch_trade_postmortem import fetch_historical_klines

LEDGER_PATH = Path("logs/trade_ledger.jsonl")
SINCE = time.mktime(time.strptime("2026-08-11 20:38:06", "%Y-%m-%d %H:%M:%S"))  # 유예기능 도입 시점
GRACE_WIDEN_MULT = 2.0  # 현재 라이브값
CANDIDATE_GRACE_SECS = [45, 90, 120, 180]  # 45=현재값, 나머지=비교대상


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
            if r.get("exit_reason") not in ("STOP_LOSS", "EXTERNAL_CLOSE_LOSS"):
                continue
            rows.append(r)
    return rows


def base_stop_pct(cfg, side):
    if side == "SHORT" and cfg.short_stop_loss_pct > 0:
        return cfg.short_stop_loss_pct
    return cfg.stop_loss_pct


def analyze(ex, cfg, trade):
    symbol, side = trade["symbol"], trade["side"]
    entry_price, entered_at, exited_at, leverage = (
        trade["entry_price"], trade["entered_at"], trade["exited_at"], trade.get("leverage", 4),
    )
    # [수정] exited_at+60초로 윈도우를 잡으면 원래 청산이 빨랐던 거래는 120/180초 유예
    # 후보를 테스트할 데이터 자체가 없다 — 후보 중 최댓값(180초) 기준으로 고정 확보.
    window_end = max(exited_at + 60, entered_at + max(CANDIDATE_GRACE_SECS) + 60)
    try:
        df = fetch_historical_klines(ex, symbol, entered_at, window_end)
    except Exception:
        return None
    if df.empty:
        return None
    df = df[df["open_time"] >= pd.to_datetime(entered_at, unit="s")]
    if df.empty:
        return None

    base_pct = base_stop_pct(cfg, side)
    entered_ts = pd.Timestamp(entered_at, unit="s")

    # 각 후보 유예시간별로 "몇 초째에 실제 손절폭을 뚫었는지"(뚫린 적 없으면 None)를 계산
    result = {"symbol": symbol, "side": side, "orig_reason": trade.get("exit_reason"),
              "orig_pnl": trade.get("estimated_pnl_usdt"), "hit_sec_by_grace": {}}
    for grace_sec in CANDIDATE_GRACE_SECS:
        hit_at = None
        for _, row in df.iterrows():
            elapsed = (row["open_time"] - entered_ts).total_seconds()
            effective_pct = base_pct * GRACE_WIDEN_MULT if elapsed < grace_sec else base_pct
            if side == "LONG":
                adverse_pct = (1 - row["low"] / entry_price) * 100
            else:
                adverse_pct = (row["high"] / entry_price - 1) * 100
            adverse_roe = adverse_pct * leverage
            if adverse_roe >= effective_pct:
                hit_at = elapsed
                break
        result["hit_sec_by_grace"][grace_sec] = hit_at
    return result


def main():
    cfg = Config()
    ex = Exchange(cfg)
    trades = load_trades()
    print(f"STOP_LOSS/EXTERNAL_CLOSE_LOSS {len(trades)}건 분석 시작...")
    results = []
    for i, t in enumerate(trades):
        r = analyze(ex, cfg, t)
        if r:
            results.append(r)
        time.sleep(0.1)

    lines = [f"=== 유예시간별 재현 ({len(results)}건, 배수={GRACE_WIDEN_MULT}x 고정) ==="]
    for grace_sec in CANDIDATE_GRACE_SECS:
        saved = sum(1 for r in results if r["hit_sec_by_grace"][grace_sec] is None)
        still_hit = len(results) - saved
        lines.append(f"유예 {grace_sec}초: 살아남음(끝까지 정상폭 안 뚫림)={saved}건 / 여전히 손절={still_hit}건")
    lines.append("")
    lines.append("=== 상세 (현재45초 vs 120초 비교) ===")
    for r in results:
        h45 = r["hit_sec_by_grace"][45]
        h120 = r["hit_sec_by_grace"][120]
        h45s = f"{h45:.0f}초" if h45 is not None else "안뚫림"
        h120s = f"{h120:.0f}초" if h120 is not None else "안뚫림"
        changed = "★120초면 살았음" if (h45 is not None and h120 is None) else ""
        lines.append(f"{r['symbol']} {r['side']} 기존pnl={r['orig_pnl']:+.3f} 45초기준={h45s} 120초기준={h120s} {changed}")

    text = "\n".join(lines)
    Path("grace_duration_backtest_result.txt").write_text(text, encoding="utf-8")
    print("done")


if __name__ == "__main__":
    main()
