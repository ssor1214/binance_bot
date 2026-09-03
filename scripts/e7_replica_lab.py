"""e7 신호 원본 그대로 경로 시뮬레이션 — **사전등록본**.

코덱스 지적(2026-09-04)이 맞다. 이전에 낸 "[대조] 순수볼밴 역추세 단독" 결과는
e7 의 **원형**이지 e7 자체가 아니다. [scripts/scalp_bot_e7.py](scripts/scalp_bot_e7.py)
의 `e7_signal()` 은 다음을 추가로 가진다: RSI(단순평균, Wilder 아님) 방향필터,
반전 캔들(양봉/음봉) 확인, 거래량 3배 급증 제외, 캔들범위 2배 급증 제외,
ATR 상위10% 제외(**확장 윈도우** 백분위 — bar 60부터 현재까지 전체, 고정폭 롤링이
아니다), 그리고 TP=중심선/SL=위험기반(밴드거리 또는 ATR*0.5 중 큰 값)/최대보유
15분. 이 스크립트는 이 전부를 원본 코드와 동일하게 재현한다.

────────────────────────────────────────────────────────────────────────
사전 등록 (실행 전 고정, 결과를 보고 바꾸지 않는다)
────────────────────────────────────────────────────────────────────────
  1) RSI: e7_signal 그대로 — 최근 14개 종가 변화량의 **단순평균**
     (Wilder 스무딩 아님). gains/losses 를 15개 종가에서 14개 diff 로 계산.
  2) 거래량 필터: avgvol = 직전 20개 거래량 평균(현재봉 제외).
     vol_ratio=volumes[i]/avgvol >= 3.0 이면 제외.
  3) 캔들범위 필터: avg_range = 직전 20개 (H-L) 평균(현재봉 제외).
     cur_range=H[i]-L[i] >= avg_range*2.0 이면 제외.
  4) ATR: 최근 14개 (H-L) 평균(단순 범위, 이전 종가 미사용 — e7 원본 그대로).
     ATR 백분위는 **bar 60부터 현재 바까지의 확장 윈도우**에서 계산한다
     (F1 처럼 고정폭 rolling 이 아니다 — e7 코드가 실제로 그렇게 짜여 있다).
     atr_pct >= 0.90 이면 제외.
  5) 진입: LONG = 종가<=하단 & RSI<=40 & 종가>=시가(양봉).
           SHORT = 종가>=상단 & RSI>=60 & 종가<=시가(음봉).
  6) TP = 중심선(bbm, 진입시점 고정). SL = 진입가 - max(진입가-하단, ATR*0.5)
     (LONG, SHORT 는 대칭). 최대보유 15분(15봉), 넘으면 시장가 청산.
  7) 비용은 **둘 다** 보고한다 — e7 자체 가정(수수료 0.04%+슬리피지 0.04%=
     왕복 0.08%)과 edge_lab 의 이중 비용선(taker 0.14% / maker 0.04%).
     하나만 쓰면 그게 유리한 쪽 고르기가 된다.
  8) 판정: 건당 순기대값 양수 + 롱/숏 둘 다 양수 + 3구간 부호 유지.
  9) 결과를 보고 필터 임계값을 바꾸지 않는다.

측정하지 못하는 것: 밴드/RSI 조건을 동시에 만족하는 시점에 **그 가격으로
체결**된다고 가정한다(지정가가 아니라 조건 성립 시점의 종가 진입 근사).
실제 e7 이 시장가로 넣는지 지정가로 넣는지는 라이브 배선을 확인해야 한다.
이 결과는 진입 판정 로직의 경로 시뮬레이션이지 라이브 체결의 완전한 재현이 아니다.
"""
import argparse
import pathlib
import sys

import numpy as np

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from edge_lab import build_indicators, load_panel  # noqa: E402


def simple_rsi(C, period=14):
    """e7_signal 의 RSI 그대로: 최근 period 개 diff 의 단순평균(Wilder 아님)."""
    T, S = C.shape
    diff = np.diff(C, axis=0)
    gains = np.where(diff > 0, diff, 0.0)
    losses = np.where(diff < 0, -diff, 0.0)
    cg = np.vstack([np.zeros((1, S)), np.cumsum(gains, axis=0)])
    cl = np.vstack([np.zeros((1, S)), np.cumsum(losses, axis=0)])
    rsi = np.full((T, S), np.nan)
    for i in range(period, T):
        ag = (cg[i] - cg[i - period]) / period
        al = (cl[i] - cl[i - period]) / period
        with np.errstate(invalid="ignore", divide="ignore"):
            r = np.where(al == 0, 100.0, 100 - 100 / (1 + ag / al))
        rsi[i] = r
    return rsi


