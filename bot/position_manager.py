import json
import logging
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from .config import Config
from .strategy import pnl_pct

log = logging.getLogger("bot.position")

STATS_FILE = Path(__file__).resolve().parent.parent / ".bot_stats.json"


@dataclass
class TrackedPosition:
    symbol: str
    side: str
    entry_price: float
    quantity: float
    armed: bool = False  # 최소 익절선(take_profit_min)을 넘어 트레일링이 활성화됐는지
    peak_pnl: float = 0.0
    entered_at: float = field(default_factory=time.time)  # 스캘핑 최대 보유시간 계산용
    leverage: float = 1.0  # 손절 판단(ROE% = pnl% * leverage)에 사용, 바이낸스 앱 표시값과 동일 기준
    # [2026-08-10] 예전엔 "물타기 했는지 여부"만 bool로 기록해서 포지션당 딱 1회만 허용했는데,
    # 사용자 요청으로 "몇 번이든 나눠서 넣을 수 있게 하되, 포지션 전체 마진 합계는 항상
    # 일정 비율(기본 6%) 밑으로만"으로 바꿨다 — 그래서 횟수 대신 실제 투입된 마진 금액을
    # 직접 추적하고, 매번 "총 상한을 넘는지"로 판단한다(횟수 제한이 아니라 금액 상한).
    average_down_count: int = 0  # 로그/트리거 폭 계산용(몇 번째 추가인지)
    initial_margin_usdt: float = 0.0  # 최초 진입 시 증거금(추가 물타기 전) — 상한 계산의 기준
    total_margin_added_usdt: float = 0.0  # 지금까지 물타기로 추가된 증거금 합계
    max_total_margin_usdt: float = 0.0  # 이 포지션에 허용된 총 증거금 상한(최초 진입 시점 잔고 기준으로 1회 계산)
    origin: str = "bot"  # "bot"(봇이 직접 진입) 또는 "manual"(사용자가 직접 진입) — 성과통계 반영 여부에 사용
    # [2026-08-06] 거래소 자체 STOP_MARKET 주문 ID — 30초 폴링 사이 급락(TAKEUSDT 실측: -20%)을
    # 막기 위해 진입 직후 거래소에 걸어두는 손절 주문. 봇이 먼저 청산하면 취소해야 함.
    stop_order_id: int | None = None
    # [2026-08-09] 거래소 네이티브 트레일링(Algo TRAILING_STOP_MARKET) 주문 ID. 기존 폴링(30초
    # 주기) 기반 트레일링 익절은 캔들 사이 급반전을 놓치는 문제(실측: PTBUSDT 고점ROE 8.15%→
    # 확인 시점엔 -0.18%, 목표 하락폭 1.5%p인데 실제로는 8.33%p나 밀린 뒤에야 청산)가 확인돼
    # 추가함. 등록 실패해도 기존 폴링 트레일링이 그대로 안전망으로 동작하므로 진입을 막지 않음.
    trailing_order_id: int | None = None
    # [2026-08-10 사용자요청] "스윙이란 건 포지션의 최대일 때뿐" — 스윙 국면 감지로 거래소
    # TRAILING_STOP_MARKET 콜백폭을 넓히는 건 armed(최소 익절선 이미 도달) 상태에서만 하고,
    # 한 번 넓히면 이 포지션이 살아있는 동안은 다시 좁히지 않는다(반복 재등록 방지, 매 5초
    # 주기마다 불필요하게 주문을 다시 걸지 않기 위함).
    exchange_trailing_widened: bool = False
    # [2026-08-09] TRAILING_STOP_MARKET 등록 실패시의 거래소측 폴백 익절 주문 ID.
    tp_fallback_order_id: int | None = None
    # [2026-08-11 사용자요청] 진입 직후 유예기간 동안 손절폭을 넓혀 걸어뒀는지 여부 — True면
    # reconcile 루프가 유예기간 만료 시 원래 폭으로 다시 좁혀야 한다는 표시.
    stop_loss_widened: bool = False

    @property
    def protection_state(self) -> str:
        """[2026-08-09] 거래소측 보호주문 상태를 세 개의 주문ID 필드로부터 계산해서 반환한다.
        별도 필드로 매 등록/취소 지점마다 수동으로 갱신하게 하면 어긋날 위험이 있어(등록은
        했는데 상태 갱신을 깜빡하는 식), 항상 실제 ID 필드 기준으로 계산되는 property로 둔다.

        UNPROTECTED: 손절 주문조차 없음 (등록 실패 직후 등, 정상 운영 중엔 거의 없어야 함)
        STOP_ONLY: 손절만 있고 익절 계열(트레일링/폴백)은 없음
        TRAILING_ACTIVE: 거래소 네이티브 트레일링이 활성 상태
        TP_FALLBACK_ACTIVE: 트레일링 대신 고정가 익절 폴백이 활성 상태
        """
        if self.stop_order_id is None:
            return "UNPROTECTED"
        if self.trailing_order_id is not None:
            return "TRAILING_ACTIVE"
        if self.tp_fallback_order_id is not None:
            return "TP_FALLBACK_ACTIVE"
        return "STOP_ONLY"


def profit_usdt(pos: TrackedPosition, mark_price: float) -> float:
    """포지션의 미실현/실현 손익을 USDT 절대금액으로 계산한다 (수수료 미반영, 총(gross)
    손익). ROE/문턱 비교 등 실시간 판단 로직은 계속 이 함수를 그대로 쓴다 — 여기에
    수수료를 섞으면 기존에 검증된 문턱값들의 의미가 달라진다."""
    if pos.side == "LONG":
        return (mark_price - pos.entry_price) * pos.quantity
    return (pos.entry_price - mark_price) * pos.quantity


def net_profit_usdt(pos: TrackedPosition, mark_price: float, cfg: Config) -> float:
    """[2026-08-10 사용자요청] "장부는 수익인데 실제 지갑은 줄어든다" 착시 방지 — 진입+청산
    왕복 수수료(cfg.fee_rate_roundtrip)를 뺀 순(net) 손익. 통계/텔레그램/거래장부에 최종
    기록하는 값은 반드시 이걸 써야 실제 지갑 변화와 일치한다(실시간 ROE 판단용
    profit_usdt()와는 용도가 다름 — 혼동 금지)."""
    gross = profit_usdt(pos, mark_price)
    fee_estimate = pos.entry_price * abs(pos.quantity) * cfg.fee_rate_roundtrip
    return gross - fee_estimate


