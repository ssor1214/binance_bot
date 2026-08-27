"""
연속손실 편향(복수매매 패턴) 분석 스크립트.
- logs/trade_ledger.jsonl (origin=bot) 만 사용, 순수 통계/시퀀스 재현.
- 라이브 코드/설정 수정 없음. 네트워크 호출 없음.
"""
import json
from pathlib import Path
from statistics import mean, stdev

LEDGER = Path(__file__).resolve().parents[2] / "logs" / "trade_ledger.jsonl"

trades = []
with open(LEDGER, encoding="utf-8") as f:
    for line in f:
        line = line.strip()
        if not line:
            continue
        try:
            d = json.loads(line)
        except json.JSONDecodeError:
            continue
        if d.get("origin") != "bot":
            continue
        if "entered_at" not in d or "exited_at" not in d:
            continue
        trades.append(d)

trades.sort(key=lambda d: d["exited_at"])  # 종료시점 기준 시간순 (실제 재진입 판단 기준)

print(f"총 bot 실거래 청산 건수: {len(trades)}")

def pnl_pct(d):
    return d.get("estimated_pnl_pct")

def pnl_usdt(d):
    return d.get("estimated_pnl_usdt")

def is_loss(d):
    p = pnl_usdt(d)
    if p is None:
        return None
    return p < 0

# ---------- 1. 직전 거래 손실/이익 여부에 따른 다음 거래 통계 ----------
prev_loss_group = []
prev_win_group = []
gap_prev_loss = []
gap_prev_win = []

for i in range(1, len(trades)):
    prev = trades[i - 1]
    cur = trades[i]
    pl = is_loss(prev)
    if pl is None:
        continue
    gap_min = (cur["entered_at"] - prev["exited_at"]) / 60.0
    if pl:
        prev_loss_group.append(cur)
        gap_prev_loss.append(gap_min)
    else:
        prev_win_group.append(cur)
        gap_prev_win.append(gap_min)

def summarize(group, label):
    n = len(group)
    if n == 0:
        print(f"{label}: 데이터 없음")
        return
    wins = [d for d in group if is_loss(d) is False]
    losses = [d for d in group if is_loss(d) is True]
    winrate = len(wins) / n * 100 if n else 0
    pnl_pcts = [pnl_pct(d) for d in group if pnl_pct(d) is not None]
    pnl_usdts = [pnl_usdt(d) for d in group if pnl_usdt(d) is not None]
    avg_pct = mean(pnl_pcts) if pnl_pcts else float("nan")
    avg_usdt = mean(pnl_usdts) if pnl_usdts else float("nan")
    print(f"{label}: n={n}, 승률={winrate:.1f}%, 평균순ROE%={avg_pct:.3f}, 평균순PnL(USDT)={avg_usdt:.4f}")

print("\n=== 1. 직전 거래 결과별 다음 거래 통계 ===")
summarize(prev_loss_group, "직전=손실 -> 다음 거래")
summarize(prev_win_group, "직전=이익 -> 다음 거래")

if gap_prev_loss and gap_prev_win:
    print(f"\n재진입 간격(분): 직전손실 후 평균={mean(gap_prev_loss):.2f} (n={len(gap_prev_loss)}), "
          f"직전이익 후 평균={mean(gap_prev_win):.2f} (n={len(gap_prev_win)})")
    print(f"재진입 간격 중앙값: 직전손실 후={sorted(gap_prev_loss)[len(gap_prev_loss)//2]:.2f}, "
          f"직전이익 후={sorted(gap_prev_win)[len(gap_prev_win)//2]:.2f}")

# ---------- 2. 연속 손실 N회 이상 뒤 다음 거래 통계 ----------
print("\n=== 2. 연속손실 N회 이상 직후 다음 거래 통계 ===")

streak = 0
streak_before_trade = []  # 각 거래 시작 시점의 "직전까지의 연속손실 카운트"
for d in trades:
    streak_before_trade.append(streak)
    il = is_loss(d)
    if il is True:
        streak += 1
    elif il is False:
        streak = 0
    # il is None -> streak 유지 (추정불가 데이터)

for min_streak in (2, 3, 4):
    group = [trades[i] for i in range(len(trades)) if streak_before_trade[i] >= min_streak]
    summarize(group, f"직전 연속손실>= {min_streak}회 -> 이 거래")

baseline_all = trades
summarize(baseline_all, "전체 baseline (모든 거래)")

# ---------- 3. 연속손실 후 재진입 간격 분석 ----------
print("\n=== 3. 연속손실 스트릭별 재진입 간격(분) ===")
for min_streak in (1, 2, 3):
    gaps = []
    for i in range(1, len(trades)):
        if streak_before_trade[i] >= min_streak:
            gaps.append((trades[i]["entered_at"] - trades[i - 1]["exited_at"]) / 60.0)
    if gaps:
        print(f"연속손실>={min_streak}: n={len(gaps)}, 평균간격={mean(gaps):.2f}분, "
              f"중앙값={sorted(gaps)[len(gaps)//2]:.2f}분")

# ---------- 4. 전체 일시정지 시뮬레이션 ----------
print("\n=== 4. 연속손실 기반 전체 일시정지 시뮬레이션 ===")

def simulate_pause(trades, streak_threshold, pause_min):
    """streak_threshold 연속손실 발생 시 pause_min분간 신규진입(entered_at) 스킵."""
    kept = []
    skipped = []
    streak = 0
    pause_until = None
    for d in trades:
        if pause_until is not None and d["entered_at"] < pause_until:
            skipped.append(d)
            # 스킵된 거래는 스트릭 갱신에 영향 주지 않음(실제로 발생 안 했으므로)
            continue
        kept.append(d)
        il = is_loss(d)
        if il is True:
            streak += 1
            if streak >= streak_threshold:
                pause_until = d["exited_at"] + pause_min * 60.0
                streak = 0
        elif il is False:
            streak = 0
    return kept, skipped

def total_pnl(group):
    vals = [pnl_usdt(d) for d in group if pnl_usdt(d) is not None]
    return sum(vals), len(vals)

base_pnl, base_n = total_pnl(trades)
print(f"Baseline: 거래수={len(trades)}, 순PnL합={base_pnl:.3f} USDT, 평균={base_pnl/base_n:.4f}")

for th, pm in [(3, 30), (3, 60), (5, 10), (5, 30)]:
    kept, skipped = simulate_pause(trades, th, pm)
    kept_pnl, kept_n = total_pnl(kept)
    skipped_pnl, skipped_n = total_pnl(skipped)
    print(f"[임계={th}연속손실, 일시정지={pm}분] 유지={kept_n}건 순PnL={kept_pnl:.3f}, "
          f"스킵={skipped_n}건 (스킵된 거래들의 실제순PnL합={skipped_pnl:.3f}, "
          f"스킵거래 평균PnL={ (skipped_pnl/skipped_n) if skipped_n else float('nan'):.4f}) "
          f"-> 시뮬결과 총PnL={kept_pnl:.3f} (baseline대비 {kept_pnl-base_pnl:+.3f})")
