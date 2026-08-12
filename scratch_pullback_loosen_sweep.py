"""[2026-08-12 사용자요청] 완전차단(0.8%) 유지한 채 원시 신호 임계값을 하나씩 완화해서
어느 게 승률/손익/거래수 균형이 가장 좋은지 비교."""
from pathlib import Path
import offline_backtest as ob

DATA_PATH = Path("scratch_klines_v4.json")

def summarize(result, label):
    ledger = result["ledger"]
    if not ledger:
        return f"=== {label} === 거래없음"
    wins = [r for r in ledger if r["net_pnl"] > 0]
    net = sum(r["net_pnl"] for r in ledger)
    gp = sum(r["net_pnl"] for r in wins)
    gl = abs(sum(r["net_pnl"] for r in ledger if r["net_pnl"] <= 0))
    pf = gp/gl if gl else float("inf")
    return f"=== {label} ===\n거래수={len(ledger)} 승률={len(wins)/len(ledger)*100:.2f}% 순손익={net:+.4f} 손익비={pf:.3f}"

def main():
    data, _ = ob.load_data(DATA_PATH)
    base = dict(
        starting_balance=38.0, leverage=4.0, max_positions=3, margin_fraction=0.20,
        short_candle_change_pct=0.45, short_volume_ratio=2.6, short_taker_buy_ratio_max=0.43,
        short_adx_threshold=24.0, short_max_close_from_low_pct=0.5,
        long_max_close_from_high_pct=0.8, long_max_upper_wick_body_ratio=2.0,
    )
    variants = [
        ("기준(차단 0.8%만)", {}),
        ("candle_change_pct 0.35->0.25", {"candle_change_pct": 0.25}),
        ("volume_ratio 2.0->1.6", {"volume_ratio": 1.6}),
        ("taker_ratio 0.55->0.50", {"taker_ratio": 0.50}),
        ("adx_threshold 20->16", {"adx_threshold": 16.0}),
        ("min_avg_quote_volume 150->80", {"min_avg_quote_volume": 80.0}),
    ]
    out = []
    for label, override in variants:
        kwargs = {**base, **override}
        s = ob.Settings(**kwargs)
        r = ob.run_backtest(data, s)
        out.append(summarize(r, label))
    text = "\n\n".join(out)
    print(text)
    Path("pullback_loosen_sweep_result.txt").write_text(text, encoding="utf-8")

if __name__ == "__main__":
    main()
