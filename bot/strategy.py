import pandas as pd

from .config import Config


def btc_alignment_multiplier(ex, cfg: Config, side: str) -> float:
    """Return a gentle size multiplier based on BTC's short-term direction.

    BTC is treated as context, not truth. If BTC and the alt point the same way,
    the next entry gets a small boost. If BTC points the other way, size is
    reduced but entries are not blocked outright.
    """
    try:
        df = ex.get_klines(cfg.btc_check_symbol, interval=cfg.btc_alignment_timeframe)
    except Exception:
        return cfg.btc_alignment_neutral_mult

    if len(df) < max(cfg.btc_alignment_slow_ema + 2, 10):
        return cfg.btc_alignment_neutral_mult

    close = df["close"]
    fast = close.ewm(span=cfg.btc_alignment_fast_ema, adjust=False).mean().iloc[-1]
    slow = close.ewm(span=cfg.btc_alignment_slow_ema, adjust=False).mean().iloc[-1]
    if fast > slow and side == "LONG":
        return cfg.btc_alignment_match_mult
    if fast < slow and side == "SHORT":
        return cfg.btc_alignment_match_mult
    if fast > slow or fast < slow:
        return cfg.btc_alignment_mismatch_mult
    return cfg.btc_alignment_neutral_mult


def btc_short_term_momentum_opposes(ex, cfg: Config, side: str) -> bool:
    """[2026-08-14 사용자요청] 오늘 09:26~09:36 LONG 4연속 손실(BTC가 그 10분간 -0.22%
    미끄러짐) 재발방지 — 기존 btc_alignment_multiplier는 15분봉 EMA20/50 기준이라 이런
    10분 이내의 짧은 역행은 못 잡는다. 최근 btc_momentum_gate_window_min분간 BTC 종가
    변화율이 신호 방향과 반대이고 btc_momentum_gate_threshold_pct 이상이면 True.

    [실거래 리플레이 검증, 2026-08-14] 1043건 기준 방향성은 확인됐으나(모든 임계값에서
    역행그룹 승률이 baseline보다 낮음, 스킵 시뮬레이션 순손익 전부 개선) 걸리는 표본이
    0.5~6.7%로 작아 통계적 유의성은 약함(z -1.3~-2.4). 그래도 스킵이 아니라 비중만
    축소하는 낮은 리스크 개입이라 사용자 승인 하에 적용, 데이터 더 쌓이면 재검증."""
    if not getattr(cfg, "btc_momentum_gate_enabled", True):
        return False
    try:
        df = ex.get_klines(cfg.btc_check_symbol, interval="1m")
    except Exception:
        return False
    window = int(getattr(cfg, "btc_momentum_gate_window_min", 5))
    if len(df) < window + 1:
        return False
    close = df["close"]
    ref_price = float(close.iloc[-1 - window])
    last_price = float(close.iloc[-1])
    if ref_price <= 0:
        return False
    change_pct = (last_price - ref_price) / ref_price * 100
    threshold = abs(getattr(cfg, "btc_momentum_gate_threshold_pct", 0.10))
    if side == "LONG" and change_pct <= -threshold:
        return True
    if side == "SHORT" and change_pct >= threshold:
        return True
    return False


def mtf_trend_alignment(ex, cfg: Config, symbol: str, side: str) -> tuple[int, int]:
    """1분봉 신호가 나온 뒤, 상위 시간대(기본 5분/15분/1시간/4시간)들의 추세와
    같은 방향인지 확인한다 (EMA fast/slow 기준의 단순 추세 판단).

    각 시간대에서 데이터가 부족하면(신규 상장 코인 등) 그 시간대는 집계에서 제외한다.
    반환값: (동의한 시간대 수, 판단 가능했던 시간대 수)
    """
    agree = 0
    total = 0
    for interval in cfg.mtf_timeframes:
        try:
            df = ex.get_klines(symbol, limit=max(cfg.ema_slow + 10, 60), interval=interval)
        except Exception:
            continue
        if len(df) < cfg.ema_slow + 2:
            continue
        close = df["close"]
        ema_fast = close.ewm(span=cfg.ema_fast, adjust=False).mean().iloc[-1]
        ema_slow = close.ewm(span=cfg.ema_slow, adjust=False).mean().iloc[-1]
        total += 1
        if side == "LONG" and ema_fast > ema_slow:
            agree += 1
        elif side == "SHORT" and ema_fast < ema_slow:
            agree += 1
    return agree, total


