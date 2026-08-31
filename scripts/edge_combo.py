"""약한 성분들을 합쳐 비용선을 넘길 수 있는지 — train/holdout 사전등록 실험.

## 왜 조합인가

HANDOFF_2026-08-31 이 기각한 후보 여섯 개는 전부 **단독** 신호였고, 크기가
0.003~0.03% 로 비용선(메이커 0.04%)의 1/4~1/10 이었다. 그런데 14장에서
`눌림 단독 +0.0038` -> `눌림 + 4h정합 +0.0110` 으로 **조건을 얹자 3배**가 됐다.
성분들이 서로 완전히 겹치지 않으면 합이 부분들보다 크다. 이건 아직 안 해봤다.

## 이번에는 다르게 한다 (12·13장에서 배운 것)

12장은 4.1년 전체를 보면서 신호를 골라 t+3.1 을 보고했고 그건 위상 artifact 였다.
13장은 stride 위상이라는 **선택한 줄 모르는 선택**이 있었다. 같은 실수를 막는다.

  1. **성분을 먼저 고정한다.** 아래 5개. 실행 전에 정하고 실행 후에 바꾸지 않는다.
  2. **가중치를 적합하지 않는다.** 전부 동일 가중 합. 자유도 0.
  3. **holdout 을 먼저 잘라둔다.** 뒤쪽 25% 는 마지막에 딱 한 번만 본다.
  4. **위상 전수 평균**으로만 판정한다(13장).
  5. **상장폐지 심볼을 유니버스에 포함**한 기준선을 쓴다(9장) — 메이저 패널에는
     상폐가 없으므로 이 항목은 해당 없음을 명시한다.

## 성분 (사전 등록 — 전부 위상 전수에서 부호가 일정했던 것들)

    C1 추세      EMA20 > EMA50
    C2 모멘텀    RSI14 > 60 / < 40
    C3 상대강도  40봉 횡단면 수익률이 중앙값 위/아래
    C4 CM기울기  HullMA20 이 2봉 전보다 위/아래
    C5 눌림      기울기 방향과 종가-HullMA 위치가 **반대** (유동성 제공 자세)

    합성점수 = C1+C2+C3+C4+C5  (각 -1/0/+1)
    진입: 점수 >= +THRESH 롱 / <= -THRESH 숏

C5 는 14장에서 3분봉·15분봉 양쪽 모든 위상에서 양수였던 유일한 성분이다.
방향 예측이 아니라 체결 자세에 가깝다는 점에서 나머지 넷과 정보 종류가 다르다.
"""
import argparse
import importlib.util
import pathlib
import sys

import numpy as np

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
ROOT = pathlib.Path(__file__).resolve().parent.parent

_spec = importlib.util.spec_from_file_location(
    "edge_lab", str(pathlib.Path(__file__).resolve().parent / "edge_lab.py"))
EL = importlib.util.module_from_spec(_spec)
sys.modules["edge_lab"] = EL
_spec.loader.exec_module(EL)


def components(P, I, lb=40):
    """사전 등록한 5개 성분. 각각 -1/0/+1 행렬."""
    C = P["c"]
    out = {}

    def sign(longm, shortm):
        a = np.zeros(C.shape, dtype=np.int8)
        a[longm] = 1
        a[shortm] = -1
        return a

    out["C1 추세"] = sign(I["ema20"] > I["ema50"], I["ema20"] < I["ema50"])
    out["C2 모멘텀"] = sign(I["rsi"] > 60, I["rsi"] < 40)

    r = np.full(C.shape, np.nan)
    with np.errstate(invalid="ignore"):
        r[lb:] = C[lb:] / C[:-lb] - 1.0
    med = np.nanmedian(r, axis=1, keepdims=True)
    rel = r - med
    out["C3 상대강도"] = sign(rel > 0, rel < 0)

    up = I["hma_up"] == 1
    dn = I["hma_up"] == 0
    out["C4 CM기울기"] = sign(up, dn)
    # 눌림: 기울기 방향과 종가 위치가 반대인 자리 (14장에서 유일하게 전 위상 양수)
    out["C5 눌림"] = sign(up & (C < I["hma"]), dn & (C > I["hma"]))

    valid = ~np.isnan(I["hma"]) & ~np.isnan(C) & ~np.isnan(I["rsi"])
    for k in out:
        out[k] = np.where(valid, out[k], 0).astype(np.int8)
    return out


