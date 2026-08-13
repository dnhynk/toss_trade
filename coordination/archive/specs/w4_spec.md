# W4 — feat/collector : 티어드 수집 루프 + 검출기 (Wave 2 통합 지점)

너는 toss_trade Phase 1 오케스트레이션의 W4 워커다. 모델: claude opus / effort xhigh.
**라이브 리스 없음**: `TOSS_LIVE=0`, `TOSS_BASE_URL=http://127.0.0.1:8899`(mock)만 사용.
`api_keys` 읽기·토큰 발급·`openapi.tossinvest.com` 호출 시도는 즉시 작업 중단 사유.

## 시작 절차
1. `python -m venv .venv && .venv\Scripts\pip install -e ".[dev]"` (Windows)
   (`git branch -m` 은 시도하지 마라 — Windows 파일 락으로 실패한다. 현재 브랜치 그대로 쓴다.)
2. mock 서버 기동: `python tools/mock_server.py --port 8899` (별도 터미널 또는 백그라운드)
3. 읽을 것: `docs/04_contracts.md` 전체(**개정 A1·A2 절 필독**), `docs/06_live_facts.md` 전체,
   `docs/03_phase1_monitor_design.md` 전체(**§6 계획 변경 필독**). 다른 문서 통독 금지.

## 소유 경로 (이 밖 수정 금지)
`tossmon/collector/**`, `tossmon/config.py`, `tests/test_collector*.py`

## 태스크
1. `scheduler.py`: `/market-calendar/US` 기반 세션 인지 루프(day/pre/regular/after/closed),
   서머타임 자동 대응(하드코딩 금지), 세션 전환 시 티어 재구성.
   실측 세션(KST): day 09:00–17:00, pre 17:00–22:30, regular 22:30–05:00, after 05:00–08:50.
2. `config.py`: `config/config.yaml` + env 오버라이드 → `Config` dataclass 단일 출처 (계약 C-9).
   `config.example.yaml` 의 키와 1:1 유지. 키 추가가 필요하면 `ask`.
3. `loops.py`: tier1 가격 스윕 / tier2 1분봉 / tier3 마이크로(trades+orderbook) / 랭킹 4종 스냅샷.
   asyncio 단일 프로세스, 루프별 독립 task.
   **A2 반영 필수**: 호가 폴링 15~20s로 늦추고 `/trades` 3~5s로 조밀하게. 랭킹 스냅샷은
   과거 조회가 불가능한 유일한 데이터이므로 **수집 우선순위 최상위**. 실시간 1분봉 우선순위는 낮춘다
   (백필로 대체 가능).
4. `detector.py`: 전조 스코어 → 티어 승격/강등 상태머신(히스테리시스로 플래핑 방지, `promotions` 기록),
   이벤트 실시간 감지 → `events` 기록.
   **피처 계산은 W3의 `tossmon.analysis` 함수를 재사용한다 (중복 구현 금지).**
   실시간 판정에는 `extract_precursor_features(..., include_t0=True)` 를 쓴다 (계약 개정 A1-1).
   **Tier1 승격 트리거 1순위는 `first_print`(timestamp null→값 전이)와 `staleness_s` 급감** —
   "휴면 동전주가 깨어나는 순간" (A2-3).
5. `budget.py`: 그룹별 실사용량 관측 → 한도 70% 초과 예측 시 자동 티어 축소(안전 강등).
   초과는 버그가 아니라 사고로 취급한다.
6. 견고성: 크래시/재시작 시 이어받기(마지막 수집 지점 복원), 네트워크 단절 복구,
   로컬-서버 시간 오차 처리, 장시간 무인 실행 메모리 안정성.
   `notifier.py` 는 콘솔+파일 로그(텔레그램은 인터페이스만).
7. 테스트: mock 서버 + `tests/synth.py`(W3 제공, `from tests import synth`)로 **가속 리플레이
   통합 테스트**(정규장 1세션을 수십 초로 압축), 429 주입 시 예산 가드 동작,
   승격/강등 히스테리시스, 재시작 이어받기.

## W1이 실측으로 확인한 함정 (반드시 대응하라)
- **함정1**: 미존재 심볼은 404가 아니라 **200 + 응답에서 조용히 누락**된다. 200개 요청이 180개
  응답일 수 있다 — 요청/응답 심볼을 대조하라.
- **함정2**: 소형주는 체결이 없으면 `timestamp`가 null이거나 과거에 고정되는데 **`lastPrice`는 계속 온다**.
  이 값을 '현재가'로 신뢰하면 안 된다.
- **함정3**: `before`는 inclusive라 `nextBefore`를 그대로 넘기면 경계 봉 1개가 중복된다
  (upsert라 저장은 멱등하지만 카운팅이 틀어진다).
- **함정4**: 일봉 당일 봉이 장중에도 이미 존재하며 **진행형**이다 — 완성봉으로 쓰면 베이스라인이 깨진다.
- **함정5**: 체결이 없는 분은 **1분봉 자체가 빠진다** — 결측 분을 0거래량으로 리샘플해야 한다.
- **함정6**: `/trades` 응답에 `symbol` 필드가 없다(클라이언트가 주입).
- **응답형태**: rankings는 `price` 블록 중첩, 캘린더 키는 today/previousBusinessDay/nextBusinessDay,
  rate limit 헤더는 정상응답에도 실려온다.

## 불변 규칙 (위반은 반송 사유)
1. **라이브 API 리스**: client당 유효 토큰 1개 — 리스 보유자만 실서버 호출 가능. 너는 리스가 없다. mock만.
2. **계약 불변**: `docs/04_contracts.md`의 시그니처·규약은 코디네이터 승인 없이 변경 금지.
   변경 필요 시 코드를 고치지 말고 `ask`.
3. **파일 소유권**: 소유 경로 밖 파일 생성·수정 금지(`pyproject.toml`, `config/*`, `docs/**`,
   `tossmon/api/**`, `tossmon/store/**`, `tossmon/analysis/**` 포함). 필요 시 `ask`.
   완료 시 `git diff --stat $(git merge-base main HEAD)..HEAD` 로 증명하라.
4. **거래 코드 금지**: 주문/조건주문/계좌변경 코드를 어떤 형태로도 만들지 않는다.
   `TossClient`의 GET-only 차단을 우회하지 않는다.
5. **시크릿**: `api_keys` 내용을 출력·로그·커밋·메시지에 절대 포함하지 않는다.
6. **브랜치**: 자기 브랜치에만 커밋. `main` 직접 커밋·머지 금지. `git rebase main`으로만 동기화.
7. **하위 분기 금지**: 하위 워커/워크트리 생성 금지.
8. **컨텍스트 위생**: 지정된 문서만 읽는다. 필요한 사실은 `ask`.
9. **테스트 없는 완료 금지**: 테스트 통과 로그를 보고에 포함. 실패 중 완료 선언 금지.
10. **보고 포맷** — `worker_done` 시: (a) 변경 파일 + diff --stat (b) 실행 테스트와 결과
    (c) 계약 위반/변경 요청 유무 (d) 다음 워커가 알아야 할 사실 (e) 미해결 리스크 상위 3개.
    정확히 1회, task/dispatch ID 포함, 이후 idle.
11. **에스컬레이션**: 설계 전제를 깨는 발견이면 자체 판단 말고 즉시 escalate.
12. **"준비됨/완료" 보고 전에 반드시 `git log`로 커밋을 확인하라.** 워커 간 공유는 커밋으로만 이루어진다.
