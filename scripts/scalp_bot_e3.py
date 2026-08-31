"""스캘핑 봇 e3 - CM 방향필터 + 눌림 실행 하이브리드 전략 (실주문).

e1(급변동 추종)과 완전히 별개의 전략이다. 신호/진입/청산 전부 다르다.

[현재 운영 상태 - 2026-08-25]
  TradingView `CM_Ultimate_MA_MTF_V2` 기본 계산식으로 방향 신호를 만든 뒤,
  실제 체결은 기존 e2/e3의 EMA 눌림 진입선을 그대로 사용한다.
  즉 "순수 CM 추세추종"도 아니고 "순수 EMA 눌림목"도 아닌 하이브리드다.

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
  python scripts/scalp_bot_e3.py --minutes 20 --dry-run          # 권장
  python scripts/scalp_bot_e3.py --minutes 20 --i-know-it-loses  # 실주문
"""
from __future__ import annotations

import collections
import argparse
import atexit
import json
import math
import os
import re
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bot.config import Config
from bot.exchange import Exchange
from bot.ws_client import FileBackedKlineCache

VERSION = "e3"
LOG_DIR = Path(__file__).resolve().parent.parent / "logs"
LEDGER = LOG_DIR / "scalp_bot_e3_cm_ledger.jsonl"
# [2026-08-20 버그4] e2 가 관리하는 심볼 목록. 재시작 시 남의 포지션을 채택하지 않기 위함.
STATE = LOG_DIR / "scalp_bot_e3_cm_state.json"
WS_PID_FILE = LOG_DIR / "scalp_bot_e3_cm_ws_pid.json"
# [2026-08-21] 봇 본체의 중복 실행을 막는다. WS 워커만 PID 관리가 있었고
# 본체는 없어서, 재시작할 때마다 인스턴스가 쌓여 6개가 동시에 실주문을 냈다.
BOT_PID_FILE = LOG_DIR / "scalp_bot_e3_cm_bot_pid.json"

# [2026-08-25 관측 복구] e3는 say()가 print만 해서, 런처가 stdout을 파일로 안 묶으면
# 실행 중 로그가 어디에도 안 남았다(실측: 21:52 기동분의 stdout/stderr 로그가 20:53에서
# 멈춰 있었다). 거래가 시간당 40건대인데 원장 몇 줄 말고는 사후 복기 수단이 없었다.
# 런처와 무관하게 항상 파일에 남기도록 say() 안에서 직접 append 한다.
RUN_LOG = LOG_DIR / "scalp_bot_e3_cm_run.log"
RUN_LOG_MAX_BYTES = 20 * 1024 * 1024
RUN_LOG_BACKUPS = 3


def log_line(msg: str) -> None:
    """실행 로그를 파일에 남긴다. 관측 코드가 실매매를 죽이면 안 되므로 전부 삼킨다."""
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        try:
            if RUN_LOG.exists() and RUN_LOG.stat().st_size >= RUN_LOG_MAX_BYTES:
                for i in range(RUN_LOG_BACKUPS - 1, 0, -1):
                    older = RUN_LOG.with_suffix(f".log.{i}")
                    newer = RUN_LOG.with_suffix(f".log.{i + 1}")
                    if older.exists():
                        older.replace(newer)
                RUN_LOG.replace(RUN_LOG.with_suffix(".log.1"))
        except Exception:
            pass
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        with RUN_LOG.open("a", encoding="utf-8") as f:
            f.write(stamp + " [" + VERSION + "] pid=" + str(os.getpid()) + " " + str(msg) + chr(10))
    except Exception:
        pass


@dataclass
class Pos:
    symbol: str
    side: str
    legs: list = field(default_factory=list)     # 분할 진입가 목록
    qty: float = 0.0
    entered_at: float = 0.0
    leverage: int = 5
    stop_price: float = 0.0
    # [2026-08-27] 볼밴 익절 제거. 값은 남기되 **아무도 설정하지 않는다**
    # (state 파일 하위호환용). 익절은 CM 지정가 TP 하나로 통일한다 - 원칙 0.
    # 제거 근거: 세 경로 중 안전망(포지션 유입의 93%)만 tp_bb 를 안 넣고 있어서
    # **경로에 따라 청산 규칙이 달랐다.** 볼밴은 e2 잔재이고 원칙 0 위배이므로
    # 통일 방향은 "빼는 쪽"이다. 실동작 변화는 없다(이미 93% 경로에서 꺼져 있었다).
    tp_bb: float = 0.0
    tp_rr: float = 0.0
    max_adverse_roe: float = 0.0
    max_favorable_roe: float = 0.0
    stop_algo_id: int = 0
    # [2026-08-25] 원칙 0(CM) 최대 익절선 기준 지정가 TP. 거래소에 reduceOnly LIMIT 로
    # 실제로 걸어둔 주문의 id 와 가격. 봇이 죽어도 익절이 남고, 폴링 10초 공백이 없어지며,
    # 청산이 taker(0.05%) 대신 maker(0.02%) 로 나간다.
    tp_limit_price: float = 0.0
    tp_order_id: int = 0
    tp_order_placed_at: float = 0.0
    # [2026-08-27] 되돌림 청산을 **지정가로 먼저 시도**할 때의 마감 시각.
    # >0 이면 이미 지정가를 걸어둔 상태다(중복 발동 방지).
    gb_pending: float = 0.0
    # [2026-08-20 버그3] 실손익 집계용. 진입 직전의 마지막 체결 id.
    # 이보다 큰 체결만 이 포지션 것으로 본다. 같은 심볼을 반복 거래할 때
    # 이전 포지션의 체결이 섞여 real_net 이 오염되는 것을 막는다.
    since_trade_id: int = 0
    adopted: bool = False        # 재시작 채택분은 진입 수수료를 알 수 없다
    # [2026-08-26 안B] 안전망(reconcile_live_positions)이 뒤늦게 주워온 포지션인가.
    # 봇이 진입 판정을 거쳐 연 것이 아니므로 "같은 방향 편중" 카운트에서 제외한다.
    # 자본(슬롯/노출)은 실제로 점유하므로 슬롯 카운트에서는 제외하지 않는다.
    swept: bool = False
    # [2026-08-26 1단계 계측] 경과 시간별 ROE 스냅샷 {초: ROE%}. 매매 동작에는
    # 전혀 쓰이지 않고 원장에만 남는다(세 원칙 무관).
    # 목적: "애매한 거래를 언제 끊어야 하나" 를 추정이 아니라 측정으로 답하기 위함.
    # 지금 원장에는 max_favorable_roe(최고점)만 있어 **그 최고점이 몇 분에 왔는지**를
    # 알 수 없다. 그래서 되돌림 9건(-8.05)이 조기 익절로 회수 가능한지 판정이 안 된다.
    roe_marks: dict = field(default_factory=dict)

    @property
    def entry(self) -> float:
        return sum(self.legs) / len(self.legs) if self.legs else 0.0


