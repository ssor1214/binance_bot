"""e3(CM) 진입축 백테스트 하네스.

설계 원칙 (CLAUDE.md "pending 경로" 규칙의 정신을 그대로 지킴):
  - 신호는 **3분봉 마감**으로 확정한다. 진입은 **그 다음 봉 이후**의 저/고가로만 판정한다.
  - same-candle-close 진입 없음. 눌림(EMA5) 터치 판정에도 미래 봉을 쓰지 않는다.
  - 지표 함수는 scalp_bot_e3 의 것을 그대로 import 한다(재구현 금지).

한계(반드시 리포트에 명시):
  - 라이브는 10초 폴링으로 **형성 중인 봉**의 EMA5 를 터치 판정한다. 여기서는 3분 마감
    단위라 체결 시점/가격이 최대 3분 어긋난다.
  - 한 봉 안에서 손절선과 익절선을 모두 건드리면 순서를 알 수 없다. 기본은 손절 우선
    (비관적). optimistic 으로 익절 우선 감도도 잰다.
"""
import argparse, json, math, sys, pathlib, statistics as st, collections

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
import importlib.util

_spec = importlib.util.spec_from_file_location(
    "sb_e3", str(pathlib.Path(__file__).resolve().parent / "scalp_bot_e3.py"))
sb = importlib.util.module_from_spec(_spec)
sys.modules["sb_e3"] = sb
_spec.loader.exec_module(sb)

CACHE = pathlib.Path(__file__).resolve().parent.parent / "scratch_cm_klines.json"
LEV = 5
ROUNDTRIP_FEE = 0.0007
MIN_NET_TP = 0.0002


def ema_series(vals, span):
    a = 2.0 / (span + 1.0)
    out, p = [], vals[0]
    for v in vals:
        p = a * v + (1 - a) * p
        out.append(p)
    out[0] = vals[0]
    return out


def prep(sym, b3, b4):
    """심볼 1개의 지표를 전부 선계산한다. 인덱스 i 봉 마감 시점의 정보만 쓴다."""
    t = [r[0] for r in b3]
    o = [r[1] for r in b3]
    h = [r[2] for r in b3]
    lo = [r[3] for r in b3]
    c = [r[4] for r in b3]
    n = len(c)
    hs = sb._series_hma(c, 20)
    e5 = ema_series(c, 5)
    e25 = ema_series(c, 25)
    bbu, bbl = [None] * n, [None] * n
    for i in range(19, n):
        w = c[i - 19:i + 1]
        mu = sum(w) / 20.0
        sd = (sum((x - mu) ** 2 for x in w) / 20.0) ** 0.5
        bbu[i], bbl[i] = mu + 2 * sd, mu - 2 * sd

    def ok(i):
        return i >= 3 and hs[i - 1] is not None and hs[i - 3] is not None

    up_start, dn_start = [0] * n, [0] * n
    cm_up, cm_dn = [0.0] * n, [0.0] * n
    run_hi, run_lo = -1e30, 1e30
    for i in range(n):
        d = ok(i) and (hs[i - 1] >= hs[i - 3])
        up_start[i] = up_start[i - 1] if (d and i > 22) else i
        dn_start[i] = dn_start[i - 1] if ((not d) and ok(i) and i > 22) else i
        run_hi = h[i] if up_start[i] == i else max(run_hi, h[i])
        run_lo = lo[i] if dn_start[i] == i else min(run_lo, lo[i])
        cm_up[i], cm_dn[i] = run_hi, run_lo

    maup = [None] * n
    for i in range(2, n):
        if hs[i] is not None and hs[i - 2] is not None:
            maup[i] = hs[i] >= hs[i - 2]

    c4 = [r[4] for r in b4]
    t4 = [r[0] for r in b4]
    e4 = ema_series(c4, 200)
    htf = [None] * n
    j = 0
    for i in range(n):
        while j + 1 < len(t4) and t4[j + 1] + 4 * 3600000 <= t[i] + 180000:
            j += 1
        if j >= 200 and t4[j] + 4 * 3600000 <= t[i] + 180000:
            htf[i] = c[i] > e4[j]
    return dict(sym=sym, t=t, o=o, h=h, l=lo, c=c, n=n, hs=hs, e5=e5, e25=e25,
                bbu=bbu, bbl=bbl, up_start=up_start, dn_start=dn_start,
                cm_up=cm_up, cm_dn=cm_dn, maup=maup, htf=htf)


def signal_at(S, i):
    mu = S["maup"][i]
    if mu is None or S["hs"][i] is None:
        return None
    if mu and S["c"][i] > S["hs"][i]:
        return "LONG"
    if (not mu) and S["c"][i] < S["hs"][i]:
        return "SHORT"
    return None


def flip_age(S, i, want_up):
    mu = S["maup"][i]
    if mu is None or mu != want_up:
        return None
    return i - (S["up_start"][i] if want_up else S["dn_start"][i])


