# -*- coding: utf-8 -*-
"""슬롯 배분(신호 선별)에 값어치가 있는가 — 사전등록 측정 (2026-09-02).

## 왜 이걸 재는가

보강 18 의 V3(밴드 지정가 체결)는 **418.6 건/심볼일** 을 만든다. 81심볼이면
하루 33,900건인데 슬롯 10개 x 15분 보유로는 하루 960건, 즉 **신호의 2.8% 만**
소화한다. 나머지 97.2% 를 어떻게 버릴지가 **남은 마지막 자유도**다.

지금까지의 모든 측정은 신호를 **전부 같은 값어치로** 평균냈다. 만약 어떤 신호가
다른 신호보다 체계적으로 낫다면, 그것만 골라 슬롯을 채우는 것만으로 건당 엣지가
올라간다 — 신호를 새로 찾지 않고도.

## 이 축은 사후 선택 위험이 가장 큰 축이다

보강 13 에서 심볼 셔플 순열검정이 정확히 이 종류의 후보를 죽였다(백분위 69.4%,
p=0.31). "골라보니 좋더라"는 표본 안에서는 **항상** 성립한다. 그래서 측정 설계를
결과 보기 전에 못박는다.

## 사전 등록

  모집단   V3 체결(보강 18) / 보유 15봉 / stride 15 / 위상 15개 전수
  선별축   아래 5개만 본다. 결과를 보고 더 붙이지 않는다.
           F1 밴드폭      (bbu-bbl)/bbm            — 변동성이 큰 자리가 나은가
           F2 거래량비    v / 20봉 평균            — 수급이 몰린 자리가 나은가
           F3 역추세 강도 5봉 수익률의 역방향 크기 — 급하게 떨어진 자리가 나은가
           F4 RSI 극단    |RSI-50| 의 역방향 크기  — 과매도가 깊은 자리가 나은가
           F5 심볼        (train 성적 상위 20%)    — 보강 13 의 대조군. 죽어야 정상
  절차     ① train(앞 60%) 에서 5분위 중 **최고 분위를 고른다**  <- 여기가 선택
           ② holdout(뒤 40%) 에서 **그 분위만** 재측정           <- 여기가 판정
           ③ train/holdout 5분위 프로파일 상관을 낸다
           ④ 순열검정 500회: feature 를 무작위로 섞어 같은 절차를 반복해
              holdout 성적의 **귀무분포**를 만들고 실제 선택의 백분위를 낸다

## 판정

  통과 = holdout 성적이 (a) 무선별 기준선보다 유의하게 높고
         (b) 순열 백분위 95% 이상이고
         (c) train/holdout 프로파일 상관이 양수
  그리고 그러고도 **비용선(0.0216 / 0.0400)** 을 넘어야 의미가 있다.
  하나라도 못 넘으면 기각한다. 5분위는 상위 20% 라 수용량(2.8%)보다 느슨하다 —
  즉 **낙관 방향**이고, 여기서 안 되면 더 좁혀도 안 된다.
"""
import argparse
import pathlib
import sys
import time

