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
import ctypes
import datetime
import subprocess
import sys
import time
from pathlib import Path

LOG_DIR = Path(__file__).resolve().parent / "logs"
LOG_DIR.mkdir(exist_ok=True)
LOG_FILE = LOG_DIR / "supervisor.log"
HEARTBEAT_PATH = LOG_DIR / "heartbeat.txt"  # bot/main.py의 HEARTBEAT_PATH와 반드시 동일해야 함

# [2026-08-12 사용자요청] "RAM 문제 등으로 멈추면 새로 시작 + RAM 수급까지 자동으로" —
# 이 PC는 4GB RAM 중 여유분이 0.1~0.6GB까지 떨어지는 일이 실측됐고(사용자가 직접 확인),
# 그 상태에서 pandas 등 무거운 프로세스가 뜨면 MemoryError로 봇이 죽을 위험이 있다.
# 재시작만으론 부족하다 — 같은 RAM 부족 상태로 재시작해봐야 다시 죽을 뿐이므로, 재시작
# 전후로 여유 RAM을 확보한다. Windows 전용(GlobalMemoryStatusEx), 이 프로젝트가 이
# 환경에서만 운영되므로 별도 의존성(psutil 등) 추가 없이 ctypes로 직접 조회한다.
LOW_RAM_MB_THRESHOLD = 500
RAM_CHECK_INTERVAL_SEC = 60
# [매우 중요] 여기 나열된 것만 자동 종료한다 — 실거래/모니터링 관련 프로세스(bot.main,
# ws_worker, dashboard/server.py 등)는 절대 포함시키면 안 된다. ChatGPT.exe는 사용자가
# 이 세션 중 두 차례 직접 승인해 종료한 전례가 있는, 매매와 무관한 비필수 앱이라 자동
# 종료 후보로 확정했다. 새 항목을 추가할 땐 반드시 매매/모니터링과 무관함을 먼저 확인할 것.
KNOWN_SAFE_TO_CLOSE = ["ChatGPT.exe"]


class _MemoryStatusEx(ctypes.Structure):
    _fields_ = [
        ("dwLength", ctypes.c_ulong),
        ("dwMemoryLoad", ctypes.c_ulong),
        ("ullTotalPhys", ctypes.c_ulonglong),
        ("ullAvailPhys", ctypes.c_ulonglong),
        ("ullTotalPageFile", ctypes.c_ulonglong),
        ("ullAvailPageFile", ctypes.c_ulonglong),
        ("ullTotalVirtual", ctypes.c_ulonglong),
        ("ullAvailVirtual", ctypes.c_ulonglong),
        ("sullAvailExtendedVirtual", ctypes.c_ulonglong),
    ]


def get_available_ram_mb() -> float | None:
    """Windows GlobalMemoryStatusEx로 현재 가용 물리메모리(MB)를 조회한다. 실패하면 None
    (RAM 관리 기능을 비활성화한 것과 동일하게 동작 — 감시 자체를 막지 않기 위함)."""
    try:
        stat = _MemoryStatusEx()
        stat.dwLength = ctypes.sizeof(_MemoryStatusEx)
        if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat)):
            return None
        return stat.ullAvailPhys / (1024 * 1024)
    except Exception:
        return None


def free_ram_if_low() -> None:
    """가용 RAM이 LOW_RAM_MB_THRESHOLD 밑이면 KNOWN_SAFE_TO_CLOSE 목록의 프로세스만
    종료를 시도한다. 실거래/대시보드 프로세스는 이 목록에 없으므로 절대 건드리지 않는다."""
    avail = get_available_ram_mb()
    if avail is None or avail >= LOW_RAM_MB_THRESHOLD:
        return
    log(f"가용 RAM {avail:.0f}MB로 부족(기준 {LOW_RAM_MB_THRESHOLD}MB) — 비필수 프로세스 정리 시도: {KNOWN_SAFE_TO_CLOSE}")
    for name in KNOWN_SAFE_TO_CLOSE:
        try:
            result = subprocess.run(
                ["taskkill", "/IM", name, "/F"],
                capture_output=True, timeout=10, text=True,
            )
            if result.returncode == 0:
                log(f"{name} 종료 완료")
        except Exception as exc:
            log(f"{name} 종료 시도 중 오류(무시하고 계속): {exc}")

