import os
from dataclasses import dataclass, field
from pathlib import Path
from dotenv import load_dotenv

ENV_PATH = Path(__file__).resolve().parent.parent / ".env"
load_dotenv(ENV_PATH)


def _bool(name: str, default: str = "true") -> bool:
    return os.getenv(name, default).strip().lower() in ("1", "true", "yes", "y")


def _float(name: str, default: float) -> float:
    return float(os.getenv(name, default))


def _int(name: str, default: int) -> int:
    return int(os.getenv(name, default))


def set_env_value(key: str, value) -> None:
    """실행 중 .env 파일의 값을 갱신한다 (봇 재시작해도 바뀐 값이 유지되도록).
    자동 튜닝(시간당 손익 분석 후 제안 승인)에서 사용한다."""
    val_str = str(value)
    lines = []
    if ENV_PATH.exists():
        with open(ENV_PATH, "r", encoding="utf-8") as f:
            lines = f.readlines()
    found = False
    for i, line in enumerate(lines):
        if line.strip().startswith(f"{key}="):
            lines[i] = f"{key}={val_str}\n"
            found = True
            break
    if not found:
        lines.append(f"{key}={val_str}\n")
    with open(ENV_PATH, "w", encoding="utf-8") as f:
        f.writelines(lines)


# 자동 튜닝(시간당 손익 분석)이 건드릴 수 있는 설정 화이트리스트.
# 레버리지·포지션비중처럼 계좌 전체를 흔드는 값은 제외했지만, 손절선은 좁은 범위(3~6%) 안에서만
# 조정 가능하도록 열어뒀다 — "진입을 얼마나 깐깐하게 거를지" + "손실을 어디서 끊을지"까지 다룬다.
TUNABLE_PARAMS = {
    "MIN_ENTRY_PROBABILITY": ("min_entry_probability", float, 0.55, 0.90),
    "SHORT_MIN_ENTRY_PROBABILITY": ("short_min_entry_probability", float, 0.60, 0.92),
    "TAKER_IMBALANCE_THRESHOLD": ("taker_imbalance_threshold", float, 0.50, 0.65),
    "REQUIRE_TWO_CANDLE_CONFIRMATION": ("require_two_candle_confirmation", bool, None, None),
    "PROFIT_HOLD_REVERSAL_VOTES": ("profit_hold_reversal_votes", int, 1, 3),
    "STOP_LOSS_PCT": ("stop_loss_pct", float, 3.0, 6.0),
    # 아래는 1시간 자동분석이 스스로 제안하진 않고, 사용자가 텔레그램에서 "설정변경 KEY 값"으로
    # 직접 요청할 때만 쓰인다 — 본인이 명시적으로 입력한 값이라 범위를 좀 더 넓게 열어뒀다.
    "TAKE_PROFIT_MIN": ("take_profit_min", float, 1.0, 15.0),
    "SHORT_TAKE_PROFIT_MIN": ("short_take_profit_min", float, 1.0, 15.0),
    "TAKE_PROFIT_MAX": ("take_profit_max", float, 1.0, 60.0),
    "TAKE_PROFIT_HARD_CAP": ("take_profit_hard_cap", float, 2.0, 300.0),
    "MIN_SIGNAL_CONFIRMATIONS": ("min_confirmations", int, 1, 2),
    "AGGRESSIVE_MAX_POSITIONS": ("aggressive_max_positions", int, 1, 8),
    "LEVERAGE_MIN": ("leverage_min", int, 1, 10),
    "LEVERAGE_MAX": ("leverage_max", int, 1, 20),
    "BTC_SUDDEN_MOVE_PCT": ("btc_sudden_move_pct", float, 0.3, 5.0),
    # [2026-08-11 사용자요청] "30분마다 복기하고 개선 제안은 텔레그램 승인/직접요청으로만" —
    # 복기 결과가 제안할 수 있는 대상 파라미터들을 추가.
    "SHORT_STOP_LOSS_PCT": ("short_stop_loss_pct", float, 0.0, 6.0),
    "STOP_LOSS_GRACE_SEC": ("stop_loss_grace_sec", float, 0.0, 180.0),
    "STOP_LOSS_GRACE_WIDEN_MULT": ("stop_loss_grace_widen_mult", float, 1.0, 3.0),
    "TRAIL_DRAWDOWN_PCT": ("trail_drawdown_pct", float, 0.3, 3.0),
    "STALE_SIGNAL_TOLERANCE_PCT": ("stale_signal_tolerance_pct", float, 0.15, 1.5),
    "MAX_ATR_VS_STOP_RATIO": ("max_atr_vs_stop_ratio", float, 0.0, 8.0),
    "SOFT_STOP_MIN_HOLD_SEC": ("soft_stop_min_hold_sec", float, 0.0, 180.0),
    "POSITION_SIZE_MIN": ("position_size_min", float, 0.05, 0.30),
    "POSITION_SIZE_MAX": ("position_size_max", float, 0.05, 0.30),
}

# TUNABLE_PARAMS 키를 텔레그램 화면에 표시할 때 쓰는 한글 설명. 영문 KEY 그대로 보여주면
# 사용자가 뭘 바꾸는 건지 알기 어렵다는 피드백(2026-08-09)에 따라 추가.
TUNABLE_PARAMS_KO = {
    "MIN_ENTRY_PROBABILITY": "롱 진입확률 최소기준",
    "SHORT_MIN_ENTRY_PROBABILITY": "숏 진입확률 최소기준",
    "TAKER_IMBALANCE_THRESHOLD": "매수/매도 체결 쏠림 기준",
    "REQUIRE_TWO_CANDLE_CONFIRMATION": "2봉 연속 확인 사용여부",
    "PROFIT_HOLD_REVERSAL_VOTES": "익절 구간 반전판단 표결수",
    "STOP_LOSS_PCT": "손절 기준(ROE %)",
    "TAKE_PROFIT_MIN": "롱 익절 트레일링 시작(ROE %)",
    "SHORT_TAKE_PROFIT_MIN": "숏 익절 트레일링 시작(ROE %)",
    "TAKE_PROFIT_MAX": "익절 상한(모멘텀 유지시, ROE %)",
    "TAKE_PROFIT_HARD_CAP": "익절 절대상한(무조건 확정, ROE %)",
    "MIN_SIGNAL_CONFIRMATIONS": "신호 확인 최소 지표수",
    "AGGRESSIVE_MAX_POSITIONS": "공격모드 최대 동시포지션",
    "LEVERAGE_MIN": "최소 레버리지",
    "LEVERAGE_MAX": "최대 레버리지",
    "BTC_SUDDEN_MOVE_PCT": "BTC 급변동 감지 기준(%)",
    "SHORT_STOP_LOSS_PCT": "숏 전용 손절폭(ROE %, 0=공용값 사용)",
    "STOP_LOSS_GRACE_SEC": "진입직후 손절 유예시간(초)",
    "STOP_LOSS_GRACE_WIDEN_MULT": "유예기간 중 손절폭 확장배수",
    "TRAIL_DRAWDOWN_PCT": "트레일링 콜백폭 기준(%)",
    "STALE_SIGNAL_TOLERANCE_PCT": "신호 스캔 후 허용 가격이탈(%)",
    "MAX_ATR_VS_STOP_RATIO": "휩쏘필터 변동성 상한배수",
    "SOFT_STOP_MIN_HOLD_SEC": "SOFT_STOP 최소 보유시간(초)",
    "POSITION_SIZE_MIN": "슬롯당 최소 비중",
    "POSITION_SIZE_MAX": "슬롯당 최대 비중",
}