def phase_stats(sig, FN, stride, T):
    """전 위상 평균 (건당평균, 심볼중앙값, 시각t, 심볼t, 건수)."""
    ms, md, tt, ts, ns = [], [], [], [], []
    for off in range(stride):
        keep = np.zeros(T, dtype=bool)
        keep[off::stride] = True
        sg = np.where(keep[:, None], sig, 0).astype(np.int8)
        r = EL.stats(sg, FN)
        if r:
            ns.append(r[0])
            ms.append(r[1])
            md.append(r[2])
            tt.append(r[3])
            ts.append(r[4])
    if not ms:
        return None
    return (int(np.mean(ns)), float(np.mean(ms)), float(np.nanmean(md)),
            float(np.mean(tt)), float(np.nanmean(ts)), min(ms), max(ms))


def line(name, r, cost):
    if r is None:
        return f"{name:<26}  (표본 부족)"
    n, m, med, tt, ts, lo, hi = r
    mark = "  <-- 비용선 초과" if (m > cost and med > cost) else ""
    return (f"{name:<26}{n:>8}{m:+10.4f}|{med:+9.4f}{tt:+8.2f}{ts:+8.2f}"
            f"   {lo:+.4f}~{hi:+.4f}{mark}")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--cache", default="scratch_edge_1h_major.npz")
    p.add_argument("--interval", default="1h")
    p.add_argument("--hold", type=int, default=24, help="보유 봉수(=stride)")
    p.add_argument("--lookback", type=int, default=40, help="C3 상대강도 룩백")
    p.add_argument("--thresh", type=int, default=3, help="합성점수 진입 문턱")
    p.add_argument("--holdout-frac", type=float, default=0.25)
    p.add_argument("--cost", type=float, default=0.04, help="비용선(메이커 왕복)")
    p.add_argument("--keep-symbols", default="")
    a = p.parse_args()

    P = EL.load_panel([a.cache], a.interval)
    T, S = P["c"].shape
    bar_min = int(a.interval[:-1]) * (60 if a.interval.endswith("h") else 1)
    print(f"패널 {S}심볼 x {T}봉 ({a.interval}) / 보유 {a.hold * bar_min / 60:g}시간 "
          f"/ 문턱 {a.thresh} / 비용선 {a.cost}%")

    I = EL.build_indicators(P)
    comp = components(P, I, a.lookback)
    score = sum(comp.values()).astype(np.int16)

    if a.keep_symbols:
        keep = {x.strip().upper() for x in a.keep_symbols.split(",") if x.strip()}
        mask = np.array([s in keep for s in P["syms"]])[None, :]
        print(f"신호 심볼 {int(mask.sum())}/{S} (순위·기준선은 전체 유지)")
    else:
        mask = np.ones((1, S), dtype=bool)

    F = EL.forward(P, [a.hold])[a.hold]
    with np.errstate(invalid="ignore"):
        FN = F - np.nanmean(F, axis=1, keepdims=True)

    cut = int(T * (1 - a.holdout_frac))
    print(f"train 0~{cut}봉 / holdout {cut}~{T}봉 "
          f"({(T - cut) * bar_min / 1440:.0f}일)\n")

    hdr = (f"{'신호':<26}{'건수':>8}{'위상평균|중앙값':>20}{'시각t':>8}{'심볼t':>8}"
           f"   {'위상 범위':<20}")

    for label, sl in (("TRAIN", slice(0, cut)), ("HOLDOUT", slice(cut, T))):
        print(f"[{label}]")
        print(hdr)
        Ts = sl.stop - sl.start
        fn = FN[sl]
        for k, v in comp.items():
            sg = np.where(mask, v[sl], 0).astype(np.int8)
            print(line(k, phase_stats(sg, fn, a.hold, Ts), a.cost))
        for th in sorted({2, 3, a.thresh}):
            sg = np.zeros((Ts, S), dtype=np.int8)
            sc = score[sl]
            sg[sc >= th] = 1
            sg[sc <= -th] = -1
            sg = np.where(mask, sg, 0).astype(np.int8)
            print(line(f"** 합성 (>= {th})", phase_stats(sg, fn, a.hold, Ts), a.cost))
        print()

    print("판정: train 에서 성분들이 양수여야 하고, **holdout 에서 합성이 "
          "성분 최고치보다 크고 비용선을 넘어야** 의미가 있다.")
    print("가중치를 적합하지 않았으므로 holdout 은 진짜 out-of-sample 이다.")
    print("holdout 을 보고 문턱/룩백을 바꾸면 그 순간 out-of-sample 이 아니게 된다.")


if __name__ == "__main__":
    main()
