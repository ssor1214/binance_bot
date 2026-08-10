# 바이낸스 선물 자동매매 봇

RSI + MACD + EMA(이동평균) 조합 신호로 진입하고, 포지션당 **+3~7% 익절(트레일링) / -5% 손절**을 자동으로 처리하는 바이낸스 USDT-M 선물 자동매매 봇입니다.

## ⚠️ 먼저 읽어주세요

- 이 봇은 **실제 자금으로 주문을 실행**합니다. 레버리지 선물 거래는 원금 이상의 손실이 발생할 수 있습니다.
- API 키를 발급할 때 **출금(Withdraw) 권한은 반드시 끄고**, 선물 거래(Enable Futures) 권한만 부여하세요.
- `.env`의 `USE_TESTNET=true`로 먼저 [바이낸스 선물 테스트넷](https://testnet.binancefuture.com)에서 최소 며칠간 검증한 뒤 실전으로 전환하는 것을 강력히 권장합니다.
- `POSITION_SIZE_RATIO`와 `LEVERAGE`는 감당 가능한 손실 범위 내에서 작게 시작하세요.
- 이 코드는 참고용 구현이며, 특정 수익을 보장하지 않습니다. 시장 상황에 따라 손실이 발생할 수 있습니다.

## 전략 요약

1. **진입 신호** (`bot/strategy.py`)
   - 추세: EMA(fast) vs EMA(slow)
   - 모멘텀: RSI가 과매도(30) 구간에서 반등 / 과매수(70) 구간에서 하락
   - 확인: MACD 골든크로스 / 데드크로스
   - 세 조건이 동시에 맞을 때만 진입 (거짓 신호 감소 목적)
2. **청산 로직** (`bot/position_manager.py`)
   - 수익률이 `TAKE_PROFIT_MAX`(기본 7%) 이상이면 즉시 익절
   - 수익률이 `TAKE_PROFIT_MIN`(기본 3%) 이상이면 트레일링 모드로 전환, 이후 최고점 대비 1%p 하락 시 익절 확정 (3~7% 구간에서 자동 청산)
   - 수익률이 `-STOP_LOSS_PCT`(기본 -5%) 이하이면 즉시 손절

## 설치

```bash
cd binance-futures-bot
python -m venv .venv
.venv\Scripts\activate   # Windows
pip install -r requirements.txt
copy .env.example .env
```

`.env` 파일을 열어 API 키와 파라미터를 채웁니다.

## 실행 (로컬)

```bash
python -m bot.main
```

VS Code에서는 `binance-futures-bot` 폴더를 열고 `bot/main.py`를 실행하거나 위 명령어를 통합 터미널에서 실행하면 됩니다. 로그는 콘솔과 `logs/bot.log`에 함께 기록됩니다.

## 클라우드/서버 배포

### Docker

```bash
docker build -t binance-futures-bot .
docker run -d --name futures-bot --env-file .env --restart unless-stopped binance-futures-bot
```

- `--restart unless-stopped`로 서버 재부팅 시에도 자동 재시작되도록 합니다.
- 봇은 시작 시 거래소에 이미 열려 있는 포지션을 자동으로 다시 추적하므로(`sync_existing_positions`), 재시작해도 손절/익절 로직이 끊기지 않습니다.

### VPS(예: AWS/GCP/Oracle 프리티어)에 직접 배포

1. 서버에 Python 3.11 설치
2. 이 폴더를 업로드 (`.env`는 절대 git에 커밋하지 말고 서버에서 직접 생성)
3. `pip install -r requirements.txt`
4. `systemd` 서비스나 `tmux`/`screen`, 혹은 위 Docker 방식으로 상시 실행

## 커스터마이징

| 항목 | 위치 | 설명 |
|---|---|---|
| 거래 심볼/주기 | `.env` `SYMBOLS`, `INTERVAL` | 쉼표로 여러 심볼 지정 가능 |
| 레버리지/포지션 크기 | `.env` `LEVERAGE`, `POSITION_SIZE_RATIO` | 자산의 몇 %를 한 포지션에 쓸지 |
| 익절/손절 기준 | `.env` `TAKE_PROFIT_MIN/MAX`, `STOP_LOSS_PCT` | 요청사항인 3~7% 익절 / -5% 손절 |
| 지표 파라미터 | `.env` RSI/EMA/MACD 관련 값 | 기간 등 세부 조정 |
| 진입 로직 자체 | `bot/strategy.py` `generate_signal()` | 다른 지표 조합으로 교체 가능 |

## 알려진 제한사항

- 동시에 한 심볼당 하나의 포지션만 관리합니다(피라미딩 없음).
- 네트워크 단절이나 거래소 장애 시 청산 주문이 지연될 수 있습니다 — 반드시 거래소 앱에서도 주기적으로 포지션을 확인하세요.
- 슬리피지로 인해 실제 체결가가 목표 익절/손절률과 약간 다를 수 있습니다(시장가 주문 특성).

## 2026-08-09 야간 리팩터링 요약 (계좌 소진 방지 강화)

"단기 수익을 보장하지 않되 실제 순기대값이 양수인 조건만 거래해서 복리 운용 가능성을
검증·개선"을 목표로 진행한 작업 기록. **코드/테스트/문서 범위로만 진행했고, 이 세션 동안
실거래 프로세스 재시작이나 `.env` 실거래 값 변경, 실 API 호출은 하지 않았다** — 모든 변경은
사용자 승인 후 재시작해야 실거래에 반영된다.

### 변경 파일 목록

| 파일 | 변경 내용 |
|---|---|
| `bot/exchange.py` | `place_take_profit_market()` 추가(트레일링 실패시 거래소측 폴백 주문) |
| `bot/position_manager.py` | `trailing_order_id`/`tp_fallback_order_id` 필드, `protection_state` 계산 property, `average_down_enabled` 플래그 반영 |
| `bot/main.py` | 트레일링 등록 후 REST 재확인, 실패시 TAKE_PROFIT_MARKET 폴백(모든 등록/재등록 지점), 신규 주문 확인 후에만 기존 주문 취소하도록 순서 변경, 포트폴리오 기대값 필터(`passes_expected_value_filter`), 심볼별 마이너스 기대값 자동제외(`compute_negative_ev_symbols`), 합산 증거금 상관리스크 필터(`passes_aggregate_risk_filter`), 리스크 기반 수량계산 함수(`compute_risk_based_position_size`, 아직 미연결), 기본 마진타입 CROSS→ISOLATED, 원장 기록 연결(`record_trade_ledger`), 중복인스턴스 종료코드 구분 |
| `bot/config.py` | `average_down_enabled`, `ev_filter_min_sample`, `max_aggregate_margin_pct`, `profit_lock_buffer_pct` 등 신규 설정값 |
| `bot/trade_ledger.py` | (신규) 거래별 상세 원장 스키마(`TradeRecord`)와 JSONL 저장/조회 |
| `bot/ev_analysis.py` | (신규) 원장 기반 심볼/방향/시간대별 승률·기대값·profit factor·최대낙폭 분석 |
| `bot/telegram_notifier.py` | 수동청산 시 폴백주문 정리 + 원장 기록, 봇수익 기준 리셋(직접매매 제외), 설정이력 한글화, 고정메뉴 |
| `run_forever.py` | 중복인스턴스 감지시 5초→120초 백오프로 재시작 경합 방지 |
| `tests/test_protection_orders.py`, `test_trade_ledger.py`, `test_ev_analysis.py`, `test_risk_sizing.py`, `test_aggregate_risk.py` | (신규) mock/임시파일 기반 단위테스트 47건, 실거래 API 절대 미호출 |

### 설계 이유 (요약)

- **트레일링 폴백**: `TRAILING_STOP_MARKET`이 실패(-2007 등)하면 기존엔 30초 폴링에만 의존했는데, 스캘핑 특성상 거래소측 보호가 항상 있어야 한다는 요구에 따라 `TAKE_PROFIT_MARKET` 폴백을 추가. API 응답만 믿지 않고 `get_open_algo_order_ids()`로 실제 존재를 재확인.
- **기대값 필터**: 신호별 확률 추정치는 과거 분석에서 승패와 상관관계가 없음이 확인돼(2026-08-05) 신뢰하지 않고, 대신 실측 롤링 승률/원장 기반 통계로 포트폴리오·심볼 단위 기대값을 계산.
- **격리마진/무물타기**: 계좌 전체가 아니라 개별 포지션 증거금으로 손실을 제한하고, 지는 포지션에 추가 투입하는 마틴게일형 리스크를 제거.
- **원장 스키마 분리**: `.bot_stats.json`은 누적 집계만 남기고 거래별 상세(진입/청산 사유, 슬리피지 등)가 없었음. WebSocket 없이는 정확한 체결가/수수료를 못 채우므로, 해당 필드는 `None` 허용으로 설계해 나중에 채우기만 하면 되게 함.

### API 호출량 (추정)

- 변경 전/후 모두 메인 루프의 REST 호출량 자체는 동일(250개 심볼 30초 폴링 유지 — WebSocket 전환은 이번 범위에서 보류).
- 신규 추가된 호출: 트레일링 등록 후 검증 1회(`get_open_algo_order_ids`, 이미 다른 안전망에서도 쓰던 엔드포인트라 캐시 재사용 가능), 실패시 폴백 등록 1회(드물게만 발생).
- `compute_negative_ev_symbols`/`passes_aggregate_risk_filter`는 로컬 파일(JSONL) 기반이라 **API 호출 없음**.

### 남아있는 위험 / 미완성 항목

- **WebSocket 시장데이터/계정 레이어(`bot/ws_client.py`, 2026-08-10 신규)**: 소켓 연결·재연결·메시지 파싱만 담당하는 독립 모듈로 1단계 구현 완료(`MarketDataWebSocket`: kline 스트림+`KlineCache`, `UserDataWebSocket`: 계정/체결 이벤트). **`bot/main.py`가 이 모듈을 아직 import하지 않으므로 실거래 스캔/결정 루프는 여전히 100% 기존 REST 폴링 그대로 동작** — 이 파일이 존재한다고 봇 동작이 조금도 바뀌지 않음.
  - **테스트넷 실증 검증 완료(2026-08-10)**: 사용자가 발급한 별도 테스트넷 키(`.env`의 `BINANCE_TESTNET_API_KEY/SECRET`, 실거래 키와 분리)로 실제 연결 테스트 진행. 이 과정에서 **진짜 버그를 발견**했다 — python-binance의 `start_kline_futures_socket`이 문서상 예상과 달리 `"kline"` 이벤트가 아니라 `"continuous_kline"` 이벤트(심볼 필드가 `"s"`가 아니라 `"ps"`)를 보내서, 최초 파서가 모든 메시지를 조용히 버리고 있었다. `parse_futures_kline_message`가 두 형식을 모두 지원하도록 수정하고 회귀테스트 추가, 재검증에서 BTCUSDT kline 정상 수신 확인.
  - **체결 파싱(`OrderFill`/`parse_order_trade_update`/`FillTracker`, 2026-08-10 신규)**: User Data Stream의 `ORDER_TRADE_UPDATE` 이벤트에서 실제 체결(`x=="TRADE"`)만 골라 평균체결가/수수료/실현손익을 추출하고 심볼별 최신값을 캐시하는 유틸리티까지 구현. `record_trade_ledger`가 청산 시 이 값을 조회해 `actual_fill_*` 필드를 채우는 용도(다음 단계). **주의**: kline 파서와 달리 이 파서는 아직 테스트넷 실제 체결로 검증하지 못했다 — 계정에 체결이 발생해야만 이 이벤트가 오므로 구독만 해서는 형식을 확인할 수 없었음. 실거래/테스트넷에서 실제 주문이 한 번 체결된 뒤 형식이 맞는지 재확인 필요.
  - 오프라인 단위테스트 29건(파싱/캐시/콜백안전성/소켓배선, 체결파싱 포함) 전체 통과, 전체 스위트 90/90 통과.
  - **`bot/main.py`에는 여전히 아무것도 연결 안 함** — `UserDataWebSocket`을 실제로 `.start()`하는 코드가 없어 현재 실행 중인 봇은 계정 스트림에 전혀 연결되지 않는다(연결하면 실 API 키로 상시 연결이 열리는 더 큰 결정이라 별도 확인 후 진행 예정).
  - **① REST→WebSocket 실제 연결 완료(2026-08-10)**: `RollingKlineCache`(확정 캔들 N개를 REST `get_klines()`와 동일한 컬럼으로 유지)를 만들고, `Exchange.get_klines()`가 WS 캐시에 충분한 히스토리가 있으면 REST 호출 없이 그걸 쓰고 부족하면 자동으로 REST 폴백하도록 연결. `main()`이 `cfg.ws_market_data_enabled=true`일 때만 부팅 시 심볼별 REST로 초기 시딩 후 `MarketDataWebSocket`을 기동한다. **기본값 False라 지금 실행 중인 봇은 여전히 100% 기존 REST 폴링.**
  - **② User Data Stream 실제 연결 완료(2026-08-10)**: `main()`이 `cfg.ws_user_data_enabled=true`일 때만 `UserDataWebSocket`을 기동해 `FillTracker`를 `Exchange`에 연결하고, `record_trade_ledger`가 청산 시 `ex.get_last_fill()`로 실제 체결가/수수료를 `actual_fill_exit_price`/`commission_usdt`에 채운다. **기본값 False.**
  - **테스트넷 실증 검증에서 발견한 진짜 버그 3건(전부 수정+회귀테스트 완료)**: (a) kline 이벤트가 문서와 달리 `"continuous_kline"`(심볼필드 `"ps"`)로 옴 — 최초엔 모든 메시지를 조용히 버리고 있었음. (b) `RollingKlineCache.seed()`가 open_time을 Timestamp로, `append_closed()`는 정수(ms)로 저장해 타입 불일치로 중복방지 비교가 항상 실패 — REST시딩 직후 첫 WS캔들에서 중복 행 생성 가능했음. (c) `to_dataframe()`이 `pd.to_datetime()`에 `unit="ms"`를 안 줘서 정수를 나노초로 잘못 해석, 모든 캔들 시각이 1970년대로 나오는 버그(지표 계산이 몽땅 깨졌을 것) — 실제 테스트넷 연결로 3번 다 잡아냄. 켜기 전 오프라인 테스트만으론 못 잡는 버그들이었다는 게 이번 검증의 핵심 교훈.
  - **실거래 사고(2026-08-10) + 재설계**: 사용자 승인 후 `.env`에서 두 플래그를 켜고 실거래(250심볼) 봇을 재시작한 결과, `MarketDataWebSocket`이 심볼마다 개별 `start_kline_futures_socket()` 연결을 250개 동시에 열려다 `RuntimeError: This event loop is already running`와 대량 handshake 타임아웃이 발생. `UserDataWebSocket`도 이벤트루프가 막혀 연결 실패. 코드의 예외처리(try/except+REST 폴백)가 정상 동작해 실거래 로직 자체는 영향 없었지만, 대량 연결실패 반복이 **바이낸스 IP 차단으로 이어져 REST 실거래까지 막힐 위험**이 있어 즉시 두 플래그를 다시 꺼서 재시작함(사용자 확인 완료). 원인은 "심볼당 개별 연결"이 설계적으로 잘못된 것 — combined/multiplex 스트림(연결 하나에 여러 심볼 묶어 구독, `MAX_STREAMS_PER_CONNECTION=150`으로 자동 분할)으로 재설계 완료. 테스트넷에서 심볼 203개로 재검증: 연결 1.73초 내 완료, 연결 수 2개(기존 250개 대비), 실제 kline 정상 수신 확인. 오프라인 테스트도 "연결 개수가 심볼 수만큼 늘어나지 않는지" 회귀테스트로 고정(`test_market_data_start_splits_large_symbol_list_into_multiple_connections`).
  - **두 번째 실거래 사고(2026-08-10) + 재설계**: 연결 개수 문제를 고친 뒤 다시 켜서 재시작했더니, 연결 수가 2개로 줄었는데도 **동일한 "이벤트 루프가 이미 실행 중" 오류가 재발**했고 계정스트림 연결도 실패함. 원인 재분석 결과, 연결 개수가 아니라 **시장데이터용 매니저와 계정스트림용 매니저를 각각 별도로(`ThreadedWebsocketManager` 2개 인스턴스) 동시에 띄운 것 자체**가 충돌 원인이었다 — 시장데이터만 단독, 계정스트림만 단독으로는 각각 테스트넷 검증했지만 **"둘을 동시에" 조합은 검증한 적이 없었던 진짜 테스트 공백**이었음(자체 인정하고 기록).
    - 해결: `MarketDataWebSocket.start()`/`UserDataWebSocket.start()`에 `twm` 파라미터를 추가해 외부에서 이미 시작된 공유 매니저를 주입받을 수 있게 함. `main()`이 매니저를 **하나만** 만들어 둘 다에 넘긴다. `stop()`도 공유 매니저(주입된 경우)는 건드리지 않도록 분리(`_owns_twm` 플래그로 구분).
    - **테스트넷 재검증(사고 시나리오 그대로 재현)**: 심볼 203개 + 계정스트림을 "같은" 매니저 하나로 동시에 기동 — `market_ws.start()` 0.20초, `user_ws.start()` 0.00초(같은 매니저라 추가 연결 없음), 이벤트루프 충돌 없이 kline 정상 수신, 정상 종료까지 확인.
    - 오프라인 회귀테스트 4건 추가: 주입된 twm으로 새 매니저를 안 만드는지, `stop()`이 공유 매니저를 안 끄는지, 시장데이터+계정스트림이 실제로 같은 매니저를 동시에 쓸 수 있는지(`ThreadedWebsocketManager` 생성 자체를 막아두고 검증 — 코드가 여전히 새 매니저를 만들려 하면 테스트가 실패하도록 설계).
  - **세 번째 실거래 사고(2026-08-10) + 근본원인 규명 + 워치독 도입**: 매니저 공유로 재설계한 뒤에도 `Read loop has been closed, please reset the websocket connection...` 오류가 초당 수십~수백 건씩 무한 반복되는 문제가 재발. **캐시 신선도 확인(아래 항목)이 먼저 도입돼 매매 판단 자체는 안전했지만, 연결 자체가 회복이 안 되는 근본 원인을 이 시점에 처음으로 라이브러리 소스코드까지 직접 읽어 규명**: `python-binance==1.0.37`의 `reconnecting_websocket.py`에서, 내부 read loop 코루틴이 예기치 못한 예외로 죽으면 `_handle_read_loop`를 `None`으로 **영구히** 남겨두고 스스로 재연결을 시도하지 않는데, `threaded_stream.py`의 소비 루프는 `recv()`가 이 상태에서 즉시 예외를 던지는데도 그냥 로그만 남기고 다시 `recv()`를 호출하는 무한루프를 돈다 — **라이브러리 자체의 버그**(파이썬 3.14/3.12 둘 다 동일 재현, 파이썬 버전 문제가 아니었음).
    - 재현 조건 특정 시도: 테스트넷에서 3개/250개 심볼, 150~180초씩 여러 번 재현을 시도했으나 **재현 안 됨** — 심볼 개수만의 문제가 아니라, 실거래 프로세스 안에서 다른 동시 작업(포지션 관리 REST 호출 등)과 맞물릴 때만 가끔 발생하는 것으로 추정되며 조건을 완전히 못 박지는 못했다.
    - **해결 방향 전환**: 라이브러리가 스스로 못 고치는 버그이므로, "안 죽게 만들기"가 아니라 **"죽으면 우리가 감지해서 직접 재연결"**로 접근을 바꿈. `bot/main.py`에 `start_ws_layer()`/`stop_ws_layer()`/`ws_layer_needs_restart()` 워치독을 도입 — 메인 루프가 매 주기(30초) 대표 심볼(BTCUSDT)의 캐시 신선도를 확인하고, `WS_KLINE_MAX_STALENESS_SEC`(기본 150초)보다 오래 갱신 안 됐으면 기존 연결을 완전히 정리하고 처음부터 다시 연결한다.
    - 오프라인 회귀테스트 10건 추가(매니저 공유 생성, 정리 시 예외 전파 방지, 신선도 기반 재시작 판단) — 전체 스위트 138/138 통과.
  - **네 번째 실거래 사고(2026-08-10) — 프로세스 응답불능 실측 + 프로세스 격리로 근본해결**: in-process 워치독을 켜고 재검증하던 중, `Read loop has been closed` 폭주가 로그만 시끄러운 게 아니라 **프로세스 자체를 CPU 붙잡는 tight loop로 응답불능**에 빠뜨리는 걸 실측(터미널 입력/Ctrl+C도 안 먹힘, `run_forever.py`의 `subprocess.run()`도 무한정 안 리턴됨 — 감시 스크립트조차 무력화). 같은 프로세스 안에서 아무리 정교한 재시작 로직을 짜도, "재시작 판단을 내리는 코드" 자체가 같은 GIL을 놓고 경합하는 처지라 근본 해결이 안 된다는 걸 확인.
    - **재설계**: WS 연결을 `bot/ws_worker.py`라는 **완전히 별도의 OS 프로세스**로 옮겼다. 이 프로세스가 얼어붙어도 메인 봇 프로세스(`bot.main`)는 별도 GIL/프로세스라 전혀 영향받지 않는다. 워커는 캔들/체결 데이터를 파일(`logs/ws_worker_cache.json`, 원자적 쓰기)과 하트비트(`logs/ws_worker_heartbeat.txt`)로 남기고, 메인 프로세스는 `FileBackedKlineCache`/`FileBackedFillTracker`(읽기 전용 어댑터, `RollingKlineCache`/`FillTracker`와 동일 인터페이스)로 그 파일만 읽는다. 워커 하트비트가 오래 끊기면(=죽었거나 응답불능) 캐시 파일 내용이 아무리 최신처럼 보여도 신뢰하지 않고 REST로 폴백 — "데이터가 있다"와 "워커가 지금 살아있다"를 분리해서 확인.
    - **강제종료 보장**: `stop_ws_layer()`가 `terminate()` → (5초 내 안 죽으면) `kill()` 순으로 시도한다. OS 레벨 강제종료는 대상 프로세스가 내부에서 뭘 하고 있든(tight loop라도) 항상 통한다 — 실제 테스트넷 검증에서 확인(아래).
    - **메인 프로세스 자체의 응답불능 대비책도 별도 추가**: `bot/main.py`가 매 주기 하트비트(`logs/heartbeat.txt`)를 남기고, `run_forever.py`가 `subprocess.run()` 대신 `Popen`+주기적 하트비트 확인으로 바뀌어, 300초 이상 하트비트가 없으면 `bot.main` 프로세스 자체를 강제종료 후 재시작한다 — WS 워커 분리로 이 경로를 탈 일은 없어졌지만, 미래의 다른 원인으로 인한 행(hang)에도 방어가 되도록 이중 안전장치로 남겨둠.
    - **테스트넷 실증 검증**: 워커 단독 기동(심볼 10개) → 시세/체결 정상 연결, 캐시 파일 정상 기록, `kill()` 0.00초 즉시 성공. 실거래와 동일 규모(심볼 250개) 4분 soak에서도 하트비트 계속 신선 유지, 최종 강제종료 0.01초. 오프라인 회귀테스트 20건 추가(워커 프로세스 관리 12건, 파일캐시 읽기 8건) — 전체 스위트 **152/152 통과**(Python 3.14/3.12 둘 다).
  - **다시 켜기 전 권장 확인사항**: 라이브러리 버그 자체(연결이 가끔 죽는 것)는 여전히 완전히 없앨 수 없지만, 이제 ① 매매 판단은 신선도 확인으로 항상 안전하고, ② WS 관련 문제가 생겨도 **메인 트레이딩 프로세스에는 구조적으로 영향을 줄 수 없다**(별도 프로세스). `.env`에서 다시 켤 때는 재시작 직후 "WS 워커 프로세스 기동 완료" 메시지 확인, 이후 `logs/ws_worker.log`(워커 전용 로그, 메인 로그와 분리됨)에서 연결 상태를 확인할 것.
  - `bot/main.py`가 아직 두 플래그 다 기본 꺼짐 상태로 유지하는 한, 이 모든 코드가 존재해도 실거래 프로세스는 지금과 동일하게 동작한다. 켤지 여부/시점은 사용자 결정 필요 — 이 리팩터링에서는 `.env`를 건드리지 않았다.
  - 오프라인 단위테스트 총 33건(kline/체결 파싱, 캐시, 소켓배선, Exchange 오버레이, 원장 연동) + 실제 테스트넷 연결검증 2회, 전체 스위트 109/109 통과.
- **리스크 기반 수량계산(`compute_risk_based_position_size`)은 2026-08-10에 `execute_entry`에 연결됐지만 기본은 꺼짐(옵트인)** — `.env`에 `RISK_BASED_SIZING_ENABLED=true`를 명시적으로 추가하지 않는 한 기존 방식(잔고 비율, `compute_position_size`)이 그대로 쓰이므로 현재 실거래 동작에는 변화가 없다. 켤 경우 `RISK_BASED_SIZING_PCT`(기본 1.5, "손절까지 갔을 때 잃는 금액이 잔고의 몇 %"인지) 값으로 손절가까지의 거리 기반 수량을 계산한다. 전환 전 과거 데이터 시뮬레이션으로 승률/낙폭 영향을 먼저 확인 권장(`RiskBasedSizingOptInDefaultTests`로 기본값 False가 회귀테스트됨).
- **실제 체결가/수수료/펀딩비는 여전히 mark price 기반 추정치** — User Data Stream 없이는 정확도 한계.
- 429/418(IP 차단) 대응, 전역 rate limiter, WebSocket 단절 처리는 WS 레이어가 없어 테스트/구현 모두 보류.
- `mark_bot_position_open` 등 일부 원장 연동은 외부(Codex) 작업과 겹쳐 진행 중 — 통합 상태 재확인 필요.

### 롤백 방법

- 모든 변경은 `.env` 실거래 값을 건드리지 않았으므로, 코드 파일만 이전 커밋/백업으로 되돌리면 즉시 원복된다.
- 개별적으로 되돌리고 싶다면: `AVERAGE_DOWN_ENABLED=true`(물타기 복원), 진입 코드에서 `passes_expected_value_filter`/`passes_aggregate_risk_filter`/`compute_negative_ev_symbols` 호출부만 제거하면 해당 필터만 비활성화 가능(각각 독립적으로 되돌릴 수 있게 설계함).
- 트레일링 폴백 로직은 실패해도 항상 기존 STOP_MARKET+폴링 트레일링이 안전망으로 남아있어, 코드를 되돌리지 않아도 즉시 위험해지지 않는다.
