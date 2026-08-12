"""[2026-08-10] WS(시장데이터/계정스트림) 연결을 완전히 별도 OS 프로세스로 격리하는 워커.

배경: python-binance 1.0.37의 내부 read loop 버그가 재연결 실패로 끝나는 게 아니라
**프로세스 자체를 CPU 붙잡는 tight loop로 응답불능**에 빠뜨리는 게 실거래에서 실측됐다
(터미널 입력/Ctrl+C도 안 먹힘). 이 상태에서는 봇 프로세스 내부의 어떤 감시 로직도
무의미하다 — 같은 프로세스/스레드가 GIL을 뺏겨 있으면 아무것도 못 하기 때문이다.

해결: WS 연결을 이 파일(`python -m bot.ws_worker`)로 완전히 분리된 프로세스에서 돌린다.
- 이 프로세스가 얼어붙어도 메인 봇 프로세스(bot.main)는 전혀 영향받지 않는다 — 별도
  프로세스라 GIL을 공유하지 않는다.
- 메인 프로세스는 이 워커가 파일에 남기는 하트비트만 감시하다가, 오래 멈추면
  `Popen.kill()`로 강제종료한다 — OS 레벨 강제종료는 내부 상태와 무관하게 항상 통한다.
- 실제 캔들/체결 데이터도 파일(JSON, 원자적 쓰기)로 넘긴다 — 메인 프로세스는 그 파일을
  읽기만 하면 되고, 워커가 죽어있는 동안은 기존 신선도(is_fresh) 검사가 자동으로
  REST 폴백을 선택하므로 항상 안전하다(기존 안전장치 그대로 재사용).

이 모듈은 단독 실행(`python -m bot.ws_worker`)을 전제로 하며, bot.main이 서브프로세스로
띄운다. 표준입력으로 아무것도 받지 않고, .env를 직접 읽어 자기 설정을 구성한다(부모와
동일한 설정을 보게 하기 위함 — 별도 인자 전달 불필요)."""
from __future__ import annotations

import json
import logging
import os
import sys
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bot.config import Config
from bot.exchange import Exchange
from bot.ws_client import FillTracker, MarketDataWebSocket, RollingKlineCache, UserDataWebSocket, WsHealthMonitor

LOG_DIR = Path(__file__).resolve().parent.parent / "logs"
LOG_DIR.mkdir(exist_ok=True)

# [2026-08-10, Codex 제안 반영] 샤딩 지원 — main.py가 이 워커를 여러 프로세스로 띄울 때
# 각자 역할("market"/"user")과 샤드 번호를 환경변수로 넘긴다. 인자를 안 받으면(단독 실행,
# 기존 테스트/수동 실행) role="both", shard_index=0, shard_count=1로 기존과 완전히 동일하게
# 동작한다 — 하위호환.
WORKER_ROLE = os.environ.get("WS_WORKER_ROLE", "both")  # "market" | "user" | "both"
SHARD_INDEX = int(os.environ.get("WS_SHARD_INDEX", "0"))
SHARD_COUNT = int(os.environ.get("WS_SHARD_COUNT", "1"))

_suffix = ""
if WORKER_ROLE == "market" and SHARD_COUNT > 1:
    _suffix = f"_market{SHARD_INDEX}"
elif WORKER_ROLE == "user":
    _suffix = "_user"
WS_CACHE_PATH = LOG_DIR / f"ws_worker_cache{_suffix}.json"
WS_STATUS_PATH = LOG_DIR / f"ws_worker_status{_suffix}.json"
WS_HEARTBEAT_PATH = LOG_DIR / f"ws_worker_heartbeat{_suffix}.txt"

# [2026-08-11 사용자요청 리뷰로 발견] 기존엔 회전 없는 FileHandler라, python-binance
# read-loop 버그가 에러를 초당 수천 건씩 쏟아내는 순간(오늘 90분 테스트에서 실측
# error_count_60s=269,229) 로그 파일이 무제한으로 불어나 디스크를 잠식할 수 있었다
# (실측 147MB). RotatingFileHandler로 상한을 걸고, 샤드마다 파일명도 분리한다(여러
# 워커 프로세스가 같은 로그 파일에 동시에 쓰면서 줄이 섞이는 것도 방지).
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(),
        RotatingFileHandler(LOG_DIR / f"ws_worker{_suffix}.log", maxBytes=5_000_000, backupCount=5, encoding="utf-8"),
    ],
)
log = logging.getLogger("bot.ws_worker")


