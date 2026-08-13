# 14 — 적대적 감사 3차: 사전등록 정합 코드 (분석 착수 전 최종)

> **[기록]** 소유 W6 · 2026-08-01 · 감사 3 차 (사전등록 정합)
> 상태 표기의 뜻과 전수 목록: [`docs/INDEX.md`](INDEX.md)

> 작성: 2026-08-01 (KST), W6. 대상: 2026-07-31 머지 커밋 `14d0b8c`(W3 1차 정합),
> `fa6b45a`(W3 2차: 분할·as-of·밴드), `dbeec4e`(W2 U-4). 정본 대조:
> `docs/12_preregistration.md`(개정 이력 4행 포함 최신판), `docs/07_analysis_spec.md`.
> 라이브 호출 0, mock/합성 전용. W5 수집 DB 미접근. 리포 수정은 본 문서 1개뿐.
>
> **결론 요약**: 치명 2건(F-1 일봉 as-of 앵커 룩어헤드, F-2 분할일 오등록) — **분석
> 착수 전 수정 필수(blocker)**. 중간 3건(F-3, F-4, F-5)은 분석 러너 작성 규율로 회피
> 가능하나 이번에 고치는 편이 싸다. 낮음 4건. 이전 감사 지적 사항(U-4)의 수정은
> 실측으로 유효 확인. 나머지 오늘 머지분의 사전등록 정합은 §V의 확인 목록대로 건전하다.

## 0. 방법과 재현 환경

- 모든 발견은 **실행 가능한 재현 스크립트**로 확인했다(관찰만으로 보고한 항목 없음).
  스크립트 전문은 본 문서에 인라인으로 포함한다(§VI). 리포 파일 수정 금지 원칙 때문에
  스크립트 자체는 커밋하지 않았다.
- 실행 환경 함정 2건(전역 site-packages 의 `tests` 패키지가 리포 `tests/` 를 가림,
  `filelock` 미설치)은 `coordination/HANDOFF-W6.md` §1 그대로다. §VI 의 부트스트랩이
  둘 다 우회한다.
- 전체 스위트 기준선: HEAD(= main `adb5821` 동등 코드)에서 795 passed / 1 skipped 확인
  (샌드박스, `PYTHONPATH` 로 filelock 주입).

핵심 실측 전제 2개 (본 감사의 발견 대부분이 이 둘의 교차점에서 나왔다):

1. **일봉 캔들 ts = 13:00 KST 고정 = 00:00 ET** (docs/06 §2-2, line 109). 일봉은
   "미국 현지 자정 기준 날짜 키"다.
2. **매매일의 첫 세션(day)은 ET 기준 전날 20:00 에 시작**한다 (docs/01 §5 실측 정정:
   day 09:00–17:00 KST). 즉 매매일 D 의 스팬은 ET 로 [20:00 D−1, ~19:50 D) 이고,
   D 의 일봉 ts(00:00 ET D)는 **day 세션이 있을 때만** 스팬 안에 있다.
   `UsMarketDay.day` 는 `None` 이 허용된다(`api/models.py:246`,
   `api/client.py:333-336` — `dayMarket` 노드 부재 시 None). 한국 휴일(설날·추석·
   어린이날 등)에 미국장이 열리는 날이 실재하는 후보다.

---

## I. 치명 (blocker — 분석 착수 전 수정 필수)

### F-1. 일봉 베이스라인 as-of 앵커가 day 세션 결측일에 평가일 자기 일봉을 통과시킨다

- **심각도**: 치명. **소유**: W3 (`tossmon/analysis/baselines.py:78-161`,
  `prereg_daily_baseline`), 문언 측면은 W7(§2.2 앵커 정의).
- **사전등록 문언** (§2.2, 2026-07-31 보강·추인): "후보는 **평가일 D 기준 엄격히
  과거의 일봉만**이며 … **앵커는 평가일 매매일의 시작(첫 세션 시작)이다** … 평가일의
  데이마켓·프리마켓도 미래로 취급한다."
- **현상**: 구현은 `df[df["ts_ms"] < as_of_ms]` 의 **ms 비교**다
  (`baselines.py:124`). 실데이터에서 D 의 일봉 ts 는 00:00 ET D 인데,
  - day 세션이 있는 정상일: 앵커(첫 세션 시작) = 20:00 ET D−1 < 00:00 ET D → D 자기
    일봉이 정상 배제된다. ✓
  - **day 세션이 None 인 매매일**(한국 휴일 등): 앵커 = pre 시작 = 04:00 ET D >
    00:00 ET D → **평가일 자기 일봉(그날 전체의 종가·거래량·고저 — 명백한 미래
    데이터)이 "엄격히 과거" 필터를 통과**한다. `as_of_applied=True` 라 표식으로도
    안 잡힌다.
