"""고정된 신호 하나를 train/holdout + 순열검정으로 판정한다.

`edge_phase_holdout.py` 는 "24개 위상 중 8개를 고르는" 선택 자유도를 검정하려고
만든 것이다. 이 스크립트는 다르다 — **고를 것이 없는, 이미 사전등록된 규칙**을
아웃오브샘플에서 재는 용도다.

배경(2026-09-01): CLAUDE.md 원칙 0 보강 13 의 후보
`[볼밴눌림] 기울기 + 중심선 + 4h` 가 오늘 기각한 열두 개 중 유일하게 애매한
위치에 있었다 — 상장폐지·stride·위상 가드를 다 걸고도 전체 부호가 양수이고
두 t 가 살아 있는데(t+3.1/+5.3), **국면 3등분의 구간 2 하나에서만 무너진다.**
그 구간이 진짜 다른 국면이었는지, 90일을 3등분한 게 우연히 나쁜 30일을 잘라낸
것인지 3등분만으로는 알 수 없다. 그래서 아웃오브샘플로 가른다.

────────────────────────────────────────────────────────────────────────
사전 등록 (실행 전 고정)
────────────────────────────────────────────────────────────────────────
  1) 신호는 `--signal` 하나뿐이다. 규칙은 이미 고정돼 있으므로 train 에서
     아무것도 고르지 않는다. train 결과는 서술용이고 **판정은 holdout 으로만** 한다.
  2) 분할은 시간순 train 60% / holdout 40%. 셔플하지 않는다.
  3) 판정은 CLAUDE.md 원칙 0 보강 2 세 조건을 **holdout 에서** 본다.
       조건1 건당평균·심볼중앙값이 둘 다 비용선 초과
       조건2 시각클러스터 t·심볼클러스터 t 가 둘 다 2 이상
       조건3 holdout 을 3등분했을 때 전 구간 같은 부호
  4) **순열검정(심볼 셔플)**: 각 시각에서 신호를 심볼 축으로 무작위 재배치한다.
     시각별 롱/숏 개수와 시간 구조는 보존되고 **"어느 심볼인가"라는 정보만**
     파괴된다. 실제 신호가 이 귀무분포의 상위 5% 밖이면 기각.
     (위상 검정과 달리 여기서는 '선택'이 아니라 '정보'를 검정한다.)
  5) 위 넷을 **모두** 통과해야 후보다. 하나라도 못 넘으면 기각이고, 결과를 보고
     기준을 바꾸지 않는다.
"""
import argparse
import pathlib
import sys

import numpy as np

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from edge_lab import (FEE_MAKER_RT, build_indicators, forward, load_panel,  # noqa: E402
                      signals, stats)


def strided(sg, stride, off, T):
    if stride <= 1:
        return sg
    keep = np.zeros(T, dtype=bool)
    keep[off::stride] = True
    return np.where(keep[:, None], sg, 0).astype(np.int8)