def timeframe_trend_matches(ex, cfg: Config, symbol: str, side: str, interval: str) -> bool:
    """Return whether one specific timeframe's EMA trend matches the side."""
    try:
        df = ex.get_klines(symbol, limit=max(cfg.ema_slow + 10, 60), interval=interval)
    except Exception:
        return False
    if len(df) < cfg.ema_slow + 2:
        return False
    close = df["close"]
    ema_fast = close.ewm(span=cfg.ema_fast, adjust=False).mean().iloc[-1]
    ema_slow = close.ewm(span=cfg.ema_slow, adjust=False).mean().iloc[-1]
    if side == "LONG":
        return bool(ema_fast > ema_slow)
    return bool(ema_fast < ema_slow)


def _signal_component_snapshot(df: pd.DataFrame, cfg: Config) -> dict:
    """Return the current EMA/BB signal building blocks for observability.

    scan_entry_candidate() logs signal_missing very frequently. Keeping this
    logic in one helper lets the live scan and offline analysis inspect the
    same conditions without re-implementing them in multiple places.
    """
    curr = df.iloc[-1]
    prev = df.iloc[-2]
    slope_lookback = max(1, int(getattr(cfg, "ema_slope_lookback", 2)))
    breakout_lookback = max(1, int(getattr(cfg, "bb_breakout_lookback", 1)))
    slope_idx = max(0, len(df) - 1 - slope_lookback)
    ema_fast_past = float(df.iloc[slope_idx]["ema_fast"])
    ema_slow_past = float(df.iloc[slope_idx]["ema_slow"])
    ema_fast_slope = float(curr["ema_fast"] - ema_fast_past)
    ema_slow_slope = float(curr["ema_slow"] - ema_slow_past)
    close_price = max(float(curr["close"]), 1e-12)
    ema_fast_slope_pct = ema_fast_slope / close_price * 100
    ema_slow_slope_pct = ema_slow_slope / close_price * 100
    slope_tolerance_pct = max(
        0.0, float(getattr(cfg, "ema_slope_relation_tolerance_pct", 0.0))
    )
    ema_gap_pct = abs(float(curr["ema_fast"] - curr["ema_slow"])) / max(float(curr["close"]), 1e-12) * 100
    ema_gap_ok = ema_gap_pct >= max(0.0, float(getattr(cfg, "ema_gap_min_pct", 0.10)))

    curr_bb_width = float(curr["bb_high"] - curr["bb_low"])
    prev_start = max(0, len(df) - 1 - breakout_lookback)
    prev_width_window = df.iloc[prev_start:len(df) - 1]
    prev_bb_width = float((prev_width_window["bb_high"] - prev_width_window["bb_low"]).mean()) if len(prev_width_window) else curr_bb_width
    width_ratio = (curr_bb_width / prev_bb_width) if prev_bb_width > 0 else 1.0
    width_expanding = width_ratio >= float(getattr(cfg, "bb_width_expansion_ratio", 1.01))

    # EMA direction is the base score. Price location is kept as a bonus
    # signal, not a hard gate, so BB events do not disappear in a 3m swing.
    long_trend_soft = (
        curr["ema_fast"] > curr["ema_slow"]
        and ema_fast_slope > 0
        and ema_fast_slope_pct >= ema_slow_slope_pct - slope_tolerance_pct
    )
    short_trend_soft = (
        curr["ema_fast"] < curr["ema_slow"]
        and ema_fast_slope < 0
        and ema_fast_slope_pct <= ema_slow_slope_pct + slope_tolerance_pct
    )
    long_trend = long_trend_soft and ema_gap_ok
    short_trend = short_trend_soft and ema_gap_ok

    long_mid_reclaim_raw = (
        prev["low"] <= prev["bb_mid"]
        and curr["low"] >= curr["bb_mid"]
        and curr["close"] > prev["high"]
        and curr["close"] >= curr["open"]
    )
    short_mid_reject_raw = (
        prev["high"] >= prev["bb_mid"]
        and curr["high"] <= curr["bb_mid"]
        and curr["close"] < prev["low"]
        and curr["close"] <= curr["open"]
    )
    long_mid_reclaim = long_mid_reclaim_raw and width_expanding
    short_mid_reject = short_mid_reject_raw and width_expanding

    long_band_break_raw = (
        prev["close"] <= prev["bb_high"]
        and curr["close"] >= curr["bb_high"]
        and curr["close"] > prev["high"]
    )
    short_band_break_raw = (
        prev["close"] >= prev["bb_low"]
        and curr["close"] <= curr["bb_low"]
        and curr["close"] < prev["low"]
    )
    long_band_break = long_band_break_raw and width_expanding
    short_band_break = short_band_break_raw and width_expanding
    return {
        "curr": curr,
        "ema_gap_pct": float(ema_gap_pct),
        "ema_gap_ok": bool(ema_gap_ok),
        "ema_fast_slope": float(ema_fast_slope),
        "ema_slow_slope": float(ema_slow_slope),
        "ema_fast_slope_pct": float(ema_fast_slope_pct),
        "ema_slow_slope_pct": float(ema_slow_slope_pct),
        "ema_slope_relation_tolerance_pct": float(slope_tolerance_pct),
        "width_ratio": float(width_ratio),
        "width_expanding": bool(width_expanding),
        "long_trend_soft": bool(long_trend_soft),
        "short_trend_soft": bool(short_trend_soft),
        "long_trend": bool(long_trend),
        "short_trend": bool(short_trend),
        "long_mid_reclaim_raw": bool(long_mid_reclaim_raw),
        "short_mid_reject_raw": bool(short_mid_reject_raw),
        "long_mid_reclaim": bool(long_mid_reclaim),
        "short_mid_reject": bool(short_mid_reject),
        "long_band_break_raw": bool(long_band_break_raw),
        "short_band_break_raw": bool(short_band_break_raw),
        "long_band_break": bool(long_band_break),
        "short_band_break": bool(short_band_break),
    }