def expanding_percentile_fenwick(x):
    """x[i] 가 x[pool_start..i] 안에서 차지하는 백분위(자기 포함).
    e7 의 atr_hist 는 bar 60(0-idx)부터 누적되므로 pool_start=60 으로 호출한다."""
    n = len(x)
    out = np.full(n, np.nan)
    valid = np.where(~np.isnan(x))[0]
    if len(valid) < 2:
        return out
    vals = x[valid]
    order = np.argsort(vals, kind="mergesort")
    ranks = np.empty(len(vals), dtype=np.int64)
    ranks[order] = np.arange(len(vals))
    m = len(vals)
    tree = np.zeros(m + 2, dtype=np.int64)

    def update(i):
        i += 1
        while i <= m:
            tree[i] += 1
            i += i & (-i)

    def query(i):
        i += 1
        s = 0
        while i > 0:
            s += tree[i]
            i -= i & (-i)
        return s

    for k in range(m):
        r = int(ranks[k])
        update(r)
        cnt_le = query(r)
        out[valid[k]] = cnt_le / (k + 1)
    return out


def build_e7_masks(P, I):
    """e7_signal 의 필터/신호를 벡터화 재현. 반환: (long_ok, short_ok)."""
    C, O, H, L, V = P["c"], P["o"], P["h"], P["l"], P["v"]
    T, S = C.shape
    bbl, bbu, bbm = I["bbl"], I["bbu"], I["bbm"]

    print("  RSI(단순, 14) 계산 중...")
    rsi = simple_rsi(C, 14)

    print("  거래량/범위 필터 계산 중...")

    def prev_mean20(X):
        # cs[k] = sum(X[0:k]); s20[m] = sum(X[m:m+20]) for m=0..T-20 (길이 T-19).
        # 그 창은 "현재봉 바로 앞" 위치인 out[m+20] 에 배정한다(길이 T-20 필요
        # -> s20 마지막 원소는 대응할 자리가 없어 버린다).
        cs = np.vstack([np.zeros((1, S)), np.cumsum(np.nan_to_num(X), axis=0)])
        cnt = np.vstack([np.zeros((1, S)), np.cumsum(~np.isnan(X), axis=0)])
        s20 = (cs[20:] - cs[:-20])[:-1]
        n20 = (cnt[20:] - cnt[:-20])[:-1]
        out = np.full((T, S), np.nan)
        with np.errstate(invalid="ignore", divide="ignore"):
            out[20:] = np.where(n20 >= 15, s20 / np.maximum(n20, 1), np.nan)
        return out

    avgvol = prev_mean20(np.roll(V, 1, axis=0))       # 현재봉 제외 = 1칸 민 뒤 mean20
    avgvol[0] = np.nan
    range_ = H - L
    avgrange = prev_mean20(np.roll(range_, 1, axis=0))
    avgrange[0] = np.nan
    with np.errstate(invalid="ignore", divide="ignore"):
        vol_ratio = V / avgvol
    reject_vol = vol_ratio >= 3.0
    reject_range = range_ >= avgrange * 2.0

    print("  ATR(14) + 확장윈도우 백분위 계산 중 (심볼별, 시간이 걸린다)...")

    def roll_mean14(X):
        cs = np.vstack([np.zeros((1, S)), np.cumsum(X, axis=0)])
        out = np.full((T, S), np.nan)
        out[13:] = (cs[14:] - cs[:-14]) / 14.0
        return out

    atr = roll_mean14(range_)
    atr_pct = np.full((T, S), np.nan)
    for j in range(S):
        col = atr[:, j].copy()
        col[:60] = np.nan          # e7: pool 은 bar 60(0-idx)부터
        atr_pct[:, j] = expanding_percentile_fenwick(col)
        if (j + 1) % 20 == 0:
            print(f"    {j + 1}/{S} 심볼 완료")
    reject_atr = atr_pct >= 0.90

    reject = reject_vol | reject_range | reject_atr
    bull = C >= O
    bear = C <= O

    long_ok = (C <= bbl) & (rsi <= 40) & bull & ~reject & ~np.isnan(bbl) & ~np.isnan(rsi)
    short_ok = (C >= bbu) & (rsi >= 60) & bear & ~reject & ~np.isnan(bbu) & ~np.isnan(rsi)
    return long_ok, short_ok, atr, bbm, bbl, bbu


def race(P, long_ok, short_ok, atr, bbm, bbl, bbu, max_bars, stride, rowslice=None):
    C, H, L = P["c"], P["h"], P["l"]
    if rowslice is not None:
        C, H, L = C[rowslice], H[rowslice], L[rowslice]
    T, S = C.shape
    out = {"LONG": [], "SHORT": []}
    for j in range(S):
        for i in range(0, T - 1, stride):
            for side, ok in (("LONG", long_ok), ("SHORT", short_ok)):
                if not ok[i, j]:
                    continue
                ent = C[i, j]
                if np.isnan(ent) or ent <= 0 or np.isnan(atr[i, j]):
                    continue
                if side == "LONG":
                    tp = bbm[i, j]
                    risk = max(ent - bbl[i, j], atr[i, j] * 0.5)
                    sl = ent - risk
                else:
                    tp = bbm[i, j]
                    risk = max(bbu[i, j] - ent, atr[i, j] * 0.5)
                    sl = ent + risk
                if np.isnan(tp) or np.isnan(sl) or risk <= 0:
                    continue
                tp_pct = (tp / ent - 1) * 100 * (1 if side == "LONG" else -1)
                if tp_pct <= 0:
                    continue
                res = None
                for k in range(i + 1, min(i + 1 + max_bars, T)):
                    hi, lo = H[k, j], L[k, j]
                    if np.isnan(hi) or np.isnan(lo):
                        continue
                    if side == "LONG":
                        hit_sl, hit_tp = lo <= sl, hi >= tp
                    else:
                        hit_sl, hit_tp = hi >= sl, lo <= tp
                    if hit_sl:
                        res = ("SL", -(risk / ent) * 100)
                        break
                    if hit_tp:
                        res = ("TP", tp_pct)
                        break
                if res is None:
                    k = min(i + max_bars, T - 1)
                    if np.isnan(C[k, j]):
                        continue
                    r = (C[k, j] / ent - 1) * 100 * (1 if side == "LONG" else -1)
                    res = ("TO", r)
                out[side].append(res)
    return out


