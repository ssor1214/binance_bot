"""[2026-08-14] aggTrade(체결) 스트림 전용 프로세스 격리 워커.

bot/ws_worker.py(시장데이터/계정스트림 워커)와 동일한 안정성 패턴을 그대로 aggTrade
스트림에 적용한 것 — python-binance 1.0.37의 내부 read loop가 예외 없이 프로세스를
통째로 응답불능(tight loop, GIL 점유) 상태로 만드는 버그는, 같은 프로세스 안의 어떤
감시 로직으로도 못 막고 오직 완전한 OS 프로세스 격리 + 하트비트 기반 Popen.kill()
워치독으로만 막을 수 있다는 실측 전례를 그대로 따른다.

이 모듈이 절대 하지 않는 것:
- bot/main.py / run_forever.py가 감시하는 라이브 프로세스에 이 모듈을 연결(import)하지
  않는다. bot/ws_trade_client.py의 TradeStreamWebSocket을 별도 프로세스에서 돌리는
  독립 실행 스크립트일 뿐이다. 워치독(재시작/백오프/헬스체크)은 이 워커를 기동하는
  쪽(현재는 qa_* 검증 스크립트)에서 bot/main.py의 ws_layer_needs_restart() /
  _compute_ws_restart_backoff_sec() 패턴을 그대로 재사용해 구현한다.
- ThreadedWebsocketManager 내부를 수정하지 않는다.

단독 실행: `python -m bot.ws_trade_worker`
환경변수:
- WS_TRADE_WORKER_SYMBOLS: JSON 배열 문자열(예: '["BTCUSDT","ETHUSDT"]'). 없으면
  cfg.symbols(또는 auto_symbols)를 그대로 사용한다.
- WS_TRADE_SHARD_INDEX / WS_TRADE_SHARD_COUNT: main.py의 시장데이터 워커와 동일한
  interleave 샤딩(0,2,4,... 방식)을 지원한다. 안 주면 shard_index=0, shard_count=1로
  기존과 동일하게 동작한다(하위호환).
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bot.config import Config
from bot.exchange import Exchange
from bot.ws_client import WsHealthMonitor
from bot.ws_trade_client import TradeStreamWebSocket, TradeStreamWebSocketV2, detect_volume_spike

LOG_DIR = Path(__file__).resolve().parent.parent / "logs"
LOG_DIR.mkdir(exist_ok=True)

SHARD_INDEX = int(os.environ.get("WS_TRADE_SHARD_INDEX", "0"))
SHARD_COUNT = int(os.environ.get("WS_TRADE_SHARD_COUNT", "1"))

_suffix = f"_trade{SHARD_INDEX}" if SHARD_COUNT > 1 else "_trade"
WS_STATUS_PATH = LOG_DIR / f"ws_trade_worker_status{_suffix}.json"
WS_HEARTBEAT_PATH = LOG_DIR / f"ws_trade_worker_heartbeat{_suffix}.txt"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(),
        RotatingFileHandler(LOG_DIR / f"ws_trade_worker{_suffix}.log", maxBytes=5_000_000, backupCount=5, encoding="utf-8"),
    ],
)
log = logging.getLogger("bot.ws_trade_worker")

# [2026-08-16 사용자요청] python-binance의 read-loop 에러("Read loop has been closed")는
# 초당 수천 줄까지 쏟아진다(실측: 53분간 error_count 8만~47만, 20초에 약 25,000줄). 정작
# 데이터 수신은 정상이었으므로(25,520메시지/60초) 이 로그 자체가 tight loop의 실질 부하
# 상당 부분을 차지한다 — 파일/콘솔 I/O가 GIL을 붙잡는다.
#
# [주의] 처음엔 setLevel(CRITICAL)로 막으려 했으나, WsHealthMonitor가 바로 이 로거들에
# _ReadLoopErrorWatcher 핸들러를 붙여 에러를 세고 있어서(bot/ws_client.py:81-82) 레벨을
# 올리면 ERROR 레코드가 핸들러에 도달하지 못해 error_count 집계 자체가 죽는다 — 부하는
# 줄지만 관측성을 통째로 잃는다(테스트 3건이 이를 잡아냄).
# 그래서 레벨은 그대로 두고 propagate만 끊는다: 로거에 직접 붙은 _ReadLoopErrorWatcher는
# 계속 레코드를 받아 카운팅하고, root의 RotatingFileHandler/StreamHandler(실제 I/O 비용)
# 로는 전파되지 않는다. 부하만 제거하고 관측성은 보존하는 절충.
logging.getLogger("binance.ws.threaded_stream").propagate = False
logging.getLogger("binance.ws.reconnecting_websocket").propagate = False


def _atomic_write_json(path: Path, data: dict) -> None:
    """bot/ws_worker.py의 _atomic_write_json과 동일한 패턴(Windows PermissionError
    재시도 포함) — 별도 모듈로 뽑지 않고 그대로 복제해서, 이 워커가 기존 ws_worker.py의
    내부 구현에 의존하지 않고 완전히 독립적으로 동작하게 유지한다."""
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


WORKER_PID_FILE = WS_STATUS_PATH.parent / f"ws_trade_worker{_suffix}_pid.json"


def _pid_alive(pid: int) -> bool:
    """Windows 에서는 signal 0 이 통하지 않을 수 있어 tasklist 로 확인한다."""
    if pid <= 0:
        return False
    if os.name == "nt":
        try:
            out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}"],
                                 capture_output=True, text=True, timeout=10)
            return str(pid) in (out.stdout or "")
        except Exception:
            return False
    try:
        os.kill(pid, 0)
        return True
    except Exception:
        return False


def _acquire_worker_lock() -> bool:
    """같은 샤드가 이미 돌고 있으면 False. 자세한 이유는 호출부 주석 참조."""
    try:
        if WORKER_PID_FILE.exists():
            old = json.loads(WORKER_PID_FILE.read_text(encoding="utf-8"))
            opid = int(old.get("pid") or 0)
            if opid and opid != os.getpid() and _pid_alive(opid):
                log.warning("체결 워커(샤드 %d) 가 이미 PID %d 로 실행 중 — 기동 중단",
                            SHARD_INDEX, opid)
                return False
    except Exception:
        pass
    try:
        WORKER_PID_FILE.parent.mkdir(parents=True, exist_ok=True)
        WORKER_PID_FILE.write_text(
            json.dumps({"pid": os.getpid(), "shard": SHARD_INDEX, "ts": time.time()}),
            encoding="utf-8")
    except Exception:
        pass
    return True


def dump_status(symbols: list, ws: TradeStreamWebSocket, health: WsHealthMonitor,
                cfg: Config) -> None:
    try:
        spikes = {}
        for s in symbols:
            try:
                # [2026-08-19] cfg 값을 안 넘겨 .env의 SPIKE_ENTRY_* 설정이 워커에는
                # 반영되지 않고 있었다(현재는 기본값과 같아 실해는 없었으나 잠재 버그).
                spikes[s] = detect_volume_spike(
                    ws.cache, s,
                    spike_multiplier=cfg.spike_entry_multiplier,
                    spike_window_sec=cfg.spike_entry_window_sec,
                    baseline_window_sec=cfg.spike_entry_baseline_sec,
                )
            except Exception:
                continue
        payload = {
            "dumped_at": time.time(),
            "role": "trade",
            "shard_index": SHARD_INDEX,
            "shard_count": SHARD_COUNT,
            "symbol_count": len(symbols),
            "health": health.snapshot(),
            "spikes": spikes,
        }
        _atomic_write_json(WS_STATUS_PATH, payload)
        WS_HEARTBEAT_PATH.write_text(str(time.time()), encoding="utf-8")
    except Exception:
        log.exception("체결 워커 status dump 실패; 계속 진행")


def _build_trade_stream(cfg: Config, health: WsHealthMonitor):
    """기본은 기존 python-binance 경로를 유지하고, 명시적 설정일 때만 V2를 사용한다."""
    stream_cls = TradeStreamWebSocketV2 if cfg.ws_trade_use_v2 else TradeStreamWebSocket
    return stream_cls(api_key=cfg.api_key, api_secret=cfg.api_secret, testnet=cfg.use_testnet, health=health)


def run() -> None:
    cfg = Config()

    symbols_raw = os.environ.get("WS_TRADE_WORKER_SYMBOLS", "").strip()
    if symbols_raw:
        try:
            all_symbols = [str(s).strip().upper() for s in json.loads(symbols_raw) if str(s).strip()]
        except Exception:
            log.exception("WS_TRADE_WORKER_SYMBOLS 파싱 실패 — cfg 심볼로 폴백")
            all_symbols = None
    else:
        all_symbols = None

    if not all_symbols:
        ex = Exchange(cfg)
        all_symbols = ex.get_active_usdt_perpetual_symbols(limit=cfg.max_auto_symbols) if cfg.auto_symbols else cfg.symbols

    # [2026-08-16 사용자요청] 구독 심볼을 유동성 상위 N개로 제한 — read-loop 버그의 방아쇠인
    # 메시지 폭주량 자체를 줄인다. all_symbols는 get_active_usdt_perpetual_symbols()가
    # 24시간 거래대금(quoteVolume) 내림차순으로 주므로 앞에서 자르면 상위 유동성이 남는다.
    # main.py가 WS_TRADE_WORKER_SYMBOLS로 넘겨줄 때도 같은 순서를 유지한다.
    if cfg.spike_entry_max_symbols > 0 and len(all_symbols) > cfg.spike_entry_max_symbols:
        log.info("체결 워커 구독 심볼 제한: %d개 → 유동성 상위 %d개",
                  len(all_symbols), cfg.spike_entry_max_symbols)
        all_symbols = all_symbols[:cfg.spike_entry_max_symbols]

    symbols = all_symbols[SHARD_INDEX::SHARD_COUNT] if SHARD_COUNT > 1 else all_symbols
    log.info("체결 워커(샤드 %d/%d) 담당 심볼 %d개(전체 %d개 중), testnet=%s",
              SHARD_INDEX, SHARD_COUNT, len(symbols), len(all_symbols), cfg.use_testnet)

    # [2026-08-21] 샤드별 중복 실행 방지.
    # 체결 워커에는 e2/e3 와 달리 중복 기동 방지 장치가 없었다. 같은 샤드가 여러 개
    # 뜨면 같은 스트림에 연결을 중복으로 열어 바이낸스 커넥션 제한에 걸리고,
    # 전부 handshake timeout 으로 밀린다. 오늘 실수로 3개를 띄웠을 때 로그에
    # connected=3 이 찍혀 이 경로를 확인했다. 로그 파일도 공유하므로 사후 분석까지
    # 어려워진다. 살아있는 선행 인스턴스가 있으면 조용히 물러난다.
    if not _acquire_worker_lock():
        return

    health = WsHealthMonitor()
    # [2026-08-16 §3-A 후속] 기본값은 기존 python-binance 경로 유지. 다만 메인넷 선물 raw
    # 무음 원인이 fstream.binance.com/stream의 구식 라우팅이었고, /market/stream 수정 뒤
    # 독립 메인넷 QA에서 20분/20심볼(25,019틱) 클린 통과가 확인됐다. 그래서 라이브 즉시
    # 전환 대신 "명시적 플래그일 때만 V2 사용"하는 안전 스위치로 복귀 경로를 연다.
    ws = _build_trade_stream(cfg, health)
    ws.start(symbols)
    log.info(
        "aggTrade 스트림 연결 완료(%s 경로) — 심볼 %d개",
        "raw-v2" if cfg.ws_trade_use_v2 else "python-binance",
        len(symbols),
    )

    status_interval = max(0.5, cfg.ws_status_dump_interval_sec)
    log.info("체결 워커 시작 완료 — status %.1f초 주기", status_interval)

    # [QA 전용] 실제 python-binance read loop 버그(예외 없이 프로세스가 tight loop로
    # GIL을 붙잡고 응답불능이 되는 현상)를 최대한 충실히 재현하기 위한 훅. 라이브 경로와는
    # 무관하다 — 이 워커 자체가 bot/main.py에 연결돼 있지 않고(qa_* 스크립트 전용),
    # WS_TRADE_QA_FREEZE_AFTER_SEC 환경변수를 명시적으로 준 경우에만 발동한다(기본 미설정).
    # OS 레벨 프로세스 정지(NtSuspendProcess 등)는 격리/가상화 환경에 따라 신뢰할 수
    # 없다는 게 실측으로 확인돼, 대신 프로세스 스스로 하트비트/상태 파일 갱신을 멈추고
    # busy-loop에 빠지는 방식으로 동일 증상(하트비트 정체 + 여전히 kill()엔 응답)을 만든다.
    qa_freeze_after_sec = os.environ.get("WS_TRADE_QA_FREEZE_AFTER_SEC", "").strip()
    freeze_at = time.time() + float(qa_freeze_after_sec) if qa_freeze_after_sec else None

    while True:
        if freeze_at is not None and time.time() >= freeze_at:
            log.warning("[QA] WS_TRADE_QA_FREEZE_AFTER_SEC 도달 — 하트비트 정지 후 tight loop 진입(kill()로만 종료 가능)")
            while True:
                pass  # 의도적 tight loop — 실제 버그의 GIL 점유 증상을 재현
        dump_status(symbols, ws, health, cfg)
        time.sleep(status_interval)


if __name__ == "__main__":
    run()
