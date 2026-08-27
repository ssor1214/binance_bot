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
import os
from pathlib import Path

LOG_DIR = Path(__file__).resolve().parent / "logs"
LOG_DIR.mkdir(exist_ok=True)
LOG_FILE = LOG_DIR / "supervisor.log"
HEARTBEAT_PATH = LOG_DIR / "heartbeat.txt"  # bot/main.py의 HEARTBEAT_PATH와 반드시 동일해야 함
SUPERVISOR_LOCK_PATH = LOG_DIR.parent / ".run_forever.lock"
_SUPERVISOR_MUTEX = None


def has_same_supervisor_parent() -> bool:
    """Reject a second wrapper when an identical wrapper already owns this process.

    [2026-08-25 버그수정] 원래는 부모의 CommandLine에 "run_forever.py"와 "binance-futures-bot"이
    들어 있으면 무조건 중첩으로 판정했다. 그런데 사람이 수동으로 띄울 때 쓰는 런처(PowerShell
    Start-Process, bash `cd <repo> && python run_forever.py` 등)의 명령줄에도 그 두 문자열이
    그대로 들어간다. 그래서 **정상적인 수동 기동이 전부 "중첩 감시기"로 오판돼 즉시 종료**됐다
    (실측: 17:04~17:06 세 번의 기동 시도가 모두 이 경로로 죽었고, 봇이 supervisor 없이
    맨몸 bot.main으로만 돌던 원인이기도 하다).
    진짜 중첩은 multiprocessing spawn이 run_forever.py를 재실행하는 경우이고, 그때 부모는
    반드시 python 프로세스다. 그래서 부모의 실행 파일명이 python인지까지 확인한다.
    """
    try:
        import subprocess
        # [2026-08-25 버그수정 2] os.getppid()를 쓰면 안 된다. Git Bash/MSYS에서 실행하면
        # 윈도우 PID가 아니라 MSYS PID를 돌려주고, 그 값이 이미 재사용된 다른 프로세스와
        # 겹치면 엉뚱한 프로세스를 "내 부모"로 오인한다(실측: getppid()=7428이 이미 죽고
        # 재사용된 PID였고, 그 자리에 있던 python.exe 때문에 중첩으로 오판됐다).
        # 자기 PID(os.getpid())는 항상 정확하므로, WMI로 자기 레코드를 먼저 찾고 거기서
        # ParentProcessId를 읽는다.
        own_pid = os.getpid()
        out = subprocess.check_output(
            [
                "powershell", "-NoProfile", "-Command",
                f"$me=Get-CimInstance Win32_Process -Filter 'ProcessId={own_pid}';"
                "if($me){$p=Get-CimInstance Win32_Process -Filter \"ProcessId=$($me.ParentProcessId)\";"
                "if($p){ $p.Name + '|' + $p.CommandLine }}",
            ],
            text=True, stderr=subprocess.DEVNULL, timeout=8,
        )
        name, _, cmdline = out.strip().partition("|")
        if not name.lower().startswith("python"):
            return False  # 셸/런처가 부모면 중첩이 아니라 정상적인 수동 기동이다
        return "run_forever.py" in cmdline and "binance-futures-bot" in cmdline
    except Exception:
        return False


# [2026-08-25] 커널 뮤텍스 이름을 상수로 뺀다. 테스트가 전역 이름을 그대로 쓰면
# "라이브 봇이 돌고 있을 때만 실패하는" 테스트가 되어버린다(실제로 그렇게 깨졌다).
SUPERVISOR_MUTEX_NAME = r"Global\BinanceFuturesBotSupervisor"


def acquire_named_mutex(name: str) -> bool:
    """Use a kernel mutex because msvcrt file locks are unreliable across launches."""
    global _SUPERVISOR_MUTEX
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateMutexW.restype = ctypes.c_void_p
    kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p]
    handle = kernel32.CreateMutexW(None, False, name)
    if not handle:
        return False
    if ctypes.get_last_error() == 183:  # ERROR_ALREADY_EXISTS
        kernel32.CloseHandle(handle)
        return False
    _SUPERVISOR_MUTEX = handle
    return True

