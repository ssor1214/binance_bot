"""위상(=UTC 진입시각) 계절성이 실재하는가 — 홀드아웃 + 순열검정 + 신호간 교차확인.

배경: logs/phase_sweep.txt 에서 횡단면 모멘텀 20봉의 24개 위상이 -0.04 ~ +0.15 로
갈렸고, 양수 구간(UTC 17~02시)이 흩어지지 않고 **연속으로 뭉쳐** 있었다. 커밋
792034c 는 "위상은 숨은 자유도"라며 후보를 기각했는데 그건 옳다. 다만 기각된 것은
*그 후보*이지 **"시간대 효과가 실재하는가"라는 질문은 검증된 적이 없다.**

이 스크립트는 그 질문만 판정한다. 세 관문을 **모두** 통과해야 한다.

  B-1 홀드아웃 : 표본을 시간순 train 60% / holdout 40% 로 자른다.
                 **train 에서만** 위상을 고르고 holdout 에서 잰다.
                 선택 규칙은 사전 등록한다 — "train 건당평균 상위 K개 위상", 고정 K.
                 눈으로 고르지 않고, 결과를 보고 K 를 바꾸지 않는다.
  B-2 순열검정 : "24개 중 K개 고르기" 자체가 자유도다. t 값으로는 판정할 수 없다.
                 train 을 무시하고 무작위 K개를 뽑아 holdout 을 재는 일을 N회 반복해
                 귀무분포를 만든다. train 이 고른 집합이 상위 5% 밖이면 기각.
  B-3 교차확인 : 시간대 효과가 실재한다면 시장 전체의 성질이므로 **신호와 무관하게**
                 같은 시각이 좋아야 한다. 여러 신호의 24차원 위상 프로파일을 뽑아
                 서로 상관을 낸다. 한 신호만 튀면 신호x위상 상호작용 = 과적합이다.

전제(스크립트 실행 전에 확인됨): 1h 패널은 UTC 00:00 정각에서 시작하고 봉 누락이
0개라 **위상 k = UTC k시**가 정확히 성립한다. 이게 깨지면 아래 전부 무의미하다.

비용선은 edge_lab 과 같다(작업 A): e3선(maker/maker) 0.04%.
"""
import argparse
import pathlib
import sys

import numpy as np

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from edge_lab import (FEE_MAKER_RT, build_indicators, forward, load_panel,  # noqa: E402
                      signals, stats)


def phase_stat(sg, fn, T, phase, stride, rowslice):
    """한 위상만 남기고 지정 구간에서 stats 를 낸다."""
    keep = np.zeros(T, dtype=bool)
    keep[phase::stride] = True
    m = np.where(keep[:, None], sg, 0).astype(np.int8)
    return stats(m[rowslice], fn[rowslice])


