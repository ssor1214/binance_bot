"""호가·주문흐름 축 전용 측정 하네스 — **사전등록본**.

`edge_lab.py` 는 봉 패널용이라 5초 스냅샷을 받지 못한다. 이 스크립트가 그 자리를 맡되
**자(尺)는 edge_lab 과 똑같이 쓴다** — 드리프트 중립 + 이중 클러스터 t + 심볼중앙값 +
stride + 이중 비용선. 실제로 `edge_lab.stats` 를 그대로 임포트해 쓴다. 자를 두 개 두면
비교가 안 되기 때문이다.

────────────────────────────────────────────────────────────────────────
사전 등록 (2026-09-01 작성. 데이터는 이 시점에 8.7시간뿐이고 아무것도 측정하지 않았다)
────────────────────────────────────────────────────────────────────────
2026-08-31~09-01 사이 여덟 개 후보를 기각하면서 매번 같은 함정에 걸렸다: **결과를 본 뒤
규칙을 고르는 것**. 위상 스윕(792034c), 12장 후보, 15분 조건부 통과 4건이 전부 그랬다.
호가 축은 마지막 남은 축이므로 그 함정을 원천 차단한다. **아래를 지금 못박고, 데이터가
쌓인 뒤에 규칙을 추가하거나 바꾸지 않는다.**

  1) 신호는 아래 8개뿐이다. 결과를 보고 더 붙이지 않는다.
       OBI1      최우선호가 불균형   (bid_qty-ask_qty)/(bid_qty+ask_qty)
       OBI5      5호가 누적 불균형   (d5_bid-d5_ask)/(d5_bid+d5_ask)
       TFI       체결흐름 불균형     (buy_vol-sell_vol)/(buy_vol+sell_vol)
       OBI5+TFI 동의 / OBI5 극단(횡단면 10%) / TFI 극단 / 그리고 OBI5·TFI 의 역방향 2개
     역방향을 넣는 이유: 마이크로구조는 평균회귀하는 경우가 흔해 부호를 미리 단정할
     수 없다. 순방향만 등록하면 음수가 나왔을 때 "뒤집으면 되잖아"가 사후 선택이 된다.
  2) 지평은 5초 버킷 기준 12(1분) / 60(5분) / 180(15분=라이브 보유) / 720(60분) 넷이다.
  3) 판정은 CLAUDE.md 원칙 0 보강 2 세 조건 전부 + 비용선.
       조건1 건당평균과 심볼중앙값이 **둘 다** 비용선 초과
       조건2 시각클러스터 t 와 심볼클러스터 t 가 **둘 다** 2 이상
       조건3 표본 3등분에서 **전 구간 같은 부호**  (--split 3)
     비용선은 edge_lab 과 같다: 보수(taker) 0.10+슬립x2 / e3(maker) 0.04.
  4) **stride 는 보유 버킷수와 같게 둔다.** 5초 간격 표본에서 15분 보유는 180겹으로
     겹치므로 stride 없이 재면 t 가 통째로 거짓이 된다.
  5) 표본이 최소 **한 달 + 세 국면**을 덮기 전에는 판정하지 않는다(CLAUDE.md).
     그 전 실행은 전부 배관 점검용이며, 그 결과로 규칙을 고르지 않는다.

────────────────────────────────────────────────────────────────────────
데이터 주의
────────────────────────────────────────────────────────────────────────
* **`last_price` 를 쓰지 않는다.** 그 5초 창에 체결이 없으면 0 이 적히는데(실측 13.8%)
  이걸 가격으로 쓰면 -100% 수익률이 섞여 표본이 통째로 오염된다.
  가격은 항상 **중간가 `(bid+ask)/2`** 로 쓴다 — bid/ask 는 결측이 0건이다.
* 수익률 규약은 edge_lab 과 맞춘다: 신호는 버킷 i 로 확정, **진입은 i+1 의 중간가**,
  청산은 **i+1+h 의 중간가**. 같은 버킷 진입은 lookahead 다.
"""
import argparse
import collections
import csv
import gzip
import pathlib
import sys

import numpy as np

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from edge_lab import FEE_MAKER_RT, FEE_TAKER_RT, stats  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parent.parent
OBOOK = ROOT / "logs" / "obook"