def _atomic_write_json(path: Path, data: dict) -> None:
    """부분 쓰기 상태를 읽는 걸 방지하기 위해 임시파일에 쓴 뒤 os.replace로 원자적 교체한다
    — 메인 프로세스가 하필 쓰는 도중에 파일을 읽어서 깨진 JSON을 만나는 경우를 없앤다.

    [2026-08-10 실거래 검증에서 발견] Windows에서 방금 쓴 임시파일을 os.replace()로 교체하려
    할 때 `PermissionError: [WinError 5]`가 간헐적으로 발생했다 — 메인 프로세스가 이 파일을
    자주(250심볼 스캔마다) read_text()로 여는 것과 겹칠 때 Windows가 잠깐 배타적 접근을
    요구하는 게 원인으로 추정된다. 1차로 5회(최대 0.75초) 재시도를 넣었으나 실거래 검증에서
    여전히 60초간 8회 실패가 나와 부족함이 확인됨 — 재시도 횟수/총 대기시간을 늘렸다
    (최대 20회, 총 약 2초까지). 그래도 실패하면(진짜 심각한 문제) 예외를 던져 호출부가
    로그만 남기고 다음 2초 주기에 다시 시도하게 한다 — 어느 쪽이든 캐시가 잠깐 갱신 안 될
    뿐 메인 프로세스는 신선도 검사로 항상 안전하게 REST 폴백한다."""
    tmp_path = path.with_suffix(".tmp")
    tmp_path.write_text(json.dumps(data), encoding="utf-8")
    last_error = None
    for attempt in range(20):
        try:
            tmp_path.replace(path)
            return
        except PermissionError as e:
            last_error = e
            time.sleep(0.1)
    raise last_error


def _snapshot_fills(fill_tracker) -> dict:
    fills = {}
    if fill_tracker is not None:
        with fill_tracker._lock:
            for symbol, fill in fill_tracker._last_fill.items():
                fills[symbol] = {
                    "symbol": fill.symbol, "side": fill.side, "avg_price": fill.avg_price,
                    "commission": fill.commission, "commission_asset": fill.commission_asset,
                    "realized_pnl": fill.realized_pnl, "order_status": fill.order_status,
                    "event_time_ms": fill.event_time_ms,
                }
    return fills


def dump_status(symbol_count: int, fill_tracker, health: WsHealthMonitor | None = None) -> None:
    """작고 자주 필요한 상태만 별도 파일로 기록한다."""
    try:
        payload = {
            "dumped_at": time.time(),
            "role": WORKER_ROLE,
            "shard_index": SHARD_INDEX,
            "shard_count": SHARD_COUNT,
            "symbol_count": symbol_count,
            "fills": _snapshot_fills(fill_tracker),
        }
        if health is not None:
            payload["health"] = health.snapshot()
        _atomic_write_json(WS_STATUS_PATH, payload)
        WS_HEARTBEAT_PATH.write_text(str(time.time()), encoding="utf-8")
    except Exception:
        log.exception("WS status dump failed; continuing")


def dump_cache(kline_cache: RollingKlineCache, fill_tracker, health: WsHealthMonitor | None = None) -> None:
    """kline_cache/fill_tracker의 현재 상태를 파일로 내보낸다. 이 함수 자체가 실패해도
    (디스크 문제 등) 워커 프로세스가 죽으면 안 되므로 예외를 삼킨다 — 죽으면 하트비트가
    끊겨서 메인 프로세스가 재시작시킬 텐데, 그보다는 데이터 없이라도 계속 살아서 재시도하는
    편이 낫다.

    [2026-08-10, Codex 제안 반영] "프로세스가 파일을 쓰고 있다"는 것과 "실제로 시장데이터를
    수신하고 있다"는 것은 다르다 — read loop가 죽어도 이 while 루프(dump_cache 호출부)
    자체는 안 죽고 계속 파일을 갱신할 수 있어(빈 캐시라도), 하트비트만 보면 "살아있다"고
    오판할 위험이 있었다. health.snapshot()을 같이 실어 실제 수신 여부까지 판단 근거로 쓴다."""
    try:
        with kline_cache._lock:
            rows_by_symbol = {s: list(rows) for s, rows in kline_cache._rows.items()}
            last_update_ts = dict(kline_cache._last_update_ts)
        payload = {
            "dumped_at": time.time(),
            "rows_by_symbol": rows_by_symbol,
            "last_update_ts": last_update_ts,
            "fills": _snapshot_fills(fill_tracker),
        }
        if health is not None:
            payload["health"] = health.snapshot()
        _atomic_write_json(WS_CACHE_PATH, payload)
    except Exception:
        log.exception("캐시 덤프 실패(무시하고 계속 진행)")


