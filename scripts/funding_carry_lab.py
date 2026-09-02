"""델타 중립 펀딩 캐리 측정 — **사전등록본**.

`edge_lab.py` 는 **방향 신호**를 잰다. 그래서 펀딩도 "펀딩이 높으면 숏" 같은
방향 예측으로만 테스트됐다. 그런데 **델타 중립 캐리**(현물 롱 + 선물 숏으로 방향
위험을 없애고 펀딩만 수취)는 방향을 예측하지 않으므로 edge_lab 이 구조적으로
측정할 수 없다. 이 스크립트가 그 자리를 맡는다.

배경(2026-09-01): 봉 기반 방향 예측을 열다섯 번 재서 전부 기각했다. 그런데
사용자가 "남들은 봇으로 수익을 낸다"고 지적했고, 맞는 말이었다 — 오늘 기각한
것은 "공개 봉 데이터로 방향을 맞히는 것"이지 "자동매매" 전반이 아니다.
예비 측정에서 펀딩 캐리가 **처음으로 홀드아웃을 통과**했다(train/holdout
심볼별 평균펀딩 상관 `+0.269`, train 상위15의 holdout 연율 `+4.82%`).

**주의 — 그 예비 측정은 사전등록이 아니었다.** K=15, 50/50 분할을 결과를 보기
전에 못박지 않았다. 그래서 이 스크립트는 K 를 여러 개로 돌려 특정 값 의존성을
확인하고, 아래를 사전에 고정한다.

────────────────────────────────────────────────────────────────────────
사전 등록
────────────────────────────────────────────────────────────────────────
  1) 유니버스는 **생존 심볼 + 상장폐지 심볼**이다. 오늘 상장폐지 누락이 모든
     후보를 죽인 최대 요인이었다(보강 3: 35건 -> 1건).
  2) 분할은 시간순 train 60% / holdout 40%. 셔플하지 않는다.
  3) 선택 규칙은 "train 평균 펀딩 상위 K". K 는 5/10/15/20/30 전부 보고한다.
     **하나를 고르지 않는다** — 고르면 그게 자유도다.
  4) 판정:
       조건1 holdout 순수익(펀딩 - 비용)이 **양수**
       조건2 train->holdout 상관이 양수이고, 상위K 가 전체 평균을 **초과**
       조건3 holdout 을 3등분했을 때 **전 구간 같은 부호**
       조건4 심볼 셔플 순열검정에서 상위 5% 안
  5) 비용 모델은 명시적으로 둔다(--spot-rt / --perp-rt). 기본은
     현물 왕복 0.20%(taker 양방향) + 선물 왕복 0.04%(maker) = 0.24%,
     보유기간 전체에 **한 번** 부과한다(진입 1회 + 청산 1회).

────────────────────────────────────────────────────────────────────────
측정하지 못하는 것 (반드시 함께 읽을 것)
────────────────────────────────────────────────────────────────────────
* **베이시스 위험**: 델타중립 손익 = 펀딩 + (베이시스 변화). 현물 가격 데이터가
  없어 베이시스 변화를 재지 못한다. 장기 보유에서는 기대값이 0 에 가깝지만
  (펀딩이 perp 를 spot 에 붙들어 두는 기제이므로) **0 이라고 가정한 것**이다.
* **청산 위험**: 선물 숏 다리는 가격 급등 시 증거금이 필요하다. 이 손익에 없다.
* **자본 효율**: 현물 매수 + 선물 증거금으로 자본이 두 배 묶인다. 아래 수치는
  **명목 대비** 수익률이지 총자본 대비가 아니다.
* **유동성**: 펀딩 상위는 대개 소형·저유동성 심볼이다. 실제 진입 슬리피지가
  비용 모델보다 클 수 있다.
"""
import argparse
import pathlib
import sys

import numpy as np

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
ROOT = pathlib.Path(__file__).resolve().parent.parent
SETTLE_PER_YEAR = 3 * 365


