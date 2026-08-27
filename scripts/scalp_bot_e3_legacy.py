from __future__ import annotations

import argparse
import atexit
import json
import logging
import math
import os
import signal
import statistics
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bot.config import Config
from bot.exchange import Exchange
from bot.grid_e3 import GridState, build_grid_levels

VERSION = "e3"
ROOT = Path(__file__).resolve().parent.parent
LOG_DIR = ROOT / "logs"
LEDGER = LOG_DIR / f"scalp_bot_{VERSION}_ledger.jsonl"
STATE = LOG_DIR / f"scalp_bot_{VERSION}_state.json"
BOT_PID_FILE = LOG_DIR / f"scalp_bot_{VERSION}_bot_pid.json"


class Tg:
    def __init__(self, cfg: Config):
        self.token = cfg.telegram_bot_token
        self.chat = cfg.telegram_chat_id
        self.enabled = bool(self.token and self.chat)
        self.offset = 0

    def _api(self, method: str, payload: dict, timeout: float = 10):
        if not self.enabled:
            return None
        import urllib.request
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{self.token}/{method}",
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
        )
        try:
            return json.loads(urllib.request.urlopen(req, timeout=timeout).read())
        except Exception:
            return None

    def send(self, text: str, reply_markup: dict | None = None) -> None:
        payload = {"chat_id": self.chat, "text": text}
        if reply_markup:
            payload["reply_markup"] = reply_markup
        self._api("sendMessage", payload)

    BUTTONS = {
        "📊 e3상태": "status",
        "🪜 e3격자": "gridview",
        "📈 e3브리핑": "brief",
        "⏸ e3정지": "pause",
        "▶️ e3재개": "resume",
        "🛑 e3전량정리": "flat",
    }

    def send_photo(self, png: bytes, caption: str = "") -> None:
        """차트 이미지를 보낸다. 텍스트 사다리는 격자가 많으면 한눈에 안 들어온다.

        sendPhoto 는 multipart 라 _api(JSON) 를 쓸 수 없어 직접 조립한다.
        """
        if not self.enabled or not png:
            return
        import urllib.request
        boundary = "----e3grid" + str(int(time.time() * 1000))
        parts = []
        for key, val in (("chat_id", str(self.chat)), ("caption", caption[:1000])):
            if val:
                parts.append(
                    f"--{boundary}\r\nContent-Disposition: form-data; name=\"{key}\"\r\n\r\n{val}\r\n"
                    .encode())
        parts.append(
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"photo\";"
            f" filename=\"grid.png\"\r\nContent-Type: image/png\r\n\r\n".encode())
        parts.append(png)
        parts.append(f"\r\n--{boundary}--\r\n".encode())
        body = b"".join(parts)
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{self.token}/sendPhoto",
            data=body,
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
        try:
            urllib.request.urlopen(req, timeout=20).read()
        except Exception:
            pass

    def menu(self) -> None:
        self.send(f"[{VERSION}] 조작 메뉴를 하단에 고정했습니다.", {
            "keyboard": [
                [{"text": "🪜 e3격자"}],
                [{"text": "📊 e3상태"}, {"text": "📈 e3브리핑"}],
                [{"text": "⏸ e3정지"}, {"text": "▶️ e3재개"}],
                [{"text": "🛑 e3전량정리"}],
            ],
            "resize_keyboard": True,
            "is_persistent": True,
        })

    def poll(self) -> list[tuple[str, str]]:
        res = self._api(
            "getUpdates",
            {"offset": self.offset, "timeout": 0, "allowed_updates": ["callback_query", "message"]},
            timeout=8,
        )
        out: list[tuple[str, str]] = []
        if not res or not res.get("ok"):
            return out
        for upd in res.get("result", []):
            self.offset = upd["update_id"] + 1
            txt = ((upd.get("message") or {}).get("text") or "").strip()
            if not txt:
                continue
            if txt in ("/e3", "e3", "/menu"):
                self.menu()
            elif txt in self.BUTTONS:
                out.append(("", self.BUTTONS[txt]))
        return out


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        try:
            out = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            return str(pid) in (out.stdout or "")
        except Exception:
            return False
    try:
        os.kill(pid, 0)
        return True
    except Exception:
        return False


def acquire_bot_lock() -> bool:
    try:
        if BOT_PID_FILE.exists():
            old = json.loads(BOT_PID_FILE.read_text(encoding="utf-8"))
            opid = int(old.get("pid") or 0)
            if opid and opid != os.getpid() and _pid_alive(opid):
                print(
                    f"[중단] 이미 {VERSION} 봇이 실행 중입니다 (PID {opid}).",
                    flush=True,
                )
                return False
    except Exception:
        pass
    BOT_PID_FILE.write_text(
        json.dumps({"pid": os.getpid(), "ts": time.time()}),
        encoding="utf-8",
    )
    return True


