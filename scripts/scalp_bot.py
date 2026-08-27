"""1분봉 스캘핑 봇 (독립 실행) - 실주문을 낸다.

[설계 근거 / 2026-08-19]
  신호  : bot.strategy._direction_scores 와 동일한 펌프감지(1분봉 변동폭 + 거래량비)
          + 라이브 게이트(캔들방향 / 테이커확인 / 진입확률 / MTF정합)
  청산  : 라이브와 동일 규칙 - 손절 / 무장후 트레일링 / 익절 하드캡
  사이징: 슬롯 N개 x 비중 frac (B방식). 잔고가 작아 슬롯이 1개밖에 안 나오면
          --slots 1 --size 1.0 으로 A방식(전액 집중)이 된다. 코드는 동일하다.

[검증 상태 - 반드시 읽을 것]
  이 전략은 검증되지 않았다. 2026-08-19 검증에서
    - 85심볼 10일(신호 3만건) 전방수익: 1분봉 -0.0078% (우위 없음)
    - 8심볼 2일 초봉 + 라이브청산규칙: 일 +2.96% (표본 1,687건)
  두 결과가 상충하며, 대표본 초봉 검증은 수집 중단으로 미완이다.
  즉 실거래 근거가 확정되지 않은 상태이므로 소액/단시간 테스트 용도로만 쓸 것.

[안전장치]
  - --minutes 로 최대 실행시간을 강제한다 (무기한 실행 없음)
  - --min-balance 아래로 내려가면 신규진입을 멈춘다
  - --max-loss-pct 만큼 잃으면 그 즉시 종료한다
  - 종료 시 보유 포지션을 정리할지(--close-on-exit) 선택할 수 있다
  - 기존 라이브 봇이 실행 중이면 경고하고, --force 없이는 시작하지 않는다
    (같은 계정에 두 봇이 주문하면 서로의 포지션을 건드릴 수 있다)

[사용]
  python scripts/scalp_bot.py --minutes 20 --slots 1 --size 1.0 --dry-run
  python scripts/scalp_bot.py --minutes 20 --slots 1 --size 1.0        # 실주문
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bot.config import Config
from bot.exchange import Exchange
from bot.indicators import add_indicators
from bot.strategy import (
    generate_signal_with_probability,
    immediate_momentum_ok,
    mtf_trend_alignment,
    pnl_pct,
    volume_direction_ok,
)

LEDGER = Path(__file__).resolve().parent.parent / "logs" / "scalp_bot_ledger.jsonl"
FEE_ONE_WAY = 0.000284  # 실측 왕복 0.0568%의 편도


@dataclass
class Pos:
    symbol: str
    side: str
    entry_price: float
    quantity: float
    entered_at: float
    leverage: int
    peak_roe: float = 0.0
    armed: bool = False
    max_adverse_roe: float = 0.0
    max_favorable_roe: float = 0.0


def live_bot_running() -> list[str]:
    """기존 라이브 봇 프로세스를 찾는다. 같은 계정에 두 봇이 주문하면 충돌한다."""
    try:
        out = subprocess.run(
            ["wmic", "process", "where", "name='python.exe'",
             "get", "ProcessId,CommandLine", "/format:csv"],
            capture_output=True, text=True, timeout=20,
        ).stdout
    except Exception:
        return []
    found = []
    for line in out.splitlines():
        if "binance-futures-bot" not in line:
            continue
        if "run_forever" in line or "-m bot.main" in line:
            found.append(line.strip()[:120])
    return found


def append_ledger(rec: dict) -> None:
    LEDGER.parent.mkdir(parents=True, exist_ok=True)
    with LEDGER.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def main() -> int:
    p = argparse.ArgumentParser(description="1분봉 스캘핑 봇 (실주문)")
    p.add_argument("--minutes", type=float, required=True, help="최대 실행시간(분). 무기한 실행 방지")
    p.add_argument("--slots", type=int, default=1, help="동시보유 한도")
    p.add_argument("--size", type=float, default=1.0, help="포지션당 잔고 비중(1.0=전액)")
    p.add_argument("--leverage", type=int, default=4)
    p.add_argument("--pump-chg", type=float, default=0.6, help="1분봉 변동 문턱(%%)")
    p.add_argument("--symbols", type=int, default=85)
    p.add_argument("--poll", type=float, default=20.0, help="스캔 주기(초)")
    p.add_argument("--rest-min-interval", type=float, default=0.35, help="REST 최소간격(초). IP밴 방지")
    p.add_argument("--min-balance", type=float, default=1.5, help="이 아래면 신규진입 중단")
    p.add_argument("--max-loss-pct", type=float, default=25.0, help="시작잔고 대비 이만큼 잃으면 종료")
    p.add_argument("--close-on-exit", action="store_true", help="종료 시 보유 포지션 시장가 정리")
    p.add_argument("--dry-run", action="store_true", help="주문을 내지 않고 판단만 출력")
    p.add_argument("--force", action="store_true", help="라이브 봇이 떠 있어도 강행")
    args = p.parse_args()

    running = live_bot_running()
    if running and not args.force:
        print("[중단] 기존 라이브 봇이 실행 중입니다. 같은 계정에 두 봇이 주문하면 서로의")
        print("       포지션을 건드릴 수 있습니다. 라이브 봇을 먼저 정지하거나 --force 를 쓰십시오.")
        for r in running:
            print("   ", r)
        return 1
    if running:
        print(f"[경고] 라이브 봇 {len(running)}개가 떠 있는데 --force 로 강행합니다.")

    cfg = Config()
    cfg.pump_min_candle_chg_pct = args.pump_chg
    ex = Exchange(cfg)

    start_bal = ex.get_total_margin_balance()
    stop_bal = start_bal * (1 - args.max_loss_pct / 100)
    symbols = (ex.get_active_usdt_perpetual_symbols(limit=args.symbols)
               if cfg.auto_symbols else list(cfg.symbols)[: args.symbols])

    mode = "DRY-RUN(주문없음)" if args.dry_run else "실주문"
    print(f"[{mode}] 시작잔고 {start_bal:.4f} / 슬롯{args.slots} / 비중{args.size} / "
          f"{args.leverage}배 / 문턱{args.pump_chg}% / {len(symbols)}심볼")
    print(f"  종료조건: {args.minutes}분 경과 또는 잔고 {stop_bal:.4f} 이하")

    positions: dict[str, Pos] = {}
    deadline = time.time() + args.minutes * 60
    last_rest = 0.0
    n_sig = n_entry = n_exit = 0

    def klines(sym):
        nonlocal last_rest
        wait = args.rest_min_interval - (time.time() - last_rest)
        if wait > 0:
            time.sleep(wait)
        last_rest = time.time()
        return ex.get_klines(sym)

    def close(pos: Pos, reason: str) -> None:
        nonlocal n_exit
        mark = ex.get_mark_price(pos.symbol)
        roe = pnl_pct(pos.entry_price, mark, pos.side) * pos.leverage
        if not args.dry_run:
            try:
                ex.close_market_position(pos.symbol, pos.side, abs(pos.quantity))
            except Exception as e:
                print(f"  [청산실패] {pos.symbol} {e}")
                return
        nominal = pos.entry_price * pos.quantity
        pnl = nominal * (roe / 100 / pos.leverage) - nominal * FEE_ONE_WAY * 2
        append_ledger(dict(
            symbol=pos.symbol, side=pos.side, entry_price=pos.entry_price,
            exit_price=mark, quantity=pos.quantity, exit_reason=reason,
            entered_at=pos.entered_at, exited_at=time.time(),
            held_seconds=time.time() - pos.entered_at, leverage=pos.leverage,
            roe_pct=roe, nominal=nominal, est_net_pnl=pnl,
            max_adverse_roe=pos.max_adverse_roe, max_favorable_roe=pos.max_favorable_roe,
            origin="scalp_bot", dry_run=args.dry_run,
        ))
        positions.pop(pos.symbol, None)
        n_exit += 1
        print(f"  청산 {pos.symbol} {reason} ROE{roe:+.2f}% 추정손익{pnl:+.4f}")

    while time.time() < deadline:
        try:
            cycle = time.time()

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
                        print(f"  무장 {sym} ROE{roe:+.2f}%")
                    if pos.armed:
                        pos.peak_roe = max(pos.peak_roe, roe)
                        if roe <= pos.peak_roe - cfg.trail_drawdown_pct:
                            reason = "TRAIL"
                if reason:
                    close(pos, reason)

            bal = ex.get_total_margin_balance()
            if bal <= stop_bal:
                print(f"[종료] 손실한도 도달 (잔고 {bal:.4f} <= {stop_bal:.4f})")
                break

            if len(positions) < args.slots and bal >= args.min_balance:
                for sym in symbols:
                    if time.time() > deadline or len(positions) >= args.slots:
                        break
                    if sym in positions:
                        continue
                    try:
                        df = add_indicators(klines(sym), cfg)
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
                    need = (cfg.min_entry_probability if sig == "LONG"
                            else cfg.short_min_entry_probability)
                    if prob < need:
                        continue
                    agree, total = mtf_trend_alignment(ex, cfg, sym, sig)
                    if total == 0 or agree / total < cfg.mtf_min_agree_ratio:
                        continue

                    price = ex.get_mark_price(sym)
                    margin = bal * args.size
                    qty = ex.round_quantity(sym, margin * args.leverage / price,
                                            price=price, max_notional=margin * args.leverage * 1.05)
                    if not qty:
                        continue
                    if args.dry_run:
                        print(f"  [DRY] 진입 {sym} {sig} @{price} qty={qty}")
                        positions[sym] = Pos(sym, sig, price, qty, time.time(), args.leverage)
                        n_entry += 1
                        continue
                    try:
                        ex.set_margin_type(sym, "ISOLATED")
                        ex.set_leverage(sym, args.leverage)
                        ex.open_market_position(sym, sig, qty)
                    except Exception as e:
                        print(f"  [진입실패] {sym} {e}")
                        continue
                    live = ex.get_position(sym)
                    fill = live["entry_price"] if live else price
                    positions[sym] = Pos(sym, sig, fill, qty, time.time(), args.leverage)
                    n_entry += 1
                    print(f"  진입 {sym} {sig} @{fill} qty={qty} 명목{fill*qty:.2f}")

            slept = args.poll - (time.time() - cycle)
            if slept > 0:
                time.sleep(slept)
        except KeyboardInterrupt:
            print("\n[중단] 사용자 요청")
            break
        except Exception as e:
            print(f"  [주기오류] {type(e).__name__}: {e}")
            time.sleep(args.poll)

    if positions and args.close_on_exit:
        print("보유 포지션 정리 중...")
        for sym in list(positions):
            close(positions[sym], "SHUTDOWN")

    end_bal = ex.get_total_margin_balance()
    print(f"\n종료: 신호{n_sig} 진입{n_entry} 청산{n_exit} 미청산{len(positions)}")
    print(f"  잔고 {start_bal:.4f} -> {end_bal:.4f} ({(end_bal/start_bal-1)*100:+.2f}%)")
    if positions:
        print(f"  [주의] 미청산 포지션 {len(positions)}개: {', '.join(positions)}")
    print(f"  원장: {LEDGER}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
