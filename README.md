# 바이낸스 선물 자동매매 봇

바이낸스 USDT-M 무기한선물에서 "이미 크게 움직이기 시작한 1분봉(변동폭+거래량 급증)"에
올라타는 방식의 스캘핑 자동매매 봇입니다. 여러 심볼을 순환하며 짧게 들어갔다 짧게 나오는
"순환매매"를 기본으로 하고, 진입 직후 청산/방어 로직, WebSocket 실시간 시세, 텔레그램
원격제어, 읽기전용 모니터링 대시보드까지 포함합니다.

이 문서는 **바이낸스 계정 자체를 처음 만드는 사람**도 처음부터 끝까지 따라올 수 있도록
작성했습니다. 이미 바이낸스 선물 계정과 API 키가 있다면 [3. 봇 설치](#3-봇-설치)부터
보셔도 됩니다.

---

## 목차

1. [먼저 읽어주세요 — 위험 고지](#1-먼저-읽어주세요--위험-고지)
2. [바이낸스 계정/API 키 준비 (처음이신 분)](#2-바이낸스-계정api-키-준비-처음이신-분)
3. [봇 설치](#3-봇-설치)
4. [.env 설정](#4-env-설정)
5. [테스트넷(모의투자)으로 먼저 검증](#5-테스트넷모의투자으로-먼저-검증)
6. [실행](#6-실행)
7. [전략 요약](#7-전략-요약)
8. [아키텍처](#8-아키텍처)
9. [모니터링 (대시보드 · 텔레그램)](#9-모니터링-대시보드--텔레그램)
10. [테스트 · 백테스트](#10-테스트--백테스트)
11. [도구 (scripts/)](#11-도구-scripts)
12. [클라우드/서버 배포](#12-클라우드서버-배포)
13. [주요 설정 값 (.env)](#13-주요-설정-값-env)
14. [알려진 제한사항](#14-알려진-제한사항)
15. [문제 해결(트러블슈팅)](#15-문제-해결트러블슈팅)

---

## 1. 먼저 읽어주세요 — 위험 고지

> ⚠️ **이 봇은 실제 자금으로 주문을 실행합니다.** 무기한선물(퍼페추얼)은 레버리지 거래라
> **원금 전액 손실은 물론, 설정에 따라 원금 이상의 손실도 발생할 수 있습니다.**

- 이 코드는 참고용 구현이며, **특정 수익을 보장하지 않습니다.** 시장 상황에 따라 손실이
  발생할 수 있고, 실제로 손실이 나는 구간도 있습니다.
- 처음에는 **테스트넷(모의투자)** 으로 최소 며칠, 가능하면 다양한 시장 상황(상승장/하락장/
  횡보장)을 겪어본 뒤 실전으로 전환하세요. → [5장](#5-테스트넷모의투자으로-먼저-검증)
- 실전 전환 후에도 **감당 가능한 손실 범위 내**의 작은 금액으로 시작하세요.
- API 키를 발급할 때 **출금(Withdraw) 권한은 반드시 끄세요.** 봇은 주문/조회 권한만
  있으면 되고, 출금 권한이 있는 키가 유출되면 자금을 통째로 잃을 수 있습니다.
- 봇을 켜뒀다고 완전히 손 놓지 마세요 — 네트워크 단절, 거래소 장애, 이 코드에 아직 발견
  못 한 버그가 있을 수 있습니다. 주기적으로 바이낸스 앱에서 직접 포지션을 확인하세요.
- 이 프로젝트는 개인이 실거래로 직접 운용하며 계속 다듬어온 것이라, 코드 곳곳에 실거래
  중 발견한 문제와 그 수정 이력이 한글 주석으로 남아있습니다. 설정을 바꾸기 전에 관련
  주석을 한번 읽어보시는 걸 권합니다 — 왜 지금 이 값인지 대부분 이유가 적혀있습니다.

---

## 2. 바이낸스 계정/API 키 준비 (처음이신 분)

이미 바이낸스 계정이 있고 선물(Futures) 거래를 해보셨다면 이 장은 건너뛰어도 됩니다.

### 2.1 계정 생성 및 본인인증(KYC)

1. [binance.com](https://www.binance.com)에서 이메일 또는 전화번호로 회원가입합니다.
2. 로그인 후 **신원인증(KYC)** 을 완료합니다 — 신분증 촬영, 얼굴인증 등이 필요합니다.
   이걸 안 하면 입출금/선물거래 등 대부분 기능이 제한됩니다.
3. 원화 입금이 필요하다면 국내 원화 입출금이 가능한 경로(바이낸스 커넥트 등, 국가/시점에
   따라 다름)를 이용하거나, 이미 보유한 코인을 바이낸스 지갑으로 전송합니다.

### 2.2 선물 계좌 활성화 및 자금 이체

1. 상단 메뉴에서 **파생상품(Derivatives) → USDⓈ-M 선물** 로 들어갑니다.
2. 처음이면 위험 고지 동의 절차를 거쳐 선물 계좌를 활성화합니다.
3. **현물 지갑 → USDⓈ-M 선물 지갑**으로 USDT를 이체합니다(화면의 "이체" 버튼). 이 봇은
   USDT-M 무기한선물만 사용하므로, 반드시 **USDⓈ-M 선물 지갑**에 USDT가 있어야 합니다.

### 2.3 API 키 발급 (가장 중요한 단계)

1. 우측 상단 프로필 아이콘 → **API 관리(API Management)** 로 이동합니다.
2. "API 키 생성"을 누르고 라벨(이름)을 아무거나 입력합니다(예: `futures-bot`).
3. 보안 인증(이메일/OTP 등)을 완료하면 **API Key**와 **Secret Key**가 발급됩니다.
   - **Secret Key는 이 화면을 벗어나면 다시 볼 수 없습니다.** 반드시 그 자리에서
     안전한 곳(비밀번호 관리자 등)에 복사해두세요.
4. 발급된 키의 권한(permissions)을 편집합니다:
   - ✅ **선물 거래 활성화(Enable Futures)** — 반드시 켭니다.
   - ❌ **출금 활성화(Enable Withdrawals)** — 반드시 **꺼둡니다.** 이 봇은 출금 기능을
     전혀 쓰지 않고, 켜두면 키 유출 시 자금이 통째로 빠져나갈 수 있습니다.
   - (선택) IP 접근 제한 — 봇을 고정 IP 서버에서 돌린다면 그 IP로 제한해두면 더 안전합니다.
     가정용 PC처럼 IP가 자주 바뀌면 제한하지 않아도 됩니다.
5. 완성된 **API Key / Secret Key**를 이후 [4장](#4-env-설정)에서 `.env` 파일에 넣습니다.

> 테스트넷 API 키는 실제 계정과 별개로 발급받아야 합니다 — [5장](#5-테스트넷모의투자으로-먼저-검증)에서 안내합니다.

---

## 3. 봇 설치

### 3.1 필요한 것

- Python 3.12 (3.11도 대부분 호환되지만, 이 프로젝트는 3.12 기준으로 운용/검증됩니다)
- Git (이 저장소를 내려받기 위함)
- (선택) 텔레그램 계정 — 알림/원격제어를 쓰려면 필요

### 3.2 설치 절차

```bash
git clone https://github.com/ssor1214/binance_bot.git
cd binance_bot

python -m venv .venv312
.venv312\Scripts\activate      # Windows
# source .venv312/bin/activate   # macOS/Linux

pip install -r requirements.txt
copy .env.example .env         # Windows
# cp .env.example .env           # macOS/Linux
```

`.env.example`은 이 봇이 지금까지 실거래로 검증하며 다듬어온 **모든 설정값의 현재
기준값**을 그대로 담고 있고, 값마다 "왜 이 값인지" 한글 주석이 달려있습니다. 처음
쓰신다면 [4장](#4-env-설정)에서 안내하는 핵심 몇 개만 채우고 나머지는 그대로 두는 걸
권장합니다.

---

## 4. .env 설정

`.env` 파일을 열어 아래 항목을 채웁니다. 나머지 값은 이미 실거래로 검증된 기준값이라
바로 바꾸지 않아도 됩니다.

| 항목 | 설명 |
|---|---|
| `BINANCE_API_KEY` / `BINANCE_API_SECRET` | [2.3](#23-api-키-발급-가장-중요한-단계)에서 발급받은 실전 키 |
| `USE_TESTNET` | **처음엔 반드시 `true`.** 테스트넷으로 충분히 검증한 뒤에만 `false`로 |
| `BINANCE_TESTNET_API_KEY` / `BINANCE_TESTNET_API_SECRET` | 테스트넷 전용 키 ([5장](#5-테스트넷모의투자으로-먼저-검증) 참고) |
| `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` | (선택) 알림을 받으려면 채움, [9장](#9-모니터링-대시보드--텔레그램) 참고 |

나머지 값들은 [13장](#13-주요-설정-값-env)에 표로 정리해뒀고, `.env.example` 안의 주석에도
각 값의 배경이 적혀있습니다.

---

## 5. 테스트넷(모의투자)으로 먼저 검증

바이낸스 선물은 **테스트넷**이라는 완전히 분리된 모의투자 환경을 제공합니다. 가짜 자금으로
실제 API와 동일하게 동작해서, 코드/설정을 실전 자금 없이 검증할 수 있습니다.

1. [testnet.binancefuture.com](https://testnet.binancefuture.com)에 접속해 **깃허브
   계정으로 로그인**합니다(실제 바이낸스 계정과는 별개입니다).
2. 로그인하면 테스트용 USDT가 자동으로 지급됩니다(부족하면 화면의 충전 버튼으로 재충전
   가능).
3. 상단 API Key 메뉴에서 테스트넷 전용 API Key/Secret을 발급받습니다(실전 키 발급과
   절차는 비슷하지만 완전히 별개의 키입니다).
4. `.env`에 `USE_TESTNET=true`로 두고, `BINANCE_TESTNET_API_KEY`/`BINANCE_TESTNET_API_SECRET`에
   방금 발급받은 값을 넣습니다.
5. [6장](#6-실행)대로 실행해서 최소 며칠간 지켜보세요. 로그(`logs/bot.log`)와 텔레그램
   알림으로 진입/청산이 의도대로 되는지 확인합니다.

테스트넷에서 충분히 검증됐다고 판단되면, `.env`의 `USE_TESTNET=false`로 바꾸고
`BINANCE_API_KEY`/`BINANCE_API_SECRET`(실전 키)이 채워져 있는지 다시 확인한 뒤 재시작하면
실전으로 전환됩니다.

---

## 6. 실행

```bash
# 감시자를 통한 상시 실행 (권장) — bot.main이 응답불능/크래시면 자동 재시작하고,
# 여유 메모리가 부족해지면 자동으로 정리를 시도한다.
python run_forever.py

# 또는 봇만 단독 실행 (개발/디버깅용, 자동복구 없음)
python -m bot.main

# 모니터링 대시보드 (선택, 읽기전용 — 주문/설정 변경 기능 없음)
python dashboard/server.py
```

로그는 콘솔과 `logs/bot.log`에 함께 기록됩니다. `logs/heartbeat.txt`가 최근에 갱신됐는지로
생존 여부를 확인할 수 있습니다(`run_forever.py`가 이 파일로 상태를 감시합니다).

### 라이브 재시작 시 주의사항

- 반드시 `python -m unittest discover -s tests -q` 전체 통과를 먼저 확인하세요.
- 실행 중인 프로세스를 종료할 때는 정확한 PID(`run_forever.py`, `bot.main`,
  `bot.ws_worker`)만 종료하세요. `dashboard/server.py`는 별도 프로세스이므로 봇을
  재시작해도 건드릴 필요가 없습니다.
- 봇은 시작 시 거래소에 이미 열려 있는 포지션을 자동으로 다시 추적하므로
  (`sync_existing_positions`), 재시작해도 손절/익절 로직이 끊기지 않습니다.

---

## 7. 전략 요약

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
     순간적인 되돌림(휩쏘)에 스치지 않게 합니다.
   - EARLY_EXIT/SOFT_STOP: 손실 중 추세반전 신호(EMA/MACD/RSI)가 뚜렷하면 정식
     손절선까지 기다리지 않고 더 작은 손실에서 먼저 정리합니다. 단, 진입 직후 짧은
     시간(`EARLY_EXIT_MIN_HOLD_SEC`/`SOFT_STOP_MIN_HOLD_SEC`)은 노이즈성 반전으로
     오판하지 않도록 발동을 유예합니다.
   - 순환매매 강제청산: 일정 시간 이상 보유 중이고 최소 수익 기준을 넘기면 자리를 비워
     다음 후보로 순환합니다.
4. **비중/방어 로직**
   - BTC정렬/방향성과/계좌방어/기대값방어/상관리스크 등 여러 배율이 순차적으로 곱해져
     비중을 정하되, `DEFENSE_STACK_MIN_RATIO_MULT` 하한 밑으로는 내려가지 않게 해서
     방어배율이 겹쳐 진입 자체가 스킵되는 걸 방지합니다.
   - 저잔고 구간(`LOW_BALANCE_NEW_ENTRY_PAUSE_THRESHOLD` 이하)에서는 고확률 후보만/
     축소된 비중으로 진입하는 복구모드가 자동으로 걸리고, `CRITICAL_BALANCE_STOP_USDT`
     이하로 떨어지면 신규 진입 자체를 완전히 차단합니다(기존 포지션 관리는 계속).
   - 잔고가 커지면(복리) 포지션당 절대 리스크가 같이 커지는 걸 막기 위해, 잔고가
     `LARGE_BALANCE_TIER1~3_THRESHOLD`(기본 300/500/1000 USDT)를 초과할 때마다
     비중 상한을 단계적으로 낮춥니다(기본 15%/12%/10%).

---

## 8. 아키텍처

```
run_forever.py        (감시자) 하트비트 확인, bot.main 응답불능/크래시 시 강제종료 후
                       재시작. 여유 메모리가 부족해지면(기본 500MB 미만) 알려진 비필수
                       프로세스를 자동 정리해 재기동 여유를 확보한다.
  └─ bot/main.py       (메인 루프) 신호 스캔 → 필터 → 진입/청산 결정, 30분마다 자동 복기를
                       텔레그램으로 전송(거래수/승률/손익/손실거래 재조회 등)
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

---

## 9. 모니터링 (대시보드 · 텔레그램)

### 대시보드

`python dashboard/server.py`로 실행하면 기본 포트 8787에서 읽기전용 대시보드가 뜹니다.
같은 PC/네트워크에서 `http://127.0.0.1:8787`로 접속해 확인할 수 있고, 외부(폰 등)에서
보려면 [Cloudflare Tunnel](https://github.com/cloudflare/cloudflared) 같은 터널링 도구로
외부 URL을 발급받아 쓸 수 있습니다(대시보드 자체엔 주문/설정 변경 기능이 없어 URL이
노출돼도 자금에 직접적인 위험은 없지만, 임의 공개는 권장하지 않습니다).

### 텔레그램

1. [BotFather](https://t.me/BotFather)에게 `/newbot`으로 새 봇을 만들고 토큰을 받습니다.
2. 만든 봇과 대화를 시작한 뒤, `.env`에 `TELEGRAM_BOT_TOKEN`을 넣고 봇을 재시작합니다.
3. 텔레그램에서 봇에게 아무 메시지나 보내면, `logs/bot.log`나 [getUpdates API](https://core.telegram.org/bots/api#getupdates)로
   자신의 Chat ID를 확인해 `.env`의 `TELEGRAM_CHAT_ID`에 넣습니다.
4. 재시작하면 진입/청산 즉시 알림, 30분마다 자동 복기 리포트, 잔고/포지션 조회 메뉴,
   파라미터 튜닝 제안(승인 버튼 포함) 등을 받을 수 있습니다.

---

## 10. 테스트 · 백테스트

```bash
python -m unittest discover -s tests -q
```

`tests/`에 순수 오프라인 단위테스트(실 API 호출 없음)가 있습니다. 전략/설정을 바꾸는
모든 변경은 배포 전에 전체 테스트가 통과해야 합니다.

전략/파라미터 변경 검증에는 `offline_backtest.py`(로컬 1분봉 스냅샷 기반, 네트워크 접근
자체를 차단한 오프라인 백테스터, "다음 캔들 시가 체결" 방식으로 lookahead bias를 막음)를
사용하세요. **주의**: 이 백테스터는 `bot/strategy.py`의 실제 신호 로직과 별개로 재구현된
것이라, 확률게이트나 최근 추가된 필터들을 그대로 반영하지 않습니다 — 전략 변경 검증엔
`bot/strategy.py`/`bot/indicators.py`의 실제 함수를 직접 불러와 재현하는 방식을 권장합니다.

---

## 11. 도구 (`scripts/`)

| 스크립트 | 용도 |
|---|---|
| `scripts/postmortem.py` | 손실거래를 1분봉으로 재조회해 청산후 회복 여부/타이밍 품질 분석 (읽기전용) |
| `scripts/analyze_trade_ledger.py` | `logs/trade_ledger.jsonl` 기반 승률/손익비/청산사유분포 분석 |

---

## 12. 클라우드/서버 배포

### Docker

```bash
docker build -t binance-futures-bot .
docker run -d --name futures-bot --env-file .env --restart unless-stopped binance-futures-bot
```

- `--restart unless-stopped`로 서버 재부팅 시에도 자동 재시작되도록 합니다.
- 이미지는 `run_forever.py`로 실행되므로(`bot.main` 직접 실행이 아님) 컨테이너 안에서도
  자동복구가 동작합니다.
- 대시보드/텔레그램을 같이 쓰려면 대시보드 포트(기본 8787)를 `-p 8787:8787`로 열어주세요.

### VPS(예: AWS/GCP/Oracle 프리티어)에 직접 배포

1. 서버에 Python 3.12 설치
2. 이 폴더를 업로드 (`.env`는 절대 git에 커밋하지 말고 서버에서 직접 생성)
3. `pip install -r requirements.txt`
4. `systemd` 서비스나 `tmux`/`screen`, 혹은 위 Docker 방식으로 `run_forever.py`를 상시 실행

### 최소 사양

이 봇은 원래 4GB RAM 수준의 저사양 PC에서도 돌아가도록 다듬어졌습니다(`run_forever.py`의
자동 메모리 관리 기능 참고). 다만 여유가 있다면 최소 2 vCPU / 4GB RAM 이상을 권장합니다 —
동시에 여러 심볼을 스캔하고 WebSocket 워커까지 별도 프로세스로 띄우기 때문입니다.

---

## 13. 주요 설정 값 (.env)

| 항목 | 설명 |
|---|---|
| `USE_TESTNET` | true면 테스트넷, false면 실전 |
| `SYMBOLS` / `MAX_AUTO_SYMBOLS` | 고정 심볼 목록 또는 거래량 상위 N개 자동선정 |
| `LEVERAGE_MIN/MAX` | 레버리지 범위 |
| `POSITION_SIZE_MIN/MAX` | 포지션당 비중(잔고 대비 %) |
| `LARGE_BALANCE_TIER1~3_THRESHOLD/MAX_RATIO` | 잔고가 커지면(복리) 단계적으로 비중 상한을 낮추는 구간 |
| `TAKE_PROFIT_MIN`, `SHORT_TAKE_PROFIT_MIN`, `TAKE_PROFIT_HARD_CAP` | 익절 기준(LONG/SHORT 분리) |
| `STOP_LOSS_PCT`, `SHORT_STOP_LOSS_PCT` | 손절 기준(LONG/SHORT 분리) |
| `STOP_LOSS_GRACE_SEC`, `STOP_LOSS_GRACE_WIDEN_MULT` | 진입직후 손절 유예기간/확대배율 |
| `EARLY_EXIT_MIN_LOSS_ROE`, `EARLY_EXIT_MIN_HOLD_SEC` | 추세반전 조기탈출 발동 손실기준/최소보유시간 |
| `SOFT_STOP_MIN_LOSS_ROE`, `SOFT_STOP_MIN_HOLD_SEC` | 1시간봉 재평가 약손절 발동 손실기준/최소보유시간 |
| `ADX_THRESHOLD`, `PUMP_MIN_CANDLE_CHG_PCT`, `PUMP_MIN_VOLUME_RATIO` | 진입 신호 임계값 |
| `MIN_ENTRY_PROBABILITY`, `SHORT_MIN_ENTRY_PROBABILITY` | 진입 확률 게이트 |
| `ENTRY_RANGE_POSITION_FILTER_ENABLED/LOOKBACK_MIN/MAX_PCT` | 꼭대기/바닥 추격매매 진입 차단 필터 |
| `DEFENSE_STACK_MIN_RATIO_MULT` | 방어배율 누적 하한(과도한 스킵 방지) |
| `LOW_BALANCE_NEW_ENTRY_PAUSE_THRESHOLD` 등 | 저잔고 복구모드 |
| `CRITICAL_BALANCE_STOP_USDT` | 이 잔고 이하로 떨어지면 신규 진입 완전 차단(원금 보호 최후 방어선) |
| `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` | 텔레그램 알림/원격 파라미터 조정 승인 |

전체 항목은 `.env.example`의 주석을 참고하세요 — 각 값이 왜 지금 값인지, 언제/어떤
실거래 근거로 바뀌었는지가 대부분 남아있습니다.

---

## 14. 알려진 제한사항

- 네트워크 단절이나 거래소 장애 시 청산 주문이 지연될 수 있습니다 — 거래소 앱에서도 주기적으로 포지션을 확인하세요.
- 슬리피지로 인해 실제 체결가가 목표 익절/손절률과 약간 다를 수 있습니다.
- WS 실시간 데이터가 끊겨도 REST 폴링으로 자동 폴백하지만, 그만큼 반응속도가 느려질 수 있습니다.
- `run_forever.py`의 자동 메모리 관리는 Windows 전용(`ctypes`로 `GlobalMemoryStatusEx` 호출)입니다 — 다른 OS에서는 이 기능만 비활성 상태로 동작합니다.

---

## 15. 문제 해결(트러블슈팅)

| 증상 | 확인할 것 |
|---|---|
| 봇이 안 켜짐 | `.env`의 API 키가 올바른지, `USE_TESTNET` 값과 키 종류(실전/테스트넷)가 일치하는지 확인 |
| 거래가 전혀 안 됨 | `logs/bot.log`에서 스캔은 도는지 확인. 시장 자체가 조용하면(변동성 낮음) 정상적으로 신호가 안 뜰 수 있음 |
| 텔레그램이 안 옴 | `TELEGRAM_BOT_TOKEN`/`TELEGRAM_CHAT_ID` 값, 봇과 먼저 대화를 시작했는지 확인 |
| 재시작이 반복됨 | `logs/supervisor.log`에서 하트비트 정지 사유 확인. 실제로 매매가 되고 있었다면 오탐일 수 있으니 `logs/bot.log`의 해당 시간대 활동 여부를 대조 |
| 프로세스가 안 죽고 남음(미아 프로세스) | Windows에서 venv 셔틀이 실제 인터프리터를 자식으로 spawn하는 구조라, 강제종료 시 자식까지 같이 안 죽을 수 있음. `taskkill /PID <pid> /T /F`로 트리 전체 종료 |
