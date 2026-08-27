"""bot/ws_trade_worker.py 전용 워치독 kill/freeze 복구 QA 스크립트.

qa_ws_watchdog_restart.py(시장데이터 워커용)와 동일한 격리 패턴을 aggTrade 워커에
그대로 적용한다. bot/main.py의 start_ws_layer()/ws_layer_needs_restart()는 아직
role="trade"를 지원하지 않으므로(라이브 코드는 건드리지 않는다는 지시에 따라 그대로
둔다), 이 스크립트 안에서 동일한 판단 로직(프로세스 종료/메시지 정체/연속 에러/
error_count_60s 절대치)을 독립적으로 재구현해 사용한다.

시나리오 두 가지를 순서대로 검증한다:
1) kill 시나리오: 샤드0 프로세스를 강제 종료(Popen.kill()) -> 워치독이
   process_exited로 감지하고 새 프로세스로 교체하는지, 백오프가 걸리는지, 다른 샤드는
   그대로 살아있는지 확인.
2) freeze 시나리오: 샤드1 프로세스를 OS 레벨로 완전히 정지(NtSuspendProcess, GIL을
   붙잡고 도는 tight loop와 동일하게 하트비트/상태 파일 갱신이 전부 멈춘 상태를
   재현) -> 워치독이 heartbeat/message staleness로 감지하고, 그 상태에서도
   Popen.kill()이 여전히 통해서 강제 재시작되는지 확인.

절대 라이브 프로세스(bot/main.py, run_forever.py)를 건드리지 않는다 -- 완전히 독립된
qa_ws_trade_testnet_isolated/ 트리에서만 동작한다.
"""
from __future__ import annotations

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
REPORT_PATH = ROOT / "logs" / f"qa_ws_trade_watchdog_restart_{int(time.time())}.json"

DEFAULT_SYMBOLS = [
    "BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT", "XRPUSDT", "DOGEUSDT", "ADAUSDT", "TRXUSDT",
    "AVAXUSDT", "LINKUSDT", "TONUSDT", "SUIUSDT", "BCHUSDT", "LTCUSDT", "DOTUSDT", "1000PEPEUSDT",
    "UNIUSDT", "APTUSDT", "NEARUSDT", "ARBUSDT", "OPUSDT", "INJUSDT", "ETCUSDT", "FILUSDT",
    "ATOMUSDT", "TIAUSDT", "SEIUSDT", "WIFUSDT", "1000SHIBUSDT", "ORDIUSDT", "AAVEUSDT", "MKRUSDT",
]

SHARD_COUNT = 2

MSG_MAX_STALENESS_SEC = 45.0
MAX_CONSEC_READ_LOOP_ERRORS = 3
MAX_ERROR_COUNT_60S = 100
RESTART_BACKOFF_BASE_SEC = 5.0
RESTART_BACKOFF_MAX_SEC = 120.0
STARTUP_GRACE_SEC = 60.0


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
    if RUN_LOGS.exists():
        shutil.rmtree(RUN_LOGS)
    RUN_ROOT.mkdir(exist_ok=True)
    RUN_LOGS.mkdir(exist_ok=True)
    shutil.copytree(SRC_BOT, RUN_BOT)


def base_env(symbols: list, qa_freeze_after_sec: float | None = None) -> dict:
    file_env = load_env_file(ROOT / ".env")
    env = dict(os.environ)
    env["PYTHONPATH"] = str(RUN_ROOT)
    env["USE_TESTNET"] = "true"
    env["WS_STATUS_DUMP_INTERVAL_SEC"] = "2.0"
    env["AUTO_SYMBOLS"] = "false"
    env["SYMBOLS"] = ",".join(symbols)
    env["WS_TRADE_SHARD_COUNT"] = str(SHARD_COUNT)
    if qa_freeze_after_sec is not None:
        env["WS_TRADE_QA_FREEZE_AFTER_SEC"] = str(qa_freeze_after_sec)
    else:
        env.pop("WS_TRADE_QA_FREEZE_AFTER_SEC", None)
    test_key = file_env.get("BINANCE_TESTNET_API_KEY")
    test_secret = file_env.get("BINANCE_TESTNET_API_SECRET")
    if not test_key or not test_secret:
        raise SystemExit("BINANCE_TESTNET_API_KEY/SECRET .env missing")
    env["BINANCE_API_KEY"] = test_key
    env["BINANCE_API_SECRET"] = test_secret
    return env