- **오염 범위**: 그 날짜의 모든 이벤트에서 `atr20_pct`·`daily_vol_z`·`adv20_qu`·
  `ret_std` 가 평가일 자신을 포함해 계산된다. 이벤트일은 정의상 폭등일이므로 자기
  거래량이 분모 통계에 들어가 z 가 구조적으로 눌린다(자기오염의 정확히 그 형태).
  §6.1 훈련 구간(2025-09~2025-12)에 추석 연휴, val 구간에 설날 연휴가 들어 있어
  발생 일수는 연 10일 안팎으로 추정된다.
- **재현** (§VI repro1 — 21 매매일, 실제 일봉 ts 규약, 평가일 일봉만 극단 변조):

  ```text
  day_session_missing=False as_of_applied=True n_days=20 immune_to_eval_day_poison=True
  day_session_missing=True  as_of_applied=True n_days=20 immune_to_eval_day_poison=False
    LOOKAHEAD: eval-day daily bar entered the baseline
    adv20_qu clean=11500000 poisoned=60450000  atr20_pct clean=0.0000 poisoned=0.0490
  ```

- **권고**: 일봉 후보 선정을 ms 부등호가 아니라 **매매일 date 단위**로 바꿔라 —
  일봉 ts 를 날짜 키로 해석해(00:00 ET = ts 의 ET 날짜) `bar_date < as_of_date` 로
  거르거나, 최소한 앵커를 `min(첫 세션 시작, 00:00 ET of D)` 로 클램프. 회귀 테스트는
  **일봉 ts 를 실규약(00:00 ET)으로 두고 `day=None` 매매일**을 포함해야 한다(§IV-1
  fixture 결함 참조). 아울러 `prereg_daily_baseline` 은 raw ms 를 받으므로 호출자가
  t0_ms 를 넘기는 실수(그러면 **모든** 날에서 D 자기 일봉이 통과)를 막을 수 없다 —
  (date, calendar) 서명으로 바꾸는 것을 권고한다.
- **전제의 한계**: 실제 API 가 한국 휴일에 `dayMarket=None` 을 주는지는 라이브 미실측
  (모델·파서는 허용, 캘린더에 그 날 day 윈도우가 채워져 오면 미발현). 분석 착수 전
  해당 날짜 1건이라도 `/market-calendar/US?date=` 로 실측 확인하라 — 확인 전까지는
  blocker 로 취급한다. 단 F-2 의 트리거 (a)는 캘린더와 무관하게 성립한다.

### F-2. r 결측일을 사이에 둔 분할이 **다음 계산 가능일**에 등록된다 — 가드를 넘긴 채 가짜 갭 이벤트가 카탈로그에 들어간다

- **심각도**: 치명. **소유**: W3 (`baselines.py:397-424 detect_split_dates`).
- **사전등록 문언** (§7-e): "매매일 경계에서 r 이 직전 매매일 대비 1.5배 이상(또는
  1/1.5 이하)으로 급변하면 **그 매매일을** 분할일로 등록한다" / "결측일 건너뛰기(다음
  계산 가능일을 마지막 관측 r 과 비교)로 **완화**되지만 제거되지 않는다" /
  "`split_dates` 없이 돌린 검출 결과는 주 분석에 쓸 수 없다 — 가짜 갭 이벤트가
  카탈로그를 직접 오염시키기 때문이다."
- **현상**: 분할 유효일 D 의 r 을 계산할 수 없으면(아래 트리거), 점프는 D+1 에서
  관측되고 구현은 **점프를 관측한 날(D+1)을** 분할일로 등록한다. 그 결과
  `split_dates` 를 **성실히 전달해도**:
  - D: 당일 조건이 제외되지 않는다 → D−1 전일종가(분할 전 스케일) 대 D 원주가
    (분할 후 스케일) → **+900% 가짜 `kind='day'` 이벤트가 카탈로그에 잔류**.
  - D+1: 당일 조건이 억울하게 제외된다(실해는 없음 — D 대비 갭은 ~0이다).
  즉 "결측 건너뛰기 완화 장치"는 분할 발생 **사실**은 잡지만 보호가 필요한 날을
  보호하지 못한다. §7-e 가 이미 인정한 "장기 결측 사각(한계 2)"과 다른 결함이다 —
  이것은 놓침이 아니라 **오등록**이며, 결측 1일이면 충분히 발생한다.
- **트리거** (재현 확인 2종):
  - (a) 분할일 자신의 **일봉 1개 결측** — 캘린더 정상이어도 발생. 1m/1d 보관 깊이
    차이(docs/11 §1)·백필 실패 1건으로 현실적으로 생긴다.
  - (b) 분할일이 **day 세션 결측 매매일** — F-1 과 같은 근본 원인: 00:00 ET 일봉 ts 가
    어느 매매일 스팬에도 속하지 않아 `split_ratio_series._bucket` 이 조용히 버린다
    (`baselines.py:366-368`). 일봉이 존재해도 r 행이 사라진다.
