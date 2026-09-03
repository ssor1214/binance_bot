"""CM 진입축의 '순수 방향성 엣지' 측정.

청산규칙·슬롯경쟁·수수료를 전부 빼고, **신호봉 마감 -> 다음 봉 시가 진입** 후
N봉 뒤 종가까지의 선행수익률만 본다. 청산규칙과 동어반복이 되는 승률 대신
건당 평균 선행수익률과 t값으로 판정한다. (lookahead 없음: 진입은 항상 다음 봉 시가)
"""
import sys, pathlib, statistics as st

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import cm_backtest as B
import json

raw = json.loads(B.CACHE.read_text(encoding="utf-8"))
data = {s: B.prep(s, b3, raw["bars4h"][s]) for s, b3 in raw["bars3m"].items()}
HOR = [1, 2, 5, 10, 20, 40]


def measure(name, keep):
    buckets = {h: [] for h in HOR}
    for S in data.values():
        n = S["n"]
        for i in range(260, n - max(HOR) - 1):
            sig = B.signal_at(S, i)
            if sig is None:
                continue
            if not keep(S, i, sig):
                continue
            e = S["o"][i + 1]
            if e <= 0:
                continue
            sgn = 1 if sig == "LONG" else -1
            for h in HOR:
                buckets[h].append(sgn * (S["c"][i + 1 + h] / e - 1) * 100)
    out = []
    for h in HOR:
        v = buckets[h]
        if len(v) < 30:
            out.append("      -")
            continue
        m = st.mean(v)
        t = m / (st.pstdev(v) / len(v) ** 0.5)
        out.append("%+.4f(t%+.1f)" % (m, t))
    print("%-28s n=%-7d %s" % (name, len(buckets[HOR[0]]), "  ".join(out)))


def htf_ok(S, i, sig):
    u = S["htf"][i]
    return u is not None and u == (sig == "LONG")


def flip(S, i, sig, mx):
    fa = B.flip_age(S, i, sig == "LONG")
    return fa is not None and fa <= mx


print("선행수익률(%%) — 봉수:", HOR, "(3분봉)\n")
measure("CM신호 전체", lambda S, i, s: True)
measure("CM + 4h EMA200 정합", htf_ok)
measure("CM + HTF + flip<=5 (라이브)", lambda S, i, s: htf_ok(S, i, s) and flip(S, i, s, 5))
measure("CM + HTF + flip<=1", lambda S, i, s: htf_ok(S, i, s) and flip(S, i, s, 1))
measure("CM + HTF + flip<=2", lambda S, i, s: htf_ok(S, i, s) and flip(S, i, s, 2))
measure("CM + HTF + flip<=10", lambda S, i, s: htf_ok(S, i, s) and flip(S, i, s, 10))
measure("CM + HTF역행", lambda S, i, s: S["htf"][i] is not None and not htf_ok(S, i, s))
measure("CM LONG only", lambda S, i, s: s == "LONG")
measure("CM SHORT only", lambda S, i, s: s == "SHORT")

# ---- 드리프트 중립화: 같은 시각 84심볼의 횡단면 평균 수익률을 뺀다 ----
print("\n[드리프트 중립] 같은 시각 전체 심볼 평균 선행수익률을 차감한 값\n")
syms = list(data)
N = min(data[s]["n"] for s in syms)
mkt = {h: [0.0] * N for h in HOR}
for h in HOR:
    for i in range(0, N - h - 1):
        acc = k = 0
        for s in syms:
            S = data[s]
            e = S["o"][i + 1]
            if e > 0:
                acc += (S["c"][i + 1 + h] / e - 1) * 100
                k += 1
        mkt[h][i] = acc / k if k else 0.0


def measure_n(name, keep):
    buckets = {h: [] for h in HOR}
    for S in data.values():
        for i in range(260, N - max(HOR) - 1):
            sig = B.signal_at(S, i)
            if sig is None or not keep(S, i, sig):
                continue
            e = S["o"][i + 1]
            if e <= 0:
                continue
            sgn = 1 if sig == "LONG" else -1
            for h in HOR:
                buckets[h].append(sgn * ((S["c"][i + 1 + h] / e - 1) * 100 - mkt[h][i]))
    out = []
    nl = 0
    for h in HOR:
        v = buckets[h]
        m = st.mean(v)
        t = m / (st.pstdev(v) / len(v) ** 0.5)
        out.append("%+.4f(t%+.1f)" % (m, t))
    print("%-28s n=%-7d %s" % (name, len(buckets[HOR[0]]), "  ".join(out)))


measure_n("CM신호 전체", lambda S, i, s: True)
measure_n("CM + 4h EMA200 정합", htf_ok)
measure_n("CM + HTF + flip<=5 (라이브)", lambda S, i, s: htf_ok(S, i, s) and flip(S, i, s, 5))
measure_n("CM + HTF + flip<=2", lambda S, i, s: htf_ok(S, i, s) and flip(S, i, s, 2))
measure_n("CM + HTF역행", lambda S, i, s: S["htf"][i] is not None and not htf_ok(S, i, s))
measure_n("CM LONG only", lambda S, i, s: s == "LONG")
measure_n("CM SHORT only", lambda S, i, s: s == "SHORT")

# 롱/숏 구성비
cl = cs = 0
for S in data.values():
    for i in range(260, N - 41):
        sg = B.signal_at(S, i)
        if sg and htf_ok(S, i, sg) and flip(S, i, sg, 5):
            cl += sg == "LONG"
            cs += sg == "SHORT"
print("\n라이브 필터 통과 신호 구성: LONG %d (%.1f%%) / SHORT %d" % (cl, 100 * cl / (cl + cs), cs))
