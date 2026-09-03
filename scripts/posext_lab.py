"""포지셔닝 극단 역추세(청산 캐스케이드 프록시) — **사전등록본**.

배경: "청산 캐스케이드를 이용하면?" 이라는 질문에 대한 답. **바이낸스는 과거
청산(forceOrder) 스트림을 REST 로 제공하지 않는다** — 실시간 WS 로만 받을 수
있고, 우리는 그걸 수집한 적이 없다(obook_recorder 는 호가만 수집한다). 즉
"과거 청산 이벤트로 캐스케이드를 재본다"는 **데이터가 없어 불가능**하다.

대신 [edge_metrics.py](edge_metrics.py) 가 이미 수집한 **포지셔닝(OI) 데이터**를
프록시로 쓴다. `tt_sum`(상위트레이더 포지션가중 롱/숏비)이 극단으로 쏠리면
레버리지가 한쪽에 몰린 것이고, 그 방향이 청산될 위험이 커진다 — "쏠림을 거슬러
탄다(contrarian)"가 청산 캐스케이드 아이디어의 근사다. 직접적인 청산 이벤트가
아니라 **그 전조 신호**를 보는 것임을 명확히 한다.

────────────────────────────────────────────────────────────────────────
사전 등록 (실행 전 고정)
────────────────────────────────────────────────────────────────────────
  1) 크라우딩 지표: `tt_sum` 의 **확장윈도우 z-score**(직전까지만, 최소 100개
     관측 필요). 고정폭 rolling 이 아니라 순수 확장(expanding)이라 계산이
     빠르고 재현이 쉽다.
  2) 신호: |z| >= 1.5 일 때만 진입.
       역추세(가설): z>=1.5(쏠림 롱) -> SHORT / z<=-1.5(쏠림 숏) -> LONG
       추세(대조군): z>=1.5 -> LONG / z<=-1.5 -> SHORT  (방향을 사후에 고르지
       않기 위한 짝 — 하나만 등록하면 그게 사후 선택이다)
  3) 시간대: 1시간봉, 상장폐지 심볼 병합. 지평 4/12/24시간, stride=지평.
  4) 판정은 CLAUDE.md 원칙 0 보강 2 세 조건 + 이중 비용선 + 위상 전수 평균.
  5) 결과를 보고 z 임계값을 바꾸지 않는다.

측정 못하는 것: 진짜 청산 이벤트가 아니라 포지셔닝 데이터로 근사한 것이다.
실제 캐스케이드는 훨씬 짧은 시간(초~분)에 발생하는데 1시간봉으로는 그 해상도를
못 본다. 이 결과가 음수여도 "청산 캐스케이드가 없다"를 증명하지 않는다 —
"이 프록시·이 해상도로는 못 잡는다"만 말한다.
"""
import argparse
import pathlib
import sys

import numpy as np

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from edge_lab import FEE_MAKER_RT, FEE_TAKER_RT, forward, load_metrics, load_panel, stats  # noqa: E402


def expanding_z_prev(x, min_n=100):
    """직전까지의 확장평균/표준편차로 z-score(현재값 자기 자신은 기준에서 제외)."""
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


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cache", default="scratch_edge_1h_180d.npz")
    ap.add_argument("--extra-cache", default="scratch_edge_delisted_1h.npz")
    ap.add_argument("--metrics", default="scratch_metrics.npz")
    ap.add_argument("--interval", default="1h")
    ap.add_argument("--horizons", default="4,12,24")
    ap.add_argument("--z", type=float, default=1.5)
    ap.add_argument("--split", type=int, default=3)
    a = ap.parse_args()

    paths = [a.cache] + ([a.extra_cache] if a.extra_cache else [])
    P = load_panel(paths, a.interval)
    T, S = P["c"].shape
    print(f"패널: {S}심볼 x {T}봉 ({a.interval})")

    M, have = load_metrics(a.metrics, P)
    print(f"포지셔닝 데이터 보유 심볼: {have}/{S}")
    tt = M["tt_sum"]
    z = expanding_z_prev(tt)
    crowd_long = z >= a.z
    crowd_short = z <= -a.z
    print(f"신호 발생: crowd_long(쏠림롱) {int(np.nansum(crowd_long))}건 / "
          f"crowd_short(쏠림숏) {int(np.nansum(crowd_short))}건")
    print()

    SIG = {
        "[포지셔닝] 역추세(쏠림을 거스름)": (crowd_short, crowd_long),   # long_mask, short_mask
        "[포지셔닝] 추세(쏠림을 따름/대조군)": (crowd_long, crowd_short),
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

    def phase_stats(lm, sm, fn, stride):
        rs = []
        for off in range(stride):
            keep = np.zeros(T, dtype=bool)
            keep[off::stride] = True
            sg = np.zeros((T, S), dtype=np.int8)
            sg[lm & keep[:, None]] = 1
            sg[sm & keep[:, None]] = -1
            r = stats(sg, fn)
            if r:
                rs.append(r)
        if not rs:
            return None
        return (int(np.mean([r[0] for r in rs])), float(np.mean([r[1] for r in rs])),
                float(np.nanmean([r[2] for r in rs])), float(np.mean([r[3] for r in rs])),
                float(np.mean([r[4] for r in rs])))

    for h in hor:
        print(f"=== 지평 {h}시간 (stride={h}, 위상 전수 평균) ===")
        for name, (lm, sm) in SIG.items():
            r = phase_stats(lm, sm, FN[h], h)
            if r is None:
                print(f"  {name:<34} 표본부족")
                continue
            n, m, med, tt_, ts = r
            days = T / 24.0
            print(f"  {name:<34} n={n:6d}  건/심볼일{n / S / days:6.2f}  "
                  f"건당{m:+8.4f}|심볼중앙{med:+8.4f}  t{tt_:+6.1f}/{ts:+5.1f}")
        print()
    if a.split > 1:
        step = T // a.split
        h0 = hor[len(hor) // 2]
        print(f"=== 국면 3등분 (지평 {h0}시간) ===")
        for k in range(a.split):
            sl = slice(k * step, (k + 1) * step if k < a.split - 1 else T)
            for name, (lm, sm) in SIG.items():
                lm2, sm2 = lm[sl], sm[sl]
                r = phase_stats(lm2, sm2, FN[h0][sl], h0)
                if r is None:
                    continue
                n, m, med, tt_, ts = r
                print(f"  구간{k + 1}/{a.split} {name:<30} 건당{m:+8.4f}|"
                      f"심볼중앙{med:+8.4f} t{tt_:+6.1f}/{ts:+5.1f}")
    print()
    print("판정: 평균·중앙값 둘 다 비용선 초과 + 두 t 모두 >=2 + 3구간 동일부호.")


if __name__ == "__main__":
    main()
