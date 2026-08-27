# e3 그림자(shadow) 런처 — dry-run A/B.
#
# ⚠ **용도는 '진입 비교' 전용이다. 손익 비교에 쓰면 안 된다.**
#   dry-run 에는 거래소 주문이 없어서 청산 구조가 라이브와 완전히 다르다:
#     - 지정가 TP 가 안 걸린다 -> TP_LIMIT 대신 RR(폴링)로 나가고,
#       tp_rr 은 --cm-tp-max-roe 상한을 안 받아 실측 +18.75% 까지 갔다.
#     - 거래소 손절이 안 걸린다 -> STOP_EMA25(1분 폴링)로 나가며 밀려서,
#       고정 8% 설정인데 실측 -11.14% 로 끝났다.
#   그래서 여기서 나오는 순손익/승률/손익비는 라이브와 비교 불가다.
#
#   ✅ 유효한 것: 어떤 신호를 잡는가 / 시간당 몇 건 잡는가 / 스킵 구성
#      -> **원칙 1(거래 활발) 판정에 쓴다.**
#   ❌ 무효한 것: 순손익, 승률, 손익비, 청산 사유 분포
#      -> 청산 비교는 replay_exits.py(원장 반사실 재생)로 한다.
#
# 라이브를 절대 건드리지 않기 위한 세 가지:
#   --instance-tag shadow : 원장/상태/로그/PID 파일 전부 분리
#   --attach-ws           : 워커를 띄우지 않고 라이브 워커 캐시에 읽기로만 붙는다
#   --dry-run             : 주문 없음 + 봇 락을 잡지 않아 라이브와 공존
#   --no-telegram         : 알림 중복 방지
#
# 라이브와 다른 부분만 아래 [비교 대상] 에 둔다. 나머지는 동일해야 비교가 성립한다.
Set-Location $PSScriptRoot
$argv = @(
  'scripts\scalp_bot_e3.py',
  '--dry-run','--instance-tag','shadow','--attach-ws','--no-telegram',
  '--signal-tf-min','3','--tranches','1','--cm-atype','4',
  '--min-leg-margin','30','--max-leg-margin','50',
  '--max-concurrency','10','--max-same-side','3',
  '--min-entry-edge-pct','0',
  '--stop-widen-pct','0.65',
  '--cm-tp-pullback-pct','0.5','--cm-tp-max-roe','4.3',
  '--max-signal-age','10','--watchdog-sec','180','--max-exposure','0.95',
  '--adopt-max-stop-roe','5','--new-max-stop-roe','0','--stop-rr-match','0',
  '--early-adverse-sec','0','--mae-cut-roe','0',
  '--cm-htf-filter','--cm-htf-counter-mult','0',
  '--stop-limit','--stop-limit-slip-pct','0.05',
  '--stop-limit-timeout-sec','20','--stop-limit-fail-pct','0.30',
  # ---- [비교 대상] 라이브와 다른 설정을 여기 둔다 ----
  '--stop-fixed-roe','8.0',
  '--cm-flip-max-bars','-1',   # 라이브는 5 (전환필터 없이 비교)
  '--giveback-arm-roe','0','--giveback-frac','0.4',
  '--i-know-it-loses'
)
Start-Process -FilePath ".venv\Scripts\python.exe" -ArgumentList $argv -WindowStyle Hidden -RedirectStandardError "logs\e3_shadow_crash.log"
