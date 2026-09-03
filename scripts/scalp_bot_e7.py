"""E7 independent short-term futures strategy.

Strategy: 1m Bollinger mean reversion with abnormal-move rejection.
Default is dry-run. This module does not import or share e3 state/ledger.
Live order integration is intentionally disabled until separately reviewed.
"""
from __future__ import annotations
from dataclasses import dataclass
from typing import Literal, Optional
import argparse, time

Side = Literal["LONG", "SHORT"]

@dataclass(frozen=True)
class E7Config:
    leverage: int = 3
    margin_fraction: float = 0.125
    max_hold_seconds: int = 15 * 60
    no_reaction_seconds: int = 3 * 60
    bb_period: int = 20
    bb_sigma: float = 2.0
    rsi_period: int = 14
    volume_period: int = 20
    atr_period: int = 14
    volume_spike_max: float = 3.0
    candle_range_max_mult: float = 2.0
    atr_percentile_max: float = 0.90
    stop_swing_bars: int = 5
    stop_atr_mult: float = 0.8
    stop_atr_max_mult: float = 2.0
    fee_roundtrip_pct: float = 0.04
    slippage_roundtrip_pct: float = 0.04

@dataclass(frozen=True)
class Signal:
    side: Side
    entry: float
    target: float
    stop: float
    reason: str

@dataclass(frozen=True)
class Candidate:
    side: Side
    signal_time: float
    band: float
    rsi: float
    atr: float

def _rsi(closes, p):
    if len(closes) <= p: return None
    d=[closes[i]-closes[i-1] for i in range(1,len(closes))]
    ag=sum(max(x,0) for x in d[:p])/p; al=sum(max(-x,0) for x in d[:p])/p
    for x in d[p:]:
        ag=(ag*(p-1)+max(x,0))/p; al=(al*(p-1)+max(-x,0))/p
    return 100.0 if al == 0 else 100-100/(1+ag/al)

def _atr(opens, highs, lows, closes, p):
    if len(closes) <= p: return None
    tr=[]
    for i in range(1,len(closes)):
        tr.append(max(highs[i]-lows[i], abs(highs[i]-closes[i-1]), abs(lows[i]-closes[i-1])))
    return sum(tr[-p:])/p

def _metrics(opens, highs, lows, closes, volumes, cfg):
    n=len(closes); p=cfg.bb_period
    if n < max(p, cfg.volume_period+1, cfg.atr_period+1, 60)+1: return None
    w=closes[-p:]; mid=sum(w)/p; sd=(sum((x-mid)**2 for x in w)/p)**0.5
    atr=_atr(opens,highs,lows,closes,cfg.atr_period)
    r=_rsi(closes,cfg.rsi_period)
    av=sum(volumes[-cfg.volume_period-1:-1])/cfg.volume_period
    ranges=[highs[i]-lows[i] for i in range(n-cfg.volume_period-1,n-1)]
    if atr is None or r is None or av <= 0 or not ranges: return None
    ah=[]
    for k in range(max(cfg.atr_period, 60), n+1):
        ah.append(_atr(opens[:k], highs[:k], lows[:k], closes[:k], cfg.atr_period))
    atr_pct=sum(x <= atr for x in ah if x is not None)/len(ah) if ah else 1.0
    return mid, mid+cfg.bb_sigma*sd, mid-cfg.bb_sigma*sd, r, atr, volumes[-1]/av, (highs[-1]-lows[-1])/(sum(ranges)/len(ranges)), atr_pct

def e7_candidate(opens, highs, lows, closes, volumes, signal_time, cfg=E7Config()):
    m=_metrics(opens,highs,lows,closes,volumes,cfg)
    if not m: return None
    mid, upper, lower, r, atr, vr, rr, atr_pct=m
    if vr >= cfg.volume_spike_max or rr >= cfg.candle_range_max_mult or atr_pct >= cfg.atr_percentile_max: return None
    if closes[-1] <= lower and r <= 40: return Candidate("LONG",signal_time,lower,r,atr)
    if closes[-1] >= upper and r >= 60: return Candidate("SHORT",signal_time,upper,r,atr)
    return None

def confirm_candidate(candidate, opens, highs, lows, closes, volumes, cfg=E7Config()):
    """Confirm on the next completed candle; return an executable signal."""
    if not candidate or len(closes) < 2: return None
    px=closes[-1]; prev=closes[-2]
    if candidate.side == "LONG" and not (closes[-1] > opens[-1] and px > prev): return None
    if candidate.side == "SHORT" and not (closes[-1] < opens[-1] and px < prev): return None
    target=(candidate.band + px)/2
    look=max(2, cfg.stop_swing_bars)
    if candidate.side == "LONG":
        structural=px-min(lows[-look:]) + candidate.atr*0.2
    else:
        structural=max(highs[-look:])-px + candidate.atr*0.2
    risk=max(structural, candidate.atr*cfg.stop_atr_mult)
    if risk > candidate.atr*cfg.stop_atr_max_mult: return None
    if candidate.side == "LONG":
        stop=max(px-risk, px*0.001); target=max(target,px*(1+0.0001))
    else:
        stop=px+risk; target=min(target,px*(1-0.0001))
    # Require a realistic gross target before any exchange order is considered.
    cost=(cfg.fee_roundtrip_pct+cfg.slippage_roundtrip_pct)/100
    if abs(target/px-1) <= cost*3: return None
    return Signal(candidate.side,px,target,stop,"bb_reversal_confirmed")

def e7_signal(opens, highs, lows, closes, volumes, cfg=E7Config()) -> Optional[Signal]:
    # Backward-compatible one-call helper: candidate on prior candle, confirmation now.
    if len(closes) < 2: return None
    c=e7_candidate(opens[:-1],highs[:-1],lows[:-1],closes[:-1],volumes[:-1],0,cfg)
    return confirm_candidate(c,opens,highs,lows,closes,volumes,cfg)

def position_size(balance: float, price: float, cfg=E7Config()):
    if balance <= 0 or price <= 0 or cfg.leverage <= 0 or not 0 < cfg.margin_fraction <= 0.25: return 0.0
    margin=min(balance*cfg.margin_fraction, balance*0.15)
    return margin*cfg.leverage/price

def main():
    ap=argparse.ArgumentParser(description="Independent E7 dry-run strategy")
    ap.add_argument('--dry-run', action='store_true', default=True)
    ap.add_argument('--balance', type=float, default=4.0)
    ap.add_argument('--leverage', type=int, default=3)
    args=ap.parse_args()
    print(f'E7 DRY-RUN ONLY | balance={args.balance:.4f} USDT | leverage={args.leverage}x | max_hold=900s')
    print('No exchange/order/state integration is enabled in this independent scaffold.')
if __name__ == '__main__': main()
