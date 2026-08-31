"""방향 신호 후보들의 '순수 방향성 엣지'를 같은 자로 비교한다.

cm_signal_edge.py 의 확장. 세 가지가 다르다.

  1) **CM 말고 다른 뼈대도 잰다.** CLAUDE.md 원칙 0 에 적힌 메인봇 뼈대
     (볼밴+EMA+RSI)와 평균회귀/횡단면 계열까지 같은 표본·같은 자로 비교한다.
  2) **t값을 시각 클러스터로 보정한다.** 같은 시각 84심볼은 서로 독립이 아니고
     (시장 전체가 같이 움직인다), 겹치는 보유구간도 독립이 아니다. 순진한 t는
     그래서 부풀려진다. 시각별로 평균을 낸 뒤 시각 축에서 t를 다시 계산한다.
     **판정은 이 보정 t로 한다.**
  3) npz 패널(T x S 행렬)로 계산해 30일 표본도 수초에 끝난다.

측정 규약 (lookahead 없음):
    신호는 봉 i 마감으로 확정 -> 진입은 봉 i+1 **시가** -> 봉 i+1+h **종가**로 청산.
    청산규칙·슬롯·수수료는 넣지 않는다. 청산규칙을 넣으면 승률이 청산규칙과
    동어반복이 되기 때문이다(CLAUDE.md 판정 참고).

비용선: 왕복 0.11% (수수료 0.07 + 슬리피지 가정 0.04). 이 선을 못 넘는 신호는
진입축을 아무리 튜닝해도 돈을 벌 수 없다.
"""
import argparse
import math
import pathlib
import sys

import numpy as np

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
ROOT = pathlib.Path(__file__).resolve().parent.parent
COST_PCT = 0.11


# ---------------------------------------------------------------- 지표 (열 단위)
def wma(a, length):
    """scalp_bot_e3._series_wma 와 동일한 부분창 워밍업 의미를 numpy 로."""
    n = len(a)
    w = np.arange(1.0, length + 1.0)
    out = np.full(n, np.nan)
    if n == 0:
        return out
    if n >= length:
        out[length - 1:] = np.convolve(a, w[::-1] / w.sum(), mode="valid")
    for i in range(min(length - 1, n)):
        k = i + 1
        lw = w[-k:]
        out[i] = float(np.dot(a[:k], lw) / lw.sum())
    return out


def hma(a, length):
    half = max(1, int(length / 2))
    root = max(1, int(round(math.sqrt(length))))
    return wma(2.0 * wma(a, half) - wma(a, length), root)


def ema(a, span):
    al = 2.0 / (span + 1.0)
    out = np.empty(len(a), dtype=np.float64)
    p = a[0]
    for i in range(len(a)):
        p = al * a[i] + (1 - al) * p
        out[i] = p
    out[0] = a[0]
    return out


def rsi_wilder(a, period=14):
    n = len(a)
    if n <= period + 1:
        return np.full(n, np.nan)
    d = np.diff(a, prepend=a[0])
    up = np.clip(d, 0, None)
    dn = np.clip(-d, 0, None)
    au = np.full(n, np.nan)
    ad = np.full(n, np.nan)
    au[period] = up[1:period + 1].mean()
    ad[period] = dn[1:period + 1].mean()
    for i in range(period + 1, n):
        au[i] = (au[i - 1] * (period - 1) + up[i]) / period
        ad[i] = (ad[i - 1] * (period - 1) + dn[i]) / period
    with np.errstate(divide="ignore", invalid="ignore"):
        rs = np.where(ad > 0, au / ad, np.inf)
    return 100.0 - 100.0 / (1.0 + rs)


def roll_mean_std(a, w):
    n = len(a)
    m = np.full(n, np.nan)
    s = np.full(n, np.nan)
    if n < w:
        return m, s
    c1 = np.cumsum(np.insert(a, 0, 0.0))
    c2 = np.cumsum(np.insert(a * a, 0, 0.0))
    mu = (c1[w:] - c1[:-w]) / w
    var = np.maximum((c2[w:] - c2[:-w]) / w - mu * mu, 0.0)
    m[w - 1:] = mu
    s[w - 1:] = np.sqrt(var)
    return m, s