def load(paths, bucket_sec, min_cover=0.5):
    """obook CSV(.gz 포함)를 T x S 패널로 읽는다.

    5초 원본을 bucket_sec 로 묶는다(합계는 체결량, 마지막값은 호가). 메모리를 위해
    float32 를 쓴다 — 3개월 5초면 1.5M x 83 이라 float64 로는 안 들어간다.
    """
    rows = collections.defaultdict(dict)   # bucket -> sym -> tuple
    syms = set()
    for p in paths:
        op = gzip.open if str(p).endswith(".gz") else open
        with op(p, "rt", encoding="utf-8", newline="") as fh:
            for r in csv.DictReader(fh):
                b = int(r["ts_ms"]) // 1000 // bucket_sec
                s = r["symbol"]
                syms.add(s)
                bid, ask = float(r["bid"]), float(r["ask"])
                if bid <= 0 or ask <= 0 or bid >= ask:
                    continue          # 결측/크로스는 버린다(실측 0건이지만 방어)
                cur = rows[b].get(s)
                bv, sv = float(r["buy_vol"]), float(r["sell_vol"])
                if cur is None:
                    rows[b][s] = [bid, float(r["bid_qty"]), ask, float(r["ask_qty"]),
                                  bv, sv, float(r["d5_bid"]), float(r["d5_ask"])]
                else:
                    # 호가는 버킷의 **마지막** 값, 체결량은 **합계**
                    cur[0], cur[1], cur[2], cur[3] = bid, float(r["bid_qty"]), ask, float(r["ask_qty"])
                    cur[4] += bv
                    cur[5] += sv
                    cur[6], cur[7] = float(r["d5_bid"]), float(r["d5_ask"])

    if not rows:
        raise SystemExit("[중단] 읽은 행이 없다")
    bs = sorted(rows)
    b0, b1 = bs[0], bs[-1]
    T = b1 - b0 + 1
    sl = sorted(syms)
    idx = {s: j for j, s in enumerate(sl)}
    F = {k: np.full((T, len(sl)), np.nan, dtype=np.float32)
         for k in ("bid", "bq", "ask", "aq", "bv", "sv", "d5b", "d5a")}
    keys = ("bid", "bq", "ask", "aq", "bv", "sv", "d5b", "d5a")
    for b, d in rows.items():
        i = b - b0
        for s, v in d.items():
            j = idx[s]
            for k, x in zip(keys, v):
                F[k][i, j] = x
    # 커버리지가 낮은 심볼은 뺀다(상장 중간 편입 등)
    cover = np.mean(~np.isnan(F["bid"]), axis=0)
    keep = cover >= min_cover
    if keep.sum() < 2:
        raise SystemExit("[중단] 커버리지를 넘긴 심볼이 2개 미만")
    for k in F:
        F[k] = F[k][:, keep]
    sl = [s for s, k in zip(sl, keep) if k]
    return F, sl, T, b0


def features(F):
    """사전등록한 원시 피처. 여기서 끝이고 더 만들지 않는다."""
    def imb(a, b):
        d = a + b
        with np.errstate(invalid="ignore", divide="ignore"):
            return np.where(d > 0, (a - b) / d, np.nan)
    return {
        "OBI1": imb(F["bq"], F["aq"]),
        "OBI5": imb(F["d5b"], F["d5a"]),
        "TFI": imb(F["bv"], F["sv"]),
    }


def signals(X):
    """사전등록 8개. **이 목록은 고정이다.**"""
    S = {}

    def put(name, lm, sm, valid):
        a = np.zeros(lm.shape, dtype=np.int8)
        a[lm & valid] = 1
        a[sm & valid] = -1
        S[name] = a

    def xrank(v):
        """같은 시각 심볼 간 백분위(0~1). 횡단면 극단을 잡을 때 쓴다."""
        out = np.full(v.shape, np.nan, dtype=np.float32)
        for i in range(v.shape[0]):
            row = v[i]
            ok = ~np.isnan(row)
            n = int(ok.sum())
            if n >= 10:
                r = np.argsort(np.argsort(row[ok]))
                out[i, ok] = r / max(n - 1, 1)
        return out

    for k in ("OBI1", "OBI5", "TFI"):
        v = X[k]
        ok = ~np.isnan(v)
        put(f"{k} 순방향", v > 0, v < 0, ok)
    for k in ("OBI5", "TFI"):
        v = X[k]
        ok = ~np.isnan(v)
        put(f"{k} 역방향", v < 0, v > 0, ok)

    o, t = X["OBI5"], X["TFI"]
    ok = ~np.isnan(o) & ~np.isnan(t)
    put("OBI5+TFI 동의", (o > 0) & (t > 0), (o < 0) & (t < 0), ok)

    for k in ("OBI5", "TFI"):
        r = xrank(X[k])
        ok = ~np.isnan(r)
        put(f"{k} 극단 10%", r >= 0.9, r <= 0.1, ok)
    return S


