"""스캘핑 봇 e1 (임시 실험 버전) - 실주문.

[e1에서 보완한 것 / 2026-08-19]
  (1) 손익을 추정하지 않고 거래소 실제 체결 기록(futures_account_trades)에서
      commission / realizedPnl 을 읽어 원장에 남긴다.
      - e0에서는 편도 수수료 0.0284% 가정으로 -0.0582를 추정했는데
        실제 잔고 감소는 -0.1198로 2배였다. 추정치를 신뢰할 수 없음이 확인됨.
  (2) 텔레그램 브리핑: 신호 발생 / 탈락 사유 / 진입 체결 / 청산 결과를 계속 보고한다.
  (3) 잔고 전액 사용 시 -2019(Margin insufficient)가 나므로 안전계수를 둔다.

[검증 상태 - 반드시 읽을 것]
  이 전략은 검증되지 않았다. 2026-08-19 30심볼 10일 초단위 검증에서
    문턱 0.5 -> 일 -11.60% / 0.6 -> -4.99% / 0.9 -> +10.68%(낙폭 41.5%)
  기본값 0.6은 대표본에서 마이너스다. 실험/관측 목적으로만 쓸 것.

[사용]
  python scripts/scalp_bot_e1.py --minutes 20 --slots 1 --size 0.95 --dry-run
  python scripts/scalp_bot_e1.py --minutes 20 --slots 1 --size 0.95
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bot.config import Config
from bot.exchange import Exchange
from bot.ws_client import FileBackedKlineCache
from bot.indicators import add_indicators
from bot.strategy import (
    generate_signal_with_probability,
    immediate_momentum_ok,
    mtf_trend_alignment,
    pnl_pct,
    volume_direction_ok,
)
VERSION = "e1"
LEDGER = Path(__file__).resolve().parent.parent / "logs" / f"scalp_bot_{VERSION}_ledger.jsonl"


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
    stop_algo_id: int = 0
    trail_algo_id: int = 0
    max_adverse_roe: float = 0.0
    max_favorable_roe: float = 0.0
    entry_commission: float = 0.0
    intended_entry: float = 0.0   # 신호 판단에 쓴 WS 캔들 종가(슬리피지 측정용)


def live_bot_running() -> list[str]:
    try:
        out = subprocess.run(
            ["wmic", "process", "where", "name='python.exe'",
             "get", "ProcessId,CommandLine", "/format:csv"],
            capture_output=True, text=True, timeout=20,
        ).stdout
    except Exception:
        return []
    return [l.strip()[:120] for l in out.splitlines()
            if "binance-futures-bot" in l and ("run_forever" in l or "-m bot.main" in l)]


class Tg:
    """전송 전용 경량 텔레그램 클라이언트.

    bot.telegram_notifier.TelegramNotifier 는 Exchange/PositionManager 를 요구하고
    라이브 봇의 상태(일시정지 플래그, 튜닝 승인 등)를 함께 관리한다. e1은 자체
    포지션 관리를 하므로 그 의존을 끌어오지 않고 전송만 한다.
    """

    def __init__(self, cfg: Config):
        self.token = cfg.telegram_bot_token
        self.chat = cfg.telegram_chat_id
        self.enabled = bool(self.token and self.chat)

    def send(self, text: str) -> None:
        if not self.enabled:
            return
        import urllib.parse
        import urllib.request
        data = urllib.parse.urlencode({"chat_id": self.chat, "text": text}).encode()
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{self.token}/sendMessage", data=data)
        try:
            urllib.request.urlopen(req, timeout=10).read()
        except Exception:
            pass


def append_ledger(rec: dict) -> None:
    LEDGER.parent.mkdir(parents=True, exist_ok=True)
    with LEDGER.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def real_trades(ex: Exchange, symbol: str, since_ms: int) -> list[dict]:
    """거래소 실제 체결 내역. 추정 대신 이걸로 수수료/실현손익을 집계한다."""
    try:
        return ex.client.futures_account_trades(symbol=symbol, startTime=since_ms)
    except Exception:
        return []


def exit_facts(trades: list[dict], pos_side: str, nominal: float, lev: int) -> dict:
    """체결 기록에서 실제 청산가와 ROE를 뽑는다.

    [2026-08-20] 기존엔 청산 시점의 get_mark_price()로 ROE를 계산했다. 거래소측
    STOP_MARKET이 발동한 경우 봇이 다음 폴링에서야 발견하는데, 그 사이 가격이
    되돌아오면 실제 체결가와 크게 어긋난다(실측: 실현손익은 -6.5% 수준인데
    기록된 ROE는 -1.07%). 실현손익 기반으로 계산해 이 오차를 없앤다.
    """
    close_side = "SELL" if pos_side == "LONG" else "BUY"
    qty = 0.0
    notional = 0.0
    for t in trades:
        if t.get("side") != close_side:
            continue
        q = abs(float(t.get("qty", 0) or 0))
        p = float(t.get("price", 0) or 0)
        qty += q
        notional += q * p
    realized = sum(float(t.get("realizedPnl", 0) or 0) for t in trades)
    margin = (nominal / lev) if lev else 0.0
    return {
        "exit_price": (notional / qty) if qty else 0.0,
        "roe_pct": (realized / margin * 100) if margin else 0.0,
        "realized": realized,
        "commission": sum(float(t.get("commission", 0) or 0) for t in trades),
    }


def auto_size(bal: float, lev: int, max_expo: float, min_notional: float,
              safety: float, fixed_slots: int, fixed_size: float,
              max_concurrency: int = 8):
    """잔고에 맞춰 (슬롯수, 포지션당 비중)을 정한다.

    거래소 최소 주문 명목(기본 5.0 USDT) 때문에, 잔고가 작으면 슬롯을 늘릴수록
    각 포지션이 최소명목에 미달해 아예 진입하지 못한다. 그래서 잔고가 감당할 수
    있는 슬롯 수를 매 주기 다시 계산한다. 잔고 1 USDT 에서도 슬롯 1개는 열린다.

    [2026-08-20 수정] 잔고 한계만 보면 잔고 100에서 슬롯 84개가 나와 비중이 1/84로
    쪼개졌다(포지션당 명목 5.65, 자본활용률 1%). 실측 동시보유 분포는 3개 이하가
    99.3%이고 85심볼 환산 p99가 8개라, 그 이상은 만들어도 비어 있다.
    잔고 한계와 실제 필요치 중 작은 쪽을 쓴다.
    """
    need_margin = min_notional / lev * safety      # 포지션 1개에 필요한 최소 증거금
    if fixed_slots > 0:
        slots = fixed_slots
    else:
        slots = max(1, min(int(bal * max_expo // need_margin), max_concurrency))
    size = fixed_size if fixed_size > 0 else (max_expo / slots)
    # 계산된 비중으로 최소명목을 못 넘기면 슬롯을 줄여가며 맞춘다
    while slots > 1 and bal * size * lev < min_notional:
        slots -= 1
        size = fixed_size if fixed_size > 0 else (max_expo / slots)
    return slots, size


LOG_DIR = Path(__file__).resolve().parent.parent / "logs"


def ws_health() -> dict:
    """WS 워커 상태 스냅샷. 판정 불가면 빈 dict.

    [2026-08-20 실측 정상값] 하트비트 0.1~1.8초, 분당 메시지 3,488~3,729건,
    err60s=0, consecutive_read_loop_errors=0 (40심볼 1분봉 구독 기준).
    """
    out = {}
    hb = LOG_DIR / "ws_worker_heartbeat.txt"
    if hb.exists():
        try:
            out["hb_age"] = time.time() - float(hb.read_text(encoding="utf-8").strip())
        except Exception:
            pass
    for fp in (LOG_DIR / "ws_worker_status.json", LOG_DIR / "ws_worker_cache.json"):
        if not fp.exists():
            continue
        try:
            d = json.loads(fp.read_text(encoding="utf-8"))
        except Exception:
            continue
        h = d.get("health")
        if h:
            out.update({k: h.get(k) for k in
                        ("message_count_60s", "error_count_60s",
                         "consecutive_read_loop_errors", "last_market_message_ts")})
        lu = d.get("last_update_ts")
        if lu:
            out["symbols"] = len(lu)
            out["stalled"] = sum(1 for v in lu.values() if time.time() - v > 180)
    return out


def ws_is_frozen(h: dict) -> str:
    """§10의 python-binance read-loop freeze 징후를 판정한다.

    freeze의 특징은 "하트비트는 살아있는데 메시지가 끊기는 것"이라, 하트비트만
    봐서는 못 잡는다. 메시지 유입과 심볼 정체를 함께 본다. 정상 판단 근거가
    없으면(필드 부재) 빈 문자열 - 판단불가를 재시작 사유로 쓰지 않는다.
    """
    if h.get("hb_age") is not None and h["hb_age"] > 60:
        return f"하트비트 정체 {h['hb_age']:.0f}초"
    if h.get("consecutive_read_loop_errors") and h["consecutive_read_loop_errors"] >= 3:
        return f"read-loop 연속오류 {h['consecutive_read_loop_errors']}회"
    mc = h.get("message_count_60s")
    if mc is not None and mc == 0:
        return "60초간 메시지 0건"
    if h.get("symbols") and h.get("stalled") is not None:
        if h["stalled"] >= max(2, h["symbols"] // 2):
            return f"심볼 {h['stalled']}/{h['symbols']} 180초 정체"
    return ""


def start_ws(symbols: list) -> tuple:
    """bot/ws_worker.py 를 별도 프로세스로 띄우고 FileBackedKlineCache 를 돌려준다.

    라이브 봇(bot/main.py)의 _spawn_ws_worker / _ws_worker_paths 규칙을 그대로 따른다.
    워커가 캐시 파일을 쓰고 이 프로세스는 읽기만 한다(쓰기는 워커 전담).
    """
    env = dict(os.environ)
    env["WS_WORKER_ROLE"] = "market"
    env["WS_SHARD_INDEX"] = "0"
    env["WS_SHARD_COUNT"] = "1"
    env["WS_WORKER_SYMBOLS"] = json.dumps(list(symbols), ensure_ascii=False)
    # [2026-08-20 실측 진단] 라이브 기본값으로는 WS 캐시가 절대 채택되지 않는다.
    #  (1) seed가 limit=min(history_len,99)=99를 요청하는데 get_klines가 미완성봉 1개를
    #      버려 98개만 들어간다. has_sufficient_history(sym, 99)가 1개 차이로 영원히 False.
    #  (2) 1분봉은 확정 캔들이 60초에 한 번 오는데 staleness 기준이 20초라 대부분의
    #      시간 동안 is_fresh가 False.
    # 두 값 모두 올려야 하며 하나만 고치면 다른 쪽에서 막힌다. 잔고와 무관한 배관 설정이다.
    env["WS_KLINE_HISTORY_LEN"] = "150"
    env["WS_KLINE_MAX_STALENESS_SEC"] = "90"
    proc = subprocess.Popen(
        [sys.executable, "-m", "bot.ws_worker"],
        cwd=str(Path(__file__).resolve().parent.parent),
        env=env,
    )
    cache_path = LOG_DIR / "ws_worker_cache.json"
    hb_path = LOG_DIR / "ws_worker_heartbeat.txt"
    status_path = LOG_DIR / "ws_worker_status.json"
    return proc, FileBackedKlineCache(cache_path, hb_path, status_path=status_path)


def main() -> int:
    p = argparse.ArgumentParser(description=f"스캘핑 봇 {VERSION} (실주문)")
    p.add_argument("--minutes", type=float, required=True,
                   help="최대 실행시간(분). 0 = 무기한(Ctrl+C로 종료)")
    p.add_argument("--slots", type=int, default=0,
                   help="동시보유 한도. 0 = 잔고에 맞춰 자동 계산")
    p.add_argument("--max-exposure", type=float, default=0.95,
                   help="총 노출 상한(잔고 대비). 자동 사이징에서 사용")
    p.add_argument("--max-concurrency", type=int, default=8,
                   help="실제 필요 동시보유 상한. 잔고가 커도 이 이상 슬롯을 만들지 않는다. "
                        "실측 동시보유 분포(30심볼): 0개 50.5%%, 1개 34.5%%, 2개 11.9%%, "
                        "3개 2.5%%, 최대 10개. 85심볼 환산 p99 = 8개.")
    p.add_argument("--min-notional", type=float, default=5.0,
                   help="거래소 최소 주문 명목")
    p.add_argument("--notional-safety", type=float, default=1.12,
                   help="최소명목 대비 여유배수. 가격변동/반올림으로 미달되는 것을 막는다")
    p.add_argument("--size", type=float, default=0.0,
                   help="포지션당 잔고 비중. 0 = 자동(총노출/슬롯수)")
    p.add_argument("--leverage", type=int, default=4)
    p.add_argument("--pump-chg", type=float, default=0.6)
    p.add_argument("--min-exhaust", type=float, default=0.7,
                   help="신호봉 소진도 하한. 종가가 진행 방향 끝에 얼마나 붙었나. "
                        "롱=(종가-저가)/(고가-저가), 숏=1-그값. 0=제한없음. "
                        "[2026-08-20 측정, 85심볼10일 10,249건] 거래당 기대값: "
                        "전체 +0.0304%%, >=0.6 +0.0458%%, >=0.7 +0.0644%%(총액최고), "
                        ">=0.8 +0.0808%%(거래당최고), >=0.9 +0.0638%%. "
                        "가설(소진되면 되돌린다)과 반대로 끝까지 밀어붙인 캔들이 이어진다.")
    p.add_argument("--max-entry-slip", type=float, default=0.5,
                   help="주문 직전 실제 호가가 신호봉 종가에서 이만큼(%%) 이상 불리하게 "
                        "벌어져 있으면 진입하지 않는다. 0=제한없음. "
                        "[2026-08-20 실측] 진입 슬리피지 중앙 +1.83%%(ROE 5배 환산 +9.2%%p)로 "
                        "손절선(가격 1.2%%)보다 컸다. 급변동 직후 호가가 계속 밀린 결과.")
    p.add_argument("--short-arm", type=float, default=None,
                   help="숏 무장선(ROE %%). 미지정이면 .env의 SHORT_TAKE_PROFIT_MIN(4.0). "
                        "[2026-08-20 측정] 손절6.0 기준 총액: 4.0 +554.5, 3.8 +571.0(최적), "
                        "3.5 +528.9, 3.2 +487.1, 3.0 +456.0")
    p.add_argument("--pump-vol", type=float, default=None,
                   help="거래량 배수(20봉 평균 대비). 미지정이면 .env의 PUMP_MIN_VOLUME_RATIO. "
                        "[2026-08-20 측정] 문턱1.3/지연2초에서 총액: 1.0 +485, 1.1 +532, "
                        "1.2 +509, 1.4 +554(최고), 1.6 +477, 2.0 +421.")
    p.add_argument("--no-taker", action="store_true",
                   help="테이커 쏠림 게이트를 끈다. [2026-08-20 측정] 모든 거래량 배수에서 "
                        "끈 쪽이 총액이 높았다(1.4 기준 +554.5 vs +507.2). 신호를 6~7%% "
                        "줄이면서 품질을 올리지도 못한다.")
    p.add_argument("--symbols", type=int, default=85)
    p.add_argument("--poll", type=float, default=20.0)
    p.add_argument("--rest-min-interval", type=float, default=0.35)
    p.add_argument("--min-balance", type=float, default=1.5)
    p.add_argument("--max-loss-pct", type=float, default=25.0,
                   help="시작 총잔고 대비 이만큼 잃으면 종료. 0 = 해제")
    p.add_argument("--close-on-exit", action="store_true")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--force", action="store_true")
    p.add_argument("--no-telegram", action="store_true")
    p.add_argument("--max-signal-age", type=float, default=5.0,
                   help="신호봉 확정 후 이 초를 넘으면 진입하지 않는다. 0=제한없음. "
                        "[2026-08-20 측정] 진입 지연이 이 전략의 전부다. 30심볼 10일 초단위 "
                        "검증에서 거래당 기대값: 0초 +0.0750%%, 5초 -0.0079%%, 10초 -0.0724%%, "
                        "30초 -0.0997%%. 5초만 늦어도 우위가 사라진다. "
                        "실측 진입지연이 평균 24초여서 실거래가 -0.78%%였다.")
    p.add_argument("--bar-align", action="store_true", default=True,
                   help="매 분 00초 직후에 스캔을 시작하도록 정렬한다(기본 켬).")
    p.add_argument("--no-bar-align", dest="bar_align", action="store_false")
    p.add_argument("--no-exchange-trail", action="store_true",
                   help="거래소측 TRAILING_STOP_MARKET을 걸지 않는다. "
                        "실측 사고(2026-08-20): BTWUSDT가 +13%% ROE까지 갔다가 폴링이 그 구간을 "
                        "못 봐서 트레일(+11.7%%) 대신 -6%% 손절까지 되돌아왔다.")
    p.add_argument("--no-exchange-stop", action="store_true",
                   help="거래소측 STOP_MARKET을 걸지 않는다(폴링 손절만 사용). "
                        "2026-08-20 실측: 폴링만 쓰면 손절 -6.0%% 설정에 -11.40%%까지 밀렸다.")
    p.add_argument("--no-mtf", action="store_true",
                   help="MTF 정합 게이트를 끈다. 검증(sec_live.py)에는 MTF가 없었으므로 "
                        "거래당 +0.0728%%는 MTF 없이 나온 값이다. 2026-08-20 09:25 실측: "
                        "신호 6건 전부 MTF 0/2로 탈락(상위시간대가 하락추세인 국면).")
    p.add_argument("--ws", action="store_true",
                   help="WS 캔들 워커를 띄워 REST 순회 지연을 없앤다. "
                        "REST 순회는 40심볼x0.15초=6초라 1분봉 신호를 놓친다(실측 확인).")
    p.add_argument("--brief-every", type=float, default=300.0,
                   help="주기 브리핑 간격(초). 0이면 끔. --brief-on-clock 이면 무시된다.")
    p.add_argument("--symbol-refresh-min", type=float, default=60.0,
                   help="거래대금 상위 심볼 목록 갱신 주기(분). 0=갱신 안 함. "
                        "e1은 기존에 시작 시 한 번만 읽어, 무기한 실행 시 목록이 낡았다. "
                        "목록이 바뀌면 WS 워커도 새 목록으로 재구독한다.")
    p.add_argument("--brief-on-clock", action="store_true",
                   help="간격 대신 매시 정각/30분에만 브리핑한다(알림 빈도 축소).")
    args = p.parse_args()

    running = live_bot_running()
    if running and not args.force:
        print("[중단] 기존 라이브 봇 실행 중. --force 로 강행하거나 먼저 정지하십시오.")
        for r in running:
            print("   ", r)
        return 1

    cfg = Config()
    cfg.pump_min_candle_chg_pct = args.pump_chg
    if args.pump_vol is not None:
        cfg.pump_min_volume_ratio = args.pump_vol
    if args.short_arm is not None:
        cfg.short_take_profit_min = args.short_arm
    # 이 프로세스(읽기 측)의 신선도 기준도 같이 올린다 - 위 start_ws 주석 참고.
    cfg.ws_kline_max_staleness_sec = 90.0
    ex = Exchange(cfg)
    tg = None if args.no_telegram else Tg(cfg)

    def say(msg: str, tg_send: bool = True) -> None:
        """tg_send=False 면 콘솔에만 남긴다(탈락 사유 등 저빈도 관심 항목)."""
        print(msg, flush=True)
        if tg and tg_send:
            try:
                tg.send(f"[{VERSION}] {msg}")
            except Exception:
                pass

    start_bal = ex.get_total_margin_balance()
    stop_bal = (start_bal * (1 - args.max_loss_pct / 100)
                if args.max_loss_pct > 0 else 0.0)
    symbols = (ex.get_active_usdt_perpetual_symbols(limit=args.symbols)
               if cfg.auto_symbols else list(cfg.symbols)[: args.symbols])

    positions: dict[str, Pos] = {}

    # [2026-08-20] 재시작 시 (WS 대기 100초 전에 실행 - 그동안 포지션이 무방비였다) 거래소에 이미 있는 포지션을 채택한다.
    # 이전에는 빈 dict로 시작해 기존 포지션이 완전히 방치됐다 - 봇이 모르니
    # 익절/손절을 안 하고, 재시작 때 고아 algo 주문을 취소하므로 거래소측
    # 보호막도 사라져 무방비 상태가 됐다(실측: SKYAIUSDT/PRLUSDT 2건).
    if not args.dry_run:
        try:
            # futures_position_information 응답에는 leverage 필드가 없다(실측).
            # futures_account()의 positions 가 leverage/isolated 를 함께 준다.
            live_all = [x for x in ex.client.futures_account()["positions"]
                        if float(x.get("positionAmt", 0) or 0) != 0]
        except Exception as e:
            live_all = []
            say(f"경고 기존 포지션 조회 실패({e})")
        for lp in live_all:
            sym0 = lp["symbol"]
            amt = float(lp["positionAmt"])
            side0 = "LONG" if amt > 0 else "SHORT"
            ep = float(lp["entryPrice"])
            qty0 = abs(amt)
            if ep <= 0 or qty0 <= 0:
                continue
            # [2026-08-20] 채택 시 args.leverage(5)를 그대로 넣으면, 그 포지션이
            # 다른 배율로 열려 있었을 때 ROE 계산이 통째로 어긋난다(20배면 4배 오차).
            # 거래소가 알려주는 실제 배율을 쓴다.
            try:
                lev0 = int(float(lp.get("leverage") or args.leverage))
            except Exception:
                lev0 = args.leverage
            if lev0 != args.leverage:
                say(f"주의 {sym0} 실제 레버리지 {lev0}x (봇 설정 {args.leverage}x) - 실제값으로 추적")
            positions[sym0] = Pos(sym0, side0, ep, qty0, time.time(), lev0)
            # 보호 주문 재등록 - 기존 algo가 남아 있으면 중복될 수 있으나
            # reduceOnly라 포지션 이상으로 체결되지 않는다.
            if not args.no_exchange_stop:
                spx = (ep * (1 - cfg.stop_loss_pct / 100 / lev0) if side0 == "LONG"
                       else ep * (1 + cfg.stop_loss_pct / 100 / lev0))
                try:
                    r0 = ex.place_stop_market(sym0, side0, qty0, spx)
                    positions[sym0].stop_algo_id = int((r0 or {}).get("algoId") or 0)
                except Exception as e:
                    say(f"경고 {sym0} 손절 재등록 실패({e})")
            if not args.no_exchange_trail:
                armp0 = (cfg.take_profit_min if side0 == "LONG"
                         else cfg.short_take_profit_min) / 100 / lev0
                act0 = ep * (1 + armp0) if side0 == "LONG" else ep * (1 - armp0)
                # 이미 무장선을 넘긴 포지션은 활성화가가 현재가 반대편이라
                # -2021(Order would immediately trigger)로 거부된다. 그런 경우
                # 현재가에서 트레일폭만큼 떨어진 지점을 활성화가로 쓴다.
                try:
                    mk0 = ex.get_mark_price(sym0)
                except Exception:
                    mk0 = ep
                passed = (mk0 >= act0) if side0 == "LONG" else (mk0 <= act0)
                if passed:
                    td0 = cfg.trail_drawdown_pct / 100 / lev0
                    act0 = mk0 * (1 - td0 / 2) if side0 == "LONG" else mk0 * (1 + td0 / 2)
                try:
                    r1 = ex.place_trailing_stop_market(
                        sym0, side0, qty0, act0, cfg.trail_drawdown_pct / lev0)
                    positions[sym0].trail_algo_id = int((r1 or {}).get("algoId") or 0)
                    if passed:
                        positions[sym0].armed = True
                except Exception as e:
                    say(f"경고 {sym0} 트레일 재등록 실패({e}) - 폴링 익절로 대체")
            say(f"기존포지션 채택 {sym0} {side0} qty={qty0} 진입{ep} - SL/TP 재등록")

    ws_proc = None
    if args.ws:
        ws_proc, ws_cache = start_ws(symbols)
        # seed(REST 스냅샷)는 10~15초면 끝나지만, last_update_ts 는 WS로 "확정된" 캔들이
        # 도착해야 갱신된다. 1분봉이라 최대 60초를 더 기다려야 is_fresh 가 True가 된다.
        # 45초로는 부족했다(실측 0/10). 여유를 둬 100초 대기한다.
        print("WS 워커 기동 - seed + 첫 확정캔들까지 100초 대기", flush=True)
        time.sleep(100)
        ex.set_ws_kline_cache(ws_cache)
        ready = sum(1 for sym in symbols[:10]
                    if ws_cache.has_sufficient_history(sym, 99)
                    and ws_cache.is_fresh(sym, cfg.ws_kline_max_staleness_sec))
        if ready >= 8:
            say(f"WS 활성 - 앞 10심볼 중 {ready}개 준비됨. REST 순회 지연 없음")
        else:
            say(f"WS 미준비({ready}/10) - REST 폴백으로 동작. 순회 지연이 남는다")

    slots, size = auto_size(start_bal, args.leverage, args.max_exposure,
                            args.min_notional, args.notional_safety,
                            args.slots, args.size, args.max_concurrency)
    mode = "DRY-RUN(주문없음)" if args.dry_run else "실주문"
    say(f"시작 [{mode}] 잔고 {start_bal:.4f} / 슬롯{slots}(자동) / 비중{size:.3f} / "
        f"{args.leverage}배 / 문턱{args.pump_chg}%x거래량{cfg.pump_min_volume_ratio} / "
        f"{len(symbols)}심볼 / "
        f"{'무기한' if args.minutes<=0 else f'{args.minutes:.0f}분'} / "
        f"손실한도{'없음' if args.max_loss_pct<=0 else f'{args.max_loss_pct:.0f}%'}")

    # minutes=0 이면 무기한. 사용자가 Ctrl+C 로 종료한다.
    # [2026-08-20] 신호가 예상의 31%뿐인 원인 추적용 계측.
    diag = {"scans": 0, "sym_seen": 0, "pump_hit": 0, "bars_covered": set(),
            "scan_cut": 0, "ages": [], "entry_sec": [], "scan_len": []}
    scan_ran = False
    last_sym_refresh = time.time()
    start_t = time.time()
    deadline = (time.time() + args.minutes * 60) if args.minutes > 0 else float("inf")
    last_rest = 0.0
    last_brief = time.time()
    _lt0 = time.localtime()
    last_brief_slot = (_lt0.tm_hour, 0 if _lt0.tm_min < 30 else 30)
    n_sig = n_entry = n_exit = 0
    stats = {"win": 0, "net": 0.0, "nom": 0.0}
    reject: dict[str, int] = {}
    last_ws_check = time.time()
    # [2026-08-20] 신호가 예상의 31%뿐인 원인 추적용 계측.
    ws_restart_at = 0.0
    ws_restarts = 0

    def klines(sym):
        """[2026-08-20 버그수정] WS 캐시가 서빙 가능한 심볼은 REST를 안 타므로
        스로틀을 걸 이유가 없다. 그런데 무조건 0.35초씩 잠들어 85심볼 한 바퀴에
        30초가 걸렸고, 그만큼 포지션 확인이 늦어졌다.
        실측 사고: BTWUSDT 가 +13% ROE 까지 갔다가 트레일(+11.7%)에 안 걸리고
        -6% 손절까지 되돌아왔다 - 봇이 그 40초 사이를 통째로 못 봤다."""
        nonlocal last_rest
        ws = getattr(ex, "_ws_kline_cache", None)
        served_by_ws = (
            ws is not None
            and ws.has_sufficient_history(sym, 99)
            and ws.is_fresh(sym, cfg.ws_kline_max_staleness_sec)
        )
        if not served_by_ws:
            wait = args.rest_min_interval - (time.time() - last_rest)
            if wait > 0:
                time.sleep(wait)
            last_rest = time.time()
        return ex.get_klines(sym)

    def close(pos: Pos, reason: str) -> None:
        nonlocal n_exit
        mark = ex.get_mark_price(pos.symbol)
        roe = pnl_pct(pos.entry_price, mark, pos.side) * pos.leverage
        since = int(pos.entered_at * 1000) - 5000
        if not args.dry_run:
            for aid in (pos.stop_algo_id, pos.trail_algo_id):
                if aid:
                    try:
                        ex.cancel_order(pos.symbol, aid)
                    except Exception:
                        pass
            try:
                ex.close_market_position(pos.symbol, pos.side, abs(pos.quantity))
            except Exception as e:
                say(f"청산실패 {pos.symbol}: {e}")
                return
            time.sleep(1.0)  # 체결 반영 대기
        trades = [] if args.dry_run else real_trades(ex, pos.symbol, since)
        nominal = pos.entry_price * pos.quantity
        if trades:
            f = exit_facts(trades, pos.side, nominal, pos.leverage)
            commission, realized = f["commission"], f["realized"]
            if f["exit_price"]:
                mark = f["exit_price"]
            roe = f["roe_pct"]
        else:
            commission = realized = 0.0
        net = realized - commission
        append_ledger(dict(
            version=VERSION, symbol=pos.symbol, side=pos.side,
            entry_price=pos.entry_price, exit_price=mark, quantity=pos.quantity,
            exit_reason=reason, entered_at=pos.entered_at, exited_at=time.time(),
            held_seconds=time.time() - pos.entered_at, leverage=pos.leverage,
            roe_pct=roe, nominal=pos.entry_price * pos.quantity,
            intended_entry=pos.intended_entry,
            entry_slip_pct=(((pos.entry_price / pos.intended_entry - 1) * 100
                             * (1 if pos.side == "LONG" else -1))
                            if pos.intended_entry else None),
            real_commission=commission, real_realized_pnl=realized, real_net=net,
            fill_count=len(trades),
            max_adverse_roe=pos.max_adverse_roe, max_favorable_roe=pos.max_favorable_roe,
            origin=f"scalp_bot_{VERSION}", dry_run=args.dry_run,
        ))
        positions.pop(pos.symbol, None)
        n_exit += 1
        stats["win"] += 1 if net > 0 else 0
        stats["net"] += net
        stats["nom"] += nominal
        wr = stats["win"] / n_exit * 100 if n_exit else 0.0
        say(f"청산 {pos.symbol} {pos.side} {reason} ROE{roe:+.2f}% 순손익{net:+.4f}"
            f" | 누적 {n_exit}건 승률{wr:.1f}% 손익{stats['net']:+.4f}"
            f" 명목당{stats['net'] / max(stats['nom'], 1e-9) * 100:+.3f}%")

    def rej(reason: str) -> None:
        reject[reason] = reject.get(reason, 0) + 1

    while time.time() < deadline:
        try:
            cycle = time.time()

            for sym in list(positions):
                pos = positions[sym]
                # [2026-08-20] 거래소측 STOP_MARKET이 발동하면 포지션이 사라지는데 봇은
                # 모른다. 그대로 두면 유령 포지션이 슬롯을 차지하고 청산 시도가 -2022로
                # 실패한다. 매 주기 실제 보유를 확인해 사라졌으면 원장에 남기고 정리한다.
                if not args.dry_run:
                    try:
                        live = ex.get_position(sym)
                    except Exception:
                        live = None
                        pass
                    else:
                        if live is None or abs(float(live.get("amount", 0) or 0)) == 0:
                            mk = ex.get_mark_price(sym)
                            roe = pnl_pct(pos.entry_price, mk, pos.side) * pos.leverage
                            tr = real_trades(ex, sym, int(pos.entered_at * 1000) - 5000)
                            nom = pos.entry_price * pos.quantity
                            if tr:
                                f = exit_facts(tr, pos.side, nom, pos.leverage)
                                comm, rz = f["commission"], f["realized"]
                                if f["exit_price"]:
                                    mk = f["exit_price"]
                                roe = f["roe_pct"]
                            else:
                                comm = rz = 0.0
                            append_ledger(dict(
                                version=VERSION, symbol=sym, side=pos.side,
                                entry_price=pos.entry_price, exit_price=mk,
                                quantity=pos.quantity, exit_reason="EXCHANGE_STOP",
                                entered_at=pos.entered_at, exited_at=time.time(),
                                held_seconds=time.time() - pos.entered_at,
                                leverage=pos.leverage, roe_pct=roe,
                                nominal=pos.entry_price * pos.quantity,
                                intended_entry=pos.intended_entry,
                                entry_slip_pct=(((pos.entry_price / pos.intended_entry - 1) * 100
                                                 * (1 if pos.side == "LONG" else -1))
                                                if pos.intended_entry else None),
                                real_commission=comm, real_realized_pnl=rz,
                                real_net=rz - comm, fill_count=len(tr),
                                max_adverse_roe=pos.max_adverse_roe,
                                max_favorable_roe=pos.max_favorable_roe,
                                origin=f"scalp_bot_{VERSION}", dry_run=False,
                            ))
                            for aid in (pos.stop_algo_id, pos.trail_algo_id):
                                if aid:
                                    try:
                                        ex.cancel_order(sym, aid)
                                    except Exception:
                                        pass
                            positions.pop(sym, None)
                            n_exit += 1
                            _net = rz - comm
                            stats["win"] += 1 if _net > 0 else 0
                            stats["net"] += _net
                            stats["nom"] += nom
                            _wr = stats["win"] / n_exit * 100 if n_exit else 0.0
                            say(f"청산 {sym} {pos.side} EXCHANGE_STOP ROE{roe:+.2f}%"
                                f" 순손익{_net:+.4f} | 누적 {n_exit}건 승률{_wr:.1f}%"
                                f" 손익{stats['net']:+.4f}"
                                f" 명목당{stats['net'] / max(stats['nom'], 1e-9) * 100:+.3f}%")
                            continue
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
                        say(f"무장 {sym} ROE{roe:+.2f}%", tg_send=False)
                    if pos.armed:
                        pos.peak_roe = max(pos.peak_roe, roe)
                        if roe <= pos.peak_roe - cfg.trail_drawdown_pct:
                            reason = "TRAIL"
                if reason:
                    close(pos, reason)

            bal = ex.get_total_margin_balance()
            if stop_bal > 0 and bal <= stop_bal:
                say(f"손실한도 도달 종료 (잔고 {bal:.4f} <= {stop_bal:.4f})")
                break

            slots, size = auto_size(bal, args.leverage, args.max_exposure,
                                    args.min_notional, args.notional_safety,
                                    args.slots, args.size, args.max_concurrency)
            if len(positions) < slots and bal >= args.min_balance:
                scan_started = time.time()
                scan_ran = True
                diag["scans"] += 1
                diag["bars_covered"].add(int(scan_started // 60))
                for sym in symbols:
                    if time.time() > deadline or len(positions) >= slots:
                        break
                    # 스캔이 길어지면 보유 포지션 확인이 밀린다. 15초마다 스캔을
                    # 중단하고 다음 주기로 넘겨 청산 판정을 먼저 돌린다.
                    if time.time() - scan_started > 15:
                        diag["scan_cut"] += 1
                        break
                    diag["sym_seen"] += 1
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
                    diag["pump_hit"] += 1
                    # 소진도: 신호봉 안에서 종가가 진행 방향 끝에 얼마나 붙었나
                    if args.min_exhaust > 0:
                        _c = df.iloc[-1]
                        _rng = float(_c["high"]) - float(_c["low"])
                        if _rng > 0:
                            _pos = (float(_c["close"]) - float(_c["low"])) / _rng
                            _exh = _pos if sig == "LONG" else (1 - _pos)
                            if _exh < args.min_exhaust:
                                rej(f"소진미달({_exh:.2f})")
                                say(f"신호 {sym} {sig} -> 탈락(소진도 {_exh:.2f}<"
                                    f"{args.min_exhaust})", tg_send=False)
                                continue
                    if not immediate_momentum_ok(df, sig):
                        rej("캔들방향"); say(f"신호 {sym} {sig} -> 탈락(캔들방향)", tg_send=False); continue
                    if not args.no_taker and not volume_direction_ok(df, sig, cfg):
                        rej("테이커"); say(f"신호 {sym} {sig} -> 탈락(테이커 쏠림 부족)", tg_send=False); continue
                    need = (cfg.min_entry_probability if sig == "LONG"
                            else cfg.short_min_entry_probability)
                    if prob < need:
                        rej("확률"); say(f"신호 {sym} {sig} -> 탈락(확률 {prob:.2f}<{need:.2f})", tg_send=False); continue
                    if not args.no_mtf:
                        agree, total = mtf_trend_alignment(ex, cfg, sym, sig)
                        if total == 0 or agree / total < cfg.mtf_min_agree_ratio:
                            rej("MTF"); say(f"신호 {sym} {sig} -> 탈락(MTF {agree}/{total})", tg_send=False); continue

                    # [2026-08-20] 신호봉이 이미 지나갔으면 진입하지 않는다.
                    # 검증은 봉 확정 직후 시가 진입인데 봇은 평균 24초 늦었다.
                    age = time.time() % 60
                    if args.max_signal_age > 0 and age > args.max_signal_age:
                        diag["ages"].append(age)
                        rej(f"신호노후({age:.0f}s)"); continue
                    # [2026-08-20 재수정] 캔들 종가를 진입가로 쓰면 실제 체결가와
                    # 크게 어긋난다(실측 중앙 +1.83%). 급변동 직후라 봉 확정 뒤에도
                    # 호가가 계속 밀리기 때문이다. 실제 호가를 확인하고,
                    # 이미 크게 벌어졌으면 진입을 포기한다(REST 1회 ~300ms).
                    bar_close = float(df["close"].iloc[-1])
                    try:
                        price = ex.get_mark_price(sym)
                    except Exception:
                        rej("호가조회실패"); continue
                    if args.max_entry_slip > 0 and bar_close > 0:
                        dev = (price / bar_close - 1) * 100 * (1 if sig == "LONG" else -1)
                        if dev > args.max_entry_slip:
                            rej(f"호가이탈({dev:.2f}%)")
                            say(f"신호 {sym} {sig} -> 탈락(호가 {dev:+.2f}% 이탈)", tg_send=False)
                            continue
                    margin = bal * size
                    qty = ex.round_quantity(sym, margin * args.leverage / price,
                                            price=price, max_notional=margin * args.leverage)
                    if not qty:
                        rej("수량"); say(f"신호 {sym} {sig} -> 탈락(최소수량/명목 미달)", tg_send=False); continue
                    if args.dry_run:
                        say(f"[DRY] 진입 {sym} {sig} @{price} qty={qty} 명목{price*qty:.2f}")
                        positions[sym] = Pos(sym, sig, price, qty, time.time(), args.leverage)
                        n_entry += 1
                        continue
                    ent_t0 = time.time()
                    # [2026-08-20] set_leverage 실패를 무시하면 그 심볼의 기존 배율
                    # (예: 20x)로 주문이 나가 명목/위험이 몇 배가 된다. 실패하면 진입을
                    # 포기한다.
                    try:
                        ex.set_margin_type(sym, "ISOLATED")
                    except Exception:
                        pass          # 이미 ISOLATED면 -4046이 정상 반환된다
                    try:
                        ex.set_leverage(sym, args.leverage)
                    except Exception as e:
                        rej("배율설정실패")
                        say(f"신호 {sym} {sig} -> 진입포기(레버리지 설정 실패: {e})")
                        continue
                    try:
                        ex.open_market_position(sym, sig, qty)
                    except Exception as e:
                        rej("주문거부"); say(f"신호 {sym} {sig} -> 진입실패: {e}"); continue
                    time.sleep(1.0)
                    live = ex.get_position(sym)
                    fill = live["entry_price"] if live else price
                    ent_tr = real_trades(ex, sym, int(time.time() * 1000) - 15000)
                    ent_comm = sum(float(t.get("commission", 0)) for t in ent_tr)
                    # [2026-08-20] 폴링(10초)만으로는 급락 시 손절이 크게 밀린다.
                    # 실측: BTWUSDT LONG 이 -6.0% 설정에 -11.40%로 체결(체결 5건 분할).
                    # 거래소에 STOP_MARKET을 걸어두면 거래소가 즉시 반응한다.
                    algo_id = 0
                    if not args.no_exchange_stop:
                        sp_price = (fill * (1 - cfg.stop_loss_pct / 100 / args.leverage)
                                    if sig == "LONG"
                                    else fill * (1 + cfg.stop_loss_pct / 100 / args.leverage))
                        try:
                            r = ex.place_stop_market(sym, sig, qty, sp_price)
                            algo_id = int((r or {}).get("algoId") or 0)
                        except Exception as e:
                            say(f"경고 {sym} 거래소 손절주문 실패({e}) - 폴링 손절만 동작")
                    # 거래소측 트레일링 익절 - 손절만 거래소가 지키면 이익은 폴링이 흘린다.
                    # 활성화가는 무장선(롱 3.0 / 숏 4.0 ROE), 되돌림폭은 trail을 레버리지로 나눈 가격%.
                    trail_id = 0
                    if not args.no_exchange_trail:
                        armp = (cfg.take_profit_min if sig == "LONG"
                                else cfg.short_take_profit_min) / 100 / args.leverage
                        act = fill * (1 + armp) if sig == "LONG" else fill * (1 - armp)
                        cb = cfg.trail_drawdown_pct / args.leverage
                        try:
                            r2 = ex.place_trailing_stop_market(sym, sig, qty, act, cb)
                            trail_id = int((r2 or {}).get("algoId") or 0)
                        except Exception as e:
                            say(f"경고 {sym} 거래소 트레일링 실패({e}) - 폴링 익절만 동작")
                    positions[sym] = Pos(sym, sig, fill, qty, time.time(),
                                         args.leverage, entry_commission=ent_comm,
                                         intended_entry=bar_close,
                                         stop_algo_id=algo_id, trail_algo_id=trail_id)
                    n_entry += 1
                    diag["entry_sec"].append(time.time() - ent_t0)
                    say(f"진입 {sym} {sig} @{fill} qty={qty} 명목{fill*qty:.2f} 수수료{ent_comm:.4f}")

            if diag["scans"] and "scan_started" in dir():
                pass
            if (args.symbol_refresh_min > 0
                    and time.time() - last_sym_refresh >= args.symbol_refresh_min * 60):
                last_sym_refresh = time.time()
                try:
                    fresh = (ex.get_active_usdt_perpetual_symbols(limit=args.symbols)
                             if cfg.auto_symbols else list(cfg.symbols)[: args.symbols])
                except Exception as e:
                    fresh = None
                    say(f"경고 심볼 목록 갱신 실패({e})", tg_send=False)
                if fresh and set(fresh) != set(symbols):
                    added = [x for x in fresh if x not in symbols]
                    removed = [x for x in symbols if x not in fresh]
                    # 보유 중인 심볼은 제외 목록에서 빼서 계속 추적한다
                    keep = [x for x in removed if x in positions]
                    symbols = fresh + keep
                    say(f"심볼 갱신 +{len(added)} -{len(removed)} (보유유지 {len(keep)}) "
                        f"-> {len(symbols)}종")
                    if ws_proc is not None:
                        try:
                            ws_proc.terminate(); ws_proc.wait(timeout=10)
                        except Exception:
                            pass
                        ws_proc, ws_cache = start_ws(symbols)
                        ex.set_ws_kline_cache(None)
                        ws_restart_at = time.time()

            if ws_proc is not None and time.time() - last_ws_check >= 60:
                last_ws_check = time.time()
                h = ws_health()
                why = ws_is_frozen(h)
                if why:
                    say(f"WS 이상 감지({why}) - 워커 재시작")
                    try:
                        ws_proc.terminate(); ws_proc.wait(timeout=10)
                    except Exception:
                        pass
                    ws_proc, ws_cache = start_ws(symbols)
                    ex.set_ws_kline_cache(None)   # 재seed 동안은 REST 폴백
                    ws_restart_at = time.time()
                    ws_restarts += 1
                elif ws_restart_at and time.time() - ws_restart_at >= 100:
                    ex.set_ws_kline_cache(ws_cache)
                    ws_restart_at = 0.0
                    say("WS 워커 재연결 완료 - 캐시 재사용 시작")

            if scan_ran:
                diag["scan_len"].append(time.time() - scan_started)
                scan_ran = False
            # 정각/30분 정렬 모드: 경계를 막 지났고 이번 경계에 아직 안 보냈으면 발송
            if args.brief_on_clock:
                _lt = time.localtime()
                _slot = (_lt.tm_hour, 0 if _lt.tm_min < 30 else 30)
                due = (_slot != last_brief_slot)
            else:
                due = bool(args.brief_every) and (time.time() - last_brief >= args.brief_every)
            if due:
                last_brief = time.time()
                if args.brief_on_clock:
                    _lt = time.localtime()
                    last_brief_slot = (_lt.tm_hour, 0 if _lt.tm_min < 30 else 30)
                rj = " ".join(f"{k}{v}" for k, v in sorted(reject.items(), key=lambda x: -x[1]))
                held = ", ".join(f"{s}({positions[s].side})" for s in positions) or "없음"
                wsinfo = ""
                if ws_proc is not None:
                    h = ws_health()
                    wsinfo = (f" WS[msg60s={h.get('message_count_60s','?')} "
                              f"hb={h.get('hb_age',-1):.0f}s 정체{h.get('stalled','?')} "
                              f"재시작{ws_restarts}]")
                el = time.time() - start_t
                mins = max(1, int(el // 60))
                import statistics as _st
                ag = diag["ages"]; en = diag["entry_sec"]; sl_ = diag["scan_len"]
                say(f"계측 스캔{diag['scans']}회/{mins}분 커버봉{len(diag['bars_covered'])} "
                    f"심볼조회{diag['sym_seen']} 펌프적중{diag['pump_hit']} "
                    f"스캔중단{diag['scan_cut']}", tg_send=False)
                say(f"계측2 노후탈락{len(ag)}건"
                    + (f" age중앙{_st.median(ag):.0f}s 최대{max(ag):.0f}s" if ag else "")
                    + (f" | 진입처리 중앙{_st.median(en):.1f}s 최대{max(en):.1f}s({len(en)}건)" if en else "")
                    + (f" | 스캔소요 중앙{_st.median(sl_):.1f}s 최대{max(sl_):.1f}s" if sl_ else ""), tg_send=False)
                say(f"브리핑 잔고{bal:.4f}({(bal/start_bal-1)*100:+.2f}%) "
                    f"신호{n_sig} 진입{n_entry} 청산{n_exit} 보유[{held}] "
                    f"탈락[{rj or '없음'}]{wsinfo}")

            if args.bar_align:
                # [2026-08-20 수정] 이전 구현은 "봉까지 10초 넘게 남으면 poll만큼만 자고
                # 루프 재시작"이었다. 그러면 다음 스캔이 봉 확정 직후가 아닌 시점에 돌아
                # 전부 신호노후로 버려지고, 정작 봉이 확정되는 순간에는 자고 있었다.
                # 실측: 11:44 TRUMPUSDT 1.93%x10.3 이 조건을 완전히 충족했는데 신호 0건.
                # 스캔이 1.72초면 끝나므로(85심볼, WS), 봉 확정 직후로 정확히 정렬하고
                # 남는 시간에는 청산 판정만 반복한다.
                while True:
                    wait = 60 - (time.time() % 60) + 0.2
                    if wait <= 2.0:
                        time.sleep(wait)
                        break
                    time.sleep(min(2.0, wait - 0.5))
                    # 대기 중에도 보유 포지션은 계속 감시한다
                    for sym2 in list(positions):
                        p2 = positions[sym2]
                        try:
                            mk2 = ex.get_mark_price(sym2)
                        except Exception:
                            continue
                        r2 = pnl_pct(p2.entry_price, mk2, p2.side) * p2.leverage
                        p2.max_adverse_roe = min(p2.max_adverse_roe, r2)
                        p2.max_favorable_roe = max(p2.max_favorable_roe, r2)
                        a2 = (cfg.take_profit_min if p2.side == "LONG"
                              else cfg.short_take_profit_min)
                        if not p2.armed and r2 >= a2:
                            p2.armed = True
                        if p2.armed:
                            p2.peak_roe = max(p2.peak_roe, r2)
            else:
                slept = args.poll - (time.time() - cycle)
                if slept > 0:
                    time.sleep(slept)
        except KeyboardInterrupt:
            say("사용자 중단")
            break
        except Exception as e:
            print(f"  [주기오류] {type(e).__name__}: {e}", flush=True)
            time.sleep(args.poll)

    if positions and args.close_on_exit:
        for sym in list(positions):
            close(positions[sym], "SHUTDOWN")

    if ws_proc is not None:
        try:
            ws_proc.terminate()
            ws_proc.wait(timeout=10)
        except Exception:
            pass
        print("WS 워커 정리 완료", flush=True)

    end_bal = ex.get_total_margin_balance()
    rj = " ".join(f"{k}{v}" for k, v in sorted(reject.items(), key=lambda x: -x[1]))
    say(f"종료 신호{n_sig} 진입{n_entry} 청산{n_exit} 미청산{len(positions)} "
        f"| 잔고 {start_bal:.4f}->{end_bal:.4f} ({(end_bal/start_bal-1)*100:+.2f}%) "
        f"| 탈락[{rj or '없음'}]")
    if positions:
        say(f"주의: 미청산 {len(positions)}개 - {', '.join(positions)}")
    print(f"원장: {LEDGER}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
