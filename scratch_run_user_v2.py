"""사용자 제공 스크립트(v2)를 실제 데이터(BTCUSDT, scratch_klines_v4.json)로 그대로 실행해
기본값에서 진입이 실제로 막히는지 검증."""
import json
import sys

import pandas as pd

sys.path.insert(0, "archive/scratch_scripts")
from user_scalping_backtest_v2 import ScalpingBacktestSimulator

raw = json.load(open("scratch_klines_v4.json", encoding="utf-8"))
rows = raw["BTCUSDT"]
df = pd.DataFrame(rows, columns=["ts", "open", "high", "low", "close", "vol", "close_ts", "qvol", "n", "taker_base", "taker_quote", "ignore"])
for col in ("open", "high", "low", "close"):
    df[col] = df[col].astype(float)
df.index = pd.to_datetime(df["ts"], unit="ms")

sim = ScalpingBacktestSimulator()  # 스크립트 기본값 그대로: leverage=3, slot_ratio=0.1, max_margin_ratio=0.08
df = sim.calculate_indicators(df)
result = sim.run_simulation(df)
print("=== 기본값 그대로(leverage=3, slot_ratio=0.1, max_margin_ratio=0.08) ===")
print(result)

print()
sim2 = ScalpingBacktestSimulator(leverage=3, slot_ratio=0.05, max_margin_ratio=0.08)  # slot_ratio를 max_margin_ratio 이하로 낮춰봄
result2 = sim2.run_simulation(df)
print("=== slot_ratio만 0.05로 낮춤(그 외 기본값) ===")
print(result2)