# ---------------------------------------------------------------- 패널 적재
def load_panel(path, interval):
    z = np.load(ROOT / path, allow_pickle=True)
    syms = [str(x) for x in z["__symbols__"]]
    tset = set()
    for s in syms:
        tset |= set(z[f"{s}|{interval}"][:, 0].astype(np.int64).tolist())
    times = sorted(tset)
    T, S = len(times), len(syms)
    idx = {t: i for i, t in enumerate(times)}
    P = {k: np.full((T, S), np.nan) for k in "ohlcv"}
    for j, s in enumerate(syms):
        b = z[f"{s}|{interval}"]
        rows = np.array([idx[t] for t in b[:, 0].astype(np.int64).tolist()])
        for k, col in zip("ohlcv", range(1, 6)):
            P[k][rows, j] = b[:, col]
    P["t"] = np.asarray(times, dtype=np.int64)
    P["syms"] = syms
    P["h4"] = {s: z[f"{s}|4h"] for s in syms}
    return P


def build_indicators(P):
    T, S = P["c"].shape
    keys = ("hma", "hma_up", "ema20", "ema50", "rsi", "bbu", "bbl", "bbm", "htf")
    ind = {k: np.full((T, S), np.nan) for k in keys}
    for j, s in enumerate(P["syms"]):
        c = P["c"][:, j]
        ok = ~np.isnan(c)
        if ok.sum() < 300:
            continue
        # 결측(상장 전/거래정지)은 직전값으로 채워 지표 연속성만 유지하고,
        # 신호는 아래에서 원래 결측 위치를 다시 가린다.
        cf = c.copy()
        first = int(np.argmax(ok))
        cf[:first] = c[first]
        for i in range(first + 1, T):
            if np.isnan(cf[i]):
                cf[i] = cf[i - 1]
        h = hma(cf, 20)
        ind["hma"][:, j] = h
        up = np.full(T, np.nan)
        up[2:] = (h[2:] >= h[:-2]).astype(float)
        ind["hma_up"][:, j] = up
        ind["ema20"][:, j] = ema(cf, 20)
        ind["ema50"][:, j] = ema(cf, 50)
        ind["rsi"][:, j] = rsi_wilder(cf, 14)
        m, sd = roll_mean_std(cf, 20)
        ind["bbm"][:, j] = m
        ind["bbu"][:, j] = m + 2 * sd
        ind["bbl"][:, j] = m - 2 * sd
        # 4시간 EMA200: **마감된** 상위봉만 쓴다 (lookahead 없음)
        b4 = P["h4"][s]
        t4 = b4[:, 0].astype(np.int64)
        e4 = ema(b4[:, 4], 200)
        close4 = t4 + 4 * 3600_000          # 상위봉 마감 시각
        pos = np.searchsorted(close4, P["t"], side="right") - 1
        good = pos >= 200
        val = np.where(good, e4[np.clip(pos, 0, len(e4) - 1)], np.nan)
        ind["htf"][:, j] = (cf > val).astype(float)
        ind["htf"][np.isnan(val), j] = np.nan
        for k in keys:
            ind[k][~ok, j] = np.nan
    return ind


