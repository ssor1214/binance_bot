import pandas as pd
from ta.momentum import RSIIndicator, StochRSIIndicator
from ta.trend import ADXIndicator, EMAIndicator, MACD
from ta.volatility import AverageTrueRange, BollingerBands

from .config import Config


def add_indicators(df: pd.DataFrame, cfg: Config) -> pd.DataFrame:
    # 데이터가 너무 적으면(신규 상장 코인, API 응답 잘림 등) 내부 ta 라이브러리가
    # IndexError 등 알아보기 힘든 예외를 던진다. 여기서 미리 걸러서 원인이 분명한
    # 예외로 바꿔준다 — 호출부의 except Exception이 여전히 잡아서 그 심볼만 건너뛴다.
    min_len = max(
        cfg.ema_slow, cfg.macd_slow, cfg.bb_period, cfg.atr_period,
        cfg.adx_period, cfg.volume_ma_period, cfg.stoch_rsi_period, cfg.rsi_period,
    ) + cfg.macd_signal + 2
    if len(df) < min_len:
        raise ValueError(
            f"add_indicators: 데이터가 부족합니다 (필요 {min_len}개, 실제 {len(df)}개) — "
            f"신규 상장 코인이거나 API 응답이 잘렸을 수 있습니다."
        )
    df = df.copy()
    df["rsi"] = RSIIndicator(df["close"], window=cfg.rsi_period).rsi()

    ema_fast = EMAIndicator(df["close"], window=cfg.ema_fast).ema_indicator()
    ema_slow = EMAIndicator(df["close"], window=cfg.ema_slow).ema_indicator()
    df["ema_fast"] = ema_fast
    df["ema_slow"] = ema_slow

    macd = MACD(
        df["close"],
        window_fast=cfg.macd_fast,
        window_slow=cfg.macd_slow,
        window_sign=cfg.macd_signal,
    )
    df["macd"] = macd.macd()
    df["macd_signal"] = macd.macd_signal()
    df["macd_diff"] = macd.macd_diff()

    stoch_rsi = StochRSIIndicator(df["close"], window=cfg.stoch_rsi_period)
    df["stoch_rsi_k"] = stoch_rsi.stochrsi_k() * 100
    df["stoch_rsi_d"] = stoch_rsi.stochrsi_d() * 100

    bb = BollingerBands(df["close"], window=cfg.bb_period, window_dev=cfg.bb_std)
    df["bb_high"] = bb.bollinger_hband()
    df["bb_low"] = bb.bollinger_lband()
    df["bb_mid"] = bb.bollinger_mavg()

    atr = AverageTrueRange(df["high"], df["low"], df["close"], window=cfg.atr_period)
    df["atr"] = atr.average_true_range()

    adx = ADXIndicator(df["high"], df["low"], df["close"], window=cfg.adx_period)
    df["adx"] = adx.adx()
    # +DI/-DI: ADX 값 자체는 "추세가 강한지"만 알려주고 방향은 안 알려준다.
    # 진짜 상승 추세인지 하락 추세인지는 +DI와 -DI 중 어느 쪽이 우세한지로 판단해야 한다.
    df["di_plus"] = adx.adx_pos()
    df["di_minus"] = adx.adx_neg()

    df["volume_ma"] = df["volume"].rolling(window=cfg.volume_ma_period).mean()

    # 테이커(시장가) 매수 비율: 0.5보다 크면 그 캔들에서 매수 체결이 더 많았다는 뜻(진짜 매수세),
    # 작으면 매도 체결이 더 많았다는 뜻. 단순 거래량보다 방향성을 더 정확히 보여준다.
    df["taker_buy_ratio"] = (df["taker_buy_base"] / df["volume"]).where(df["volume"] > 0, 0.5)

    return df
