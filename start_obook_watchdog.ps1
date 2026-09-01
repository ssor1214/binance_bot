# 호가·주문흐름 기록기 **워치독**.
#
# [2026-09-01] 기록기가 8.7시간 만에 조용히 죽어 있었다. stderr 0바이트,
# stdout 이 정상 사이클 중간에 끊김, 종료 메시지 없음 -> 예외가 아니라 강제 종료다.
# start_obook.ps1 은 Start-Process 한 번뿐이라 죽으면 아무도 되살리지 않는다.
# CLAUDE.md 는 이 데이터를 2~3개월 쌓아야 한다고 적고 있는데, 그 사이 한 번만
# 죽어도 나머지가 통째로 빈다. 그래서 워치독을 둔다.
#
# 하는 일:
#   1. 기록기를 띄우고 죽으면 5초 뒤 되살린다.
#   2. 프로세스는 살아 있는데 **하트비트가 멎은 경우**(WS 는 붙어 있으나 수신 정지)도
#      정체로 보고 죽여서 되살린다. 실측 재접속은 기록기 내부에서 처리되므로
#      여기 임계는 넉넉히 300초로 둔다.
#   3. 기동/사망을 logs\obook_watchdog.log 에 남긴다 — 다음에 또 죽으면 원인 추적용.
#
# 기록기 자체는 공개 WS 전용 / API 키 미사용 / 주문 기능 없음이라 라이브 봇과 간섭하지
# 않는다. CSV 는 append 모드라 재시작해도 기존 데이터를 덮어쓰지 않는다.
Set-Location $PSScriptRoot

$log = Join-Path $PSScriptRoot "logs\obook_watchdog.log"
$hb = Join-Path $PSScriptRoot "logs\obook\heartbeat.txt"
# [버그수정] 하트비트는 --status-sec(600초)마다만 쓰인다. 임계를 그보다 짧게 두면
# **건강한 기록기를 10분마다 죽인다.** 상태주기 3회분으로 넉넉히 잡는다.
$stallSec = 1800

$argv = @(
  'scripts\obook_recorder.py',
  '--flush', '5',
  '--depth',
  '--streams-per-conn', '45',
  '--status-sec', '600'
)

function Write-Log($msg) {
  $t = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
  Add-Content -Path $log -Value "$t $msg" -Encoding utf8
}

Write-Log "워치독 기동 (정체 임계 ${stallSec}초 = 상태주기 600초 x 3)"

while ($true) {
  $stamp = Get-Date -Format "yyyyMMdd_HHmmss"
  $out = Join-Path $PSScriptRoot "logs\obook_run_$stamp.out"
  $err = Join-Path $PSScriptRoot "logs\obook_run_$stamp.err"

  $p = Start-Process -FilePath ".venv\Scripts\python.exe" -ArgumentList $argv `
    -WindowStyle Hidden -PassThru -RedirectStandardOutput $out -RedirectStandardError $err
  # [버그수정] 기동 시각을 잡아 둔다. heartbeat.txt 는 **이전 실행이 남긴 낡은 값**이라
  # 이걸 안 보면 갓 띄운 기록기를 곧바로 정체로 오판해 죽인다(실측 무한 재기동).
  $startedAt = [DateTimeOffset]::UtcNow.ToUnixTimeSeconds()
  Write-Log "기록기 기동 pid=$($p.Id) out=$(Split-Path $out -Leaf)"

  $killedForStall = $false
  while (-not $p.HasExited) {
    Start-Sleep -Seconds 30
    if ($p.HasExited) { break }
    # 하트비트 정체 감시. 파일이 아직 없을 수 있으므로(기동 직후) 없으면 넘어간다.
    if (Test-Path $hb) {
      try {
        $now = [DateTimeOffset]::UtcNow.ToUnixTimeSeconds()
        $first = (Get-Content $hb -TotalCount 1).Split(' ')[0]
        $hbAt = [int64]$first
        # 하트비트가 이번 기동보다 오래됐으면 '아직 안 쓴 것'이다. 그 경우는 기동 이후
        # 경과로 재고, 그렇지 않으면 하트비트 이후 경과로 잰다.
        if ($hbAt -lt $startedAt) { $age = $now - $startedAt } else { $age = $now - $hbAt }
        if ($age -gt $stallSec) {
          Write-Log "하트비트 정체 ${age}초 -> pid=$($p.Id) 강제 종료 후 재기동"
          Stop-Process -Id $p.Id -Force -ErrorAction SilentlyContinue
          $killedForStall = $true
          break
        }
      } catch {
        # 하트비트를 쓰는 중이면 파싱이 실패할 수 있다. 다음 주기에 다시 본다.
      }
    }
  }

  if (-not $killedForStall) {
    $p.Refresh()
    $code = "?"
    try { $code = $p.ExitCode } catch { }
    Write-Log "기록기 사망 pid=$($p.Id) exit=$code"
  }
  Start-Sleep -Seconds 5
}