- **재현** (§VI repro2):

  ```text
  A. daily ts=regular_start(테스트 fixture 규약), 전체 존재  -> {'2026-06-04'} OK
  B. daily ts=00:00 ET(실규약), 전체 존재                    -> {'2026-06-04'} OK
  C. 실규약 + 분할일 일봉 1개 결측  -> {'2026-06-05'} 등록, 06-04 에 kind='day' 1건 잔류
  D. 실규약 + 분할일 day=None      -> {'2026-06-05'} 등록, 06-04 에 kind='day' 1건 잔류
  ```

- **권고**: 점프가 결측 구간을 넘어 관측되면 **(마지막 관측일, 관측일] 구간의 모든
  매매일을 분할일로 등록**하라(보수적 — 구간 내 어느 날이 진짜 분할일인지 원리상
  알 수 없다). §7-e 의 "그 매매일" 문언은 결측 하에서 다의적이므로 W7 이 이 해석을
  문언으로 추인해야 한다(§9 절차, 데이터 열람 전). (b)는 F-1 의 date-단위 버킷팅
  수정(일봉 ts 의 ET 날짜 = 매매일 date 매핑)으로 함께 해소된다.
- 보고 의무 연동: 이렇게 넓힌 등록일은 `split_excluded`(scope=`symbol_day`) 카운트를
  키운다 — §7-e 의 "r 미계산 (심볼, 매매일) 건수 보고" 항목과 함께 내면 사각의 크기가
  숫자로 드러난다.

---

## II. 중간 (분석 러너 규율로 회피 가능 — 단 이번에 고치는 편이 싸다)

### F-3. `split_dates` 를 넘겨도 `calendar` 가 없으면 조용히 무위 — provenance 는 "적용됨"으로 남는다

- **심각도**: 중간. **소유**: W3 (`labeling.py:257-259, 277`, `features.py:237-243`).
- **문언**: §7-e "`split_dates` 없이 돌린 검출 결과는 주 분석에 쓸 수 없다" — 이
  강제는 `events.attrs["split_dates_applied"]` 표식에 의존한다(구현 커밋 메시지도
  "Provenance is enforced" 라고 명시).
- **현상**: `detect_events(df, params, split_dates=..., calendar=None)` 이면 매매일
  스팬의 `md` 가 전부 None 이라 `md.date in sym_splits` 가 영원히 거짓 — 제외 0건.
  그런데 `split_dates_applied = split_dates is not None = True`. **가짜 갭 이벤트가
  남았는데 리포트 표식은 "적용됨"**이다. `extract_precursor_features` 도 동일 계열:
  calendar 없으면 `cutoff_day=None` → `gap_from_prev_close` NaN 가드가 무음 스킵된다
  (이쪽은 M-3 의 `day_grouping_calendar=0.0` 경고가 간접 신호로는 남는다).
- **재현** (§VI repro3): calendar 유 → 이벤트 0·`split_excluded=1`; calendar 무 →
  `kind='day'` 1건 잔류·`split_excluded=0`·`applied=True`. features 는 calendar 무 시
  가짜 갭 +900% 반환.
- **권고**: `detect_events` 에서 `split_dates is not None and calendar is None` 이면
  raise 하거나 `split_dates_applied=False` 로 강등하라. 주 분석 경로는 어차피
  calendar 필수(§2.5)라 정상 경로 영향 없음.

### F-4. 명시 `prev_close_u` 가 다일 프레임의 **모든 매매일**에 적용된다

- **심각도**: 중간 (백필 다일 분석에서의 오용 함정). **소유**: W3
  (`labeling.py:232-234, 253-255`).
- **문언**: §2.3 "전일 종가 C_prev: **직전 매매일의** 정규장 마지막 1분봉 종가(원주가)
  를 명시 인자로 넘긴다" — 정의상 (심볼, 평가일)별 값이다.
- **현상**: 인자 형태가 스칼라/`{symbol: value}` 뿐이라, 다일 프레임에서 넘기면 같은
  값이 **모든 날**의 당일 조건 기준이 된다(사슬보다 우선). 라이브 1일 운용을 위한
  설계가 백필 다일 경로에 그대로 노출돼 있다.
- **재현** (§VI repro4): 3 매매일(종가 0.99 → 1.00 → 1.29). 사슬: 이벤트 0(3일차
  +29% < 30%). 스칼라 `prev_close_u=990000` 전달: 3일차에 +30.3% **조작 이벤트 1건**.
- **권고**: `{(symbol, date): value}` 형태를 받거나, 다일 프레임(스팬 2개 이상)에서
  스칼라/심볼-dict 가 오면 raise. 최소한 docstring 에 "다일 백필에서는 넘기지 말 것 —
  사슬이 정본" 을 명시하라. 아울러 `labeling.py:202-204` 의 인자 설명이 아직 구버전
  ("그것도 없으면 당일 첫 봉 시가")이다 — 실제 구현은 금지된 대체를 제거했으므로
  문서만 낡았다(F-8 과 함께 정리).

### F-5. `half_day_length_sample` — "검사 안 함"이 "0건"으로 보인다

- **심각도**: 중간 (리포트 정직성 — 같은 커밋이 만든 `(filter_not_applied)` 규약과
  자기모순). **소유**: W3 (`evaluate.py:676-680, run_all`).