# [2026-08-12 사용자요청] "RAM 문제 등으로 멈추면 새로 시작 + RAM 수급까지 자동으로" —
# 이 PC는 4GB RAM 중 여유분이 0.1~0.6GB까지 떨어지는 일이 실측됐고(사용자가 직접 확인),
# 그 상태에서 pandas 등 무거운 프로세스가 뜨면 MemoryError로 봇이 죽을 위험이 있다.
# 재시작만으론 부족하다 — 같은 RAM 부족 상태로 재시작해봐야 다시 죽을 뿐이므로, 재시작
# 전후로 여유 RAM을 확보한다. Windows 전용(GlobalMemoryStatusEx), 이 프로젝트가 이
# 환경에서만 운영되므로 별도 의존성(psutil 등) 추가 없이 ctypes로 직접 조회한다.
LOW_RAM_MB_THRESHOLD = 500
RAM_CHECK_INTERVAL_SEC = 60
# [매우 중요] 여기 나열된 것만 자동 종료한다 — 실거래/모니터링 관련 프로세스(bot.main,
# ws_worker, dashboard/server.py 등)는 절대 포함시키면 안 된다.
# [2026-08-16 수정] Codex/ChatGPT 데스크톱 앱이 실제 작업 채널이므로 자동 종료 대상에서
# 완전히 제외한다. 메모리 확보가 필요하더라도 사용자의 작업 앱을 죽이면 운영/대응 자체가
# 끊겨 버리므로, 기본값은 "자동 종료 대상 없음"으로 둔다.
KNOWN_SAFE_TO_CLOSE: list[str] = []


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
    if not KNOWN_SAFE_TO_CLOSE:
        log(
            f"가용 RAM {avail:.0f}MB로 부족(기준 {LOW_RAM_MB_THRESHOLD}MB) — "
            "자동 종료 대상이 비어 있어 어떤 앱도 강제 종료하지 않습니다."
        )
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


def acquire_supervisor_lock():
    """중복 run_forever 인스턴스를 막는다.

    bot.main 락만으로는 실주문 중복은 막을 수 있어도, 중복 supervisor가 계속 bot.main을
    재기동 경쟁하면서 ws_worker 고아/로그 오염/재시작 경합을 만들 수 있다."""
    if not acquire_named_mutex(SUPERVISOR_MUTEX_NAME):
        log("이미 다른 run_forever 인스턴스가 실행 중입니다. named mutex로 중복 감시기를 차단합니다.")
        raise SystemExit(DUPLICATE_INSTANCE_EXIT_CODE)

    import msvcrt

    lock_file = open(SUPERVISOR_LOCK_PATH, "w")
    try:
        msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError:
        lock_file.close()
        log("이미 다른 run_forever 인스턴스가 실행 중입니다. 중복 감시기를 방지하기 위해 종료합니다.")
        raise SystemExit(DUPLICATE_INSTANCE_EXIT_CODE)
    return lock_file


def heartbeat_timestamp() -> float | None:
    """하트비트 파일에 기록된 epoch 시각을 반환한다."""
    try:
        return float(HEARTBEAT_PATH.read_text(encoding="utf-8").strip())
    except Exception:
        return None