def release_bot_lock() -> None:
    try:
        if BOT_PID_FILE.exists():
            cur = json.loads(BOT_PID_FILE.read_text(encoding="utf-8"))
            if int(cur.get("pid") or 0) == os.getpid():
                BOT_PID_FILE.unlink()
    except Exception:
        pass


@dataclass
class GridOrder:
    order_id: int
    side: str
    rung: int
    price: float
    quantity: float


@dataclass
class CycleState:
    symbol: str
    center_price: float
    started_at: float
    wallet_balance_start: float
    levels: list[float]
    qty_per_rung: float
    held_buy_rungs: list[int] = field(default_factory=list)
    buy_orders: dict[str, dict] = field(default_factory=dict)
    sell_orders: dict[str, dict] = field(default_factory=dict)
    realized_grid_profit_est: float = 0.0
    reset_count: int = 0
    last_fill_at: float = 0.0


def append_ledger(rec: dict) -> None:
    LEDGER.parent.mkdir(parents=True, exist_ok=True)
    with LEDGER.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def save_state(state: CycleState) -> None:
    STATE.write_text(json.dumps(asdict(state), ensure_ascii=False, indent=2), encoding="utf-8")


def load_state() -> CycleState | None:
    if not STATE.exists():
        return None
    try:
        data = json.loads(STATE.read_text(encoding="utf-8"))
        return CycleState(**data)
    except Exception:
        return None


def _price_close(a: float, b: float, tol: float = 1e-9) -> bool:
    return abs(a - b) <= max(abs(a), abs(b), 1.0) * tol


def parse_symbol_csv(raw: str) -> set[str]:
    return {part.strip().upper() for part in (raw or "").split(",") if part.strip()}


def fetch_open_regular_orders(ex: Exchange, symbol: str) -> list[dict]:
    try:
        return list(ex.client.futures_get_open_orders(symbol=symbol))
    except Exception:
        return []


def reconcile_cycle_state_with_exchange(
    ex: Exchange,
    state: CycleState,
    cancel_duplicates: bool = False,
    cancel_stale: bool = False,
) -> CycleState:
    # [2026-08-21] 조회 실패를 '주문 없음' 으로 오인하면 안 된다.
    # fetch_open_regular_orders 는 예외 시 빈 목록을 주는데, 그대로 대조하면
    # 살아 있는 주문을 상태에서 지워버리고 다음 사이클에 중복 주문을 낸다.
    # 증거금을 두 번 묶고 -2019 로 이어진다. 실패하면 상태를 그대로 둔다.
    try:
        open_orders = list(ex.client.futures_get_open_orders(symbol=state.symbol))
    except Exception:
        return state
    buy_orders: dict[str, dict] = {}
    sell_orders: dict[str, dict] = {}
    seen_buy: set[int] = set()
    seen_sell: set[int] = set()
    qty_tol = max(abs(state.qty_per_rung), 1.0) * 0.02
    to_cancel: list[int] = []
    level_to_buy_rung: dict[float, int] = {}
    level_to_sell_rung: dict[float, int] = {}
    for rung, level in enumerate(state.levels[:-1]):
        level_to_buy_rung[ex.round_price(state.symbol, level)] = rung
    for rung, level in enumerate(state.levels[1:], start=1):
        level_to_sell_rung[ex.round_price(state.symbol, level)] = rung

    for order in open_orders:
        try:
            price = float(order.get("price", 0) or 0)
            qty = float(order.get("origQty", 0) or 0)
            order_id = int(order["orderId"])
        except (TypeError, ValueError, KeyError):
            continue
        if abs(qty - state.qty_per_rung) > qty_tol:
            continue
        side = str(order.get("side"))
        reduce_only = bool(order.get("reduceOnly"))
        rounded_price = ex.round_price(state.symbol, price)

        if side == "BUY" and not reduce_only:
            rung = level_to_buy_rung.get(rounded_price)
            if rung is None:
                if cancel_stale:
                    to_cancel.append(order_id)
                continue
            if rung in seen_buy:
                if cancel_duplicates:
                    to_cancel.append(order_id)
                continue
            seen_buy.add(rung)
            buy_orders[str(rung)] = {
                "order_id": order_id,
                "rung": rung,
                "price": state.levels[rung],
                "quantity": state.qty_per_rung,
            }
        elif side == "SELL" and reduce_only:
            rung = level_to_sell_rung.get(rounded_price)
            if rung is None:
                if cancel_stale:
                    to_cancel.append(order_id)
                continue
            if rung in seen_sell:
                if cancel_duplicates:
                    to_cancel.append(order_id)
                continue
            seen_sell.add(rung)
            sell_orders[str(rung)] = {
                "order_id": order_id,
                "rung": rung,
                "price": state.levels[rung],
                "quantity": state.qty_per_rung,
            }

    if cancel_duplicates or cancel_stale:
        for order_id in to_cancel:
            try:
                ex.cancel_regular_order(state.symbol, order_id)
            except Exception:
                pass

    held_buy_rungs = sorted(max(0, int(v["rung"]) - 1) for v in sell_orders.values())
    pos = ex.get_position(state.symbol)
    if pos and abs(float(pos["amount"])) > 0 and not held_buy_rungs and state.held_buy_rungs:
        held_buy_rungs = list(state.held_buy_rungs)
    state.buy_orders = buy_orders
    state.sell_orders = sell_orders
    state.held_buy_rungs = held_buy_rungs
    return state


