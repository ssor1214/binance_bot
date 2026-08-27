from __future__ import annotations

from dataclasses import dataclass, field


def build_grid_levels(center_price: float, width_pct: float, grid_count: int) -> list[float]:
    if center_price <= 0:
        raise ValueError("center_price must be positive")
    if width_pct <= 0:
        raise ValueError("width_pct must be positive")
    if grid_count < 2:
        raise ValueError("grid_count must be at least 2")
    low = center_price * (1.0 - width_pct / 100.0)
    high = center_price * (1.0 + width_pct / 100.0)
    step = (high - low) / (grid_count - 1)
    return [low + step * i for i in range(grid_count)]


@dataclass
class GridState:
    levels: list[float]
    held_buy_rungs: set[int] = field(default_factory=set)

    def __post_init__(self) -> None:
        if len(self.levels) < 2:
            raise ValueError("grid needs at least 2 levels")

    @property
    def low(self) -> float:
        return self.levels[0]

    @property
    def high(self) -> float:
        return self.levels[-1]

    def grid_gap_pct(self) -> float:
        return ((self.levels[1] / self.levels[0]) - 1.0) * 100.0

    def in_range(self, price: float) -> bool:
        return self.low <= price <= self.high

    def eligible_buy_rungs(self, mark_price: float) -> list[int]:
        return [
            i for i in range(len(self.levels) - 1)
            if self.levels[i] < mark_price and i not in self.held_buy_rungs
        ]

    def eligible_sell_rungs(self, mark_price: float) -> list[int]:
        return [
            buy_rung + 1 for buy_rung in sorted(self.held_buy_rungs)
            if 0 <= buy_rung + 1 < len(self.levels)
        ]

    def register_buy_fill(self, buy_rung: int) -> int:
        if buy_rung < 0 or buy_rung >= len(self.levels) - 1:
            raise ValueError("buy rung out of range")
        self.held_buy_rungs.add(buy_rung)
        return buy_rung + 1

    def register_sell_fill(self, sell_rung: int) -> int:
        buy_rung = sell_rung - 1
        if buy_rung not in self.held_buy_rungs:
            raise ValueError("no inventory mapped to sell rung")
        self.held_buy_rungs.remove(buy_rung)
        return buy_rung
