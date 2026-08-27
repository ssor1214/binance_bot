# -*- coding: utf-8 -*-
"""안 1 — 그림자 A/B 를 **진입 비교 전용**으로 쓴다.

dry-run 은 청산이 라이브와 다르므로(거래소 주문 없음) 손익은 비교하지 않는다.
여기서 보는 것은 원칙 1 판정에 필요한 것뿐이다: 시간당 진입수, 스킵 구성, 잡은 신호.
"""
import json, io, re, sys, time
from collections import Counter

def entries(path, pid_filter=None):
    """런로그에서 진입 시각 목록."""
    pat = re.compile(r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d) \[e3\] pid=(\d+) 진입 (\S+) (LONG|SHORT)")
    out = []
    for ln in io.open(path, encoding="utf-8", errors="replace"):
        m = pat.search(ln)
        if m:
            ts = time.mktime(time.strptime(m.group(1), "%Y-%m-%d %H:%M:%S"))
            out.append((ts, m.group(3), m.group(4)))
    return out

def skips(path):
    """마지막 스킵누적 줄을 사유별로 파싱."""
    last = None
    for ln in io.open(path, encoding="utf-8", errors="replace"):
        if "스킵누적" in ln: last = ln
    if not last: return {}, 0
    m = re.search(r"진입(\d+) 스킵(\d+) .*\[(.*?)\]", last)
    if not m: return {}, 0
    d = {}
    for tok in m.group(3).split():
        mm = re.match(r"([^\d]+)(\d+)$", tok)
        if mm: d[mm.group(1)] = int(mm.group(2))
    return d, int(m.group(1))

if __name__ == "__main__":
    since = time.mktime(time.strptime(sys.argv[1] if len(sys.argv) > 1
                                      else "2026-08-27 16:47", "%Y-%m-%d %H:%M"))
    now = time.time(); hrs = (now - since) / 3600
    print("=" * 78)
    print(f"[진입 비교 — 원칙 1 판정용]  기준 {time.strftime('%H:%M', time.localtime(since))} 이후 {hrs:.2f}시간")
    print("  ※ 손익/승률은 비교하지 않는다. dry-run 은 청산 구조가 다르다.")
    print("=" * 78)
    rows = []
    for tag, log in (("라이브 (전환필터 2)", "logs/scalp_bot_e3_cm_run.log"),
                     ("그림자 (전환필터 없음)", "logs/scalp_bot_e3_cm_run_shadow.log")):
        e = [x for x in entries(log) if x[0] >= since]
        sk, n_in = skips(log)
        rows.append((tag, e, sk))
        sides = Counter(x[2] for x in e)
        print(f"{tag:<24}진입 {len(e):>3}건  시간당 {len(e)/hrs:>5.1f}건  "
              f"LONG {sides['LONG']:>2} / SHORT {sides['SHORT']:>2}")
    print()
    print("스킵 구성(누적):")
    keys = sorted({k for _, _, sk in rows for k in sk}, key=lambda k: -max(sk.get(k, 0) for _, _, sk in rows))
    print(f"  {'사유':<16}" + "".join(f"{r[0][:10]:>12}" for r in rows))
    for k in keys:
        print(f"  {k:<16}" + "".join(f"{r[2].get(k,0):>12}" for r in rows))
    print()
    a = {(x[1], x[2]) for x in rows[0][1]}
    b = {(x[1], x[2]) for x in rows[1][1]}
    print(f"두 인스턴스가 **같이** 잡은 심볼·방향: {len(a & b)}개")
    print(f"  라이브만: {sorted(a - b)[:8]}")
    print(f"  그림자만: {sorted(b - a)[:8]}")