class PositionManager:
    """포지션별 3~7% 익절 / -5% 손절 로직을 관리한다.

    - pnl이 take_profit_max(7%) 이상이면 즉시 익절.
    - pnl이 take_profit_min(3%) 이상이면 트레일링 모드로 전환(armed).
      이후 최고점(peak_pnl) 대비 1%p 이상 하락하면 익절(수익 3~7% 구간에서 확정).
    - pnl이 -stop_loss_pct(-5%) 이하이면 즉시 손절.
    """

    def _stop_loss_pct_for(self, side: str, entered_at: float | None = None) -> float:
        """[2026-08-11 사용자요청] SHORT만 손절폭을 따로 조이기 위한 헬퍼. SHORT_STOP_LOSS_PCT가
        0(기본값)이면 기존처럼 공용 stop_loss_pct를 쓰고, 양수면 SHORT 포지션에서만 그 값을
        쓴다(LONG은 항상 공용 stop_loss_pct 그대로).

        [2026-08-12 실거래에서 발견/수정] 진입 직후 유예기간(STOP_LOSS_GRACE_SEC) 동안은
        거래소 STOP_MARKET을 넓혀뒀는데, 이 폴링 기반 체크는 그걸 몰라서 좁은 기존 폭으로
        먼저 손절을 발동시켜버려 유예기간이 사실상 무력화되는 사고가 있었다(ONEUSDT ROE
        -3.04%에서 즉시청산, 원래 유예중이면 6%까지 버텼어야 함). bot/main.py의
        compute_stop_loss_pct()와 동일한 유예 로직을 여기도 반영해서 두 경로가 항상 같은
        기준을 쓰도록 맞춘다."""
        base = self.cfg.short_stop_loss_pct if side == "SHORT" and self.cfg.short_stop_loss_pct > 0 else self.cfg.stop_loss_pct
        if self.cfg.stop_loss_grace_sec <= 0 or entered_at is None:
            return base
        if (time.time() - entered_at) < self.cfg.stop_loss_grace_sec:
            return base * self.cfg.stop_loss_grace_widen_mult
        return base

    def __init__(self, cfg: Config):
        self.cfg = cfg
        # [2026-08-10 사용자요청] 텔레그램 명령어(예: 수동 /close)는 별도 스레드(폴링
        # 스레드)에서 이 인스턴스의 record_result()를 호출할 수 있다 — 메인 루프도 같은
        # PositionManager를 공유하므로, 두 스레드가 동시에 self.wins 등을 갱신하면
        # read-modify-write 경합(레이스 컨디션)으로 카운트가 누락될 수 있다. asyncio가
        # 아니라 스레드 기반이라 threading.Lock으로 보호한다.
        self._lock = threading.Lock()
        self.positions: dict[str, TrackedPosition] = {}
        self.win_streak: int = 0
        self.total_trades: int = 0
        self.wins: int = 0
        self.losses: int = 0
        # [2026-08-10 사용자요청] "전체 승률만 보면 롱/숏 중 한쪽이 나빠도 숨겨진다" — 방향별로
        # 따로 집계해야 실제로 어느 쪽 로직이 문제인지 보인다.
        self.long_wins: int = 0
        self.long_losses: int = 0
        self.short_wins: int = 0
        self.short_losses: int = 0
        # [2026-08-10 사용자요청] "횟수"만으론 부족하다 — 방향별로 실제 얼마를 벌고
        # 잃었는지(손익비 계산의 기반)까지 따로 누적한다.
        self.long_pnl_usdt: float = 0.0
        self.short_pnl_usdt: float = 0.0
        self.realized_pnl_usdt: float = 0.0
        self.total_balance: float = 0.0  # main.py가 매 사이클 갱신 (소액 구간 익절 기준 판단용)
        self.symbol_loss_streak: dict[str, int] = {}
        self.symbol_blacklist_until: dict[str, float] = {}
        # [2026-08-13 사용자요청] 연속(스트릭) 손실과 별개로 "짧은 시간창 내 반복 손실"을
        # 추적한다 — symbol -> 최근 손실 시각 리스트(윈도우 밖은 정리). 재시작 시 초기화되어도
        # 창 자체가 짧아(기본 30분) 리스크가 작다고 판단해 별도 파일 영속화는 하지 않는다.
        self.symbol_loss_timestamps: dict[str, list[float]] = {}
        # [2026-08-11 사용자요청] 심볼별 격리와 별개로, 봇 전체가 짧게 연속으로 지고 있으면
        # (장세 자체가 안 맞을 가능성) 신규 진입을 잠깐 전체 정지한다. win_streak과 달리
        # 이건 "이겼을 때"뿐 아니라 "심볼이 바뀌어도" 계속 누적되는 전역 카운터다.
        self.global_consecutive_losses: int = 0
        self.global_pause_until: float = 0.0
        # [2026-08-12 사용자요청] 자산 절대 하한선(critical_balance_stop_usdt) 진입/해제
        # 상태전환에서만 텔레그램 알림을 보내기 위한 플래그 — 매 주기(30~40초)마다 같은
        # 알림을 반복 전송하지 않도록 한다.
        self.critical_balance_stop_notified: bool = False
        # [2026-08-06] 같은 코인 단시간 연속 재진입 리스크 완화용 — symbol -> (사용한 비중, 진입시각)
        self.symbol_recent_ratio: dict[str, tuple[float, float]] = {}
        self.recent_trade_results: list[dict] = []
        self._load_stats()

    def _load_stats(self):
        """재시작해도 통계가 0으로 초기화되지 않도록 파일에서 복원한다."""
        try:
            if STATS_FILE.exists():
                data = json.loads(STATS_FILE.read_text(encoding="utf-8"))
                self.win_streak = data.get("win_streak", 0)
                self.total_trades = data.get("total_trades", 0)
                self.wins = data.get("wins", 0)
                self.losses = data.get("losses", 0)
                self.long_wins = data.get("long_wins", 0)
                self.long_losses = data.get("long_losses", 0)
                self.short_wins = data.get("short_wins", 0)
                self.short_losses = data.get("short_losses", 0)
                self.long_pnl_usdt = data.get("long_pnl_usdt", 0.0)
                self.short_pnl_usdt = data.get("short_pnl_usdt", 0.0)
                self.realized_pnl_usdt = data.get("realized_pnl_usdt", 0.0)
                self.global_consecutive_losses = data.get("global_consecutive_losses", 0)
                self.global_pause_until = data.get("global_pause_until", 0.0)
                self.recent_trade_results = data.get("recent_trade_results", [])[-100:]
                log.info(
                    "이전 통계 복원: 거래=%d 승=%d 패=%d(롱 %d승%d패/숏 %d승%d패) 연속승리=%d 누적손익=%.2fUSDT",
                    self.total_trades, self.wins, self.losses,
                    self.long_wins, self.long_losses, self.short_wins, self.short_losses,
                    self.win_streak, self.realized_pnl_usdt,
                )
        except Exception:
            log.exception("통계 파일 로드 실패 — 0부터 다시 시작합니다")

    def _save_stats(self):
        """[2026-08-10 사용자요청] 강제종료(예: Stop-Process -Force, kill -9)가 하필 이
        쓰기 도중에 떨어지면 파일이 반쯤 쓰인 채로 잘려서 다음 시작 때 통계가 깨질 수
        있다 — ws_worker.py의 캐시 저장에 이미 적용한 것과 같은 원자적 쓰기(임시파일에
        먼저 쓰고 os.replace로 교체)를 여기(더 중요한 누적 성과 파일)에도 적용한다."""
        try:
            payload = json.dumps({
                "win_streak": self.win_streak,
                "total_trades": self.total_trades,
                "wins": self.wins,
                "losses": self.losses,
                "long_wins": self.long_wins,
                "long_losses": self.long_losses,
                "short_wins": self.short_wins,
                "short_losses": self.short_losses,
                "long_pnl_usdt": self.long_pnl_usdt,
                "short_pnl_usdt": self.short_pnl_usdt,
                "realized_pnl_usdt": self.realized_pnl_usdt,
                "global_consecutive_losses": self.global_consecutive_losses,
                "global_pause_until": self.global_pause_until,
                "recent_trade_results": self.recent_trade_results[-100:],
            })
            tmp_path = STATS_FILE.with_suffix(".tmp")
            tmp_path.write_text(payload, encoding="utf-8")
            tmp_path.replace(STATS_FILE)
        except Exception:
            log.exception("통계 파일 저장 실패")

    def record_result(
        self, symbol: str, pnl_pct_value: float, pnl_usdt: float = 0.0, origin: str = "bot",
        side: str | None = None, leverage: float = 1.0,
    ):
        """청산 손익을 통계에 반영한다. 봇이 직접 진입한 포지션만 승/패·누적손익·연속승리에
        반영한다 — origin="manual"(사용자가 직접 연 포지션)이면 봇의 판단력과 무관하므로
        성과 통계에서 완전히 제외한다(승률 계산 왜곡 방지).

        4) 같은 심볼에서 연속 손실이 symbol_blacklist_loss_threshold(기본 2)회 이상 나면,
        symbol_blacklist_cooldown_min(기본 60분) 동안 그 심볼은 신규 진입 대상에서 제외한다.

        5) [2026-08-10 사용자요청] 연속 손실 횟수와 별개로, 단 1회의 손실이라도 실제
        ROE%(pnl_pct_value*leverage)가 설정 손절폭(stop_loss_pct)보다
        slippage_quarantine_multiplier배 이상 크게 실현되면(호가가 얇아 슬리피지가 심한
        코인일 가능성) 즉시 slippage_quarantine_cooldown_min(기본 24시간) 동안 격리한다."""
        if origin == "manual":
            log.info(
                "[%s] 수동 진입 포지션 종료 — 성과 통계에는 반영하지 않음 (pnl=%.2f%%, %.2fUSDT)",
                symbol, pnl_pct_value, pnl_usdt,
            )
            return
        with self._lock:
            self._record_result_locked(symbol, pnl_pct_value, pnl_usdt, side, leverage)

    def _record_result_locked(self, symbol, pnl_pct_value, pnl_usdt, side, leverage):
        """record_result()의 실제 상태 변경 부분 — 반드시 self._lock을 쥔 채로만 호출한다."""
        self.total_trades += 1
        self.realized_pnl_usdt += pnl_usdt
        if side == "LONG":
            self.long_pnl_usdt += pnl_usdt
        elif side == "SHORT":
            self.short_pnl_usdt += pnl_usdt
        self.recent_trade_results.append({
            "ts": time.time(),
            "symbol": symbol,
            "side": side,
            "pnl_pct": pnl_pct_value,
            "pnl_usdt": pnl_usdt,
        })
        self.recent_trade_results = self.recent_trade_results[-100:]
        # [2026-08-14 정확성감사] 승/패 판정을 gross 가격변동률(pnl_pct_value)이 아니라
        # net 손익(pnl_usdt, 수수료 반영)으로 바꿈 — 가격변동이 0~수수료율 사이일 때
        # gross로는 승리인데 실제로는 손실인 거래가 "승리"로 카운트되어 연속손실
        # 서킷브레이커/심볼 블랙리스트 발동을 지연·누락시키는 걸 실측으로 확인함(항상
        # 보호가 느슨해지는 방향으로만 편향). main.py의 다른 통계 집계는 이미 net 기준.
        if pnl_usdt > 0:
            self.wins += 1
            if side == "LONG":
                self.long_wins += 1
            elif side == "SHORT":
                self.short_wins += 1
            self.win_streak += 1
            self.global_consecutive_losses = 0
            self.symbol_loss_streak[symbol] = 0
            log.info("연속 승리 %d회 (다음 포지션 비중 확대)", self.win_streak)
            # [2026-08-06] 실측 발견: HFTUSDT가 익절(20:27:56) 후 단 30초 만에 같은 코인에
            # 재진입해서 곧바로 손절됨 — 방금 큰 폭 움직인 코인은 되돌림 위험이 크므로, 익절
            # 직후엔 재진입 자체를 짧게 금지한다(손절 후 재진입은 그대로 허용, 비중축소만 적용).
            cooldown_until = time.time() + self.cfg.post_win_reentry_cooldown_min * 60
            self.symbol_blacklist_until[symbol] = cooldown_until
            log.info(
                "[%s] 익절 직후 재진입 %.0f분 쿨다운 적용",
                symbol, self.cfg.post_win_reentry_cooldown_min,
            )
        else:
            self.losses += 1
            if side == "LONG":
                self.long_losses += 1
            elif side == "SHORT":
                self.short_losses += 1
            if self.win_streak:
                log.info("연속 승리 종료 (손실 발생, 비중 최소치로 복귀)")
            self.win_streak = 0
            self.global_consecutive_losses += 1
            if self.global_consecutive_losses >= self.cfg.global_loss_streak_threshold:
                self.global_pause_until = time.time() + self.cfg.global_pause_min * 60
                log.error(
                    "전체 연속손실 %d회(임계치 %d) — 코인 구분 없이 %.0f분 동안 신규 진입을 전체 정지합니다",
                    self.global_consecutive_losses, self.cfg.global_loss_streak_threshold, self.cfg.global_pause_min,
                )
                self.global_consecutive_losses = 0
            streak = self.symbol_loss_streak.get(symbol, 0) + 1
            self.symbol_loss_streak[symbol] = streak
            effective_blacklist_threshold = max(
                self.cfg.symbol_blacklist_loss_threshold,
                self.cfg.symbol_blacklist_min_loss_streak,
            )
            if streak >= effective_blacklist_threshold:
                cooldown_until = time.time() + self.cfg.symbol_blacklist_cooldown_min * 60
                self.symbol_blacklist_until[symbol] = cooldown_until
                log.warning(
                    "[%s] 연속 손실 %d회 — %.0f분 동안 이 심볼 신규 진입을 쉽니다",
                    symbol, streak, self.cfg.symbol_blacklist_cooldown_min,
                )
            # [2026-08-13 사용자요청] 동일 심볼에서 짧은 시간창(symbol_cooldown_window_min)
            # 안에 손실이 symbol_cooldown_loss_count회 이상 나면, 연속손실 스트릭 조건과
            # 무관하게(중간에 승리가 끼어 있어도) 그 심볼만 짧게(symbol_cooldown_block_min)
            # 재진입을 차단한다. 기존 is_symbol_blacklisted() 게이트를 그대로 재사용한다.
            now_ts = time.time()
            window_sec = self.cfg.symbol_cooldown_window_min * 60
            loss_ts = self.symbol_loss_timestamps.setdefault(symbol, [])
            loss_ts.append(now_ts)
            loss_ts[:] = [t for t in loss_ts if now_ts - t <= window_sec]
            if len(loss_ts) >= self.cfg.symbol_cooldown_loss_count:
                cooldown_until = now_ts + self.cfg.symbol_cooldown_block_min * 60
                if cooldown_until > self.symbol_blacklist_until.get(symbol, 0):
                    self.symbol_blacklist_until[symbol] = cooldown_until
                log.warning(
                    "[%s] %.0f분 내 손실 %d회 — %.0f분 동안 이 심볼 재진입을 짧게 차단합니다",
                    symbol, self.cfg.symbol_cooldown_window_min, len(loss_ts), self.cfg.symbol_cooldown_block_min,
                )
            actual_roe = abs(pnl_pct_value) * leverage
            if actual_roe >= self.cfg.stop_loss_pct * self.cfg.slippage_quarantine_multiplier:
                slippage_cooldown_until = time.time() + self.cfg.slippage_quarantine_cooldown_min * 60
                # 기존 연속손실 격리보다 더 길면(보통 그럴 것) 그 값으로 갱신 — 더 짧게 덮어쓰지 않음
                if slippage_cooldown_until > self.symbol_blacklist_until.get(symbol, 0):
                    self.symbol_blacklist_until[symbol] = slippage_cooldown_until
                log.error(
                    "[%s] 비정상 슬리피지 감지(실제손절 ROE=%.2f%% >= 설정손절(%.1f%%)의 %.1f배) — "
                    "%.0f분(약 %.1f시간) 동안 이 심볼 신규 진입을 차단합니다",
                    symbol, actual_roe, self.cfg.stop_loss_pct, self.cfg.slippage_quarantine_multiplier,
                    self.cfg.slippage_quarantine_cooldown_min, self.cfg.slippage_quarantine_cooldown_min / 60,
                )
        self._save_stats()
        long_total = self.long_wins + self.long_losses
        short_total = self.short_wins + self.short_losses
        log.info(
            "누적 성과: 거래=%d 승=%d 패=%d 승률=%.1f%% 누적손익=%.2fUSDT "
            "(롱 %d승%d패 승률%.1f%% / 숏 %d승%d패 승률%.1f%%)",
            self.total_trades, self.wins, self.losses,
            (self.wins / self.total_trades * 100) if self.total_trades else 0.0,
            self.realized_pnl_usdt,
            self.long_wins, self.long_losses, (self.long_wins / long_total * 100) if long_total else 0.0,
            self.short_wins, self.short_losses, (self.short_wins / short_total * 100) if short_total else 0.0,
        )

    def is_symbol_blacklisted(self, symbol: str) -> bool:
        until = self.symbol_blacklist_until.get(symbol)
        return until is not None and time.time() < until

    def is_globally_paused(self) -> bool:
        """[2026-08-11 사용자요청] 전체 연패 서킷브레이커 — True인 동안은 신규 진입만
        막는다(daily_loss_limit_pct와 같은 방식). 기존 포지션의 익절/손절/트레일링 관리는
        이 값과 무관하게 계속된다(main.py 호출부에서 별도로 처리)."""
        return time.time() < self.global_pause_until

    def recent_performance_size_multiplier(self) -> float:
        """Keep trade frequency, but reduce size when the latest bot trades are weak."""
        window = max(1, self.cfg.recent_performance_window)
        recent = self.recent_trade_results[-window:]
        if len(recent) < self.cfg.recent_performance_min_trades:
            return 1.0

        wins = sum(1 for r in recent if float(r.get("pnl_usdt", 0.0)) > 0)
        winrate = wins / len(recent)
        net = sum(float(r.get("pnl_usdt", 0.0)) for r in recent)
        if winrate < self.cfg.recent_defense_winrate_threshold or net < 0:
            mult = max(0.0, min(1.0, self.cfg.recent_defense_size_mult))
            log.info(
                "최근 %d건 성과 방어모드: 승률=%.1f%% 순손익=%.2fUSDT — 신규 진입 비중 %.0f%% 적용",
                len(recent), winrate * 100, net, mult * 100,
            )
            return mult
        return 1.0

    def expected_value_size_multiplier(self) -> float:
        """Keep scanning, but reduce size when the structural roundtrip EV is weak."""
        if self.total_trades < self.cfg.ev_filter_min_sample:
            return 1.0

        winrate = self.wins / self.total_trades if self.total_trades else 0.0
        fee_roundtrip_roe = self.cfg.fee_rate_roundtrip * 100 * self.cfg.leverage_max
        ev = (
            winrate * self.cfg.take_profit_min
            - (1 - winrate) * self.cfg.stop_loss_pct
            - fee_roundtrip_roe
        )
        if ev >= 0:
            return 1.0

        mult = max(0.0, min(1.0, self.cfg.ev_defense_size_mult))
        log.info(
            "수수료 반영 기대값이 음수(%.3f%% ROE) — 신규 진입은 유지하고 비중 %.0f%% 적용",
            ev, mult * 100,
        )
        return mult

    def direction_size_multiplier(self, side: str) -> float:
        """Scale LONG/SHORT size by recent side performance without blocking entries."""
        base = self.cfg.short_size_multiplier if side == "SHORT" else 1.0
        recent = [
            r for r in self.recent_trade_results
            if r.get("side") == side
        ][-max(1, self.cfg.direction_performance_window):]
        if len(recent) < self.cfg.direction_performance_min_trades:
            return base

        wins = sum(1 for r in recent if float(r.get("pnl_usdt", 0.0)) > 0)
        winrate = wins / len(recent)
        net = sum(float(r.get("pnl_usdt", 0.0)) for r in recent)
        mult = base
        if side == "SHORT" and net > 0 and winrate >= 0.5:
            mult = 1.0
        elif net < 0:
            mult = base * self.cfg.direction_loss_size_mult
        mult = max(self.cfg.direction_min_size_mult, min(1.0, mult))
        if abs(mult - base) > 1e-9:
            log.info(
                "%s 최근 %d건 성과 반영: 승률=%.1f%% 순손익=%.2fUSDT — 방향 비중 %.0f%%",
                side, len(recent), winrate * 100, net, mult * 100,
            )
        return mult

    def _large_balance_ratio_cap(self, balance: float) -> float | None:
        """[2026-08-13 사용자요청] 복리로 잔고가 커지면 비중(%)은 그대로라 포지션당 달러
        리스크가 계속 커지는 문제 — 잔고 구간이 딱 문턱을 "초과"하면 비중 상한을 단계적으로
        낮춘다(300 초과 15%, 500 초과 12%, 1000 초과 10%). 손실로 다시 문턱 밑으로 내려가면
        스티키 없이 즉시 그 구간 기준으로 되돌아간다. bot/main.py의
        compute_large_balance_ratio_cap()과 동일 로직(순환import 방지를 위해 여기서도
        직접 구현) — 둘 중 하나를 고치면 반드시 같이 고칠 것."""
        cfg = self.cfg
        if balance > cfg.large_balance_tier3_threshold:
            return cfg.large_balance_tier3_max_ratio
        if balance > cfg.large_balance_tier2_threshold:
            return cfg.large_balance_tier2_max_ratio
        if balance > cfg.large_balance_tier1_threshold:
            return cfg.large_balance_tier1_max_ratio
        return None

    def next_position_size_ratio(self, balance: float, symbol: str | None = None) -> float:
        """연속 승리 횟수에 따라 비중을 키운다.

        잔고가 small_balance_threshold(기본 100 USDT) 미만이면 최대 비중을
        small_balance_max_ratio(기본 100%)까지 허용하고, 그 이상이면 기존
        position_size_max(기본 50%)를 상한으로 쓴다.

        [2026-08-06] 같은 코인에 짧은 시간 안에 연속 재진입하면(실측: TAKEUSDT 4연속 최대비중
        재진입 직후 30초 만에 +7%->-8% 급반전으로 손절선을 건너뛰는 사례 확인) 이미 과열된
        코인일 가능성이 높으므로, symbol이 주어지고 최근 재진입 쿨다운 창 안에 같은 심볼로
        진입한 기록이 있으면 그때 비중의 same_symbol_reentry_ratio_mult(기본 0.45=50% 미만)
        배 이하로 강제 축소한다. 재진입 자체는 막지 않는다(사용자 명시 요청).
        """
        if balance < self.cfg.small_balance_threshold:
            max_ratio = self.cfg.small_balance_max_ratio
        else:
            large_balance_cap = self._large_balance_ratio_cap(balance)
            max_ratio = large_balance_cap if large_balance_cap is not None else self.cfg.position_size_max
        ratio = self.cfg.position_size_min + self.win_streak * self.cfg.position_size_step
        ratio = min(ratio, max_ratio)

        if symbol:
            loss_streak = self.symbol_loss_streak.get(symbol, 0)
            if loss_streak > 0:
                loss_mult = self.cfg.loss_reentry_size_mult ** loss_streak
                loss_mult = max(self.cfg.loss_reentry_min_mult, min(1.0, loss_mult))
                ratio *= loss_mult
                log.info(
                    "[%s] 최근 손실 %d회 — 거래는 유지하되 재진입 비중 %.0f%% 적용",
                    symbol, loss_streak, loss_mult * 100,
                )
            prev = self.symbol_recent_ratio.get(symbol)
            if prev is not None:
                prev_ratio, entered_at = prev
                window_sec = self.cfg.same_symbol_reentry_window_min * 60
                if time.time() - entered_at <= window_sec:
                    cap = prev_ratio * self.cfg.same_symbol_reentry_ratio_mult
                    if ratio > cap:
                        log.info(
                            "[%s] %.0f분 내 재진입 감지(직전 비중 %.0f%%) — 비중을 %.0f%%로 축소",
                            symbol, self.cfg.same_symbol_reentry_window_min, prev_ratio * 100, cap * 100,
                        )
                        ratio = cap
        return ratio

    def record_entry_ratio(self, symbol: str, ratio: float):
        """이번 진입에 실제로 쓴 비중을 기록한다 (다음 재진입 시 축소 판단용)."""
        self.symbol_recent_ratio[symbol] = (ratio, time.time())

    def track(self, symbol: str, side: str, entry_price: float, quantity: float, leverage: float = 1.0,
              origin: str = "bot", balance_at_entry: float | None = None):
        pos = TrackedPosition(symbol, side, entry_price, quantity, leverage=leverage, origin=origin)
        if balance_at_entry:
            # [2026-08-10] 진입 시점 잔고를 기준으로 물타기 총상한을 미리 확정해둔다 — 나중에
            # should_average_down에서 처음 발견할 때 계산하는 것보다, "이 포지션을 열 때의
            # 잔고"가 더 정확한 기준이라 진입 즉시 여기서 채워두는 걸 우선한다(should_average_down
            # 쪽의 지연 초기화는 이 값이 없는 경우—reconcile로 발견된 기존 포지션 등—를 위한 폴백).
            pos.initial_margin_usdt = (entry_price * abs(quantity)) / (leverage or 1.0)
            pos.max_total_margin_usdt = balance_at_entry * self.cfg.average_down_max_total_margin_ratio
        self.positions[symbol] = pos
        log.info("포지션 추적 시작: %s %s entry=%s qty=%s leverage=%sx origin=%s", symbol, side, entry_price, quantity, leverage, origin)

    def untrack(self, symbol: str):
        self.positions.pop(symbol, None)

    def is_tracked(self, symbol: str) -> bool:
        return symbol in self.positions

    def should_average_down(self, symbol: str, mark_price: float, balance: float | None = None) -> bool:
        """손절선까지 가기 전, 그 지점(트랜치마다 더 깊게)에서 물타기(평단가 낮추기) 조건을
        충족하는지 확인한다. 횟수 제한은 없지만(여러 번 나눠서 가능), 이 포지션에 이미 투입된
        총 증거금(초기+추가 전부 합산)이 max_total_margin_usdt에 도달했으면 더 이상 안 한다 —
        "몇 번이든 되지만 총액은 절대 상한을 못 넘는다"는 게 핵심 안전장치.

        [2026-08-10] max_total_margin_usdt/initial_margin_usdt가 아직 초기화 안 된 포지션
        (reconcile로 새로 발견됐거나 이 기능 도입 전부터 있던 포지션 등)은 여기서 처음 호출될
        때 즉시 채운다 — "상한 정보가 없으면 상한 없음"으로 새는 사각지대를 만들면 안 되므로,
        balance가 주어지면 반드시 그 시점 잔고 기준으로 상한을 계산해 채워넣는다.

        [2026-08-09] 계좌 소진 방지 강화 요청으로 average_down_enabled=False가 기본값이 됨 —
        지는 포지션에 추가로 넣는 건 파산 경로의 대표적 패턴이라 완전히 끈다. 코드는 남겨두고
        플래그로 꺼서, 나중에 필요하면 되돌리기 쉽게 한다."""
        if not self.cfg.average_down_enabled:
            return False
        pos = self.positions.get(symbol)
        if pos is None:
            return False
        if pos.max_total_margin_usdt <= 0 and balance:
            pos.initial_margin_usdt = pos.initial_margin_usdt or (pos.entry_price * abs(pos.quantity)) / (pos.leverage or 1.0)
            pos.max_total_margin_usdt = balance * self.cfg.average_down_max_total_margin_ratio
            log.info(
                "[%s] 물타기 총상한 최초 설정: 초기증거금=%.2fUSDT 총상한=%.2fUSDT",
                symbol, pos.initial_margin_usdt, pos.max_total_margin_usdt,
            )
        total_margin_used = pos.initial_margin_usdt + pos.total_margin_added_usdt
        if pos.max_total_margin_usdt > 0 and total_margin_used >= pos.max_total_margin_usdt:
            return False  # 이 포지션의 총 증거금 상한 도달 — 더는 추가 안 함
        pnl = pnl_pct(pos.entry_price, mark_price, pos.side)
        roe = pnl * pos.leverage
        # 트랜치마다 더 깊은 지점에서 발동하도록 (N번째 추가는 손절폭의 trigger_ratio*N 지점).
        # 손절 경계(-stop_loss_pct) 자체를 절대 넘지 않도록 0.95배로 캡한다.
        depth_ratio = min(0.95, self.cfg.average_down_trigger_ratio * (pos.average_down_count + 1))
        side_stop_loss_pct = self._stop_loss_pct_for(pos.side, pos.entered_at)
        trigger_roe = -side_stop_loss_pct * depth_ratio
        return trigger_roe >= roe > -side_stop_loss_pct

    def apply_average_down(self, symbol: str, new_entry_price: float, new_quantity: float, added_margin_usdt: float = 0.0):
        """물타기 주문 체결 후, 거래소에서 갱신된 평단가/수량으로 추적 정보를 업데이트한다.
        added_margin_usdt는 이번에 실제로 추가된 증거금(호출부가 계산해서 넘김) — 누적
        총 증거금 추적에 반영해 다음 판단(should_average_down)의 상한 체크에 쓰인다."""
        pos = self.positions.get(symbol)
        if pos is None:
            return
        pos.entry_price = new_entry_price
        pos.quantity = new_quantity
        pos.average_down_count += 1
        pos.total_margin_added_usdt += added_margin_usdt
        pos.armed = False
        pos.peak_pnl = 0.0
        log.info(
            "[%s] 물타기 반영(%d번째): 새 평단가=%s 수량=%s 누적추가증거금=%.2fUSDT (상한 %.2fUSDT)",
            symbol, pos.average_down_count, new_entry_price, new_quantity,
            pos.total_margin_added_usdt, pos.max_total_margin_usdt,
        )

    def evaluate(
        self, symbol: str, mark_price: float, momentum_continuing: bool = False, swing_continuing: bool = False
    ) -> str | None:
        """'TAKE_PROFIT', 'STOP_LOSS', 'TIME_STOP' 중 하나를 반환하거나, 유지 시 None.

        익절/손절 모두 바이낸스 앱에 표시되는 것과 같은 레버리지 반영 수익률(ROE%)
        기준이다 (pnl% * leverage). 예: 5배 레버리지면 가격 +0.3%만 움직여도
        ROE +1.5%로 익절 조건을 볼 수 있다. 앱에서 보는 숫자와 봇의 판단 기준이
        동일해서 헷갈리지 않는다.

        익절은 최소 익절선(take_profit_min, ROE 기준)을 넘고 수수료 실비까지
        커버되면 바로 확정한다 (고점 대비 % 하락을 기다리는 트레일링 대신
        즉시 익절 → 회전율↑, 거래 다양화).

        손실이 깊지 않은데(-2% ROE 이내) scalp_max_hold_minutes를 넘기면 방향성 없다고
        보고 슬롯을 비운다(TIME_STOP) — 스캘핑 재설계 후 무기한 정체 문제가 실측돼 추가함.
        """
        pos = self.positions.get(symbol)
        if pos is None:
            return None

        pnl = pnl_pct(pos.entry_price, mark_price, pos.side)
        roe = pnl * pos.leverage
        margin_used = (pos.entry_price * pos.quantity) / pos.leverage if pos.leverage else pos.entry_price * pos.quantity
        side_stop_loss_pct = self._stop_loss_pct_for(pos.side, pos.entered_at)

        if roe <= -side_stop_loss_pct:
            log.info("[%s] 손절 조건 충족: ROE=%.2f%% (가격변동=%.2f%%, 레버리지=%sx)", symbol, roe, pnl, pos.leverage)
            return "STOP_LOSS"

        effective_hard_cap = self.cfg.swing_take_profit_hard_cap if swing_continuing else self.cfg.take_profit_hard_cap
        if roe >= effective_hard_cap:
            log.info(
                "[%s] 절대 상한 익절선 도달(더 이상 모멘텀과 무관하게 확정): ROE=%.2f%% 상한=%.2f%%%s",
                symbol, roe, effective_hard_cap, " (스윙확대)" if swing_continuing else "",
            )
            return "TAKE_PROFIT"

        # [2026-08-11 사용자요청] 순환매매 맛보기 — 보유시간 초과 + 이미 익절 상태(ROE 최소치
        # 이상)면 트레일링을 기다리지 않고 바로 확정한다. 손실 중이면 이 규칙은 아예 안 보고
        # 그대로 통과(아래 정상 로직으로 이어짐) — "무조건 청산"이 아니라 "익절일 때만" 강제.
        # [2026-08-11 실거래 발견/수정] 龙虾USDT 실측: 보유9.1분/ROE1.65%에서 강제확정됐는데
        # 직후 추가로 4~5%p 더 올라간 채로 놓침 — 다른 트레일링 로직처럼 "지금도 모멘텀이
        # 강하게 지속 중이면 이번 주기엔 확정을 보류"하는 가드를 추가한다. 대부분의 포지션은
        # 5분 뒤엔 이미 모멘텀이 식어있어 평소처럼 그대로 확정되고, 지금처럼 "아직 세게
        # 밀고있는" 소수 케이스만 한 사이클(다음 스캔까지) 더 지켜본다 — 전체 회전속도에는
        # 거의 영향 없음.
        if self.cfg.force_profit_exit_max_hold_min > 0:
            held_minutes = (time.time() - pos.entered_at) / 60
            if held_minutes >= self.cfg.force_profit_exit_max_hold_min and roe >= self.cfg.force_profit_exit_min_roe:
                if momentum_continuing:
                    log.info(
                        "[%s] 순환매매 강제익절 조건 충족했지만 모멘텀 지속 중 — 이번 주기 보류: "
                        "보유%.1f분 ROE=%.2f%%",
                        symbol, held_minutes, roe,
                    )
                else:
                    log.info(
                        "[%s] 순환매매 강제익절: 보유%.1f분(기준%.0f분 초과) ROE=%.2f%%(기준%.2f%% 이상)",
                        symbol, held_minutes, self.cfg.force_profit_exit_max_hold_min, roe, self.cfg.force_profit_exit_min_roe,
                    )
                    return "TAKE_PROFIT"

        fee_estimate = (pos.entry_price * pos.quantity) * self.cfg.fee_rate_roundtrip
        current_profit = profit_usdt(pos, mark_price)

        if self.total_balance and self.total_balance < self.cfg.small_profit_lock_balance_threshold:
            if roe >= self.cfg.small_profit_lock_roe and current_profit > fee_estimate:
                if not pos.armed:
                    log.info(
                        "[%s] 소액계좌 조기 수익잠금 시작: ROE=%.2f%% profit=%.2fUSDT",
                        symbol, roe, current_profit,
                    )
                pos.armed = True
            if pos.armed:
                if roe > pos.peak_pnl:
                    pos.peak_pnl = roe
                if (
                    pos.peak_pnl >= self.cfg.small_profit_lock_roe
                    and pos.peak_pnl - roe >= self.cfg.small_profit_lock_drawdown_roe
                    and current_profit > fee_estimate
                ):
                    log.info(
                        "[%s] 소액계좌 조기 수익잠금 확정: 고점ROE=%.2f%% 현재ROE=%.2f%% (%.2f%%p 하락)",
                        symbol, pos.peak_pnl, roe, pos.peak_pnl - roe,
                    )
                    return "TAKE_PROFIT"

        if (
            self.total_balance
            and self.total_balance < self.cfg.small_profit_balance_threshold
            and pos.origin == "bot"
        ):
            # 초소형 자금 단계: %가 아니라 "수수료 제외 순수익 X달러"를 절대 기준으로 즉시 익절
            # (자산을 빠르게 회전시키며 불려나가기 위함, 40달러 넘으면 아래 ROE%기준으로 전환)
            required_profit = self.cfg.small_profit_target_usdt + fee_estimate
            if current_profit >= required_profit and roe > 0:
                pos.armed = True
                log.info(
                    "[%s] 소액구간 순수익 목표(%.2fUSDT+수수료%.2fUSDT) 도달: profit=%.2fUSDT",
                    symbol, self.cfg.small_profit_target_usdt, fee_estimate, current_profit,
                )
                if roe <= self.cfg.small_profit_immediate_max_roe:
                    return "TAKE_PROFIT"
                log.info(
                    "[%s] 소액구간 수익 강함: ROE=%.2f%% > %.2f%% — 즉시익절 대신 트레일링 유지",
                    symbol, roe, self.cfg.small_profit_immediate_max_roe,
                )
        else:
            # [2026-08-06] 펌프감지 재설계 후 롱/숏 모두 같은 방식(변동폭+거래량)으로 진입하므로
            # "숏만 즉시확정"하던 예전(추세추종 시절) 비대칭을 없애고 트레일링을 양쪽에 동일 적용.
            # 72시간/300심볼 백테스트: 즉시확정(8%)보다 트레일링이 두 표본 모두에서 순이익 개선됨
            # (표본A 평균순ROE +3.10%->+3.57%, 표본B +1.61%->+1.62~1.67%, 3%p 기준).
            take_profit_min = self.cfg.short_take_profit_min if pos.side == "SHORT" else self.cfg.take_profit_min
            if roe >= take_profit_min:
                min_profit_usdt = margin_used * (take_profit_min / 100)
                # 수수료 실비(fee_estimate)만큼만 여유를 두고 확인한다.
                if current_profit >= min_profit_usdt + fee_estimate:
                    # 여기서는 armed만 표시한다 — 아래 트레일링 로직이 고점 대비 일정폭
                    # 하락하면 확정한다(살아있는 동안은 계속 태움).
                    if not pos.armed:
                        log.info(
                            "[%s] 최소 익절선(ROE %.2f%%) 도달, 트레일링 시작: ROE=%.2f%% profit=%.2fUSDT",
                            symbol, take_profit_min, roe, current_profit,
                        )
                    pos.armed = True

            if pos.armed:
                if roe > pos.peak_pnl:
                    pos.peak_pnl = roe
                effective_trail_drawdown = (
                    self.cfg.trail_drawdown_pct * self.cfg.swing_trail_drawdown_multiplier
                    if swing_continuing
                    else self.cfg.trail_drawdown_pct
                )
                if pos.peak_pnl - roe >= effective_trail_drawdown:
                    # [2026-08-10] 지금 캔들이 여전히 포지션 방향으로 크게 움직이는 중이면
                    # (진입 신호와 같은 기준: 변동폭+거래량 급증) 트레일링 확정을 이번 주기엔
                    # 보류한다 — 큰 양봉/음봉 도중에 서둘러 내려서 나머지 상승분을 놓치는
                    # 문제를 줄이기 위함. peak_pnl은 위에서 이미 갱신됐으므로 다음 주기에
                    # 모멘텀이 꺾이면 그 시점 고점 기준으로 정상적으로 확정된다. 절대 상한
                    # (take_profit_hard_cap)이 항상 위에서 무조건 확정시키므로 무한정 노출되진
                    # 않는다.
                    if momentum_continuing:
                        log.info(
                            "[%s] 트레일링 확정 조건 충족했지만 모멘텀 지속 중(변동폭+거래량 급증) — "
                            "이번 주기 확정 보류, 계속 태움: 고점ROE=%.2f%% 현재ROE=%.2f%%",
                            symbol, pos.peak_pnl, roe,
                        )
                    else:
                        log.info(
                            "[%s] 트레일링 확정: 고점ROE=%.2f%% 현재ROE=%.2f%% (%.2f%%p 하락)",
                            symbol, pos.peak_pnl, roe, pos.peak_pnl - roe,
                        )
                        return "TAKE_PROFIT"

        # [2026-08-04] 스캘핑 재설계 후 실거래에서 발견된 문제: 방향성 없는 코인은 익절(2%)도
        # 손절(4%)도 안 닿은 채 슬롯을 몇 시간이고 무기한 차지했다(TRXUSDT 약 7시간 정체 실측).
        # 스윙 시절엔 무기한 보유가 맞았지만, 지금은 빠른 회전이 전제라 손실이 깊지 않은데
        # 너무 오래 안 풀리면 슬롯을 비워준다.
        # [2026-08-07 QA 감사에서 발견/수정] 하드코딩된 -2.0% 문턱이 stop_loss_pct(-3%)보다
        # 안쪽이라, ROE가 -2.0%~-3.0% 사이(손절에는 못 미치지만 손실 중)에 낀 포지션은 이
        # TIME_STOP도, 아래 STOP_LOSS도 걸리지 않는 사각지대에 무기한 방치될 수 있었다
        # (early_exit도 EARLY_EXIT_MIN_LOSS_ROE가 STOP_LOSS_PCT와 같은 값이라 사실상 발동 안 됨).
        # 이미 손절 조건(roe <= -stop_loss_pct)은 위에서 먼저 걸러지므로, 여기 도달했다는 건
        # roe > -stop_loss_pct라는 뜻 — 그 전체 손실 구간을 시간제한 대상으로 잡아야 사각지대가 없다.
        if roe > -side_stop_loss_pct:
            held_minutes = (time.time() - pos.entered_at) / 60
            if held_minutes >= self.cfg.scalp_max_hold_minutes:
                log.info(
                    "[%s] 스캘핑 최대 보유시간(%.0f분) 초과 — 방향성 없어 슬롯 정리: ROE=%.2f%% (%.0f분 경과)",
                    symbol, self.cfg.scalp_max_hold_minutes, roe, held_minutes,
                )
                return "TIME_STOP"

        return None