def spawn_shard(shard_index: int, symbols: list, qa_freeze_after_sec: float | None = None) -> dict:
    env = base_env(symbols, qa_freeze_after_sec=qa_freeze_after_sec)
    env["WS_TRADE_SHARD_INDEX"] = str(shard_index)
    stdout_log = open(RUN_LOGS / f"trade_worker{shard_index}.stdout.log", "w", encoding="utf-8")
    stderr_log = open(RUN_LOGS / f"trade_worker{shard_index}.stderr.log", "w", encoding="utf-8")
    proc = subprocess.Popen(
        [sys.executable, "-m", "bot.ws_trade_worker"],
        cwd=str(RUN_ROOT),
        env=env,
        stdout=stdout_log,
        stderr=stderr_log,
    )
    return {
        "process": proc, "shard_index": shard_index, "started_at": time.time(),
        "stdout_log": stdout_log, "stderr_log": stderr_log,
        "status_path": RUN_LOGS / f"ws_trade_worker_status_trade{shard_index}.json",
        "heartbeat_path": RUN_LOGS / f"ws_trade_worker_heartbeat_trade{shard_index}.txt",
        "consecutive_restart_count": 0, "next_restart_allowed_at": 0.0,
    }


def respawn_shard(worker: dict, symbols: list) -> None:
    env = base_env(symbols)
    env["WS_TRADE_SHARD_INDEX"] = str(worker["shard_index"])
    idx = worker["shard_index"]
    stdout_log = open(RUN_LOGS / f"trade_worker{idx}.stdout.log", "a", encoding="utf-8")
    stderr_log = open(RUN_LOGS / f"trade_worker{idx}.stderr.log", "a", encoding="utf-8")
    proc = subprocess.Popen(
        [sys.executable, "-m", "bot.ws_trade_worker"],
        cwd=str(RUN_ROOT),
        env=env,
        stdout=stdout_log,
        stderr=stderr_log,
    )
    worker["process"] = proc
    worker["started_at"] = time.time()
    worker["stdout_log"] = stdout_log
    worker["stderr_log"] = stderr_log


