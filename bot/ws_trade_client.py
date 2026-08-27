"""[2026-08-14] 체결(trade/aggTrade) 스트림 레이어 - 신규/독립 모듈.

메모리 로드맵(orderbook-slippage-tracking-todo)에 따른 다음 단계: 10초 체결량 스파이크
감지 + 부산물로 초단위 캔들 합성을 위한 원본 체결 데이터 수집기.

이 모듈이 절대 하지 않는 것:
- 기존 bot/ws_client.py의 어떤 클래스도 수정하지 않는다.

[2026-08-15 라이브 배선] 아래 두 줄은 원래 "이 모듈이 절대 하지 않는 것"에 있었으나,
스파이크 조기체결 로직(bot/main.py의 early_entry_spike, place_entry_order aggressive=)이
백테스트(scratch_spike_entry_v2~v4)로 충분히 검증되고 사용자가 명시적으로 라이브 연결을
승인해서 의도적으로 깨졌다: bot/ws_trade_worker.py는 이제 bot/main.py의 start_ws_layer()가
cfg.spike_entry_enabled=True일 때 별도 프로세스로 기동한다(기존 시장데이터/계정스트림
워커와 동일한 격리 패턴). 원래의 "절대 연결 안 함" 원칙은 "장시간 테스트넷 검증 + 명시적
사용자 승인 없이는 연결 안 함"이었지, 영원히 연결 금지가 아니었다 — 60분/50심볼 소크테스트,
워치독 kill/freeze 복구, User Data Stream 검증을 모두 통과한 뒤 이 조건이 충족됐다.

설계:
- TradeTickCache: 심볼별 최근 체결(가격/수량/시각/방향)을 ring buffer(deque)로 들고 있는
  스레드 세이프 캐시.
- TradeStreamWebSocket: futures aggTrade 스트림을 구독하는 클래스. MarketDataWebSocket과
  동일한 combined/multiplex 스트림 + 청크 분할 패턴을 재사용했다.
- 순수 함수 detect_volume_spike() / resample_ticks_to_ohlcv(): 네트워크/전역상태와
  완전히 분리된 함수.
- FileBackedSpikeCache: bot/ws_client.py의 FileBackedKlineCache와 동일한 프로세스 격리
  어댑터 패턴. ws_trade_worker.py는 원시 틱을 프로세스 경계 밖으로 내보내지 않고(무거움),
  자신이 이미 계산한 스파이크 판정(dump_status의 "spikes" 딕셔너리)만 상태파일에 남긴다 —
  메인 프로세스는 그 판정을 읽기만 한다. 워커가 죽었거나 하트비트가 오래됐으면 보수적으로
  is_spike=False.
"""
from __future__ import annotations

import json
import logging
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

log = logging.getLogger("bot.ws_trade_client")


@dataclass
class TradeTick:
    symbol: str
    price: float
    quantity: float
    event_time_ms: int
    trade_time_ms: int
    is_buyer_maker: bool

    @property
    def quote_volume(self) -> float:
        return self.price * self.quantity


def parse_futures_agg_trade_message(msg: dict) -> Optional[TradeTick]:
    try:
        data = msg.get("data", msg)
        if data.get("e") not in (None, "aggTrade"):
            return None
        symbol = data["s"]
        return TradeTick(
            symbol=symbol,
            price=float(data["p"]),
            quantity=float(data["q"]),
            event_time_ms=int(data.get("E", 0)),
            trade_time_ms=int(data.get("T", data.get("E", 0))),
            is_buyer_maker=bool(data.get("m", False)),
        )
    except (KeyError, TypeError, ValueError):
        log.debug("aggTrade parse failed, ignoring: %s", msg)
        return None


