"""[분석전용] 순환매매 강제익절 momentum_continuing 연기 사례들의 실제 가격 움직임 확인.
공개 klines 엔드포인트만 사용 (API 키 불필요), 0.4초 스로틀 + 20콜마다 추가 휴식.
IP밴 재발 방지 최우선. 라이브 코드는 절대 건드리지 않음 - 순수 조회/분석.
"""
import time
import datetime
import requests

BASE = "https://fapi.binance.com/fapi/v1/klines"

CASES = [
    # (symbol, defer_start_KST_str, confirm_KST_str, note) -- log timestamps are KST (UTC+9)
    ("VELVETUSDT", "2026-08-14 13:37:00", "2026-08-14 13:42:00", "사용자 언급과 유사 - 큰 폭 되돌림"),
    ("VELVETUSDT", "2026-08-15 10:03:00", "2026-08-15 10:08:00", "사용자가 실제 지적한 건 (즉시확정, held6.1min roe1.61%)"),
    ("TUTUSDT", "2026-08-14 09:25:00", "2026-08-14 09:29:00", "연기 중 되돌림, 확정ROE < 초기DEFER ROE"),
    ("AVAAIUSDT", "2026-08-14 10:36:00", "2026-08-14 10:40:00", "연기 후 거래소 직접종료 pnl=-0.31% (역전)"),
    ("DOSUSDT", "2026-08-13 12:41:00", "2026-08-13 12:46:00", "소폭 등락, 개선 미미"),
]

def fetch(symbol, start_dt, end_dt):
    start_ms = int(start_dt.timestamp() * 1000)
    end_ms = int(end_dt.timestamp() * 1000)
    r = requests.get(BASE, params={
        "symbol": symbol, "interval": "1m",
        "startTime": start_ms, "endTime": end_ms, "limit": 100,
    }, timeout=10)
    r.raise_for_status()
    return r.json()

def main():
    n = 0
    for symbol, s, e, note in CASES:
        kst = datetime.timezone(datetime.timedelta(hours=9))
        sdt = datetime.datetime.strptime(s, "%Y-%m-%d %H:%M:%S").replace(tzinfo=kst)
        edt = datetime.datetime.strptime(e, "%Y-%m-%d %H:%M:%S").replace(tzinfo=kst)
        try:
            kl = fetch(symbol, sdt, edt)
        except Exception as ex:
            print(f"[{symbol}] fetch fail: {ex}")
            time.sleep(0.5)
            continue
        print(f"\n=== {symbol} {s}~{e} ({note}) ===")
        for k in kl:
            ot_utc = datetime.datetime.utcfromtimestamp(k[0] / 1000)
            ot_kst = ot_utc + datetime.timedelta(hours=9)
            ot = ot_kst.strftime("%H:%M") + "KST"
            o, h, l, c = float(k[1]), float(k[2]), float(k[3]), float(k[4])
            print(f"  {ot} O={o:.6g} H={h:.6g} L={l:.6g} C={c:.6g}")
        n += 1
        time.sleep(0.4)
        if n % 20 == 0:
            time.sleep(3)

if __name__ == "__main__":
    main()
