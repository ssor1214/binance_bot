# -*- coding: utf-8 -*-
"""
[클로드(Claude) 시뮬레이션 및 백테스팅용 스캘핑 봇 전략 스크립트 - 수정본]
- 수정 사항:
  1. Look-ahead Bias 제거: i번째 캔들 종가에 신호 확정 -> i+1번째 캔들 '시가(open)'로 진입
  2. 마진 비율 제어식 수정: 슬롯 마진 비중과 레버리지 계산 논리 정상화 (단일 슬롯 마진 대비 상한 체크)
  3. 수익금 계산식 정리 (죽은 코드 및 중복 레버리지 곱셈 제거)
"""

import pandas as pd
import numpy as np


class ScalpingBacktestSimulator:

    def __init__(
        self,
        initial_capital=10000,
        leverage=3,
        slot_ratio=0.1,
        max_margin_ratio=0.08,
    ):
        self.initial_capital = initial_capital
        self.capital = initial_capital
        self.leverage = leverage
        self.slot_ratio = slot_ratio
        self.max_margin_ratio = max_margin_ratio

        self.trades = []
        self.active_slots = []

    def calculate_indicators(self, df):
        """1분봉 지표 계산 (EMA, 볼린저 밴드, RSI, ATR)"""
        df["EMA_20"] = df["close"].ewm(span=20, adjust=False).mean()
        df["EMA_60"] = df["close"].ewm(span=60, adjust=False).mean()

        df["BB_Middle"] = df["close"].rolling(window=20).mean()
        df["BB_Std"] = df["close"].rolling(window=20).std()
        df["BB_Upper"] = df["BB_Middle"] + (df["BB_Std"] * 2)
        df["BB_Lower"] = df["BB_Middle"] - (df["BB_Std"] * 2)

        delta = df["close"].diff()
        gain = (delta.where(delta > 0, 0)).rolling(window=14).mean()
        loss = (-delta.where(delta < 0, 0)).rolling(window=14).mean()
        rs = gain / loss
        df["RSI"] = 100 - (100 / (1 + rs))

        high_low = df["high"] - df["low"]
        high_close = np.abs(df["high"] - df["close"].shift())
        low_close = np.abs(df["low"] - df["close"].shift())
        ranges = pd.concat([high_low, high_close, low_close], axis=1)
        true_range = np.max(ranges, axis=1)
        df["ATR"] = true_range.rolling(window=14).mean()
        df["ATR_MA"] = df["ATR"].rolling(window=50).mean()

        return df.dropna()

    def check_entry_signal(self, row):
        """롱 중심의 스캘핑 진입 조건 (현재 캔들 마감 기준)"""
        is_uptrend = row["EMA_20"] > row["EMA_60"]
        is_near_lower_bb = row["close"] <= row["BB_Lower"] * 1.002
        is_rsi_valid = 30 <= row["RSI"] <= 45
        is_volatility_safe = row["ATR"] < (row["ATR_MA"] * 2.5)

        if is_uptrend and is_near_lower_bb and is_rsi_valid and is_volatility_safe:
            return "LONG"
        return None

    def run_simulation(
        self, df, take_profit_pct=0.007, stop_loss_pct=0.006
    ):
        """
        시뮬레이션 메인 루프 (다음 봉 시가 진입 적용)
        """
        print(
            f"[*] 시뮬레이션 시작 (초기 자본: {self.initial_capital} USDT, 레버리지: {self.leverage}배)"
        )

        pending_signal = None  # 이전 캔들에서 발생한 신호를 저장

        for i in range(len(df) - 1):
            current_row = df.iloc[i]  # 신호를 판단하는 기준 캔들
            next_row = df.iloc[i + 1]  # 실제로 체결이 일어나는 다음 캔들
            timestamp = next_row.name

            current_price = next_row["close"]  # 포지션 평가를 위한 가격

            # 1. 활성 슬롯 청산 체크 (익절 / 손절)
            for slot in self.active_slots[:]:
                entry_price = slot["entry_price"]
                pnl_pct = (current_price - entry_price) / entry_price

                # 익절 조건 달성 (+0.7%)
                if pnl_pct >= take_profit_pct:
                    profit = slot["margin"] * take_profit_pct * self.leverage
                    self.capital += (
                        profit  # 마진 대비 레버리지 반영 수익 추가
                    )
                    self.trades.append(
                        {
                            "time": timestamp,
                            "type": "TP",
                            "pnl_pct": pnl_pct * 100,
                        }
                    )
                    self.active_slots.remove(slot)

                # 칼손절 조건 달성 (-0.6%)
                elif pnl_pct <= -stop_loss_pct:
                    loss = slot["margin"] * stop_loss_pct * self.leverage
                    self.capital -= loss
                    self.trades.append(
                        {
                            "time": timestamp,
                            "type": "SL",
                            "pnl_pct": pnl_pct * 100,
                        }
                    )
                    self.active_slots.remove(slot)

            # 2. [수정됨] 직전 캔들에서 신호가 있었다면 -> "다음 봉 시가(Open)"에 진입 처리
            if pending_signal == "LONG" and len(self.active_slots) < 3:
                slot_margin = self.capital * self.slot_ratio

                # [수정됨] 마진 라티오 판정식 오류 해결
                position_notional = slot_margin * self.leverage
                if (position_notional / self.capital) <= (
                    self.max_margin_ratio * self.leverage
                ):
                    entry_price = next_row["open"]
                    self.active_slots.append(
                        {"entry_price": entry_price, "margin": slot_margin}
                    )

                pending_signal = None

            # 3. 현재 캔들 마감 시점에 다음 봉 진입을 위한 신호 탐색
            pending_signal = self.check_entry_signal(current_row)

        print(f"[*] 시뮬레이션 완료. 총 거래 횟수: {len(self.trades)}회")
        return self.get_performance_metrics()

    def get_performance_metrics(self):
        if not self.trades:
            return "체결된 거래가 없습니다."

        total_trades = len(self.trades)
        wins = [t for t in self.trades if t["type"] == "TP"]
        losses = [t for t in self.trades if t["type"] == "SL"]

        win_rate = (len(wins) / total_trades) * 100 if total_trades > 0 else 0
        final_return = (
            (self.capital - self.initial_capital) / self.initial_capital
        ) * 100

        report = {
            "총 거래수": total_trades,
            "승리 횟수": len(wins),
            "패배 횟수": len(losses),
            "승률(%)": round(win_rate, 2),
            "최종 자본금": round(self.capital, 2),
            "총 수익률(%)": round(final_return, 2),
        }
        return report