@dataclass
class CMUltimateMASettings:
    """TradingView CM_Ultimate_MA_MTF_V2 defaults.

    공개 페이지의 Pine 소스 기준 기본값을 그대로 둔다.
    """

    use_current_resolution: bool = True
    res_custom: str = "D"
    len: int = 20
    factor_t3: int = 7
    atype: int = 1
    spc: bool = False
    cc: bool = True
    smoothe: int = 2
    doma2: bool = False
    spc2: bool = False
    len2: int = 50
    sfactor_t3: int = 7
    atype2: int = 1
    cc2: bool = True
    sd: bool = False


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
        "📊 e3상태": "status",
        "📈 e3브리핑": "brief",
        "📋 e3포지션": "pos",
        "📉 e3복기": "review",
        "⏸ e3정지": "pause",
        "▶️ e3재개": "resume",
        "🛑 e3전량청산": "flat",
    }

    def menu(self) -> None:
        """화면 하단에 고정되는 e2 조작 메뉴. 라이브 봇 메뉴와 문구가 겹치지 않게
        전부 'e2' 를 붙였다 — 두 봇이 같은 채팅방을 쓰기 때문."""
        self.send(f"[{VERSION}] 조작 메뉴를 하단에 고정했습니다. 언제든 누르세요.", {
            "keyboard": [
                [{"text": "📊 e3상태"}, {"text": "📈 e3브리핑"}],
                [{"text": "📋 e3포지션"}, {"text": "📉 e3복기"}],
                [{"text": "⏸ e3정지"}, {"text": "▶️ e3재개"}],
                [{"text": "🛑 e3전량청산"}],
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
            if cq and str(cq.get("data", "")).startswith("e3:"):
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
    """심볼의 최신 체결 id. 실패하면 0(=필터 안 함).

    [2026-08-27 버그수정] 종전엔 `limit=1` 로 받아 `tr[-1]` 을 썼는데, 바이낸스는
    **오래된 순**으로 주고 limit 은 앞에서 자른다 — 즉 limit=1 은 최신이 아니라
    **최초** 체결이다(실측 ICPUSDT: 8건 중 가장 오래된 416126090 반환).
    이 값이 since_trade_id 로 쓰여 "이전 포지션 체결이 섞이는 것"을 막는 방어인데,
    오래된 id 라 **필터가 아무것도 거르지 못했다.** 같은 심볼을 반복 거래하면
    이전 포지션 손익이 이번 거래에 섞인다.
    """
    try:
        tr = ex.client.futures_account_trades(symbol=symbol, limit=500)
        return max(int(t["id"]) for t in tr) if tr else 0
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


def live_position_entry(ex, symbol: str, fallback: float) -> float:
    """거래소가 들고 있는 실제 평단을 정본으로 쓴다.

    [2026-08-25 실사고] UAIUSDT 진입에서 봇 내부 평단이 0.326660 으로 기록됐는데 실제
    체결은 0.3383 이었다(3.4% 차이). 원인은 체결 조회가 늦으면 entry_fill_after 가
    신호 시점 가격으로 폴백하기 때문이다. 그 값이 손절선/익절선 계산에 그대로 들어가서
    "LONG 인데 손절가가 진입가 위"인 포지션이 만들어졌고, 3분 만에 잘렸다.
    원장 표시만의 문제가 아니라 실매매 오류였다. 거래소 평단을 정본으로 삼는다.
    """
    try:
        p = ex.get_position(symbol)
        if p:
            v = float(p.get("entry_price") or 0.0)
            if v > 0:
                return v
    except Exception:
        pass
    return fallback


def stop_is_sane(entry: float, stop: float, side: str) -> bool:
    """손절선이 방향에 맞는 쪽에 있는지. LONG 은 진입가 아래, SHORT 은 위여야 한다.

    [2026-08-25] 원인이 무엇이든(평단 오류/지표 이상값/EMA 계산 실패) 결과적으로 말이
    안 되는 손절선을 여기서 전부 잡는다. UAIUSDT 사고 때 이 한 줄이 있었으면 바로 드러났다.
    """
    if entry <= 0 or stop <= 0:
        return False
    return (stop < entry) if side == "LONG" else (stop > entry)


def fallback_stop(entry: float, side: str, pct: float = 1.2) -> float:
    """손절선이 비정상일 때 쓰는 고정 % 폴백(채택 경로의 기존 폴백과 같은 값)."""
    return entry * (1 - pct / 100.0) if side == "LONG" else entry * (1 + pct / 100.0)


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


def place_limit_entry_nowait(ex, symbol: str, side: str, quantity: float):
    """지정가 진입 주문만 내고 즉시 반환한다(체결 대기 없음).

    [2026-08-25 B안] 기존 place_v2_limit_entry는 그 자리에서 10초를 기다렸다. 진입 스캔과
    보유 포지션 청산 판정이 같은 루프에 있어서, 그 10초 동안 청산 폴링이 통째로 밀렸다.
    e3는 거래소 TP 주문이 없어 BB/RR 익절이 폴링 전용이라 익절 타이밍을 그만큼 놓친다.
    실측(3.5분): 진입 시도 4건 중 3건이 10초 안에 안 붙어 포기됐다 — 대기가 병목인데
    늘리면 블로킹도 같이 늘어나는 구조였다.
    주문만 내고 다음 루프에서 체결을 확인하면, 대기시간을 늘리면서 블로킹은 0이 된다.
    반환: (order_id, price) 또는 (0, 0.0)
    """
    book = ex.get_book_ticker(symbol)
    price = float(book["bid"] if side == "LONG" else book["ask"])
    quantity = ex.round_quantity(symbol, quantity, price=price)
    if not quantity or price <= 0:
        return 0, 0.0
    order = ex.open_limit_position(symbol, side, quantity, price)
    return int(order["orderId"]), price


def entry_order_state(ex, symbol: str, order_id: int) -> tuple[str, float]:
    """진입 주문 상태를 (상태, 체결수량)으로 돌려준다. 조회 실패는 UNKNOWN으로 둔다."""
    try:
        st = ex.get_order_status(symbol, order_id)
    except Exception:
        return "UNKNOWN", 0.0
    return str(st.get("status") or "UNKNOWN"), float(st.get("executedQty", 0) or 0)


def place_v2_limit_entry(ex, symbol: str, side: str, quantity: float, wait_sec: float = 10.0) -> bool:
    """v2 passive limit entry; never chases an unfilled order with market."""
    book = ex.get_book_ticker(symbol)
    price = float(book["bid"] if side == "LONG" else book["ask"])
    quantity = ex.round_quantity(symbol, quantity, price=price)
    if not quantity or price <= 0:
        return False
    order = ex.open_limit_position(symbol, side, quantity, price)
    order_id = order["orderId"]
    deadline = time.time() + max(0.0, wait_sec)
    while time.time() < deadline:
        status = ex.get_order_status(symbol, order_id)
        if status.get("status") == "FILLED":
            return True
        time.sleep(0.5)
    status = ex.get_order_status(symbol, order_id)
    executed = float(status.get("executedQty", 0) or 0)
    ex.cancel_regular_order(symbol, order_id)
    if executed > 0:
        return True
    return False


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


def pullback_depth_pct(entry: float, hma20: float) -> float:
    """진입가가 HullMA20 에서 얼마나 벌어져 있는지(가격 %). '눌림 깊이'.

    [2026-08-26 개선⑥] 캐시 132건에서 7개 후보 지표 중 성적과 가장 강하게 연결된 값이다
    (상관 r=-0.344, t=-4.18). 방향은 직관과 반대다 — **깊게 눌린 것일수록 나쁘다**.
    깊은 눌림은 이미 크게 움직였다는 뜻이고, 그런 자리는 손절이 멀어지고 되돌림도 크다.
    같은 결론이 세 경로에서 독립적으로 나왔다:
      - 캐시 눌림 깊이 r=-0.344
      - 실원장 'CM 목표까지 거리' r=-0.261 (t=-2.67, n=100)
      - 어제 whipsaw 분석: 최저변동성 구간이 최고(+0.599%), 1.50+ 최악(-1.747%)
    """
    if entry <= 0 or hma20 <= 0:
        return 0.0
    return abs(entry - hma20) / entry * 100.0


def same_side_count(positions: dict, entry_orders: dict, side: str) -> int:
    """이미 같은 방향으로 잡고 있는 슬롯 수. 미체결 진입주문도 센다.

    미체결까지 세는 이유: 같은 사이클에 여러 심볼이 동시에 신호를 내면 주문만 4~5개가
    한꺼번에 나가고, 체결된 뒤에야 편중이 드러난다. 그때는 이미 늦다.

    [2026-08-26 안B] 안전망이 주워온 포지션(`swept`)은 세지 않는다.
    ④의 근거는 원장 168건 "**봇이** 같은 방향 3개를 들고 있을 때 승률 36.8%" 였고,
    그 표본은 전부 봇 자기 진입이다. 봇이 진입 판정도 하지 않은 포지션을 같은
    카운터에 넣는 것은 근거 없는 확대적용이다. 실제로 12:04~12:21 구간에서 안전망
    채택분 6건이 슬롯을 채우자 방향편중 스킵이 74%(106/144)로 뛰고 **봇 자체 진입이
    0건**이 됐다 — 원칙 1을 근거 없이 해친 것이다.
    자본 제약(슬롯/노출/증거금)은 그대로 둔다 — 그쪽은 실제로 점유하기 때문이다.
    """
    n = sum(1 for p in positions.values() if p.side == side and not p.swept)
    n += sum(1 for o in entry_orders.values() if o.get("side") == side)
    return n


class StopAlreadyBreached(Exception):
    """손절선을 이미 지난 상태에서 SL 등록이 거부됐다(-2021). 붙들지 말고 즉시 끊어야 한다."""

    def __init__(self, symbol: str):
        super().__init__(symbol)
        self.symbol = symbol


def sync_stop(ex, symbol: str, side: str, qty: float, stop: float,
              old_algo_id: int = 0, dry: bool = False, warn=None,
              limit_price: float = 0.0) -> int:
    """거래소 손절주문을 '항상 하나만' 유지한다.

    [2026-08-20 버그1 / P0] 이전 구현은 추가 진입 시 새 leg 수량으로 손절을 먼저 걸고,
    그 다음 합산 수량으로 또 걸면서 먼저 건 주문을 취소하지도 추적하지도 않았다.
    남은 고아 STOP_MARKET 이 나중에 따로 발동해 포지션 일부를 예기치 않게 잘라낸다.
    등록은 반드시 이 함수 하나만 거치게 한다: 먼저 취소, 그 다음 등록.

    limit_price>0 이면 STOP_MARKET 대신 **스탑-리밋**으로 건다(수수료 maker 시도).
    미체결 위험이 생기므로 호출부가 반드시 guard_stop_breach 로 감시해야 한다.
    """
    if dry:
        return 0
    if old_algo_id:
        try:
            ex.cancel_order(symbol, old_algo_id)
        except Exception:
            pass
    try:
        if limit_price and limit_price > 0:
            r = ex.place_stop_limit(symbol, side, qty, stop, limit_price)
        else:
            r = ex.place_stop_market(symbol, side, qty, stop)
        return int((r or {}).get("algoId") or 0)
    except Exception as e:
        # [2026-08-26 P0] -2021 "Order would immediately trigger" 는 다른 실패와 뜻이 다르다.
        # **이미 손절선을 지났다**는 뜻이다. 그런데 지금까지는 경고만 하고 unprotected 에
        # 넣어 재등록을 계속 시도했다 — 이미 손절 조건인 포지션을 붙들고 있었던 것이다.
        # 거래소 SL 이 없으니 10초 폴링이 잡을 때까지 손실이 벌어졌다.
        # 실측(8/26 밤샘): 이 경로로 빠진 9건이 ROE -6% 를 넘겼고(최악 -18.76%,
        # 24초 만에 -10.24%), 그 9건만으로 손실의 34%(-20.80)를 만들었다.
        # 재등록이 아니라 즉시 시장가로 끊는 게 맞다.
        if "-2021" in str(e) or "immediately trigger" in str(e).lower():
            if warn:
                warn(f"{symbol} 손절선 이미 통과(-2021) - 재등록 대신 즉시 시장가 청산")
            raise StopAlreadyBreached(symbol) from e
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


def _series_ema(values, length: int) -> list[float]:
    if length <= 0 or not values:
        return []
    alpha = 2.0 / (length + 1.0)
    out = []
    prev = None
    for value in values:
        value = float(value)
        prev = value if prev is None else (alpha * value + (1.0 - alpha) * prev)
        out.append(prev)
    return out


def _series_rma(values, length: int) -> list[float]:
    if length <= 0 or not values:
        return []
    alpha = 1.0 / float(length)
    out = []
    prev = None
    for value in values:
        value = float(value)
        prev = value if prev is None else (alpha * value + (1.0 - alpha) * prev)
        out.append(prev)
    return out


def _series_sma(values, length: int) -> list[float]:
    out = []
    window = []
    total = 0.0
    for value in values:
        value = float(value)
        window.append(value)
        total += value
        if len(window) > length:
            total -= window.pop(0)
        out.append(total / len(window))
    return out


def _series_wma(values, length: int) -> list[float]:
    if length <= 0:
        return []
    out = []
    weights = list(range(1, length + 1))
    wsum = float(sum(weights))
    for idx in range(len(values)):
        window = [float(v) for v in values[max(0, idx - length + 1):idx + 1]]
        local_weights = weights[-len(window):]
        out.append(sum(v * w for v, w in zip(window, local_weights)) / sum(local_weights))
    return out


def _series_vwma(close_values, volume_values, length: int) -> list[float]:
    out = []
    cv = []
    vv = []
    for close, volume in zip(close_values, volume_values):
        close = float(close)
        volume = float(volume)
        cv.append(close * volume)
        vv.append(volume)
        if len(cv) > length:
            cv.pop(0)
            vv.pop(0)
        denom = sum(vv)
        out.append(sum(cv) / denom if denom else close)
    return out


def _series_hma(values, length: int) -> list[float]:
    if length <= 0:
        return []
    half = max(1, int(length / 2))
    root = max(1, int(round(math.sqrt(length))))
    wma_half = _series_wma(values, half)
    wma_full = _series_wma(values, length)
    raw = [2.0 * a - b for a, b in zip(wma_half, wma_full)]
    return _series_wma(raw, root)


def _series_tema(values, length: int) -> list[float]:
    ema1 = _series_ema(values, length)
    ema2 = _series_ema(ema1, length)
    ema3 = _series_ema(ema2, length)
    return [3.0 * (a - b) + c for a, b, c in zip(ema1, ema2, ema3)]


def _gd_series(values, length: int, factor: float) -> list[float]:
    ema1 = _series_ema(values, length)
    ema2 = _series_ema(ema1, length)
    return [a * (1.0 + factor) - b * factor for a, b in zip(ema1, ema2)]


def _series_tilson_t3(values, length: int, factor_input: int) -> list[float]:
    factor = float(factor_input) * 0.10
    return _gd_series(_gd_series(_gd_series(values, length, factor), length, factor), length, factor)


def _series_tilson_t3_second(values, length: int, factor_input: int) -> list[float]:
    """원본 Pine의 2nd MA T3 구현을 그대로 따른다.

    공개 소스에는 `st3()` 첫 단계가 `sgd(gd(...))`로 되어 있다.
    의도된 구현과 다를 수 있지만, 일치 검증에서는 이 형태를 보존해야 한다.
    """

    factor = float(factor_input) * 0.10
    return _gd_series(_gd_series(_gd_series(values, length, factor), length, factor), length, factor)


def _series_ma(values, volume_values, length: int, atype: int, factor_input: int, second: bool = False) -> list[float]:
    if atype == 1:
        return _series_sma(values, length)
    if atype == 2:
        return _series_ema(values, length)
    if atype == 3:
        return _series_wma(values, length)
    if atype == 4:
        return _series_hma(values, length)
    if atype == 5:
        return _series_vwma(values, volume_values, length)
    if atype == 6:
        return _series_rma(values, length)
    if atype == 7:
        return _series_tema(values, length)
    if atype == 8:
        return _series_tilson_t3_second(values, length, factor_input) if second else _series_tilson_t3(values, length, factor_input)
    raise ValueError(f"unsupported ma type: {atype}")


# [2026-08-26] 봇이 API 로 낸 주문은 clientOrderId 가 브로커 태그 `x-` 로 시작한다.
# 바이낸스 앱/웹에서 낸 수동 주문은 `ios_`/`web_`/`android_` 등 다른 접두어를 쓴다.
# 실측(100건): 봇 LIMIT 76 + MARKET(손절체결 포함) 23 = 전부 `x-Cb7ytekJ`, 수동 1건은 `ios_`.
# 수동 개입 체결이 원장에 섞이면 봇 성과가 오염된다 — 실사고 PYTHUSDT:
# 앱에서 낸 BUY 11450(reduceOnly=False)이 봇의 SHORT 4404 를 덮고 LONG 7046 으로 뒤집었고,
# 그 손익 -7.13/명목 622.93 이 봇 원장에 SHORT 로 기록됐다.
_BOT_ORDER_PREFIX = "x-"
# 진입 체결이 발견 시각보다 얼마나 앞설 수 있는가. 눌림 지정가 대기 +
# 폴링 주기를 넉넉히 덮는다. since_trade_id 가 실제 경계를 잡으므로 넓어도 안전.
WIDE_TRADE_LOOKBACK_MS = 6 * 60 * 60 * 1000   # 6시간


def trades_start_ms(pos) -> int:
    """체결 조회 startTime. **entered_at 은 실제 체결 시각이 아니다.**

    [2026-09-01 P0 버그수정] 종전엔 `entered_at*1000 - 5000`(5초 버퍼)이었다.
    그런데 주 진입 경로(안전망, 유입의 93%)는 Pos 를 만들 때 `entered_at=now_ts`,
    즉 **폴링이 포지션을 발견한 시각**을 쓴다. 눌림 지정가는 수 분 대기 후 체결되고
    발견은 그 다음 스캔이라, 진입 체결이 5초 창 **앞으로 빠진다.**
    그러면 `tr` 에 청산 다리만 남아 `real_commission` 이 **한쪽 다리만** 기록된다.

    실측(원장 1,017건): 377건(37%)이 한 다리만 기록됐고 그중 345건이
    realizedPnl != 0 — 즉 빠진 쪽이 진입 다리다. 왕복 수수료율 0.0408% -> 0.0511%,
    순손익 -169.30 -> -185.46 으로 **원장이 9.5% 낙관 편향**돼 있었다.

    이 포지션의 체결만 고르는 **진짜 필터는 `since_trade_id`** 이고, 그것은 진입 주문
    을 내기 **전**에 잡히므로(4520행) 창을 넓혀도 이전 포지션이 섞이지 않는다.
    그래서 since_trade_id 가 있을 때만 넓게 잡고, 없으면 (dry-run·조회실패) 종전대로
    좁게 둔다 — 필터가 없는 상태에서 창까지 넓히면 이전 포지션이 섞이기 때문이다.
    """
    if getattr(pos, "since_trade_id", 0):
        return int(pos.entered_at * 1000) - WIDE_TRADE_LOOKBACK_MS
    return int(pos.entered_at * 1000) - 5000


def manual_order_ids(ex, symbol: str, start_ms: int) -> set:
    """해당 구간에서 **봇이 내지 않은** 주문의 orderId 집합. 조회 실패 시 빈 집합(=필터 안 함)."""
    try:
        rows = ex.client.futures_get_all_orders(symbol=symbol, startTime=max(0, start_ms))
    except Exception:
        return set()
    out = set()
    for o in rows:
        cid = str(o.get("clientOrderId") or "")
        if not cid.startswith(_BOT_ORDER_PREFIX):
            try:
                out.add(int(o.get("orderId")))
            except Exception:
                pass
    return out


def drop_manual_trades(ex, symbol: str, trades: list, start_ms: int, say=None) -> list:
    """체결 목록에서 수동 주문분을 제거한다. userTrades 에는 clientOrderId 가 없어
    주문 조회로 orderId 를 매핑한다(청산 1회당 조회 1회)."""
    if not trades:
        return trades
    bad = manual_order_ids(ex, symbol, start_ms)
    if not bad:
        return trades
    kept = [t for t in trades if int(t.get("orderId", 0)) not in bad]
    if say and len(kept) != len(trades):
        _n = len(trades) - len(kept)
        say(f"경고 수동 개입 체결 {_n}건 제외 {symbol} - 봇 성과에서 분리(원장 오염 방지)")
    return kept


def cm_ultimate_ma_mtf_v2(df, mtf_df=None, settings: CMUltimateMASettings | None = None) -> dict | None:
    """TradingView CM_Ultimate_MA_MTF_V2 계산식을 로컬 DataFrame에 적용한다.

    `df`는 차트 기준 시간대 봉, `mtf_df`는 `use_current_resolution=False`일 때 사용할
    상위/하위 시간대 봉이다. 반환값은 현재 봉 기준 주요 출력과 판정 플래그다.
    """

    if df is None or len(df) == 0:
        return None
    settings = settings or CMUltimateMASettings()
    source_df = df if settings.use_current_resolution or mtf_df is None else mtf_df
    if source_df is None or len(source_df) == 0:
        return None
    close_values = [float(x) for x in source_df["close"].tolist()]
    volume_values = [float(x) for x in source_df["volume"].tolist()]
    if not close_values:
        return None

    avg = _series_ma(close_values, volume_values, settings.len, settings.atype, settings.factor_t3, second=False)
    avg2 = _series_ma(close_values, volume_values, settings.len2, settings.atype2, settings.sfactor_t3, second=True)

    if source_df is df:
        out1_series = avg
        out2_series = avg2
    else:
        import pandas as _pd
        # [2026-08-28 lookahead 수정] 종전에는 상위봉의 **open_time** 으로 backward 병합했다.
        # 그러면 open_time=T 인 상위봉의 out1(=T+span 에 확정되는 종가 기반 값)이
        # [T, T+span) 구간의 차트봉에 붙는다 — **아직 마감되지 않은 봉의 종가를 미리 보는 것**이다.
        # 백테스트에서는 lookahead 이고 라이브에서는 리페인트다(봉 안에서 판정이 뒤집힌다).
        # TradingView 의 security(..., lookahead_off) 는 상위봉이 **마감된 뒤**에만 값을 주므로
        # 원본과 맞추려면 상위봉 시각을 span 만큼 밀어 '마감 시각' 으로 병합해야 한다.
        #
        # 이 경로는 --cm-use-alt-resolution 을 켤 때만 탄다(현재 라이브는 꺼져 있어 미사용).
        # 원칙 0 의 "15분 HullMA20" 을 실제로 켜려면 이 경로를 쓰게 되므로 미리 고쳐 둔다.
        # 함께 볼 것: --cm-res-custom 기본값이 "D"(일봉)라, 15분을 원하면 반드시 15 를 명시해야 한다.
        _src_t = source_df["open_time"]
        if len(_src_t) < 2:
            return None                      # span 을 못 재면 안전하게 판정 불가
        _span = _src_t.diff().median()
        # Timedelta/숫자 어느 dtype 이든 통하는 0 비교
        if _pd.isna(_span) or _span <= _span * 0:
            return None
        _right = source_df[["open_time"]].assign(out1=avg, out2=avg2)
        _shifted = _src_t + _span                            # 마감 시각으로 이동
        # [dtype 보존] open_time 이 int64(ms) 면 median 이 float 라 컬럼이 float64 로 승격되고,
        # merge_asof 가 "incompatible merge keys" 로 죽는다. 원래 dtype 으로 되돌린다.
        if _pd.api.types.is_integer_dtype(_src_t.dtype):
            _shifted = _shifted.round().astype(_src_t.dtype)
        _right["open_time"] = _shifted
        aligned = _pd.merge_asof(
            df[["open_time"]].sort_values("open_time"),
            _right.sort_values("open_time"),
            on="open_time",
            direction="backward",
        )
        out1_series = aligned["out1"].astype(float).tolist()
        out2_series = aligned["out2"].astype(float).tolist()

    if not out1_series:
        return None

    smooth = max(1, int(settings.smoothe))
    idx = len(df) - 1
    out1 = float(out1_series[-1])
    out2 = float(out2_series[-1]) if out2_series else 0.0
    open_ = float(df["open"].iloc[idx])
    close_ = float(df["close"].iloc[idx])
    prev_idx = max(0, len(out1_series) - 1 - smooth)
    ma_up = out1 >= float(out1_series[prev_idx])
    ma_down = out1 < float(out1_series[prev_idx])
    cr_up = open_ < out1 and close_ > out1
    cr_down = open_ > out1 and close_ < out1
    cr_up2 = open_ < out2 and close_ > out2
    cr_down2 = open_ > out2 and close_ < out2
    crossed = len(out1_series) >= 2 and len(out2_series) >= 2 and (
        (out1_series[-2] <= out2_series[-2] and out1 > out2) or
        (out1_series[-2] >= out2_series[-2] and out1 < out2)
    )
    return {
        "close": close_,
        "out1": out1,
        "out2": out2,
        "ma_up": ma_up,
        "ma_down": ma_down,
        "cr_up": cr_up,
        "cr_down": cr_down,
        "cr_up2": cr_up2,
        "cr_down2": cr_down2,
        "crossed": crossed,
        "settings": settings,
    }


def _tv_resolution_to_binance_interval(resolution: str) -> str:
    res = str(resolution or "").strip()
    if not res:
        raise ValueError("empty TradingView resolution")
    upper = res.upper()
    mapping = {
        "D": "1d",
        "1D": "1d",
        "W": "1w",
        "1W": "1w",
        "M": "1M",
        "1M": "1M",
    }
    if upper in mapping:
        return mapping[upper]
    if upper.endswith("H"):
        return f"{int(upper[:-1])}h"
    if upper.endswith("D"):
        return f"{int(upper[:-1])}d"
    if upper.endswith("W"):
        return f"{int(upper[:-1])}w"
    if upper.endswith("M") and upper[:-1].isdigit():
        return f"{int(upper[:-1])}M"
    if res.isdigit():
        minutes = int(res)
        if minutes <= 0:
            raise ValueError(f"invalid resolution: {resolution}")
        if minutes % 60 == 0 and minutes >= 60:
            hours = minutes // 60
            return f"{hours}h" if hours < 24 else f"{hours // 24}d"
        return f"{minutes}m"
    raise ValueError(f"unsupported TradingView resolution: {resolution}")


def build_cm_settings_from_args(args) -> CMUltimateMASettings:
    return CMUltimateMASettings(
        use_current_resolution=not getattr(args, "cm_use_alt_resolution", False),
        res_custom=str(getattr(args, "cm_res_custom", "D")),
        len=int(getattr(args, "cm_len", 20)),
        factor_t3=int(getattr(args, "cm_factor_t3", 7)),
        atype=int(getattr(args, "cm_atype", 1)),
        spc=bool(getattr(args, "cm_show_price_crossing", False)),
        cc=not getattr(args, "cm_disable_color_direction", False),
        smoothe=int(getattr(args, "cm_smoothe", 2)),
        doma2=bool(getattr(args, "cm_enable_second_ma", False)),
        spc2=bool(getattr(args, "cm_show_price_crossing_second", False)),
        len2=int(getattr(args, "cm_len2", 50)),
        sfactor_t3=int(getattr(args, "cm_factor_t3_second", 7)),
        atype2=int(getattr(args, "cm_atype2", 1)),
        cc2=not getattr(args, "cm_disable_color_direction_second", False),
        sd=bool(getattr(args, "cm_show_cross_dots", False)),
    )


# [2026-08-26] 상위 타임프레임 추세 캐시. {symbol: (판정시각, 정배열여부)}
# 4시간봉은 WS 캐시에 없어 항상 REST 다. 85심볼을 매 사이클 조회하면 IP 밴 위험이라
# 반드시 캐시한다(4시간봉은 4시간에 한 번 바뀌므로 10분 캐시로 충분하다).
_HTF_TREND: dict = {}
_HTF_WANTED: set = set()


def htf_uptrend(ex, symbol: str, interval: str, ema_len: int,
                ttl_sec: float = 600.0) -> bool | None:
    """상위 타임프레임 종가가 EMA 위인가. 판정 불가면 None.

    CLAUDE.md 원칙 0 은 "15분 HullMA20 방향/교차 + **4시간 EMA200 필터**" 인데,
    `--cm-use-alt-resolution` 이 꺼져 있어 상위 타임프레임을 전혀 보지 않고
    3분봉 단일 시간대로만 판단하고 있었다. 원칙 0 의 절반이 실행되지 않은 것이다.

    원장 332건 전수 검증(4시간봉 65심볼 수집):
      정합 163건(49.1%) 승률52.1% 건당-0.0784 t=-0.84 합계 -12.78
      역행 169건(50.9%) 승률46.7% 건당-0.2393 t=-2.71 합계 -40.45
    전체 손실 -53.23 중 **-40.45(76%)가 역행 진입**이었다.

    [2026-08-26 P0 회귀수정] **스캔 루프에서 절대 블록하면 안 된다.**
    처음 구현은 캐시 미스 때 REST 를 동기 호출했는데, 4시간봉은 WS 캐시에 없어
    항상 REST 다. 85심볼이 순차로 미스 나면 그 시간만큼 메인 루프가 멈춘다.
    실사고: 17:02 배포 후 봇이 6분간 진전 없음(CPU 20초에 0.03초), 신규 매매 0건.
    워치독(180초)도 발동하지 않았다 - 정렬 대기 루프의 beat 가 살아 있었기 때문이다.
    이제 **캐시에 없으면 즉시 None(필터 통과)** 을 돌려주고, 채우는 일은
    백그라운드 워커(start_htf_refresher)가 스로틀을 걸어 따로 한다.
    """
    hit = _HTF_TREND.get(symbol)
    if hit and time.time() - hit[0] < ttl_sec:
        return hit[1]
    _HTF_WANTED.add(symbol)      # 백그라운드가 채워준다
    return None                  # 판정 불가 = 필터 통과(원칙 1 보호)


def start_htf_refresher(ex, interval: str, ema_len: int, log=None) -> None:
    """상위 타임프레임 추세 캐시를 백그라운드에서 채운다(스캔 루프 비블로킹).

    4시간봉은 4시간에 한 번 바뀌므로 급할 것이 없다. 스로틀을 넉넉히 걸어
    IP 밴 위험도 없앤다.
    """
    def _work():
        while True:
            try:
                want = list(_HTF_WANTED)
                for sym in want:
                    hit = _HTF_TREND.get(sym)
                    if hit and time.time() - hit[0] < 540.0:
                        continue
                    try:
                        df = ex.get_klines(sym, limit=300, interval=interval)
                        if df is not None and len(df) >= ema_len:
                            ema = df["close"].astype(float).ewm(
                                span=ema_len, adjust=False).mean()
                            _HTF_TREND[sym] = (
                                time.time(),
                                bool(float(df["close"].iloc[-1]) > float(ema.iloc[-1])))
                    except Exception:
                        pass
                    # [2026-08-27] 0.35 -> 0.15. 85심볼 한 바퀴가 30초 -> 13초.
                    # 종전엔 TTL(9분)과 경쟁해 `상위추세미확정` 이 28% 로 남아 있었고
                    # (12시간 전 33% 에서 안 줄었다), 그만큼 진입 후보가 사라졌다.
                    # **판정식·TTL·스캔 루프는 그대로다 — 캐시 채우는 속도만 바뀐다.**
                    # 4시간봉은 심볼당 9분에 1회라 REST 는 분당 6.5 -> 13회 수준이다.
                    time.sleep(0.15)          # 스로틀
            except Exception:
                pass
            time.sleep(5.0)

    threading.Thread(target=_work, name="htf-refresher", daemon=True).start()
    if log:
        log(f"상위추세 캐시 워커 기동 ({interval}/EMA{ema_len}, 비블로킹)")


def cm_signal_snapshot(ex, symbol: str, args, chart_df=None) -> dict | None:
    # [2026-08-26 개선①] chart_df 를 인자로 받는다.
    # 호출부가 방금 signal_bars() 로 만든 것과 **완전히 같은 신호봉**을 여기서 한 번 더
    # 만들고 있었다(같은 ex/symbol/signal_tf_min). 심볼당 get_klines + to_datetime +
    # groupby 리샘플이 2회씩 돌아 85심볼 1회전이 2배로 느려졌고, 그 지연이 곧
    # max_signal_age(10초) 창을 넘겨 **스캔 뒷줄 심볼이 구조적으로 탈락**하는 원인이었다.
    # (그 병목 자체는 max_signal_age 주석에 이미 실측으로 기록돼 있다.)
    # 신호값·필터·진입가는 한 줄도 바뀌지 않는다 - 같은 값을 두 번 계산하던 것을 없앨 뿐.
    if chart_df is None:
        chart_df = signal_bars(ex, symbol, args.signal_tf_min)
    settings = build_cm_settings_from_args(args)
    mtf_df = None
    if not settings.use_current_resolution:
        interval = _tv_resolution_to_binance_interval(settings.res_custom)
        mtf_df = ex.get_klines(symbol, limit=300, interval=interval)
    cm = cm_ultimate_ma_mtf_v2(chart_df, mtf_df=mtf_df, settings=settings)
    if not cm:
        return None
    long_ok = cm["ma_up"] and cm["close"] > cm["out1"]
    short_ok = cm["ma_down"] and cm["close"] < cm["out1"]
    if settings.doma2:
        long_ok = long_ok and cm["close"] > cm["out2"]
        short_ok = short_ok and cm["close"] < cm["out2"]
    signal = "LONG" if long_ok and not short_ok else "SHORT" if short_ok and not long_ok else None
    cm["signal"] = signal
    return cm


def signal_bars(ex, symbol: str, minutes: int):
    """신호 판정용 봉을 가져온다. minutes>1 이면 1분봉을 합쳐서 만든다."""
    df = ex.get_klines(symbol, limit=klines_limit_for_tf(minutes))
    return resample_bars(df, minutes)


def flip_age(df, tf_min: int, want_up: bool):
    """HullMA20 이 지금 방향으로 돌아선 뒤 몇 봉이 지났는가. 0 = 전환 봉 자신.

    판정 규칙은 CM 원본과 같다: `ma_up = out1 >= out1[smoothe]`(smoothe=2).
    되짚어 올라가며 같은 방향이 유지된 봉 수를 센다. 판정 불가면 None.
    df 는 이미 신호봉(3분)으로 리샘플된 것이어야 한다.
    """
    try:
        c = [float(x) for x in df["close"].tolist()]
        v = [float(x) for x in df["volume"].tolist()]
    except Exception:
        return None
    if len(c) < 30:
        return None
    hs = _series_ma(c, v, 20, 4, 7, second=False)
    n = len(hs)
    if n < 5 or hs[-1] is None or hs[-3] is None:
        return None
    cur = hs[-1] >= hs[-3]
    if cur != want_up:
        return None
    age, k = 0, n - 1
    while k >= 3 and hs[k - 1] is not None and hs[k - 3] is not None:
        if (hs[k - 1] >= hs[k - 3]) != cur:
            break
        age += 1
        k -= 1
        if age > 20:
            break
    return age


def indicators(df):
    """CM Rule 0: 15m HullMA20 plus legacy e2 risk levels."""
    c = [float(x) for x in df["close"].tolist()]
    if len(c) < 30:
        return None
    e5, e10, e15, e25 = (ema_last(c, 5), ema_last(c, 10),
                         ema_last(c, 15), ema_last(c, 25))
    w = c[-20:]
    mu = sum(w) / 20
    sd = (sum((x - mu) ** 2 for x in w) / 20) ** 0.5
    def wma(values, n):
        weights = list(range(1, n + 1))
        return sum(v * w for v, w in zip(values[-n:], weights)) / sum(weights)
    if len(c) < 30:
        return None
    raw = [2 * wma(c[:i], 10) - wma(c[:i], 20) for i in range(20, len(c) + 1)]
    h20 = wma(raw, 4)
    hprev_values = raw[:-1]
    hprev = wma(hprev_values, 4) if len(hprev_values) >= 4 else h20
    # ---- 원칙 0(CM) 기반 최대 익절선 ----
    # HullMA20 이 지금 방향으로 돌아선 지점까지 되짚어 올라가, 그 구간의 스윙 극값을
    # 이번 추세가 실제로 닿아본 최대치로 본다. 볼밴 상/하단은 e2 뼈대 잔재라 원칙 0 과
    # 어긋나 있었다. 캐시 3분봉 1,943 진입 A/B: 볼밴+시장가 건당 -0.35%/승률 52.9% vs
    # CM극값-0.2% 지정가 +0.38%/승률 75.6%(체결률 43.1% -> 82.5%).
    hs = [wma(raw[:k + 1], 4) if k + 1 >= 4 else None for k in range(len(raw))]
    hi = [float(x) for x in df["high"].tolist()]
    lowv = [float(x) for x in df["low"].tolist()]
    off = len(c) - len(hs)          # hs[k] 는 c[off+k] 에 대응
    cm_up, cm_dn = 0.0, 0.0
    if len(hs) >= 4 and hs[-1] is not None:
        for up in (True, False):
            k = len(hs) - 1
            while (k > 3 and hs[k - 1] is not None and hs[k - 3] is not None
                   and ((hs[k - 1] >= hs[k - 3]) if up else (hs[k - 1] < hs[k - 3]))):
                k -= 1
            lo_i = max(0, off + k)
            if up:
                cm_up = max(hi[lo_i:]) if hi[lo_i:] else 0.0
            else:
                cm_dn = min(lowv[lo_i:]) if lowv[lo_i:] else 0.0
    return {"e5": e5, "e10": e10, "e15": e15, "e25": e25,
            "hma20": h20, "hma20_prev": hprev,
            "cm_tp_long": cm_up, "cm_tp_short": cm_dn,
            "bb_u": mu + 2 * sd, "bb_l": mu - 2 * sd, "close": c[-1]}


def sync_tp_limit(ex, pos, dry_run: bool, say) -> None:
    """포지션의 지정가 TP 주문을 거래소 상태와 맞춘다(있으면 갈아끼우고 없으면 건다).

    reduceOnly 라 포지션이 없으면 체결되지 않는다. 수량이 바뀌는 추가 진입에서는
    반드시 기존 주문을 먼저 취소해야 부분 reduceOnly 주문이 고아로 남지 않는다.
    """
    if dry_run or pos.tp_limit_price <= 0 or pos.qty <= 0:
        return
    if pos.tp_order_id:
        try:
            ex.cancel_regular_order(pos.symbol, pos.tp_order_id)
        except Exception:
            pass
        pos.tp_order_id = 0
    try:
        r = ex.close_limit_position(pos.symbol, pos.side, abs(pos.qty), pos.tp_limit_price)
        pos.tp_order_id = int((r or {}).get("orderId") or 0)
        pos.tp_order_placed_at = time.time()
        say(f"지정가 TP 등록 {pos.symbol} {pos.side} {pos.tp_limit_price:.8f} "
            f"qty={pos.qty} (CM 최대익절선 기준)")
    except Exception as e:
        pos.tp_order_id = 0
        pos.tp_order_placed_at = 0.0
        say(f"지정가 TP 등록 실패 {pos.symbol}: {e} - 봇 폴링 익절로 대체")


def cancel_tp_limit(ex, pos, dry_run: bool) -> None:
    """다른 사유로 청산할 때 남은 TP 주문을 반드시 지운다.

    안 지우면 reduceOnly 고아 주문이 남아, **같은 심볼에 다음에 새로 진입하는 순간**
    엉뚱한 가격에 포지션을 잘라낸다. STOP_MARKET 고아 주문에서 이미 겪은 사고다.
    """
    if dry_run or not pos.tp_order_id:
        return
    try:
        ex.cancel_regular_order(pos.symbol, pos.tp_order_id)
    except Exception:
        pass
    pos.tp_order_id = 0


def cap_stop_roe(entry: float, stop: float, side: str, leverage: int,
                 max_roe_pct: float) -> float:
    """손절폭에 ROE 상한을 씌운다. 상한을 넘으면 상한선까지 당겨온다.

    [2026-08-25 안1] EMA25 가 멀리 있으면 손절도 그만큼 멀어져 한 방에 크게 잃는다.
    실거래 22건 진입 로그에서 손절폭이 ROE 2% 짜리와 139% 짜리(TRUMPUSDT 가격 27.8%)가
    섞여 나왔다. 캐시 실측(동일 신호/동일 TP): 상한 없음 건당 -0.36% -> ROE5% 상한 -0.25%.
    거래를 거르지 않고 손절선만 옮기므로 거래수는 변하지 않는다(원칙 1 무영향).

    max_roe_pct <= 0 이면 상한을 적용하지 않는다.
    """
    if max_roe_pct <= 0 or entry <= 0 or leverage <= 0 or stop <= 0:
        return stop
    cap = abs(max_roe_pct) / 100.0 / leverage
    limit = entry * (1 - cap) if side == "LONG" else entry * (1 + cap)
    if (stop < limit) if side == "LONG" else (stop > limit):
        return limit
    return stop


def cap_tp_roe(entry: float, tp: float, side: str, leverage: int,
               max_roe_pct: float) -> float:
    """익절선이 ROE max_roe_pct%% 를 넘으면 그 지점으로 당긴다. 0 이면 그대로.

    [2026-08-26] 종전엔 이 상한이 **CM 익절선 경로에만** 걸려 있었다.
    저녁에 "CM 익절선 무효 -> 손익비 TP(1:2) 폴백"을 추가하면서 폴백 경로에는
    상한을 안 걸어, 손절폭이 넓은 건은 익절 목표가 ROE 9~10%까지 벌어졌다
    (실측 TUTUSDT 손절-4.39% / 익절+9.99%, LITUSDT +9.38%).
    그런데 원장은 목표가 멀수록 나쁘다고 말한다:
      2~4%   58건 TP체결64% 건당+0.1844   <- 유일한 플러스
      4~5.9% 28건 TP체결36% 건당-0.3630
      5.9~6% 55건 TP체결29% 건당-0.5372
    폴백도 같은 상한을 받아야 일관된다. 익절선 위치만 바꾸므로 거래수는 불변이다.
    """
    if tp <= 0 or entry <= 0 or max_roe_pct <= 0 or leverage <= 0:
        return tp
    roe = ((tp / entry - 1) if side == "LONG" else (1 - tp / entry)) * leverage * 100
    if roe <= max_roe_pct:
        return tp
    d = max_roe_pct / 100.0 / leverage
    return entry * (1 + d) if side == "LONG" else entry * (1 - d)


def cm_tp_price(ind: dict, entry: float, side: str, pullback_pct: float,
                leverage: int = 5, max_roe_pct: float = 0.0) -> float:
    """원칙 0(CM) 최대 익절선에서 pullback_pct 만큼 앞당긴 지정가 TP.

    앞당기는 이유: 극값은 "닿아본 적 있는 값"이라 정확히 그 가격에 지정가를 걸면
    체결이 안 되고 되돌림에 그대로 반납한다. 캐시 실측 체결률 — 0.0% 앞당김 75.2%,
    0.2% 78.6%. 더 크게 앞당기면 체결률은 오르지만 목표가 진입가 아래로 내려가
    무효가 되는 비율이 폭증한다(0.5% -> 무효 83%). 0.2% 가 균형점이다.

    목표가 이미 진입가를 지나쳐 있으면 0 을 돌려준다 — 호출부가 기존 볼밴 TP 로
    폴백한다(캐시 실측 26.5%가 여기 해당). 그 26.5%는 오늘과 동작이 같다.
    """
    if entry <= 0:
        return 0.0
    tgt = float(ind.get("cm_tp_long" if side == "LONG" else "cm_tp_short") or 0.0)
    if tgt <= 0:
        return 0.0
    lim = (tgt * (1 - pullback_pct / 100.0) if side == "LONG"
           else tgt * (1 + pullback_pct / 100.0))
    # [2026-08-26 사용자제안] CM 목표가 너무 멀면 그 목표를 기다리지 않고 상한에서 끊는다.
    # 실원장 100건: 목표 ROE 8% 이상 8건은 승률 0%, 건당 -1.611 (상관 r=-0.261, t=-2.67).
    # 먼 목표는 "많이 벌 자리"가 아니라 "도달 못 하고 되돌아올 자리"였다.
    # 캐시 교차검증(심볼 반분) 앞당김0.5%+상한ROE6%: A +1.05% / B +1.03% (격차 0.01).
    # 거래를 거르는 게 아니라 익절선 위치만 바꾸므로 거래수는 불변이다(원칙 1 무관).
    if max_roe_pct > 0 and leverage > 0:
        roe = ((lim / entry - 1) if side == "LONG" else (1 - lim / entry)) * leverage * 100
        if roe > max_roe_pct:
            d = max_roe_pct / 100.0 / leverage
            lim = entry * (1 + d) if side == "LONG" else entry * (1 - d)
    if (lim <= entry) if side == "LONG" else (lim >= entry):
        return 0.0
    return lim


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


_HEARTBEAT = {"ts": 0.0, "phase": "init"}


def beat(phase: str) -> None:
    """메인 루프가 살아 있다는 표시. 워치독이 이 값을 본다."""
    _HEARTBEAT["ts"] = time.time()
    _HEARTBEAT["phase"] = phase


def start_main_loop_watchdog(stale_sec: float) -> None:
    """메인 루프가 멈추면 프로세스를 스스로 재기동한다.

    [2026-08-26 실사고] 봇이 11:19 에 네트워크 호출에서 블록돼 12분간 아무것도 하지
    않았다(CPU 25초에 0.078초). 죽지 않았으니 아무도 되살리지 않았고, 그 사이 체결된
    ONGUSDT 포지션에 손절이 걸리지 않았다. REST 타임아웃(10초)으로 그 경로는 막았지만
    freeze 를 만드는 원인은 그것 하나가 아니다 - 결과를 보고 되살리는 층이 필요하다.

    WS 레이어에는 워치독이 있었는데 메인 루프에는 없었다.

    e3 는 supervisor 없이 단독 실행이라 같은 인자로 새 프로세스를 띄우고 자신은 죽는다.
    보유 포지션은 상태파일 기반 채택 경로가 이어받는다(이미 검증된 경로).
    """
    if stale_sec <= 0:
        return

    def _watch():
        while True:
            time.sleep(15.0)
            last = float(_HEARTBEAT.get("ts") or 0.0)
            if last <= 0:
                continue
            idle = time.time() - last
            if idle < stale_sec:
                continue
            # [2026-08-26] 12:04 시점에 원인 불명 재기동이 2회 있었는데 워치독 로그가
            # 없었다. 워치독이 범인인지 아닌지조차 가릴 수 없었다. 다음엔 확실히 알도록
            # 런로그와 별개인 전용 파일에도 남긴다(런로그 쓰기가 실패해도 흔적이 남는다).
            _msg = (f"[워치독] 메인 루프 {idle:.0f}초째 진전 없음"
                    f"(마지막 단계={_HEARTBEAT.get('phase')}) - 프로세스 재기동")
            try:
                with (LOG_DIR / "scalp_bot_e3_cm_watchdog.log").open(
                        "a", encoding="utf-8") as _f:
                    _f.write(time.strftime("%Y-%m-%d %H:%M:%S")
                             + f" pid={os.getpid()} {_msg}" + chr(10))
            except Exception:
                pass
            try:
                log_line(_msg)
            except Exception:
                pass
            try:
                release_bot_lock()      # 새 프로세스가 중복으로 판단하지 않도록 먼저 푼다
            except Exception:
                pass
            try:
                kw = {}
                if os.name == "nt":
                    # 부모가 죽어도 살아남도록 분리해서 띄운다.
                    kw["creationflags"] = (subprocess.DETACHED_PROCESS
                                           | subprocess.CREATE_NEW_PROCESS_GROUP)
                else:
                    kw["start_new_session"] = True
                subprocess.Popen([sys.executable] + sys.argv,
                                 cwd=str(LOG_DIR.parent), close_fds=True, **kw)
            except Exception as e:
                try:
                    log_line(f"[워치독] 재기동 실패({e}) - 그대로 종료한다")
                except Exception:
                    pass
            # atexit 을 건너뛴다. 미체결 진입주문은 상태파일에 남아 새 프로세스가 복원한다.
            os._exit(9)

    threading.Thread(target=_watch, name="main-loop-watchdog", daemon=True).start()


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
                "WS_KLINE_HISTORY_LEN": "150", "WS_KLINE_MAX_STALENESS_SEC": "90",
                # 1m cache supplies both the 1m execution view and the 5m CM view.
                "INTERVAL": "1m"})
    proc = subprocess.Popen([sys.executable, "-m", "bot.ws_worker"],
                            cwd=str(Path(__file__).resolve().parent.parent), env=env)
    _record_ws_pid(proc)
    return proc, FileBackedKlineCache(LOG_DIR / "ws_worker_cache.json",
                                      LOG_DIR / "ws_worker_heartbeat.txt",
                                      status_path=LOG_DIR / "ws_worker_status.json")


# [2026-08-28] 정합성 대조는 **회계가 아니라 화재경보**다. 목적은 하나 —
# "원장에 거래가 빠졌는가". (08-21 사고: 손실 19건이 원장에서 빠져 승률이 14%p
# 부풀었는데 몇 시간 뒤에야 발견했다.)
#
# 종전 구현에는 결함 셋이 겹쳐 있었고, 그 결과 **경보가 상시로 울려 무시하게 됐다**:
#   1) 창이 `run_started_at` 부터라 무한히 자란다. futures_income_history 는
#      limit 에서 잘리는데(실측 12h 427행 / 24h 1000행 = 절단) 잘려도 조용히
#      그럴듯한 숫자를 냈다. 하루 이상 가동하면 경고 수치가 허위로 커진다.
#   2) 창 양끝에 걸친 포지션의 **진입 수수료**가 편향을 만든다. 아직 안 닫힌
#      포지션의 수수료는 거래소 income 에만 있고 원장에는 없다(실측 -1.1235).
#      그래서 재시작 직후엔 **항상** 같은 방향으로 경보가 떴다 — 08-28 브리핑
#      5회가 전부 그랬다.
#   3) 스칼라 하나라 무엇이 틀렸는지 알 수 없다. 심볼 단위로 쪼개면 즉시 보인다:
#      실측 19심볼 중 18개가 0.01 이내 일치했고 1건만 어긋났다.
#
# 그래서 (a) 창을 6시간으로 고정해 절단을 피하고(1회 호출), (b) 경계 잡음의
# 근원인 수수료를 **경보에서 빼고** REALIZED_PNL 만 **심볼 단위**로 대조한다.
# 누적 전수 대조는 이 함수가 아니라 scripts/reconcile_realized_pnl.py 의 몫이다.
CHK_WINDOW_SEC = 6 * 3600.0
CHK_LIMIT = 1000
CHK_SYMBOL_EPS = 0.05


def ledger_vs_exchange_report(income_rows, ledger_lines, since: float,
                              now_ts: float, limit: int = CHK_LIMIT) -> dict:
    """거래소 수입내역과 원장을 대조한다. 순수 함수 — I/O 는 호출부가 한다.

    `income_rows` 가 `limit` 행에 닿았으면 합계가 불완전하므로
    **틀린 숫자 대신 `truncated=True`(판정 불가)** 를 낸다.
    """
    w_from = max(float(since), float(now_ts) - CHK_WINDOW_SEC)
    rows = list(income_rows or [])
    ex_pnl: dict = collections.defaultdict(float)
    ex_com = 0.0
    for x in rows:
        try:
            kind = x.get("incomeType")
            val = float(x.get("income", 0) or 0)
        except Exception:
            continue
        if kind == "REALIZED_PNL":
            ex_pnl[str(x.get("symbol") or "")] += val
        elif kind == "COMMISSION":
            ex_com += val
    led_pnl: dict = collections.defaultdict(float)
    led_com = 0.0
    for ln in (ledger_lines or []):
        if not str(ln).strip():
            continue
        try:
            r = json.loads(ln)
        except Exception:
            continue
        if r.get("dry_run") or float(r.get("exited_at", 0) or 0) < w_from:
            continue
        led_pnl[str(r.get("symbol") or "")] += float(r.get("real_realized_pnl", 0) or 0)
        led_com += float(r.get("real_commission", 0) or 0)
    # 08-21 실패 모드: 거래소엔 청산이 있는데 원장에 그 심볼이 아예 없다.
    missing = sorted(s for s in ex_pnl if s and s not in led_pnl)
    mismatch = []
    for s in sorted(set(ex_pnl) | set(led_pnl)):
        diff = ex_pnl.get(s, 0.0) - led_pnl.get(s, 0.0)
        if abs(diff) > CHK_SYMBOL_EPS:
            mismatch.append((s, ex_pnl.get(s, 0.0), led_pnl.get(s, 0.0), diff))
    mismatch.sort(key=lambda t: -abs(t[3]))
    return {
        "truncated": len(rows) >= limit,
        "window_h": max(0.0, (float(now_ts) - w_from) / 3600.0),
        "missing": missing,
        "mismatch": mismatch,
        "exch_pnl": sum(ex_pnl.values()),
        "led_pnl": sum(led_pnl.values()),
        # 수수료는 경계 편향 때문에 경보에 쓰지 않는다. 참고용으로만 돌려준다.
        "exch_com": ex_com,
        "led_com": -led_com,
    }


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
    p = argparse.ArgumentParser(description=f"스캘핑 봇 {VERSION} - CM HMA20/4h EMA200")
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
    # [2026-08-26 P0] 지정가 진입이 일부만 체결되면 잔량을 취소하므로 포지션이 목표보다
    # 작게 남는다(실측 23:17 REUSDT 309 주문 -> 24 체결 = 명목 12.5, POLUSDT 1423 -> 515).
    # 그런데 슬롯은 크기와 무관하게 한 자리를 통째로 쓴다 - 명목 12.5 짜리는 익절해도
    # +0.05, 손절해도 -0.03 이면서 동시보유/방향편중 상한을 정상 크기 진입과 똑같이
    # 점유한다. 증거금 하한을 30 으로 올린 것과 같은 이유로, 실패한 진입은 정리하고
    # 슬롯을 되돌려주는 편이 원칙 1(거래 활발)에 유리하다.
    p.add_argument("--min-fill-ratio", type=float, default=0.5,
                   help="지정가 진입 체결량이 주문량의 이 비율에 못 미치면 그 자리에서 "
                        "시장가로 정리하고 슬롯을 비운다. 0=사용안함.")
    # [2026-08-27 A안] 손절폭을 익절폭(원칙 0 = CM 최대익절선)에 맞춘다.
    # 종전엔 손절선(EMA25 x 확대)이 익절선과 **무관하게** 정해져, CM 목표가 코앞인
    # 심볼에서도 손절만 상한을 꽉 채웠다. 진입 시점 계획 손익비로 원장 225건을 가르면:
    #   0.0~0.4  93건 승률77% 건당-0.035   <- 익절선이 코앞, 거의 다 맞음(본전)
    #   0.4~1.0  96건 승률50% 건당-0.375   <- 손실 -36.00 이 전부 여기 몰림
    #   1.0~1.5  34건 승률50% 건당+0.003
    # 손절폭을 익절폭 이하로 눌러 0.4~1.0 밴드를 1.0 쪽으로 옮긴다.
    # **넓히지는 않는다** - min() 이므로 익절선이 멀어도 손절은 그대로다.
    # [2026-08-27] 지정가 손절. 시장가 손절은 taker(0.05%) + 스프레드 전액을 낸다.
    # 3일 12만건 실측에서 CM 엣지(15분 +0.0866%, 4h필터 적용)가 왕복비용
    # (수수료 0.0552~0.0862% + 스프레드 0.0359%)과 같은 크기라, 비용 절감이
    # 부호를 가르는 유일한 레버로 남았다.
    # **확실한 절감은 아니다** - 손절가는 정의상 현재가의 불리한 쪽이라 트리거 순간
    # 호가에 상대가 있으면 그대로 taker 로 먹힌다. 절감 기대값은 0.03%p 가 아니라
    # 그 일부다. 대신 급락 시 미체결 위험이 새로 생기므로 감시가 필수다.
    p.add_argument("--stop-limit", action="store_true", default=False,
                   help="손절을 STOP_MARKET 대신 스탑-리밋으로 건다(maker 시도).")
    p.add_argument("--stop-limit-slip-pct", type=float, default=0.05,
                   help="스탑-리밋의 지정가를 트리거보다 이 %%만큼 불리하게 둔다. "
                        "0=트리거와 동일(체결률 낮고 절감 큼). 크게 둘수록 체결률은 "
                        "오르지만 taker 가 되어 절감이 사라진다.")
    p.add_argument("--stop-limit-timeout-sec", type=float, default=20.0,
                   help="손절선을 지난 뒤 이 초 안에 체결되지 않으면 시장가로 끊는다.")
    p.add_argument("--stop-limit-fail-pct", type=float, default=0.30,
                   help="손절선을 이 %%(가격 기준) 넘게 지나쳤으면 타임아웃을 기다리지 "
                        "않고 즉시 시장가로 끊는다(급락 보호).")
    # [2026-08-27 안 2] 유리구간 되돌림 청산 — 원칙 2 보강의 구현.
    # "애매한 거래는 빠르게 작은 수익으로 익절하고 종료한다. 본전 되돌림은 손실로
    # 취급한다"(CLAUDE.md). 지금까지 이 축이 비어 있었다 — 익절은 CM 목표 하나뿐이라
    # 그 문턱을 못 넘으면 손절까지 갔다.
    # 계측 정상분 실측: 손실의 86% 가 "유리했다가 되돌린" 거래였다
    #   MFE 0.0~0.5%  1건(14%)  방향오류
    #   MFE 0.5~2.0%  3건(43%)
    #   MFE 2.0~4.3%  3건(43%)  TP 문턱(4.3)을 못 넘고 되돌아섬
    # 3일 하네스 스윕(19,056건): 현행 -0.0585% -> arm 1.0/frac 0.4 에서 -0.0436%.
    # **arm 을 더 낮추면(0.5) 다시 나빠져(-0.0488) 최적점이 안쪽에 있다** —
    # "빨리 자를수록 좋다"는 단순 조기청산과는 다르다는 방증이다.
    # frac 은 0.2~0.6 에서 성능이 평평해 과적합을 피해 중간값을 쓴다.
    #
    # **꺼둔 조기청산과 다른 것이다**: --early-adverse-sec / --mae-cut-roe 는
    # **불리한** 움직임에 반응해 자르는 규칙(사용자가 의도적으로 off).
    # 이건 **유리했던 것을 지키는** 규칙이다.
    # [2026-08-27] 고정폭 손절. >0 이면 EMA25 기준을 쓰지 않고 진입가에서 이 ROE%%
    # 떨어진 곳에 손절을 둔다.
    # 근거(3일 하네스, **공통 표본 19,056건** — 현행 손절이 유효한 거래만):
    #   현행 EMA25x1.65  -0.0436%  SL체결 46%
    #   EMA25 x2.0       -0.0388%       41%
    #   EMA25 x3.0       -0.0270%       31%
    #   고정 ROE 6.0     +0.0069%        8%   t=+1.84
    #   고정 ROE 8.0     +0.0120%        4%   t=+3.11   <- 부호 전환
    # **EMA25 를 3배로 넓혀도 마이너스인데 고정폭은 플러스다.** 문제는 폭이 아니라
    # 기준이었다 - 변동성이 죽으면 EMA25 가 진입가에 달라붙어 손절이 종잇장이 되고
    # 그 거래가 그대로 털린다. 고정폭은 그 얇은 쪽을 넓혀준다.
    # 꼬리도 안전하다: 최악 -8.29% ROE(무손절은 -31.84%).
    # **오전에 시험한 --new-max-stop-roe 8 과 다르다** — 그건 상한이라 얇은 손절은
    # 얇은 채로 남았다. 오전 실패가 이 안을 반증하지 않는다.
    # [2026-08-27] HullMA 방향 전환 후 N봉 이내만 진입한다.
    # 3일 x 85심볼 실측(보유 15분 고정, 수수료 차감 후 순익) — 4h 정합 신호를
    # 전환 후 경과봉으로 가르면:
    #   정합/전환후0봉  4,872건 +0.0302%  t+16.0
    #   정합/전환후1봉  4,329건 +0.0470%  t+17.5
    #   정합/전환후2봉  3,917건 +0.0580%  t+18.5
    #   정합/지속      23,808건 -0.0236%  t+17.9   <- 전체의 64%인데 마이너스
    # **"정합 전체 +0.0004%" 는 좋은 1/3 과 나쁜 2/3 의 평균이었다.**
    # 원칙 1 검토(가장 중요) — 거래수가 줄어드는가:
    #   실효공급 = 신호 x 눌림체결률.  현행 37,151 x 33.3% = 172/h (봇 18/h 의 9.5배)
    #              전환2봉  13,235 x 40.2% =  74/h (4.1배)
    #   **체결률이 오히려 오른다**(33.3 -> 40.2%). 방향이 막 바뀐 자리는 되돌림이 잘 온다.
    #   그리고 진입 속도를 정하는 것은 공급이 아니라 슬롯이다 — 같은방향 상한(3)에
    #   시간의 73% 를 붙어 있고 동시보유는 평균 2.9/10 이다. 3분봉당 후보가 26 -> 8 개로
    #   줄어도 봇이 3분에 넣는 건 0.9건이라 병목이 옮겨오지 않는다.
    # 한계: 저유동성 시간대는 3분봉당 후보 p10 이 2개라 여유가 얇다.
    p.add_argument("--cm-flip-max-bars", type=int, default=-1,
                   help="HullMA 방향 전환 후 이 봉수 이내의 신호만 받는다. -1=제한없음")
    p.add_argument("--cm-recheck-on-entry", action="store_true",
                   help="지정가 발주 직전에 CM 방향/4h/flip을 재확인한다")
    p.add_argument("--same-side-stop-cooldown-sec", type=float, default=0.0,
                   help="최근 같은 방향 STOP_EXCHANGE 횟수가 2건 이상이면 신규 진입을 잠시 막는다")
    p.add_argument("--stop-fixed-roe", type=float, default=0.0,
                   help="손절을 진입가 기준 고정 ROE%%로 둔다(EMA25 무시). 0=사용안함")
    p.add_argument("--giveback-arm-roe", type=float, default=0.0,
                   help="보유 중 최고 ROE(MFE)가 이 %%를 넘으면 되돌림 청산을 무장한다. "
                        "0=사용안함")
    # [2026-08-27] 되돌림 청산을 시장가 대신 **지정가로 먼저** 시도한다.
    # 새 설정에서 청산의 68%가 taker 다(GB 43 + 만료 21 + SL 4). 왕복 수수료
    # 기대값 0.0604%. GB 43% 를 maker 로 바꾸면 0.0475% 로 내려가고
    # 건당 순익이 +0.0120% -> +0.0249% 로 두 배가 된다(하루 +8.3 -> +17.2 USDT).
    # 미체결 위험은 --stop-limit 과 같은 방식으로 감시한다(마감 후 시장가 전환).
    # 추가 증거금이 필요 없다 - 청산 주문의 종류만 바뀐다.
    p.add_argument("--giveback-limit-sec", type=float, default=20.0,
                   help="되돌림 청산을 지정가로 걸고 이 초 안에 체결되지 않으면 "
                        "시장가로 전환한다. 0=처음부터 시장가")
    p.add_argument("--giveback-frac", type=float, default=0.4,
                   help="무장 후 고점 대비 이 비율만큼 되돌리면 시장가로 청산한다. "
                        "0.4 = 고점의 40%% 반납")
    # [2026-08-27] 그림자(shadow) 인스턴스 — 설정 A/B 를 **봇 코드 그대로** 비교한다.
    # 하네스는 오늘 세 번 고쳐도 라이브를 재현하지 못했다(TP 체결률 32% vs 57%).
    # 근본 원인은 모델링 실수가 아니라 **모집단이 다른 것**이다:
    #   하네스는 신호 전부를 거래하고, 라이브는 1.7%(시간당 1,097신호 -> 18건)만 잡는다.
    #   그 1.7% 는 슬롯 여유·쿨다운·방향편중·TTL·스캔순서가 고른 편향된 부분집합이다.
    # 봇 코드를 그대로 dry-run 으로 돌리면 그 선택 과정이 전부 재현되므로
    # **모델링 오차가 정의상 0** 이다.
    #
    # 안전장치 셋 (라이브를 절대 건드리면 안 된다):
    #   1) --instance-tag 로 원장/상태/로그/PID 파일을 전부 분리
    #   2) --attach-ws 로 **워커를 새로 띄우지 않고** 라이브 워커의 캐시 파일에 붙는다.
    #      이게 없으면 cleanup_tracked_ws_worker() 가 공유 PID 파일을 보고
    #      **라이브의 WS 워커를 죽인다.**
    #   3) --dry-run 이면 봇 락을 잡지 않으므로 라이브와 공존한다(기존 동작)
    p.add_argument("--instance-tag", type=str, default="",
                   help="원장/상태/로그/PID 파일 이름에 붙일 꼬리표. 그림자 인스턴스용.")
    p.add_argument("--attach-ws", action="store_true",
                   help="WS 워커를 새로 띄우지 않고 이미 도는 워커의 캐시 파일에 붙는다. "
                        "그림자 인스턴스는 반드시 이걸 쓴다(라이브 워커를 죽이지 않도록).")
    p.add_argument("--start-paused", action="store_true",
                   help="일시정지 상태로 기동한다(신규 진입만 멈추고 보유분 손절/익절은 "
                        "계속 관리). 텔레그램 '▶️ e3재개' 로 풀 수 있다.")
    p.add_argument("--stop-rr-match", type=float, default=1.0,
                   help="손절폭 <= 익절폭 / 이 값. 1.0 이면 손절폭을 익절폭 이하로 맞춘다. "
                        "0=사용안함. CM 익절선이 무효(손익비 폴백)면 적용하지 않는다 - "
                        "그 폴백 익절선은 손절선에서 역산한 값이라 순환이 된다.")
    p.add_argument("--stop-match-floor-roe", type=float, default=2.5,
                   help="위 축소의 하한(ROE %%). 익절선이 코앞인 거래까지 손절을 조이면 "
                        "노이즈에 즉시 털린다. 이 아래로는 안 좁힌다.")
    p.add_argument("--min-entry-edge-pct", type=float, default=0.0,
                   help="진입가가 **신호 발생 봉의 종가**보다 이 %% 이상 유리해야 정상 크기로 "
                        "들어간다. 미달이면 --entry-edge-size-mult 배율로 축소(차단하지 않음). "
                        "0=사용안함. 원장 111건 실측 - edge>=0.3%% 39건 승률87.2%% 건당+0.7242 vs "
                        "edge<0.3%% 72건 건당-0.2274. 구간 교차검증 3/3 개선, "
                        "심볼 반분 A/B 격차 0.0108(채택 기준 0.01 수준).")
    p.add_argument("--entry-edge-size-mult", type=float, default=0.3,
                   help="진입 우위가 --min-entry-edge-pct 에 미달할 때의 증거금 배율. "
                        "차단(0)하면 거래수가 65%% 줄어 원칙 1(14건/h 하한)을 위반한다. "
                        "0.3 이면 거래수를 그대로 두고 차단 효과의 70%%를 얻는다.")
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
    # [2026-08-25 B안] 진입 지정가를 몇 초까지 살려둘지. 예전엔 10초를 그 자리에서
    # 블로킹하며 기다렸는데, 이제 주문만 내고 다음 루프에서 확인하므로 길게 줘도
    # 청산 폴링이 밀리지 않는다. 3분봉 전략이라 봉 하나(180초)를 넘기지 않는 게 자연스럽다.
    p.add_argument("--entry-order-ttl-sec", type=float, default=45.0,
                   help="진입 지정가를 살려두는 시간(초). 초과하면 취소하고 포기")
    p.add_argument("--entry-depth-pct", type=float, default=0.0,
                   help="진입 목표선을 EMA 에서 이만큼 더 깊게(가격 %%). 0=기존")
    # [2026-08-21] 익절 ROE 하한. 볼밴선과 하한선 중 더 먼 쪽을 쓴다.
    # [2026-08-21 사용자요청] 신호 판정 봉 길이(분). 1=기존 1분봉.
    # 1분봉을 합쳐서 만들므로 WS 캐시를 그대로 쓴다(추가 API 호출 없음).
    p.add_argument("--signal-tf-min", type=int, default=15,
                   help="CM 신호 판정 봉 길이(분). 기본 15분")
    p.add_argument("--confirm-tf-min", type=int, default=0,
                   help="추가 CM 방향 확인 봉 길이(분). 0이면 사용 안 함")
    p.add_argument("--entry-tf-min", type=int, default=0,
                   help="눌림 실행용 봉 길이(분). 0이면 신호 봉 사용")
    p.add_argument("--cm-use-alt-resolution", action="store_true",
                   help="TradingView CM의 Use Current Chart Resolution?을 끄고 res_custom을 security()로 투영")
    p.add_argument("--cm-res-custom", type=str, default="D",
                   help="TradingView CM MTF 해상도. 예: 3, 15, 60, 240, D, W")
    p.add_argument("--cm-len", type=int, default=20, help="CM 1st MA 길이")
    p.add_argument("--cm-atype", type=int, default=1,
                   help="CM 1st MA 타입 1=SMA 2=EMA 3=WMA 4=HMA 5=VWMA 6=RMA 7=TEMA 8=T3")
    p.add_argument("--cm-factor-t3", type=int, default=7, help="CM 1st MA Tilson T3 factor input")
    p.add_argument("--cm-smoothe", type=int, default=2, help="CM Color Smoothing 길이")
    p.add_argument("--cm-enable-second-ma", action="store_true",
                   help="CM 2nd MA를 같이 계산하고 진입 필터에 사용")
    p.add_argument("--cm-len2", type=int, default=50, help="CM 2nd MA 길이")
    p.add_argument("--cm-atype2", type=int, default=1,
                   help="CM 2nd MA 타입 1=SMA 2=EMA 3=WMA 4=HMA 5=VWMA 6=RMA 7=TEMA 8=T3")
    p.add_argument("--cm-factor-t3-second", type=int, default=7,
                   help="CM 2nd MA Tilson T3 factor input")
    p.add_argument("--cm-show-price-crossing", action="store_true",
                   help="원본 Pine 입력값 보존용. 현재 진입 판정에는 직접 쓰지 않는다")
    p.add_argument("--cm-show-price-crossing-second", action="store_true",
                   help="원본 Pine 입력값 보존용. 현재 진입 판정에는 직접 쓰지 않는다")
    p.add_argument("--cm-show-cross-dots", action="store_true",
                   help="원본 Pine 입력값 보존용. 현재 진입 판정에는 직접 쓰지 않는다")
    p.add_argument("--cm-disable-color-direction", action="store_true",
                   help="원본 Pine cc=false 대응")
    p.add_argument("--cm-disable-color-direction-second", action="store_true",
                   help="원본 Pine cc2=false 대응")
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
    p.add_argument("--min-leg-margin", type=float, default=35.0,
                   help="e3 슬롯 1차 진입 최소 증거금(USDT). 기본 35")
    # [2026-08-25 버그수정] --min-leg-margin 은 "하한"일 뿐이고 실제 크기는
    # `잔고 x max_exposure / slots` 다. slots 는 int(잔고*노출//하한) 이라 잔고가 줄면
    # 함께 줄고, 그러면 건당 증거금이 오히려 커진다 — 진입할수록 다음 진입이 커지는
    # 양성 피드백이다. 실측: 하한 35로 돌렸는데 PUMPUSDT 에 81.37 이 들어갔고(잔고의 절반)
    # 그 직후 TRUMPUSDT 가 "Margin is insufficient" 로 실패했다.
    # 상한을 두어 베팅액을 고정한다. 0이면 기존 동작(상한 없음).
    # [2026-08-25] 채택(재시작 시 기존 포지션 인수) 손절폭 상한(ROE%).
    # 채택 손절은 EMA25 로 잡는데 상한이 없어, 이미 EMA 에서 멀어진 포지션은 손절도 멀다.
    # 실측: ONGUSDT 채택분이 가격 -3.53% x 레버 5 = ROE -17.65% 에서 잘려 -6.36 USDT.
    # 채택 4건이 e3 손실의 59%였고, 그 4건의 MFE 는 +0.11/+0.66/+0.00/+0.84% 로
    # 어떤 상한을 걸어도 익절 기회를 자르지 않는다. 봇 자체 진입(손절 중앙 -2.31%,
    # 최악 -4.25%)은 이 상한에 애초에 안 걸리므로 영향이 없다. 0=상한 없음.
    p.add_argument("--adopt-max-stop-roe", type=float, default=0.0,
                   help="채택 포지션 손절폭 상한(ROE%%). 0=상한 없음")
    p.add_argument("--max-leg-margin", type=float, default=0.0,
                   help="1차당 증거금 상한(USDT). 0=상한 없음. min 과 같게 두면 고정 크기")
    p.add_argument("--max-new-orders-per-cycle", type=int, default=0,
                   help="한 스캔 사이클 신규 진입 발주 상한. 0=무제한")
    p.add_argument("--adopt-unowned-positions", action="store_true", default=False,
                   help="상태파일에 없는 계좌 포지션도 채택. 기본값은 수동 포지션 보호를 위해 비활성")
    p.add_argument("--max-exposure", type=float, default=0.95)
    p.add_argument("--min-notional", type=float, default=5.0)
    p.add_argument("--poll", type=float, default=10.0)
    # [2026-08-25] 원칙 0(CM) 기반 지정가 TP. 캐시 3분봉 1,943 진입 A/B 결과
    # 건당 ROE -0.35% -> +0.38%, 승률 52.9% -> 75.6%, 체결률 43.1% -> 82.5%.
    p.add_argument("--cm-tp-limit", action="store_true", default=True,
                   help="CM 최대익절선 기준 reduceOnly 지정가 TP 를 거래소에 등록")
    p.add_argument("--no-cm-tp-limit", dest="cm_tp_limit", action="store_false",
                   help="끄면 기존 볼밴/RR 폴링 익절만 쓴다(원복용)")
    # [2026-08-25 사용자요청] 텔레그램 누적 표시를 특정 시점부터 다시 센다.
    # 원장 파일은 그대로 두고 "표시"만 리셋한다 - 분석/백테스트는 전체 원장을 계속 쓴다.
    # "now" = 기동 시각, 또는 "YYYY-MM-DD HH:MM", 또는 epoch 초.
    p.add_argument("--stats-since", type=str, default="",
                   help='누적 집계 시작 시점. "now" 또는 "YYYY-MM-DD HH:MM" 또는 epoch')
    p.add_argument("--max-pullback-pct", type=float, default=0.0,
                   help="진입가가 HullMA20 에서 이 %%보다 멀면 진입 안 함. 0이면 끔. 권장 0.5")
    p.add_argument("--watchdog-sec", type=float, default=180.0,
                   help="메인 루프가 이 초만큼 진전이 없으면 프로세스를 재기동. 0이면 끔")
    p.add_argument("--max-same-side", type=int, default=0,
                   help="같은 방향 동시보유 슬롯 상한(미체결 진입주문 포함). 0이면 끔. 권장 3")
    p.add_argument("--new-max-stop-roe", type=float, default=0.0,
                   help="신규 진입 손절폭 ROE 상한(%%). 0 이면 끔. 안1 권장값 5")
    # [2026-08-26] 0.2 -> 0.5. 캐시 104건(눌림필터 적용 후) TP체결 47.1% -> 65.4%,
    # 승률 50.0% -> 65.4%, 건당 +0.39% -> +0.65%. 1.0% 까지 키우면 수수료를 못 넘어 -0.18%.
    p.add_argument("--min-stop-roe", type=float, default=0.0,
                   help="진입 시점 손절선까지의 거리가 이 ROE%% 미만이면 진입하지 않는다. "
                        "0=사용안함. 원장 355건 재계산 실측 - 손절폭이 좁을수록 나쁘다: "
                        "0~2%% 65건 승률40.0%% 건당-0.1367 / 2~4%% 139건 승률41.0%% "
                        "건당-0.2517(전체 손실의 57%%) / 4~6%% 76건 승률59.2%% -0.0942 / "
                        "6~9%% 48건 승률60.4%% -0.1150. STOP률도 60%% -> 40%%대로 갈린다. "
                        "좁은 손절은 노이즈에 털린다. 반대로 **상한**은 해롭다 - "
                        "어떤 상한값도 잔존 건당을 개선하지 못했다(안 1 이 실패한 이유).")
    p.add_argument("--cm-htf-filter", action="store_true", default=False,
                   help="원칙 0 의 상위 타임프레임 추세 확인을 켠다. 상위 TF 종가가 EMA "
                        "위면 LONG, 아래면 SHORT 가 '정합'이고 반대는 '역행'이다. "
                        "역행은 **차단하지 않고 크기만 줄인다**(--cm-htf-counter-mult).")
    p.add_argument("--cm-htf-counter-mult", type=float, default=0.3,
                   help="상위추세 역행 진입의 증거금 배율. 0 이면 차단. "
                        "원장 333건 실측 - 정합 164건 건당-0.0826 / "
                        "역행 169건 건당-0.2393(t=-2.71), 전체 손실의 76%% 가 역행. "
                        "차단(0)은 총손익이 가장 좋지만 거래수가 -51%% 라 원칙 1 을 "
                        "정면으로 해친다. 0.3 배는 거래수를 그대로 두고 총손익 "
                        "-53.99 -> -25.68, 건당 -0.1622 -> -0.0771 로 개선한다. "
                        "(전례: CLAUDE.md SHORT_REVERSAL_RISK_SIZE_MULT 도 같은 판단 - "
                        "'거래수를 줄이므로' 차단 대신 비중만 축소했다.) "
                        "이격 임계로 역행을 세분하려 했으나 역행의 86%%가 이미 EMA200 "
                        "에서 10%% 이상 떨어져 있어 구분되지 않았다.")
    p.add_argument("--cm-htf-interval", type=str, default="4h",
                   help="상위 타임프레임 봉 간격(원칙 0 기준 4h)")
    p.add_argument("--cm-htf-ema", type=int, default=200,
                   help="상위 타임프레임 EMA 길이(원칙 0 기준 200)")
    p.add_argument("--require-cm-tp", action="store_true", default=False,
                   help="CM 최대익절선이 무효(목표가 이미 진입가를 지나침)면 진입하지 않는다. "
                        "원장 실측(④ 이후 봇 진입 57건, 재시작 채택분 제외): "
                        "CM TP 등록됨 34건 승률58.8%% 건당-0.068 vs "
                        "무효 23건 승률21.7%% 건당-0.461(t=-2.19). "
                        "두 시간구간 교차검증 통과(좋았던 구간 41.7%%/-0.053, "
                        "나빴던 구간 0.0%%/-0.905). 거래수 -40%% 대신 합계 -12.92 -> -2.33.")
    p.add_argument("--cm-tp-pullback-pct", type=float, default=0.5,
                   help="CM 최대익절선에서 앞당길 폭(가격 %%). 0.5~0.7 이 최적 구간")
    p.add_argument("--cm-tp-max-roe", type=float, default=0.0,
                   help="CM 익절선이 이 ROE(%%)보다 멀면 여기서 끊는다. 0이면 끔. 권장 6")
    p.add_argument("--cm-invalid-tp-size-mult", type=float, default=1.0,
                   help="CM TP 무효 거래 증거금 배율. 기본 1.0=기존 동작")
    p.add_argument("--cm-invalid-tp-min-margin", type=float, default=0.0,
                   help="CM TP 무효 거래 전용 증거금 하한. 0=일반 하한")
    p.add_argument("--bar-align", action="store_true", default=True,
                   help="매 분 00초 직후에 스캔한다. e1 실측: 진입 지연이 5초를 넘으면 "
                        "우위가 사라졌다(0초 +0.0750%%, 5초 -0.0079%%, 30초 -0.0997%%).")
    p.add_argument("--no-bar-align", dest="bar_align", action="store_false")
    # [2026-08-26] 5.0 -> 20.0. age 는 `time.time() % 60`, 즉 '매 분의 앞 N초'다.
    # 5초면 창이 8.3% 뿐인데 루프 주기가 10초라 절반 가까운 분은 창에 아예 못 들어간다.
    # 실측(10:10~10:22): 스킵 250건 중 신호노후 92건(37%)으로 최대 병목이었고,
    # 같은 구간 진입은 1건뿐이었다. 신호는 3분봉이라 20초 지연은 품질에 영향이 없다.
    # [2026-08-26 재조정] 20 -> 10. "품질 영향 없다"는 처음 판단은 근거가 없었다.
    # 원장 실측: age 0~5초 120건 승률53.3% 건당-0.080 vs 5~20초 34건 승률35.3% -0.580,
    # 청산사유도 STOP 48% -> 69% 로 크게 나빠진다. 다만 원장의 entered_at 은 주문 시각이
    # 아니라 체결 확인 시각이라 "체결이 늦은 주문"과 "신호가 낡은 주문"이 섞여 있어
    # 인과를 가릴 수 없다. 실제 돈이 걸린 문제이므로 안전한 쪽으로 잡는다.
    # 스캔 소요시간 실측(진입 로그 초 분포): 0~4초 100건 / 5~9초 58건 / 10초+ 5건.
    # 85심볼 1회전에 약 10초가 걸려 뒷줄 심볼이 구조적으로 탈락하던 것이 병목이었다.
    # 10초면 스캔의 97%를 커버한다 - 20초까지 넓혀도 5건 더 얻으려고 위험구간만 늘린다.
    p.add_argument("--max-signal-age", type=float, default=10.0,
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

    args.stats_since_ts = 0.0
    if args.stats_since:
        _v = args.stats_since.strip()
        if _v.lower() == "now":
            args.stats_since_ts = time.time()
        else:
            try:
                args.stats_since_ts = float(_v)
            except ValueError:
                try:
                    args.stats_since_ts = time.mktime(
                        time.strptime(_v, "%Y-%m-%d %H:%M"))
                except ValueError:
                    print(f"[중단] --stats-since 해석 실패: {_v}")
                    return 2

    if not args.dry_run and not args.i_know_it_loses:
        print("[중단] 이 전략은 85심볼 10일 검증에서 6개 변형 전부 마이너스였다.")
        print("       거래당 -0.0296% ~ -0.0669% (표본 8만~13만건)")
        print("       --dry-run 으로 동작만 보거나, 그래도 실주문하려면 --i-know-it-loses")
        return 1

    if not args.dry_run and not acquire_bot_lock():
        return 1

    # 하트비트를 먼저 찍어야 기동이 느릴 때 워치독이 오작동하지 않는다.
    beat("startup")
    if not args.dry_run:
        start_main_loop_watchdog(args.watchdog_sec)

    cfg = Config()
    if args.instance_tag:
        # 라이브와 파일이 하나라도 겹치면 원장 오염 / 상태 충돌 / 로그 뒤섞임이 난다.
        global LEDGER, STATE, WS_PID_FILE, BOT_PID_FILE, RUN_LOG
        _t = re.sub(r"[^A-Za-z0-9_-]", "", args.instance_tag)
        LEDGER = LOG_DIR / f"scalp_bot_e3_cm_ledger_{_t}.jsonl"
        STATE = LOG_DIR / f"scalp_bot_e3_cm_state_{_t}.json"
        WS_PID_FILE = LOG_DIR / f"scalp_bot_e3_cm_ws_pid_{_t}.json"
        BOT_PID_FILE = LOG_DIR / f"scalp_bot_e3_cm_bot_pid_{_t}.json"
        RUN_LOG = LOG_DIR / f"scalp_bot_e3_cm_run_{_t}.log"
    if args.roundtrip_fee_rate is None:
        args.roundtrip_fee_rate = float(getattr(cfg, "fee_rate_roundtrip", 0.001))
    # [2026-08-26 P0] 종전 고정 90초. `is_fresh()` 가 보는 `_last_update_ts` 는 **봉이
    # 갱신될 때** 찍히는데, 신호봉이 3분이면 그 간격이 180초다. 90초 기준이면
    # **매 봉 주기의 절반 동안 모든 심볼이 "낡음"으로 판정**된다. 그 구간에서는
    # get_klines 가 85심볼 전부 REST 로 폴백하고(IP밴 위험), 기동 시에는 캐너리가
    # 0/10 이라 "WS 준비 미완료 - 강제 진행" 경고를 내며 REST 로 운영된다.
    # 실측(14:04 캐시 덤프): 85심볼 전부 99봉 보유, 메시지 4651/60초, 에러 0 —
    # **캐시는 완전히 정상인데 판정 기준만 틀렸다.** 기동 성공/실패가 반반이었던 것도
    # 확인 시점이 봉 주기의 앞쪽이냐 뒤쪽이냐에 따라 갈렸기 때문이다.
    # 봉 간격 + 여유 2분으로 잡는다(3분봉 -> 300초).
    cfg.ws_kline_max_staleness_sec = max(90.0, args.signal_tf_min * 60.0 + 120.0)
    ex = Exchange(cfg)
    tg = None if args.no_telegram else Tg(cfg)

    def say(msg, tg_send=True):
        # Hidden Start-Process may expose an invalid stdout handle. Logging and
        # Telegram must remain alive even when console output is unavailable.
        try:
            try:
                print(msg, flush=True)
            except UnicodeEncodeError:
                # Windows cp949 콘솔에서 거래소 심볼의 비ASCII 표시명이 봇을 죽이지 않게 한다.
                print(str(msg).encode("ascii", "backslashreplace").decode("ascii"), flush=True)
        except OSError:
            pass
        log_line(str(msg))          # [2026-08-25] 런처와 무관하게 파일에도 남긴다
        if tg and tg_send:
            tg.send(f"[{VERSION}] {msg}")

    bal0 = args.base_balance if args.base_balance > 0 else ex.get_total_margin_balance()
    symbols = (ex.get_active_usdt_perpetual_symbols(limit=args.symbols)
               if cfg.auto_symbols else list(cfg.symbols)[: args.symbols])
    mode = "DRY-RUN(주문없음)" if args.dry_run else "실주문"
    say(f"재시작/기동 감지 [{mode}] 잔고 {bal0:.4f} / {args.leverage}배 / "
        f"{len(symbols)}심볼 / 분할{args.tranches}차 / 손익비{args.rr}")

    # [2026-08-25 B안] 봇이 죽었는데 진입 지정가가 남아 있으면 나중에 예기치 않게 체결된다.
    # 종료 시 반드시 정리한다.
    def _cancel_open_entry_orders():
        # entry_orders 가 아직 정의되기 전에 종료될 수도 있다(초기화 중 예외 등).
        try:
            _items = list(entry_orders.items())
        except NameError:
            return
        for _s, _o in _items:
            try:
                ex.cancel_regular_order(_s, _o["order_id"])
                log_line(f"종료 정리: 진입 지정가 취소 {_s}")
            except Exception:
                pass
        try:
            entry_orders.clear()
        except Exception:
            pass

    atexit.register(_cancel_open_entry_orders)

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
    if args.ws and args.attach_ws:
        # 워커를 띄우지 않는다. 이미 도는 워커가 쓰는 캐시 파일에 읽기로만 붙는다.
        ws_proc = None
        ws_cache = FileBackedKlineCache(LOG_DIR / "ws_worker_cache.json",
                                        LOG_DIR / "ws_worker_heartbeat.txt",
                                        status_path=LOG_DIR / "ws_worker_status.json")
        ex.set_ws_kline_cache(ws_cache)
        say("WS 부착 모드 - 기존 워커의 캐시를 읽기로만 사용(워커를 띄우지 않음)")
    elif args.ws:
        _old_ws = cleanup_tracked_ws_worker()
        if _old_ws:
            say(f"이전 e2 WS 워커 {_old_ws}건 정리 후 재기동")
        ws_proc, ws_cache = start_ws(symbols)
        ws_ready_need = min(8, len(symbols[:10]))
        ws_ready_deadline = time.time() + 100.0
        ws_next_check_at = time.time()
        say("WS 워커 기동 - 준비 상태 확인 시작 (최대 100초). "
            "준비 전에는 보유 포지션 관리만 하고 신규/추가 진입은 막습니다")

    # [2026-08-26] 상위추세 캐시는 백그라운드에서만 채운다(스캔 루프 비블로킹).
    if args.cm_htf_filter:
        for _s in symbols:
            _HTF_WANTED.add(_s)
        start_htf_refresher(ex, args.cm_htf_interval, args.cm_htf_ema, say)
    say(f"시작 [{mode}] CM방향+눌림실행 / 잔고 {bal0:.4f} / {args.leverage}배 / "
        f"{len(symbols)}심볼 / 분할{args.tranches}차 / 손익비{args.rr}")

    positions: dict[str, Pos] = {}
    pending: dict[str, dict] = {}     # 정배열 확인 후 눌림 대기 중
    # [2026-08-25 B안] 체결 대기 중인 진입 지정가. 주문만 내고 다음 루프에서 확인한다.
    # {sym: {order_id, side, qty, stop, bb_target, since_id, legs, placed_at, price}}
    entry_orders: dict[str, dict] = {}
    _restored_orders: dict = {}
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
    _swept_prev: set = set()
    if STATE.exists():
        try:
            _st = json.loads(STATE.read_text(encoding="utf-8"))
            _owned = set(_st.get("symbols", []))
            # [2026-08-26 P0] 미체결 진입주문을 낸 심볼도 '우리 것'이다.
            # 실사고 ONGUSDT: 10:06:12 에 지정가 진입이 체결됐는데 봇이 그 체결을 확인하기
            # 전에 재시작됐다. 새 프로세스는 상태파일에 ONGUSDT 가 없으니 "e2 소유 아님"
            # 으로 분류해 채택에서 제외했고, 그 포지션은 SL 도 TP 도 없이 방치됐다.
            # entry_orders 는 메모리에만 있어 재시작으로 통째로 증발한 것이 원인이다.
            _owned |= set(_st.get("entry_order_symbols", []))
            _swept_prev = set(_st.get("swept", []))   # [2026-08-26] 안전망 채택 이력
            # [2026-08-26 P0-2] 주문 내용까지 복원해야 새 프로세스가 체결을 확인하고
            # 손절을 걸 수 있다. taskkill /F 는 atexit 을 건너뛰므로 진입주문이 살아남아
            # 재시작 뒤에 체결된다 - 그때 이 복원이 없으면 무보호 포지션이 된다.
            # 실사고: POL/STAR/STX/VIRTUAL 4건이 SL 없이 방치됐다.
            _restored_orders = _st.get("entry_orders") or {}
        except Exception:
            _owned, _owned_at, _swept_prev = set(), {}, set()

    def save_state():
        """소유 심볼과 진입시각을 남긴다. 진입시각이 있어야 재시작해도 보유시간과
        시간손절(--max-hold-sec)이 이어진다.
        [2026-08-20] 폴백인 positionAmt.updateTime 은 '마지막 변경 시각'이라
        3분할 포지션에서는 3차 진입 시각이 잡힌다. 1차 시각은 여기에만 있다."""
        try:
            STATE.write_text(json.dumps(
                {"symbols": sorted(positions),
                 # 아직 체결 확인이 안 된 진입주문도 남긴다. 이게 없으면 체결 직후
                 # 재시작 시 무보호 고아 포지션이 된다(ONGUSDT 실사고).
                 "entry_order_symbols": sorted(entry_orders),
                 # [2026-08-26 P0-2] 심볼 목록만으로는 부족했다. 새 프로세스가 체결을
                 # 확인하려면 order_id/side/stop 등 주문 내용 전체가 필요하다.
                 "entry_orders": entry_orders,
                 "entered_at": {k: v.entered_at for k, v in positions.items()},
                 # [2026-08-26 안B 버그] `swept`(안전망이 주워온 포지션) 표식이
                 # 상태파일에 없어서 **재시작할 때마다 초기화**됐다. 새 프로세스는
                 # 거래소 포지션을 startup 채택 경로로 다시 만드는데 그 경로가
                 # swept=False 라, 안전망 채택분이 방향편중 카운트로 되살아났다.
                 # 실측(13:21 진단): positions=[STORJ:SHORT, LDO:SHORT, ENA:SHORT]
                 # 중 LDO/ENA 는 직전 프로세스에서 swept 였는데 표식이 사라져 ssc=3
                 # 으로 상한에 걸렸다. 오늘 재시작이 6회라 안 B 가 유지된 적이 없다.
                 "swept": sorted(k for k, v in positions.items() if v.swept),
                 "legs": {k: len(v.legs) for k, v in positions.items()}},
                ensure_ascii=False), encoding="utf-8")
        except Exception:
            pass

    def rebuild_excursion(sym: str, side: str, entry: float, lev: int,
                          t_in: float, t_out: float) -> tuple[float, float, str]:
        """청산 후 1분봉 고저로 MAE/MFE 를 되짚어 계산한다. (MAE, MFE) 를 ROE %% 로 돌려준다.

        [2026-08-27 P0] 종전엔 **메인 사이클(분 단위) 폴링으로만** 갱신했다. 그래서
        1분 안에 끝난 거래 52건 중 48건(92%)이 계측 없이 0 으로 기록됐고,
        `MFE < 실현 ROE` 라는 모순이 58건 나왔다(TACUSDT 실현 +6.56%인데 MFE 0.00).
        **누락이 무작위가 아니라는 게 핵심이다** — 짧은 거래는 익절이 몰린 구간이라
        TP_LIMIT 기록률 55% vs STOP 76% 로 **승자만 체계적으로 빠졌다**. 그 데이터로
        승자 MAE 를 재면 오래 끈 승자만 남아 실제보다 나쁘게 나오고, 손절폭 판단이
        바로 그 값에 기대고 있었다.

        1분봉 고가/저가를 쓰므로 1분 폴링보다 오히려 정확하다(분 사이 극값까지 잡는다).
        WS 캐시가 1분봉 200개를 들고 있어 **REST 호출이 늘지 않는다**(IP밴 사고 예방).
        실패하면 (0.0, 0.0) 을 돌려주고 호출부가 폴링값을 그대로 쓴다.
        """
        if entry <= 0 or t_out <= t_in:
            return 0.0, 0.0, "none"
        try:
            df = ex.get_klines(sym, limit=WS_KLINE_CACHE_LEN, interval="1m")
        except Exception:
            return 0.0, 0.0, "none"
        if df is None or len(df) == 0:
            return 0.0, 0.0, "none"
        try:
            import pandas as _pd
            # [2026-08-27 버그] `astype("int64")` 의 단위는 dtype 에 따라 다르다.
            # get_klines 는 datetime64[**ms**] 를 주는데 10**9 로 나눠 초로 바꾸려 해서
            # 값이 1787 같은 쓰레기가 됐고, 마스크가 항상 0개라 복원이 전부 실패했다
            # (excursion_src 가 계속 "none"). 단위에 의존하지 않게 초로 먼저 내린다.
            ot = _pd.to_datetime(df["open_time"]).astype("datetime64[s]").astype("int64")
            m = (ot >= int(t_in) - 60) & (ot <= int(t_out))
            n_bars = int(m.sum())
            if n_bars <= 0:
                return 0.0, 0.0, "none"
            hi = float(df.loc[m, "high"].max())
            lo = float(df.loc[m, "low"].min())
        except Exception:
            return 0.0, 0.0, "none"
        if not (hi > 0 and lo > 0):
            return 0.0, 0.0, "none"
        L = side == "LONG"
        best = ((hi / entry - 1) if L else (1 - lo / entry)) * lev * 100
        worst = ((lo / entry - 1) if L else (1 - hi / entry)) * lev * 100
        # [정밀도] 봉 하나가 보유 구간보다 길면 **진입 전/청산 후 움직임까지** 섞인다.
        # 실측 BTRUSDT: 보유 0.1분인데 복원 MFE 14.49% - 그 봉 전체의 폭이다.
        # 이런 건을 다른 건과 섞어 평균 내면 승자 MFE 가 통째로 부풀어 판단을 망친다.
        # 값은 남기되 출처를 표시해 분석에서 가릴 수 있게 한다.
        src = "bars" if (t_out - t_in) >= 60.0 and n_bars >= 2 else "bars_coarse"
        return min(0.0, worst), max(0.0, best), src

    def excursion_for(pos, t_out: float) -> tuple[float, float, str]:
        """폴링값과 봉 복원값 중 더 극단인 쪽을 쓴다(둘 다 하한이지 상한이 아니다)."""
        mae, mfe, src = rebuild_excursion(pos.symbol, pos.side, pos.entry,
                                          pos.leverage, pos.entered_at, t_out)
        if src == "none":
            src = "poll" if (pos.max_adverse_roe or pos.max_favorable_roe) else "none"
        return (min(pos.max_adverse_roe, mae), max(pos.max_favorable_roe, mfe), src)

    def planned_tp(entry: float, side: str, lev: int,
                   ind: dict | None = None, cm_target: float = 0.0) -> float:
        """attach_tp 가 실제로 걸 CM 기준 익절가를 미리 계산한다(A안 손절 정합용).

        attach_tp 와 **같은 함수·같은 인자**를 쓴다 - 어긋나면 손절폭을 실제와 다른
        익절폭에 맞추게 된다. CM 익절선이 무효면 0 을 돌려주고, 그 경우 손절 정합은
        적용하지 않는다(손익비 폴백 익절선은 손절선에서 역산한 값이라 순환이 된다).
        """
        if not args.cm_tp_limit:
            return 0.0
        L = side == "LONG"
        if cm_target:
            return cm_tp_price({"cm_tp_long": cm_target if L else 0.0,
                                "cm_tp_short": 0.0 if L else cm_target},
                               entry, side, args.cm_tp_pullback_pct, lev,
                               args.cm_tp_max_roe)
        if ind:
            return cm_tp_price(ind, entry, side, args.cm_tp_pullback_pct, lev,
                               args.cm_tp_max_roe)
        return 0.0

    def decide_stop(entry: float, side: str, lev: int, cap_roe: float,
                    stop_hint: float = 0.0, ind: dict | None = None,
                    tp_price: float = 0.0) -> float:
        """**모든 진입 경로가 손절선을 여기서만 정한다.**

        순서가 중요하다: 기준선 -> 하한(넓힘) -> 방향검증 -> 상한(자름).
        하한을 상한보다 먼저 적용해야 "넓힌 뒤 상한으로 자르는" 의도대로 동작한다.

        [2026-08-26] 오늘 이 계산이 세 곳에 복제돼 있어 같은 유형의 사고가 다섯 번 났다:
          - 안전망 경로에만 widened_stop 이 없어 하한이 무력화(UAIUSDT ROE 2.23%)
          - 안전망이 `_ours` 인데 채택용 상한을 걸어 원복한 안 1 이 부활
          - `--min-stop-roe` 를 제거하자 그 인자에 의존하던 체결가 재검증이 조용히 사망
        한 곳으로 모아 재발을 막는다.

        cap_roe: 신규 진입은 `--new-max-stop-roe`, 재시작 채택분은 `--adopt-max-stop-roe`.
                 경로마다 다르므로 **호출부가 명시**한다(기본값을 두지 않는다).
        tp_price: 지정가 TP 로 실제로 걸 가격(원칙 0 = CM 최대익절선 기준). 주면
                 손절폭을 그 폭 이하로 맞춘다(A안). 0 이면 종전과 동일하게 동작한다.
        """
        # [2026-08-27] 고정폭 손절이 켜져 있으면 여기서 끝난다.
        # 하한(widened_stop)·상한(cap_stop_roe)·손익비 정합(match_stop_to_tp)을
        # 전부 건너뛴다 — 고정폭 자체가 그 셋을 대체하는 값이다. 겹치면 오늘 오전처럼
        # "상한이 여러 겹이라 가장 조인 것 하나가 나머지를 무의미하게" 만든다.
        if args.stop_fixed_roe > 0 and entry > 0 and lev > 0:
            # [2026-08-27] 경로별 상한(cap_roe)은 고정폭에도 살려둔다.
            # 종전엔 무조건 반환해 재시작 채택분의 --adopt-max-stop-roe(5)가
            # 조용히 무력화됐다. 오늘 반복된 "경로별 차이가 조용히 사라지는" 유형이다.
            _r = min(args.stop_fixed_roe, cap_roe) if cap_roe > 0 else args.stop_fixed_roe
            d = _r / 100.0 / lev
            return entry * (1 - d) if side == "LONG" else entry * (1 + d)
        st = float(stop_hint or 0.0)
        if st <= 0:
            st = (ind or {}).get("e25") or 0.0
        if st <= 0:
            st = fallback_stop(entry, side)
        st = widened_stop(entry, st, side, args.stop_widen_pct)
        if not stop_is_sane(entry, st, side):
            st = fallback_stop(entry, side)
        st = cap_stop_roe(entry, st, side, lev, cap_roe)
        return match_stop_to_tp(entry, st, side, lev, tp_price)

    def match_stop_to_tp(entry: float, st: float, side: str, lev: int,
                         tp_price: float) -> float:
        """[2026-08-27 A안] 손절폭을 익절폭 이하로 눌러 계획 손익비를 세운다.

        **좁히기만 한다.** 익절선이 멀다고 손절선을 넓히면 어젯밤의 -18% 가 돌아온다.
        하한(--stop-match-floor-roe)을 두는 이유: 익절선이 코앞인 거래(계획 손익비
        0.4 미만, 원장 93건 승률 77%)까지 손절을 조이면 노이즈에 즉시 털린다.
        그 밴드는 지금 본전이라 건드릴 이유가 없다.
        """
        if args.stop_rr_match <= 0 or tp_price <= 0 or entry <= 0 or st <= 0:
            return st
        L = side == "LONG"
        tp_w = ((tp_price - entry) / entry) if L else ((entry - tp_price) / entry)
        cur_w = ((entry - st) / entry) if L else ((st - entry) / entry)
        if tp_w <= 0 or cur_w <= 0:
            return st
        max_w = max(tp_w / args.stop_rr_match,
                    args.stop_match_floor_roe / max(1, lev) / 100.0)
        if cur_w <= max_w:
            return st
        return entry * (1 - max_w) if L else entry * (1 + max_w)

    def attach_tp(pos, entry: float, lev: int, ind: dict | None = None,
                  cm_target: float = 0.0, label: str = "") -> None:
        """**모든 진입 경로가 지정가 TP 를 여기서만 건다.**

        CM 최대익절선 -> 상한 -> (무효면) 손익비 폴백 -> 상한 -> 등록.
        둘 다 무효면 경고를 남기고 폴링 익절에만 맡긴다.

        [2026-08-26] 폴백을 세 경로에 각각 넣다가 두 곳에 상한(`cap_tp_roe`)을 빠뜨려
        익절 목표가 ROE 9~10% 까지 벌어졌다(TUTUSDT +9.99%). 원장은 4% 초과 대역이
        가장 나쁘다고 말한다(5.9~6% 55건 TP체결29% 건당-0.5372). 한 곳으로 모은다.
        """
        if not args.cm_tp_limit:
            return
        side = pos.side
        L = side == "LONG"
        src = "CM 최대익절선"
        tp = 0.0
        if cm_target:
            tp = cm_tp_price({"cm_tp_long": cm_target if L else 0.0,
                              "cm_tp_short": 0.0 if L else cm_target},
                             entry, side, args.cm_tp_pullback_pct, lev, args.cm_tp_max_roe)
        elif ind:
            tp = cm_tp_price(ind, entry, side, args.cm_tp_pullback_pct,
                             lev, args.cm_tp_max_roe)
        if tp <= 0:
            rr = cap_tp_roe(entry, pos.tp_rr, side, lev, args.cm_tp_max_roe)
            if rr > 0 and ((rr > entry) if L else (rr < entry)):
                tp, src = rr, "손익비 폴백"
        if tp > 0:
            pos.tp_limit_price = tp
            sync_tp_limit(ex, pos, args.dry_run, say)
            if src != "CM 최대익절선":
                say(f"{label}CM 익절선 무효 {pos.symbol}({side}) - {src}으로 지정가 TP 등록")
        else:
            say(f"경고 {label}지정가 TP 미등록 {pos.symbol}({side}) - "
                f"CM/손익비 모두 무효, 폴링 익절만 남음")

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

    # [2026-08-25] 이전 프로세스가 남긴 reduceOnly 지정가 TP 를 파악한다.
    # 이걸 안 지우고 채택 경로에서 새로 걸면 같은 수량의 reduceOnly 가 둘이 되어
    # -2022(ReduceOnly Order is rejected) 로 거부된다(실측: 1000PEPE/DOGE 2건).
    _existing_tps: dict = {}
    if not args.dry_run:
        try:
            for _o in ex.client.futures_get_open_orders() or []:
                if str(_o.get("type")) == "LIMIT" and _o.get("reduceOnly"):
                    _existing_tps.setdefault(_o.get("symbol"), []).append(
                        int(_o.get("orderId") or 0))
        except Exception as e:
            say(f"경고 기존 지정가 TP 조회 실패({e})")

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
        elif _live and args.adopt_unowned_positions:
            say(f"상태파일 없음 - 계좌 포지션 {len(_live)}건을 명시적 옵션으로 채택합니다")
        elif _live:
            say(f"수동 포지션 보호: 상태파일 미소유 계좌 포지션 {len(_live)}건 채택 제외")
            _live = []
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
            # [2026-08-26 통합] 손절선은 decide_stop 한 곳에서만 정한다.
            # 재시작 전후로 같은 전략이 다르게 동작하면 안 된다.
            _stop = decide_stop(_ep, _side, _lev, args.adopt_max_stop_roe, ind=_ind,
                                tp_price=planned_tp(_ep, _side, _lev, ind=_ind))
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
            # [2026-08-27] tp_bb(볼밴 익절) 제거 — 아래 세 경로 공통.
            _p = Pos(_sym, _side, [_ep], _qty, _entered_at, _lev,
                     stop_price=_stop, tp_rr=_tprr,
                     swept=(_sym in _swept_prev))
            # [2026-08-25 사용자요청] 채택분에도 CM 지정가 TP 를 건다.
            # 채택 포지션은 진입 맥락이 유실돼 CM 추세 시작점이 진입 당시와 다를 수
            # 있지만, cm_tp_price 가 "목표가 이미 평단을 지났으면 무효" 를 걸러주므로
            # 엉뚱한 방향의 주문은 나가지 않는다. 무효면 기존 볼밴/RR 폴링으로 간다.
            for _oid in _existing_tps.pop(_sym, []):
                try:
                    ex.cancel_regular_order(_sym, _oid)
                    say(f"채택 전 기존 지정가 TP 취소 {_sym} orderId={_oid}")
                except Exception:
                    pass
            attach_tp(_p, _ep, _lev, ind=_ind, label="채택 ")
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
        # [2026-08-26 P0-2] 살아남은 진입주문을 되살린다. 이미 포지션이 된 것(채택 완료)과
        # 거래소에서 사라진 것은 뺀다. 남은 것은 다음 사이클의 체결 확인 경로가 처리한다.
        for _sy, _od in _restored_orders.items():
            if _sy in positions or not isinstance(_od, dict) or not _od.get("order_id"):
                continue
            entry_orders[_sy] = _od
        if entry_orders:
            say(f"진입주문 복원 {len(entry_orders)}건: {', '.join(sorted(entry_orders))} "
                f"- 체결 확인 후 손절 등록")
            save_state()
        # 아직 안 닫힌 채택 포지션의 진입도 집계에 넣는다
        for _s2, _p2 in positions.items():
            if _p2.entered_at >= time.time() - 7500:
                entries.append((_p2.entered_at, _p2.side))
        entries.sort()
    deadline = (time.time() + args.minutes * 60) if args.minutes > 0 else float("inf")
    n_align = n_entry = n_exit = 0
    run_started_at = time.time()      # 정합성 대조의 기준 시각
    cooldown: dict[str, float] = {}
    # [2026-08-25 버그] "손절선통과" 키가 빠져 있어 그 분기를 타면 KeyError 가 났다.
    # KeyError 는 바깥 `except Exception` 이 삼켜서 **그 사이클의 남은 심볼 전체가
    # 통째로 건너뛰어졌다** — 그 분(minute)에는 진입이 한 건도 나가지 않는다.
    # 게다가 오류가 say() 가 아니라 print() 로만 나가서 런로그에 흔적조차 없었다.
    # 이후 키가 늘어도 같은 사고가 나지 않도록 defaultdict 로 바꾼다.
    # [2026-08-26 개선③] "근접손절"이 봉 기준 분기와 마크가격 기준 분기 **두 곳에서**
    # 같은 키로 집계돼 왔다. 스킵의 절반 이상을 차지하는 사유인데 어느 쪽이 얼마인지
    # 가릴 수 없어 손절 하한 판정(안3)의 근거로 쓸 수가 없었다. 키를 분리한다.
    skips = collections.defaultdict(int, {"근접손절": 0, "근접손절순간": 0, "쿨다운": 0,
                                          "전환경과": 0,
                                          "익절선통과": 0, "신호노후": 0,
                                          "손절선통과": 0, "CM익절선무효": 0,
                                          "상위추세역행": 0, "손절폭부족": 0,
                                          # [2026-08-26] 8/25 에 "손절선통과" 키 누락으로
                                          # 사이클 전체가 죽은 사고가 있었다. defaultdict 라
                                          # KeyError 는 안 나지만 방어적으로 전부 채운다.
                                          "눌림과다": 0, "추격진입차단": 0, "방향편중": 0,
                                          "진입우위부족(축소)": 0})
    # [2026-08-25 안3 판정용] 손절 하한을 둘지 결정하려면 "근접손절로 몇 %를 버리는지"를
    # 실측해야 한다. 캐시 시뮬은 93% 를 버린다고 나왔지만 라이브와 대조된 적이 없다.
    skips_logged_at = 0.0
    # [2026-08-26 개선②] 스캔 시작 오프셋. 아래 신호탐색 루프가 symbols 를 항상 같은
    # 순서로 돌아서, 1회전에 걸리는 시간만큼 **늘 같은 뒷줄 심볼이** max_signal_age
    # 창을 놓쳤다. 지연이 특정 심볼에 고정되지 않도록 매 사이클 시작점을 돌린다.
    # 대상 집합·판정·필터는 그대로이므로 신호 품질에는 영향이 없다.
    scan_offset = 0
    stats = {"win": 0, "net": 0.0, "nom": 0.0}
    _lt0 = time.localtime()
    last_slot = (_lt0.tm_hour, 0 if _lt0.tm_min < 30 else 30)
    side_stat = {"LONG": [0, 0, 0.0], "SHORT": [0, 0, 0.0]}   # 건수, 승, 순익
    stop_history = {"LONG": [], "SHORT": []}
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
            if (_r.get("exit_reason") == "STOP_EXCHANGE"
                    and _r.get("side") in stop_history):
                stop_history[_r["side"]].append(float(_r["exited_at"]))
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
            if float(_r.get("exited_at", 0) or 0) < args.stats_since_ts:
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
    # [2026-08-27] 일시정지 상태가 텔레그램 런타임에만 있어 재시작하면 풀렸다.
    # 진단하는 동안 매매를 멈춰두려면 기동 시점에 걸 수 있어야 한다.
    paused = bool(args.start_paused)
    stop_guard_next_at = 0.0        # 지정가 손절 미체결 감시 다음 실행 시각
    stop_breach_since: dict = {}    # {심볼: 손절선을 처음 지난 시각}

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
        """원장과 거래소를 대조한다(I/O 담당). 판정은 ledger_vs_exchange_report.

        조회/읽기 실패면 None. 성공하면 report dict.
        """
        if args.dry_run:
            return None
        now_ = time.time()
        w_from = max(float(since), now_ - CHK_WINDOW_SEC)
        try:
            inc = ex.client.futures_income_history(
                startTime=int(w_from * 1000), limit=CHK_LIMIT)
        except Exception:
            return None
        try:
            lines_ = LEDGER.read_text(encoding="utf-8").splitlines()
        except Exception:
            return None
        try:
            return ledger_vs_exchange_report(inc, lines_, since, now_)
        except Exception:
            return None

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
            _w = chk["window_h"]
            if chk["truncated"]:
                # 잘린 합계로 만든 숫자는 내지 않는다.
                lines.insert(1, f"[주의] 정합성 판정 불가 - 거래소 내역이 {CHK_LIMIT}행에서 "
                                f"잘렸다(최근 {_w:.1f}h). 전수 대조는 "
                                f"scripts/reconcile_realized_pnl.py")
            elif chk["missing"]:
                _m = chk["missing"]
                lines.insert(1, f"[경고] 원장 누락 의심 {len(_m)}건 "
                                f"({', '.join(_m[:4])}{'...' if len(_m) > 4 else ''}) - "
                                f"거래소엔 청산이 있는데 원장에 없다(최근 {_w:.1f}h) "
                                f"- 집계를 믿지 말 것")
            elif chk["mismatch"]:
                _s, _e, _l, _d = chk["mismatch"][0]
                lines.insert(1, f"[주의] 실현손익 불일치 {len(chk['mismatch'])}건 - "
                                f"최대 {_s} 거래소{_e:+.4f} 원장{_l:+.4f} 차이{_d:+.4f} "
                                f"(최근 {_w:.1f}h)")
            else:
                lines.append(f"  정합성 OK (최근 {_w:.1f}h 실현손익 심볼단위 일치, "
                             f"거래소{chk['exch_pnl']:+.4f})")
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

    def close_breached(sym: str, why: str = "손절선 통과",
                       ledger_reason: str = "") -> None:
        """손절선을 이미 지난 포지션(-2021)을 즉시 시장가로 끊는다.

        재등록을 기다리면 10초 폴링이 잡을 때까지 손실이 커진다(실측 최악 -18.76%).
        청산 후 원장 기록은 reconcile/record_external_close 가 이어서 처리한다.
        """
        pos = positions.get(sym)
        if pos is None or args.dry_run:
            return
        cancel_tp_limit(ex, pos, args.dry_run)
        try:
            ex.close_market_position(sym, pos.side, abs(pos.qty))
            say(f"{why} 즉시청산 {sym} {pos.side} qty={pos.qty}")
        except Exception as e:
            say(f"경고 {sym} 즉시청산 실패({e}) - 봇 폴링 손절로 넘김")
            unprotected[sym] = {"next_retry_at": time.time() + 5.0,
                                "last_warn_at": time.time(),
                                "since": time.time()}
            return
        record_external_close(sym, forced_reason=ledger_reason)

    def stop_limit_px(stop: float, side: str) -> float:
        """스탑-리밋의 지정가. 0 이면 시장가 손절(종전 동작)."""
        if not args.stop_limit or stop <= 0:
            return 0.0
        d = max(0.0, args.stop_limit_slip_pct) / 100.0
        # 트리거보다 **불리한 쪽**으로 d 만큼. LONG 손절은 매도이므로 더 낮게.
        return stop * (1 - d) if side == "LONG" else stop * (1 + d)

    def tick_fast(now_ts: float) -> None:
        """5초 주기 틱. 마크가격을 **1회 호출**로 받아 여러 일을 한 번에 처리한다.

        [2026-08-27] MFE/MAE 갱신을 메인 사이클(1분)에서 여기로 옮긴다.
        되돌림 청산이 실전에서 **한 번도 발동하지 않았다** — 원인은 규칙이 아니라
        계측 주기였다. 실측 BTRUSDT: MFE +3.78% 까지 갔다가 -5.26% 로 손절됐는데,
        arm 1.0/frac 0.4 면 2.27% 에서 청산됐어야 한다. 보유가 1.5분뿐이라 1분 폴링이
        고점을 놓쳐 무장조차 못 했다. 익절 보유 중앙이 3.9분이라 **규칙이 겨냥한
        거래 상당수가 그 사각지대**에 있었다.
        (계측 고장 때와 같은 원인이다. 그때는 청산 후 봉 복원으로 고쳤지만
         되돌림은 실시간 판정이라 사후 복원으로는 못 고친다.)

        5초로 옮기면 granularity 가 12배 촘촘해지고 **API 호출은 늘지 않는다** —
        이미 이 루프가 전 심볼 마크가격을 1회로 받고 있다.
        dry-run(그림자)에서도 갱신은 돌려야 라이브와 비교가 성립한다. 주문만 막는다.
        """
        nonlocal stop_guard_next_at
        if now_ts < stop_guard_next_at:
            return
        stop_guard_next_at = now_ts + 5.0
        if not positions:
            return
        try:
            marks = {m["symbol"]: float(m["markPrice"])
                     for m in ex.client.futures_mark_price()}
        except Exception:
            return
        for sm in list(positions):
            pos = positions.get(sm)
            if pos is None:
                continue
            mk = marks.get(sm)
            if not mk or pos.entry <= 0:
                continue
            L = pos.side == "LONG"
            roe = ((mk / pos.entry - 1) if L else (1 - mk / pos.entry)) * pos.leverage * 100
            pos.max_adverse_roe = min(pos.max_adverse_roe, roe)
            pos.max_favorable_roe = max(pos.max_favorable_roe, roe)
            # --- 되돌림 청산 판정도 여기서 한다(지정가 경로) ---
            # 손절선을 이미 지났으면 손절이 먼저다 - 건드리지 않는다.
            _breached = (mk <= pos.stop_price) if L else (mk >= pos.stop_price)
            if (not args.dry_run and not _breached and not pos.gb_pending
                    and args.giveback_arm_roe > 0 and args.giveback_limit_sec > 0
                    and pos.max_favorable_roe >= args.giveback_arm_roe
                    and roe <= pos.max_favorable_roe * (1 - args.giveback_frac)):
                try:
                    # [2026-08-27 버그] format_price 는 API payload 용 **문자열**을
                    # 돌려준다. 그대로 넣으면 sync_tp_limit 의 `tp_limit_price <= 0`
                    # 에서 TypeError 가 나고, 그 예외를 메인 루프의 except 가 잡아
                    # **그 사이클 전체가 중단된다** — 스캔이 통째로 멈춘다.
                    # 실사고 15:0x~15:16 [주기오류] 반복, 신규 진입 0.
                    pos.tp_limit_price = float(ex.round_price(sm, mk))
                    sync_tp_limit(ex, pos, args.dry_run, log_line)
                    if pos.tp_order_id:
                        pos.gb_pending = now_ts + args.giveback_limit_sec
                        say(f"되돌림 지정가 청산 {sm} {pos.side} {pos.tp_limit_price:.8f} "
                            f"(MFE {pos.max_favorable_roe:.2f}% -> 현재 {roe:.2f}%)")
                        save_state()
                except Exception as _e:
                    log_line(f"경고 되돌림 지정가 등록 실패 {sm}: {_e}")
        guard_stop_breach_inner(now_ts, marks)

    def guard_stop_breach_inner(now_ts: float, marks: dict) -> None:
        """지정가 손절이 미체결로 남아 포지션이 무방어가 되는 것을 막는다.

        스탑-리밋은 급락으로 가격이 지정가를 지나쳐 버리면 체결되지 않는다.
        메인 사이클 폴링(분 단위)만 믿으면 최대 60초를 무방어로 보낸다.
        여기서 **5초마다** 마크가격 전체를 1회 호출로 받아 확인한다(심볼 수와 무관).

        두 조건 중 하나면 시장가로 끊는다:
          - 손절선을 지난 지 --stop-limit-timeout-sec 초 경과
          - 손절선을 --stop-limit-fail-pct %% 넘게 지나침(급락 즉시 보호)
        """
        if args.dry_run or not args.stop_limit:
            return
        for sm in list(positions):
            pos = positions.get(sm)
            if pos is None or pos.stop_price <= 0:
                continue
            mk = marks.get(sm)
            if not mk:
                continue
            L = pos.side == "LONG"
            over = (pos.stop_price - mk) if L else (mk - pos.stop_price)
            if over <= 0:
                stop_breach_since.pop(sm, None)
                continue
            t0 = stop_breach_since.setdefault(sm, now_ts)
            gap = over / pos.stop_price * 100.0
            if gap >= args.stop_limit_fail_pct or (now_ts - t0) >= args.stop_limit_timeout_sec:
                say(f"지정가 손절 미체결 {sm} {pos.side} "
                    f"(손절선 {gap:.2f}% 초과 / {now_ts - t0:.0f}초 경과) - 시장가 전환")
                stop_breach_since.pop(sm, None)
                close_breached(sm, why="지정가 손절 미체결",
                               ledger_reason="STOP_EXCHANGE")

    def guard_giveback(now_ts: float) -> None:
        """되돌림 지정가 청산이 마감까지 안 채워졌으면 시장가로 끊는다.

        지정가로 앉히면 maker(0.02%)가 되지만 안 채워질 수 있다. 방치하면
        되돌림 판정이 났는데도 포지션이 계속 열려 있게 된다 - 그건 이 규칙의
        목적(유리했던 것을 지킨다)을 정면으로 어긴다.
        """
        if args.dry_run or args.giveback_limit_sec <= 0:
            return
        for sm in list(positions):
            pos = positions.get(sm)
            if pos is None or not pos.gb_pending or now_ts < pos.gb_pending:
                continue
            pos.gb_pending = 0.0
            say(f"되돌림 지정가 미체결 {sm} {pos.side} "
                f"({args.giveback_limit_sec:.0f}초) - 시장가 전환")
            close_breached(sm, why="되돌림 미체결",
                           ledger_reason="GIVEBACK_MARKET")

    def drop_if_underfilled(sym: str, want: float, got: float) -> bool:
        """부분체결로 목표보다 작게 잡힌 포지션이면 즉시 정리한다. 정리했으면 True.

        잔량 취소는 이미 끝난 뒤에 부른다 - 순서가 반대면 취소 안 된 잔량이
        청산 직후에 체결돼 반대 포지션이 열린다.
        """
        if args.min_fill_ratio <= 0 or args.dry_run:
            return False
        if want <= 0 or got <= 0 or got >= want * args.min_fill_ratio:
            return False
        say(f"부분체결 미달 {sym}: {got}/{want} "
            f"({got / want * 100:.0f}% < {args.min_fill_ratio * 100:.0f}%) - 정리")
        close_breached(sym, why="부분체결 미달",
                       ledger_reason="UNDERFILL_CLEANUP")
        return sym not in positions

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
            try:
                algo = sync_stop(ex, sm, pos.side, pos.qty, pos.stop_price,
                                 pos.stop_algo_id, args.dry_run, say,
                                 limit_price=stop_limit_px(pos.stop_price, pos.side))
            except StopAlreadyBreached:
                unprotected.pop(sm, None)
                close_breached(sm)
                continue
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
            cancel_tp_limit(ex, ps, args.dry_run)
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
        if args.attach_ws:
            # [2026-08-27] 부착 모드는 워커를 **절대** 띄우지 않는다.
            # 여기서 start_ws 를 부르면 공유 PID 파일을 덮어써서, 다음에
            # 라이브가 cleanup 할 때 엉뚱한 프로세스를 죽이게 된다.
            say(f"WS 부착 모드 - 재기동하지 않고 대기({reason})")
            reset_ws_warmup(now_ts)
            return
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
        if args.ws and args.attach_ws:
            say("WS 부착 모드 - 심볼 갱신 시 워커를 재기동하지 않는다")
        elif args.ws:
            ex.set_ws_kline_cache(None)
            stop_ws(ws_proc)
            time.sleep(1.0)
            ws_proc, ws_cache = start_ws(symbols)
            reset_ws_warmup(now_ts)
            ws_last_restart_at = now_ts
            ws_restart_count += 1
            say("심볼 갱신 반영 - WS 워커 재기동, 준비 전에는 보유 포지션 관리만 수행")

    def reconcile_live_positions(now_ts: float) -> None:
        nonlocal reconcile_next_at, n_entry
        if args.dry_run or now_ts < reconcile_next_at:
            return
        reconcile_next_at = now_ts + 15.0
        try:
            live = ex.client.futures_account()["positions"]
        except Exception:
            return
        live_amt = {}
        for p in live:
            try:
                _a = float(p.get("positionAmt", 0) or 0)
            except Exception:
                _a = 0.0
            if abs(_a) > 0:
                live_amt[p["symbol"]] = _a
        live_nonzero = set(live_amt)
        # [2026-08-26 P0] **방향/수량 불일치 감지.**
        # 종전엔 "심볼이 살아 있는가"만 봐서, 포지션이 반대로 뒤집혀도 알아채지 못했다.
        # 실사고 PYTHUSDT: 앱에서 낸 BUY 11450(reduceOnly=False)이 봇의 SHORT 4404 를 덮고
        # LONG 7046 으로 뒤집혔는데, 봇은 3분 넘게 SHORT 로 알고 SL/TP 를 관리했다.
        # 방향이 다르면 알던 포지션을 원장에 닫고(아래 두 번째 루프가 새 방향으로 재채택),
        # 수량만 다르면 추적 수량을 맞춘다(SL/TP 주문 수량이 실제와 어긋나는 것을 막는다).
        for sm in list(positions):
            _amt = live_amt.get(sm)
            if _amt is not None:
                _live_side = "LONG" if _amt > 0 else "SHORT"
                if _live_side != positions[sm].side:
                    say(f"경고 방향 불일치 {sm}: 봇은 {positions[sm].side} 로 아는데 "
                        f"거래소는 {_live_side} {abs(_amt)} - 알던 포지션을 닫고 재채택한다")
                    record_external_close(sm)
                    continue
                _q = abs(_amt)
                if positions[sm].qty > 0 and abs(_q - positions[sm].qty) / positions[sm].qty > 0.02:
                    say(f"수량 보정 {sm}({positions[sm].side}): "
                        f"{positions[sm].qty} -> {_q} (거래소 정본)")
                    positions[sm].qty = _q
            if sm in live_nonzero:
                continue
            # [2026-08-21 P0] 여기서 그냥 지우면 손실이 원장에 남지 않는다.
            # 반드시 record_external_close 를 거쳐 체결 이력으로 손익을 남긴다.
            record_external_close(sm)
        # [2026-08-26 P0-2] 반대 방향 안전망: '우리 것인데 추적이 안 되는' 포지션을 잡는다.
        # 지금까지 이 함수는 사라진 포지션만 지웠지 새로 생긴 것은 보지 않았다. 그래서
        # 체결 확인 전에 죽거나(taskkill /F) 어떤 이유로든 추적이 끊기면 손절 없는
        # 포지션이 영구히 방치됐다(실사고: POL/STAR/STX/VIRTUAL 4건 SL 0건).
        # 원인을 하나씩 막는 것보다 결과를 주기적으로 대조하는 쪽이 확실하다.
        for p in live:
            sm = p["symbol"]
            amt = float(p.get("positionAmt", 0) or 0)
            if abs(amt) <= 0 or sm in positions:
                continue
            # [2026-08-26] 진입주문이 있는 심볼은 원래 무조건 건너뛰었는데, 그러면
            # 체결 확인 경로가 막히거나 늦을 때(봇 freeze 등) 안전망이 무력화된다.
            # 실사고 ONGUSDT: 11:19:06 전량 체결됐는데 봇이 네트워크 호출에서 멈춰
            # 12분간 인식하지 못했고 그동안 손절이 없었다.
            # 주문 발주 후 TTL+30초가 지나도록 해소가 안 됐으면 안전망이 가져간다.
            _eo = entry_orders.get(sm)
            # [2026-08-26 P0] 종전엔 진입주문이 있으면 TTL+30초(=75초)를 기다렸다.
            # 그런데 **체결 확인 루프는 메인 사이클(분 정렬로 1분에 1회)에서 돌고,
            # 이 안전망은 정렬 대기 루프에서 15초마다 돈다** — 경쟁이 되지 않는다.
            # 실측: 13:30 이후 포지션 유입의 93%(13/14)가 이 경로였고, 그 75초 동안
            # 포지션은 무보호였다(BMTUSDT 발견 시 이미 ROE -10.11%).
            # **거래소에 포지션이 있다는 것 자체가 체결의 증거**이므로 유예를 두지 않고
            # 즉시 가져오되, 주문에 담긴 진입 맥락(손절/legs/CM목표)을 그대로 살린다.
            # 그러면 보호 지연이 75초+ -> 15초로 줄고, `CM 익절선 무효` 폴백과
            # 계측 공백(진입 로그 누락)도 함께 사라진다. 진입 판정은 건드리지 않는다.
            if (not args.adopt_unowned_positions and sm not in _owned):
                continue          # 다른 봇/수동 포지션은 건드리지 않는다
            sd = (_eo.get("side") if _eo else None) or ("LONG" if amt > 0 else "SHORT")
            ep = float(p.get("entryPrice") or 0)
            if ep <= 0:
                continue
            _ours = _eo is not None          # 우리 주문이 체결된 것인가
            # 지표는 CM 지정가 TP 계산에 쓴다.
            try:
                _i = indicators(signal_bars(ex, sm, args.signal_tf_min))
            except Exception:
                _i = None
            # [2026-08-26 통합] 손절선은 decide_stop 한 곳에서만 정한다.
            #   _ours(봇 자기 진입)  -> 진입 판정 때 계산한 손절선을 힌트로 주고
            #                          체결가 기준으로 하한/상한을 다시 적용한다.
            #   미추적(수동/고아)     -> 힌트 없이 현재 EMA25 로 새로 잡는다.
            # 상한은 경로별로 다르다: 자기 진입은 --new-max-stop-roe(0=없음),
            # 채택분은 --adopt-max-stop-roe. 이 구분을 놓쳐 원복한 안 1 이 부활한 적이 있다.
            _hint = float(_eo.get("stop") or 0) if _ours else 0.0
            _cap = args.new_max_stop_roe if _ours else args.adopt_max_stop_roe
            st_ = decide_stop(ep, sd, args.leverage, _cap, stop_hint=_hint, ind=_i,
                              tp_price=planned_tp(ep, sd, args.leverage, ind=_i,
                                                  cm_target=float((_eo or {}).get("cm_target") or 0.0)))
            _legs = [ep] * max(1, len((_eo or {}).get("legs") or [1]))
            positions[sm] = Pos(sm, sd, _legs, abs(amt), now_ts, args.leverage,
                                stop_price=st_,
                                tp_rr=fee_aware_rr_price(ep, st_, sd, args.rr,
                                                         args.roundtrip_fee_rate),
                                since_trade_id=int((_eo or {}).get("since_id") or 0),
                                # 우리 주문 체결분은 봇이 판정해서 연 포지션이므로
                                # 방향편중 카운트 대상이다(안B 의 제외 대상이 아니다).
                                swept=not _ours)
            # [2026-08-26 P0] 부분체결 잔량을 반드시 취소한다.
            # 정상 체결 경로(3297행)에는 원래 이 방어가 있었는데, 오늘 이 안전망 경로를
            # 추가하면서 빠뜨렸다. 그리고 오늘 안전망이 **주 진입 경로**(유입의 93%)가
            # 되면서 구멍이 상시 열려 있었다.
            # 실사고 VVVUSDT: 25.48 주문 -> 11.46 부분체결 -> 안전망이 채택하며 추적 종료
            # -> 잔량 14.02 가 거래소에 살아남아 나중에 체결되며 원치 않는 추가 진입.
            # reduceOnly 가 아니므로 포지션을 키운다 - 방치하면 추적 수량과 실제가 어긋난다.
            if _eo and _eo.get("order_id"):
                try:
                    _st_, _ = entry_order_state(ex, sm, _eo["order_id"])
                    if _st_ in ("NEW", "PARTIALLY_FILLED"):
                        ex.cancel_regular_order(sm, _eo["order_id"])
                        say(f"부분체결 잔량 취소 {sm} orderId={_eo['order_id']} "
                            f"(status={_st_})")
                except Exception as _e:
                    log_line(f"경고 부분체결 잔량 취소 실패 {sm}: {_e}")
            entry_orders.pop(sm, None)     # 안전망이 가져갔으므로 주문 추적은 종료
            # 잔량 취소가 끝난 지금이 판정 시점이다(취소 전에 닫으면 잔량이 뒤늦게
            # 체결돼 반대 포지션이 열린다).
            if _ours and drop_if_underfilled(sm, float(_eo.get("qty") or 0), abs(amt)):
                continue
            if _ours:
                # [2026-08-26] 이 경로가 정상 진입 경로를 대체하게 되면서 `n_entry` 가
                # 증가하지 않아 `스킵누적` 라인의 진입 카운트가 항상 0 으로 나왔다.
                # 빈도를 보는 주 지표라 여기서도 세어야 한다.
                n_entry += 1
                entries.append((now_ts, sd))
                say(f"진입 {sm} {sd} 평단{ep:.6f} {len(_legs)}차 qty={abs(amt)} "
                    f"손절{st_:.6f} (거래소 포지션으로 체결 확인)")
            else:
                say(f"미추적 포지션 발견 {sm} {sd} qty={abs(amt)} 평단{ep} - 뒤늦게 채택")
            try:
                positions[sm].stop_algo_id = sync_stop(
                    ex, sm, sd, abs(amt), st_, 0, args.dry_run, say,
                    limit_price=stop_limit_px(st_, sd))
            except StopAlreadyBreached:
                close_breached(sm)
                continue
            if not positions[sm].stop_algo_id:
                unprotected[sm] = {"next_retry_at": now_ts + 15.0,
                                   "last_warn_at": now_ts, "since": now_ts}
            attach_tp(positions[sm], ep, args.leverage, ind=_i)
            save_state()

    def record_external_close(sym, forced_reason: str = ""):
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
        tr = []          # 조회가 예외로 빠져도 아래 라벨 판정이 참조하므로 미리 둔다
        try:
            _st_ms = trades_start_ms(pos)

            def _fetch():
                _t = ex.client.futures_account_trades(
                        symbol=sym, startTime=_st_ms, limit=1000)
                if pos.since_trade_id:
                    _t = [x for x in _t if int(x.get("id", 0)) > pos.since_trade_id]
                return _t

            tr = _fetch()
            if not tr:
                # 청산 직후 경쟁. 한 번만 짧게 기다렸다 다시 본다 — 실측상 수 초면
                # 조회 가능해진다(ICPUSDT 는 5초 뒤 빈 배열, 지금 재현하면 4건 정상).
                # 여기는 15초 폴링 루프라 1.5초 지연은 매매에 영향이 없다.
                time.sleep(1.5)
                tr = _fetch()
                if tr:
                    log_line(f"[재시도] {sym} 체결조회 1회 재시도로 {len(tr)}건 확보")
            tr = drop_manual_trades(ex, sym, tr, _st_ms, say)
            # [2026-08-27 P0 버그수정] **빈 체결을 성공으로 취급하면 안 된다.**
            # 청산 직후(수 초)에는 거래소가 체결을 아직 반환하지 않는 경쟁이 있다.
            # 그때 realized_fill_snapshot([]) 은 entry=exit 폴백으로 **정상처럼 보이는**
            # dict 를 돌려주고(roe 0), comm=rz=0 이 되어 **손익이 0 으로 기록된다.**
            # 실사고 ICPUSDT 19:26:28 체결 -> 19:26:33 조회 빈 배열 -> 원장 0.0000,
            # 거래소 실제 -4.18. 같은 패턴이 오늘만 4건(EUL/VELVET/BLESS/ICP).
            # 원장이 실제보다 좋게 나와 **모든 판정이 낙관 편향**된다.
            if not tr:
                raise RuntimeError("체결 조회가 비어 있음(청산 직후 경쟁) - 추정으로 기록")
            comm = sum(float(t.get("commission", 0)) for t in tr)
            rz = sum(float(t.get("realizedPnl", 0)) for t in tr)
            fill = realized_fill_snapshot(tr, pos.side, pos.entry, pos.entry,
                                          pos.qty, pos.leverage)
        except Exception as e:
            say(f"경고 {sym} 외부청산 체결조회 실패({e}) - 표시값은 추정으로 기록")
        # [2026-08-25 원장 오염 수정] 체결조회가 실패하면 예전엔 exit_price=entry, roe_pct=0으로
        # 적었는데, real_net은 실제값이라 "ROE 0%인데 손익 -1.5 USDT"인 행이 생긴다.
        # ROE 기준 통계(평균 ROE/승률 분해)가 조용히 오염된다.
        # 이제 마크가로라도 ROE를 추정하고, 추정분임을 roe_estimated 플래그로 남긴다.
        # 집계할 때 roe_estimated=true 행은 ROE 통계에서 빼면 된다(손익 통계는 그대로 유효).
        roe_estimated = False
        if fill is None:
            roe_estimated = True
            est_exit = pos.entry
            try:
                est_exit = float(ex.get_mark_price(sym)) or pos.entry
            except Exception:
                pass
            est_roe = 0.0
            if pos.entry > 0 and pos.leverage > 0 and est_exit > 0:
                gross = ((est_exit / pos.entry) - 1.0) if pos.side == "LONG" else ((pos.entry / est_exit) - 1.0)
                est_roe = gross * pos.leverage * 100.0
            fill = {"entry_price": pos.entry, "exit_price": est_exit,
                    "quantity": pos.qty, "nominal": pos.entry * pos.qty,
                    "roe_pct": est_roe}
        net = rz - comm
        nominal = fill["nominal"]
        n_exit += 1
        stats["win"] += 1 if net > 0 else 0
        stats["net"] += net
        stats["nom"] += nominal
        # [2026-08-25 버그] 외부청산은 전부 STOP_EXCHANGE 로 찍혔다. 지정가 TP 가
        # 체결돼도 손절로 기록되어, 손절률은 부풀고 TP 체결률은 아예 잴 수 없었다.
        # 청산 사유를 판정하려면 그 주문이 실제로 체결됐는지 거래소에 물어봐야 한다.
        # [2026-08-27 버그수정] 청산 사유를 **호출부가 명시**할 수 있게 한다.
        # 종전엔 `why` 가 로그 문구에만 쓰이고 원장 라벨은 여기서 따로 추론했다.
        # 그 이중 구조 때문에 되돌림 시장가 전환이 STOP_EXCHANGE 로 오기록됐다
        # (guard_giveback 이 gb_pending 을 먼저 0 으로 지우므로). 손절 체결률이
        # 부풀어 --stop-fixed-roe 와 되돌림 규칙의 효과를 잴 수 없었다.
        _reason = forced_reason or "STOP_EXCHANGE"
        if not forced_reason and pos.tp_order_id:
            # [2026-08-27 버그수정] 종전엔 **주문 상태를 다시 물어서** 판정했는데,
            # 청산 직후라 아직 FILLED 로 안 보이면 손절로 분류됐다(버그 A 와 같은 경쟁).
            # 실사고 AKEUSDT 19:55: 지정가 TP 체결(+0.0305)인데 STOP_EXCHANGE 로 기록.
            # 손절률이 부풀어 --stop-fixed-roe / --cm-tp-max-roe 판정이 왜곡된다.
            # **이미 확보한 체결 목록(tr)의 orderId 로 판정한다** — 추가 호출 0, 경쟁 없음.
            _exit_side = "SELL" if pos.side == "LONG" else "BUY"
            _oids = {int(t.get("orderId") or 0) for t in (tr or [])
                     if str(t.get("side")) == _exit_side}
            if int(pos.tp_order_id) in _oids:
                # 되돌림 지정가로 갈아끼운 주문이면 TP 가 아니다.
                _reason = "GIVEBACK_LIMIT" if pos.gb_pending else "TP_LIMIT"
            elif not _oids:
                # 체결 목록을 못 받은 경우에만 주문 상태 조회로 폴백한다.
                try:
                    _st = ex.get_order_status(sym, pos.tp_order_id)
                    if str((_st or {}).get("status")) == "FILLED":
                        _reason = "GIVEBACK_LIMIT" if pos.gb_pending else "TP_LIMIT"
                except Exception:
                    pass
        _mae, _mfe, _exsrc = excursion_for(pos, time.time())
        append_ledger(dict(version=VERSION, symbol=sym, side=pos.side,
                           entry_price=fill["entry_price"],
                           exit_price=fill["exit_price"],
                           quantity=fill["quantity"],
                           tp_limit_price=pos.tp_limit_price,
                           tp_order_placed_at=pos.tp_order_placed_at,
                           tp_wait_sec=(time.time() - pos.tp_order_placed_at
                                        if pos.tp_order_placed_at > 0 else None),
                           exit_reason=_reason, entered_at=pos.entered_at,
                           exited_at=time.time(), leverage=pos.leverage,
                           roe_pct=fill["roe_pct"], nominal=nominal,
                           legs=len(pos.legs),
                           real_commission=comm, real_realized_pnl=rz, real_net=net,
                           max_adverse_roe=_mae, max_favorable_roe=_mfe,
                           excursion_src=_exsrc,
                           roe_marks=pos.roe_marks,
                           adopted=pos.adopted, external_close=True,
                           roe_estimated=roe_estimated,
                           origin=f"scalp_bot_{VERSION}", dry_run=False))
        ss = side_stat[pos.side]
        ss[0] += 1
        ss[1] += 1 if net > 0 else 0
        ss[2] += net
        recent.append((time.time(), pos.side, net, nominal))
        if _reason == "STOP_EXCHANGE" and args.same_side_stop_cooldown_sec > 0:
            stop_history[pos.side].append(time.time())
        ws_ = why_stat.setdefault(_reason, [0, 0.0])
        ws_[0] += 1
        ws_[1] += net
        cancel_tp_limit(ex, pos, args.dry_run)
        positions.pop(sym, None)
        pending.pop(sym, None)
        unprotected.pop(sym, None)
        cooldown[sym] = time.time() + args.cooldown_sec
        save_state()
        wr = stats["win"] / n_exit * 100 if n_exit else 0.0
        say(f"외부청산 기록 {sym} {pos.side} ROE{fill['roe_pct']:+.2f}% "
            f"순손익{net:+.4f}"
            f" | 누적 {n_exit}건 승률{wr:.1f}% 손익{stats['net']:+.4f}")

    def sweep_orphan_tp_orders():
        """포지션이 없는데 남아 있는 reduceOnly 지정가 주문을 지운다.

        지정가 TP 는 봇이 죽어도 살아남는 게 장점이지만, 그 사이 포지션이 손절로
        닫히면 주문만 남는다. 그 상태로 같은 심볼에 새로 진입하면 **진입하는 순간**
        엉뚱한 가격에 잘려나간다. STOP_MARKET 고아 주문으로 이미 겪은 사고라
        같은 방식으로 주기적으로 대조해 지운다.
        """
        if args.dry_run:
            return
        try:
            live = {p["symbol"] for p in ex.client.futures_account()["positions"]
                    if abs(float(p.get("positionAmt", 0) or 0)) > 0}
            orders = ex.client.futures_get_open_orders()
        except Exception:
            return
        for o in orders:
            try:
                if str(o.get("type")) != "LIMIT" or not o.get("reduceOnly"):
                    continue
                if o["symbol"] in live:
                    continue
                ex.cancel_regular_order(o["symbol"], int(o["orderId"]))
                say(f"고아 지정가 TP 취소 {o['symbol']} orderId={o['orderId']} (포지션 없음)")
            except Exception:
                pass

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
        beat("cycle")
        try:
            handle_buttons()
            retry_unprotected_stops(time.time())
            reconcile_live_positions(time.time())
            sweep_orphan_tp_orders()
            # [2026-08-25 안3 판정용] 10분마다 스킵 사유를 남긴다. 지금까지 이 값이
            # 종료 시에만 찍혀서, 라이브가 신호를 얼마나 버리는지 관측할 수 없었다.
            if time.time() - skips_logged_at >= 600:
                skips_logged_at = time.time()
                _tot = sum(skips.values())
                if _tot or n_entry:
                    _sk = " ".join(f"{k}{v}" for k, v in skips.items() if v)
                    _den = _tot + n_entry
                    log_line(f"스킵누적 진입{n_entry} 스킵{_tot} "
                             f"(근접손절 {(skips.get('근접손절', 0) + skips.get('근접손절순간', 0)) / max(1, _den) * 100:.0f}%) [{_sk}]")
            refresh_symbol_universe(time.time())
            if args.ws and ws_cache is not None:
                _health = ws_cache.health() or {}
                _now = time.time()
                _last_msg = float(_health.get("last_market_message_ts", 0.0) or 0.0)
                _msg_60s = int(_health.get("message_count_60s", 0) or 0)
                _err_60s = int(_health.get("error_count_60s", 0) or 0)
                _consec = int(_health.get("consecutive_read_loop_errors", 0) or 0)
                # [2026-08-26] 종전엔 캔들 신선도 기준을 그대로 썼는데, 그 값이
                # 봉 주기에 묶여 커지면 WS 사망 감지가 같이 둔해진다.
                # 이건 **메시지 흐름**(초당 수십 건) 기준이므로 봉 주기와 무관하게
                # 짧게 잡아야 한다.
                _stale = _last_msg > 0 and (_now - _last_msg) > 60.0
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
                # [2026-08-26 1단계 계측] 경과 구간을 처음 넘어설 때의 ROE 를 한 번씩 남긴다.
                # 보유시간별 성적이 0~5분 승률 63% vs 5~20분 29% 로 갈리는데,
                # 그 사이에 무슨 일이 일어나는지가 기록되지 않아 임계값을 못 정한다.
                _held = now_ts - pos.entered_at
                for _mk in (60, 120, 180, 300, 420, 600, 900, 1200):
                    if _held >= _mk and str(_mk) not in pos.roe_marks:
                        pos.roe_marks[str(_mk)] = round(roe, 3)
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
                    elif (args.giveback_arm_roe > 0
                          and not pos.gb_pending
                          and pos.max_favorable_roe >= args.giveback_arm_roe
                          and roe <= pos.max_favorable_roe * (1 - args.giveback_frac)):
                        # 손절선이 먼저다(위 분기). 되돌림은 그 다음.
                        why = "GIVEBACK"
                    elif pos.tp_rr and ((mark >= pos.tp_rr) if L else (mark <= pos.tp_rr)):
                        why = "RR"
                    elif args.max_hold_sec > 0 and (now_ts - pos.entered_at) >= args.max_hold_sec:
                        why = "TIME_STOP"
                if not why:
                    continue
                # [2026-08-27] 되돌림 청산은 **지정가로 먼저** 시도한다(수수료 maker).
                # 지금 시세에 reduceOnly 지정가를 걸어두고, 마감까지 안 채워지면
                # guard_giveback 이 시장가로 전환한다. 손절은 이 분기 앞에서 이미
                # 걸러졌으므로(STOP_EMA25) 무방어 구간이 생기지 않는다 —
                # 거래소 손절주문도 그대로 살아 있다.
                if (why == "GIVEBACK" and args.giveback_limit_sec > 0
                        and not args.dry_run and not pos.gb_pending):
                    pos.tp_limit_price = float(ex.round_price(sym, mark))
                    sync_tp_limit(ex, pos, args.dry_run, log_line)
                    if pos.tp_order_id:
                        pos.gb_pending = now_ts + args.giveback_limit_sec
                        say(f"되돌림 지정가 청산 {sym} {pos.side} {pos.tp_limit_price:.8f} "
                            f"(MFE {pos.max_favorable_roe:.2f}% -> 현재 {roe:.2f}%)")
                        save_state()
                        continue
                    # 지정가 등록 실패 -> 아래 시장가 경로로 그대로 진행
                nominal = pos.entry * pos.qty
                if not args.dry_run:
                    # 남은 지정가 TP 를 먼저 지운다. 안 지우면 reduceOnly 고아 주문이
                    # 다음 진입 때 엉뚱한 가격에 포지션을 잘라낸다.
                    cancel_tp_limit(ex, pos, args.dry_run)
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
                    _st_ms = trades_start_ms(pos)
                    tr = ex.client.futures_account_trades(
                        symbol=sym, startTime=_st_ms, limit=1000)
                    # [버그3] 진입 직전 체결 id 이후만 이 포지션 것으로 본다.
                    # 같은 심볼을 반복 거래하면 이전 포지션 체결이 섞여 순손익이 오염된다.
                    if pos.since_trade_id:
                        tr = [t for t in tr if int(t.get("id", 0)) > pos.since_trade_id]
                    tr = drop_manual_trades(ex, sym, tr, _st_ms, say)
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
                _mae2, _mfe2, _exsrc2 = excursion_for(pos, time.time())
                append_ledger(dict(version=VERSION, symbol=sym, side=pos.side,
                                   entry_price=fill["entry_price"],
                                   exit_price=fill["exit_price"],
                                   quantity=fill["quantity"],
                                   exit_reason=why, entered_at=pos.entered_at,
                                   exited_at=time.time(), leverage=pos.leverage,
                                   roe_pct=roe, nominal=nominal, legs=len(pos.legs),
                                   real_commission=comm, real_realized_pnl=rz, real_net=net,
                                   max_adverse_roe=_mae2, max_favorable_roe=_mfe2,
                                   excursion_src=_exsrc2,
                                   roe_marks=pos.roe_marks,
                                   adopted=pos.adopted,
                                   origin=f"scalp_bot_{VERSION}", dry_run=args.dry_run))
                ss = side_stat[pos.side]
                ss[0] += 1
                ss[1] += 1 if net > 0 else 0
                ss[2] += net
                recent.append((time.time(), pos.side, net, nominal))
                if why == "STOP_EXCHANGE" and args.same_side_stop_cooldown_sec > 0:
                    stop_history[pos.side].append(time.time())
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

            # [2026-08-25 B안] 체결 대기 중인 진입 지정가 확인 (블로킹 없음)
            # [2026-08-26 P0] 주문조회로 체결을 못 잡으면 포지션이 안전망(TTL+30초=75초)
            # 까지 **무보호로 방치**된다. 실사고 BMTUSDT: 13:47 발주 -> 13:49:25 안전망이
            # 발견했을 때 이미 ROE -10.11%(-2.71 USDT). 13:30 이후 포지션 유입의
            # 93%(13/14)가 정상 경로가 아니라 안전망 경로였고, 최근 15분 손절 8건이
            # 전부 그 경로였다. 핸드오프 1절이 "-6% 초과 0건" 을 달성했다고 기록한 뒤
            # 다시 -10.11% 가 나온 것도 이것 때문이다(원인은 다르지만 증상은 사고 A 와 같다).
            #
            # 거래소에 **실제 포지션이 있다는 사실 자체가 체결의 증거**다. 주문조회 결과를
            # 기다리지 않고 즉시 체결로 처리한다. 보호 지연이 75초+ -> 다음 폴링으로 줄고,
            # 진입 맥락(legs/CM목표)이 보존돼 `CM 익절선 무효` 폴백과 계측 공백도 함께 준다.
            # 진입 판정 로직은 건드리지 않는다(원칙 0/1/2 무관).
            _live_amt: dict = {}
            if entry_orders:
                try:
                    _live_amt = {q["symbol"]: q["amount"]
                                 for q in ex.get_open_positions()}
                except Exception:
                    _live_amt = {}
            for _osym in list(entry_orders):
                _o = entry_orders[_osym]
                _status, _exec = entry_order_state(ex, _osym, _o["order_id"])
                _age = time.time() - _o["placed_at"]
                # 거래소 포지션 우선. _status 는 건드리지 않는다 —
                # 부분체결이면 아래 `_status != "FILLED"` 분기가 잔여 주문을 취소해야 한다.
                _lq = abs(float(_live_amt.get(_osym) or 0.0))
                if _lq > 0 and _exec <= 0 and _osym not in positions:
                    _exec = _lq
                    say(f"체결 확인(거래소 포지션 기준) {_osym} qty={_lq} "
                        f"- 주문조회 status={_status}, 무보호 대기 생략")
                if _status == "UNKNOWN" and _exec <= 0 and _age < args.entry_order_ttl_sec:
                    continue                     # 조회 실패는 다음 주기에 다시 본다
                if _exec <= 0 and _status in ("NEW", "PARTIALLY_FILLED", "UNKNOWN"):
                    if _age < args.entry_order_ttl_sec:
                        continue                 # 아직 대기 — 블로킹하지 않고 넘어간다
                    try:
                        ex.cancel_regular_order(_osym, _o["order_id"])
                    except Exception:
                        pass
                    _status2, _exec = entry_order_state(ex, _osym, _o["order_id"])
                    if _exec <= 0:
                        # [2026-08-26 P0] 종전에는 `_status2` 를 대입만 하고 쓰지 않아,
                        # **취소가 실패했는데도** 체결량만 0이면 "포기"로 선언하고 추적을
                        # 끊었다. 그 주문은 거래소에 살아남아 몇 시간 뒤에 체결되고,
                        # 봇이 모르는 무보호 포지션이 된다(핸드오프 사고 B 의 재발 원인).
                        # 실사고: 龙虾USDT 09:41 포기 -> 12:14 체결(2시간 33분 뒤).
                        # 오늘 "포기" 28건이 전부 같은 위험을 안고 있었다.
                        # 이제 **주문이 죽은 것이 확인됐을 때만** 추적을 끊는다.
                        if _status2 in ("CANCELED", "EXPIRED", "REJECTED"):
                            say(f"지정가 미체결 진입 포기 {_osym} ({_age:.0f}초 대기)")
                            entry_orders.pop(_osym, None)
                            pending.pop(_osym, None)
                            continue
                        # 취소 미확인 — 아직 살아 있을 수 있으므로 추적을 유지하고
                        # 다음 주기에 다시 취소를 시도한다. 추적을 유지하는 편이
                        # 유령 체결보다 언제나 안전하다(슬롯 한 칸이 비용의 전부다).
                        _o["cancel_fails"] = _o.get("cancel_fails", 0) + 1
                        _cf = _o["cancel_fails"]
                        if _cf in (3, 10) or _cf % 30 == 0:
                            say(f"경고 진입주문 취소 미확인 {_osym} "
                                f"status={_status2} {_cf}회 {_age:.0f}초 - "
                                f"추적 유지(유령 체결 방지)")
                        save_state()
                        continue
                if _exec <= 0 and _status in ("CANCELED", "EXPIRED", "REJECTED"):
                    say(f"진입주문 {_status} {_osym}")
                    entry_orders.pop(_osym, None)
                    pending.pop(_osym, None)
                    continue
                # 여기 오면 체결(전량 또는 부분)이다.
                # [중요] 부분체결이면 잔여 지정가가 아직 거래소에 살아 있다. 취소하지 않으면
                # 포지션을 등록한 뒤에도 계속 채워져 추적 수량과 실제가 어긋난다.
                # (기존 동기 버전은 대기 후 항상 취소하고 나서 판정했다 — 그 동작을 유지한다.)
                if _status != "FILLED":
                    try:
                        ex.cancel_regular_order(_osym, _o["order_id"])
                    except Exception:
                        pass
                    _s2, _e2 = entry_order_state(ex, _osym, _o["order_id"])
                    if _e2 > 0:
                        _exec = _e2
                entry_orders.pop(_osym, None)
                save_state()
                _side = _o["side"]
                _L = _side == "LONG"
                _stop = _o["stop"]
                _ent_t = time.time()
                _fill, _fqty = entry_fill_after(ex, _osym, _side, _o["since_id"],
                                                _o["price"], _exec or _o["qty"])
                _fqty = live_position_qty(ex, _osym, _fqty or _exec or _o["qty"])
                # [2026-08-25 ①] 거래소 평단을 정본으로. 체결 조회가 늦으면
                # entry_fill_after 가 신호 시점 가격으로 폴백해 평단이 통째로 틀어진다.
                _fill_src = _fill
                _fill = live_position_entry(ex, _osym, _fill)
                if _fill_src > 0 and abs(_fill - _fill_src) / _fill_src > 0.002:
                    say(f"평단 교정 {_osym}: {_fill_src:.8f} -> {_fill:.8f} (거래소 정본)")
                # [2026-08-26 통합] 체결가가 계획가와 다르면 손절폭이 달라진다.
                # 체결가 기준으로 하한/방향/상한을 다시 확정한다(안전망 경로와 동일).
                _stop_plan = _stop
                _stop = decide_stop(_fill, _side, args.leverage,
                                    args.new_max_stop_roe, stop_hint=_stop,
                                    tp_price=planned_tp(_fill, _side, args.leverage,
                                                        cm_target=float(_o.get("cm_target") or 0.0)))
                if _stop != _stop_plan:
                    say(f"체결가 기준 손절폭 재확정 {_osym}({_side}) "
                        f"{_stop_plan:.8f} -> {_stop:.8f} (체결{_fill:.8f})")
                # 방향 검증은 decide_stop 안에서 끝났지만, 폴백 로그는 남긴다.
                if not stop_is_sane(_fill, _stop, _side):
                    _bad = _stop
                    _stop = fallback_stop(_fill, _side)
                    say(f"경고 {_osym} 손절선 비정상({_side} 평단{_fill:.8f} 손절{_bad:.8f}) "
                        f"- 고정 1.2% 폴백 {_stop:.8f} 적용")
                _legs = list(_o["legs"]) or [_fill]
                _legs = [_fill] * len(_legs)
                positions[_osym] = Pos(
                    _osym, _side, _legs, _fqty, _ent_t, args.leverage,
                    stop_price=_stop,
                    tp_rr=fee_aware_rr_price(
                        _fill, _stop, _side, args.rr, args.roundtrip_fee_rate))
                positions[_osym].since_trade_id = _o["since_id"]
                # [2026-08-25] 원칙 0(CM) 최대 익절선 -pullback% 에 지정가 TP 를 실제로 건다.
                # CM 목표가 이미 진입가를 지나쳤으면(캐시 실측 26.5%) 0 이 나오고,
                # 그 경우는 기존 볼밴/RR 폴링 익절로 폴백한다 - 오늘과 동작이 같다.
                attach_tp(positions[_osym], _fill, args.leverage,
                          cm_target=float(_o.get("cm_target") or 0.0))
                try:
                    positions[_osym].stop_algo_id = sync_stop(
                        ex, _osym, _side, _fqty, _stop, 0, args.dry_run, say,
                        limit_price=stop_limit_px(_stop, _side))
                except StopAlreadyBreached:
                    close_breached(_osym)
                    continue
                if positions[_osym].stop_algo_id:
                    unprotected.pop(_osym, None)
                else:
                    unprotected[_osym] = {"next_retry_at": time.time() + 15.0,
                                          "last_warn_at": time.time()}
                if _osym in pending:
                    pending[_osym]["legs"] = list(_legs)
                    pending[_osym]["done"] = len(_legs)
                if drop_if_underfilled(_osym, float(_o.get("qty") or 0), _fqty):
                    continue
                entries.append((_ent_t, _side))
                save_state()
                n_entry += 1
                say(f"진입 {_osym} {_side} 평단{positions[_osym].entry:.6f} "
                    f"{len(_legs)}차 qty={_fqty} 손절{_stop:.6f} 대기{_age:.0f}s")

            # 신호 탐색
            if paused or not symbols:
                _scan_order = ()
            else:
                scan_offset %= len(symbols)
                _scan_order = symbols[scan_offset:] + symbols[:scan_offset]
                # 다음 사이클은 이번에 뒷줄이었던 곳부터 시작한다.
                scan_offset = (scan_offset + max(1, len(symbols) // 3)) % len(symbols)
            new_orders_this_cycle = 0
            for sym in _scan_order:
                if time.time() > deadline:
                    break
                if args.ws and not ws_ready:
                    continue
                if (args.max_new_orders_per_cycle > 0
                        and new_orders_this_cycle >= args.max_new_orders_per_cycle):
                    skips["사이클발주상한"] += 1
                    continue
                if sym in positions:
                    # e3 CM은 1슬롯 1회 진입만 허용한다. 기존 e2의 분할 재진입
                    # 분기를 의도적으로 차단한다.
                    continue
                elif len(positions) >= slots:
                    continue
                if cooldown.get(sym, 0) > time.time():
                    skips["쿨다운"] += 1
                    continue
                try:
                    df = signal_bars(ex, sym, args.signal_tf_min)
                    cm_sig = cm_signal_snapshot(ex, sym, args, chart_df=df)
                    if args.confirm_tf_min > 0:
                        confirm_df = signal_bars(ex, sym, args.confirm_tf_min)
                        confirm_sig = cm_signal_snapshot(ex, sym, args, chart_df=confirm_df)
                        if (not confirm_sig or not confirm_sig.get("signal") or
                                confirm_sig["signal"] != cm_sig.get("signal")):
                            skips["확인봉불일치"] += 1
                            pending.pop(sym, None)
                            continue
                    entry_df = (signal_bars(ex, sym, args.entry_tf_min)
                                if args.entry_tf_min > 0 else df)
                except Exception:
                    continue
                ind = indicators(entry_df)
                if not ind or not cm_sig:
                    continue
                L = cm_sig["signal"] == "LONG"
                S = cm_sig["signal"] == "SHORT"
                if not (L or S):
                    pending.pop(sym, None)
                    continue
                side = "LONG" if L else "SHORT"
                # [2026-08-26] 원칙 0 상위 타임프레임 필터. CM 이 정한 방향이
                # 상위 추세와 반대면 진입하지 않는다.
                #
                # [2026-08-27] **판정 불가(None)도 이제는 막는다.**
                # 종전엔 통과시켰다(원칙 1 보호). 그런데 3일 12만건 실측에서
                # 이 필터가 순익 부호를 가르는 유일한 요소로 확인됐다:
                #   1군 필터없음 15분 +0.0341% -> 수수료 차감 -0.0521%
                #   1군 +4h필터  15분 +0.0866% -> 수수료 차감 +0.0004%
                # 판정 불가를 통과시키면 기동 직후 캐시가 비어 있는 30초 동안
                # **필터가 통째로 꺼진 상태로 진입**한다. 그 구간이 곧 손실 구간이다.
                # 신호는 시간당 1,097건인데 봇은 18건(1.7%)만 소화하므로,
                # 캐시가 찰 때까지 건너뛰어도 원칙 1 에 실질 영향이 없다.
                _htf_counter = False
                if args.cm_htf_filter:
                    _up = htf_uptrend(ex, sym, args.cm_htf_interval, args.cm_htf_ema)
                    if _up is None:
                        skips["상위추세미확정"] += 1
                        pending.pop(sym, None)
                        continue
                    if _up != L:
                        if args.cm_htf_counter_mult <= 0:
                            skips["상위추세역행"] += 1
                            pending.pop(sym, None)
                            continue
                        # 차단하지 않는다(원칙 1). 아래 증거금 계산에서 크기만 줄인다.
                        _htf_counter = True
                # [2026-08-27] 전환 후 N봉 이내만. 위 4h 필터를 통과한 뒤에 본다.
                if args.cm_flip_max_bars >= 0:
                    _fa = flip_age(df, args.signal_tf_min, L)
                    if _fa is None or _fa > args.cm_flip_max_bars:
                        skips["전환경과"] += 1
                        pending.pop(sym, None)
                        continue
                if args.same_side_stop_cooldown_sec > 0:
                    _now = time.time()
                    _hist = [t for t in stop_history[side]
                             if _now - t <= args.same_side_stop_cooldown_sec]
                    stop_history[side] = _hist
                    if len(_hist) >= 2:
                        skips["동일방향연속손절"] += 1
                        pending.pop(sym, None)
                        continue
                # [2026-08-26 개선④] 같은 방향 편중 제한.
                # 크립토는 BTC 에 동조하므로 같은 방향 7슬롯은 분산이 아니라 한 베팅의
                # 7배 레버리지다. 실제로 오늘 아침 7슬롯이 전부 SHORT 였다.
                # 원장 168건 실측 — 진입 시점에 같은 방향을 몇 개 들고 있었는가로 가르면:
                #   0~2개 130건(77%) 승률 53.1% 건당 -0.113
                #   3개+   38건(23%) 승률 36.8% 건당 -0.423   <- 거래 23%가 손실의 52%
                # 이 그룹을 막으면 순손익 -30.78 -> -14.71 (적자 52% 축소), 거래수 -23%.
                # 원칙 1(거래 활발)을 해치지만 잘리는 23% 가 가장 나쁜 23% 다.
                if args.max_same_side > 0 and sym not in positions:
                    # [2026-08-26] 이 게이트는 positions(안전망 채택분 제외) + **미체결
                    # 진입주문**을 함께 센다. 진단 로그로 확인한 실제 구성 예:
                    #   ssc=3 = positions[TRUMP:LONG] + entry_orders[CYS:LONG, SPX:LONG]
                    # 보유가 1건뿐인데 상한에 걸리는 것은 미체결 주문 때문이다(의도된 동작).
                    if same_side_count(positions, entry_orders, side) >= args.max_same_side:
                        skips["방향편중"] += 1
                        pending.pop(sym, None)
                        continue
                if sym not in pending:
                    # [2026-08-26] 신호가 켜진 봉의 종가를 남긴다. 진입가가 이보다
                    # 얼마나 유리한지가 "추격이냐 눌림이냐"의 실질 기준이다.
                    # 기존 눌림 판정(EMA5 터치)은 추세장에서 현재가를 바짝 따라가
                    # 사실상 시장가 진입이 된다(진입가-EMA5 편차 중앙 +0.084%).
                    pending[sym] = {"side": side, "legs": [], "since": time.time(),
                                    "sig_close": float(df["close"].iloc[-1])}
                    n_align += 1
                pd = pending[sym]
                # A pending CM signal is only valid for the entry TTL. Previously
                # this was 3600s, allowing stale signals to become late chase entries.
                if pd["side"] != side or time.time() - pd["since"] > args.entry_order_ttl_sec:
                    if time.time() - pd["since"] > args.entry_order_ttl_sec:
                        skips["신호노후"] += 1
                    pending.pop(sym, None)
                    continue
                # CM이 방향을 정하면, 실제 체결은 기존 EMA 눌림 진입선을 사용한다.
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
                # [2026-08-26 개선⑥] 너무 깊게 눌린 자리는 들어가지 않는다.
                # 캐시 132건 교차검증(심볼 반분): 상한 0.5% 에서
                #   A그룹 54건 +0.41% / B그룹 50건 +0.38%  (미적용 전체 -0.36%)
                # 양쪽이 거의 같아 과적합 가능성이 낮다. 잔존 79%(거래수 -21%).
                # 더 조이면(0.4%) A -0.03 / B +0.87 로 흔들려 과적합 냄새가 난다.
                if args.max_pullback_pct > 0:
                    _pb = pullback_depth_pct(entry, ind.get("hma20") or 0.0)
                    if _pb > args.max_pullback_pct:
                        skips["눌림과다"] += 1
                        pending.pop(sym, None)
                        continue
                stop = ind["e25"]
                risk = abs(entry - stop) / entry
                # 손절폭이 너무 좁으면 노이즈에 바로 털린다
                # [2026-08-27] **고정폭 손절을 쓰면 이 필터의 전제가 깨진다.**
                # 근거는 "EMA25 가 붙어 손절폭이 0.09%까지 좁아져 노이즈에 즉시 손절"
                # 이었는데, --stop-fixed-roe 는 손절을 EMA25 와 무관하게 고정한다.
                # 그대로 두면 **없는 위험을 이유로 거래를 버린다**(스킵의 5~18%).
                if args.stop_fixed_roe <= 0 and risk * 100 < args.min_risk_pct:
                    skips["근접손절"] += 1
                    pending.pop(sym, None)
                    continue
                # [2026-08-26] 손절폭 하한(ROE 기준). min_risk_pct 는 가격 기준이라
                # 레버리지가 반영되지 않아 체감과 어긋난다. ROE 로 직접 건다.
                if args.min_stop_roe > 0:
                    if risk * 100 * args.leverage < args.min_stop_roe:
                        skips["손절폭부족"] += 1
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
                    # [2026-08-27 진단] 이 게이트는 **볼밴** 기준이라 원칙 0 위배다.
                    # 그런데 12:22 배포로 볼밴 청산(tp_bb)을 없앴으므로, 지금은
                    # **봇이 쓰지도 않는 선**으로 진입을 막고 있다.
                    # 그럼에도 실질은 "이미 크게 움직인 자리" 필터일 수 있고,
                    # 그 유형은 이 봇에서 네 번 독립적으로 나쁘다고 확인됐다.
                    # 지우기 전에 **차단이 옳았는지**를 봉으로 되짚어 재려면 후보를
                    # 남겨야 한다. 카운터만으로는 사후 검증이 불가능하다.
                    log_line(f"[진단] 익절선통과(계획가) {sym} {side} "
                             f"진입{entry:.8f} 볼밴선{tp_bb0:.8f} "
                             f"CM목표{(ind.get('cm_tp_long') if L else ind.get('cm_tp_short')) or 0:.8f}")
                    pending.pop(sym, None)
                    continue
                # [2026-08-26] CM 최대익절선이 무효면(목표가 이미 진입가를 지나침)
                # 그 움직임은 이미 끝난 것이다. 지금까지는 볼밴/RR 폴링 익절로 폴백해
                # 그냥 들어갔는데, 원장이 이 그룹을 명확히 가른다.
                #   ④ 이후 봇 진입 57건(재시작 채택분 제외 - 그쪽은 맥락 유실이라 교란):
                #     CM TP 등록됨 34건 승률58.8% 손절률32.4% 건당-0.068
                #     CM TP 무효   23건 승률21.7% 손절률78.3% 건당-0.461 (t=-2.19)
                #   시간구간 교차검증(둘 다 같은 방향):
                #     09:37~13:00  등록 68.8%/+0.151  vs  무효 41.7%/-0.053
                #     13:00~       등록 50.0%/-0.263  vs  무효  0.0%/-0.905 (11건 전부 손절)
                # 원칙 0 지킴(오히려 CM 을 더 충실히 따른다) / 원칙 1 해침(거래수 -40%)
                # / 원칙 2 지킴(승률 43.9%->58.8%, 합계 -12.92 -> -2.33).
                # 같은 결론이 네 경로에서 나왔다 - 눌림깊이(⑥), 동일방향 U자,
                # TP목표ROE 상관(r=-0.261), 그리고 이것. 전부 "이미 크게 움직인 자리".
                if args.require_cm_tp:
                    if cm_tp_price(ind, entry, side, args.cm_tp_pullback_pct,
                                   args.leverage, args.cm_tp_max_roe) <= 0:
                        skips["CM익절선무효"] += 1
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
                    # [2026-08-26 개선③] 이건 마크가격 기준의 **순간값**이다. 봉 기준
                    # 근접손절(위쪽 분기)과 달리 다음 폴링에 폭이 벌어질 수 있는데,
                    # pending 을 통째로 버려서 그때까지 쌓은 legs(터치한 진입 목표선)가
                    # 같이 날아갔다. 후보를 폐기하지 말고 이번 회차만 건너뛴다.
                    # 필터 문턱(min_risk_pct)은 그대로다 - 진입 조건은 완화되지 않는다.
                    skips["근접손절순간"] += 1
                    continue
                # [2026-08-26 통합] 필터를 통과한 뒤 손절선을 확정한다(진입 종목 수 불변).
                # 계획 진입가 기준이며, 체결가가 다르면 체결 경로가 다시 확정한다.
                stop = decide_stop(entry, side, args.leverage,
                                   args.new_max_stop_roe, stop_hint=stop, ind=ind,
                                   tp_price=planned_tp(entry, side, args.leverage, ind=ind))
                if tp_bb0 and ((price >= tp_bb0) if L else (price <= tp_bb0)):
                    skips["익절선통과"] += 1
                    log_line(f"[진단] 익절선통과(마크가) {sym} {side} "
                             f"마크{price:.8f} 진입{entry:.8f} 볼밴선{tp_bb0:.8f} "
                             f"CM목표{(ind.get('cm_tp_long') if L else ind.get('cm_tp_short')) or 0:.8f}")
                    pending.pop(sym, None)
                    continue
                # [2026-08-20] 한 봉에서 EMA5·EMA10 을 동시에 터치하면 legs 가 두 개
                # 잡힌다. 그때 1/3 만 넣으면 그 차수의 자본이 통째로 누락된다.
                # 아직 집행하지 않은 차수만큼(_new_legs) 곱해서 넣는다.
                _new_legs = max(1, len(pd["legs"]) - pd.get("done", 0))
                margin = max(args.min_leg_margin, bal * size / args.tranches * _new_legs)
                _pre_mult = margin          # 축소배율 적용 전 값(하한 로그 판정용)
                # [2026-08-26] 진입 우위(신호봉 종가 대비)가 부족하면 크기를 줄인다.
                # **차단하지 않는다** - 차단하면 통과율 35%로 거래수가 65% 줄어
                # 원칙 1(시간당 14건 하한)을 위반한다.
                # 변형-초스캘프 원칙 3: 신호봉 종가 대비 최소 우위가 없으면
                # 추격 진입으로 간주해 주문 자체를 차단한다.
                if args.min_entry_edge_pct > 0:
                    _sc = float(pd.get("sig_close") or 0.0)
                    if _sc > 0:
                        _edge = ((_sc - entry) / _sc * 100.0) if L else ((entry - _sc) / _sc * 100.0)
                        if _edge < args.min_entry_edge_pct:
                            log_line(f"추격 진입 차단 {sym} {side}: "
                                     f"우위{_edge:+.3f}% < {args.min_entry_edge_pct:.2f}%")
                            skips["추격진입차단"] += 1
                            pending.pop(sym, None)
                            continue
                # [2026-08-26] 상위추세 역행이면 크기를 줄인다(차단하지 않음 - 원칙 1).
                if _htf_counter and args.cm_htf_counter_mult > 0:
                    _before = margin
                    margin = margin * args.cm_htf_counter_mult
                    log_line(f"상위추세 역행 크기축소 {sym} {side}: "
                             f"{_before:.2f} -> {margin:.2f} "
                             f"(x{args.cm_htf_counter_mult})")
                # CM TP가 무효한 거래는 방향 신호는 유효하지만 익절 구조가 폴백으로
                # 바뀐다. 기본값은 1.0/0이라 기존 라이브 동작과 완전히 같다.
                _cm_tp_valid = cm_tp_price(
                    ind, entry, side, args.cm_tp_pullback_pct,
                    args.leverage, args.cm_tp_max_roe) > 0
                if (not _cm_tp_valid and args.cm_invalid_tp_size_mult > 0
                        and args.cm_invalid_tp_size_mult < 1.0):
                    _before = margin
                    margin = margin * args.cm_invalid_tp_size_mult
                    log_line(f"CM TP 무효 크기축소 {sym} {side}: "
                             f"{_before:.2f} -> {margin:.2f} "
                             f"(x{args.cm_invalid_tp_size_mult})")
                # [2026-08-26 P0] **축소배율은 하한 아래로 내려갈 수 없다.**
                # 종전엔 하한을 먼저 적용하고 그 뒤에 배율을 곱해서, 배율이 겹치면
                # 증거금이 9 USDT 대까지 떨어졌다(실측 21:36 REUSDT 31.96 -> 9.59).
                # 그런데 **슬롯은 크기와 무관하게 한 자리를 통째로 차지한다** —
                # 동시보유 10 / 같은방향 3 상한을 9 USDT 짜리가 점유하면, 그 자리에
                # 들어왔을 정상 크기 진입이 밀린다. 즉 축소가 원칙 1(거래 활발)을
                # 슬롯 쪽에서 갉아먹는다. 크기를 줄이려면 아예 안 들어가는 게 맞고,
                # 들어갈 거면 하한은 지킨다.
                # 하한이 배율보다 커서 축소가 무력해지는 잔고대가 있다는 것은
                # 감수한다 - 그건 하한/상한을 조정할 문제지 슬롯을 낭비할 이유가 아니다.
                _margin_floor = args.min_leg_margin
                if (not _cm_tp_valid and args.cm_invalid_tp_min_margin > 0):
                    _margin_floor = min(_margin_floor, args.cm_invalid_tp_min_margin)
                if margin < _margin_floor:
                    if abs(margin - _pre_mult) > 1e-9:
                        log_line(f"축소 하한 적용 {sym}: {margin:.2f} -> "
                                 f"{_margin_floor:.2f} (최소 증거금)")
                    margin = _margin_floor
                # [2026-08-25] 잔고가 줄수록 건당 크기가 커지는 피드백을 상한으로 끊는다.
                if args.max_leg_margin > 0:
                    # _new_legs 는 "한 봉에서 여러 차수 목표를 동시에 터치"한 경우를 위한
                    # 배수인데, tranches 를 넘을 수 없다. 넘으면 그것 자체가 이상값이라
                    # 상한 계산에서 tranches 로 자른다(실측: tranches=1 인데 81 USDT 진입).
                    _legs_for_cap = max(1, min(_new_legs, args.tranches))
                    _capped = min(margin, args.max_leg_margin * _legs_for_cap)
                    if _capped < margin:
                        log_line(f"증거금 상한 적용 {sym}: {margin:.2f} -> {_capped:.2f}")
                    margin = _capped
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
                    # [2026-08-25 B안] 신규 진입은 주문만 내고 넘어간다(블로킹 0).
                    # 추가 차수(2·3차)는 기존 동기 경로를 그대로 둔다 — tranches=1이면 안 탄다.
                    if sym not in positions:
                        if args.cm_recheck_on_entry:
                            _latest_cm = cm_signal_snapshot(ex, sym, args)
                            _cm_ok = bool(_latest_cm and _latest_cm.get("signal") == side)
                            if _cm_ok and args.cm_htf_filter:
                                _cm_htf = htf_uptrend(ex, sym, args.cm_htf_interval, args.cm_htf_ema)
                                _cm_ok = _cm_htf is not None and _cm_htf == (side == "LONG")
                            if _cm_ok and args.cm_flip_max_bars >= 0:
                                _cm_age = flip_age(signal_bars(ex, sym, args.signal_tf_min),
                                                    args.signal_tf_min, side == "LONG")
                                _cm_ok = _cm_age is not None and _cm_age <= args.cm_flip_max_bars
                            if not _cm_ok:
                                say(f"진입 직전 CM 재확인 실패 {sym} {side} - 지정가 미발주")
                                pending.pop(sym, None)
                                continue
                        try:
                            ex.set_leverage(sym, args.leverage)
                            _oid, _px = place_limit_entry_nowait(ex, sym, side, qty)
                        except Exception as e:
                            say(f"진입실패 {sym}: {e}")
                            pending.pop(sym, None)
                            continue
                        if not _oid:
                            pending.pop(sym, None)
                            continue
                        entry_orders[sym] = {
                            "order_id": _oid, "side": side, "qty": qty, "stop": stop,
                            "bb_target": (ind["bb_u"] if L else ind["bb_l"]),
                            "cm_target": (ind.get("cm_tp_long") if L else ind.get("cm_tp_short")) or 0.0,
                            "since_id": _since_id, "legs": list(pd["legs"]),
                            "placed_at": time.time(), "signal_at": float(pd.get("since") or time.time()),
                            "price": _px,
                        }
                        say(f"진입주문 발주 {sym} {side} 신호후{time.time() - entry_orders[sym]['signal_at']:.1f}s "
                            f"TTL{args.entry_order_ttl_sec:.0f}s")
                        new_orders_this_cycle += 1
                        # 주문을 낸 즉시 상태파일에 남긴다. 체결과 재시작 사이의 틈에서
                        # 무보호 고아 포지션이 생기는 것을 막는다(ONGUSDT 실사고).
                        # _owned 에도 넣어야 미추적 안전망이 이 심볼을 '우리 것'으로 본다.
                        _owned.add(sym)
                        save_state()
                        continue
                    try:
                        ex.set_leverage(sym, args.leverage)
                        if not place_v2_limit_entry(ex, sym, side, qty, wait_sec=10.0):
                            say(f"지정가 미체결 진입 포기 {sym}")
                            pending.pop(sym, None)
                            continue
                    except Exception as e:
                        say(f"진입실패 {sym}: {e}")
                        pending.pop(sym, None)
                        continue
                    entry_fill, filled_qty = entry_fill_after(
                        ex, sym, side, _since_id, price, qty)
                else:
                    entry_fill, filled_qty = price, qty
                # [2026-08-27] **체결가 기준 손절폭 재확정** — 이 경로에만 빠져 있었다.
                # 비동기 체결 경로(3900행대)에는 있는데 여기(동기/추가차수/dry-run)엔
                # 없어서, 계획가와 체결가가 다르면 손절폭이 어긋났다.
                # 실측(그림자 dry-run): --stop-fixed-roe 6.0 인데 실제 6.57 / 11.36 /
                # 2.78% 로 흩어졌다. 라이브는 다른 경로를 타서 정확히 8.00% 였다.
                # 경로마다 동작이 다른 오늘의 반복 유형이다.
                if entry_fill > 0:
                    _sp = decide_stop(entry_fill, side, args.leverage,
                                      args.new_max_stop_roe, stop_hint=stop, ind=ind,
                                      tp_price=planned_tp(entry_fill, side,
                                                          args.leverage, ind=ind))
                    if _sp > 0 and abs(_sp - stop) / max(stop, 1e-12) > 1e-9:
                        log_line(f"체결가 기준 손절폭 재확정 {sym}({side}) "
                                 f"{stop:.8f} -> {_sp:.8f} (체결{entry_fill:.8f})")
                        stop = _sp
                # [2026-08-20 버그1] 손절주문은 아래 분기에서 sync_stop 으로만 건다.
                # 여기서 미리 걸면 추가 진입 분기가 그것을 취소하지 않아 고아 주문이 남는다.
                _ent_t = time.time()
                _signal_at = float(_o.get("signal_at") or _o.get("placed_at") or _ent_t)
                say(f"체결 지연 {_osym} {side} 신호->발주{float(_o.get('placed_at') or _ent_t) - _signal_at:.1f}s "
                    f"발주->체결{_ent_t - float(_o.get('placed_at') or _ent_t):.1f}s "
                    f"총{_ent_t - _signal_at:.1f}s")
                if sym in positions:
                    # 2·3차: 기존 포지션에 합산하고 손절/익절선을 평단 기준으로 갱신
                    _p = positions[sym]
                    _p.legs = list(_p.legs) + [entry_fill] * _new_legs
                    pd["legs"] = list(_p.legs)
                    # 내부 누적은 한 번만 어긋나도 영구히 틀어진다. 거래소를 정본으로.
                    _p.qty = live_position_qty(ex, sym, _p.qty + (filled_qty or qty))
                    _p.stop_price = stop
                    _p.tp_rr = fee_aware_rr_price(
                        _p.entry, stop, side, args.rr, args.roundtrip_fee_rate)
                    if args.cm_tp_limit:
                        _cmtp = cm_tp_price(ind, _p.entry, side, args.cm_tp_pullback_pct,
                                            args.leverage, args.cm_tp_max_roe)
                        _p.tp_limit_price = _cmtp
                        # 수량이 바뀌었으므로 무효가 됐더라도 기존 주문은 반드시 지운다.
                        if _cmtp > 0:
                            sync_tp_limit(ex, _p, args.dry_run, say)
                        else:
                            cancel_tp_limit(ex, _p, args.dry_run)
                    try:
                        _p.stop_algo_id = sync_stop(ex, sym, side, _p.qty, stop,
                                                    _p.stop_algo_id, args.dry_run, say,
                                                    limit_price=stop_limit_px(stop, side))
                    except StopAlreadyBreached:
                        close_breached(sym)
                        continue
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
                    tp_rr=fee_aware_rr_price(
                        entry_fill, stop, side, args.rr, args.roundtrip_fee_rate))
                positions[sym].since_trade_id = _since_id
                try:
                    positions[sym].stop_algo_id = sync_stop(
                        ex, sym, side, qty, stop, 0, args.dry_run, say,
                        limit_price=stop_limit_px(stop, side))
                except StopAlreadyBreached:
                    close_breached(sym)
                    continue
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
                    beat("align")
                    handle_buttons()
                    retry_unprotected_stops(time.time())
                    reconcile_live_positions(time.time())
                    tick_fast(time.time())
                    guard_giveback(time.time())
                    time.sleep(min(2.0, wait - 0.5))
            else:
                time.sleep(args.poll)
        except KeyboardInterrupt:
            say("사용자 중단")
            break
        except Exception as e:
            # [2026-08-25] print 로만 나가서 관측이 안 됐다. 이 except 는 사이클 전체를
            # 삼키므로(진입 0건) 반드시 런로그에 남겨야 한다.
            log_line(f"[주기오류] {type(e).__name__}: {e}")
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