def quantity_for_rung(
    ex: Exchange,
    symbol: str,
    budget_usdt: float,
    leverage: int,
    grid_count: int,
    mark_price: float,
) -> float:
    per_rung_notional = (budget_usdt * leverage) / grid_count
    raw_qty = per_rung_notional / mark_price
    return ex.round_quantity(
        symbol,
        raw_qty,
        price=mark_price,
        max_notional=per_rung_notional,
    )


def score_candidate_symbol(
    ex: Exchange,
    symbol: str,
    wallet_balance: float,
    leverage: int,
    grid_count: int,
    capital_usage: float,
    min_width_pct: float = 4.0,
    min_mean_abs_1m_pct: float = 0.08,
    max_spread_pct: float = 0.12,
    max_abs_ret_24h_pct: float = 15.0,
) -> dict | None:
    try:
        df = ex.get_klines(symbol, limit=240, interval="1m")
        closes = df["close"].astype(float).tolist()
        highs = df["high"].astype(float).tolist()
        lows = df["low"].astype(float).tolist()
        if len(closes) < 180:
            return None
        last = closes[-1]
        qty = quantity_for_rung(
            ex, symbol, wallet_balance * capital_usage, leverage, grid_count, last
        )
        if qty <= 0:
            return None
        abs_1m = [abs((closes[i] / closes[i - 1] - 1.0) * 100.0) for i in range(1, len(closes))]
        mean_abs_1m = statistics.mean(abs_1m) if abs_1m else 0.0
        width_pct = ((max(highs) - min(lows)) / last) * 100.0 if last > 0 else 0.0
        ret_60 = ((last / closes[-60]) - 1.0) * 100.0
        ret_180 = ((last / closes[0]) - 1.0) * 100.0
        book = ex.get_book_ticker(symbol)
        ticker24 = ex.get_24h_ticker(symbol)
        ret_24h = float(ticker24.get("price_change_pct", 0.0) or 0.0)
        quote_volume = float(ticker24.get("quote_volume", 0.0) or 0.0)
        spread_pct = ((book["ask"] - book["bid"]) / last) * 100.0 if last > 0 else 0.0
        if width_pct < min_width_pct:
            return None
        if mean_abs_1m < min_mean_abs_1m_pct:
            return None
        if spread_pct > max_spread_pct:
            return None
        if abs(ret_24h) > max_abs_ret_24h_pct:
            return None
        volume_bonus = min(6.0, max(0.0, math.log10(max(quote_volume, 1.0)) - 5.0) * 2.0)
        score = (
            width_pct * 2.1
            + mean_abs_1m * 18.0
            + volume_bonus
            + max(0.0, 12.0 - spread_pct * 80.0)
            - abs(ret_60) * 1.5
            - abs(ret_180) * 0.7
            - abs(ret_24h) * 1.6
            - spread_pct * 15.0
        )
        return {
            "symbol": symbol,
            "score": round(score, 4),
            "price": round(last, 8),
            "width_pct": round(width_pct, 4),
            "mean_abs_1m_pct": round(mean_abs_1m, 4),
            "ret_60_pct": round(ret_60, 4),
            "ret_180_pct": round(ret_180, 4),
            "ret_24h_pct": round(ret_24h, 4),
            "spread_pct": round(spread_pct, 5),
            "quote_volume": round(quote_volume, 2),
            "qty_per_rung": qty,
        }
    except Exception:
        return None


def rank_candidate_symbols(
    ex: Exchange,
    wallet_balance: float,
    leverage: int,
    grid_count: int,
    capital_usage: float,
    candidate_limit: int = 60,
    top_n: int = 12,
    exclude_symbols: set[str] | None = None,
    min_width_pct: float = 4.0,
    min_mean_abs_1m_pct: float = 0.08,
    max_spread_pct: float = 0.12,
    max_abs_ret_24h_pct: float = 15.0,
) -> list[dict]:
    exclude_symbols = {s.upper() for s in (exclude_symbols or set())}
    ranked: list[dict] = []
    for symbol in ex.get_active_usdt_perpetual_symbols(limit=candidate_limit):
        if symbol.upper() in exclude_symbols:
            continue
        row = score_candidate_symbol(
            ex,
            symbol,
            wallet_balance,
            leverage,
            grid_count,
            capital_usage,
            min_width_pct=min_width_pct,
            min_mean_abs_1m_pct=min_mean_abs_1m_pct,
            max_spread_pct=max_spread_pct,
            max_abs_ret_24h_pct=max_abs_ret_24h_pct,
        )
        if row is not None:
            ranked.append(row)
    ranked.sort(key=lambda x: x["score"], reverse=True)
    return ranked[:top_n]


