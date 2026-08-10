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


def _direction_scores(df: pd.DataFrame, cfg: Config):
    """방향별(LONG/SHORT) 신호 점수(최대 1)와 현재 캔들 값을 반환한다.

    [2026-08-04 재설계] 기존 6개 지표 투표 방식을 실측 백테스트로 검증한 결과 손익분기
    승률에 못 미쳐 폐기. 이후 MACD모멘텀+거래량 방식으로 교체했으나, 이 역시 8/6 재검증
    (모멘텀/평균회귀/채널돌파/ADX+테이커쏠림/RSI/EMA/OI+롱숏비율 등 13차례, 1000개+ 조합)에서
    전부 손익분기를 못 넘겨 폐기.

    [2026-08-06 펌프감지 재설계] 대신 "이미 크게 움직이기 시작한 캔들(변동폭+거래량 급증)에
    올라타는" 방식을 72시간/200심볼 교차검증한 결과, 승률 37%대(손익분기 30.9%, TP8%/SL3%
    기준)로 EV가 유의미하게 양전인 유일한 신호로 확인됨. 승률 자체는 낮지만 익절:손절
    비율을 크게(8:3) 벌려서 "자주 틀려도 크게 먹는" 비대칭 구조로 이긴다 — 필터(추세일치/
    RSI과열회피/테이커확인 등)를 추가해도 승률이 안 오르고 표본만 줄어 오히려 손해임을
    확인했으므로 필터 없이 순수 변동폭+거래량 조건만 사용한다.
    """
    curr = df.iloc[-1]

    candle_chg_pct = ((curr["close"] / curr["open"]) - 1) * 100 if curr["open"] else 0.0
    volume_ratio = (curr["volume"] / curr["volume_ma"]) if curr["volume_ma"] else 0.0

    pump_up = candle_chg_pct >= cfg.pump_min_candle_chg_pct and volume_ratio >= cfg.pump_min_volume_ratio
    pump_down = candle_chg_pct <= -cfg.pump_min_candle_chg_pct and volume_ratio >= cfg.pump_min_volume_ratio

    long_score = 1 if pump_up else 0
    short_score = 1 if pump_down else 0
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
    """신호 신뢰도(재설계 후 최대 2개 중 몇 개 일치: MACD모멘텀가속, 거래량)와 추세 강도(ADX)를
    결합해 진입 성공(수익 전환) 확률을 0.0~1.0으로 추정한다. 변동성(ATR)은 반등/역행 위험도
    같이 키우므로 이 확률 추정에는 포함하지 않는다.

    1분봉 스캘핑 기준으로 판단하므로, adx_cap(기본 25)을 넘으면 만점을 준다.
    """
    confirmation_ratio = matched_count / 1
    trend_component = min(adx_value / adx_cap, 1.0) if adx_cap > 0 else 0.0
    return confirmation_ratio * 0.6 + trend_component * 0.4


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
    if long_score >= threshold and long_score >= short_score:
        return "LONG"
    if short_score >= threshold and short_score > long_score:
        return "SHORT"
    return None


def generate_signal_with_probability(df: pd.DataFrame, cfg: Config, min_confirmations: int | None = None) -> tuple[str | None, float]:
    """generate_signal과 동일하게 판단하되, 함께 진입 성공 확률 추정치도 반환한다."""
    warmup = max(cfg.ema_slow, cfg.macd_slow, cfg.bb_period, cfg.atr_period, cfg.adx_period, cfg.volume_ma_period)
    if len(df) < warmup + cfg.macd_signal + 2:
        return None, 0.0

    long_score, short_score, curr = _direction_scores(df, cfg)
    threshold = min_confirmations if min_confirmations is not None else cfg.min_confirmations
    if long_score >= threshold and long_score >= short_score:
        return "LONG", estimate_entry_probability(long_score, curr["adx"], cfg.probability_adx_cap)
    if short_score >= threshold and short_score > long_score:
        return "SHORT", estimate_entry_probability(short_score, curr["adx"], cfg.probability_adx_cap)
    return None, 0.0


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
    return bool(ratio <= 1 - cfg.taker_imbalance_threshold)


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
