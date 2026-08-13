"""scratch_short_stop_5pct_test.py 결과(scratch_short_stop_results.json)를 읽어
baseline(8.0%) vs variant(5.0%) 비교 리포트를 만든다. 진입가/체결가로부터 재계산한
pnl_pct를 사용(수수료 왕복 0.1% 근사 반영), 실제 pnl_usdt 스케일을 맞추기 위해
quantity*entry_price로 명목가치를 근사."""
import json
from pathlib import Path

FEE_ROUNDTRIP_PCT = 0.1  # 왕복 수수료 근사(0.1%), 실제 ledger의 fee_rate_roundtrip과 동일 근사

results = json.loads(Path("scratch_short_stop_results.json").read_text(encoding="utf-8"))
print(f"총 {len(results)}건")


def price_pnl_pct(entry_price, exit_price, leverage):
    # SHORT: 가격이 떨어지면 이익
    raw = (entry_price / exit_price - 1) * 100 * leverage
    return raw - FEE_ROUNDTRIP_PCT * leverage


rows = []
for r in results:
    entry_price = r["entry_price"]
    leverage = r["leverage"]
    qty = r.get("quantity") or 0
    notional = entry_price * qty

    actual_pnl_usdt = r["actual_pnl_usdt"]
    actual_reason = r["actual_reason"]
    exited_at = r["exited_at"]

    hit8 = r["hit8"]
    hit5 = r["hit5"]

    # baseline(8%): 8% 손절이 실제 청산보다 먼저 도달했으면 그 시점에 손절가로 청산된 것으로 간주
    if hit8 and hit8["hit_time"] < exited_at:
        base_reason = "STOP_LOSS_8"
        base_pnl_pct = price_pnl_pct(entry_price, hit8["stop_price"], leverage)
        base_pnl_usdt = notional * base_pnl_pct / 100.0 / leverage * leverage  # pnl_pct already *leverage-scaled(ROE), usdt = notional/leverage * pnl_pct(ROE)/100
        base_pnl_usdt = (notional / leverage) * (base_pnl_pct / 100.0)
    else:
        base_reason = actual_reason
        base_pnl_usdt = actual_pnl_usdt

    # variant(5%)
    if hit5 and hit5["hit_time"] < exited_at:
        # 5%가 실제 청산보다 먼저 온 경우 -> 5% 손절가로 청산
        var_reason = "STOP_LOSS_5"
        var_pnl_pct = price_pnl_pct(entry_price, hit5["stop_price"], leverage)
        var_pnl_usdt = (notional / leverage) * (var_pnl_pct / 100.0)
    else:
        var_reason = actual_reason
        var_pnl_usdt = actual_pnl_usdt

    # baseline과 variant 둘 다 8%/5% 도달 이전에 실제 청산이 일어난 경우는 실제결과 그대로(동일)
    rows.append({
        "symbol": r["symbol"], "entered_at": r["entered_at"], "leverage": leverage,
        "actual_reason": actual_reason, "actual_pnl_usdt": actual_pnl_usdt,
        "base_reason": base_reason, "base_pnl_usdt": base_pnl_usdt,
        "var_reason": var_reason, "var_pnl_usdt": var_pnl_usdt,
        "hit8": hit8, "hit5": hit5,
    })


def summarize(label, pnl_key, reason_key):
    total = sum(r[pnl_key] for r in rows)
    wins = [r for r in rows if r[pnl_key] > 0]
    losses = [r for r in rows if r[pnl_key] <= 0]
    win_rate = len(wins) / len(rows) * 100
    avg_win = sum(r[pnl_key] for r in wins) / len(wins) if wins else 0
    avg_loss = sum(r[pnl_key] for r in losses) / len(losses) if losses else 0
    pf = (sum(r[pnl_key] for r in wins) / abs(sum(r[pnl_key] for r in losses))) if losses and sum(r[pnl_key] for r in losses) != 0 else float("inf")
    print(f"\n--- {label} ---")
    print(f"거래수={len(rows)} 승률={win_rate:.1f}% 순손익={total:+.3f} USDT 손익비(PF)={pf:.2f}")
    print(f"평균이익={avg_win:+.4f} 평균손실={avg_loss:+.4f} 건당기대값={total/len(rows):+.4f}")


summarize("실제(라이브 로그 그대로)", "actual_pnl_usdt", "actual_reason")
summarize("baseline 시뮬레이션(8.0% 손절 통일적용)", "base_pnl_usdt", "base_reason")
summarize("variant 시뮬레이션(5.0% 손절 통일적용)", "var_pnl_usdt", "var_reason")

# baseline vs variant에서 결과가 달라진 케이스만 분석
diff = [r for r in rows if abs(r["base_pnl_usdt"] - r["var_pnl_usdt"]) > 1e-9]
print(f"\n8.0->5.0 변경으로 결과가 달라진 거래: {len(diff)}건 / {len(rows)}건")

improved = []  # 5%가 더 일찍 잘려서 손실이 줄어든 케이스 (base가 손실이고 var 손실이 base보다 작은 경우)
worsened = []  # 5%가 일찍 잘려서 이익/더 나은 결과를 놓친 케이스
for r in diff:
    b, v = r["base_pnl_usdt"], r["var_pnl_usdt"]
    if v > b:
        improved.append(r)
    else:
        worsened.append(r)

print(f"\n[5.0%가 더 유리했던 건] {len(improved)}건, 개선액 합계={sum(v['var_pnl_usdt']-v['base_pnl_usdt'] for v in improved):+.3f} USDT")
for r in sorted(improved, key=lambda x: x["var_pnl_usdt"] - x["base_pnl_usdt"], reverse=True)[:10]:
    print(f"  {r['symbol']} base={r['base_reason']}({r['base_pnl_usdt']:+.3f}) var={r['var_reason']}({r['var_pnl_usdt']:+.3f}) 개선={r['var_pnl_usdt']-r['base_pnl_usdt']:+.3f}")

print(f"\n[5.0%가 더 불리했던 건(이익 기회를 조기청산으로 놓침)] {len(worsened)}건, 손실액 합계={sum(v['var_pnl_usdt']-v['base_pnl_usdt'] for v in worsened):+.3f} USDT")
for r in sorted(worsened, key=lambda x: x["var_pnl_usdt"] - x["base_pnl_usdt"])[:15]:
    print(f"  {r['symbol']} base={r['base_reason']}({r['base_pnl_usdt']:+.3f}) var={r['var_reason']}({r['var_pnl_usdt']:+.3f}) 손해={r['var_pnl_usdt']-r['base_pnl_usdt']:+.3f}")

net_effect = sum(v["var_pnl_usdt"] - v["base_pnl_usdt"] for v in rows)
print(f"\n순효과(8.0->5.0): {net_effect:+.3f} USDT")

# held-out 최근 2일
import time
cutoff = max(r["entered_at"] for r in rows) - 2 * 86400
recent = [r for r in rows if r["entered_at"] >= cutoff]
print(f"\n=== held-out 최근 2일 ({len(recent)}건) ===")
b_total = sum(r["base_pnl_usdt"] for r in recent)
v_total = sum(r["var_pnl_usdt"] for r in recent)
b_wins = sum(1 for r in recent if r["base_pnl_usdt"] > 0)
v_wins = sum(1 for r in recent if r["var_pnl_usdt"] > 0)
print(f"baseline(8.0): 순손익={b_total:+.3f} 승률={b_wins/len(recent)*100:.1f}%")
print(f"variant(5.0): 순손익={v_total:+.3f} 승률={v_wins/len(recent)*100:.1f}%")
