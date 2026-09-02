"""거래소 간 가격차(차익) 측정 — **사전등록본**.

배경(2026-09-01): 봉 기반 **방향 예측**을 열다섯 번 재서 전부 기각했고, 예측을
하지 않는 축(펀딩 캐리)이 처음으로 네 관문을 통과했다(보강 17). 그래서 예측 없는
축을 순서대로 본다. 사용자 우선순위: **1) 거래소 간 차익  2) 베이시스 캐리
3) 펀딩 캐리(완료)**.

────────────────────────────────────────────────────────────────────────
사전 등록 (실행 전 고정)
────────────────────────────────────────────────────────────────────────
  1) **측정 대상은 "가격차가 비용을 넘는가"이지 방향이 아니다.**
     spread_bps(t) = (P_a - P_b) / mid * 10000.
  2) **비용선**: 양쪽 거래소에서 각각 진입·청산해야 하므로 taker 4다리다.
     기본 가정 = 거래소당 왕복 0.10%(taker 0.05 x 2) x 2거래소 = **0.20% = 20bp**.
     `--cost-bp` 로 노출한다. maker 로 깔 수 있다는 가정은 하지 않는다 —
     차익은 **먼저 잡는 쪽이 가져가므로** 지정가 대기가 성립하지 않는다.
  3) 판정:
       관문1 |spread| 가 비용선을 넘는 **시간 비율**이 유의미한가(>=1%)
       관문2 넘은 뒤 **수렴**하는가 — 진입 후 h분 뒤 |spread| 가 줄어드는가
       관문3 그 수렴폭의 평균이 비용선을 넘는가 (= 실제 순익)
       관문4 train/holdout 분할에서 유지되는가
  4) **1분봉 종가로 재는 것은 근사다.** 진짜 차익은 초 이하에 산다. 다만
     **1분 해상도에서 이미 비용선 아래면, 남은 기회는 우리가 경쟁할 수 없는
     지연시간대에 있다**는 뜻이므로 판정으로 충분하다. 이 한계를 결과와 함께 읽을 것.
  5) 결과를 보고 비용 가정이나 관문을 바꾸지 않는다.

측정하지 못하는 것: 양 거래소 동시 체결 실패(레그 리스크) / 출금·입금 지연 /
거래소별 증거금 요건 / 1분봉 종가가 동시에 체결 가능한 가격이라는 보장.
"""
import argparse
import pathlib
import sys

import numpy as np

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
ROOT = pathlib.Path(__file__).resolve().parent.parent


def load(path):
    d = np.load(ROOT / path, allow_pickle=True)
    pairs = {}
    for k in d.keys():
        if k.startswith("__"):
            continue
        sym, ven = k.split("|")
        pairs.setdefault(sym, {})[ven] = d[k]
    return pairs, (str(d["__meta__"][0]).split(",") if "__meta__" in d else [])