def _direction_scores(df: pd.DataFrame, cfg: Config):
    """방향별(LONG/SHORT) 진입 점수(최대 2)와 현재 캔들 값을 반환한다.

    사용자가 요구한 현재 관점은 "1분/3분 미니스윙 + 볼밴/EMA 중심"이다.
    그래서 방향 점수도 아래 두 축만 본다.

    1. EMA 추세: LONG은 ema_fast > ema_slow, SHORT은 반대
    2. 볼밴 이벤트:
       - 재진입/리클레임: BB 중단선 근처를 찍고 방향 쪽으로 되돌림
       - 추세 지속: 상단/하단 밴드 돌파를 종가로 유지

    15분/30분 MTF는 별도 가산점일 뿐, 여기서 신호 자체를 뒤집지 않는다.
    """
    snapshot = _signal_component_snapshot(df, cfg)
    curr = snapshot["curr"]

    long_score = 0.0
    short_score = 0.0

    # 직전봉 재출발이 가장 우선이지만, 밴드 확장 동반 돌파도 같은 1점으로 본다.
    if snapshot["long_trend"]:
        long_score += 1
    if snapshot["short_trend"]:
        short_score += 1
    if getattr(cfg, "bb_mid_reclaim_entry_enabled", True) and snapshot["long_mid_reclaim"]:
        long_score += 1
    elif (
        getattr(cfg, "bb_mid_reclaim_entry_enabled", True)
        and snapshot["long_trend_soft"]
        and snapshot["long_mid_reclaim_raw"]
    ):
        long_score += max(0.0, min(1.0, float(getattr(cfg, "bb_width_soft_score", 0.5))))
    if getattr(cfg, "bb_mid_reclaim_entry_enabled", True) and snapshot["short_mid_reject"]:
        short_score += 1
    elif (
        getattr(cfg, "bb_mid_reclaim_entry_enabled", True)
        and snapshot["short_trend_soft"]
        and snapshot["short_mid_reject_raw"]
    ):
        short_score += max(0.0, min(1.0, float(getattr(cfg, "bb_width_soft_score", 0.5))))
    if getattr(cfg, "bb_breakout_entry_enabled", True):
        breakout_score = max(0.0, min(1.0, float(getattr(cfg, "bb_breakout_entry_score", 0.5))))
        if snapshot["long_band_break"]:
            long_score += breakout_score
        elif snapshot["long_trend_soft"] and snapshot["long_band_break_raw"]:
            long_score += min(breakout_score, max(0.0, float(getattr(cfg, "bb_width_soft_score", 0.5))))
        if snapshot["short_band_break"]:
            short_score += breakout_score
        elif snapshot["short_trend_soft"] and snapshot["short_band_break_raw"]:
            short_score += min(breakout_score, max(0.0, float(getattr(cfg, "bb_width_soft_score", 0.5))))
    # Band expansion is a state bonus, not a prerequisite. Mid reclaim and
    # breakout remain the stronger event bonuses above.
    if snapshot["width_expanding"]:
        long_score += 0.25 if snapshot["long_trend"] else 0.0
        short_score += 0.25 if snapshot["short_trend"] else 0.0
    return long_score, short_score, curr