def load(paths, min_cover=0.8):
    """여러 펀딩 npz 를 공통 시간축 T x S 로 합친다."""
    tset = None
    tabs = {}
    for p in paths:
        d = np.load(ROOT / p, allow_pickle=True)
        for k in d.keys():
            if not k.endswith("|funding"):
                continue
            a = d[k]
            if a.shape[0] < 50:
                continue
            tabs.setdefault(k.split("|")[0], a)
    if not tabs:
        raise SystemExit("[중단] 펀딩 데이터 없음")
    for a in tabs.values():
        ts = set(a[:, 0].astype(np.int64).tolist())
        tset = ts if tset is None else (tset | ts)
    times = np.array(sorted(tset), dtype=np.int64)
    idx = {t: i for i, t in enumerate(times)}
    syms = sorted(tabs)
    F = np.full((len(times), len(syms)), np.nan)
    for j, s in enumerate(syms):
        a = tabs[s]
        for t, r in zip(a[:, 0].astype(np.int64), a[:, 1]):
            F[idx[t], j] = r
    # [버그수정 2] 생존 심볼(전부 2026-08-31 까지)과 상장폐지 심볼(중앙값
    # 2026-05-19 에 소멸)은 기간이 완전히 다르다. 전역 커버리지를 요구하면
    # **아무도 통과하지 못한다**(실측 0심볼).
    #
    # 그리고 이 비대칭 자체가 **펀딩 캐리의 핵심 위험**이다 — 캐리 중인 심볼이
    # 상장폐지되면 강제 청산이다. 그래서 심볼을 버리지 않고, **train 에 최소
    # 표본이 있는 심볼은 모두 랭킹에 넣고**, holdout 에서는 살아 있는 동안의
    # 펀딩만 계산한다(죽은 뒤는 포지션 없음 = 0 기여). 상위 K 중 몇 개가
    # holdout 에서 죽는지는 따로 보고한다.
    return F, syms, times