def make_cycle_state(
    ex: Exchange,
    symbol: str,
    mark_price: float,
    wallet_balance: float,
    width_pct: float,
    grid_count: int,
    leverage: int,
    capital_usage: float,
) -> CycleState:
    levels = build_grid_levels(mark_price, width_pct, grid_count)
    budget_usdt = wallet_balance * capital_usage
    qty = quantity_for_rung(ex, symbol, budget_usdt, leverage, grid_count, mark_price)
    if qty <= 0:
        raise RuntimeError("격자 1칸 수량이 최소주문금액을 못 넘습니다")
    return CycleState(
        symbol=symbol,
        center_price=mark_price,
        started_at=time.time(),
        wallet_balance_start=wallet_balance,
        levels=levels,
        qty_per_rung=qty,
    )


def regular_order_status(ex: Exchange, symbol: str, order_id: int) -> dict | None:
    try:
        return ex.get_order_status(symbol, order_id)
    except Exception:
        return None


def place_limit_order(
    ex: Exchange,
    symbol: str,
    side: str,
    quantity: float,
    price: float,
    reduce_only: bool,
) -> int:
    order_side = "BUY" if side == "LONG" else "SELL"
    rounded_price = ex.round_price(symbol, price)
    resp = ex.client.futures_create_order(
        symbol=symbol,
        side=order_side,
        type="LIMIT",
        quantity=quantity,
        price=rounded_price,
        timeInForce="GTC",
        reduceOnly=reduce_only,
    )
    return int(resp["orderId"])


# [2026-08-21] 주문 실패 백오프.
# 실사고: -2019(증거금 부족)로 같은 주문을 1시간 동안 8초마다 재시도해
# PLACE_ORDER_FAILED 가 461건 쌓였다. 그동안 격자에 구멍이 뚫린 채 돌았고
# 매도가 걸릴 자리가 없어 완결률이 7.7% 로 떨어졌다.
# 증거금 부족은 재시도로 풀리지 않는다. 포지션이 정리되거나 가격이 움직여야 한다.
_ORDER_BACKOFF: dict[tuple, float] = {}
ORDER_BACKOFF_SEC = 300.0          # 같은 칸 재시도 간격
_BACKOFF_CODES = ("-2019", "-4131", "-1013")   # 증거금/유동성/최소수량


def _backoff_key(symbol: str, side: str, price: float) -> tuple:
    return (symbol, side, round(float(price), 10))


def order_is_backed_off(symbol: str, side: str, price: float,
                        now: float | None = None) -> bool:
    """직전 실패로 아직 쉬어야 하는 주문인지."""
    until = _ORDER_BACKOFF.get(_backoff_key(symbol, side, price), 0.0)
    return (now if now is not None else time.time()) < until


def clear_order_backoff(symbol: str, side: str, price: float) -> None:
    _ORDER_BACKOFF.pop(_backoff_key(symbol, side, price), None)


def try_place_limit_order(
    ex: Exchange,
    symbol: str,
    side: str,
    quantity: float,
    price: float,
    reduce_only: bool,
) -> int | None:
    if order_is_backed_off(symbol, side, price):
        return None
    try:
        oid = place_limit_order(ex, symbol, side, quantity, price, reduce_only)
        clear_order_backoff(symbol, side, price)
        return oid
    except Exception as e:
        log_event(
            "PLACE_ORDER_FAILED",
            symbol=symbol,
            side=side,
            quantity=quantity,
            price=price,
            reduce_only=reduce_only,
            error=str(e),
        )
        # 증거금 부족 같은 실패는 재시도로 풀리지 않는다.
        # 포지션이 정리되거나 가격이 움직여야 한다. 그때까지 이 칸은 쉰다.
        if any(c in str(e) for c in _BACKOFF_CODES):
            _ORDER_BACKOFF[_backoff_key(symbol, side, price)] = (
                time.time() + ORDER_BACKOFF_SEC)
        return None


def cancel_all_regular_orders(ex: Exchange, symbol: str, orders: dict[str, dict]) -> None:
    for payload in list(orders.values()):
        try:
            ex.cancel_regular_order(symbol, int(payload["order_id"]))
        except Exception:
            pass


def log_event(kind: str, **kwargs) -> None:
    append_ledger({"ts": time.time(), "kind": kind, **kwargs})


