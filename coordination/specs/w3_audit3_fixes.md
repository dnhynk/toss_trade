# W3 — 3차 감사 수정: blocker 2건 + 동반 fixture + 권고 3건 (분석 착수 전 마지막 수정)

너는 W3(`tossmon/analysis/**`, `tools/report.py`, `tests/synth.py`, `tests/test_analysis*.py`,
`docs/07_analysis_spec.md` 소유자)다. W6 3차 감사(`docs/14_audit_prereg_align.md`, main 머지됨)가
네 정합 코드에서 치명 2건을 재현 실측으로 확인했다. **감사 문서 §I·§II·§IV·§VI(재현 스크립트
전문 포함)를 정독하고 그대로 집행하라.** 감사와 이견이 있으면 고치지 말고 재현 증거와 함께 (c)에.

**라이브 호출 금지**(`TOSS_LIVE=0`, mock). W5 라이브 수집이 아직 진행 중 — 토큰 발급 금지,
**W5 DB 접근 금지.**

## 시작 절차

`git rebase main` 먼저 (main 에 docs/14 머지됨).

## 작업

1. **[blocker] F-1 — 일봉 as-of 앵커 룩어헤드** (감사 §I F-1): `prereg_daily_baseline` 의
   일봉 후보 선정을 ms 부등호가 아니라 **매매일 date 단위**로 바꿔라 — 일봉 ts 의 ET 날짜를
   날짜 키로 해석해 `bar_date < 평가일 매매일 date` 로 거른다. day=None 매매일에서 평가일
   자기 일봉이 통과하던 결함 해소. 감사 권고대로 **(date, calendar) 서명 도입**을 검토하라
   (raw ms 오전달 실수 차단) — C-7 위치인자 불변 원칙 안에서 키워드 확장으로.
   (W7 이 §2.2 에 같은 취지 문언을 병행 추인 중이다 — 코디네이터 결정 사항이므로 기다리지
   말고 구현하라.)
2. **[blocker] F-2 — 분할일 오등록** (감사 §I F-2): 점프가 결측 구간을 넘어 관측되면
   **(마지막 관측 매매일, 관측 매매일] 구간의 모든 매매일을 분할일로 등록**하라(보수적 확대 —
   코디네이터 결정, W7 문언 추인 병행 중). 넓힌 등록일은 `split_excluded`(scope=`symbol_day`)
   로 자연히 계상된다. **r 미계산 (심볼, 매매일) 건수**도 카운트로 노출하라(§7-e 보고 의무).
   트리거 (b)(day=None 매매일의 00:00 ET 일봉이 버킷팅에서 버려짐)는 F-1 의 date-단위
   매핑으로 함께 해소하라 (`split_ratio_series._bucket`).
3. **[blocker 동반] T-1/T-2 — fixture 실규약화** (감사 §IV): 분할·as-of 테스트의 일봉 ts 를
   실규약(`regular.start_ms − 570분` = 00:00 ET)으로 바꾸고, **day=None 매매일 케이스**와
   **분할일 자신의 일봉 결측 케이스**를 추가하라. 감사 §VI repro1·repro2 를 회귀 테스트로
   옮기면 된다. 수정 전 소스에서 실패함을 stash 로 증명하라.
4. **[권고 — 이번에 고친다] F-3**: `detect_events` 에서 `split_dates is not None and
   calendar is None` 이면 **raise**. `extract_precursor_features` 의 같은 계열(무음 스킵)도
   정리. 거짓 `split_dates_applied=True` 가 나올 수 없게.
5. **[권고 — 이번에 고친다] F-4**: 다일 프레임(매매일 스팬 2개 이상)에서 스칼라/심볼-dict
   `prev_close_u` 가 오면 **raise** (또는 `{(symbol, date): value}` 수용). docstring 의
   구버전 사슬 설명(F-8)도 같이 정정.
6. **[권고 — 이번에 고친다] F-5**: `run_all` 에서 `curve is None` 이면 반일장 행을
   `n=NaN` + `(half_day_check_not_run)` provenance 행(scope=`provenance`)으로.
7. `docs/07_analysis_spec.md` 에 바뀐 정의(일봉 date-단위 소속, 분할 보수 등록 규칙) 반영.

## 불변 규칙

1. 라이브 호출·`api_keys`·W5 DB 금지. 소유 경로 밖 수정 금지 (merge-base diff 증명).
2. C-7 위치인자 시그니처 불변, 확장은 키워드 전용 + 기본값.
3. `main` 직접 커밋 금지. `feat/analyzer` 에만 커밋. 보고 전 `git log` 확인.
4. 전체 pytest green. `worker_done` 1회, 보고 (a)~(e), 이후 idle.
5. 콘솔 비ASCII 금지(cp949). 파일 입출력 `encoding="utf-8"`. 보고문 셸 인자에 백틱 금지 —
   **그리고 `worker_done` 본문은 반드시 ASCII 로만 써라** (W6 의 한글 본문이 인코딩 깨짐으로
   도착한 사고가 방금 있었다. 국문 상세는 보고 파일에 쓰고 본문은 ASCII 요약으로).
