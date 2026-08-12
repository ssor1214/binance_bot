# -*- coding: utf-8 -*-
"""
[Strategy 1: Market Making & Spread Scalping Simulator]
- 특징: 지정가(Maker) 주문 체결 가정, 스프레드 수익 추구, 인벤토리 컷(재고 관리)
"""

import numpy as np
import pandas as pd


class MarketMakingSimulator:

    def __init__(
        self,
        initial_capital=10000,
        leverage=3,
        slot_ratio=0.1,
        spread_pct=0.0015,
        maker_fee=-0.0002,
        taker_fee=0.0004,
    ):
        self.initial_capital = initial_capital
        self.capital = initial_capital
        self.leverage = leverage
        self.slot_ratio = slot_ratio
        self.spread_pct = spread_pct
        self.maker_fee = maker_fee
        self.taker_fee = taker_fee

        self.trades = []
        self.inventory = []

    def run_simulation(self, df):
        print(
            f"[*] 마켓 메이킹 시뮬레이션 시작 (초기 자본: {self.initial_capital} USDT)"
        )

        for i in range(1, len(df)):
            current_price = df.iloc[i]["price"]
            timestamp = df.iloc[i]["timestamp"]

            if len(self.inventory) >= 3:
                total_loss = 0
                for slot in self.inventory:
                    loss_pct = (
                        current_price - slot["entry_price"]
                    ) / slot["entry_price"]
                    total_loss += (
                        slot["margin"] * loss_pct * self.leverage
                    ) + (slot["margin"] * self.leverage * self.taker_fee)

                self.capital += total_loss
                self.trades.append(
                    {
                        "time": timestamp,
                        "type": "INVENTORY_CUT",
                        "pnl": total_loss,
                    }
                )
                self.inventory.clear()
                continue

            prev_price = df.iloc[i - 1]["price"]
            price_change = (current_price - prev_price) / prev_price

            if price_change <= -self.spread_pct:
                slot_margin = self.capital * self.slot_ratio
                self.inventory.append(
                    {"entry_price": current_price, "margin": slot_margin}
                )

            for slot in self.inventory[:]:
                target_price = slot["entry_price"] * (1 + self.spread_pct)
                if current_price >= target_price:
                    gross_profit = (
                        slot["margin"] * self.spread_pct * self.leverage
                    )
                    fee = slot["margin"] * self.leverage * abs(self.maker_fee)
                    net_profit = gross_profit + fee

                    self.capital += net_profit
                    self.trades.append(
                        {"time": timestamp, "type": "MAKER_TP", "pnl": net_profit}
                    )
                    self.inventory.remove(slot)

        return self.get_metrics()

    def get_metrics(self):
        total_trades = len(self.trades)
        final_return = (
            (self.capital - self.initial_capital) / self.initial_capital
        ) * 100
        return {
            "전략": "Market Making",
            "총 거래수": total_trades,
            "최종 자본금": round(self.capital, 2),
            "수익률(%)": round(final_return, 2),
        }
