# W4 — 긴급 핫픽스: 랭킹 저장 int64 오버플로 (애프터 세션 데이터 유실 진행 중)

너는 W4(`tossmon/collector/**`, `tossmon/config.py`, `tests/test_collector*.py` 소유자)다.
**지금 라이브에서 데이터가 새고 있다**: 05:00:36 애프터 전환 직후부터 모든 랭킹 폴이
`WARNING rankings: unexpected OverflowError: Python int too large to convert to SQLite
INTEGER` 로 실패, `rankings_snap` 저장 0건 (139회/110분, 폴 주기와 1:1). 원인 가설:
애프터 세션 랭킹 페이로드의 어떤 정수 필드(마이크로 단위 — mcap_u/거래대금 류)가
SQLite INTEGER 최대 9.22e18 을 초과. 경고에 심볼이 안 찍혀 특정 불가.

**목표: 30분 내 작고 확실한 수정.** 범위를 넓히지 마라.

## 작업

1. 랭킹 스냅 쓰기 경로에서 int64 초과 값을 **행 단위로 처리**: 초과 필드는 NULL(또는
   클램프 — 네 판단, 단 무엇을 했는지 행에 표식)로 저장하고 **심볼·필드·원값을 로그에
   명시**. 배치 전체가 죽지 않게 (지금은 한 행이 폴 전체를 죽이는 것으로 추정 — 확인).
2. **저장 실패를 보이게 하라**: 지금 이 실패가 `api_errors=0` 인 채로 삼켜졌다
   (docs/11 §11-1 의 사각 그대로). `rankings_write_failures` 류 카운터를 텔레메트리에
   추가해 다시는 조용히 새지 않게.
3. 회귀 테스트: 9.22e18 초과 정수를 담은 랭킹 페이로드 → 해당 필드만 NULL/클램프,
   나머지 행 정상 저장, 카운터 증가, 로그에 심볼 명시. 수정 전 실패 stash 증명.
4. 전체 pytest green.

## 불변 규칙

1. **라이브 호출 금지**(mock 전용 — 수집기는 지금 W5 리스로 돌고 있다), `api_keys` 금지,
   W5 수집 DB 접근 금지.
2. 소유 경로 밖 수정 금지 (merge-base diff 증명). `main` 직접 커밋 금지,
   `w4-collector` 에만. 보고 전 `git log` 확인.
3. `worker_done` 1회, **본문 ASCII**, (a)~(e). 빠르게 — 애프터 마감(08:50)까지 반영돼야
   남은 구간이라도 건진다. 머지·재기동은 코디네이터/W5-b 몫이다.
