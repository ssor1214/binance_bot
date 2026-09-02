"""볼밴 역추세 경로 시뮬레이션 — **사전등록본**.

`edge_lab.py` 는 **고정시간 강제청산**으로 진입 엣지만 잰다(청산규칙을 넣으면 승률이
청산규칙과 동어반복이 되기 때문 — CLAUDE.md 판정 참고). 그래서 e6 처럼
**밴드 지정가 진입 + 중심선 지정가 익절 + 조건부 손절** 구조의 기대값은 잴 수 없다.

이 스크립트가 그 자리를 맡는다. 밴드에 닿은 뒤 **중심선(TP)에 먼저 가는가,
손절선에 먼저 가는가**를 봉 경로로 경주시킨다.

배경(2026-09-02): e6 가 CM 을 완전히 제거하고 볼밴 역추세 + 밴드 지정가로 전환했다.
측정상 근거가 있다 — CM 은 1분봉 60일 92만 건에서 `-0.0076`(t `-6.6`/`-5.7`)로
유의하게 음수였고, 볼밴 단순터치 역추세는 `+0.0127`(t `+3.3`/`+3.5`)로 유일하게
유의한 양수였다. 그리고 밴드->중심선 거리가 **중앙 0.292%로 maker 비용선의 7배**다.

────────────────────────────────────────────────────────────────────────
사전 등록 (실행 전 고정)
────────────────────────────────────────────────────────────────────────
  1) 진입: 종가가 밴드를 접촉/이탈한 봉에서 **밴드 가격**에 체결된 것으로 본다.
     (지정가가 밴드에 걸려 있었다면 그 가격에 체결됐을 것이다.)
     **이건 낙관 가정이다** — 실제로는 미체결·부분체결이 있다. 결과와 함께 읽을 것.
  2) TP: **진입 시점의 중심선**(bbm). 진입 후 갱신하지 않는다(e6 가 진입 시
     reduceOnly 지정가를 거는 구조이므로).
  3) SL: 가격 기준 고정폭과 **변동성 스케일(k x 밴드폭)** 둘 다 본다.
     한쪽만 보면 사후 선택이 된다.
  4) 같은 봉에서 TP·SL 이 모두 닿으면 **SL 먼저**로 본다(보수적).
  5) 최대 보유 `--max-bars` 를 넘기면 시장가 청산으로 본다.
  6) 비용: TP 청산 = maker 왕복 `0.04%` / SL 청산 = maker 진입 + taker 손절 `0.07%`
     / 시간초과 = maker 진입 + taker 청산 `0.07%`. `--fee-*` 로 노출한다.
  7) 판정: **건당 순기대값이 양수**이고, 롱/숏이 **둘 다** 양수이며(드리프트 배제),
     3구간에서 **부호가 유지**될 것.
  8) 결과를 보고 SL 값이나 비용 가정을 바꾸지 않는다. 전 조합을 그대로 보고한다.

측정하지 못하는 것: 실제 체결률(밴드에 걸어도 안 닿으면 거래 없음) / 부분체결 /
역선택(체결된 건이 계속 가는 쪽에 몰리는 효과) / 봉 내부 경로(고가·저가 순서).
**특히 (1)의 낙관 가정 때문에 이 결과는 상한으로 읽어야 한다.**
"""
import argparse
import pathlib
import sys

import numpy as np

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from edge_lab import build_indicators, load_panel  # noqa: E402


def race(P, I, sl_mode, sl_val, max_bars, stride, f1_lo, f1_hi, sl_min=0.0,
         tp_frac=1.0):
    """밴드 접촉 후 TP/SL 경주. -> (롱결과, 숏결과) 각각 dict."""
    C, H, L = P["c"], P["h"], P["l"]
    bbu, bbl, bbm = I["bbu"], I["bbl"], I["bbm"]
    T, S = C.shape
    bw = (bbu - bbl) / np.where(bbm > 0, bbm, np.nan)

    # F1 = 밴드폭의 심볼별 1,440봉 rolling 백분위 (e6 와 같은 정의)
    use_f1 = not (f1_lo <= 0.0 and f1_hi >= 1.0)
    f1q = np.full(bw.shape, np.nan)
    if use_f1:
        for j in range(S):
            v = bw[:, j]
            for i in range(1440, T):
                w = v[i - 1440:i]
                ok = ~np.isnan(w)
                if ok.sum() > 500 and not np.isnan(v[i]):
                    f1q[i, j] = (w[ok] < v[i]).mean()

    out = {"LONG": [], "SHORT": []}
    for side in ("LONG", "SHORT"):
        for j in range(S):
            for i in range(1440, T - 1, stride):
                if use_f1:
                    q = f1q[i, j]
                    if np.isnan(q) or not (f1_lo <= q <= f1_hi):
                        continue
                b_u, b_l, b_m = bbu[i, j], bbl[i, j], bbm[i, j]
                if np.isnan(b_u) or np.isnan(b_m) or np.isnan(C[i, j]):
                    continue
                # tp_frac: 밴드 -> 중심선 거리의 몇 %를 목표로 하는가.
                # 1.0 = 중심선(e6 현행). 낮출수록 도달률이 오르고 건당 이익은 준다.
                if side == "LONG":
                    if C[i, j] > b_l:
                        continue
                    ent = b_l
                    tp = b_l + (b_m - b_l) * tp_frac
                else:
                    if C[i, j] < b_u:
                        continue
                    ent = b_u
                    tp = b_u - (b_u - b_m) * tp_frac
                if ent <= 0:
                    continue
                # 손절폭
                if sl_mode == "fixed":
                    d = sl_val / 100.0
                else:                       # 'bw' : k x 밴드폭
                    if np.isnan(bw[i, j]):
                        continue
                    d = max(sl_val * bw[i, j], sl_min / 100.0)
                sl = ent * (1 - d) if side == "LONG" else ent * (1 + d)
                tp_pct = (tp / ent - 1) * 100 * (1 if side == "LONG" else -1)
                if tp_pct <= 0:
                    continue
                res = None
                for k in range(i + 1, min(i + 1 + max_bars, T)):
                    hi, lo = H[k, j], L[k, j]
                    if np.isnan(hi) or np.isnan(lo):
                        continue
                    if side == "LONG":
                        hit_sl, hit_tp = lo <= sl, hi >= tp
                    else:
                        hit_sl, hit_tp = hi >= sl, lo <= tp
                    if hit_sl:              # 동시 발생 시 SL 우선(보수적)
                        res = ("SL", -d * 100)
                        break
                    if hit_tp:
                        res = ("TP", tp_pct)
                        break
                if res is None:
                    k = min(i + max_bars, T - 1)
                    if np.isnan(C[k, j]):
                        continue
                    r = (C[k, j] / ent - 1) * 100 * (1 if side == "LONG" else -1)
                    res = ("TO", r)
                out[side].append(res)
    return out


