"""bot.main이 예상치 못한 오류로 죽으면 자동으로 재시작하는 감시 스크립트.

사용법: python run_forever.py
(Ctrl+C를 누르면 감시 스크립트와 봇 모두 종료된다.)

[2026-08-10] bot.main을 subprocess.run()으로 띄워 "끝날 때까지 무한정 기다리기만" 했는데,
python-binance 라이브러리 버그로 bot.main 프로세스 자체가 CPU를 붙잡고 도는 tight loop에
빠져 응답불능이 되는 사고가 실거래에서 실측됐다(터미널 입력/Ctrl+C도 안 먹힘) — 이 상태에서는
subprocess.run()이 영원히 리턴 안 되므로 감시 스크립트도 사실상 무력해진다.
그래서 subprocess.Popen()으로 바꾸고, bot.main이 남기는 하트비트 파일(logs/heartbeat.txt)을
주기적으로 확인하다가 너무 오래(HEARTBEAT_STALE_SEC) 안 갱신되면 "멈췄다"고 판단해
process.kill()로 강제종료한다 — OS 레벨 강제종료는 대상 프로세스가 내부에서 뭘 하고 있든
(GIL을 붙잡은 tight loop라도) 항상 통한다."""
import datetime
import subprocess
import sys
import time
from pathlib import Path

LOG_DIR = Path(__file__).resolve().parent / "logs"
LOG_DIR.mkdir(exist_ok=True)
LOG_FILE = LOG_DIR / "supervisor.log"
HEARTBEAT_PATH = LOG_DIR / "heartbeat.txt"  # bot/main.py의 HEARTBEAT_PATH와 반드시 동일해야 함

RESTART_DELAY_SEC = 5
# bot.main이 "이미 다른 인스턴스가 실행 중"이라 스스로 물러난 경우의 종료코드(bot/main.py의
# DUPLICATE_INSTANCE_EXIT_CODE와 반드시 같은 값이어야 함). 이 경우 5초마다 재시도하면 두
# 인스턴스가 서로 물고 무한 재시작 루프에 빠질 수 있어(2026-08-09 실측 사고), 훨씬 길게 기다린다.
DUPLICATE_INSTANCE_EXIT_CODE = 78
DUPLICATE_INSTANCE_RETRY_DELAY_SEC = 120

# [2026-08-10] 하트비트가 이만큼(초) 안 갱신되면 "멈췄다"고 판단해 강제종료한다. bot.main
# 시작 직후(250심볼 스캔 등)에도 하트비트를 바로 남기므로 여유는 크게 안 둬도 되지만,
# 일시적 GC/네트워크 지연 등으로 오탐하지 않도록 충분히 넉넉하게(5분) 잡는다.
HEARTBEAT_STALE_SEC = 300
HEARTBEAT_CHECK_INTERVAL_SEC = 15


def log(message: str):
    ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"{ts} [supervisor] {message}"
    print(line)
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def heartbeat_timestamp() -> float | None:
    """하트비트 파일에 기록된 epoch 시각을 반환한다."""
    try:
        return float(HEARTBEAT_PATH.read_text(encoding="utf-8").strip())
    except Exception:
        return None


def heartbeat_age_sec(*, process_started_at: float | None = None, now: float | None = None) -> float | None:
    """하트비트 파일의 "마지막 기록 시각으로부터 지금까지 경과 시간"을 반환한다.
    새 bot.main을 감시할 때는 이전 프로세스가 남긴 오래된 하트비트 때문에 새 프로세스를
    즉시 죽이지 않도록 프로세스 시작 시각을 최소 기준으로 사용한다."""
    heartbeat_at = heartbeat_timestamp()
    if heartbeat_at is None and process_started_at is None:
        return None
    freshest_at = max(ts for ts in (heartbeat_at, process_started_at) if ts is not None)
    return max(0.0, (time.time() if now is None else now) - freshest_at)


def run_and_watch() -> int:
    """bot.main을 기동하고, 끝나거나 하트비트가 멈출 때까지 감시한다. 반환값은 프로세스의
    종료코드(강제종료한 경우 특수한 음수값 대신 -9를 그대로 씀 — Popen이 kill 후 wait하면
    보통 -9(SIGKILL) 또는 플랫폼에 따라 다른 값이 나오는데, 우리는 아래 main()에서 이 반환값을
    DUPLICATE_INSTANCE_EXIT_CODE와만 비교하므로 정확한 시그널 번호는 중요치 않다)."""
    process_started_at = time.time()
    process = subprocess.Popen([sys.executable, "-m", "bot.main"])
    while True:
        returncode = process.poll()
        if returncode is not None:
            return returncode

        age = heartbeat_age_sec(process_started_at=process_started_at)
        if age is not None and age > HEARTBEAT_STALE_SEC:
            log(
                f"하트비트가 {age:.0f}초 동안 갱신되지 않음 — bot.main이 응답불능 상태로 판단해 강제종료합니다 "
                f"(pid={process.pid})"
            )
            process.kill()
            try:
                process.wait(timeout=10)
            except Exception:
                log("강제종료 후에도 프로세스가 안 끝남 — 그래도 재시작을 진행합니다")
            return process.returncode if process.returncode is not None else -9

        time.sleep(HEARTBEAT_CHECK_INTERVAL_SEC)


def main():
    log("=== 감시 스크립트 시작 ===")
    try:
        while True:
            log("bot.main 실행")
            start = time.time()
            returncode = run_and_watch()
            elapsed_min = (time.time() - start) / 60
            if returncode == DUPLICATE_INSTANCE_EXIT_CODE:
                log(
                    f"bot.main이 중복 인스턴스 감지로 종료됨 ({elapsed_min:.1f}분간 실행됨). "
                    f"다른 인스턴스와 재시작 경합을 피하기 위해 {DUPLICATE_INSTANCE_RETRY_DELAY_SEC}초 후 재시도합니다."
                )
                time.sleep(DUPLICATE_INSTANCE_RETRY_DELAY_SEC)
            else:
                log(f"bot.main 종료됨 (exit code={returncode}, {elapsed_min:.1f}분간 실행됨). {RESTART_DELAY_SEC}초 후 재시작합니다.")
                time.sleep(RESTART_DELAY_SEC)
    except KeyboardInterrupt:
        log("사용자가 Ctrl+C로 감시 스크립트를 중지했습니다.")


if __name__ == "__main__":
    main()