def render_grid_view(
    state: CycleState,
    mark_price: float,
    wallet_balance: float,
    paused: bool = False,
    max_rows: int = 21,
) -> str:
    """바이낸스 그리드 화면처럼 격자를 사다리로 보여준다.

    위가 고가, 아래가 저가다(호가창과 같은 방향).
    각 칸의 상태를 한눈에 보려는 것이 목적이다.
        ●  보유    - 그 칸에서 매수 체결됨. 위 칸에 매도가 걸려 있다
        ○  매도대기
        ◇  매수대기
        ·  비어 있음
        ◀  현재가 위치

    격자가 많으면(예: 30개) 전부 찍으면 읽기 어렵다.
    현재가 주변만 잘라서 보여주고 생략된 칸 수를 표시한다.
    """
    levels = state.levels
    n = len(levels)
    if n == 0:
        return "[e3 격자] 표시할 격자가 없습니다"

    held = set(state.held_buy_rungs)
    buy_rungs = {int(v["rung"]) for v in state.buy_orders.values()}
    sell_rungs = {int(v["rung"]) for v in state.sell_orders.values()}

    # 현재가가 어느 칸 사이에 있는지 (levels[i] <= mark < levels[i+1])
    cursor = 0
    for i, lv in enumerate(levels):
        if mark_price >= lv:
            cursor = i

    # 현재가 주변으로 창을 자른다
    half = max_rows // 2
    lo_i = max(0, cursor - half)
    hi_i = min(n - 1, lo_i + max_rows - 1)
    lo_i = max(0, hi_i - max_rows + 1)

    width = max(len(f"{lv:.6f}") for lv in levels)
    rows = []
    for i in range(hi_i, lo_i - 1, -1):
        lv = levels[i]
        if i in sell_rungs:
            mark_ch, label = "○", "매도대기"
        elif i in held:
            mark_ch, label = "●", "보유"
        elif i in buy_rungs:
            mark_ch, label = "◇", "매수대기"
        else:
            mark_ch, label = "·", ""
        here = "  ◀ 현재가" if i == cursor else ""
        rows.append(f" {i:>2} {lv:>{width}.6f} {mark_ch} {label}{here}")

    out = [f"[e3 격자] {state.symbol}  {'일시정지' if paused else '가동중'}"]
    if hi_i < n - 1:
        out.append(f"     위로 {n - 1 - hi_i}칸 생략")
    out += rows
    if lo_i > 0:
        out.append(f"     아래로 {lo_i}칸 생략")

    in_range = levels[0] <= mark_price <= levels[-1]
    filled = len(held)
    out.append("")
    out.append(f"현재가 {mark_price:.6f}"
               + ("" if in_range else "  ⚠ 범위 이탈"))
    out.append(f"범위 {levels[0]:.6f} ~ {levels[-1]:.6f}"
               f" (중심 {state.center_price:.6f})")
    out.append(f"보유 {filled}/{n}칸  대기 매수{len(buy_rungs)} 매도{len(sell_rungs)}")
    out.append(f"격자 실현(추정) {state.realized_grid_profit_est:+.6f} USDT")
    pnl = wallet_balance - state.wallet_balance_start
    out.append(f"자산 {wallet_balance:.4f} (시작 {state.wallet_balance_start:.4f},"
               f" {pnl:+.4f})")
    out.append(f"재센터링 {state.reset_count}회")
    out.append("")
    out.append("※ 격자 실현은 맞물린 짝만 센 값입니다. 보유 중인 칸의"
               " 평가손익은 '자산' 쪽에 반영됩니다.")
    return "\n".join(out)


def summarize_status(state: CycleState, mark_price: float, wallet_balance: float, paused: bool) -> str:
    buy_count = len(state.buy_orders)
    sell_count = len(state.sell_orders)
    held = len(state.held_buy_rungs)
    mode = "일시정지" if paused else "가동중"
    return (
        f"[e3 상태]\n"
        f"심볼 {state.symbol}\n"
        f"상태 {mode}\n"
        f"중심가 {state.center_price:.6f} / 현재가 {mark_price:.6f}\n"
        f"범위 {state.levels[0]:.6f} ~ {state.levels[-1]:.6f}\n"
        f"대기매수 {buy_count} / 대기매도 {sell_count} / 보유 rung {held}\n"
        f"rung당 수량 {state.qty_per_rung}\n"
        f"추정 격자 실현 {state.realized_grid_profit_est:+.6f}\n"
        f"총자산 {wallet_balance:.4f} USDT / 재센터링 {state.reset_count}회"
    )