# ---------------------------------------------------------------- 신호 정의
def signals(P, I):
    C, H, L = P["c"], P["h"], P["l"]

    def prev(a):
        r = np.roll(a, 1, axis=0)
        r[0] = np.nan
        return r

    prevC, prevH, prevL = prev(C), prev(H), prev(L)
    prevU, prevLo = prev(I["bbu"]), prev(I["bbl"])
    up = I["hma_up"] == 1
    dn = I["hma_up"] == 0
    valid = ~np.isnan(I["hma"]) & ~np.isnan(C)

    S = {}

    def put(name, longm, shortm):
        a = np.zeros(C.shape, dtype=np.int8)
        a[longm & valid] = 1
        a[shortm & valid] = -1
        S[name] = a

    # --- CM 계열 (현행 e3) -------------------------------------------------
    cm_l, cm_s = up & (C > I["hma"]), dn & (C < I["hma"])
    put("CM 전체 [현행뼈대]", cm_l, cm_s)
    htf_up, htf_dn = I["htf"] == 1, I["htf"] == 0
    put("CM + 4h정합", cm_l & htf_up, cm_s & htf_dn)

    # --- 메인봇 뼈대: 볼밴 돌파 + EMA추세 + RSI (CLAUDE.md 원칙 0 참조) ------
    trend_l = I["ema20"] > I["ema50"]
    trend_s = I["ema20"] < I["ema50"]
    brk_l = (prevC <= prevU) & (C >= I["bbu"]) & (C > prevH)
    brk_s = (prevC >= prevLo) & (C <= I["bbl"]) & (C < prevL)
    rsi_l, rsi_s = I["rsi"] > 50, I["rsi"] < 50
    put("볼밴돌파+EMA+RSI [메인봇]", brk_l & trend_l & rsi_l, brk_s & trend_s & rsi_s)
    put("볼밴돌파+EMA", brk_l & trend_l, brk_s & trend_s)
    put("볼밴돌파+RSI", brk_l & rsi_l, brk_s & rsi_s)
    put("볼밴돌파 단독", brk_l, brk_s)
    put("EMA추세 단독", trend_l, trend_s)

    # --- 평균회귀 계열 (돌파의 정반대로 태운다) -----------------------------
    put("볼밴 역추세(이탈 반대)", brk_s, brk_l)

    # --- RSI 계열 --------------------------------------------------------
    # 교과서 방향(과매도=반등)과 그 정반대(과매수=추세지속)를 둘 다 잰다.
    # 어느 쪽이 맞는지는 시장이 정하지 우리가 정하지 않는다.
    for lo, hi in ((30, 70), (20, 80), (40, 60)):
        put(f"RSI 역추세 {lo}/{hi}(과매도롱)", I["rsi"] < lo, I["rsi"] > hi)
        put(f"RSI 모멘텀 {lo}/{hi}(과매수롱)", I["rsi"] > hi, I["rsi"] < lo)
    rmom_l, rmom_s = I["rsi"] > 70, I["rsi"] < 30
    put("RSI모멘텀 + EMA추세", rmom_l & trend_l, rmom_s & trend_s)
    put("RSI모멘텀 + 4h정합", rmom_l & htf_up, rmom_s & htf_dn)
    put("RSI모멘텀 + CM방향", rmom_l & cm_l, rmom_s & cm_s)

    # --- 횡단면 계열: 같은 시각 다른 심볼 대비 상대강도 ----------------------
    for lb in (5, 20):
        r = np.full(C.shape, np.nan)
        r[lb:] = C[lb:] / C[:-lb] - 1.0
        med = np.nanmedian(r, axis=1, keepdims=True)
        rel = r - med
        q = np.nanquantile(rel, [0.2, 0.8], axis=1)
        lo, hi = q[0][:, None], q[1][:, None]
        strong, weak = rel >= hi, rel <= lo
        put(f"횡단면 모멘텀 {lb}봉(강세롱)", strong, weak)
        put(f"횡단면 역추세 {lb}봉(약세롱)", weak, strong)
    return S


# ---------------------------------------------------------------- 측정
def forward(P, hor):
    C, O = P["c"], P["o"]
    T = C.shape[0]
    out = {}
    for h in hor:
        e = np.full(C.shape, np.nan)
        x = np.full(C.shape, np.nan)
        e[:T - 1 - h] = O[1:T - h]
        x[:T - 1 - h] = C[1 + h:]
        with np.errstate(invalid="ignore", divide="ignore"):
            out[h] = (x / e - 1.0) * 100.0
    return out


