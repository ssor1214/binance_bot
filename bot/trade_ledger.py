"""[2026-08-09] 실제 순손익 원장.

기존 .bot_stats.json은 누적 승/패/손익만 남기고 거래 하나하나의 상세(진입/청산 사유,
수수료, 슬리피지, 그 시점 설정값 등)는 남기지 않는다 — "지금 손익이 mark price 기반
추정값인지 실제 체결 기준인지" 구분이 안 되고, 사후 분석(어떤 조건에서 기대값이
마이너스였는지)도 할 수 없다. 이 모듈은 거래 1건마다 하나의 레코드를 JSONL로 남겨서
그런 분석이 가능하게 한다.

[범위 제한] 실시간 체결가/수수료/펀딩비는 User Data Stream(WebSocket)이 있어야 정확히
잡을 수 있는데 아직 그 인프라가 없다. 지금은 REST 폴링으로 얻을 수 있는 값(추정 pnl,
mark price 등)을 우선 기록하고, 정확한 값(commission_usdt, commission_asset,
funding_fee_usdt, slippage_pct)은 알 수 있을 때만 채우는 방식으로 스키마를 먼저
확정한다. WebSocket 레이어가 생기면 같은 스키마에 정확한 값을 채우기만 하면 된다.
"""
import json
import logging
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

log = logging.getLogger("bot.trade_ledger")

DEFAULT_LEDGER_PATH = Path(__file__).resolve().parent.parent / "logs" / "trade_ledger.jsonl"
DEFAULT_OPEN_POSITIONS_PATH = Path(__file__).resolve().parent.parent / "logs" / "open_bot_positions.json"


def _is_test_process() -> bool:
    argv = " ".join(sys.argv).lower()
    return "unittest" in argv or "pytest" in argv


def _is_default_ledger_path(path: Path) -> bool:
    try:
        return path.resolve() == DEFAULT_LEDGER_PATH.resolve()
    except Exception:
        return path == DEFAULT_LEDGER_PATH


@dataclass
class TradeRecord:
    """거래 1건(진입~청산)의 상세 기록. 필드 중 실시간 체결 기반 값(commission_usdt 등)은
    현재 인프라(REST 폴링)로는 항상 정확히 채울 수 없어 None을 허용한다 — "모른다"와
    "0이다"를 구분하기 위함."""

    symbol: str
    side: str  # "LONG" / "SHORT"
    origin: str  # "bot" / "manual"
    entry_reason: str  # 예: "PUMP_SIGNAL"
    exit_reason: str  # 예: "TAKE_PROFIT", "STOP_LOSS", "EARLY_EXIT", "TIME_STOP" 등
    entry_price: float
    exit_price: float
    quantity: float
    leverage: float
    entered_at: float  # epoch seconds
    exited_at: float  # epoch seconds
    held_seconds: float
    estimated_pnl_pct: float  # mark price 기반 추정 손익률(레버리지 반영 전)
    estimated_pnl_usdt: float  # mark price 기반 추정 손익(USDT)
    bot_version: str  # 이 거래를 처리한 코드 버전(짧은 식별자, 예: git rev나 날짜 태그)
    config_snapshot: dict  # 진입 시점의 주요 전략 설정값 스냅샷
    # 아래는 WebSocket/User Data Stream 도입 전엔 채워지지 않을 수 있는 값들
    actual_fill_entry_price: float | None = None
    actual_fill_exit_price: float | None = None
    commission_usdt: float | None = None
    commission_asset: str | None = None
    funding_fee_usdt: float | None = None
    slippage_pct: float | None = None  # (실제 체결가 - 의도한 가격) / 의도한 가격
    recorded_at: float = field(default_factory=time.time)


def strategy_config_snapshot(cfg) -> dict:
    """TradeRecord.config_snapshot에 넣을, 손익에 직접 영향을 주는 핵심 설정값만 추린다.
    cfg 전체를 덤프하면(API 키 등 민감정보 포함 가능) 위험하므로 화이트리스트 방식으로 고른다."""
    return {
        "stop_loss_pct": cfg.stop_loss_pct,
        "take_profit_min": cfg.take_profit_min,
        "short_take_profit_min": cfg.short_take_profit_min,
        "take_profit_hard_cap": cfg.take_profit_hard_cap,
        "trail_drawdown_pct": cfg.trail_drawdown_pct,
        "leverage_min": cfg.leverage_min,
        "leverage_max": cfg.leverage_max,
        "pump_min_candle_chg_pct": cfg.pump_min_candle_chg_pct,
        "pump_min_volume_ratio": cfg.pump_min_volume_ratio,
        "min_entry_probability": cfg.min_entry_probability,
        "short_min_entry_probability": cfg.short_min_entry_probability,
        "mtf_min_agree_ratio": cfg.mtf_min_agree_ratio,
        "average_down_enabled": cfg.average_down_enabled,
        "fee_rate_roundtrip": cfg.fee_rate_roundtrip,
    }