RESTART_DELAY_SEC = 5
# bot.main이 "이미 다른 인스턴스가 실행 중"이라 스스로 물러난 경우의 종료코드(bot/main.py의
# DUPLICATE_INSTANCE_EXIT_CODE와 반드시 같은 값이어야 함). 이 경우 5초마다 재시도하면 두
# 인스턴스가 서로 물고 무한 재시작 루프에 빠질 수 있어(2026-08-09 실측 사고), 훨씬 길게 기다린다.
DUPLICATE_INSTANCE_EXIT_CODE = 78
DUPLICATE_INSTANCE_RETRY_DELAY_SEC = 120

# [2026-08-13 실측 사고] bot.main() 시작부(get_active_usdt_perpetual_symbols 등)의 REST 호출은
# 예외처리가 없어, 바이낸스 IP밴(-1003) 같은 일시적 장애 중엔 기동하자마자 미처리 예외로 즉시
# 죽는다. RESTART_DELAY_SEC=5초가 너무 짧아서 "기동->즉시크래시->5초 후 재기동->또 REST
# 호출->밴 연장" 죽음의 루프가 됐고, 이게 IP밴 해제 시각을 계속 뒤로 밀어 20분 넘게 매매가
# 끊긴 사고로 이어졌다(원인 조회 자체도 REST라 루프를 완전히 끄기 전엔 확인도 못 함).
# 특정 REST 호출 하나만 예외처리하는 것보다 "짧게 산 재시작이 반복되면 대기시간을 지수적으로
# 늘리는" 게 원인 불문 범용 방어라 이 방식을 택함. 정상 기동해서 오래 살아남으면(문턱 이상)
# 바로 원래 대기시간으로 복귀한다.
FAST_CRASH_THRESHOLD_MIN = 1.0  # 이보다 짧게 살고 죽으면 "급속 크래시"로 간주
FAST_CRASH_BACKOFF_SEC = [5, 30, 60, 120, 300]  # 연속 급속크래시 횟수에 따라 순서대로 적용(마지막 값에서 고정)

# [2026-08-10] 하트비트가 이만큼(초) 안 갱신되면 "멈췄다"고 판단해 강제종료한다. bot.main
# 시작 직후(250심볼 스캔 등)에도 하트비트를 바로 남기므로 여유는 크게 안 둬도 되지만,
# 일시적 GC/네트워크 지연 등으로 오탐하지 않도록 충분히 넉넉하게(5분) 잡는다.
HEARTBEAT_STALE_SEC = 300
HEARTBEAT_CHECK_INTERVAL_SEC = 15


