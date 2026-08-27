"""스캘핑 봇 e2 - EMA 정배열 눌림목 전략 (실주문).

e1(급변동 추종)과 완전히 별개의 전략이다. 신호/진입/청산 전부 다르다.

[전략 - 사용자 제시안 그대로, 1분봉 기준]
  지표   EMA 5 / 10 / 15 / 25, 볼린저밴드(20, 2σ)
  진입   1. 정배열 확인 (롱: EMA5>10>15>25 / 숏: 반대)
         2. 즉시 진입하지 않고 눌림 대기
         3. 1차: 캔들이 EMA5 도달
         4. 2차: EMA10까지 밀릴 때
         5. 3차: EMA15까지 추가 하락
  손절   EMA25 이탈 시 즉시
  익절   볼린저 상단(숏은 하단) 도달, 또는 손익비 1:2 도달

[검증 결과 - 반드시 읽을 것]
  2026-08-20, 85심볼 10일 초봉, 수수료 왕복 0.1002% 차감:
    1차만+BB+RR2   129,841건  거래당 -0.0296%  승률 46.7%  총액 -3839.8
    1차만+BB       129,841건  거래당 -0.0260%  승률 45.6%
    3차분할+BB+RR2  80,212건  거래당 -0.0577%  승률 27.2%
    2차분할+BB+RR2  98,224건  거래당 -0.0669%  승률 34.4%
  6개 변형 전부 마이너스다. 청산사유 분해:
    STOP_EMA25  63,278건  평균 -0.4546%   <- EMA25 손절이 가변폭이라 깊다
    BB          53,153건  평균 +0.4132%
  분할할수록 승률이 급락한다(46.7% -> 34.4% -> 27.2%). 눌림이 깊어져 2·3차까지
  가는 건 추세가 깨지는 중이라는 뜻이고, 그 자리에 추가 투입하니 손실이 커진다.

  **이 전략은 검증에서 기각됐다. 실주문 사용을 권하지 않는다.**
  드라이런으로 동작만 확인하는 용도로 만들었다.

[사용]
  python scripts/scalp_bot_e2.py --minutes 20 --dry-run          # 권장
  python scripts/scalp_bot_e2.py --minutes 20 --i-know-it-loses  # 실주문
"""
from __future__ import annotations

import argparse
import atexit
import json
import os
import signal
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bot.config import Config
from bot.exchange import Exchange
from bot.ws_client import FileBackedKlineCache

VERSION = "e2"
LOG_DIR = Path(__file__).resolve().parent.parent / "logs"
LEDGER = LOG_DIR / f"scalp_bot_{VERSION}_ledger.jsonl"
# [2026-08-20 버그4] e2 가 관리하는 심볼 목록. 재시작 시 남의 포지션을 채택하지 않기 위함.
STATE = LOG_DIR / f"scalp_bot_{VERSION}_state.json"
WS_PID_FILE = LOG_DIR / f"scalp_bot_{VERSION}_ws_pid.json"
# [2026-08-21] 봇 본체의 중복 실행을 막는다. WS 워커만 PID 관리가 있었고
# 본체는 없어서, 재시작할 때마다 인스턴스가 쌓여 6개가 동시에 실주문을 냈다.
BOT_PID_FILE = LOG_DIR / f"scalp_bot_{VERSION}_bot_pid.json"


@dataclass
class Pos:
    symbol: str
    side: str
    legs: list = field(default_factory=list)     # 분할 진입가 목록
    qty: float = 0.0
    entered_at: float = 0.0
    leverage: int = 5
    stop_price: float = 0.0
    tp_bb: float = 0.0
    tp_rr: float = 0.0
    max_adverse_roe: float = 0.0
    max_favorable_roe: float = 0.0
    stop_algo_id: int = 0
    # [2026-08-20 버그3] 실손익 집계용. 진입 직전의 마지막 체결 id.
    # 이보다 큰 체결만 이 포지션 것으로 본다. 같은 심볼을 반복 거래할 때
    # 이전 포지션의 체결이 섞여 real_net 이 오염되는 것을 막는다.
    since_trade_id: int = 0
    adopted: bool = False        # 재시작 채택분은 진입 수수료를 알 수 없다

    @property
    def entry(self) -> float:
        return sum(self.legs) / len(self.legs) if self.legs else 0.0


def fee_aware_rr_price(entry: float, stop: float, side: str, rr: float,
                       roundtrip_fee_rate: float) -> float:
    """순손익 기준 RR이 맞도록 수수료를 TP 가격에 반영한다.

    기존 계산은 gross 기준 `rr * risk`만 더했다. 실제 손절은 가격 손실에
    왕복 수수료가 더해지고, 익절은 가격 이익에서 왕복 수수료가 빠진다.
    순손익 기준 RR을 맞추려면:
      gross_tp = rr * gross_stop_risk + (rr + 1) * roundtrip_fee
    """
    if entry <= 0 or stop <= 0 or rr <= 0:
        return 0.0
    risk = abs(entry - stop) / entry
    if risk <= 0:
        return 0.0
    fee = max(0.0, roundtrip_fee_rate)
    gross_target = rr * risk + (rr + 1.0) * fee
    if side == "LONG":
        return entry * (1.0 + gross_target)
    return entry * (1.0 - gross_target)


def early_cut_reason(pos: Pos, roe: float, now_ts: float,
                     early_adverse_sec: float,
                     early_adverse_roe: float,
                     early_adverse_min_favorable_roe: float,
                     mae_cut_roe: float,
                     mae_cut_grace_sec: float,
                     mae_cut_min_favorable_roe: float) -> str | None:
    """진입 직후 역행과 깊은 MAE를 기존 STOP보다 먼저 자른다.

    max_favorable_roe 가 충분했던 거래는 단순 역행이 아니라 되돌림일 수 있어
    이 컷의 대상에서 제외한다.
    """
    if pos.entered_at <= 0:
        return None
    held_sec = max(0.0, now_ts - pos.entered_at)
    early_limit = -abs(early_adverse_roe)
    if (early_adverse_sec > 0 and early_adverse_roe > 0
            and held_sec <= early_adverse_sec
            and roe <= early_limit
            and pos.max_favorable_roe < early_adverse_min_favorable_roe):
        return "EARLY_ADVERSE"

    mae_limit = -abs(mae_cut_roe)
    if (mae_cut_roe > 0
            and held_sec >= max(0.0, mae_cut_grace_sec)
            and pos.max_adverse_roe <= mae_limit
            and roe <= mae_limit
            and pos.max_favorable_roe < mae_cut_min_favorable_roe):
        return "MAE_CUT"
    return None


def padded_tp(entry: float, tp: float, side: str,
              extra_roe_pct: float, leverage: int) -> float:
    """익절선을 ROE 기준 extra_roe_pct 만큼 더 멀리 민다.

    [2026-08-21 사용자요청] "목표 수익 %를 0.5%정도만 더 늘리면 어때?"

    단위는 **ROE(%)** 다 — 손절 쪽 --stop-widen-pct 가 가격 % 인 것과 다르다.
    사용자가 "목표 수익 %" 를 말할 때 기준으로 삼는 값이 ROE 이기 때문이다
    (실측 볼밴 익절 ROE 중앙 +2.11%). 격리 2배면 ROE 0.5% = 가격 0.25%.

    실측 근거: 볼밴 익절이 너무 빨리 걸려 보유 중앙값이 4.0분이고 15분 이상은
    9.6% 뿐이다. 목표를 늦추면 보유시간이 늘고 건당 이익이 커지지만, 대신
    도달 못하고 되돌아 손절로 끝나는 건이 늘어난다 — 측정 대상이다.

    tp 가 0(익절 비활성)이면 그대로 0을 돌려준다.
    """
    if entry <= 0 or tp <= 0 or extra_roe_pct <= 0 or leverage <= 0:
        return tp
    extra = entry * (extra_roe_pct / 100.0 / leverage)
    return tp + extra if side == "LONG" else tp - extra


def deepen_target(target: float, side: str, depth_pct: float) -> float:
    """진입 목표선을 지정한 %(가격)만큼 더 깊게 민다.

    [2026-08-21] 백테스트 85심볼 10일 — **깊이가 지금까지 잰 것 중 가장 큰 축**이다.
    거래당 순익(수수료 후):
      깊이 0.0%(현행) -0.0958%  |  0.2% -0.0748%  |  0.3% -0.0619%
      깊이 0.5%        -0.0482%  (+0.0476%p, t -6.78 -> -1.74)
    대가는 거래수 42%. 목표가 깊어지면 도달 빈도가 줄기 때문이다.

    같은 축을 EMA 기간으로도 확인했다(진입선 EMA5 -0.1039% -> EMA15 -0.0533%
    -> EMA20 +0.0342%). "더 깊이 산다"는 하나의 축이며 EMA 기간 변경과 동치다.
    EMA 기간 대신 이 인자를 쓰는 이유: 정배열 판정(e5>e10>e15>e25)을 건드리지
    않고 목표가만 조절할 수 있어 변수가 하나로 유지된다.
    """
    if target <= 0 or depth_pct <= 0:
        return target
    return (target * (1 - depth_pct / 100.0) if side == "LONG"
            else target * (1 + depth_pct / 100.0))


def tp_with_floor(entry: float, tp: float, side: str,
                  floor_roe_pct: float, leverage: int) -> float:
    """익절선에 ROE 하한을 씌운다 — 볼밴선과 하한선 중 **더 먼 쪽**.

    [2026-08-21] 볼밴 익절이 너무 빨리 걸려 보유 중앙값이 10분이었다.
    ROE 하한별 실측(85심볼 10일):
      하한 0%(현행) 보유중앙 10분 / 거래당 -0.1061%
      하한 3%       보유중앙 14분 / 거래당 -0.0838%
      하한 6%       보유중앙 20분 / 거래당 -0.0724%
    목표를 멀리 둘수록 거래당 손실이 준다. 다만 손절률이 39.6% -> 61.7% 로 오르고,
    보유시간 분포는 양극화된다(15~20분 구간 비중은 오히려 감소, 60분+ 가 증가).

    tp 가 0(익절 비활성)이면 하한선만으로 익절선을 만든다.
    """
    if entry <= 0 or floor_roe_pct <= 0 or leverage <= 0:
        return tp
    fl = (entry * (1 + floor_roe_pct / 100.0 / leverage) if side == "LONG"
          else entry * (1 - floor_roe_pct / 100.0 / leverage))
    if tp <= 0:
        return fl
    return max(tp, fl) if side == "LONG" else min(tp, fl)


def tranche_targets(ind: dict, side: str, tranches: int,
                    second_at_band: bool, min_gap_pct: float = 0.0) -> list:
    """분할 진입 목표가 목록. k번째 차수는 targets[k] 를 터치하면 들어간다.

    기본은 [EMA5, EMA10, EMA15] 로 눌림을 단계적으로 받는다.

    [2026-08-21 사용자요청] second_at_band 면 2차를 **볼린저 반대편 밴드**로
    바꾼다(롱이면 하단, 숏이면 상단). 얕은 눌림이 아니라 밴드까지 밀렸을 때만
    2차를 넣어 평단을 더 유리하게 만든다.

    [2026-08-21 실사고] 배포 직후 2차가 1차와 **사실상 같은 가격**에 체결됐다:
      LITUSDT   1차 2.739798 / 2차 2.738500  간격 0.047% (ROE 0.095%)
      AVAAIUSDT 1차 0.013492 / 2차 0.013456  간격 0.267% (ROE 0.534%)
    손절이 1.5%(ROE 3.0%)인데 2차가 0.15% 지점이면 분할이 아니라 그냥 두 배로
    한 번에 들어가는 것이다. 원인: 볼밴이 손절선 밖이라 EMA10 으로 되돌아갔는데
    추세장에서 EMA10 은 EMA5 바로 옆이다.

    그래서 2차 목표를 아래 순서로 정한다:
      1. second_at_band 면 밴드(1차보다 깊을 때만)
      2. min_gap_pct 가 있으면 1차에서 최소 그만큼은 떨어뜨린다
      3. 손절선(EMA25)을 넘어가면 1차~손절선의 80% 지점으로 당긴다
         (되돌리기보다 이쪽이 낫다 — EMA10 으로 돌아가면 위 실사고가 재발한다)
      4. 그래도 1차보다 안쪽이면 2차를 아예 두지 않는다
    """
    base = [ind["e5"], ind["e10"], ind["e15"]][:max(0, tranches)]
    if len(base) < 2:
        return base
    long_ = side == "LONG"
    first, stop = base[0], ind["e25"]
    deeper = (lambda a, b: min(a, b)) if long_ else (lambda a, b: max(a, b))
    cand = base[1]

    if second_at_band:
        band = ind["bb_l"] if long_ else ind["bb_u"]
        if band > 0 and ((band < first) if long_ else (band > first)):
            cand = band

    if min_gap_pct > 0:
        floor = (first * (1 - min_gap_pct / 100.0) if long_
                 else first * (1 + min_gap_pct / 100.0))
        cand = deeper(cand, floor)

    if (cand <= stop) if long_ else (cand >= stop):
        cand = first - 0.8 * (first - stop) if long_ else first + 0.8 * (stop - first)

    if (cand >= first) if long_ else (cand <= first):
        return base[:1]
    base[1] = cand
    return base