class TradeTickCache:
    def __init__(self, max_ticks_per_symbol: int = 5000):
        import threading
        self.max_ticks_per_symbol = max_ticks_per_symbol
        self._lock = threading.Lock()
        self._ticks: dict = {}

    def append(self, tick: TradeTick) -> None:
        with self._lock:
            dq = self._ticks.setdefault(tick.symbol, deque(maxlen=self.max_ticks_per_symbol))
            dq.append(tick)

    def get_recent(self, symbol: str, lookback_sec: float, now_ms: Optional[int] = None) -> list:
        if now_ms is None:
            now_ms = int(time.time() * 1000)
        cutoff_ms = now_ms - int(lookback_sec * 1000)
        with self._lock:
            dq = self._ticks.get(symbol)
            if not dq:
                return []
            return [t for t in dq if t.trade_time_ms >= cutoff_ms]

    def symbols(self) -> list:
        with self._lock:
            return list(self._ticks.keys())

    def prune(self, max_age_sec: float) -> None:
        cutoff_ms = int(time.time() * 1000) - int(max_age_sec * 1000)
        with self._lock:
            for symbol, dq in self._ticks.items():
                while dq and dq[0].trade_time_ms < cutoff_ms:
                    dq.popleft()


class TradeStreamWebSocket:
    MAX_STREAMS_PER_CONNECTION = 150

    def __init__(self, api_key: str, api_secret: str, testnet: bool = False,
                 on_trade: Optional[Callable[[TradeTick], None]] = None,
                 health: Optional[object] = None):
        self.api_key = api_key
        self.api_secret = api_secret
        self.testnet = testnet
        self.on_trade = on_trade
        self.cache = TradeTickCache()
        self._twm = None
        self._stream_keys: list = []
        self._started = False
        self._owns_twm = True
        self.health = health

    def _handle_message(self, msg: dict) -> None:
        if self.health is not None:
            try:
                self.health.note_message()
            except Exception:
                pass
        try:
            tick = parse_futures_agg_trade_message(msg)
            if tick is None:
                return
            self.cache.append(tick)
            if self.on_trade is not None:
                self.on_trade(tick)
        except Exception:
            log.exception("aggTrade handler error, ignoring")

    def start(self, symbols: list, twm=None) -> None:
        if self._started:
            raise RuntimeError("TradeStreamWebSocket already started")

        if twm is not None:
            self._twm = twm
            self._owns_twm = False
        else:
            from binance import ThreadedWebsocketManager
            self._twm = ThreadedWebsocketManager(api_key=self.api_key, api_secret=self.api_secret, testnet=self.testnet)
            self._twm.start()
            self._owns_twm = True

        stream_names = [f"{s.lower()}@aggTrade" for s in symbols]
        chunks = [stream_names[i:i + self.MAX_STREAMS_PER_CONNECTION]
                  for i in range(0, len(stream_names), self.MAX_STREAMS_PER_CONNECTION)]
        for chunk in chunks:
            key = self._twm.start_futures_multiplex_socket(callback=self._handle_message, streams=chunk)
            self._stream_keys.append(key)
        self._started = True
        log.info(
            "TradeStreamWebSocket started: symbols=%d testnet=%s conns=%d(max %d per) shared_twm=%s",
            len(symbols), self.testnet, len(chunks), self.MAX_STREAMS_PER_CONNECTION, twm is not None,
        )

    def stop(self) -> None:
        if self._twm is not None and self._owns_twm:
            try:
                self._twm.stop()
            except Exception:
                log.exception("TradeStreamWebSocket stop error")
        self._started = False


