import logging
import math
import re
import time
from decimal import Decimal, ROUND_DOWN
import pandas as pd
from binance import Client
from binance.exceptions import BinanceAPIException

from .config import Config

log = logging.getLogger("bot.exchange")

# [2026-08-26] 정산일이 이 일수 안으로 잡힌 무기한 심볼은 신규 스캔 대상에서 뺀다.
# 폐지 예정이 공지되면 그 순간부터 거래할 이유가 없으므로 넉넉하게 잡는다.
DELIST_EXCLUDE_DAYS = 30.0

_BAN_UNTIL_RE = re.compile(r"banned until (\d+)")


def parse_ban_wait_seconds(exc: Exception) -> float | None:
    """[2026-08-11 사용자요청 리뷰로 발견] -1003(Way too many requests)/418/429로 밴당했을 때,
    막연히 고정 시간을 기다리는 대신 실제 해제 시각까지만 정확히 기다리기 위한 헬퍼.

    python-binance(CCXT 아님)는 HTTP 헤더의 Retry-After가 아니라, 예외 메시지 본문에
    "IP banned until <ms epoch>" 형태로 해제 시각을 담아준다 — 오늘 실제 밴 사고 때 받은
    메시지 형식 그대로("APIError(code=-1003): Way too many requests; IP banned until
    <ms>. ..."). 코드(-1003/418/429)나 문구가 안 맞으면 None을 반환해 호출부가 밴이
    아닌 다른 오류로 처리하게 한다."""
    code = getattr(exc, "code", None)
    message = str(exc)
    is_rate_limit = code == -1003 or "418" in message or "429" in message or "too many requests" in message.lower()
    if not is_rate_limit:
        return None
    match = _BAN_UNTIL_RE.search(message)
    if not match:
        return None
    ban_until_ms = int(match.group(1))
    wait_sec = ban_until_ms / 1000 - time.time()
    return max(0.0, wait_sec)


