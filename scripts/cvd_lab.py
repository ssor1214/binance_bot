"""거래량 불균형(CVD, 매수/매도 체결량 비율) — **사전등록본**.

지금까지 잰 39개 신호는 전부 **가격에서 유도한** 지표였다(CM/RSI/볼밴/MA는
결국 가격의 함수다). `taker_buy_base_volume`(그 분봉에서 시장가 매수로 체결된
수량)은 가격이 아니라 **주문흐름 그 자체**를 담은, 이 세션에서 처음 쓰는
독립 정보원이다. 다만 이것은 진짜 호가창(주문북)이 아니라 **사후 체결 결과의
매수/매도 분해**라는 한계가 있다 — obook_recorder 가 쌓는 실시간 호가와는
다른 축이다.

────────────────────────────────────────────────────────────────────────
사전 등록 (실행 전 고정)
────────────────────────────────────────────────────────────────────────
  1) 불균형 정의: imb = (buyvol - sellvol) / v = 2*buyvol/v - 1.
     sellvol = v - buyvol. v=0(무거래봉)인 곳은 NaN.
  2) 신호는 **직전 N분 합산** 불균형(현재 진행중인 봉은 미포함 - lookahead 방지):
     N ∈ {1, 5, 20} (스캘핑 해상도부터 그 위까지 셋 다 낸다).
  3) 방향은 **모멘텀/역추세 둘 다** 등록한다(하나만 고르면 사후 선택):
       모멘텀:  imb 크다(매수우위) -> LONG  / imb 작다(매도우위) -> SHORT
       역추세:  imb 크다(매수우위) -> SHORT / imb 작다(매도우위) -> LONG
     임계값은 |z-score| >= 1.0 (확장윈도우 z, expanding, 최소 100개).
  4) 팔로워 순방향 수익률은 edge_lab.forward() 로 계산하고 드리프트
     중립화(횡단면 평균 차감)한다.
  5) 지평(스캘핑): 5분/15분. stride=지평.
  6) 판정은 CLAUDE.md 원칙 0 보강 2 세 조건 + 이중 비용선 + 3구간.
  7) 결과를 보고 N, z임계값, 방향을 바꾸지 않는다.

측정 못하는 것: 체결 후 분류(taker 매수 vs 매도)라 실시간 호가 정보가 아니다.
큰 손이 지정가로 잠식하듯 매수하면 taker 매수로 안 잡혀 이 신호에 안 보인다.
"""
import argparse
import pathlib
import sys

import numpy as np

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from edge_lab import FEE_MAKER_RT, FEE_TAKER_RT, forward, stats  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parent.parent


def load_panel_cvd(path, interval):
    """load_panel과 동일한 시간정렬이지만 buyvol(7번째 컬럼)까지 포함."""
    z = np.load(ROOT / path, allow_pickle=True)
    syms = [str(x) for x in z["__symbols__"]]
    tset = set()
    for s in syms:
        tset |= set(z[f"{s}|{interval}"][:, 0].astype(np.int64).tolist())
    times = sorted(tset)
    T, S = len(times), len(syms)
    idx = {t: i for i, t in enumerate(times)}
    P = {k: np.full((T, S), np.nan) for k in ("o", "h", "l", "c", "v", "buyvol")}
    for j, s in enumerate(syms):
        b = z[f"{s}|{interval}"]
        keep = [(i, idx[t]) for i, t in enumerate(b[:, 0].astype(np.int64).tolist())
                if t in idx]
        if not keep:
            continue
        src = np.array([k[0] for k in keep])
        rows = np.array([k[1] for k in keep])
        for k, col in zip(("o", "h", "l", "c", "v", "buyvol"), range(1, 7)):
            P[k][rows, j] = b[src, col]
    P["t"] = np.asarray(times, dtype=np.int64)
    P["syms"] = syms
    return P


def expanding_z_prev(x, min_n=100):
    T, S = x.shape
    valid = (~np.isnan(x)).astype(np.float64)
    xv = np.where(np.isnan(x), 0.0, x)
    cnt = np.vstack([np.zeros((1, S)), np.cumsum(valid, axis=0)])[:-1]
    s1 = np.vstack([np.zeros((1, S)), np.cumsum(xv, axis=0)])[:-1]
    s2 = np.vstack([np.zeros((1, S)), np.cumsum(xv * xv, axis=0)])[:-1]
    with np.errstate(invalid="ignore", divide="ignore"):
        mean = np.where(cnt > 0, s1 / np.maximum(cnt, 1), np.nan)
        var = np.where(cnt > 0, s2 / np.maximum(cnt, 1) - mean ** 2, np.nan)
    var = np.clip(var, 1e-12, None)
    std = np.sqrt(var)
    with np.errstate(invalid="ignore", divide="ignore"):
        z = (x - mean) / std
    z[cnt < min_n] = np.nan
    return z


