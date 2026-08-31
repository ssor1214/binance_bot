"""호가·주문흐름 기록기 — 지금 시작해야 몇 달 뒤에 잴 수 있는 유일한 축.

HANDOFF_2026-08-31 10장 결론: 파라미터·신호뼈대·시간대·가격외데이터 네 축이 모두
비용선 미달로 닫혔다. 남은 미탐색은 **호가(top of book)와 주문흐름**인데,
바이낸스 공개 덤프에 bookTicker 이력이 아예 없고 aggTrades 는 심볼당 하루 31MB 라
규모상 못 받는다. **과거를 살 수 없으므로 지금부터 쌓는 수밖에 없다.**

무엇을 남기나 (5초 버킷, 심볼별 한 줄):
    bid/ask 와 각 잔량      -> 스프레드, 호가 불균형(top-of-book imbalance)
    taker 매수/매도 체결량   -> 주문흐름 불균형, 체결 강도
    체결 건수, 마지막 가격

설계 메모:
- **python-binance 를 쓰지 않는다.** 내부 read-loop 가 얼어붙는 기존 버그가 있고
  (memory: 웹소켓 read-loop freeze), 이 프로세스는 몇 달을 무인으로 돌아야 한다.
  websockets 라이브러리로 직접 붙고, **무응답 감시 -> 재접속**을 명시적으로 넣었다.
- 공개 스트림만 쓴다. **API 키 미사용 / 인증 0회 / 주문 기능 없음.** 라이브 봇과
  자격증명이 겹치지 않으므로 IP 밴 사고([[backtest-ip-ban-incident]])와 무관하다.
- 원장·상태 파일을 건드리지 않는다. 쓰는 곳은 `logs/obook/` 하나뿐이다.
- 하루 단위로 gzip CSV 를 새로 연다. 83심볼 기준 하루 약 20~30MB.

읽을 때는 edge_lab.py 의 판정 장치(드리프트 중립 + 이중 클러스터 t + 중앙값 +
stride=보유봉수)를 그대로 쓸 것. 그게 이 저장소가 지금까지 배운 전부다.
"""
import argparse
import asyncio
import contextlib
import datetime as dt
import gzip
import json
import pathlib
import signal
import sys
import time
from collections import defaultdict

import websockets

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
ROOT = pathlib.Path(__file__).resolve().parent.parent
OUTDIR = ROOT / "logs" / "obook"
WS = "wss://fstream.binance.com/stream?streams="
HEADER = ("ts_ms,symbol,bid,bid_qty,ask,ask_qty,"
          "buy_vol,sell_vol,trades,last_price,d5_bid,d5_ask\n")


class State:
    def __init__(self):
        self.book = {}                       # sym -> (bid, bidq, ask, askq)
        self.depth = {}                      # sym -> (5호가 매수잔량합, 매도잔량합)
        self.flow = defaultdict(lambda: [0.0, 0.0, 0, 0.0])   # buy, sell, n, last
        self.last_msg = time.time()
        self.msgs = 0
        self.rows = 0
        self.reconnects = 0


def say(*a):
    print(dt.datetime.now().strftime("%H:%M:%S"), *a, flush=True)


async def pump(streams, st, stop):
    """한 연결을 유지한다. 무응답이면 스스로 끊고 재접속한다."""
    url = WS + "/".join(streams)
    while not stop.is_set():
        try:
            async with websockets.connect(url, ping_interval=20, ping_timeout=20,
                                          max_queue=4096, close_timeout=5) as ws:
                say(f"연결 ({len(streams)}스트림)")
                while not stop.is_set():
                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=30)
                    except asyncio.TimeoutError:
                        # 30초 무응답 = read-loop 가 죽었다고 보고 재접속한다.
                        say("30초 무응답 -> 재접속")
                        st.reconnects += 1
                        break
                    st.last_msg = time.time()
                    st.msgs += 1
                    m = json.loads(raw)
                    d = m.get("data")
                    if not d:
                        continue
                    e = d.get("e")
                    if e == "bookTicker":
                        st.book[d["s"]] = (d["b"], d["B"], d["a"], d["A"])
                    elif e == "depthUpdate":
                        b = d.get("b") or []
                        aa = d.get("a") or []
                        st.depth[d["s"]] = (
                            sum(float(x[1]) for x in b[:5]),
                            sum(float(x[1]) for x in aa[:5]))
                    elif e in ("trade", "aggTrade"):
                        f = st.flow[d["s"]]
                        q = float(d["q"])
                        # m=True 는 매수자가 메이커 -> taker 가 판 것
                        if d["m"]:
                            f[1] += q
                        else:
                            f[0] += q
                        f[2] += 1
                        f[3] = float(d["p"])
        except asyncio.CancelledError:
            raise
        except Exception as ex:
            if stop.is_set():
                return
            st.reconnects += 1
            say(f"연결 오류 -> 3초 뒤 재접속: {type(ex).__name__} {ex}")
            await asyncio.sleep(3)