class TradeStreamWebSocketV2:
    """[2026-08-15] python-binance ThreadedWebsocketManager를 쓰지 않는 대안 구현.

    배경: python-binance 1.0.37의 내부 read loop가 예외 없이 "Read loop has been closed"류
    증상으로 죽거나 tight loop(GIL 점유)에 빠지는 버그가 aggTrade 조기진입 기능 4회 시도에서
    계속 재현됐다(TradeStreamWebSocket/ws_trade_worker.py의 이력 참고). python-binance
    자체도 내부적으로 `websockets` 라이브러리를 쓰므로, 그 문제는 python-binance의 래퍼
    로직(재연결/read-loop 관리)에 있는 것으로 보고, 저수준 `websockets`(asyncio)로 직접
    combined stream(wss://fstream.binance.com/stream?streams=...)에 연결해 이 문제 자체를
    회피한다.

    인터페이스는 기존 TradeStreamWebSocket과 최대한 동일하게 맞췄다(drop-in 호환):
    생성자(api_key/api_secret/testnet/on_trade/health), `.cache`, `.start(symbols)`,
    `.stop()`. 다만 twm 인자는 없다(공유 ThreadedWebsocketManager 개념 자체가 없음) — 대신
    asyncio 이벤트루프를 별도 데몬 스레드에서 돌리고 start()는 그 스레드를 띄운 뒤 즉시
    반환한다(호출부 동기 코드와 맞물리도록).

    재연결: 모든 recv/connect 지점을 명시적으로 try/except + 타임아웃으로 감싸고, 예외가
    나면 반드시 로그를 남긴 뒤 backoff(1s→2s→5s→10s→30s, 이후 30s 고정)로 재연결한다.
    재연결 루프의 매 반복에는 `asyncio.sleep()`으로 이벤트루프에 양보하는 지점이 있어
    tight loop(CPU 100% 점유, 응답불능)가 재현되지 않게 한다 — 이게 이 재구현의 핵심 목적.

    메시지 파싱은 기존 parse_futures_agg_trade_message()를 그대로 재사용하고, health는
    기존 bot.ws_client.WsHealthMonitor의 note_message() 훅을 그대로 호출한다(신규 로직
    없음)."""

    MAX_STREAMS_PER_CONNECTION = 150
    BACKOFF_SCHEDULE = (1.0, 2.0, 5.0, 10.0, 30.0)
    RECV_TIMEOUT_SEC = 60.0

    def __init__(self, api_key: str, api_secret: str, testnet: bool = False,
                 on_trade: Optional[Callable[[TradeTick], None]] = None,
                 health: Optional[object] = None):
        # api_key/api_secret은 기존 TradeStreamWebSocket과의 시그니처 호환을 위해 받지만,
        # aggTrade는 공개(public) 스트림이라 실제 연결에는 사용하지 않는다(인증 불필요).
        self.api_key = api_key
        self.api_secret = api_secret
        self.testnet = testnet
        self.on_trade = on_trade
        self.cache = TradeTickCache()
        self.health = health
        self._started = False
        self._stop_event = None  # threading.Event, start()에서 생성
        self._threads: list = []
        self._loops: list = []

    @staticmethod
    def compute_backoff_sec(attempt: int) -> float:
        """attempt(0부터 시작, 연속 실패 횟수)에 대응하는 backoff 초를 계산하는 순수 함수.
        BACKOFF_SCHEDULE을 벗어나면(3회 이상 연속 실패) 마지막 값(30s)에 고정된다."""
        if attempt < 0:
            raise ValueError("attempt must be >= 0")
        schedule = TradeStreamWebSocketV2.BACKOFF_SCHEDULE
        if attempt < len(schedule):
            return schedule[attempt]
        return schedule[-1]

    def _stream_url(self, stream_names: list) -> str:
        if self.testnet:
            return "wss://stream.binancefuture.com/stream?streams=" + "/".join(stream_names)
        # [2026-08-16 §3-A 수정] Binance USD-M 메인넷 WebSocket은 2026년 URL 분리 이후
        # market data를 /market 하위로 라우팅한다. 기존 fstream.binance.com/stream?... 는
        # 메인넷 선물 raw aggTrade에서 조용히 0건(무음) 재현됐고, 같은 요청을
        # /market/stream?... 로 바꾸면 즉시 수신이 복구되는 것을 독립 프로브로 확인했다.
        return "wss://fstream.binance.com/market/stream?streams=" + "/".join(stream_names)

    def _handle_message(self, msg: dict) -> None:
        if self.health is not None:
            try:
                self.health.note_message()
            except Exception:
                pass
        try:
            tick = parse_futures_agg_trade_message(msg)
            if tick is None:
                return
            self.cache.append(tick)
            if self.on_trade is not None:
                self.on_trade(tick)
        except Exception:
            log.exception("aggTrade handler error (v2), ignoring")

    def _run_connection_loop(self, stream_names: list, stop_event) -> None:
        """단일 커넥션(스트림 청크 1개)을 담당하는 asyncio 코루틴을 별도 스레드에서 돌린다.
        이 함수 자체는 동기 진입점 — 내부에서 새 이벤트루프를 만들어 코루틴을 실행한다."""
        import asyncio

        async def _connect_and_consume() -> None:
            import websockets

            url = self._stream_url(stream_names)
            attempt = 0
            while not stop_event.is_set():
                try:
                    # [2026-08-21] ping_interval=20/ping_timeout=20 을 끈다.
                    #
                    # 실측(8/19 21:11~21:34 로그): 연결이 성립한 뒤 **매번 40~45초 만에**
                    # ConnectionClosedError(1011 keepalive ping timeout) 로 끊겼다.
                    # 20초에 클라이언트 ping 을 보내고 20초 안에 pong 이 안 오면 끊는
                    # 설정값과 정확히 일치한다 — 바이낸스가 클라이언트발 ping 에 pong 을
                    # 주지 않은 것이다. 끊긴 직후 재접속도 handshake timeout 으로 밀려
                    # 23분간 데이터 0건(message_count_60s=0) 이 됐다. 이게 "V2 무음"의
                    # 정체이며, URL(/market/stream)은 정상이었다.
                    #
                    # 클라이언트 ping 은 여기서 불필요하다 — 아래 recv 루프가 이미
                    # RECV_TIMEOUT_SEC(60초) 무수신이면 재접속한다. 상위 20개 심볼의
                    # aggTrade 는 초당 수십~수백 건이라 60초 무음은 명백한 사망 신호다.
                    # 서버발 ping 에는 websockets 가 자동으로 pong 하므로 영향 없다.
                    async with websockets.connect(url, ping_interval=None,
                                                    close_timeout=5, open_timeout=20) as ws:
                        log.info("TradeStreamWebSocketV2 connected: streams=%d url_host=%s",
                                  len(stream_names), url.split("//")[1].split("/")[0])
                        attempt = 0  # 연결 성공 시 backoff 리셋
                        while not stop_event.is_set():
                            try:
                                raw = await asyncio.wait_for(ws.recv(), timeout=self.RECV_TIMEOUT_SEC)
                            except asyncio.TimeoutError:
                                # RECV_TIMEOUT_SEC 동안 메시지가 전혀 없으면(비정상 무음) 연결을
                                # 재수립한다 — ping/pong으로 대부분 감지되지만 이중 안전장치.
                                log.warning("TradeStreamWebSocketV2 recv timeout(%.0fs) — reconnecting",
                                             self.RECV_TIMEOUT_SEC)
                                break
                            try:
                                msg = json.loads(raw)
                            except (TypeError, ValueError):
                                log.debug("TradeStreamWebSocketV2 non-JSON message ignored")
                                continue
                            self._handle_message(msg)
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    log.warning("TradeStreamWebSocketV2 connection error (attempt=%d): %r", attempt, e)

                if stop_event.is_set():
                    break
                backoff = self.compute_backoff_sec(attempt)
                log.info("TradeStreamWebSocketV2 reconnecting in %.1fs (attempt=%d)", backoff, attempt)
                try:
                    await asyncio.sleep(backoff)
                except asyncio.CancelledError:
                    raise
                attempt += 1

        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._loops.append(loop)
        try:
            loop.run_until_complete(_connect_and_consume())
        except Exception:
            log.exception("TradeStreamWebSocketV2 connection loop crashed unexpectedly")
        finally:
            try:
                loop.close()
            except Exception:
                pass

    def start(self, symbols: list) -> None:
        if self._started:
            raise RuntimeError("TradeStreamWebSocketV2 already started")
        import threading

        stream_names = [f"{s.lower()}@aggTrade" for s in symbols]
        chunks = [stream_names[i:i + self.MAX_STREAMS_PER_CONNECTION]
                  for i in range(0, len(stream_names), self.MAX_STREAMS_PER_CONNECTION)]

        self._stop_event = threading.Event()
        for chunk in chunks:
            t = threading.Thread(target=self._run_connection_loop, args=(chunk, self._stop_event), daemon=True)
            t.start()
            self._threads.append(t)

        self._started = True
        log.info(
            "TradeStreamWebSocketV2 started: symbols=%d testnet=%s conns=%d(max %d per)",
            len(symbols), self.testnet, len(chunks), self.MAX_STREAMS_PER_CONNECTION,
        )

    def stop(self) -> None:
        if self._stop_event is not None:
            self._stop_event.set()
        for t in self._threads:
            t.join(timeout=10)
        self._started = False
        self._threads = []
        self._loops = []


