# e3 shadow 비교군 — 4시간 EMA200 역행 신호를 30% 증거금으로 허용
# 라이브 주문/상태/원장과 분리한다. 라이브 설정은 변경하지 않는다.
Set-Location $PSScriptRoot
$argv = @(
  'scripts\scalp_bot_e3.py',
  '--dry-run','--instance-tag','htf03','--attach-ws','--no-telegram',
  '--signal-tf-min','3','--tranches','1','--cm-atype','4',
  '--min-leg-margin','30','--max-leg-margin','50',
  '--max-concurrency','10','--max-same-side','3',
  '--min-entry-edge-pct','0',
  '--stop-widen-pct','0.65',
  '--cm-tp-pullback-pct','0.5','--cm-tp-max-roe','4.3',
  '--max-signal-age','10','--watchdog-sec','180','--max-exposure','0.95',
  '--adopt-max-stop-roe','5','--new-max-stop-roe','0','--stop-rr-match','0',
  '--early-adverse-sec','0','--mae-cut-roe','0',
  '--cm-htf-filter','--cm-htf-counter-mult','0.3',
  '--stop-limit','--stop-limit-slip-pct','0.05',
  '--stop-limit-timeout-sec','20','--stop-limit-fail-pct','0.30',
  '--stop-fixed-roe','8.0',
  '--cm-flip-max-bars','5',
  '--giveback-arm-roe','0','--giveback-frac','0.4',
  '--i-know-it-loses'
)
Start-Process -FilePath ".venv\Scripts\python.exe" -ArgumentList $argv -WindowStyle Hidden -RedirectStandardError "logs\e3_htf03_crash.log"