def log(message: str):
    ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"{ts} [supervisor] {message}"
    try:
        print(line)
    except UnicodeEncodeError:
        # [2026-08-12] 콘솔 코드페이지(cp949 등)가 em-dash(—) 같은 일부 유니코드 문자를
        # 인코딩 못 해 print() 자체가 죽는 걸 실측했다 — 감시 스크립트가 로그 출력 때문에
        # 죽으면 안 되므로, 콘솔 출력만 깨진 문자를 무시하고 계속 진행한다(파일 로그는
        # 항상 UTF-8이라 원본 내용이 그대로 보존됨).
        print(line.encode(sys.stdout.encoding or "utf-8", errors="replace").decode(sys.stdout.encoding or "utf-8", errors="replace"))
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
    free_ram_if_low()  # 재시작 직후 다시 RAM 부족으로 죽는 걸 막기 위해 기동 전에 한 번 확보
    process_started_at = time.time()
    process = subprocess.Popen([sys.executable, "-m", "bot.main"])
    last_ram_check_at = 0.0
    while True:
        returncode = process.poll()
        if returncode is not None:
            return returncode

        if time.time() - last_ram_check_at >= RAM_CHECK_INTERVAL_SEC:
            last_ram_check_at = time.time()
            free_ram_if_low()

        age = heartbeat_age_sec(process_started_at=process_started_at)
        # [2026-08-13] 사용자가 자는 동안 이 판정이 여러 차례(47분/51분/230분 실행 후) 오탐으로
        # 확인됨 — bot.log엔 킬 직전까지 30~40초 간격으로 정상 스캔 로그가 계속 찍혀있어 실제로
        # 멈춘 게 아니었다. 정확한 원인(heartbeat.txt 읽기 실패? 다른 값?)을 다음 발생 시 바로
        # 알 수 있도록, 문턱의 60% 지점부터 원본 값을 남긴다.
        if age is not None and age > HEARTBEAT_STALE_SEC * 0.6:
            log(f"[진단] heartbeat_age={age:.0f}초 heartbeat_raw={heartbeat_timestamp()} process_started_at={process_started_at} now={time.time()}")
        if age is not None and age > HEARTBEAT_STALE_SEC:
            log(
                f"하트비트가 {age:.0f}초 동안 갱신되지 않음 — bot.main이 응답불능 상태로 판단해 강제종료합니다 "
                f"(pid={process.pid})"
            )
            # [2026-08-13] process.kill()은 이 PID 하나만 죽이고 그 자식(bot.ws_worker 3개)은
            # 정리 안 돼 고아로 남는 사고가 실측됨(밤새 여러 세대의 ws_worker 고아 축적, RAM
            # 소모 지속). taskkill /T로 프로세스 트리 전체를 종료해 고아가 안 남게 한다.
            try:
                subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"], capture_output=True, timeout=15)
            except Exception:
                log("taskkill 실행 중 오류 — process.kill()로 폴백")
                process.kill()
            try:
                process.wait(timeout=10)
            except Exception:
                log("강제종료 후에도 프로세스가 안 끝남 — 그래도 재시작을 진행합니다")
            return process.returncode if process.returncode is not None else -9

        time.sleep(HEARTBEAT_CHECK_INTERVAL_SEC)


def compute_fast_crash_backoff_sec(streak: int) -> int:
    """연속 급속크래시 횟수(streak, 이번 크래시 포함 전 카운트)에 따른 대기시간(초)을 반환한다.
    FAST_CRASH_BACKOFF_SEC 테이블을 순서대로 쓰고, 범위를 넘으면 마지막 값에 고정된다."""
    idx = min(max(streak, 0), len(FAST_CRASH_BACKOFF_SEC) - 1)
    return FAST_CRASH_BACKOFF_SEC[idx]


def main():
    log("=== 감시 스크립트 시작 ===")
    fast_crash_streak = 0  # 연속으로 짧게 살고 죽은 횟수(지수 백오프 계산용)
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
                fast_crash_streak = 0  # 중복인스턴스는 별개 사유라 백오프 카운트에 안 넣음
                time.sleep(DUPLICATE_INSTANCE_RETRY_DELAY_SEC)
            elif elapsed_min < FAST_CRASH_THRESHOLD_MIN:
                delay = compute_fast_crash_backoff_sec(fast_crash_streak)
                fast_crash_streak += 1
                log(
                    f"bot.main이 {elapsed_min:.1f}분만에 종료됨(exit code={returncode}) — 급속 크래시 "
                    f"{fast_crash_streak}회 연속. IP밴 등 일시적 장애가 재시작으로 계속 악화되는 걸 막기 "
                    f"위해 {delay}초 대기 후 재시작합니다."
                )
                time.sleep(delay)
            else:
                fast_crash_streak = 0
                log(f"bot.main 종료됨 (exit code={returncode}, {elapsed_min:.1f}분간 실행됨). {RESTART_DELAY_SEC}초 후 재시작합니다.")
                time.sleep(RESTART_DELAY_SEC)
    except KeyboardInterrupt:
        log("사용자가 Ctrl+C로 감시 스크립트를 중지했습니다.")


if __name__ == "__main__":
    main()