def summarize_brief(state: CycleState, wallet_balance: float) -> str:
    elapsed_min = max(0.0, (time.time() - state.started_at) / 60.0)
    pnl = wallet_balance - state.wallet_balance_start
    return (
        f"[e3 브리핑]\n"
        f"{state.symbol} / 경과 {elapsed_min:.1f}분\n"
        f"시작자산 {state.wallet_balance_start:.4f} -> 현재 {wallet_balance:.4f} USDT\n"
        f"총자산 변화 {pnl:+.4f} USDT\n"
        f"격자 실현 추정 {state.realized_grid_profit_est:+.6f}\n"
        f"열린 매수 {len(state.buy_orders)} / 열린 매도 {len(state.sell_orders)} / 보유 rung {len(state.held_buy_rungs)}"
    )


def ensure_grid_orders(
    ex: Exchange,
    state: CycleState,
    grid: GridState,
    mark_price: float,
    live: bool,
) -> None:
    for rung in grid.eligible_buy_rungs(mark_price):
        key = str(rung)
        if key in state.buy_orders:
            continue
        price = state.levels[rung]
        if live:
            order_id = try_place_limit_order(
                ex, state.symbol, "LONG", state.qty_per_rung, price, reduce_only=False
            )
            if order_id is None:
                continue
        else:
            order_id = -(rung + 1)
        state.buy_orders[key] = {
            "order_id": order_id,
            "rung": rung,
            "price": price,
            "quantity": state.qty_per_rung,
        }
        log_event("PLACE_BUY", symbol=state.symbol, rung=rung, price=price, quantity=state.qty_per_rung, live=live)

    for sell_rung in grid.eligible_sell_rungs(mark_price):
        key = str(sell_rung)
        if key in state.sell_orders:
            continue
        price = state.levels[sell_rung]
        if live:
            order_id = try_place_limit_order(
                ex, state.symbol, "SHORT", state.qty_per_rung, price, reduce_only=True
            )
            if order_id is None:
                continue
        else:
            order_id = -(1000 + sell_rung)
        state.sell_orders[key] = {
            "order_id": order_id,
            "rung": sell_rung,
            "price": price,
            "quantity": state.qty_per_rung,
        }
        log_event("PLACE_SELL", symbol=state.symbol, rung=sell_rung, price=price, quantity=state.qty_per_rung, live=live)


def process_dry_fills(state: CycleState, grid: GridState, mark_price: float) -> None:
    for key, payload in list(state.buy_orders.items()):
        if mark_price <= payload["price"]:
            sell_rung = grid.register_buy_fill(int(payload["rung"]))
            state.held_buy_rungs = sorted(grid.held_buy_rungs)
            state.buy_orders.pop(key, None)
            state.last_fill_at = time.time()
            log_event("BUY_FILLED", symbol=state.symbol, rung=payload["rung"], price=payload["price"], next_sell_rung=sell_rung, live=False)
    for key, payload in list(state.sell_orders.items()):
        if mark_price >= payload["price"]:
            buy_rung = grid.register_sell_fill(int(payload["rung"]))
            state.held_buy_rungs = sorted(grid.held_buy_rungs)
            state.sell_orders.pop(key, None)
            state.realized_grid_profit_est += (state.levels[buy_rung + 1] - state.levels[buy_rung]) * state.qty_per_rung
            state.last_fill_at = time.time()
            log_event("SELL_FILLED", symbol=state.symbol, rung=payload["rung"], price=payload["price"], paired_buy_rung=buy_rung, live=False)


def process_live_fills(ex: Exchange, state: CycleState, grid: GridState) -> None:
    for key, payload in list(state.buy_orders.items()):
        status = regular_order_status(ex, state.symbol, int(payload["order_id"]))
        if not status or status.get("status") != "FILLED":
            continue
        sell_rung = grid.register_buy_fill(int(payload["rung"]))
        state.held_buy_rungs = sorted(grid.held_buy_rungs)
        state.buy_orders.pop(key, None)
        state.last_fill_at = time.time()
        log_event("BUY_FILLED", symbol=state.symbol, rung=payload["rung"], price=payload["price"], next_sell_rung=sell_rung, live=True)
    for key, payload in list(state.sell_orders.items()):
        status = regular_order_status(ex, state.symbol, int(payload["order_id"]))
        if not status or status.get("status") != "FILLED":
            continue
        buy_rung = grid.register_sell_fill(int(payload["rung"]))
        state.held_buy_rungs = sorted(grid.held_buy_rungs)
        state.sell_orders.pop(key, None)
        state.realized_grid_profit_est += (state.levels[buy_rung + 1] - state.levels[buy_rung]) * state.qty_per_rung
        state.last_fill_at = time.time()
        log_event("SELL_FILLED", symbol=state.symbol, rung=payload["rung"], price=payload["price"], paired_buy_rung=buy_rung, live=True)


def flatten_position(ex: Exchange, symbol: str) -> None:
    live = ex.get_position(symbol)
    if not live:
        return
    ex.close_market_position(symbol, live["side"], abs(float(live["amount"])))


