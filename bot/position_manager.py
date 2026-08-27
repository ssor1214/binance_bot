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
    # [2026-08-16] 지금 거래소 STOP_MARKET에 실제로 걸려 있는 손절폭(%). 중간 계단(stage2)이
    # 생기면서 "완전히 풀릴 때 한 번만" 재등록하던 방식으로는 계단 전환이 반영되지 않아,
    # 목표 폭과 비교해 계단마다 재등록할 수 있도록 현재 적용값을 기억한다. 0이면 미적용.
    applied_stop_loss_pct: float = 0.0
    # [2026-08-15 사용자요청] "V2 이후 실제로 스파이크 조기체결이 적용된 거래가 어느 건지
    # 알 수가 없다"는 관찰성 문제 발견 — candidate["early_entry_spike"]가 execute_entry까지는
    # 전달됐지만 포지션/거래기록엔 저장이 안 되고 있었다. 진입 시점에 여기 저장해서 청산 시
    # TradeRecord에 그대로 실어보낸다.
    early_entry_spike: bool = False
    # [2026-08-25 관측] 진입 근거를 원장까지 실어보낸다. 지금까지 원장엔 진입 점수도
    # 볼밴 관여 여부도 없어서 "순수 EMA 진입 vs 볼밴 관여 진입"의 손익 비교가 불가능했다
    # (원칙 2 판정 불가). BB_PARTICIPATION_REQUIRED 효과 검증에 직접 쓰인다.
    entry_score: float | None = None
    entry_bb_event: bool | None = None
    entry_width_expanding: bool | None = None
    entry_rsi: float | None = None
    entry_rsi_aligned: bool | None = None
    # [2026-08-25] 순방향 분할 — 2차 추가를 이미 했는지, 그때 ROE가 얼마였는지.
    # apply_average_down이 평단 변경 때문에 roe_at_* 관측값을 초기화하므로, 판정에 쓸
    # 트리거 시점 ROE는 여기 따로 남긴다.
    scale_in_done: bool = False
    scale_in_trigger_roe: float | None = None
    # [2026-08-25] 2차 체결 시각. 직후에 "작은 수익 익절"이 발동하면 2차 수수료만 내고
    # 곧바로 청산되므로(AMBIGUOUS 창 60~180초가 2차 창과 겹친다), 짧은 쿨다운을 둔다.
    scale_in_at: float = 0.0
    # [2026-08-25 관측] 실제 진입 체결가. 청산 쪽(actual_fill_exit_price)만 배선돼 있어
    # 진입 슬리피지/메이커 여부를 원장으로 검증할 수 없었다.
    actual_fill_entry_price: float | None = None
    # [2026-08-15 백테스트 검증 후 추가] 손절 유예 게이트 상태.
    # sl_defer_until: 이 시각(epoch)까지는 손절 확정을 보류한다(0이면 유예 중 아님).
    # sl_defer_start_roe: 유예를 시작한 시점의 ROE — 안전캡(추가손실 한도) 계산 기준점.
    # sl_defer_used: 포지션당 1회만 유예한다(무한정 미루기 방지). 백테스트도 거래당 1회
    #   유예만 시뮬레이션했으므로 그 설계와 일치시킨다.
    # sl_defer_prev_stop_order_id: 유예 시작 시 넓혀 재등록하기 전의 원래 STOP_MARKET id.
    sl_defer_until: float = 0.0
    sl_defer_start_roe: float = 0.0
    sl_defer_used: bool = False
    sl_defer_prev_stop_order_id: str | None = None
    # [2026-08-17 관측 전용] 손익비 복기용 계측 필드 — 청산 판단에는 일절 관여하지 않는다.
    #
    # 배경: 실현손익 보정 후 436건을 재집계했더니 순익 전체가 "봇 트레일링이 무장(armed)한
    # 69건(+8.99USDT, 승률 98.6%)"에서 나오고, 무장 못 한 367건은 -16.76USDT였다. 그런데
    # 무장 여부를 bot.log의 "트레일링 시작" 문자열로 역산할 수밖에 없어서(원장에 없음)
    # 무장률 16%라는 수치의 신뢰도가 낮았고, "거래소 트레일링이 봇보다 먼저 닫아서 무장을
    # 못 한 것"이라는 가설도 콜백폭 역산 추정으로만 뒷받침됐다.
    #
    # peak_pnl은 armed 이후에만 갱신되므로 "무장 전에 얼마나 올랐다가 닫혔는지"를 못 남긴다.
    # 그래서 진입 시점부터 무조건 누적되는 관측값을 따로 둔다.
    #
    # max_favorable_roe / max_adverse_roe: 봇이 폴링으로 실제 관측한 최고/최저 ROE.
    #   거래소가 먼저 닫으면 마지막 폴링 값에서 멈추므로, 실현 ROE와의 차이 자체가
    #   "봇이 못 본 구간"의 크기가 된다(이것도 측정 대상이다).
    # armed_at / armed_roe: 무장 시각과 그때의 ROE. 0이면 끝까지 무장 못 한 거래.
    # evaluate_calls: 진입 후 evaluate()가 몇 번 돌았는지 — 무장 실패가 "가격이 안 왔다"인지
    #   "폴링이 못 따라갔다"인지 구분하는 데 쓴다.
    max_favorable_roe: float = 0.0
    max_adverse_roe: float = 0.0
    armed_at: float = 0.0
    armed_roe: float = 0.0
    evaluate_calls: int = 0
    force_profit_extension_used: bool = False
    # [2026-08-18 진입품질 규명용 관측 전용] 진입 후 특정 시점의 ROE 스냅샷.
    #
    # 배경: 관측 190건에서 "고점 ROE가 1.5%에 못 미친 거래" 65건(34.2%)이 승률 9.2%,
    # 순익 -10.173으로 손실 전부를 만든다. 나머지 125건은 +7.291이라 이 34%만 없으면 흑자다.
    # 그런데 진입 시점 피처로는 구분이 안 된다 — 확률/우선순위/강도/speed/total_score/
    # mtf_agree/btc_mult 9개를 대조했더니 8개가 z<2로 무의미했고(확률은 불량 0.9420 vs
    # 정상 0.9409), 유일하게 유의한 명목크기(z=-2.13)는 잔고가 컸던 시기 효과로 보인다.
    #
    # 그래서 **진입 후 짧은 구간**에 판별 가능한지를 본다. 지금 원장에는 전 구간 최고/최저만
    # 있고 시간에 따른 궤적이 없어서, 30초/60초 시점 ROE를 남긴다.
    # 이 값들은 **청산 판단에 일절 쓰지 않는다.** 측정 먼저, 규칙은 그다음이다.
    # (2026-08-17에 "무조건 120/180초 후 컷"을 검증했다가 승률 -11~12%p로 기각한 이력이 있다.
    #  그때는 측정 없이 자른 것이고, 이번엔 판별 가능성부터 잰다.)
    # [2026-08-19 S2 조건부 시간컷 검증용] 120초 시점을 추가한다.
    # 근거: V2 이후 629건에서 보유 2~5분 구간이 명목당 net -0.3818%로 유일하게 크게 나쁘다
    # (~2분 +0.6307% / 5~15분 -0.1724% / 15분~ -0.3924%). 그 구간의 시작점이 120초다.
    # 60초 신호는 2026-08-19에 사전등록 기준(탐지60%/오탐20%) 미달로 정식 기각했으나,
    # 그건 **무조건 컷** 판별이었다. 이번에 재려는 것은 "120초 시점에 **아직 무장 못한**
    # 거래"만 대상으로 하는 조건부 컷이며, 무장 거래(승률 98.8%, 명목당 net +1.0566%)는
    # 절대 건드리지 않는다는 점이 다르다.
    # 이 값도 **청산 판단에 일절 쓰지 않는다.** 측정 먼저, 규칙은 그다음.
    roe_at_30s: float | None = None
    roe_at_60s: float | None = None
    roe_at_120s: float | None = None

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