def widened_stop(entry: float, stop: float, side: str, widen_pct: float) -> float:
    """손절선이 진입가에서 최소 widen_pct% 는 떨어지도록 밀어낸다.

    [2026-08-21] 손절선은 항상 EMA25 였다. 실측(원장 130건)에서 손절 28건 중
    50%가 진입 후 단 한 틱도 유리한 적이 없었고, 반대로 익절 102건의
    최대불리 ROE 중앙값은 +0.00% 였다 — 이길 거래는 손절선 근처에 가지도
    않는다. 즉 손절을 넓혀도 이긴 거래를 잃지 않는다(-2% 아래로 밀렸던
    승리는 102건 중 1건뿐).

    실제 1분봉 재생(28건, 120분 관찰)에서는 시험한 모든 확대폭이 현재보다
    나았다(-4.33 -> -3.60~-0.86). 다만 -3%와 -5%의 순서가 뒤집히는 비단조라
    표본 28건으로는 '방향'만 신뢰하고 최적폭은 신뢰하지 않는다.

    0 이면 기존 동작(EMA25 그대로)을 유지한다.
    """
    if entry <= 0 or stop <= 0 or widen_pct <= 0:
        return stop
    if side == "LONG":
        return min(stop, entry * (1.0 - widen_pct / 100.0))
    return max(stop, entry * (1.0 + widen_pct / 100.0))


def fee_aware_bb_price(entry: float, bb_price: float, side: str,
                       roundtrip_fee_rate: float,
                       min_net_profit_rate: float = 0.0) -> float:
    """BB 익절선이 수수료 후 플러스가 아닐 때는 BB 청산을 비활성화한다."""
    if entry <= 0 or bb_price <= 0:
        return 0.0
    gross = (bb_price / entry - 1.0) if side == "LONG" else (1.0 - bb_price / entry)
    if gross <= max(0.0, roundtrip_fee_rate) + max(0.0, min_net_profit_rate):
        return 0.0
    return bb_price


class Tg:
    def __init__(self, cfg: Config):
        self.token = cfg.telegram_bot_token
        self.chat = cfg.telegram_chat_id
        self.enabled = bool(self.token and self.chat)
        self.offset = 0

    def _api(self, method: str, payload: dict, timeout: float = 10):
        """텔레그램 Bot API 호출. 실패해도 봇 본체를 멈추지 않는다."""
        if not self.enabled:
            return None
        import urllib.request
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{self.token}/{method}",
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"})
        try:
            return json.loads(urllib.request.urlopen(req, timeout=timeout).read())
        except Exception:
            return None

    def send(self, text: str, reply_markup: dict | None = None) -> None:
        pl = {"chat_id": self.chat, "text": text}
        if reply_markup:
            pl["reply_markup"] = reply_markup
        self._api("sendMessage", pl)

    # 하단 고정 메뉴의 버튼 문구 -> 동작 코드.
    # [2026-08-20] 인라인 버튼은 메시지가 쌓이면 위로 흘러가 상시 조작이 안 된다.
    # 라이브 봇과 같은 방식(리플라이 키보드 + is_persistent)으로 바꿨다.
    BUTTONS = {
        "📊 e2상태": "status",
        "📈 e2브리핑": "brief",
        "📋 e2포지션": "pos",
        "📉 e2복기": "review",
        "⏸ e2정지": "pause",
        "▶️ e2재개": "resume",
        "🛑 e2전량청산": "flat",
    }

    def menu(self) -> None:
        """화면 하단에 고정되는 e2 조작 메뉴. 라이브 봇 메뉴와 문구가 겹치지 않게
        전부 'e2' 를 붙였다 — 두 봇이 같은 채팅방을 쓰기 때문."""
        self.send(f"[{VERSION}] 조작 메뉴를 하단에 고정했습니다. 언제든 누르세요.", {
            "keyboard": [
                [{"text": "📊 e2상태"}, {"text": "📈 e2브리핑"}],
                [{"text": "📋 e2포지션"}, {"text": "📉 e2복기"}],
                [{"text": "⏸ e2정지"}, {"text": "▶️ e2재개"}],
                [{"text": "🛑 e2전량청산"}],
            ],
            "resize_keyboard": True,
            "is_persistent": True,
        })

    def poll(self) -> list:
        """대기 중인 버튼 입력을 가져온다. 반환: [(callback_id, action)]"""
        r = self._api("getUpdates", {"offset": self.offset, "timeout": 0,
                                     "allowed_updates": ["callback_query", "message"]},
                      timeout=8)
        out = []
        if not r or not r.get("ok"):
            return out
        for u in r.get("result", []):
            self.offset = u["update_id"] + 1
            cq = u.get("callback_query")
            if cq and str(cq.get("data", "")).startswith("e2:"):
                out.append((cq["id"], cq["data"][3:]))
                continue
            txt = ((u.get("message") or {}).get("text") or "").strip()
            if not txt:
                continue
            if txt in ("/e2", "e2", "/menu"):
                self.menu()
            elif txt in self.BUTTONS:
                # 리플라이 키보드는 콜백이 아니라 일반 메시지로 온다.
                # 응답할 callback_id 가 없으므로 빈 문자열을 넘긴다.
                out.append(("", self.BUTTONS[txt]))
        return out

    def answer(self, cq_id: str, text: str = "") -> None:
        if not cq_id:          # 리플라이 키보드 입력은 응답할 콜백이 없다
            return
        self._api("answerCallbackQuery", {"callback_query_id": cq_id, "text": text[:190]})


def last_trade_id(ex, symbol: str) -> int:
    """심볼의 최신 체결 id. 실패하면 0(=필터 안 함)."""
    try:
        tr = ex.client.futures_account_trades(symbol=symbol, limit=1)
        return int(tr[-1]["id"]) if tr else 0
    except Exception:
        return 0


def _weighted_avg_fill(trades: list[dict], side: str) -> tuple[float, float]:
    qty_sum = 0.0
    notional_sum = 0.0
    for t in trades:
        if str(t.get("side")) != side:
            continue
        qty = abs(float(t.get("qty", 0) or 0))
        px = float(t.get("price", 0) or 0)
        if qty <= 0 or px <= 0:
            continue
        qty_sum += qty
        notional_sum += qty * px
    return ((notional_sum / qty_sum) if qty_sum > 0 else 0.0, qty_sum)


def live_position_qty(ex, symbol: str, fallback: float) -> float:
    """거래소가 보고하는 실제 보유 수량. 봇 내부 누적은 어긋날 수 있다.

    [2026-08-21] 내부 누적(`_p.qty += ...`)은 체결 조회가 한 번만 틀려도
    영구히 어긋난다. 그 수량으로 손절주문을 걸고 청산을 시도하므로 위험하다.
    진입/추가진입 직후에는 거래소 값을 정본으로 덮어쓴다.
    """
    try:
        for p in ex.client.futures_account()["positions"]:
            if p.get("symbol") == symbol:
                amt = abs(float(p.get("positionAmt", 0) or 0))
                return amt if amt > 0 else fallback
    except Exception:
        pass
    return fallback


def merge_symbol_universe(top_symbols: list[str], held_symbols, limit: int) -> list[str]:
    """거래량 상위 목록을 기준으로 하되, 보유 중인 심볼은 빠져도 관리 대상에 남긴다."""
    out = list(top_symbols[: max(0, limit)])
    seen = set(out)
    for sym in sorted(set(held_symbols or [])):
        if sym not in seen:
            out.append(sym)
            seen.add(sym)
    return out


def entry_fill_after(ex, symbol: str, side: str, since_trade_id: int,
                     fallback_price: float, expected_qty: float = 0.0) -> tuple[float, float]:
    """시장가 진입 직후 실제 진입 평균가를 가져온다. 실패하면 주문 직전 mark 를 쓴다."""
    fill_side = "BUY" if side == "LONG" else "SELL"
    # [2026-08-21 P0] since_trade_id 가 0 이면 필터를 못 건다. 그 상태로 합산하면
    # 최근 50건의 같은 방향 체결을 전부 더해 버린다.
    # 실측: BOMEUSDT 2차 진입에서 12,113 대신 265,690 이 반환돼 봇 내부 수량이
    # 277,809(거래소 실제 24,232)로 어긋났다. 손절주문 수량도 그 값으로 나간다.
    # last_trade_id() 가 조회 실패 시 조용히 0 을 반환하는 것이 발단이었다.
    if not since_trade_id:
        return fallback_price, expected_qty
    for _ in range(3):
        try:
            rows = ex.client.futures_account_trades(symbol=symbol, limit=50)
            rows = [t for t in rows if int(t.get("id", 0)) > since_trade_id]
            px, qty = _weighted_avg_fill(rows, fill_side)
            # 주문 수량보다 크게 벗어나면 이번 차수의 체결만 걸러진 게 아니다
            if expected_qty > 0 and qty > expected_qty * 1.5:
                return fallback_price, expected_qty
            if px > 0 and (expected_qty <= 0 or qty >= expected_qty * 0.95):
                return px, qty
        except Exception:
            pass
        time.sleep(0.2)
    return fallback_price, expected_qty


def realized_fill_snapshot(trades: list[dict], side: str, fallback_entry: float,
                           fallback_exit: float, fallback_qty: float,
                           leverage: int) -> dict:
    """실체결 평균가로 원장 표시값(entry/exit/roe/nominal)을 맞춘다.

    기존 구현은 신호/마크 가격으로 ROE를 기록해서, 실제 체결 손익(realizedPnL)과
    방향이 어긋나는 거래가 있었다. 실손익 집계는 그대로 두고 표시값만 같은 체결 집합으로
    재구성하면 슬롯/원장/브리핑 숫자가 일관된다.
    """
    entry_side = "BUY" if side == "LONG" else "SELL"
    exit_side = "SELL" if side == "LONG" else "BUY"
    entry_px, entry_qty = _weighted_avg_fill(trades, entry_side)
    exit_px, exit_qty = _weighted_avg_fill(trades, exit_side)
    entry_px = entry_px or fallback_entry
    exit_px = exit_px or fallback_exit
    qty = max(entry_qty, exit_qty, abs(fallback_qty))
    nominal = entry_px * qty
    roe = 0.0
    if entry_px > 0 and leverage > 0:
        gross = ((exit_px / entry_px) - 1.0) if side == "LONG" else ((entry_px / exit_px) - 1.0)
        roe = gross * leverage * 100.0
    return {
        "entry_price": entry_px,
        "exit_price": exit_px,
        "quantity": qty,
        "nominal": nominal,
        "roe_pct": roe,
    }


def sync_stop(ex, symbol: str, side: str, qty: float, stop: float,
              old_algo_id: int = 0, dry: bool = False, warn=None) -> int:
    """거래소 손절주문을 '항상 하나만' 유지한다.

    [2026-08-20 버그1 / P0] 이전 구현은 추가 진입 시 새 leg 수량으로 손절을 먼저 걸고,
    그 다음 합산 수량으로 또 걸면서 먼저 건 주문을 취소하지도 추적하지도 않았다.
    남은 고아 STOP_MARKET 이 나중에 따로 발동해 포지션 일부를 예기치 않게 잘라낸다.
    등록은 반드시 이 함수 하나만 거치게 한다: 먼저 취소, 그 다음 등록.
    """
    if dry:
        return 0
    if old_algo_id:
        try:
            ex.cancel_order(symbol, old_algo_id)
        except Exception:
            pass
    try:
        r = ex.place_stop_market(symbol, side, qty, stop)
        return int((r or {}).get("algoId") or 0)
    except Exception as e:
        if warn:
            warn(f"경고 {symbol} 손절주문 등록 실패({e}) - 봇 폴링 손절만 남음")
        return 0


def ema_last(vals, span):
    a = 2 / (span + 1)
    p = vals[0]
    for v in vals[1:]:
        p = a * v + (1 - a) * p
    return p


# WS 캔들 캐시가 1분봉 200개를 들고 있다(ws_client.py MAX_LEN=200).
# limit 을 200 이하로 요청해야 REST 폴백 없이 캐시를 쓴다 — 85심볼을 매 사이클
# REST 로 긁으면 예전 IP 밴 사고와 같은 경로가 된다.
WS_KLINE_CACHE_LEN = 200


def klines_limit_for_tf(minutes: int, need_bars: int = 40) -> int:
    """신호봉 minutes 분짜리를 need_bars 개 만들려면 1분봉이 몇 개 필요한가."""
    if minutes <= 1:
        return 99
    return min(WS_KLINE_CACHE_LEN, need_bars * minutes + minutes)


def resample_bars(df, minutes: int):
    """1분봉 DataFrame 을 minutes 분봉으로 합친다.

    [2026-08-21 사용자요청] "진입도 1분봉이 아닌 노이즈때문에 3분봉으로 갈아타보자"

    실측(85심볼 10일, 거래당 순익 / 보유 중앙):
      1분 -0.1025% / 10분   2분 -0.0845% / 16분   3분 -0.0831% / 20분
      5분 -0.0417% / 25분  10분 -0.0481% / 39분
    노이즈 가설이 확인됐다. 3분봉이면 보유 중앙값이 20분으로 미니스윙 구간이 된다.

    **벽시계 경계(00:00, 00:03, ...)로 묶는다.** 끝에서부터 N개씩 묶으면 매 분
    봉 경계가 밀려 지표가 흔들린다.

    마지막 묶음이 minutes 개를 못 채웠으면 **미완성 봉이므로 버린다** —
    get_klines 가 1분봉의 미완성 캔들을 이미 잘라내는 것과 같은 원칙이다.
    """
    if minutes <= 1 or df is None or len(df) == 0:
        return df
    import pandas as _pd
    bucket = _pd.to_datetime(df["open_time"]).dt.floor(f"{minutes}min")
    g = df.groupby(bucket, sort=True)
    # open_time 은 **버킷 경계**를 쓴다(첫 1분봉의 시각이 아니라).
    # 그래야 같은 봉이 항상 같은 시각을 갖는다.
    out = _pd.DataFrame({
        "open": g["open"].first(),
        "high": g["high"].max(),
        "low": g["low"].min(),
        "close": g["close"].last(),
        "volume": g["volume"].sum(),
    })
    out.index.name = "open_time"
    out = out.reset_index()
    sizes = g.size().tolist()
    if len(out) and sizes[-1] < minutes:
        out = out.iloc[:-1].reset_index(drop=True)
    return out