def phase_mean(sg, fn, stride, T, sl):
    """전 위상 평균. 한 위상만 보면 숨은 자유도를 승자로 보고하게 된다(792034c)."""
    rs = []
    for off in range(max(1, stride)):
        r = stats(strided(sg, stride, off, T)[sl], fn[sl])
        if r:
            rs.append(r)
    if not rs:
        return None
    return (int(np.mean([r[0] for r in rs])), float(np.mean([r[1] for r in rs])),
            float(np.nanmean([r[2] for r in rs])), float(np.mean([r[3] for r in rs])),
            float(np.mean([r[4] for r in rs])))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cache", default="scratch_edge_15m_90d.npz")
    ap.add_argument("--extra-cache", default="scratch_edge_delisted_15m.npz")
    ap.add_argument("--interval", default="15m")
    ap.add_argument("--signal", default="[볼밴눌림] 기울기 + 중심선 + 4h")
    ap.add_argument("--horizon", type=int, default=1)
    ap.add_argument("--stride", type=int, default=4)
    ap.add_argument("--train-frac", type=float, default=0.6)
    ap.add_argument("--perm", type=int, default=500)
    ap.add_argument("--seed", type=int, default=20260901)
    a = ap.parse_args()

    paths = [a.cache] + ([a.extra_cache] if a.extra_cache else [])
    P = load_panel(paths, a.interval)
    T, S = P["c"].shape
    I = build_indicators(P)
    SIG = signals(P, I)
    if a.signal not in SIG:
        print(f"[중단] 신호 없음: {a.signal}")
        print("후보:", [k for k in SIG if "볼밴" in k or "눌림" in k])
        return
    sg = SIG[a.signal]
    F = forward(P, [a.horizon])
    with np.errstate(invalid="ignore"):
        FN = F[a.horizon] - np.nanmean(F[a.horizon], axis=1, keepdims=True)

    cut = int(T * a.train_frac)
    tr, ho = slice(0, cut), slice(cut, T)
    print(f"패널: {S}심볼 x {T}봉 ({a.interval})")
    print(f"신호: {a.signal} / 지평 {a.horizon}봉 / stride {a.stride}(위상 전수 평균)")
    print(f"분할: train {cut}봉 / holdout {T - cut}봉 (시간순, 셔플 없음)")
    print(f"비용선(e3 maker) {FEE_MAKER_RT}% / 순열 {a.perm}회(심볼 셔플)")
    print()

    for lbl, sl in (("train", tr), ("holdout", ho)):
        r = phase_mean(sg, FN, a.stride, T, sl)
        if r is None:
            print(f"  {lbl}: 표본 부족")
            continue
        n, m, med, tt, ts = r
        print(f"  {lbl:<8} n={n:6d}  건당 {m:+.4f} | 심볼중앙 {med:+.4f}  "
              f"t {tt:+.2f}/{ts:+.2f}")
    print()

    r = phase_mean(sg, FN, a.stride, T, ho)
    if r is None:
        print("[중단] holdout 표본 부족")
        return
    n, m, med, tt, ts = r

    c1 = min(m, med) > FEE_MAKER_RT
    c2 = abs(tt) >= 2.0 and abs(ts) >= 2.0
    step = (T - cut) // 3
    thirds = []
    for k in range(3):
        s3 = slice(cut + k * step, cut + (k + 1) * step if k < 2 else T)
        rr = phase_mean(sg, FN, a.stride, T, s3)
        thirds.append(rr[1] if rr else np.nan)
    c3 = all(x > 0 for x in thirds) or all(x < 0 for x in thirds)

    print(f"  조건1 평균·중앙 둘 다 비용선({FEE_MAKER_RT}%) 초과 : "
          f"{'통과' if c1 else '실패'}  ({m:+.4f} / {med:+.4f})")
    print(f"  조건2 두 t 모두 >=2                          : "
          f"{'통과' if c2 else '실패'}  ({tt:+.2f} / {ts:+.2f})")
    print(f"  조건3 holdout 3등분 동일 부호                 : "
          f"{'통과' if c3 else '실패'}  {['%+.4f' % x for x in thirds]}")
    print()

    # --- 순열검정: 각 시각에서 신호를 심볼 축으로 셔플 ----------------------
    # 시각별 롱/숏 개수와 시간 구조는 그대로 두고 "어느 심볼인가"만 파괴한다.
    # 실제 신호가 귀무분포 상위 5% 밖이면 그 정보는 값어치가 없다는 뜻이다.
    rng = np.random.default_rng(a.seed)
    base = sg[ho]
    fnh = FN[ho]
    Th = base.shape[0]
    null = []
    for _ in range(a.perm):
        perm = base.copy()
        for i in range(Th):
            row = perm[i]
            if np.any(row != 0):
                perm[i] = row[rng.permutation(S)]
        rs = []
        for off in range(max(1, a.stride)):
            keep = np.zeros(Th, dtype=bool)
            keep[off::a.stride] = True
            rr = stats(np.where(keep[:, None], perm, 0).astype(np.int8), fnh)
            if rr:
                rs.append(rr[1])
        if rs:
            null.append(float(np.mean(rs)))
    null = np.array(null)
    pct = float((null < m).mean() * 100) if null.size else float("nan")
    print(f"[순열검정] 심볼 셔플 {null.size}회 — 시각별 신호 개수는 보존, 심볼만 무작위")
    print(f"  귀무분포 평균 {null.mean():+.4f}  95분위 {np.percentile(null, 95):+.4f}  "
          f"최대 {null.max():+.4f}")
    print(f"  실제 신호 {m:+.4f} -> 백분위 {pct:.1f}%  (p={1 - pct / 100:.3f})")
    c4 = pct >= 95.0
    print(f"  조건4 상위 5% 안 : {'통과' if c4 else '실패'}")
    print()
    print("=" * 68)
    print("판정: " + ("전 관문 통과 — 페이퍼 후보로 승격 가능"
                     if (c1 and c2 and c3 and c4) else "기각"))
    print("=" * 68)


if __name__ == "__main__":
    main()