def compress_finished(keep_day):
    """마감된 날짜의 평문 CSV 를 gzip 으로 접는다.

    당일 파일을 gzip 으로 열어두면 스트림이 끝나지 않아 **읽을 수가 없다**
    (EOFError: ended before end-of-stream marker). 몇 달을 쌓는 기록기에서
    "오늘 데이터를 못 본다"는 건 치명적이라, 당일은 평문으로 쓰고
    날짜가 바뀔 때 압축한다. 전원이 나가도 평문은 살아남는다.
    """
    for f in sorted(OUTDIR.glob("obook-*.csv")):
        if f.stem.replace("obook-", "") == keep_day:
            continue
        gz = f.with_suffix(".csv.gz")
        try:
            with open(f, "rb") as src, gzip.open(gz, "wb", compresslevel=6) as dst:
                while chunk := src.read(1 << 20):
                    dst.write(chunk)
            f.unlink()
            say(f"압축 완료: {gz.name} ({gz.stat().st_size // 1024}KB)")
        except Exception as ex:
            say(f"압축 실패 {f.name}: {ex}")


async def writer(st, stop, flush_sec):
    day, fh = None, None
    try:
        while not stop.is_set():
            await asyncio.sleep(flush_sec)
            now = dt.datetime.now(dt.timezone.utc)
            d = now.strftime("%Y-%m-%d")
            if d != day:
                if fh:
                    fh.close()
                OUTDIR.mkdir(parents=True, exist_ok=True)
                path = OUTDIR / f"obook-{d}.csv"
                new = not path.exists()
                fh = open(path, "a", encoding="utf-8", newline="")
                if new:
                    fh.write(HEADER)
                day = d
                say(f"기록 파일: {path.name}")
                compress_finished(d)
            ts = int(now.timestamp() * 1000)
            flow, st.flow = st.flow, defaultdict(lambda: [0.0, 0.0, 0, 0.0])
            n = 0
            for sym, bk in st.book.items():
                f = flow.get(sym)
                if not f and sym not in st.book:
                    continue
                buy, sell, cnt, last = f if f else (0.0, 0.0, 0, 0.0)
                dp = st.depth.get(sym, ("", ""))
                fh.write(f"{ts},{sym},{bk[0]},{bk[1]},{bk[2]},{bk[3]},"
                         f"{buy:.8g},{sell:.8g},{cnt},{last:.8g},{dp[0]},{dp[1]}\n")
                n += 1
            fh.flush()
            st.rows += n
    finally:
        if fh:
            fh.close()


async def status(st, stop, every):
    while not stop.is_set():
        await asyncio.sleep(every)
        age = time.time() - st.last_msg
        say(f"메시지 {st.msgs:,} / 기록행 {st.rows:,} / 재접속 {st.reconnects} / "
            f"마지막수신 {age:.0f}초 전 / 심볼 {len(st.book)}")
        (OUTDIR / "heartbeat.txt").write_text(
            f"{time.time():.0f} msgs={st.msgs} rows={st.rows} "
            f"reconnects={st.reconnects} last_msg_age={age:.0f}\n", encoding="utf-8")


async def main_async(a):
    if a.symbols:
        syms = [s.strip().upper() for s in a.symbols.split(",") if s.strip()]
    else:
        cache = json.load(open(ROOT / "logs/ws_worker_cache.json", encoding="utf-8"))
        syms = [s for s in cache["rows_by_symbol"] if s.isascii() and s.endswith("USDT")]
    streams = []
    for s in syms:
        low = s.lower()
        streams += [f"{low}@bookTicker", f"{low}@{a.trade_stream}"]
        if a.depth:
            streams.append(f"{low}@depth5@100ms")
    chunks = [streams[i:i + a.streams_per_conn]
              for i in range(0, len(streams), a.streams_per_conn)]
    say(f"{len(syms)}심볼 / {len(streams)}스트림 / 연결 {len(chunks)}개 / "
        f"{a.flush}초 버킷 -> {OUTDIR}")

    st = State()
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    with contextlib.suppress(NotImplementedError):
        for s in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(s, stop.set)

    tasks = [asyncio.create_task(pump(c, st, stop)) for c in chunks]
    tasks.append(asyncio.create_task(writer(st, stop, a.flush)))
    tasks.append(asyncio.create_task(status(st, stop, a.status_sec)))
    if a.seconds:
        await asyncio.sleep(a.seconds)
        stop.set()
    else:
        # 무한 실행: stop 이 설정될 때까지(SIGINT/SIGTERM) 기다린다.
        # 이 대기를 빼먹으면 곧바로 아래 cancel 로 떨어져 즉시 종료된다.
        await stop.wait()
    for t in tasks:
        t.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    say(f"종료 — 메시지 {st.msgs:,} / 기록행 {st.rows:,} / 재접속 {st.reconnects}")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--symbols", default="", help="쉼표 구분. 비우면 ws_worker_cache 기준")
    p.add_argument("--flush", type=float, default=5.0, help="버킷 길이(초)")
    p.add_argument("--streams-per-conn", type=int, default=80)
    p.add_argument("--status-sec", type=float, default=300.0)
    p.add_argument("--seconds", type=float, default=0, help="N초 뒤 종료(0=무한)")
    p.add_argument("--trade-stream", default="trade",
                   help="체결 스트림 이름. 이 거래소는 aggTrade 를 서빙하지 않아 trade 가 기본이다")
    p.add_argument("--depth", action="store_true",
                   help="5호가 스냅샷도 함께 받는다(메시지량 약 2배)")
    a = p.parse_args()
    try:
        asyncio.run(main_async(a))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