def signal_bars(ex, symbol: str, minutes: int):
    """신호 판정용 봉을 가져온다. minutes>1 이면 1분봉을 합쳐서 만든다."""
    df = ex.get_klines(symbol, limit=klines_limit_for_tf(minutes))
    return resample_bars(df, minutes)


def indicators(df):
    """EMA 5/10/15/25 와 볼린저(20,2) 를 1분봉 종가로 계산."""
    c = [float(x) for x in df["close"].tolist()]
    if len(c) < 30:
        return None
    e5, e10, e15, e25 = (ema_last(c, 5), ema_last(c, 10),
                         ema_last(c, 15), ema_last(c, 25))
    w = c[-20:]
    mu = sum(w) / 20
    sd = (sum((x - mu) ** 2 for x in w) / 20) ** 0.5
    return {"e5": e5, "e10": e10, "e15": e15, "e25": e25,
            "bb_u": mu + 2 * sd, "bb_l": mu - 2 * sd, "close": c[-1]}


def append_ledger(rec: dict) -> None:
    LEDGER.parent.mkdir(parents=True, exist_ok=True)
    with LEDGER.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def _pid_alive(pid: int) -> bool:
    """해당 PID 가 살아 있는지. Windows 는 signal 0 이 통하지 않을 수 있어
    tasklist 로 확인한다."""
    if pid <= 0:
        return False
    if os.name == "nt":
        try:
            out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}"],
                                 capture_output=True, text=True, timeout=10)
            return str(pid) in (out.stdout or "")
        except Exception:
            return False
    try:
        os.kill(pid, 0)
        return True
    except Exception:
        return False


def acquire_bot_lock() -> bool:
    """이미 다른 인스턴스가 돌고 있으면 False.

    [2026-08-21] 실사고: Git Bash 의 ps/kill 이 Windows python.exe 를 죽이지
    못하는데 grep -c 는 0 을 반환해 '죽었다'고 오판했다. 그 결과 구버전 4개 +
    신버전 2개가 동시에 실주문을 냈고 1차 투입이 의도의 1/3 로 쪼개졌다.
    """
    try:
        if BOT_PID_FILE.exists():
            old = json.loads(BOT_PID_FILE.read_text(encoding="utf-8"))
            opid = int(old.get("pid") or 0)
            if opid and opid != os.getpid() and _pid_alive(opid):
                print(f"[중단] 이미 e2 봇이 실행 중입니다 (PID {opid}). "
                      f"먼저 종료하거나 {BOT_PID_FILE.name} 를 지우십시오.",
                      flush=True)
                return False
    except Exception:
        pass
    try:
        BOT_PID_FILE.write_text(
            json.dumps({"pid": os.getpid(), "ts": time.time()}), encoding="utf-8")
    except Exception:
        pass
    return True


def release_bot_lock() -> None:
    try:
        if BOT_PID_FILE.exists():
            cur = json.loads(BOT_PID_FILE.read_text(encoding="utf-8"))
            if int(cur.get("pid") or 0) == os.getpid():
                BOT_PID_FILE.unlink()
    except Exception:
        pass


def _clear_ws_pid_file() -> None:
    try:
        WS_PID_FILE.unlink(missing_ok=True)
    except Exception:
        pass


def _record_ws_pid(proc: subprocess.Popen) -> None:
    try:
        WS_PID_FILE.write_text(json.dumps({"pid": int(proc.pid), "ts": time.time()}), encoding="utf-8")
    except Exception:
        pass


def _terminate_pid(pid: int) -> bool:
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return False
    except Exception:
        return False
    return True


def cleanup_tracked_ws_worker() -> int:
    if not WS_PID_FILE.exists():
        return 0
    try:
        payload = json.loads(WS_PID_FILE.read_text(encoding="utf-8"))
        pid = int(payload.get("pid") or 0)
    except Exception:
        _clear_ws_pid_file()
        return 0
    killed = 1 if pid > 0 and _terminate_pid(pid) else 0
    _clear_ws_pid_file()
    return killed


def start_ws(symbols):
    env = dict(os.environ)
    env.update({"WS_WORKER_ROLE": "market", "WS_SHARD_INDEX": "0", "WS_SHARD_COUNT": "1",
                "WS_WORKER_SYMBOLS": json.dumps(list(symbols), ensure_ascii=False),
                "WS_KLINE_HISTORY_LEN": "150", "WS_KLINE_MAX_STALENESS_SEC": "90"})
    proc = subprocess.Popen([sys.executable, "-m", "bot.ws_worker"],
                            cwd=str(Path(__file__).resolve().parent.parent), env=env)
    _record_ws_pid(proc)
    return proc, FileBackedKlineCache(LOG_DIR / "ws_worker_cache.json",
                                      LOG_DIR / "ws_worker_heartbeat.txt",
                                      status_path=LOG_DIR / "ws_worker_status.json")


def stop_ws(proc) -> None:
    if proc is None:
        return
    try:
        proc.terminate()
        proc.wait(timeout=10)
    except Exception:
        try:
            proc.kill()
            proc.wait(timeout=5)
        except Exception:
            pass
    _clear_ws_pid_file()