def detect_reversal(df: pd.DataFrame, cfg: Config, side: str, min_votes: int | None = None) -> bool:
    """진입 이후 추세가 반대로 뒤집혔는지(또는 약해졌는지) 확인한다.
    조기 탈출(손실 중)과 동적 익절(수익 중, 더 태울지 판단) 양쪽에서 재사용한다.

    진입 시점의 크로스/반등 '사건'과 달리, 지금 이 순간의 상태 3가지를 본다:
      1. EMA 추세가 반대로 뒤집혔는지
      2. MACD가 반대쪽으로 넘어갔는지
      3. RSI가 중립선(50)을 반대 방향으로 넘었는지
    min_votes(기본 cfg.reversal_min_votes)개 이상 해당하면 추세 전환/약화로 판단한다.
    """
    curr = df.iloc[-1]
    if side == "LONG":
        trend_flipped = curr["ema_fast"] < curr["ema_slow"]
        momentum_flipped = curr["macd"] < curr["macd_signal"]
        rsi_weak = curr["rsi"] < 50
    else:
        trend_flipped = curr["ema_fast"] > curr["ema_slow"]
        momentum_flipped = curr["macd"] > curr["macd_signal"]
        rsi_weak = curr["rsi"] > 50

    votes = sum([trend_flipped, momentum_flipped, rsi_weak])
    threshold = min_votes if min_votes is not None else cfg.reversal_min_votes
    return votes >= threshold


def estimate_entry_probability(matched_count: int, adx_value: float, adx_cap: float = 25.0) -> float:
    """신호 신뢰도(EMA 방향성과 BB 이벤트 중 몇 개가 맞는지)와 추세 강도(ADX)를 결합해
    진입 성공 확률 추정치를 만든다. 변동성(ATR)은 반등/역행 위험도 같이 키우므로
    이 확률 추정에는 포함하지 않는다.

    1분봉 스캘핑 기준으로 판단하므로, adx_cap(기본 25)을 넘으면 만점을 준다.
    """
    confirmation_ratio = min(max(matched_count, 0), 2) / 2
    trend_component = min(adx_value / adx_cap, 1.0) if adx_cap > 0 else 0.0
    return confirmation_ratio * 0.6 + trend_component * 0.4


def bb_participates(snapshot: dict, side: str) -> bool:
    """볼린저밴드가 이 방향 판단에 실제로 관여했는지.

    [2026-08-25 원칙0 정합] 원칙 0은 "볼밴 매매 + EMA(3분봉)"인데, 점수제에서 EMA 추세만으로
    1.0점이 나오고 MIN_SIGNAL_CONFIRMATIONS=1이라 볼밴이 하나도 안 걸린 순수 EMA 진입이
    통과하고 있었다. 여기서 "볼밴 관여"를 명시적으로 정의한다 —
      - 중단선 리클레임/거부 이벤트, 또는
      - 상/하단 밴드 돌파 이벤트, 또는
      - 밴드 폭 확장 상태(가격 위치는 아니지만 볼밴 지표에서 나오는 판단 근거)
    이벤트만 필수로 걸면 신호 공급이 16~18%까지 떨어져 원칙 1을 정면으로 깬다(3분봉 캐시
    85심볼 5,270표본 실측). 밴드확장까지 볼밴 관여로 인정하면 LONG 73.1% / SHORT 64.4%가
    남아 원칙 1 타격을 감당 가능한 수준으로 줄이면서 순수 EMA 진입만 배제한다.
    """
    if side == "LONG":
        event = bool(snapshot["long_mid_reclaim_raw"] or snapshot["long_band_break_raw"])
    else:
        event = bool(snapshot["short_mid_reject_raw"] or snapshot["short_band_break_raw"])
    return bool(event or snapshot["width_expanding"])


def _bb_gate_ok(df: pd.DataFrame, cfg: Config, side: str) -> bool:
    """cfg.bb_participation_required가 켜져 있을 때만 볼밴 관여를 강제한다(꺼져 있으면 기존 동작)."""
    if not getattr(cfg, "bb_participation_required", False):
        return True
    return bb_participates(_signal_component_snapshot(df, cfg), side)