def append_trade_record(record: TradeRecord, path: Path | str = DEFAULT_LEDGER_PATH) -> None:
    """레코드 1건을 JSONL 파일에 추가한다. 쓰기 실패해도 예외를 던지지 않고 로그만 남긴다
    (원장 기록 실패가 실거래 로직을 막으면 안 됨 — 로깅과 동일한 성격의 부가 기능)."""
    path = Path(path)
    try:
        if _is_test_process() and _is_default_ledger_path(path):
            log.error("refusing to write test trade record to live ledger path: %s", path)
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(asdict(record), ensure_ascii=False) + "\n")
    except Exception:
        log.exception("거래 원장 기록 실패 (거래 자체는 정상 처리됨)")


def load_trade_records(path: Path | str = DEFAULT_LEDGER_PATH) -> list[dict]:
    """JSONL 원장을 읽어 dict 리스트로 반환한다. 파일이 없으면 빈 리스트."""
    path = Path(path)
    if not path.exists():
        return []
    records = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                log.warning("거래 원장의 잘못된 줄 무시: %r", line[:100])
    return records


def load_open_bot_positions(path: Path | str = DEFAULT_OPEN_POSITIONS_PATH) -> dict[str, dict]:
    """Load locally tracked bot-origin open positions.

    This file is intentionally separate from .bot_stats.json. Stats can reset or
    be rebuilt, but origin classification must survive restarts so manually
    opened positions are not counted as bot trades.
    """
    path = Path(path)
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        log.exception("open bot position state load failed; treating as empty")
        return {}
    return data if isinstance(data, dict) else {}


def save_open_bot_positions(positions: dict[str, dict], path: Path | str = DEFAULT_OPEN_POSITIONS_PATH) -> None:
    path = Path(path)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(positions, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        log.exception("open bot position state save failed")


def mark_bot_position_open(symbol: str, side: str, entry_price: float, quantity: float,
                           leverage: float, path: Path | str = DEFAULT_OPEN_POSITIONS_PATH) -> None:
    positions = load_open_bot_positions(path)
    positions[symbol] = {
        "symbol": symbol,
        "side": side,
        "entry_price": float(entry_price),
        "quantity": float(quantity),
        "leverage": float(leverage),
        "opened_at": time.time(),
    }
    save_open_bot_positions(positions, path)


def mark_position_closed(symbol: str, path: Path | str = DEFAULT_OPEN_POSITIONS_PATH) -> None:
    positions = load_open_bot_positions(path)
    if symbol in positions:
        positions.pop(symbol, None)
        save_open_bot_positions(positions, path)


def infer_open_position_origin(symbol: str, side: str, entry_price: float, quantity: float,
                               path: Path | str = DEFAULT_OPEN_POSITIONS_PATH) -> str:
    """Return bot only when the live position matches a locally recorded bot entry."""
    record = load_open_bot_positions(path).get(symbol)
    if not record:
        return "manual"
    if record.get("side") != side:
        return "manual"
    try:
        saved_entry = float(record.get("entry_price", 0.0))
        saved_qty = float(record.get("quantity", 0.0))
        entry = float(entry_price)
        qty = abs(float(quantity))
    except (TypeError, ValueError):
        return "manual"
    entry_ok = abs(saved_entry - entry) <= max(abs(saved_entry), abs(entry), 1.0) * 0.002
    qty_ok = abs(saved_qty - qty) <= max(abs(saved_qty), abs(qty), 1.0) * 0.01
    return "bot" if entry_ok and qty_ok else "manual"


def find_open_bot_position_opened_at(symbol: str, side: str, entry_price: float, quantity: float,
                                      path: Path | str = DEFAULT_OPEN_POSITIONS_PATH) -> float | None:
    """[2026-08-14 실측 사고] 재시작 시 sync_existing_positions()가 entered_at을 항상 '지금'으로
    새로 채우고 stop_loss_widened를 항상 False로 초기화해서, 진입 유예기간(180초) 동안 넓혀둔
    손절폭(~20% ROE)이 재시작 이후 영원히 원래 폭(8%)으로 안 좁혀지는 버그가 있었다(APRUSDT
    -20.1% ROE 손절 실사고). infer_open_position_origin()과 동일한 매칭 기준으로 이 심볼의
    실제 최초 진입시각(opened_at)을 찾아 반환한다 — 못 찾으면 None(호출측이 안전하게 폴백)."""
    record = load_open_bot_positions(path).get(symbol)
    if not record or record.get("side") != side:
        return None
    try:
        saved_entry = float(record.get("entry_price", 0.0))
        saved_qty = float(record.get("quantity", 0.0))
        entry = float(entry_price)
        qty = abs(float(quantity))
        opened_at = float(record.get("opened_at"))
    except (TypeError, ValueError):
        return None
    entry_ok = abs(saved_entry - entry) <= max(abs(saved_entry), abs(entry), 1.0) * 0.002
    qty_ok = abs(saved_qty - qty) <= max(abs(saved_qty), abs(qty), 1.0) * 0.01
    return opened_at if entry_ok and qty_ok else None
