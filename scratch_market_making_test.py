"""[2026-08-11 사용자요청] 마켓메이킹 전략 근사 시뮬레이션.
1분봉 종가만으로 원본 스크립트를 그대로 실행한다(원본 로직 자체가 이전 행 대비
close-to-close 가격변동만 보므로 1분봉으로도 그 로직 자체는 충실히 재현되지만, 1분
안에서 여러 번 스프레드를 오갈 수 있는 실제 틱 상황은 못 잡는다 — 신뢰도 낮음, 근사치임을
명시). 심볼별로 독립 실행 후 합산. 실 API 호출 없음."""
import json
import sys

import pandas as pd

sys.path.insert(0, "archive/scratch_scripts")
from user_market_making_v1 import MarketMakingSimulator

raw = json.load(open("scratch_klines_v4.json", encoding="utf-8"))

total_trades = 0
total_start = 0.0
total_end = 0.0
per_symbol = []

for symbol, rows in raw.items():
    df = pd.DataFrame(rows, columns=["ts", "open", "high", "low", "close", "vol", "close_ts", "qvol", "n", "taker_base", "taker_quote", "ignore"])
    df["price"] = df["close"].astype(float)
    df["timestamp"] = df["ts"]

    sim = MarketMakingSimulator(initial_capital=250.0)  # 40심볼 합계가 대략 10000이 되도록 분배
    metrics = sim.run_simulation(df[["timestamp", "price"]])
    per_symbol.append((symbol, metrics))
    total_trades += metrics["총 거래수"]
    total_start += 250.0
    total_end += sim.capital

print("\n=== 심볼별 상위/하위 5개 ===")
per_symbol.sort(key=lambda x: x[1]["수익률(%)"])
for symbol, m in per_symbol[:5]:
    print(f"  최하위: {symbol}: 거래{m['총 거래수']}건 수익률{m['수익률(%)']}%")
for symbol, m in per_symbol[-5:]:
    print(f"  최상위: {symbol}: 거래{m['총 거래수']}건 수익률{m['수익률(%)']}%")

print(f"\n=== 40심볼 합산 ===")
print(f"총 거래수={total_trades} 시작자본합계={total_start:.0f} 최종자본합계={total_end:.2f} "
      f"전체수익률={(total_end-total_start)/total_start*100:.2f}%")
