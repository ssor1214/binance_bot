"""[2026-08-10] 실시간 WebSocket 시장데이터/계정 스트림 레이어.

ChatGPT 스펙에서 요청한 "REST 30초 폴링 대신 WebSocket으로 신호 반응 지연/API 호출량을
줄인다"의 1단계 구현. 아직은 이 모듈 하나만 새로 추가한 것이고, `bot/main.py`의 실거래
스캔/결정 루프에는 연결하지 않았다 — 기존 REST 폴링 경로가 그대로 유지되므로 이 모듈이
있다고 실거래 동작이 바뀌지 않는다(코드 존재 자체가 아무 부작용 없음, import도 안 됨).

연결하려면(다음 단계): main.py 스캔 루프가 REST get_klines 대신 이 클라이언트가 유지하는
최신 캔들 캐시를 읽도록 바꿔야 하고, User Data Stream 콜백으로 실제 체결가/수수료를
받아 trade_ledger의 미채움 필드(actual_fill_*)를 채우도록 연결해야 한다. 둘 다 실거래
동작을 바꾸는 민감한 변경이라, 테스트넷에서 먼저 최소 며칠 검증 후 전환할 것을 권장한다
(README 기존 권고사항과 동일한 원칙).

설계 원칙:
- 콜백 기반: 이 모듈은 소켓 연결/재연결만 담당하고, 받은 데이터를 어떻게 쓸지는 호출부(main.py
  등)가 콜백으로 주입한다 — 전략 로직과 결합시키지 않아 테스트하기 쉽다.
- 재연결: python-binance의 ThreadedWebsocketManager 자체가 내부적으로 재연결을 시도하지만,
  콜백에서 예외가 나면 스레드가 조용히 죽는 경우가 있어 콜백을 항상 try/except로 감싼다.
- 최신 캔들 캐시(KlineCache)는 심볼별로 마지막 완성/미완성 캔들만 갖고 있는 경량 구조 —
  REST get_klines가 반환하는 DataFrame과 호환되는 형태로 변환하는 함수도 함께 제공한다.
"""
from __future__ import annotations

import logging
import json
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

log = logging.getLogger("bot.ws_client")


class _ReadLoopErrorWatcher(logging.Handler):
    """[2026-08-10, Codex 제안 반영] python-binance 내부(binance.ws.threaded_stream/
    reconnecting_websocket)가 큐 폭주(BinanceWebsocketQueueOverflow) 등으로 read loop가
    죽으면 "Read loop has been closed" 에러를 반복 로깅한다 — 우리 콜백(_handle_message)은
    전혀 호출되지 않는 구간이라 메시지 카운터만으로는 이 상태를 못 잡는다. 그 내부 로거에
    이 핸들러를 붙여 에러 발생을 직접 카운트한다(연속 발생 횟수는 정상 메시지가 한 번이라도
    오면 0으로 리셋 — read loop가 실제로 회복됐다는 뜻이므로)."""

    def __init__(self):
        super().__init__()
        self.consecutive_errors = 0
        self.error_count_since = []  # 최근 60초 윈도 카운트용 타임스탬프 리스트
        self._lock = threading.Lock()

    def emit(self, record: logging.LogRecord) -> None:
        msg = record.getMessage()
        if "Read loop has been closed" in msg or "QueueOverflow" in msg:
            with self._lock:
                self.consecutive_errors += 1
                self.error_count_since.append(time.time())

    def note_message_received(self) -> None:
        with self._lock:
            self.consecutive_errors = 0

    def error_count_60s(self) -> int:
        cutoff = time.time() - 60
        with self._lock:
            self.error_count_since = [t for t in self.error_count_since if t >= cutoff]
            return len(self.error_count_since)


