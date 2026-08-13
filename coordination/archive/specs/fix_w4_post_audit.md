# W4 — 감사 후속 2건 (리셋 후 복구 태스크)

너는 W4(`tossmon/collector/**`, `tossmon/config.py`, `tests/test_collector*.py` 소유자)다.
**오케스트레이션 런타임이 리셋됐다** — 옛 taskId/dispatchId 는 전부 무효다. 이 태스크가 새 기준이다.
네 재개 지점은 `coordination/HANDOFF-W4.md` 에 네가 직접 적어뒀다. 먼저 그것부터 읽어라.

**라이브 호출 절대 금지** (`TOSS_LIVE=0`, `TOSS_BASE_URL=http://127.0.0.1:8899`).
라이브 리스는 아무도 갖고 있지 않으며, 계약 A6 의 전환 규칙이 적용 중이다.

## 시작
`git rebase main` (main 은 감사수정 3건 + 핸드오프 8건이 전부 머지된 상태, **704 tests green**)

## 수정 1 — `ForbiddenEndpoint` 미처리 예외 [치명, 최우선]
계약 개정 **A6** 으로 `ForbiddenEndpoint` 가 `TossApiError` 를 **상속하지 않게** 됐다
(`tossmon/api/errors.py`). "잡지 말 것"을 타입이 강제하게 한 의도적 변경이다.

그 결과 `loops.py` 의 `except (TossApiError, OSError)` 에 **더 이상 잡히지 않아
미처리 예외로 수집 루프가 죽는다.** W1 이 발견해 넘겼고, 소유 밖이라 손대지 않았다.

수정: `except ForbiddenEndpoint:` 를 **명시적으로** 두고 `ctx.shutdown()` + **alert 경보**.
- 조용히 삼키지 마라. 이건 "주문 계열 엔드포인트에 도달했다"는 뜻이고 Phase 2 의 최종 방어선이다.
- 광역 `except Exception` 으로 덮지 마라 (A6 의 의도를 무효화한다).
- 회귀 테스트: allowlist 위반이 발생하면 **루프가 조용히 죽지도, 계속 돌지도 않고**
  shutdown + 경보로 끝나는지.

## 수정 2 — 가짜 ERROR 경보 [높음]
라이브 로그에서 관측됐다:
```
budget: RANKING predicted 0.33 req/s > target 3.50 — 랭킹은 축소 대상이 아니다. 주기/한도를 재검토하라
```
**0.33 은 3.50 보다 크지 않다.** 비교식 또는 메시지 생성 로직 버그다.

무인 운영에서 가짜 ERROR 는 **경보 무시 습관**을 만들어 진짜 경보를 묻는다
(W5 가 healthcheck 오탐에서 이미 같은 지적을 했다).
- 조건을 고치고, **"랭킹은 축소 대상이 아니다"** 경로가 실제로 언제 발화해야 하는지
  주석으로 명확히 하라 (RANKING 그룹은 티어 축소로 줄일 수 없으므로 사람이 개입해야 하는 상황).
- 회귀 테스트: 예산 이내일 때 경보가 **나지 않고**, 진짜 초과일 때만 나는지.

## 참고 — 야간 라이브에서 관측된 것 (튜닝 근거, 이번 태스크 범위 밖)
- promotions **17,144건** / events 216건 — churn 이 극심하다.
- tier3 피크 4/20, 진입이 드물다.
- 유니버스 필터 적용 전 데이터라 대형주가 tier2 를 차지했다(네가 이미 고쳤다).
이 수치는 `docs/11_live_rehearsal.md` 와 W5 리포트에 있다. 튜닝은 별도 태스크로 다룬다.

## 불변 규칙
1. 라이브 호출 금지, `api_keys` 읽기 금지.
2. 소유 경로 밖 수정 금지. 완료 시 `git diff --name-only $(git merge-base main HEAD)..HEAD` 로 증명.
3. `main` 직접 커밋 금지. 보고 전 `git log` 로 커밋 확인.
4. `worker_done` 정확히 1회, 보고 (a)~(e).