def summarize(rows, fee_tp, fee_sl, fee_to):
    if not rows:
        return None
    n = len(rows)
    tp = [r for r in rows if r[0] == "TP"]
    sl = [r for r in rows if r[0] == "SL"]
    to = [r for r in rows if r[0] == "TO"]
    net = (sum(r[1] - fee_tp for r in tp) + sum(r[1] - fee_sl for r in sl)
           + sum(r[1] - fee_to for r in to)) / n
    return dict(n=n, tp=len(tp) / n * 100, sl=len(sl) / n * 100, to=len(to) / n * 100,
                net=net, tp_avg=(np.mean([r[1] for r in tp]) if tp else 0.0),
                sl_avg=(np.mean([r[1] for r in sl]) if sl else 0.0))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cache", default="scratch_edge_1m_60d.npz")
    ap.add_argument("--interval", default="1m")
    ap.add_argument("--max-bars", type=int, default=15)
    ap.add_argument("--stride", type=int, default=15)
    ap.add_argument("--split", type=int, default=3)
    a = ap.parse_args()

    P = load_panel([a.cache], a.interval)
    I = build_indicators(P)
    T, S = P["c"].shape
    print(f"패널: {S}심볼 x {T}봉 ({a.interval})")

    long_ok, short_ok, atr, bbm, bbl, bbu = build_e7_masks(P, I)
    n_l, n_s = int(np.nansum(long_ok)), int(np.nansum(short_ok))
    print(f"필터 통과: LONG {n_l}건 / SHORT {n_s}건 "
          f"(before filters 대비 비율은 별도 출력 없음, 최종 신호 수만 표시)")
    print()

    r = race(P, long_ok, short_ok, atr, bbm, bbl, bbu, a.max_bars, a.stride)

    print("=== e7 원본 재현 (진입=조건성립 종가, TP=중심선, SL=위험기반, 최대15분) ===")
    for lbl, fee_tp, fee_sl, fee_to in (
        ("e7 자체가정(수수료0.04+슬리피지0.04=왕복0.08%)", 0.08, 0.08, 0.08),
        ("edge_lab maker선(0.04%)", 0.04, 0.07, 0.07),
        ("edge_lab taker선(0.14%)", 0.14, 0.14, 0.14),
    ):
        print(f"--- 비용: {lbl} ---")
        tot = []
        for side, rows in r.items():
            s = summarize(rows, fee_tp, fee_sl, fee_to)
            if s is None:
                print(f"  {side}: 표본 없음")
                continue
            tot += rows
            print(f"  {side:<6} n={s['n']:5d}  TP {s['tp']:5.1f}%  SL {s['sl']:5.1f}%  "
                  f"TO {s['to']:5.1f}%  TP평균{s['tp_avg']:+7.3f}  SL평균{s['sl_avg']:+7.3f}"
                  f"  건당순{s['net']:+8.4f}")
        st = summarize(tot, fee_tp, fee_sl, fee_to)
        if st:
            print(f"  합계   n={st['n']:5d}  건당순{st['net']:+8.4f}")
        print()

    if a.split > 1:
        T2 = T
        step = T2 // a.split
        print("=== 국면 3등분 (비용: e7 자체가정) ===")
        for k in range(a.split):
            s0, s1 = k * step, (k + 1) * step if k < a.split - 1 else T2
            r2 = race(P, long_ok[s0:s1], short_ok[s0:s1], atr[s0:s1], bbm[s0:s1],
                      bbl[s0:s1], bbu[s0:s1], a.max_bars, a.stride,
                      rowslice=slice(s0, s1))
            tot2 = r2["LONG"] + r2["SHORT"]
            st2 = summarize(tot2, 0.08, 0.08, 0.08)
            if st2:
                print(f"  구간{k + 1}/{a.split}  n={st2['n']:5d}  건당순{st2['net']:+8.4f}")
    print()
    print("⚠ 진입가는 조건 성립 시점의 종가로 가정한다(지정가/시장가 여부는 미확인).")
    print("⚠ 이 결과는 e7_signal() 로직의 경로 시뮬레이션이지 라이브 체결의 완전한 재현이 아니다.")


if __name__ == "__main__":
    main()