def generate_signal(df: pd.DataFrame, cfg: Config, min_confirmations: int | None = None) -> str | None:
    """지표 조합으로 진입 신호를 판단한다 (스캘핑용, 투표 방식 다중 확인).

    아래 6개 지표를 각각 0/1(충족 여부)로 판단해 방향별로 합산한다.
      1. 추세: EMA(fast/slow)
      2. RSI 반등/하락
      3. 스토캐스틱 RSI 반등/하락
      4. MACD 골든/데드 크로스
      5. ADX가 임계값 이상 (추세 강도, 횡보장 필터링)
      6. 거래량이 평균 이상 (가짜 돌파 방지)

    합산 점수가 cfg.min_confirmations 이상이면 진입. 6개 전부가 아니라
    일부만 맞아도 되므로, min_confirmations를 낮추면 더 자주 거래되고
    (대신 거짓 신호도 늘어남), 높이면 더 신중하게 거래된다.
    """
    warmup = max(cfg.ema_slow, cfg.macd_slow, cfg.bb_period, cfg.atr_period, cfg.adx_period, cfg.volume_ma_period)
    if len(df) < warmup + cfg.macd_signal + 2:
        return None

    long_score, short_score, _curr = _direction_scores(df, cfg)
    threshold = min_confirmations if min_confirmations is not None else cfg.min_confirmations
    if long_score >= threshold and long_score >= short_score and _bb_gate_ok(df, cfg, "LONG"):
        return "LONG"
    if short_score >= threshold and short_score > long_score and _bb_gate_ok(df, cfg, "SHORT"):
        return "SHORT"
    return None


def generate_signal_with_probability(df: pd.DataFrame, cfg: Config, min_confirmations: int | None = None) -> tuple[str | None, float]:
    """generate_signal과 동일하게 판단하되, 함께 진입 성공 확률 추정치도 반환한다."""
    warmup = max(cfg.ema_slow, cfg.macd_slow, cfg.bb_period, cfg.atr_period, cfg.adx_period, cfg.volume_ma_period)
    if len(df) < warmup + cfg.macd_signal + 2:
        return None, 0.0

    long_score, short_score, curr = _direction_scores(df, cfg)
    threshold = min_confirmations if min_confirmations is not None else cfg.min_confirmations
    if long_score >= threshold and long_score >= short_score and _bb_gate_ok(df, cfg, "LONG"):
        return "LONG", estimate_entry_probability(long_score, curr["adx"], cfg.probability_adx_cap)
    if short_score >= threshold and short_score > long_score and _bb_gate_ok(df, cfg, "SHORT"):
        return "SHORT", estimate_entry_probability(short_score, curr["adx"], cfg.probability_adx_cap)
    return None, 0.0


def generate_frequency_signal_with_probability(
    df: pd.DataFrame,
    cfg: Config,
    min_confirmations: int | None = None,
) -> tuple[str | None, float, dict]:
    """Return a narrowly-relaxed signal for the small frequency lane only.

    This rescues near-miss EMA+BB setups that miss the main lane by a small
    margin, typically because BB width or EMA gap is slightly late. It still
    requires the EMA direction to point the same way and a real BB trigger.
    """
    warmup = max(cfg.ema_slow, cfg.macd_slow, cfg.bb_period, cfg.atr_period, cfg.adx_period, cfg.volume_ma_period)
    if len(df) < warmup + cfg.macd_signal + 2:
        return None, 0.0, {}
    if not getattr(cfg, "frequency_lane_enabled", False) or not getattr(cfg, "frequency_lane_signal_enabled", False):
        return None, 0.0, {}

    long_score, short_score, curr = _direction_scores(df, cfg)
    threshold = float(min_confirmations if min_confirmations is not None else cfg.min_confirmations)
    relaxed_threshold = max(0.0, threshold - max(0.0, float(getattr(cfg, "frequency_lane_signal_score_discount", 0.5))))
    snapshot = _signal_component_snapshot(df, cfg)

    long_bb_trigger = bool(snapshot["long_mid_reclaim_raw"] or snapshot["long_band_break_raw"])
    short_bb_trigger = bool(snapshot["short_mid_reject_raw"] or snapshot["short_band_break_raw"])
    long_ok = (
        snapshot["long_trend_soft"]
        and long_bb_trigger
        and long_score >= relaxed_threshold
        and long_score > short_score
    )
    short_ok = (
        snapshot["short_trend_soft"]
        and short_bb_trigger
        and short_score >= relaxed_threshold
        and short_score > long_score
    )
    if long_ok:
        return "LONG", estimate_entry_probability(long_score, curr["adx"], cfg.probability_adx_cap), {
            "relaxed_threshold": float(relaxed_threshold),
            "score": float(long_score),
            "direction": "LONG",
        }
    if short_ok:
        return "SHORT", estimate_entry_probability(short_score, curr["adx"], cfg.probability_adx_cap), {
            "relaxed_threshold": float(relaxed_threshold),
            "score": float(short_score),
            "direction": "SHORT",
        }
    return None, 0.0, {}