def summarize(rows, fee_tp, fee_sl, fee_to):
    if not rows:
        return None
    n = len(rows)
    tp = [r for r in rows if r[0] == "TP"]
    sl = [r for r in rows if r[0] == "SL"]
    to = [r for r in rows if r[0] == "TO"]
    gross = sum(r[1] for r in rows) / n
    net = (sum(r[1] - fee_tp for r in tp)
           + sum(r[1] - fee_sl for r in sl)
           + sum(r[1] - fee_to for r in to)) / n
    return dict(n=n, tp=len(tp) / n * 100, sl=len(sl) / n * 100, to=len(to) / n * 100,
                gross=gross, net=net,
                tp_avg=(np.mean([r[1] for r in tp]) if tp else 0.0),
                sl_avg=(np.mean([r[1] for r in sl]) if sl else 0.0))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cache", default="scratch_edge_1m_60d.npz")
    ap.add_argument("--interval", default="1m")
    ap.add_argument("--max-bars", type=int, default=60)
    ap.add_argument("--stride", type=int, default=15)
    ap.add_argument("--f1-lo", type=float, default=0.80, help="e6 기본 80~100%%")
    ap.add_argument("--f1-hi", type=float, default=1.00)
    ap.add_argument("--fee-tp", type=float, default=0.04)
    ap.add_argument("--fee-sl", type=float, default=0.07)
    ap.add_argument("--fee-to", type=float, default=0.07)
    ap.add_argument("--tp-fracs", default="0.25,0.4,0.6,0.8,1.0",
                   help="밴드->중심선 거리의 목표 비율. **전부 보고한다** — "
                        "하나를 고르면 그게 사후 선택이다.")
    ap.add_argument("--sl-list", default="0.3,0.5",
                   help="고정 손절폭(%%). 전부 보고한다.")
    ap.add_argument("--split", type=int, default=3)
    a = ap.parse_args()

    P = load_panel([a.cache], a.interval)
    I = build_indicators(P)
    T, S = P["c"].shape
    print(f"패널: {S}심볼 x {T}봉 ({a.interval})")
    print(f"F1 {a.f1_lo:.0%}~{a.f1_hi:.0%} / 최대보유 {a.max_bars}봉 / stride {a.stride}")
    print(f"비용: TP청산 {a.fee_tp}% / SL청산 {a.fee_sl}% / 시간초과 {a.fee_to}%")
    print()
    print(f"{'손절설정':<20}{'방향':>6}{'건수':>8}{'TP%':>7}{'SL%':>7}{'초과%':>7}"
          f"{'TP평균':>9}{'SL평균':>9}{'건당순':>10}")
    fracs = [float(x) for x in a.tp_fracs.split(",")]
    combos = [("fixed", v, f"고정 {v}%") for v in
              [float(x) for x in a.sl_list.split(",")]]
    for frac in fracs:
        print(f"--- TP = 밴드->중심선의 {frac:.0%} ---")
        for mode, val, lbl in combos:
            r = race(P, I, mode, val, a.max_bars, a.stride, a.f1_lo, a.f1_hi,
                     tp_frac=frac)
            for side in ("LONG", "SHORT"):
                sm = summarize(r[side], a.fee_tp, a.fee_sl, a.fee_to)
                if sm is None:
                    continue
                print(f"{lbl:<20}{side:>6}{sm['n']:>8}{sm['tp']:>7.1f}"
                      f"{sm['sl']:>7.1f}{sm['to']:>7.1f}{sm['tp_avg']:>+9.3f}"
                      f"{sm['sl_avg']:>+9.3f}{sm['net']:>+10.4f}")
            st = summarize(r["LONG"] + r["SHORT"], a.fee_tp, a.fee_sl, a.fee_to)
            if st:
                print(f"{'':<20}{'합계':>6}{st['n']:>8}{st['tp']:>7.1f}"
                      f"{st['sl']:>7.1f}{st['to']:>7.1f}{st['tp_avg']:>+9.3f}"
                      f"{st['sl_avg']:>+9.3f}{st['net']:>+10.4f}")
        print()
    print("⚠ 진입은 '밴드에 닿으면 그 가격에 체결'로 가정했다. 실제 미체결·부분체결·")
    print("  역선택이 빠져 있으므로 **이 결과는 상한**이다.")
    print("⚠ 드리프트 중립화를 하지 않았다(경로 시뮬이라). 롱/숏이 둘 다 양수인지로 확인할 것.")


if __name__ == "__main__":
    main()
