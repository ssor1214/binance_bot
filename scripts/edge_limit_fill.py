# -*- coding: utf-8 -*-
"""밴드 지정가 체결이 표본을 바꾸는가 — 사전등록 측정 (2026-09-02).

## 왜 이걸 재는가

지금까지 열다섯 번의 측정은 **전부** `진입 = 신호봉 다음봉 시가` 였다. 즉
**종가가 밴드 밖으로 마감한 봉만** 신호로 센다. 그런데 역추세를 실제로 매매한다면
밴드에 **지정가를 미리 걸어둔다.** 그러면 체결 조건이 `close <= 하단` 이 아니라
`low <= 하단` 이 되고, 그 차이에 해당하는 봉이 바로

    "밴드를 찍고 곧바로 튀어올라 종가는 밴드 위에서 마감한 봉"

이다. 역추세에서 가장 좋은 거래인데 종가 기준 신호는 이걸 통째로 놓친다.
보강 7 에서 눌림 진입을 기각한 논리(진입 조건을 조이면 승자가 체계적으로 빠진다)가
여기서는 **반대 방향으로** 작동한다 — 지정가는 표본을 넓히고, 넓어지는 부분이
승자 쪽일 수 있다. **한 번도 측정된 적이 없는 축이다.**

## 사전 등록 (결과를 보기 전에 못박는다)

효과가 둘 섞여 있으므로 **분해해서** 넷을 본다. 결과를 보고 더 붙이지 않는다.

  BASE  종가기준 신호 + 다음봉 시가 진입      <- 현행 측정 재현(대조군)
  V1    지정가 체결 조건 + 다음봉 시가 진입    <- **표본 효과만**
  V2    종가기준 신호 + 밴드가격 진입          <- **체결가 효과만**
  V3    지정가 체결 조건 + 밴드가격 진입       <- 실제 전략 (V1 x V2)

  롱 = 하단밴드, 숏 = 상단밴드. 청산은 넷 다 **동일하게** `C[i+1+h]` 로 고정한다
  (청산규칙을 섞으면 승률이 청산규칙과 동어반복이 된다).

## 판정 기준 (원칙 0 보강 2)

  조건 1  건당 평균과 심볼중앙값이 **모두** 비용선 위
          비용선: 현행 maker 왕복 0.040% / VIP+BNB 왕복 0.0216%
  조건 2  시각 클러스터 t 와 심볼 클러스터 t 가 **모두** 2 이상
  조건 3  표본 3등분에서 **전 구간 같은 부호**
  가드    stride = 보유 봉수(겹침 제거) + **위상 전수 평균**으로 판정
          (기본 표는 위상 0 만 본다 — 보강 13 의 함정)

## 이 측정의 낙관 편향 (미리 적어둔다)

  - 지정가가 닿기만 하면 **항상 체결된다고 가정**한다. 큐 순위를 무시하므로
    실제보다 유리하다. 특히 `low == 밴드` 로 스치기만 한 봉은 실제로는 미체결이다.
  - 갭하락은 시가 체결(매수에 유리)로 처리한다.
  - 즉 V3 는 **상한**이다. 상한이 비용선을 못 넘으면 실제는 볼 것도 없다.
"""
import argparse
import pathlib
import sys

import numpy as np

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from edge_lab import load_panel, build_indicators, forward, stats  # noqa: E402


