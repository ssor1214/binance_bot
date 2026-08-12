from __future__ import annotations

import json
import math
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

from binance import Client


ROOT = Path(__file__).resolve().parent
RUN_ROOT = ROOT / "qa_ws_user_stream_isolated"
SRC_BOT = ROOT / "bot"
RUN_BOT = RUN_ROOT / "bot"
RUN_LOGS = RUN_ROOT / "logs"
REPORT_PATH = ROOT / "logs" / f"qa_ws_user_stream_fill_{int(time.time())}.json"


def load_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def prepare_isolated_tree() -> None:
    if RUN_BOT.exists():
        shutil.rmtree(RUN_BOT)
    if RUN_LOGS.exists():
        shutil.rmtree(RUN_LOGS)
    RUN_ROOT.mkdir(exist_ok=True)
    RUN_LOGS.mkdir(exist_ok=True)
    shutil.copytree(SRC_BOT, RUN_BOT)


def base_env() -> dict[str, str]:
    file_env = load_env_file(ROOT / ".env")
    env = dict(os.environ)
    env["PYTHONPATH"] = str(RUN_ROOT)
    env["USE_TESTNET"] = "true"
    env["WS_MARKET_DATA_ENABLED"] = "false"
    env["WS_USER_DATA_ENABLED"] = "true"
    env["WS_STATUS_DUMP_INTERVAL_SEC"] = "1"
    env["WS_CACHE_DUMP_INTERVAL_SEC"] = "60"
    env["OPENBLAS_NUM_THREADS"] = "1"
    env["OMP_NUM_THREADS"] = "1"
    env["MKL_NUM_THREADS"] = "1"
    test_key = file_env.get("BINANCE_TESTNET_API_KEY")
    test_secret = file_env.get("BINANCE_TESTNET_API_SECRET")
    if not test_key or not test_secret:
        raise RuntimeError("BINANCE_TESTNET_API_KEY / BINANCE_TESTNET_API_SECRET not found in .env")
    env["BINANCE_API_KEY"] = test_key
    env["BINANCE_API_SECRET"] = test_secret
    return env


def status_path() -> Path:
    return RUN_LOGS / "ws_worker_status_user.json"


def read_status() -> dict:
    try:
        return json.loads(status_path().read_text(encoding="utf-8"))
    except Exception:
        return {}


def read_fill(symbol: str) -> dict | None:
    status = read_status()
    fill = (status.get("fills") or {}).get(symbol)
    return fill if isinstance(fill, dict) else None


def wait_for_fill(symbol: str, side: str, timeout_sec: float = 45.0) -> dict | None:
    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        fill = read_fill(symbol)
        if fill and fill.get("side") == side:
            return fill
        time.sleep(0.5)
    return None


def symbol_filters(client: Client, symbol: str) -> dict:
    info = client.futures_exchange_info()
    for item in info["symbols"]:
        if item["symbol"] != symbol:
            continue
        filters = {f["filterType"]: f for f in item["filters"]}
        lot = filters.get("LOT_SIZE", {})
        min_notional_filter = filters.get("MIN_NOTIONAL", {})
        return {
            "quantity_precision": int(item["quantityPrecision"]),
            "min_qty": float(lot.get("minQty", 0)),
            "step_size": float(lot.get("stepSize", 0)),
            "min_notional": float(min_notional_filter.get("notional", min_notional_filter.get("minNotional", 5))),
        }
    raise RuntimeError(f"symbol not found: {symbol}")


def round_up_quantity(qty: float, step: float, precision: int) -> float:
    if step <= 0:
        return round(qty, precision)
    return round(math.ceil(qty / step) * step, precision)


def choose_quantity(client: Client, symbol: str) -> tuple[float, float, dict]:
    ticker = client.futures_symbol_ticker(symbol=symbol)
    price = float(ticker["price"])
    filters = symbol_filters(client, symbol)
    target_notional = max(filters["min_notional"] * 1.25, 6.0)
    qty = round_up_quantity(target_notional / price, filters["step_size"], filters["quantity_precision"])
    qty = max(qty, filters["min_qty"])
    return qty, price, filters