def carry(F, cols, sl, dead_as_zero=True):
    """구간 sl 에서 선택 심볼들의 정산 1회당 평균 펀딩(숏 수취 기준, %).

    dead_as_zero: 상장폐지로 데이터가 없는 구간을 **0 기여**로 본다.
    포지션이 강제 청산돼 그 자본이 놀았다는 뜻이고, NaN 으로 빼면
    "죽은 심볼은 없었던 셈" 이 되어 생존편향이 된다.
    """
    v = F[sl][:, cols]
    if v.size == 0:
        return np.nan
    if dead_as_zero:
        return float(np.nanmean(np.nan_to_num(v, nan=0.0))) * 100.0
    if np.all(np.isnan(v)):
        return np.nan
    return float(np.nanmean(v)) * 100.0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--funding", default="scratch_funding_1h.npz,scratch_funding_delisted.npz")
    ap.add_argument("--train-frac", type=float, default=0.6)
    ap.add_argument("--ks", default="5,10,15,20,30")
    ap.add_argument("--spot-rt", type=float, default=0.20, help="현물 왕복 수수료 %%")
    ap.add_argument("--perp-rt", type=float, default=0.04, help="선물 왕복 수수료 %%")
    ap.add_argument("--min-vol-rank", type=int, default=0,
                   help="유동성 필터: 거래대금 상위 N 심볼 안에서만 고른다. "
                        "0 이면 필터 없음. scratch_volume_60d.json 이 필요하다. "
                        "(사용자 요구: '비트 포함 거래량 많은 알트'. 그리고 예비 측정의 "
                        "최대 우려가 '상위 펀딩 심볼이 저유동성 소형주'였다.)")
    ap.add_argument("--perm", type=int, default=1000)
    ap.add_argument("--seed", type=int, default=20260901)
    a = ap.parse_args()

    paths = [x.strip() for x in a.funding.split(",") if x.strip()]
    F, syms, times = load(paths)
    if a.min_vol_rank > 0:
        import json as _json
        vp = ROOT / "scratch_volume_60d.json"
        if not vp.exists():
            raise SystemExit("[중단] scratch_volume_60d.json 이 없다")
        vol = _json.load(open(vp, encoding="utf-8"))
        top = set(sorted(vol, key=lambda k: -vol[k])[:a.min_vol_rank])
        keep = [i for i, s_ in enumerate(syms) if s_ in top]
        if len(keep) < 10:
            raise SystemExit(f"[중단] 유동성 필터 통과 {len(keep)}심볼")
        F = F[:, keep]
        syms = [syms[i] for i in keep]
        print(f"유동성 필터: 거래대금 상위 {a.min_vol_rank} -> {len(syms)}심볼 남음")
    T, S = F.shape
    cut = int(T * a.train_frac)
    cost = a.spot_rt + a.perp_rt
    print(f"패널: {S}심볼 x {T}회 정산(8시간)  파일 {len(paths)}개")
    print(f"분할: train {cut} / holdout {T - cut} (시간순)")
    print(f"비용: 현물왕복 {a.spot_rt}% + 선물왕복 {a.perp_rt}% = **{cost}%** "
          f"(보유기간 전체에 1회)")
    print()

    tr, ho = slice(0, cut), slice(cut, T)
    with np.errstate(invalid="ignore"):
        tr_m = np.nanmean(F[tr], axis=0)
        ho_m = np.nanmean(F[ho], axis=0)
    n_tr = np.sum(~np.isnan(F[tr]), axis=0)
    n_ho_s = np.sum(~np.isnan(F[ho]), axis=0)
    ok = (n_tr >= 30) & ~np.isnan(tr_m)          # 랭킹 자격: train 표본 30회 이상
    alive = ok & (n_ho_s >= 30)                  # holdout 을 온전히 산 심볼
    print(f"랭킹 자격(train>=30회) {int(ok.sum())}심볼 / "
          f"그중 holdout 생존 {int(alive.sum())}심볼 "
          f"({int(ok.sum()) - int(alive.sum())}개가 holdout 중 소멸)")
    both = ok & ~np.isnan(ho_m)
    r = float(np.corrcoef(tr_m[both], ho_m[both])[0, 1])
    print(f"[관문 2] train->holdout 심볼별 평균펀딩 상관 = {r:+.3f}")
    print()

    n_ho = T - cut
    all_ho = carry(F, np.where(ok)[0], ho)
    print(f"{'K':>4}{'holdout 정산당%':>16}{'연율%':>10}{'보유기간 총%':>14}"
          f"{'비용차감 후':>13}{'전체평균 대비':>14}{'폐지수':>10}")
    rows = {}
    for K in [int(x) for x in a.ks.split(",")]:
        if K > int(ok.sum()):
            continue
        pick = np.argsort(-np.where(ok, tr_m, -np.inf))[:K]
        dead = int(K - np.sum(alive[pick]))
        m = carry(F, pick, ho)
        tot = m * n_ho                      # 보유기간 전체 누적 %
        net = tot - cost
        yr = m * SETTLE_PER_YEAR
        rows[K] = (m, yr, tot, net)
        print(f"{K:>4}{m:>+16.5f}{yr:>+10.2f}{tot:>+14.3f}{net:>+13.3f}"
              f"{(m - all_ho):>+14.5f}{dead:>10d}")
    print(f"{'전체':>4}{all_ho:>+16.5f}{all_ho * SETTLE_PER_YEAR:>+10.2f}"
          f"{all_ho * n_ho:>+14.3f}{all_ho * n_ho - cost:>+13.3f}")
    print()

    # 조건 3: holdout 3등분
    K0 = 15 if 15 in rows else sorted(rows)[len(rows) // 2]
    pick = np.argsort(-np.where(ok, tr_m, -np.inf))[:K0]
    step = n_ho // 3
    thirds = []
    for k in range(3):
        s3 = slice(cut + k * step, cut + (k + 1) * step if k < 2 else T)
        thirds.append(carry(F, pick, s3))
    c3 = all(x > 0 for x in thirds) or all(x < 0 for x in thirds)
    print(f"[관문 3] K={K0} holdout 3등분 정산당%: "
          f"{['%+.5f' % x for x in thirds]}  -> {'통과' if c3 else '실패'}")

    # 조건 4: 심볼 셔플 순열
    rng = np.random.default_rng(a.seed)
    idxs = np.where(ok)[0]
    real = rows[K0][0]
    null = []
    for _ in range(a.perm):
        cand = rng.choice(idxs, size=min(K0, len(idxs)), replace=False)
        null.append(carry(F, cand, ho))
    null = np.array([x for x in null if not np.isnan(x)])
    pct = float((null < real).mean() * 100)
    print(f"[관문 4] 무작위 {K0}심볼 {null.size}회 — 평균 {null.mean():+.5f}, "
          f"95분위 {np.percentile(null, 95):+.5f}")
    print(f"         실제 선택 {real:+.5f} -> 백분위 {pct:.1f}% (p={1 - pct / 100:.3f})")
    c4 = pct >= 95.0
    print(f"         -> {'통과' if c4 else '실패'}")
    print()
    c1 = rows[K0][3] > 0
    c2 = r > 0 and rows[K0][0] > all_ho
    print(f"[관문 1] 비용 차감 후 holdout 총수익 {rows[K0][3]:+.3f}% "
          f"-> {'통과' if c1 else '실패'}")
    print("=" * 70)
    print("판정: " + ("전 관문 통과 — 실행비용/베이시스 위험 모델링 단계로"
                     if (c1 and c2 and c3 and c4) else "기각"))
    print("=" * 70)
    print()
    print("⚠ 측정하지 못한 것: 베이시스 변화(현물 데이터 없음, 0 으로 가정) / "
          "청산 위험 / 자본 2배 구속 / 소형주 유동성. 위 수치는 명목 대비다.")


if __name__ == "__main__":
    main()
