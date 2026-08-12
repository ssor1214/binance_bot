from __future__ import annotations

import json
import os
import shutil
import sys
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parent
RUN_ROOT = ROOT / "qa_ws_watchdog_isolated"
SRC_BOT = ROOT / "bot"
RUN_BOT = RUN_ROOT / "bot"
RUN_LOGS = RUN_ROOT / "logs"
REPORT_PATH = ROOT / "logs" / f"qa_ws_watchdog_restart_{int(time.time())}.json"

DEFAULT_100_SYMBOLS = [
    "BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT", "XRPUSDT", "DOGEUSDT", "ADAUSDT", "TRXUSDT", "AVAXUSDT", "LINKUSDT",
    "TONUSDT", "SUIUSDT", "BCHUSDT", "LTCUSDT", "DOTUSDT", "UNIUSDT", "APTUSDT", "NEARUSDT", "ARBUSDT", "OPUSDT",
    "INJUSDT", "ETCUSDT", "FILUSDT", "ATOMUSDT", "TIAUSDT", "SEIUSDT", "WIFUSDT", "1000PEPEUSDT", "1000SHIBUSDT", "ORDIUSDT",
    "AAVEUSDT", "MKRUSDT", "JUPUSDT", "RUNEUSDT", "GALAUSDT", "ARUSDT", "LDOUSDT", "PENDLEUSDT", "FETUSDT", "RENDERUSDT",
    "WLDUSDT", "ENAUSDT", "STRKUSDT", "JTOUSDT", "PYTHUSDT", "DYDXUSDT", "STXUSDT", "IMXUSDT", "SANDUSDT", "MANAUSDT",
    "APEUSDT", "AXSUSDT", "GMTUSDT", "CHZUSDT", "CRVUSDT", "COMPUSDT", "SNXUSDT", "YFIUSDT", "SUSHIUSDT", "1INCHUSDT",
    "ENSUSDT", "LRCUSDT", "ZRXUSDT", "KAVAUSDT", "MINAUSDT", "ROSEUSDT", "CELOUSDT", "IOTAUSDT", "ZILUSDT", "ONTUSDT",
    "ICXUSDT", "QTUMUSDT", "VETUSDT", "ALGOUSDT", "EGLDUSDT", "KSMUSDT", "FLOWUSDT", "CFXUSDT", "ACHUSDT", "BLURUSDT",
    "MAGICUSDT", "MASKUSDT", "HOOKUSDT", "IDUSDT", "LQTYUSDT", "RDNTUSDT", "AGIXUSDT", "GMXUSDT", "HIGHUSDT", "SSVUSDT",
    "PHBUSDT", "PERPUSDT", "TRBUSDT", "API3USDT", "TWTUSDT", "FXSUSDT", "LPTUSDT", "CKBUSDT", "BICOUSDT", "ZECUSDT",
]


class DummyExchange:
    def __init__(self) -> None:
        self._ws_kline_cache = None
        self._fill_tracker = None

    def set_ws_kline_cache(self, cache) -> None:
        self._ws_kline_cache = cache

    def set_fill_tracker(self, tracker) -> None:
        self._fill_tracker = tracker


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


def apply_testnet_env() -> None:
    file_env = load_env_file(ROOT / ".env")
    os.environ["PYTHONPATH"] = str(RUN_ROOT)
    os.environ["USE_TESTNET"] = "true"
    os.environ["WS_MARKET_DATA_ENABLED"] = "true"
    os.environ["WS_USER_DATA_ENABLED"] = "false"
    os.environ["WS_MARKET_SHARD_COUNT"] = "2"
    os.environ["SYMBOLS"] = ",".join(DEFAULT_100_SYMBOLS)
    os.environ["AUTO_SYMBOLS"] = "false"
    os.environ["MAX_AUTO_SYMBOLS"] = "100"
    os.environ["INTERVAL"] = "1m"
    os.environ["WS_KLINE_HISTORY_LEN"] = "100"
    os.environ["WS_STATUS_DUMP_INTERVAL_SEC"] = "2"
    os.environ["WS_CACHE_DUMP_INTERVAL_SEC"] = "60"
    os.environ["WS_MESSAGE_MAX_STALENESS_SEC"] = "45"
    os.environ["WS_MAX_CONSECUTIVE_READ_LOOP_ERRORS"] = "3"
    os.environ["WS_RESTART_BACKOFF_BASE_SEC"] = "5"
    os.environ["WS_RESTART_BACKOFF_MAX_SEC"] = "120"
    os.environ["OPENBLAS_NUM_THREADS"] = "1"
    os.environ["OMP_NUM_THREADS"] = "1"
    os.environ["MKL_NUM_THREADS"] = "1"
    test_key = file_env.get("BINANCE_TESTNET_API_KEY")
    test_secret = file_env.get("BINANCE_TESTNET_API_SECRET")
    if test_key:
        os.environ["BINANCE_API_KEY"] = test_key
    if test_secret:
        os.environ["BINANCE_API_SECRET"] = test_secret


