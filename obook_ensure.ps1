# 호가 기록기 **보장** 스크립트 — 작업 스케줄러가 5분마다 호출한다.
#
# [2026-09-02] 무한루프 워치독(start_obook_watchdog.ps1)이 두 번 실패했다.
#   1차: 기록기가 죽고 워치독도 없어 1.9시간 유실
#   2차: 워치독 프로세스는 살아 있었는데 루프가 멈춰(절전 추정) 27시간 유실
# 원인은 같다 — **감시자 자신이 오래 살아야 하는 구조**다. 그래서 뒤집는다.
# 이 스크립트는 한 번 확인하고 즉시 끝난다. 오래 사는 주체는 작업 스케줄러이고,
# 스케줄러는 절전·재부팅·로그오프를 견딘다.
#
# 하는 일: 기록기가 없거나 하트비트가 정체면 띄운다. 그게 전부다.
$ErrorActionPreference = 'SilentlyContinue'
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root

$log = Join-Path $root 'logs\obook_ensure.log'
$hb = Join-Path $root 'logs\obook\heartbeat.txt'
$stallSec = 1800   # --status-sec(600) x 3. 이보다 짧으면 건강한 기록기를 죽인다.

function Write-Log($m) {
  Add-Content -Path $log -Value ("{0} {1}" -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $m) -Encoding utf8
}

$rec = @(Get-CimInstance Win32_Process -Filter "Name LIKE '%python%'" |
         Where-Object { $_.CommandLine -like '*obook_recorder*' })
# 자식 프로세스를 빼고 '뿌리'만 센다(기록기는 자식 파이썬을 하나 띄운다).
$ids = @($rec | ForEach-Object { $_.ProcessId })
$roots = @($rec | Where-Object { $ids -notcontains $_.ParentProcessId })

# 중복 정리: 뿌리가 2개 넘으면 최신 것만 남긴다(CSV 중복 기록 방지).
if ($roots.Count -gt 1) {
  $keep = ($roots | Sort-Object CreationDate -Descending)[0]
  foreach ($p in $roots) {
    if ($p.ProcessId -ne $keep.ProcessId) {
      Stop-Process -Id $p.ProcessId -Force
      Write-Log "중복 기록기 종료 pid=$($p.ProcessId)"
    }
  }
  $roots = @($keep)
}

$need = $false
$why = ''
if ($roots.Count -eq 0) {
  $need = $true
  $why = '프로세스 없음'
} elseif (Test-Path $hb) {
  try {
    $hbAt = [int64]((Get-Content $hb -TotalCount 1).Split(' ')[0])
    $age = [DateTimeOffset]::UtcNow.ToUnixTimeSeconds() - $hbAt
    if ($age -gt $stallSec) {
      $need = $true
      $why = "하트비트 정체 ${age}초"
      foreach ($p in $roots) { Stop-Process -Id $p.ProcessId -Force }
    }
  } catch { }
}

if (-not $need) { exit 0 }

$stamp = Get-Date -Format 'yyyyMMdd_HHmmss'
$out = Join-Path $root "logs\obook_run_$stamp.out"
$err = Join-Path $root "logs\obook_run_$stamp.err"
$argv = @('scripts\obook_recorder.py', '--flush', '5', '--depth',
          '--streams-per-conn', '45', '--status-sec', '600')
$p = Start-Process -FilePath (Join-Path $root '.venv\Scripts\python.exe') `
     -ArgumentList $argv -WindowStyle Hidden -PassThru `
     -RedirectStandardOutput $out -RedirectStandardError $err
Write-Log "기록기 기동 pid=$($p.Id) 사유=$why out=$(Split-Path $out -Leaf)"

# 오래된 run 로그 정리(30개 초과분).
Get-ChildItem (Join-Path $root 'logs\obook_run_*.out') |
  Sort-Object LastWriteTime -Descending | Select-Object -Skip 30 |
  ForEach-Object {
    Remove-Item $_.FullName -Force
    Remove-Item ($_.FullName -replace '\.out$', '.err') -Force
  }