def immediate_momentum_ok(df: pd.DataFrame, side: str) -> bool:
    """진입 즉시 불리하게 시작할 위험이 큰 상황을 걸러낸다.

    캔들 방향 확인: 지표는 반등/크로스를 알려줘도, 지금 이 순간의 캔들이 반대
    방향으로 마감 중이면(롱인데 음봉 등) 진입 직후 불리하게 시작할 가능성이 높다.

    [2026-08-06] 기존엔 볼린저 상단/하단 이탈 시("과열") 진입을 막았으나, 펌프감지
    재설계 이후로는 신호 자체가 "이미 크게 움직인 캔들"을 의도적으로 잡는 방식이라
    이 과열 체크가 검증된 신호를 그대로 걸러내는 문제가 있어 제거함(백테스트에는
    이 필터가 없었음).
    """
    curr = df.iloc[-1]
    if side == "LONG":
        bullish_candle = curr["close"] >= curr["open"]
        return bool(bullish_candle)
    else:
        bearish_candle = curr["close"] <= curr["open"]
        return bool(bearish_candle)


def is_momentum_continuing(df: pd.DataFrame, cfg: Config, side: str) -> bool:
    """[2026-08-10] 익절 트레일링 확정 직전, 지금 이 캔들이 포지션 방향으로 "또" 크게
    움직이는 중(진입 신호와 같은 기준: 변동폭+거래량 급증)이면 트레일링 확정을 이번
    주기엔 보류하고 조금 더 태운다 — 큰 양봉(롱)/음봉(숏) 도중에 최소 익절선 근처에서
    서둘러 내려서 나머지 상승분을 놓치는 문제를 줄이기 위함.

    진입 신호(_direction_scores)와 완전히 같은 기준(pump_min_candle_chg_pct/
    pump_min_volume_ratio)을 재사용한다 — "처음 올라탈 때 크게 움직이는 캔들을 잡는다"는
    같은 논리를 "타는 도중에도 여전히 크게 움직이면 아직 안 끝났다고 본다"로 그대로
    확장한 것이라, 새 파라미터를 따로 튜닝할 필요가 없다. 절대 상한(take_profit_hard_cap)이
    항상 위에서 무조건 확정시키므로, 계속 보류돼도 무한정 노출되지는 않는다."""
    curr = df.iloc[-1]
    candle_chg_pct = ((curr["close"] / curr["open"]) - 1) * 100 if curr["open"] else 0.0
    volume_ratio = (curr["volume"] / curr["volume_ma"]) if curr.get("volume_ma") else 0.0
    if side == "LONG":
        return candle_chg_pct >= cfg.pump_min_candle_chg_pct and volume_ratio >= cfg.pump_min_volume_ratio
    else:
        return candle_chg_pct <= -cfg.pump_min_candle_chg_pct and volume_ratio >= cfg.pump_min_volume_ratio