import numpy as np

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from edge_lab import load_panel, build_indicators, forward  # noqa: E402


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--cache", default="scratch_edge_1m_60d.npz")
    p.add_argument("--interval", default="1m")
    p.add_argument("--hold", type=int, default=15)
    p.add_argument("--nq", type=int, default=5)
    p.add_argument("--perm", type=int, default=500)
    p.add_argument("--train", type=float, default=0.6)
    a = p.parse_args()

    P = load_panel([a.cache], a.interval)
    I = build_indicators(P)
    O, C, L, H, V = P["o"], P["c"], P["l"], P["h"], P["v"]
    bbl, bbu, bbm = I["bbl"], I["bbu"], I["bbm"]
    T, S = C.shape
    h = a.hold

    def shift_up(x, k):
        o = np.full_like(x, np.nan)
        if k < T:
            o[:T - k] = x[k:]
        return o

    nxt_o, nxt_l, nxt_h = shift_up(O, 1), shift_up(L, 1), shift_up(H, 1)
    exit_c = shift_up(C, 1 + h)
    F = forward(P, [h])[h]
    with np.errstate(invalid="ignore"):
        bench = np.nanmean(F, axis=1, keepdims=True)

    fill_l, fill_s = nxt_l <= bbl, nxt_h >= bbu
    ent_l, ent_s = np.minimum(nxt_o, bbl), np.maximum(nxt_o, bbu)
    with np.errstate(invalid="ignore", divide="ignore"):
        rl = (exit_c / ent_l - 1.0) * 100.0 - bench
        rs = -((exit_c / ent_s - 1.0) * 100.0 - bench)

    ml = fill_l & ~np.isnan(rl)
    ms = fill_s & ~np.isnan(rs) & ~ml
    RET = np.where(ml, rl, np.where(ms, rs, np.nan))
    SIDE = np.where(ml, 1.0, np.where(ms, -1.0, np.nan))   # +1 롱 / -1 숏

    # --- 선별 축 (전부 신호봉 i 까지의 정보만 쓴다) -------------------------
    with np.errstate(invalid="ignore", divide="ignore"):
        f_bw = (bbu - bbl) / bbm
        vm = np.full_like(V, np.nan)
        cs = np.nancumsum(np.nan_to_num(V), axis=0)
        vm[20:] = (cs[20:] - cs[:-20]) / 20.0
        f_vol = V / vm
        mom = np.full_like(C, np.nan)
        mom[5:] = C[5:] / C[:-5] - 1.0
        f_mom = -mom * SIDE                      # 역방향으로 얼마나 세게 갔나
        f_rsi = -(I["rsi"] - 50.0) * SIDE        # 과매도(롱)/과매수(숏) 깊이
    feats = {"F1 밴드폭": f_bw, "F2 거래량비": f_vol,
             "F3 역추세강도": f_mom, "F4 RSI극단": f_rsi}

    mask = ~np.isnan(RET)
    ti, si = np.nonzero(mask)
    ret = RET[ti, si]
    phase = ti % h
    cut = int(T * a.train)
    tr = ti < cut
    ho = ~tr
    print(f"패널 {S}심볼 x {T}봉 / 체결 {ret.size:,}건 "
          f"(train {tr.sum():,} / holdout {ho.sum():,})")
    print(f"보유 {h}봉 / stride {h} / 위상 {h}개 / 5분위 / 순열 {a.perm}회")
    print("비용선: VIP+BNB 0.0216% / 현행 maker 0.0400%\n")

    # --- 그룹평균을 bincount 로 벡터화한다 -------------------------------
    # (성능 전용 변경. 사전등록한 절차·판정 기준은 그대로다. 종전 구현은 순열
    #  500회 x 4축을 마스크 반복으로 돌아 실용 시간 안에 끝나지 않았다.)
    N = ret.size

    def group_mean(q, period, minc=50):
        """(위상 x 분위) 평균 -> 분위별 위상평균. q<0 은 결측."""
        m = period & (q >= 0)
        g = (phase * a.nq + q)[m]
        sw = np.bincount(g, weights=ret[m], minlength=h * a.nq)
        cw = np.bincount(g, minlength=h * a.nq).astype(float)
        with np.errstate(invalid="ignore"):
            mu = np.where(cw > minc, sw / np.where(cw > 0, cw, 1), np.nan)
        mu = mu.reshape(h, a.nq)
        with np.errstate(invalid="ignore"):
            return np.nanmean(mu, axis=0)      # 분위별 위상평균 (nq,)

    def flat_mean(sel):
        v = ret[sel]
        return float(v.mean()) if v.size else float("nan")

    def pavg(sel):
        """위상 전수 평균(선별 없는 기준선용)."""
        q0 = np.where(sel, 0, -1).astype(np.int64)
        return float(group_mean(q0, np.ones(N, dtype=bool), minc=50)[0])

    base_tr, base_ho = pavg(tr), pavg(ho)
    print(f"{'무선별 기준선':<16} train {base_tr:+.4f}   holdout {base_ho:+.4f}")
    print()

    def quintile(x):
        """train 분위 경계로 나눈다(holdout 에 미래정보가 안 들어가게)."""
        q = np.full(x.size, -1, dtype=np.int64)
        good = ~np.isnan(x)
        edges = np.nanquantile(x[good & tr], np.linspace(0, 1, a.nq + 1)[1:-1])
        q[good] = np.searchsorted(edges, x[good])
        return q

    print(f"{'축':<14}{'train 5분위 프로파일':<46}{'선택':>5}"
          f"{'holdout':>10}{'상관':>8}{'순열백분위':>12}")
    rng = np.random.default_rng(20260902)
    total_perm = len(feats) * a.perm + a.perm
    done_perm = 0
    progress_started = time.monotonic()

    def show_progress(force=False):
        """순열 계산 진행률을 콘솔에 즉시 표시한다."""
        elapsed = time.monotonic() - progress_started
        pct = done_perm / total_perm * 100.0 if total_perm else 100.0
        if force or done_perm == total_perm or done_perm % max(1, a.perm // 20) == 0:
            print(f"[진행률] {done_perm:,}/{total_perm:,} ({pct:5.1f}%) "
                  f"경과 {elapsed:.0f}초", flush=True)

    print(f"[진행률] 전체 순열 {total_perm:,}회 시작", flush=True)
    for name, Fm in feats.items():
        x = Fm[ti, si]
        q = quintile(x)
        prof_tr, prof_ho = group_mean(q, tr), group_mean(q, ho)
        pick = int(np.nanargmax(prof_tr))
        real = prof_ho[pick]
        ok = ~np.isnan(prof_tr) & ~np.isnan(prof_ho)
        cor = (np.corrcoef(prof_tr[ok], prof_ho[ok])[0, 1] if ok.sum() > 2
               else float("nan"))
        null = np.empty(a.perm)
        for b in range(a.perm):
            qs = quintile(rng.permutation(x))
            pt = group_mean(qs, tr)
            null[b] = group_mean(qs, ho)[int(np.nanargmax(pt))]
            done_perm += 1
            show_progress()
        pct = float(np.nanmean(null < real) * 100.0)
        prof = " ".join(f"{v:+.4f}" for v in prof_tr)
        print(f"{name:<14}{prof:<46}{pick + 1:>5}{real:>+10.4f}"
              f"{cor:>+8.2f}{pct:>11.1f}%")

    # --- F5 심볼 (보강 13 의 대조군) ---------------------------------------
    ssum = np.bincount(si, weights=ret * tr, minlength=S)
    scnt = np.bincount(si, weights=tr.astype(float), minlength=S)
    sym_tr = np.where(scnt > 200, ssum / np.where(scnt > 0, scnt, 1), np.nan)
    thr = np.nanquantile(sym_tr, 1 - 1.0 / a.nq)
    top = np.nan_to_num(sym_tr, nan=-1e9) >= thr
    real = flat_mean(ho & top[si])
    null = np.empty(a.perm)
    for b in range(a.perm):
        null[b] = flat_mean(ho & rng.permutation(top)[si])
        done_perm += 1
        show_progress()
    print(f"{'F5 심볼상위20%':<14}{'(train 심볼평균 상위 20% 를 고른다)':<46}"
          f"{'-':>5}{real:>+10.4f}{'':>8}"
          f"{float(np.nanmean(null < real) * 100):>11.1f}%")


if __name__ == "__main__":
    main()
