import logging
import threading
import time

import requests

from .config import Config, TUNABLE_PARAMS, TUNABLE_PARAMS_KO, set_env_value
from .trade_ledger import TradeRecord, append_trade_record, strategy_config_snapshot
from .exchange import Exchange
from .indicators import add_indicators
from .position_manager import PositionManager
from .position_manager import profit_usdt
from .strategy import (
    _direction_scores,
    estimate_entry_probability,
    immediate_momentum_ok,
    mtf_trend_alignment,
    pnl_pct,
    quick_profit_score,
    volume_direction_ok,
)

log = logging.getLogger("bot.telegram")

API_BASE = "https://api.telegram.org/bot{token}"


class TelegramNotifier:
    """텔레그램으로 매매 알림을 보내고, 버튼(잔고/포지션/종료) 및 명령어에 응답한다."""

    def __init__(self, cfg: Config, ex: Exchange, pm: PositionManager):
        self.cfg = cfg
        self.ex = ex
        self.pm = pm
        self.enabled = bool(cfg.telegram_bot_token and cfg.telegram_chat_id)
        self._offset = 0
        self.trading_paused = False
        self._awaiting_confirmation = False
        self.daily_state = None  # main.py가 실행 중 daily_state dict를 연결해준다
        self._pending_tune = None  # 1시간 자동 분석이 제안한 변경안 (승인 대기 중)
        self._pending_tune_diagnosis = ""
        self._tune_history: list = []  # [{"ts", "changes", "decision"("applied"/"ignored"/"manual"), "diagnosis"}]

    def _url(self, method: str) -> str:
        return f"{API_BASE.format(token=self.cfg.telegram_bot_token)}/{method}"

    def send(self, text: str, reply_markup: dict | None = None):
        if not self.enabled:
            return
        payload = {"chat_id": self.cfg.telegram_chat_id, "text": text}
        if reply_markup:
            payload["reply_markup"] = reply_markup
        try:
            requests.post(self._url("sendMessage"), json=payload, timeout=10)
        except Exception:
            log.exception("텔레그램 메시지 전송 실패")

    def _answer_callback(self, callback_query_id: str, text: str = ""):
        try:
            requests.post(
                self._url("answerCallbackQuery"),
                json={"callback_query_id": callback_query_id, "text": text},
                timeout=10,
            )
        except Exception:
            log.exception("콜백 응답 실패")

    def send_menu(self):
        """항상 화면 하단에 고정되는 메뉴(리플라이 키보드)를 띄운다. 인라인 버튼과 달리
        메시지가 쌓여도 사라지지 않고 계속 그 자리에 있어서, 매번 /menu를 다시 부를 필요가 없다.
        [2026-08-09] 사용자 요청: 메뉴가 대화창에 흘러가버려서 계속 다시 불러야 하는 게 불편함."""
        keyboard = {
            "keyboard": [
                [{"text": "💰 잔고"}, {"text": "📊 포지션"}],
                [{"text": "📈 오늘수익률"}, {"text": "🏆 성과"}],
                [{"text": "⚙️ 설정"}, {"text": "🛑 포지션종료"}],
            ],
            "resize_keyboard": True,
            "is_persistent": True,
        }
        self.send("메뉴가 하단에 고정됐어요. 언제든 눌러서 사용하세요:", reply_markup=keyboard)

    def notify_entry(self, symbol: str, side: str, quantity: float, price: float, leverage: int, ratio: float,
                      strength: float | None = None, probability: float | None = None):
        emoji = "🟢" if side == "LONG" else "🔴"
        extra = ""
        if strength is not None and probability is not None:
            extra = f"\n신호강도: {strength:.2f}  확률: {probability*100:.0f}%"
        self.send(
            f"{emoji} 진입 {symbol} {side}\n"
            f"수량: {quantity} @ {price}\n"
            f"레버리지: {leverage}x  비중: {ratio*100:.0f}%{extra}"
        )

    def notify_scan_candidates(self, candidates: list[dict], slots: int):
        """이번 주기에 모인 진입 후보 목록을 텔레그램으로도 보여준다 (로그와 동일 정보)."""
        if not candidates:
            return
        top = candidates[:5]
        lines = [f"🔍 이번 주기 후보 {len(candidates)}개 (슬롯 {slots}개)"]
        for c in top:
            lines.append(
                f"  {c['symbol']} {c['signal']} score={c['score']:.2f} "
                f"강도={c['strength']:.2f} 확률={c.get('probability', 0)*100:.0f}%"
            )
        self.send("\n".join(lines))

    def notify_exit(self, symbol: str, action: str, pnl_pct_value: float):
        emoji = {"TAKE_PROFIT": "✅", "STOP_LOSS": "🛑", "EARLY_EXIT": "⚡", "SOFT_STOP": "🟡", "TIME_STOP": "⏰", "FUNDING_FORCE_CLOSE": "💸"}.get(action, "ℹ️")
        self.send(f"{emoji} 청산 {symbol} {action}\npnl: {pnl_pct_value:.2f}%")

    def notify_startup(self, mode: str, symbol_count: int):
        self.send(f"🤖 봇 시작 — {mode}\n대상 심볼 수: {symbol_count}")
        # [2026-08-11 사용자요청] "메뉴 고정 멘트는 이제 없어도 돼" — 오늘 재시작이 잦아서
        # 매번 이 안내가 반복되는 게 불필요해짐. 메뉴 자체(/menu, 하단 버튼)는 그대로 쓸 수
        # 있고, 여기서 재기동 시 자동 발송만 멈춘다.

    def notify_error(self, symbol: str, message: str):
        self.send(f"⚠️ {symbol} 오류\n{message}")

    def ask_daily_checkpoint(self, daily_pnl_pct: float, threshold: float):
        """일일 수익률이 체크포인트(목표 또는 목표+10%씩)에 도달하면 계속할지 물어본다.
        답이 올 때까지 신규 진입은 자동으로 멈춘다."""
        self.trading_paused = True
        self._awaiting_confirmation = True
        keyboard = {
            "inline_keyboard": [
                [{"text": "✅ 계속", "callback_data": "confirm_yes"},
                 {"text": "🛑 중단", "callback_data": "confirm_no"}],
            ]
        }
        self.send(
            f"🎯 오늘 봇 수익 +{daily_pnl_pct:.1f}% 달성! (체크포인트 {threshold:.0f}%, 직접매매 제외 기준)\n"
            f"계속 매매할까요?\n"
            f"(답할 때까지 신규 진입은 잠시 멈춰요. 기존 포지션은 그대로 관리됩니다.)",
            reply_markup=keyboard,
        )

    def _balance_text(self) -> str:
        balance = self.ex.get_available_balance_usdt()
        total = self.ex.get_total_margin_balance()
        margin_ratio = self.ex.get_margin_ratio()
        return (
            f"총자산: {total:.2f} USDT\n"
            f"가용잔고: {balance:.2f} USDT\n"
            f"마진 비율: {margin_ratio*100:.1f}%"
        )

    def _stats_text(self) -> str:
        win_rate = (self.pm.wins / self.pm.total_trades * 100) if self.pm.total_trades else 0.0
        long_total = self.pm.long_wins + self.pm.long_losses
        short_total = self.pm.short_wins + self.pm.short_losses
        long_rate = (self.pm.long_wins / long_total * 100) if long_total else 0.0
        short_rate = (self.pm.short_wins / short_total * 100) if short_total else 0.0
        lines = [
            "🏆 성과 통계",
            f"연속 승리: {self.pm.win_streak}회",
            f"누적 거래: {self.pm.total_trades}건 (승 {self.pm.wins} / 패 {self.pm.losses})",
            f"승률: {win_rate:.1f}%",
            # [2026-08-10 사용자요청] 전체 승률만 보면 롱/숏 중 한쪽이 나빠도 숨겨진다 —
            # 방향별로 따로 보여줘야 실제 어느 쪽 로직이 문제인지 바로 보인다.
            f"🟢 LONG: {self.pm.long_wins}승 {self.pm.long_losses}패 (승률 {long_rate:.1f}%, 손익 {self.pm.long_pnl_usdt:+.2f}USDT)",
            f"🔴 SHORT: {self.pm.short_wins}승 {self.pm.short_losses}패 (승률 {short_rate:.1f}%, 손익 {self.pm.short_pnl_usdt:+.2f}USDT)",
            f"누적 실현손익: {self.pm.realized_pnl_usdt:+.2f} USDT (봇이 직접 진입한 거래만, 수동 진입분 제외)",
        ]
        try:
            dw = self.ex.get_deposit_withdraw_totals()
            lines.append(
                f"누적 입금: {dw['deposited']:.2f} USDT / 누적 출금: {dw['withdrawn']:.2f} USDT"
            )
        except Exception:
            pass
        return "\n".join(lines)

    def _positions_text(self) -> str:
        if not self.pm.positions:
            return "보유 포지션 없음"
        lines = ["--- 보유 포지션 ---"]
        for symbol, pos in self.pm.positions.items():
            try:
                mark = self.ex.get_mark_price(symbol)
                pnl = pnl_pct(pos.entry_price, mark, pos.side)
                lines.append(f"{symbol} {pos.side} entry={pos.entry_price} qty={pos.quantity} pnl={pnl:.2f}%")
            except Exception:
                lines.append(f"{symbol} {pos.side} entry={pos.entry_price} qty={pos.quantity}")
        return "\n".join(lines)

    def _status_text(self) -> str:
        return self._balance_text() + "\n\n" + self._stats_text() + "\n\n" + self._positions_text()

    def _parse_symbol_and_side(self, text: str) -> tuple[str | None, str | None]:
        """'APTUSDT 롱', '숏 APT', 'apt long' 처럼 심볼과 방향을 같이 입력하면
        둘 다 인식한다. 방향 단어가 없으면 side는 None(자동 판단)을 반환한다."""
        LONG_WORDS = {"롱", "LONG", "매수", "BUY"}
        SHORT_WORDS = {"숏", "SHORT", "매도", "SELL"}
        tokens = text.strip().upper().replace("/", " ").split()
        side = None
        symbol_token = None
        for tok in tokens:
            if tok in LONG_WORDS:
                side = "LONG"
            elif tok in SHORT_WORDS:
                side = "SHORT"
            else:
                symbol_token = tok
        return symbol_token, side

    def _analyze_symbol_text(self, raw_symbol: str, requested_side: str | None = None) -> str:
        """봇이 실제로 진입 판단에 쓰는 기준들을 그대로 돌려서, 이 코인이 지금
        스캘핑하기 좋은 자리인지 100점 만점 점수와 근거를 계산한다.
        requested_side를 주면(사용자가 롱/숏을 직접 지정하면) 그 방향으로 강제 평가하고,
        없으면 지표상 더 우세한 방향을 자동으로 고른다."""
        symbol = raw_symbol.upper().strip()
        if not symbol.endswith("USDT"):
            symbol += "USDT"

        try:
            df = self.ex.get_klines(symbol)
            df = add_indicators(df, self.cfg)
        except Exception as e:
            return f"⚠️ {symbol} 데이터를 가져오지 못했어요 (없는 심볼이거나 일시적 오류: {e})"

        if len(df) < self.cfg.ema_slow + 2:
            return f"⚠️ {symbol}은 데이터가 부족해서(신규 상장 등) 분석할 수 없어요."

        long_score, short_score, curr = _direction_scores(df, self.cfg)
        if requested_side in ("LONG", "SHORT"):
            side = requested_side
            matched = long_score if side == "LONG" else short_score
        else:
            side = "LONG" if long_score >= short_score else "SHORT"
            matched = max(long_score, short_score)
        probability = estimate_entry_probability(matched, curr["adx"], self.cfg.probability_adx_cap)

        speed = quick_profit_score(df, self.cfg, side)
        momentum_ok = immediate_momentum_ok(df, side)
        vol_ok = volume_direction_ok(df, side, self.cfg)
        agree, total = mtf_trend_alignment(self.ex, self.cfg, symbol, side)
        mtf_ratio = (agree / total) if total else 0.0

        try:
            funding = self.ex.get_funding_rate_pct(symbol)
        except Exception:
            funding = None

        # 실제 진입 로직과 같은 가중치로 100점 만점 점수를 구성한다.
        score_probability = probability * 50       # 신호 신뢰도(6개 지표+추세강도) — 절반 비중
        score_speed = speed * 20                    # 변동성 대비 목표 도달 가능성
        score_momentum = 10 if momentum_ok else 0    # 과열/캔들방향 확인
        score_volume = 10 if vol_ok else 0            # 실제 매수/매도 체결 방향성
        score_mtf = mtf_ratio * 10                    # 상위 시간대(5분/15분) 정합
        total_score = round(score_probability + score_speed + score_momentum + score_volume + score_mtf)

        side_kr = "롱(상승)" if side == "LONG" else "숏(하락)"
        direction_note = " — 직접 지정" if requested_side in ("LONG", "SHORT") else " — 자동판단"
        lines = [
            f"📊 {symbol} 스캘핑 점수: {total_score}/100  ({side_kr} 방향 기준{direction_note})",
            "",
            f"- 신호 신뢰도(확률 추정): {probability*100:.0f}% → {score_probability:.0f}/50점",
            f"- 변동성/속도 점수: {speed:.2f} → {score_speed:.0f}/20점",
            f"- 진입 모멘텀(과열·캔들방향): {'통과' if momentum_ok else '미통과'} → {score_momentum}/10점",
            f"- 거래량 방향성(매수/매도 체결비): {'통과' if vol_ok else '미통과'} → {score_volume}/10점",
            f"- 상위시간대(5분/15분) 정합: {agree}/{total} → {score_mtf:.0f}/10점",
        ]
        if funding is not None:
            whale_note = " ⚠️ 쏠림 큼(고래성)" if abs(funding) > self.cfg.max_abs_funding_rate_pct else ""
            lines.append(f"- 펀딩비: {funding:.3f}%{whale_note}")
        lines.append("")
        if total_score >= 80:
            lines.append("→ 지금 봇 기준으로도 진입 후보에 들 만큼 확실한 자리예요.")
        elif total_score >= 60:
            lines.append("→ 나쁘지 않지만, 봇의 실제 진입 기준(확률 70~85%대)엔 약간 못 미쳐요.")
        else:
            lines.append("→ 지금은 신호가 약해요. 스캘핑 진입엔 신중하는 게 좋아요.")
        return "\n".join(lines)

    def propose_tuning(self, diagnosis: str, changes: dict):
        """1시간마다 손익을 분석한 뒤, 진입 기준을 조이거나 풀자는 제안을 승인 버튼과 함께 보낸다.
        사용자가 승인해야만 실제로 적용된다 (자동 적용 아님)."""
        self._pending_tune = changes
        self._pending_tune_diagnosis = diagnosis
        lines = ["🧠 1시간 손익 분석", diagnosis, "", "제안하는 변경:"]
        for key, value in changes.items():
            lines.append(f"  {key} → {value}")
        keyboard = {
            "inline_keyboard": [
                [{"text": "✅ 적용", "callback_data": "tune_yes"},
                 {"text": "❌ 무시", "callback_data": "tune_no"}],
            ]
        }
        self.send("\n".join(lines), reply_markup=keyboard)

    def _apply_tuning(self, changes: dict) -> list[str]:
        """여러 항목을 한 번에 적용한다. 적용 후 cfg.validate()로 전체 정합성을
        재확인해서, 예를 들어 LEVERAGE_MAX를 LEVERAGE_MIN보다 낮게 바꿔버리는 것처럼
        항목 하나만 보면 멀쩡해도 다른 값과 합쳐지면 모순되는 상태를 걸러낸다.
        문제가 있으면 이번에 바꾼 항목들을 전부 원래 값으로 되돌리고 실패를 알린다."""
        previous = {}
        applied = []
        for key, value in changes.items():
            mapping = TUNABLE_PARAMS.get(key)
            if not mapping:
                continue
            attr, _type, _lo, _hi = mapping
            previous[key] = (attr, getattr(self.cfg, attr))
            setattr(self.cfg, attr, value)
            applied.append(f"{key} → {value}")

        try:
            self.cfg.validate()
        except Exception as e:
            for key, (attr, old_value) in previous.items():
                setattr(self.cfg, attr, old_value)
            return [f"⚠️ 적용 취소됨 — 다른 설정과 충돌해서 되돌렸어요: {e}"]

        for key in changes:
            if key in previous:
                set_env_value(key, changes[key])
        return applied

    def _record_tune_history(self, decision: str, changes: dict, diagnosis: str = ""):
        self._tune_history.append({"ts": time.time(), "changes": changes, "decision": decision, "diagnosis": diagnosis})
        self._tune_history = self._tune_history[-20:]  # 최근 20건만 보관

    def _settings_text(self) -> str:
        lines = ["⚙️ 현재 자동조정 대상 설정값"]
        for key, (attr, _type, lo, hi) in TUNABLE_PARAMS.items():
            value = getattr(self.cfg, attr)
            bound = f" (범위 {lo}~{hi})" if lo is not None else ""
            label_ko = TUNABLE_PARAMS_KO.get(key, key)
            lines.append(f"  {label_ko}: {value}{bound}")

        lines.append("")
        lines.append("📜 최근 제안/변경 이력 (최근 5건)")
        if not self._tune_history:
            lines.append("  아직 없음")
        else:
            label = {"applied": "✅적용", "ignored": "❌무시", "manual": "✍️직접변경"}
            for h in self._tune_history[-5:]:
                when = time.strftime("%m-%d %H:%M", time.localtime(h["ts"]))
                changes_text = ", ".join(
                    f"{TUNABLE_PARAMS_KO.get(k, k)}→{v}" for k, v in h["changes"].items()
                ) or "(변경없음)"
                lines.append(f"  [{when}] {label.get(h['decision'], h['decision'])}: {changes_text}")

        lines.append("")
        lines.append("직접 값을 바꾸려면 이렇게 입력하세요:")
        lines.append("  설정변경 KEY 값   (예: 설정변경 STOP_LOSS_PCT 4.5)")
        return "\n".join(lines)

    def _manual_set(self, key: str, raw_value: str) -> str:
        key = key.strip().upper()
        mapping = TUNABLE_PARAMS.get(key)
        if not mapping:
            return f"'{key}'는 직접 변경 가능한 항목이 아니에요. /설정 으로 가능한 목록을 확인하세요."
        attr, _type, lo, hi = mapping
        try:
            if _type is bool:
                value = raw_value.strip().lower() in ("1", "true", "yes", "y", "on", "켜기", "켬")
            else:
                value = _type(raw_value)
        except ValueError:
            return f"값 '{raw_value}'을(를) 해석할 수 없어요. 숫자로 입력해주세요."
        if lo is not None and not (lo <= value <= hi):
            return f"{key}는 {lo}~{hi} 범위 안에서만 바꿀 수 있어요 (입력값: {value})."

        old_value = getattr(self.cfg, attr)
        setattr(self.cfg, attr, value)
        try:
            self.cfg.validate()
        except Exception as e:
            setattr(self.cfg, attr, old_value)
            return f"⚠️ 이 값은 다른 설정과 충돌해서 적용하지 않았어요: {e}"

        set_env_value(key, value)
        self._record_tune_history("manual", {key: value})
        return f"✅ {key} → {value} 로 변경했어요."

    def send_digest(self, total_balance: float):
        """주기적으로(기본 10분마다) 기준자산/오늘 봇 수익/보유 포지션 요약을 보낸다.
        [2026-08-09] 오늘 수익은 봇 실현손익 기준(직접매매 제외)으로 통일."""
        state = self.daily_state
        if state and state.get("start_balance"):
            start = state["start_balance"]
            bot_pnl_start = state.get("bot_pnl_start", self.pm.realized_pnl_usdt)
            daily_bot_pnl_usdt = self.pm.realized_pnl_usdt - bot_pnl_start
            daily_bot_pnl_pct = (daily_bot_pnl_usdt / start * 100) if start else 0.0
            header = (
                f"📋 정기 요약\n"
                f"기준자산: {start:.2f} USDT\n"
                f"현재 총자산: {total_balance:.2f} USDT\n"
                f"오늘 봇 수익: {daily_bot_pnl_usdt:+.2f} USDT ({daily_bot_pnl_pct:+.2f}%, 직접매매 제외)"
            )
        else:
            header = f"📋 정기 요약\n현재 총자산: {total_balance:.2f} USDT"
        self.send(header + "\n\n" + self._positions_text())

    def _daily_pnl_text(self) -> str:
        """[2026-08-09] 봇 자체 실현손익(origin=bot)만으로 계산 — 사용자 직접매매는 제외.
        지갑 잔고 전체 변화는 아래 별도 줄로 참고용만 보여준다."""
        state = self.daily_state
        if not state or not state.get("start_balance"):
            return "아직 오늘 기준 자산이 설정되지 않았어요 (봇이 막 시작됐을 수 있어요)."
        total = self.ex.get_total_margin_balance()
        start = state["start_balance"]
        bot_pnl_start = state.get("bot_pnl_start", self.pm.realized_pnl_usdt)
        daily_bot_pnl_usdt = self.pm.realized_pnl_usdt - bot_pnl_start
        daily_bot_pnl_pct = (daily_bot_pnl_usdt / start * 100) if start else 0.0
        next_threshold = state.get("next_threshold")
        return (
            f"오늘({state['date']}) 기준자산: {start:.2f} USDT\n"
            f"현재 총자산: {total:.2f} USDT (직접매매 포함, 참고용)\n"
            f"오늘 봇 수익: {daily_bot_pnl_usdt:+.2f} USDT ({daily_bot_pnl_pct:+.2f}%, 직접매매 제외)\n"
            f"다음 체크포인트: {next_threshold:.0f}%"
        )

    def _close_menu_keyboard(self) -> dict | None:
        if not self.pm.positions:
            return None
        rows = [[{"text": f"❌ {symbol} 종료", "callback_data": f"close:{symbol}"}] for symbol in self.pm.positions]
        rows.append([{"text": "취소", "callback_data": "cancel"}])
        return {"inline_keyboard": rows}

    def _close_position(self, symbol: str) -> str:
        pos = self.pm.positions.get(symbol)
        if pos is None:
            return f"{symbol} 포지션을 찾을 수 없어요 (이미 종료됐을 수 있어요)."
        try:
            mark_price = self.ex.get_mark_price(symbol)
            result_pnl = pnl_pct(pos.entry_price, mark_price, pos.side)
            result_pnl_usdt = profit_usdt(pos, mark_price)
            if pos.stop_order_id:
                self.ex.cancel_order(symbol, pos.stop_order_id)
            if pos.trailing_order_id:
                self.ex.cancel_order(symbol, pos.trailing_order_id)
            if pos.tp_fallback_order_id:
                self.ex.cancel_order(symbol, pos.tp_fallback_order_id)
            self.ex.close_market_position(symbol, pos.side, abs(pos.quantity))
        except Exception:
            log.exception("[%s] 수동 청산 실패", symbol)
            return f"⚠️ {symbol} 청산 요청이 실패했어요. 바이낸스 앱에서 직접 확인해주세요."
        self.pm.untrack(symbol)
        self.pm.record_result(symbol, result_pnl, result_pnl_usdt, origin=pos.origin, side=pos.side)
        try:
            now = time.time()
            record = TradeRecord(
                symbol=symbol, side=pos.side, origin=pos.origin, entry_reason="PUMP_SIGNAL",
                exit_reason="MANUAL_CLOSE_TELEGRAM", entry_price=pos.entry_price, exit_price=mark_price,
                quantity=abs(pos.quantity), leverage=pos.leverage,
                entered_at=pos.entered_at, exited_at=now, held_seconds=now - pos.entered_at,
                estimated_pnl_pct=result_pnl, estimated_pnl_usdt=result_pnl_usdt,
                bot_version="2026-08-09-protection-fallback-v1", config_snapshot=strategy_config_snapshot(self.cfg),
            )
            append_trade_record(record)
        except Exception:
            log.exception("[%s] 거래 원장 레코드 생성 실패 (거래 자체는 정상 처리됨)", symbol)
        return f"✅ {symbol} 포지션을 종료했어요 (pnl={result_pnl:.2f}%, {result_pnl_usdt:+.2f}USDT)"

    def _handle_command(self, text: str):
        stripped = text.strip()
        cmd = stripped.lower().lstrip("/")
        # 고정 메뉴(리플라이 키보드) 버튼은 "💰 잔고"처럼 이모지+공백이 앞에 붙어서 온다.
        # 기존 텍스트 명령("잔고" 등)과 같은 분기를 타도록 이모지/기호를 떼어내고 비교한다.
        cmd_no_emoji = "".join(ch for ch in cmd if ch.isalnum()).strip()

        # "설정변경 KEY 값" 형식으로 직접 파라미터를 바꿀 수 있게 한다.
        # 접두어 뒤에 공백을 안 붙이는 실수("설정변경STOP_LOSS_PCT 4.5")도 인식하되,
        # 영어 "set"은 SETUSDT 같은 실제 코인 티커와 헷갈리지 않도록 뒤에 공백이
        # 있을 때만(단어 경계) 명령으로 인식한다. "설정변경"은 그런 코인이 없어 안전하다.
        lowered = stripped.lower()
        tune_prefix = None
        if lowered.startswith("설정변경"):
            tune_prefix = "설정변경"
        elif lowered.startswith("set ") or lowered == "set":
            tune_prefix = "set"
        if tune_prefix is not None:
            rest = stripped[len(tune_prefix):].strip()
            parts = rest.split()
            if len(parts) >= 2:
                self.send(self._manual_set(parts[0], parts[1]))
            else:
                self.send("형식: 설정변경 KEY 값  (예: 설정변경 STOP_LOSS_PCT 4.5)")
            return

        if self._awaiting_confirmation:
            if cmd in ("네", "예", "y", "yes", "계속", "ok"):
                self.trading_paused = False
                self._awaiting_confirmation = False
                self.send("✅ 알겠습니다. 매매를 계속 진행할게요.")
                return
            if cmd in ("아니오", "아니요", "n", "no", "중단", "stop"):
                self._awaiting_confirmation = False
                self.trading_paused = True
                self.send("🛑 알겠습니다. 오늘은 신규 진입을 중단할게요 (기존 포지션은 정상 관리됩니다).")
                return

        if cmd in ("status", "상태") or cmd_no_emoji in ("status", "상태"):
            self.send(self._status_text())
        elif cmd in ("balance", "잔고") or cmd_no_emoji in ("balance", "잔고"):
            self.send(self._balance_text())
        elif cmd in ("positions", "포지션") or cmd_no_emoji in ("positions", "포지션"):
            self.send(self._positions_text())
        elif cmd in ("daily", "오늘수익률", "수익률") or cmd_no_emoji in ("daily", "오늘수익률", "수익률"):
            self.send(self._daily_pnl_text())
        elif cmd in ("stats", "성과", "통계", "승률") or cmd_no_emoji in ("stats", "성과", "통계", "승률"):
            self.send(self._stats_text())
        elif cmd in ("settings", "설정", "이력") or cmd_no_emoji in ("settings", "설정", "이력"):
            self.send(self._settings_text())
        elif cmd in ("menu", "메뉴") or cmd_no_emoji in ("menu", "메뉴"):
            self.send_menu()
        elif cmd_no_emoji in ("포지션종료", "종료"):
            keyboard = self._close_menu_keyboard()
            if keyboard is None:
                self.send("현재 보유 중인 포지션이 없어요.")
            else:
                self.send("종료할 포지션을 선택하세요:", reply_markup=keyboard)
        elif cmd in ("start", "help", "도움말"):
            self.send(
                "사용 가능한 명령어: /menu (버튼 메뉴), /status (잔고·포지션 확인), "
                "/settings (설정값+변경이력 확인)\n"
                "설정을 직접 바꾸려면: 설정변경 KEY 값 (예: 설정변경 STOP_LOSS_PCT 4.5)\n"
                "코인 이름만 입력하면(예: ZECUSDT 또는 ZEC) 스캘핑 점수를 분석해드려요.\n"
                "방향을 직접 지정하려면: 심볼+롱/숏 (예: APTUSDT 롱, 숏 APT, apt long)"
            )
            self.send_menu()
        else:
            candidate, requested_side = self._parse_symbol_and_side(text)
            # 코인 티커는 영문 알파벳+숫자만 쓴다. str.isalnum()은 한글도 통과시켜서
            # ("어때".isalnum() == True) 사용자가 그냥 채팅한 문장의 마지막 단어를
            # 심볼로 오인해 "XX코인 못 찾음" 같은 엉뚱한 응답을 보내는 문제가 있었다.
            is_ticker_like = bool(candidate) and 2 <= len(candidate) <= 15 and candidate.replace("USDT", "").isascii() and candidate.replace("USDT", "").isalnum()
            if is_ticker_like:
                self.send(self._analyze_symbol_text(candidate, requested_side))
            else:
                self.send(
                    "무슨 말인지 이해하지 못했어요. /menu 로 메뉴를 보거나, "
                    "코인 이름(예: BTCUSDT)을 입력해보세요."
                )

    def _handle_callback(self, callback_query: dict):
        callback_id = callback_query.get("id", "")
        data = callback_query.get("data", "")
        self._answer_callback(callback_id)

        if self._awaiting_confirmation and data in ("confirm_yes", "confirm_no"):
            if data == "confirm_yes":
                self.trading_paused = False
                self._awaiting_confirmation = False
                self.send("✅ 알겠습니다. 매매를 계속 진행할게요.")
            else:
                self._awaiting_confirmation = False
                self.trading_paused = True
                self.send("🛑 알겠습니다. 오늘은 신규 진입을 중단할게요 (기존 포지션은 정상 관리됩니다).")
            return

        if data == "balance":
            self.send(self._balance_text())
        elif data == "positions":
            self.send(self._positions_text())
        elif data == "daily_pnl":
            self.send(self._daily_pnl_text())
        elif data == "stats":
            self.send(self._stats_text())
        elif data == "settings":
            self.send(self._settings_text())
        elif data == "close_menu":
            keyboard = self._close_menu_keyboard()
            if keyboard is None:
                self.send("현재 보유 중인 포지션이 없어요.")
            else:
                self.send("종료할 포지션을 선택하세요:", reply_markup=keyboard)
        elif data == "cancel":
            self.send("취소했어요.")
        elif data.startswith("close:"):
            symbol = data.split(":", 1)[1]
            result = self._close_position(symbol)
            self.send(result)
        elif data == "tune_yes":
            if self._pending_tune:
                applied = self._apply_tuning(self._pending_tune)
                self._record_tune_history("applied", self._pending_tune, self._pending_tune_diagnosis)
                self._pending_tune = None
                self.send("✅ 수정 처리됐어요:\n" + "\n".join(applied))
            else:
                self.send("적용할 제안이 없어요 (이미 처리됐거나 만료됨).")
        elif data == "tune_no":
            if self._pending_tune:
                self._record_tune_history("ignored", self._pending_tune, self._pending_tune_diagnosis)
            self._pending_tune = None
            self.send("❌ 이번 제안은 무시할게요. 기존 설정 그대로 유지합니다.")

    def _poll_loop(self):
        while True:
            try:
                resp = requests.get(
                    self._url("getUpdates"),
                    params={"offset": self._offset + 1, "timeout": 20},
                    timeout=25,
                )
                data = resp.json()
                for update in data.get("result", []):
                    self._offset = update["update_id"]

                    callback_query = update.get("callback_query")
                    if callback_query:
                        chat_id = str(callback_query.get("message", {}).get("chat", {}).get("id", ""))
                        if chat_id == str(self.cfg.telegram_chat_id):
                            self._handle_callback(callback_query)
                        continue

                    message = update.get("message", {})
                    chat_id = str(message.get("chat", {}).get("id", ""))
                    text = message.get("text", "")
                    if chat_id == str(self.cfg.telegram_chat_id) and text:
                        self._handle_command(text)
            except Exception:
                log.exception("텔레그램 명령어 수신 중 오류")
                time.sleep(5)

    def start_command_listener(self):
        if not self.enabled:
            return
        thread = threading.Thread(target=self._poll_loop, daemon=True)
        thread.start()