def status_path(shard_index: int) -> Path:
    return RUN_LOGS / f"ws_worker_status_market{shard_index}.json"


def read_status(shard_index: int) -> dict:
    try:
        return json.loads(status_path(shard_index).read_text(encoding="utf-8"))
    except Exception:
        return {}


def sample(handles: dict, elapsed: float, event: str) -> dict:
    shards = []
    for idx, worker in enumerate(handles.get("workers", [])):
        status = read_status(worker["shard_index"])
        shards.append({
            "idx": idx,
            "role": worker.get("role"),
            "shard_index": worker.get("shard_index"),
            "pid": worker["process"].pid,
            "returncode": worker["process"].poll(),
            "health": status.get("health") or {},
            "status_dumped_at": status.get("dumped_at"),
        })
    return {"elapsed_sec": round(elapsed, 1), "event": event, "shards": shards}


def write_report(report: dict) -> None:
    REPORT_PATH.parent.mkdir(exist_ok=True)
    REPORT_PATH.write_text(json.dumps(report, indent=2), encoding="utf-8")


def main() -> int:
    prepare_isolated_tree()
    apply_testnet_env()
    sys.path.insert(0, str(RUN_ROOT))

    from bot.config import Config
    import bot.main as bot_main

    # The isolated bot copy must own all paths and worker subprocesses for this QA run.
    bot_main.LOG_DIR = RUN_LOGS
    bot_main.WS_WORKER_STARTUP_GRACE_SEC = 90.0

    cfg = Config()
    ex = DummyExchange()
    symbols = DEFAULT_100_SYMBOLS
    report = {
        "created_at": time.time(),
        "duration_sec": int(os.environ.get("QA_WS_WATCHDOG_SEC", "1800")),
        "kill_after_sec": int(os.environ.get("QA_WS_WATCHDOG_KILL_AFTER_SEC", "150")),
        "samples": [],
        "restart_events": [],
        "final": {},
    }

    handles = bot_main.start_ws_layer(ex, cfg, symbols)
    start = time.time()
    killed = False
    restarted = False
    killed_old_pid = None

    try:
        while time.time() - start < report["duration_sec"]:
            elapsed = time.time() - start
            if not killed and elapsed >= report["kill_after_sec"]:
                worker = handles["workers"][0]
                killed_old_pid = worker["process"].pid
                worker["process"].terminate()
                try:
                    worker["process"].wait(timeout=5)
                except Exception:
                    worker["process"].kill()
                    worker["process"].wait(timeout=5)
                killed = True
                report["restart_events"].append({
                    "elapsed_sec": round(time.time() - start, 1),
                    "action": "terminated_shard0",
                    "old_pid": killed_old_pid,
                })

            stale = bot_main.ws_layer_needs_restart(cfg, handles, "BTCUSDT")
            if stale:
                report["restart_events"].append({
                    "elapsed_sec": round(time.time() - start, 1),
                    "action": "watchdog_detected",
                    "stale_indices": stale,
                })
                restart_now = [
                    idx for idx in stale
                    if time.time() >= handles["workers"][idx].get("next_restart_allowed_at", 0.0)
                ]
                if restart_now:
                    bot_main.stop_ws_layer(ex, handles, only_indices=restart_now)
                    for idx in restart_now:
                        worker = handles["workers"][idx]
                        new_process = bot_main._spawn_ws_worker(worker["role"], worker["shard_index"], worker["shard_count"])
                        worker["process"] = new_process
                        worker["started_at"] = time.time()
                        backoff = bot_main._compute_ws_restart_backoff_sec(worker["consecutive_restart_count"], cfg)
                        worker["next_restart_allowed_at"] = time.time() + backoff
                        worker["consecutive_restart_count"] += 1
                        report["restart_events"].append({
                            "elapsed_sec": round(time.time() - start, 1),
                            "action": "restarted",
                            "idx": idx,
                            "old_pid": killed_old_pid if idx == 0 else None,
                            "new_pid": new_process.pid,
                            "backoff_sec": backoff,
                        })
                        restarted = True

            report["samples"].append(sample(handles, time.time() - start, "poll"))
            write_report(report)
            time.sleep(30)
    finally:
        bot_main.stop_ws_layer(ex, handles)

    final_sample = report["samples"][-1] if report["samples"] else {}
    report["final"] = {
        "killed": killed,
        "restarted": restarted,
        "old_pid_replaced": bool(killed_old_pid and any(
            e.get("action") == "restarted" and e.get("old_pid") == killed_old_pid and e.get("new_pid") != killed_old_pid
            for e in report["restart_events"]
        )),
        "final_sample": final_sample,
    }
    write_report(report)
    print(REPORT_PATH)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