def forward(F, hor):
    """진입 = 다음 버킷 중간가 / 청산 = h버킷 뒤 중간가. last_price 는 쓰지 않는다."""
    mid = (F["bid"] + F["ask"]) / 2.0
    T = mid.shape[0]
    out = {}
    for h in hor:
        e = np.full(mid.shape, np.nan, dtype=np.float32)
        x = np.full(mid.shape, np.nan, dtype=np.float32)
        if T - 1 - h > 0:
            e[:T - 1 - h] = mid[1:T - h]
            x[:T - 1 - h] = mid[1 + h:]
        with np.errstate(invalid="ignore", divide="ignore"):
            out[h] = (x / e - 1.0) * 100.0
    return out


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--glob", default="obook-*.csv*", help="logs/obook/ 안의 대상 파일")
    p.add_argument("--bucket-sec", type=int, default=5,
                   help="집계 버킷(초). 원본 5초. 장기 표본은 60 으로 올려 메모리를 줄인다")
    p.add_argument("--horizons", default="",
                   help="버킷 단위 지평. 비우면 1/5/15/60분에 해당하는 값을 자동 계산")
    p.add_argument("--stride", type=int, default=0,
                   help="0 이면 첫 지평과 같게 자동(겹침 제거). 사전등록: 보유 버킷수와 같게")
    p.add_argument("--split", type=int, default=0, help="표본 N등분 국면 검증(사전등록 3)")
    p.add_argument("--slip", type=float, default=0.02, help="편도 슬리피지 가정 %%")
    p.add_argument("--min-cover", type=float, default=0.5)
    a = p.parse_args()

    paths = sorted(OBOOK.glob(a.glob))
    if not paths:
        raise SystemExit(f"[중단] {OBOOK}/{a.glob} 에 파일이 없다")
    F, syms, T, b0 = load(paths, a.bucket_sec, a.min_cover)
    span_h = T * a.bucket_sec / 3600.0
    print(f"패널: {len(syms)}심볼 x {T}버킷 ({a.bucket_sec}초, 약 {span_h:.1f}시간 "
          f"= {span_h / 24:.2f}일)  파일 {len(paths)}개")

    if a.horizons:
        hor = [int(x) for x in a.horizons.split(",") if x]
    else:
        hor = sorted({max(1, int(m * 60 / a.bucket_sec)) for m in (1, 5, 15, 60)})
    stride = a.stride or hor[0]

    X = features(F)
    SIG = signals(X)
    FW = forward(F, hor)
    with np.errstate(invalid="ignore"):
        FN = {h: FW[h] - np.nanmean(FW[h], axis=1, keepdims=True) for h in hor}

    if stride > 1:
        # 5초 표본에서 15분 보유는 180겹으로 겹친다. 위상 자유도를 피하려고
        # 한 위상만 보지 않고 **전 위상 평균**으로 판정한다(792034c 의 교훈).
        print(f"(stride={stride}: 전 위상 {stride}개를 모두 돌려 평균으로 판정)")
    print()

    line_t = FEE_TAKER_RT + 2 * a.slip
    line_m = FEE_MAKER_RT

    def cell(sg, fn):
        rs = []
        for off in range(stride):
            if stride > 1:
                keep = np.zeros(sg.shape[0], dtype=bool)
                keep[off::stride] = True
                s2 = np.where(keep[:, None], sg, 0).astype(np.int8)
            else:
                s2 = sg
            r = stats(s2, fn)
            if r:
                rs.append(r)
        if not rs:
            return None
        return (int(np.mean([r[0] for r in rs])), float(np.mean([r[1] for r in rs])),
                float(np.nanmean([r[2] for r in rs])), float(np.mean([r[3] for r in rs])),
                float(np.mean([r[4] for r in rs])))

    def table(title, FF, sl=None):
        print(f"[{title}] 진입=다음버킷 중간가 / 청산=h버킷 뒤 중간가, 단위 %(건당)")
        head = "".join(f"{h * a.bucket_sec / 60:g}분".rjust(30) for h in hor)
        print(f"{'신호':<22}{'건수':>10}{head}")
        for name, sg in SIG.items():
            sgx = sg if sl is None else sg[sl]
            cs, n0 = [], 0
            for h in hor:
                fn = FF[h] if sl is None else FF[h][sl]
                r = cell(sgx, fn)
                if r is None:
                    cs.append("-")
                    continue
                n0 = r[0]
                cs.append(f"{r[1]:+.4f}|{r[2]:+.4f} (t{r[3]:+.1f}/{r[4]:+.1f})")
            print(f"{name:<22}{n0:>10}" + "".join(c.rjust(30) for c in cs))
        print()

    table("드리프트 중립", FN)
    if a.split > 1:
        step = T // a.split
        for k in range(a.split):
            table(f"드리프트 중립 — 구간 {k + 1}/{a.split}", FN,
                  slice(k * step, (k + 1) * step if k < a.split - 1 else T))

    print(f"비용선: 보수(taker/taker) {line_t:.3f}% / e3(maker/maker) {line_m:.3f}%.")
    print("표기 = 건당평균|심볼중앙값 (시각클러스터 t / 심볼클러스터 t).")
    print("판정(사전등록): 평균·중앙값이 **둘 다** 비용선 초과 + 두 t **모두** >=2 "
          "+ --split 3 에서 전 구간 동일 부호. 셋 다여야 후보다.")
    if span_h < 24 * 30:
        print()
        print(f"⚠ 표본이 {span_h / 24:.2f}일뿐이다. CLAUDE.md 는 최소 한 달 + 세 국면을 "
              f"요구한다. **이 실행은 배관 점검용이며 판정 근거가 아니다.**")


if __name__ == "__main__":
    main()
