"""상위 타임프레임 국면 전환 매매 — **사전등록본**.

사용자 제안: "15분봉으로 국면(추세/횡보)을 판정하고, 1분봉 진입 규칙을
국면에 따라 전환한다 — 횡보면 역추세만, 추세면 순추세만."

지금까지 국면 필터를 세 가지 방식(같은 타임프레임 밴드폭 백분위=F1, MA5/20/60
배열, 장기추세)으로 시도했고 전부 실패했다(HANDOFF_2026-08-31 21·24장,
CLAUDE.md 원칙 0 보강 8·16). 다만 **"국면 판정을 별도의 상위 타임프레임에서
하는" 조합은 리터럴하게는 처음이다.** 그래서 정식으로 잰다.

────────────────────────────────────────────────────────────────────────
사전 등록 (실행 전 고정)
────────────────────────────────────────────────────────────────────────
  1) 국면 판정: **15분봉**(사용자 요청)을 캘린더 정렬로 리샘플한다(1분봉을
     15개씩 묶는 게 아니라 UTC 15분 경계로 자른다 — lookahead 방지).
     밴드폭 = (bbu-bbl)/bbm, 96개 15분봉(=24시간) rolling 분포에서의 백분위.
     **중앙값 분할**(상위50%=추세국면 / 하위50%=횡보국면)을 쓴다. 극단 백분위
     (예: 80~100)를 고르면 그게 사후 선택이 된다 — F1 이 이미 그 함정에 걸렸었다.
  2) 국면은 **마감된 직전 15분봉**의 것만 쓴다(현재 진행 중인 15분봉 사용 금지
     = lookahead 방지). 15분봉이 마감되기 전까지는 그 이전 국면이 유지된다.
  3) 진입 신호(1분봉):
       추세국면 -> CM 상태(HullMA20 기울기 + 가격위치), 현행 e3/e5 와 동일 정의
       횡보국면 -> 볼밴 단순 터치 역추세(하단->롱/상단->숏), 21장에서 측정한 최선
  4) 대조군 3개를 **함께** 낸다(하나만 보면 사후 선택):
       - CM 단독(국면 무시, 항상 추세규칙)
       - 순수 볼밴 역추세 단독(국면 무시, 항상 역추세규칙)
       - **국면 반대로 배선**(추세국면에 역추세, 횡보국면에 추세) — 대조가
         맞는지 확인하는 안전장치. 이게 더 좋으면 국면 라벨이 뒤집힌 것이다.
  5) 보유(사용자 요청): **5분(스캘핑)** 과 **15분(짧은 스윙, 강제종료 아님을
     고정시간 청산으로 근사)** 둘 다 낸다. stride = 보유봉수(겹침 제거).
  6) 판정은 CLAUDE.md 원칙 0 보강 2 세 조건 + 이중 비용선(FEE_MAKER_RT/
     FEE_TAKER_RT, edge_lab 과 공유) + 3구간 국면 검증.
  7) 결과를 보고 국면 임계값(중앙값)이나 대조군을 바꾸지 않는다.

이 스크립트는 **진입 엣지만** 잰다(edge_lab 방식 — 고정시간 강제청산, TP/SL
없음). 여기서 양수가 나와도 e6 처럼 TP/SL 구조를 씌우면 사라질 수 있다는 것을
이미 여러 번 확인했다(HANDOFF_E6 11장) — 그 검증은 이 결과가 양수일 때만
bb_race_lab.py 로 이어서 한다.
"""
import argparse
import pathlib
import sys

import numpy as np

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from edge_lab import (FEE_MAKER_RT, FEE_TAKER_RT, build_indicators,  # noqa: E402
                      forward, load_panel, stats)