def run() -> None:
    cfg = Config()
    run_market = cfg.ws_market_data_enabled and WORKER_ROLE in ("market", "both")
    run_user = cfg.ws_user_data_enabled and WORKER_ROLE in ("user", "both")
    if not (run_market or run_user):
        log.info("이 워커(역할=%s)가 담당할 기능이 꺼져있어 시작하지 않고 즉시 종료합니다", WORKER_ROLE)
        return

    ex = Exchange(cfg)
    symbols = []
    if run_market:
        worker_symbols_raw = os.environ.get("WS_WORKER_SYMBOLS", "").strip()
        if worker_symbols_raw:
            try:
                all_symbols = [str(s).strip().upper() for s in json.loads(worker_symbols_raw) if str(s).strip()]
            except Exception:
                log.exception("WS_WORKER_SYMBOLS 파싱 실패 — 워커 내부 설정으로 폴백")
                all_symbols = ex.get_active_usdt_perpetual_symbols(limit=cfg.max_auto_symbols) if cfg.auto_symbols else cfg.symbols
        else:
            all_symbols = ex.get_active_usdt_perpetual_symbols(limit=cfg.max_auto_symbols) if cfg.auto_symbols else cfg.symbols
        # [2026-08-10, Codex 제안 반영] 샤드 개수만큼 심볼을 나눠 맡는다. 연속 구간이 아니라
        # interleave(0,2,4,...) 방식으로 나눠서, 거래대금 순위가 한쪽 샤드에 몰리지 않고
        # 고르게 분산되게 한다(순위 상위/하위가 섞여야 부하도 고르게 나뉨).
        symbols = all_symbols[SHARD_INDEX::SHARD_COUNT] if SHARD_COUNT > 1 else all_symbols
        log.info("이 워커(샤드 %d/%d) 담당 심볼 %d개(전체 %d개 중)", SHARD_INDEX, SHARD_COUNT, len(symbols), len(all_symbols))

    kline_cache = RollingKlineCache(max_len=cfg.ws_kline_history_len)
    fill_tracker = FillTracker(log_events=True, event_log_dir=LOG_DIR) if run_user else None
    # [2026-08-10, Codex 제안 반영] 메시지 수신/에러 상태를 별도로 추적 — 하트비트 파일이
    # 갱신되는 것만으로는 "read loop가 실제로 살아서 데이터를 받는지"를 알 수 없다.
    health = WsHealthMonitor() if run_market else None

    if run_market:
        # [2026-08-10 사용자요청] "청크 분할 없는 REST 초기 시딩은 IP 밴 위험" — 오늘 실제로
        # 겪은 IP 차단 사고와 같은 종류. 샤딩된 워커 여러 개가 동시에 각자 시딩을 시작하면
        # 순간 REST 호출이 몰릴 수 있어, 심볼을 청크로 나눠 요청하고 청크 사이 쉬어준다.
        chunk_size = max(1, cfg.ws_seed_chunk_size)
        for i in range(0, len(symbols), chunk_size):
            chunk = symbols[i:i + chunk_size]
            for s in chunk:
                try:
                    kline_cache.seed(s, ex.get_klines(s, limit=min(cfg.ws_kline_history_len, 99)))
                except Exception:
                    log.exception("[%s] 초기 히스토리 시딩 실패", s)
            if i + chunk_size < len(symbols):
                time.sleep(cfg.ws_seed_chunk_delay_sec)

    from binance import ThreadedWebsocketManager
    twm = ThreadedWebsocketManager(api_key=cfg.api_key, api_secret=cfg.api_secret, testnet=cfg.use_testnet)
    twm.start()

    if run_market:
        market_ws = MarketDataWebSocket(api_key=cfg.api_key, api_secret=cfg.api_secret, testnet=cfg.use_testnet,
                                         on_kline=kline_cache.append_closed, health=health)
        market_ws.start(symbols, interval=cfg.interval, twm=twm)
        log.info("시장데이터 연결 완료 — 심볼 %d개", len(symbols))

    if run_user:
        user_ws = UserDataWebSocket(api_key=cfg.api_key, api_secret=cfg.api_secret, testnet=cfg.use_testnet,
                                     on_account_update=fill_tracker.on_message)
        user_ws.start(twm=twm)
        log.info("계정(체결) 스트림 연결 완료")

    status_interval = max(0.5, cfg.ws_status_dump_interval_sec)
    cache_interval = max(status_interval, cfg.ws_cache_dump_interval_sec)
    last_cache_dump_at = 0.0
    log.info("WS 워커 시작 완료(역할=%s) — status %.1f초, full cache %.1f초", WORKER_ROLE, status_interval, cache_interval)
    while True:
        dump_status(len(symbols), fill_tracker, health)
        now = time.time()
        if now - last_cache_dump_at >= cache_interval:
            dump_cache(kline_cache, fill_tracker, health)
            last_cache_dump_at = now
        time.sleep(status_interval)


if __name__ == "__main__":
    run()