class WsHealthMonitor:
    """[2026-08-10, Codex 제안 반영] ws_worker의 하트비트를 "프로세스가 살아서 파일을
    쓰고 있다"뿐 아니라 "실제로 시장데이터를 계속 수신하고 있다"까지 반영하도록 확장하는
    상태 수집기. MarketDataWebSocket이 메시지를 받을 때마다 note_message()를 호출하고,
    ws_worker.py가 주기적으로 snapshot()을 캐시 파일에 함께 기록한다."""

    def __init__(self):
        self.last_market_message_ts: float = 0.0
        self._message_timestamps: list = []
        self._latency_samples: list = []
        self._lock = threading.Lock()
        self._error_watcher = _ReadLoopErrorWatcher()
        self._error_watcher.setLevel(logging.ERROR)
        logging.getLogger("binance.ws.threaded_stream").addHandler(self._error_watcher)
        logging.getLogger("binance.ws.reconnecting_websocket").addHandler(self._error_watcher)

    def note_message(self) -> None:
        now = time.time()
        with self._lock:
            self.last_market_message_ts = now
            self._message_timestamps.append(now)
        self._error_watcher.note_message_received()

    def note_latency(self, latency_ms: float) -> None:
        """[2026-08-10 사용자요청] "웹소켓=실시간"이라는 착각을 깨는 안전장치 — 메시지에
        실린 이벤트 발생시각(E)과 우리가 실제로 수신한 시각의 차이를 기록해둔다. 데이터를
        버리지는 않는다(캔들 캐시에 빈 구간이 생기면 오히려 기존 신선도 안전장치와
        충돌할 수 있음) — 대신 "큐 폭주가 터지기 직전 지연이 서서히 늘어나는 조짐"을
        미리 포착하는 조기경보 지표로만 쓴다."""
        with self._lock:
            self._latency_samples.append(latency_ms)
            self._latency_samples = self._latency_samples[-200:]

    def snapshot(self) -> dict:
        cutoff = time.time() - 60
        with self._lock:
            self._message_timestamps = [t for t in self._message_timestamps if t >= cutoff]
            msg_count = len(self._message_timestamps)
            last_ts = self.last_market_message_ts
            latency_samples = list(self._latency_samples)
        return {
            "last_market_message_ts": last_ts,
            "message_count_60s": msg_count,
            "error_count_60s": self._error_watcher.error_count_60s(),
            "consecutive_read_loop_errors": self._error_watcher.consecutive_errors,
            "max_latency_ms_recent": max(latency_samples) if latency_samples else 0.0,
            "avg_latency_ms_recent": (sum(latency_samples) / len(latency_samples)) if latency_samples else 0.0,
        }


@dataclass
class KlineUpdate:
    symbol: str
    open_time: int
    open: float
    high: float
    low: float
    close: float
    volume: float
    is_closed: bool
    close_time: int
    # [2026-08-10] RollingKlineCache가 REST get_klines()와 동일한 컬럼의 DataFrame을 재구성하려면
    # 이 3개도 필요하다 — 기존 KlineCache(최신 1개만 보는 경량 캐시)는 안 쓰던 필드라 기본값을
    # 둬서 하위호환을 유지한다.
    quote_asset_volume: float = 0.0
    num_trades: int = 0
    taker_buy_base: float = 0.0


class KlineCache:
    """심볼별 최신 캔들(완성 여부 무관)만 메모리에 들고 있는 경량 캐시.
    REST 폴링을 완전히 대체하려면 과거 N개 캔들 히스토리가 필요하지만, 이 캐시는
    "방금 들어온 최신 값"만 다루는 실시간 보조용이다 — 초기 히스토리는 여전히 REST로
    한 번 채운 뒤, 그 이후 증분만 이 캐시로 갱신하는 하이브리드 방식을 염두에 두고 설계했다."""

    def __init__(self):
        self._lock = threading.Lock()
        self._latest: dict[str, KlineUpdate] = {}

    def update(self, k: KlineUpdate) -> None:
        with self._lock:
            self._latest[k.symbol] = k

    def get(self, symbol: str) -> Optional[KlineUpdate]:
        with self._lock:
            return self._latest.get(symbol)

    def symbols(self) -> list[str]:
        with self._lock:
            return list(self._latest.keys())


