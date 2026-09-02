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

비용선 (2026-08-31 실측 재교정, --rescore 참조):
    종전의 단일선 0.11%(수수료 0.07 + 슬리피지 0.04)는 **체결 방식을 특정하지 않은**
    값이었다. e3 원장 1,017건에서 건별 왕복 수수료율을 뽑으니 경로별로 갈린다.
      - 지정가 TP 경로 : 0.0401%  (maker 진입 + maker 청산)
      - 손절 경로      : 0.0504%~0.07% (maker 진입 + taker 손절)
    그리고 edge_lab 의 측정 규약(시가 진입/종가 청산)은 **둘 다 taker** 이므로
    0.10% 를 써야 한다. 즉 하나의 선이 아니라 두 개다.
      보수선(taker/taker) = 0.10 + 슬리피지*2
      e3선  (maker/maker) = 0.04 + 0     (지정가는 스프레드를 건너지 않는다)
    두 선 **사이**에 있는 신호는 기각도 채택도 아니고 "체결을 maker 로 만들 수 있으면
    산다"는 **조건부**다. 이 칸이 종전에 없어서 후보가 통째로 기각되고 있었다.

    슬리피지는 실측하지 못했다. trade_ledger 의 slippage_pct 는 exit_price 가 mark
    추정치라 체결가-마크 괴리를 재고 있을 뿐 실행비용이 아니다(익절 +0.68% / 손절
    -0.72% 로 부호가 청산방향과 상관). 그래서 가정으로 남기되 --slip 으로 노출한다.
