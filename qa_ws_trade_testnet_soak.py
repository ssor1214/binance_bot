"""aggTrade 전용 워커(bot/ws_trade_worker.py) 테스트넷 격리 소크테스트.

qa_ws_testnet_compare.py와 동일한 격리 패턴: bot/ 소스를 별도 트리(qa_ws_trade_testnet_isolated/)
로 복사하고, PYTHONPATH를 그 트리로 돌려 완전히 독립된 프로세스로 돌린다. .env의
BINANCE_TESTNET_API_KEY/SECRET만 읽는다 — 라이브 메인넷 키는 이 스크립트 어디에서도
읽지 않는다(USE_TESTNET=true 강제).

절대 bot/main.py, run_forever.py를 건드리거나 기동하지 않는다 — 완전히 독립된 검증 스크립트.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
RUN_ROOT = ROOT / "qa_ws_trade_testnet_isolated"
SRC_BOT = ROOT / "bot"
RUN_BOT = RUN_ROOT / "bot"
RUN_LOGS = RUN_ROOT / "logs"
REPORT_PATH = ROOT / "logs" / f"qa_ws_trade_testnet_soak_{int(time.time())}.json"

DEFAULT_SYMBOLS = [
    "BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT", "XRPUSDT", "DOGEUSDT", "ADAUSDT", "TRXUSDT",
    "AVAXUSDT", "LINKUSDT", "TONUSDT", "SUIUSDT", "BCHUSDT", "LTCUSDT", "DOTUSDT", "1000PEPEUSDT",
    "UNIUSDT", "APTUSDT", "NEARUSDT", "ARBUSDT", "OPUSDT", "INJUSDT", "ETCUSDT", "FILUSDT",
    "ATOMUSDT", "TIAUSDT", "SEIUSDT", "WIFUSDT", "1000SHIBUSDT", "ORDIUSDT", "AAVEUSDT", "MKRUSDT",
    "JUPUSDT", "RUNEUSDT", "GALAUSDT", "ARUSDT", "LDOUSDT", "PENDLEUSDT", "FETUSDT", "RENDERUSDT",
    "WLDUSDT", "ENAUSDT", "STRKUSDT", "JTOUSDT", "PYTHUSDT", "DYDXUSDT", "STXUSDT", "IMXUSDT",
    "SANDUSDT", "MANAUSDT",
]


def load_env_file(path: Path) -> dict:
    values = {}
    if not path.exists():
        return values
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        values[k.strip()] = v.strip().strip('"').strip("'")
    return values


def prepare_isolated_tree() -> None:
    if RUN_BOT.exists():
        shutil.rmtree(RUN_BOT)
    RUN_ROOT.mkdir(exist_ok=True)
    RUN_LOGS.mkdir(exist_ok=True)
    shutil.copytree(SRC_BOT, RUN_BOT)


def base_env(symbols: list) -> dict:
    file_env = load_env_file(ROOT / ".env")
    env = dict(os.environ)
    env["PYTHONPATH"] = str(RUN_ROOT)
    env["USE_TESTNET"] = "true"
    env["WS_STATUS_DUMP_INTERVAL_SEC"] = "2.0"
    env["WS_TRADE_WORKER_SYMBOLS"] = json.dumps(symbols)
    env["AUTO_SYMBOLS"] = "false"
    env["SYMBOLS"] = ",".join(symbols)
    test_key = file_env.get("BINANCE_TESTNET_API_KEY")
    test_secret = file_env.get("BINANCE_TESTNET_API_SECRET")
    if not test_key or not test_secret:
        raise SystemExit("BINANCE_TESTNET_API_KEY/SECRET가 .env에 없습니다 — 중단")
    env["BINANCE_API_KEY"] = test_key
    env["BINANCE_API_SECRET"] = test_secret
    return env


def run_soak(duration_sec: int, symbols: list) -> dict:
    prepare_isolated_tree()
    env = base_env(symbols)
    stdout_log = open(RUN_LOGS / "trade_worker.stdout.log", "w", encoding="utf-8")
    stderr_log = open(RUN_LOGS / "trade_worker.stderr.log", "w", encoding="utf-8")
    proc = subprocess.Popen(
        [sys.executable, "-m", "bot.ws_trade_worker"],
        cwd=str(RUN_ROOT),
        env=env,
        stdout=stdout_log,
        stderr=stderr_log,
    )
    status_path = RUN_LOGS / "ws_trade_worker_status_trade.json"
    hb_path = RUN_LOGS / "ws_trade_worker_heartbeat_trade.txt"
    # ws_trade_worker.py writes into <repo>/logs relative to bot module location — override via LOG_DIR
    # (LOG_DIR = Path(__file__).resolve().parent.parent / "logs", parent of RUN_BOT == RUN_ROOT, so
    #  RUN_ROOT/logs is correct).
    started_at = time.time()
    last_heartbeat_seen = None
    max_heartbeat_gap = 0.0
    poll_errors = []
    crash_detected = False
    freeze_detected = False
    last_status_snapshot = None

    while time.time() - started_at < duration_sec:
        time.sleep(15)
        elapsed = time.time() - started_at
        if proc.poll() is not None:
            crash_detected = True
            poll_errors.append(f"process exited early at {elapsed:.0f}s with code {proc.returncode}")
            break
        if hb_path.exists():
            try:
                hb_ts = float(hb_path.read_text(encoding="utf-8").strip())
                gap = time.time() - hb_ts
                max_heartbeat_gap = max(max_heartbeat_gap, gap)
                if gap > 90:
                    freeze_detected = True
                    poll_errors.append(f"heartbeat gap {gap:.0f}s at elapsed {elapsed:.0f}s")
                last_heartbeat_seen = hb_ts
            except Exception as e:
                poll_errors.append(f"heartbeat read error at {elapsed:.0f}s: {e!r}")
        if status_path.exists():
            try:
                last_status_snapshot = json.loads(status_path.read_text(encoding="utf-8"))
            except Exception:
                pass
        print(f"[{elapsed:.0f}s/{duration_sec}s] alive=True heartbeat_gap={max_heartbeat_gap:.1f}s "
              f"health={last_status_snapshot.get('health') if last_status_snapshot else None}")

    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except Exception:
            proc.kill()

    stdout_log.close()
    stderr_log.close()

    stderr_text = (RUN_LOGS / "trade_worker.stderr.log").read_text(encoding="utf-8", errors="replace")
    traceback_lines = [l for l in stderr_text.splitlines() if "Traceback" in l]
    bad_patterns = ["MemoryError", "Read loop has been closed", "QueueOverflow",
                     "BinanceWebsocketQueueOverflow"]
    bad_hits = {p: stderr_text.count(p) for p in bad_patterns if p in stderr_text}

    result = {
        "duration_target_sec": duration_sec,
        "duration_actual_sec": time.time() - started_at,
        "symbols": symbols,
        "crash_detected": crash_detected,
        "freeze_detected": freeze_detected,
        "max_heartbeat_gap_sec": max_heartbeat_gap,
        "traceback_count": len(traceback_lines),
        "bad_pattern_hits": bad_hits,
        "poll_errors": poll_errors,
        "final_status_snapshot": last_status_snapshot,
    }
    REPORT_PATH.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print("REPORT_PATH:", REPORT_PATH)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return result


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--duration-sec", type=int, default=1800)
    ap.add_argument("--symbols", type=str, default=",".join(DEFAULT_SYMBOLS))
    args = ap.parse_args()
    syms = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    run_soak(args.duration_sec, syms)