class RollingKlineCache:
    """[2026-08-10] 심볼별 "확정된 캔들 N개"를 메모리에 들고 있다가, REST get_klines()와
    동일한 컬럼의 DataFrame으로 재구성해주는 캐시 — 전략(지표계산)이 REST든 WS든 똑같은
    형태의 데이터를 받도록 하는 게 핵심이다(호출부 코드를 안 바꿔도 되게).

    사용 흐름: ① seed()로 REST 스냅샷을 한 번 넣어 초기 히스토리를 채운다(부팅 직후에는
    WS로 아직 캔들이 안 쌓였으므로 필수). ② 이후 WS에서 "확정된"(is_closed=True) 캔들이
    올 때마다 append_closed()로 갱신한다 — 미확정 캔들은 get_klines()도 항상 버리므로
    (마지막 진행중 캔들 제외) 여기서도 받지 않는다. ③ to_dataframe()으로 언제든 스냅샷.

    max_len개를 넘으면 오래된 것부터 버려 메모리를 무한히 쓰지 않게 한다."""

    _COLUMNS = [
        "open_time", "open", "high", "low", "close", "volume",
        "close_time", "quote_asset_volume", "num_trades",
        "taker_buy_base", "taker_buy_quote", "ignore",
    ]

    def __init__(self, max_len: int = 200):
        self.max_len = max_len
        self._lock = threading.Lock()
        # [2026-08-10 사용자요청] 예전엔 list + "초과분을 del rows[:N]으로 앞에서 잘라내기"
        # 방식이었다 — 이건 매번 남은 요소를 전부 한 칸씩 당겨야 하는 O(N) 연산이다.
        # collections.deque(maxlen=)는 양방향 큐라 앞/뒤 추가·제거가 O(1)이고, maxlen을
        # 넘으면 오래된 항목이 자동으로 밀려나서 수동 트리밍 코드 자체가 필요 없어진다.
        self._rows: dict[str, deque] = {}
        # [2026-08-10 실거래 사고 발견/수정] WS 연결이 끊겨도(라이브러리 내부 재연결 실패 등)
        # 캐시에는 예전 캔들이 그대로 남아있어, has_sufficient_history()만 확인하는 get_klines()
        # 오버레이가 "개수는 충분하니 최신인 줄 알고" 멈춰버린 옛날 가격을 계속 매매 판단에
        # 쓸 뻔한 실제 위험을 발견했다(연결이 죽어도 겉으로는 티가 안 남 — 가장 위험한 종류의
        # 버그). last_update_ts로 "마지막으로 갱신된 시각"을 심볼별로 남겨, is_fresh()가 이
        # 캐시를 계속 믿어도 되는지 판단할 수 있게 한다.
        self._last_update_ts: dict[str, float] = {}

    def seed(self, symbol: str, df) -> None:
        """REST get_klines()가 반환한 DataFrame으로 초기 히스토리를 채운다(덮어쓰기).

        [2026-08-10] open_time을 항상 정수(ms epoch)로 정규화해서 저장한다 — get_klines()가
        주는 open_time은 pandas Timestamp인데, append_closed()가 WS에서 받는 open_time은
        정수(ms)라서 타입이 다르면 append_closed()의 "마지막 행과 open_time이 같으면 덮어쓰기"
        중복방지 비교가 절대 True가 안 돼(Timestamp == int는 항상 False), REST seed 직후
        받는 첫 WS 캔들이 seed의 마지막 행과 겹칠 때 중복 행이 생기는 실제 버그였다."""
        rows = []
        for _, r in df.iterrows():
            open_time = r["open_time"]
            if hasattr(open_time, "value"):  # pandas Timestamp -> ms epoch 정수로 변환
                open_time = int(open_time.value // 10**6)
            rows.append({
                "open_time": int(open_time), "open": float(r["open"]), "high": float(r["high"]),
                "low": float(r["low"]), "close": float(r["close"]), "volume": float(r["volume"]),
                "close_time": r["close_time"], "quote_asset_volume": float(r.get("quote_asset_volume", 0.0)),
                "num_trades": int(r.get("num_trades", 0)), "taker_buy_base": float(r.get("taker_buy_base", 0.0)),
                "taker_buy_quote": float(r.get("taker_buy_quote", 0.0)), "ignore": 0,
            })
        with self._lock:
            self._rows[symbol] = deque(rows[-self.max_len:], maxlen=self.max_len)
            self._last_update_ts[symbol] = time.time()  # 시딩도 "최신 데이터가 들어왔다"로 취급

    def append_closed(self, k: KlineUpdate) -> None:
        """WS에서 확정된(is_closed=True) 캔들 하나를 받아 뒤에 이어붙인다. 같은 open_time이면
        (드물게 중복 전송) 마지막 행을 덮어써서 중복 append를 막는다."""
        if not k.is_closed:
            return
        row = {
            "open_time": k.open_time, "open": k.open, "high": k.high, "low": k.low, "close": k.close,
            "volume": k.volume, "close_time": k.close_time, "quote_asset_volume": k.quote_asset_volume,
            "num_trades": k.num_trades, "taker_buy_base": k.taker_buy_base,
            "taker_buy_quote": 0.0, "ignore": 0,
        }
        with self._lock:
            rows = self._rows.setdefault(k.symbol, deque(maxlen=self.max_len))
            if rows and rows[-1]["open_time"] == k.open_time:
                rows[-1] = row
            else:
                rows.append(row)  # maxlen이 꽉 차면 deque가 자동으로 가장 오래된 항목을 밀어냄(O(1))
            self._last_update_ts[k.symbol] = time.time()

    def to_dataframe(self, symbol: str):
        """symbol의 현재 캐시를 REST get_klines()와 동일한 컬럼의 DataFrame으로 반환한다.
        데이터가 없으면 None — 호출부가 REST 폴백을 선택할 수 있게 한다.

        [2026-08-10 테스트넷 실증 발견] open_time을 ms epoch 정수로 저장해두고 여기서
        unit 지정 없이 pd.to_datetime()을 부르면, pandas가 정수를 나노초 단위로 해석해서
        (기본 단위가 ns) 1970년대 날짜가 나오는 실제 버그가 있었다 — 반드시 unit="ms"를
        명시해야 한다. get_klines()의 원래 REST 경로도 동일하게 unit="ms"를 쓰고 있다."""
        import pandas as pd
        with self._lock:
            rows = list(self._rows.get(symbol, []))
        if not rows:
            return None
        df = pd.DataFrame(rows, columns=self._COLUMNS)
        df["open_time"] = pd.to_datetime(df["open_time"], unit="ms")
        return df

    def has_sufficient_history(self, symbol: str, min_len: int) -> bool:
        with self._lock:
            return len(self._rows.get(symbol, [])) >= min_len

    def is_fresh(self, symbol: str, max_age_sec: float) -> bool:
        """[2026-08-10 실거래 사고 발견/수정] 캐시에 데이터가 "있다"와 "최신이다"는 다른
        질문이다 — WS 연결이 죽어도 예전 캔들은 그대로 남아있으므로, 호출부는 반드시
        has_sufficient_history()뿐 아니라 이것도 함께 확인해야 한다. 아직 한 번도 갱신된
        적 없는 심볼(seed도 안 됨)은 False — 그런 경우는 애초에 신선도를 논할 데이터가 없다."""
        with self._lock:
            last_ts = self._last_update_ts.get(symbol)
        if last_ts is None:
            return False
        return (time.time() - last_ts) <= max_age_sec


def parse_futures_kline_message(msg: dict) -> Optional[KlineUpdate]:
    """futures_multiplex/kline 소켓의 원본 메시지를 KlineUpdate로 변환한다.
    형식이 예상과 다르면(None 등) 조용히 None을 반환 — 호출부가 판단하게 한다.

    [2026-08-10 테스트넷 실증 발견] python-binance의 start_kline_futures_socket은 문서상
    "kline" 이벤트(심볼 필드 "s")를 줄 것 같았지만, 실제 테스트넷 연결 검증에서는
    "continuous_kline" 이벤트(심볼 필드가 "s"가 아니라 "ps", 계약타입 "ct" 포함)로 온다는
    것이 확인됐다 — 최초 구현은 "s"만 찾다가 매 메시지를 조용히 버리는 실제 버그였음.
    두 형식을 모두 지원해 어느 쪽이 와도 파싱되게 한다."""
    try:
        data = msg.get("data", msg)  # multiplex는 {"stream":..,"data":{...}}, 단일소켓은 바로 최상위
        k = data["k"]
        symbol = data.get("s") or data["ps"]  # "kline" 이벤트는 "s", "continuous_kline"은 "ps"
        return KlineUpdate(
            symbol=symbol,
            open_time=int(k["t"]),
            open=float(k["o"]),
            high=float(k["h"]),
            low=float(k["l"]),
            close=float(k["c"]),
            volume=float(k["v"]),
            is_closed=bool(k["x"]),
            close_time=int(k["T"]),
            quote_asset_volume=float(k.get("q", 0.0)),
            num_trades=int(k.get("n", 0)),
            taker_buy_base=float(k.get("V", 0.0)),
        )
    except (KeyError, TypeError, ValueError):
        log.debug("kline 메시지 파싱 실패(형식 불일치로 판단, 무시): %s", msg)
        return None


class MarketDataWebSocket:
    """여러 심볼의 kline 스트림을 하나의 ThreadedWebsocketManager로 관리한다.
    실제 바이낸스 연결은 start() 호출 시에만 이뤄지고, 이 클래스를 생성만 해서는(import 등)
    아무 네트워크 연결도 발생하지 않는다 — 테스트에서 안전하게 인스턴스화 가능."""

    def __init__(self, api_key: str, api_secret: str, testnet: bool = False,
                 on_kline: Optional[Callable[[KlineUpdate], None]] = None,
                 health: Optional["WsHealthMonitor"] = None):
        self.api_key = api_key
        self.api_secret = api_secret
        self.testnet = testnet
        self.on_kline = on_kline
        self.cache = KlineCache()
        self._twm = None
        self._stream_keys: list[str] = []
        self._started = False
        # [2026-08-10, Codex 제안 반영] 외부에서 WsHealthMonitor를 주입하면 메시지 수신마다
        # 상태를 기록한다 — 주입 안 하면(기본값 None) 기존 동작과 100% 동일(하위호환).
        self.health = health

    def _handle_message(self, msg: dict) -> None:
        # [설계원칙] 콜백 스레드가 예외로 죽으면 그 심볼은 이후 영구히 갱신이 멈추는데 겉으로는
        # 티가 안 나는 게 제일 위험하다 — 반드시 여기서 전부 삼키고 로그만 남긴다.
        if self.health is not None:
            self.health.note_message()
            # [2026-08-10 사용자요청] "웹소켓=실시간"이라는 착각 경계 — 메시지에 실린 이벤트
            # 발생시각(E, ms)과 우리가 실제로 수신한 시각의 차이를 조기경보 지표로 기록한다.
            # 데이터 자체는 절대 버리지 않는다(버리면 캔들 캐시에 빈 구간이 생겨 기존 신선도
            # 안전장치와 충돌할 수 있음).
            try:
                event_time_ms = (msg.get("data", msg)).get("E")
                if event_time_ms:
                    self.health.note_latency(time.time() * 1000 - event_time_ms)
            except Exception:
                pass
        try:
            k = parse_futures_kline_message(msg)
            if k is None:
                return
            self.cache.update(k)
            if self.on_kline is not None:
                self.on_kline(k)
        except Exception:
            log.exception("kline 메시지 처리 중 오류(무시하고 계속 수신)")

    # [2026-08-10 실거래 사고 발견/수정] 심볼마다 start_kline_futures_socket()을 개별 호출해
    # 250개 소켓을 동시에 열려고 했더니 실제로 "이벤트 루프가 이미 실행 중"이라는 내부 오류와
    # 대량 handshake 타임아웃이 발생했다(연결 250개를 순식간에 여는 걸 라이브러리가 못 버팀).
    # 더 위험한 건, 이런 대량 연결 실패가 반복되면 바이낸스가 이 IP를 비정상 트래픽으로 보고
    # 일시 차단할 수 있어 REST 기반 실거래 로직까지 덩달아 막힐 뻔했다는 점 — 실제 거래계좌로
    # 겪은 사고. 해결책: 심볼마다 별도 연결을 열지 않고, "여러 스트림을 하나의 연결로 묶는"
    # combined/multiplex 스트림을 쓴다. 바이낸스는 연결 하나당 구독 가능한 스트림 수에 상한이
    # 있어(공식 한도보다 여유있게) MAX_STREAMS_PER_CONNECTION개씩 나눠서 연결도 소수(보통 2개)만 연다.
    MAX_STREAMS_PER_CONNECTION = 150

    def start(self, symbols: list[str], interval: str = "1m", twm=None) -> None:
        """[2026-08-10 두 번째 실거래 사고 발견/수정] 시장데이터용/계정스트림용으로 각자
        별도의 ThreadedWebsocketManager를 만들어 "동시에" 띄웠더니, 연결 개수를 150개로
        줄인 뒤에도 여전히 "이벤트 루프가 이미 실행 중" 오류가 재발했다 — 원인은 연결
        개수가 아니라 **매니저 인스턴스를 두 개(각자 내부 스레드+이벤트루프) 동시에
        운용하는 것 자체**였다(시장데이터 매니저와 계정스트림 매니저가 충돌). 그래서 이제
        twm(이미 시작된 공유 ThreadedWebsocketManager)을 외부에서 주입받을 수 있게 하고,
        주입되면 새로 만들지 않고 그걸 그대로 쓴다 — main()이 매니저를 하나만 만들어
        시장데이터/계정스트림 둘 다에 넘겨주는 식으로 써야 한다(미주입시엔 기존처럼 단독
        사용도 가능 — 테스트/단일기능용 하위호환)."""
        if self._started:
            raise RuntimeError("이미 시작된 MarketDataWebSocket 인스턴스입니다")

        if twm is not None:
            self._twm = twm
            self._owns_twm = False
        else:
            from binance import ThreadedWebsocketManager  # 지연 임포트: 이 모듈을 import만 해도
            self._twm = ThreadedWebsocketManager(api_key=self.api_key, api_secret=self.api_secret, testnet=self.testnet)
            self._twm.start()
            self._owns_twm = True

        # continuous_kline 스트림 이름 형식(테스트넷 실증으로 확인): "<symbol>_perpetual@continuousKline_<interval>"
        stream_names = [f"{s.lower()}_perpetual@continuousKline_{interval}" for s in symbols]
        chunks = [stream_names[i:i + self.MAX_STREAMS_PER_CONNECTION]
                  for i in range(0, len(stream_names), self.MAX_STREAMS_PER_CONNECTION)]
        for chunk in chunks:
            key = self._twm.start_futures_multiplex_socket(callback=self._handle_message, streams=chunk)
            self._stream_keys.append(key)
        self._started = True
        log.info(
            "MarketDataWebSocket 시작: 심볼 %d개, interval=%s, testnet=%s, 연결 %d개(스트림 묶음당 최대 %d개), 공유매니저=%s",
            len(symbols), interval, self.testnet, len(chunks), self.MAX_STREAMS_PER_CONNECTION, twm is not None,
        )

    def stop(self) -> None:
        # 공유 매니저(twm이 외부 주입된 경우)는 다른 스트림(예: 계정스트림)이 아직 쓰고 있을 수
        # 있으므로 여기서 stop()하지 않는다 — 매니저 자체의 수명은 주입한 쪽(main())이 관리한다.
        if self._twm is not None and getattr(self, "_owns_twm", True):
            try:
                self._twm.stop()
            except Exception:
                log.exception("MarketDataWebSocket 종료 중 오류")
        self._started = False


class UserDataWebSocket:
    """계정(체결/포지션/잔고) 이벤트 스트림. TradeRecord의 actual_fill_* 필드를 정확히
    채우려면 이 스트림의 ORDER_TRADE_UPDATE 이벤트(체결가/수수료 포함)가 필요하다 — 아직
    trade_ledger 연결은 하지 않았고(다음 단계), 이 클래스는 원시 이벤트 수신/콜백 전달까지만."""

    def __init__(self, api_key: str, api_secret: str, testnet: bool = False,
                 on_account_update: Optional[Callable[[dict], None]] = None):
        self.api_key = api_key
        self.api_secret = api_secret
        self.testnet = testnet
        self.on_account_update = on_account_update
        self._twm = None
        self._started = False

    def _handle_message(self, msg: dict) -> None:
        try:
            if self.on_account_update is not None:
                self.on_account_update(msg)
        except Exception:
            log.exception("계정 이벤트 처리 중 오류(무시하고 계속 수신)")

    def start(self, twm=None) -> None:
        """[2026-08-10] MarketDataWebSocket과 동일한 이유로 twm(공유 매니저) 주입을 지원한다
        — 시장데이터/계정스트림이 각자 매니저를 만들어 동시에 띄우면 내부 이벤트루프가
        충돌하는 실거래 사고가 있었다. main()에서 매니저 하나를 만들어 양쪽에 넘길 것."""
        if self._started:
            raise RuntimeError("이미 시작된 UserDataWebSocket 인스턴스입니다")

        if twm is not None:
            self._twm = twm
            self._owns_twm = False
        else:
            from binance import ThreadedWebsocketManager
            self._twm = ThreadedWebsocketManager(api_key=self.api_key, api_secret=self.api_secret, testnet=self.testnet)
            self._twm.start()
            self._owns_twm = True

        self._twm.start_futures_user_socket(callback=self._handle_message)
        self._started = True
        log.info("UserDataWebSocket 시작: testnet=%s, 공유매니저=%s", self.testnet, twm is not None)

    def stop(self) -> None:
        if self._twm is not None and getattr(self, "_owns_twm", True):
            try:
                self._twm.stop()
            except Exception:
                log.exception("UserDataWebSocket 종료 중 오류")
        self._started = False


@dataclass
class OrderFill:
    """실제 체결 1건. trade_ledger의 TradeRecord.actual_fill_* 필드를 채우는 데 쓴다."""
    symbol: str
    side: str
    avg_price: float
    commission: float
    commission_asset: str
    realized_pnl: float
    order_status: str
    event_time_ms: int


def parse_order_trade_update(msg: dict) -> Optional[OrderFill]:
    """Futures User Data Stream의 ORDER_TRADE_UPDATE 이벤트에서 "실제 체결(x=TRADE)"만
    골라 OrderFill로 변환한다. 체결이 아닌 주문상태 변경(취소/신규접수 등)이거나 형식이
    다르면 None — trade_ledger 쪽은 None을 무시하고 기존 mark-price 추정치를 유지하면 된다.

    [2026-08-10] bot/ws_client.py의 kline 파서에서 이미 한 번 "문서와 실제 이벤트 형식이
    다를 수 있다"는 교훈을 테스트넷 실증으로 얻었으므로, 이 파서도 반드시 실제 계정에 체결이
    발생했을 때 테스트넷으로 검증한 뒤 실거래에 연결해야 한다(아직 미검증 — 주문이 없으면
    ORDER_TRADE_UPDATE 자체가 발생하지 않아 kline처럼 그냥 구독만 해서는 형식을 확인할 수
    없었다)."""
    try:
        if msg.get("e") != "ORDER_TRADE_UPDATE":
            return None
        o = msg["o"]
        if o.get("x") != "TRADE":  # x: 이번 이벤트를 유발한 실행 종류. TRADE=실제 체결
            return None
        return OrderFill(
            symbol=o["s"],
            side=o["S"],
            avg_price=float(o["ap"]),
            commission=float(o.get("n", 0.0)),
            commission_asset=o.get("N", ""),
            realized_pnl=float(o.get("rp", 0.0)),
            order_status=o.get("X", ""),
            event_time_ms=int(msg.get("E", 0)),
        )
    except (KeyError, TypeError, ValueError):
        log.debug("ORDER_TRADE_UPDATE 파싱 실패(형식 불일치로 판단, 무시): %s", msg)
        return None


class FillTracker:
    """심볼별 가장 최근 체결 정보만 들고 있는 경량 캐시. entry/exit 구분은 하지 않고
    "지금 이 심볼의 마지막 체결이 이거였다"만 기록한다 — record_trade_ledger가 청산
    직후 이 값을 조회해 actual_fill_* 필드에 채워 넣는 용도로 설계했다(아직 main.py에
    연결 안 함, 다음 단계)."""

    def __init__(self, log_events: bool = False, event_log_dir: Optional[Path] = None):
        self._lock = threading.Lock()
        self._last_fill: dict[str, OrderFill] = {}
        self.log_events = log_events
        self.event_log_dir = Path(event_log_dir) if event_log_dir is not None else Path(__file__).resolve().parent.parent / "logs"

    def _append_event_log(self, fill: OrderFill, msg: dict) -> None:
        if not self.log_events:
            return
        try:
            self.event_log_dir.mkdir(exist_ok=True)
            event_day = time.strftime("%Y%m%d", time.localtime(fill.event_time_ms / 1000 if fill.event_time_ms else time.time()))
            path = self.event_log_dir / f"fill_events_{event_day}.jsonl"
            order = msg.get("o", {}) if isinstance(msg, dict) else {}
            row = {
                "logged_at": time.time(),
                "event_time_ms": fill.event_time_ms,
                "symbol": fill.symbol,
                "side": fill.side,
                "avg_price": fill.avg_price,
                "quantity": float(order.get("q", 0.0) or 0.0),
                "last_filled_quantity": float(order.get("l", 0.0) or 0.0),
                "commission": fill.commission,
                "commission_asset": fill.commission_asset,
                "realized_pnl": fill.realized_pnl,
                "order_status": fill.order_status,
                "order_id": order.get("i"),
                "client_order_id": order.get("c"),
                "execution_type": order.get("x"),
            }
            with path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")
        except Exception:
            log.exception("체결 이벤트 로그 기록 실패(무시하고 계속 진행)")

    def on_message(self, msg: dict) -> None:
        fill = parse_order_trade_update(msg)
        if fill is None:
            return
        with self._lock:
            self._last_fill[fill.symbol] = fill
        self._append_event_log(fill, msg)

    def get(self, symbol: str) -> Optional[OrderFill]:
        with self._lock:
            return self._last_fill.get(symbol)

    def clear(self, symbol: str) -> None:
        with self._lock:
            self._last_fill.pop(symbol, None)


class FileBackedKlineCache:
    """[2026-08-10] WS 연결을 별도 프로세스(bot/ws_worker.py)로 격리한 뒤, 메인 프로세스가
    그 워커가 남기는 캐시 파일을 읽기만 하는 어댑터. RollingKlineCache와 동일한 인터페이스
    (has_sufficient_history/is_fresh/to_dataframe)를 제공해 Exchange.get_klines()가 어느
    쪽을 쓰든 코드를 안 바꿔도 되게 한다.

    설계 원칙: 이 클래스는 파일을 읽기만 하고 절대 쓰지 않는다(쓰기는 워커 전담). 파일이
    없거나 깨져있거나 오래됐으면 전부 "데이터 없음"으로 안전하게 처리해 REST 폴백을
    유도한다 — 워커 프로세스가 죽어있어도 메인 프로세스의 매매 판단에는 영향이 없다."""

    def __init__(self, cache_path, heartbeat_path, worker_max_staleness_sec: float = 30.0, status_path=None):
        self.cache_path = Path(cache_path)
        self.heartbeat_path = Path(heartbeat_path)
        self.status_path = Path(status_path) if status_path is not None else None
        # 워커 프로세스 자체가 하트비트를 이 시간(초) 이상 못 남기면(죽었거나 응답불능이면)
        # 파일 내용이 아무리 최신이어도 신뢰하지 않는다 — 파일은 안 지워지고 남아있으므로
        # 하트비트를 별도로 봐야 "워커가 지금 살아있다"를 판단할 수 있다.
        self.worker_max_staleness_sec = worker_max_staleness_sec
        # [2026-08-10 실측 발견] get_klines() 한 번 호출에 has_sufficient_history/is_fresh/
        # to_dataframe이 각각 _read()를 불러, 심볼 1개당 캐시파일 전체를 3번씩 다시 읽고
        # JSON 파싱했다 — 250심볼 스캔이면 사이클당 750번의 파일 I/O가 발생해 REST 폴백 대비
        # 속도 이점(테스트넷 실측 1.2배에 불과)이 거의 상쇄됐다. mtime이 그대로면 디스크를
        # 다시 읽지 않고 메모리에 캐시해둔 파싱 결과를 재사용한다.
        self._cached_data: Optional[dict] = None
        self._cached_mtime: Optional[float] = None
        self._cached_status: Optional[dict] = None
        self._cached_status_mtime: Optional[float] = None

    def _heartbeat_is_fresh(self) -> bool:
        if not self.heartbeat_path.exists():
            return False
        hb_age = time.time() - float(self.heartbeat_path.read_text(encoding="utf-8").strip())
        return hb_age <= self.worker_max_staleness_sec

    def _read(self) -> Optional[dict]:
        try:
            if not self._heartbeat_is_fresh():
                return None  # 워커가 죽었거나 응답불능 — 파일 내용을 믿지 않음
            mtime = self.cache_path.stat().st_mtime
            if self._cached_data is not None and mtime == self._cached_mtime:
                return self._cached_data
            import json
            data = json.loads(self.cache_path.read_text(encoding="utf-8"))
            self._cached_data = data
            self._cached_mtime = mtime
            return data
        except Exception:
            return None

    def _read_status(self) -> Optional[dict]:
        if self.status_path is None:
            return None
        try:
            if not self._heartbeat_is_fresh():
                return None
            mtime = self.status_path.stat().st_mtime
            if self._cached_status is not None and mtime == self._cached_status_mtime:
                return self._cached_status
            import json
            data = json.loads(self.status_path.read_text(encoding="utf-8"))
            self._cached_status = data
            self._cached_status_mtime = mtime
            return data
        except Exception:
            return None

    def has_sufficient_history(self, symbol: str, min_len: int) -> bool:
        data = self._read()
        if data is None:
            return False
        return len(data.get("rows_by_symbol", {}).get(symbol, [])) >= min_len

    def health(self) -> Optional[dict]:
        """[2026-08-10, Codex 제안 반영] 워커가 dump_cache()에 함께 실어보낸 health
        스냅샷(last_market_message_ts/message_count_60s/error_count_60s/
        consecutive_read_loop_errors)을 그대로 반환한다. 워커가 죽었거나(하트비트 실패)
        구버전 워커라 health 필드 자체가 없으면 None — 호출부는 반드시 None을 "판단 불가"로
        보수적으로 처리해야 한다(예: 재시작 여부를 이 값 하나로만 단정하면 안 됨)."""
        data = self._read_status()
        if data is None:
            data = self._read()
        if data is None:
            return None
        return data.get("health")

    def is_fresh(self, symbol: str, max_age_sec: float) -> bool:
        data = self._read()
        if data is None:
            return False
        last_ts = data.get("last_update_ts", {}).get(symbol)
        if last_ts is None:
            return False
        return (time.time() - last_ts) <= max_age_sec

    def to_dataframe(self, symbol: str):
        import pandas as pd
        data = self._read()
        if data is None:
            return None
        rows = data.get("rows_by_symbol", {}).get(symbol)
        if not rows:
            return None
        df = pd.DataFrame(rows, columns=RollingKlineCache._COLUMNS)
        df["open_time"] = pd.to_datetime(df["open_time"], unit="ms")
        return df


class MultiFileBackedKlineCache:
    """[2026-08-10, Codex 제안 반영] 시장데이터 워커를 여러 개(샤드)로 나눠 띄웠을 때,
    각 샤드가 서로 다른(겹치지 않는) 심볼 집합을 담당한다 — 이 클래스는 그 샤드별
    FileBackedKlineCache 여러 개를 감싸서, 심볼 하나를 조회하면 그 심볼을 실제로 담당하는
    샤드의 캐시를 찾아 위임한다. Exchange.get_klines()가 기대하는 것과 동일한 인터페이스
    (has_sufficient_history/is_fresh/to_dataframe/health)를 제공해 단일 워커일 때와
    코드를 안 바꿔도 되게 한다."""

    def __init__(self, shard_caches: list):
        self._shards = shard_caches

    def has_sufficient_history(self, symbol: str, min_len: int) -> bool:
        return any(s.has_sufficient_history(symbol, min_len) for s in self._shards)

    def is_fresh(self, symbol: str, max_age_sec: float) -> bool:
        return any(s.is_fresh(symbol, max_age_sec) for s in self._shards)

    def to_dataframe(self, symbol: str):
        for s in self._shards:
            df = s.to_dataframe(symbol)
            if df is not None:
                return df
        return None

    def health(self) -> Optional[dict]:
        """샤드별 health를 합산한다 — message_count/error_count는 전체 합, 연속에러는
        샤드 중 가장 심각한(가장 큰) 값, last_market_message_ts는 가장 최근 값을 쓴다.
        하나라도 health가 없는(죽은) 샤드가 있으면 그 샤드는 집계에서 제외하되, 전부
        없으면 None을 반환해 호출부가 기존(캔들신선도 only) 판단으로 폴백하게 한다."""
        snapshots = [s.health() for s in self._shards]
        snapshots = [s for s in snapshots if s is not None]
        if not snapshots:
            return None
        return {
            "last_market_message_ts": max(s.get("last_market_message_ts", 0) for s in snapshots),
            "message_count_60s": sum(s.get("message_count_60s", 0) for s in snapshots),
            "error_count_60s": sum(s.get("error_count_60s", 0) for s in snapshots),
            "consecutive_read_loop_errors": max(s.get("consecutive_read_loop_errors", 0) for s in snapshots),
        }


class FileBackedFillTracker:
    """FileBackedKlineCache와 같은 원리로, 워커 프로세스가 남긴 체결정보 파일을 읽기만
    한다. record_trade_ledger가 기대하는 OrderFill과 동일한 필드를 가진 간단한 네임스페이스
    객체를 반환한다(dataclass가 아니어도 속성 접근만 되면 충분)."""

    def __init__(self, cache_path, heartbeat_path, worker_max_staleness_sec: float = 30.0, status_path=None):
        self.cache_path = Path(cache_path)
        self.heartbeat_path = Path(heartbeat_path)
        self.worker_max_staleness_sec = worker_max_staleness_sec
        self.status_path = Path(status_path) if status_path is not None else None

    def get(self, symbol: str) -> Optional[OrderFill]:
        try:
            if not self.heartbeat_path.exists():
                return None
            hb_age = time.time() - float(self.heartbeat_path.read_text(encoding="utf-8").strip())
            if hb_age > self.worker_max_staleness_sec:
                return None
            import json
            source_path = self.status_path if self.status_path is not None and self.status_path.exists() else self.cache_path
            data = json.loads(source_path.read_text(encoding="utf-8"))
            fill_dict = data.get("fills", {}).get(symbol)
            if fill_dict is None:
                return None
            return OrderFill(**fill_dict)
        except Exception:
            return None