def rolling_sum_prev(x, n):
    """직전 n분 합(현재 진행봉 제외) — shift(1) 후 rolling sum."""
    T, S = x.shape
    xv = np.where(np.isnan(x), 0.0, x)
    valid = (~np.isnan(x)).astype(np.float64)
    cs = np.vstack([np.zeros((1, S)), np.cumsum(xv, axis=0)])
    cc = np.vstack([np.zeros((1, S)), np.cumsum(valid, axis=0)])
    out = np.full((T, S), np.nan)
    outc = np.zeros((T, S))
    if T > n:
        out[n:] = cs[n:T] - cs[0:T - n]
        outc[n:] = cc[n:T] - cc[0:T - n]
    # 현재 진행봉을 빼기 위해 한 칸 shift
    out2 = np.full((T, S), np.nan)
    out2[1:] = out[:-1]
    outc2 = np.zeros((T, S))
    outc2[1:] = outc[:-1]
    out2[outc2 < n * 0.5] = np.nan
    return out2


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cache", default="scratch_edge_1m_30d_cvd.npz")
    ap.add_argument("--interval", default="1m")
    ap.add_argument("--horizons", default="5,15")
    ap.add_argument("--ns", default="1,5,20")
    ap.add_argument("--z", type=float, default=1.0)
    ap.add_argument("--split", type=int, default=3)
    a = ap.parse_args()

    P = load_panel_cvd(a.cache, a.interval)
    T, S = P["c"].shape
    print(f"패널: {S}심볼 x {T}봉 ({a.interval}, CVD)")

    v, bv = P["v"], P["buyvol"]

    hor = [int(x) for x in a.horizons.split(",")]
    ns = [int(x) for x in a.ns.split(",")]
    F = forward(P, hor)
    FN = {}
    for h in hor:
        with np.errstate(invalid="ignore"):
            FN[h] = F[h] - np.nanmean(F[h], axis=1, keepdims=True)

    line_t = FEE_TAKER_RT + 2 * 0.02
    line_m = FEE_MAKER_RT
    print(f"비용선: 보수(taker) {line_t:.3f}% / e3(maker) {line_m:.3f}%")
    print()

    def phase_stats(sg, fn, stride):
        rs = []
        for off in range(stride):
            keep = np.zeros(T, dtype=bool)
            keep[off::stride] = True
            m2 = np.where(keep[:, None], sg, 0).astype(np.int8)
            r = stats(m2, fn)
            if r:
                rs.append(r)
        if not rs:
            return None
        return (int(np.mean([r[0] for r in rs])), float(np.mean([r[1] for r in rs])),
                float(np.nanmean([r[2] for r in rs])), float(np.mean([r[3] for r in rs])),
                float(np.mean([r[4] for r in rs])))

    results = {}
    for n in ns:
        # imb 는 buyvol합/v합 방식으로 n분 평균(단순평균이 아니라 거래량가중 비율)
        bv_n = rolling_sum_prev(bv, n)
        v_n = rolling_sum_prev(v, n)
        with np.errstate(invalid="ignore", divide="ignore"):
            imb_avg = np.where(v_n > 0, (2 * bv_n / v_n - 1), np.nan)
        z = expanding_z_prev(imb_avg)
        hi = z >= a.z
        lo = z <= -a.z
        SIG = {
            "[CVD] 모멘텀(매수우위→LONG)": (hi, lo),
            "[CVD] 역추세(매수우위→SHORT)": (lo, hi),
        }
        for name, (lm, sm) in SIG.items():
            key = f"N={n:<3} {name}"
            print(f"=== {key} ===")
            for h in hor:
                sg = np.zeros((T, S), dtype=np.int8)
                sg[lm] = 1
                sg[sm] = -1
                r = phase_stats(sg, FN[h], h)
                if r is None:
                    print(f"  지평{h}분  표본부족")
                    continue
                n_, m, med, tt, ts = r
                days = T / 1440.0
                print(f"  지평{h}분  n={n_:8d}  건/심볼일{n_ / S / days:7.1f}  "
                      f"건당{m:+8.4f}|심볼중앙{med:+8.4f}  t{tt:+6.1f}/{ts:+5.1f}")
                results[(n, name, h)] = (sg, r)
            print()

    # 국면 3등분: N=5, 모멘텀/역추세 둘 다, 지평 5분 (사전등록 - 대표조합)
    if a.split > 1:
        h0 = hor[0]
        n0 = 5 if 5 in ns else ns[0]
        bv_n = rolling_sum_prev(bv, n0)
        v_n = rolling_sum_prev(v, n0)
        with np.errstate(invalid="ignore", divide="ignore"):
            imb_avg = np.where(v_n > 0, (2 * bv_n / v_n - 1), np.nan)
        z = expanding_z_prev(imb_avg)
        hi = z >= a.z
        lo = z <= -a.z
        print(f"=== 국면 3등분 (N={n0}, 지평{h0}분) ===")
        step = T // a.split
        for label, (lm, sm) in {"모멘텀": (hi, lo), "역추세": (lo, hi)}.items():
            for k in range(a.split):
                sl = slice(k * step, (k + 1) * step if k < a.split - 1 else T)
                sg = np.zeros((T, S), dtype=np.int8)
                sg[lm] = 1
                sg[sm] = -1
                r = phase_stats(sg[sl], FN[h0][sl], h0)
                if r is None:
                    continue
                n_, m, med, tt, ts = r
                print(f"  {label} 구간{k + 1}/{a.split}  건당{m:+8.4f}|심볼중앙{med:+8.4f} "
                      f"t{tt:+6.1f}/{ts:+5.1f}")
    print()
    print("판정: 평균·중앙값 둘 다 비용선 초과 + 두 t 모두 >=2 + 3구간 동일부호.")


if __name__ == "__main__":
    main()