@dataclass
class Config:
    api_key: str = os.getenv("BINANCE_API_KEY", "")
    api_secret: str = os.getenv("BINANCE_API_SECRET", "")
    use_testnet: bool = _bool("USE_TESTNET", "true")

    symbols: list = field(default_factory=lambda: [
        s.strip().upper() for s in os.getenv("SYMBOLS", "BTCUSDT").split(",") if s.strip()
    ])
    auto_symbols: bool = os.getenv("SYMBOLS", "BTCUSDT").strip().upper() == "AUTO"
    max_auto_symbols: int = _int("MAX_AUTO_SYMBOLS", 150)
    interval: str = os.getenv("INTERVAL", "15m")
    leverage_min: int = _int("LEVERAGE_MIN", 3)
    leverage_max: int = _int("LEVERAGE_MAX", 5)
    position_size_min: float = _float("POSITION_SIZE_MIN", 0.1)
    position_size_max: float = _float("POSITION_SIZE_MAX", 0.5)
    position_size_step: float = _float("POSITION_SIZE_STEP", 0.1)
    small_balance_threshold: float = _float("SMALL_BALANCE_THRESHOLD", 100.0)
    small_balance_max_ratio: float = _float("SMALL_BALANCE_MAX_RATIO", 1.0)
    # If the account has already fallen into survival mode, stop opening new
    # positions and keep managing only existing exposure.  Recovery mode below
    # re-opens only the strongest one-shot candidates so a tiny balance can
    # still try to climb back without fanning out into low-quality churn.
    low_balance_new_entry_pause_threshold: float = _float("LOW_BALANCE_NEW_ENTRY_PAUSE_THRESHOLD", 25.0)
    low_balance_recovery_enabled: bool = _bool("LOW_BALANCE_RECOVERY_ENABLED", "true")
    low_balance_recovery_max_positions: int = _int("LOW_BALANCE_RECOVERY_MAX_POSITIONS", 3)
    low_balance_recovery_min_probability: float = _float("LOW_BALANCE_RECOVERY_MIN_PROBABILITY", 0.80)
    low_balance_recovery_min_score: float = _float("LOW_BALANCE_RECOVERY_MIN_SCORE", 0.68)
    low_balance_recovery_size_mult: float = _float("LOW_BALANCE_RECOVERY_SIZE_MULT", 0.75)

    balance_tier_threshold: float = _float("BALANCE_TIER_THRESHOLD", 300.0)
    max_positions_low: int = _int("MAX_POSITIONS_LOW", 3)
    max_positions_high: int = _int("MAX_POSITIONS_HIGH", 8)

    aggressive_balance_threshold: float = _float("AGGRESSIVE_BALANCE_THRESHOLD", 100.0)
    aggressive_max_positions: int = _int("AGGRESSIVE_MAX_POSITIONS", 3)
    aggressive_min_confirmations: int = _int("AGGRESSIVE_MIN_SIGNAL_CONFIRMATIONS", 2)

    take_profit_min: float = _float("TAKE_PROFIT_MIN", 3.0)
    short_take_profit_min: float = _float("SHORT_TAKE_PROFIT_MIN", 0.3)
    take_profit_max: float = _float("TAKE_PROFIT_MAX", 7.0)
    take_profit_hard_cap: float = _float("TAKE_PROFIT_HARD_CAP", 8.0)
    stop_loss_pct: float = _float("STOP_LOSS_PCT", 5.0)
    # [2026-08-11 사용자요청] SHORT만 승률/손익이 저조해 손절폭을 따로 조이고 싶은데,
    # stop_loss_pct는 LONG/SHORT 공용이라 그대로 바꾸면 잘 되고 있는 LONG까지 같이 영향
    # 받는다. 0(기본값)이면 기존처럼 stop_loss_pct를 그대로 쓰고, 양수를 넣으면 SHORT
    # 포지션에서만 이 값을 쓴다(LONG은 영향 없음).
    short_stop_loss_pct: float = _float("SHORT_STOP_LOSS_PCT", 0.0)
    trail_drawdown_pct: float = _float("TRAIL_DRAWDOWN_PCT", 0.5)
    # [2026-08-11 사용자요청] "순환매매 맛보기" — 보유시간이 이 분(分)을 넘겼는데 ROE가
    # 최소치 이상(익절 상태)이면, 트레일링 조건을 기다리지 않고 바로 확정한다. 손실 중이거나
    # 이 최소치 미만이면 이 규칙은 개입하지 않고 기존 로직(트레일링/손절)을 그대로 따른다 —
    # "5분 지나면 무조건 청산"이 아니라 "5분 지나고 이미 이기고 있으면 더 안 기다린다"는
    # 뜻. 0이면 비활성화(기존 동작 그대로).
    force_profit_exit_max_hold_min: float = _float("FORCE_PROFIT_EXIT_MAX_HOLD_MIN", 0.0)
    force_profit_exit_min_roe: float = _float("FORCE_PROFIT_EXIT_MIN_ROE", 1.5)
    # [2026-08-10 실거래 분석 발견] 거래소에 걸어둔 TRAILING_STOP_MARKET의 callbackRate를
    # 지금까지 trail_drawdown_pct와 정확히 같은 폭으로 계산해왔다 — 그런데 거래소 주문은
    # 틱 단위로 즉시 반응하고 봇은 폴링(타이머) 기반이라, 폭이 같으면 거래소가 사실상 항상
    # 먼저 청산해버려서 봇 자신의 판단(SOFT_STOP 재평가, 모멘텀 지속 중 보류 등)이 개입할
    # 기회가 거의 없었다(실측: 최근 청산의 72~80%가 봇 판단이 아닌 거래소측 EXTERNAL_CLOSE).
    # 거래소 쪽 폭을 이 배수만큼 넓게 둬서 "봇이 놓쳤을 때만 발동하는 최종 안전망"으로 역할을
    # 분리한다 — 기본 2배: bot 1.0%p vs 거래소 2.0%p(레버리지 반영 전 ROE 기준).
    exchange_trailing_backstop_multiplier: float = _float("EXCHANGE_TRAILING_BACKSTOP_MULTIPLIER", 2.0)

    # [2026-08-10 사용자요청] "양봉이 많아지면 스윙의 관점에서 보고 익절 구간을 최대한으로
    # 늘려라" — 현재 캔들 하나만 보는 모멘텀 홀드(is_momentum_continuing)보다 한 단계 더
    # 넓게, 최근 N개 캔들이 연속으로 같은 방향으로 강하게 움직이면(스윙 국면으로 판단)
    # 절대 익절상한과 트레일링 허용폭을 모두 크게 넓혀서 추세를 최대한 태운다.
    # [2026-08-10 20:xx 사용자요청] "스캘핑~5분봉~최대 15분봉으로 발라먹고 싶다" — 1분봉
    # 기준 3개(3분)는 너무 짧아 짧은 흐름밖에 못 잡음. 5~15분 구간을 대표하는 중간값 8분으로
    # 상향(위쪽은 take_profit_hard_cap이 항상 별도로 무한노출을 막아준다).
    swing_streak_candles: int = _int("SWING_STREAK_CANDLES", 8)
    swing_take_profit_hard_cap: float = _float("SWING_TAKE_PROFIT_HARD_CAP", 20.0)
    swing_trail_drawdown_multiplier: float = _float("SWING_TRAIL_DRAWDOWN_MULTIPLIER", 3.0)
    # [2026-08-10 20:xx 사용자요청] "캔들 전부가 개별적으로 강해야" -> "구간 누적으로 판단"
    # 방식으로 재설계하며 추가됨. N개 캔들 누적 문턱 = pump_min_candle_chg_pct * N * 이 비율.
    # 0.6이면 "N개 전부가 펌프급일 필요 없이, 평균적으로 그 세기의 60%만 유지되면 인정".
    swing_cumulative_threshold_ratio: float = _float("SWING_CUMULATIVE_THRESHOLD_RATIO", 0.6)
    # [2026-08-10 사용자요청] 봇 내부 판단(evaluate())만 넓혀서는 부족하다 — 거래소에 걸어둔
    # TRAILING_STOP_MARKET 자체가 좁은 폭(exchange_trailing_backstop_multiplier 기준)으로
    # 남아있으면, 스윙 국면에서도 거래소가 봇보다 먼저 잘라버린다(실측: BICOUSDT 56초만에
    # EXTERNAL_CLOSE). armed 상태(이미 최소 익절선 도달)에서 스윙이 감지되면, 거래소 주문을
    # 이 배수로 다시 넓혀 재등록한다 — exchange_trailing_backstop_multiplier보다 더 넓게.
    swing_exchange_trailing_multiplier: float = _float("SWING_EXCHANGE_TRAILING_MULTIPLIER", 6.0)

    # 물타기: 손절선의 이 비율 지점(ROE 기준)에서, 포지션당 1회만, 가용잔고의 이 비율만큼 추가 투입
    # [2026-08-09] 계좌 소진 방지 강화(4x 격리+무물타기+기대값필터 조합) 요청으로 기본값을
    # False로 전환 — 지는 포지션에 추가 투입하는 마틴게일형 리스크를 완전히 제거한다.
    average_down_enabled: bool = _bool("AVERAGE_DOWN_ENABLED", "false")
    average_down_trigger_ratio: float = _float("AVERAGE_DOWN_TRIGGER_RATIO", 0.5)
    average_down_size_ratio: float = _float("AVERAGE_DOWN_SIZE_RATIO", 0.5)
    # [2026-08-10] 물타기를 몇 번이든 나눠서 할 수 있게 됐지만(횟수 제한 없음), 포지션 하나에
    # 투입되는 총 증거금(초기+모든 추가분 합산)은 진입 시점 잔고의 이 비율을 절대 못 넘는다 —
    # "횟수는 자유, 총액은 하드캡"이 핵심 안전장치. 기본 6%.
    average_down_max_total_margin_ratio: float = _float("AVERAGE_DOWN_MAX_TOTAL_MARGIN_RATIO", 0.06)

    # [2026-08-10] 리스크 기반 수량계산(compute_risk_based_position_size) 실거래 연결 스위치.
    # 기본값 False로 두어, 이 값을 .env에서 명시적으로 켜지 않는 한 기존 방식(잔고비율 기반
    # compute_position_size)이 그대로 쓰인다 — 민감한 변경이라 검증 전에는 항상 기존 동작 유지.
    risk_based_sizing_enabled: bool = _bool("RISK_BASED_SIZING_ENABLED", "false")
    risk_based_sizing_pct: float = _float("RISK_BASED_SIZING_PCT", 1.5)

    # [2026-08-10] WebSocket 시장데이터/계정 스트림 연결 스위치. 기본값 False — .env에서
    # 명시적으로 켜지 않는 한 main()이 소켓을 아예 열지 않고 기존 REST 폴링/mark-price
    # 추정치만 쓰는 지금 동작이 100% 그대로 유지된다. 테스트넷에서 충분히 지켜본 뒤 전환 권장.
    ws_market_data_enabled: bool = _bool("WS_MARKET_DATA_ENABLED", "false")
    ws_user_data_enabled: bool = _bool("WS_USER_DATA_ENABLED", "false")
    ws_kline_history_len: int = _int("WS_KLINE_HISTORY_LEN", 200)
    # [2026-08-10 사용자요청] "청크 분할 없는 REST 초기 시딩은 IP 밴 위험" — 오늘 실제로
    # 겪은 사고와 같은 종류. 샤딩 도입 후 워커 여러 개가 동시에 각자 시딩을 시작하면 순간
    # REST 호출이 몰릴 수 있어, 심볼을 이 개수씩 묶어 요청하고 묶음 사이 이 초만큼 쉰다.
    ws_seed_chunk_size: int = _int("WS_SEED_CHUNK_SIZE", 20)
    ws_seed_chunk_delay_sec: float = _float("WS_SEED_CHUNK_DELAY_SEC", 1.0)
    # [2026-08-10 실거래 사고 발견] WS 연결이 죽어도 캐시엔 예전 캔들이 그대로 남아있어서,
    # "개수는 충분함"만 확인하면 멈춰버린 옛날 가격을 계속 매매판단에 쓸 위험이 있었다.
    # 이 값(초)보다 오래 갱신 안 된 캐시는 신선하지 않은 것으로 보고 REST로 폴백한다.
    # 1분봉 기준 2.5분(캔들 2~3개 놓친 정도)이면 충분히 보수적인 여유.
    ws_kline_max_staleness_sec: float = _float("WS_KLINE_MAX_STALENESS_SEC", 150.0)
    # [2026-08-10, Codex 제안 반영] 테스트넷 실측(2026-08-10): python-binance read loop가
    # 약 90초마다 재발하는 큐 폭주(BinanceWebsocketQueueOverflow)로 죽는 게 확인됨 — canary
    # kline freshness(150초, 캔들 "마감" 기준)만으로는 감지가 너무 늦다. 실시간 메시지 자체가
    # 끊긴 시간과 "Read loop has been closed" 연속 발생 횟수를 추가 재시작 조건으로 쓴다.
    ws_message_max_staleness_sec: float = _float("WS_MESSAGE_MAX_STALENESS_SEC", 45.0)
    # Watchdog canary uses closed 1m klines, so it must not share the trading
    # cache freshness threshold.  A 20s trading freshness threshold is valid for
    # REST fallback, but it would falsely restart a healthy shard between closed
    # 1m candles.  Keep the watchdog canary looser and let message health catch
    # real stream stalls quickly.
    ws_canary_max_staleness_sec: float = _float("WS_CANARY_MAX_STALENESS_SEC", 120.0)
    ws_max_consecutive_read_loop_errors: int = _int("WS_MAX_CONSECUTIVE_READ_LOOP_ERRORS", 3)
    # QueueOverflow/Read-loop faults can still receive intermittent messages, which resets
    # consecutive_read_loop_errors.  A high 60s error count is therefore also a hard fault.
    ws_max_error_count_60s: int = _int("WS_MAX_ERROR_COUNT_60S", 100)
    # [2026-08-10, Codex 제안 반영] 심볼 전부를 워커 하나(연결 하나의 매니저)에 몰아넣으면
    # 메시지가 한 워커에 집중돼 큐 폭주 확률이 커진다 — 시장데이터를 이 개수만큼의 독립
    # 워커 프로세스로 나눠(각자 별도 ThreadedWebsocketManager) 심볼을 균등 분산한다.
    # 기본값 1이면 기존과 동일하게 단일 워커(하위호환). User Data Stream은 항상 시장데이터
    # 워커와 별도 프로세스로 뜬다(활성화된 경우).
    ws_market_shard_count: int = _int("WS_MARKET_SHARD_COUNT", 1)
    # Keep watchdog/status updates fast, but avoid serializing the full kline cache every few seconds.
    ws_status_dump_interval_sec: float = _float("WS_STATUS_DUMP_INTERVAL_SEC", 2.0)
    ws_cache_dump_interval_sec: float = _float("WS_CACHE_DUMP_INTERVAL_SEC", 60.0)
    # [2026-08-10 사용자요청] "딜레이 없는 맹목적 재연결은 IP 밴을 부른다" — 오늘 실제로
    # REST 대량호출로 IP 차단을 당한 사고와 같은 종류의 위험. 워커가 재시작해도 곧바로 다시
    # 불안정해지는 상황이 반복되면(예: 진짜 바이낸스측 문제), 재시작 간격을 지수적으로
    # 늘려서(5초→10→20→...→최대 120초) 연결 시도 자체가 스팸이 되지 않게 한다. 워커가
    # 한 사이클이라도 정상이면(재시작 대상에서 빠지면) 카운터를 0으로 리셋한다.
    ws_restart_backoff_base_sec: float = _float("WS_RESTART_BACKOFF_BASE_SEC", 5.0)
    ws_restart_backoff_max_sec: float = _float("WS_RESTART_BACKOFF_MAX_SEC", 120.0)

    # 손실 중일 때, 추세 전환 신호(3개 중 이만큼)가 나오면 손절선까지 안 기다리고 조기 탈출
    reversal_min_votes: int = _int("REVERSAL_MIN_VOTES", 3)
    # 조기 탈출은 손실이 최소 이만큼(ROE %)은 돼야 고려한다 (너무 작은 노이즈에 반응해
    # 수수료만 나가는 것을 방지)
    early_exit_min_loss_roe: float = _float("EARLY_EXIT_MIN_LOSS_ROE", 1.0)
    # [2026-08-04] 스캘핑 재설계 후: 손실이 깊지 않은데(-2% ROE 이내) 이 시간(분)을 넘기면
    # 방향성 없는 코인으로 보고 슬롯을 비운다 (무기한 정체로 회전율이 떨어지는 문제 방지)
    scalp_max_hold_minutes: float = _float("SCALP_MAX_HOLD_MINUTES", 90.0)
    # 수익 구간에서 모멘텀이 꺾였다고 판단할 투표 수(3개 중, 낮을수록 더 빨리 확정)
    profit_hold_reversal_votes: int = _int("PROFIT_HOLD_REVERSAL_VOTES", 2)
    profit_buffer_usdt: float = _float("PROFIT_BUFFER_USDT", 0.3)
    # [2026-08-09] 모멘텀반전 즉시확정 로직이 익절 최소선(take_profit_min)을 넘자마자(예: ROE
    # 5.55%, 최소선 4%) 숨돌릴 틈도 없이 발동하는 문제(EPICUSDT 실측: pnl 1.39%로 조기확정)가
    # 확인돼, "익절 최소선 + 이 값(ROE %p)"을 넘어야만 반전확정 체크를 적용하도록 여유구간을 둔다.
    profit_lock_buffer_pct: float = _float("PROFIT_LOCK_BUFFER_PCT", 1.0)

    # 총자산이 이 값(USDT) 미만이면 %가 아니라 절대금액(수수료 제외 순수익) 기준으로 익절
    small_profit_balance_threshold: float = _float("SMALL_PROFIT_BALANCE_THRESHOLD", 40.0)
    small_profit_target_usdt: float = _float("SMALL_PROFIT_TARGET_USDT", 0.04)
    small_profit_immediate_max_roe: float = _float("SMALL_PROFIT_IMMEDIATE_MAX_ROE", 1.5)
    # [2026-08-10] On sub-50 USDT accounts, lock small ROE wins before the normal
    # 4% ROE exchange trailing activation.
    small_profit_lock_balance_threshold: float = _float("SMALL_PROFIT_LOCK_BALANCE_THRESHOLD", 50.0)
    small_profit_lock_roe: float = _float("SMALL_PROFIT_LOCK_ROE", 1.0)
    small_profit_lock_drawdown_roe: float = _float("SMALL_PROFIT_LOCK_DRAWDOWN_ROE", 0.4)
    fee_rate_roundtrip: float = _float("FEE_RATE_ROUNDTRIP", 0.001)  # 진입+청산 왕복 수수료 추정치(0.1%)
    # [2026-08-09] 기대값 필터가 판단을 시작하기 전 최소 표본 수(그 이하면 항상 통과)
    ev_filter_min_sample: int = _int("EV_FILTER_MIN_SAMPLE", 15)
    # [2026-08-10] Small accounts need trade frequency for compounding tests. Keep
    # the structural EV guard, but by default reduce entry size instead of pausing
    # all new scans. Set EV_FILTER_HARD_PAUSE=true to restore the old behavior.
    ev_filter_hard_pause: bool = _bool("EV_FILTER_HARD_PAUSE", "false")
    ev_defense_size_mult: float = _float("EV_DEFENSE_SIZE_MULT", 0.75)
    # Keep the 12~18 trades/hour target feasible: symbol-level negative EV does
    # not remove a symbol from scans by default. It stays tradable but is
    # ranked/sized down.
    negative_ev_symbol_size_mult: float = _float("NEGATIVE_EV_SYMBOL_SIZE_MULT", 0.60)

    rsi_period: int = _int("RSI_PERIOD", 14)
    rsi_oversold: float = _float("RSI_OVERSOLD", 30)
    rsi_overbought: float = _float("RSI_OVERBOUGHT", 70)
    ema_fast: int = _int("EMA_FAST", 20)
    ema_slow: int = _int("EMA_SLOW", 50)
    macd_fast: int = _int("MACD_FAST", 12)
    macd_slow: int = _int("MACD_SLOW", 26)
    macd_signal: int = _int("MACD_SIGNAL", 9)

    stoch_rsi_period: int = _int("STOCH_RSI_PERIOD", 14)
    stoch_rsi_oversold: float = _float("STOCH_RSI_OVERSOLD", 20)
    stoch_rsi_overbought: float = _float("STOCH_RSI_OVERBOUGHT", 80)
    bb_period: int = _int("BB_PERIOD", 20)
    bb_std: int = _int("BB_STD", 2)
    atr_period: int = _int("ATR_PERIOD", 14)
    adx_period: int = _int("ADX_PERIOD", 14)
    adx_threshold: float = _float("ADX_THRESHOLD", 20)
    volume_ma_period: int = _int("VOLUME_MA_PERIOD", 20)

    max_margin_ratio: float = _float("MAX_MARGIN_RATIO", 0.20)
    # [2026-08-09] 보유 포지션 증거금 합계가 잔고 대비 이 %를 넘으면 신규 진입 중단(상관 리스크
    # 방어선, max_margin_ratio는 유지증거금 기준이라 훨씬 작은 값이라 별개의 지표).
    max_aggregate_margin_pct: float = _float("MAX_AGGREGATE_MARGIN_PCT", 60.0)
    # [2026-08-10] For small accounts, keep entry frequency by scaling down new
    # entries when aggregate margin is already high. Set AGGREGATE_RISK_HARD_PAUSE
    # true to restore the old whole-cycle pause.
    aggregate_risk_hard_pause: bool = _bool("AGGREGATE_RISK_HARD_PAUSE", "false")
    aggregate_risk_size_mult: float = _float("AGGREGATE_RISK_SIZE_MULT", 0.65)
    min_margin_usdt: float = _float("MIN_MARGIN_USDT", 5.0)
    aggressive_min_margin_usdt: float = _float("AGGRESSIVE_MIN_MARGIN_USDT", 3.0)
    small_balance_min_margin_usdt: float = _float("SMALL_BALANCE_MIN_MARGIN_USDT", 4.0)

    min_confirmations: int = _int("MIN_SIGNAL_CONFIRMATIONS", 2)
    # [2026-08-06] 펌프(급등/급락) 감지 재설계 — 72시간/200심볼 교차검증으로 승률37%대·
    # 손익분기30.9%(TP8/SL3 기준)로 EV 양전 확인된 유일한 신호. 캔들 자체 변동폭+거래량배수로 판단.
    pump_min_candle_chg_pct: float = _float("PUMP_MIN_CANDLE_CHG_PCT", 2.5)
    pump_min_volume_ratio: float = _float("PUMP_MIN_VOLUME_RATIO", 2.0)
    min_entry_probability: float = _float("MIN_ENTRY_PROBABILITY", 0.70)
    # 숏은 손실이 잦아서(급반등에 취약) 롱보다 더 까다로운 확률 기준과 더 작은 비중을 적용
    short_min_entry_probability: float = _float("SHORT_MIN_ENTRY_PROBABILITY", 0.78)
    short_size_multiplier: float = _float("SHORT_SIZE_MULTIPLIER", 0.5)
    # 숏은 스윙이 아니라 스캘핑으로만 진입한다: 텔레그램 분석과 같은 100점 만점 종합점수가
    # 이 값 이상이고, 5분 내 목표 익절(SHORT_TAKE_PROFIT_MIN)에 도달할 가능성이 있을 때만 진입한다.
    short_min_total_score: float = _float("SHORT_MIN_TOTAL_SCORE", 65.0)
    # 롱도 같은 100점 만점 종합점수 게이트를 적용한다 (5분 도달가능성/즉시확정은 적용 안 함 — 롱은 스윙 유지)
    long_min_total_score: float = _float("LONG_MIN_TOTAL_SCORE", 62.0)
    # 진입 확률 추정치가 이 값 이상이면 비중을 아래 값까지 크게 키운다 (보유 테더 최대 투입)
    high_confidence_probability: float = _float("HIGH_CONFIDENCE_PROBABILITY", 0.85)
    high_confidence_size_ratio: float = _float("HIGH_CONFIDENCE_SIZE_RATIO", 1.0)
    # 스캔 시점 가격 대비 진입 직전 가격이 이만큼(%) 이상 불리하게 움직였으면
    # 신호가 낡은 것으로 보고 진입을 건너뛴다 (여러 심볼 스캔하는 동안 시간차 보정)
    stale_signal_tolerance_pct: float = _float("STALE_SIGNAL_TOLERANCE_PCT", 0.15)
    # [2026-08-10] 소형 알트 복구 모드 안전장치. 스캘핑은 bid/ask 스프레드가 수수료보다
    # 커지는 순간 기대값이 무너질 수 있으므로, 진입 직전 스프레드가 이 기준을 넘으면
    # 해당 후보만 건너뛴다. 네트워크/조회 실패시에는 기존 동작을 유지하기 위해 막지 않는다.
    max_entry_spread_pct: float = _float("MAX_ENTRY_SPREAD_PCT", 0.18)
    one_min_noise_filter_enabled: bool = _bool("ONE_MIN_NOISE_FILTER_ENABLED", "true")
    one_min_noise_max_wick_body_ratio: float = _float("ONE_MIN_NOISE_MAX_WICK_BODY_RATIO", 2.5)
    # [2026-08-12 백테스트] SHORT 스캘핑 신호 1분봉이 저가에서 0.5% 이상 되감겨 마감하면
    # 아래꼬리/흡수 후 반등으로 보고 SHORT 진입을 제외한다. 5일 1m 백테스트에서
    # SHORT 손익 -10.3960 -> +2.7449, validation 순손익 -3.4363 -> +1.7357로 개선.
    short_scalp_max_close_from_low_pct: float = _float("SHORT_SCALP_MAX_CLOSE_FROM_LOW_PCT", 0.5)
    # Do not block trades for a possible SHORT snapback; keep the scalp loop active,
    # but rank these candidates behind cleaner setups and reduce their size.
    short_reversal_risk_enabled: bool = _bool("SHORT_REVERSAL_RISK_ENABLED", "true")
    short_reversal_risk_priority_penalty: float = _float("SHORT_REVERSAL_RISK_PRIORITY_PENALTY", 0.08)
    short_reversal_risk_size_mult: float = _float("SHORT_REVERSAL_RISK_SIZE_MULT", 0.65)
    low_balance_recovery_skip_two_candle_confirmation: bool = _bool("LOW_BALANCE_RECOVERY_SKIP_TWO_CANDLE_CONFIRMATION", "true")

    # 1분봉 신호가 나온 후, 이 상위 시간대들의 추세와 같은 방향인지 최종 확인 (스캘핑 신호를 더 확실하게)
    mtf_timeframes: list = field(default_factory=lambda: [
        s.strip() for s in os.getenv("MTF_TIMEFRAMES", "5m,15m,1h,4h").split(",") if s.strip()
    ])
    mtf_min_agree_ratio: float = _float("MTF_MIN_AGREE_RATIO", 0.75)
    probability_adx_cap: float = _float("PROBABILITY_ADX_CAP", 25.0)

    # 진입 직전 비트코인이 급락/급등하고 있으면(알트코인은 BTC를 따라가는 경향이 강함)
    # 그 코인 자체 신호가 좋아도 신규 진입을 이번 주기엔 쉰다 (시장 전체가 흔들리는 상황 회피)
    btc_check_symbol: str = os.getenv("BTC_CHECK_SYMBOL", "BTCUSDT")
    btc_sudden_move_pct: float = _float("BTC_SUDDEN_MOVE_PCT", 1.2)
    # BTC 방향과 알트 방향이 같으면 다음 진입 비중에 가산, 반대면 감산한다.
    # BTC가 절대 정답은 아니므로 "스킵"이 아니라 "가중치"로만 반영한다.
    btc_alignment_timeframe: str = os.getenv("BTC_ALIGNMENT_TIMEFRAME", "15m")
    btc_alignment_fast_ema: int = _int("BTC_ALIGNMENT_FAST_EMA", 20)
    btc_alignment_slow_ema: int = _int("BTC_ALIGNMENT_SLOW_EMA", 50)
    btc_alignment_match_mult: float = _float("BTC_ALIGNMENT_MATCH_MULT", 1.12)
    btc_alignment_mismatch_mult: float = _float("BTC_ALIGNMENT_MISMATCH_MULT", 0.88)
    btc_alignment_neutral_mult: float = _float("BTC_ALIGNMENT_NEUTRAL_MULT", 1.0)
    # 진입 후 바로 손절되는 잦은 whipsaw를 줄이기 위해,
    # 최근 5분봉 변동성이 손절폭 대비 이 정도는 되어야 진입한다.
    min_atr_vs_stop_ratio: float = _float("MIN_ATR_VS_STOP_RATIO", 0.8)
    # [2026-08-11 사용자요청] "휩쏘 필터가 하한선만 있다" — 변동성이 너무 작을 때만 걸러내고
    # 너무 클 때(=노이즈가 손절폭보다 훨씬 커서 방향과 무관하게 스탑에 스치기 쉬운 상황)는
    # 걸러내지 못하고 있었다. SHORT 손절의 39%가 진입 1분 이내(순수 STOP_LOSS 중앙값
    # 1.84분)에 발생한 실측 근거로 상한선을 추가한다. 5분봉 ATR%가 손절폭의 이 배수를
    # 넘으면(기본 4배) 너무 날뛰는 걸로 보고 진입을 생략한다. 0이면 비활성화(기존 동작 유지).
    max_atr_vs_stop_ratio: float = _float("MAX_ATR_VS_STOP_RATIO", 4.0)
    # [2026-08-11 사용자요청] 진입 직후 짧은 유예기간 동안은 손절폭을 이 배수만큼 넓혀서(거래소
    # STOP_MARKET은 항상 유지 — 무보호 구간 없음) 진입 직후의 순간적 되돌림(휩쏘)에 스탑이
    # 스치는 걸 줄인다. 유예기간이 지나면 다음 주기 정합성 점검(reconcile) 루프에서 자동으로
    # 원래 폭으로 다시 좁힌다. 0이면 비활성화(기존 동작 그대로, 항상 기본 손절폭).
    stop_loss_grace_sec: float = _float("STOP_LOSS_GRACE_SEC", 0.0)
    stop_loss_grace_widen_mult: float = _float("STOP_LOSS_GRACE_WIDEN_MULT", 1.5)
    # [2026-08-11 사용자요청] check_hourly_soft_stop이 "새 1시간봉 첫 체크"라는 조건만 보고
    # 최소 보유시간 가드가 없어서, 진입 직후(실측 4.8초/9.6초) 순간 노이즈로 ROE가 잠깐
    # -1.5%(soft_stop_min_loss_roe) 밑으로만 내려가도 바로 발동하는 문제가 LONG에서
    # 확인됐다(SHORT의 STOP_MARKET 휩쏘와 같은 종류, 다른 경로). 이 초 이내면 SOFT_STOP
    # 판단 자체를 보류한다. 0이면 비활성화(기존 동작 그대로).
    soft_stop_min_hold_sec: float = _float("SOFT_STOP_MIN_HOLD_SEC", 60.0)
    # true면 신규 진입 스캔 자체를 완전히 쉰다 (기존 보유 포지션 관리는 그대로 계속함).
    # 지표 재설계/검증 중 신규 진입을 완전히 멈춰야 할 때 사용.
    pause_new_entries: bool = _bool("PAUSE_NEW_ENTRIES", "false")
    btc_lookback_candles: int = _int("BTC_LOOKBACK_CANDLES", 3)
    # [2026-08-10 사용자요청] 블로그의 "3단계 BTC 필터" 중 저희한테 없던 2번째 단계 —
    # 순간 급변동(위 btc_lookback_candles/btc_sudden_move_pct, 짧은 창)과 별개로, 더 긴
    # 창에서 "천천히 지속되는 하락"도 잡아야 함. 둘 중 하나라도 걸리면 신규 진입 스캔 생략.
    btc_macro_lookback_candles: int = _int("BTC_MACRO_LOOKBACK_CANDLES", 15)
    btc_macro_move_pct: float = _float("BTC_MACRO_MOVE_PCT", 1.5)

    # 보유 포지션 재평가: 1시간봉이 새로 바뀔 때마다, 손실 중인 포지션의 방향을 상위
    # 시간대(1시간/15분/5분)가 여전히 지지하는지 재확인한다. 손실이 이 값(ROE%) 이상이고
    # 상위 시간대 과반이 더 이상 지지하지 않으면, 정식 손절선까지 기다리지 않고 약손절한다.
    soft_stop_min_loss_roe: float = _float("SOFT_STOP_MIN_LOSS_ROE", 1.5)
    soft_stop_mtf_min_ratio: float = _float("SOFT_STOP_MTF_MIN_RATIO", 0.5)

    telegram_bot_token: str = os.getenv("TELEGRAM_BOT_TOKEN", "")
    telegram_chat_id: str = os.getenv("TELEGRAM_CHAT_ID", "")
    telegram_digest_interval_sec: int = _int("TELEGRAM_DIGEST_INTERVAL_SEC", 600)

    daily_profit_target_pct: float = _float("DAILY_PROFIT_TARGET_PCT", 30.0)
    daily_profit_step_pct: float = _float("DAILY_PROFIT_STEP_PCT", 10.0)
    # [2026-08-11 사용자요청] 원래는 target 도달 후 step씩 무한정 계속 물어봤다(하루에 여러 번
    # 벌면 체크포인트 알림이 계속 옴). 0이면 무제한(기존 동작 그대로), 양수면 그 횟수만큼만
    # 묻고 그 이후엔 알림 없이 계속 거래한다.
    daily_profit_max_checkpoints: int = _int("DAILY_PROFIT_MAX_CHECKPOINTS", 0)

    # [2026-08-07] 일일 손실 서킷브레이커 — 하루 기준자산 대비 이 비율(%) 이상 손실나면
    # 신규 진입을 자동으로 멈춘다(기존 포지션의 익절/손절/시간초과 관리는 그대로 계속됨).
    # 수익 쪽엔 목표 확인 장치가 있었는데 손실 쪽엔 대칭되는 장치가 없었던 걸 보완.
    daily_loss_limit_pct: float = _float("DAILY_LOSS_LIMIT_PCT", 15.0)
    # [2026-08-11 사용자요청] 원래는 트리거 후 "날짜가 바뀔 때까지"였는데, 트리거 시각에
    # 따라 실제 정지시간이 몇 분~24시간까지 들쭉날쭉했다. 고정 시간 후 자동 재개로 변경.
    daily_loss_pause_hours: float = _float("DAILY_LOSS_PAUSE_HOURS", 4.0)

    # [2026-08-11 사용자요청] 글로벌 연패 서킷브레이커 — 특정 심볼이 아니라 봇 전체가
    # 짧은 시간에 연속으로 지고 있으면(장세 자체가 안 맞는 상황일 가능성), 심볼별 격리와
    # 별개로 전체 신규 진입을 잠깐 멈춘다(기존 포지션 관리는 계속됨, daily_loss_limit_pct와
    # 동일한 "신규진입만 중단" 방식).
    global_loss_streak_threshold: int = _int("GLOBAL_LOSS_STREAK_THRESHOLD", 5)
    global_pause_min: float = _float("GLOBAL_PAUSE_MIN", 10.0)

    poll_interval_sec: int = _int("POLL_INTERVAL_SEC", 30)
    # [2026-08-10] 실거래 분석으로 발견: 보유 포지션 관리(트레일링/SOFT_STOP/모멘텀보류 판단)가
    # 250심볼 스캔과 같은 30초 주기에 묶여있어서, 거래소에 걸어둔 실시간 트레일링 주문이
    # 항상 먼저 청산해버리고 봇 자신의 판단(모멘텀 지속 중 보류 등)은 실행될 기회조차 거의
    # 없었다(실측: 최근 청산의 72%가 거래소측 EXTERNAL_CLOSE). 보유 포지션만 이 값(초)마다
    # 훨씬 자주 확인해서 봇 판단이 실제로 개입할 기회를 늘린다 — 신규 진입 스캔(무거움, API
    # 호출량 큼)은 기존처럼 poll_interval_sec 그대로 유지.
    position_check_interval_sec: float = _float("POSITION_CHECK_INTERVAL_SEC", 5.0)

    # [2026-08-10] 진입을 시장가 대신 지정가(메이커, LONG=매수호가/SHORT=매도호가)로 해서
    # 슬리피지 리스크를 없앤다 — 대신 상대가 와서 체결시켜줄 때까지 기다려야 하므로 항상
    # 체결되진 않는다. 기본 꺼짐(false)이라 켜지 않으면 기존 시장가 진입 그대로 유지된다.
    limit_entry_enabled: bool = _bool("LIMIT_ENTRY_ENABLED", "false")
    # 이 시간(초) 안에 체결 안 되면 취소하고 진입을 포기한다(시장가로 쫓아가지 않음 — 그게
    # 이 기능의 핵심 목적인 "리스크 최소화"를 지키는 부분).
    limit_entry_wait_sec: float = _float("LIMIT_ENTRY_WAIT_SEC", 8.0)
    # 최근 30분 0% 승률 원인 중 하나가 진입 직후 1~3분 역행이었다. 거래수를 줄이는
    # 차단 필터 대신, 지정가를 호가보다 아주 조금 유리하게 놓아 캔들 끝단 추격 진입을 줄인다.
    # LONG은 bid 아래, SHORT는 ask 위. 0이면 기존 호가 그대로.
    limit_entry_pullback_pct: float = _float("LIMIT_ENTRY_PULLBACK_PCT", 0.0)

    # 1) 유동성 얇은 시간대(UTC 기준 시각)에는 신규 진입을 쉰다
    blocked_hours_utc: list = field(default_factory=lambda: [
        int(s.strip()) for s in os.getenv("BLOCKED_HOURS_UTC", "").split(",") if s.strip()
    ])

    # 2) 펀딩비 정산 시각(UTC 00:00/08:00/16:00) 전후 이 분(分) 동안은 신규 진입을 쉰다
    funding_avoid_minutes: float = _float("FUNDING_AVOID_MINUTES", 10.0)
    # [2026-08-05] 위 시간대엔 신규 진입뿐 아니라 기존 포지션도 강제 정리한다(정산 급변동 리스크 회피).
    # 단, ROE가 이 값(%) 이상으로 이미 크게 수익 중이면 굳이 끊지 않고 그대로 둔다.
    funding_force_close_exempt_roe: float = _float("FUNDING_FORCE_CLOSE_EXEMPT_ROE", 4.0)
    # [2026-08-06] 정산 시각 이 분(分) 전부터는 수익 여부와 무관하게(예외 없이) 강제 정리한다
    funding_unconditional_close_min: float = _float("FUNDING_UNCONDITIONAL_CLOSE_MIN", 1.0)
    # [2026-08-05] TIME_STOP(방향성 없음 판단) 대상이어도, 상위시간대 추세가 이 비율 이상
    # 여전히 포지션 방향을 지지하면(=진짜 스윙으로 가는 중) 강제청산을 면제하고 트레일링/손절에 맡긴다
    time_stop_trend_exempt_min_ratio: float = _float("TIME_STOP_TREND_EXEMPT_MIN_RATIO", 1.0)

    # 4) 한 심볼에서 연속 손실이 이 횟수 이상이면, 이 시간(분) 동안 그 심볼 신규 진입을 쉰다
    symbol_blacklist_loss_threshold: int = _int("SYMBOL_BLACKLIST_LOSS_THRESHOLD", 2)
    symbol_blacklist_cooldown_min: float = _float("SYMBOL_BLACKLIST_COOLDOWN_MIN", 60.0)
    # [2026-08-10 사용자요청] 연속손실 횟수와 별개로, 단 1회의 손실이라도 설정 손절폭
    # (stop_loss_pct)보다 이 배수 이상 크게 실현되면(호가가 얇아 슬리피지가 심한 코인일
    # 가능성) 즉시 더 긴 기간 격리한다 — "2번 연속 져야 격리"로는 첫 손실부터 손절폭을
    # 크게 넘겨버리는 '악질 슬리피지' 코인을 놓친다.
    slippage_quarantine_multiplier: float = _float("SLIPPAGE_QUARANTINE_MULTIPLIER", 1.7)
    slippage_quarantine_cooldown_min: float = _float("SLIPPAGE_QUARANTINE_COOLDOWN_MIN", 1440.0)
    # [2026-08-10] 소액 계좌에서는 거래 횟수를 줄이면 복리 회전이 죽는다.
    # 따라서 1~2회 손실은 차단보다 "다음 진입 비중 축소"로 대응하고,
    # 실제 심볼 차단은 최소 3연속 손실부터 적용한다.
    symbol_blacklist_min_loss_streak: int = _int("SYMBOL_BLACKLIST_MIN_LOSS_STREAK", 3)
    loss_reentry_size_mult: float = _float("LOSS_REENTRY_SIZE_MULT", 0.55)
    loss_reentry_min_mult: float = _float("LOSS_REENTRY_MIN_MULT", 0.30)
    recent_performance_window: int = _int("RECENT_PERFORMANCE_WINDOW", 10)
    recent_performance_min_trades: int = _int("RECENT_PERFORMANCE_MIN_TRADES", 5)
    recent_defense_winrate_threshold: float = _float("RECENT_DEFENSE_WINRATE_THRESHOLD", 0.45)
    recent_defense_size_mult: float = _float("RECENT_DEFENSE_SIZE_MULT", 0.75)
    direction_performance_window: int = _int("DIRECTION_PERFORMANCE_WINDOW", 5)
    direction_performance_min_trades: int = _int("DIRECTION_PERFORMANCE_MIN_TRADES", 3)
    direction_loss_size_mult: float = _float("DIRECTION_LOSS_SIZE_MULT", 0.85)
    direction_min_size_mult: float = _float("DIRECTION_MIN_SIZE_MULT", 0.35)

    # [2026-08-06] 같은 코인에 이 시간(분) 안에 재진입하면 비중을 직전 진입 대비 이 배수
    # 이하로 축소한다 (재진입 자체는 막지 않음 — TAKEUSDT 4연속 최대비중 재진입 후 급반전 실측)
    same_symbol_reentry_window_min: float = _float("SAME_SYMBOL_REENTRY_WINDOW_MIN", 60.0)
    same_symbol_reentry_ratio_mult: float = _float("SAME_SYMBOL_REENTRY_RATIO_MULT", 0.45)
    # [2026-08-06] 익절로 끝난 직후엔 이 시간(분) 동안 같은 심볼 재진입을 아예 금지한다
    # (손절 후 재진입은 그대로 허용, 비중축소만 적용 — HFTUSDT 익절 30초 후 재진입->손절 실측)
    post_win_reentry_cooldown_min: float = _float("POST_WIN_REENTRY_COOLDOWN_MIN", 5.0)

    # 5) 펀딩비(시장 쏠림/고래 포지셔닝 신호)가 이 절대값(%)을 넘으면 "고래성 코인"으로 분류한다.
    # 진입 자체를 막지는 않고, 대신 아래 whale_* 안전장치를 강제 적용한다.
    max_abs_funding_rate_pct: float = _float("MAX_ABS_FUNDING_RATE_PCT", 0.05)
    whale_max_position_ratio: float = _float("WHALE_MAX_POSITION_RATIO", 0.10)
    whale_max_leverage: int = _int("WHALE_MAX_LEVERAGE", 3)

    # 테이커 매수 비율이 이 값 이상/이하로 확실히 한쪽으로 쏠려야 진입 (0.5=중립, 0.55면 매수우위 55%)
    taker_imbalance_threshold: float = _float("TAKER_IMBALANCE_THRESHOLD", 0.55)

    # 최근 캔들 평균 거래대금(USDT/분)이 이 값 미만이면 아예 후보에서 제외한다 (절대 유동성 하한선).
    # 상대적 거래량(자기 평균 대비)만으로는 원래부터 극도로 얇은 코인을 못 걸러낸다 — 실측 결과
    # PENGUSDT 같은 코인은 분당 $44 수준이라 캔들이 거의 안 움직여 진입해도 포지션이 정체됨.
    min_avg_quote_volume_usdt: float = _float("MIN_AVG_QUOTE_VOLUME_USDT", 150.0)

    # [2026-08-07] 비중 조절용 두 번째 유동성 지표 — 신호 판단에 쓰는 "이번 캔들 거래량 급증"과
    # 별개로, "이 코인이 평소(최근 20개 캔들 평균)에 얼마나 두꺼운 시장인가"를 비중에 반영한다.
    # 신호용 거래량(현재 캔들 급증분)은 고래 한 명이 만들 수 있어 비중 확대 근거로 쓰면 위험하다는
    # 사용자 피드백에 따라, 평균 거래대금(스파이크에 잘 안 흔들림)만 비중 스케일링에 사용한다.
    # avg_quote_volume이 min_avg_quote_volume_usdt(하한, 진입 자체 컷) 근처면 비중을
    # liquidity_size_min_mult까지 축소하고, liquidity_size_full_usdt 이상이면 비중 축소 없음(1.0배).
    liquidity_size_full_usdt: float = _float("LIQUIDITY_SIZE_FULL_USDT", 2000.0)
    liquidity_size_min_mult: float = _float("LIQUIDITY_SIZE_MIN_MULT", 0.5)

    # [2026-08-12 사용자요청] BTC정렬/방향성과/계좌방어/기대값방어/상관리스크 등 여러 방어
    # 배율이 순차적으로 곱해지면서 겹칠 때, 개별로는 합리적인 배율들이 합쳐져 최종 비중이
    # 0 근처까지 떨어져 "계산된 수량이 0 이하"로 진입 자체가 스킵되는 문제 실측(실거래
    # 사후검증: 스킵된 80건을 진입시켰다고 가정 시 승률 80.8%). 아무리 배율이 겹쳐도
    # 최초 비중(base_ratio_before_defense)의 이 비율 밑으로는 안 내려가도록 하한을 둔다.
    defense_stack_min_ratio_mult: float = _float("DEFENSE_STACK_MIN_RATIO_MULT", 0.30)

    # [2026-08-12 사용자요청] "순환매매를 하려는데 돌파매매(꼭대기 추격)가 되어 진입 직후
    # 전부 -부터 시작한다"는 실거래 문제 확인. 최근1시간 LONG진입 실측: 진입가가 직전20분
    # 가격범위의 60~94%(거의 꼭대기)에 몰려있었음. 4시간 표본 간단검증: 진입위치 70%미만
    # 유지시 손익 -1.99USDT, 70%이상(꼭대기추격) 포함시 -4.95USDT — 필터 적용시 손실 약
    # 71% 감소 확인 후 적용. LONG은 상단 근접(추격매수), SHORT는 하단 근접(추격매도)을 막는다.
    entry_range_position_filter_enabled: bool = _bool("ENTRY_RANGE_POSITION_FILTER_ENABLED", "true")
    entry_range_position_lookback_min: int = _int("ENTRY_RANGE_POSITION_LOOKBACK_MIN", 20)
    entry_range_position_max_pct: float = _float("ENTRY_RANGE_POSITION_MAX_PCT", 70.0)

    # 2봉 연속 확인: 신호가 처음 뜬 캔들 다음에도 같은 방향이 유지돼야 진입 (가짜 신호 필터)
    require_two_candle_confirmation: bool = _bool("REQUIRE_TWO_CANDLE_CONFIRMATION", "true")

    def validate(self):
        if not self.api_key or not self.api_secret:
            raise ValueError("BINANCE_API_KEY / BINANCE_API_SECRET가 설정되지 않았습니다 (.env 확인).")
        if self.stop_loss_pct <= 0:
            raise ValueError("STOP_LOSS_PCT는 0보다 커야 합니다.")
        if not (0 < self.position_size_min <= self.position_size_max <= 1):
            raise ValueError("POSITION_SIZE_MIN은 0보다 크고 POSITION_SIZE_MAX 이하, POSITION_SIZE_MAX는 1 이하여야 합니다.")
        if not (1 <= self.leverage_min <= self.leverage_max):
            raise ValueError("LEVERAGE_MIN은 1 이상이고 LEVERAGE_MAX 이하여야 합니다.")
        if self.take_profit_min > self.take_profit_max:
            raise ValueError("TAKE_PROFIT_MIN은 TAKE_PROFIT_MAX보다 작거나 같아야 합니다.")
        if self.take_profit_max > self.take_profit_hard_cap:
            raise ValueError("TAKE_PROFIT_MAX는 TAKE_PROFIT_HARD_CAP보다 작거나 같아야 합니다.")
        if self.soft_stop_min_loss_roe >= self.stop_loss_pct:
            raise ValueError("SOFT_STOP_MIN_LOSS_ROE는 STOP_LOSS_PCT보다 작아야 합니다 (그래야 정식 손절 전에 약손절이 먼저 검토됨).")