def handle_global_stop(
    ex: Exchange,
    state: CycleState,
    symbol: str,
    wallet_balance: float,
    max_loss_pct: float,
    live: bool,
) -> bool:
    if wallet_balance > state.wallet_balance_start * (1.0 - max_loss_pct / 100.0):
        return False
    cancel_all_regular_orders(ex, symbol, state.buy_orders)
    cancel_all_regular_orders(ex, symbol, state.sell_orders)
    if live:
        flatten_position(ex, symbol)
    log_event(
        "GLOBAL_STOP",
        symbol=symbol,
        wallet_balance=wallet_balance,
        wallet_balance_start=state.wallet_balance_start,
        max_loss_pct=max_loss_pct,
        live=live,
    )
    save_state(state)
    return True


def recenter_cycle(
    ex: Exchange,
    state: CycleState,
    mark_price: float,
    width_pct: float,
    leverage: int,
    capital_usage: float,
    live: bool,
) -> CycleState:
    cancel_all_regular_orders(ex, state.symbol, state.buy_orders)
    cancel_all_regular_orders(ex, state.symbol, state.sell_orders)
    if live:
        flatten_position(ex, state.symbol)
    wallet = ex.get_total_margin_balance()
    new_state = make_cycle_state(
        ex,
        state.symbol,
        mark_price,
        wallet,
        width_pct,
        len(state.levels),
        leverage,
        capital_usage,
    )
    new_state.reset_count = state.reset_count + 1
    log_event("RECENTER", symbol=state.symbol, old_center=state.center_price, new_center=mark_price, reset_count=new_state.reset_count, live=live)
    return new_state


def current_width_pct(state: CycleState) -> float:
    if state.center_price <= 0 or len(state.levels) < 2:
        return 0.0
    return max(0.0, ((state.levels[-1] / state.center_price) - 1.0) * 100.0)


def expand_range_cycle(
    ex: Exchange,
    state: CycleState,
    mark_price: float,
    width_mult: float = 1.5,
) -> CycleState:
    cancel_all_regular_orders(ex, state.symbol, state.buy_orders)
    cancel_all_regular_orders(ex, state.symbol, state.sell_orders)

    width_pct = max(current_width_pct(state), 0.01)
    low = state.center_price * (1.0 - width_pct / 100.0)
    high = state.center_price * (1.0 + width_pct / 100.0)
    step_count = 0
    while not (low <= mark_price <= high) and step_count < 12:
        width_pct *= max(width_mult, 1.01)
        low = state.center_price * (1.0 - width_pct / 100.0)
        high = state.center_price * (1.0 + width_pct / 100.0)
        step_count += 1
    if not (low <= mark_price <= high):
        required = abs((mark_price / state.center_price) - 1.0) * 100.0
        width_pct = max(width_pct, required * 1.02)

    new_state = CycleState(
        symbol=state.symbol,
        center_price=state.center_price,
        started_at=state.started_at,
        wallet_balance_start=state.wallet_balance_start,
        levels=build_grid_levels(state.center_price, width_pct, len(state.levels)),
        qty_per_rung=state.qty_per_rung,
        held_buy_rungs=[
            rung for rung in sorted(state.held_buy_rungs)
            if 0 <= int(rung) < len(state.levels) - 1
        ],
        buy_orders={},
        sell_orders={},
        realized_grid_profit_est=state.realized_grid_profit_est,
        reset_count=state.reset_count + 1,
    )
    log_event(
        "RANGE_EXPAND",
        symbol=state.symbol,
        old_center=state.center_price,
        mark_price=mark_price,
        old_width_pct=current_width_pct(state),
        new_width_pct=width_pct,
        reset_count=new_state.reset_count,
        held_rungs=len(new_state.held_buy_rungs),
    )
    return new_state


def configure_symbol(ex: Exchange, symbol: str, leverage: int) -> None:
    ex.set_margin_type(symbol, "ISOLATED")
    ex.set_leverage(symbol, leverage)


