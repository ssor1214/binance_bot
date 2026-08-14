import logging
import json
import os
import sys
import time
from datetime import date, datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Optional

from .config import Config
from .exchange import Exchange, parse_ban_wait_seconds
from .indicators import add_indicators
from .position_manager import PositionManager, net_profit_usdt, profit_usdt
from .trade_ledger import (
    TradeRecord,
    append_trade_record,
    infer_open_position_origin,
    load_trade_records,
    mark_bot_position_open,
    mark_position_closed,
    strategy_config_snapshot,
)
from .ev_analysis import analyze_by_symbol, negative_ev_segments
from .ws_client import FileBackedFillTracker, FileBackedKlineCache, MultiFileBackedKlineCache
from .strategy import btc_alignment_multiplier, btc_short_term_momentum_opposes, detect_reversal, generate_signal_with_probability, immediate_momentum_ok, is_momentum_continuing, is_swing_continuing, mtf_trend_alignment, pnl_pct, quick_profit_score, signal_strength, volume_direction_ok
from .telegram_notifier import TelegramNotifier

LOG_DIR = Path(__file__).resolve().parent.parent / "logs"
LOG_DIR.mkdir(exist_ok=True)

# [2026-08-10 세 번째 WS 실거래 사고에서 발견한 더 근본적인 문제] python-binance 라이브러리의
# read loop 버그가 재연결 실패로 끝나는 게 아니라, CPU를 붙잡고 도는 tight loop가 돼서
# **프로세스 자체가 응답불능**이 되는 걸 실측했다(터미널 입력도 안 먹히고 Ctrl+C도 안 들음).
# 이러면 run_forever.py의 감시 스크립트조차 `subprocess.run()`이 리턴되길 무한정 기다리기만
# 하고 아무것도 못 한다 — 유일한 해법은 "봇이 살아있다"는 걸 외부에서 확인할 방법(하트비트
# 파일)을 만들고, 감시 스크립트가 그게 너무 오래 안 갱신되면 OS 레벨로 강제종료(kill)하는 것.
# 프로세스를 강제로 죽이는 건 내부에서 뭘 하고 있든(GIL을 붙잡은 tight loop라도) 항상 통한다.
HEARTBEAT_PATH = LOG_DIR / "heartbeat.txt"

# [2026-08-10] WS 워커(bot/ws_worker.py)가 남기는 캐시/하트비트 파일 경로 — 반드시
# ws_worker.py가 같은 역할/샤드 조합에 대해 계산하는 경로와 동일해야 한다(양쪽이 서로
# 다른 파일을 보면 당연히 아무 데이터도 못 읽는다). 단일 워커(샤딩 없음)일 때 쓰는 기본 경로.
WS_WORKER_CACHE_PATH = LOG_DIR / "ws_worker_cache.json"
WS_WORKER_STATUS_PATH = LOG_DIR / "ws_worker_status.json"
WS_WORKER_HEARTBEAT_PATH = LOG_DIR / "ws_worker_heartbeat.txt"


def _ws_worker_paths(role: str, shard_index: int, shard_count: int) -> tuple[Path, Path]:
    """[2026-08-10, Codex 제안 반영] ws_worker.py가 역할/샤드에 따라 계산하는 캐시/하트비트
    경로 규칙을 여기서도 그대로 재현한다 — 두 파일이 절대 어긋나면 안 되므로, ws_worker.py의
    경로 계산(_suffix) 로직과 반드시 동기화해서 수정할 것."""
    suffix = ""
    if role == "market" and shard_count > 1:
        suffix = f"_market{shard_index}"
    elif role == "user":
        suffix = "_user"
    return LOG_DIR / f"ws_worker_cache{suffix}.json", LOG_DIR / f"ws_worker_heartbeat{suffix}.txt"


def _ws_worker_status_path(role: str, shard_index: int, shard_count: int) -> Path:
    suffix = ""
    if role == "market" and shard_count > 1:
        suffix = f"_market{shard_index}"
    elif role == "user":
        suffix = "_user"
    return LOG_DIR / f"ws_worker_status{suffix}.json"


def write_heartbeat() -> None:
    """[2026-08-10] 메인 루프가 살아서 진행 중임을 파일에 기록한다. run_forever.py가 이
    파일의 수정시각을 감시해서, 너무 오래 안 갱신되면(=메인 루프가 멈췄다고 판단) 프로세스를
    강제종료 후 재시작한다. 하트비트 기록 자체가 실패해도(디스크 문제 등) 매매 로직에
    영향을 주면 안 되므로 예외를 삼킨다."""
    try:
        HEARTBEAT_PATH.write_text(str(time.time()), encoding="utf-8")
    except Exception:
        log.exception("하트비트 기록 실패(무시하고 계속 진행)")

# [2026-08-09] run_forever.py가 "중복 인스턴스라 스스로 물러난 것"과 "진짜 크래시"를 구분해
# 재시작 대기시간을 다르게 주도록 하는 전용 종료코드. 일반 미처리 예외로 인한 종료는 파이썬
# 기본값(1)이나 트레이스백에 의해 다른 코드가 나오므로, 이 값과 겹치지 않게 특이한 숫자로 둔다.
DUPLICATE_INSTANCE_EXIT_CODE = 78

# [2026-08-09] 거래 원장(trade_ledger)에 기록할 코드 버전 식별자. git rev를 자동으로 읽어오게
# 만들면 이 리포가 git 저장소가 아닐 때(현재 상태) 에러가 나므로, 수동 갱신하는 날짜 태그로
# 둔다 — 큰 전략 변경이 있을 때만 갱신하면 충분하다.
BOT_VERSION = "2026-08-09-protection-fallback-v1"

# [2026-08-10 발견/수정] logging.basicConfig()가 모듈 최상단에서 무조건 실행되면, 이 모듈을
# import만 해도(테스트 스위트 전체가 `from bot.main import ...`를 함) 실제 실거래 로그 파일
# (logs/bot.log)에 핸들러가 붙어버린다 — 그 결과 `python -m unittest`를 돌릴 때마다 테스트용
# 가짜 심볼(TESTUSDT)/모의 예외 로그가 실제 운영 로그에 섞여 들어가, 실거래 사고 조사 중
# 로그를 읽다가 테스트 잔재와 실제 이벤트를 구분 못 하는 혼란을 실제로 겪었다. `python -m
# bot.main`으로 실행될 때만(__name__ == "__main__") 파일 핸들러를 붙이도록 아래로 옮긴다 —
# 테스트 등 import 전용 상황에서는 로그가 콘솔에도 안 나가고 조용히 무시된다(NullHandler).
logging.getLogger().addHandler(logging.NullHandler())
log = logging.getLogger("bot.main")


def _configure_logging_for_live_process() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[
            logging.StreamHandler(),
            RotatingFileHandler(LOG_DIR / "bot.log", maxBytes=5_000_000, backupCount=5, encoding="utf-8"),
        ],
        force=True,  # 위에서 이미 만든 root logger 기본 설정(NullHandler)을 덮어씀
    )


def analyze_hourly_performance(ex: Exchange, cfg: Config, signal_symbol_count: int) -> tuple[str, dict]:
    """최근 1시간 실현손익을 분석해서, 진입 기준/손절선을 조이거나 풀자는 제안이 필요한지 판단한다.
    변경 제안이 없어도 항상 (진단문구, {}) 를 반환한다 — 실제 적용은 텔레그램에서
    사용자가 승인해야만 이뤄진다.

    실현손익(stats)은 계좌 전체의 REALIZED_PNL 내역이라 봇이 직접 진입한 거래뿐 아니라
    사용자가 앱에서 직접 매매한 것도 자동으로 포함된다. signal_symbol_count는 반대로
    "거래로 이어지지 않은 코인"까지 포함한 숫자다 — 매 주기 스캔에서 신호+확률 기준을
    통과한(슬롯 부족/블랙리스트 등으로 실제 진입은 못 했을 수도 있는) 코인 개수를
    1시간 동안 누적해서 센 것이라, 시장 전체에 기회가 얼마나 있었는지 보여준다.

    거래 0건이거나 3건 미만이면 표본이 너무 적어 판단을 보류하고 완화만 검토한다.
    3건 이상이면 승률뿐 아니라 평균 익절/평균 손절 크기(비대칭 여부)까지 같이 보고,
    승률은 괜찮은데 손실 1건이 이익 여러 건을 깎아먹는 구간(수수료 대비 이익이 작은 경우)도
    잡아내서 손절선(STOP_LOSS_PCT, 3~6% 안에서만) 조정까지 제안할 수 있다.
    """
    stats = ex.get_income_history_stats(hours=1.0)
    trades = stats["trades"]
    signal_note = f"(참고: 이번 1시간 신호 감지 코인 {signal_symbol_count}개 — 실제 거래 안 한 코인 포함)"

    if trades == 0:
        diagnosis = f"최근 1시간 거래 0건이에요. 진입 기준이 너무 빡빡할 수 있어요. {signal_note}"
        changes = {}
        new_prob = round(max(0.55, cfg.min_entry_probability - 0.03), 2)
        if new_prob != cfg.min_entry_probability:
            changes["MIN_ENTRY_PROBABILITY"] = new_prob
        new_short = round(max(0.60, cfg.short_min_entry_probability - 0.03), 2)
        if new_short != cfg.short_min_entry_probability:
            changes["SHORT_MIN_ENTRY_PROBABILITY"] = new_short
        new_taker = round(max(0.50, cfg.taker_imbalance_threshold - 0.02), 2)
        if new_taker != cfg.taker_imbalance_threshold:
            changes["TAKER_IMBALANCE_THRESHOLD"] = new_taker
        return diagnosis, changes

    if trades < 3:
        diagnosis = (
            f"최근 1시간 {trades}건뿐이라 판단하기엔 표본이 적어요 "
            f"(손익 {stats['realized_pnl']:+.2f}USDT). 조금 더 지켜볼게요. {signal_note}"
        )
        return diagnosis, {}

    win_rate = stats["wins"] / trades * 100
    avg_win = stats["avg_win"]
    avg_loss = abs(stats["avg_loss"])  # 음수를 절대값으로
    asymmetry = (avg_loss / avg_win) if avg_win > 0 else None  # 손실이 이익의 몇 배인지

    if win_rate < 45:
        diagnosis = (
            f"최근 1시간 {trades}건, 승률 {win_rate:.0f}% (손익 {stats['realized_pnl']:+.2f}USDT). "
            f"신호 품질이 낮아요 — 기준을 더 엄격하게 조이는 걸 제안해요. {signal_note}"
        )
        changes = {}
        new_prob = round(min(0.90, cfg.min_entry_probability + 0.05), 2)
        if new_prob != cfg.min_entry_probability:
            changes["MIN_ENTRY_PROBABILITY"] = new_prob
        new_short = round(min(0.92, cfg.short_min_entry_probability + 0.05), 2)
        if new_short != cfg.short_min_entry_probability:
            changes["SHORT_MIN_ENTRY_PROBABILITY"] = new_short
        if not cfg.require_two_candle_confirmation:
            changes["REQUIRE_TWO_CANDLE_CONFIRMATION"] = True
        return diagnosis, changes

    if win_rate >= 60 and stats["realized_pnl"] > 0:
        diagnosis = (
            f"최근 1시간 {trades}건, 승률 {win_rate:.0f}% (손익 {stats['realized_pnl']:+.2f}USDT). "
            f"기준이 잘 맞고 있어요 — 살짝 완화해서 거래 빈도를 늘려볼까요? {signal_note}"
        )
        changes = {}
        new_prob = round(max(0.55, cfg.min_entry_probability - 0.02), 2)
        if new_prob != cfg.min_entry_probability:
            changes["MIN_ENTRY_PROBABILITY"] = new_prob
        return diagnosis, changes

    # 승률은 45~60% 애매한 구간 — 승률 자체보다 "손실 1건의 크기"가 문제인 경우가 많다.
    # 평균 손절이 평균 익절의 1.5배를 넘으면(비대칭), 승률이 나쁘지 않아도 손익은 마이너스가 되기 쉽다.
    if asymmetry is not None and asymmetry >= 1.5 and stats["realized_pnl"] <= 0:
        diagnosis = (
            f"최근 1시간 {trades}건, 승률 {win_rate:.0f}% (손익 {stats['realized_pnl']:+.2f}USDT, "
            f"평균익절 {avg_win:+.2f}/평균손절 {-avg_loss:.2f}USDT). "
            f"승률은 괜찮은데 손실 1건이 너무 커요 — 손절선을 좁혀서 손실 크기를 줄이는 걸 제안해요. {signal_note}"
        )
        changes = {}
        new_sl = round(max(3.0, cfg.stop_loss_pct - 0.5), 2)
        if new_sl != cfg.stop_loss_pct:
            changes["STOP_LOSS_PCT"] = new_sl
        if cfg.profit_hold_reversal_votes > 1:
            changes["PROFIT_HOLD_REVERSAL_VOTES"] = cfg.profit_hold_reversal_votes - 1  # 익절도 더 빨리 잠그기
        return diagnosis, changes

    diagnosis = (
        f"최근 1시간 {trades}건, 승률 {win_rate:.0f}% (손익 {stats['realized_pnl']:+.2f}USDT). "
        f"특별히 조정할 정도는 아니에요. {signal_note}"
    )
    return diagnosis, {}


BTC_TREND_TIMEFRAMES = ("1h", "4h", "12h", "1d", "3d")


def analyze_btc_multi_timeframe_trend(ex: Exchange, cfg: Config) -> dict:
    """[2026-08-11 사용자요청] "오늘 비트가 많이 떨어져서 알트가 급락했다" — BTC 흐름을
    1h/4h/12h/1d/3d로 나눠서 보고, 텔레그램으로 30분마다 요약해서 알려준다(자동 조치는
    하지 않음, 참고용 정보 제공만). 각 시간대별로 EMA(fast/slow) 정배열/역배열로 추세를,
    조회한 캔들 구간의 시가 대비 종가 변화율로 강도를 함께 본다."""
    results = {}
    for interval in BTC_TREND_TIMEFRAMES:
        try:
            df = ex.get_klines("BTCUSDT", limit=60, interval=interval)
            df = add_indicators(df, cfg)
        except Exception:
            results[interval] = None
            continue
        curr = df.iloc[-1]
        first_close = df["close"].iloc[0]
        change_pct = ((curr["close"] / first_close) - 1) * 100 if first_close else 0.0
        trend = "상승" if curr["ema_fast"] > curr["ema_slow"] else "하락"
        results[interval] = {"trend": trend, "change_pct": change_pct, "close": curr["close"]}
    return results


# [2026-08-12 사용자요청] "순환매매로 전환한 뒤 전체 누적 승률/손익"을 30분 리포트에
# 매번 포함시키기 위한 고정 기준시각. 이 시각 이후 진입한 봇 거래만 "순환매매 이후"로 집계한다.
ROTATION_TRADING_START_TS = datetime(2026, 8, 11, 10, 53, 48).timestamp()