def stats(sig, fwd):
    """(건수, 건당평균, 심볼중앙값, 시각클러스터 t, 심볼클러스터 t).

    t 를 두 축으로 낸다. 어느 하나만 크면 엣지로 인정하지 않는다.
      - 시각 t: 같은 시각 심볼들의 상관을 제거. "특정 몇 시간이 다 만든 값인가"를 잡는다.
      - 심볼 t: 심볼별 평균들 사이의 t. "특정 몇 종목이 다 만든 값인가"를 잡는다.
    심볼중앙값이 0 근처인데 평균만 크면 소수 급등종목의 꼬리다.
    """
    v = np.where(sig != 0, fwd * sig, np.nan)
    flat = v[~np.isnan(v)]
    n = flat.size
    if n < 200:
        return None
    mean = float(flat.mean())
    with np.errstate(invalid="ignore"):
        row = np.nanmean(v, axis=1)
        col = np.nanmean(v, axis=0)
        cnt = np.sum(~np.isnan(v), axis=0)
    row = row[~np.isnan(row)]
    col = col[(cnt >= 20) & ~np.isnan(col)]
    t_time = t_sym = float("nan")
    if row.size > 30:
        t_time = float(row.mean()) / (row.std(ddof=1) / math.sqrt(row.size))
    if col.size > 5:
        t_sym = float(col.mean()) / (col.std(ddof=1) / math.sqrt(col.size))
    med = float(np.median(col)) if col.size else float("nan")
    return n, mean, med, t_time, t_sym


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--cache", default="scratch_edge_3m_30d.npz")
    p.add_argument("--interval", default="3m")
    p.add_argument("--horizons", default="5,20,40,80")
    p.add_argument("--raw", action="store_true", help="드리프트 중립화 전 값도 함께 출력")
    p.add_argument("--stride", type=int, default=1,
                   help="N봉마다 한 번만 표본추출 — 보유구간 겹침 제거용")
    p.add_argument("--detail", default="", help="이름에 이 문자열이 든 신호를 롱/숏·심볼별로 분해")
    p.add_argument("--split", type=int, default=0, help="표본을 N등분해 국면별로 재측정")
    p.add_argument("--funding", default="", help="펀딩비 npz (edge_funding.py 산출물)")
    a = p.parse_args()
    hor = [int(x) for x in a.horizons.split(",")]

    P = load_panel(a.cache, a.interval)
    T, S = P["c"].shape
    bar_min = int(a.interval[:-1]) * (60 if a.interval.endswith("h") else 1)
    days = T * bar_min / 1440.0
    print(f"패널: {S}심볼 x {T}봉 ({a.interval}, 약 {days:.1f}일)\n")

    I = build_indicators(P)
    SIG = signals(P, I)
    F = forward(P, hor)
    FN = {}
    for h in hor:
        with np.errstate(invalid="ignore"):
            FN[h] = F[h] - np.nanmean(F[h], axis=1, keepdims=True)

    FD = {}
    if a.funding:
        # 보유구간에 걸린 펀딩 정산액을 뺀다.
        # 부호 규약: rate > 0 이면 롱이 지불한다. 신호 방향 부호(sgn)를 곱하는 것은
        # stats() 이므로, 여기서는 "롱 기준 비용"인 rate 합을 그대로 만들어 두고
        # 가격수익률에서 빼기만 하면 롱/숏 양쪽이 자동으로 맞는다.
        #   롱 순익  = +(ret - fund),  숏 순익 = -(ret - fund) = -ret + fund
        fz = np.load(ROOT / a.funding, allow_pickle=True)
        bar_ms = bar_min * 60_000
        cum = np.zeros((T + 1, S))
        for j, s in enumerate(P["syms"]):
            key = f"{s}|funding"
            if key not in fz:
                cum[:, j] = np.nan
                continue
            fr = fz[key]
            # 각 봉 시각까지의 누적 펀딩률. 마지막 행은 마지막 봉의 '마감' 시각용.
            edges = np.concatenate([P["t"], [P["t"][-1] + bar_ms]])
            pos = np.searchsorted(fr[:, 0], edges, side="right")
            cum[:, j] = np.concatenate([[0.0], np.cumsum(fr[:, 1])])[pos]
        miss = int(np.isnan(cum[0]).sum())
        for h in hor:
            f = np.full((T, S), np.nan)
            # 진입 = 봉 i+1 시가(t[i+1]), 청산 = 봉 i+1+h 마감(t[i+1+h] + bar_ms)
            lo = cum[1:T - h]                 # t[i+1]        (i = 0..T-h-2)
            hi = cum[2 + h:T + 1]             # t[i+1+h]+bar  (같은 i 범위)
            f[:T - 1 - h] = (hi - lo) * 100.0
            FD[h] = f
        avg = {h: np.nanmean(FD[h]) for h in hor}
        print("펀딩비 적용: " + " / ".join(
            f"{h * bar_min / 60:g}시간 보유 평균 {avg[h]:+.4f}%(롱 기준 비용)" for h in hor))
        if miss:
            print(f"  주의: 펀딩 이력이 없는 심볼 {miss}개는 이 표에서 제외된다.")
        print()

    if a.stride > 1:
        # 겹치는 보유구간은 서로 독립이 아니다. h봉 보유를 h봉 간격으로만 표본추출하면
        # 선행수익률이 거의 겹치지 않아 t 가 정직해진다. 건수는 stride 배로 줄어든다.
        keep = np.zeros(T, dtype=bool)
        keep[::a.stride] = True
        for k in SIG:
            SIG[k] = np.where(keep[:, None], SIG[k], 0).astype(np.int8)
        print(f"(stride={a.stride}: {a.stride}봉마다 한 번만 표본추출해 보유구간 겹침을 제거)")
        print()

    def table(title, FF, rowslice=None):
        print(f"[{title}] 진입=다음봉 시가 / 청산=h봉 뒤 종가, 단위 % (건당)")
        def hlab(h):
            m = h * bar_min
            return f"{m}분" if m < 120 else f"{m / 60:g}시간"

        head = "".join(hlab(h).rjust(30) for h in hor)
        print(f"{'신호':<30}{'건수':>9}{'건/심볼일':>11}{head}")
        for name, sg in SIG.items():
            sgx = sg if rowslice is None else sg[rowslice]
            cells, n0 = [], 0
            for h in hor:
                fx = FF[h] if rowslice is None else FF[h][rowslice]
                r = stats(sgx, fx)
                if r is None:
                    cells.append("-")
                    continue
                n, m, med, tt, ts = r
                n0 = n
                cells.append(f"{m:+.4f}|{med:+.4f} (t{tt:+.1f}/{ts:+.1f})")
            span = days if rowslice is None else days * (rowslice.stop - rowslice.start) / T
            print(f"{name:<30}{n0:>9}{n0 / S / max(span, 1e-9):>11.1f}"
                  + "".join(c.rjust(30) for c in cells))
        print()

    if a.raw:
        table("원시 (드리프트 포함)", F)
    table("드리프트 중립", FN)
    if FD:
        FNF = {h: FN[h] - FD[h] for h in hor}
        table("드리프트 중립 + 펀딩비 차감", FNF)
        FN = FNF   # 이후 detail/split 도 펀딩 반영분으로 본다

    if a.detail:
        # 롱/숏 분해 + 심볼 집중도. 드리프트 중립화가 롱 편향을 제대로 걷어냈는지,
        # 엣지가 소수 심볼에 몰려 있지 않은지 확인한다.
        for name, sg in SIG.items():
            if a.detail not in name:
                continue
            print(f"[분해] {name}")
            for lab, mask in (("LONG만", sg > 0), ("SHORT만", sg < 0)):
                one = np.where(mask, sg, 0).astype(np.int8)
                cells = []
                for h in hor:
                    r = stats(one, FN[h])
                    cells.append("-" if r is None else
                                 f"{r[1]:+.4f}|{r[2]:+.4f} (t{r[3]:+.1f}/{r[4]:+.1f})")
                cnt = int((one != 0).sum())
                print(f"  {lab:<10}{cnt:>9}" + "".join(c.rjust(30) for c in cells))
            # 심볼별 기여 (가장 긴 지평 기준)
            h = hor[-1]
            v = np.where(sg != 0, FN[h] * sg, np.nan)
            per = [(P["syms"][j], np.nanmean(v[:, j]), int(np.sum(~np.isnan(v[:, j]))))
                   for j in range(S) if np.sum(~np.isnan(v[:, j])) >= 20]
            per.sort(key=lambda x: -x[1])
            pos = sum(1 for x in per if x[1] > 0)
            print(f"  심볼 {len(per)}개 중 {pos}개가 양수 ({pos / max(len(per), 1) * 100:.0f}%). "
                  f"상위 {', '.join('%s %+.2f' % (x[0], x[1]) for x in per[:3])} / "
                  f"하위 {', '.join('%s %+.2f' % (x[0], x[1]) for x in per[-3:])}")
            print()

    if a.split > 1:
        step = T // a.split
        for k in range(a.split):
            sl = slice(k * step, (k + 1) * step if k < a.split - 1 else T)
            table(f"드리프트 중립 — 구간 {k + 1}/{a.split}", FN, sl)

    print(f"비용선: 왕복 {COST_PCT}% (수수료 0.07 + 슬리피지 가정 0.04). "
          f"건당 값이 이 선을 못 넘으면 진입축을 어떻게 튜닝해도 못 번다.")
    print("표기 = 건당평균|심볼중앙값 (시각클러스터 t / 심볼클러스터 t).")
    print("판정 규칙: 평균과 중앙값이 같은 부호로 붙어 있고 두 t 가 모두 살아 있어야 엣지로 본다. "
          "평균만 크고 중앙값이 0 근처면 소수 급등종목의 꼬리이지 반복 가능한 엣지가 아니다.")


if __name__ == "__main__":
    main()
