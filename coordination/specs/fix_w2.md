# W2 수정 — build_universe 진입점 (감사 blocker 지원)

너는 W2(`tossmon/store/**`, `tossmon/universe/**`, `tests/test_store*.py`,
`tests/test_universe*.py` 소유자)다. 짧은 태스크 1건.

**라이브 리스 없음 — 실서버 호출 절대 금지.** W5 가 야간 라이브 수집 중이고, 감사가 밝힌
토큰 경로 결함 때문에 **네가 토큰을 발급하면 그 수집이 즉사한다.** mock 전용:
`TOSS_LIVE=0`, `TOSS_BASE_URL=http://127.0.0.1:8899`. **W5 의 DB 를 건드리지 마라.**

## 배경 (감사 최상위 blocker)
적대적 감사 2건이 독립적으로 확인했다: **`build_universe` 는 호출자가 없다.**
collector 는 `symbols` 테이블을 읽지도 않는다. 그래서 유니버스 필터가 수집 경로에
**한 번도 적용된 적이 없고**, 랭킹에 뜬 대형주(NOK·AMD·ASML·META·GS)가 그대로 워치리스트에
등록됐다. 결과적으로 대형주가 tier2 를 차지해 tier3 진입이 봉쇄됐고, **오늘 밤 수집 데이터는
잘못된 모집단의 것**이 됐다.

collector 쪽 소비(워치리스트 필터링, symbols 시드 읽기)는 **W4 에 지시했다.**
너는 **공급 쪽**을 만든다.

## 작업
1. `git rebase main` (main 79fec4c — 감사 3건 + 사전등록 머지됨)
2. **`build_universe` 실행 진입점**을 만들어라 — 일 1회 실행 전제(`docs/03` §1 Tier 0).
   `python -m tossmon.universe` 형태의 `__main__.py` 가 자연스럽다(collector 가
   `python -m tossmon.collector` 인 것과 대칭). `--config` 로 설정을 받고,
   결과 요약(tier0/tier1/former_runners 수)을 표준출력과 로그에 남겨라.
3. **재실행 안전성**: 하루에 여러 번 돌려도 안전해야 한다(upsert 기반이므로 멱등이겠지만
   실제로 그런지 테스트로 증명하라). 부분 실패 시 이전 상태를 망가뜨리지 않아야 한다.
4. **W4 가 소비할 계약을 명확히 하라** — collector 가 `symbols` 를 시드로 읽을 때
   무엇을 기준으로 고르는지(tier 컬럼? status? updated_ms 신선도?). `Reader.symbols(tier=...)`
   가 이미 있으니 그 의미를 docstring 으로 확정하라. **W4 가 이 계약을 보고 붙인다.**
5. 심볼 디렉토리 수집이 네트워크에 의존하는데(`seed.py`), **네트워크 없이도 캐시로 동작**하는지
   확인하고 테스트하라. 무인 운영에서 외부 사이트가 죽으면 유니버스 갱신이 통째로 멈춘다.
6. 테스트: 진입점 스모크(mock 서버 대상), 재실행 멱등성, 캐시 폴백, 필터 경계값.

## 참고 — 고치지 않아도 되는 것
`filters.py:passes_tier0` 자체는 감사에서 문제로 지적되지 않았다. **필터가 없는 게 아니라
아무도 부르지 않는 것**이 문제다. 필터 로직을 바꾸지 마라(바꿔야 한다고 판단되면 `ask`).

## 불변 규칙
1. 라이브 호출 금지, `api_keys` 읽기 금지, W5 DB 접근 금지.
2. 소유 경로 밖 수정 금지. 완료 시 `git diff --name-only $(git merge-base main HEAD)..HEAD` 로 증명.
3. `main` 직접 커밋 금지. 보고 전 `git log` 확인.
4. `worker_done` 정확히 1회, 보고 (a)~(e).
5. **머지 시점은 내가 정한다**(W5 야간 수집 종료 후). 커밋까지만 하고 보고하라.
