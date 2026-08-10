from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parent
RUN_ROOT = ROOT / "qa_ws_testnet_isolated"
SRC_BOT = ROOT / "bot"
RUN_BOT = RUN_ROOT / "bot"
RUN_LOGS = RUN_ROOT / "logs"
REPORT_PATH = ROOT / "logs" / f"qa_ws_testnet_compare_{int(time.time())}.json"

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
    RUN_ROOT.mkdir(exist_ok=True)
    RUN_LOGS.mkdir(exist_ok=True)
    shutil.copytree(SRC_BOT, RUN_BOT)


def base_env() -> dict[str, str]:
    file_env = load_env_file(ROOT / ".env")
    env = dict(os.environ)
    env["PYTHONPATH"] = str(RUN_ROOT)
    env["USE_TESTNET"] = "true"
    env["WS_MARKET_DATA_ENABLED"] = "true"
    env["WS_USER_DATA_ENABLED"] = "false"
    env["SYMBOLS"] = ",".join(DEFAULT_100_SYMBOLS)
    env["AUTO_SYMBOLS"] = "false"
    env["MAX_AUTO_SYMBOLS"] = "100"
    env["INTERVAL"] = "1m"
    env["WS_KLINE_HISTORY_LEN"] = "100"
    env["OPENBLAS_NUM_THREADS"] = "1"
    env["OMP_NUM_THREADS"] = "1"
    env["MKL_NUM_THREADS"] = "1"
    test_key = file_env.get("BINANCE_TESTNET_API_KEY")
    test_secret = file_env.get("BINANCE_TESTNET_API_SECRET")
    if test_key:
        env["BINANCE_API_KEY"] = test_key
    if test_secret:
        env["BINANCE_API_SECRET"] = test_secret
    return env


def cache_path(role: str, shard_index: int, shard_count: int) -> Path:
    suffix = ""
    if role == "market" and shard_count > 1:
        suffix = f"_market{shard_index}"
    elif role == "user":
        suffix = "_user"
    return RUN_LOGS / f"ws_worker_cache{suffix}.json"


def read_health(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data.get("health") or {}
    except Exception:
        return {}


def run_phase(name: str, shard_count: int, duration_sec: int) -> dict:
    env = base_env()
    procs: list[subprocess.Popen] = []
    log_handles = []
    for shard_index in range(shard_count):
        worker_env = dict(env)
        worker_env["WS_WORKER_ROLE"] = "market"
        worker_env["WS_SHARD_INDEX"] = str(shard_index)
        worker_env["WS_SHARD_COUNT"] = str(shard_count)
        out_path = RUN_LOGS / f"{name}_shard{shard_index}.stdout.log"
        err_path = RUN_LOGS / f"{name}_shard{shard_index}.stderr.log"
        out_f = out_path.open("w", encoding="utf-8", errors="ignore")
        err_f = err_path.open("w", encoding="utf-8", errors="ignore")
        log_handles.extend([out_f, err_f])
        procs.append(
            subprocess.Popen(
                [sys.executable, "-m", "bot.ws_worker"],
                cwd=str(RUN_ROOT),
                env=worker_env,
                stdout=out_f,
                stderr=err_f,
            )
        )

    start = time.time()
    samples = []
    try:
        while time.time() - start < duration_sec:
            time.sleep(30)
            shard_samples = []
            for shard_index, proc in enumerate(procs):
                health = read_health(cache_path("market", shard_index, shard_count))
                shard_samples.append(
                    {
                        "shard_index": shard_index,
                        "pid": proc.pid,
                        "returncode": proc.poll(),
                        "health": health,
                    }
                )
            samples.append({"elapsed_sec": round(time.time() - start, 1), "shards": shard_samples})
            REPORT_PATH.parent.mkdir(exist_ok=True)
            REPORT_PATH.write_text(json.dumps({"in_progress": name, "samples": samples}, indent=2), encoding="utf-8")
    finally:
        for proc in procs:
            if proc.poll() is None:
                proc.terminate()
        deadline = time.time() + 10
        for proc in procs:
            while proc.poll() is None and time.time() < deadline:
                time.sleep(0.2)
            if proc.poll() is None:
                proc.kill()
        for handle in log_handles:
            handle.close()

    read_loop_errors = 0
    queue_errors = 0
    log_path = RUN_LOGS / "ws_worker.log"
    if log_path.exists():
        text = log_path.read_text(encoding="utf-8", errors="ignore")
        read_loop_errors = text.count("Read loop has been closed")
        queue_errors = text.count("QueueOverflow") + text.count("queue size 100")

    return {
        "name": name,
        "shard_count": shard_count,
        "duration_sec": duration_sec,
        "samples": samples,
        "read_loop_error_count_in_isolated_log": read_loop_errors,
        "queue_overflow_error_count_in_isolated_log": queue_errors,
    }


def main() -> int:
    duration_each = int(os.environ.get("QA_WS_PHASE_SEC", "2700"))
    phase_filter = os.environ.get("QA_WS_ONLY_PHASE", "").strip().lower()
    prepare_isolated_tree()
    phase_specs = {
        "single": ("single_worker_100_symbols", 1),
        "50x2": ("two_workers_50x2_symbols", 2),
        "two": ("two_workers_50x2_symbols", 2),
    }
    if phase_filter:
        name, shard_count = phase_specs[phase_filter]
        phases = [run_phase(name, shard_count, duration_each)]
    else:
        phases = [
            run_phase("single_worker_100_symbols", 1, duration_each),
            run_phase("two_workers_50x2_symbols", 2, duration_each),
        ]
    report = {"created_at": time.time(), "duration_each_sec": duration_each, "phases": phases}
    REPORT_PATH.parent.mkdir(exist_ok=True)
    REPORT_PATH.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(REPORT_PATH)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