def main() -> int:
    p = argparse.ArgumentParser(description=f"스캘핑 봇 {VERSION} - EMA 눌림목")
    p.add_argument("--minutes", type=float, default=0, help="0=무기한")
    p.add_argument("--leverage", type=int, default=5)
    p.add_argument("--symbols", type=int, default=85)
    p.add_argument("--tranches", type=int, default=1,
                   help="분할 진입 차수(1~3). 검증상 분할할수록 승률 급락(46.7->27.2%%)")
    p.add_argument("--rr", type=float, default=2.0, help="손익비 익절 배수. 0=사용안함")
    p.add_argument("--roundtrip-fee-rate", type=float, default=None,
                   help="진입+청산 왕복 수수료율. 0.001002=0.1002%%. 기본은 Config.FEE_RATE_ROUNDTRIP")
    p.add_argument("--min-net-tp-rate", type=float, default=0.0002,
                   help="BB 익절도 수수료 차감 후 최소 이 순수익률 이상일 때만 사용")
    p.add_argument("--min-risk-pct", type=float, default=0.35,
                   help="진입가~EMA25 거리 하한(%%). 이보다 가까우면 진입하지 않는다. "
                        "[드라이런 실측] EMA25가 현재가에 붙어 손절폭이 0.09%%까지 좁아져 "
                        "노이즈에 즉시 손절됐다.")
    # [2026-08-21] --min-risk-pct 는 손절을 넓히는 값이 아니라 진입을 거르는
    # 필터다(EMA25 가 가까우면 그 거래를 버린다). 손절폭 자체를 넓히려면
    # 이 인자를 쓴다. 필터를 통과한 뒤에 적용하므로 진입 종목 수는 안 바뀐다.
    p.add_argument("--stop-widen-pct", type=float, default=0.0,
                   help="손절 최소폭(진입가 대비 %%). EMA25 가 이보다 가까우면 "
                        "이 폭까지 밀어낸다. 0=기존 동작(EMA25 그대로)")
    # [2026-08-21 사용자요청] "볼밴 하단에 닿았을때 2차 매수".
    # 2차 목표를 EMA10 대신 볼린저 반대편 밴드로 바꾼다. tranches>=2 에서만 의미.
    p.add_argument("--tranche2-band", action="store_true",
                   help="2차 진입 목표를 볼린저 반대편 밴드로 (기본: EMA10)")
    # [2026-08-21 사용자요청] 목표 수익을 조금 더 멀리. 단위는 ROE(%%) 다.
    # 손절쪽 --stop-widen-pct 는 가격 %% 이므로 헷갈리지 말 것.
    # [2026-08-21 실사고] 2차가 1차와 0.047% 차이로 체결돼 분할이 무의미했다.
    # 1차에서 최소 이만큼(가격 %%)은 떨어진 곳에만 2차를 둔다.
    p.add_argument("--tranche-min-gap-pct", type=float, default=0.0,
                   help="2차 목표를 1차에서 최소 이만큼 떨어뜨린다(가격 %%). 0=사용안함")
    # [2026-08-21] 진입 목표선을 EMA 에서 이만큼 더 깊게 민다(가격 %%).
    # 백테스트에서 가장 큰 개선 축이었다(0.5% 에서 +0.0476%%p, 거래수 42%%).
    p.add_argument("--entry-depth-pct", type=float, default=0.0,
                   help="진입 목표선을 EMA 에서 이만큼 더 깊게(가격 %%). 0=기존")
    # [2026-08-21] 익절 ROE 하한. 볼밴선과 하한선 중 더 먼 쪽을 쓴다.
    # [2026-08-21 사용자요청] 신호 판정 봉 길이(분). 1=기존 1분봉.
    # 1분봉을 합쳐서 만들므로 WS 캐시를 그대로 쓴다(추가 API 호출 없음).
    p.add_argument("--signal-tf-min", type=int, default=1,
                   help="신호 판정 봉 길이(분). 1=기존")
    p.add_argument("--tp-floor-roe-pct", type=float, default=0.0,
                   help="익절 ROE 하한(%%). 0=기존(볼밴선 그대로)")
    p.add_argument("--tp-extra-roe-pct", type=float, default=0.0,
                   help="볼밴 익절선을 ROE 기준 이만큼 더 멀리 (0=기존)")
    p.add_argument("--cooldown-sec", type=float, default=300.0,
                   help="같은 심볼 청산 후 재진입 금지 시간(초). "
                        "[드라이런 실측] DOGEUSDT가 같은 가격에 5회 연속 재진입했다.")
    p.add_argument("--max-concurrency", type=int, default=8)
    # [2026-08-20] 1차 투입이 1 USDT 수준이면 몇 % 먹어도 절대금액이 안 남는다.
    # 슬롯 수를 "1차당 증거금 >= min-leg-margin" 이 되도록 역산한다.
    p.add_argument("--min-leg-margin", type=float, default=3.0)
    p.add_argument("--max-exposure", type=float, default=0.95)
    p.add_argument("--min-notional", type=float, default=5.0)
    p.add_argument("--poll", type=float, default=10.0)
    p.add_argument("--bar-align", action="store_true", default=True,
                   help="매 분 00초 직후에 스캔한다. e1 실측: 진입 지연이 5초를 넘으면 "
                        "우위가 사라졌다(0초 +0.0750%%, 5초 -0.0079%%, 30초 -0.0997%%).")
    p.add_argument("--no-bar-align", dest="bar_align", action="store_false")
    p.add_argument("--max-signal-age", type=float, default=5.0,
                   help="봉 확정 후 이 초를 넘으면 진입하지 않는다. 0=제한없음")
    p.add_argument("--max-hold-sec", type=float, default=0.0,
                   help="보유 시간이 이 초를 넘고 STOP/BB/RR이 아직 아니면 시간 만료로 청산한다. 0=사용안함")
    p.add_argument("--early-adverse-sec", type=float, default=180.0,
                   help="진입 후 이 시간 안의 즉시 역행 컷 감시 구간(초). 0=사용안함")
    p.add_argument("--early-adverse-roe", type=float, default=1.5,
                   help="즉시 역행 컷 ROE 손실 기준(%%). 예: 1.5면 -1.5%% ROE")
    p.add_argument("--early-adverse-min-favorable-roe", type=float, default=0.5,
                   help="보유 중 최고 ROE가 이 값 이상이면 즉시 역행 컷 제외(%%)")
    p.add_argument("--mae-cut-roe", type=float, default=3.0,
                   help="MAE 기반 조기 컷 ROE 손실 기준(%%). 0=사용안함")
    p.add_argument("--mae-cut-grace-sec", type=float, default=180.0,
                   help="진입 후 MAE 컷을 적용하기 전 대기 시간(초)")
    p.add_argument("--mae-cut-min-favorable-roe", type=float, default=1.0,
                   help="보유 중 최고 ROE가 이 값 이상이면 MAE 컷 제외(%%)")
    p.add_argument("--symbol-refresh-sec", type=float, default=1800.0,
                   help="SYMBOLS=AUTO일 때 거래량 상위 심볼 목록을 다시 뽑는 주기(초). 0=기동 시 1회만")
    p.add_argument("--brief-on-clock", action="store_true",
                   help="매시 정각/30분에 브리핑을 보낸다")
    p.add_argument("--ws", action="store_true", default=True)
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--no-telegram", action="store_true")
    # [2026-08-21] 거래소 손절주문을 이 시간 안에 못 걸면 포지션을 정리한다.
    # 손절 없는 포지션을 방치하는 것이 이 프로젝트 최악의 사고였다(ROE -38.79%).
    p.add_argument("--max-unprotected-sec", type=float, default=120.0)
    # [2026-08-20] 순익을 이 잔고 기준으로 센다. 0=기동 시점 잔고를 기준으로 삼음.
    p.add_argument("--base-balance", type=float, default=0.0)
    p.add_argument("--no-buttons", dest="buttons", action="store_false",
                   help="텔레그램 조작 버튼을 띄우지 않는다")
    p.add_argument("--i-know-it-loses", action="store_true",
                   help="검증에서 전 조합 마이너스였음을 알고도 실주문한다")
    args = p.parse_args()

    if not args.dry_run and not args.i_know_it_loses:
        print("[중단] 이 전략은 85심볼 10일 검증에서 6개 변형 전부 마이너스였다.")
        print("       거래당 -0.0296% ~ -0.0669% (표본 8만~13만건)")
        print("       --dry-run 으로 동작만 보거나, 그래도 실주문하려면 --i-know-it-loses")
        return 1

    if not args.dry_run and not acquire_bot_lock():
        return 1

    cfg = Config()
    if args.roundtrip_fee_rate is None:
        args.roundtrip_fee_rate = float(getattr(cfg, "fee_rate_roundtrip", 0.001))
    cfg.ws_kline_max_staleness_sec = 90.0
    ex = Exchange(cfg)
    tg = None if args.no_telegram else Tg(cfg)

    def say(msg, tg_send=True):
        print(msg, flush=True)
        if tg and tg_send:
            tg.send(f"[{VERSION}] {msg}")

    bal0 = args.base_balance if args.base_balance > 0 else ex.get_total_margin_balance()
    symbols = (ex.get_active_usdt_perpetual_symbols(limit=args.symbols)
               if cfg.auto_symbols else list(cfg.symbols)[: args.symbols])
    mode = "DRY-RUN(주문없음)" if args.dry_run else "실주문"
    say(f"재시작/기동 감지 [{mode}] 잔고 {bal0:.4f} / {args.leverage}배 / "
        f"{len(symbols)}심볼 / 분할{args.tranches}차 / 손익비{args.rr}")

    ws_proc, ws_cache = (None, None)
    ws_ready = not args.ws
    ws_ready_count = 0
    ws_ready_need = 0
    ws_ready_deadline = 0.0
    ws_next_check_at = 0.0
    ws_bad_since = 0.0
    ws_last_restart_at = 0.0
    ws_restart_count = 0
    reconcile_next_at = 0.0
    symbol_refresh_next_at = time.time() + args.symbol_refresh_sec if (
        cfg.auto_symbols and args.symbol_refresh_sec > 0) else float("inf")
    if args.ws:
        _old_ws = cleanup_tracked_ws_worker()
        if _old_ws:
            say(f"이전 e2 WS 워커 {_old_ws}건 정리 후 재기동")
        ws_proc, ws_cache = start_ws(symbols)
        ws_ready_need = min(8, len(symbols[:10]))
        ws_ready_deadline = time.time() + 100.0
        ws_next_check_at = time.time()
        say("WS 워커 기동 - 준비 상태 확인 시작 (최대 100초). "
            "준비 전에는 보유 포지션 관리만 하고 신규/추가 진입은 막습니다")

    say(f"시작 [{mode}] EMA눌림목 / 잔고 {bal0:.4f} / {args.leverage}배 / "
        f"{len(symbols)}심볼 / 분할{args.tranches}차 / 손익비{args.rr}")

    positions: dict[str, Pos] = {}
    pending: dict[str, dict] = {}     # 정배열 확인 후 눌림 대기 중
    # 거래소 STOP_MARKET 등록 실패 포지션. 봇 폴링 손절만 남은 위험 상태라
    # 루프에서 재등록을 계속 시도하고, 성공 전까지 경고를 반복한다.
    unprotected: dict[str, dict] = {}
    # [2026-08-20] 브리핑이 '청산 완료' 건만 세고 있어서, 진입만 하고 아직 안 닫힌
    # 거래가 통째로 빠졌다. 텔레그램으로 진입 알림을 받았는데 브리핑은 0건으로
    # 나오는 문제. 진입 시각도 따로 남긴다. (진입시각, 방향)
    # 채택 블록에서 참조하므로 반드시 그보다 먼저 선언해야 한다.
    entries: list[tuple] = []

    # [2026-08-20 버그4] e2 가 자기 것만 채택하도록 상태파일로 소유권을 남긴다.
    # 전체 계좌 포지션을 무조건 채택하면 라이브 봇이나 수동 포지션까지 e2 가
    # 손절 재등록·청산 관리를 해버린다(교차 간섭).
    _owned: set = set()
    _owned_at: dict = {}
    if STATE.exists():
        try:
            _st = json.loads(STATE.read_text(encoding="utf-8"))
            _owned = set(_st.get("symbols", []))
            _owned_at = {k: float(v) for k, v in (_st.get("entered_at") or {}).items()}
        except Exception:
            _owned, _owned_at = set(), {}

    def save_state():
        """소유 심볼과 진입시각을 남긴다. 진입시각이 있어야 재시작해도 보유시간과
        시간손절(--max-hold-sec)이 이어진다.
        [2026-08-20] 폴백인 positionAmt.updateTime 은 '마지막 변경 시각'이라
        3분할 포지션에서는 3차 진입 시각이 잡힌다. 1차 시각은 여기에만 있다."""
        try:
            STATE.write_text(json.dumps(
                {"symbols": sorted(positions),
                 "entered_at": {k: v.entered_at for k, v in positions.items()},
                 "legs": {k: len(v.legs) for k, v in positions.items()}},
                ensure_ascii=False), encoding="utf-8")
        except Exception:
            pass

    # [2026-08-20] 재시작 시 거래소에 이미 있는 포지션을 채택한다.
    # 채택하지 않으면 봇이 잊어버려 손절도 익절도 안 된다(실사고: BTWUSDT ROE -38.79%).
    # [2026-08-21] 계좌에 이미 걸려 있는 손절주문을 미리 파악한다.
    # 채택 시 이걸 취소하지 않으면 중복 등록되고, 포지션이 없는 심볼의 것은
    # 고아로 남아 나중에 예기치 않게 발동한다.
    _existing_stops: dict = {}
    if not args.dry_run:
        try:
            _ao = ex.client.futures_get_open_algo_orders()
            _rows = _ao.get("orders", _ao) if isinstance(_ao, dict) else _ao
            for _o in _rows or []:
                _existing_stops.setdefault(_o.get("symbol"), []).append(
                    int(_o.get("algoId") or 0))
        except Exception as e:
            say(f"경고 기존 손절주문 조회 실패({e})")

    if not args.dry_run:
        try:
            _live = [x for x in ex.client.futures_account()["positions"]
                     if float(x.get("positionAmt", 0) or 0) != 0]
        except Exception as e:
            _live = []
            say(f"경고 기존 포지션 조회 실패({e})")
        if _owned:
            _skip = [x["symbol"] for x in _live if x["symbol"] not in _owned]
            _live = [x for x in _live if x["symbol"] in _owned]
            if _skip:
                say("채택 제외(e2 소유 아님): " + ", ".join(_skip)
                    + " - 다른 봇이나 수동 포지션이면 그쪽에서 관리하세요")
        elif _live:
            # 상태파일이 없는 첫 기동이다. 버리는 쪽이 더 위험하므로 전부 채택하되 알린다.
            say(f"상태파일 없음 - 계좌 포지션 {len(_live)}건을 전부 e2 것으로 채택합니다")
        _adopted = 0
        _sl_ok = 0
        _sl_fail = 0
        for lp in _live:
            _sym = lp["symbol"]
            _amt = float(lp["positionAmt"])
            _side = "LONG" if _amt > 0 else "SHORT"
            _ep = float(lp["entryPrice"])
            _qty = abs(_amt)
            if _ep <= 0 or _qty <= 0:
                continue
            try:
                _lev = int(float(lp.get("leverage") or args.leverage))
            except Exception:
                _lev = args.leverage
            try:
                _df = signal_bars(ex, _sym, args.signal_tf_min)
                _ind = indicators(_df)
            except Exception:
                _ind = None
            _L = _side == "LONG"
            _stop = (_ind["e25"] if _ind else
                     _ep * (1 - 0.012 if _L else 1 + 0.012))
            # 재시작 전후로 같은 전략이 다르게 동작하면 안 된다.
            _stop = widened_stop(_ep, _stop, _side, args.stop_widen_pct)
            _tp_raw = (_ind["bb_u"] if _L else _ind["bb_l"]) if _ind else 0.0
            _tp = tp_with_floor(_ep, padded_tp(_ep, fee_aware_bb_price(
                _ep, _tp_raw, _side, args.roundtrip_fee_rate, args.min_net_tp_rate),
                _side, args.tp_extra_roe_pct, _lev),
                _side, args.tp_floor_roe_pct, _lev)
            # [2026-08-20 버그2] 채택 포지션의 손익비 1:2 익절이 사라져 있었다.
            # 재시작 전후로 같은 전략이 다르게 동작하면 안 된다. 평단과 EMA25 손절선으로
            # 손절폭을 재구성해 tp_rr 을 복원한다.
            _tprr = fee_aware_rr_price(
                _ep, _stop, _side, args.rr, args.roundtrip_fee_rate)
            # 진입시각: 상태파일이 1순위(1차 진입 시각까지 정확),
            # 없으면 updateTime 폴백(3분할이면 마지막 차수 시각으로 짧게 잡힌다)
            _entered_at = time.time()
            try:
                _ut = float(lp.get("updateTime") or 0) / 1000.0
                if 0 < _ut <= time.time():
                    _entered_at = _ut
            except Exception:
                pass
            _entered_at = _owned_at.get(_sym) or _entered_at
            _p = Pos(_sym, _side, [_ep], _qty, _entered_at, _lev,
                     stop_price=_stop, tp_bb=_tp, tp_rr=_tprr)
            _p.adopted = True
            _p.since_trade_id = last_trade_id(ex, _sym)
            # [2026-08-21] 채택은 sync_stop 을 거쳐 '기존 손절주문을 먼저 취소'해야 한다.
            # 그냥 새로 걸면 재시작할 때마다 같은 심볼에 손절주문이 하나씩 쌓인다.
            # 실측: 재가동 후 포지션 2건에 손절주문 4건(REUSDT 중복 + BOMEUSDT 고아).
            try:
                for _o in _existing_stops.get(_sym, []):
                    try:
                        ex.cancel_order(_sym, _o)
                    except Exception:
                        pass
                _r = ex.place_stop_market(_sym, _side, _qty, _stop)
                _p.stop_algo_id = int((_r or {}).get("algoId") or 0)
                _msg = "SL 등록"
                _sl_ok += 1
            except Exception as e:
                _msg = f"SL 등록실패({e})"
                _sl_fail += 1
                unprotected[_sym] = {"next_retry_at": 0.0, "last_warn_at": 0.0}
            positions[_sym] = _p
            _adopted += 1
            say(f"기존포지션 채택 {_sym} {_side} qty={_qty} 진입{_ep} 손절{_stop:.6f}"
                f" 보유{(time.time() - _entered_at) / 60:.1f}분 - {_msg}")
        # [2026-08-20] 채택 직후 바로 저장한다. 안 하면 포지션 변동이 한 번도
        # 없는 상태에서 또 재시작할 때 진입시각이 유실돼 보유시간이 리셋된다.
        save_state()
        # 포지션이 없는 심볼에 남은 손절주문은 고아다. 나중에 따로 발동할 수 있고,
        # 어긋난 수량이 그대로 남아 있는 경우도 있다(실측 BOMEUSDT qty=277,809).
        _orphan = 0
        for _sm, _ids in _existing_stops.items():
            if _sm in positions:
                continue
            for _o in _ids:
                try:
                    ex.cancel_order(_sm, _o)
                    _orphan += 1
                except Exception:
                    pass
        if _orphan:
            say(f"고아 손절주문 {_orphan}건 정리(보유 없는 심볼)")
        say(f"재시작 복구 요약: 채택 {_adopted}건 / SL성공 {_sl_ok}건 / SL실패 {_sl_fail}건")
        # 아직 안 닫힌 채택 포지션의 진입도 집계에 넣는다
        for _s2, _p2 in positions.items():
            if _p2.entered_at >= time.time() - 7500:
                entries.append((_p2.entered_at, _p2.side))
        entries.sort()
    deadline = (time.time() + args.minutes * 60) if args.minutes > 0 else float("inf")
    n_align = n_entry = n_exit = 0
    run_started_at = time.time()      # 정합성 대조의 기준 시각
    cooldown: dict[str, float] = {}
    skips = {"근접손절": 0, "쿨다운": 0, "익절선통과": 0, "신호노후": 0}
    stats = {"win": 0, "net": 0.0, "nom": 0.0}
    _lt0 = time.localtime()
    last_slot = (_lt0.tm_hour, 0 if _lt0.tm_min < 30 else 30)
    side_stat = {"LONG": [0, 0, 0.0], "SHORT": [0, 0, 0.0]}   # 건수, 승, 순익
    why_stat: dict[str, list] = {}
    # [2026-08-20] 최근 1시간 롤링 집계용. (청산시각, 방향, 순손익, 명목)
    # 재시작이 잦아서(프로세스 정리·패치) 원장에서 복원한다. 안 하면 재시작 직후
    # 브리핑의 "최근 1시간"이 0건으로 나와 성적을 오판하게 된다.
    recent: list[tuple] = []
    if LEDGER.exists():
        _cut = time.time() - 3600
        for _ln in LEDGER.read_text(encoding="utf-8").splitlines():
            if not _ln.strip():
                continue
            try:
                _r = json.loads(_ln)
            except Exception:
                continue
            if _r.get("dry_run") or _r.get("exited_at", 0) < _cut:
                continue
            recent.append((_r["exited_at"], _r["side"],
                           _r.get("real_net", 0.0), _r.get("nominal", 0.0)))
            if _r.get("entered_at", 0) >= _cut:
                entries.append((_r["entered_at"], _r["side"]))
        recent.sort()
        entries.sort()
        if recent:
            print(f"최근 1시간 집계 {len(recent)}건 원장에서 복원", flush=True)
        # [2026-08-20] 누적도 원장에서 복원한다. 재시작이 잦은 운영 환경에서 메모리 기준
        # 누적은 의미가 없고, 사용자는 e2 전체 실행 구간(현재는 base_balance=32.13 기준)의
        # 승률/순익을 보고 싶어 한다. dry_run은 제외한다.
        for _ln in LEDGER.read_text(encoding="utf-8").splitlines():
            if not _ln.strip():
                continue
            try:
                _r = json.loads(_ln)
            except Exception:
                continue
            if _r.get("dry_run"):
                continue
            n_exit += 1
            _net = float(_r.get("real_net", 0.0) or 0.0)
            _nom = float(_r.get("nominal", 0.0) or 0.0)
            stats["win"] += 1 if _net > 0 else 0
            stats["net"] += _net
            stats["nom"] += _nom
            _ss = side_stat.setdefault(_r.get("side", "?"), [0, 0, 0.0])
            _ss[0] += 1
            _ss[1] += 1 if _net > 0 else 0
            _ss[2] += _net
        if n_exit:
            print(f"누적 집계 {n_exit}건 원장에서 복원", flush=True)
    paused = False

    def hour_window():
        """[2026-08-20] 롤링 60분이 아니라 정각 기준 구간을 쓴다.
        정각 브리핑이면 직전 1시간(13:00~14:00) 완결 구간,
        30분 브리핑이면 진행 중인 시간의 앞 절반(14:00~14:30).
        롤링이면 브리핑마다 구간이 겹쳐서 같은 거래가 두 번 집계된다."""
        now = time.time()
        lt = time.localtime(now)
        top = now - lt.tm_min * 60 - lt.tm_sec      # 직전 정각
        if lt.tm_min < 2:                           # 정각 브리핑
            return top - 3600, top, time.strftime("%H:00", time.localtime(top - 3600)) \
                + "~" + time.strftime("%H:00", time.localtime(top))
        return top, now, time.strftime("%H:00", time.localtime(top)) \
            + f"~{lt.tm_hour:02d}:{lt.tm_min:02d} 진행중"

    def hour_stats(t_from, t_to):
        """구간 집계. 반환 dict: ALL/LONG/SHORT -> [청산건수, 승, 순익, 진입건수]

        청산과 진입을 나눠서 센다. 청산만 세면 진입해서 아직 들고 있는 거래가
        통째로 빠져 '거래 0건'으로 보인다.
        """
        cut = time.time() - 7500        # 직전 정각 구간까지 보려면 2시간분을 남긴다
        while recent and recent[0][0] < cut:
            recent.pop(0)
        while entries and entries[0][0] < cut:
            entries.pop(0)
        agg = {"ALL": [0, 0, 0.0, 0], "LONG": [0, 0, 0.0, 0], "SHORT": [0, 0, 0.0, 0]}
        for _t, sd, net, _nom in recent:
            if not (t_from <= _t < t_to):
                continue
            for k in ("ALL", sd):
                agg[k][0] += 1
                agg[k][1] += 1 if net > 0 else 0
                agg[k][2] += net
        for _t, sd in entries:
            if not (t_from <= _t < t_to):
                continue
            for k in ("ALL", sd):
                agg[k][3] += 1
        return agg

    def _line(tag, v):
        wr = (v[1] / v[0] * 100) if v[0] else 0.0
        return (f"{tag} 진입{v[3]}건 청산{v[0]}건 "
                f"승률{wr:.0f}% 순익{v[2]:+.4f}")

    def _perf_line(tag: str, n: int, w: int, net: float) -> str:
        wr = (w / n * 100) if n else 0.0
        return f"{tag} {n}건 승률{wr:.1f}% 순익{net:+.4f}"

    def _rows_between(t_from: float, t_to: float) -> list[dict]:
        rows = []
        if not LEDGER.exists():
            return rows
        for ln in LEDGER.read_text(encoding="utf-8").splitlines():
            if not ln.strip():
                continue
            try:
                r = json.loads(ln)
            except Exception:
                continue
            if r.get("dry_run"):
                continue
            xt = float(r.get("exited_at", 0.0) or 0.0)
            if t_from <= xt < t_to:
                rows.append(r)
        return rows

    def _ledger_rebuild_count() -> int:
        if not LEDGER.exists():
            return 0
        n = 0
        for ln in LEDGER.read_text(encoding="utf-8").splitlines():
            if not ln.strip():
                continue
            try:
                r = json.loads(ln)
            except Exception:
                continue
            if r.get("dry_run"):
                continue
            if r.get("reconstructed"):
                n += 1
        return n

    def ledger_vs_exchange(since: float):
        """원장 합계와 거래소 수입내역을 대조한다.

        [2026-08-21] 오늘 손실 19건이 원장에서 빠져 승률이 14%p 부풀었는데
        몇 시간 뒤에야 발견했다. 브리핑마다 대조하면 몇 분 안에 잡힌다.
        반환: (원장합, 거래소합, 차이) / 조회 실패면 None
        """
        if args.dry_run:
            return None
        try:
            inc = ex.client.futures_income_history(
                startTime=int(since * 1000), limit=1000)
        except Exception:
            return None
        exch = sum(float(x.get("income", 0) or 0) for x in inc
                   if x.get("incomeType") in ("REALIZED_PNL", "COMMISSION"))
        led = 0.0
        try:
            for ln in LEDGER.read_text(encoding="utf-8").splitlines():
                if not ln.strip():
                    continue
                r = json.loads(ln)
                if r.get("dry_run") or r.get("exited_at", 0) < since:
                    continue
                led += float(r.get("real_net", 0) or 0)
        except Exception:
            return None
        return led, exch, exch - led

    def brief_text(bal):
        _f, _t, _lbl = hour_window()
        a = hour_stats(_f, _t)
        wr = (stats["win"] / n_exit * 100) if n_exit else 0.0
        per = (stats["net"] / max(stats["nom"], 1e-9) * 100)
        sd = " / ".join(
            f"{k} {v[0]}건 승률{(v[1] / v[0] * 100 if v[0] else 0):.0f}% 순익{v[2]:+.4f}"
            for k, v in side_stat.items() if v[0])
        lines = [
            f"브리핑 {time.strftime('%H:%M')}" + (" [일시정지]" if paused else ""),
            f"잔고 {bal:.4f} (기준{bal0:.2f} 대비 {bal - bal0:+.4f} / "
            f"{(bal / bal0 - 1) * 100:+.2f}%)",
            f"── {_lbl} ──",
            "  " + _line("전체", a["ALL"]),
            "  " + _line("롱", a["LONG"]),
            "  " + _line("숏", a["SHORT"]),
            f"  보유중 {len(positions)}건"
            + (f" ({', '.join(f'{k}{len(v.legs)}차' for k, v in positions.items())})"
               if positions else ""),
            "── 누적 ──",
            f"  진입{n_entry} 청산{n_exit} 승률{wr:.1f}% "
            f"순익{stats['net']:+.4f} 명목당{per:+.3f}%",
        ]
        if sd:
            lines.append("  " + sd)
        rebuilt_n = _ledger_rebuild_count()
        if rebuilt_n:
            lines.append(f"  주의: 누적에 재구성 원장 {rebuilt_n}건 포함")
        # [2026-08-21] 집계가 거래소와 어긋나면 나머지 숫자를 믿을 수 없다.
        # 오늘 손실 19건이 원장에서 빠져 승률이 14%p 부풀었는데 몇 시간 뒤에야
        # 발견했다. 브리핑마다 대조해 맨 위에 알린다.
        chk = ledger_vs_exchange(run_started_at)
        if chk is not None:
            _led, _exc, _gap = chk
            if abs(_gap) > 0.05:
                lines.insert(1, f"[경고] 원장과 거래소 불일치 {_gap:+.4f} "
                                f"(원장{_led:+.4f} 거래소{_exc:+.4f}) - 집계를 믿지 말 것")
            else:
                lines.append(f"  정합성 OK (거래소 대비 {_gap:+.4f})")
        return "\n".join(lines)

    def config_diag_text() -> str:
        """[2026-08-21 사용자요청] 정각마다 '지금 설정이 맞는지'를 판정한다.

        묻는 것 네 가지:
          1. 수수료 제외 순익이 +인가       -> 유일한 합격 기준
          2. 익절 ROE 하한 선택이 맞는가    -> 하한에 걸린 건 vs 볼밴에 걸린 건
          3. EMA/깊이 선택이 맞는가         -> 진입 직후 최대불리 ROE 분포
          4. 3분봉 + 마켓 진입이 맞는가     -> 보유시간과 시간당 거래수

        원장 전체가 아니라 **이번 기동 이후**만 본다. 설정이 다른 구간이 섞이면
        판단이 오염된다(§30-5 참조).
        """
        import math as _math
        import statistics as _st
        rows = _rows_between(run_started_at, time.time())
        n = len(rows)
        el_h = max((time.time() - run_started_at) / 3600.0, 1e-9)
        head = f"── 설정 점검 (기동 후 {el_h:.1f}시간 / {n}건) ──"
        if n < 5:
            return (head + chr(10) +
                    f"  표본 {n}건 — 최소 5건은 쌓여야 판단 가능")

        nets = [float(r.get("real_net", 0) or 0) for r in rows]
        gross = sum(float(r.get("real_realized_pnl", 0) or 0) for r in rows)
        fees = sum(float(r.get("real_commission", 0) or 0) for r in rows)
        nom = sum(float(r.get("nominal", 0) or 0) for r in rows) or 1e-9
        pre = gross / nom * 100
        fee_pct = fees / nom * 100
        mu = _st.mean(nets)
        se = (_st.stdev(nets) / _math.sqrt(n)) if n > 1 else 0.0

        out = [head]
        # 1) 합격선
        verdict = "합격" if pre > fee_pct else "미달"
        out.append(f"  [1] 수수료전 엣지 {pre:+.4f}% vs 수수료 {fee_pct:.4f}%"
                   f" -> {verdict} ({pre - fee_pct:+.4f}%p)")
        if se > 0:
            lo, hi = mu - 1.96 * se, mu + 1.96 * se
            sig = "0 포함(판단보류)" if lo * hi < 0 else ("유의 플러스" if lo > 0 else "유의 마이너스")
            out.append(f"      거래당 {mu:+.4f} t={mu / se:+.2f} "
                       f"[{lo:+.4f}~{hi:+.4f}] {sig}")
        # 심볼 분해 — 1등을 빼도 플러스인가
        bysym = {}
        for r in rows:
            bysym.setdefault(r.get("symbol", "?"), []).append(float(r.get("real_net", 0) or 0))
        if len(bysym) > 1:
            tot = sum(nets)
            top_s = max(bysym, key=lambda k: sum(bysym[k]))
            wo = tot - sum(bysym[top_s])
            out.append(f"      1등 {top_s} 제외시 {wo:+.4f} "
                       f"({'유지' if wo > 0 else '뒤집힘'}) / 전체 {tot:+.4f}")

        # 2) 익절 하한
        tp = [r for r in rows if float(r.get("roe_pct", 0) or 0) > 0]
        sl = [r for r in rows if float(r.get("roe_pct", 0) or 0) <= 0]
        if tp:
            roes = sorted(float(r.get("roe_pct", 0) or 0) for r in tp)
            med = _st.median(roes)
            near = sum(1 for x in roes if abs(x - args.tp_floor_roe_pct) < 0.5)
            out.append(f"  [2] 익절 {len(tp)}건 ROE중앙 {med:+.2f}% "
                       f"(하한 {args.tp_floor_roe_pct:.1f}%) "
                       f"하한부근 {near}건")
            if args.tp_floor_roe_pct > 0 and med < args.tp_floor_roe_pct * 0.9:
                out.append("      주의: 익절 중앙이 하한보다 낮다 - 하한이 안 먹고 있다")
        if sl:
            out.append(f"      손절 {len(sl)}건 ROE중앙 "
                       f"{_st.median([float(r.get('roe_pct', 0) or 0) for r in sl]):+.2f}% "
                       f"(설정 -{args.stop_widen_pct * args.leverage:.1f}%)")
            if tp:
                aw = _st.mean([float(r.get("real_net", 0) or 0) for r in tp])
                al = _st.mean([float(r.get("real_net", 0) or 0) for r in sl])
                be = abs(al) / (aw + abs(al)) * 100 if (aw + abs(al)) else 0
                out.append(f"      손익비 1:{abs(aw / al):.2f} "
                           f"손익분기승률 {be:.1f}% vs 실제 {len(tp) / n * 100:.1f}%")

        # 3) 진입선(EMA/깊이) 적절성
        adv = [float(r.get("max_adverse_roe", 0) or 0) for r in rows]
        fav = [float(r.get("max_favorable_roe", 0) or 0) for r in rows]
        zero_fav = sum(1 for x in fav if x <= 0.001)
        out.append(f"  [3] 진입선 깊이 {args.entry_depth_pct:.1f}% / EMA5 기준")
        out.append(f"      진입후 최대불리 중앙 {_st.median(adv):+.2f}% / "
                   f"한번도 유리한적 없음 {zero_fav}건 ({zero_fav / n * 100:.0f}%)")
        if zero_fav / n > 0.45:
            out.append("      주의: 절반 가까이가 진입 즉시 역행 - 더 깊게(깊이↑) 검토")

        # 4) 3분봉 + 마켓 진입
        holds = sorted((float(r.get("exited_at", 0) or 0)
                        - float(r.get("entered_at", 0) or 0)) / 60.0 for r in rows)
        rt = fee_pct / 2
        maker_gain = (rt - 0.02) * 2
        out.append(f"  [4] 신호봉 {args.signal_tf_min}분 / 마켓진입")
        out.append(f"      보유중앙 {_st.median(holds):.0f}분 "
                   f"(목표 15~20분) / 시간당 {n / el_h:.1f}건")
        out.append(f"      메이커 전환시 절감 여지 {maker_gain:.4f}%p "
                   f"(현재 격차 {pre - fee_pct:+.4f}%p)")
        return chr(10).join(out)

    def hourly_perf_text(bal: float) -> str:
        now = time.time()
        lt = time.localtime(now)
        top = now - lt.tm_min * 60 - lt.tm_sec
        t_from, t_to = top - 3600, top
        rows = _rows_between(t_from, t_to)
        recent_perf = {
            "ALL": [0, 0, 0.0],
            "LONG": [0, 0, 0.0],
            "SHORT": [0, 0, 0.0],
        }
        for r in rows:
            side = r.get("side", "?")
            net = float(r.get("real_net", 0.0) or 0.0)
            for k in ("ALL", side):
                if k not in recent_perf:
                    continue
                recent_perf[k][0] += 1
                recent_perf[k][1] += 1 if net > 0 else 0
                recent_perf[k][2] += net

        loss = [r for r in rows if float(r.get("real_net", 0.0) or 0.0) < 0]
        by_reason: dict[str, list] = {}
        by_leg: dict[int, list] = {}
        by_symbol: dict[str, list] = {}
        for r in loss:
            by_reason.setdefault(r.get("exit_reason", "?"), []).append(r)
            by_leg.setdefault(int(r.get("legs", 1) or 1), []).append(r)
            by_symbol.setdefault(r.get("symbol", "?"), []).append(r)
        worst_reason = sorted(
            by_reason.items(),
            key=lambda kv: sum(float(x.get("real_net", 0.0) or 0.0) for x in kv[1])
        )
        worst_leg = sorted(
            by_leg.items(),
            key=lambda kv: sum(float(x.get("real_net", 0.0) or 0.0) for x in kv[1])
        )
        worst_symbol = sorted(
            by_symbol.items(),
            key=lambda kv: sum(float(x.get("real_net", 0.0) or 0.0) for x in kv[1])
        )
        add2 = [r for r in rows if int(r.get("legs", 1) or 1) >= 2]
        add3 = [r for r in rows if int(r.get("legs", 1) or 1) >= 3]
        review = []
        if loss:
            review.append(f"손실 {len(loss)}건")
            if worst_reason:
                _k, _v = worst_reason[0]
                review.append(f"사유 {_k} {len(_v)}건 {sum(float(x.get('real_net', 0.0) or 0.0) for x in _v):+.4f}")
            if worst_leg:
                _k, _v = worst_leg[0]
                review.append(f"{_k}차 {len(_v)}건 {sum(float(x.get('real_net', 0.0) or 0.0) for x in _v):+.4f}")
            if worst_symbol:
                _k, _v = worst_symbol[0]
                review.append(f"{_k} {len(_v)}건 {sum(float(x.get('real_net', 0.0) or 0.0) for x in _v):+.4f}")
        else:
            review.append("손실거래 없음")

        cum_long = side_stat.get("LONG", [0, 0, 0.0])
        cum_short = side_stat.get("SHORT", [0, 0, 0.0])
        lines = [
            f"정각브리핑 {time.strftime('%H:00', time.localtime(t_to))}",
            f"잔고 {bal:.4f} / 기준{bal0:.2f} 대비 {bal - bal0:+.4f}",
            "누적",
            "  " + _perf_line("전체", n_exit, stats["win"], stats["net"]),
            "  " + _perf_line("롱", cum_long[0], cum_long[1], cum_long[2]),
            "  " + _perf_line("숏", cum_short[0], cum_short[1], cum_short[2]),
            f"최근1시간 {time.strftime('%H:00', time.localtime(t_from))}~{time.strftime('%H:00', time.localtime(t_to))}",
            "  " + _perf_line("전체", recent_perf["ALL"][0], recent_perf["ALL"][1], recent_perf["ALL"][2]),
            "  " + _perf_line("롱", recent_perf["LONG"][0], recent_perf["LONG"][1], recent_perf["LONG"][2]),
            "  " + _perf_line("숏", recent_perf["SHORT"][0], recent_perf["SHORT"][1], recent_perf["SHORT"][2]),
            "  "
            + f"추가진입 2차이상 {len(add2)}건 "
            + f"순익{sum(float(r.get('real_net', 0.0) or 0.0) for r in add2):+.4f} / "
            + f"3차 {len(add3)}건 "
            + f"순익{sum(float(r.get('real_net', 0.0) or 0.0) for r in add3):+.4f}",
            "복기 " + " / ".join(review),
        ]
        return "\n".join(lines)

    def review_text(hours: float = 6.0):
        """손절 건이 어디서 새는지 본다.
        max_favorable_roe(보유 중 최고 ROE)로 손절을 세 종류로 가른다.
          방향오류 = 진입하자마자 반대로 갔다 (고점 ROE 가 낮다)
          반락     = 익절 근처까지 갔다가 되돌려 손절됐다 (익절 로직 문제)
          애매     = 그 사이
        고치는 곳이 다르다. 방향오류가 많으면 진입 조건을, 반락이 많으면 익절을 본다."""
        if not LEDGER.exists():
            return "원장이 없습니다"
        cut = time.time() - hours * 3600
        rs = []
        for ln in LEDGER.read_text(encoding="utf-8").splitlines():
            if not ln.strip():
                continue
            try:
                r = json.loads(ln)
            except Exception:
                continue
            if r.get("dry_run") or r.get("reconstructed") or r.get("exited_at", 0) < cut:
                continue
            rs.append(r)
        if not rs:
            return f"최근 {hours:.0f}시간 청산 없음"

        loss = [r for r in rs if r.get("real_net", 0) < 0]
        L = [f"복기 최근 {hours:.0f}시간 / 청산 {len(rs)}건 중 손실 {len(loss)}건"]

        if loss:
            grp = {"방향오류": [], "애매": [], "반락": []}
            for r in loss:
                mf = r.get("max_favorable_roe") or 0.0
                k = "방향오류" if mf < 1.0 else ("반락" if mf > 3.0 else "애매")
                grp[k].append(r)
            L.append("── 손실 유형 ──")
            for k, v in grp.items():
                if not v:
                    continue
                mfa = sum((x.get("max_favorable_roe") or 0.0) for x in v) / len(v)
                L.append(f"  {k} {len(v)}건 평균{sum(x['real_net'] for x in v) / len(v):+.4f}"
                         f" 고점ROE평균{mfa:+.2f}%")
            if grp["반락"]:
                L.append("   반락=익절선 근처까지 갔다가 놓친 건. 익절 조건을 본다")
            if grp["방향오류"]:
                L.append("   방향오류=진입 직후 역행. 진입 조건을 본다")

        # 심볼별 순익 (손실 상위)
        sym = {}
        for r in rs:
            a = sym.setdefault(r["symbol"], [0, 0, 0.0])
            a[0] += 1
            a[1] += 1 if r.get("real_net", 0) > 0 else 0
            a[2] += r.get("real_net", 0)
        bad = sorted(sym.items(), key=lambda x: x[1][2])[:4]
        if bad and bad[0][1][2] < 0:
            L.append("── 손실 상위 심볼 ──")
            for k, v in bad:
                if v[2] >= 0:
                    break
                L.append(f"  {k} {v[0]}건 승률{v[1] / v[0] * 100:.0f}% {v[2]:+.4f}")

        # 차수별 — 3분할이 실제로 도움이 되는지
        leg = {}
        for r in rs:
            a = leg.setdefault(r.get("legs", 1), [0, 0, 0.0])
            a[0] += 1
            a[1] += 1 if r.get("real_net", 0) > 0 else 0
            a[2] += r.get("real_net", 0)
        L.append("── 진입 차수별 ──")
        for k in sorted(leg):
            v = leg[k]
            L.append(f"  {k}차 {v[0]}건 승률{v[1] / v[0] * 100:.0f}% "
                     f"건당{v[2] / v[0]:+.4f} 합{v[2]:+.4f}")

        # 청산사유별
        wy = {}
        for r in rs:
            a = wy.setdefault(r.get("exit_reason", "?"), [0, 0.0, 0.0])
            a[0] += 1
            a[1] += r.get("real_net", 0)
            a[2] += r.get("roe_pct", 0)
        L.append("── 청산사유 ──")
        for k, v in sorted(wy.items(), key=lambda x: -x[1][0]):
            L.append(f"  {k} {v[0]}건 ROE평균{v[2] / v[0]:+.2f}% 합{v[1]:+.4f}")
        return "\n".join(L)

    def pos_text():
        if not positions:
            return "보유 포지션 없음"
        rows = []
        for sm, ps in positions.items():
            try:
                mk = ex.get_mark_price(sm)
                roe = ((mk / ps.entry - 1) if ps.side == "LONG"
                       else (1 - mk / ps.entry)) * 100 * ps.leverage
            except Exception:
                roe = float("nan")
            extra = " [보호주문없음]" if sm in unprotected else ""
            rows.append(f"{sm} {ps.side} {len(ps.legs)}차 평단{ps.entry:.6f} ROE{roe:+.2f}%{extra}")
        return "\n".join(rows)

    def retry_unprotected_stops(now_ts: float) -> None:
        if args.dry_run:
            return
        for sm in list(unprotected):
            meta = unprotected.get(sm) or {}
            pos = positions.get(sm)
            if pos is None:
                unprotected.pop(sm, None)
                continue
            if now_ts < float(meta.get("next_retry_at", 0.0) or 0.0):
                continue
            algo = sync_stop(ex, sm, pos.side, pos.qty, pos.stop_price,
                             pos.stop_algo_id, args.dry_run, say)
            if algo:
                pos.stop_algo_id = algo
                unprotected.pop(sm, None)
                save_state()
                say(f"{sm} 거래소 손절 재등록 성공 algoId={algo}")
                continue
            # [2026-08-21] 무한 재시도는 손절 없는 포지션을 방치하는 것과 같다.
            # 실사고: BTWUSDT 가 손절 없이 ROE -38.79% 까지 갔다.
            # 상한을 넘기면 보호를 포기하고 시장가로 정리한다.
            since = float(meta.get("since", 0.0) or 0.0) or now_ts
            meta["since"] = since
            if args.max_unprotected_sec > 0 and \
                    now_ts - since >= args.max_unprotected_sec:
                say(f"경고 {sm} 손절 등록이 {now_ts - since:.0f}초째 실패 - "
                    f"보호 불가로 판단해 시장가 청산합니다")
                try:
                    ex.close_market_position(sm, pos.side, abs(pos.qty))
                    time.sleep(0.5)
                    record_external_close(sm)
                except Exception as e:
                    say(f"{sm} 보호불가 청산 실패: {e}")
                unprotected.pop(sm, None)
                continue
            meta["next_retry_at"] = now_ts + 15.0
            if now_ts - float(meta.get("last_warn_at", 0.0) or 0.0) >= 60.0:
                meta["last_warn_at"] = now_ts
                say(f"경고 {sm} 보호주문 미등록 지속 - 봇 폴링 손절만 남음")
            unprotected[sm] = meta

    def flat_all():
        n = 0
        for sm in list(positions):
            ps = positions[sm]
            if args.dry_run:
                positions.pop(sm, None)
                n += 1
                continue
            if ps.stop_algo_id:
                try:
                    ex.cancel_order(sm, ps.stop_algo_id)
                except Exception:
                    pass
            try:
                ex.close_market_position(sm, ps.side, abs(ps.qty))
                # [2026-08-21] 수동 전량청산도 원장에 남겨야 한다.
                # 그냥 지우면 이 거래가 성적 집계에서 통째로 빠진다.
                time.sleep(0.5)
                record_external_close(sm)
                n += 1
            except Exception as e:
                say(f"수동청산 실패 {sm}: {e}")
        save_state()
        return n

    def handle_buttons():
        """텔레그램 조작 버튼 처리."""
        nonlocal paused
        if not tg or not args.buttons:
            return
        for cq, act in tg.poll():
            try:
                if act == "status":
                    tg.answer(cq, "상태 전송")
                    say(f"보유{len(positions)} 대기{len(pending)} "
                        + (f"보호미등록{len(unprotected)} " if unprotected else "")
                        + ("일시정지 중" if paused else "가동 중"))
                elif act == "brief":
                    tg.answer(cq, "브리핑 전송")
                    say(brief_text(ex.get_total_margin_balance()))
                elif act == "pos":
                    tg.answer(cq, "포지션 전송")
                    say(pos_text())
                elif act == "review":
                    tg.answer(cq, "복기 전송")
                    say(review_text())
                elif act == "pause":
                    paused = True
                    tg.answer(cq, "신규 진입 중단")
                    say("일시정지 - 신규 진입만 멈춥니다. 보유분 손절/익절은 계속 관리합니다")
                elif act == "resume":
                    paused = False
                    tg.answer(cq, "재개")
                    say("재개 - 신규 진입을 다시 받습니다")
                elif act == "flat":
                    tg.answer(cq, "전량청산 실행")
                    say(f"전량청산 {flat_all()}건 실행")
            except Exception as e:
                say(f"버튼 처리 오류({act}): {e}")

    if tg and args.buttons:
        tg.poll()          # 재시작 전에 눌려 있던 묵은 입력은 버린다
        tg.menu()
    atexit.register(release_bot_lock)

    def reset_ws_warmup(now_ts: float) -> None:
        nonlocal ws_ready, ws_ready_count, ws_ready_deadline, ws_next_check_at, ws_bad_since
        ws_ready = False
        ws_ready_count = 0
        ws_ready_deadline = now_ts + 100.0
        ws_next_check_at = now_ts
        ws_bad_since = 0.0

    def restart_ws(reason: str) -> None:
        nonlocal ws_proc, ws_cache, ws_last_restart_at, ws_restart_count
        now_ts = time.time()
        say(f"경고: WS 워커 재기동 - {reason}")
        ex.set_ws_kline_cache(None)
        stop_ws(ws_proc)
        time.sleep(1.0)
        ws_proc, ws_cache = start_ws(symbols)
        reset_ws_warmup(now_ts)
        ws_last_restart_at = now_ts
        ws_restart_count += 1
        say("WS 워커 재기동 완료 - 준비 전에는 보유 포지션 관리만 하고 신규/추가 진입은 막습니다")

    def refresh_symbol_universe(now_ts: float) -> None:
        nonlocal symbols, symbol_refresh_next_at, ws_proc, ws_cache, ws_last_restart_at, ws_restart_count
        if not cfg.auto_symbols or args.symbol_refresh_sec <= 0 or now_ts < symbol_refresh_next_at:
            return
        symbol_refresh_next_at = now_ts + args.symbol_refresh_sec
        try:
            top = ex.get_active_usdt_perpetual_symbols(limit=args.symbols)
        except Exception as e:
            say(f"경고 거래량 상위 심볼 갱신 실패({e})")
            return
        fresh = merge_symbol_universe(top, positions.keys(), args.symbols)
        if fresh == symbols:
            return
        old = set(symbols)
        new = set(fresh)
        added = sorted(new - old)
        removed = sorted(old - new)
        symbols = fresh
        for sm in list(pending):
            if sm not in new and sm not in positions:
                pending.pop(sm, None)
        say(f"거래량 상위 심볼 갱신 {len(symbols)}개"
            + (f" / 추가 {', '.join(added[:5])}" if added else "")
            + (f" / 제외 {', '.join(removed[:5])}" if removed else ""))
        if args.ws:
            ex.set_ws_kline_cache(None)
            stop_ws(ws_proc)
            time.sleep(1.0)
            ws_proc, ws_cache = start_ws(symbols)
            reset_ws_warmup(now_ts)
            ws_last_restart_at = now_ts
            ws_restart_count += 1
            say("심볼 갱신 반영 - WS 워커 재기동, 준비 전에는 보유 포지션 관리만 수행")

    def reconcile_live_positions(now_ts: float) -> None:
        nonlocal reconcile_next_at
        if args.dry_run or now_ts < reconcile_next_at:
            return
        reconcile_next_at = now_ts + 15.0
        try:
            live = ex.client.futures_account()["positions"]
        except Exception:
            return
        live_nonzero = {
            p["symbol"] for p in live
            if abs(float(p.get("positionAmt", 0) or 0)) > 0
        }
        for sm in list(positions):
            if sm in live_nonzero:
                continue
            # [2026-08-21 P0] 여기서 그냥 지우면 손실이 원장에 남지 않는다.
            # 반드시 record_external_close 를 거쳐 체결 이력으로 손익을 남긴다.
            record_external_close(sm)

    def record_external_close(sym):
        """봇이 모르는 사이에 닫힌 포지션을 원장에 기록하고 정리한다.

        거래소 손절주문 발동, 수동 청산, 강제 청산이 여기 해당한다.
        [2026-08-21 P0] 이걸 거치지 않고 positions 에서 지우기만 하면 손실이
        원장에 전혀 남지 않는다. 원장에는 봇이 직접 청산한 익절만 쌓여
        성적이 낙관 쪽으로 왜곡된다(실측 90분 구간 원장 +0.17 vs 거래소 -2.76).
        """
        nonlocal n_exit
        pos = positions.get(sym)
        if pos is None:
            return
        if pos.stop_algo_id:
            try:
                ex.cancel_order(sym, pos.stop_algo_id)
            except Exception:
                pass
        comm = rz = 0.0
        fill = None
        try:
            tr = ex.client.futures_account_trades(
                symbol=sym, startTime=int(pos.entered_at * 1000) - 5000)
            if pos.since_trade_id:
                tr = [t for t in tr if int(t.get("id", 0)) > pos.since_trade_id]
            comm = sum(float(t.get("commission", 0)) for t in tr)
            rz = sum(float(t.get("realizedPnl", 0)) for t in tr)
            fill = realized_fill_snapshot(tr, pos.side, pos.entry, pos.entry,
                                          pos.qty, pos.leverage)
        except Exception as e:
            say(f"경고 {sym} 외부청산 체결조회 실패({e}) - 손익 0으로 기록")
        if fill is None:
            fill = {"entry_price": pos.entry, "exit_price": pos.entry,
                    "quantity": pos.qty, "nominal": pos.entry * pos.qty,
                    "roe_pct": 0.0}
        net = rz - comm
        nominal = fill["nominal"]
        n_exit += 1
        stats["win"] += 1 if net > 0 else 0
        stats["net"] += net
        stats["nom"] += nominal
        append_ledger(dict(version=VERSION, symbol=sym, side=pos.side,
                           entry_price=fill["entry_price"],
                           exit_price=fill["exit_price"],
                           quantity=fill["quantity"],
                           exit_reason="STOP_EXCHANGE", entered_at=pos.entered_at,
                           exited_at=time.time(), leverage=pos.leverage,
                           roe_pct=fill["roe_pct"], nominal=nominal,
                           legs=len(pos.legs),
                           real_commission=comm, real_realized_pnl=rz, real_net=net,
                           max_adverse_roe=pos.max_adverse_roe,
                           max_favorable_roe=pos.max_favorable_roe,
                           adopted=pos.adopted, external_close=True,
                           origin=f"scalp_bot_{VERSION}", dry_run=False))
        ss = side_stat[pos.side]
        ss[0] += 1
        ss[1] += 1 if net > 0 else 0
        ss[2] += net
        recent.append((time.time(), pos.side, net, nominal))
        ws_ = why_stat.setdefault("STOP_EXCHANGE", [0, 0.0])
        ws_[0] += 1
        ws_[1] += net
        positions.pop(sym, None)
        pending.pop(sym, None)
        unprotected.pop(sym, None)
        cooldown[sym] = time.time() + args.cooldown_sec
        save_state()
        wr = stats["win"] / n_exit * 100 if n_exit else 0.0
        say(f"외부청산 기록 {sym} {pos.side} ROE{fill['roe_pct']:+.2f}% "
            f"순손익{net:+.4f}"
            f" | 누적 {n_exit}건 승률{wr:.1f}% 손익{stats['net']:+.4f}")

    def reconcile_closed(bal):
        """사이클 시작 시 백스톱. 15초 감시가 놓친 것을 마지막으로 잡는다."""
        if args.dry_run or not positions:
            return
        try:
            live = {p["symbol"] for p in ex.client.futures_account()["positions"]
                    if abs(float(p.get("positionAmt", 0) or 0)) > 0}
        except Exception:
            return
        for sym in [k for k in positions if k not in live]:
            record_external_close(sym)

    while time.time() < deadline:
        try:
            handle_buttons()
            retry_unprotected_stops(time.time())
            reconcile_live_positions(time.time())
            refresh_symbol_universe(time.time())
            if args.ws and ws_cache is not None:
                _health = ws_cache.health() or {}
                _now = time.time()
                _last_msg = float(_health.get("last_market_message_ts", 0.0) or 0.0)
                _msg_60s = int(_health.get("message_count_60s", 0) or 0)
                _err_60s = int(_health.get("error_count_60s", 0) or 0)
                _consec = int(_health.get("consecutive_read_loop_errors", 0) or 0)
                _stale = _last_msg > 0 and (_now - _last_msg) > max(20.0, cfg.ws_kline_max_staleness_sec)
                _bad = _stale or _consec >= 50 or _err_60s >= 500 or (_msg_60s == 0 and _last_msg > 0 and (_now - _last_msg) > 10.0)
                if _bad:
                    if ws_bad_since <= 0:
                        ws_bad_since = _now
                    elif (_now - ws_bad_since) >= 8.0 and (_now - ws_last_restart_at) >= 30.0:
                        restart_ws(f"health bad msg60={_msg_60s} err60={_err_60s} consec={_consec}")
                else:
                    ws_bad_since = 0.0
            if args.ws and not ws_ready and ws_cache is not None and time.time() >= ws_next_check_at:
                ws_next_check_at = time.time() + 2.0
                canary = symbols[:10]
                ready = 0
                for _sym in canary:
                    try:
                        if (ws_cache.has_sufficient_history(_sym, 30)
                                and ws_cache.is_fresh(_sym, cfg.ws_kline_max_staleness_sec)):
                            ready += 1
                    except Exception:
                        pass
                ws_ready_count = ready
                if ready >= ws_ready_need:
                    ws_ready = True
                    ex.set_ws_kline_cache(ws_cache)
                    say(f"WS 준비 완료 {ready}/{len(canary)} - 실시간 스캔/신규 진입 재개")
                elif time.time() >= ws_ready_deadline:
                    ws_ready = True
                    ex.set_ws_kline_cache(ws_cache)
                    say(f"경고: WS 준비 미완료 {ready}/{len(canary)} - "
                        "최대 대기 100초 도달, 강제 진행")
            bal = ex.get_total_margin_balance()
            # [2026-08-21 P0] 거래소 손절주문(STOP_MARKET)이 발동해 포지션이 닫히면
            # 봇은 그 사실을 모르고 원장에 아무것도 남기지 않았다. 그 결과 원장에는
            # 봇이 직접 청산한 익절(BB/RR)만 쌓이고 손절은 통째로 빠졌다.
            # 실측: 90분 구간에서 원장 +0.1691 vs 거래소 -2.7594 (차이 -2.93).
            # 승률·순익·브리핑이 전부 이 원장을 읽으므로 성적이 낙관 쪽으로 왜곡됐다.
            reconcile_closed(bal)
            # [2026-08-20] 3분할 진입은 1차당 명목도 최소명목(5.0)을 넘어야 한다.
            # 슬롯8이면 1차 명목이 3.63으로 미달해 주문이 안 나간다.
            # 1차 증거금 = 잔고 x (노출/슬롯) / 차수  ->  x레버리지 >= 5.0x1.12
            need_per_leg = max(args.min_notional / args.leverage * 1.12,
                               args.min_leg_margin) * args.tranches
            slots = max(1, min(int(bal * args.max_exposure // need_per_leg),
                               args.max_concurrency))
            size = args.max_exposure / slots

            # 청산 판정
            for sym in list(positions):
                pos = positions[sym]
                try:
                    mark = ex.get_mark_price(sym)
                except Exception:
                    continue
                L = pos.side == "LONG"
                roe = ((mark / pos.entry - 1) if L else (1 - mark / pos.entry)) * pos.leverage * 100
                pos.max_adverse_roe = min(pos.max_adverse_roe, roe)
                pos.max_favorable_roe = max(pos.max_favorable_roe, roe)
                now_ts = time.time()
                why = early_cut_reason(
                    pos, roe, now_ts,
                    args.early_adverse_sec,
                    args.early_adverse_roe,
                    args.early_adverse_min_favorable_roe,
                    args.mae_cut_roe,
                    args.mae_cut_grace_sec,
                    args.mae_cut_min_favorable_roe,
                )
                if not why:
                    if (mark <= pos.stop_price) if L else (mark >= pos.stop_price):
                        why = "STOP_EMA25"
                    elif pos.tp_bb and ((mark >= pos.tp_bb) if L else (mark <= pos.tp_bb)):
                        why = "BB"
                    elif pos.tp_rr and ((mark >= pos.tp_rr) if L else (mark <= pos.tp_rr)):
                        why = "RR"
                    elif args.max_hold_sec > 0 and (now_ts - pos.entered_at) >= args.max_hold_sec:
                        why = "TIME_STOP"
                if not why:
                    continue
                nominal = pos.entry * pos.qty
                if not args.dry_run:
                    if pos.stop_algo_id:
                        try:
                            ex.cancel_order(sym, pos.stop_algo_id)
                        except Exception:
                            pass
                    try:
                        ex.close_market_position(sym, pos.side, abs(pos.qty))
                    except Exception as e:
                        say(f"청산실패 {sym}: {e}")
                        continue
                    time.sleep(1.0)
                    tr = ex.client.futures_account_trades(
                        symbol=sym, startTime=int(pos.entered_at * 1000) - 5000)
                    # [버그3] 진입 직전 체결 id 이후만 이 포지션 것으로 본다.
                    # 같은 심볼을 반복 거래하면 이전 포지션 체결이 섞여 순손익이 오염된다.
                    if pos.since_trade_id:
                        tr = [t for t in tr if int(t.get("id", 0)) > pos.since_trade_id]
                    comm = sum(float(t.get("commission", 0)) for t in tr)
                    rz = sum(float(t.get("realizedPnl", 0)) for t in tr)
                    fill = realized_fill_snapshot(
                        tr, pos.side, pos.entry, mark, pos.qty, pos.leverage)
                else:
                    comm = nominal * 0.000501 * 2
                    rz = nominal * (roe / 100 / pos.leverage)
                    fill = {
                        "entry_price": pos.entry,
                        "exit_price": mark,
                        "quantity": pos.qty,
                        "nominal": nominal,
                        "roe_pct": roe,
                    }
                net = rz - comm
                nominal = float(fill["nominal"])
                roe = float(fill["roe_pct"])
                n_exit += 1
                stats["win"] += 1 if net > 0 else 0
                stats["net"] += net
                stats["nom"] += nominal
                append_ledger(dict(version=VERSION, symbol=sym, side=pos.side,
                                   entry_price=fill["entry_price"],
                                   exit_price=fill["exit_price"],
                                   quantity=fill["quantity"],
                                   exit_reason=why, entered_at=pos.entered_at,
                                   exited_at=time.time(), leverage=pos.leverage,
                                   roe_pct=roe, nominal=nominal, legs=len(pos.legs),
                                   real_commission=comm, real_realized_pnl=rz, real_net=net,
                                   max_adverse_roe=pos.max_adverse_roe,
                                   max_favorable_roe=pos.max_favorable_roe,
                                   adopted=pos.adopted,
                                   origin=f"scalp_bot_{VERSION}", dry_run=args.dry_run))
                ss = side_stat[pos.side]
                ss[0] += 1
                ss[1] += 1 if net > 0 else 0
                ss[2] += net
                recent.append((time.time(), pos.side, net, nominal))
                ws_ = why_stat.setdefault(why, [0, 0.0])
                ws_[0] += 1
                ws_[1] += net
                positions.pop(sym, None)
                pending.pop(sym, None)
                unprotected.pop(sym, None)
                save_state()
                cooldown[sym] = time.time() + args.cooldown_sec
                wr = stats["win"] / n_exit * 100
                say(f"청산 {sym} {pos.side} {why} ROE{roe:+.2f}% 순손익{net:+.4f}"
                    f" | 누적 {n_exit}건 승률{wr:.1f}% 손익{stats['net']:+.4f}")

            # 신호 탐색
            for sym in (() if paused else symbols):
                if time.time() > deadline:
                    break
                if args.ws and not ws_ready:
                    continue
                if sym in positions:
                    # 2·3차 추가 진입 대상인지 확인
                    _pos = positions[sym]
                    _pd = pending.get(sym)
                    if not _pd or len(_pos.legs) >= args.tranches:
                        continue
                elif len(positions) >= slots:
                    continue
                if cooldown.get(sym, 0) > time.time():
                    skips["쿨다운"] += 1
                    continue
                try:
                    df = signal_bars(ex, sym, args.signal_tf_min)
                except Exception:
                    continue
                ind = indicators(df)
                if not ind:
                    continue
                L = ind["e5"] > ind["e10"] > ind["e15"] > ind["e25"]
                S = ind["e5"] < ind["e10"] < ind["e15"] < ind["e25"]
                if not (L or S):
                    pending.pop(sym, None)
                    continue
                side = "LONG" if L else "SHORT"
                if sym not in pending:
                    pending[sym] = {"side": side, "legs": [], "since": time.time()}
                    n_align += 1
                pd = pending[sym]
                if pd["side"] != side or time.time() - pd["since"] > 3600:
                    pending.pop(sym, None)
                    continue
                # 눌림 터치 확인
                lo = float(df["low"].iloc[-1])
                hi = float(df["high"].iloc[-1])
                targets = tranche_targets(
                    ind, side, args.tranches, args.tranche2_band,
                    args.tranche_min_gap_pct)
                # 진입 목표선을 더 깊게 민다(정배열 판정은 그대로 둔다).
                if args.entry_depth_pct > 0:
                    targets = [deepen_target(t, side, args.entry_depth_pct)
                               for t in targets]
                k = len(pd["legs"])
                if k < len(targets):
                    tgt = targets[k]
                    if (lo <= tgt) if L else (hi >= tgt):
                        pd["legs"].append(tgt)
                # 이번 주기에 새로 터치한 게 없으면 대기
                if len(pd["legs"]) == pd.get("done", 0):
                    continue
                entry = sum(pd["legs"]) / len(pd["legs"])
                stop = ind["e25"]
                risk = abs(entry - stop) / entry
                # 손절폭이 너무 좁으면 노이즈에 바로 털린다
                if risk * 100 < args.min_risk_pct:
                    skips["근접손절"] += 1
                    pending.pop(sym, None)
                    continue
                # 진입 시점에 이미 익절선을 넘었으면 들어갈 이유가 없다
                tp_bb0 = tp_with_floor(entry, padded_tp(entry, fee_aware_bb_price(
                    entry, ind["bb_u"] if L else ind["bb_l"], side,
                    args.roundtrip_fee_rate, args.min_net_tp_rate),
                    side, args.tp_extra_roe_pct, args.leverage),
                    side, args.tp_floor_roe_pct, args.leverage)
                if tp_bb0 and ((entry >= tp_bb0) if L else (entry <= tp_bb0)):
                    skips["익절선통과"] += 1
                    pending.pop(sym, None)
                    continue
                age = time.time() % 60
                if args.max_signal_age > 0 and age > args.max_signal_age:
                    skips["신호노후"] += 1
                    continue
                price = ex.get_mark_price(sym)
                live_risk = abs(price - stop) / price if price > 0 else 0.0
                if ((price <= stop) if L else (price >= stop)):
                    skips["손절선통과"] += 1
                    pending.pop(sym, None)
                    continue
                if live_risk * 100 < args.min_risk_pct:
                    skips["근접손절"] += 1
                    pending.pop(sym, None)
                    continue
                # 필터를 통과한 뒤에 손절폭을 넓힌다(진입 종목 수는 불변).
                stop = widened_stop(entry, stop, side, args.stop_widen_pct)
                if tp_bb0 and ((price >= tp_bb0) if L else (price <= tp_bb0)):
                    skips["익절선통과"] += 1
                    pending.pop(sym, None)
                    continue
                # [2026-08-20] 한 봉에서 EMA5·EMA10 을 동시에 터치하면 legs 가 두 개
                # 잡힌다. 그때 1/3 만 넣으면 그 차수의 자본이 통째로 누락된다.
                # 아직 집행하지 않은 차수만큼(_new_legs) 곱해서 넣는다.
                _new_legs = max(1, len(pd["legs"]) - pd.get("done", 0))
                margin = bal * size / args.tranches * _new_legs
                qty = ex.round_quantity(sym, margin * args.leverage / price,
                                        price=price, max_notional=margin * args.leverage)
                if not qty:
                    pending.pop(sym, None)
                    continue
                # [버그3] 이 포지션의 체결만 골라내기 위해 진입 직전 체결 id 를 기록한다.
                # [2026-08-20] 추가 진입(2·3차)에서도 반드시 잡아야 한다. 0으로 두면
                # entry_fill_after 가 필터를 건너뛰어 최근 50건을 전부 가중평균하는데,
                # 거기에 1차 체결이 섞여 '이번 차수 체결가'가 아니라 '누적 평단'이 된다.
                # 그 값을 legs 에 또 붙이므로 1차가 중복 반영된다.
                _since_id = 0
                if not args.dry_run:
                    _since_id = last_trade_id(ex, sym)
                    try:
                        ex.set_margin_type(sym, "ISOLATED")
                    except Exception:
                        pass
                    try:
                        ex.set_leverage(sym, args.leverage)
                        ex.open_market_position(sym, side, qty)
                    except Exception as e:
                        say(f"진입실패 {sym}: {e}")
                        pending.pop(sym, None)
                        continue
                    entry_fill, filled_qty = entry_fill_after(
                        ex, sym, side, _since_id, price, qty)
                else:
                    entry_fill, filled_qty = price, qty
                # [2026-08-20 버그1] 손절주문은 아래 분기에서 sync_stop 으로만 건다.
                # 여기서 미리 걸면 추가 진입 분기가 그것을 취소하지 않아 고아 주문이 남는다.
                _ent_t = time.time()
                if sym in positions:
                    # 2·3차: 기존 포지션에 합산하고 손절/익절선을 평단 기준으로 갱신
                    _p = positions[sym]
                    _p.legs = list(_p.legs) + [entry_fill] * _new_legs
                    pd["legs"] = list(_p.legs)
                    # 내부 누적은 한 번만 어긋나도 영구히 틀어진다. 거래소를 정본으로.
                    _p.qty = live_position_qty(ex, sym, _p.qty + (filled_qty or qty))
                    _p.stop_price = stop
                    _p.tp_bb = tp_with_floor(_p.entry, padded_tp(
                        _p.entry, fee_aware_bb_price(
                            _p.entry, ind["bb_u"] if L else ind["bb_l"], side,
                            args.roundtrip_fee_rate, args.min_net_tp_rate),
                        side, args.tp_extra_roe_pct, args.leverage),
                        side, args.tp_floor_roe_pct, args.leverage)
                    _p.tp_rr = fee_aware_rr_price(
                        _p.entry, stop, side, args.rr, args.roundtrip_fee_rate)
                    _p.stop_algo_id = sync_stop(ex, sym, side, _p.qty, stop,
                                                _p.stop_algo_id, args.dry_run, say)
                    if _p.stop_algo_id:
                        unprotected.pop(sym, None)
                    else:
                        unprotected[sym] = {"next_retry_at": time.time() + 15.0,
                                            "last_warn_at": time.time()}
                    pd["done"] = len(pd["legs"])
                    entries.append((_ent_t, side))
                    save_state()
                    n_entry += 1
                    say(f"추가진입 {sym} {side} {len(_p.legs)}차 평단{_p.entry:.6f} "
                        f"누적qty={_p.qty} 손절{stop:.6f}")
                    continue
                filled_qty = live_position_qty(ex, sym, filled_qty or qty) \
                    if not args.dry_run else (filled_qty or qty)
                actual_legs = [entry_fill] * len(pd["legs"])
                pd["legs"] = list(actual_legs)
                actual_risk = abs(entry_fill - stop) / entry_fill if entry_fill else risk
                positions[sym] = Pos(
                    sym, side, actual_legs, filled_qty, _ent_t, args.leverage,
                    stop_price=stop,
                    tp_bb=tp_with_floor(entry_fill, padded_tp(
                        entry_fill, fee_aware_bb_price(
                            entry_fill, ind["bb_u"] if L else ind["bb_l"], side,
                            args.roundtrip_fee_rate, args.min_net_tp_rate),
                        side, args.tp_extra_roe_pct, args.leverage),
                        side, args.tp_floor_roe_pct, args.leverage),
                    tp_rr=fee_aware_rr_price(
                        entry_fill, stop, side, args.rr, args.roundtrip_fee_rate))
                positions[sym].since_trade_id = _since_id
                positions[sym].stop_algo_id = sync_stop(
                    ex, sym, side, qty, stop, 0, args.dry_run, say)
                if positions[sym].stop_algo_id:
                    unprotected.pop(sym, None)
                else:
                    unprotected[sym] = {"next_retry_at": time.time() + 15.0,
                                        "last_warn_at": time.time()}
                pd["done"] = len(pd["legs"])
                # [2026-08-20 버그5] 1차 진입 직후 pending을 지우면, 다음 봉에서 EMA10/15를
                # 터치해도 2차/3차 추가진입 경로(sym in positions and pending[sym])로 못 들어간다.
                # 청산/정배열 붕괴/타임아웃 전까지는 pending을 남겨 추가 차수 상태를 이어간다.
                save_state()
                n_entry += 1
                # [2026-08-20] 로그·텔레그램에도 실제 체결 평단을 쓴다.
                # 지역변수 entry 는 체결 반영 전의 EMA 평균이라 화면에만 옛 값이 남는다.
                say(f"진입 {sym} {side} 평단{positions[sym].entry:.6f} "
                    f"{len(positions[sym].legs)}차 qty={filled_qty} "
                    f"손절{stop:.6f} 지연{_ent_t % 60:.1f}s")

            if args.brief_on_clock:
                _lt = time.localtime()
                _slot = (_lt.tm_hour, 0 if _lt.tm_min < 30 else 30)
                if _slot != last_slot:
                    last_slot = _slot
                    say(brief_text(bal))
                    if _slot[1] == 0:
                        say(hourly_perf_text(bal))
                        # [2026-08-21 사용자요청] 정각마다 설정 판정도 함께 보낸다.
                        try:
                            say(config_diag_text())
                        except Exception as _e:
                            say(f"[설정점검 실패] {_e}")

            if args.bar_align:
                while True:
                    wait = 60 - (time.time() % 60) + 0.2
                    if wait <= 2.0:
                        time.sleep(wait)
                        break
                    handle_buttons()
                    retry_unprotected_stops(time.time())
                    reconcile_live_positions(time.time())
                    time.sleep(min(2.0, wait - 0.5))
            else:
                time.sleep(args.poll)
        except KeyboardInterrupt:
            say("사용자 중단")
            break
        except Exception as e:
            print(f"  [주기오류] {type(e).__name__}: {e}", flush=True)
            time.sleep(args.poll)

    if ws_proc is not None:
        stop_ws(ws_proc)
    end = ex.get_total_margin_balance()
    sk = " ".join(f"{k}{v}" for k, v in skips.items() if v)
    say(f"종료 정배열{n_align} 진입{n_entry} 청산{n_exit} | 잔고 {bal0:.4f}->{end:.4f}"
        + (f" | 스킵[{sk}]" if sk else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
