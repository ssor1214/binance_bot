"""진짜 페어 트레이딩(두 종목 가격비 회귀) — **사전등록본**.

지금까지 잰 "횡단면 역추세"는 여러 종목을 한꺼번에 서열화하는 것이라 시장
전체 드리프트에 노출된다(그래서 매번 드리프트 중립화를 했다). **진짜 페어
트레이딩은 두 특정 종목의 가격비 하나만 본다** — 원리적으로 시장 방향과
무관해서(둘 다 오르면 비율은 그대로) 이번 세션에서 반복된 "국면 요인" 문제를
피할 가능성이 있다. 다만 페어는 **양다리**라 왕복 비용이 단일종목의 2배다.

────────────────────────────────────────────────────────────────────────
사전 등록 (실행 전 고정)
────────────────────────────────────────────────────────────────────────
  1) 페어 선정은 **train 구간(앞 60%)만으로** 한다 — 3,240개 가능한 쌍 중
     보기 좋은 쌍을 사후에 고르면 다중검정 함정이다(오늘 세션에서 반복
     확인된 것과 같다). train 1분 수익률 상관 상위 K 쌍을 쓰고, **K 를
     5/10/20/40 전부 보고**한다(하나만 고르면 그게 또 사후 선택이다).
  2) 스프레드 = log(price_A) - log(price_B). z-score 는 **직전 1440분(1일)
     rolling** 평균/표준편차 기준(현재값 제외).
  3) 진입: |z| >= 2.0 이면 회귀 베팅
     (z 가 크면 A 고평가 -> A숏+B롱 / z 가 작으면 A 저평가 -> A롱+B숏).
  4) 청산은 **고정시간**(edge_lab 방식과 통일, h분 뒤): 5분/15분 둘 다 낸다.
     반환 = (A 편도수익률) - (B 편도수익률), 부호는 진입방향 반영.
  5) **비용은 2배**로 계산한다 — 두 다리를 동시에 열고 닫으므로
     e3(maker) 왕복 `0.04%*2=0.08%` / 보수(taker) `0.14%*2=0.28%`.
  6) 판정: holdout(뒤 40%) 에서 조건1(비용차감 후 양수) + 조건2(train->holdout
     상관, 심볼셔플과 유사하게 **쌍 순서를 무작위로 섞은 순열귀무분포** 상위
     5% 밖이면 기각) + 조건3(holdout 3등분 부호 유지).
  7) 결과를 보고 K, z임계값, 상관기준을 바꾸지 않는다.

측정 못하는 것: 실제 체결(지정가 대기, 부분체결, 두 다리 중 하나만 체결되는
레그 리스크) — 이 결과는 **레그 리스크 없이 두 다리가 동시에 정확히 체결된다는
낙관 가정**의 상한이다.
"""
import argparse
import pathlib
import sys

import numpy as np

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from edge_lab import FEE_MAKER_RT, FEE_TAKER_RT, load_panel  # noqa: E402


def rolling_z(spread, window=1440):
    """직전 window 분 rolling z-score(현재값 제외). 심볼별 아니라 1개 시계열."""
    n = len(spread)
    z = np.full(n, np.nan)
    cs = np.concatenate([[0.0], np.cumsum(spread)])
    cs2 = np.concatenate([[0.0], np.cumsum(spread * spread)])
    for i in range(window, n):
        s1 = cs[i] - cs[i - window]
        s2 = cs2[i] - cs2[i - window]
        m = s1 / window
        v = max(s2 / window - m * m, 1e-12)
        z[i] = (spread[i] - m) / np.sqrt(v)
    return z