def is_swing_continuing(df: pd.DataFrame, cfg: Config, side: str) -> bool:
    """[2026-08-10 사용자요청] "5분~최대 15분봉으로 발라먹고 싶다" — 예시로 든 실제 코인
    캔들(예: 1.12, 1.36, 2.93, 1.41, -0.50, 1.71, -0.97, 1.48, 2.55%)을 보면, 중간중간
    -0.5%/-0.97% 같은 작은 눌림목이 끼어 있어도 전체적으로는 뚜렷한 상승 흐름이다.

    [최초 버전, 폐기] "최근 N개 캔들이 전부 개별적으로 강해야 한다"는 방식이었는데, 이러면
    한 캔들이라도 눌림목이면 즉시 False가 돼서 위 예시 같은 패턴을 못 잡는다는 게 사용자
    피드백으로 확인됨.

    [현재 버전] 개별 캔들 전부를 보는 대신, 최근 N개(cfg.swing_streak_candles, 5~15분에
    대응) 구간의 "누적" 변동폭과 평균 거래량으로 판단한다 — 중간의 작은 반대방향 캔들은
    누적치를 깎아먹을 뿐 즉시 탈락시키지 않는다. 누적 문턱은 "캔들 하나가 펌프 기준을
    막 넘기는 강도(pump_min_candle_chg_pct)"를 N개 중 과반(swing_cumulative_threshold_ratio,
    기본 0.6)만큼만 채우면 되게 완화했다 — 매 캔들이 펌프급일 필요는 없고, 전체 흐름이
    그 정도 세기로 이어지면 충분하다는 뜻."""
    n = cfg.swing_streak_candles
    if len(df) < n:
        return False
    recent = df.iloc[-n:]
    first_open = recent.iloc[0]["open"]
    last_close = recent.iloc[-1]["close"]
    cum_chg_pct = ((last_close / first_open) - 1) * 100 if first_open else 0.0
    volume_ma_mean = recent["volume_ma"].mean() if "volume_ma" in recent else 0.0
    avg_vol_ratio = (recent["volume"] / recent["volume_ma"]).mean() if volume_ma_mean else 0.0
    cum_threshold = cfg.pump_min_candle_chg_pct * n * cfg.swing_cumulative_threshold_ratio
    vol_threshold = cfg.pump_min_volume_ratio * cfg.swing_cumulative_threshold_ratio
    if side == "LONG":
        return cum_chg_pct >= cum_threshold and avg_vol_ratio >= vol_threshold
    else:
        return cum_chg_pct <= -cum_threshold and avg_vol_ratio >= vol_threshold


def volume_direction_ok(df: pd.DataFrame, side: str, cfg: Config) -> bool:
    """단순히 거래량이 많은지가 아니라, 실제 매수/매도 중 어느 쪽 체결이 더 많았는지
    (테이커 매수 비율)로 방향성을 확인한다. 캔들이 위로 마감해도 사실 매도 체결이
    더 많았을 수 있는데, 그런 '힘 없는' 움직임을 걸러낸다."""
    curr = df.iloc[-1]
    ratio = curr["taker_buy_ratio"]
    if side == "LONG":
        return bool(ratio >= cfg.taker_imbalance_threshold)
    # SHORT 전용 임계값이 설정돼 있으면(>0) 그걸 쓰고, 아니면 공용값을 그대로 쓴다.
    threshold = getattr(cfg, "short_taker_imbalance_threshold", 0.0) or cfg.taker_imbalance_threshold
    return bool(ratio <= 1 - threshold)


def volume_increase_ok(df: pd.DataFrame, cfg: Config) -> bool:
    if not getattr(cfg, "volume_increase_filter_enabled", True):
        return True
    curr = df.iloc[-1]
    volume = float(curr.get("volume", 0.0) or 0.0)
    volume_ma = float(curr.get("volume_ma", 0.0) or 0.0)
    if volume_ma <= 0:
        return True
    return volume / volume_ma >= max(0.0, float(getattr(cfg, "volume_increase_min_ratio", 1.05)))


def quick_profit_score(df: pd.DataFrame, cfg: Config, side: str) -> float:
    """이 코인이 최대 보유시간(max_hold_minutes) 안에 목표 익절선까지
    도달할 가능성이 얼마나 높은지를 0.0~1.0으로 추정한다 (수익을 보장하는 건
    아니고, 평소 변동성 대비 목표가 상대적으로 가깝다는 걸 나타내는 지표).

    1분봉 ATR(평균 한 캔들의 움직임)을 목표 익절률과 비교해서, ATR이 목표에
    비해 클수록(평소에도 그만큼은 잘 움직이는 코인일수록) 점수가 높다.
    """
    curr = df.iloc[-1]
    price = curr["close"]
    if price <= 0:
        return 0.0

    atr_pct = (curr["atr"] / price) * 100
    target_pct = cfg.short_take_profit_min if side == "SHORT" else cfg.take_profit_min
    if target_pct <= 0:
        return 0.0

    # ATR이 목표 익절률과 같거나 크면(한 캔들만으로도 닿을 수 있는 변동성) 만점
    return min(atr_pct / target_pct, 1.0)


