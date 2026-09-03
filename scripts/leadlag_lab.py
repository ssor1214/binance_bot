"""BTC/ETH 선행 효과(리드-래그) — **사전등록본**.

지금까지 잰 "페어 트레이딩"은 두 종목이 **동시에** 얼마나 벌어졌는가를 봤다.
안 재본 것은 "대형코인이 방금 움직였을 때 소형코인이 몇 분 뒤 따라가는가"다.
이건 동시 스프레드가 아니라 **시차를 둔 전파**를 보는 것이라 구조가 다르다.

가장 중요한 함정: BTC 수익률로 다른 코인을 예측하는 신호는, BTC 가 시장
전체와 상관이 높다면 **그냥 베타를 다시 재는 것**일 수 있다. 그래서 반드시
드리프트 중립화(횡단면 평균 차감)를 거친다 — edge_lab 과 동일한 자를 쓴다.

────────────────────────────────────────────────────────────────────────
사전 등록 (실행 전 고정)
────────────────────────────────────────────────────────────────────────
  1) 리더: **BTC, ETH 둘 다** 등록한다(하나만 고르면 사후 선택).
  2) 리더의 과거 수익률(lag): 최근 1분/3분/5분 **셋 다** 낸다.
     신호 = sign(리더의 과거 L분 수익률). 팔로워(BTC/ETH 제외 79개)에
     **전부 동일하게** 적용한다(개별 종목별로 다른 리더를 고르지 않는다).
  3) 팔로워의 순방향 수익률은 edge_lab.forward() 로 계산하고 **드리프트
     중립화**(횡단면 평균 차감, BTC/ETH 도 그 평균에 포함된 채로 — 리더 자신을
     빼는 특별취급을 하지 않는다. 79/81 이라 영향은 작다)한다.
  4) 지평(스캘핑 시간대): 5분/15분. stride=지평.
  5) 판정은 CLAUDE.md 원칙 0 보강 2 세 조건 + 이중 비용선 + 3구간.
  6) 결과를 보고 리더나 lag 값을 바꾸지 않는다.

측정 못하는 것: 리더 자신의 그 시점 수익률을 신호로 쓰므로, 진짜 리드-래그가
아니라 "리더가 막 움직인 방향으로 전체 시장이 계속 간다"는 **시장 모멘텀의
변형**일 수도 있다. 드리프트 중립화가 이를 어느 정도 걸러내지만 완전하지
않다 — 리더 신호와 팔로워 반응이 진짜 인과인지, 공통 요인의 동시 반영인지는
이 측정만으로 확정할 수 없다.
"""
import argparse
import pathlib
import sys

import numpy as np

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from edge_lab import FEE_MAKER_RT, FEE_TAKER_RT, forward, load_panel, stats  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cache", default="scratch_edge_1m_60d.npz")
    ap.add_argument("--interval", default="1m")
    ap.add_argument("--horizons", default="5,15")
    ap.add_argument("--lags", default="1,3,5")
    ap.add_argument("--split", type=int, default=3)
    a = ap.parse_args()

    P = load_panel([a.cache], a.interval)
    T, S = P["c"].shape
    syms = P["syms"]
    print(f"패널: {S}심볼 x {T}봉 ({a.interval})")

    leaders = [s for s in ("BTCUSDT", "ETHUSDT") if s in syms]
    print(f"리더: {leaders}")
    C = P["c"]
    ret1 = np.full(T, np.nan)

    hor = [int(x) for x in a.horizons.split(",")]
    lags = [int(x) for x in a.lags.split(",")]
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

    for leader in leaders:
        li = syms.index(leader)
        lc = C[:, li]
        for lag in lags:
            lag_ret = np.full(T, np.nan)
            lag_ret[lag:] = (lc[lag:] / lc[:-lag] - 1)
            sig_l = lag_ret > 0
            sig_s = lag_ret < 0
            # 팔로워 전체(리더 포함 81개 컬럼)에 동일 신호를 적용한다. 리더 자신도
            # 포함(제외해도 79/81 이라 결과 차이는 미미하고, 제외 로직을 더하면
            # 그 자체가 사후 선택 여지가 된다).
            sg = np.zeros((T, S), dtype=np.int8)
            sg[sig_l, :] = 1
            sg[sig_s, :] = -1
            print(f"=== 리더={leader} lag={lag}분 ===")
            for h in hor:
                r = phase_stats(sg, FN[h], h)
                if r is None:
                    print(f"  지평{h}분  표본부족")
                    continue
                n, m, med, tt, ts = r
                days = T / 1440.0
                print(f"  지평{h}분  n={n:8d}  건/심볼일{n / S / days:7.1f}  "
                      f"건당{m:+8.4f}|심볼중앙{med:+8.4f}  t{tt:+6.1f}/{ts:+5.1f}")
            print()

    # 국면 3등분: BTC lag=3분, 지평 5분 하나만(사전등록 - 대표 조합)
    if a.split > 1:
        li = syms.index(leaders[0])
        lc = C[:, li]
        lag = 3 if 3 in lags else lags[0]
        lag_ret = np.full(T, np.nan)
        lag_ret[lag:] = (lc[lag:] / lc[:-lag] - 1)
        sg = np.zeros((T, S), dtype=np.int8)
        sg[lag_ret > 0, :] = 1
        sg[lag_ret < 0, :] = -1
        h0 = hor[0]
        step = T // a.split
        print(f"=== 국면 3등분 (리더={leaders[0]} lag={lag}분, 지평{h0}분) ===")
        for k in range(a.split):
            sl = slice(k * step, (k + 1) * step if k < a.split - 1 else T)
            r = phase_stats(sg[sl], FN[h0][sl], h0)
            if r is None:
                continue
            n, m, med, tt, ts = r
            print(f"  구간{k + 1}/{a.split}  건당{m:+8.4f}|심볼중앙{med:+8.4f} "
                  f"t{tt:+6.1f}/{ts:+5.1f}")
    print()
    print("판정: 평균·중앙값 둘 다 비용선 초과 + 두 t 모두 >=2 + 3구간 동일부호.")


if __name__ == "__main__":
    main()
