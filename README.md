# 바이낸스 선물 자동매매 봇

바이낸스 USDT-M 무기한선물에서 "이미 크게 움직이기 시작한 1분봉(변동폭+거래량 급증)"에
올라타는 방식의 스캘핑 자동매매 봇입니다. 여러 심볼을 순환하며 짧게 들어갔다 짧게 나오는
"순환매매"를 기본으로 하고, 진입 직후 청산/방어 로직, WebSocket 실시간 시세, 텔레그램
원격제어, 읽기전용 모니터링 대시보드까지 포함합니다.

## ⚠️ 먼저 읽어주세요

- 이 봇은 **실제 자금으로 주문을 실행**합니다. 레버리지 선물 거래는 원금 이상의 손실이 발생할 수 있습니다.
- API 키를 발급할 때 **출금(Withdraw) 권한은 반드시 끄고**, 선물 거래(Enable Futures) 권한만 부여하세요.
- `.env`의 `USE_TESTNET=true`로 먼저 [바이낸스 선물 테스트넷](https://testnet.binancefuture.com)에서 최소 며칠간 검증한 뒤 실전으로 전환하세요.
- `LEVERAGE`, `POSITION_SIZE_MIN/MAX`는 감당 가능한 손실 범위 내에서 작게 시작하세요.
- 이 코드는 참고용 구현이며, 특정 수익을 보장하지 않습니다. 시장 상황에 따라 손실이 발생할 수 있습니다.

## 전략 요약

1. **진입 신호** (`bot/strategy.py: generate_signal_with_probability`)
   - 1분봉 변동폭(`PUMP_MIN_CANDLE_CHG_PCT`)과 거래량 배율(`PUMP_MIN_VOLUME_RATIO`)이
     동시에 임계값을 넘는 캔들을 "펌프"로 감지해 그 방향으로 진입합니다.
   - ADX(추세강도)와 신호일치도로 진입확률을 추정하고, `MIN_ENTRY_PROBABILITY`
     (SHORT는 별도로 `SHORT_MIN_ENTRY_PROBABILITY`, 더 엄격) 미만이면 진입하지 않습니다.
2. **진입 전 필터** (`bot/main.py`, `scan_entry_candidate`) — 신호가 떠도 아래를 모두 통과해야 진입:
   - 휩쏘 필터: 5분봉 변동성이 손절폭 대비 너무 작거나(추세 약함) 너무 크면(휩쏘 위험) 제외
   - 1분봉 노이즈 필터: 진입 캔들의 꼬리가 몸통보다 과도하게 크면 제외
   - SHORT 반전위험 필터: 급반등(양봉) 신호가 있으면 SHORT 진입 제외
   - 진입범위 필터: 진입가가 직전 N분 가격범위 상단(LONG)/하단(SHORT)에 너무 가까우면
     제외 — "이미 다 오른 꼭대기를 추격매수"하는 걸 막기 위함
3. **청산 로직** (`bot/position_manager.py`)
   - `TAKE_PROFIT_MIN`(LONG/SHORT 별도) 이상이면 트레일링 모드로 전환, 최고점 대비
     `TRAIL_DRAWDOWN_PCT` 하락 시 익절 확정. `TAKE_PROFIT_HARD_CAP` 도달 시 즉시 익절.
   - `STOP_LOSS_PCT`(SHORT는 `SHORT_STOP_LOSS_PCT`) 이하이면 즉시 손절. 진입 직후
     `STOP_LOSS_GRACE_SEC` 동안은 손절폭을 `STOP_LOSS_GRACE_WIDEN_MULT`배로 넓혀
     순간적인 되돌림(휩쏘)에 스치지 않게 합니다 — 거래소 STOP_MARKET 주문과 봇 내부
     폴링체크(`PositionManager.evaluate`) 양쪽 모두 동일하게 반영됩니다.
   - 순환매매 강제청산: 일정 시간 이상 보유 중이고 최소 수익 기준을 넘기면 자리를 비워
     다음 후보로 순환합니다.
4. **비중/방어 로직**
   - BTC정렬/방향성과/계좌방어/기대값방어/상관리스크 등 여러 배율이 순차적으로 곱해져
     비중을 정하되, `DEFENSE_STACK_MIN_RATIO_MULT` 하한 밑으로는 내려가지 않게 해서
     방어배율이 겹쳐 진입 자체가 스킵되는 걸 방지합니다.
   - 저잔고 구간에서는 `LOW_BALANCE_NEW_ENTRY_PAUSE_THRESHOLD` 이하일 때 고확률
     후보만/축소된 비중으로 진입하는 복구모드가 자동으로 걸립니다.

## 아키텍처

```
run_forever.py        (감시자) 하트비트 확인, bot.main 응답불능 시 강제종료 후 재시작
  └─ bot/main.py       (메인 루프) 신호 스캔 → 필터 → 진입/청산 결정, 30분마다 자동 복기
       ├─ bot/strategy.py         신호 생성, 확률 추정, 추세정렬
       ├─ bot/position_manager.py 포지션 상태, 손절/익절/물타기 판단
       ├─ bot/exchange.py         바이낸스 REST 주문/조회 래퍼
       ├─ bot/ws_worker.py        (별도 OS 프로세스) 실시간 시세/체결 WS, 파일로 캐시 공유
       ├─ bot/trade_ledger.py     거래별 상세 기록(JSONL)
       ├─ bot/ev_analysis.py      원장 기반 승률/기대값/profit factor 분석
       └─ bot/telegram_notifier.py 텔레그램 알림/원격 파라미터 조정 승인
dashboard/server.py    (별도 프로세스, 읽기전용) .bot_stats.json/원장/실시간잔고를
                        20초마다 갱신되는 대시보드로 서빙 — 주문/설정 변경 기능 없음
```

WS 워커는 메인 프로세스와 완전히 분리된 별도 OS 프로세스입니다 — 워커가 응답불능에
빠져도 메인 매매 프로세스에는 구조적으로 영향을 주지 않으며, 하트비트가 오래 끊기면
자동으로 REST 폴링으로 폴백합니다.

## 설치

```bash
cd binance-futures-bot
python -m venv .venv312
.venv312\Scripts\activate   # Windows
pip install -r requirements.txt
copy .env.example .env
```

`.env` 파일을 열어 API 키와 파라미터를 채웁니다. 항목이 많으므로 `.env.example`의
주석을 먼저 읽어보세요.

## 실행

```bash
# 감시자를 통한 상시 실행 (권장) — bot.main이 응답불능이면 자동 재시작
python run_forever.py

# 또는 봇만 단독 실행 (개발/디버깅용)
python -m bot.main

# 모니터링 대시보드 (선택, 읽기전용)
python dashboard/server.py
```

로그는 콘솔과 `logs/bot.log`에 함께 기록됩니다. `logs/heartbeat.txt`가 최근에
갱신됐는지로 생존 여부를 확인할 수 있습니다.

### 라이브 재시작 시 주의사항

- 반드시 `python -m unittest discover -s tests -q` 전체 통과를 먼저 확인하세요.
- 실행 중인 프로세스를 종료할 때는 정확한 라이브 PID(`run_forever.py`, `bot.main`,
  `bot.ws_worker`)만 종료하세요. `dashboard/server.py`는 별도 프로세스이므로 봇을
  재시작해도 건드릴 필요가 없습니다.
- 봇은 시작 시 거래소에 이미 열려 있는 포지션을 자동으로 다시 추적하므로
  (`sync_existing_positions`), 재시작해도 손절/익절 로직이 끊기지 않습니다.

## 테스트

```bash
python -m unittest discover -s tests -q
```

`tests/`에 순수 오프라인 단위테스트(실 API 호출 없음)가 있습니다. 전략/설정을
바꾸는 모든 변경은 배포 전에 전체 테스트가 통과해야 합니다.

전략/파라미터 변경 검증에는 `offline_backtest.py`(로컬 1분봉 스냅샷 기반, 네트워크
접근 자체를 차단한 오프라인 백테스터, "다음 캔들 시가 체결" 방식으로 lookahead bias를
막음)를 사용하세요.

## 도구 (`scripts/`)

| 스크립트 | 용도 |
|---|---|
| `scripts/postmortem.py` | 손실거래를 1분봉으로 재조회해 청산후 회복 여부/타이밍 품질 분석 (읽기전용) |
| `scripts/analyze_trade_ledger.py` | `logs/trade_ledger.jsonl` 기반 승률/손익비/청산사유분포 분석 |

## 클라우드/서버 배포

### Docker

```bash
docker build -t binance-futures-bot .
docker run -d --name futures-bot --env-file .env --restart unless-stopped binance-futures-bot
```

- `--restart unless-stopped`로 서버 재부팅 시에도 자동 재시작되도록 합니다.

### VPS(예: AWS/GCP/Oracle 프리티어)에 직접 배포

1. 서버에 Python 3.12 설치
2. 이 폴더를 업로드 (`.env`는 절대 git에 커밋하지 말고 서버에서 직접 생성)
3. `pip install -r requirements.txt`
4. `systemd` 서비스나 `tmux`/`screen`, 혹은 위 Docker 방식으로 `run_forever.py`를 상시 실행

## 주요 설정 (`.env`)

| 항목 | 설명 |
|---|---|
| `USE_TESTNET` | true면 테스트넷, false면 실전 |
| `SYMBOLS` / `MAX_AUTO_SYMBOLS` | 고정 심볼 목록 또는 거래량 상위 N개 자동선정 |
| `LEVERAGE`, `POSITION_SIZE_MIN/MAX` | 레버리지, 포지션당 비중(%) |
| `TAKE_PROFIT_MIN`, `SHORT_TAKE_PROFIT_MIN`, `TAKE_PROFIT_HARD_CAP` | 익절 기준(LONG/SHORT 분리) |
| `STOP_LOSS_PCT`, `SHORT_STOP_LOSS_PCT` | 손절 기준(LONG/SHORT 분리) |
| `STOP_LOSS_GRACE_SEC`, `STOP_LOSS_GRACE_WIDEN_MULT` | 진입직후 손절 유예기간/확대배율 |
| `ADX_THRESHOLD`, `PUMP_MIN_CANDLE_CHG_PCT`, `PUMP_MIN_VOLUME_RATIO` | 진입 신호 임계값 |
| `MIN_ENTRY_PROBABILITY`, `SHORT_MIN_ENTRY_PROBABILITY` | 진입 확률 게이트 |
| `ENTRY_RANGE_POSITION_FILTER_ENABLED/LOOKBACK_MIN/MAX_PCT` | 꼭대기/바닥 추격매매 진입 차단 필터 |
| `DEFENSE_STACK_MIN_RATIO_MULT` | 방어배율 누적 하한(과도한 스킵 방지) |
| `LOW_BALANCE_NEW_ENTRY_PAUSE_THRESHOLD` 등 | 저잔고 복구모드 |
| `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` | 텔레그램 알림/원격 파라미터 조정 승인 |

전체 항목은 `.env.example`의 주석을 참고하세요.

## 알려진 제한사항

- 네트워크 단절이나 거래소 장애 시 청산 주문이 지연될 수 있습니다 — 거래소 앱에서도 주기적으로 포지션을 확인하세요.
- 슬리피지로 인해 실제 체결가가 목표 익절/손절률과 약간 다를 수 있습니다.
- WS 실시간 데이터가 끊겨도 REST 폴링으로 자동 폴백하지만, 그만큼 반응속도가 느려질 수 있습니다.
