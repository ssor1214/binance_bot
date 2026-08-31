# 호가·주문흐름 기록기 런처. 인자를 한 곳에서만 관리한다.
# 공개 WS 만 쓰고 API 키를 쓰지 않으며 주문 기능이 없다 — 라이브 봇과 무관하게 상주시킨다.
# 목적: HANDOFF_2026-08-31 10장의 유일한 미탐색 축(호가/주문흐름) 데이터를 지금부터 쌓는다.
Set-Location $PSScriptRoot
$argv = @(
  'scripts\obook_recorder.py',
  '--flush','5',
  '--depth',
  '--streams-per-conn','45',
  '--status-sec','600'
)
Start-Process -FilePath ".venv\Scripts\python.exe" -ArgumentList $argv -WindowStyle Hidden `
  -RedirectStandardOutput "logs\obook_recorder.out" -RedirectStandardError "logs\obook_recorder.err"