- **문언**: §2.7 "이 사유의 이벤트 제외 건수를 다른 사유와 같은 형식(사유별 카운트)
  으로 보고한다" + 구현 자신의 규약 "'not run' is never confused with 'zero excluded'"
  (커밋 `14d0b8c` 메시지).
- **현상**: 반일장 검사는 `curve`(와 그 안의 `dropped_below_min_days`)가 전달될 때만
  실행된다. `run_all(meta=...)` 를 `curve=` 없이 부르면 그 행이 **n=0, scope=event**
  로 나간다 — 제외 0건과 구분 불가. 재현 (§VI repro5): `n=0`, provenance 행 없음.
- **권고**: `curve is None` 이면 해당 행을 `n=NaN` + `(half_day_check_not_run)`
  provenance 행(scope=`provenance`)으로 내라. `(split_dates_not_applied)` 와 같은 꼴.

---

## III. 낮음

### F-6. mcap ±10% 밴드는 "시총 검사에 도달한 이벤트"만 센다

- **소유**: W3 (`evaluate.py:689-699`). **문언**: §2.7 "밴드에 든 이벤트 건수를 —
  필터에 걸린 쪽과 통과한 쪽 **양쪽 모두** — 별도 보고".
- 가격/증권종류/메타 사유로 **먼저** 제외된 이벤트는 시총이 밴드 안이어도 밴드 행에
  안 잡힌다(사유 판정이 단락 평가라 mcap 자체가 계산되지 않음). 재현 (§VI repro7-D):
  가격 $25(제외) × 시총 $10.5M → `mcap_band_low_excluded=0`.
- 감도 보고의 목적(경계 오분류가 표본을 바꾸는가)에는 이 조건화가 오히려 맞다는
  해석도 성립한다 — 가격에서 이미 죽은 이벤트는 경계가 움직여도 표본에 못 들어온다.
  **권고**: 코드 수정 대신 리포트 각주로 "밴드 카운트는 시총 검사 도달 이벤트 조건부"
  를 명시(W3, 리포트 생성 시). 수정한다면 사유 판정과 독립적으로 mcap 을 계산하라.

### F-7. `split_excluded` 는 당일 조건이 원래 판정 불가였던 날도 센다

- **소유**: W3 (`labeling.py:257-259`). §2.7 scope 문언은 "당일 조건이 제외된
  (심볼, 매매일) 수". 첫 매매일(전일 종가 자체가 없음)이 분할일이면 — 제외할 당일
  조건이 애초에 없는데도 +1 된다. 재현 (§VI repro7-E). 카운트 부풀림은 보고 통계
  해석에만 영향(±수 건). 리포트 각주로 충분.

### F-8. `detect_events` docstring 의 전일 종가 사슬 설명이 구현과 다르다

- **소유**: W3 (`labeling.py:202-204`). "없으면 df 내 이전 봉의 마지막 종가, 그것도
  없으면 당일 첫 봉 시가" — **금지된 대체가 살아 있는 것처럼 읽힌다.** 실제 구현
  (248-252 주석과 코드)은 §2.3 사슬 그대로이고 첫 시가 대체는 없다. 문서만 수정.

### F-9. `min_days` 기본값 1 로 도는 프로덕션 경로 — 라이브 detector 한정, 주 분석 경로에는 없음

- 전수 조사 결과: `minute_of_session_volume_curve` 의 프로덕션 호출은
  `collector/detector.py:734 build_curve`(라이브 W4) 단 1곳이며 `min_days` 미지정
  (=1), `window_days` 는 오늘 커밋으로 기본 20 이 됐다. 주 분석은
  `prereg_volume_curve`(20/10 강제)만 쓰면 되고, 그 외 경로는 테스트뿐이다.
  라이브 함의: 반일장 당일 히스토리에 같은 길이 관측이 없으면 그 버킷이 아예 없어
  RVOL=NaN → 게이트 미가용으로 흐른다(감지 공백이지 오염은 아님). **수정 불요,
  W4 인지 사항**. 참고: `build_curve` 의 분모가 오늘부터 최근 20 매매일로 잘리는
  동작 변화가 라이브에 조용히 들어갔다 — 의도된 정합으로 판단하나 W4 가 알아야 한다.

### (범위 밖 관찰) 라이브 `_refresh_baseline` 의 prev_close 는 수정주가 일봉이다

- `collector/loops.py:1334` 가 `ctx.prev_close[symbol]` 에 **일봉(수정주가) 종가**를
  넣는다. 라이브 검출기의 당일 조건이 원주가 1m 대 수정주가 1d 비교가 된다 — §2.3 이
  금지한 혼합의 라이브 판이다. 오늘 감사 대상 커밋 밖(기존 코드)이고, `events` 테이블은
  §7-a 에 의해 텔레메트리 전용이라 **주 분석 오염은 없다**. 분할 당일 라이브 허위
  알림 요인이므로 W4 백로그에 기록만 남긴다.

---

## IV. 테스트 결함 (틀린 이유로 통과 — 남은 것)