"""
import argparse
import math
import pathlib
import sys

import numpy as np

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
ROOT = pathlib.Path(__file__).resolve().parent.parent
# 편도 수수료 실측(e3 원장): maker 0.02% / taker 0.05%.
FEE_TAKER_RT = 0.10   # 시가진입+종가청산 = taker 왕복
FEE_MAKER_RT = 0.04   # 지정가 진입 + 지정가 TP = maker 왕복


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
def load_panel(paths, interval, end_date="", start_date=""):
    """여러 npz 를 하나의 T x S 패널로 합친다.

    생존편향 측정 때 '살아남은 심볼' 캐시와 '상장폐지 심볼' 캐시를 같은 시간축에
    올려야 하기 때문에 여러 경로를 받는다. 심볼이 겹치면 처음 것이 이긴다.
    """
    if isinstance(paths, str):
        paths = [paths]
    zs, syms, owner = [], [], {}
    for path in paths:
        z = np.load(ROOT / path, allow_pickle=True)
        zs.append(z)
        for x in z["__symbols__"]:
            s = str(x)
            if s not in owner:
                owner[s] = z
                syms.append(s)
    tset = set()
    for s in syms:
        tset |= set(owner[s][f"{s}|{interval}"][:, 0].astype(np.int64).tolist())
    times = sorted(tset)
    import datetime as _dt

    def _ms(d):
        return _dt.datetime.strptime(d, "%Y-%m-%d").replace(
            tzinfo=_dt.timezone.utc).timestamp() * 1000

    if start_date:
        times = [t for t in times if t >= _ms(start_date)]
    if end_date:
        times = [t for t in times if t < _ms(end_date)]
    T, S = len(times), len(syms)
    idx = {t: i for i, t in enumerate(times)}
    P = {k: np.full((T, S), np.nan) for k in "ohlcv"}
    for j, s in enumerate(syms):
        b = owner[s][f"{s}|{interval}"]
        keep = [(i, idx[t]) for i, t in enumerate(b[:, 0].astype(np.int64).tolist())
                if t in idx]
        if not keep:
            continue
        src = np.array([k[0] for k in keep])
        rows = np.array([k[1] for k in keep])
        for k, col in zip("ohlcv", range(1, 6)):
            P[k][rows, j] = b[src, col]
    P["t"] = np.asarray(times, dtype=np.int64)
    P["syms"] = syms
    P["h4"] = {s: owner[s][f"{s}|4h"] for s in syms}
    return P


def build_indicators(P, smoothe: int = 2):
    T, S = P["c"].shape
    keys = ("hma", "hma_up", "ema20", "ema50", "rsi", "bbu", "bbl", "bbm", "htf",
            "ma5", "ma20", "ma60")
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
        # 색 판정 = Pine `ma_up = out1 >= out1[smoothe]`. CM 기본 2, 블로그 권장 3~5.
        k = max(1, int(smoothe))
        up = np.full(T, np.nan)
        up[k:] = (h[k:] >= h[:-k]).astype(float)
        ind["hma_up"][:, j] = up
        ind["ema20"][:, j] = ema(cf, 20)
        # 통상적인 5/20/60 이동평균 3종(사용자 요청 축). SMA 로 둔다.
        for _len, _k in ((5, "ma5"), (20, "ma20"), (60, "ma60")):
            _m, _ = roll_mean_std(cf, _len)
            ind[_k][:, j] = _m
        ind["ema50"][:, j] = ema(cf, 50)
        ind["rsi"][:, j] = rsi_wilder(cf, 14)
        m, sd = roll_mean_std(cf, 20)
        ind["bbm"][:, j] = m
        ind["bbu"][:, j] = m + 2 * sd
        ind["bbl"][:, j] = m - 2 * sd
        # 4시간 EMA200: **마감된** 상위봉만 쓴다 (lookahead 없음)
        b4 = P["h4"][s]
        if len(b4) >= 210:
            t4 = b4[:, 0].astype(np.int64)
            e4 = ema(b4[:, 4], 200)
            close4 = t4 + 4 * 3600_000          # 상위봉 마감 시각
            pos = np.searchsorted(close4, P["t"], side="right") - 1
            good = pos >= 200
            val = np.where(good, e4[np.clip(pos, 0, len(e4) - 1)], np.nan)
            ind["htf"][:, j] = (cf > val).astype(float)
            ind["htf"][np.isnan(val), j] = np.nan
        # else: 상위봉이 없는 심볼(상폐분)은 htf 를 NaN 으로 둔다 — 4h 필터 계열만 빠진다
        for k in keys:
            ind[k][~ok, j] = np.nan
    return ind


def load_funding(paths, P, bar_min, hor):
    """보유구간 펀딩 정산액(FD)과 각 봉 시점의 최신 펀딩률(FRM)을 만든다.

    부호 규약: rate > 0 이면 **롱이 지불한다**. 방향 부호(sgn)는 stats() 가 곱하므로
    여기서는 '롱 기준 비용'인 rate 합만 만들어 두고 가격수익률에서 빼면
    롱/숏 양쪽이 자동으로 맞는다.
        롱 순익 = +(ret - fund),  숏 순익 = -(ret - fund) = -ret + fund
    """
    T, S = P["c"].shape
    bar_ms = bar_min * 60_000
    zs = [np.load(ROOT / x, allow_pickle=True) for x in paths]
    cum = np.zeros((T + 1, S))
    frm = np.full((T, S), np.nan)
    edges = np.concatenate([P["t"], [P["t"][-1] + bar_ms]])
    miss = 0
    for j, sym in enumerate(P["syms"]):
        key = f"{sym}|funding"
        z = next((z for z in zs if key in z), None)
        if z is None:
            cum[:, j] = np.nan
            miss += 1
            continue
        fr = z[key]
        pos = np.searchsorted(fr[:, 0], edges, side="right")
        cum[:, j] = np.concatenate([[0.0], np.cumsum(fr[:, 1])])[pos]
        # 각 봉 시점에서 '마지막으로 정산된' 펀딩률 (미래 정보 없음)
        idx = np.clip(pos[:T] - 1, -1, len(fr) - 1)
        v = np.where(idx >= 0, fr[np.clip(idx, 0, len(fr) - 1), 1], np.nan)
        frm[:, j] = v
    FD = {}
    for h in hor:
        f = np.full((T, S), np.nan)
        # 진입 = 봉 i+1 시가(t[i+1]), 청산 = 봉 i+1+h 마감(t[i+1+h] + bar_ms)
        f[:T - 1 - h] = (cum[2 + h:T + 1] - cum[1:T - h]) * 100.0
        FD[h] = f
    return FD, frm, miss


def load_metrics(path, P):
    """metrics 를 봉 시각에 맞춰 리샘플한다 — 각 봉 시각 **이전의 마지막 관측**만 쓴다."""
    T, S = P["c"].shape
    z = np.load(ROOT / path, allow_pickle=True)
    cols = [str(x) for x in z["__cols__"]]
    M = {c: np.full((T, S), np.nan) for c in cols}
    have = 0
    for j, sym in enumerate(P["syms"]):
        key = f"{sym}|metrics"
        if key not in z:
            continue
        m = z[key]
        pos = np.searchsorted(m[:, 0], P["t"], side="right") - 1
        good = pos >= 0
        for k, c in enumerate(cols, start=1):
            v = np.where(good, m[np.clip(pos, 0, len(m) - 1), k], np.nan)
            M[c][:, j] = v
        have += 1
    return M, have


# ---------------------------------------------------------------- 신호 정의
def signals(P, I, FRM=None, M=None):
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

    # 캔들이 HullMA 를 관통(Pine: Show Price Crossing = 노란 캔들). 아래 두 블록이 쓴다.
    O = P["o"]
    cr_up = (O < I["hma"]) & (C > I["hma"])
    cr_dn = (O > I["hma"]) & (C < I["hma"])

    # --- 블로그(rajya) 가 제시한 "스나이퍼 콤보" ------------------------------
    # 출처: 사용자가 제공한 네이버 블로그 본문. 라이브 e3 와 **네 군데가 다르다**.
    #   차트      15분봉        (라이브 3분봉)
    #   1st MA    HullMA20      (같음)
    #   2nd MA    4h EMA200     (라이브도 4h EMA200 필터 — 같음)
    #   진입      HullMA 색이 **빨강->라임으로 전환** + 캔들이 HullMA 를 **관통**(노란 캔들)
    #             (라이브는 `ma_up` **상태** + `close > hma` **상태**. 전환도 관통도 아니다)
    #   스무딩    3~5 권장      (라이브/Pine 기본 2)  -> --cm-smoothe 로 뺀다
    #
    # 블로그 원문에 오류가 하나 있다: "Use Different Timeframe 을 **체크 해제**하고
    # 240(4시간봉)으로 맞추라"고 적혀 있는데, 해제하면 상위봉을 안 쓴다. 4h EMA200 을
    # 하위 차트에 얹으려면 **체크**해야 한다. 의도(4h EMA200 을 대추세로)로 구현한다.
    #
    # 사전 등록: 아래 4개만 본다. 결과를 보고 더 붙이지 않는다.
    flip_l = up & ~np.roll(up, 1, axis=0)      # 직전봉 하락 -> 이번봉 상승 (빨강->라임)
    flip_s = dn & ~np.roll(dn, 1, axis=0)
    flip_l[0], flip_s[0] = False, False

    def within(m, n):
        """전환이 최근 n봉 안에 있었는가(전환봉 포함)."""
        a = m.copy()
        for i in range(1, n):
            a |= np.roll(m, i, axis=0)
        a[:n] = False
        return a

    put("[블로그] 색전환 단독", flip_l, flip_s)
    put("[블로그] 색전환 + 4h정합", flip_l & htf_up, flip_s & htf_dn)
    put("[블로그] 색전환+관통+4h (동일봉)",
        flip_l & cr_up & htf_up, flip_s & cr_dn & htf_dn)
    put("[블로그] 전환5봉내+관통+4h",
        within(flip_l, 5) & cr_up & htf_up, within(flip_s, 5) & cr_dn & htf_dn)

    # --- 원칙 0 "교차" (문서에는 있는데 코드에 없던 축) ----------------------
    # CLAUDE.md 원칙 0 은 "HullMA20 방향/**교차**" 라고 적혀 있는데, 라이브 코드는
    # 교차를 한 번도 쓰지 않는다. scalp_bot_e3 의 cr_up/cr_down/crossed 는 계산되어
    # 반환 dict 에 담기기만 하고 **어디서도 소비되지 않는다**(각 2회 등장 = 정의+반환).
    # 실제 규칙은 `ma_up and close > hma` 로 교차(event)가 아니라 상태(state)다.
    # 그래서 "교차로 진입했다면 어땠는가"는 한 번도 측정된 적이 없다. 여기서 잰다.
    #
    # 원본 Pine 정의 그대로: cr_up = open < out1 and close > out1 (캔들이 MA 를 관통).
    # out1 x out2 크로스(crossed)는 범위 밖이다 — 라이브가 doma2=False 라 두 번째
    # MA 자체를 판정에 안 쓴다. 없는 축을 만들어 붙이지 않는다.
    #
    # 사전 등록: 아래 3개만 본다. 좋아 보이는 걸 더 찾아 붙이지 않는다.
    put("[교차] 관통 단독", cr_up, cr_dn)
    put("[교차] 관통 + 방향정합", cr_up & up, cr_dn & dn)
    put("[교차] 관통 + 방향 + 4h정합",
        cr_up & up & (I["htf"] == 1), cr_dn & dn & (I["htf"] == 0))

    # --- 원칙 0 분해 (CM 을 구성요소로 쪼갠다) ------------------------------
    # CM 진입 규칙 = "HullMA20 기울기 방향" AND "종가가 HullMA 위/아래" 두 조건의 곱이다.
    # 이 둘을 한 번도 따로 재본 적이 없다. 그리고 네 번째 조합(기울기는 위인데 종가가
    # HullMA 아래 = 상승추세 눌림)은 **라이브 e3 가 실제로 진입하는 자리**에 가장 가까운데
    # (e3 는 CM 신호 후 EMA5 눌림을 기다린다) 지금까지 측정 대상에 없었다.
    # 사전 등록: 아래 6개만 본다. 좋아 보이는 걸 더 찾아 붙이지 않는다.
    put("[분해] 기울기만", up, dn)
    put("[분해] 가격위치만", C > I["hma"], C < I["hma"])
    put("[분해] 눌림(기울기↑ & 종가<HMA)", up & (C < I["hma"]), dn & (C > I["hma"]))
    put("[분해] 눌림 + 4h정합",
        up & (C < I["hma"]) & (I["htf"] == 1), dn & (C > I["hma"]) & (I["htf"] == 0))
    put("[분해] CM + 4h역행", cm_l & (I["htf"] == 0), cm_s & (I["htf"] == 1))

    # --- 메인봇 뼈대: 볼밴 돌파 + EMA돌파 + RSI (CLAUDE.md 원칙 0 참조) ------
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

    # --- F1(밴드폭) 선별 x 방향 — e5 실거래 진단용 -----------------------------
    # e5 라이브는 F1(밴드폭) 92~94 백분위로 **변동성 높은 심볼**을 고른 뒤
    # **CM 추세추종**으로 진입한다. 실거래 20건에서 승률 15%, 건당 -0.246 이다.
    #
    # 가설: 밴드폭이 크다 = 변동성이 높다 = **평균회귀가 강한 국면**이다.
    # 그렇다면 F1 이 고르는 국면과 CM 추세추종은 서로 반대일 수 있다.
    # 같은 선별에 방향만 바꿔 비교한다. 사전 등록: 아래 4개만 본다.
    bw_f1 = (I["bbu"] - I["bbl"]) / np.where(I["bbm"] > 0, I["bbm"], np.nan)
    f1q = np.full(bw_f1.shape, np.nan)
    for j in range(bw_f1.shape[1]):
        v = bw_f1[:, j]
        for i in range(1440, len(v)):          # e5 와 같은 1,440봉 rolling 분포
            w = v[i - 1440:i]
            ok = ~np.isnan(w)
            if ok.sum() > 500 and not np.isnan(v[i]):
                f1q[i, j] = (w[ok] < v[i]).mean()
    f1_hi = (f1q >= 0.92) & (f1q <= 0.94)      # e5 라이브 게이트와 동일 구간
    put("[F1] 선별 + CM추세 (= e5 현행)", f1_hi & cm_l, f1_hi & cm_s)
    put("[F1] 선별 + CM반전", f1_hi & dn & (C < I["hma"]), f1_hi & up & (C > I["hma"]))
    put("[F1] 선별 + 볼밴역추세", f1_hi & (C <= I["bbl"]), f1_hi & (C >= I["bbu"]))
    put("[F1] 선별만 (방향=롱)", f1_hi, np.zeros_like(f1_hi, dtype=bool))

    # --- 순수 볼밴 매매 (CM 을 완전히 뺀다) ----------------------------------
    # 기존 "볼밴돌파/역추세" 는 `종가<=하단 AND 신저가` 라는 **돌파형** 조건이다.
    # 그런데 통상 "볼밴 매매"라 하면 **단순 밴드 터치 후 중심선 회귀**를 뜻한다.
    # 신저가 조건이 빠지면 건수가 크게 늘고 성격도 달라진다 — 한 번도 안 쟀다.
    #
    # 사전 등록: 아래 3개만 본다. 결과를 보고 더 붙이지 않는다.
    #   1) 단순 터치 역추세 : 하단 터치 -> 롱 / 상단 터치 -> 숏 (신고저 조건 없음)
    #   2) 단순 터치 순추세 : 그 반대 (부호를 미리 단정하지 않으려고 짝으로 넣는다)
    #   3) 스퀴즈 돌파      : 밴드폭이 최근 100봉 하위 20% 로 좁아진 뒤의 돌파
    bw = (I["bbu"] - I["bbl"]) / np.where(I["bbm"] > 0, I["bbm"], np.nan)
    bw_q = np.full(bw.shape, np.nan)
    for j in range(bw.shape[1]):
        v = bw[:, j]
        for i in range(100, len(v)):
            w = v[i - 100:i]
            ok = ~np.isnan(w)
            if ok.sum() > 50 and not np.isnan(v[i]):
                bw_q[i, j] = (w[ok] < v[i]).mean()
    squeeze = bw_q <= 0.20

    touch_l, touch_s = C <= I["bbl"], C >= I["bbu"]
    put("[순수볼밴] 단순터치 역추세", touch_l, touch_s)
    put("[순수볼밴] 단순터치 순추세", touch_s, touch_l)
    put("[순수볼밴] 스퀴즈 후 돌파", squeeze & touch_s, squeeze & touch_l)

    # --- MA 5/20/60 + 볼밴 역추세 (CM 완전 배제) -----------------------------
    # 사용자 요청: "CM 포기, 일반 볼밴 + 5/20/60 이동평균으로 역추세 스캘핑".
    # 1분봉 기준 MA5=5분 / MA20=20분 / MA60=1시간 이라 스캘핑 축과 맞는다.
    #
    # 오늘 확인된 것: 추세 필터를 역추세 신호에 얹으면 **나빠졌다**
    # (CM 기울기↑ + 중심선 +0.0041 vs 순수 볼밴 +0.0127). 그래서 배열 조건도
    # 도움이 될지 해가 될지 모른다 -> **정배열/역배열을 짝으로** 등록해 사후 선택을 막는다.
    #
    # 사전 등록: 아래 3개만 본다. 결과를 보고 더 붙이지 않는다.
    ma_up3 = (I["ma5"] > I["ma20"]) & (I["ma20"] > I["ma60"])     # 정배열
    ma_dn3 = (I["ma5"] < I["ma20"]) & (I["ma20"] < I["ma60"])     # 역배열
    bl, bu = C <= I["bbl"], C >= I["bbu"]
    put("[MA576] 정배열 + 볼밴역추세", ma_up3 & bl, ma_dn3 & bu)
    put("[MA576] 역배열 + 볼밴역추세", ma_dn3 & bl, ma_up3 & bu)
    put("[MA576] 60선 위/아래 + 볼밴역추세",
        (C > I["ma60"]) & bl, (C < I["ma60"]) & bu)

    # --- CM 반전(역추세) 계열 ------------------------------------------------
    # 사용자 요청: "CM 지표는 그대로 쓰고 역추세로 전환". 가장 단순한 형태는
    # **신호를 그대로 뒤집는 것**이다 — CM 이 롱이라 하면 숏, 숏이라 하면 롱.
    # (CM 전체가 1분봉 60일에서 -0.0038 이므로 반전은 정확히 +0.0038 이다.)
    #
    # 그리고 거기에 볼밴의 **명시적 과매도 기준(2시그마)** 을 더한 것이 (C) 다.
    # CM 반전 롱 = `기울기 하락 & 종가 < HullMA` 이고 볼밴 하단터치도 종가가
    # 이동평균 아래이므로 **논리적으로 배타가 아니다**(보강 9 의 문제가 없다).
    #
    # 기울기 방향을 한쪽만 등록하면 사후 선택이 된다. 이미 등록된
    # `[볼밴눌림] 기울기 + 하단터치`(상승 기울기)의 **짝**으로 하락 기울기를 넣어
    # 쌍을 완성한다. 사전 등록: 아래 2개만 본다.
    put("[CM반전] 단독", dn & (C < I["hma"]), up & (C > I["hma"]))
    put("[CM반전] + 볼밴하단터치",
        dn & (C < I["hma"]) & (C <= I["bbl"]),
        up & (C > I["hma"]) & (C >= I["bbu"]))

    # --- CM 방향 + 볼밴을 **진입 트리거**로 (눌림 깊이를 바꾼다) ---------------
    # 20장에서 확인된 것: 눌림 자리 체결이 추격 자리보다 건당 2배 덜 잃는데
    # (-0.354 vs -0.730), **EMA5 눌림은 얕아서 45.6% 만 눌림 자리에 든다.**
    # 그러면 더 깊은 눌림선을 쓰면 어떻게 되는가 — 볼밴 하단/중심선이 그것이다.
    # (봇 코드에 second_at_band 로 개념은 있으나 --tranches 1 이라 라이브 미사용.)
    #
    # 주의: 방향은 **기울기만**(up) 을 쓴다. CM 전체는 `close > hma` 를 포함하는데
    # 볼밴 하단 터치는 `close <= bbl < sma20 ~ hma` 라 **논리적으로 배타적**이다
    # (보강 9 에서 확인, 실측 0건). 그래서 여기서는 가격위치 조건을 뺀다.
    #
    # 사전 등록: 아래 4개만 본다. 결과를 보고 더 붙이지 않는다.
    put("[볼밴눌림] 기울기 + 하단터치", up & (C <= I["bbl"]), dn & (C >= I["bbu"]))
    put("[볼밴눌림] 기울기 + 하단터치 + 4h",
        up & (C <= I["bbl"]) & htf_up, dn & (C >= I["bbu"]) & htf_dn)
    put("[볼밴눌림] 기울기 + 중심선터치", up & (C <= I["bbm"]), dn & (C >= I["bbm"]))
    put("[볼밴눌림] 기울기 + 중심선 + 4h",
        up & (C <= I["bbm"]) & htf_up, dn & (C >= I["bbm"]) & htf_dn)

    # --- 볼밴 x CM (보조지표 용법) ------------------------------------------
    # 블로그(rajya)와 사용자 질문의 요지: CM 은 **단독 진입 신호가 아니라 필터**로
    # 쓸 때 값어치가 있다. 실제로 21장에서 그 방향은 데이터가 지지했다
    # (색전환 단독 -0.0199 -> +4h정합 -0.0025 / 관통 단독 -0.0123 -> +방향+4h +0.0059).
    # 그런데 **볼밴 x CM 조합은 한 번도 측정된 적이 없다.** 여기서 잰다.
    #
    # 보조지표 평가는 절대값이 아니라 **단독 대비 차이(delta)** 로 본다. 그래서
    # 돌파/역추세 각각에 대해 (단독) / (+CM) / (+CM+4h) 를 짝으로 넣는다.
    # 위 "볼밴돌파 단독" 과 "볼밴 역추세" 가 그 짝의 기준선이다.
    #
    # 사전 등록: 아래 4개만 본다. 결과를 보고 더 붙이지 않는다.
    put("볼밴돌파 + CM", brk_l & cm_l, brk_s & cm_s)
    put("볼밴돌파 + CM + 4h", brk_l & cm_l & htf_up, brk_s & cm_s & htf_dn)
    put("볼밴역추세 + CM", brk_s & cm_l, brk_l & cm_s)
    put("볼밴역추세 + CM + 4h", brk_s & cm_l & htf_up, brk_l & cm_s & htf_dn)

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

    # --- 펀딩 캐리 계열 -----------------------------------------------------
    # 지표가 아니라 '남들이 한쪽으로 쏠려서 내는 돈'을 받는 쪽에 서는 구성이다.
    # 펀딩이 높다(롱 과밀) -> 숏, 음수다(숏 과밀) -> 롱. 판정은 반드시 --funding 을
    # 켜고 봐야 한다. 받는 펀딩이 수익원의 절반이기 때문이다.
    if FRM is not None:
        fq = np.nanquantile(FRM, [0.2, 0.8], axis=1)
        flo, fhi = fq[0][:, None], fq[1][:, None]
        put("펀딩캐리 횡단면(고펀딩 숏)", FRM <= flo, FRM >= fhi)
        put("펀딩캐리 역방향(고펀딩 롱)", FRM >= fhi, FRM <= flo)
        for th in (0.0003, 0.001):
            put(f"펀딩캐리 절대 {th * 100:.2f}%", FRM <= -th, FRM >= th)
        put("펀딩캐리 + EMA추세 정합",
            (FRM <= flo) & trend_l, (FRM >= fhi) & trend_s)

    # --- OI / 포지셔닝 계열 (klines 에 없는 정보) ---------------------------
    if M is not None:
        LB = 24                       # 1일 변화
        def chg(a):
            r = np.full(a.shape, np.nan)
            with np.errstate(invalid="ignore", divide="ignore"):
                r[LB:] = a[LB:] / a[:-LB] - 1.0
            return r

        doi = chg(M["oi"])
        dpx = chg(C)
        up_px, dn_px = dpx > 0, dpx < 0
        oi_up = doi > 0.02            # 미결제약정 2% 이상 증가
        oi_dn = doi < -0.02
        # 신규 자금이 들어오며 움직인 방향 = 추세지속 가설
        put("OI증가+가격방향(추세지속)", oi_up & up_px, oi_up & dn_px)
        # 포지션이 청산되며 움직인 방향 = 되돌림 가설
        put("OI감소+가격방향(페이드)", oi_dn & up_px, oi_dn & dn_px)

        def xq(a, name, lo_long=True):
            q = np.nanquantile(a, [0.2, 0.8], axis=1)
            lo, hi = q[0][:, None], q[1][:, None]
            weak, strong = a <= lo, a >= hi
            if lo_long:
                put(name, weak, strong)
            else:
                put(name, strong, weak)

        # 군중이 롱으로 쏠렸을 때 반대(역추세)와 순방향(모멘텀)을 둘 다 잰다
        xq(M["acct"], "전체계정 롱숏비 역추세(과밀롱=숏)", lo_long=True)
        xq(M["acct"], "전체계정 롱숏비 순방향", lo_long=False)
        xq(M["tt_sum"], "상위트레이더 포지션 순방향", lo_long=False)
        xq(M["tt_sum"], "상위트레이더 포지션 역추세", lo_long=True)
        xq(M["taker"], "taker 매수우위 순방향", lo_long=False)
        xq(M["taker"], "taker 매수우위 역추세", lo_long=True)
        xq(doi, "OI 증가율 상위 롱", lo_long=False)

    # --- 횡단면 계열: 같은 시각 다른 심볼 대비 상대강도 ----------------------
    for lb in (5, 10, 20, 40, 80):
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


def vol_matrix(P, window):
    """봉수익률의 이동 표준편차(분수 단위). 위험조정 측정에 쓴다."""
    C = P["c"]
    T, S = C.shape
    with np.errstate(invalid="ignore", divide="ignore"):
        r = np.full((T, S), np.nan)
        r[1:] = C[1:] / C[:-1] - 1.0
    out = np.full((T, S), np.nan)
    for j in range(S):
        v = r[:, j]
        ok = ~np.isnan(v)
        if ok.sum() < window + 10:
            continue
        x = np.where(ok, v, 0.0)
        n = ok.astype(float)
        c1 = np.cumsum(np.insert(x, 0, 0.0))
        c2 = np.cumsum(np.insert(x * x, 0, 0.0))
        cn = np.cumsum(np.insert(n, 0, 0.0))
        su = c1[window:] - c1[:-window]
        sq = c2[window:] - c2[:-window]
        cnt = cn[window:] - cn[:-window]
        with np.errstate(invalid="ignore", divide="ignore"):
            mu = su / cnt
            var = np.maximum(sq / cnt - mu * mu, 0.0)
        sd = np.sqrt(var)
        sd[cnt < window * 0.5] = np.nan
        # 창 합 c1[k+window]-c1[k] 은 x[k:k+window], 즉 인덱스 k+window-1 에서 끝난다.
        out[window - 1:, j] = sd
    return out


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
    p.add_argument("--extra-cache", default="",
                   help="함께 합칠 npz(쉼표 구분). 상장폐지 심볼 캐시 등")
    p.add_argument("--end-date", default="", help="YYYY-MM-DD 이후 봉은 버린다")
    p.add_argument("--start-date", default="", help="YYYY-MM-DD 이전 봉은 버린다")
    p.add_argument("--drop-symbols", default="", help="제외할 심볼(쉼표 구분)")
    p.add_argument("--keep-symbols", default="",
                   help="이 심볼만 남긴다(쉼표 구분). 시장평균 기준선은 전체로 유지")
    p.add_argument("--interval", default="3m")
    p.add_argument("--horizons", default="5,20,40,80")
    p.add_argument("--raw", action="store_true", help="드리프트 중립화 전 값도 함께 출력")
    p.add_argument("--vol-norm", type=int, default=0,
                   help="N봉 이동변동성으로 나눠 위험조정 단위(sigma)로 측정한다. "
                        "비용선 비교는 못 하지만 '정보가 있는가'는 훨씬 정확히 잰다")
    p.add_argument("--phase-sweep", action="store_true",
                   help="stride 의 전 위상을 돌려 평균·범위를 낸다(권장). 첫 horizon 기준")
    p.add_argument("--stride-offset", type=int, default=0,
                   help="stride 표본추출의 위상. 24시간 보유에서 '하루 중 몇 시에 진입하는가'")
    p.add_argument("--stride", type=int, default=1,
                   help="N봉마다 한 번만 표본추출 — 보유구간 겹침 제거용")
    p.add_argument("--detail", default="", help="이름에 이 문자열이 든 신호를 롱/숏·심볼별로 분해")
    p.add_argument("--split", type=int, default=0, help="표본을 N등분해 국면별로 재측정")
    p.add_argument("--funding", default="", help="펀딩비 npz (쉼표 구분 가능)")
    p.add_argument("--metrics", default="", help="OI/포지셔닝 npz (edge_metrics.py 산출물)")
    p.add_argument("--cm-smoothe", type=int, default=2,
                   help="CM 색 판정 스무딩 `ma_up = out1 >= out1[smoothe]`. "
                        "Pine/라이브 기본 2, 블로그 권장 3~5.")
    p.add_argument("--slip", type=float, default=0.02,
                   help="**편도** 슬리피지 가정 %%. 실측 불가라 가정치다(기본 0.02 "
                        "= 종전 왕복 0.04 가정과 동일). taker 선에만 왕복으로 더한다.")
    p.add_argument("--rescore", action="store_true",
                   help="두 비용선(taker/maker)으로 전 신호x전 지평을 재채점한 표를 낸다")
    a = p.parse_args()
    hor = [int(x) for x in a.horizons.split(",")]

    caches = [a.cache] + [x for x in a.extra_cache.split(",") if x]
    P = load_panel(caches, a.interval, a.end_date, a.start_date)
    if a.keep_symbols:
        # 신호는 이 심볼들에서만 발생시키되, **드리프트 중립화의 시장평균 기준선은
        # 전체 유니버스로 유지**한다. 기준선까지 유동 심볼로 좁히면 9장에서 본
        # 생존편향과 같은 경로로 편향이 생긴다.
        keep = {x.strip().upper() for x in a.keep_symbols.split(",") if x.strip()}
        P["_signal_mask"] = np.array([s in keep for s in P["syms"]])
        print(f"(신호 발생 심볼 {int(P['_signal_mask'].sum())}개로 제한 — "
              f"시장평균 기준선은 {len(P['syms'])}개 전체 유지)")
        print()

    if a.drop_symbols:
        drop = {x.strip().upper() for x in a.drop_symbols.split(",") if x.strip()}
        keepj = [j for j, s in enumerate(P["syms"]) if s not in drop]
        if len(keepj) < len(P["syms"]):
            print(f"(제외 {len(P['syms']) - len(keepj)}심볼)")
            for k in "ohlcv":
                P[k] = P[k][:, keepj]
            P["syms"] = [P["syms"][j] for j in keepj]
    T, S = P["c"].shape
    bar_min = int(a.interval[:-1]) * (60 if a.interval.endswith("h") else 1)
    days = T * bar_min / 1440.0
    print(f"패널: {S}심볼 x {T}봉 ({a.interval}, 약 {days:.1f}일)\n")

    I = build_indicators(P, smoothe=a.cm_smoothe)
    # 펀딩은 신호보다 먼저 만든다 — 펀딩 캐리 신호가 이 값을 쓰기 때문이다.
    FD, FRM = {}, None
    if a.funding:
        FD, FRM, miss = load_funding(
            [x for x in a.funding.split(",") if x], P, bar_min, hor)
        avg = {h: np.nanmean(FD[h]) for h in hor}
        print("펀딩비 적용: " + " / ".join(
            f"{h * bar_min / 60:g}시간 보유 평균 {avg[h]:+.4f}%(롱 기준 비용)" for h in hor))
        cov = S - miss
        print(f"  펀딩 이력 보유 심볼 {cov}/{S} — 캐리 신호는 이 {cov}개에서만 발생한다"
              f"(시장평균 기준선은 {S}개 전체로 계산).")
        print()

    MET = None
    if a.metrics:
        MET, have = load_metrics(a.metrics, P)
        print(f"OI/포지셔닝 지표: {have}/{S} 심볼 보유 — 해당 신호는 이 심볼들에서만 발생한다.")
        print()

    SIG = signals(P, I, FRM, MET)
    if "_signal_mask" in P:
        m = P["_signal_mask"][None, :]
        for k in SIG:
            SIG[k] = np.where(m, SIG[k], 0).astype(np.int8)
    F = forward(P, hor)
    FN = {}
    for h in hor:
        with np.errstate(invalid="ignore"):
            FN[h] = F[h] - np.nanmean(F[h], axis=1, keepdims=True)
    if a.vol_norm:
        # 위험조정: 각 관측을 그 심볼의 사전 변동성으로 나눈다(=동일 위험 사이징).
        # **비용선과 직접 비교할 수 없다** — 비용도 같은 배율로 커지기 때문이다.
        # 이 모드가 답하는 질문은 "이 신호에 정보가 있는가" 하나다.
        VM = vol_matrix(P, a.vol_norm)
        for h in hor:
            with np.errstate(invalid="ignore", divide="ignore"):
                denom = VM * math.sqrt(h) * 100.0
                FN[h] = np.where(denom > 0, FN[h] / denom, np.nan)
        print(f"위험조정 모드: {a.vol_norm}봉 이동변동성으로 나눔. "
              f"단위는 %가 아니라 **sigma** 이고 비용선 비교 불가.")
        print()

    if a.stride > 1 and a.phase_sweep:
        # **stride 표본추출에는 숨은 자유도가 있다.** keep[::stride] 는 항상 위상 0 을
        # 고르는데, 24시간 보유에서 그건 "매일 특정 시각에만 진입"을 뜻한다.
        # 위상에 따라 결과가 크게 갈리므로(실측 -0.04 ~ +0.15), 한 위상만 보고하면
        # 자기도 모르게 stride 방향 탐색의 승자를 보고하게 된다.
        # -> 전 위상을 돌려 **평균과 범위**를 함께 낸다. 판정은 평균으로 한다.
        print(f"[위상 스윕] stride={a.stride} 의 {a.stride}개 위상을 모두 측정한다.")
        print(f"{'신호':<30}{'위상평균|중앙값':>22}{'평균 시각t':>12}{'  위상 범위(평균)':>22}")
        base = {k: v.copy() for k, v in SIG.items()}
        h = hor[0]
        for name in base:
            ms, md, ts = [], [], []
            for off in range(a.stride):
                keep = np.zeros(T, dtype=bool)
                keep[off::a.stride] = True
                sg = np.where(keep[:, None], base[name], 0).astype(np.int8)
                r = stats(sg, FN[h])
                if r:
                    ms.append(r[1])
                    md.append(r[2])
                    ts.append(r[3])
            if not ms:
                continue
            print(f"{name:<30}{np.mean(ms):+10.4f}|{np.nanmean(md):+9.4f}"
                  f"{np.mean(ts):+12.2f}   {min(ms):+.4f} ~ {max(ms):+.4f}")
        print()
        print("한 위상만 좋은 신호는 엣지가 아니라 위상 운이다. 판정은 위상평균으로 한다.")
        print()

    SIG_FULL = {k: v.copy() for k, v in SIG.items()}   # stride 적용 전 원본(재채점 위상스윕용)
    if a.stride > 1:
        # 겹치는 보유구간은 서로 독립이 아니다. h봉 보유를 h봉 간격으로만 표본추출하면
        # 선행수익률이 거의 겹치지 않아 t 가 정직해진다. 건수는 stride 배로 줄어든다.
        keep = np.zeros(T, dtype=bool)
        keep[a.stride_offset % a.stride::a.stride] = True
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

    line_taker = FEE_TAKER_RT + 2.0 * a.slip
    line_maker = FEE_MAKER_RT

    if a.rescore:
        # --- 비용선 재채점 -------------------------------------------------
        # 종전 단일선 0.11% 는 체결 방식을 특정하지 않은 값이었다. 두 선으로 나눠
        # 다시 매긴다. 판정 규칙은 CLAUDE.md 원칙 0 보강 2 그대로:
        #   평균과 심볼중앙값이 **둘 다** 선 위 + 시각t·심볼t 가 **둘 다** 2 이상.
        # 어느 하나라도 못 넘으면 그 선은 통과가 아니다.
        def verdict(m, med, tt, ts, line):
            if min(m, med) <= line:
                return False
            return abs(tt) >= 2.0 and abs(ts) >= 2.0

        print(f"[재채점] 보수선(taker/taker) {line_taker:.3f}%"
              f" = 수수료 {FEE_TAKER_RT:.2f} + 슬리피지 {a.slip:.3f}x2"
              f"   /   e3선(maker/maker) {line_maker:.3f}% = 수수료만")
        print("판정: ○○ 두 선 다 통과(무조건 채택) / ·○ e3선만 통과(**조건부** — "
              "진입·청산을 지정가로 만들 수 있으면 산다) / ·· 기각")
        print(f"{'신호':<30}{'지평':>10}{'건당|심볼중앙':>24}{'t(시각/심볼)':>16}"
              f"{'최악위상':>9}{'판정':>8}")
        hit_c = hit_b = 0
        # stride 를 걸었다면 위상 0 만 보지 않는다. 전 위상을 돌려 **평균으로 판정**하고
        # **최악 위상**도 같이 찍는다(커밋 792034c: 위상은 숨은 자유도다).
        phases = range(a.stride) if a.stride > 1 else [0]
        for name in SIG_FULL:
            for h in hor:
                rs = []
                for off in phases:
                    if a.stride > 1:
                        keep = np.zeros(T, dtype=bool)
                        keep[off::a.stride] = True
                        sg = np.where(keep[:, None], SIG_FULL[name], 0).astype(np.int8)
                    else:
                        sg = SIG_FULL[name]
                    r = stats(sg, FN[h])
                    if r is not None:
                        rs.append(r)
                if not rs:
                    continue
                m = float(np.mean([r[1] for r in rs]))
                med = float(np.nanmean([r[2] for r in rs]))
                tt = float(np.mean([r[3] for r in rs]))
                ts = float(np.mean([r[4] for r in rs]))
                worst = min(r[1] for r in rs)
                ok_t = verdict(m, med, tt, ts, line_taker)
                ok_m = verdict(m, med, tt, ts, line_maker)
                if not ok_m:
                    continue          # 두 선 다 못 넘으면 표에 싣지 않는다(기존 기각 유지)
                mark = "○○" if ok_t else "·○"
                hit_b += ok_t
                hit_c += (ok_m and not ok_t)
                mm = h * bar_min
                hl = f"{mm}분" if mm < 120 else f"{mm / 60:g}시간"
                wf = f"{worst:+.3f}" if len(phases) > 1 else "  -  "
                print(f"{name:<30}{hl:>10}{m:+11.4f}|{med:+9.4f}"
                      f"{tt:+8.1f}/{ts:+6.1f}{wf:>9}{mark:>8}")
        if hit_b == 0 and hit_c == 0:
            print("  (통과한 신호x지평이 하나도 없다 — 비용선을 내려도 기각 목록은 그대로다)")
        else:
            print(f"  -> 무조건 채택 {hit_b}건 / 조건부(maker 필요) {hit_c}건")
        print()

    print(f"비용선: 보수(taker/taker) {line_taker:.3f}% / e3(maker/maker) {line_maker:.3f}%. "
          f"슬리피지 {a.slip:.3f}%(편도)는 **실측 불가라 가정**이며 taker 선에만 붙는다.")
    print("표기 = 건당평균|심볼중앙값 (시각클러스터 t / 심볼클러스터 t).")
    print("판정 규칙: 평균과 중앙값이 같은 부호로 붙어 있고 두 t 가 모두 살아 있어야 엣지로 본다. "
          "평균만 크고 중앙값이 0 근처면 소수 급등종목의 꼬리이지 반복 가능한 엣지가 아니다.")


if __name__ == "__main__":
    main()