def resample_15m(P):
    """1분봉 패널을 UTC 15분 경계로 캘린더 정렬 리샘플."""
    t = P["t"].astype(np.int64)
    bucket = t // (15 * 60_000)
    change = np.zeros(len(t), dtype=bool)
    change[0] = True
    change[1:] = bucket[1:] != bucket[:-1]
    starts = np.where(change)[0]
    ends = np.r_[starts[1:], len(t)]
    T15 = len(starts)
    S = P["c"].shape[1]
    o15 = np.full((T15, S), np.nan)
    h15 = np.full((T15, S), np.nan)
    l15 = np.full((T15, S), np.nan)
    c15 = np.full((T15, S), np.nan)
    t15 = t[starts]
    for k, (s, e) in enumerate(zip(starts, ends)):
        seg_o, seg_h, seg_l, seg_c = P["o"][s:e], P["h"][s:e], P["l"][s:e], P["c"][s:e]
        with np.errstate(invalid="ignore"):
            valid = ~np.all(np.isnan(seg_c), axis=0)
        for j in np.where(valid)[0]:
            col_o = seg_o[:, j]
            ok = ~np.isnan(col_o)
            if ok.any():
                o15[k, j] = col_o[ok][0]
            col_c = seg_c[:, j]
            ok = ~np.isnan(col_c)
            if ok.any():
                c15[k, j] = col_c[ok][-1]
            h15[k, j] = np.nanmax(seg_h[:, j])
            l15[k, j] = np.nanmin(seg_l[:, j])
    return dict(t=t15, o=o15, h=h15, l=l15, c=c15), starts, ends


def roll_bandwidth_pct(c15, lookback=96):
    """15분봉 밴드폭의 심볼별 rolling 백분위. **직전까지만** 본다(현재봉 제외)."""
    T, S = c15.shape
    m = np.full((T, S), np.nan)
    sd = np.full((T, S), np.nan)
    for j in range(S):
        col = c15[:, j]
        for i in range(20, T):
            w = col[max(0, i - 19):i + 1]
            ok = ~np.isnan(w)
            if ok.sum() >= 15:
                m[i, j] = np.nanmean(w)
                sd[i, j] = np.nanstd(w)
    bw = np.where(m > 0, (4 * sd) / m, np.nan)   # 2σ 밴드폭 근사 (bbu-bbl)/bbm
    q = np.full((T, S), np.nan)
    for j in range(S):
        v = bw[:, j]
        for i in range(lookback, T):
            w = v[i - lookback:i]              # 직전 lookback개(현재 제외)
            ok = ~np.isnan(w)
            if ok.sum() > lookback // 2 and not np.isnan(v[i]):
                q[i, j] = (w[ok] < v[i]).mean()
    return q, bw