def align(a, b):
    """두 (t, close) 배열을 공통 시각으로 맞춘다."""
    ta, tb = a[:, 0].astype(np.int64), b[:, 0].astype(np.int64)
    common = np.intersect1d(ta, tb)
    if common.size < 100:
        return None, None, None
    ia = np.searchsorted(ta, common)
    ib = np.searchsorted(tb, common)
    return common, a[ia, 1], b[ib, 1]


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--cache", default="scratch_xex_1m.npz")
    p.add_argument("--cost-bp", type=float, default=20.0,
                   help="왕복 총비용(bp). 기본 20 = 거래소당 taker 왕복 10bp x 2")
    p.add_argument("--horizons", default="1,5,15,60", help="수렴 확인 분")
    p.add_argument("--train-frac", type=float, default=0.6)
    p.add_argument("--bucket", type=int, default=1,
                   help="관측 주기(분). 1/3/5/15. 폴링을 이 주기로 한다고 보고 "
                        "그 시점의 종가만 남긴다 — 주기가 길수록 짧은 괴리를 놓친다.")
    a = p.parse_args()

    pairs, venues = load(a.cache)
    hor = [int(x) for x in a.horizons.split(",")]
    print(f"심볼 {len(pairs)}개 / 거래소 {venues} / 비용선 {a.cost_bp:.1f}bp "
          f"/ 관측주기 {a.bucket}분")
    print()

    rows = []
    for sym, byv in sorted(pairs.items()):
        vs = sorted(byv)
        if len(vs) < 2:
            continue
        t, pa, pb = align(byv[vs[0]], byv[vs[1]])
        if t is None:
            continue
        if a.bucket > 1:
            # 관측 주기 재현: bucket 분 경계의 봉만 남긴다.
            keep = (t // 60000) % a.bucket == 0
            t, pa, pb = t[keep], pa[keep], pb[keep]
            if len(t) < 100:
                continue
        mid = (pa + pb) / 2.0
        with np.errstate(invalid="ignore", divide="ignore"):
            sp = (pa - pb) / mid * 10000.0
        rows.append((sym, t, sp))

    if not rows:
        raise SystemExit("[중단] 정렬된 쌍 없음")

    allsp = np.concatenate([r[2] for r in rows])
    allsp = allsp[np.isfinite(allsp)]
    q = np.percentile(np.abs(allsp), [50, 90, 99, 99.9])
    over = float((np.abs(allsp) > a.cost_bp).mean() * 100)
    print("=== |가격차| 분포 (bp) ===")
    print(f"  중앙 {q[0]:.2f}  p90 {q[1]:.2f}  p99 {q[2]:.2f}  p99.9 {q[3]:.2f}  "
          f"최대 {np.abs(allsp).max():.1f}")
    print(f"  평균 |spread| {np.abs(allsp).mean():.2f}bp")
    print(f"[관문 1] 비용선 {a.cost_bp:.0f}bp 초과 시간 비율 = **{over:.3f}%**  "
          f"-> {'통과' if over >= 1.0 else '실패'}")
    print()

    # 관문 2·3: 비용선을 넘은 시점에서 진입했다고 보고 h분 뒤 수렴폭
    print("=== 진입 후 수렴 (비용선 초과 시점에서 스프레드 축소분, bp) ===")
    print(f"{'h(버킷)':>8}{'=분':>5}{'표본':>8}{'평균 수렴':>12}{'중앙':>10}"
          f"{'비용차감 후':>13}")
    best = None
    for h in hor:
        gains = []
        for sym, t, sp in rows:
            s = sp
            n = len(s)
            if n <= h:
                continue
            ent = np.abs(s[:n - h])
            ex = np.abs(s[h:])
            m = np.isfinite(ent) & np.isfinite(ex) & (ent > a.cost_bp)
            if m.sum():
                gains.append(ent[m] - ex[m])      # 좁아진 만큼이 수익
        if not gains:
            print(f"{h:>8}{h * a.bucket:>5}{0:>8}{'-':>12}{'-':>10}{'-':>13}")
            continue
        g = np.concatenate(gains)
        net = g.mean() - a.cost_bp
        print(f"{h:>8}{h * a.bucket:>5}{g.size:>8}{g.mean():>+12.2f}"
              f"{np.median(g):>+10.2f}{net:>+13.2f}")
        if best is None or net > best[1]:
            best = (h, net, g)
    print()
    if best is None:
        print("판정: 기각 — 비용선을 넘는 표본이 없다")
        return
    h, net, g = best
    print(f"[관문 2] 수렴 여부: h={h} 평균 축소 {g.mean():+.2f}bp "
          f"-> {'통과' if g.mean() > 0 else '실패'}")
    print(f"[관문 3] 비용 차감 후 {net:+.2f}bp -> {'통과' if net > 0 else '실패'}")

    # 관문 4: train/holdout
    tr_g, ho_g = [], []
    for sym, t, sp in rows:
        n = len(sp)
        cut = int(n * a.train_frac)
        for sl, box in ((slice(0, cut), tr_g), (slice(cut, n), ho_g)):
            s = sp[sl]
            if len(s) <= h:
                continue
            ent, ex = np.abs(s[:len(s) - h]), np.abs(s[h:])
            m = np.isfinite(ent) & np.isfinite(ex) & (ent > a.cost_bp)
            if m.sum():
                box.append(ent[m] - ex[m])
    if tr_g and ho_g:
        tr_g, ho_g = np.concatenate(tr_g), np.concatenate(ho_g)
        print(f"[관문 4] train {tr_g.mean():+.2f}bp (n={tr_g.size}) / "
              f"holdout {ho_g.mean():+.2f}bp (n={ho_g.size}) "
              f"-> {'통과' if ho_g.mean() - a.cost_bp > 0 else '실패'}")
    print()
    print("⚠ 1분봉 종가 근사다. 진짜 차익은 초 이하에 산다. 다만 1분에서 비용선 아래면"
          " 남은 기회는 경쟁 불가능한 지연시간대에 있다는 뜻이다.")
    print("⚠ 안 잰 것: 레그 리스크(한쪽만 체결) / 거래소별 증거금 / 출금 지연 /"
          " 종가가 동시 체결 가능한 가격이라는 보장.")


if __name__ == "__main__":
    main()