def current_position_amt(client: Client, symbol: str) -> float:
    positions = client.futures_position_information(symbol=symbol)
    if not positions:
        return 0.0
    return float(positions[0].get("positionAmt", 0.0))


def close_any_position(client: Client, symbol: str) -> dict | None:
    amt = current_position_amt(client, symbol)
    if abs(amt) <= 0:
        return None
    side = "SELL" if amt > 0 else "BUY"
    return client.futures_create_order(symbol=symbol, side=side, type="MARKET", quantity=abs(amt), reduceOnly=True)


def main() -> int:
    prepare_isolated_tree()
    env = base_env()
    symbol = os.environ.get("QA_WS_USER_SYMBOL", "DOGEUSDT")
    report: dict = {
        "created_at": time.time(),
        "symbol": symbol,
        "status_path": str(status_path()),
        "events": [],
        "final": {},
    }

    client = Client(env["BINANCE_API_KEY"], env["BINANCE_API_SECRET"], testnet=True)
    server_time = client.futures_time()["serverTime"]
    client.timestamp_offset = server_time - int(time.time() * 1000)

    proc = subprocess.Popen(
        [sys.executable, "-m", "bot.ws_worker"],
        cwd=str(RUN_ROOT),
        env={**env, "WS_WORKER_ROLE": "user", "WS_SHARD_INDEX": "0", "WS_SHARD_COUNT": "1"},
    )

    opened = False
    try:
        deadline = time.time() + 30
        while time.time() < deadline and not status_path().exists():
            time.sleep(0.5)
        if not status_path().exists():
            raise RuntimeError("user stream status file was not created")

        qty, price, filters = choose_quantity(client, symbol)
        report["quantity"] = qty
        report["price_before"] = price
        report["filters"] = filters

        # Ensure the chosen test symbol starts flat; do not touch other symbols.
        close_any_position(client, symbol)
        time.sleep(1)

        open_order = client.futures_create_order(symbol=symbol, side="BUY", type="MARKET", quantity=qty)
        opened = True
        report["events"].append({
            "action": "open_market_buy",
            "order_id": open_order.get("orderId"),
            "status": open_order.get("status"),
        })
        buy_fill = wait_for_fill(symbol, "BUY")
        report["buy_fill"] = buy_fill

        close_order = client.futures_create_order(symbol=symbol, side="SELL", type="MARKET", quantity=qty, reduceOnly=True)
        opened = False
        report["events"].append({
            "action": "close_reduce_only_sell",
            "order_id": close_order.get("orderId"),
            "status": close_order.get("status"),
        })
        sell_fill = wait_for_fill(symbol, "SELL")
        report["sell_fill"] = sell_fill

        final_amt = current_position_amt(client, symbol)
        report["final"] = {
            "buy_fill_seen": buy_fill is not None,
            "sell_fill_seen": sell_fill is not None,
            "final_position_amt": final_amt,
            "status_file_bytes": status_path().stat().st_size if status_path().exists() else 0,
            "worker_returncode_before_stop": proc.poll(),
        }
    finally:
        if opened:
            try:
                close_any_position(client, symbol)
            except Exception as exc:
                report.setdefault("cleanup_errors", []).append(str(exc))
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except Exception:
                proc.kill()
                proc.wait(timeout=10)
        REPORT_PATH.parent.mkdir(exist_ok=True)
        REPORT_PATH.write_text(json.dumps(report, indent=2), encoding="utf-8")

    print(REPORT_PATH)
    ok = (
        report.get("final", {}).get("buy_fill_seen")
        and report.get("final", {}).get("sell_fill_seen")
        and abs(float(report.get("final", {}).get("final_position_amt", 999))) < 1e-12
    )
    return 0 if ok else 2


if __name__ == "__main__":
    raise SystemExit(main())