def heartbeat_age_sec(
    *, process_started_at: float | None = None, now: float | None = None, last_known_at: float | None = None
) -> float | None:
    """하트비트 파일의 "마지막 기록 시각으로부터 지금까지 경과 시간"을 반환한다.
    새 bot.main을 감시할 때는 이전 프로세스가 남긴 오래된 하트비트 때문에 새 프로세스를
    즉시 죽이지 않도록 프로세스 시작 시각을 최소 기준으로 사용한다.

    [2026-08-15 실측 오탐 원인 확정] 325분 정상 운행 후 "heartbeat_age=19514초(=프로세스
    가동시간과 정확히 일치)"로 오탐 발생 — 진단로그의 heartbeat_raw는 거의 동시각(now와
    0.05초 차이)이었다. 즉 실제로는 멈추지 않았고, HEARTBEAT_PATH.write_text()가 파일을
    truncate 후 쓰는 게 원자적이지 않아, 감시 루프가 하필 그 찰나에 읽어서 빈 문자열/파싱
    실패(heartbeat_timestamp()가 None 반환)를 만난 것. 기존 코드는 heartbeat_at이 None이면
    process_started_at까지 확 되돌아가버려서(0분 전 상태 취급), 5시간 넘게 잘 돌던 프로세스도
    "방금 시작한 것"처럼 나이가 계산돼 결과적으로 그 순간의 실제 now와의 차이가 고스란히
    "정지시간"으로 둔갑했다. last_known_at(호출자가 마지막으로 성공적으로 읽은 하트비트 값을
    계속 들고 있다가 넘겨줌)을 process_started_at보다 우선시켜, 이런 일시적 읽기 실패 한 번으로
    오탐이 나지 않게 한다 — 진짜로 하트비트가 멈췄다면 last_known_at도 결국 똑같이 오래된 값이라
    정상적으로 감지된다."""
    heartbeat_at = heartbeat_timestamp()
    freshest_at = max(
        (ts for ts in (heartbeat_at, last_known_at, process_started_at) if ts is not None),
        default=None,
    )
    if freshest_at is None:
        return None
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
    last_known_heartbeat_at = None  # [2026-08-15] 마지막으로 성공적으로 읽힌 하트비트값 — 아래 참고
    while True:
        returncode = process.poll()
        if returncode is not None:
            return returncode

        if time.time() - last_ram_check_at >= RAM_CHECK_INTERVAL_SEC:
            last_ram_check_at = time.time()
            free_ram_if_low()

        # [2026-08-15] heartbeat.txt write_text()가 원자적이지 않아 감시 루프가 쓰기 도중
        # 읽으면 일시적으로 파싱 실패(None)할 수 있다 — 그 순간 process_started_at까지
        # 되돌아가 몇 시간짜리 오탐 강제종료를 낸 사고가 실측됨. 마지막으로 성공한 값을
        # 계속 들고 있다가 heartbeat_age_sec에 최소 기준으로 넘겨 이런 찰나의 읽기 실패를
        # 흡수한다(진짜 정지라면 이 값도 결국 똑같이 오래돼서 정상 감지됨).
        raw_heartbeat_at = heartbeat_timestamp()
        if raw_heartbeat_at is not None:
            last_known_heartbeat_at = raw_heartbeat_at

        age = heartbeat_age_sec(process_started_at=process_started_at, last_known_at=last_known_heartbeat_at)
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
    # [2026-08-25 장애수정] 중복 감시기 차단은 named mutex(acquire_supervisor_lock 안)만으로
    # 판정한다. 부모 프로세스 검사는 보조 신호로만 남긴다.
    # 이유: 이 검사가 오탐으로 정상 기동을 계속 죽였다(17:04~17:12 사이 6번 연속 기동 실패,
    #       그 시간 동안 봇이 완전히 내려가 있었다). 원인은 두 겹이었다 —
    #       (1) 사람이 쓰는 런처(PowerShell Start-Process / bash `cd <repo> && python ...`)의
    #           명령줄에도 "run_forever.py"와 "binance-futures-bot"이 그대로 들어간다.
    #       (2) Git Bash/MSYS에서는 os.getppid()가 윈도우 PID가 아니라 MSYS PID를 돌려주고,
    #           그 값이 재사용된 PID와 겹치면 엉뚱한 프로세스를 부모로 오인한다.
    #       커널 mutex는 이런 오탐이 구조적으로 없고 이미 중복을 정확히 막고 있으므로,
    #       "봇이 안 뜨는" 실패 모드를 만드는 이 검사에 기동 권한을 주지 않는다.
    if has_same_supervisor_parent():
        log("경고: 부모가 동일 저장소 run_forever로 보입니다. 중복 여부는 named mutex로 판정합니다.")
    _lock = acquire_supervisor_lock()
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