def subset_stat(sg, fn, T, phases, stride, rowslice):
    keep = np.zeros(T, dtype=bool)
    for p in phases:
        keep[p::stride] = True
    m = np.where(keep[:, None], sg, 0).astype(np.int8)
    return stats(m[rowslice], fn[rowslice])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cache", default="scratch_edge_1h_180d.npz")
    ap.add_argument("--extra-cache", default="scratch_edge_delisted_1h.npz")
    ap.add_argument("--interval", default="1h")
    ap.add_argument("--horizon", type=int, default=24)
    ap.add_argument("--stride", type=int, default=24)
    ap.add_argument("--target", default="횡단면 모멘텀 20봉(강세롱)",
                    help="B-1/B-2 를 적용할 신호(위상 스윕에서 후보였던 것)")
    ap.add_argument("--topk", type=int, default=8, help="사전등록: train 상위 K개 위상")
    ap.add_argument("--train-frac", type=float, default=0.6)
    ap.add_argument("--perm", type=int, default=1000)
    ap.add_argument("--seed", type=int, default=20260901)
    a = ap.parse_args()

    paths = [a.cache] + ([a.extra_cache] if a.extra_cache else [])
    P = load_panel(paths, a.interval)
    T, S = P["c"].shape
    print(f"패널: {S}심볼 x {T}봉 ({a.interval})")

    I = build_indicators(P)
    SIG = signals(P, I)
    F = forward(P, [a.horizon])
    with np.errstate(invalid="ignore"):
        FN = F[a.horizon] - np.nanmean(F[a.horizon], axis=1, keepdims=True)

    cut = int(T * a.train_frac)
    tr, ho = slice(0, cut), slice(cut, T)
    print(f"분할: train {cut}봉 / holdout {T - cut}봉 "
          f"(train_frac={a.train_frac}, 시간순 — 셔플하지 않는다)")
    print(f"사전등록: '{a.target}' / 지평 {a.horizon}봉 / stride {a.stride} / "
          f"train 건당평균 상위 {a.topk}개 위상 / 순열 {a.perm}회 / 비용선 {FEE_MAKER_RT}%")
    print()

    if a.target not in SIG:
        print(f"[중단] 신호 '{a.target}' 없음. 후보: {list(SIG)[:5]} ...")
        return
    sg = SIG[a.target]

    # ---------------- B-1 --------------------------------------------------
    print("[B-1] 위상별 train / holdout")
    print(f"{'위상(UTC시)':>10}{'train 건당':>12}{'holdout 건당':>14}{'holdout n':>11}")
    tr_mean, ho_mean = {}, {}
    for p in range(a.stride):
        rt = phase_stat(sg, FN, T, p, a.stride, tr)
        rh = phase_stat(sg, FN, T, p, a.stride, ho)
        tr_mean[p] = rt[1] if rt else np.nan
        ho_mean[p] = rh[1] if rh else np.nan
        print(f"{p:>10}{tr_mean[p]:>+12.4f}{ho_mean[p]:>+14.4f}"
              f"{(rh[0] if rh else 0):>11}")
    order = sorted((p for p in range(a.stride) if not np.isnan(tr_mean[p])),
                   key=lambda p: -tr_mean[p])
    pick = sorted(order[:a.topk])
    print()
    print(f"train 상위 {a.topk} 위상 = UTC {pick}")

    r = subset_stat(sg, FN, T, pick, a.stride, ho)
    if r is None:
        print("[중단] holdout 표본 부족")
        return
    n, m, med, tt, ts = r
    print(f"holdout 성적: n={n}  건당 {m:+.4f}  심볼중앙 {med:+.4f}  "
          f"t(시각/심볼) {tt:+.2f}/{ts:+.2f}")
    cond1 = min(m, med) > FEE_MAKER_RT
    cond2 = abs(tt) >= 2.0 and abs(ts) >= 2.0
    thirds = []
    step = (T - cut) // 3
    for k in range(3):
        sl = slice(cut + k * step, cut + (k + 1) * step if k < 2 else T)
        rr = subset_stat(sg, FN, T, pick, a.stride, sl)
        thirds.append(rr[1] if rr else np.nan)
    cond3 = all(x > 0 for x in thirds) or all(x < 0 for x in thirds)
    print(f"  조건1 평균·중앙 모두 비용선({FEE_MAKER_RT}%) 초과 : {'통과' if cond1 else '실패'}")
    print(f"  조건2 두 t 모두 >=2                        : {'통과' if cond2 else '실패'}")
    print(f"  조건3 3구간 동일 부호 {['%+.4f' % x for x in thirds]} : "
          f"{'통과' if cond3 else '실패'}")
    print()

    # ---------------- B-2 --------------------------------------------------
    print(f"[B-2] 순열검정 — 무작위 {a.topk}개 위상을 {a.perm}회 뽑아 holdout 귀무분포")
    rng = np.random.default_rng(a.seed)
    null = []
    for _ in range(a.perm):
        cand = sorted(rng.choice(a.stride, size=a.topk, replace=False).tolist())
        rr = subset_stat(sg, FN, T, cand, a.stride, ho)
        if rr:
            null.append(rr[1])
    null = np.array(null)
    pct = float((null < m).mean() * 100)
    print(f"  귀무분포 n={null.size}  평균 {null.mean():+.4f}  "
          f"95분위 {np.percentile(null, 95):+.4f}  최대 {null.max():+.4f}")
    print(f"  실제 선택의 holdout 건당 {m:+.4f} -> 백분위 {pct:.1f}%  "
          f"(p={1 - pct / 100:.3f})")
    cond4 = pct >= 95.0
    print(f"  조건4 상위 5% 안 : {'통과' if cond4 else '실패'}")
    print()

    # ---------------- B-3 --------------------------------------------------
    print("[B-3] 신호 간 위상 프로파일 상관 — 시간대 효과라면 신호와 무관해야 한다")
    probe = [k for k in ("CM 전체 [현행뼈대]", "EMA추세 단독", "볼밴돌파 단독",
                         "RSI 모멘텀 30/70(과매수롱)", "횡단면 모멘텀 20봉(강세롱)",
                         "횡단면 모멘텀 80봉(강세롱)") if k in SIG]
    prof = {}
    for name in probe:
        v = []
        for p in range(a.stride):
            rr = phase_stat(SIG[name], FN, T, p, a.stride, slice(0, T))
            v.append(rr[1] if rr else np.nan)
        prof[name] = np.array(v)
    print(f"{'':<28}" + "".join(f"{i:>8}" for i in range(len(probe))))
    cors = []
    for i, na in enumerate(probe):
        row = []
        for nb in probe:
            x, y = prof[na], prof[nb]
            ok = ~(np.isnan(x) | np.isnan(y))
            row.append(np.corrcoef(x[ok], y[ok])[0, 1] if ok.sum() > 3 else np.nan)
        print(f"{i} {na:<26}" + "".join(f"{c:>+8.2f}" for c in row))
        for j in range(i + 1, len(probe)):
            cors.append(row[j])
    mc = float(np.nanmean(cors))
    print(f"  쌍 평균 상관 {mc:+.3f}")
    cond5 = mc >= 0.3
    print(f"  조건5 신호간 위상 프로파일이 공통(평균상관>=0.3) : "
          f"{'통과' if cond5 else '실패'}")
    print()

    allok = cond1 and cond2 and cond3 and cond4 and cond5
    print("=" * 70)
    print("판정: " + ("전 관문 통과 — 페이퍼 후보로 승격 가능" if allok else
                     "기각 — 시간대 계절성은 실재 근거 없음"))
    print("=" * 70)


if __name__ == "__main__":
    main()