class Exchange:
    """Binance USDT-M Futures 접근 래퍼."""

    def __init__(self, cfg: Config):
        self.cfg = cfg
        # [2026-08-26 P0] REST 호출에 타임아웃을 건다. 지금까지 아무 데도 없었다.
        # python-binance 는 requests 세션을 쓰는데 timeout 이 없으면 커넥션이 끊긴 채
        # 응답이 안 오면 **영원히 블록**된다. 실사고: 봇이 11:19 에 멈춰 12분간 아무것도
        # 하지 않았고(CPU 25초에 0.078초), 그 사이 체결된 ONGUSDT 포지션에 손절이
        # 걸리지 않았다. 로그도 상태파일도 그 시각에서 정지했다.
        # 10초면 정상 응답(수십~수백 ms)에는 여유가 크고, 멈춘 커넥션은 확실히 끊는다.
        self.client = Client(cfg.api_key, cfg.api_secret, testnet=cfg.use_testnet,
                             requests_params={"timeout": 10})
        self._symbol_info_cache = {}
        # [2026-08-10] WebSocket 캔들/체결 레이어 연결점. 둘 다 기본 None이라 main()에서
        # set_ws_kline_cache()/set_fill_tracker()를 명시적으로 호출하지 않는 한(옵트인 플래그가
        # 꺼져있으면 호출 자체가 없음) 기존 REST 폴링 동작과 100% 동일하게 유지된다.
        self._ws_kline_cache = None
        self._fill_tracker = None
        self.sync_time()

    def set_ws_kline_cache(self, cache) -> None:
        """RollingKlineCache를 연결한다. 연결되면 get_klines()가 REST 대신 이 캐시를
        우선 사용하려고 시도한다(데이터가 충분치 않으면 자동으로 REST 폴백)."""
        self._ws_kline_cache = cache

    def set_fill_tracker(self, tracker) -> None:
        self._fill_tracker = tracker

    def get_last_fill(self, symbol: str):
        """FillTracker가 연결돼 있고 해당 심볼의 최근 실제 체결 정보가 있으면 반환, 없으면 None."""
        if self._fill_tracker is None:
            return None
        return self._fill_tracker.get(symbol)

    def sync_time(self):
        """윈도우 시계가 바이낸스 서버 시간과 조금 어긋나 있어도(-1021 오류) 문제 없도록,
        서버 시간과의 차이를 계산해서 매 요청에 자동으로 보정해준다. OS 시계를 직접
        맞출 필요가 없어진다."""
        try:
            server_time = self.client.futures_time()["serverTime"]
            local_time = int(time.time() * 1000)
            self.client.timestamp_offset = server_time - local_time
            log.info("서버 시간 동기화 완료 (offset=%dms)", self.client.timestamp_offset)
        except Exception:
            log.exception("서버 시간 동기화 실패 — 다음 기회에 재시도")

    def setup_symbol(self, symbol: str):
        self.set_leverage(symbol, self.cfg.leverage_min)

    def set_leverage(self, symbol: str, leverage: int) -> bool:
        """[2026-08-15 사용자신고] 실패해도 경고만 남기고 True/False를 안 돌려줘서, 호출측이
        "설정이 안 먹은 채로 주문이 나가는" 상황을 알 수 없었다(실측: -4028 "Leverage 5 is
        not valid" 7건). 성공 여부를 반환해 호출측이 판단할 수 있게 한다."""
        try:
            self.client.futures_change_leverage(symbol=symbol, leverage=leverage)
            return True
        except BinanceAPIException as e:
            log.warning("레버리지 설정 실패 %s: %s", symbol, e)
            return False

    def set_margin_type(self, symbol: str, margin_type: str) -> bool:
        """마진 타입을 ISOLATED(격리) 또는 CROSSED(교차)로 설정한다.
        이미 그 타입이면 바이낸스가 에러(-4046)를 주는데, 정상 상황이라 성공으로 취급한다.

        [2026-08-15 사용자신고] -4067("Position side cannot be changed if there exists open
        orders")이 64건 쌓여 있었다 — 봇은 보유 심볼에 STOP_MARKET/트레일링 주문을 항상
        걸어두므로 이 실패가 상시 발생할 조건이다. 실패를 삼키면 심볼이 CROSSED로 남은 채
        진입이 진행돼, 2026-08-09에 격리로 전환한 이유(한 포지션 손실이 계좌 전체를 갉아먹는
        것 방지)가 그대로 뚫린다. 성공 여부를 반환한다."""
        try:
            self.client.futures_change_margin_type(symbol=symbol, marginType=margin_type)
            return True
        except BinanceAPIException as e:
            if getattr(e, "code", None) == -4046:
                return True  # 이미 원하는 타입 — 정상
            log.warning("마진 타입 설정 실패 %s: %s", symbol, e)
            return False

    def get_symbol_risk_settings(self, symbol: str) -> dict | None:
        """[2026-08-15 추가] 그 심볼의 거래소 실제 레버리지/마진모드를 조회한다.
        futures_position_information 응답에는 leverage/isolated가 없는 계정/모드가 있어
        (실측 확인) futures_account()의 positions에서 읽는다. 무거운 호출이므로
        set_leverage/set_margin_type이 실패했을 때만 확인용으로 부른다.

        반환: {"leverage": float, "isolated": bool} 또는 조회 실패 시 None."""
        try:
            account = self.client.futures_account()
            for p in account.get("positions", []):
                if p.get("symbol") != symbol:
                    continue
                try:
                    lev = float(p.get("leverage"))
                except (TypeError, ValueError):
                    lev = None
                return {"leverage": lev, "isolated": bool(p.get("isolated"))}
        except Exception:
            log.exception("[%s] 심볼 리스크 설정 조회 실패", symbol)
        return None

    def get_klines(self, symbol: str, limit: int = 99, interval: str | None = None) -> pd.DataFrame:
        # [2026-08-10] WS 캔들 캐시가 연결돼 있고(옵트인), 요청한 것과 같은 interval이며,
        # 캐시에 충분한 히스토리(limit개 이상)가 쌓여 있으면 REST 호출 없이 그걸 반환한다 —
        # API 호출량/반응지연을 줄이는 목적. 넷 중 하나라도 아니면 기존 REST 경로 그대로 폴백
        # (데이터 부족/미연결/다른 interval 요청/오래된 데이터 등 어떤 경우에도 조용히 안전하게
        # REST로 넘어감).
        #
        # [2026-08-10 실거래 사고 발견/수정] 원래는 has_sufficient_history()만 확인했는데,
        # 이건 "캔들 개수가 충분한가"만 볼 뿐 "최신인가"는 안 본다 — 실거래에서 WS 연결이
        # (내부 재연결 실패로) 죽었는데도 캐시엔 죽기 직전의 캔들이 그대로 남아있어서, 이
        # 조건만으로는 멈춰버린 옛날 가격을 계속 매매판단에 쓸 뻔한 위험이 실제로 있었다.
        # is_fresh()를 반드시 함께 확인해 "최근 갱신됨"까지 확인된 경우에만 WS 데이터를 믿는다.
        use_ws = (
            self._ws_kline_cache is not None
            and (interval or self.cfg.interval) == self.cfg.interval
            and self._ws_kline_cache.has_sufficient_history(symbol, limit)
            and self._ws_kline_cache.is_fresh(symbol, self.cfg.ws_kline_max_staleness_sec)
        )
        if use_ws:
            df = self._ws_kline_cache.to_dataframe(symbol)
            if df is not None and len(df) >= limit:
                return df.iloc[-limit:].reset_index(drop=True)
            log.debug("[%s] WS 캔들 캐시 데이터 부족 — REST로 폴백", symbol)
        elif self._ws_kline_cache is not None:
            log.debug("[%s] WS 캔들 캐시가 최신이 아니거나 조건 불충족 — REST로 폴백", symbol)

        # limit<100이면 바이낸스 API 가중치가 1, 100 이상이면 2로 두 배가 된다.
        # 지표 계산에 필요한 최대 warmup(~61개)를 충분히 커버하면서 가중치 1 구간을 확실히 지킨다.
        # interval을 넘기면 기본(1분봉) 대신 다른 시간대(상위 타임프레임 확인용)를 조회한다.
        raw = self.client.futures_klines(symbol=symbol, interval=interval or self.cfg.interval, limit=limit)
        df = pd.DataFrame(raw, columns=[
            "open_time", "open", "high", "low", "close", "volume",
            "close_time", "quote_asset_volume", "num_trades",
            "taker_buy_base", "taker_buy_quote", "ignore",
        ])
        for col in ("open", "high", "low", "close", "volume", "taker_buy_base"):
            df[col] = df[col].astype(float)
        df["open_time"] = pd.to_datetime(df["open_time"], unit="ms")
        # 바이낸스는 마지막 캔들로 "아직 마감 안 된(진행 중인)" 캔들을 함께 내려준다.
        # 지표/신호는 확정된 캔들만 봐야 안정적이므로 마지막 미완성 캔들은 제외한다.
        df = df.iloc[:-1].reset_index(drop=True)
        return df

    def get_mark_price(self, symbol: str) -> float:
        return float(self.client.futures_mark_price(symbol=symbol)["markPrice"])

    def get_last_price_fast(self, symbol: str) -> float:
        """[2026-08-11 사용자요청] 진입 직전 "신호 이후 가격 재검증"처럼, 정확한 마크가격이
        아니라 "지금 대략 어디 있는지"만 빨리 알면 되는 곳에서 쓴다. WS 캔들 캐시가 있고
        신선하면 REST 왕복 없이 그 마지막 종가를 즉시 반환한다(그 왕복 시간 동안 가격이
        더 움직여서 재검증에 계속 걸리는 문제를 줄임). 캐시가 없거나 오래됐으면 기존처럼
        get_mark_price()로 안전하게 폴백한다. 실제 손익/트리거 계산에는 쓰지 않는다 —
        그런 곳은 반드시 get_mark_price()(정식 마크가격)를 그대로 써야 한다."""
        if (
            self._ws_kline_cache is not None
            and self._ws_kline_cache.is_fresh(symbol, self.cfg.ws_kline_max_staleness_sec)
        ):
            try:
                df = self._ws_kline_cache.to_dataframe(symbol)
                if df is not None and len(df):
                    return float(df["close"].iloc[-1])
            except Exception:
                pass  # 캐시 읽기 실패해도 조용히 REST로 폴백
        return self.get_mark_price(symbol)

    def get_book_ticker(self, symbol: str) -> dict:
        ticker = self.client.futures_orderbook_ticker(symbol=symbol)
        return {
            "bid": float(ticker["bidPrice"]),
            "ask": float(ticker["askPrice"]),
        }

    def get_24h_ticker(self, symbol: str) -> dict:
        ticker = self.client.futures_ticker(symbol=symbol)
        return {
            "price_change_pct": float(ticker.get("priceChangePercent", 0.0) or 0.0),
            "quote_volume": float(ticker.get("quoteVolume", 0.0) or 0.0),
        }

    def get_funding_rate_pct(self, symbol: str) -> float:
        """다음 정산 예정 펀딩비율을 %로 반환한다. 절대값이 크면 시장이 한쪽으로
        쏠려있다는(고래의 대량 포지션 등) 신호로 볼 수 있다."""
        return float(self.client.futures_mark_price(symbol=symbol).get("lastFundingRate", 0)) * 100

    def get_total_margin_balance(self) -> float:
        """포지션 평가손익까지 포함한 총 자산(USDT)을 반환한다 (자금 규모 판단용)."""
        account = self.client.futures_account()
        return float(account["totalMarginBalance"])

    def get_margin_ratio(self) -> float:
        """현재 계좌의 마진 비율(0~1)을 반환한다. 유지증거금 / 마진잔고 기준."""
        account = self.client.futures_account()
        margin_balance = float(account["totalMarginBalance"])
        maint_margin = float(account["totalMaintMargin"])
        if margin_balance <= 0:
            return 0.0
        return maint_margin / margin_balance

    def get_income_history_stats(self, hours: float = 1.0) -> dict:
        """최근 N시간 동안의 실현손익 내역을 집계한다 (자동 튜닝 진단용).

        바이낸스는 시장가 주문 하나가 여러 가격대에 걸쳐 부분체결되면 REALIZED_PNL
        내역을 체결 건별로 여러 줄 나눠서 준다 (포지션 1번 청산 = 로우 여러 개일 수 있음).
        그걸 그대로 승/패로 세면 실제로는 1번의 손실인데 8번의 손실처럼 부풀려져서
        승률이 실제보다 훨씬 낮게 보이는 문제가 있었다. 그래서 같은 심볼에서 3초 이내에
        연달아 찍힌 내역은 한 번의 청산(논리적 거래)으로 묶어서 합산한 뒤 승/패를 센다.
        """
        start_ms = int((time.time() - hours * 3600) * 1000)
        try:
            income = self.client.futures_income_history(
                incomeType="REALIZED_PNL", startTime=start_ms, limit=1000,
            )
        except Exception:
            log.exception("손익 내역 조회 실패")
            return {"trades": 0, "wins": 0, "losses": 0, "realized_pnl": 0.0, "avg_win": 0.0, "avg_loss": 0.0}

        income.sort(key=lambda i: int(i["time"]))
        trade_amounts = []
        last_seen = {}  # symbol -> (그룹 인덱스, 마지막 체결 시각ms)
        MERGE_WINDOW_MS = 3000
        for i in income:
            symbol = i["symbol"]
            t = int(i["time"])
            amt = float(i["income"])
            if symbol in last_seen and t - last_seen[symbol][1] <= MERGE_WINDOW_MS:
                idx = last_seen[symbol][0]
                trade_amounts[idx] += amt
                last_seen[symbol] = (idx, t)
            else:
                trade_amounts.append(amt)
                last_seen[symbol] = (len(trade_amounts) - 1, t)

        win_amounts = [a for a in trade_amounts if a > 0]
        loss_amounts = [a for a in trade_amounts if a < 0]
        realized_pnl = sum(trade_amounts)
        return {
            "trades": len(win_amounts) + len(loss_amounts),
            "wins": len(win_amounts),
            "losses": len(loss_amounts),
            "realized_pnl": realized_pnl,
            "avg_win": (sum(win_amounts) / len(win_amounts)) if win_amounts else 0.0,
            "avg_loss": (sum(loss_amounts) / len(loss_amounts)) if loss_amounts else 0.0,
        }

    def get_net_transfers(self, since_ms: int) -> float:
        """지정 시각 이후 선물지갑 입출금(스팟↔선물 이체) 순액(USDT)을 반환한다.
        입금은 양수, 출금은 음수로 반환된다. 오늘 수익률처럼 총자산 변화로 손익을
        계산하는 곳에서, 사용자가 직접 송금/인출한 금액을 손익으로 착각하지 않도록
        빼주는 용도로 쓴다."""
        try:
            transfers = self.client.futures_income_history(
                incomeType="TRANSFER", startTime=since_ms, limit=1000,
            )
        except Exception:
            log.exception("입출금(TRANSFER) 내역 조회 실패")
            return 0.0
        return sum(float(t["income"]) for t in transfers)

    def get_deposit_withdraw_totals(self) -> dict:
        """스팟 지갑 기준 누적 입금/출금 총액(USDT 환산 불가한 코인은 제외하고 USDT만 집계)을 반환한다.
        원금 대비 실제 성과(입출금 제외한 순수 매매 손익)를 가늠하는 용도."""
        deposited = 0.0
        withdrawn = 0.0
        try:
            deposits = self.client.get_deposit_history(coin="USDT")
            for d in deposits:
                if str(d.get("status")) in ("1", "6"):  # 1=완료(구버전), 6=완료(신버전)
                    deposited += float(d.get("amount", 0))
        except Exception:
            log.exception("입금 내역 조회 실패")
        try:
            withdraws = self.client.get_withdraw_history(coin="USDT")
            for w in withdraws:
                if str(w.get("status")) == "6":  # 완료
                    withdrawn += float(w.get("amount", 0))
        except Exception:
            log.exception("출금 내역 조회 실패")
        return {"deposited": deposited, "withdrawn": withdrawn}

    def get_available_balance_usdt(self) -> float:
        balances = self.client.futures_account_balance()
        for b in balances:
            if b["asset"] == "USDT":
                return float(b["availableBalance"])
        return 0.0

    def get_symbol_filters(self, symbol: str) -> dict:
        if symbol in self._symbol_info_cache:
            return self._symbol_info_cache[symbol]
        info = self.client.futures_exchange_info()
        for s in info["symbols"]:
            if s["symbol"] == symbol:
                filters = {f["filterType"]: f for f in s["filters"]}
                result = {
                    "quantity_precision": s["quantityPrecision"],
                    "price_precision": s["pricePrecision"],
                    "tick_size": float(filters.get("PRICE_FILTER", {}).get("tickSize", 0)),
                    "min_qty": float(filters.get("LOT_SIZE", {}).get("minQty", 0)),
                    "step_size": float(filters.get("LOT_SIZE", {}).get("stepSize", 0)),
                    "min_notional": float(filters.get("MIN_NOTIONAL", {}).get("notional", 0)),
                }
                self._symbol_info_cache[symbol] = result
                return result
        raise ValueError(f"심볼 정보를 찾을 수 없습니다: {symbol}")

    def round_quantity(self, symbol: str, qty: float, price: float | None = None, max_notional: float | None = None) -> float:
        """수량을 거래소 최소 단위로 내림한다.

        - 최소 주문 수량(LOT_SIZE)에 못 미치면 0을 반환한다.
        - price가 주어지면 최소 주문 금액(MIN_NOTIONAL)도 확인해서, 부족하면 그만큼 수량을 올린다.
          단, max_notional(감당 가능한 한도)을 넘어서야 한다면 무리하게 주문하지 않고 0을 반환한다.
        """
        f = self.get_symbol_filters(symbol)
        step = f["step_size"] or (10 ** -f["quantity_precision"])
        precision = f["quantity_precision"]
        rounded = round((qty // step) * step, precision)
        if rounded < f["min_qty"]:
            # 리스크 방어로 계산된 수량이 최소수량보다 작더라도 계좌 한도 안에서 거래소
            # 최소 주문을 만들 수 있으면 최소수량까지만 올린다. 예전에는 여기서 즉시 0을
            # 반환해 아래 MIN_NOTIONAL 보정까지 도달하지 못해 신호 자체가 사라졌다.
            needed_qty = f["min_qty"]
            if price and max_notional is not None and needed_qty * price > max_notional:
                return 0.0
            rounded = needed_qty

        min_notional = f.get("min_notional", 0)
        if price and min_notional and rounded * price < min_notional:
            needed_qty = math.ceil((min_notional / price) / step) * step
            needed_qty = round(needed_qty, precision)
            needed_notional = needed_qty * price
            if max_notional is not None and needed_notional > max_notional:
                return 0.0
            rounded = needed_qty

        return rounded

    @staticmethod
    def _infer_leverage(p: dict) -> float:
        """futures_position_information 응답에 'leverage' 필드가 없는 계정/모드가 있어
        (실제 확인됨: BLESSUSDT 사고 — 4배 포지션인데 기본값 1로 잘못 추적되어 손절선이
        의도보다 4배 느슨해짐) notional/initialMargin으로 실제 레버리지를 역산한다."""
        raw = p.get("leverage")
        if raw is not None:
            try:
                lev = float(raw)
                if lev > 0:
                    return lev
            except (TypeError, ValueError):
                pass
        try:
            notional = abs(float(p.get("notional", 0)))
            initial_margin = float(p.get("initialMargin", 0))
            if notional > 0 and initial_margin > 0:
                return round(notional / initial_margin)
        except (TypeError, ValueError):
            pass
        return 1.0

    def get_position(self, symbol: str) -> dict | None:
        positions = self.client.futures_position_information(symbol=symbol)
        for p in positions:
            amt = float(p["positionAmt"])
            if amt != 0:
                return {
                    "symbol": symbol,
                    "amount": amt,
                    "entry_price": float(p["entryPrice"]),
                    "side": "LONG" if amt > 0 else "SHORT",
                    "leverage": self._infer_leverage(p),
                }
        return None

    def get_open_positions(self) -> list[dict]:
        """모든 심볼의 보유 포지션을 한 번의 호출로 가져온다(심볼이 많을 때 효율적)."""
        positions = self.client.futures_position_information()
        result = []
        for p in positions:
            amt = float(p["positionAmt"])
            if amt != 0:
                result.append({
                    "symbol": p["symbol"],
                    "amount": amt,
                    "entry_price": float(p["entryPrice"]),
                    "mark_price": float(p.get("markPrice", 0.0) or 0.0),
                    "side": "LONG" if amt > 0 else "SHORT",
                    "leverage": self._infer_leverage(p),
                })
        return result

    def get_active_usdt_perpetual_symbols(self, limit: int | None = None) -> list[str]:
        """USDT 무기한(Perpetual) 선물 중 현재 거래 가능한 심볼을 반환한다.

        limit이 주어지면 24시간 거래대금(quoteVolume) 상위 순으로 그만큼만 반환한다.
        너무 많은 심볼을 매 주기마다 스캔하면 바이낸스 API 요청 한도(분당 2400)를
        초과하기 쉽고, 유동성 낮은 코인은 스캘핑에도 적합하지 않다.
        """
        # [2026-08-26] 상장폐지 예정 심볼을 제외한다.
        # 폐지 예정이어도 정산 직전까지 status 는 계속 "TRADING" 이라 종전 필터로는
        # 걸러지지 않았다. 구분은 deliveryDate 에 있다 — 정상 무기한은 전부
        # 4133404800000(2100-12-25) 센티넬이고(실측 524개 전부 동일),
        # 폐지 예정 심볼에만 실제 정산 시각이 들어간다(실측 3개).
        # 정산되면 바이낸스가 **마크가격으로 강제 청산**하므로 봇이 걸어둔 SL/TP 가
        # 아예 작동하지 않는다. 그 전에도 유동성이 마르고 스프레드가 벌어진다.
        # 실사고 직전: STORJUSDT(오늘 18:00 정산)가 85심볼 유니버스에 들어와
        # 오늘만 10건 거래됐다.
        # 거래수 영향은 85개 중 1개(-1.2%)로 사실상 없다(원칙 1 무관).
        _now_ms = time.time() * 1000
        _cut_ms = _now_ms + max(0.0, DELIST_EXCLUDE_DAYS) * 86400_000
        info = self.client.futures_exchange_info()
        symbols = [
            s["symbol"] for s in info["symbols"]
            if s.get("quoteAsset") == "USDT"
            and s.get("contractType") == "PERPETUAL"
            and s.get("status") == "TRADING"
            # deliveryDate 가 0/누락이면 정보 없음으로 보고 통과시킨다(기존 동작 유지).
            and not (0 < float(s.get("deliveryDate") or 0) < _cut_ms)
        ]
        if not limit:
            return symbols

        tickers = self.client.futures_ticker()
        volume_by_symbol = {t["symbol"]: float(t.get("quoteVolume", 0)) for t in tickers}
        symbols.sort(key=lambda s: volume_by_symbol.get(s, 0), reverse=True)
        return symbols[:limit]

    def open_market_position(self, symbol: str, side: str, quantity: float):
        order_side = "BUY" if side == "LONG" else "SELL"
        log.info("시장가 진입 주문: %s %s qty=%s", symbol, order_side, quantity)
        return self.client.futures_create_order(
            symbol=symbol, side=order_side, type="MARKET", quantity=quantity,
        )

    def open_limit_position(self, symbol: str, side: str, quantity: float, price: float):
        """[2026-08-10] 시장가 대신 지정가(수동적, 메이커 쪽)로 진입한다 — 슬리피지 리스크를
        최소화하기 위함. LONG은 매수호가(bid)에, SHORT는 매도호가(ask)에 걸어서 "스프레드를
        내가 내지 않고 상대가 와서 체결시켜주길" 기다리는 방식이다(메이커 주문). 체결 안 되면
        타임아웃 후 취소(시장가로 쫓아가지 않음 — 그게 이 기능의 핵심: 리스크 최소화가 목적이라
        불리하면 그냥 진입을 포기하는 쪽을 택한다)."""
        order_side = "BUY" if side == "LONG" else "SELL"
        price = self.round_price(symbol, price)
        log.info("지정가 진입 주문: %s %s qty=%s price=%s", symbol, order_side, quantity, price)
        return self.client.futures_create_order(
            symbol=symbol, side=order_side, type="LIMIT", quantity=quantity,
            price=price, timeInForce="GTC",
        )

    def close_limit_position(self, symbol: str, side: str, quantity: float, price: float):
        """reduceOnly 지정가 청산. LONG 포지션은 SELL, SHORT 포지션은 BUY 주문을 낸다.

        [2026-08-23 실거래 수정] 시장가 청산과 달리 여기엔 -2022(ReduceOnly 거부) 재시도가
        없어, TP/EARLY_EXIT/모멘텀 잠금처럼 지정가 청산을 쓰는 경로에서 포지션 수량이
        미세하게 어긋나면 그대로 실패했다. 같은 방식으로 실제 포지션을 재조회해 1회만
        재시도해 즉시 복구한다.
        """
        order_side = "SELL" if side == "LONG" else "BUY"
        price = self.round_price(symbol, price)
        log.info("지정가 청산 주문: %s %s qty=%s price=%s", symbol, order_side, quantity, price)
        try:
            return self.client.futures_create_order(
                symbol=symbol, side=order_side, type="LIMIT", quantity=quantity,
                price=price, timeInForce="GTC", reduceOnly=True,
            )
        except BinanceAPIException as e:
            if getattr(e, "code", None) != -2022:
                raise
            log.warning("%s 지정가 청산 -2022(ReduceOnly 거부) — 실제 보유수량 재조회 후 즉시 재시도", symbol)
            live = self.get_position(symbol)
            if live is None or live["amount"] == 0:
                log.info("%s 재조회 결과 이미 포지션 없음 — 지정가 청산 불필요(이미 종료됨)", symbol)
                return None
            live_qty = abs(live["amount"])
            live_side = "SELL" if live["side"] == "LONG" else "BUY"
            log.info("%s 재조회 실제수량=%s(side=%s)로 지정가 청산 재시도", symbol, live_qty, live["side"])
            return self.client.futures_create_order(
                symbol=symbol, side=live_side, type="LIMIT", quantity=live_qty,
                price=price, timeInForce="GTC", reduceOnly=True,
            )

    def get_order_status(self, symbol: str, order_id: int) -> dict:
        return self.client.futures_get_order(symbol=symbol, orderId=order_id)

    def cancel_regular_order(self, symbol: str, order_id: int) -> None:
        """place_stop_market 등이 쓰는 Algo Order와 달리, 일반 지정가 주문은 일반 취소
        엔드포인트를 쓴다 — 절대 헷갈리면 안 된다(Algo cancel로는 안 지워짐)."""
        try:
            self.client.futures_cancel_order(symbol=symbol, orderId=order_id)
            log.info("지정가 주문 취소: %s orderId=%s", symbol, order_id)
        except BinanceAPIException as e:
            if getattr(e, "code", None) != -2011:  # -2011: 이미 체결/취소됨
                log.warning("지정가 주문 취소 실패 %s orderId=%s: %s", symbol, order_id, e)

    def close_market_position(self, symbol: str, side: str, quantity: float):
        """[2026-08-10] 실거래 분석: -2022(ReduceOnly Order is rejected)로 청산이 두 번
        연속 실패하고, 27초 뒤에야 거래소 자체 스탑 주문이 대신 처리된 사례 발견(GRVTUSDT,
        -3.01USDT — 그 사이 불리한 방향으로 더 움직여 손실이 커짐). -2022는 대개 봇이 추적
        중인 수량과 거래소 실제 보유 수량이 어긋났을 때 발생하므로, 그 자리에서 즉시 거래소
        실제 수량을 재조회해 그 값으로 1회 재시도한다 — 다음 폴링 주기나 거래소 리모트
        스탑에만 의존하던 기존 동작보다 훨씬 빠르게 복구된다."""
        order_side = "SELL" if side == "LONG" else "BUY"
        log.info("시장가 청산 주문: %s %s qty=%s", symbol, order_side, quantity)
        try:
            return self.client.futures_create_order(
                symbol=symbol, side=order_side, type="MARKET", quantity=quantity, reduceOnly=True,
            )
        except BinanceAPIException as e:
            if getattr(e, "code", None) != -2022:
                raise
            log.warning("%s 청산 -2022(ReduceOnly 거부) — 실제 보유수량 재조회 후 즉시 재시도", symbol)
            live = self.get_position(symbol)
            if live is None or live["amount"] == 0:
                log.info("%s 재조회 결과 이미 포지션 없음 — 청산 불필요(이미 종료됨)", symbol)
                return None
            live_qty = abs(live["amount"])
            live_side = "SELL" if live["side"] == "LONG" else "BUY"
            log.info("%s 재조회 실제수량=%s(side=%s)로 재시도", symbol, live_qty, live["side"])
            return self.client.futures_create_order(
                symbol=symbol, side=live_side, type="MARKET", quantity=live_qty, reduceOnly=True,
            )

    def round_price(self, symbol: str, price: float) -> float:
        f = self.get_symbol_filters(symbol)
        precision = f["price_precision"]
        tick = f.get("tick_size", 0)
        if not tick:
            return round(price, precision)
        rounded = (Decimal(str(price)) / Decimal(str(tick))).to_integral_value(rounding=ROUND_DOWN) * Decimal(str(tick))
        return round(float(rounded), precision)

    def format_price(self, symbol: str, price: float) -> str:
        """API payload용 가격 문자열. 작은 가격이 8.3e-05처럼 과학 표기로 나가면 거부된다."""
        f = self.get_symbol_filters(symbol)
        precision = int(f["price_precision"])
        rounded = self.round_price(symbol, price)
        return f"{rounded:.{precision}f}"

    def place_stop_market(self, symbol: str, side: str, quantity: float, stop_price: float):
        """[2026-08-06] 진입 직후 거래소에 손절 주문을 걸어둔다 — 30초 폴링 사이 급락(실측:
        TAKEUSDT -20%, HFTUSDT -8.45%)에도 거래소가 즉시 반응하도록. side는 포지션 방향(LONG/SHORT).

        [2026-08-06 재시도] 일반 futures_create_order(type=STOP_MARKET)는 이 계정에서 -4120
        (Algo Order API 필요) 에러로 거부됨을 확인 — python-binance를 1.0.19->1.0.37로 올려
        futures_create_algo_order(신규 Algo Order 엔드포인트)로 재구현.
        """
        order_side = "SELL" if side == "LONG" else "BUY"
        stop_price = self.format_price(symbol, stop_price)
        log.info("STOP_MARKET(Algo) 주문 등록: %s %s qty=%s triggerPrice=%s", symbol, order_side, quantity, stop_price)
        return self.client.futures_create_algo_order(
            algoType="CONDITIONAL", symbol=symbol, side=order_side, type="STOP_MARKET",
            quantity=quantity, triggerPrice=stop_price, reduceOnly="true",
        )

    def place_stop_limit(self, symbol: str, side: str, quantity: float,
                         stop_price: float, limit_price: float):
        """[2026-08-27] 스탑-리밋 손절. 트리거되면 **지정가** 주문을 낸다.

        시장가 손절은 taker(0.05%) + 스프레드 전액을 낸다. 실측(85심볼) 스프레드
        중앙 0.0179% 라 손절 1건당 왕복비용의 절반 이상이 여기서 나온다.
        지정가로 앉으면 maker(0.02%) 가 될 수 있다.

        **다만 확실한 절감이 아니다.** 손절가는 정의상 현재가의 불리한 쪽이라,
        트리거 순간 시세가 그 가격이다. 호가에 상대가 있으면 즉시 taker 로 먹히고,
        없으면 maker 로 앉는다. 그리고 **급락으로 지나쳐 버리면 미체결로 남아
        포지션이 무방어가 된다** - 호출부가 반드시 미체결 감시를 걸어야 한다.
        """
        order_side = "SELL" if side == "LONG" else "BUY"
        stop_price = self.format_price(symbol, stop_price)
        limit_price = self.format_price(symbol, limit_price)
        log.info("STOP(Algo, 지정가) 주문 등록: %s %s qty=%s trigger=%s limit=%s",
                 symbol, order_side, quantity, stop_price, limit_price)
        return self.client.futures_create_algo_order(
            algoType="CONDITIONAL", symbol=symbol, side=order_side, type="STOP",
            quantity=quantity, triggerPrice=stop_price, price=limit_price,
            timeInForce="GTC", reduceOnly="true",
        )

    def place_trailing_stop_market(self, symbol: str, side: str, quantity: float, activation_price: float, callback_rate: float):
        """[2026-08-09] 거래소 네이티브 트레일링 익절 주문. 기존 폴링(30초 주기) 방식은 캔들
        사이 급반전을 놓쳐 트레일 목표 하락폭보다 훨씬 크게 밀린 뒤에야 청산되는 문제가 실측으로
        확인됨(PTBUSDT: 목표 1.5%p인데 실제 8.33%p 밀림) — 거래소가 실시간으로 처리하는 이
        주문으로 대체/보완한다. side는 포지션 방향(LONG/SHORT). callback_rate는 %단위 가격
        기준 되돌림폭(예: 0.375 = 0.375%, ROE 기준 trail_drawdown_pct를 레버리지로 나눈 값)이며
        바이낸스 허용범위(0.1~5)를 벗어나면 클램프한다. place_stop_market과 동일하게 이 계정은
        Algo Order API(futures_create_algo_order)가 필요하다."""
        order_side = "SELL" if side == "LONG" else "BUY"
        activation_price = self.round_price(symbol, activation_price)
        callback_rate = max(0.1, min(5.0, round(callback_rate, 2)))
        log.info(
            "TRAILING_STOP_MARKET(Algo) 주문 등록: %s %s qty=%s activationPrice=%s callbackRate=%s%%",
            symbol, order_side, quantity, activation_price, callback_rate,
        )
        return self.client.futures_create_algo_order(
            algoType="CONDITIONAL", symbol=symbol, side=order_side, type="TRAILING_STOP_MARKET",
            quantity=quantity, activatePrice=activation_price, callbackRate=callback_rate, reduceOnly="true",
        )

    def place_take_profit_market(self, symbol: str, side: str, quantity: float, trigger_price: float):
        """[2026-08-09] TRAILING_STOP_MARKET 등록/검증에 실패했을 때의 거래소측 폴백 익절 주문.
        기존엔 실패하면 30초 폴링 기반 청산으로만 대체돼 무방비 구간이 생겼는데(-2007 Invalid
        callBack rate 실측 사례), 트레일링만큼 정교하진 않아도 "최소 익절 목표가에서는 거래소가
        확정 체결해준다"는 보장을 걸어두기 위함. place_stop_market과 동일하게 이 계정은
        Algo Order API가 필요하다."""
        order_side = "SELL" if side == "LONG" else "BUY"
        trigger_price = self.round_price(symbol, trigger_price)
        log.info("TAKE_PROFIT_MARKET(Algo) 주문 등록: %s %s qty=%s triggerPrice=%s", symbol, order_side, quantity, trigger_price)
        return self.client.futures_create_algo_order(
            algoType="CONDITIONAL", symbol=symbol, side=order_side, type="TAKE_PROFIT_MARKET",
            quantity=quantity, triggerPrice=trigger_price, reduceOnly="true",
        )

    def cancel_order(self, symbol: str, algo_id: int):
        """지정한 Algo 주문을 취소한다. 이미 체결/취소돼 없는 주문이면 조용히 무시한다."""
        try:
            self.client.futures_cancel_algo_order(symbol=symbol, algoId=algo_id)
            log.info("STOP_MARKET(Algo) 주문 취소: %s algoId=%s", symbol, algo_id)
        except BinanceAPIException as e:
            if getattr(e, "code", None) != -2011:  # -2011: Unknown order sent(이미 체결/취소됨)
                log.warning("주문 취소 실패 %s algoId=%s: %s", symbol, algo_id, e)

    def get_open_algo_order_ids(self) -> set[int]:
        """[2026-08-07] 전체 계좌의 현재 살아있는 STOP_MARKET(Algo) 주문 ID 집합을 반환한다.
        GRVTUSDT 실측: 등록 로그는 있었지만 취소 로그 없이 거래소에서 주문이 사라져 포지션이
        무방비 상태로 남은 사고가 확인됨 — 원인 불문하고 주기적으로 대조해 복구하기 위함."""
        orders = self.client.futures_get_open_algo_orders()
        return {int(o["algoId"]) for o in orders}

    def get_open_algo_orders(self) -> list[dict]:
        """Return live open futures algo orders with their raw Binance fields.

        After a process restart, restored in-memory positions often have
        stop_order_id=None even when an exchange-side STOP_MARKET already exists.
        The restart safety net needs symbol/side/quantity/triggerPrice details
        so it can adopt an existing protective stop instead of placing a
        duplicate reduceOnly stop.
        """
        return list(self.client.futures_get_open_algo_orders())

    def cancel_orphan_algo_orders(self, held_symbols: set[str]) -> list[dict]:
        """[2026-08-17 실거래 사고로 추가] 포지션이 없는 심볼에 남아 있는 조건부 주문을 정리한다.

        사고: 거래소에 TRAILING_STOP_MARKET 4건이 고아로 남아 있었다(BTW/AIO/CYS/SPORTFUN).
        전부 reduceOnly라 포지션이 없을 땐 잠자고 있다가, **그 심볼에 새로 진입하는 순간
        즉시 발동해 방금 만든 포지션을 청산**한다. 사용자가 "진입하자마자 1초도 안 돼 매도된다"고
        보고한 현상의 원인이다(실측: CYSUSDT 15:28:28 진입 → 15:29:44 거래소 직접 종료,
        고아 주문은 14:12 생성분).

        원인: 재시작 시 STOP_MARKET은 "거래소에서 발견해 채택"하는 로직이 있지만
        TRAILING_STOP_MARKET에는 그 경로가 없어, 재시작마다 새로 등록하면서 옛 주문이 남는다.
        고아 생성시각이 재시작 직전과 정확히 맞물린다(12:32:21/13:45:35/14:35:55).

        근본 수정(트레일링 채택)은 범위가 크므로, 우선 기동 시 청소로 사고를 막는다.
        반환: 취소한 주문 목록(로그/알림용)."""
        cancelled: list[dict] = []
        try:
            orders = self.get_open_algo_orders()
        except Exception:
            log.exception("고아 조건부주문 조회 실패 — 이번 기동에서는 정리를 건너뜀")
            return cancelled
        for o in orders:
            symbol = o.get("symbol")
            if not symbol or symbol in held_symbols:
                continue
            try:
                self.client.futures_cancel_algo_order(symbol=symbol, algoId=o["algoId"])
                cancelled.append(o)
                log.warning(
                    "[%s] 포지션 없는 고아 조건부주문 취소: %s algoId=%s (신규 진입 즉시청산 방지)",
                    symbol, o.get("orderType"), o.get("algoId"),
                )
            except Exception:
                log.exception("[%s] 고아 조건부주문 취소 실패 algoId=%s", symbol, o.get("algoId"))
        return cancelled

    def find_matching_stop_market_order(
        self,
        open_algo_orders: list[dict],
        symbol: str,
        side: str,
        quantity: float,
        stop_price: float,
    ) -> dict | None:
        """Find an already-open reduceOnly STOP_MARKET order for this position."""
        close_side = "SELL" if side == "LONG" else "BUY"
        rounded_stop = self.round_price(symbol, stop_price)
        expected_qty = float(quantity)
        for order in open_algo_orders:
            if order.get("symbol") != symbol:
                continue
            if order.get("side") != close_side:
                continue
            order_type = order.get("type")
            if order_type not in (None, "STOP_MARKET"):
                continue
            if order.get("activatePrice") not in (None, "", "0", "0.0"):
                continue
            if order.get("callbackRate") not in (None, "", "0", "0.0"):
                continue
            try:
                order_trigger = float(order.get("triggerPrice"))
                order_qty = float(order.get("quantity"))
            except (TypeError, ValueError):
                continue
            if abs(order_trigger - rounded_stop) > 1e-12:
                continue
            if abs(order_qty - expected_qty) > 1e-12:
                continue
            return order
        return None