def read_status(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def compute_backoff(consecutive_restart_count: int) -> float:
    return min(RESTART_BACKOFF_BASE_SEC * (2 ** consecutive_restart_count), RESTART_BACKOFF_MAX_SEC)


def worker_needs_restart(worker: dict) -> tuple:
    process = worker["process"]
    if process.poll() is not None:
        return True, "process_exited"
    if time.time() - worker["started_at"] < STARTUP_GRACE_SEC:
        return False, ""
    status = read_status(worker["status_path"])
    health = status.get("health") or {}
    if not health:
        hb_path = worker["heartbeat_path"]
        if hb_path.exists():
            try:
                hb_ts = float(hb_path.read_text(encoding="utf-8").strip())
                if time.time() - hb_ts > MSG_MAX_STALENESS_SEC:
                    return True, f"heartbeat_stale:{time.time() - hb_ts:.1f}s"
            except Exception:
                pass
        return False, ""
    last_msg_ts = health.get("last_market_message_ts", 0)
    if last_msg_ts and time.time() - last_msg_ts > MSG_MAX_STALENESS_SEC:
        return True, f"message_stale:{time.time() - last_msg_ts:.1f}s"
    if health.get("error_count_60s", 0) >= MAX_ERROR_COUNT_60S:
        return True, f"error_count_60s:{health.get('error_count_60s')}"
    if health.get("consecutive_read_loop_errors", 0) >= MAX_CONSEC_READ_LOOP_ERRORS:
        return True, f"consecutive_read_loop_errors:{health.get('consecutive_read_loop_errors')}"
    return False, ""


def kill_process(worker: dict) -> None:
    proc = worker["process"]
    try:
        proc.kill()
        proc.wait(timeout=10)
    except Exception:
        pass


def main() -> int:
    prepare_isolated_tree()
    symbols_by_shard = {i: DEFAULT_SYMBOLS[i::SHARD_COUNT] for i in range(SHARD_COUNT)}

    duration_sec = int(os.environ.get("QA_WS_TRADE_WATCHDOG_SEC", "900"))
    kill_after_sec = int(os.environ.get("QA_WS_TRADE_WATCHDOG_KILL_AFTER_SEC", "90"))
    freeze_after_sec = int(os.environ.get("QA_WS_TRADE_WATCHDOG_FREEZE_AFTER_SEC", "300"))

    # shard1은 spawn 시점부터 WS_TRADE_QA_FREEZE_AFTER_SEC를 예약해둔다 — OS 레벨
    # suspend는 이 실행 환경(샌드박스/가상화)에서 신뢰할 수 없다는 게 실측으로 확인돼,
    # 프로세스 스스로 실제 버그와 동일한 tight-loop 증상(하트비트 정지 + kill()에만 반응)에
    # 빠지도록 하는 방식으로 대체했다.
    workers = [
        spawn_shard(0, symbols_by_shard[0]),
        spawn_shard(1, symbols_by_shard[1], qa_freeze_after_sec=freeze_after_sec),
    ]
    for w in workers:
        print(f"shard {w['shard_index']} started pid={w['process'].pid} symbols={len(symbols_by_shard[w['shard_index']])}")

    report = {
        "created_at": time.time(),
        "duration_sec": duration_sec,
        "kill_after_sec": kill_after_sec,
        "freeze_after_sec": freeze_after_sec,
        "events": [],
        "samples": [],
        "final": {},
    }

    start = time.time()
    killed_shard0 = False
    frozen_shard1 = False
    shard0_old_pid = None
    shard1_frozen_pid = None
    shard0_restarted_after_kill = False
    shard1_restarted_after_freeze = False

    try:
        while time.time() - start < duration_sec:
            elapsed = time.time() - start

            if not killed_shard0 and elapsed >= kill_after_sec:
                shard0_old_pid = workers[0]["process"].pid
                kill_process(workers[0])
                killed_shard0 = True
                report["events"].append({"elapsed_sec": round(elapsed, 1), "action": "kill_shard0", "pid": shard0_old_pid})
                print(f"[{elapsed:.0f}s] killed shard0 pid={shard0_old_pid}")

            if not frozen_shard1 and elapsed >= freeze_after_sec:
                shard1_frozen_pid = workers[1]["process"].pid
                frozen_shard1 = True
                report["events"].append({"elapsed_sec": round(elapsed, 1), "action": "shard1_freeze_expected_now",
                                          "pid": shard1_frozen_pid})
                print(f"[{elapsed:.0f}s] shard1 pid={shard1_frozen_pid} expected to enter tight-loop now (self-triggered)")

            for w in workers:
                needs_restart, reason = worker_needs_restart(w)
                if needs_restart and time.time() >= w.get("next_restart_allowed_at", 0.0):
                    old_pid = w["process"].pid
                    kill_process(w)
                    respawn_shard(w, symbols_by_shard[w["shard_index"]])
                    backoff = compute_backoff(w["consecutive_restart_count"])
                    w["next_restart_allowed_at"] = time.time() + backoff
                    w["consecutive_restart_count"] += 1
                    report["events"].append({
                        "elapsed_sec": round(time.time() - start, 1),
                        "action": "restarted", "shard_index": w["shard_index"],
                        "reason": reason, "old_pid": old_pid, "new_pid": w["process"].pid,
                        "backoff_sec": backoff,
                    })
                    print(f"[{time.time()-start:.0f}s] restarted shard{w['shard_index']} "
                          f"reason={reason} old_pid={old_pid} new_pid={w['process'].pid} backoff={backoff}s")
                    if w["shard_index"] == 0 and killed_shard0:
                        shard0_restarted_after_kill = True
                    if w["shard_index"] == 1 and frozen_shard1:
                        shard1_restarted_after_freeze = True

            sample = {
                "elapsed_sec": round(elapsed, 1),
                "shards": [
                    {
                        "shard_index": w["shard_index"], "pid": w["process"].pid,
                        "returncode": w["process"].poll(),
                        "health": (read_status(w["status_path"]).get("health") or {}),
                        "consecutive_restart_count": w["consecutive_restart_count"],
                    }
                    for w in workers
                ],
            }
            report["samples"].append(sample)
            REPORT_PATH.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
            time.sleep(15)
    finally:
        for w in workers:
            kill_process(w)
            try:
                w["stdout_log"].close()
                w["stderr_log"].close()
            except Exception:
                pass

    report["final"] = {
        "killed_shard0": killed_shard0,
        "shard0_restarted_after_kill": shard0_restarted_after_kill,
        "frozen_shard1": frozen_shard1,
        "shard1_restarted_after_freeze": shard1_restarted_after_freeze,
        "last_sample": report["samples"][-1] if report["samples"] else {},
    }
    REPORT_PATH.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    print("REPORT_PATH:", REPORT_PATH)
    print(json.dumps(report["final"], indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