def run(cfg, data):
    """전 심볼 공통 3분 타임라인 위에서 슬롯 경쟁까지 포함해 시뮬레이션한다."""
    syms = list(data)
    N = min(data[s]["n"] for s in syms)
    warm = 260
    pending = {}
    pos = {}
    cooldown = {}
    trades = []
    for i in range(warm, N):
        # 1) 보유 포지션 청산 판정 (이번 봉의 고/저가)
        for sym in list(pos):
            S, p = data[sym], pos[sym]
            H, L = S["h"][i], S["l"][i]
            hitS = (L <= p["stop"]) if p["side"] == "LONG" else (H >= p["stop"])
            hitT = p["tp"] > 0 and ((H >= p["tp"]) if p["side"] == "LONG" else (L <= p["tp"]))
            why = px = None
            if hitS and hitT:
                why, px = ("TP", p["tp"]) if cfg["optimistic"] else ("STOP", p["stop"])
            elif hitT:
                why, px = "TP", p["tp"]
            elif hitS:
                why, px = "STOP", p["stop"]
            elif cfg["max_hold_bars"] and i - p["i"] >= cfg["max_hold_bars"]:
                why, px = "EXPIRE", S["c"][i]
            if why:
                p.update(exit_px=px, why=why, bars=i - p["i"], exit_i=i)
                trades.append(p)
                del pos[sym]
                cooldown[sym] = i + cfg["cooldown_bars"]

        # 2) 미체결 pending 의 체결 판정 (신호봉 '다음' 봉부터)
        for sym in list(pending):
            pd = pending[sym]
            if pd["since"] >= i:
                continue
            S = data[sym]
            if i - pd["since"] > cfg["pend_expire_bars"]:
                del pending[sym]
                continue
            if sym in pos or len(pos) >= cfg["max_conc"]:
                continue
            if cooldown.get(sym, -1) > i:
                continue
            ss = sum(1 for q in pos.values() if q["side"] == pd["side"])
            if cfg["max_same_side"] and ss >= cfg["max_same_side"]:
                continue
            L = pd["side"] == "LONG"
            tgt = pd["tgt"]
            O, H, LOW = S["o"][i], S["h"][i], S["l"][i]
            if L:
                fill = O if O <= tgt else (tgt if LOW <= tgt else None)
            else:
                fill = O if O >= tgt else (tgt if H >= tgt else None)
            if fill is None:
                continue
            k = pd["since"]
            ind = {"cm_tp_long": S["cm_up"][k], "cm_tp_short": S["cm_dn"][k],
                   "bb_u": S["bbu"][k], "bb_l": S["bbl"][k], "e25": S["e25"][k]}
            if cfg["max_pullback_pct"] > 0:
                pb = sb.pullback_depth_pct(fill, S["hs"][k] or 0.0)
                if pb > cfg["max_pullback_pct"]:
                    del pending[sym]
                    continue
            bb = sb.fee_aware_bb_price(fill, ind["bb_u"] if L else ind["bb_l"],
                                       pd["side"], ROUNDTRIP_FEE, MIN_NET_TP)
            if bb and ((fill >= bb) if L else (fill <= bb)):
                del pending[sym]
                continue
            tp = sb.cm_tp_price(ind, fill, pd["side"], cfg["tp_pullback"],
                                LEV, cfg["tp_max_roe"])
            cm_ok = tp > 0
            if not cm_ok:
                if cfg["require_cm_tp"]:
                    del pending[sym]
                    continue
                d0 = cfg["stop_roe"] / 100.0 / LEV
                stop0 = fill * (1 - d0) if L else fill * (1 + d0)
                rr = sb.fee_aware_rr_price(fill, stop0, pd["side"], 2.0, ROUNDTRIP_FEE)
                tp = sb.cap_tp_roe(fill, rr, pd["side"], LEV, cfg["tp_max_roe"])
                if not (tp > 0 and ((tp > fill) if L else (tp < fill))):
                    tp = 0.0
            d = cfg["stop_roe"] / 100.0 / LEV
            stop = fill * (1 - d) if L else fill * (1 + d)
            pos[sym] = dict(sym=sym, side=pd["side"], entry=fill, stop=stop, tp=tp,
                            i=i, wait=i - pd["since"], cm_ok=cm_ok,
                            sig_close=S["c"][pd["since"]])
            del pending[sym]

        # 3) 신호 판정 (봉 마감)
        for sym in syms:
            S = data[sym]
            if sym in pos:
                pending.pop(sym, None)
                continue
            sig = signal_at(S, i)
            if sig is None:
                pending.pop(sym, None)
                continue
            L = sig == "LONG"
            if cfg["htf"]:
                u = S["htf"][i]
                if u is None or u != L:
                    pending.pop(sym, None)
                    continue
            if cfg["flip_max_bars"] >= 0:
                fa = flip_age(S, i, L)
                if fa is None or fa > cfg["flip_max_bars"]:
                    pending.pop(sym, None)
                    continue
            pd = pending.get(sym)
            if pd and pd["side"] != sig:
                pd = None
            since = pd["since"] if pd else i
            tgt = (math.inf if L else 0.0) if cfg["entry_mode"] == "immediate" else S["e5"][i]
            pending[sym] = {"side": sig, "tgt": tgt, "since": since}
    return trades, N - warm


