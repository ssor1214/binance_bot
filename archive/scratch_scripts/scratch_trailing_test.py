import json, io, sys
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

with open('scratch_klines_v4.json') as f:
    klines_data = json.load(f)

LEVERAGE = 4
TP_HARD_CAP = 20.0
STOP_LOSS_PCT = 3.0
TP_MIN = 4.0
TRAIL_PCT = 1.5
CHG_THRESH = 0.8
VOL_RATIO_THRESH = 2.3
MAX_HOLD_CANDLES = 240

def simulate_polling(candles, entry_idx, side):
    """현재 방식 근사: 캔들 '종가'만 매분 확인(30초 폴링을 1분봉 단위로 근사) — 캔들 내
    고점/저점을 실시간으로 못 보고, 다음 종가에서야 트레일 조건을 확인한다."""
    entry_price = float(candles[entry_idx][4])
    peak_roe = -999.0
    armed = False
    for j in range(entry_idx + 1, min(entry_idx + 1 + MAX_HOLD_CANDLES, len(candles))):
        close = float(candles[j][4])
        if side == 'LONG':
            roe = (close - entry_price) / entry_price * 100 * LEVERAGE
        else:
            roe = (entry_price - close) / entry_price * 100 * LEVERAGE
        if roe >= TP_HARD_CAP:
            return TP_HARD_CAP
        if roe <= -STOP_LOSS_PCT:
            return -STOP_LOSS_PCT
        if not armed and roe >= TP_MIN:
            armed = True
            peak_roe = roe
        elif armed:
            if roe > peak_roe:
                peak_roe = roe
            if peak_roe - roe >= TRAIL_PCT:
                return roe  # 실제로 확인한 시점의(이미 밀린) ROE로 청산 — 폴링 지연 반영
    last_close = float(candles[min(entry_idx + MAX_HOLD_CANDLES, len(candles)-1)][4])
    if side == 'LONG':
        return (last_close - entry_price) / entry_price * 100 * LEVERAGE
    else:
        return (entry_price - last_close) / entry_price * 100 * LEVERAGE

def simulate_native(candles, entry_idx, side):
    """거래소 네이티브 트레일링 가정: 캔들 내 고가/저가까지 실시간으로 보고, 고점 대비
    정확히 TRAIL_PCT 빠지는 순간 체결된다고 가정(이상적 케이스)."""
    entry_price = float(candles[entry_idx][4])
    peak_roe = -999.0
    armed = False
    for j in range(entry_idx + 1, min(entry_idx + 1 + MAX_HOLD_CANDLES, len(candles))):
        high = float(candles[j][2]); low = float(candles[j][3])
        if side == 'LONG':
            roe_high = (high - entry_price) / entry_price * 100 * LEVERAGE
            roe_low = (low - entry_price) / entry_price * 100 * LEVERAGE
        else:
            roe_high = (entry_price - low) / entry_price * 100 * LEVERAGE
            roe_low = (entry_price - high) / entry_price * 100 * LEVERAGE
        if roe_high >= TP_HARD_CAP:
            return TP_HARD_CAP
        if roe_low <= -STOP_LOSS_PCT:
            return -STOP_LOSS_PCT
        candle_favorable_roe = roe_high
        if not armed and candle_favorable_roe >= TP_MIN:
            armed = True
            peak_roe = candle_favorable_roe
        elif armed:
            if candle_favorable_roe > peak_roe:
                peak_roe = candle_favorable_roe
            if peak_roe - roe_low >= TRAIL_PCT:
                return max(peak_roe - TRAIL_PCT, TP_MIN)  # 정확히 트레일 지점에서 체결
    last_close = float(candles[min(entry_idx + MAX_HOLD_CANDLES, len(candles)-1)][4])
    if side == 'LONG':
        return (last_close - entry_price) / entry_price * 100 * LEVERAGE
    else:
        return (entry_price - last_close) / entry_price * 100 * LEVERAGE

def get_signal(candles, volumes, i):
    o = float(candles[i][1]); c = float(candles[i][4])
    chg_pct = (c - o) / o * 100 if o else 0
    avg_vol20 = sum(volumes[i-20:i]) / 20
    vol_ratio = volumes[i] / avg_vol20 if avg_vol20 > 0 else 0
    if abs(chg_pct) < CHG_THRESH or vol_ratio < VOL_RATIO_THRESH:
        return None
    return 'LONG' if chg_pct > 0 else 'SHORT'

results_polling = []
results_native = []
for sym, candles in klines_data.items():
    volumes = [float(c[5]) for c in candles]
    for i in range(20, len(candles) - 1):
        sig = get_signal(candles, volumes, i)
        if sig:
            results_polling.append(simulate_polling(candles, i, sig))
            results_native.append(simulate_native(candles, i, sig))

for label, rs in [("폴링방식(현재, 30초~1분 지연)", results_polling), ("거래소 네이티브 트레일링(제안)", results_native)]:
    n = len(rs)
    wins = sum(1 for r in rs if r > 0)
    net = sum(rs)
    avg = net/n if n else 0
    print(f"{label}: 신호={n}건 승률={wins/n*100:.1f}% 건당평균={avg:+.2f}%p 합계={net:+.1f}%p")

diff_net = sum(results_native) - sum(results_polling)
print(f"\n차이(합계ROE): {diff_net:+.1f}%p ({len(results_polling)}건 기준, 건당 평균 개선 {diff_net/len(results_polling):+.3f}%p)")
