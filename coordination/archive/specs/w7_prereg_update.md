# W7 — 사전등록 갱신: M-3/M-4 해소 반영 + 계약 A6/A7 대조 (§9 개정 창)

너는 W7(`docs/12_preregistration.md` 소유자)다. **오케스트레이션이 리셋됐다** — 옛 ID는 무효.
네 재개 문서는 `coordination/HANDOFF-W7.md`다. 직전 태스크(사전등록 최초 작성)는 머지 완료.

**라이브 호출 금지, `api_keys` 금지, W5 DB·수집 로그·집계 열람 금지** (W5가 라이브 리스로
재수집 중이고, 너는 §0.1 "데이터 무열람" 상태를 유지해야 문서의 신뢰 근거가 산다).
이 태스크는 §9의 **"데이터 열람 전" 개정 창** 안에서 하는 문서 작업이며,
**코디네이터 승인은 이 태스크 자체가 그 승인이다.** §1 사전 예측 소급 수정 금지.

## 시작 절차

1. `git rebase main` (main = `6def9b5`, 749 passed·1 skipped). 브랜치 `w7-prereg` 그대로.
   `git branch -m` 금지(Windows 파일 락).
2. 참조는 읽기만: `docs/07_analysis_spec.md`(W3 갱신분, 곡선 §2.4 정의),
   `docs/04_contracts.md`(개정 A6·A7).

## 배경 — 왜 지금

W3가 M-3(겨울 UTC 매매일 분할)·M-4(반일장 곡선 오염)를 수정해 main에 머지했다(`c6f1886`).
그 수정이 docs/12의 확정 정의와 두 곳에서 상호작용하고, 분석 착수(§8 단계 3) 전인 지금이
§9 개정이 가능한 마지막 창이다.

## 작업 (수정 대상은 `docs/12_preregistration.md` 하나뿐)

1. **§2.2 한 문장 추가** — 시간대 보정 곡선은 `(세션, 세션길이 session_len_min, 분위치)`
   3단으로 색인하며, 기존 "관측일 10일 미만 → NaN → `rvol_gated=False`" 규칙은
   **(세션, 길이) 버킷별로** 적용된다. (구현: `baselines.minute_of_session_volume_curve`,
   `min_days` 키워드 — 주 분석은 10을 명시로 넘겨야 한다.)
2. **§2.7 제외 사유 추가** — **"반일장(세션 길이 표본 부족)"**. 반일장 당일 이벤트는
   `min_days=10` 하에서 자동으로 `rvol_gated=False`가 되어 주 분석에서 빠진다.
   제외 건수 출처는 `curve.attrs["dropped_below_min_days"]`.
3. **M-3/M-4 해소 사실 기록** — 배치는 네 판단(§7에 항목을 추가해 '해소' 표기하든,
   개정 이력에 기록하든). 요지:
   - M-3: `_history_features`가 `UsMarketDay` span 기준으로 매매일을 묶는다 →
     §2.5("세션 판정은 `/market-calendar/US`만")의 정신이 이력 피처까지 확장됨.
     val 구간(2026-01-01~04-30)·train 후반(겨울)의 `hist_days_available`·former-runner
     프록시 오염이 제거됐다.
   - M-4: 곡선 3단 색인 + `min_days`로 해소. 캘린더 없는 폴백은
     `day_grouping_calendar=0.0` + `RuntimeWarning`으로 드러난다. 주의: 이건 **피처**지
     라벨이 아니다(§2.4 라벨 얼림과 무충돌) — 다만 docs/12 어딘가가 피처 키 집합을
     얼렸다면 그 절도 함께 §9로 개정해야 한다. 확인하라.
4. **계약 A6/A7 대조** (HANDOFF-W7 네가 스스로 적은 권장 첫 작업) —
   `docs/04_contracts.md`의 개정 A6(토큰 리스 자격증명 단위·ForbiddenEndpoint·CAS·limiter)와
   A7(KR 엔드포인트 제거·상태파일 권한)이 docs/12가 얼린 값(C-7 기본값, A2 §4, §2 확정
   정의)과 충돌하는지 판정하고, 결과를 개정 이력에 한 줄로 남겨라(충돌 없으면 "충돌 없음
   확인"). **충돌이 있으면 고치지 말고 `ask`로 물어라.**
5. §9 규정대로 **개정 이력 표에 일자·사유·변경 전후를 추가**하라.

## 불변 규칙

1. 수정 대상은 `docs/12_preregistration.md`뿐. 완료 시
   `git diff --name-only $(git merge-base main HEAD)..HEAD`로 증명.
2. §1 사전 예측 소급 수정 금지. DB·수집 로그·집계 열람 금지.
3. 라이브 호출·`api_keys` 읽기 금지.
4. `main` 직접 커밋·머지 금지. `w7-prereg`에만 커밋. 보고 전 `git log` 확인.
5. `worker_done` 정확히 1회, 보고 (a)~(e): (a) diff 요약 (b) 자기 검증 방법
   (c) A6/A7 충돌 판정 결과 (d) 다음 사람이 알아야 할 사실 (e) 미해결 리스크.
6. 콘솔에 비ASCII 출력 금지(cp949). 파일 입출력은 `encoding="utf-8"` 명시.