def signal_strength(df: pd.DataFrame, cfg: Config) -> float:
    """직전 신호가 얼마나 강한지 0.0~1.0으로 반환한다 (레버리지 자동 조절용).

    - RSI가 과매도/과매수 기준선을 얼마나 크게 뚫고 반등/하락했는지
    - MACD 히스토그램(macd - signal)이 가격 대비 얼마나 큰지
    두 값을 각각 0~1로 정규화해 평균낸다.
    """
    curr = df.iloc[-1]

    rsi_dist = abs(curr["rsi"] - 50) / 50  # 중립(50)에서 얼마나 멀리 떨어졌는지
    rsi_score = min(rsi_dist / 0.4, 1.0)  # RSI 20 또는 80 이상이면 만점

    macd_hist = abs(curr["macd"] - curr["macd_signal"])
    macd_score = min(macd_hist / (curr["close"] * 0.001), 1.0)  # 가격의 0.1% 이상 벌어지면 만점

    return round((rsi_score + macd_score) / 2, 4)


def take_profit_price(entry_price: float, side: str, cfg: Config, target_pct: float | None = None) -> float:
    pct = target_pct if target_pct is not None else cfg.take_profit_min
    pct /= 100
    return entry_price * (1 + pct) if side == "LONG" else entry_price * (1 - pct)


def stop_loss_price(entry_price: float, side: str, cfg: Config) -> float:
    pct = cfg.stop_loss_pct / 100
    return entry_price * (1 - pct) if side == "LONG" else entry_price * (1 + pct)


def pnl_pct(entry_price: float, mark_price: float, side: str) -> float:
    diff = (mark_price - entry_price) / entry_price
    return diff * 100 if side == "LONG" else -diff * 100


def should_take_profit(entry_price: float, mark_price: float, side: str, cfg: Config) -> bool:
    return pnl_pct(entry_price, mark_price, side) >= cfg.take_profit_min


def should_stop_loss(entry_price: float, mark_price: float, side: str, cfg: Config) -> bool:
    return pnl_pct(entry_price, mark_price, side) <= -cfg.stop_loss_pct


def spike_based_entry_signal(cache, symbol: str, side: str, cfg: Config, now_ms: int | None = None) -> bool:
    """[2026-08-15] 체결(aggTrade) 스트림 기반 조기진입 게이트 - 실험적, 기존 PUMP_SIGNAL
    (1분봉 완성 대기) 로직과 완전히 병행하는 별도 경로. 아직 어떤 라이브 진입 흐름에도
    연결돼 있지 않다(cfg.spike_entry_enabled로 호출 여부를 결정하는 건 호출하는 쪽 책임).

    detect_volume_spike()로 "최근 spike_entry_window_sec초 체결대금이 baseline 대비
    spike_entry_multiplier배 이상"인지만 판단한다. 방향(테이커 매수/매도 비율)까지는 보지
    않는다 - TradeTick에 개별 방향은 있지만 순수 거래량 급증만 신호로 쓰고, 실제 방향
    판단은 여전히 기존 1분봉 지표(generate_signal_with_probability)가 담당한다는 설계.
    cache가 None이거나 아직 데이터가 없으면 보수적으로 False.

    [2026-08-15 라이브 배선] 실거래에서는 체결 원시 틱이 별도 프로세스(ws_trade_worker.py)
    안에만 있고, 메인 프로세스는 그 워커가 상태파일에 남긴 "이미 계산된" 스파이크 판정만
    읽는다(FileBackedSpikeCache) — 원시 틱을 프로세스 경계 밖으로 복제하는 무거운 방식을
    피하기 위함. 이 cache가 그런 파일백드 리더면 is_spike()를 바로 쓰고, 테스트/백테스트처럼
    진짜 TradeTickCache(get_recent 보유)가 오면 기존 방식(detect_volume_spike) 그대로 유지 —
    기존 단위테스트 경로는 전혀 안 바뀐다."""
    if cache is None:
        return False
    if hasattr(cache, "is_spike") and not hasattr(cache, "get_recent"):
        try:
            return bool(cache.is_spike(symbol))
        except Exception:
            return False
    from .ws_trade_client import detect_volume_spike
    result = detect_volume_spike(
        cache, symbol,
        spike_multiplier=cfg.spike_entry_multiplier,
        spike_window_sec=cfg.spike_entry_window_sec,
        baseline_window_sec=cfg.spike_entry_baseline_sec,
        now_ms=now_ms,
    )
    return bool(result["is_spike"])