def grace_stop_multiplier(cfg, entered_at: float | None) -> float:
    """진입 후 경과시간에 따른 손절폭 배수를 반환한다(단일 소스).

    [2026-08-12 사고 이력] 이 계산이 bot/main.py와 bot/position_manager.py 두 곳에 복제돼
    있었고, 한쪽만 유예를 반영해서 "거래소 주문은 넓혀뒀는데 폴링 체크는 좁은 폭으로 먼저
    손절"하는 사고가 있었다(ONEUSDT ROE -3.04% 즉시청산). 그래서 이제 두 경로가 이 함수
    하나만 쓰도록 통합한다.

    [2026-08-16 중간 계단 추가] 유예 만료 시 배수를 1.0으로 한 번에 떨어뜨리면 그 순간
    -base~-base*widen 구간의 포지션이 일제히 청산된다(실측: STOP_LOSS의 44.1%가 보유
    170~200초에 집중). stage2를 두면 계단이 하나 더 생겨 한꺼번에 죽지 않는다.

      0 ~ grace_sec         : grace_widen_mult
      grace_sec ~ stage2_sec: stage2_mult
      stage2_sec ~          : 1.0

    entered_at=None은 "지금 막 진입하는 시점"으로 보고 항상 1단계(가장 넓은 폭)를 준다.
    """
    if cfg.stop_loss_grace_sec <= 0:
        return 1.0
    if entered_at is None:
        return cfg.stop_loss_grace_widen_mult
    elapsed = time.time() - entered_at
    if elapsed < cfg.stop_loss_grace_sec:
        return cfg.stop_loss_grace_widen_mult
    # stage2가 유예시간보다 뒤에 있을 때만 중간 계단으로 동작(아니면 기존 2단계 그대로)
    if cfg.stop_loss_grace_stage2_sec > cfg.stop_loss_grace_sec and elapsed < cfg.stop_loss_grace_stage2_sec:
        return cfg.stop_loss_grace_stage2_mult
    return 1.0


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
        기준을 쓰도록 맞춘다.

        [2026-08-16] 복제로 인한 어긋남을 막기 위해 계산을 grace_stop_multiplier()로 통합했다
        (중간 계단 stage2도 여기서 함께 반영된다)."""
        base = self.cfg.short_stop_loss_pct if side == "SHORT" and self.cfg.short_stop_loss_pct > 0 else self.cfg.stop_loss_pct
        if entered_at is None:
            # 기존 동작 유지: 추적정보가 없으면 유예를 적용하지 않고 기본 폭을 쓴다.
            return base
        return base * grace_stop_multiplier(self.cfg, entered_at)

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
                # [2026-08-17] 심볼 블락 복원. 이미 만료된 항목은 버려 파일이 무한정 커지는
                # 것을 막는다(만료된 블락을 되살리면 멀쩡한 심볼이 계속 막히는 부작용도 있다).
                now = time.time()
                raw_block = data.get("symbol_blacklist_until", {}) or {}
                self.symbol_blacklist_until = {
                    str(s): float(u) for s, u in raw_block.items()
                    if isinstance(u, (int, float)) and float(u) > now
                }
                raw_streak = data.get("symbol_loss_streak", {}) or {}
                self.symbol_loss_streak = {
                    str(s): int(v) for s, v in raw_streak.items()
                    if isinstance(v, (int, float)) and int(v) > 0
                }
                if self.symbol_blacklist_until:
                    log.info("심볼 블락 복원: %d개 (%s)", len(self.symbol_blacklist_until),
                              ", ".join(f"{s} {max(0, u - now) / 60:.0f}분남음"
                                        for s, u in sorted(self.symbol_blacklist_until.items())[:5]))
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
                # [2026-08-17 실거래 점검으로 발견] 심볼 블락(손실 후 재진입 차단 / 익절 후
                # 쿨다운)이 인메모리에만 있어서 재시작마다 통째로 사라졌다. 블락은 최대
                # symbol_blacklist_cooldown_min(기본 60분)까지 유지돼야 하는데, 재시작 간격이
                # 그보다 짧으면(오늘 실측 30~50분 간격으로 4회 재시작) 차단이 무력화된다.
                # symbol_loss_timestamps는 창이 30분으로 짧아 의도적으로 영속화하지 않는다는
                # 기존 판단이 주석에 남아 있으나, 블락은 그 근거가 적용되지 않는다.
                "symbol_blacklist_until": self.symbol_blacklist_until,
                # 블락 판정의 입력이 되는 연속손실 카운터도 함께 살려야 일관된다.
                "symbol_loss_streak": self.symbol_loss_streak,
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
            # [2026-08-17 실거래 복기] 예전엔 loss_threshold와 min_loss_streak를 max()로 묶어
            # 사용해, 사용자가 loss_threshold=1로 낮춰도 min_loss_streak=3이 더 크면 실제 발동은
            # 3연속 손실 뒤로 밀렸다. 설정을 더 공격적으로 낮췄는데 보호가 오히려 늦어지는 건
            # 직관에도 맞지 않고, 열린 과제(손실 93%가 5심볼 집중)와도 정면 충돌한다.
            # 연속손실 기반 격리는 "연속 몇 번 지면 막을지"를 정하는 주 설정값
            # symbol_blacklist_loss_threshold를 우선 사용하고, 옛 min_loss_streak는 그 값이
            # 비어 있거나 0 이하일 때만 하위호환 폴백으로 본다.
            effective_blacklist_threshold = int(self.cfg.symbol_blacklist_loss_threshold)
            if effective_blacklist_threshold <= 0:
                effective_blacklist_threshold = int(self.cfg.symbol_blacklist_min_loss_streak)
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
                # [2026-08-17 실거래 복기로 발견] 예전엔 block_min이 0이어도 "0분 동안 재진입을
                # 짧게 차단합니다"를 WARNING으로 남겼다. 실제로는 cooldown_until이 now_ts와 같아
                # 아무것도 막지 않는데 로그만 보면 차단된 것처럼 읽힌다.
                # 실측(2026-08-17 야간): PORTALUSDT 2회/HUSDT 1회 이 로그가 떴는데 전부 즉시
                # 재진입이 허용됐고, 그중 PORTALUSDT는 6거래 5손실 -1.24USDT였다. 복기하는 쪽에서
                # "차단이 걸렸는데도 왜 재진입됐나"를 쫓다가 시간을 버린다.
                # SYMBOL_COOLDOWN_BLOCK_MIN=0은 8/14 사용자요청 원복값(버그 아님, .env 693행)이라
                # 동작은 그대로 두고 로그만 사실과 맞춘다.
                if self.cfg.symbol_cooldown_block_min > 0:
                    cooldown_until = now_ts + self.cfg.symbol_cooldown_block_min * 60
                    if cooldown_until > self.symbol_blacklist_until.get(symbol, 0):
                        self.symbol_blacklist_until[symbol] = cooldown_until
                    log.warning(
                        "[%s] %.0f분 내 손실 %d회 — %.0f분 동안 이 심볼 재진입을 짧게 차단합니다",
                        symbol, self.cfg.symbol_cooldown_window_min, len(loss_ts), self.cfg.symbol_cooldown_block_min,
                    )
                else:
                    log.info(
                        "[%s] %.0f분 내 손실 %d회 — 단기 차단은 비활성(SYMBOL_COOLDOWN_BLOCK_MIN=0)이라 재진입 허용",
                        symbol, self.cfg.symbol_cooldown_window_min, len(loss_ts),
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
            reentry_ctx = self.get_same_symbol_reentry_context(symbol)
            loss_streak = int(reentry_ctx["loss_streak"])
            if loss_streak > 0:
                loss_mult = self.cfg.loss_reentry_size_mult ** loss_streak
                loss_mult = max(self.cfg.loss_reentry_min_mult, min(1.0, loss_mult))
                ratio *= loss_mult
                log.info(
                    "[%s] 최근 손실 %d회 — 거래는 유지하되 재진입 비중 %.0f%% 적용",
                    symbol, loss_streak, loss_mult * 100,
                )
            if reentry_ctx["recent_reentry"]:
                prev_ratio = float(reentry_ctx["prev_ratio"])
                cap = prev_ratio * self.cfg.same_symbol_reentry_ratio_mult
                if ratio > cap:
                    log.info(
                        "[%s] %.0f분 내 재진입 감지(직전 비중 %.0f%%) — 비중을 %.0f%%로 축소",
                        symbol, self.cfg.same_symbol_reentry_window_min, prev_ratio * 100, cap * 100,
                    )
                    ratio = cap
        return ratio

    def get_same_symbol_reentry_context(self, symbol: str) -> dict[str, float | bool | int]:
        prev = self.symbol_recent_ratio.get(symbol)
        if prev is None:
            return {
                "recent_reentry": False,
                "prev_ratio": 0.0,
                "seconds_since_entry": float("inf"),
                "loss_streak": int(self.symbol_loss_streak.get(symbol, 0)),
            }
        prev_ratio, entered_at = prev
        seconds_since_entry = time.time() - entered_at
        window_sec = self.cfg.same_symbol_reentry_window_min * 60
        return {
            "recent_reentry": seconds_since_entry <= window_sec,
            "prev_ratio": float(prev_ratio),
            "seconds_since_entry": float(seconds_since_entry),
            "loss_streak": int(self.symbol_loss_streak.get(symbol, 0)),
        }

    def record_entry_ratio(self, symbol: str, ratio: float):
        """이번 진입에 실제로 쓴 비중을 기록한다 (다음 재진입 시 축소 판단용)."""
        self.symbol_recent_ratio[symbol] = (ratio, time.time())

    def track(self, symbol: str, side: str, entry_price: float, quantity: float, leverage: float = 1.0,
              origin: str = "bot", balance_at_entry: float | None = None,
              entered_at: float | None = None, stop_loss_widened: bool = False,
              early_entry_spike: bool = False,
              entry_score: float | None = None,
              entry_bb_event: bool | None = None,
              entry_width_expanding: bool | None = None,
              entry_rsi: float | None = None,
              entry_rsi_aligned: bool | None = None,
              scale_in_done: bool = True,   # [2026-08-25 불타기 방지] 기본은 "2차 없음"이 안전하다
              actual_fill_entry_price: float | None = None):
        """[2026-08-14 실측 사고] entered_at/stop_loss_widened를 명시적으로 넘길 수 있게 해서,
        재시작 시 복원되는 포지션이 실제 최초 진입시각을 유지하고(기본값인 '지금'으로 리셋되지
        않게) 유예기간(180초) 경과 여부가 재시작과 무관하게 정확히 판정되도록 한다 — 신규
        진입(execute_entry 등)은 이 인자를 안 넘겨서 기존과 동일하게 entered_at=지금, widened=False.
        early_entry_spike: [2026-08-15] 진입 시 스파이크 조기체결(aggressive fill)이 실제로
        적용됐는지 — TradeRecord까지 그대로 이어져서 사후 분석 가능하게 한다."""
        pos = TrackedPosition(symbol, side, entry_price, quantity, leverage=leverage, origin=origin)
        if entered_at is not None:
            pos.entered_at = entered_at
        pos.stop_loss_widened = stop_loss_widened
        pos.early_entry_spike = early_entry_spike
        pos.entry_score = entry_score
        pos.entry_bb_event = entry_bb_event
        pos.entry_width_expanding = entry_width_expanding
        pos.entry_rsi = entry_rsi
        pos.entry_rsi_aligned = entry_rsi_aligned
        # [2026-08-25 불타기 방지] 1차 분할이 실제로 적용되지 않았으면(전량 진입 폴백,
        # 재시작 복원 등) 2차 추가를 원천 차단한다 — 안 그러면 총 노출이 계획을 넘는다.
        pos.scale_in_done = scale_in_done
        pos.actual_fill_entry_price = actual_fill_entry_price
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

    def apply_scale_in(self, symbol: str, new_entry_price: float, new_quantity: float,
                       added_margin_usdt: float = 0.0, current_roe: float = 0.0):
        """순방향 분할 2차 체결 후 추적 정보를 갱신한다.

        [2026-08-25] apply_average_down을 쓰면 안 된다. 그건 평단이 "나빠지는" 물타기용이라
        armed / peak_pnl / max_favorable_roe / roe_at_* 를 전부 리셋하는데, 순방향 분할에
        그대로 쓰면 부작용이 생긴다:
          - armed=False 리셋 -> UNARMED_MID_HOLD_CUT(6~8분, 무장 못 하면 컷) 발동 확률 상승
          - peak_pnl=0 리셋  -> 이미 찍은 고점이 사라져 트레일링 익절이 지연
          - roe_at_* 리셋    -> 원장 판정용 관측값 소실(2차 진입 건을 사후 분석 못 함)
        그래서 관측값과 무장 상태는 보존한다.

        단 peak_pnl만은 새 평단 기준으로 다시 잡는다. 평단이 바뀌면 ROE의 기준점 자체가
        바뀌는데, 옛 기준 고점을 그대로 두면 새 기준 ROE와 비교돼 "고점 대비 하락"이
        즉시 성립해 트레일링이 헛발동한다.
        """
        pos = self.positions.get(symbol)
        if pos is None:
            return
        pos.entry_price = new_entry_price
        pos.quantity = new_quantity
        pos.total_margin_added_usdt += added_margin_usdt
        pos.peak_pnl = max(0.0, float(current_roe))
        log.info(
            "[%s] 순방향 분할 2차 반영 — 평단 %s 수량 %s (무장=%s 고점ROE=%.2f%% 유지, "
            "트레일링 기준점만 %.2f%%로 재설정)",
            symbol, new_entry_price, new_quantity, pos.armed,
            float(getattr(pos, "max_favorable_roe", 0.0) or 0.0), pos.peak_pnl,
        )

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
        # 평단가가 바뀌면 ROE의 기준점 자체가 바뀌므로 관측값도 같이 초기화한다
        # (안 그러면 옛 평단가 기준 고점이 새 기준 값과 섞여 복기가 무의미해진다).
        pos.max_favorable_roe = 0.0
        pos.max_adverse_roe = 0.0
        pos.armed_at = 0.0
        pos.armed_roe = 0.0
        pos.roe_at_30s = None
        pos.roe_at_60s = None
        pos.roe_at_120s = None
        log.info(
            "[%s] 물타기 반영(%d번째): 새 평단가=%s 수량=%s 누적추가증거금=%.2fUSDT (상한 %.2fUSDT)",
            symbol, pos.average_down_count, new_entry_price, new_quantity,
            pos.total_margin_added_usdt, pos.max_total_margin_usdt,
        )

    def evaluate(
        self, symbol: str, mark_price: float, momentum_continuing: bool = False, swing_continuing: bool = False
    ) -> str | None:
        """[2026-08-17] 관측 계측만 덧붙인 얇은 래퍼. 판단은 전부 _evaluate_inner가 하고
        여기서는 반환값을 그대로 통과시킨다 — 계측 코드가 실패해도 청산 판단이 바뀌면 안 되므로
        예외를 삼키고, 무장 시각 기록은 finally에서 하여 조기 return 경로도 빠짐없이 잡는다.

        (armed를 세우는 지점이 _evaluate_inner 안에 네 군데라 각각에 기록을 심으면 하나를
        빠뜨리기 쉽다. 진입/이탈 한 곳에서만 관측하도록 래퍼로 분리했다.)"""
        pos = self.positions.get(symbol)
        if pos is None:
            return self._evaluate_inner(symbol, mark_price, momentum_continuing, swing_continuing)
        was_armed = pos.armed
        try:
            roe = pnl_pct(pos.entry_price, mark_price, pos.side) * pos.leverage
            pos.evaluate_calls += 1
            if roe > pos.max_favorable_roe:
                pos.max_favorable_roe = roe
            if roe < pos.max_adverse_roe:
                pos.max_adverse_roe = roe
            # 진입 후 경과시간이 기준선을 처음 넘은 폴링에서 한 번만 찍는다(폴링 주기가
            # 약 5초라 30/60초에 정확히 맞지 않는다 — 그 직후 첫 관측값을 쓴다).
            elapsed = time.time() - pos.entered_at
            if pos.roe_at_30s is None and elapsed >= 30:
                pos.roe_at_30s = roe
            if pos.roe_at_60s is None and elapsed >= 60:
                pos.roe_at_60s = roe
            if pos.roe_at_120s is None and elapsed >= 120:
                pos.roe_at_120s = roe
        except Exception:  # 계측 실패가 청산 판단을 막아선 안 된다
            roe = None
        try:
            return self._evaluate_inner(symbol, mark_price, momentum_continuing, swing_continuing)
        finally:
            if roe is not None and pos.armed and not was_armed and not pos.armed_at:
                pos.armed_at = time.time()
                pos.armed_roe = roe

    def _evaluate_inner(
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
            force_exit_hold_min = self.cfg.force_profit_exit_max_hold_min
            if (
                getattr(self.cfg, "force_profit_trend_exception_enabled", True)
                and not pos.force_profit_extension_used
                and held_minutes >= self.cfg.force_profit_exit_max_hold_min
                and roe >= max(self.cfg.force_profit_exit_min_roe, getattr(self.cfg, "force_profit_trend_exception_min_roe", 2.5))
                and momentum_continuing
                and swing_continuing
            ):
                force_exit_hold_min += max(0.0, float(getattr(self.cfg, "force_profit_trend_exception_extend_min", 6.0)))
                pos.force_profit_extension_used = True
                log.info(
                    "[%s] 강한 3분봉 추세 예외 — 순환 강제익절 1회 %.1f분 연장: 보유%.1f분 ROE=%.2f%%",
                    symbol,
                    max(0.0, float(getattr(self.cfg, "force_profit_trend_exception_extend_min", 6.0))),
                    held_minutes,
                    roe,
                )
            if held_minutes >= force_exit_hold_min and roe >= self.cfg.force_profit_exit_min_roe:
                if momentum_continuing:
                    log.info(
                        "[%s] 순환매매 강제익절 조건 충족했지만 모멘텀 지속 중 — 이번 주기 보류: "
                        "보유%.1f분 ROE=%.2f%%",
                        symbol, held_minutes, roe,
                    )
                else:
                    log.info(
                        "[%s] 순환매매 강제익절: 보유%.1f분(기준%.0f분 초과) ROE=%.2f%%(기준%.2f%% 이상)",
                        symbol, held_minutes, force_exit_hold_min, roe, self.cfg.force_profit_exit_min_roe,
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
            base_take_profit_min = self.cfg.short_take_profit_min if pos.side == "SHORT" else self.cfg.take_profit_min
            entry_fee = self.cfg.fee_rate_maker if getattr(self.cfg, "limit_entry_enabled", False) else self.cfg.fee_rate_taker
            exit_fee = self.cfg.fee_rate_maker if getattr(self.cfg, "limit_exit_enabled", False) else self.cfg.fee_rate_taker
            fee_floor_roe = (entry_fee + exit_fee) * 100 * max(pos.leverage, 1.0)
            take_profit_min = max(base_take_profit_min, fee_floor_roe + max(0.0, getattr(self.cfg, "min_net_take_profit_roe", 0.0)))
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
            if (
                getattr(self.cfg, "stagnation_time_stop_enabled", True)
                and not pos.armed
                and held_minutes >= max(0.0, float(getattr(self.cfg, "stagnation_time_stop_min_hold_min", 10.0)))
            ):
                stagnation_min_roe = float(getattr(self.cfg, "stagnation_time_stop_min_roe", -1.0))
                stagnation_max_roe = float(getattr(self.cfg, "stagnation_time_stop_max_roe", 0.6))
                if stagnation_min_roe <= roe <= stagnation_max_roe:
                    log.info(
                        "[%s] 장기 정체 포지션 정리: 보유%.1f분 ROE=%.2f%% (허용 %.2f%%~%.2f%%)",
                        symbol,
                        held_minutes,
                        roe,
                        stagnation_min_roe,
                        stagnation_max_roe,
                    )
                    return "TIME_STOP"
            if held_minutes >= self.cfg.scalp_max_hold_minutes:
                log.info(
                    "[%s] 스캘핑 최대 보유시간(%.0f분) 초과 — 방향성 없어 슬롯 정리: ROE=%.2f%% (%.0f분 경과)",
                    symbol, self.cfg.scalp_max_hold_minutes, roe, held_minutes,
                )
                return "TIME_STOP"

        return None