def _read_bot_ledger_rows(ledger_path, since_ts: float = 0.0) -> list[dict]:
    """trade_ledger.jsonl에서 origin=bot(수동매매 제외)이고 entered_at >= since_ts인 행만 읽는다."""
    import json as _json

    rows = []
    try:
        with open(ledger_path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    r = _json.loads(line)
                except Exception:
                    continue
                if r.get("origin", "bot") != "bot":
                    continue
                if r.get("entered_at", 0) >= since_ts:
                    rows.append(r)
    except Exception:
        return []
    return rows


def _win_rate_pct(rows: list[dict]) -> float:
    if not rows:
        return 0.0
    wins = sum(1 for r in rows if (r.get("estimated_pnl_usdt", 0) or 0) > 0)
    return wins / len(rows) * 100


def analyze_recent_trade_review(
    ex: Exchange, cfg: Config, window_sec: float,
    ledger_path: str = "logs/trade_ledger.jsonl", ws_restart_count: int = 0,
) -> tuple[str, dict]:
    """[2026-08-11/12 사용자요청] 30분마다 다음을 텔레그램으로 보고한다: (1) 최근 구간
    총거래수/LONG/SHORT 진입수 (2) LONG/SHORT/전체 승률 (3) 최근 구간 손익(USDT)
    (4) 최근 손실거래를 1분봉으로 재조회해 청산후15분 내 회복여부 확인(휩쏘성 조기청산
    의심 시 조정안 제안) (5) 순환매매(ROTATION_TRADING_START_TS~) 전체 누적 승률/손익
    (6) WS 재시작 횟수 + 하트비트(생존) 상태. 뚜렷한 패턴이 확인되면 구체적 파라미터
    조정안을 changes로 반환한다 — 실제 적용은 여기서 하지 않고, 호출부가
    tg.propose_tuning()으로 승인 버튼과 함께 보내서 사용자가 승인 누르거나 직접 요청할
    때만 이뤄진다(자동 적용 없음)."""
    from pathlib import Path as _Path

    ledger_path = _Path(ledger_path)
    now = time.time()
    start = now - window_sec
    rows = _read_bot_ledger_rows(ledger_path, since_ts=start)
    changes: dict = {}

    if not rows:
        recent_block = f"🔍 최근 {window_sec/60:.0f}분 복기 — 거래 없음"
    else:
        def pnl(r):
            return r.get("estimated_pnl_usdt", 0) or 0

        losses = [r for r in rows if pnl(r) <= 0]
        net = sum(pnl(r) for r in rows)
        win_rate = _win_rate_pct(rows)
        long_rows = [r for r in rows if r.get("side") == "LONG"]
        short_rows = [r for r in rows if r.get("side") == "SHORT"]
        long_win_rate = _win_rate_pct(long_rows)
        short_win_rate = _win_rate_pct(short_rows)

        lines = [
            f"🔍 최근 {window_sec/60:.0f}분 복기 — 총{len(rows)}건 (LONG {len(long_rows)}/SHORT {len(short_rows)})",
            f"승률: LONG {long_win_rate:.0f}% / SHORT {short_win_rate:.0f}% / 전체 {win_rate:.0f}%",
            f"손익: {net:+.2f}USDT",
        ]

        sample_losses = sorted(losses, key=lambda r: r["entered_at"])[-5:]
        recovered, checked = 0, 0
        for trade in sample_losses:
            try:
                symbol, side = trade["symbol"], trade["side"]
                entry_price, exited_at = trade["entry_price"], trade["exited_at"]
                raw = ex.client.futures_klines(
                    symbol=symbol, interval="1m",
                    startTime=int(exited_at * 1000), endTime=int((exited_at + 900) * 1000), limit=20,
                )
                if not raw:
                    continue
                checked += 1
                if side == "LONG":
                    if max(float(k[2]) for k in raw) > entry_price:
                        recovered += 1
                else:
                    if min(float(k[3]) for k in raw) < entry_price:
                        recovered += 1
            except Exception:
                continue

        if losses:
            avg_loss = sum(pnl(r) for r in losses) / len(losses)
            lines.append(f"손실 {len(losses)}건 평균 {avg_loss:+.3f}USDT")
        if checked >= 3 and recovered / checked >= 0.6:
            lines.append(f"⚠️ 손실 {checked}건 중 {recovered}건이 청산 후 15분 내 가격 회복 — 휩쏘성 조기청산 의심")
            lines.append("제안: 진입직후 손절 유예 확장배수를 한 단계 더 넓히기")
            changes["STOP_LOSS_GRACE_WIDEN_MULT"] = round(min(3.0, cfg.stop_loss_grace_widen_mult + 0.25), 2)
        elif checked > 0:
            lines.append(f"손실 {checked}건 중 {recovered}건 회복 — 개선 필요 뚜렷하지 않음")

        recent_block = "\n".join(lines)

    # (5) 순환매매 전체 누적
    rotation_rows = _read_bot_ledger_rows(ledger_path, since_ts=ROTATION_TRADING_START_TS)
    if rotation_rows:
        rotation_net = sum((r.get("estimated_pnl_usdt", 0) or 0) for r in rotation_rows)
        rotation_wr = _win_rate_pct(rotation_rows)
        rotation_line = f"📊 순환매매 누적({len(rotation_rows)}건): 승률 {rotation_wr:.0f}%, 손익 {rotation_net:+.2f}USDT"
    else:
        rotation_line = "📊 순환매매 누적: 기록 없음"

    # (6) WS 재시작 + 하트비트
    heartbeat_age = None
    try:
        heartbeat_age = time.time() - float(HEARTBEAT_PATH.read_text(encoding="utf-8").strip())
    except Exception:
        pass
    hb_status = "정상" if (heartbeat_age is not None and heartbeat_age < 120) else "확인필요"
    health_line = f"⚙️ WS 재시작 {ws_restart_count}회 / 하트비트 {hb_status}"

    diagnosis = "\n\n".join([recent_block, rotation_line, health_line])
    return diagnosis, changes


def format_btc_trend_digest(results: dict) -> str:
    """analyze_btc_multi_timeframe_trend()의 결과를 텔레그램 메시지로 포맷한다."""
    lines = ["📉 BTC 멀티타임프레임 흐름 (30분 주기)"]
    down_count, up_count = 0, 0
    for interval in BTC_TREND_TIMEFRAMES:
        r = results.get(interval)
        if r is None:
            lines.append(f"- {interval}: 조회 실패")
            continue
        emoji = "🔴" if r["trend"] == "하락" else "🟢"
        lines.append(f"- {interval}: {emoji} {r['trend']} ({r['change_pct']:+.2f}%)")
        if r["trend"] == "하락":
            down_count += 1
        else:
            up_count += 1
    if down_count >= 4:
        lines.append("→ 대부분 시간대가 하락 추세 — SHORT쪽이 유리한 환경일 수 있어요(참고용, 자동 조치 없음).")
    elif up_count >= 4:
        lines.append("→ 대부분 시간대가 상승 추세 — LONG쪽이 유리한 환경일 수 있어요(참고용, 자동 조치 없음).")
    else:
        lines.append("→ 시간대별로 방향이 엇갈려요 — 뚜렷한 한쪽 편향은 아닌 것 같아요.")
    return "\n".join(lines)


def sync_existing_positions(ex: Exchange, pm: PositionManager):
    """봇 재시작 시 거래소에 이미 열려있는 포지션을 한 번의 호출로 복원한다."""
    for pos in ex.get_open_positions():
        symbol = pos["symbol"]
        quantity = abs(pos["amount"])
        origin = infer_open_position_origin(symbol, pos["side"], pos["entry_price"], quantity)
        pm.track(symbol, pos["side"], pos["entry_price"], quantity, leverage=pos.get("leverage", 1.0), origin=origin)
        log.info("기존 포지션 복원: %s origin=%s", pos, origin)


def is_blocked_trading_hour(cfg: Config) -> bool:
    """1) 유동성이 얇아 변동성 대비 승률이 나쁜 시간대(UTC)에는 신규 진입을 쉰다."""
    return datetime.now(timezone.utc).hour in cfg.blocked_hours_utc


def passes_expected_value_filter(cfg: Config, pm: PositionManager) -> bool:
    """[2026-08-09] 계좌 소진 방지 강화 요청: 수수료까지 반영한 기대값이 구조적으로 마이너스면
    신규 진입을 잠시 멈춘다. 신호 하나하나의 승패를 예측하는 동적 확률모델은 예전에 "승패와
    상관관계 없음"이 확인된 바 있어(2026-08-05 실거래 20건 분석) 신뢰하지 않는다. 대신 지금까지
    실측된 봇 자체 롤링 승률과, 구조적으로 고정된 손익폭(익절 최소선 vs 손절폭)으로 포트폴리오
    단위 기대값을 계산하는 보수적인 방식을 쓴다.

    기대값(ROE 기준) = 승률 * 평균승리폭 - (1-승률) * 손절폭 - 왕복수수료(레버리지 반영)
    평균승리폭은 실측 대신 익절 최소선(take_profit_min)을 보수적으로 사용한다(실제 트레일링은
    더 벌 때가 많으므로 과소평가 방향 — 필터가 너무 낙관적으로 통과시키지 않도록 안전마진)."""
    if pm.total_trades < cfg.ev_filter_min_sample:
        return True  # 표본이 아직 부족하면(막 시작 등) 판단을 보류하고 통과시킨다
    win_rate = pm.wins / pm.total_trades
    avg_win_roe = cfg.take_profit_min
    avg_loss_roe = cfg.stop_loss_pct
    fee_roundtrip_roe = cfg.fee_rate_roundtrip * 100 * cfg.leverage_max
    ev = win_rate * avg_win_roe - (1 - win_rate) * avg_loss_roe - fee_roundtrip_roe
    return ev > 0


def record_trade_ledger(cfg: Config, pos, symbol: str, exit_reason: str, exit_price: float, result_pnl: float, result_pnl_usdt: float, ex: Exchange | None = None):
    """[2026-08-09] 청산될 때마다 거래 원장(JSONL)에 상세 레코드를 남긴다. 원장 기록은 부가
    기능이라 실패해도 실거래 로직에 영향을 주면 안 되므로 여기서 예외를 전부 삼킨다
    (append_trade_record 자체도 내부적으로 삼키지만, 레코드 생성 단계 오류까지 방어).

    [2026-08-10] ex가 주어지고 User Data Stream 체결 캐시(FillTracker)가 연결돼 있으면
    실제 청산 체결가/수수료로 actual_fill_exit_price/commission_usdt를 채운다 — cfg.ws_user_data_enabled가
    꺼져 있거나(기본값) 아직 체결 이벤트가 안 왔으면 ex.get_last_fill()이 None을 반환해
    기존처럼 mark price 추정치만 남는다(하위호환, 항상 안전한 폴백)."""
    try:
        now = time.time()
        actual_fill_exit_price = None
        commission_usdt = None
        commission_asset = None
        slippage_pct = None
        if ex is not None:
            fill = ex.get_last_fill(symbol)
            if fill is not None and fill.avg_price > 0:
                actual_fill_exit_price = fill.avg_price
                commission_usdt = fill.commission
                commission_asset = fill.commission_asset
                slippage_pct = (fill.avg_price - exit_price) / exit_price * 100 if exit_price else None
        record = TradeRecord(
            symbol=symbol, side=pos.side, origin=pos.origin, entry_reason="PUMP_SIGNAL",
            exit_reason=exit_reason, entry_price=pos.entry_price, exit_price=exit_price,
            quantity=abs(pos.quantity), leverage=pos.leverage,
            entered_at=pos.entered_at, exited_at=now, held_seconds=now - pos.entered_at,
            estimated_pnl_pct=result_pnl, estimated_pnl_usdt=result_pnl_usdt,
            bot_version=BOT_VERSION, config_snapshot=strategy_config_snapshot(cfg),
            actual_fill_exit_price=actual_fill_exit_price, commission_usdt=commission_usdt,
            commission_asset=commission_asset, slippage_pct=slippage_pct,
        )
        append_trade_record(record)
        mark_position_closed(symbol)
    except Exception:
        log.exception("[%s] 거래 원장 레코드 생성 실패 (거래 자체는 정상 처리됨)", symbol)


def compute_negative_ev_symbols(cfg: Config) -> set[str]:
    """[2026-08-09] 거래 원장에 쌓인 봇 자체(origin=bot) 거래만으로 심볼별 기대값을 계산해,
    표본이 충분한데도 구조적으로 마이너스인 심볼 집합을 반환한다. 사용자 수동매매는 봇의
    신호 품질과 무관하므로 반드시 제외한다. 원장 파일이 없거나 읽기 실패하면(막 시작한
    상태 등) 빈 집합을 반환해 아무것도 막지 않는다 — 표본 부족 시 임의로 막지 않는다는
    원칙과 동일하다."""
    try:
        records = [r for r in load_trade_records() if r.get("origin") == "bot"]
        if not records:
            return set()
        segments = analyze_by_symbol(records, min_sample=cfg.ev_filter_min_sample, fee_rate_roundtrip_pct=cfg.fee_rate_roundtrip * 100 * cfg.leverage_max)
        return {s.key for s in negative_ev_segments(segments)}
    except Exception:
        log.exception("심볼별 기대값 분석 실패 — 이번 주기는 제외 없이 진행")
        return set()


def is_near_funding_settlement(cfg: Config) -> bool:
    """2) 펀딩비 정산(UTC 00/08/16시) 전후 급변동을 피해 그 시간대엔 신규 진입을 쉰다."""
    now = datetime.now(timezone.utc)
    minutes_of_day = now.hour * 60 + now.minute
    settlement_points = (0, 8 * 60, 16 * 60, 24 * 60)  # 24*60은 자정 넘어가는 경우 대비
    return any(abs(minutes_of_day - p) <= cfg.funding_avoid_minutes for p in settlement_points)


def minutes_before_next_funding_settlement(cfg: Config) -> float:
    """다음 펀딩비 정산(UTC 00/08/16/24시)까지 남은 분(分)을 반환한다."""
    now = datetime.now(timezone.utc)
    seconds_of_day = now.hour * 3600 + now.minute * 60 + now.second
    settlement_points_sec = (0, 8 * 3600, 16 * 3600, 24 * 3600)
    diffs = [(p - seconds_of_day) / 60 for p in settlement_points_sec if p >= seconds_of_day]
    return min(diffs) if diffs else 0.0


def _btc_move_pct(df, lookback_candles: int) -> float | None:
    """최근 lookback_candles+1개 캔들 구간의 시작~끝 종가 변동률(절대값 %)을 계산.
    데이터가 부족하면 None을 반환한다."""
    if len(df) < lookback_candles + 1:
        return None
    recent = df.tail(lookback_candles + 1)
    start_price = float(recent.iloc[0]["close"])
    end_price = float(recent.iloc[-1]["close"])
    if start_price <= 0:
        return None
    return abs((end_price - start_price) / start_price * 100)


def is_btc_unstable(ex: Exchange, cfg: Config) -> bool:
    """3) 비트코인이 불안정하면(알트코인은 BTC를 따라가는 경향이 강함) 이번 주기엔 신규
    진입을 쉰다. 개별 코인 신호가 좋아도, 시장 전체가 BTC發 충격으로 흔들리는 중이면
    진입 직후 같이 끌려갈 위험이 커서 방향과 무관하게 일단 지켜본다.

    두 가지 서로 다른 창(window)을 모두 확인한다 — 둘 중 하나라도 걸리면 차단:
    ① 짧은 창(btc_lookback_candles): 순간적인 급락/급등(패닉셀/숏스퀴즈) 감지.
    ② [2026-08-10 사용자요청] 긴 창(btc_macro_lookback_candles): 짧은 창 하나로는 못 잡는
    "천천히, 하지만 꾸준히" 지속되는 하락(매크로 붕괴) 감지 — 순간 급락은 아니라서 ①은
    안 걸리지만, 알트코인이 그 흐름을 그대로 따라갈 위험은 똑같이 크다."""
    try:
        limit = max(cfg.btc_lookback_candles, cfg.btc_macro_lookback_candles) + 2
        df = ex.get_klines(cfg.btc_check_symbol, limit=limit)
    except Exception:
        return False  # 조회 실패 시 신규 진입을 막지는 않음

    move_pct = _btc_move_pct(df, cfg.btc_lookback_candles)
    if move_pct is not None and move_pct >= cfg.btc_sudden_move_pct:
        log.info(
            "BTC 급변동 감지(최근 %d분간 %.2f%%) — 이번 주기 신규 진입 스캔 생략",
            cfg.btc_lookback_candles, move_pct,
        )
        return True

    macro_move_pct = _btc_move_pct(df, cfg.btc_macro_lookback_candles)
    if macro_move_pct is not None and macro_move_pct >= cfg.btc_macro_move_pct:
        log.info(
            "BTC 매크로 하락 감지(최근 %d분간 %.2f%%, 순간급변동은 아니지만 지속적) — "
            "이번 주기 신규 진입 스캔 생략",
            cfg.btc_macro_lookback_candles, macro_move_pct,
        )
        return True

    return False


def compute_max_positions(balance: float, cfg: Config) -> int:
    """잔고 규모에 따라 동시에 보유할 수 있는 최대 포지션 개수를 정한다.

    - aggressive_balance_threshold(기본 100 USDT) 미만: 초기 자금 불리기 단계로
      aggressive_max_positions(기본 3)까지 허용 (공격적).
    - balance_tier_threshold(기본 300 USDT) 이상: max_positions_high(기본 8).
    - 그 사이: 자금이 늘어날수록 1~max_positions_low(기본 3) 사이에서 비례해 늘어난다.
    """
    if balance < cfg.aggressive_balance_threshold:
        return cfg.aggressive_max_positions
    if balance >= cfg.balance_tier_threshold:
        return cfg.max_positions_high
    ratio = max(0.0, balance) / cfg.balance_tier_threshold
    positions = 1 + round(ratio * (cfg.max_positions_low - 1))
    return max(1, min(cfg.max_positions_low, positions))


def compute_large_balance_ratio_cap(balance: float, cfg: Config) -> float | None:
    """[2026-08-13 사용자요청] 복리로 잔고가 커지면 비중(%)은 그대로라 포지션당 달러
    리스크가 계속 커지는 문제 — 잔고 구간이 딱 문턱을 "초과"하면 비중 상한을 단계적으로
    낮춘다(300 초과 15%, 500 초과 12%, 1000 초과 10%). 손실로 다시 문턱 밑으로 내려가면
    스티키 없이 즉시 그 구간 기준으로 되돌아간다 — 매 진입마다 그 시점 잔고로 실시간 판단.
    300 이하면 이 캡을 적용하지 않는다(None 반환, 호출부가 기존 position_size_max 사용)."""
    if balance > cfg.large_balance_tier3_threshold:
        return cfg.large_balance_tier3_max_ratio
    if balance > cfg.large_balance_tier2_threshold:
        return cfg.large_balance_tier2_max_ratio
    if balance > cfg.large_balance_tier1_threshold:
        return cfg.large_balance_tier1_max_ratio
    return None


def compute_min_confirmations(balance: float, cfg: Config) -> int:
    """자금이 aggressive_balance_threshold(기본 100 USDT) 미만이면 진입 기준을
    살짝 낮춰(기본 3/6) 초기 자금을 더 적극적으로 불려나가고, 그 이상이면
    기본 min_confirmations(기본 4/6)로 신중하게 전환한다."""
    if balance < cfg.aggressive_balance_threshold:
        return cfg.aggressive_min_confirmations
    return cfg.min_confirmations


def compute_leverage(strength: float, cfg: Config) -> int:
    span = cfg.leverage_max - cfg.leverage_min
    return round(cfg.leverage_min + strength * span)


def compute_min_margin(balance: float, cfg: Config) -> float:
    """총자산이 aggressive_balance_threshold(기본 100 USDT) 미만이면 최소 증거금을
    aggressive_min_margin_usdt(기본 3달러)로 낮춰 더 적극적으로 진입하고,
    그 이상이면 min_margin_usdt(기본 5달러)를 사용한다."""
    if balance < cfg.aggressive_balance_threshold:
        return cfg.aggressive_min_margin_usdt
    return cfg.min_margin_usdt


def is_critical_balance_stop(balance: float, cfg: Config) -> bool:
    """[2026-08-12 사용자요청] 절대 하한선(기본 5달러) 밑이면 저잔고 복구모드의 "고확률
    후보 허용" 예외도 없이 신규 진입을 완전히 차단한다. 기존 포지션 관리(손절/익절/트레일링)는
    이 함수와 무관하게 계속된다."""
    return cfg.critical_balance_stop_usdt > 0 and balance <= cfg.critical_balance_stop_usdt


def should_pause_new_entries_for_low_balance(balance: float, cfg: Config) -> bool:
    """Stop new entries when the account is below the survival threshold."""
    return cfg.low_balance_new_entry_pause_threshold > 0 and balance < cfg.low_balance_new_entry_pause_threshold


def is_low_balance_recovery_mode(balance: float, cfg: Config) -> bool:
    """Allow only top-quality recovery entries below the survival threshold."""
    return (
        should_pause_new_entries_for_low_balance(balance, cfg)
        and bool(getattr(cfg, "low_balance_recovery_enabled", False))
    )


def passes_low_balance_recovery_gate(candidate: dict, cfg: Config) -> bool:
    """Below the survival threshold, trade only candidates with extra cushion."""
    probability = float(candidate.get("probability", 0.0))
    score = float(candidate.get("score", 0.0))
    return (
        probability >= getattr(cfg, "low_balance_recovery_min_probability", 1.0)
        and score >= getattr(cfg, "low_balance_recovery_min_score", 1.0)
    )


def compute_aggregate_worst_case_loss_pct(pm: PositionManager, total_balance: float) -> float:
    """[2026-08-09] 계좌 소진 방지 강화 요청: 현재 보유한 모든 포지션이 동시에 전부 손절까지
    가는 최악의 시나리오를 가정했을 때, 잔고 대비 몇 %를 잃는지 계산한다. 개별 포지션은
    STOP_MARKET으로 각각 보호되지만, "여러 알트코인이 한꺼번에 같은 방향으로 급락"하는
    상관 리스크는 개별 손절만으론 못 막으므로, 합산 기준의 별도 체크가 필요하다.

    포지션당 손절시 손실(USDT) = 증거금(진입가*수량/레버리지) * (손절ROE% / 100)
    (STOP_LOSS_PCT는 ROE 기준이라 이미 레버리지가 반영돼 있다 — 손절가 자체가 그렇게 계산됨)
    """
    if total_balance <= 0:
        return 0.0
    total_worst_case_loss_usdt = 0.0
    for pos in list(pm.positions.values()):
        leverage = pos.leverage or 1.0
        margin_used = abs(pos.entry_price * pos.quantity) / leverage
        # 이 함수는 심볼별 실제 stop_loss_pct(전역 cfg 값)를 모르므로 호출부에서 곱해서 넘기지
        # 않고, 여기서는 증거금(=포지션 규모) 총합만 반환하는 형태가 더 범용적이다.
        total_worst_case_loss_usdt += margin_used
    return total_worst_case_loss_usdt / total_balance * 100  # 증거금 합계가 잔고 대비 몇 %인지


def passes_aggregate_risk_filter(pm: PositionManager, cfg: Config, total_balance: float) -> bool:
    """[2026-08-09] 보유 포지션들의 증거금 합계가 잔고 대비 max_aggregate_margin_pct를
    넘으면 신규 진입을 멈춘다. MAX_MARGIN_RATIO(유지증거금 기준, 훨씬 작은 값)와는 다른
    지표다 — 이건 "여러 알트가 동시에 손절까지 갈 때 잃을 수 있는 최대 규모"를 원금(증거금)
    기준으로 직접 제한하는, 상관 리스크에 대한 별도 방어선이다."""
    aggregate_margin_pct = compute_aggregate_worst_case_loss_pct(pm, total_balance)
    return aggregate_margin_pct < cfg.max_aggregate_margin_pct


def compute_stop_loss_pct(cfg: Config, side: str, entered_at: float | None = None) -> tuple[float, bool]:
    """[2026-08-11 사용자요청] SHORT 전용 손절폭(있으면) + 진입 직후 유예기간 동안의 확장폭을
    반영해 최종 손절 %를 계산한다. entered_at=None이면 "지금 막 진입하는 시점"으로 보고
    유예기간이 항상 적용된 것으로 취급한다. (widened, 유예중인지) 튜플을 반환한다."""
    base = cfg.short_stop_loss_pct if side == "SHORT" and cfg.short_stop_loss_pct > 0 else cfg.stop_loss_pct
    if cfg.stop_loss_grace_sec <= 0:
        return base, False
    if entered_at is None or (time.time() - entered_at) < cfg.stop_loss_grace_sec:
        return base * cfg.stop_loss_grace_widen_mult, True
    return base, False


def passes_whipsaw_volatility_filter(ex: Exchange, cfg: Config, symbol: str, side: str, leverage: float = 4.0) -> bool:
    """Skip entries when 5m volatility is too small OR too large relative to the stop width.

    [2026-08-11 사용자요청] 기존엔 하한선(변동성이 너무 작으면 스킵)만 있었다 — 실측 결과
    SHORT 손절의 39%가 진입 1분 이내(순수 STOP_LOSS 중앙값 1.84분)에 발생, LONG은 거의
    없었던 것과 대비돼(중앙값 4.55분, 1분내 0건) 휩쏘(노이즈가 손절폭보다 커서 방향과
    무관하게 스탑에 스치는 현상)로 판단, 상한선을 추가했다.

    [2026-08-13 감사수정] stop_dist_pct(가격 기준 손절폭)는 ROE 기준 stop_loss_pct를
    실제 레버리지로 나눠야 한다. 예전엔 항상 4.0(구 기본 레버리지)으로 나눠서, 신호강도로
    동적 결정되는 실제 레버리지(leverage_min~leverage_max, 기본 3~5)가 3이면 필터가
    허용하는 변동성 상/하한을 실제보다 25% 낮게(더 타이트), 5면 25% 높게(더 느슨하게)
    오판했다. 호출부(scan_entry_candidate)에서 신호강도 기반 strength를 먼저 계산해
    compute_leverage(strength, cfg)로 얻은 실제 레버리지를 넘겨받는다. leverage 인자를
    생략(레거시 호출/테스트)하면 기존과 동일하게 4.0을 쓴다."""
    try:
        df_5m = ex.get_klines(symbol, interval="5m")
        df_5m = add_indicators(df_5m, cfg)
        curr_5m = df_5m.iloc[-1]
        if curr_5m["close"] <= 0:
            return True
        atr_pct_5m = (curr_5m["atr"] / curr_5m["close"]) * 100
        # 진입 전 필터라 유예기간(그라운드 확장)은 여기 반영하지 않는다 — SHORT전용값만 반영.
        base_stop_loss_pct = cfg.short_stop_loss_pct if side == "SHORT" and cfg.short_stop_loss_pct > 0 else cfg.stop_loss_pct
        stop_dist_pct = base_stop_loss_pct / max(leverage, 1.0)  # 실제(신호강도 기반) 레버리지 사용
        if atr_pct_5m < stop_dist_pct * cfg.min_atr_vs_stop_ratio:
            return False
        if cfg.max_atr_vs_stop_ratio > 0 and atr_pct_5m > stop_dist_pct * cfg.max_atr_vs_stop_ratio:
            return False
        return True
    except Exception:
        return True


def passes_one_min_noise_filter(ex: Exchange, cfg: Config, symbol: str, side: str) -> bool:
    """Avoid entering on a 1m indecision/noise candle without waiting a whole extra candle.

    A huge wick relative to the body means price is whipping both ways inside the
    current minute.  That is exactly where the recent sub-1m stop/early-exit
    losses came from.  If data is missing, stay neutral so this does not become
    a broad trade killer.
    """
    if not getattr(cfg, "one_min_noise_filter_enabled", True):
        return True
    try:
        df_1m = ex.get_klines(symbol, interval="1m")
        if len(df_1m) < 1:
            return True
        curr = df_1m.iloc[-1]
        open_, high, low, close = map(float, (curr["open"], curr["high"], curr["low"], curr["close"]))
        body = abs(close - open_)
        full_range = high - low
        if open_ <= 0 or high <= 0 or low <= 0 or full_range <= 0:
            return True
        if side == "LONG" and close < open_:
            return False
        if side == "SHORT" and close > open_:
            return False
        if body <= 0:
            return False
        wick = full_range - body
        return (wick / body) <= getattr(cfg, "one_min_noise_max_wick_body_ratio", 2.5)
    except Exception:
        return True


def passes_entry_range_position_filter(ex: Exchange, cfg: Config, symbol: str, side: str) -> bool:
    """[2026-08-12 사용자요청] "순환매매를 하려는데 돌파매매(꼭대기 추격)가 된다"는 실거래
    문제 확인 — 최근1시간 LONG진입가가 직전20분 가격범위의 60~94%(거의 꼭대기)에 몰려
    있었고, 진입위치 70%미만 유지시 4시간 손익 -1.99USDT vs 70%이상 포함시 -4.95USDT로
    필터 적용시 손실이 약 71% 줄어드는 걸 간단검증으로 확인했다. 진입 시점 가격이 최근
    N분(기본 20분) 범위에서 이미 상단(LONG)/하단(SHORT) 가까이 와있으면(추격매수/매도)
    막는다. 데이터 부족/오류 시엔 넓은 필터 킬이 되지 않도록 통과시킨다."""
    if not getattr(cfg, "entry_range_position_filter_enabled", True):
        return True
    try:
        df_1m = ex.get_klines(symbol, interval="1m")
        lookback = getattr(cfg, "entry_range_position_lookback_min", 20)
        if len(df_1m) < lookback:
            return True
        window = df_1m.tail(lookback)
        hi, lo = float(window["high"].max()), float(window["low"].min())
        if hi <= lo:
            return True
        close = float(df_1m.iloc[-1]["close"])
        pos_pct = (close - lo) / (hi - lo) * 100
        max_pct = getattr(cfg, "entry_range_position_max_pct", 70.0)
        if side == "LONG" and pos_pct > max_pct:
            return False
        if side == "SHORT" and pos_pct < (100 - max_pct):
            return False
        return True
    except Exception:
        return True


def passes_short_scalp_reversal_filter(ex: Exchange, cfg: Config, symbol: str) -> bool:
    """Block SHORT scalp entries after a 1m intrabar bounce from the low.

    This is stricter than assess_short_reversal_risk(): recent live losses showed
    that merely lowering priority/size still allowed repeated SHORT whipsaws.
    The filter uses only the latest closed/available 1m candle shape, matching
    the offline backtest rule: if the candle closes too far above its low, the
    selloff was already absorbed and SHORT entry is skipped.
    """
    max_close_from_low_pct = getattr(cfg, "short_scalp_max_close_from_low_pct", 0.0)
    if max_close_from_low_pct <= 0:
        return True
    try:
        df_1m = ex.get_klines(symbol, interval="1m")
        if len(df_1m) < 1:
            return True
        curr = df_1m.iloc[-1]
        low = float(curr["low"])
        close = float(curr["close"])
        if low <= 0 or close <= 0:
            return True
        close_from_low_pct = (close / low - 1) * 100
        return close_from_low_pct <= max_close_from_low_pct
    except Exception:
        return True


def assess_short_reversal_risk(ex: Exchange, cfg: Config, symbol: str) -> dict:
    """Flag SHORT setups that may be late into a snapback, without blocking them.

    The goal is to preserve scalp-loop trade count: risky shorts are still
    eligible, but they are ranked behind cleaner candidates and sized smaller.
    """
    if not getattr(cfg, "short_reversal_risk_enabled", True):
        return {"risky": False, "reasons": []}
    reasons = []
    try:
        df_1m = ex.get_klines(symbol, interval="1m")
        if len(df_1m) >= 1:
            curr = df_1m.iloc[-1]
            open_, high, low, close = map(float, (curr["open"], curr["high"], curr["low"], curr["close"]))
            body = abs(close - open_)
            lower_wick = min(open_, close) - low
            if close > open_:
                reasons.append("1m_bullish_snapback")
            if body > 0 and lower_wick / body >= 1.5:
                reasons.append("1m_long_lower_wick")
    except Exception:
        pass
    try:
        df_5m = ex.get_klines(symbol, interval="5m")
        if len(df_5m) >= 1:
            curr = df_5m.iloc[-1]
            open_, high, low, close = map(float, (curr["open"], curr["high"], curr["low"], curr["close"]))
            body = abs(close - open_)
            lower_wick = min(open_, close) - low
            if close > open_:
                reasons.append("5m_bullish_snapback")
            if body > 0 and lower_wick / body >= 1.2:
                reasons.append("5m_long_lower_wick")
    except Exception:
        pass
    return {"risky": bool(reasons), "reasons": reasons}


def aggregate_risk_size_multiplier(pm: PositionManager, cfg: Config, total_balance: float) -> float:
    """Keep scanning when aggregate margin is high, but reduce the next entry size."""
    aggregate_margin_pct = compute_aggregate_worst_case_loss_pct(pm, total_balance)
    if aggregate_margin_pct < cfg.max_aggregate_margin_pct:
        return 1.0

    mult = max(0.0, min(1.0, cfg.aggregate_risk_size_mult))
    log.info(
        "보유 포지션 증거금 합계가 상관리스크 한도(%.0f%%)를 초과(현재 %.1f%%) — 신규 진입은 유지하고 비중 %.0f%% 적용",
        cfg.max_aggregate_margin_pct, aggregate_margin_pct, mult * 100,
    )
    return mult


def compute_position_size(balance: float, symbol: str, ex: Exchange, price: float, ratio: float, leverage: int, min_margin_usdt: float) -> float:
    max_notional = balance * leverage
    notional = balance * ratio * leverage
    min_notional_for_margin = min_margin_usdt * leverage  # 증거금이 최소 min_margin_usdt는 되도록
    notional = max(notional, min_notional_for_margin)
    if notional > max_notional:
        return 0.0  # 최소 증거금조차 감당 안 됨 -> 무리하게 주문하지 않고 진입 생략
    raw_qty = notional / price
    return ex.round_quantity(symbol, raw_qty, price=price, max_notional=max_notional)


def compute_risk_based_position_size(
    balance: float, symbol: str, ex: Exchange, price: float, stop_price: float,
    risk_pct_of_balance: float, leverage: int, min_margin_usdt: float,
    small_balance_min_margin_usdt: float = 0.0,
) -> float:
    """[2026-08-09] 계좌 소진 방지 강화 요청: 단순 "잔고의 N%를 증거금으로" 방식 대신,
    "이 거래가 손절까지 갔을 때 잃는 금액이 잔고의 risk_pct_of_balance를 넘지 않도록" 수량을
    역산한다 — 진입가와 손절가 사이 거리가 좁을수록(변동성 낮은 코인) 더 큰 수량을, 거리가
    넓을수록(변동성 높은 코인) 더 작은 수량을 자동으로 잡아준다. 기존 compute_position_size는
    이 함수로 대체하지 않고 남겨둔다 — 아직 실거래 진입 경로에는 연결하지 않은 상태(검증 전).

    risk_amount_usdt = balance * risk_pct_of_balance
    notional = risk_amount_usdt / (가격이 진입가~손절가까지 움직이는 비율)
    """
    price_distance_ratio = abs(price - stop_price) / price
    if price_distance_ratio <= 0:
        return 0.0  # 손절가가 진입가와 같으면(설정 오류 등) 계산 불가 — 진입 생략
    risk_amount_usdt = balance * risk_pct_of_balance
    notional = risk_amount_usdt / price_distance_ratio

    max_notional = balance * leverage
    # 리스크 기반 수량은 기본적으로 방어 배율을 그대로 반영하되, 50 USDT 미만의
    # 초소형 계좌에서는 지나치게 작은 수량이 거래소 최소 수량 근처로 눌려 신호가
    # 사실상 의미를 잃는 것을 막기 위해 아주 작은 전용 하한만 둔다.
    # 기존 MIN_MARGIN/AGGRESSIVE_MIN_MARGIN은 risk-based sizing에 직접 강제하지 않는다.
    if balance < 50.0 and small_balance_min_margin_usdt > 0:
        floor_notional = small_balance_min_margin_usdt * leverage
        if notional < floor_notional:
            notional = floor_notional
    if notional > max_notional:
        notional = max_notional  # 리스크 기준으로 계산된 수량이 레버리지 한도를 넘으면 한도로 캡
    raw_qty = notional / price
    return ex.round_quantity(symbol, raw_qty, price=price, max_notional=max_notional)


DAILY_BASELINE_OVERRIDE_PATH = Path("logs/daily_baseline_override.json")


def load_daily_baseline_override(today: date, default_next_threshold: float, path: Path = DAILY_BASELINE_OVERRIDE_PATH) -> dict | None:
    """[2026-08-13 사용자요청] "오늘 수익률/기준자산을 우리가 개선한 시점(예: 12:17 진입범위필터
    수정)으로 리셋해줘" — 기본 동작(재시작/날짜변경 시 '지금' 시점으로 자동리셋, 아래
    update_daily_checkpoint 참고)으로는 과거의 특정 시점을 기준으로 삼을 수 없다.
    logs/daily_baseline_override.json에 {"date": "YYYY-MM-DD", "start_balance": ..,
    "bot_pnl_start": ..}가 있고 date가 오늘과 일치하면 그 값을 daily_state 초기값으로 쓴다.
    파일이 없거나 날짜가 안 맞거나(다음날이 되면 자동 무시) 형식이 깨졌으면 조용히 None을
    반환해 기존 자동리셋 동작으로 폴백한다."""
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        log.warning("일일 기준자산 오버라이드 파일 파싱 실패 — 기본 동작으로 폴백")
        return None
    if data.get("date") != today.isoformat():
        return None
    if "start_balance" not in data or "bot_pnl_start" not in data:
        return None
    return {
        "date": today,
        "start_balance": float(data["start_balance"]),
        "start_ms": int(data.get("start_ms", time.time() * 1000)),
        "bot_pnl_start": float(data["bot_pnl_start"]),
        "next_threshold": float(data.get("next_threshold", default_next_threshold)),
        "checkpoint_count": int(data.get("checkpoint_count", 0)),
        "loss_limit_triggered": bool(data.get("loss_limit_triggered", False)),
    }


def update_daily_checkpoint(ex: Exchange, pm: PositionManager, cfg: Config, tg: TelegramNotifier, daily_state: dict, total_balance: float):
    """날짜가 바뀌면 기준 자산을 리셋하고, 목표 수익률(및 이후 daily_profit_step_pct씩)에
    도달할 때마다 텔레그램으로 계속할지 물어본다.

    [2026-08-09] 기존엔 지갑 잔고 변화(total_balance) 기준이라 사용자가 직접 매매해서 번
    돈까지 "봇 수익률"로 잡혀 체크포인트 알림이 과도하게(예: 하루에 +100%면 10%씩 9번) 왔다는
    피드백을 받아, 봇이 직접 진입한 거래(origin=bot)의 실현손익(pm.realized_pnl_usdt)만으로
    계산하도록 변경. 하루 시작 시점의 pm.realized_pnl_usdt를 스냅샷(bot_pnl_start)해두고
    그 이후 증가분만 오늘의 봇 수익으로 취급한다 — 사용자의 수동매매는 완전히 제외된다."""
    today = date.today()
    if daily_state["date"] != today:
        daily_state["date"] = today
        daily_state["start_balance"] = total_balance
        daily_state["start_ms"] = int(time.time() * 1000)
        daily_state["bot_pnl_start"] = pm.realized_pnl_usdt
        daily_state["next_threshold"] = cfg.daily_profit_target_pct
        daily_state["checkpoint_count"] = 0
        daily_state["loss_limit_triggered"] = False
        tg.trading_paused = False
        tg._awaiting_confirmation = False
        tg.send(
            f"📅 {today} 거래 시작 — 기준자산 {total_balance:.2f} USDT, "
            f"목표 봇수익 +{cfg.daily_profit_target_pct:.0f}% (이후 {cfg.daily_profit_step_pct:.0f}%씩 추가 확인, "
            f"사용자 직접매매는 집계에서 제외됩니다)"
        )
        return

    start_balance = daily_state["start_balance"]
    if not start_balance:
        return

    bot_pnl_start = daily_state.get("bot_pnl_start", pm.realized_pnl_usdt)
    daily_bot_pnl_usdt = pm.realized_pnl_usdt - bot_pnl_start
    daily_pnl_pct = daily_bot_pnl_usdt / start_balance * 100
    checkpoint_count = daily_state.get("checkpoint_count", 0)
    checkpoint_cap_reached = (
        cfg.daily_profit_max_checkpoints > 0 and checkpoint_count >= cfg.daily_profit_max_checkpoints
    )
    if not tg._awaiting_confirmation and not checkpoint_cap_reached and daily_pnl_pct >= daily_state["next_threshold"]:
        tg.ask_daily_checkpoint(daily_pnl_pct, daily_state["next_threshold"])
        daily_state["checkpoint_count"] = checkpoint_count + 1
        daily_state["next_threshold"] += cfg.daily_profit_step_pct

    # [2026-08-07, 2026-08-11 사용자요청으로 재설계] 일일 손실 서킷브레이커: 하루 기준자산
    # 대비 -daily_loss_limit_pct% 이상 손실이면 신규 진입을 멈춘다. 수익 확인과 달리 사용자
    # 응답을 기다리지 않고 즉시 자동으로 멈춘다. 이미 열린 포지션의 익절/손절/시간초과 관리는
    # select_and_enter_best_candidates만 건너뛰므로 그대로 계속된다.
    # 원래는 "날짜가 바뀔 때까지" 무기한 정지였는데, 트리거 시각에 따라 실제 정지시간이
    # 몇 분~24시간까지 들쭉날쭉했다 — 대신 트리거 시점부터 고정 daily_loss_pause_hours(4시간)
    # 후 날짜와 무관하게 자동 재개하도록 변경.
    if not daily_state.get("loss_limit_triggered") and daily_pnl_pct <= -cfg.daily_loss_limit_pct:
        daily_state["loss_limit_triggered"] = True
        daily_state["loss_limit_pause_until"] = time.time() + cfg.daily_loss_pause_hours * 3600
        tg.trading_paused = True
        log.warning(
            "일일 손실 한도(-%.0f%%) 도달(현재 %.2f%%) — 신규 진입 %.0f시간 자동 중단(기존 포지션 관리는 계속)",
            cfg.daily_loss_limit_pct, daily_pnl_pct, cfg.daily_loss_pause_hours,
        )
        tg.send(
            f"🛑 오늘 손실이 -{cfg.daily_loss_limit_pct:.0f}%에 도달해 신규 진입을 "
            f"{cfg.daily_loss_pause_hours:.0f}시간 동안 자동으로 멈췄어요 (현재 {daily_pnl_pct:.2f}%). "
            f"기존 포지션의 익절/손절은 그대로 관리됩니다."
        )
    elif daily_state.get("loss_limit_triggered") and time.time() >= daily_state.get("loss_limit_pause_until", 0):
        daily_state["loss_limit_triggered"] = False
        tg.trading_paused = False
        log.info("일일 손실 서킷브레이커 %.0f시간 경과 — 신규 진입 자동 재개", cfg.daily_loss_pause_hours)
        tg.send(f"✅ 신규진입 정지 {cfg.daily_loss_pause_hours:.0f}시간이 지나 자동으로 재개됐어요.")


def try_average_down(ex: Exchange, pm: PositionManager, cfg: Config, symbol: str, tg: TelegramNotifier):
    """손절선을 향해 밀릴 때(트랜치마다 더 깊은 지점에서) 가용잔고의 일부를 추가 투입해
    평단가를 낮춘다. [2026-08-10] 횟수 제한은 없지만, 포지션당 총 투입 증거금은
    average_down_max_total_margin_ratio(기본 6%)를 절대 못 넘도록 매번 남은 여유만큼만
    잘라서(min) 투입한다 — "몇 번이든 되지만 총액은 하드캡"이 핵심."""
    mark_price = ex.get_mark_price(symbol)
    balance = ex.get_available_balance_usdt()
    if not pm.should_average_down(symbol, mark_price, balance=balance):
        return

    pos = pm.positions[symbol]
    intended_margin = balance * cfg.average_down_size_ratio
    total_margin_used = pos.initial_margin_usdt + pos.total_margin_added_usdt
    remaining_room = max(0.0, pos.max_total_margin_usdt - total_margin_used)
    additional_margin = min(intended_margin, remaining_room)
    if additional_margin <= 0:
        log.info("[%s] 물타기 총상한(%.2fUSDT) 도달 — 생략", symbol, pos.max_total_margin_usdt)
        return
    leverage = pos.leverage or 1.0
    notional = additional_margin * leverage
    max_notional = balance * leverage
    quantity = ex.round_quantity(symbol, notional / mark_price, price=mark_price, max_notional=max_notional)
    if quantity <= 0:
        log.info("[%s] 물타기 자금 부족으로 생략", symbol)
        return

    try:
        if not place_entry_order(ex, cfg, symbol, pos.side, quantity):
            log.info("[%s] 물타기 지정가 미체결로 취소됨 — 이번 주기엔 물타기 생략", symbol)
            return
    except Exception:
        log.exception("[%s] 물타기 주문 실패", symbol)
        tg.notify_error(symbol, "물타기 주문 실패")
        return

    live_pos = ex.get_position(symbol)
    if live_pos:
        old_stop_order_id = pos.stop_order_id
        old_trailing_order_id = pos.trailing_order_id
        old_tp_fallback_order_id = pos.tp_fallback_order_id
        # 실제로 추가된 증거금은 반올림 등으로 intended와 살짝 다를 수 있으므로, 체결 후
        # 실제 수량 변화분으로 다시 계산해서 누적 총상한 추적에 정확히 반영한다.
        actual_added_qty = max(0.0, abs(live_pos["amount"]) - pos.quantity)
        actual_added_margin = actual_added_qty * mark_price / leverage
        pm.apply_average_down(symbol, live_pos["entry_price"], abs(live_pos["amount"]), added_margin_usdt=actual_added_margin)
        if pm.positions[symbol].origin == "bot":
            mark_bot_position_open(symbol, pm.positions[symbol].side, pm.positions[symbol].entry_price,
                                   pm.positions[symbol].quantity, pm.positions[symbol].leverage)
        # [2026-08-09] 순서 변경: 예전엔 기존 주문을 먼저 취소하고 나서 새로 등록했는데, 새 등록이
        # 실패하면 그 사이 무방비 구간이 생긴다. 이제 새 주문을 먼저 등록/검증하고, 성공했을 때만
        # 기존 주문을 취소한다 — 잠깐 신구 주문이 동시에 살아있는 건 안전하다(reduceOnly라 한쪽이
        # 체결되면 다른 쪽은 대상이 없어 조용히 무시되거나 거부될 뿐, 무보호 상태보다 항상 낫다).
        try:
            new_entry = live_pos["entry_price"]
            stop_loss_pct, widened = compute_stop_loss_pct(cfg, pos.side, pos.entered_at)
            if pos.side == "LONG":
                stop_price = new_entry * (1 - stop_loss_pct / 100 / leverage)
            else:
                stop_price = new_entry * (1 + stop_loss_pct / 100 / leverage)
            stop_order = ex.place_stop_market(symbol, pos.side, abs(live_pos["amount"]), stop_price)
            pm.positions[symbol].stop_order_id = stop_order["algoId"]
            pm.positions[symbol].stop_loss_widened = widened
            if old_stop_order_id:
                ex.cancel_order(symbol, old_stop_order_id)
        except Exception:
            log.exception("[%s] 물타기 후 STOP_MARKET 재등록 실패 — 기존 STOP_MARKET을 취소하지 않고 유지(옛 수량만 보호)", symbol)
        take_profit_min = cfg.short_take_profit_min if pos.side == "SHORT" else cfg.take_profit_min
        if pos.side == "LONG":
            activation_price = new_entry * (1 + take_profit_min / 100 / leverage)
        else:
            activation_price = new_entry * (1 - take_profit_min / 100 / leverage)
        try:
            callback_rate = (cfg.trail_drawdown_pct * cfg.exchange_trailing_backstop_multiplier) / leverage
            trailing_order = ex.place_trailing_stop_market(symbol, pos.side, abs(live_pos["amount"]), activation_price, callback_rate)
            trailing_algo_id = int(trailing_order["algoId"])
            if trailing_algo_id not in ex.get_open_algo_order_ids():
                raise RuntimeError(f"TRAILING_STOP_MARKET 응답은 성공(algoId={trailing_algo_id})이었으나 실제 활성 주문 목록에 없음")
            pm.positions[symbol].trailing_order_id = trailing_algo_id
            if old_trailing_order_id:
                ex.cancel_order(symbol, old_trailing_order_id)
            if old_tp_fallback_order_id:
                ex.cancel_order(symbol, old_tp_fallback_order_id)
                pm.positions[symbol].tp_fallback_order_id = None
        except Exception:
            log.exception("[%s] 물타기 후 TRAILING_STOP_MARKET 재등록/검증 실패 — TAKE_PROFIT_MARKET 폴백 시도", symbol)
            try:
                tp_order = ex.place_take_profit_market(symbol, pos.side, abs(live_pos["amount"]), activation_price)
                pm.positions[symbol].tp_fallback_order_id = tp_order["algoId"]
                log.info("[%s] 물타기 후 TAKE_PROFIT_MARKET 폴백 등록 성공", symbol)
                if old_trailing_order_id:
                    ex.cancel_order(symbol, old_trailing_order_id)
                if old_tp_fallback_order_id:
                    ex.cancel_order(symbol, old_tp_fallback_order_id)
            except Exception:
                log.critical("[%s] 물타기 후 TRAILING+TAKE_PROFIT_MARKET 폴백 모두 실패 — 기존 트레일링/폴백 주문을 취소하지 않고 유지", symbol)
                tg.notify_error(symbol, "물타기 후 트레일링+폴백TP 등록 모두 실패(기존 주문 유지 중)")
        tg.send(
            f"➕ {symbol} 물타기 실행 — 추가 증거금 {additional_margin:.2f} USDT\n"
            f"새 평단가: {live_pos['entry_price']}, 수량: {abs(live_pos['amount'])}"
        )


def check_early_exit(ex: Exchange, pm: PositionManager, cfg: Config, symbol: str) -> bool:
    """손실 중인 포지션의 추세가 확실히 반대로 뒤집혔다면 손절선까지 기다리지 않고
    먼저 빠져나가야 하는지 판단한다. 다만 너무 자주 발동하면 수수료만 나가고 실익이
    없으므로, 손실이 최소 기준(early_exit_min_loss_roe, 기본 -1% ROE) 이상이고
    3가지 신호(EMA/MACD/RSI)가 전부 뒤집혔을 때만(reversal_min_votes) 발동한다.
    이미 수익 중이거나, 이미 정식 손절 대상(evaluate가 처리)이면 관여하지 않는다.

    [2026-08-13 실거래 복기] 진입 후 early_exit_min_hold_sec(기본 120초) 이내에는 발동하지
    않는다 — 실측: EARLY_EXIT/EXTERNAL_CLOSE_LOSS 21건 전부(100%) 청산후15분내 회복,
    그중 52%가 진입 60초 이내 초단기 청산이었다. 진입 직후 1~2캔들짜리 노이즈를 추세전환
    으로 오판하는 패턴으로 판단, 120초 가드 백테스트(13건)에서 85%가 가드기간 동안 정식
    손절에 안 닿고 생존함을 확인 후 추가."""
    pos = pm.positions.get(symbol)
    if pos is None:
        return False
    if cfg.early_exit_min_hold_sec > 0 and (time.time() - pos.entered_at) < cfg.early_exit_min_hold_sec:
        return False  # 진입 직후 유예 — 노이즈성 반전 오판 방지
    mark_price = ex.get_mark_price(symbol)
    pnl = pnl_pct(pos.entry_price, mark_price, pos.side)
    if pnl >= 0:
        return False
    roe = pnl * pos.leverage
    if roe <= -cfg.stop_loss_pct:
        return False  # 이미 정식 손절 대상 — evaluate()가 처리하도록 둠
    if roe > -cfg.early_exit_min_loss_roe:
        return False  # 손실이 아직 너무 작음 — 노이즈일 가능성이 높아 지켜봄

    df = ex.get_klines(symbol)
    df = add_indicators(df, cfg)
    return detect_reversal(df, cfg, pos.side)


def check_profit_should_lock_in(ex: Exchange, pm: PositionManager, cfg: Config, symbol: str) -> bool:
    """ROE가 익절 최소선(take_profit_min)~상한(take_profit_max_dynamic) 사이일 때,
    지금 이 순간 캔들 모멘텀이 아직 살아있으면 더 들고(최대 상한까지), 꺾이는 신호가
    보이면 지금 확정하라고 True를 반환한다. 상한(take_profit_max)은 evaluate()가
    이미 즉시 처리하므로 여기서는 그 사이 구간만 다룬다."""
    pos = pm.positions.get(symbol)
    if pos is None or not pos.armed:
        return False
    mark_price = ex.get_mark_price(symbol)
    pnl = pnl_pct(pos.entry_price, mark_price, pos.side)
    roe = pnl * pos.leverage
    take_profit_min = cfg.short_take_profit_min if pos.side == "SHORT" else cfg.take_profit_min
    # [2026-08-09] 익절선을 막 넘은 직후(여유구간 이내)는 반전확정 체크를 적용하지 않는다 —
    # 숨돌릴 틈도 없이 바로 확정되는 문제(EPICUSDT 실측)를 막기 위한 여유구간.
    if roe < take_profit_min + cfg.profit_lock_buffer_pct or roe >= cfg.take_profit_hard_cap:
        return False  # armed 됐지만 기준 밖(여유구간 이내 포함)이거나, 절대 상한은 evaluate가 처리

    df = ex.get_klines(symbol)
    df = add_indicators(df, cfg)
    return detect_reversal(df, cfg, pos.side, min_votes=cfg.profit_hold_reversal_votes)


def check_hourly_soft_stop(ex: Exchange, cfg: Config, pm: PositionManager, symbol: str, hourly_check_state: dict) -> bool:
    """1시간봉이 새로 바뀔 때마다(심볼별로 마지막에 확인한 1시간봉 시각을 기억해서 한 번만),
    보유 포지션의 방향을 상위 시간대(1시간/15분/5분, EMA fast/slow 추세)가 여전히
    지지하는지 재평가한다.

    손실 중이고(ROE ≤ -soft_stop_min_loss_roe) 상위 시간대 과반이 더 이상 그 방향을
    지지하지 않으면, 다음 1시간(또는 5~15분) 안에 수익 전환을 기대하기 어렵다고 보고
    정식 손절선(STOP_LOSS_PCT)까지 기다리지 않고 지금의 더 작은 손실에서 약손절한다.
    """
    pos = pm.positions.get(symbol)
    if pos is None:
        return False

    # [2026-08-11 사용자요청] 진입 직후 순간 노이즈로 즉시 발동하는 걸 막는 최소 보유시간 가드.
    if cfg.soft_stop_min_hold_sec > 0 and (time.time() - pos.entered_at) < cfg.soft_stop_min_hold_sec:
        return False

    try:
        df_1h = ex.get_klines(symbol, limit=3, interval="1h")
    except Exception:
        return False
    if df_1h.empty:
        return False
    latest_candle_time = df_1h.iloc[-1]["open_time"]
    if hourly_check_state.get(symbol) == latest_candle_time:
        return False  # 이번 1시간봉에선 이미 재평가함
    hourly_check_state[symbol] = latest_candle_time

    mark_price = ex.get_mark_price(symbol)
    pnl = pnl_pct(pos.entry_price, mark_price, pos.side)
    roe = pnl * pos.leverage
    if roe >= 0:
        return False  # 수익권이면 익절/모멘텀락인 로직이 처리
    if roe > -cfg.soft_stop_min_loss_roe:
        return False  # 손실이 아직 작아 노이즈일 수 있음 — 지켜봄
    if roe <= -cfg.stop_loss_pct:
        return False  # 이미 정식 손절 대상 — evaluate()가 처리하도록 둠

    agree, total = 0, 0
    for interval in ("1h", "15m", "5m"):
        try:
            df = ex.get_klines(symbol, limit=max(cfg.ema_slow + 10, 60), interval=interval)
        except Exception:
            continue
        if len(df) < cfg.ema_slow + 2:
            continue
        close = df["close"]
        ema_fast = close.ewm(span=cfg.ema_fast, adjust=False).mean().iloc[-1]
        ema_slow = close.ewm(span=cfg.ema_slow, adjust=False).mean().iloc[-1]
        total += 1
        if pos.side == "LONG" and ema_fast > ema_slow:
            agree += 1
        elif pos.side == "SHORT" and ema_fast < ema_slow:
            agree += 1

    if total == 0:
        return False
    ratio = agree / total
    if ratio < cfg.soft_stop_mtf_min_ratio:
        log.info(
            "[%s] 1시간봉 재평가: 상위시간대 정합 %d/%d(%.0f%%) — 손실중(ROE=%.2f%%) 약손절",
            symbol, agree, total, ratio * 100, roe,
        )
        return True
    return False


def close_symbol_if_already_past_stop(ex: Exchange, pm: PositionManager, cfg: Config, symbol: str, tg: TelegramNotifier):
    """[2026-08-07] STOP_MARKET 등록이 실패했을 때(대부분 -2021: 이미 손절가를 넘어선 상태라 즉시체결
    판정) 다음 reconcile 주기(최대 수십 초)까지 기다리지 않고, 여기서 바로 현재 ROE를 확인해 이미
    손절 기준을 넘었으면 즉시 시장가로 청산한다. 초고빈도 수동매매 심볼(HFTUSDT/HEIUSDT 실측 3회
    무방비 노출 확인)에서 안전망의 반응 지연을 줄이기 위함."""
    pos = pm.positions.get(symbol)
    if pos is None:
        return
    mark_price = ex.get_mark_price(symbol)
    roe = pnl_pct(pos.entry_price, mark_price, pos.side) * pos.leverage
    if roe > -cfg.stop_loss_pct:
        return  # 아직 손절 기준 안 넘었으면 다음 정상 주기(폴링)에 맡김
    log.warning("[%s] STOP_MARKET 등록 실패 + 이미 손절 기준 초과(ROE=%.2f%%) — 즉시 시장가 청산", symbol, roe)
    if pos.trailing_order_id:
        ex.cancel_order(symbol, pos.trailing_order_id)
    if pos.tp_fallback_order_id:
        ex.cancel_order(symbol, pos.tp_fallback_order_id)
    try:
        ex.close_market_position(symbol, pos.side, abs(pos.quantity))
    except Exception:
        log.exception("[%s] STOP_MARKET 실패 후 즉시 청산도 실패 — 다음 폴링 주기에 맡김", symbol)
        return
    result_pnl = pnl_pct(pos.entry_price, mark_price, pos.side)
    result_pnl_usdt = net_profit_usdt(pos, mark_price, cfg)
    pm.untrack(symbol)
    pm.record_result(symbol, result_pnl, result_pnl_usdt, origin=pos.origin, side=pos.side, leverage=pos.leverage)
    record_trade_ledger(cfg, pos, symbol, "STOP_LOSS", mark_price, result_pnl, result_pnl_usdt, ex)
    tg.notify_exit(symbol, "STOP_LOSS", result_pnl)


def reconcile_positions(ex: Exchange, pm: PositionManager, cfg: Config, tg: TelegramNotifier):
    """봇의 추적 목록을 거래소 실제 포지션과 통째로 대조해 맞춘다 (매 주기 1회, API 호출 1건).

    - 거래소엔 있는데 봇이 모르는 포지션 → 새로 추적 시작 (직접 진입/추가매수로 새로 생긴 경우)
    - 거래소엔 있는데 수량/평단가가 다름 → 갱신 (직접 추가매수로 수량이 바뀐 경우)
    - 봇은 추적 중인데 거래소엔 없음 → 외부에서 종료된 것으로 보고 정리

    개별 심볼마다 따로 조회하던 이전 방식은, 그 순간의 응답 문제 등으로 아직 살아있는
    포지션을 잘못 "종료됨"으로 오판할 여지가 있었다. 전체를 한 번에 비교하면 더 안정적이다.
    """
    live_positions = {p["symbol"]: p for p in ex.get_open_positions()}

    for symbol in list(pm.positions.keys()):
        if symbol not in live_positions:
            pos = pm.positions[symbol]
            try:
                mark_price = ex.get_mark_price(symbol)
                result_pnl = pnl_pct(pos.entry_price, mark_price, pos.side)
                result_pnl_usdt = net_profit_usdt(pos, mark_price, cfg)
            except Exception:
                mark_price = pos.entry_price
                result_pnl, result_pnl_usdt = 0.0, 0.0
            # STOP_MARKET이 이 청산의 원인이 아니었다면(수동청산/청산 등) 남아있는 주문을 정리한다.
            # STOP_MARKET 체결이 원인이었다면 이미 없는 주문이라 조용히 무시됨(cancel_order 처리).
            if pos.stop_order_id:
                ex.cancel_order(symbol, pos.stop_order_id)
            if pos.trailing_order_id:
                ex.cancel_order(symbol, pos.trailing_order_id)
            if pos.tp_fallback_order_id:
                ex.cancel_order(symbol, pos.tp_fallback_order_id)
            pm.untrack(symbol)
            pm.record_result(symbol, result_pnl, result_pnl_usdt, origin=pos.origin, side=pos.side, leverage=pos.leverage)
            # [2026-08-11 사용자요청] 기존엔 전부 "EXTERNAL_CLOSE"로 뭉뚱그려져서(전체 청산의
            # 60%), 거래소가 이익으로 잠근 건지(트레일링/익절 발동) 손실로 끊은 건지(손절 발동)
            # 구분이 안 됐다. 매번 어느 algoId가 실제 체결됐는지 REST로 조회하면 호출이 늘어나니,
            # 대신 이미 계산된 실현손익 부호로 분류한다 — 손절 주문은 항상 손실로, 트레일링/익절
            # 주문은 항상 이익(또는 최소 본절)으로 체결되므로 부호만으로 신뢰성 있게 구분 가능.
            external_close_reason = "EXTERNAL_CLOSE_PROFIT" if result_pnl_usdt > 0 else "EXTERNAL_CLOSE_LOSS"
            record_trade_ledger(cfg, pos, symbol, external_close_reason, mark_price, result_pnl, result_pnl_usdt, ex)
            log.info("[%s] 거래소에서 직접 종료된 포지션 감지 (추정 pnl=%.2f%%, %.2fUSDT)", symbol, result_pnl, result_pnl_usdt)
            tg.send(f"ℹ️ {symbol} 포지션이 바이낸스에서 직접 종료된 것을 감지했어요 (추정 pnl={result_pnl:.2f}%, {result_pnl_usdt:+.2f}USDT)")

    for symbol, live in live_positions.items():
        if symbol not in pm.positions:
            leverage = live.get("leverage", 1.0)
            quantity = abs(live["amount"])
            origin = infer_open_position_origin(symbol, live["side"], live["entry_price"], quantity)
            pm.track(symbol, live["side"], live["entry_price"], quantity, leverage=leverage, origin=origin)
            log.info("[%s] 새 포지션 발견 — 추적 시작 (origin=%s)", symbol, origin)
            # [2026-08-07 QA 감사] 봇이 직접 연 포지션과 달리 이 경로(수동/외부 체결 발견)는
            # 지금까지 거래소단 STOP_MARKET을 등록하지 않아 30초 폴링 간격 사이 급락에
            # 무방비였다 — execute_entry()와 동일한 방식으로 여기서도 등록한다.
            pos = pm.positions[symbol]
            try:
                stop_loss_pct, widened = compute_stop_loss_pct(cfg, pos.side, pos.entered_at)
                if pos.side == "LONG":
                    stop_price = pos.entry_price * (1 - stop_loss_pct / 100 / leverage)
                else:
                    stop_price = pos.entry_price * (1 + stop_loss_pct / 100 / leverage)
                stop_order = ex.place_stop_market(symbol, pos.side, pos.quantity, stop_price)
                pos.stop_order_id = stop_order["algoId"]
                pos.stop_loss_widened = widened
            except Exception:
                log.exception("[%s] 새로 발견한 포지션 STOP_MARKET 등록 실패 — 폴링 기반 손절로 대체됨", symbol)
                # [2026-08-07] 3회 재발 확인 후 근본 개선: 초고빈도 수동매매 심볼(HFTUSDT/HEIUSDT)에서
                # 이 등록이 실패하는 건 대부분 -2021(이미 손절가를 넘어섬)이 원인이었다. 다음 reconcile
                # 주기(최대 수십 초)까지 기다리면 그 사이 무방비 노출이 생기므로, 실패 즉시 여기서
                # ROE를 확인해 이미 손절 기준을 넘었으면 그 자리에서 바로 시장가 청산한다.
                try:
                    close_symbol_if_already_past_stop(ex, pm, cfg, symbol, tg)
                except Exception:
                    log.exception("[%s] 즉시청산 폴백 실패", symbol)
            take_profit_min = cfg.short_take_profit_min if pos.side == "SHORT" else cfg.take_profit_min
            if pos.side == "LONG":
                activation_price = pos.entry_price * (1 + take_profit_min / 100 / leverage)
            else:
                activation_price = pos.entry_price * (1 - take_profit_min / 100 / leverage)
            try:
                callback_rate = (cfg.trail_drawdown_pct * cfg.exchange_trailing_backstop_multiplier) / leverage
                trailing_order = ex.place_trailing_stop_market(symbol, pos.side, pos.quantity, activation_price, callback_rate)
                trailing_algo_id = int(trailing_order["algoId"])
                if trailing_algo_id not in ex.get_open_algo_order_ids():
                    raise RuntimeError(f"TRAILING_STOP_MARKET 응답은 성공(algoId={trailing_algo_id})이었으나 실제 활성 주문 목록에 없음")
                pos.trailing_order_id = trailing_algo_id
            except Exception:
                log.exception("[%s] 새로 발견한 포지션 TRAILING_STOP_MARKET 등록/검증 실패 — TAKE_PROFIT_MARKET 폴백 시도", symbol)
                try:
                    tp_order = ex.place_take_profit_market(symbol, pos.side, pos.quantity, activation_price)
                    pos.tp_fallback_order_id = tp_order["algoId"]
                    log.info("[%s] 새로 발견한 포지션 TAKE_PROFIT_MARKET 폴백 등록 성공", symbol)
                except Exception:
                    log.critical("[%s] 새로 발견한 포지션 TRAILING+TAKE_PROFIT_MARKET 폴백 모두 실패 — 폴링 트레일링에만 의존 중", symbol)
                    tg.notify_error(symbol, "트레일링+폴백TP 등록 모두 실패(폴링 트레일링으로만 방어 중)")
            tg.send(f"🔎 {symbol} 포지션을 새로 발견해서 추적을 시작했어요 (origin={origin})")
        else:
            pos = pm.positions[symbol]
            new_qty = abs(live["amount"])
            side_flipped = live["side"] != pos.side
            if side_flipped:
                # [2026-08-06] 거래소 실제 방향(LONG/SHORT)이 봇이 추적하던 방향과 다르면(외부/수동
                # 체결로 반대 방향 포지션이 새로 생긴 경우) side를 갱신하지 않으면 이후 손절/물타기/
                # STOP_MARKET 방향이 전부 반대로 계산되어 청산 주문이 거절된다(실제 BEATUSDT 사고 사례).
                old_side = pos.side
                old_stop_order_id = pos.stop_order_id
                old_trailing_order_id = pos.trailing_order_id
                old_tp_fallback_order_id = pos.tp_fallback_order_id
                pos.side = live["side"]
                pos.entry_price = live["entry_price"]
                pos.quantity = new_qty
                if pos.origin == "bot":
                    mark_bot_position_open(symbol, pos.side, pos.entry_price, pos.quantity, pos.leverage)
                log.warning("[%s] 포지션 방향 변경 감지(외부 체결 추정): %s->%s qty=%s entry=%s — 방향 갱신, 새 보호주문 확인 후 기존 해제",
                            symbol, old_side, pos.side, pos.quantity, pos.entry_price)
                # [2026-08-09] 순서 변경: 새 방향 기준 주문을 먼저 등록/검증하고, 성공했을 때만
                # 기존(옛 방향) 주문을 취소한다. 옛 주문은 방향이 바뀌어 어차피 의미가 없어졌지만
                # (reduceOnly라 트리거돼도 거부될 뿐), 취소 자체는 새 주문이 확인된 뒤에 한다.
                try:
                    leverage = pos.leverage or 1.0
                    stop_loss_pct, widened = compute_stop_loss_pct(cfg, pos.side, pos.entered_at)
                    if pos.side == "LONG":
                        stop_price = pos.entry_price * (1 - stop_loss_pct / 100 / leverage)
                    else:
                        stop_price = pos.entry_price * (1 + stop_loss_pct / 100 / leverage)
                    stop_order = ex.place_stop_market(symbol, pos.side, pos.quantity, stop_price)
                    pos.stop_order_id = stop_order["algoId"]
                    pos.stop_loss_widened = widened
                    if old_stop_order_id:
                        ex.cancel_order(symbol, old_stop_order_id)
                except Exception:
                    log.exception("[%s] 방향 변경 후 STOP_MARKET 재등록 실패 — 기존(옛 방향) STOP_MARKET을 취소하지 않고 유지", symbol)
                leverage = pos.leverage or 1.0
                take_profit_min = cfg.short_take_profit_min if pos.side == "SHORT" else cfg.take_profit_min
                if pos.side == "LONG":
                    activation_price = pos.entry_price * (1 + take_profit_min / 100 / leverage)
                else:
                    activation_price = pos.entry_price * (1 - take_profit_min / 100 / leverage)
                try:
                    callback_rate = (cfg.trail_drawdown_pct * cfg.exchange_trailing_backstop_multiplier) / leverage
                    trailing_order = ex.place_trailing_stop_market(symbol, pos.side, pos.quantity, activation_price, callback_rate)
                    trailing_algo_id = int(trailing_order["algoId"])
                    if trailing_algo_id not in ex.get_open_algo_order_ids():
                        raise RuntimeError(f"TRAILING_STOP_MARKET 응답은 성공(algoId={trailing_algo_id})이었으나 실제 활성 주문 목록에 없음")
                    pos.trailing_order_id = trailing_algo_id
                    if old_trailing_order_id:
                        ex.cancel_order(symbol, old_trailing_order_id)
                    if old_tp_fallback_order_id:
                        ex.cancel_order(symbol, old_tp_fallback_order_id)
                except Exception:
                    log.exception("[%s] 방향 변경 후 TRAILING_STOP_MARKET 재등록/검증 실패 — TAKE_PROFIT_MARKET 폴백 시도", symbol)
                    try:
                        tp_order = ex.place_take_profit_market(symbol, pos.side, pos.quantity, activation_price)
                        pos.tp_fallback_order_id = tp_order["algoId"]
                        log.info("[%s] 방향 변경 후 TAKE_PROFIT_MARKET 폴백 등록 성공", symbol)
                        if old_trailing_order_id:
                            ex.cancel_order(symbol, old_trailing_order_id)
                        if old_tp_fallback_order_id:
                            ex.cancel_order(symbol, old_tp_fallback_order_id)
                    except Exception:
                        log.critical("[%s] 방향 변경 후 TRAILING+TAKE_PROFIT_MARKET 폴백 모두 실패 — 기존(옛 방향) 주문을 취소하지 않고 유지", symbol)
                        tg.notify_error(symbol, "방향변경 후 트레일링+폴백TP 등록 모두 실패(기존 주문 유지 중)")
                tg.send(
                    f"⚠️ {symbol} 포지션 방향이 {old_side}->{pos.side}로 바뀐 것을 감지했어요 (외부/수동 체결 추정)\n"
                    f"새 수량: {pos.quantity}, 평단가: {pos.entry_price}\n"
                    f"새 방향 기준으로 자동 손절 주문을 다시 등록했습니다."
                )
            elif abs(pos.quantity - new_qty) > 1e-9 or abs(pos.entry_price - live["entry_price"]) > 1e-12:
                old_qty = pos.quantity
                pos.entry_price = live["entry_price"]
                pos.quantity = new_qty
                if pos.origin == "bot":
                    mark_bot_position_open(symbol, pos.side, pos.entry_price, pos.quantity, pos.leverage)
                if new_qty > old_qty:
                    tg.send(
                        f"➕ {symbol} 직접 추가매수 감지 — 평단가/수량 갱신\n"
                        f"새 평단가: {pos.entry_price}, 수량: {pos.quantity}"
                    )
                log.info("[%s] 포지션 수량/평단가 변경 감지: qty %s->%s entry=%s", symbol, old_qty, new_qty, pos.entry_price)
                # [2026-08-07 QA 감사] 수동 추가매수로 수량이 늘었는데 기존 STOP_MARKET은 옛 수량만
                # 보호해 나머지가 무방비로 남는 사고(CTSIUSDT 실측: 760개만 보호, 실제 4276개)가
                # 확인됨 — 물타기(try_average_down)와 동일하게 여기서도 재등록한다.
                old_stop_order_id = pos.stop_order_id
                old_trailing_order_id = pos.trailing_order_id
                old_tp_fallback_order_id = pos.tp_fallback_order_id
                # [2026-08-09] 순서 변경: 새 주문을 먼저 등록/검증하고 성공했을 때만 기존 주문을
                # 취소한다(무보호 구간 방지).
                try:
                    leverage = pos.leverage or 1.0
                    stop_loss_pct, widened = compute_stop_loss_pct(cfg, pos.side, pos.entered_at)
                    if pos.side == "LONG":
                        stop_price = pos.entry_price * (1 - stop_loss_pct / 100 / leverage)
                    else:
                        stop_price = pos.entry_price * (1 + stop_loss_pct / 100 / leverage)
                    stop_order = ex.place_stop_market(symbol, pos.side, pos.quantity, stop_price)
                    pos.stop_order_id = stop_order["algoId"]
                    pos.stop_loss_widened = widened
                    if old_stop_order_id:
                        ex.cancel_order(symbol, old_stop_order_id)
                except Exception:
                    log.exception("[%s] 수량 변경 후 STOP_MARKET 재등록 실패 — 기존 STOP_MARKET을 취소하지 않고 유지(옛 수량만 보호)", symbol)
                leverage = pos.leverage or 1.0
                take_profit_min = cfg.short_take_profit_min if pos.side == "SHORT" else cfg.take_profit_min
                if pos.side == "LONG":
                    activation_price = pos.entry_price * (1 + take_profit_min / 100 / leverage)
                else:
                    activation_price = pos.entry_price * (1 - take_profit_min / 100 / leverage)
                try:
                    callback_rate = (cfg.trail_drawdown_pct * cfg.exchange_trailing_backstop_multiplier) / leverage
                    trailing_order = ex.place_trailing_stop_market(symbol, pos.side, pos.quantity, activation_price, callback_rate)
                    trailing_algo_id = int(trailing_order["algoId"])
                    if trailing_algo_id not in ex.get_open_algo_order_ids():
                        raise RuntimeError(f"TRAILING_STOP_MARKET 응답은 성공(algoId={trailing_algo_id})이었으나 실제 활성 주문 목록에 없음")
                    pos.trailing_order_id = trailing_algo_id
                    if old_trailing_order_id:
                        ex.cancel_order(symbol, old_trailing_order_id)
                    if old_tp_fallback_order_id:
                        ex.cancel_order(symbol, old_tp_fallback_order_id)
                        pos.tp_fallback_order_id = None
                except Exception:
                    log.exception("[%s] 수량 변경 후 TRAILING_STOP_MARKET 재등록/검증 실패 — TAKE_PROFIT_MARKET 폴백 시도", symbol)
                    try:
                        tp_order = ex.place_take_profit_market(symbol, pos.side, pos.quantity, activation_price)
                        pos.tp_fallback_order_id = tp_order["algoId"]
                        log.info("[%s] 수량 변경 후 TAKE_PROFIT_MARKET 폴백 등록 성공", symbol)
                        if old_trailing_order_id:
                            ex.cancel_order(symbol, old_trailing_order_id)
                        if old_tp_fallback_order_id:
                            ex.cancel_order(symbol, old_tp_fallback_order_id)
                    except Exception:
                        log.critical("[%s] 수량 변경 후 TRAILING+TAKE_PROFIT_MARKET 폴백 모두 실패 — 기존 주문을 취소하지 않고 유지", symbol)
                        tg.notify_error(symbol, "수량변경 후 트레일링+폴백TP 등록 모두 실패(기존 주문 유지 중)")

    # [2026-08-07 QA 감사] GRVTUSDT 실측: 등록 로그는 남았는데 취소 로그 없이 거래소에서
    # STOP_MARKET이 사라져 포지션이 무방비로 남은 사고 발생(원인 특정 안 됨). 특정 이벤트에서만
    # 재등록하는 개별 패치로는 미지의 원인을 다 못 막으므로, 매 주기 전체 계좌의 살아있는 Algo
    # 주문 ID와 대조해 빠진 게 있으면 원인과 무관하게 자동 복구하는 안전망을 둔다.
    try:
        live_algo_orders = ex.get_open_algo_orders()
        live_algo_ids = {int(o["algoId"]) for o in live_algo_orders}
    except Exception:
        log.exception("Algo 주문 목록 조회 실패 — 이번 주기 STOP_MARKET 무결성 점검 생략")
        live_algo_orders = None
        live_algo_ids = None
    if live_algo_ids is not None:
        # [2026-08-08] list()로 스냅샷을 떠서 순회한다 — 이 루프 안에서 close_symbol_if_already_past_stop()이
        # 실패한 심볼을 즉시 청산하면 pm.untrack()이 pm.positions에서 항목을 지우는데, 그 상태로 원본
        # dict를 그대로 순회하면 "dictionary changed size during iteration"으로 reconcile 전체가 죽는
        # 실사고가 있었음(HEIUSDT, 11:18:43).
        for symbol, pos in list(pm.positions.items()):
            # [2026-08-07] stop_order_id가 아예 None인 경우(재시작 후 복원된 포지션 — 기존엔
            # 복원 시 STOP_MARKET을 등록하지 않아 재시작 때마다 무방비 구간이 생겼음)도 같은
            # 안전망으로 처리한다: "이미 있던 게 사라짐"과 "애초에 없었음"을 구분하지 않고
            # 둘 다 "지금 없으니 등록"으로 통일한다.
            missing = pos.stop_order_id is None or pos.stop_order_id not in live_algo_ids
            if missing:
                leverage = pos.leverage or 1.0
                stop_loss_pct, widened = compute_stop_loss_pct(cfg, pos.side, pos.entered_at)
                if pos.side == "LONG":
                    stop_price = pos.entry_price * (1 - stop_loss_pct / 100 / leverage)
                else:
                    stop_price = pos.entry_price * (1 + stop_loss_pct / 100 / leverage)
                existing_stop = ex.find_matching_stop_market_order(
                    live_algo_orders, symbol, pos.side, pos.quantity, stop_price,
                )
                if existing_stop:
                    pos.stop_order_id = int(existing_stop["algoId"])
                    pos.stop_loss_widened = widened
                    log.info("[%s] 기존 STOP_MARKET(algoId=%s)을 거래소에서 발견해 채택 — 중복 등록 생략", symbol, pos.stop_order_id)
                    continue
                if pos.stop_order_id:
                    log.warning("[%s] STOP_MARKET(algoId=%s)이 원인 불명으로 거래소에서 사라짐 — 재등록", symbol, pos.stop_order_id)
                else:
                    log.info("[%s] STOP_MARKET 미등록 상태(재시작 복원 등) 감지 — 신규 등록", symbol)
                try:
                    stop_order = ex.place_stop_market(symbol, pos.side, pos.quantity, stop_price)
                    pos.stop_order_id = stop_order["algoId"]
                    pos.stop_loss_widened = widened
                    tg.send(f"⚠️ {symbol} STOP_MARKET이 사라져 있던 걸 감지해 재등록했어요 (원인 불명, 안전망 작동)")
                except Exception:
                    log.exception("[%s] 사라진 STOP_MARKET 재등록 실패 — 기존 폴링 기반 손절로 대체됨", symbol)
                    # [2026-08-07] 같은 이유로(등록 실패=대부분 이미 손절가를 넘어선 상태) 다음 주기까지
                    # 기다리지 않고 여기서 바로 ROE를 확인해 이미 손절 기준을 넘었으면 즉시 청산한다.
                    try:
                        close_symbol_if_already_past_stop(ex, pm, cfg, symbol, tg)
                    except Exception:
                        log.exception("[%s] STOP_MARKET 실패 후 즉시 손절 확인 중 오류", symbol)
                continue

            # [2026-08-11 사용자요청] 진입 직후 유예기간 동안 넓혀뒀던 손절폭을, 유예기간이
            # 지나면 원래 폭으로 다시 좁힌다(무보호 구간 없이 — 새 주문 먼저 등록 후 기존 취소).
            if pos.stop_loss_widened and cfg.stop_loss_grace_sec > 0:
                tightened_pct, still_widened = compute_stop_loss_pct(cfg, pos.side, pos.entered_at)
                if not still_widened:
                    leverage = pos.leverage or 1.0
                    if pos.side == "LONG":
                        stop_price = pos.entry_price * (1 - tightened_pct / 100 / leverage)
                    else:
                        stop_price = pos.entry_price * (1 + tightened_pct / 100 / leverage)
                    old_stop_order_id = pos.stop_order_id
                    try:
                        stop_order = ex.place_stop_market(symbol, pos.side, pos.quantity, stop_price)
                        pos.stop_order_id = stop_order["algoId"]
                        pos.stop_loss_widened = False
                        if old_stop_order_id:
                            ex.cancel_order(symbol, old_stop_order_id)
                        log.info("[%s] 진입 유예기간(%.0f초) 만료 — 손절폭을 원래 값(%.2f%%)으로 다시 좁힘", symbol, cfg.stop_loss_grace_sec, tightened_pct)
                    except Exception:
                        log.exception("[%s] 유예기간 만료 후 손절폭 재타이트 실패 — 넓혀진 손절폭으로 유지(무보호보단 안전)", symbol)

        # [2026-08-09] 위 안전망을 다 거치고 나서도 손절 주문조차 없는(UNPROTECTED) 포지션이
        # 남아있다면 — 등록도 실패하고 즉시청산 폴백도 실패한 최악의 경우이므로 — 확실히
        # 눈에 띄도록 CRITICAL로 별도 보고한다.
        for symbol, pos in list(pm.positions.items()):
            if pos.protection_state == "UNPROTECTED":
                log.critical("[%s] 보호주문 전무(UNPROTECTED) 상태로 남아있음 — 즉시 확인 필요", symbol)
                tg.notify_error(symbol, "🚨 보호주문(STOP_MARKET) 전무 상태 — 즉시 확인 필요")


def force_close_for_funding_settlement(ex: Exchange, pm: PositionManager, cfg: Config, tg: TelegramNotifier):
    """[2026-08-05] 펀딩비 정산 전후(FUNDING_AVOID_MINUTES) 급변동 리스크를 피하기 위해 기존
    포지션도 강제 정리한다. 단, 이미 ROE가 funding_force_close_exempt_roe(기본 4%) 이상으로
    크게 수익 중이면 그대로 둔다(굳이 잘라낼 이유가 없음).

    [2026-08-06] 단, 정산 시각 직전(funding_unconditional_close_min, 기본 1분) 이내로 들어오면
    급변동이 임박한 것으로 보고 수익 여부와 무관하게(예외 없이) 전부 정리한다.
    """
    minutes_before = minutes_before_next_funding_settlement(cfg)
    unconditional = minutes_before <= cfg.funding_unconditional_close_min
    for symbol in list(pm.positions.keys()):
        pos = pm.positions.get(symbol)
        if pos is None:
            continue
        try:
            mark_price = ex.get_mark_price(symbol)
        except Exception:
            log.exception("[%s] 펀딩비 정산 강제청산 판단 중 가격 조회 실패", symbol)
            continue
        roe = pnl_pct(pos.entry_price, mark_price, pos.side) * pos.leverage
        if roe >= cfg.funding_force_close_exempt_roe and not unconditional:
            log.info("[%s] 펀딩비 정산 근접했지만 ROE=%.2f%%로 이미 크게 수익 중 — 강제청산 면제", symbol, roe)
            continue
        if unconditional:
            log.info("[%s] 펀딩비 정산 %.1f분 전 — 수익 여부와 무관하게 강제 정리", symbol, minutes_before)
        result_pnl = pnl_pct(pos.entry_price, mark_price, pos.side)
        result_pnl_usdt = net_profit_usdt(pos, mark_price, cfg)
        if pos.stop_order_id:
            ex.cancel_order(symbol, pos.stop_order_id)
        if pos.trailing_order_id:
            ex.cancel_order(symbol, pos.trailing_order_id)
        if pos.tp_fallback_order_id:
            ex.cancel_order(symbol, pos.tp_fallback_order_id)
        try:
            ex.close_market_position(symbol, pos.side, abs(pos.quantity))
            log.info("[%s] FUNDING_FORCE_CLOSE 로 포지션 청산 완료 (정산 리스크 회피, pnl=%.2f%%)", symbol, result_pnl)
        except Exception:
            log.exception("[%s] 펀딩비 정산 강제청산 실패", symbol)
            tg.notify_error(symbol, "펀딩비 정산 강제청산 실패")
            continue
        pm.untrack(symbol)
        pm.record_result(symbol, result_pnl, result_pnl_usdt, origin=pos.origin, side=pos.side, leverage=pos.leverage)
        record_trade_ledger(cfg, pos, symbol, "FUNDING_FORCE_CLOSE", mark_price, result_pnl, result_pnl_usdt, ex)
        tg.notify_exit(symbol, "FUNDING_FORCE_CLOSE", result_pnl)


def widen_exchange_trailing_for_swing(ex: Exchange, pos, cfg: Config, mark_price: float) -> None:
    """[2026-08-10 사용자요청] "스윙은 포지션의 최대일 때뿐" — 이미 익절 문턱을 넘어
    armed된(고점을 갱신해가는) 상태에서 스윙 국면이 감지되면, 거래소에 걸어둔
    TRAILING_STOP_MARKET을 더 넓은 콜백폭으로 재등록한다. 봇 내부 판단(evaluate())만
    넓혀서는, 거래소 주문이 좁은 폭 그대로 남아있어 거래소가 봇보다 먼저 청산해버리는
    문제(실측: BICOUSDT 진입 56초만에 EXTERNAL_CLOSE)가 그대로 남기 때문이다. 실패해도
    예외를 삼킨다 — 기존(좁은) 거래소 주문이 계속 안전망으로 남아있으므로 무보호 상태가
    되지는 않는다."""
    if not pos.armed or pos.exchange_trailing_widened:
        return
    try:
        callback_rate = (cfg.trail_drawdown_pct * cfg.swing_exchange_trailing_multiplier) / pos.leverage
        # activation_price를 현재가 근처로 둬서 재등록 즉시 트레일링이 활성 상태를 유지하게 한다
        # (이미 armed라 포지션이 익절 문턱을 넘어선 상태이므로, 여기서 다시 트레일링을 처음부터
        # 시작해도 안전 — 손절선(-6% ROE)은 별도 STOP_MARKET이 계속 지켜준다).
        if pos.side == "LONG":
            activation_price = mark_price * 0.999
        else:
            activation_price = mark_price * 1.001
        new_order = ex.place_trailing_stop_market(pos.symbol, pos.side, abs(pos.quantity), activation_price, callback_rate)
        new_algo_id = int(new_order["algoId"])
        if new_algo_id not in ex.get_open_algo_order_ids():
            raise RuntimeError(f"스윙 확장 TRAILING_STOP_MARKET 응답은 성공(algoId={new_algo_id})이었으나 실제 활성 주문 목록에 없음")
        old_trailing_order_id = pos.trailing_order_id
        pos.trailing_order_id = new_algo_id
        pos.exchange_trailing_widened = True
        if old_trailing_order_id:
            ex.cancel_order(pos.symbol, old_trailing_order_id)
        log.info("[%s] 스윙 국면 감지 — 거래소 트레일링 콜백폭 확장 재등록(콜백=%.2f%%)", pos.symbol, callback_rate)
    except Exception:
        log.exception("[%s] 스윙 확장 트레일링 재등록 실패 — 기존(좁은 폭) 거래소 주문이 계속 보호", pos.symbol)


def process_tracked_position(ex: Exchange, pm: PositionManager, cfg: Config, symbol: str, tg: TelegramNotifier, hourly_check_state: dict):
    """익절/손절/시간초과 청산, 조기 탈출, 1시간봉 재평가(약손절), 물타기를 처리한다.
    외부 종료/신규 진입/수량 변경 감지는 reconcile_positions()가 매 주기 앞단에서 처리한다."""
    if symbol not in pm.positions:
        return  # reconcile_positions에서 이미 외부 종료로 정리됨

    mark_price = ex.get_mark_price(symbol)

    # [2026-08-10] 트레일링 확정 직전 "지금도 여전히 크게 움직이는 중인지"를 판단하기 위한
    # 캔들 조회 — 실패해도(일시적 REST 오류 등) 모멘텀 지속 여부를 모른다고 보수적으로
    # False 처리해 기존 트레일링 동작(즉시 확정)으로 안전하게 폴백한다.
    momentum_continuing = False
    swing_continuing = False
    try:
        pos_for_momentum = pm.positions[symbol]
        recent_df = add_indicators(ex.get_klines(symbol), cfg)
        momentum_continuing = is_momentum_continuing(recent_df, cfg, pos_for_momentum.side)
        # [2026-08-10 사용자요청] "양봉이 많아지면 스윙으로 보고 익절구간을 최대한 늘려라" —
        # 캔들 하나가 아니라 최근 N개가 연속으로 강하게 같은 방향이면 스윙 국면으로 보고
        # 익절 절대상한/트레일링 허용폭을 크게 넓힌다.
        swing_continuing = is_swing_continuing(recent_df, cfg, pos_for_momentum.side)
        if swing_continuing:
            # [2026-08-10 사용자요청] "스윙은 포지션의 최대일 때뿐" — armed(이미 익절 문턱
            # 도달) 상태가 아니면 함수 내부에서 그냥 조용히 아무 것도 안 하고 반환한다.
            widen_exchange_trailing_for_swing(ex, pos_for_momentum, cfg, mark_price)
    except Exception:
        log.exception("[%s] 모멘텀/스윙 지속 판단용 캔들 조회 실패 — 기존 트레일링 동작으로 폴백", symbol)

    action = pm.evaluate(
        symbol, mark_price, momentum_continuing=momentum_continuing, swing_continuing=swing_continuing
    )

    if action == "TIME_STOP":
        pos = pm.positions[symbol]
        # [2026-08-05] 실거래로 확인된 버그: 추세 면제에 상한이 없어서 상위시간대가 계속
        # 지지한다고 나오면 무한정 슬롯을 차지함(MONUSDT 3시간+ 실측). scalp_max_hold_minutes의
        # 2배를 절대 상한으로 둬서, 그 이상 지나면 추세 지지 여부와 무관하게 강제 정리한다.
        held_minutes = (time.time() - pos.entered_at) / 60
        # [2026-08-05] 사용자 피드백으로 상한을 300분(2배)에서 180분(150분+30분 여유)으로 축소
        hard_cap_minutes = cfg.scalp_max_hold_minutes + 30
        agree, total = mtf_trend_alignment(ex, cfg, symbol, pos.side)
        if total > 0 and agree / total >= cfg.time_stop_trend_exempt_min_ratio and held_minutes < hard_cap_minutes:
            log.info(
                "[%s] TIME_STOP 대상이지만 상위시간대 추세(%d/%d) 여전히 지지 — 강제청산 면제, 트레일링/손절에 계속 맡김 (%.0f/%.0f분)",
                symbol, agree, total, held_minutes, hard_cap_minutes,
            )
            action = None
        elif held_minutes >= hard_cap_minutes:
            log.info(
                "[%s] TIME_STOP 절대 상한(%.0f분) 초과 — 추세 지지 여부와 무관하게 강제 정리",
                symbol, hard_cap_minutes,
            )

    if action in ("TAKE_PROFIT", "STOP_LOSS", "TIME_STOP"):
        pos = pm.positions[symbol]
        result_pnl = pnl_pct(pos.entry_price, mark_price, pos.side)
        result_pnl_usdt = net_profit_usdt(pos, mark_price, cfg)
        if pos.stop_order_id:
            ex.cancel_order(symbol, pos.stop_order_id)
        if pos.trailing_order_id:
            ex.cancel_order(symbol, pos.trailing_order_id)
        if pos.tp_fallback_order_id:
            ex.cancel_order(symbol, pos.tp_fallback_order_id)
        try:
            ex.close_market_position(symbol, pos.side, abs(pos.quantity))
            log.info("[%s] %s 로 포지션 청산 완료 (pnl=%.2f%%)", symbol, action, result_pnl)
        except Exception:
            log.exception("[%s] 포지션 청산 실패", symbol)
            tg.notify_error(symbol, "포지션 청산 실패")
            return
        pm.untrack(symbol)
        pm.record_result(symbol, result_pnl, result_pnl_usdt, origin=pos.origin, side=pos.side, leverage=pos.leverage)
        record_trade_ledger(cfg, pos, symbol, action, mark_price, result_pnl, result_pnl_usdt, ex)
        tg.notify_exit(symbol, action, result_pnl)
    elif check_early_exit(ex, pm, cfg, symbol):
        pos = pm.positions[symbol]
        mark_price = ex.get_mark_price(symbol)
        result_pnl = pnl_pct(pos.entry_price, mark_price, pos.side)
        result_pnl_usdt = net_profit_usdt(pos, mark_price, cfg)
        if pos.stop_order_id:
            ex.cancel_order(symbol, pos.stop_order_id)
        if pos.trailing_order_id:
            ex.cancel_order(symbol, pos.trailing_order_id)
        if pos.tp_fallback_order_id:
            ex.cancel_order(symbol, pos.tp_fallback_order_id)
        try:
            ex.close_market_position(symbol, pos.side, abs(pos.quantity))
            log.info("[%s] EARLY_EXIT 로 포지션 청산 완료 (추세 전환 감지, pnl=%.2f%%)", symbol, result_pnl)
        except Exception:
            log.exception("[%s] 조기 탈출 청산 실패", symbol)
            tg.notify_error(symbol, "조기 탈출 청산 실패")
            return
        pm.untrack(symbol)
        pm.record_result(symbol, result_pnl, result_pnl_usdt, origin=pos.origin, side=pos.side, leverage=pos.leverage)
        record_trade_ledger(cfg, pos, symbol, "EARLY_EXIT", mark_price, result_pnl, result_pnl_usdt, ex)
        tg.notify_exit(symbol, "EARLY_EXIT", result_pnl)
    elif check_profit_should_lock_in(ex, pm, cfg, symbol):
        pos = pm.positions[symbol]
        mark_price = ex.get_mark_price(symbol)
        result_pnl = pnl_pct(pos.entry_price, mark_price, pos.side)
        result_pnl_usdt = net_profit_usdt(pos, mark_price, cfg)
        if pos.stop_order_id:
            ex.cancel_order(symbol, pos.stop_order_id)
        if pos.trailing_order_id:
            ex.cancel_order(symbol, pos.trailing_order_id)
        if pos.tp_fallback_order_id:
            ex.cancel_order(symbol, pos.tp_fallback_order_id)
        try:
            ex.close_market_position(symbol, pos.side, abs(pos.quantity))
            log.info("[%s] TAKE_PROFIT 로 포지션 청산 완료 (모멘텀 약화 감지, ROE 기준 확정, pnl=%.2f%%)", symbol, result_pnl)
        except Exception:
            log.exception("[%s] 모멘텀 약화 청산 실패", symbol)
            tg.notify_error(symbol, "익절 확정 청산 실패")
            return
        pm.untrack(symbol)
        pm.record_result(symbol, result_pnl, result_pnl_usdt, origin=pos.origin, side=pos.side, leverage=pos.leverage)
        record_trade_ledger(cfg, pos, symbol, "TAKE_PROFIT_MOMENTUM_LOCK", mark_price, result_pnl, result_pnl_usdt, ex)
        tg.notify_exit(symbol, "TAKE_PROFIT", result_pnl)
    elif check_hourly_soft_stop(ex, cfg, pm, symbol, hourly_check_state):
        pos = pm.positions[symbol]
        mark_price = ex.get_mark_price(symbol)
        result_pnl = pnl_pct(pos.entry_price, mark_price, pos.side)
        result_pnl_usdt = net_profit_usdt(pos, mark_price, cfg)
        if pos.stop_order_id:
            ex.cancel_order(symbol, pos.stop_order_id)
        if pos.trailing_order_id:
            ex.cancel_order(symbol, pos.trailing_order_id)
        if pos.tp_fallback_order_id:
            ex.cancel_order(symbol, pos.tp_fallback_order_id)
        try:
            ex.close_market_position(symbol, pos.side, abs(pos.quantity))
            log.info("[%s] SOFT_STOP 로 포지션 청산 완료 (1시간봉 재평가, pnl=%.2f%%)", symbol, result_pnl)
        except Exception:
            log.exception("[%s] 약손절 청산 실패", symbol)
            tg.notify_error(symbol, "약손절 청산 실패")
            return
        pm.untrack(symbol)
        pm.record_result(symbol, result_pnl, result_pnl_usdt, origin=pos.origin, side=pos.side, leverage=pos.leverage)
        record_trade_ledger(cfg, pos, symbol, "SOFT_STOP", mark_price, result_pnl, result_pnl_usdt, ex)
        tg.notify_exit(symbol, "SOFT_STOP", result_pnl)
    else:
        try_average_down(ex, pm, cfg, symbol, tg)


def scan_entry_candidate(ex: Exchange, cfg: Config, symbol: str, total_balance: float) -> dict | None:
    """진입 신호가 있는지만 확인하고, 있으면 후보 정보를 반환한다 (실제 주문은 내지 않음).
    이번 주기의 모든 후보를 모은 뒤 가장 강도가 높은 것부터 진입하기 위함이다."""
    df = ex.get_klines(symbol)

    # 절대 유동성 하한선: 최근 캔들 평균 거래대금이 너무 작으면 지표 계산 전에 바로 제외한다
    # (극도로 얇은 코인은 진입해도 가격이 거의 안 움직여 포지션이 정체되는 문제가 실제로 있었음).
    avg_quote_volume = (df["volume"] * df["close"]).tail(20).mean()
    if avg_quote_volume < cfg.min_avg_quote_volume_usdt:
        return None

    df = add_indicators(df, cfg)
    min_confirmations = compute_min_confirmations(total_balance, cfg)
    signal, probability = generate_signal_with_probability(df, cfg, min_confirmations=min_confirmations)
    if signal is None:
        return None

    # 신호 신뢰도+추세강도로 추정한 성공 확률이 기준 미만이면 진입하지 않는다.
    # (변동성만 보고 들어가면 반등/역행 위험도 같이 커져서 손절이 늘어나므로 여기서 걸러냄)
    # 숏은 급반등(양봉)에 취약해 손실이 잦았으므로 더 높은 확률 기준을 요구한다.
    required_probability = cfg.short_min_entry_probability if signal == "SHORT" else cfg.min_entry_probability
    if probability < required_probability:
        return None

    # [2026-08-13 감사수정] whipsaw 필터가 실제(신호강도 기반) 레버리지를 쓰도록 strength를
    # 여기서 먼저 계산한다 (signal_strength는 df/cfg만 필요해 이 시점에 이미 계산 가능).
    strength = signal_strength(df, cfg)
    entry_leverage = compute_leverage(strength, cfg)
    if not passes_whipsaw_volatility_filter(ex, cfg, symbol, signal, leverage=entry_leverage):
        return None

    if not passes_one_min_noise_filter(ex, cfg, symbol, signal):
        return None

    if signal == "SHORT" and not passes_short_scalp_reversal_filter(ex, cfg, symbol):
        return None

    if not passes_entry_range_position_filter(ex, cfg, symbol, signal):
        return None

    # 이미 과열됐거나(볼린저 밴드 이탈), 지금 이 캔들이 반대 방향으로 마감 중이면
    # 진입 직후 불리하게 시작할 위험이 커서 제외한다.
    if not immediate_momentum_ok(df, signal):
        return None

    # 5) 펀딩비가 극단적이면 시장이 한쪽으로 쏠려있다는(고래의 대량 포지션 등) 신호다.
    # 진입 자체는 막지 않되 "고래성 코인"으로 표시해서, execute_entry에서 비중/레버리지/
    # 마진타입에 특별히 더 엄격한 제한을 강제한다 (비중 10%, 레버리지 3배, 격리 마진).
    whale_signal = False
    try:
        funding_rate = ex.get_funding_rate_pct(symbol)
        whale_signal = abs(funding_rate) > cfg.max_abs_funding_rate_pct
    except Exception:
        pass  # 조회 실패해도 신호 자체를 막지는 않음

    # 단순 거래량이 아니라 실제 매수/매도 체결 비율로 방향성을 다시 확인한다.
    if not volume_direction_ok(df, signal, cfg):
        return None

    speed = quick_profit_score(df, cfg, signal)
    btc_mult = btc_alignment_multiplier(ex, cfg, signal)

    # 텔레그램 코인분석과 같은 100점 만점 종합점수 게이트. 롱/숏 각각 다른 기준을 쓴다
    # (숏이 더 보수적). 여기까지 왔다는 건 immediate_momentum_ok/volume_direction_ok를
    # 이미 통과했다는 뜻이므로, 텔레그램 분석과 동일하게 그 두 항목은 만점(10/10)으로 계산한다.
    agree, total = mtf_trend_alignment(ex, cfg, symbol, signal)
    mtf_ratio = (agree / total) if total else 0.0
    total_score = probability * 50 + speed * 20 + 10 + 10 + mtf_ratio * 10
    total_score += (btc_mult - 1.0) * 15.0
    min_total_score = cfg.short_min_total_score if signal == "SHORT" else cfg.long_min_total_score
    if total_score < min_total_score:
        return None

    if signal == "SHORT":
        short_reversal_risk = assess_short_reversal_risk(ex, cfg, symbol)
        # 숏은 스윙이 아니라 스캘핑으로만 진입한다: 5분봉 기준으로 5분 내 목표 익절에
        # 도달할 만큼 변동성이 있을 때만 진입한다 (롱은 이 추가 체크가 없음, 스윙 유지).
        try:
            df_5m = ex.get_klines(symbol, interval="5m")
            df_5m = add_indicators(df_5m, cfg)
            curr_5m = df_5m.iloc[-1]
            atr_pct_5m = (curr_5m["atr"] / curr_5m["close"] * 100) if curr_5m["close"] > 0 else 0.0
        except Exception:
            atr_pct_5m = 0.0
        expected_leverage = compute_leverage(strength, cfg)
        target_price_move_pct = cfg.short_take_profit_min / max(expected_leverage, 1)
        if atr_pct_5m < target_price_move_pct:
            return None  # 5분봉 변동성으로 봤을 때 5분 내 목표 도달 가능성이 낮음
    else:
        short_reversal_risk = {"risky": False, "reasons": []}

    # [2026-08-13 사용자요청] 진입 신호가 나온 이 캔들 자체가 최근 평균 변동폭(ATR) 대비
    # 비정상적으로 크면(chase_entry_range_mult배 이상) "소진성 스파이크" 추격진입일
    # 위험이 있다 — 진입은 막지 않고 execute_entry에서 비중만 소폭 축소한다.
    curr_row = df.iloc[-1]
    candle_range = float(curr_row["high"]) - float(curr_row["low"])
    atr_value = float(curr_row.get("atr", 0.0) or 0.0)
    chase_entry = bool(atr_value > 0 and candle_range >= atr_value * cfg.chase_entry_range_mult)
    btc_momentum_opposes = btc_short_term_momentum_opposes(ex, cfg, signal)
    price = float(df.iloc[-1]["close"])
    candle_time = df.iloc[-1]["open_time"]
    # 진입 성공 확률(probability)을 최우선으로 반영하고, 신호 강도(strength)와
    # 목표 도달 가능성(speed)은 그다음 순위로 참고한다. 예전엔 확률을 아예 반영하지
    # 않아서, 확률 80%인 후보보다 강도/속도만 우연히 높은 70%짜리가 먼저 뽑히는
    # 문제가 있었다 (실사용 중 발견됨).
    combined_score = probability * 0.7 + strength * 0.2 + speed * 0.1
    entry_priority = combined_score
    if short_reversal_risk.get("risky"):
        entry_priority -= max(0.0, getattr(cfg, "short_reversal_risk_priority_penalty", 0.08))
    return {
        "symbol": symbol, "signal": signal, "strength": strength,
        "speed": speed, "score": combined_score, "probability": probability,
        "entry_priority": entry_priority,
        "short_reversal_risk": bool(short_reversal_risk.get("risky")),
        "short_reversal_reasons": short_reversal_risk.get("reasons", []),
        "price": price, "candle_time": candle_time, "whale_signal": whale_signal,
        "avg_quote_volume": avg_quote_volume,
        "btc_mult": btc_mult,
        "chase_entry": chase_entry,
        "btc_momentum_opposes": btc_momentum_opposes,
    }


def confirm_two_candle(pending_signals: dict, cfg: Config, candidate: dict) -> bool:
    """2봉 연속 확인: 신호가 처음 뜬 캔들에는 바로 진입하지 않고, 다음 캔들에서도
    같은 방향 신호가 유지되는지 한 번 더 확인한다 (한 캔들짜리 노이즈/가짜 신호 필터).
    REQUIRE_TWO_CANDLE_CONFIRMATION=false로 끄면 예전처럼 즉시 진입한다."""
    if not cfg.require_two_candle_confirmation:
        return True

    symbol, signal, candle_time = candidate["symbol"], candidate["signal"], candidate["candle_time"]
    prior = pending_signals.get(symbol)
    if prior and prior["side"] == signal and prior["candle_time"] != candle_time:
        del pending_signals[symbol]
        return True
    pending_signals[symbol] = {"side": signal, "candle_time": candle_time}
    return False


def should_confirm_candidate_two_candle(pending_signals: dict, cfg: Config, candidate: dict, total_balance: float) -> bool:
    if (
        is_low_balance_recovery_mode(total_balance, cfg)
        and getattr(cfg, "low_balance_recovery_skip_two_candle_confirmation", True)
    ):
        return True
    return confirm_two_candle(pending_signals, cfg, candidate)


def entry_market_quality_ok(ex: Exchange, cfg: Config, symbol: str) -> bool:
    """진입 직전 bid/ask 스프레드가 너무 넓으면 수수료를 이기기 어렵다.

    조회 실패는 진입 차단 사유로 쓰지 않는다. 실거래 환경에서 Binance 일시 오류 하나로
    모든 신호를 멈추기보다, 확인 가능한 경우에만 "나쁜 체결 환경" 후보를 걸러낸다.
    """
    max_spread = getattr(cfg, "max_entry_spread_pct", 0.0)
    if max_spread <= 0:
        return True
    if not hasattr(ex, "get_book_ticker"):
        return True
    try:
        book = ex.get_book_ticker(symbol)
        bid = float(book.get("bid", 0.0))
        ask = float(book.get("ask", 0.0))
    except Exception:
        log.exception("[%s] 진입 직전 스프레드 조회 실패 — 기존 로직대로 진행", symbol)
        return True
    mid = (bid + ask) / 2
    if bid <= 0 or ask <= 0 or mid <= 0:
        return True
    spread_pct = (ask - bid) / mid * 100
    if spread_pct > max_spread:
        log.info(
            "[%s] 진입 직전 스프레드 %.3f%% > 허용 %.3f%% — 체결비용 과다로 진입 생략",
            symbol, spread_pct, max_spread,
        )
        return False
    return True


def place_entry_order(ex: Exchange, cfg: Config, symbol: str, side: str, quantity: float) -> bool:
    """[2026-08-10] 진입 주문을 낸다. cfg.limit_entry_enabled가 꺼져있으면(기본값) 기존
    시장가 그대로. 켜져있으면 지정가(메이커: LONG=매수호가, SHORT=매도호가)로 걸고
    limit_entry_wait_sec 동안 체결을 기다린다 — 슬리피지 리스크를 없애는 게 목적이라,
    시간 안에 안 채워지면 시장가로 쫓아가지 않고 그냥 취소하고 포기한다(반환값 False,
    호출부가 다른 후보로 그 슬롯을 채우게 함). 부분체결이 있었으면 그 체결분은 그대로
    추적한다(True 반환 — 호출부의 포지션 폴링이 실제 수량을 다시 조회해서 잡아준다)."""
    if not cfg.limit_entry_enabled:
        ex.open_market_position(symbol, side, quantity)
        return True

    book = ex.get_book_ticker(symbol)
    pullback_pct = max(0.0, getattr(cfg, "limit_entry_pullback_pct", 0.0)) / 100
    if side == "LONG":
        price = float(book["bid"]) * (1 - pullback_pct)
    else:
        price = float(book["ask"]) * (1 + pullback_pct)
    order = ex.open_limit_position(symbol, side, quantity, price)
    order_id = order["orderId"]
    deadline = time.time() + cfg.limit_entry_wait_sec
    while time.time() < deadline:
        status = ex.get_order_status(symbol, order_id)
        if status.get("status") == "FILLED":
            return True
        time.sleep(0.5)

    status = ex.get_order_status(symbol, order_id)
    executed_qty = float(status.get("executedQty", 0) or 0)
    ex.cancel_regular_order(symbol, order_id)
    if executed_qty > 0:
        log.info("[%s] 지정가 진입 부분체결(%.6g) 후 나머지 취소 — 체결분만 추적", symbol, executed_qty)
        return True
    log.info("[%s] 지정가 진입 %.0f초 내 전혀 미체결 — 취소하고 진입 포기(슬리피지 리스크 최소화가 목적)",
              symbol, cfg.limit_entry_wait_sec)
    return False


def execute_entry(ex: Exchange, pm: PositionManager, cfg: Config, tg: TelegramNotifier, total_balance: float, candidate: dict) -> bool:
    """선택된 후보에 실제로 진입한다. 성공하면 True."""
    symbol, signal, strength, price = candidate["symbol"], candidate["signal"], candidate["strength"], candidate["price"]
    probability = candidate.get("probability", 0.0)

    # 여러 심볼을 스캔하는 동안 시간이 지나 가격이 이미 불리하게 움직였으면
    # (신호가 낡음) 이 진입은 건너뛴다 — "진입하자마자 마이너스로 시작" 방지.
    # [2026-08-11 사용자요청] REST 왕복(get_mark_price) 대신 WS 캐시 즉시조회(get_last_price_fast)
    # 사용 — 이 조회 자체가 걸리는 시간 동안 가격이 더 움직여서 재검증에 계속 실패하던
    # 문제를 줄인다(WS 없거나 오래됐으면 기존과 동일하게 REST로 안전하게 폴백).
    fresh_price = ex.get_last_price_fast(symbol)
    adverse_move_pct = ((price - fresh_price) / price * 100) if signal == "LONG" else ((fresh_price - price) / price * 100)
    if adverse_move_pct > cfg.stale_signal_tolerance_pct:
        log.info(
            "[%s] 신호 이후 가격이 %.2f%% 불리하게 움직여 진입 생략 (스캔가=%s, 현재가=%s)",
            symbol, adverse_move_pct, price, fresh_price,
        )
        return False
    price = fresh_price

    if not entry_market_quality_ok(ex, cfg, symbol):
        return False

    # [2026-08-07] 사용자 요청: 진입 직전(주문 내기 바로 전) 방향이 여전히 유효한지 한 번 더
    # 확인한다 — 스캔 시점엔 방향이 맞았어도 그 사이 캔들이 반전됐을 수 있다("불나방" 방지).
    # 가격 괴리(위 stale_signal_tolerance_pct)만으론 못 잡는, "가격은 크게 안 움직였지만
    # 방향 자체가 뒤집힌" 경우를 걸러낸다.
    try:
        fresh_df = ex.get_klines(symbol)
        if not immediate_momentum_ok(fresh_df, signal):
            log.info("[%s] 진입 직전 재검증: 캔들 방향이 %s와 반대로 바뀜 — 진입 생략", symbol, signal)
            return False
    except Exception:
        log.exception("[%s] 진입 직전 방향 재검증 실패 — 원래 신호대로 진행", symbol)

    balance = ex.get_available_balance_usdt()
    leverage = compute_leverage(strength, cfg)

    # [2026-08-05] 실거래 20건 분석 결과 estimate_entry_probability가 승패와 상관관계가 없음
    # (오히려 패배 건 평균확률 95.8%가 승리 건 94.3%보다 높게 나옴)이 확인됨 — 근거 없는 확률
    # 숫자로 비중을 75~100%까지 키우는 건 예측력 없는 신호로 리스크만 키우는 것이라 판단해
    # 확률 기반 비중 확대를 제거한다. 비중은 항상 연속승리 기반 기본 로직만 따른다.
    if balance < cfg.small_balance_threshold:
        max_ratio = cfg.small_balance_max_ratio
    else:
        large_balance_cap = compute_large_balance_ratio_cap(balance, cfg)
        max_ratio = large_balance_cap if large_balance_cap is not None else cfg.position_size_max
    base_ratio_before_defense = min(
        cfg.position_size_min + pm.win_streak * cfg.position_size_step,
        max_ratio,
    )
    ratio = pm.next_position_size_ratio(balance, symbol=symbol)

    btc_mult = candidate.get("btc_mult", 1.0)
    if btc_mult != 1.0:
        log.info("[%s] BTC 정렬 배율 %.0f%% 적용", symbol, btc_mult * 100)
    ratio *= btc_mult

    direction_mult = pm.direction_size_multiplier(signal)
    if direction_mult != 1.0:
        log.info("[%s] %s 방향별 성과 배율 %.0f%% 적용", symbol, signal, direction_mult * 100)
    ratio *= direction_mult

    recent_perf_mult = pm.recent_performance_size_multiplier()
    if recent_perf_mult != 1.0:
        log.info("[%s] 최근 계좌 성과 방어 배율 %.0f%% 적용", symbol, recent_perf_mult * 100)
    ratio *= recent_perf_mult

    ev_defense_mult = pm.expected_value_size_multiplier()
    if ev_defense_mult != 1.0:
        log.info("[%s] 기대값 방어 배율 %.0f%% 적용", symbol, ev_defense_mult * 100)
    ratio *= ev_defense_mult

    aggregate_mult = aggregate_risk_size_multiplier(pm, cfg, total_balance)
    if aggregate_mult != 1.0:
        log.info("[%s] 상관리스크 방어 배율 %.0f%% 적용", symbol, aggregate_mult * 100)
    ratio *= aggregate_mult

    if is_low_balance_recovery_mode(total_balance, cfg):
        recovery_mult = max(0.0, min(1.0, getattr(cfg, "low_balance_recovery_size_mult", 1.0)))
        ratio *= recovery_mult
        log.info(
            "[%s] 저잔고 복구모드 — 고확률 후보만 진입, 비중 %.0f%% 추가 축소",
            symbol, recovery_mult * 100,
        )

    if candidate.get("short_reversal_risk"):
        reversal_mult = max(0.0, min(1.0, getattr(cfg, "short_reversal_risk_size_mult", 0.65)))
        ratio *= reversal_mult
        log.info(
            "[%s] SHORT 되감기 위험(%s) — 진입은 유지하고 비중 %.0f%% 적용",
            symbol, ",".join(candidate.get("short_reversal_reasons", [])) or "unknown", reversal_mult * 100,
        )

    if candidate.get("negative_ev_symbol"):
        symbol_ev_mult = max(0.0, min(1.0, getattr(cfg, "negative_ev_symbol_size_mult", 0.60)))
        ratio *= symbol_ev_mult
        log.info(
            "[%s] 심볼별 기대값 마이너스 — 거래수 유지를 위해 진입은 허용하고 비중 %.0f%% 적용",
            symbol, symbol_ev_mult * 100,
        )

    # [2026-08-07] 평소 유동성(최근 20개 캔들 평균 거래대금)이 낮을수록 비중을 축소한다.
    # 신호 판단에 쓰는 "이번 캔들 거래량 급증"은 고래 한 명이 만들 수 있어 비중 확대 근거로
    # 쓰면 위험하므로, 스파이크에 덜 흔들리는 평균값만 비중 스케일링에 사용한다.
    # min_avg_quote_volume_usdt(진입 컷 하한)에서 liquidity_size_min_mult,
    # liquidity_size_full_usdt 이상에서 1.0배가 되도록 선형 보간한다.
    avg_quote_volume = candidate.get("avg_quote_volume")
    if avg_quote_volume is not None:
        floor = cfg.min_avg_quote_volume_usdt
        full = cfg.liquidity_size_full_usdt
        min_mult = cfg.liquidity_size_min_mult
        if full > floor:
            span_ratio = (avg_quote_volume - floor) / (full - floor)
            liquidity_mult = min_mult + max(0.0, min(1.0, span_ratio)) * (1 - min_mult)
        else:
            liquidity_mult = 1.0
        if liquidity_mult < 1.0:
            log.info(
                "[%s] 평소 유동성 낮음(평균 거래대금=%.0fUSDT) — 비중을 %.0f%%로 축소",
                symbol, avg_quote_volume, liquidity_mult * 100,
            )
        ratio *= liquidity_mult

    if candidate.get("chase_entry"):
        chase_mult = max(0.0, min(1.0, cfg.chase_entry_size_mult))
        ratio *= chase_mult
        log.info(
            "[%s] 진입캔들 변동폭이 평균 변동폭(ATR)의 %.1f배 이상(소진성 스파이크 의심) — 비중 %.0f%%로 소폭 축소",
            symbol, cfg.chase_entry_range_mult, chase_mult * 100,
        )

    if candidate.get("btc_momentum_opposes"):
        btc_mom_mult = max(0.0, min(1.0, cfg.btc_momentum_gate_size_mult))
        ratio *= btc_mom_mult
        log.info(
            "[%s] BTC 최근 %d분 모멘텀이 신호 방향과 반대(%.2f%%p 이상) — 비중 %.0f%%로 축소",
            symbol, cfg.btc_momentum_gate_window_min, cfg.btc_momentum_gate_threshold_pct, btc_mom_mult * 100,
        )

    # [2026-08-12 사용자요청] "방어배율이 곱셈으로 겹쳐서 수량이 0이 돼 스킵되는" 문제 —
    # 실측: 오늘 이렇게 스킵된 80건을 진입시켰다고 가정하고 1분봉으로 재현한 결과 승률
    # 80.8%(59승14패, 20분내 TP/SL 선착 기준)로 확인돼, 개별 방어 로직들은 각각 합리적이어도
    # 여러 개가 동시에 겹치면 좋은 신호까지 통째로 놓치는 게 더 큰 손해로 판단. 아무리 겹쳐도
    # 최초 비중(base_ratio_before_defense)의 이 비율 밑으로는 안 내려가도록 하한을 둔다.
    if base_ratio_before_defense > 0:
        floor_ratio = base_ratio_before_defense * cfg.defense_stack_min_ratio_mult
        if 0 < ratio < floor_ratio:
            log.info(
                "[%s] 방어배율 누적으로 비중이 하한(%.0f%%) 밑으로 내려가 하한 적용: %.2f%% -> %.2f%%",
                symbol, cfg.defense_stack_min_ratio_mult * 100, ratio * 100, floor_ratio * 100,
            )
            ratio = floor_ratio

    # [2026-08-09] 계좌 소진 방지 강화 요청으로 기본 마진 타입을 교차(CROSS)->격리(ISOLATED)로
    # 전환. 교차는 한 포지션의 손실이 계좌 전체 증거금을 갉아먹을 수 있는데, 격리는 그 포지션의
    # 증거금으로만 손실이 제한된다("고래성 코인"에만 강제하던 걸 전체로 확대). 레버리지는
    # 기존처럼 4배 고정.
    whale_signal = candidate.get("whale_signal", False)
    ex.set_margin_type(symbol, "ISOLATED")
    if whale_signal:
        # 고래성(펀딩비 극단) 코인은 진입은 허용하되, 총자산의 10%까지만/레버리지 3배까지만 제한한다.
        leverage = min(leverage, cfg.whale_max_leverage)
        max_margin_allowed = total_balance * cfg.whale_max_position_ratio
        if balance > 0:
            ratio = min(ratio, max_margin_allowed / balance)
        log.info(
            "[%s] 고래성 코인 감지 — 레버리지<=%dx, 비중<=총자산의%.0f%% 추가 제한",
            symbol, cfg.whale_max_leverage, cfg.whale_max_position_ratio * 100,
        )

    min_margin_usdt = compute_min_margin(total_balance, cfg)
    if cfg.risk_based_sizing_enabled:
        # [2026-08-10] 검증 후 옵트인 연결. 기본은 False라 .env에서 명시적으로 켜지 않는 한
        # 이 분기는 실행되지 않고 기존 compute_position_size(잔고비율 기반)가 그대로 쓰인다.
        # [2026-08-10] risk-based sizing에서도 위에서 계산한 방어 배율(손실 후 재진입 감속,
        # BTC/방향/최근성과/EV/상관리스크/유동성)을 실제 수량에 반영한다. 기존에는 로그에는
        # 배율 적용이 찍혀도 risk_amount가 고정이라 최종 수량이 거의 줄지 않는 사각지대가 있었다.
        risk_size_mult = 1.0
        if base_ratio_before_defense > 0:
            risk_size_mult = max(0.0, min(1.0, ratio / base_ratio_before_defense))
        effective_risk_pct = (cfg.risk_based_sizing_pct / 100) * risk_size_mult
        if risk_size_mult < 1.0:
            log.info("[%s] 리스크 기반 수량에도 방어 배율 %.0f%% 반영", symbol, risk_size_mult * 100)
        _risk_sizing_stop_loss_pct, _ = compute_stop_loss_pct(cfg, signal)
        stop_price = price * (1 - _risk_sizing_stop_loss_pct / 100 / leverage) if signal == "LONG" \
            else price * (1 + _risk_sizing_stop_loss_pct / 100 / leverage)
        quantity = compute_risk_based_position_size(
            balance, symbol, ex, price, stop_price,
            effective_risk_pct, leverage, min_margin_usdt,
            cfg.small_balance_min_margin_usdt,
        )
    else:
        quantity = compute_position_size(balance, symbol, ex, price, ratio, leverage, min_margin_usdt)
    if quantity <= 0:
        log.warning("[%s] 계산된 수량이 0 이하입니다. 진입 생략", symbol)
        return False

    ex.set_leverage(symbol, leverage)
    log.info(
        "[%s] 신호=%s 강도=%.2f -> 레버리지=%dx 비중=%.0f%% (연속승리=%d)%s",
        symbol, signal, strength, leverage, ratio * 100, pm.win_streak,
        " [고래성/격리]" if whale_signal else "",
    )
    try:
        if not place_entry_order(ex, cfg, symbol, signal, quantity):
            return False
    except Exception:
        log.exception("[%s] 진입 주문 실패", symbol)
        tg.notify_error(symbol, "진입 주문 실패")
        return False

    # 주문 체결 직후 바로 조회하면 바이낸스 쪽 반영이 아주 짧게(보통 1초 이내) 늦을 때가
    # 있다. 여기서 못 찾고 그냥 넘어가면 실제로는 체결된 포지션을 봇이 추적하지 못해
    # (다음 주기 reconcile_positions가 결국 찾아내긴 하지만) 그때까지 손절/익절 관리 없이
    # 방치되므로, 짧게 재시도해서 그 무방비 구간을 최대한 줄인다.
    pos = None
    for attempt in range(3):
        try:
            pos = ex.get_position(symbol)
        except Exception:
            log.exception("[%s] 진입 직후 포지션 조회 실패 (시도 %d/3)", symbol, attempt + 1)
        if pos:
            break
        time.sleep(0.5)

    if pos:
        pm.track(symbol, pos["side"], pos["entry_price"], abs(pos["amount"]), leverage=leverage, origin="bot",
                 balance_at_entry=total_balance)
        mark_bot_position_open(symbol, pos["side"], pos["entry_price"], abs(pos["amount"]), leverage)
        pm.record_entry_ratio(symbol, ratio)

        # [2026-08-06] 거래소 자체 STOP_MARKET(Algo Order) 등록 — 30초 폴링 사이 급락(실측:
        # TAKEUSDT -20%, HFTUSDT -8.45%)에도 거래소가 즉시 반응하도록 함. 실패해도 기존 폴링
        # 기반 손절이 그대로 남아있으니 진입 자체는 막지 않는다.
        try:
            stop_loss_pct, widened = compute_stop_loss_pct(cfg, pos["side"], pm.positions[symbol].entered_at)
            if pos["side"] == "LONG":
                stop_price = pos["entry_price"] * (1 - stop_loss_pct / 100 / leverage)
            else:
                stop_price = pos["entry_price"] * (1 + stop_loss_pct / 100 / leverage)
            stop_order = ex.place_stop_market(symbol, pos["side"], abs(pos["amount"]), stop_price)
            pm.positions[symbol].stop_order_id = stop_order["algoId"]
            pm.positions[symbol].stop_loss_widened = widened
        except Exception:
            log.exception("[%s] STOP_MARKET 등록 실패 — 기존 폴링 기반 손절로 대체됨", symbol)
            tg.notify_error(symbol, "STOP_MARKET 등록 실패(폴링 손절로 대체)")

        # [2026-08-09] 거래소 네이티브 트레일링 익절 — 폴링(30초) 지연으로 트레일 확정이
        # 목표폭보다 훨씬 크게 밀리는 문제(PTBUSDT 실측 사례)를 보완한다. 등록 실패해도
        # 기존 폴링 기반 트레일링(evaluate())이 그대로 안전망으로 남아있으므로 진입은 막지 않음.
        try:
            take_profit_min = cfg.short_take_profit_min if pos["side"] == "SHORT" else cfg.take_profit_min
            if pos["side"] == "LONG":
                activation_price = pos["entry_price"] * (1 + take_profit_min / 100 / leverage)
            else:
                activation_price = pos["entry_price"] * (1 - take_profit_min / 100 / leverage)
            callback_rate = (cfg.trail_drawdown_pct * cfg.exchange_trailing_backstop_multiplier) / leverage
            trailing_order = ex.place_trailing_stop_market(
                symbol, pos["side"], abs(pos["amount"]), activation_price, callback_rate,
            )
            trailing_algo_id = int(trailing_order["algoId"])
            # [2026-08-09] API 응답이 성공을 알려도 실제로 거래소에 반영 안 됐을 가능성(네트워크
            # 등)을 배제하기 위해, 응답만 믿지 않고 살아있는 Algo 주문 목록으로 실제 존재를
            # 재확인한다. 확인 안 되면 등록 실패와 동일하게 취급해 폴백으로 넘어간다.
            if trailing_algo_id not in ex.get_open_algo_order_ids():
                raise RuntimeError(f"TRAILING_STOP_MARKET 응답은 성공(algoId={trailing_algo_id})이었으나 실제 활성 주문 목록에 없음")
            pm.positions[symbol].trailing_order_id = trailing_algo_id
        except Exception:
            log.exception("[%s] TRAILING_STOP_MARKET 등록/검증 실패 — TAKE_PROFIT_MARKET 폴백 시도", symbol)
            # [2026-08-09] 기존엔 여기서 30초 폴링 기반 트레일링으로만 대체됐는데, 스캘핑 특성상
            # 거래소측 보호가 항상 있어야 한다는 요청으로 즉시 거래소 폴백 주문을 하나 더 시도한다.
            # 실패해도 기존 폴링 트레일링은 그대로 안전망으로 남아있다(둘 다 실패해도 진입은 막지 않음).
            try:
                tp_order = ex.place_take_profit_market(symbol, pos["side"], abs(pos["amount"]), activation_price)
                pm.positions[symbol].tp_fallback_order_id = tp_order["algoId"]
                log.info("[%s] TAKE_PROFIT_MARKET 폴백 등록 성공", symbol)
            except Exception:
                log.critical("[%s] TRAILING+TAKE_PROFIT_MARKET 폴백 모두 실패 — 폴링 트레일링에만 의존 중", symbol)
                tg.notify_error(symbol, "트레일링+폴백TP 등록 모두 실패(폴링 트레일링으로만 방어 중)")

        tg.notify_entry(symbol, pos["side"], abs(pos["amount"]), pos["entry_price"], leverage, ratio, strength, probability)
        return True

    log.warning(
        "[%s] 주문은 냈지만 포지션 조회에 실패했습니다 — 실제 체결됐다면 다음 주기 "
        "reconcile_positions()가 자동으로 찾아 추적을 시작합니다.", symbol,
    )
    return False


def select_and_enter_best_candidates(ex: Exchange, pm: PositionManager, cfg: Config, tg: TelegramNotifier, total_balance: float, candidates: list[dict]):
    """이번 주기에 모인 진입 후보들 중 신호 강도가 가장 높은 것부터, 남은 슬롯만큼만 진입한다."""
    # [2026-08-12 사용자요청] "자산이 5불만 남으면 거래 자체를 멈춰줘" — 저잔고 복구모드의
    # "고확률 후보는 계속 진입" 예외도 없이, 이 절대 하한선 밑이면 신규 진입을 전부 차단한다.
    # 기존 포지션의 손절/익절/트레일링 관리는 이 return과 무관하게 계속된다(포지션 방치 아님).
    if is_critical_balance_stop(total_balance, cfg):
        if not pm.critical_balance_stop_notified:
            pm.critical_balance_stop_notified = True
            log.critical(
                "총자산 %.2f USDT가 절대 하한선 %.2f USDT 이하 — 신규 진입을 완전히 차단합니다 "
                "(기존 포지션 관리는 계속됨)", total_balance, cfg.critical_balance_stop_usdt,
            )
            tg.send(
                f"🛑 자산보호모드 진입: 총자산 {total_balance:.2f} USDT가 하한선 "
                f"{cfg.critical_balance_stop_usdt:.2f} USDT 이하로 떨어져 신규 진입을 완전히 멈췄습니다.\n"
                f"기존 보유 포지션의 손절/익절은 그대로 관리됩니다. 입금 등으로 잔고가 회복되면 자동으로 재개됩니다."
            )
        return
    elif pm.critical_balance_stop_notified:
        pm.critical_balance_stop_notified = False
        log.info("총자산 %.2f USDT로 회복 — 절대 하한선(%.2f USDT) 위로 올라와 신규 진입 재개",
                  total_balance, cfg.critical_balance_stop_usdt)
        tg.send(f"✅ 자산보호모드 해제: 총자산 {total_balance:.2f} USDT로 회복되어 신규 진입을 재개합니다.")

    if not candidates or tg.trading_paused:
        return
    # [2026-08-11 사용자요청] 전체 연패 서킷브레이커 — daily_loss_limit_pct(하루 누적)와는
    # 별개로, 짧은 시간에 5연패가 나면 "지금 장세 자체가 안 맞는다"고 보고 심볼 구분 없이
    # 잠깐(기본 10분) 전체 신규 진입만 멈춘다. 기존 포지션 관리는 이 return과 무관하게 계속됨.
    if pm.is_globally_paused():
        log.info("전체 연패 서킷브레이커 발동 중 — 이번 주기 신규 진입 후보 %d개 생략", len(candidates))
        return

    max_positions = compute_max_positions(total_balance, cfg)
    slots = max_positions - len(pm.positions)
    if slots <= 0:
        log.info(
            "동시 보유 포지션 한도(%d개, 총자산=%.2f USDT) 도달 — 이번 주기 신규 진입 후보 %d개 생략",
            max_positions, total_balance, len(candidates),
        )
        return

    # 확률을 최우선 기준으로, score(확률*0.7+강도*0.2+속도*0.1)를 2차 기준으로 정렬한다.
    # SHORT 되감기 위험은 후보를 제거하지 않고 entry_priority만 낮춰, 거래수는 유지하되
    # 같은 주기 안에 더 깨끗한 후보가 있으면 그쪽을 먼저 태운다.
    candidates = sorted(candidates, key=lambda c: (c["probability"], c.get("entry_priority", c["score"])), reverse=True)
    log.info(
        "이번 주기 진입 후보 %d개 (상위: %s, 확률/score순 정렬) — 남은 슬롯 %d개",
        len(candidates),
        [(c["symbol"], round(c["probability"], 2), round(c.get("entry_priority", c["score"]), 2)) for c in candidates[:5]],
        slots,
    )
    tg.notify_scan_candidates(candidates, slots)

    if is_low_balance_recovery_mode(total_balance, cfg):
        original_count = len(candidates)
        candidates = [c for c in candidates if passes_low_balance_recovery_gate(c, cfg)]
        slots = min(slots, max(0, getattr(cfg, "low_balance_recovery_max_positions", 1)))
        if not candidates or slots <= 0:
            log.error(
                "총자산 %.2f USDT 저잔고 복구모드 — 후보 %d개 중 고확률 기준(확률 %.2f, score %.2f) 통과 없음",
                total_balance, original_count,
                getattr(cfg, "low_balance_recovery_min_probability", 1.0),
                getattr(cfg, "low_balance_recovery_min_score", 1.0),
            )
            return
        log.error(
            "총자산 %.2f USDT 저잔고 복구모드 — 후보 %d개 중 고확률 %d개만 최대 %d개 진입 허용",
            total_balance, original_count, len(candidates), slots,
        )
    elif should_pause_new_entries_for_low_balance(total_balance, cfg):
        log.error(
            "총자산 %.2f USDT가 생존모드 기준 %.2f USDT 미만 — 후보 시그널은 표시하지만 신규 진입 주문은 생략",
            total_balance, cfg.low_balance_new_entry_pause_threshold,
        )
        return

    entered = 0
    for candidate in candidates:
        if entered >= slots:
            break
        try:
            margin_ratio = ex.get_margin_ratio()
            if margin_ratio >= cfg.max_margin_ratio:
                log.warning(
                    "마진 비율 %.1f%%로 최대 허용치(%.1f%%) 초과 — 이번 주기 나머지 진입 중단",
                    margin_ratio * 100, cfg.max_margin_ratio * 100,
                )
                break

            symbol, signal = candidate["symbol"], candidate["signal"]
            agree, total = mtf_trend_alignment(ex, cfg, symbol, signal)
            if total == 0 or agree / total < cfg.mtf_min_agree_ratio:
                log.info(
                    "[%s] 상위 시간대 추세 불일치(%d/%d) — 이 후보는 건너뜀",
                    symbol, agree, total,
                )
                continue

            if execute_entry(ex, pm, cfg, tg, total_balance, candidate):
                entered += 1
        except Exception:
            log.exception("[%s] 진입 처리 중 오류", candidate["symbol"])


def acquire_single_instance_lock():
    """이미 다른 봇 인스턴스가 돌고 있으면 즉시 종료한다 (중복 실행 시 같은 계좌에
    동시에 주문이 나가는 것을 방지). 파일 락(msvcrt)을 이용해 감지한다."""
    import msvcrt

    lock_path = LOG_DIR.parent / ".bot.lock"
    lock_file = open(lock_path, "w")
    try:
        msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError:
        log.error("이미 다른 봇 인스턴스가 실행 중입니다. 중복 실행을 방지하기 위해 종료합니다.")
        # [2026-08-09] run_forever.py가 "진짜 크래시"와 "중복 인스턴스 감지"를 구분해서
        # 후자는 5초가 아니라 훨씬 길게 대기하도록, 종료코드를 구분한다(DUPLICATE_INSTANCE_EXIT_CODE).
        # 두 슈퍼바이저가 동시에 떠 있던 사고(2026-08-09)에서 5초마다 서로 물고 재시작하는
        # 문제가 실측됨 — 근본 원인(중복 슈퍼바이저)은 별개지만, 이 방어도 함께 넣는다.
        raise SystemExit(DUPLICATE_INSTANCE_EXIT_CODE)
    return lock_file  # 참조를 계속 들고 있어야 락이 유지됨


WS_WORKER_STARTUP_GRACE_SEC = 90.0  # 워커가 REST로 초기 히스토리를 시딩하는 데 걸리는 시간(250심볼 기준 실측 20~45초) + 여유

# [2026-08-10 세 번째 실거래 사고 근본해결] python-binance 1.0.37의 read loop 버그가
# 재연결 실패로 끝나는 게 아니라 **프로세스 자체를 CPU 붙잡는 tight loop로 응답불능**에
# 빠뜨리는 게 실거래에서 실측됐다(터미널 입력/Ctrl+C도 안 먹힘). 같은 프로세스 안에서
# ThreadedWebsocketManager를 아무리 재시작해봐야, "재시작 판단을 내리는 코드"조차 같은
# GIL을 두고 경합하는 처지라 근본 해결이 안 된다. 그래서 WS 연결 자체를 `bot/ws_worker.py`
# 라는 완전히 별도의 OS 프로세스로 옮기고, 메인 프로세스는 그 워커가 파일로 남기는
# 캐시/하트비트만 읽는다 — 워커가 얼어붙어도 OS 레벨 강제종료(Popen.kill())는 항상 통하고,
# 메인 프로세스(실거래 로직)는 애초에 그 코드와 같은 프로세스에 있지 않으므로 전혀 영향받지
# 않는다.
def _compute_ws_restart_backoff_sec(consecutive_restart_count: int, cfg: Config) -> float:
    """[2026-08-10 사용자요청] "딜레이 없는 맹목적 재연결은 IP 밴을 부른다" — 지수 백오프
    (5초→10→20→...→최대 120초). consecutive_restart_count는 "직전 정상 판정 이후 연속으로
    몇 번째 재시작인지"를 뜻한다(0이면 첫 재시작이라 base_sec 그대로)."""
    return min(cfg.ws_restart_backoff_base_sec * (2 ** consecutive_restart_count), cfg.ws_restart_backoff_max_sec)


def _spawn_ws_worker(role: str, shard_index: int, shard_count: int, symbols: list | None = None) -> "subprocess.Popen":
    import subprocess
    env = dict(os.environ)
    env["WS_WORKER_ROLE"] = role
    env["WS_SHARD_INDEX"] = str(shard_index)
    env["WS_SHARD_COUNT"] = str(shard_count)
    if symbols is not None:
        env["WS_WORKER_SYMBOLS"] = __import__("json").dumps(list(symbols), ensure_ascii=False)
    return subprocess.Popen(
        [sys.executable, "-m", "bot.ws_worker"],
        cwd=str(Path(__file__).resolve().parent.parent),
        env=env,
    )


def start_ws_layer(ex: Exchange, cfg: Config, symbols: list) -> dict:
    """WebSocket 레이어를 별도 프로세스(bot/ws_worker.py)로 기동한다. cfg.ws_market_data_enabled/
    ws_user_data_enabled가 둘 다 기본값(False)이면 아무것도 하지 않고 빈 핸들을 반환한다 —
    지금까지의 REST 폴링 동작과 100% 동일. 워커 기동 자체가 실패해도 예외를 삼켜서 봇
    시작을 막지 않는다.

    [2026-08-10, Codex 제안 반영] 시장데이터는 cfg.ws_market_shard_count개의 독립 프로세스로
    나눠 띄운다(기본 1 = 기존과 동일한 단일 워커). User Data Stream은 활성화된 경우 항상
    시장데이터와 별도 프로세스로 띄운다(같은 프로세스 안에서 큐 경합을 만들지 않기 위함).

    반환값(dict)의 "workers"는 stop_ws_layer()/ws_layer_needs_restart()에 그대로 넘기면
    되는 워커별 핸들 리스트(각 원소: process/started_at/role/shard_index/symbols/
    cache_path/heartbeat_path)."""
    handles = {"workers": []}
    if not (cfg.ws_market_data_enabled or cfg.ws_user_data_enabled):
        return handles

    try:
        market_shard_caches = []
        if cfg.ws_market_data_enabled:
            shard_count = max(1, cfg.ws_market_shard_count)
            for i in range(shard_count):
                cache_path, hb_path = _ws_worker_paths("market", i, shard_count)
                status_path = _ws_worker_status_path("market", i, shard_count)
                shard_symbols = symbols[i::shard_count] if shard_count > 1 else list(symbols)
                process = _spawn_ws_worker("market", i, shard_count, symbols=symbols)
                handles["workers"].append({
                    "process": process, "started_at": time.time(), "role": "market",
                    "shard_index": i, "shard_count": shard_count, "symbols": shard_symbols,
                    "cache_path": cache_path, "heartbeat_path": hb_path, "status_path": status_path,
                    "consecutive_restart_count": 0, "next_restart_allowed_at": 0.0,
                })
                market_shard_caches.append(FileBackedKlineCache(cache_path, hb_path, status_path=status_path))
                log.info("WS 시장데이터 워커 기동(샤드 %d/%d, pid=%d, 심볼 %d개)",
                          i, shard_count, process.pid, len(shard_symbols))
            ws_cache = market_shard_caches[0] if len(market_shard_caches) == 1 else MultiFileBackedKlineCache(market_shard_caches)
            ex.set_ws_kline_cache(ws_cache)

        if cfg.ws_user_data_enabled:
            process = _spawn_ws_worker("user", 0, 1)
            cache_path, hb_path = _ws_worker_paths("user", 0, 1)
            status_path = _ws_worker_status_path("user", 0, 1)
            handles["workers"].append({
                "process": process, "started_at": time.time(), "role": "user",
                "shard_index": 0, "shard_count": 1, "symbols": [],
                "cache_path": cache_path, "heartbeat_path": hb_path, "status_path": status_path,
                "consecutive_restart_count": 0, "next_restart_allowed_at": 0.0,
            })
            ex.set_fill_tracker(FileBackedFillTracker(cache_path, hb_path, status_path=status_path))
            log.info("WS 계정(체결) 스트림 워커 기동(pid=%d)", process.pid)

        log.info("WS 워커 %d개 기동 완료 — 캐시 파일이 채워지기까지 최대 %.0f초 소요될 수 있음",
                  len(handles["workers"]), WS_WORKER_STARTUP_GRACE_SEC)
    except Exception:
        log.exception("WS 워커 프로세스 기동 실패 — REST 폴링으로 계속 진행")

    return handles


def stop_ws_layer(ex: Exchange, handles: dict, only_indices: Optional[list] = None) -> None:
    """start_ws_layer()가 반환한 핸들을 정리한다(재시작 전 호출). 워커가 응답불능이어도
    OS 레벨 강제종료(kill)는 항상 통한다 — 이게 이번 재설계의 핵심이다.

    only_indices를 주면 그 인덱스의 워커만 정리한다(부분 재시작용) — None이면 전부 정리하고
    Exchange의 캐시/트래커 참조도 같이 비운다."""
    workers = handles.get("workers", [])
    targets = only_indices if only_indices is not None else range(len(workers))
    for idx in targets:
        if idx >= len(workers):
            continue
        process = workers[idx].get("process")
        if process is None:
            continue
        try:
            process.terminate()
            try:
                process.wait(timeout=5)
            except Exception:
                log.warning("WS 워커(idx=%d)가 5초 내 정상종료 안 함 — 강제종료(kill)합니다", idx)
                process.kill()
                process.wait(timeout=5)
        except Exception:
            log.exception("WS 워커(idx=%d) 프로세스 정리 중 오류(무시하고 계속 진행)", idx)

    if only_indices is None:
        # 죽은 캐시/트래커를 계속 참조하지 않도록 정리 — get_klines()/get_last_fill()은 None이면
        # 이미 안전하게 REST/mark-price로 폴백하므로, 재연결 성공 전까지는 이 상태가 안전하다.
        ex.set_ws_kline_cache(None)
        ex.set_fill_tracker(None)


def ws_layer_needs_restart(cfg: Config, handles: dict, canary_symbol: str) -> list:
    """워치독: 워커별로 ① 프로세스 자체가 죽었는지(poll()), ② 아직 기동 유예시간(초기 REST
    시딩)을 지났는데도 실시간 메시지 수신이 끊겼는지, ③ read loop 에러가 연속으로 쌓이고
    있는지 확인한다. 유예시간을 두는 이유: 워커가 막 시작돼 REST 시딩 중일 때는 당연히
    신선한 데이터가 아직 없는데, 이때 바로 "죽었다"고 오판해 무한 재시작 루프에 빠지면 안 된다.

    [2026-08-10, Codex 제안 반영] ① 샤딩 지원 — 반환값이 bool에서 "재시작이 필요한 워커
    인덱스 리스트"로 바뀌었다(문제가 생긴 샤드만 재시작하기 위함, 전체를 다 죽이지 않음).
    ② canary(대표 심볼, 보통 BTCUSDT) kline freshness(캔들 "마감" 기준, 150초)만으로는
    감지가 너무 늦다는 게 테스트넷 실측으로 확인됨(실제 read loop는 약 90초마다 죽는데
    150초 문턱 안에서는 아직 "신선하다"고 오판) — 실시간 메시지 카운터
    (ws_message_max_staleness_sec, 기본 45초)와 연속 에러 횟수
    (ws_max_consecutive_read_loop_errors, 기본 3회)를 추가 조건으로 더 빨리 잡는다."""
    workers = handles.get("workers", [])
    if not workers:
        return []

    stale_indices = []
    restart_reasons = {}
    for idx, w in enumerate(workers):
        process, started_at = w.get("process"), w.get("started_at")
        if process is None or started_at is None:
            continue
        if process.poll() is not None:  # 프로세스 자체가 이미 종료됨(크래시 등)
            stale_indices.append(idx)
            restart_reasons[idx] = "process_exited"
            continue
        if time.time() - started_at < WS_WORKER_STARTUP_GRACE_SEC:
            continue  # 아직 초기 시딩 유예시간 이내 — 판단 보류
        if w.get("role") != "market":
            continue  # user 워커는 메시지/에러 기반 판단 근거가 없음(poll()만으로 충분)
        cache = FileBackedKlineCache(w["cache_path"], w["heartbeat_path"], status_path=w.get("status_path"))
        health = cache.health()
        if health is None:
            continue  # 구버전 워커 등으로 health 정보 자체가 없으면 이 샤드는 판단 보류
        last_msg_ts = health.get("last_market_message_ts", 0)
        if last_msg_ts and time.time() - last_msg_ts > cfg.ws_message_max_staleness_sec:
            stale_indices.append(idx)
            restart_reasons[idx] = f"message_stale:{time.time() - last_msg_ts:.1f}s>{cfg.ws_message_max_staleness_sec:.1f}s"
            continue
        if health.get("error_count_60s", 0) >= cfg.ws_max_error_count_60s:
            stale_indices.append(idx)
            restart_reasons[idx] = f"error_count_60s:{health.get('error_count_60s', 0)}>= {cfg.ws_max_error_count_60s}"
            continue
        if health.get("consecutive_read_loop_errors", 0) >= cfg.ws_max_consecutive_read_loop_errors:
            stale_indices.append(idx)
            restart_reasons[idx] = f"consecutive_read_loop_errors:{health.get('consecutive_read_loop_errors', 0)}>= {cfg.ws_max_consecutive_read_loop_errors}"
            continue

    # canary 심볼 신선도는 그 심볼을 실제로 담당하는 샤드 기준으로 확인한다 — 못 찾으면
    # (샤드 분배가 바뀌었거나 canary가 없는 등) 안전하게 시장데이터 워커 전체를 재검사 대상에 넣는다.
    if cfg.ws_market_data_enabled:
        market_workers = [(idx, w) for idx, w in enumerate(workers) if w.get("role") == "market"]
        ready = [w for _, w in market_workers if time.time() - w["started_at"] >= WS_WORKER_STARTUP_GRACE_SEC]
        if market_workers and len(ready) == len(market_workers):
            owner = next((idx for idx, w in market_workers if canary_symbol in w.get("symbols", [])), None)
            if owner is not None:
                cache = FileBackedKlineCache(workers[owner]["cache_path"], workers[owner]["heartbeat_path"], status_path=workers[owner].get("status_path"))
                if not cache.is_fresh(canary_symbol, cfg.ws_canary_max_staleness_sec) and owner not in stale_indices:
                    stale_indices.append(owner)
                    restart_reasons[owner] = f"canary_stale:{canary_symbol}>{cfg.ws_canary_max_staleness_sec:.1f}s"
            else:
                multi = MultiFileBackedKlineCache([FileBackedKlineCache(w["cache_path"], w["heartbeat_path"], status_path=w.get("status_path")) for _, w in market_workers])
                if not multi.is_fresh(canary_symbol, cfg.ws_canary_max_staleness_sec):
                    for idx, _ in market_workers:
                        if idx not in stale_indices:
                            stale_indices.append(idx)
                            restart_reasons[idx] = f"canary_missing_or_stale:{canary_symbol}>{cfg.ws_canary_max_staleness_sec:.1f}s"

    handles["last_restart_reasons"] = restart_reasons
    return stale_indices


def main():
    _lock = acquire_single_instance_lock()
    write_heartbeat()  # 시작 직후부터 기록 — run_forever.py가 시작 단계부터 감시할 수 있게

    cfg = Config()
    cfg.validate()

    mode = "TESTNET(모의투자)" if cfg.use_testnet else "실전 매매(LIVE)"
    log.info("=== 바이낸스 선물 자동매매 봇 시작 — %s ===", mode)
    if not cfg.use_testnet:
        log.warning("실전 매매 모드입니다. 실제 자금이 사용됩니다!")

    ex = Exchange(cfg)
    pm = PositionManager(cfg)
    tg = TelegramNotifier(cfg, ex, pm)
    if tg.enabled:
        tg.start_command_listener()
        log.info("텔레그램 알림 활성화됨")
    else:
        log.info("텔레그램 미설정 — TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID를 .env에 넣으면 활성화됩니다")

    symbols = ex.get_active_usdt_perpetual_symbols(limit=cfg.max_auto_symbols) if cfg.auto_symbols else cfg.symbols
    log.info(
        "심볼 수=%d (auto=%s) 레버리지=%s~%sx 비중=%.0f~%.0f%% 익절=%.1f~%.1f%% 손절=-%.1f%%",
        len(symbols), cfg.auto_symbols, cfg.leverage_min, cfg.leverage_max,
        cfg.position_size_min * 100, cfg.position_size_max * 100,
        cfg.take_profit_min, cfg.take_profit_max, cfg.stop_loss_pct,
    )
    tg.notify_startup(mode, len(symbols))

    ws_handles = start_ws_layer(ex, cfg, symbols)
    # 워치독이 "연결이 살아있는지" 판단할 대표 심볼. BTCUSDT는 항상 활발히 거래돼 연결이
    # 살아있으면 반드시 매분 캔들이 갱신되므로 대표로 삼기 좋다 — 없으면 첫 심볼로 대체.
    ws_canary_symbol = "BTCUSDT" if "BTCUSDT" in symbols else (symbols[0] if symbols else "BTCUSDT")

    sync_existing_positions(ex, pm)

    daily_state = load_daily_baseline_override(date.today(), cfg.daily_profit_target_pct)
    if daily_state is not None:
        log.info(
            "일일 기준자산 오버라이드 적용: 기준자산=%.2fUSDT 시작시점봇누적손익=%.2fUSDT",
            daily_state["start_balance"], daily_state["bot_pnl_start"],
        )
    else:
        daily_state = {"date": None, "start_balance": None, "next_threshold": None}
    tg.daily_state = daily_state  # 텔레그램 "오늘 수익률" 버튼이 같은 dict를 참조하도록 연결
    pending_signals: dict = {}  # 2봉 연속 확인용 (심볼별 대기 중인 신호)
    hourly_seen_candidates: set = set()  # 1시간 동안 신호+확률 통과한 코인(실제 거래 여부 무관, 1시간마다 리셋)
    hourly_check_state: dict = {}  # 심볼별로 마지막에 1시간봉 재평가한 캔들 시각 (약손절 판단용)

    last_time_sync_at = time.time()
    last_tune_at = time.time()
    last_btc_trend_at = 0.0  # 시작 직후 바로 한 번 보내지도록 0으로 초기화
    last_trade_review_at = time.time()
    last_symbol_refresh_at = time.time()
    # [2026-08-12 사용자요청] 30분 모니터링에 "WS 재시작 횟수"를 포함시키기 위한 누적 카운터.
    # analyze_recent_trade_review()가 보고할 때마다 0으로 리셋해서 "이번 구간" 값만 보여준다.
    ws_restart_count_since_review = 0
    TIME_SYNC_INTERVAL_SEC = 1800  # 30분마다 서버 시간과 다시 맞춰서 오차 누적을 방지
    TUNE_INTERVAL_SEC = 3600  # 1시간마다 손익 분석 후 필요하면 텔레그램으로 조정 제안
    BTC_TREND_INTERVAL_SEC = 1800  # [2026-08-11 사용자요청] 30분마다 BTC 멀티타임프레임 흐름을 텔레그램으로 안내(참고용)
    TRADE_REVIEW_INTERVAL_SEC = 1800  # [2026-08-11 사용자요청] 30분마다 수익/손실 복기 — 정기요약(digest)은 이걸로 대체됨
    # [2026-08-12 사용자요청] "거래대금 상위 50개를 상시로 스캔" — 기존엔 symbols가 시작
    # 시점에 딱 한 번만 조회되고 실행 중엔 안 바뀌어서, 오래 켜두면 새로 상위권에 들어온
    # 코인을 놓쳤다. 30분마다 REST 스캔 대상 목록만 다시 조회해 갱신한다(WS 워커가 담당하는
    # 심볼 집합은 시작 시점 그대로 유지 — 새로 추가된 심볼은 Exchange.get_klines()의 기존
    # REST 폴백 경로로 안전하게 처리됨, WS 워커 재기동 없이도 동작에 지장 없음).
    SYMBOL_REFRESH_INTERVAL_SEC = 1800

    while True:
        cycle_start = time.time()
        write_heartbeat()  # run_forever.py가 이걸 감시해서 멈춘 프로세스를 강제종료/재시작한다

        if time.time() - last_time_sync_at >= TIME_SYNC_INTERVAL_SEC:
            ex.sync_time()
            last_time_sync_at = time.time()

        # [2026-08-10] WS 연결 워치독 — python-binance 라이브러리가 내부 read loop이 죽어도
        # 스스로 재연결하지 않는 버그가 있어(소스코드로 확인), 매 주기 캐시 신선도를 확인해
        # 죽었으면 우리가 직접 완전히 새 연결로 복구한다. get_klines()는 이 재연결 시도 중에도
        # (신선도 확인이 실패하는 동안) 자동으로 REST 폴백하므로 매매 판단은 항상 안전하다.
        stale_ws_indices = ws_layer_needs_restart(cfg, ws_handles, ws_canary_symbol)
        # [2026-08-10 사용자요청] "딜레이 없는 맹목적 재연결은 IP 밴을 부른다" — 오늘 실제로
        # REST 대량호출로 IP 차단을 당한 것과 같은 위험. 정상인(이번 주기엔 재시작 대상이
        # 아닌) 워커는 연속재시작 카운터를 리셋하고, 문제 워커는 지수 백오프 대기시간이
        # 지나기 전엔 재시작을 건너뛴다(REST 폴백이 그동안 안전망 역할을 계속 해줌).
        for idx, w in enumerate(ws_handles.get("workers", [])):
            if idx not in stale_ws_indices:
                w["consecutive_restart_count"] = 0
        restart_now = [idx for idx in stale_ws_indices if time.time() >= ws_handles["workers"][idx].get("next_restart_allowed_at", 0.0)]
        skipped_backoff = [idx for idx in stale_ws_indices if idx not in restart_now]
        if skipped_backoff:
            log.info("WS 워커 %s는 백오프 대기 중이라 이번 주기 재시작을 건너뜁니다", skipped_backoff)
        if restart_now:
            # [2026-08-10, Codex 제안 반영] 샤딩된 워커 중 문제가 생긴 것만 재시작한다 —
            # 정상인 다른 샤드까지 같이 죽였다 살리면 그만큼 불필요하게 REST 폴백 구간이
            # 늘어난다. 전체 워커 목록을 다시 만드는 대신, stop_ws_layer(only_indices=...)로
            # 문제 워커만 정리한 뒤 같은 역할/샤드 정보로 그 자리만 다시 띄운다.
            restart_reasons = {idx: ws_handles.get("last_restart_reasons", {}).get(idx, "unknown") for idx in restart_now}
            log.warning("WS 워커 %d개(idx=%s)가 응답불능/데이터끊김으로 판단돼 재시작합니다 — reasons=%s", len(restart_now), restart_now, restart_reasons)
            ws_restart_count_since_review += len(restart_now)
            stop_ws_layer(ex, ws_handles, only_indices=restart_now)
            for idx in restart_now:
                w = ws_handles["workers"][idx]
                new_process = _spawn_ws_worker(w["role"], w["shard_index"], w["shard_count"], symbols=symbols if w.get("role") == "market" else None)
                w["process"] = new_process
                w["started_at"] = time.time()
                backoff = _compute_ws_restart_backoff_sec(w["consecutive_restart_count"], cfg)
                w["next_restart_allowed_at"] = time.time() + backoff
                w["consecutive_restart_count"] += 1
                log.info("WS 워커(idx=%d) 재시작 완료 — 다음 재시작까지 최소 %.0f초 대기(연속 %d회째)",
                          idx, backoff, w["consecutive_restart_count"])
            # 캐시/트래커 참조를 워커 목록 최신 상태로 다시 구성(재시작된 워커의 새 프로세스
            # 반영, 캐시/하트비트 경로 자체는 역할+샤드가 같으면 안 바뀌므로 재구성만 하면 됨).
            market_workers = [w for w in ws_handles["workers"] if w["role"] == "market"]
            if market_workers:
                caches = [FileBackedKlineCache(w["cache_path"], w["heartbeat_path"], status_path=w.get("status_path")) for w in market_workers]
                ex.set_ws_kline_cache(caches[0] if len(caches) == 1 else MultiFileBackedKlineCache(caches))
            user_worker = next((w for w in ws_handles["workers"] if w["role"] == "user"), None)
            if user_worker:
                ex.set_fill_tracker(FileBackedFillTracker(user_worker["cache_path"], user_worker["heartbeat_path"], status_path=user_worker.get("status_path")))

        if time.time() - last_tune_at >= TUNE_INTERVAL_SEC:
            last_tune_at = time.time()
            try:
                diagnosis, changes = analyze_hourly_performance(ex, cfg, len(hourly_seen_candidates))
                if changes:
                    tg.propose_tuning(diagnosis, changes)
                else:
                    tg.send(f"🧠 1시간 손익 분석\n{diagnosis}")
            except Exception:
                log.exception("1시간 자동 분석 중 오류")
            hourly_seen_candidates.clear()

        if cfg.auto_symbols and time.time() - last_symbol_refresh_at >= SYMBOL_REFRESH_INTERVAL_SEC:
            last_symbol_refresh_at = time.time()
            try:
                refreshed = ex.get_active_usdt_perpetual_symbols(limit=cfg.max_auto_symbols)
                added = sorted(set(refreshed) - set(symbols))
                removed = sorted(set(symbols) - set(refreshed))
                symbols = refreshed
                if added or removed:
                    log.info(
                        "거래대금 상위 %d개 스캔 목록 갱신 — 추가=%s 제외=%s (총 %d개)",
                        cfg.max_auto_symbols, added, removed, len(symbols),
                    )
            except Exception:
                log.exception("스캔 심볼 목록 갱신 실패 — 기존 목록 유지")

        if time.time() - last_btc_trend_at >= BTC_TREND_INTERVAL_SEC:
            last_btc_trend_at = time.time()
            try:
                btc_trend = analyze_btc_multi_timeframe_trend(ex, cfg)
                tg.send(format_btc_trend_digest(btc_trend))
            except Exception:
                log.exception("BTC 멀티타임프레임 흐름 분석 중 오류")

        try:
            total_balance = ex.get_total_margin_balance()
        except Exception as e:
            # [2026-08-11] -1003(Way too many requests)로 IP 밴을 당한 경우, 막연히 poll_interval만
            # 기다렸다 재시도하면 계속 요청을 더 보내는 꼴이라 밴이 연장될 위험이 있다(오늘 실제로
            # 겪은 사고). 메시지에 정확한 해제 시각이 있으면 그때까지 통째로 쉰다.
            ban_wait = parse_ban_wait_seconds(e)
            if ban_wait is not None:
                log.error("IP 밴 감지 — 해제까지 %.0f초 대기 후 재시도", ban_wait)
                try:
                    tg.send(f"🚨 바이낸스 IP 밴 감지! 약 {ban_wait/60:.1f}분 후 자동 재개합니다.")
                except Exception:
                    log.exception("밴 알림 전송 실패")
                time.sleep(ban_wait + 5)  # 해제 경계에서 바로 재요청해 다시 걸리는 걸 피하려는 여유분
                ex.sync_time()
                last_time_sync_at = time.time()
                continue
            log.exception("총자산 조회 실패 — 서버 시간 재동기화 후 이번 사이클 건너뜀")
            ex.sync_time()  # 타임스탬프 오차(-1021) 등으로 실패했을 가능성에 대비해 재동기화
            last_time_sync_at = time.time()
            time.sleep(cfg.poll_interval_sec)
            continue
        pm.total_balance = total_balance  # 소액 구간 절대금액 익절 기준 판단용

        try:
            update_daily_checkpoint(ex, pm, cfg, tg, daily_state, total_balance)
        except Exception:
            log.exception("일일 목표 체크 중 오류")

        now = time.time()
        if now - last_trade_review_at >= TRADE_REVIEW_INTERVAL_SEC:
            last_trade_review_at = now
            try:
                diagnosis, changes = analyze_recent_trade_review(
                    ex, cfg, TRADE_REVIEW_INTERVAL_SEC,
                    ws_restart_count=ws_restart_count_since_review,
                )
                ws_restart_count_since_review = 0
                if changes:
                    tg.propose_tuning(diagnosis, changes)
                else:
                    tg.send(diagnosis)
            except Exception:
                log.exception("30분 거래 복기 중 오류")

        try:
            reconcile_positions(ex, pm, cfg, tg)
        except Exception:
            log.exception("포지션 대조(reconcile) 중 오류")

        if is_near_funding_settlement(cfg):
            try:
                force_close_for_funding_settlement(ex, pm, cfg, tg)
            except Exception:
                log.exception("펀딩비 정산 강제청산 처리 중 오류")

        # [2026-08-10 실거래 분석 발견] 보유 포지션 관리가 250심볼 스캔과 같은 poll_interval_sec
        # (30초) 주기에 묶여있으면, 거래소에 걸어둔 실시간 트레일링 주문이 항상 먼저 청산해버려서
        # 봇 자신의 판단(SOFT_STOP 재평가, 모멘텀 지속 중 트레일링 확정 보류 등)이 실행될 기회
        # 자체가 거의 없다(실측: 최근 청산의 72%가 거래소측 EXTERNAL_CLOSE). 그래서 신규 진입
        # 스캔(무거움, API 호출량 큼)으로 넘어가기 전까지 이 시간(poll_interval_sec)을 그냥
        # 한 번 자는 대신, 보유 포지션만 훨씬 자주(position_check_interval_sec, 기본 5초)
        # 반복 확인하는 데 쓴다 — 전체 주기 길이(스캔 빈도)는 기존과 동일하게 유지된다.
        while True:
            tracked_symbols = list(pm.positions.keys())
            for symbol in tracked_symbols:
                try:
                    process_tracked_position(ex, pm, cfg, symbol, tg, hourly_check_state)
                except Exception:
                    log.exception("[%s] 처리 중 오류", symbol)
            elapsed = time.time() - cycle_start
            remaining = cfg.poll_interval_sec - elapsed
            if remaining <= 0:
                break
            write_heartbeat()
            time.sleep(min(cfg.position_check_interval_sec, remaining))

        # 보유 중이 아닌 심볼들은 이번 주기에 신호가 있는지만 먼저 모두 스캔해서 후보를 모은다.
        # 그중 신호 강도(가장 확률 좋아 보이는 정도)가 가장 높은 것부터 남은 슬롯만큼만 진입한다.
        candidates = []
        if is_blocked_trading_hour(cfg):
            log.info("유동성 얇은 시간대(UTC %d시) — 이번 주기 신규 진입 스캔 생략", datetime.now(timezone.utc).hour)
        elif is_near_funding_settlement(cfg):
            log.info("펀딩비 정산 시각 근접 — 이번 주기 신규 진입 스캔 생략")
        elif is_btc_unstable(ex, cfg):
            pass  # is_btc_unstable 내부에서 이미 로그 남김
        elif cfg.pause_new_entries:
            pass  # 신규 진입 일시 중단 상태 (지표 재설계/검증 중) — 기존 포지션 관리는 계속됨
        elif cfg.ev_filter_hard_pause and not passes_expected_value_filter(cfg, pm):
            log.info("수수료 반영 기대값이 구조적으로 마이너스 — 이번 주기 신규 진입 스캔 생략")
        elif cfg.aggregate_risk_hard_pause and not passes_aggregate_risk_filter(pm, cfg, total_balance):
            log.info(
                "보유 포지션 증거금 합계가 상관리스크 한도(%.0f%%)를 초과 — 이번 주기 신규 진입 스캔 생략",
                cfg.max_aggregate_margin_pct,
            )
        elif not tg.trading_paused:
            negative_ev_symbols = compute_negative_ev_symbols(cfg)
            if negative_ev_symbols:
                log.info("기대값 마이너스 심볼은 제외하지 않고 후순위/비중축소로 스캔 유지: %s", sorted(negative_ev_symbols))
            for symbol in symbols:
                if pm.is_tracked(symbol):
                    continue
                if pm.is_symbol_blacklisted(symbol):
                    continue
                try:
                    candidate = scan_entry_candidate(ex, cfg, symbol, total_balance)
                    if candidate:
                        if symbol in negative_ev_symbols:
                            candidate["negative_ev_symbol"] = True
                            penalty = max(0.0, 1.0 - getattr(cfg, "negative_ev_symbol_size_mult", 0.60)) * 0.10
                            candidate["entry_priority"] = candidate.get("entry_priority", candidate["score"]) - penalty
                        hourly_seen_candidates.add(symbol)  # 실제 진입 여부와 무관하게 신호 감지 자체를 기록
                        if should_confirm_candidate_two_candle(pending_signals, cfg, candidate, total_balance):
                            candidates.append(candidate)
                except Exception:
                    log.exception("[%s] 스캔 중 오류", symbol)

        select_and_enter_best_candidates(ex, pm, cfg, tg, total_balance, candidates)
        # [2026-08-10] 여기서 다시 poll_interval_sec를 자지 않는다 — 바로 위 보유 포지션
        # 빠른 확인 루프가 이미 그 시간을 소비했다(전체 사이클 길이는 기존과 동일).


if __name__ == "__main__":
    _configure_logging_for_live_process()
    main()