def report(name, trades, bars, cfg):
    if not trades:
        return dict(name=name, n=0)
    g = []
    for t in trades:
        gr = (t["exit_px"] / t["entry"] - 1) if t["side"] == "LONG" else (1 - t["exit_px"] / t["entry"])
        t["gross"] = gr * 100
        g.append(t["gross"])
    m = st.mean(g)
    se = st.pstdev(g) / len(g) ** 0.5 if len(g) > 1 else float("nan")
    cnt = collections.Counter(t["why"] for t in trades)
    net = []
    for t in trades:
        fee = cfg["fee_entry"] + (cfg["fee_maker"] if t["why"] == "TP" else cfg["fee_taker"])
        slip = cfg["slip"] * (1 if t["why"] == "TP" else 2)
        net.append(t["gross"] - fee - slip)
    return dict(name=name, n=len(trades), edge=m, t=(m / se if se else 0.0),
                net=st.mean(net), win=100 * sum(1 for x in g if x > 0) / len(g),
                tp=100 * cnt["TP"] / len(trades), sl=100 * cnt["STOP"] / len(trades),
                exp=100 * cnt["EXPIRE"] / len(trades),
                hold=st.median([t["bars"] * 3 for t in trades]),
                wait=st.median([t["wait"] * 3 for t in trades]),
                tproe=st.median([t["gross"] * LEV for t in trades if t["why"] == "TP"] or [0]),
                slroe=st.median([t["gross"] * LEV for t in trades if t["why"] == "STOP"] or [0]),
                per_h=len(trades) / (bars * 3 / 60.0))


HDR = "{:<24} {:>6} {:>6} {:>8} {:>7} {:>8} {:>6} {:>6} {:>6} {:>6} {:>6} {:>7} {:>7}"


def line(r):
    if not r.get("n"):
        return "{:<24} {:>6}".format(r["name"], 0)
    return HDR.format(r["name"], r["n"], "%.1f" % r["per_h"], "%.4f" % r["edge"],
                      "%.2f" % r["t"], "%.4f" % r["net"], "%.1f" % r["win"],
                      "%.1f" % r["tp"], "%.1f" % r["sl"], "%.0f" % r["hold"],
                      "%.0f" % r["wait"], "%.2f" % r["tproe"], "%.2f" % r["slroe"])


def header():
    return HDR.format("구성", "거래수", "건/h", "엣지%", "t값", "순엣지%", "승률",
                      "TP%", "SL%", "보유m", "대기m", "TProe", "SLroe")


def base_cfg(**kw):
    c = dict(entry_mode="pullback", flip_max_bars=5, htf=True, tp_max_roe=4.3,
             tp_pullback=0.5, stop_roe=8.0, max_conc=10, max_same_side=3,
             cooldown_bars=2, pend_expire_bars=20, max_hold_bars=0,
             max_pullback_pct=0.0, require_cm_tp=False, optimistic=False,
             fee_entry=0.05, fee_maker=0.02, fee_taker=0.05, slip=0.02)
    c.update(kw)
    return c


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--stage", default="1")
    a = ap.parse_args()
    raw = json.loads(CACHE.read_text(encoding="utf-8"))
    data = {s: prep(s, b3, raw["bars4h"][s]) for s, b3 in raw["bars3m"].items()}
    nb = min(d["n"] for d in data.values())
    print("심볼 %d / 3분봉 %d개 (%.1f일)\n" % (len(data), nb, nb * 3 / 1440))
    if a.stage == "1":
        runs = [("baseline(눌림,flip5)", base_cfg()),
                ("baseline(익절우선 감도)", base_cfg(optimistic=True))]
    elif a.stage == "2":
        runs = [("(a) 눌림대기 baseline", base_cfg()),
                ("(b) 즉시진입 다음봉시가", base_cfg(entry_mode="immediate")),
                ("(c) 눌림만료 2봉", base_cfg(pend_expire_bars=2)),
                ("(c2) 눌림만료 1봉", base_cfg(pend_expire_bars=1)),
                ("(d) 눌림깊이<=0.5%", base_cfg(max_pullback_pct=0.5)),
                ("(d2) 눌림깊이<=0.3%", base_cfg(max_pullback_pct=0.3)),
                ("(e) flip<=1", base_cfg(flip_max_bars=1)),
                ("(e) flip<=2", base_cfg(flip_max_bars=2)),
                ("(e) flip<=3", base_cfg(flip_max_bars=3)),
                ("(e) flip 무제한", base_cfg(flip_max_bars=-1)),
                ("(f) HTF필터 OFF", base_cfg(htf=False)),
                ("(g) require_cm_tp", base_cfg(require_cm_tp=True))]
    else:
        runs = []
        for tp in (4.3, 6, 8, 10, 0):
            for sl in (8.0, 6.0):
                runs.append(("TP%s/SL%s" % (tp or "무제한", sl),
                             base_cfg(tp_max_roe=tp, stop_roe=sl)))
    print(header())
    for nm, cfg in runs:
        tr, bars = run(cfg, data)
        print(line(report(nm, tr, bars, cfg)), flush=True)


if __name__ == "__main__":
    main()