def main() -> int:
    ap = argparse.ArgumentParser(description="E3 mini-grid bot")
    ap.add_argument("--symbol", required=True)
    ap.add_argument("--width-pct", type=float, default=10.0)
    ap.add_argument("--breakout-mode", choices=["expand", "recenter"], default="expand")
    ap.add_argument("--range-expand-mult", type=float, default=1.5)
    ap.add_argument("--grid-count", type=int, default=16)
    ap.add_argument("--leverage", type=int, default=3)
    ap.add_argument("--capital-usage", type=float, default=0.65)
    ap.add_argument("--max-loss-pct", type=float, default=7.0)
    ap.add_argument("--poll-sec", type=float, default=5.0)
    ap.add_argument("--minutes", type=float, default=0.0, help="0=무기한")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--resume", action="store_true")
    ap.add_argument("--no-telegram", action="store_true")
    ap.add_argument("--i-understand-grid-risks", action="store_true")
    args = ap.parse_args()

    if not args.dry_run and not args.i_understand_grid_risks:
        print("실주문은 --i-understand-grid-risks 가 필요합니다.", flush=True)
        return 2
    if args.capital_usage <= 0 or args.capital_usage > 0.9:
        print("capital-usage 는 0 초과 0.9 이하만 허용합니다.", flush=True)
        return 2
    if not acquire_bot_lock():
        return 2
    atexit.register(release_bot_lock)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[
            logging.FileHandler(LOG_DIR / f"{VERSION}_run.log", encoding="utf-8"),
            logging.StreamHandler(sys.stdout),
        ],
    )
    log = logging.getLogger(VERSION)

    cfg = Config()
    ex = Exchange(cfg)
    tg = None if args.no_telegram else Tg(cfg)
    symbol = args.symbol.upper()
    configure_symbol(ex, symbol, args.leverage)

    if ex.get_position(symbol) and not args.resume:
        print("현재 포지션이 남아 있습니다. 수동 확인 후 --resume 으로만 재개하십시오.", flush=True)
        return 2

    state = load_state() if args.resume else None
    if state is None:
        wallet = ex.get_total_margin_balance()
        if wallet <= 0:
            print("총 자산이 0 이하입니다. 계좌 상태를 먼저 확인하십시오.", flush=True)
            return 2
        mark = ex.get_mark_price(symbol)
        state = make_cycle_state(
            ex,
            symbol,
            mark,
            wallet,
            args.width_pct,
            args.grid_count,
            args.leverage,
            args.capital_usage,
        )
        log_event("START", symbol=symbol, center=mark, width_pct=args.width_pct, grid_count=args.grid_count, leverage=args.leverage, qty_per_rung=state.qty_per_rung, capital_usage=args.capital_usage, live=not args.dry_run)
        if tg and tg.enabled:
            tg.poll()
            tg.menu()
            tg.send(f"[e3 시작] {symbol} / 폭 ±{args.width_pct}% / 격자 {args.grid_count} / 레버리지 {args.leverage}x")
    else:
        state = reconcile_cycle_state_with_exchange(
            ex, state, cancel_duplicates=True, cancel_stale=True
        )
        save_state(state)
        if tg and tg.enabled:
            tg.poll()
            tg.menu()
            tg.send(f"[e3 재개] {symbol} 상태를 거래소 주문 기준으로 재구성했습니다.")

    deadline = time.time() + args.minutes * 60.0 if args.minutes > 0 else None
    paused = False
    while True:
        mark = ex.get_mark_price(symbol)
        wallet = ex.get_total_margin_balance()
        grid = GridState(state.levels, set(state.held_buy_rungs))

        if tg and tg.enabled:
            for _cq, action in tg.poll():
                if action == "status":
                    tg.send(summarize_status(state, mark, wallet, paused))
                elif action == "gridview":
                    tg.send(render_grid_view(state, mark, wallet, paused))
                elif action == "brief":
                    tg.send(summarize_brief(state, wallet))
                elif action == "pause":
                    paused = True
                    tg.send("[e3] 신규 주문 배치를 일시정지했습니다.")
                elif action == "resume":
                    paused = False
                    tg.send("[e3] 신규 주문 배치를 재개했습니다.")
                elif action == "flat":
                    cancel_all_regular_orders(ex, symbol, state.buy_orders)
                    cancel_all_regular_orders(ex, symbol, state.sell_orders)
                    state.buy_orders = {}
                    state.sell_orders = {}
                    if not args.dry_run:
                        flatten_position(ex, symbol)
                    state.held_buy_rungs = []
                    save_state(state)
                    tg.send("[e3] 전량정리를 실행했습니다. 보유/대기주문을 확인하세요.")

        if handle_global_stop(
            ex,
            state,
            symbol,
            wallet,
            args.max_loss_pct,
            live=not args.dry_run,
        ):
            return 0

        if not grid.in_range(mark):
            if args.breakout_mode == "expand":
                state = expand_range_cycle(
                    ex,
                    state,
                    mark,
                    width_mult=args.range_expand_mult,
                )
            else:
                state = recenter_cycle(
                    ex,
                    state,
                    mark,
                    args.width_pct,
                    args.leverage,
                    args.capital_usage,
                    live=not args.dry_run,
                )
            grid = GridState(state.levels, set(state.held_buy_rungs))

        if not paused:
            if args.dry_run:
                process_dry_fills(state, grid, mark)
            else:
                process_live_fills(ex, state, grid)
            ensure_grid_orders(ex, state, grid, mark, live=not args.dry_run)
        save_state(state)

        if deadline and time.time() >= deadline:
            log_event("STOP", symbol=symbol, reason="minutes_elapsed", live=not args.dry_run)
            return 0
        time.sleep(args.poll_sec)


if __name__ == "__main__":
    raise SystemExit(main())

