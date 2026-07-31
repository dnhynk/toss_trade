# W4 보고 — 감사 후속 2건 (task_6a6bc5a53927)

브랜치 `w4-collector`, 커밋 **a5f86fa** (main 병합 커밋 162c70b 직상단, rebase 완료. 머지 안 함).
mock 전용 (`TOSS_LIVE=0`), 라이브 호출·토큰 발급·`api_keys` 접근 0건.

## (a) 무엇을 했나
1. **ForbiddenEndpoint 미처리 [치명]**: 실제 증상은 "루프가 죽는다"가 아니라 더 나쁜 쪽이었다 —
   `_guarded` 의 광역 `except Exception` 이 A6 이후의 `ForbiddenEndpoint` 를 warn 한 줄로
   삼키고 **수집을 계속**했다 (조용한 allowlist 위반, stash 검증에서 5사이클 연속 재호출 관측).
   catch-all **앞에** 명시적 `except ForbiddenEndpoint` 를 두어 `forbidden_endpoint` 카운터 +
   `ctx.shutdown()`(내부에서 alert 발신)으로 수집 전체를 세운다. 광역 except 는 다른 예외용으로
   유지 — A6 의도(잡지 말 것의 타입 강제)는 이 한 곳의 의도적 종결 처리로만 만난다.
2. **RANKING 가짜 ERROR [높음]**: 원인은 비교식이 아니라 **진입 경로와 문구의 불일치 + 미소거**.
   `should_shrink` 의 축소 불가 그룹 분기는 (i) 실제 초과 또는 (ii) 429(forced) 로 진입하는데,
   어느 쪽이든 "predicted X > target Y" 문구를 냈다 — 429 로 진입하면 X<Y 인 채로 거짓 ERROR.
   게다가 forced 는 SHRINK_TIER 그룹에서만 소거되어 RANKING 은 **매 사이클 반복** 경보였다.
   수정: 실제 초과면 기존 초과 문구, 429 뿐이면 "429 on RANKING (usage X/Y, 예산 이내) —
   한도 인식이 틀렸을 수 있다" 문구로 **1회만** 경보하고 forced 소거. 발화 조건(사람 개입이
   필요한 두 상황)을 주석으로 명시.

## (b) 검증
- 회귀 테스트 3건 신규: `test_forbidden_endpoint_stops_every_loop_with_an_alert`
  (중단+경보+재호출 없음+loop_errors 0), `test_ranking_within_budget_never_raises_a_false_alert`,
  `test_429_on_ranking_alerts_the_429_once_not_a_false_overrun` (문구 검증 + 반복 도배 금지).
  기존 `test_ranking_is_never_shrunk_only_alerted` 는 진짜 초과 문구 단언으로 강화.
- `git stash` 로 수정 전 코드에 돌려 핵심 2건 실패 확인 (거짓 경보 429 경로, ForbiddenEndpoint 삼킴).
- 전체 스위트 **707 passed** (main 기준 704 + 신규 3). 리셋 전 핸드오프에 적었던
  test_api_tokens 4건 실패는 새 main 에서 해소 확인 (rebase 직후 704 green).

## (c) 이견·판단
- 재현 불가·이견 없음. 관측된 "predicted 0.33 > target 3.50" 은 야간 중 RANKING 그룹으로
  귀속된 429(또는 고수위 귀속 오차)가 forced 를 세운 것으로 설명된다 — 예산 이내였으므로
  "한도 인식이 틀렸을 수 있다" 경보가 맞는 내용이고, 이제 그렇게 말한다.
- 429 자체는 여전히 `rate_limited` 카운터·warn 으로 기록된다 — 정보 손실 없음.

## (d) 파일·커밋
- **a5f86fa** 단일 커밋. `git diff --name-only $(git merge-base main HEAD)..HEAD`:
  `tossmon/collector/{loops,budget}.py`, `tests/test_collector_{loops,budget}.py` — 전부 소유 경로.

## (e) 남은 것
- 머지는 코디네이터 몫. 튜닝(promotions 17,144건 churn, tier3 4/20)은 범위 밖 — 별도 태스크 대기.
- 참고: 429 의 그룹 귀속은 client 전역 카운터 고수위 방식이라 드물게 이웃 그룹으로 붙을 수 있다
  (보수적 방향). 정밀 귀속이 필요해지면 W1 의 client 가 그룹별 429 카운터를 내주는 것이 정도다.
