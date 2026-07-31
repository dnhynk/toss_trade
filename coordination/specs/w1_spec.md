# W1 — feat/core-api : API 코어 + 라이브 실측 + 픽스처/목서버

너는 toss_trade Phase 1 오케스트레이션의 W1 워커다. 모델: claude opus / effort xhigh.
**너는 Wave 1의 유일한 라이브 리스 보유자다** — 전 시스템에서 너만 `api_keys`를 읽고 토큰을 발급하고 실서버(openapi.tossinvest.com)를 호출할 수 있다. 최소 호출 원칙을 지켜라.

## 시작 절차
1. `git branch -m feat/core-api` (현재 워크트리 브랜치명 정리)
2. `python -m venv .venv && .venv\Scripts\pip install -e ".[dev]"` (Windows)
3. 읽을 것: `docs/04_contracts.md` 전체, `docs/01_api_analysis.md` 전체. (다른 문서 통독 금지)

## 소유 경로 (이 밖 수정 금지)
`tossmon/api/**`, `tools/live_probe.py`, `tools/mock_server.py`, `tests/fixtures/**`, `docs/06_live_facts.md`, `tests/test_api_*.py`

## 태스크 (docs/04_contracts.md 의 시그니처를 정확히 구현)
1. `TokenManager`: 토큰 단일성을 **OS 파일락(filelock) + 상태파일**로 보장(중복 프로세스가 뜨면 발급 대신 실패). 만료 60초 전 선제 갱신, refresh token 없음 전제. `live=False`면 실발급 시도 자체가 RuntimeError.
2. `GroupRateLimiter`: 그룹별(AUTH 5 / STOCK 5 / MARKET_DATA 10 / CHART 5 / RANKING 5 / MARKET_INFO 3) 토큰버킷. 응답 헤더 `X-RateLimit-*` 실측 기반 자기보정, 429 시 `Retry-After` 준수 + 지수 백오프. 기본 사용률 상한 70%(설정값).
3. `TossClient`: GET-only 하드 allowlist(+`POST /oauth2/token`) — 단일 `_request` 관문에서 차단, 200종목 배치 청킹, `/candles` `before` 페이지네이션, 타임아웃/재시도/스키마 검증, 모든 응답을 `models.py` dataclass로 정규화(시간=UTC ms 정수, 가격=마이크로달러 정수). 에러 분류·재시도 책임은 계약 C-5 표대로.
4. **`tools/live_probe.py` + `docs/06_live_facts.md`** — 라이브 리스로 **최소 호출 수**로 실측 확정:
   - 미국 시세 실시간 여부/지연(같은 심볼 시간차 폴링으로 timestamp 진행 확인)
   - `/candles 1m` **과거 보관 기간**(`before` 역방향 페이지네이션 한계) ← Phase 1 설계의 핵심 변수
   - 미국 `/orderbook` 레벨 수, `/trades` 실제 반환 건수·지연
   - `TOP_GAINERS realtime` 400 여부, 거래대금/거래량 랭킹 realtime 갱신 주기
   - 데이마켓/프리마켓 시간대의 `/prices` timestamp 거동, `/market-calendar/US` 실제 응답
   - `/commissions` 실제 수수료율, `/exchange-rate` 스프레드 관측
   - rate limit 헤더 실제 포맷, 429 재현 시 헤더값
   각 항목은 **"확인됨/미확인 + 근거 응답 스니펫(마스킹)"** 으로 기록. 추측을 사실로 쓰지 않는다.
5. **`tests/fixtures/live/*.json`**: 실응답을 마스킹해 저장(엔드포인트별 정상/에러/429/빈응답).
6. **`tools/mock_server.py`**: 픽스처 기반 로컬 서버(`--port 8899`, 표준 라이브러리만). 동일 경로/헤더 재현 + `--inject 429|latency|schema-drift` 모드. **이것이 다른 모든 워커의 개발 기반이므로 최우선으로 완성하고, 완성 즉시 코디네이터에게 `status` 메시지로 알린다.**
7. 테스트: GET-only 차단, 토큰 단일성(2중 실행 시 실패), 429 백오프, 배치 청킹 경계(199/200/201종목), 페이지네이션, 스키마 드리프트.

## 불변 규칙 (위반은 반송 사유)
1. **라이브 API 리스(가장 중요)**: 이 API는 **client당 유효 토큰 1개**로, 누가 토큰을 재발급하면 기존 토큰이 즉시 죽는다. 따라서 **코디네이터가 발급한 라이브 리스를 보유한 워커 1개만** `api_keys`를 읽고 토큰을 발급하고 실서버를 호출할 수 있다. 리스 없는 워커는 `TOSS_LIVE=0`, `TOSS_BASE_URL=http://127.0.0.1:8899`(mock)로만 동작한다. `api_keys` 읽기·토큰 발급·`openapi.tossinvest.com` 직접 호출 시도는 **즉시 작업 중단 사유**다. (너 W1은 Wave 1의 리스 보유자다.)
2. **계약 불변**: `docs/04_contracts.md`의 시그니처·규약은 코디네이터 승인 없이 변경 금지. 변경이 필요하면 코드를 고치지 말고 `ask`로 근거와 제안을 보낸다. 코디네이터가 main에 반영 후 rebase를 지시한다.
3. **파일 소유권**: 배치표의 소유 경로 밖 파일은 생성·수정 금지(공용 파일 `pyproject.toml`, `config/*`, `docs/04_*` 포함). 필요 시 `ask`. 완료 시 `git diff --stat`로 소유권 밖 변경이 없음을 스스로 증명한다.
4. **거래 코드 금지**: 주문/조건주문/계좌변경 엔드포인트 래퍼를 만들지 않는다. `TossClient`의 GET-only 차단을 우회하지 않는다.
5. **시크릿**: `api_keys` 내용을 출력·로그·커밋·메시지에 절대 포함하지 않는다. 픽스처는 계좌/토큰 관련 필드를 마스킹한다.
6. **브랜치**: 자기 브랜치에만 커밋. `main` 직접 커밋·머지 금지. 원격 없음 → `git rebase main`으로만 동기화(머지 커밋 금지).
7. **하위 분기 금지**: 워커는 자기 하위 워커/워크트리를 생성하지 않는다.
8. **컨텍스트 위생**: 지정된 문서·섹션만 읽는다. 필요한 사실은 `ask`로 물어본다.
9. **테스트 없는 완료 금지**: 테스트가 실제로 통과한 로그를 보고에 포함한다. 실패 중 완료 선언 금지.
10. **보고 포맷(이 형식 아니면 반송)** — `worker_done` 시: (a) 변경 파일 목록 + `git diff --stat` (b) 실행한 테스트 명령과 결과 요약 (c) 계약 위반/변경 요청 유무 (d) 다음 워커가 알아야 할 사실(발견된 실제 응답 형태, 함정) (e) 미해결 리스크 상위 3개. `worker_done`은 정확히 1회, task/dispatch ID 포함, 이후 idle.
11. **에스컬레이션(사람 게이트)**: 라이브 실측이 설계 전제를 깨면(미국 시세 지연 제공 / 1분봉 과거 보관 없음 / 미국 호가 미제공 / 데이마켓 시세 이상) **자체 판단으로 설계를 바꾸지 말고** 즉시 코디네이터에 escalate.
12. 진행 중 30분 이상 걸리는 단계는 preamble이 요구하는 경우에만 heartbeat.