def detect_volume_spike(cache: TradeTickCache, symbol: str, spike_multiplier: float = 3.0,
                         spike_window_sec: float = 10.0, baseline_window_sec: float = 300.0,
                         now_ms: Optional[int] = None,
                         min_baseline_coverage: float = 0.8) -> dict:
    """최근 spike_window_sec초간 체결대금(quote volume) 합이, baseline_window_sec초 전체를
    spike_window_sec 단위로 나눈 평균 대비 spike_multiplier배 이상이면 스파이크로 판정한다.
    baseline이 비어있으면(데이터 부족) is_spike=False로 보수적으로 처리한다.
    아직 어떤 진입/청산 로직에도 연결하지 않은 순수 함수."""
    if now_ms is None:
        now_ms = int(time.time() * 1000)
    if spike_window_sec <= 0 or baseline_window_sec <= 0 or baseline_window_sec < spike_window_sec:
        raise ValueError("invalid spike_window_sec/baseline_window_sec")

    baseline_ticks = cache.get_recent(symbol, baseline_window_sec, now_ms=now_ms)
    if not baseline_ticks:
        return {
            "symbol": symbol, "is_spike": False, "recent_quote_volume": 0.0,
            "baseline_avg_quote_volume": 0.0, "ratio": 0.0, "tick_count_recent": 0,
        }

    # [2026-08-19 실거래에서 발견/수정] baseline 평균을 낼 때 "실제로 캐시에 들어있는
    # 시간"과 무관하게 항상 baseline_window_sec/spike_window_sec(=30)으로 나누고 있었다.
    # 캐시가 덜 찼으면 분모만 그대로라 평균이 과소평가되고 ratio가 폭증한다 - 체결이
    # 완전히 균일해서 스파이크가 전혀 없는 시세에서도 캐시 충전 10초면 ratio 30.00,
    # 60초면 5.00이 나와 임계 3.0을 넘어버린다(즉 100초 미만이면 무조건 스파이크).
    # 실제로 이게 계속 터지고 있었다: 체결 워커가 "응답불능" 판정으로 시간당 수십 회
    # 재시작하면서 TradeTickCache가 매번 비워져 300초 baseline이 채워질 틈이 없었다.
    # 검증(2026-08-19): 공개 아카이브(data.binance.vision) aggTrades로 08-17 8심볼
    # 158건의 진입 시점을 이 함수로 복원해 원장 라벨과 대조한 결과, 라이브가 스파이크로
    # 찍은 45건 중 38~44건이 실제로는 스파이크가 아니었고(ACEUSDT는 라이브 7건 전부
    # 오탐), 오프셋을 0~-600초로 훑어도 일치율이 71%를 못 넘어 "지연"이 아니라 "오탐"
    # 임이 확인됐다.
    # 기존 docstring의 "baseline이 비어있으면 보수적으로 False" 원칙을 부분충전까지
    # 확장한다 - 관측 구간이 baseline의 min_baseline_coverage 배에 못 미치면 판단하지
    # 않는다(이 태그는 관측용이라 False로 두는 쪽이 항상 안전하다).
    observed_span_sec = (now_ms - min(t.trade_time_ms for t in baseline_ticks)) / 1000.0
    if observed_span_sec < baseline_window_sec * min_baseline_coverage:
        return {
            "symbol": symbol, "is_spike": False, "recent_quote_volume": 0.0,
            "baseline_avg_quote_volume": 0.0, "ratio": 0.0, "tick_count_recent": 0,
            "insufficient_baseline": True, "observed_span_sec": observed_span_sec,
        }

    spike_cutoff_ms = now_ms - int(spike_window_sec * 1000)
    recent_ticks = [t for t in baseline_ticks if t.trade_time_ms >= spike_cutoff_ms]
    recent_quote_volume = sum(t.quote_volume for t in recent_ticks)

    total_quote_volume = sum(t.quote_volume for t in baseline_ticks)
    num_windows = baseline_window_sec / spike_window_sec
    baseline_avg_quote_volume = total_quote_volume / num_windows

    if baseline_avg_quote_volume <= 0:
        ratio = 0.0
        is_spike = False
    else:
        ratio = recent_quote_volume / baseline_avg_quote_volume
        is_spike = ratio >= spike_multiplier

    return {
        "symbol": symbol,
        "is_spike": is_spike,
        "recent_quote_volume": recent_quote_volume,
        "baseline_avg_quote_volume": baseline_avg_quote_volume,
        "ratio": ratio,
        "tick_count_recent": len(recent_ticks),
        "insufficient_baseline": False, "observed_span_sec": observed_span_sec,
    }