def pair_returns(pa, pb, cut, z_thr, horizons):
    """한 쌍의 (전체구간) 스프레드 신호 -> 지평별 (train, holdout) 반환 리스트."""
    ok = ~np.isnan(pa) & ~np.isnan(pb) & (pa > 0) & (pb > 0)
    if ok.mean() < 0.9:
        return None
    logs = np.log(np.where(ok, pa, np.nan)) - np.log(np.where(ok, pb, np.nan))
    z = rolling_z(logs, 1440)
    n = len(pa)
    out = {h: {"train": [], "holdout": []} for h in horizons}
    for i in range(1440, n - max(horizons) - 1):
        zz = z[i]
        if np.isnan(zz) or abs(zz) < z_thr:
            continue
        direction = -1 if zz > 0 else 1     # z>0: A고평가->A숏(-1)+B롱 / z<0: 반대
        for h in horizons:
            j = i + h
            if j >= n or np.isnan(pa[j]) or np.isnan(pb[j]) or np.isnan(pa[i]) or np.isnan(pb[i]):
                continue
            ra = (pa[j] / pa[i] - 1) * 100 * direction
            rb = (pb[j] / pb[i] - 1) * 100 * (-direction)
            r = ra + rb    # 페어 스프레드 수익률(달러중립 가정)
            bucket = "train" if i < cut else "holdout"
            out[h][bucket].append((i, r))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cache", default="scratch_edge_1m_60d.npz")
    ap.add_argument("--interval", default="1m")
    ap.add_argument("--horizons", default="5,15")
    ap.add_argument("--z", type=float, default=2.0)
    ap.add_argument("--ks", default="5,10,20,40")
    ap.add_argument("--train-frac", type=float, default=0.6)
    ap.add_argument("--perm", type=int, default=300)
    ap.add_argument("--seed", type=int, default=20260904)
    a = ap.parse_args()

    P = load_panel([a.cache], a.interval)
    T, S = P["c"].shape
    syms = P["syms"]
    cut = int(T * a.train_frac)
    hor = [int(x) for x in a.horizons.split(",")]
    print(f"패널: {S}심볼 x {T}봉 / train {cut} / holdout {T - cut}")

    C = P["c"]
    print("train 구간 1분 수익률 상관으로 후보쌍 선정 중...")
    ret_tr = np.diff(C[:cut], axis=0) / C[:cut - 1]
    ok_cols = np.nanstd(ret_tr, axis=0) > 0
    ret_tr = np.where(np.isnan(ret_tr), 0.0, ret_tr)
    corr = np.corrcoef(ret_tr[:, ok_cols].T)
    idx_map = np.where(ok_cols)[0]
    S2 = len(idx_map)
    pairs = []
    for a_ in range(S2):
        for b_ in range(a_ + 1, S2):
            pairs.append((corr[a_, b_], idx_map[a_], idx_map[b_]))
    pairs.sort(key=lambda x: -x[0])
    print(f"후보쌍 {len(pairs)}개 (train 상관 상위: "
          f"{[(syms[p[1]], syms[p[2]], round(p[0], 3)) for p in pairs[:5]]})")
    print()

    fee_m = FEE_MAKER_RT * 2      # 페어 = 2다리
    fee_t = (FEE_TAKER_RT + 0.04) * 2
    print(f"비용선(페어, 2다리): e3(maker) {fee_m:.3f}% / 보수(taker) {fee_t:.3f}%")
    print()

    Ks = [int(x) for x in a.ks.split(",")]
    rng = np.random.default_rng(a.seed)

    for h in hor:
        print(f"=== 지평 {h}분 ===")
        for K in Ks:
            sel = pairs[:K]
            tr_all, ho_all = [], []
            for corr_v, ia, ib in sel:
                res = pair_returns(C[:, ia], C[:, ib], cut, a.z, [h])
                if res is None:
                    continue
                tr_all += [r for _, r in res[h]["train"]]
                ho_all += [r for _, r in res[h]["holdout"]]
            if not ho_all:
                print(f"  K={K:<3} holdout 표본 없음")
                continue
            tr_m = np.mean(tr_all) if tr_all else float("nan")
            ho_m = np.mean(ho_all)
            ho_med = np.median(ho_all)
            net_m = ho_m - fee_m
            print(f"  K={K:<3} train건당{tr_m:+8.4f} n={len(tr_all):<7} | "
                  f"holdout건당{ho_m:+8.4f} 중앙{ho_med:+8.4f} n={len(ho_all):<7} "
                  f"maker비용후{net_m:+8.4f}")
        print()

    # 순열검정: K=20 고정, 무작위 20쌍으로 holdout 재계산 후 비교
    K0 = 20 if 20 in Ks else Ks[len(Ks) // 2]
    print(f"[순열검정] K={K0} 무작위쌍 {a.perm}회 vs train상위 {K0}쌍")
    sel0 = pairs[:K0]
    ho0 = []
    for corr_v, ia, ib in sel0:
        res = pair_returns(C[:, ia], C[:, ib], cut, a.z, [hor[0]])
        if res:
            ho0 += [r for _, r in res[hor[0]]["holdout"]]
    real_mean = np.mean(ho0) if ho0 else float("nan")
    all_idx = list(range(S2))
    null = []
    for _ in range(a.perm):
        cand = rng.choice(all_idx, size=min(2 * K0, S2), replace=False)
        rp = []
        for k in range(K0):
            ia, ib = idx_map[cand[2 * k]], idx_map[cand[2 * k + 1]]
            res = pair_returns(C[:, ia], C[:, ib], cut, a.z, [hor[0]])
            if res:
                rp += [r for _, r in res[hor[0]]["holdout"]]
        if rp:
            null.append(np.mean(rp))
    null = np.array(null)
    if len(null) and not np.isnan(real_mean):
        pct = float((null < real_mean).mean() * 100)
        print(f"  귀무분포 평균{null.mean():+.4f} 95분위{np.percentile(null, 95):+.4f} "
              f"(n={len(null)})")
        print(f"  실제(train상위{K0}) holdout건당{real_mean:+.4f} -> 백분위{pct:.1f}%")
    print()
    print("판정: holdout 건당(중앙값 포함) 비용선 초과 + 순열 백분위>=95% + 3구간 부호유지.")


if __name__ == "__main__":
    main()
