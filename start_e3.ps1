# e3 라이브 런처. 인자를 한 곳에서만 관리한다.
Set-Location $PSScriptRoot
$argv = @(
  'scripts\scalp_bot_e3.py',
  '--signal-tf-min','3','--leverage','10','--tranches','1','--cm-atype','4',
  '--min-leg-margin','3.5','--max-leg-margin','8',
  '--max-new-orders-per-cycle','2',
  '--max-concurrency','10','--max-same-side','3',
  '--min-entry-edge-pct','0',
  '--min-net-tp-rate','0.001',
  '--stop-widen-pct','0.65',
  '--cm-tp-pullback-pct','0.5','--cm-tp-max-roe','2.3',
  '--max-signal-age','10','--watchdog-sec','180','--max-exposure','0.85',
  '--adopt-max-stop-roe','5','--new-max-stop-roe','0',
  '--stop-rr-match','0',
  '--early-adverse-sec','0','--mae-cut-roe','0',
  '--cm-htf-filter','--cm-htf-counter-mult','0',
  '--stop-limit','--stop-limit-slip-pct','0.05',
  '--stop-limit-timeout-sec','20','--stop-limit-fail-pct','0.30',
  '--stop-fixed-roe','8.0',
  '--cm-flip-max-bars','5',
  '--entry-order-ttl-sec','45',
  '--same-side-stop-cooldown-sec','3600',
  '--giveback-arm-roe','0','--giveback-frac','0.4',
  '--i-know-it-loses'
)
Start-Process -FilePath ".venv\Scripts\python.exe" -ArgumentList $argv -WindowStyle Hidden -RedirectStandardError "logs\e3_crash.log"