def run(P, I, h, stride, split=0):
    O, C, L, H = P["o"], P["c"], P["l"], P["h"]
    bbl, bbu = I["bbl"], I["bbu"]
    T = C.shape[0]

    # 드리프트 기준선: 대조군 정의(시가->종가)의 같은 시각 전 심볼 평균.
    # 네 변형에 **같은 기준선**을 쓴다. 그래야 차이가 순수하게 체결 효과가 된다.
    F = forward(P, [h])[h]
    with np.errstate(invalid="ignore"):
        bench = np.nanmean(F, axis=1, keepdims=True)

    def shift_up(a, k):
        out = np.full_like(a, np.nan)
        if k < T:
            out[:T - k] = a[k:]
        return out

    nxt_o, nxt_l, nxt_h = shift_up(O, 1), shift_up(L, 1), shift_up(H, 1)
    exit_c = shift_up(C, 1 + h)

    sig_l, sig_s = C <= bbl, C >= bbu          # 종가기준
    hit_l, hit_s = nxt_l <= bbl, nxt_h >= bbu  # 지정가(다음 봉이 밴드에 닿음)

    ent_open = nxt_o
    ent_lim_l = np.minimum(nxt_o, bbl)         # 갭하락이면 시가(매수에 유리)
    ent_lim_s = np.maximum(nxt_o, bbu)

    def build(fill_l, fill_s, el, es):
        with np.errstate(invalid="ignore", divide="ignore"):
            rl = (exit_c / el - 1.0) * 100.0 - bench
            rs = -((exit_c / es - 1.0) * 100.0 - bench)
        V = np.full(C.shape, np.nan)
        S = np.zeros(C.shape, dtype=np.int8)
        ml = np.asarray(fill_l) & ~np.isnan(rl)
        ms = np.asarray(fill_s) & ~np.isnan(rs) & ~ml
        V[ml], V[ms] = rl[ml], rs[ms]
        S[ml | ms] = 1
        return S, V

    variants = {
        "BASE 종가신호 + 시가진입": build(sig_l, sig_s, ent_open, ent_open),
        "V1   지정가체결 + 시가진입": build(hit_l, hit_s, ent_open, ent_open),
        "V2   종가신호 + 밴드가진입": build(sig_l, sig_s, ent_lim_l, ent_lim_s),
        "V3   지정가체결 + 밴드가진입": build(hit_l, hit_s, ent_lim_l, ent_lim_s),
    }

    offs = list(range(stride)) if stride > 1 else [0]
    print(f"\n=== 보유 {h}봉 / stride {stride} / 위상 {len(offs)}개 평균 ===")
    print(f"{'변형':<28}{'건수':>12}{'건당|심볼중앙':>24}{'t(시각/심볼)':>20}{'최악위상':>11}")
    for name, (S, V) in variants.items():
        ms, md, tt, ts, ns = [], [], [], [], []
        for off in offs:
            keep = np.zeros(T, dtype=bool)
            keep[off::stride] = True
            r = stats(np.where(keep[:, None], S, 0).astype(np.int8), V)
            if r:
                ns.append(r[0]); ms.append(r[1]); md.append(r[2])
                tt.append(r[3]); ts.append(r[4])
        if not ms:
            print(f"{name:<28}{'표본부족':>12}")
            continue
        print(f"{name:<28}{int(np.mean(ns)):>12,}"
              f"{np.mean(ms):>+13.4f}|{np.mean(md):>+9.4f}"
              f"{np.mean(tt):>+10.1f}/{np.mean(ts):>+8.1f}{min(ms):>+11.4f}")

    if split:
        print(f"--- 국면 {split}등분 (위상 전수 평균, 건당|심볼중앙) ---")
        b = np.linspace(0, T, split + 1).astype(int)
        for name, (S, V) in variants.items():
            cells = []
            for k in range(split):
                m = np.zeros(T, dtype=bool)
                m[b[k]:b[k + 1]] = True
                ms, md = [], []
                for off in offs:
                    keep = np.zeros(T, dtype=bool)
                    keep[off::stride] = True
                    r = stats(np.where((keep & m)[:, None], S, 0).astype(np.int8), V)
                    if r:
                        ms.append(r[1]); md.append(r[2])
                cells.append(f"{np.mean(ms):+.4f}|{np.mean(md):+.4f}" if ms else "-")
            print(f"{name:<28}" + "  ".join(f"{c:>19}" for c in cells))


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--cache", default="scratch_edge_1m_60d.npz")
    p.add_argument("--extra-cache", default="")
    p.add_argument("--interval", default="1m")
    p.add_argument("--horizons", default="5,15,60")
    p.add_argument("--split", type=int, default=3)
    a = p.parse_args()

    P = load_panel([a.cache] + [x for x in a.extra_cache.split(",") if x], a.interval)
    I = build_indicators(P)
    T, S = P["c"].shape
    print(f"패널: {S}심볼 x {T}봉 ({a.interval})")
    print("비용선: 현행 maker 왕복 0.0400% / VIP+BNB 왕복 0.0216%")
    print("주의: 지정가는 '닿으면 항상 체결' 가정 -> V3 는 **낙관 상한**이다.")
    for h in [int(x) for x in a.horizons.split(",")]:
        run(P, I, h, stride=h, split=a.split)


if __name__ == "__main__":
    main()