def broadcast_regime(regime15, t15, starts15, ends15, T1m, S):
    """15분 국면(0/1/nan)을 1분 타임라인으로 뿌린다. **직전 마감봉**만 쓴다
    (해당 15분봉이 진행 중일 때는 그 이전 국면을 유지 — lookahead 방지)."""
    out = np.full((T1m, S), np.nan)
    for k in range(len(starts15)):
        s, e = starts15[k], ends15[k]
        prev = regime15[k - 1] if k > 0 else np.full(S, np.nan)
        out[s:e] = prev
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cache", default="scratch_edge_1m_60d.npz")
    ap.add_argument("--interval", default="1m")
    ap.add_argument("--horizons", default="5,15")
    ap.add_argument("--split", type=int, default=3)
    a = ap.parse_args()

    P = load_panel([a.cache], a.interval)
    I = build_indicators(P)
    T, S = P["c"].shape
    print(f"패널: {S}심볼 x {T}봉 (1분봉)")

    P15, starts, ends = resample_15m(P)
    q15, _ = roll_bandwidth_pct(P15["c"], lookback=96)
    regime15 = np.where(q15 >= 0.5, 1.0, np.where(np.isnan(q15), np.nan, 0.0))
    # 1 = 추세국면(밴드폭 상위50%) / 0 = 횡보국면(하위50%)
    regime1m = broadcast_regime(regime15, P15["t"], starts, ends, T, S)
    tr_share = np.nanmean(regime1m)
    print(f"국면 라벨(15분봉 중앙값 분할): 추세국면 비율 {tr_share:.1%} "
          f"(0.5 근처가 정상 — 분할이 균형잡혔다는 뜻)")
    print()

    C = P["c"]
    up = I["hma_up"] == 1
    dn = I["hma_up"] == 0
    cm_l = up & (C > I["hma"])
    cm_s = dn & (C < I["hma"])
    touch_l = C <= I["bbl"]
    touch_s = C >= I["bbu"]
    valid = ~np.isnan(I["hma"]) & ~np.isnan(C) & ~np.isnan(regime1m)

    def sig(mask_l, mask_s):
        a_ = np.zeros(C.shape, dtype=np.int8)
        a_[mask_l & valid] = 1
        a_[mask_s & valid] = -1
        return a_

    trend_regime = regime1m == 1
    range_regime = regime1m == 0

    SIG = {
        "[전환] 추세국면=CM / 횡보국면=역추세": sig(
            (trend_regime & cm_l) | (range_regime & touch_l),
            (trend_regime & cm_s) | (range_regime & touch_s)),
        "[대조] 반대배선(추세=역추세/횡보=CM)": sig(
            (trend_regime & touch_l) | (range_regime & cm_l),
            (trend_regime & touch_s) | (range_regime & cm_s)),
        "[대조] CM 단독(국면 무시)": sig(cm_l, cm_s),
        "[대조] 순수볼밴 역추세 단독(국면 무시)": sig(touch_l, touch_s),
    }

    hor = [int(x) for x in a.horizons.split(",")]
    F = forward(P, hor)
    FN = {}
    for h in hor:
        with np.errstate(invalid="ignore"):
            FN[h] = F[h] - np.nanmean(F[h], axis=1, keepdims=True)

    line_t = FEE_TAKER_RT + 2 * 0.02
    line_m = FEE_MAKER_RT
    print(f"비용선: 보수(taker) {line_t:.3f}% / e3(maker) {line_m:.3f}%")
    print()

    def stride_stats(sgn, fn, stride):
        rs = []
        n_rows = sgn.shape[0]
        for off in range(stride):
            keep = np.zeros(n_rows, dtype=bool)
            keep[off::stride] = True
            m2 = np.where(keep[:, None], sgn, 0).astype(np.int8)
            r = stats(m2, fn)
            if r:
                rs.append(r)
        if not rs:
            return None
        return (int(np.mean([r[0] for r in rs])), float(np.mean([r[1] for r in rs])),
                float(np.nanmean([r[2] for r in rs])), float(np.mean([r[3] for r in rs])),
                float(np.mean([r[4] for r in rs])))

    for h in hor:
        mm = h
        print(f"=== 보유 {mm}분 (stride={h}, 위상 전수 평균) ===")
        print(f"{'신호':<38}{'건수':>9}{'건/심볼일':>10}{'건당|심볼중앙':>20}"
              f"{'t(시각/심볼)':>14}")
        for name, sgn in SIG.items():
            r = stride_stats(sgn, FN[h], h)
            if r is None:
                print(f"{name:<38}{'표본부족':>9}")
                continue
            n, m, med, tt, ts = r
            days = T / 1440.0
            print(f"{name:<38}{n:>9}{n / S / days:>10.1f}"
                  f"{m:>+9.4f}|{med:>+8.4f}{tt:>+7.1f}/{ts:>6.1f}")
        print()
        if a.split > 1:
            step = T // a.split
            for k in range(a.split):
                sl = slice(k * step, (k + 1) * step if k < a.split - 1 else T)
                print(f"  --- 구간 {k + 1}/{a.split} ---")
                for name, sgn in SIG.items():
                    sgx = sgn[sl, :]
                    r = stride_stats(sgx, FN[h][sl, :], h)
                    if r is None:
                        continue
                    n, m, med, tt, ts = r
                    print(f"  {name:<36}{m:>+9.4f}|{med:>+8.4f}{tt:>+7.1f}/{ts:>6.1f}")
        print()

    print("판정 규칙: 평균·중앙값 둘 다 비용선 초과 + 두 t 모두 >=2 + 3구간 동일부호.")
    print("이 결과는 진입 엣지(고정시간 청산)만 잰다. TP/SL 구조 검증은 별도로 한다.")


if __name__ == "__main__":
    main()