### T-1. 분할·as-of 테스트의 일봉 ts 가 실규약과 다르다 (`regular.start_ms` ≠ 00:00 ET)

`tests/test_analysis_split.py::_split_fixture`(line 81)와 `_daily_frame`(line 234)은
일봉 ts 를 **정규장 시작**에 둔다. 실규약은 00:00 ET(docs/06 §2-2)다. 정규장 시작은
어떤 매매일 구성에서도 스팬 안·앵커 뒤라서, 이 fixture 로는 F-1·F-2(b)가 **원리상
검출 불가능**하다 — 테스트가 "일봉 ts 는 항상 스팬 안에 있고 앵커 뒤에 있다"는 참이
아닌 전제를 심어 놓고 통과한다. 수정 시 일봉 ts 는 반드시 `regular.start_ms − 570분`
(= 00:00 ET)으로 생성하고, `day=None` 매매일 케이스를 추가하라.

### T-2. `test_missing_day_is_skipped_and_compared_to_last_observed_r` 는 결측을 분할 **전날**에만 둔다

결측이 분할 전날(06-03)이면 점프가 진짜 분할일(06-04)에서 관측돼 통과한다. 결함이
드러나는 배치는 **분할일 자신(06-04)의 결측**인데(→ 06-05 오등록, F-2), 그 케이스가
없다. W3 이 직전에 스스로 잡은 fixture 결함 2건과 같은 계열의 사각이다.

### T-3. U-4 회귀 테스트는 건전 (검증 완료)

`test_universe.py` 의 신규 2개는 구현 전 코드에서 실제로 실패하는 구성임을 확인했다
(§V-6: 구코드에서 sentinel 이 `args`/bytes-args/체인으로 샜다). 4포인트 체크리스트
(str/traceback/doc·msg·args/`__context__`·`__cause__`)도 재귀 없이 충분하다 —
둘 다 None 을 요구하므로 체인이 존재할 수 없다.

---

## V. 동작 확인된 것 (다음 감사가 재검하지 않아도 되는 목록)

전부 §VI 재현 스크립트 또는 신규 테스트 + 코드 정독으로 확인했다.

1. **곡선 20일 창 / 엄격 과거 / 자기오염 면역** (§2.2): 평가일 D 의 **전 세션 봉**
   (전 ET 날짜에 걸친 day 세션 포함)을 극단 변조해도 곡선·누적곡선이 불변
   (repro7-A). 분모는 정확히 D 이전 최근 20 매매일(repro7-B). `exclude_dates` 는
   창 슬롯을 소모하지 않는다(코드 `_window_dates` 순서 확인). `as_of_date` 문자열
   비교는 ISO 날짜라 사전순=시간순으로 안전.
2. **min_days 버킷 드롭과 3단 색인 소비처 일치** (§2.2/M-4): `(세션, 길이)` 버킷별
   드롭이 (session, len, minute) 전 분에 균일하게 적용됨(day_cnt 가 버킷 내 상수임을
   코드로 확인). 반일장에서 `rvol`/`rvol_bar`/`rvol_series`/features `rvol_curve_w`
   가 전부 (session, **210**, minute) 키로 일치(repro7-C). 2단 색인 잔존 접근 없음
   (`curve.loc`/`cum.loc` 전 호출처 grep — 모두 `curve_key` 경유). `curve_locate` 만
   쓰는 곳은 세션명·시작만 쓰므로 길이 무관.