def resample_ticks_to_ohlcv(ticks: list, bucket_sec: float) -> list:
    """체결(TradeTick) 리스트를 bucket_sec초 단위 OHLCV 캔들 리스트로 리샘플링한다.
    데이터가 없는 버킷은 생성하지 않는다(빈 캔들로 채우지 않음)."""
    if bucket_sec <= 0:
        raise ValueError("bucket_sec must be > 0")
    if not ticks:
        return []

    bucket_ms = int(bucket_sec * 1000)
    sorted_ticks = sorted(ticks, key=lambda t: t.trade_time_ms)

    buckets: dict = {}
    for t in sorted_ticks:
        bucket_open = (t.trade_time_ms // bucket_ms) * bucket_ms
        buckets.setdefault(bucket_open, []).append(t)

    result = []
    for bucket_open in sorted(buckets.keys()):
        bucket_ticks = buckets[bucket_open]
        prices = [t.price for t in bucket_ticks]
        result.append({
            "open_time_ms": bucket_open,
            "close_time_ms": bucket_open + bucket_ms - 1,
            "open": prices[0],
            "high": max(prices),
            "low": min(prices),
            "close": prices[-1],
            "volume": sum(t.quantity for t in bucket_ticks),
            "quote_volume": sum(t.quote_volume for t in bucket_ticks),
            "trade_count": len(bucket_ticks),
            "taker_buy_base": sum(t.quantity for t in bucket_ticks if not t.is_buyer_maker),
        })
    return result


class FileBackedSpikeCache:
    """[2026-08-15] ws_trade_worker.py(들)이 상태파일에 남기는 사전계산된 스파이크 판정을
    읽기만 하는 어댑터. bot/ws_client.py의 FileBackedKlineCache와 동일한 안전 원칙(파일이
    없거나 깨져있거나 워커 하트비트가 오래됐으면 무조건 "정보 없음"으로 보수적 처리 —
    워커가 죽어도 메인 프로세스의 매매 판단에는 영향 없음, REST/1분봉 경로로 그대로 계속됨).

    여러 샤드로 나뉜 워커를 지원하기 위해 (status_path, heartbeat_path) 쌍의 리스트를
    받는다(샤드 1개면 리스트 길이 1)."""

    def __init__(self, shard_paths: list, worker_max_staleness_sec: float = 30.0):
        self.shard_paths = [(Path(s), Path(h)) for s, h in shard_paths]
        self.worker_max_staleness_sec = worker_max_staleness_sec
        self._cached_spikes: dict = {}
        self._cached_mtimes: dict = {}

    def _heartbeat_is_fresh(self, heartbeat_path: Path) -> bool:
        try:
            if not heartbeat_path.exists():
                return False
            age = time.time() - float(heartbeat_path.read_text(encoding="utf-8").strip())
            return age <= self.worker_max_staleness_sec
        except Exception:
            return False

    def _refresh_shard(self, status_path: Path, heartbeat_path: Path) -> None:
        if not self._heartbeat_is_fresh(heartbeat_path):
            return  # 워커가 죽었거나 응답불능 — 이 샤드의 기존 캐시값을 그대로 둔다(새로 덮지 않음)
        try:
            mtime = status_path.stat().st_mtime
        except Exception:
            return
        if self._cached_mtimes.get(status_path) == mtime:
            return  # 안 바뀜 — 디스크 재읽기 생략
        try:
            data = json.loads(status_path.read_text(encoding="utf-8"))
        except Exception:
            return
        spikes = data.get("spikes", {})
        if isinstance(spikes, dict):
            self._cached_spikes.update(spikes)
        self._cached_mtimes[status_path] = mtime

    def is_spike(self, symbol: str) -> bool:
        """symbol의 최신 스파이크 판정을 반환한다. 판정 자체가 없거나(워커가 그 심볼을
        아직 안 다뤘음) 워커가 응답불능이면 보수적으로 False."""
        for status_path, heartbeat_path in self.shard_paths:
            self._refresh_shard(status_path, heartbeat_path)
        entry = self._cached_spikes.get(symbol)
        if not entry:
            return False
        return bool(entry.get("is_spike", False))
