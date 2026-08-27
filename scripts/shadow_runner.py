"""섀도우(Paper) 러너 - 주문을 내지 않고 "이 설정이면 무엇을 했을지"만 기록한다.

[주의] 라이브 봇과 동시 실행 시 REST 경합이 생긴다. 스로틀 기본 0.6초.

[목적] 2026-08-19 검증에서 봉 단위 백테스트가 트레일링 청산을 낙관적으로 모델링해
       (무장과 청산이 같은 봉에서 처리) 부호까지 뒤집히는 문제가 반복 확인됐다.
       초 단위로 내려가도 잔여 편향이 남아, 실제 시장/실제 폴링 주기로만 확정 가능하다.
       이 러너는 라이브와 같은 데이터·같은 주기·같은 판단 함수를 쓰되 주문만 내지 않는다.

[안전]
  - 주문 API를 일절 호출하지 않는다 (futures_create_order 등 미사용)
  - REST 호출은 심볼당 최소 간격(--rest-min-interval)을 강제한다. 2026-08-11 IP밴 이력 때문.
  - 라이브 봇과 별도 원장(logs/shadow_ledger.jsonl)에 기록한다

[사용]
  python scripts/shadow_runner.py --minutes 30 --slots 20 --size 0.05 --pump-chg 0.6
"""
from __future__ import annotations
import argparse, json, os, sys, time
from dataclasses import dataclass, asdict, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bot.config import Config
from bot.exchange import Exchange
from bot.indicators import add_indicators
from bot.strategy import (
    generate_signal_with_probability, immediate_momentum_ok,
    volume_direction_ok, mtf_trend_alignment, pnl_pct,
)

LEDGER = Path(__file__).resolve().parent.parent / "logs" / "shadow_ledger.jsonl"


@dataclass
class VPos:
    symbol: str
    side: str
    entry_price: float
    entered_at: float
    leverage: float
    peak_roe: float = 0.0
    armed: bool = False
    max_adverse_roe: float = 0.0
    max_favorable_roe: float = 0.0


def log_exit(pos: VPos, exit_price: float, reason: str, cfg: Config, size_frac: float, bal: float):
    roe = pnl_pct(pos.entry_price, exit_price, pos.side) * pos.leverage
    nominal = bal * size_frac * pos.leverage
    fee = nominal * 0.000284 * 2
    pnl = nominal * (roe / 100 / pos.leverage) - fee
    rec = dict(
        symbol=pos.symbol, side=pos.side, entry_price=pos.entry_price,
        exit_price=exit_price, exit_reason=reason,
        entered_at=pos.entered_at, exited_at=time.time(),
        held_seconds=time.time() - pos.entered_at, leverage=pos.leverage,
        roe_pct=roe, nominal=nominal, fee=fee, net_pnl=pnl,
        max_adverse_roe=pos.max_adverse_roe, max_favorable_roe=pos.max_favorable_roe,
        origin="shadow",
    )
    with LEDGER.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    return pnl


def main():
    p = argparse.ArgumentParser(description="Shadow (paper) runner — 주문 없음")
    p.add_argument("--minutes", type=float, default=30, help="실행 시간(분)")
    p.add_argument("--slots", type=int, default=20)
    p.add_argument("--size", type=float, default=0.05, help="포지션당 잔고 비중")
    p.add_argument("--pump-chg", type=float, default=None, help="변동 문턱(미지정=.env값)")
    p.add_argument("--balance", type=float, default=100.0, help="가상 시작 잔고")
    p.add_argument("--symbols", type=int, default=85)
    p.add_argument("--poll", type=float, default=30.0, help="스캔 주기(초)")
    p.add_argument("--rest-min-interval", type=float, default=0.6,
                   help="REST 호출 간 최소 간격(초). IP밴 방지")
    args = p.parse_args()

    cfg = Config()
    if args.pump_chg is not None:
        cfg.pump_min_candle_chg_pct = args.pump_chg
    ex = Exchange(cfg)

    if cfg.auto_symbols:
        symbols = ex.get_active_usdt_perpetual_symbols(limit=args.symbols)
    else:
        symbols = list(cfg.symbols)[: args.symbols]
    print(f"섀도우 시작: {len(symbols)}심볼 / 슬롯{args.slots} / 비중{args.size} / "
          f"문턱{cfg.pump_min_candle_chg_pct} / 주문 없음", flush=True)

    bal = args.balance
    positions: dict[str, VPos] = {}
    deadline = time.time() + args.minutes * 60
    last_rest = 0.0
    n_sig = n_entry = n_exit = 0

    def throttled_klines(sym, **kw):
        nonlocal last_rest
        wait = args.rest_min_interval - (time.time() - last_rest)
        if wait > 0:
            time.sleep(wait)
        last_rest = time.time()
        return ex.get_klines(sym, **kw)

    while time.time() < deadline:
        cycle = time.time()
        # 1) 보유 포지션 청산 판정
        for sym in list(positions):
            pos = positions[sym]
            try:
                mark = ex.get_mark_price(sym)
            except Exception:
                continue
            roe = pnl_pct(pos.entry_price, mark, pos.side) * pos.leverage
            pos.max_adverse_roe = min(pos.max_adverse_roe, roe)
            pos.max_favorable_roe = max(pos.max_favorable_roe, roe)
            arm = cfg.take_profit_min if pos.side == "LONG" else cfg.short_take_profit_min
            reason = None
            if roe <= -cfg.stop_loss_pct:
                reason = "STOP_LOSS"
            elif roe >= cfg.take_profit_hard_cap:
                reason = "HARD_CAP"
            else:
                if not pos.armed and roe >= arm:
                    pos.armed = True
                if pos.armed:
                    pos.peak_roe = max(pos.peak_roe, roe)
                    if roe <= pos.peak_roe - cfg.trail_drawdown_pct:
                        reason = "TRAIL"
            if reason:
                bal += log_exit(pos, mark, reason, cfg, args.size, bal)
                del positions[sym]
                n_exit += 1
                print(f"  청산 {sym} {reason} ROE{roe:+.2f}% 잔고{bal:.2f}", flush=True)

        # 2) 신규 진입 판정
        if len(positions) < args.slots:
            for sym in symbols:
                if time.time() > deadline or len(positions) >= args.slots:
                    break
                if sym in positions:
                    continue
                try:
                    df = add_indicators(throttled_klines(sym), cfg)
                except Exception:
                    continue
                sig, prob = generate_signal_with_probability(df, cfg)
                if not sig:
                    continue
                n_sig += 1
                if not immediate_momentum_ok(df, sig):
                    continue
                if not volume_direction_ok(df, sig, cfg):
                    continue
                need = cfg.min_entry_probability if sig == "LONG" else cfg.short_min_entry_probability
                if prob < need:
                    continue
                agree, total = mtf_trend_alignment(ex, cfg, sym, sig)
                if total == 0 or agree / total < cfg.mtf_min_agree_ratio:
                    continue
                price = float(df["close"].iloc[-1])
                positions[sym] = VPos(sym, sig, price, time.time(), float(cfg.leverage_min))
                n_entry += 1
                print(f"  진입 {sym} {sig} @{price} (신호{n_sig} 진입{n_entry})", flush=True)

        slept = args.poll - (time.time() - cycle)
        if slept > 0:
            time.sleep(slept)

    print(f"\n종료: 신호{n_sig} 진입{n_entry} 청산{n_exit} 미청산{len(positions)} 잔고{bal:.2f} "
          f"({(bal/args.balance-1)*100:+.2f}%)", flush=True)
    print(f"원장: {LEDGER}", flush=True)


if __name__ == "__main__":
    main()
