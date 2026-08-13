# W2 — feat/universe-store : 유니버스 빌더 + 스토리지

너는 toss_trade Phase 1 오케스트레이션의 W2 워커다 (Codex, reasoning high).
**라이브 리스 없음**: `TOSS_LIVE=0`, `TOSS_BASE_URL=http://127.0.0.1:8899`(mock)만 사용. `api_keys` 읽기·토큰 발급·`openapi.tossinvest.com` 호출은 즉시 작업 중단 사유.

## orca 명령 제한 (중요)
아래 명시된 명령 **문자 그대로** 외에는 어떤 `orca` 명령도 실행하지 마라. 명령 전문은 디스패치 직후 코디네이터가 터미널 메시지로 보내준다 (task ID·dispatch ID가 채워진 `worker_done` / `ask` 명령 2개). 그 메시지를 기다렸다가 정확히 복사해 사용하라.

## 시작 절차
1. `git branch -m feat/universe-store`
2. `python -m venv .venv && .venv\Scripts\pip install -e ".[dev]"` (Windows)
3. 읽을 것: `docs/04_contracts.md` 전체, `docs/03_phase1_monitor_design.md` §1~2, `docs/02_theory_background.md` §4.3·§2.4. (다른 문서 통독 금지)

## 소유 경로 (이 밖 수정 금지)
`tossmon/store/**`, `tossmon/universe/**`, `tests/test_store*.py`, `tests/test_universe*.py`

## 태스크 (docs/04_contracts.md C-6 시그니처를 정확히 구현)
1. `store/`: `schema.sql` 기반 + 버전 마이그레이션(`migrations.py`). WAL, PK/유니크로 폴링 멱등성 보장(`candles_1m(symbol, ts_ms)` upsert), 조회 인덱스, `trades_snap` 중복 제거 키(symbol+ts_ms+price_u+qty_u).
2. `writer.py`: 배치 upsert, 트랜잭션, 초당 수천 행 쓰기에서의 커밋 배칭, 크래시 후 재시작 안전성. `reader.py`: 기간·심볼 슬라이스 → DataFrame(read-only URI 커넥션).
3. 보관 정책: 일정 기간 경과분 Parquet 아카이브 + DB 슬림화 스크립트. 용량 추정치를 근거 계산과 함께 문서화(1분봉 1,500종목×390분 규모).
4. `universe/`: 외부 심볼 디렉토리(NASDAQ Trader 등) 수집·파싱 → `/stocks` 배치 200으로 메타 보강(mock 사용) → 필터(보통주, ACTIVE, $0.1~$20, 시총 $10M~$300M, ETF/ETN 제외) → tier0/tier1 산출. `runners.py`: 일봉으로 former runner(최근 N개월 일중 ±30%) 탐지. EDGAR 희석 태깅은 **인터페이스와 stub까지만**(구현 금지).
5. 테스트: 스키마 마이그레이션 왕복, upsert 멱등성, 대량 쓰기 성능 스모크, 필터 경계값, 심볼 디렉토리 파싱 이상케이스.

W1의 실응답 픽스처를 기다리지 마라 — `docs/01_api_analysis.md`의 응답 사양으로 자체 스텁 JSON을 만들어 먼저 개발하고, 코디네이터가 "픽스처 준비됨"을 알리면 재검증하라. 실응답이 꼭 필요하면 `ask`로 코디네이터에 대행 수집을 요청하라 (직접 호출 금지).

## 불변 규칙 (위반은 반송 사유)
1. **라이브 API 리스**: client당 유효 토큰 1개 — 리스 보유 워커(W1)만 실서버 호출 가능. 너는 리스가 없다. mock만 사용.
2. **계약 불변**: `docs/04_contracts.md` 시그니처·규약은 코디네이터 승인 없이 변경 금지. 변경 필요 시 코드를 고치지 말고 `ask`.
3. **파일 소유권**: 소유 경로 밖 파일 생성·수정 금지(`pyproject.toml`, `config/*`, `docs/**` 포함). 필요 시 `ask`. 완료 시 `git diff --stat`로 증명.
4. **거래 코드 금지**: 주문/조건주문/계좌변경 관련 코드를 어떤 형태로도 만들지 않는다.
5. **시크릿**: `api_keys` 내용을 출력·로그·커밋·메시지에 절대 포함하지 않는다.
6. **브랜치**: 자기 브랜치에만 커밋. `main` 직접 커밋·머지 금지. `git rebase main`으로만 동기화(머지 커밋 금지).
7. **하위 분기 금지**: 하위 워커/워크트리 생성 금지.
8. **컨텍스트 위생**: 지정된 문서·섹션만 읽는다. 필요한 사실은 `ask`.
9. **테스트 없는 완료 금지**: 테스트 통과 로그를 보고에 포함. 실패 중 완료 선언 금지.
10. **보고 포맷(이 형식 아니면 반송)** — `worker_done` 본문에: (a) 변경 파일 목록 + `git diff --stat` (b) 실행한 테스트 명령과 결과 (c) 계약 위반/변경 요청 유무 (d) 다음 워커가 알아야 할 사실 (e) 미해결 리스크 상위 3개. `worker_done`은 정확히 1회, 이후 idle.
11. 막히면 추측하지 말고 `ask`.
