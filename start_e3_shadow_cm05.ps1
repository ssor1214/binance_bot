# e3 shadow 비교군: CM TP 무효 거래만 증거금 0.5배, 전용 하한 15 USDT
Set-Location $PSScriptRoot
$argv = @(
  'scripts\scalp_bot_e3.py','--dry-run','--instance-tag','cm05','--attach-ws','--no-telegram',
  '--signal-tf-min','3','--tranches','1','--cm-atype','4','--min-leg-margin','30','--max-leg-margin','50',
  '--max-concurrency','10','--max-same-side','3','--min-entry-edge-pct','0','--stop-widen-pct','0.65',
  '--cm-tp-pullback-pct','0.5','--cm-tp-max-roe','4.3','--cm-invalid-tp-size-mult','0.5',
  '--cm-invalid-tp-min-margin','15','--max-signal-age','10','--watchdog-sec','180','--max-exposure','0.95',
  '--adopt-max-stop-roe','5','--new-max-stop-roe','0','--stop-rr-match','0','--early-adverse-sec','0',
  '--mae-cut-roe','0','--cm-htf-filter','--cm-htf-counter-mult','0','--stop-limit',
  '--stop-limit-slip-pct','0.05','--stop-limit-timeout-sec','20','--stop-limit-fail-pct','0.30',
  '--stop-fixed-roe','8.0','--cm-flip-max-bars','5','--giveback-arm-roe','0','--giveback-frac','0.4',
  '--i-know-it-loses'
)
Start-Process -FilePath ".venv\Scripts\python.exe" -ArgumentList $argv -WindowStyle Hidden -RedirectStandardError "logs\e3_cm05_crash.log"
