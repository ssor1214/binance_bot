# -*- coding: utf-8 -*-
"""CM 기반 손절선 후보 비교. 1분봉으로 TP/SL 중 무엇이 먼저 닿는지 실제로 걸어본다.

원칙:
  - 신호는 3분봉 CM(HullMA20, smoothe=2) + 4시간 EMA200 필터(현 라이브와 동일)
  - 진입은 신호 봉 **다음 봉 시가**(lookahead 차단)
  - 익절은 현행 그대로: CM 스윙 극값 - pullback 0.5%, 상한 ROE 4.3
  - 손절만 후보별로 바꾼다
  - 같은 1분봉에서 TP/SL 이 둘 다 닿으면 **SL 우선**(보수적)
  - 수수료 실측: 익절 왕복 0.0276% / 손절 왕복 0.0586% / 시간만료(시장가) 0.0586%
"""
import json, sys, math, statistics as st
sys.path.insert(0, ".")
from scripts.scalp_bot_e3 import _series_ma, ema_last

SP = sys.argv[1]
LEV = 5
TP_PULLBACK = 0.5
TP_MAX_ROE = 4.3
HORIZON_MIN = 60
FEE_TP = 0.0276
FEE_SL = 0.0586

def resample(bars, m):
    if m <= 1: return bars
    out = []; cur = None
    for t, o, h, l, c, v in bars:
        b = (t // 60000) // m * m * 60000
        if cur is None or cur[0] != b:
            if cur is not None and cur[6] == m: out.append(cur[:6])
            cur = [b, o, h, l, c, v, 1]
        else:
            cur[2] = max(cur[2], h); cur[3] = min(cur[3], l); cur[4] = c
            cur[5] += v; cur[6] += 1
    if cur is not None and cur[6] == m: out.append(cur[:6])
    return out

def hull_series(bars, length=20):
    close = [b[4] for b in bars]; vol = [b[5] for b in bars]
    return _series_ma(close, vol, length, 4, 7, second=False)

def leg_start(hs, i, up, smoothe=2, floor_i=0):
    """i 번째 봉 기준, 현재 방향이 시작된 봉 인덱스를 되짚는다(indicators() 와 동일 규칙).

    floor_i: 라이브가 보는 창의 시작. 그보다 앞으로는 되짚지 않는다.
    """
    k = i
    while k > max(smoothe + 1, floor_i):
        a, b = hs[k - 1], hs[k - 1 - smoothe]
        if a is None or b is None: break
        if (a >= b) if up else (a < b):
            k -= 1
        else:
            break
    return max(floor_i, k)

def htf_flags(bars, bb):
    hb = resample(bars, 240)
    if len(hb) < 5: return None
    hc = [b[4] for b in hb]; out = [None] * len(bb); k = 0
    for i, b in enumerate(bb):
        while k + 1 < len(hb) and hb[k + 1][0] <= b[0]: k += 1
        seg = hc[:k + 1]
        out[i] = (seg[-1] > ema_last(seg, min(200, len(seg)))) if len(seg) >= 3 else None
    return out

def cap_roe(entry, px, side, max_roe):
    """px 를 ROE 상한 안으로 자른다."""
    if max_roe <= 0: return px
    roe = ((px / entry - 1) if side == "LONG" else (1 - px / entry)) * LEV * 100
    if roe <= max_roe: return px
    d = max_roe / 100.0 / LEV
    return entry * (1 + d) if side == "LONG" else entry * (1 - d)

RR = 2.0
FEE_RT = 0.0007          # --roundtrip-fee-rate 실제값 (cfg.fee_rate_roundtrip)
MIN_NET_TP = 0.0002       # --min-net-tp-rate


def bb_bands(closes, i):
    w = closes[i - 19:i + 1]
    mu = sum(w) / 20
    sd = (sum((x - mu) ** 2 for x in w) / 20) ** 0.5
    return mu + 2 * sd, mu - 2 * sd


def bb_tp(entry, side, closes, i):
    """봇의 tp_bb 재현: 볼밴선이 수수료 후 플러스가 아니면 0(비활성)."""
    up, lo = bb_bands(closes, i)
    px = up if side == "LONG" else lo
    if entry <= 0 or px <= 0: return 0.0
    gross = (px / entry - 1.0) if side == "LONG" else (1.0 - px / entry)
    if gross <= FEE_RT + MIN_NET_TP:
        return 0.0
    return px          # tp_extra/tp_floor 기본 0 이라 그대로


def rr_tp(entry, stop, side):
    """봇의 tp_rr 재현: 손절선에서 역산. **CM 익절선이 무효일 때 폴백으로 쓰인다.**"""
    risk = abs(entry - stop) / entry
    if risk <= 0: return 0.0
    g = RR * risk + (RR + 1.0) * FEE_RT
    return entry * (1 + g) if side == "LONG" else entry * (1 - g)


def stop_candidates(bars3, i, ls_i, entry, side, ema25):
    """후보별 손절가. ls_i = 레그 시작 봉 인덱스."""
    seg_lo = min(b[3] for b in bars3[ls_i:i + 1])
    seg_hi = max(b[2] for b in bars3[ls_i:i + 1])
    hull = None
    out = {}
    # 0) 현행: EMA25 를 1.65 배로 넓힘 (--stop-widen-pct 0.65), 상한 없음
    for w in (1.65, 2.0, 2.5, 3.0):
        nm = "현행 EMA25x1.65" if w == 1.65 else f"EMA25 x{w}"
        out[nm] = (entry - (entry - ema25) * w if side == "LONG"
                   else entry + (ema25 - entry) * w)
    # 1) CM-A: 레그 반대쪽 극값 (익절선과 완전 대칭)
    out["CM-A 레그 극값"] = seg_lo if side == "LONG" else seg_hi
    # 2) CM-B: 레그 극값 + 0.2% 여유 (극값 정확히 걸면 스치고 털린다)
    out["CM-B 레그극값-0.2%"] = (seg_lo * 0.998 if side == "LONG" else seg_hi * 1.002)
    # 3) CM-C: 레그 극값을 ROE 8% 로 자름
    out["CM-C 레그극값 상한8"] = cap_roe(entry, out["CM-B 레그극값-0.2%"], side, 8.0)
    # --- 대조군: 구조와 무관한 고정 폭. CM-B 가 좋다면 그게 "CM 구조" 때문인지
    #     "그냥 손절이 넓어서"인지 갈라야 한다. 같은 폭의 고정 손절이 같은 성적을
    #     내면 CM 구조는 기여가 없는 것이다.
    for r in (3.0, 4.3, 6.0, 8.0):
        d = r / 100.0 / 5
        out[f"[대조] 고정 ROE {r}"] = entry * (1 - d) if side == "LONG" else entry * (1 + d)
    return out, seg_lo, seg_hi

PULLBACK_WAIT_MIN = 60      # 눌림 대기 한도(분). 넘으면 미체결로 보고 버린다.

# [2026-08-27] **라이브가 실제로 보는 창에 맞춘다.**
# 봇의 signal_bars 는 klines_limit_for_tf(3)=123 개 1분봉만 받고, 3분봉으로 합치면
# 41봉이다. indicators() 는 그 41봉만 보고 레그 시작점 / 스윙 극값 / EMA 를 계산한다.
# 하네스가 전체 히스토리를 보면 레그가 더 길게 잡혀 익절선이 달라지고, 그 결과
# 볼밴 청산 비중이 라이브 1.5% vs 하네스 23% 로 15배 어긋났다.
WIN_BARS = 41


def walk_from(m1, k, entry, side, tp, sl, bb=0.0, horizon=HORIZON_MIN,
              gb_arm=0.0, gb_frac=0.0):
    """1분봉을 걸어 청산을 찾는다.

    봇의 실제 우선순위를 재현한다:
      - 거래소 지정가 TP(tp)와 거래소 손절(sl)은 **봉 내에서** 먼저 체결될 수 있다.
      - 볼밴 익절(bb)은 **폴링**이라 봉 종가 기준으로만 발동한다(마크가격 폴링).
      - 같은 봉에서 TP/SL 이 둘 다 닿으면 SL 우선(보수적).
    """
    if k is None: return None
    L = side == "LONG"
    peak = 0.0            # MFE(ROE%) — 라이브와 같이 **폴링(봉 종가)** 기준으로만 갱신
    for j in range(k, min(k + horizon, len(m1))):
        hi, lo, c = m1[j][2], m1[j][3], m1[j][4]
        if (lo <= sl) if L else (hi >= sl):
            g = ((sl / entry - 1) if L else (1 - sl / entry)) * 100
            return g - FEE_SL, "SL"
        if (hi >= tp) if L else (lo <= tp):
            g = ((tp / entry - 1) if L else (1 - tp / entry)) * 100
            return g - FEE_TP, "TP"
        if bb > 0 and ((c >= bb) if L else (c <= bb)):
            g = ((c / entry - 1) if L else (1 - c / entry)) * 100
            return g - FEE_SL, "BB"      # 폴링 청산은 시장가
        # --- 안 2: 유리구간 되돌림 청산 (원칙 2 보강) ---
        # 거래소 주문(TP/SL)이 봉 안에서 먼저 체결되므로 그 뒤에 본다.
        # MFE 는 폴링과 같이 **봉 종가**로만 갱신한다(라이브 재현).
        roe_c = ((c / entry - 1) if L else (1 - c / entry)) * LEV * 100
        if roe_c > peak: peak = roe_c
        if gb_arm > 0 and peak >= gb_arm and roe_c <= peak * (1 - gb_frac):
            return roe_c / LEV - FEE_SL, "GB"
    j = min(k + horizon, len(m1)) - 1
    c = m1[j][4]
    g = ((c / entry - 1) if L else (1 - c / entry)) * 100
    return g - FEE_SL, "TIME"

def run(raw, bb_exit=True, gate="bb", stop_name="현행 EMA25x1.65",
        gb_arm=0.0, gb_frac=0.0, horizon=HORIZON_MIN, flip_max=None, gb_side=None):
    """flip_max: HullMA 방향 전환 후 이 봉수 이내만 받는다(None=제한없음)."""
    """bb_exit: 볼밴 폴링 익절 사용 여부
       gate: 진입 게이트 — "bb"(현행, 볼밴 익절선 통과 시 스킵) / "cm" / "none"
    """
    net, why, rr, tw_l, sw_l, n_gate = [], [], [], [], [], 0
    sides = []
    for sym, bars in raw.items():
        b3 = resample(bars, 3)
        if len(b3) < 60: continue
        hs = hull_series(b3)
        h4 = htf_flags(bars, b3)
        idx = {b[0]: k for k, b in enumerate(bars)}
        closes = [b[4] for b in b3]
        for i in range(40, len(b3) - 1):
            a = hs[i]
            if a is None or a <= 0: continue
            up = hs[i] >= (hs[i - 2] if hs[i - 2] else a)
            c = closes[i]
            side = "LONG" if (up and c > a) else "SHORT" if (not up and c < a) else None
            if side is None: continue
            if h4 is None or h4[i] is None or (side == "LONG") != h4[i]: continue
            if flip_max is not None:
                # 방향 전환 후 몇 봉째인가 (0 = 전환 봉 자신)
                _age = 0; _j = i
                while _j > 0:
                    _pu = (hs[_j-1] >= hs[_j-3]) if (_j >= 3 and hs[_j-1] and hs[_j-3]) else None
                    if _pu is None or _pu != up: break
                    _age += 1; _j -= 1
                    if _age > flip_max: break
                if _age > flip_max: continue
            wi = max(0, i - (WIN_BARS - 1))
            ema5 = ema_last(closes[wi:i + 1], 5)
            k0 = idx.get(b3[i + 1][0])
            if k0 is None: continue
            entry = 0.0; k_in = None
            for j in range(k0, min(k0 + PULLBACK_WAIT_MIN, len(bars))):
                if (bars[j][3] <= ema5) if side == "LONG" else (bars[j][2] >= ema5):
                    entry = ema5; k_in = j; break
            if entry <= 0: continue
            t_fill = bars[k_in][0]
            i2 = i
            while i2 + 1 < len(b3) and b3[i2 + 1][0] <= t_fill: i2 += 1
            if hs[i2] is None or hs[i2] <= 0: continue
            w0 = max(0, i2 - (WIN_BARS - 1))          # 라이브가 보는 41봉 창
            ls_i = leg_start(hs, i2, side == "LONG", floor_i=w0)
            ema25 = ema_last(closes[w0:i2 + 1], 25)
            cands, seg_lo, seg_hi = stop_candidates(b3, i2, ls_i, entry, side, ema25)
            # [편향 제거] 어느 후보를 쓰든 **현행 손절이 유효한 거래만** 센다.
            # 그러지 않으면 넓은 손절 쪽에만 11,480건이 더 들어가 비교가 무의미해진다.
            _cur = cands.get("현행 EMA25x1.65")
            if _cur is None or ((_cur >= entry) if side == "LONG" else (_cur <= entry)):
                continue
            if stop_name == "없음":
                sl = (entry * 0.001) if side == "LONG" else (entry * 1000)   # 사실상 무손절
            else:
                sl = cands.get(stop_name)
            if sl is None: continue
            if (sl >= entry) if side == "LONG" else (sl <= entry): continue

            bbp = bb_tp(entry, side, closes, i2)
            # --- 진입 게이트 ---
            if gate == "bb":
                # 현행: 볼밴 익절선을 이미 넘었으면 진입 자체를 버린다(e2 잔재)
                if bbp and ((entry >= bbp) if side == "LONG" else (entry <= bbp)):
                    n_gate += 1; continue
            # --- 익절선 결정: CM 우선, 무효면 손익비 폴백(봇과 동일) ---
            tgt = seg_hi if side == "LONG" else seg_lo
            tp = tgt * (1 - TP_PULLBACK / 100) if side == "LONG" else tgt * (1 + TP_PULLBACK / 100)
            tp = cap_roe(entry, tp, side, TP_MAX_ROE)
            cm_ok = (tp > entry) if side == "LONG" else (tp < entry)
            if not cm_ok:
                if gate == "cm":
                    n_gate += 1; continue          # CM 무효면 진입 자체를 버린다
                tp = cap_roe(entry, rr_tp(entry, sl, side), side, TP_MAX_ROE)
                if (tp <= entry) if side == "LONG" else (tp >= entry): continue
            _ga = gb_arm if (gb_side in (None, side)) else 0.0
            r = walk_from(bars, k_in, entry, side, tp, sl, bbp if bb_exit else 0.0,
                          gb_arm=_ga, gb_frac=gb_frac, horizon=horizon)
            if r is None: continue
            g, w = r
            net.append(g); why.append(w); sides.append(side)
            twv = abs(tp - entry) / entry; swv = abs(entry - sl) / entry
            rr.append(twv / swv if swv > 0 else 0)
            tw_l.append(twv * LEV * 100); sw_l.append(swv * LEV * 100)
    return {"net": net, "why": why, "rr": rr, "tw": tw_l, "sw": sw_l, "gate": n_gate, "side": sides}


def report(nm, d):
    g = d["net"]
    if len(g) < 50:
        print(f"{nm:<30} 표본부족 n={len(g)}"); return
    m = sum(g) / len(g); sd = st.pstdev(g)
    t = m / (sd / math.sqrt(len(g))) if sd > 0 else 0
    n = len(g); wh = d["why"]
    w = sum(1 for x in g if x > 0) / n * 100
    print(f"{nm:<30}{n:>7}{m:>+10.4f}%{t:>+8.2f}{w:>7.1f}%"
          f"{wh.count('TP')/n*100:>6.0f}%{wh.count('SL')/n*100:>6.0f}%"
          f"{wh.count('GB')/n*100:>6.0f}%{wh.count('TIME')/n*100:>6.0f}%"
          f"{st.median(d['tw']):>9.2f}%{d['gate']:>8}")


def rep_side(nm, d, want):
    idx=[k for k,x in enumerate(d["side"]) if x==want]
    g=[d["net"][k] for k in idx]; wh=[d["why"][k] for k in idx]
    if len(g)<50: print(f"{nm:<34}{len(g):>7}  표본부족"); return
    m=sum(g)/len(g); sd=st.pstdev(g); t=m/(sd/math.sqrt(len(g))) if sd>0 else 0
    n=len(g)
    print(f"{nm:<34}{n:>7}{m:>+10.4f}%{t:>+8.2f}{sum(1 for x in g if x>0)/n*100:>7.1f}%"
          f"{wh.count('TP')/n*100:>6.0f}%{wh.count('SL')/n*100:>6.0f}%{wh.count('GB')/n*100:>6.0f}%{wh.count('TIME')/n*100:>6.0f}%")

if __name__ == "__main__":
    RAW = json.load(open(SP + "/klines_1m.json"))
    S = "[대조] 고정 ROE 8.0"
    print("=" * 110)
    print("[SHORT 전용 되돌림 청산 검토] 3일 x 85심볼 / 전환필터 5봉 / 고정손절 8.0 / 4h필터")
    print("=" * 110)
    print(f"{'구성':<34}{'건수':>7}{'순익/건':>10}{'t':>8}{'승률':>8}{'TP':>6}{'SL':>6}{'GB':>6}{'만료':>6}")
    print("-" * 110)
    base = run(RAW, False, "bb", stop_name=S, flip_max=5)
    both = run(RAW, False, "bb", stop_name=S, flip_max=5, gb_arm=1.0, gb_frac=0.4)
    only = run(RAW, False, "bb", stop_name=S, flip_max=5, gb_arm=1.0, gb_frac=0.4, gb_side="SHORT")
    for want in ("LONG","SHORT"):
        print(f"── {want} ──")
        rep_side("  되돌림 없음(현재)", base, want)
        rep_side("  되돌림 양방향 arm1.0/0.4", both, want)
        rep_side("  되돌림 SHORT만 arm1.0/0.4", only, want)
    print("-" * 110)
    report("전체 — 되돌림 없음(현재)", base)
    report("전체 — 되돌림 양방향", both)
    report("전체 — 되돌림 SHORT만", only)