3. **전일 종가 대체 사슬** (§2.3): ①명시 인자 ②직전 매매일 정규장 마지막 봉 ③그날
   마지막 봉 ④당일 조건 스킵 — 구현·테스트 정합. 당일 첫 시가 대체 소멸 확인
   (`test_day_condition_skipped_when_no_prior_day_bars` + 코드). 직전 매매일에 봉이
   전혀 없으면 더 과거로 거슬러 가지 않는 것도 문언 그대로("직전 매매일 1분봉이 아예
   없으면 판정하지 않고"). `hod_ret` 도 전일 종가 부재 시 NaN.
4. **가격 계열 분리** (§2.3/A5): `gap_from_prev_close` 는 명시 원주가 인자 없으면
   NaN, `baseline["close_last_u"]`(수정주가)로의 폴백 없음. baseline 은 비율 지표
   (`atr20_pct`·`daily_vol_z`)에만 사용.
5. **§2.7 표본 필터**: 가격 [$0.10, $20.00]·시총 [$10M, $300M] 양끝 포함(코드 `<=`
   양방향 + 경계 테스트), 단위 일관(마이크로달러 × 마이크로주 ÷ 1e6 검산), 밴드
   9M~11M/270M~330M 양끝 포함, 판정은 확정 경계로만(밴드 무관 — 테스트 확인),
   scope 컬럼 4종 분리, 제외 0건 사유도 행 유지, `(filter_not_applied)`/
   `(split_dates_not_applied)` provenance 규약(F-5 의 반일장 항목만 예외).
6. **U-4 절단 실효성** (dbeec4e): 구코드 실측 — sentinel 이 `UnicodeDecodeError
   .args`(bytes)와 예외 체인으로 샜다. 신코드 실측 — 동일 입력에서 4포인트 전부
   클린. 추가 프로브: NUL 바이트(파이썬 3.13 csv 는 NUL 을 관용 — 예외 경로 자체가
   없음), 빈 파일(`no header` ValueError, ctx None), sentinel 이 든 헤더(비노출),
   **신선 다운로드 실패 경로**(sentinel 페이로드 → 최종 RuntimeError 문구에 임시
   파일 경로만 노출, 내용·체인 클린). `fetch_symbol_directory` 의 폴백 파싱이 except
   블록 밖으로 나온 구조도 확인(repro6).
7. **q6 ±1분 감도** (§7-f): `n_minus`/`n_plus`/`n_boundary_sensitive`/
   `boundary_sensitive` 의무 병기 확인, NaN `t0_min_from_open` 은 어느 버킷에도
   안 들어감. `half_peak` 은 `DECISION_POLICIES` 제외·`REPORT_ONLY_POLICIES` 등재
   (§2.6).
8. **features 엄격 컷오프** (A1 §1): t0 봉 포함 이후 전체를 변조해도 `include_t0=False`
   피처 전 키 불변(repro7-F). `include_t0=True` 프로덕션 사용처는 라이브 detector
   1곳뿐(합법 — T0 봉 종료 시점 판정).
9. **분할 탐지 정상 경로**: 실규약 일봉 ts(00:00 ET)에서도 **정상 매매일** 구성이면
   r 버킷팅·탐지·전달·`split_excluded`·provenance 모두 정확(repro2-B). 첫 매매일
   비등록, 배당 5% 표류 비검출, 2:1 정분할(×2 점프)·역분할(1/10 점프) 양방향 임계
   1.5/(1/1.5) 대칭, r 결측일 prev 미갱신 — 테스트 + 코드 확인.
10. **detect_events 의 split 처리 순서**: 명시 `prev_close_u` 가 있어도 분할일이면
    당일 조건 제외가 **우선**한다(코드 순서: 사슬 → split 체크가 마지막). features 의
    분할 가드는 컷오프가 전일에 떨어지는 극단(T0=분할일 첫 봉, include_t0=False)에서
    발화하지 않지만, 그 경우 close_cut 도 전일(분할 전) 스케일이라 가짜 갭이 생기지
    않는다 — 스케일 일관성으로 안전.

---

## VI. 재현 스크립트 전문

공통 부트스트랩(환경 함정 우회 — HANDOFF-W6 §1). 아래를 `_boot.py` 로 두고 각
스크립트가 `import _boot` 한다. `AUDIT_ROOT`=리포 루트, `AUDIT_PYLIBS`=
`pip install --target <dir> filelock` 디렉터리.

```python
import os, sys, types
ROOT = os.environ["AUDIT_ROOT"]
_libs = os.environ.get("AUDIT_PYLIBS")
if _libs:
    sys.path.insert(0, _libs)
sys.path.insert(0, ROOT)
_m = types.ModuleType("tests")
_m.__path__ = [os.path.join(ROOT, "tests")]
sys.modules["tests"] = _m
```

### repro1 — F-1 (일봉 as-of 앵커 룩어헤드)

```python
import _boot, dataclasses
import pandas as pd
from tests import synth
from tossmon.analysis import baselines as B
MIN_MS = 60_000

def daily_frame(cal, poison_last=False):
    rows = []
    for i, md in enumerate(cal):
        et_mid = md.regular.start_ms - 570 * MIN_MS   # 00:00 ET (docs/06 실규약)
        close, vol = 1_000_000, 1_000_000 * (i + 1)
        if poison_last and i == len(cal) - 1:
            close, vol = 50_000_000, 1_000_000_000
        rows.append({"symbol": "D", "ts_ms": et_mid, "open_u": close,
                     "high_u": close, "low_u": close, "close_u": close, "vol_qu": vol})
    return pd.DataFrame(rows).astype({"ts_ms": "int64"})

def run_case(drop_day_session):
    cal = synth.make_calendar(21, start="2026-06-01")
    ev = cal[-1]
    if drop_day_session:
        ev = dataclasses.replace(ev, day=None)
        cal = cal[:-1] + [ev]
    anchor = synth.session_windows(ev)[0][1].start_ms   # 매매일 시작 = 사전등록 앵커
    clean = B.prereg_daily_baseline(daily_frame(cal, False), anchor)
    poisoned = B.prereg_daily_baseline(daily_frame(cal, True), anchor)
    print(drop_day_session, clean == poisoned, clean["adv20_qu"], poisoned["adv20_qu"])

run_case(False)   # True  (면역)
run_case(True)    # False (평가일 자기 일봉이 들어감 = 룩어헤드)
```

### repro2 — F-2 (분할일 오등록, 트리거 2종)

```python
import _boot, dataclasses
import pandas as pd
from tests import synth
from tossmon.analysis import baselines as B, labeling as L
MIN_MS = 60_000; RATIO = 10; SPLIT = "2026-06-04"

def fixture(ts_mode, drop_split_daily=False, dayless=False):
    cal = synth.make_calendar(5, start="2026-06-01")
    if dayless:
        cal = [dataclasses.replace(md, day=None) if md.date == SPLIT else md
               for md in cal]
    m1, d1, raw, adj = [], [], 1_000_000, 1_000_000
    for md in cal:
        if md.date == SPLIT:
            raw *= RATIO
        for m in range(5):
            m1.append({"symbol": "SP", "ts_ms": md.regular.start_ms + m * MIN_MS,
                       "open_u": raw, "high_u": raw, "low_u": raw,
                       "close_u": raw, "vol_qu": 1_000_000})
        if drop_split_daily and md.date == SPLIT:
            continue
        ts = (md.regular.start_ms if ts_mode == "regular_start"
              else md.regular.start_ms - 570 * MIN_MS)
        d1.append({"symbol": "SP", "ts_ms": ts, "open_u": adj, "high_u": adj,
                   "low_u": adj, "close_u": adj, "vol_qu": 5_000_000})
    c = {"ts_ms": "int64", "open_u": "int64", "high_u": "int64",
         "low_u": "int64", "close_u": "int64", "vol_qu": "int64"}
    return pd.DataFrame(m1).astype(c), pd.DataFrame(d1).astype(c), cal

for tag, kw in [("A", dict(ts_mode="regular_start")),
                ("B", dict(ts_mode="et_midnight")),
                ("C", dict(ts_mode="et_midnight", drop_split_daily=True)),
                ("D", dict(ts_mode="et_midnight", dayless=True))]:
    df_1m, df_1d, cal = fixture(**kw)
    splits = B.detect_split_dates(df_1m, df_1d, cal)
    ev = L.detect_events(df_1m, L.EventParams(), calendar=cal, split_dates=splits)
    print(tag, sorted(splits), len(ev[ev["kind"].isin(["day", "both"])]))
# A/B: {'2026-06-04'} 0   C/D: {'2026-06-05'} 1  <- 가짜 갭 이벤트 잔류
```

### repro3 — F-3 (calendar=None 시 무위 + 거짓 provenance)

```python
import _boot
import pandas as pd
from tests import synth
from tossmon.analysis import labeling as L, features as F
MIN_MS = 60_000
cal = synth.make_calendar(2, start="2026-06-01")
rows = [{"symbol": "SP", "ts_ms": cal[0].regular.start_ms + m * MIN_MS,
         "open_u": 1_000_000, "high_u": 1_000_000, "low_u": 1_000_000,
         "close_u": 1_000_000, "vol_qu": 1_000_000} for m in range(5)]
rows += [{"symbol": "SP", "ts_ms": cal[1].regular.start_ms + m * MIN_MS,
          "open_u": 10_000_000, "high_u": 10_000_000, "low_u": 10_000_000,
          "close_u": 10_000_000, "vol_qu": 1_000_000} for m in range(5)]
df = pd.DataFrame(rows).astype({"ts_ms": "int64"})
splits = {"2026-06-02"}
a = L.detect_events(df, L.EventParams(), calendar=cal, split_dates=splits)
b = L.detect_events(df, L.EventParams(), split_dates=splits)
print(len(a), a.attrs["split_excluded"], a.attrs["split_dates_applied"])  # 0 1 True
print(len(b), b.attrs["split_excluded"], b.attrs["split_dates_applied"])  # 1 0 True!
t0 = cal[1].regular.start_ms + 4 * MIN_MS
f = F.extract_precursor_features(df, pd.DataFrame(), t0, symbol="SP",
                                 prev_close_u=1_000_000, split_dates=splits)
print(f["gap_from_prev_close"])   # 9.0 (+900% 가짜 갭 — 가드 무음 스킵)
```

### repro4 — F-4 (명시 prev_close_u 의 다일 전파)

```python
import _boot
import pandas as pd
from tests import synth
from tossmon.analysis import labeling as L
MIN_MS = 60_000
cal = synth.make_calendar(3, start="2026-06-01")
def day(md, c):
    return [{"symbol": "S", "ts_ms": md.regular.start_ms + m * MIN_MS, "open_u": c,
             "high_u": c, "low_u": c, "close_u": c, "vol_qu": 1_000_000}
            for m in range(5)]
df = pd.DataFrame(day(cal[0], 990_000) + day(cal[1], 1_000_000)
                  + day(cal[2], 1_290_000)).astype({"ts_ms": "int64"})
print(len(L.detect_events(df, L.EventParams(), calendar=cal)))                    # 0
print(len(L.detect_events(df, L.EventParams(), calendar=cal,
                          prev_close_u=990_000)))                                 # 1 (조작)
```

### repro5 — F-5 (반일장 사유의 모호한 0)

```python
import _boot
import pandas as pd
from tossmon.analysis import evaluate as E
MICRO = 1_000_000; t = 1_780_000_000_000
events = pd.DataFrame([{"symbol": "HD", "t0_ms": t, "kind": "win", "peak_ms": t,
                        "peak_ret": 0.2, "ret_30m": 0.1, "ret_close": 0.0,
                        "session": "regular", "rvol_gated": True}])
df_1m = pd.DataFrame([{"symbol": "HD", "ts_ms": t, "open_u": 5 * MICRO,
                       "high_u": 5 * MICRO, "low_u": 5 * MICRO,
                       "close_u": 5 * MICRO, "vol_qu": 1}]).astype({"ts_ms": "int64"})
meta = pd.DataFrame([{"symbol": "HD", "security_type": "STOCK", "status": "ACTIVE",
                      "is_common": 1, "shares_outstanding_qu": 10_000_000 * MICRO}])
sf = E.run_all(events, pd.DataFrame(), pd.DataFrame(), df_1m,
               meta=meta)["sample_filter"]          # curve= 미전달
print(sf[sf["reason"] == "half_day_length_sample"][["n", "scope"]])  # n=0, event
```

### repro6 — U-4 실측 (구코드 유출 증명 + 신코드 4포인트 + 대체 경로 4종)

`git show dbeec4e^:tossmon/universe/seed.py > old_seed.py` 후, sentinel 이 든 깨진
인코딩 파일(`"Symbol|Name\nSENTINEL|x\n" UTF-8 + b"\xff\xfe" + sentinel bytes`)로
구/신 `parse_directory_file` 을 호출해 str/traceback/doc·msg·args/bytes-args/
`__context__`·`__cause__`/체인 전체 repr 을 검사한다. 추가로 NUL 파일, 빈 파일,
sentinel 헤더 파일, `urlopen` 을 sentinel 페이로드 반환으로 바꾼
`fetch_symbol_directory` 신선 다운로드 실패 경로를 같은 검사기로 돌린다. 결과는
§V-6. (스크립트 전문은 분량상 생략 — 검사 함수는 `test_universe.py::
_assert_no_leak_in_chain` 의 확장으로, 체인 전체 str+repr(args) 스캔을 추가한 것.)

### repro7 — 확인 목록 검증 (§V-1·2·8 + F-6·F-7)

25 매매일 곡선 면역/창 크기, 반일장 12일 캘린더에서 `rvol`/`rvol_bar`/`rvol_series`
(210 길이 키) 일치, 가격 제외 × 밴드 내 시총 이벤트의 밴드 행 0, 첫 매매일 분할
카운트, `coil_pop` 시나리오에서 t0 이후 전체 변조 → 피처 전 키 불변. 출력:

```text
A. curve immune to eval-day poison (incl. day session on prev ET date): True
B. days in denominator = 20, mean at ('regular', 390, 0) = 14500000
C. half-day key=('regular', 210, 100) rvol=1.000 rvol_bar=1.000 rvol_series=1.000 agree=True
D. price-excluded event with mcap $10.5M: band_low_excluded=0 (blind: True)
E. first day (no prev close ever) on split date: split_excluded=1
F. include_t0=False: features differing after t0-and-later poison: []
```

---

## VII. 내일 아침 디스패치를 위한 분류

| 항목 | 분류 | 소유 | 비고 |
|---|---|---|---|
| F-1 일봉 as-of date-단위 비교 | **blocker** | W3 | 실캘린더 `dayMarket=None` 실측 1건 병행(W4/W5 협조) |
| F-2 분할 결측 구간 보수 등록 | **blocker** | W3 | §7-e "그 매매일" 해석 추인 필요(W7, §9 절차) |
| T-1/T-2 fixture 실규약화 + 케이스 추가 | **blocker 동반** | W3 | F-1/F-2 수정의 회귀 테스트 전제 |
| F-3 calendar 없는 split_dates raise | 권고(분석 전) | W3 | 러너가 calendar 필수를 지키면 무해 |
| F-4 prev_close_u 다일 가드 | 권고(분석 전) | W3 | 러너가 사슬만 쓰면 무해 |
| F-5 반일장 provenance 행 | 권고(분석 전) | W3 | 러너가 curve 를 항상 넘기면 무해 |
| F-6/F-7 리포트 각주 | 나중 가능 | W3 | 리포트 생성 시점에 |
| F-8 docstring 정정 | 나중 가능 | W3 | 문서만 |
| F-9 build_curve 동작 변화 | 나중 가능 | W4 | 인지 + 반일장 라이브 감지 공백 기록 |
| loops.py prev_close 수정주가 | 나중 가능 | W4 | 텔레메트리 한정, 분석 무영향 |

"권고(분석 전)" 3건을 코드로 안 고칠 경우, Phase 1-C 러너 스펙에 다음 3줄을 강제
조항으로 넣어야 한다: ① `detect_events`/`extract_precursor_features` 는 항상
`calendar=` 전달, ② 다일 백필에서 `prev_close_u` 명시 인자 금지(사슬 사용), ③
`run_all` 에 `curve=`/`calendar=` 항상 전달.
