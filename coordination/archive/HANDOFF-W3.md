# HANDOFF — W3 (analyzer)

> 오케스트레이션 런타임 리셋 대비 재개 문서. 작성 2026-07-31.
> 리셋 후 옛 taskId/dispatchId 는 무효다. 이 문서가 유일한 재개 지점이다.

## 0. 신원·위치

| 항목 | 값 |
|---|---|
| 워커 ID | **W3** (feat/analyzer, 터미널 `term_a1c5ec49-9cc4-44fa-a7a1-c3d561102442`) |
| 워크트리 | `C:\Users\dongh\orca\workspaces\toss_trade\w3-analyzer` |
| 브랜치 | `feat/analyzer` |
| 현재 HEAD | **이 문서를 추가한 커밋**(브랜치 tip). 그 부모 = **`4869293`** — 여기까지 rebase 동기화했다 |
| 미머지 코드 커밋 | **0건.** 분석 코드는 전부 main 에 들어가 있다 (아래 2절). 내 브랜치에 남은 것은 이 문서 1개뿐 |
| main 이 앞서 있을 수 있음 | 다른 워커들이 동시에 handoff 를 커밋 중이다. 재개 시 `git rebase main` 먼저 하라 (이 문서는 단일 파일 추가라 충돌 없음) |
| 소유 경로 | `tossmon/analysis/**`, `tools/report.py`, `tests/synth.py`, `tests/test_analysis*.py`, `docs/07_analysis_spec.md` |
| 워크트리 상태 | **clean** (이 문서 커밋 외 미커밋 작업 없음) |

## 1. 직전 태스크의 요지

Phase 1 분석 계층 전부를 만드는 일이었다: 검출기 평가용 **합성데이터 생성기**(최우선,
W4 의 전제), 시간대 보정 RVOL·ATR·세션 VWAP·20일 거래량 z 의 **베이스라인**,
docs/03 §3 의 **이벤트 라벨링**, **T0 이전 구간만 쓰는 전조 피처**(룩어헤드 금지를
테스트로 증명할 것), 검증질문 6개에 1:1 대응하는 **평가 함수 + 마크다운 리포트**,
그리고 각 지표의 정의식·경계조건·편향을 수식 수준으로 적은 **docs/07**.
제약: 라이브 리스 없음(mock 전용), 계약 시그니처 무단 변경 금지, 소유 경로 밖 수정 금지.

## 2. 끝난 것 (전부 main 에 머지됨)

| 커밋 | 내용 |
|---|---|
| `290f0c8` | analyzer 4모듈 + report + tools/report.py CLI + tests/synth.py + 테스트 6파일 + docs/07 |
| `b1f24fa` | DB 경유 리포트 경로 보강 (Reader 생성자 예외, `meta_json` 라벨 복원) |
| `203837f` | 위 2건의 main 머지 (게이트 5/5, 소유권 위반 0건) |

산출물 요약:
- **`tests/synth.py`** — `make_scenario(kind, seed) -> (df_1m, truth)`. 7종:
  `coil_pop|instant|fade|dump|noise|daymarket|halt_gap`. 이력 3일 + 이벤트 당일 + 다음날,
  4세션 1분봉, 랭킹 스냅샷(2 type), 캔들 공백/홀트 재현. `make_dataset`(다심볼 번들),
  `make_calendar`, `make_history_1d`, `drop_random_bars`.
- **`baselines.py`** — 세션 누적 RVOL(시간대 보정), ATR(SMA), 세션 VWAP, 로그공간 일봉 z.
- **`labeling.py`** — 이벤트 검출 + A1 §4 라벨 전부(`EVENT_COLUMNS`).
- **`features.py`** — A1 §1 엄격 컷오프 + `include_t0` 플래그, A2 §3 first_print/no_print
  프록시, 토스 쏠림도, 이력 피처. 키 집합은 `feature_names()` 로 고정.
- **`evaluate.py`** — `q1`~`q6` + `base_rate_comparison` + `run_all`.
- **`report.py` / `tools/report.py`** — 의존성 없는 마크다운 렌더러 + CLI.
- **`docs/07_analysis_spec.md`** — 정의식·경계조건 + **알려진 편향 15건 표**(§7).

계약 준수: `ask` 6건 → **C-7 개정 A1** 로 정본화(전부 승인 + 조건 3건 이행).
확장은 전부 **키워드 전용 + 기본값 None**, 위치인자 시그니처 100% 불변.

## 3. 다음에 할 일 (재개 지점 — 구체적)

**내 코드는 `docs/12_preregistration.md` 보다 먼저 작성됐다.** 사전등록이 정의를 얼리면서
내 구현과 어긋나는 지점이 3곳 생겼다. **이것이 W3 재개 시 최우선 작업이다.**
(전부 확인 완료. 추측 아님.)

### 3-1. [P0] RVOL 곡선에 20일 창 + 최소 관측일 규칙 (사전등록 §2.2)
사전등록: 평가일 D 에 대해 **D 이전(엄격히 과거) 최근 20 매매일**로만 곡선을 구성하고,
**해당 (세션,분)의 관측일이 10일 미만이면 NaN → 게이트 미가용 → `rvol_gated=False`**.

현재 `baselines.minute_of_session_volume_curve` 는 넘겨준 calendar 를 전부 쓰고
`day_cnt >= 1` 이면 값을 낸다 (`baselines.py:211` 부근). **20일 창도, 최소 10일 규칙도 없다.**
→ 키워드 전용 인자 추가: `window_days: int = 20`, `min_days: int = 10`.
`min_days` 미달 키는 곡선·누적곡선 모두에서 NaN 으로 두고, `rvol_series` 가 NaN 을 그대로
전파하면 `detect_events` 의 게이트가 자동으로 미충족 처리된다(이미 그렇게 동작).
테스트: 관측 9일 → NaN → 이벤트가 `rvol_gated=False` 로 표시되는지.

### 3-2. [P0] `gap_from_prev_close` 가 원주가 × 수정주가를 섞는다 (사전등록 §2.3 위반)
`features.py:381-384` 는 `close_cut`(1분봉 = **원주가**)를
`baseline["close_last_u"]`(일봉 = **수정주가**)로 나눈다. 사전등록 §2.3 은
**"두 계열 간 가격 수준 직접 비교 금지"** 를 명시한다. 분할이 낀 종목에서 이 피처는
가짜 갭을 만든다.
→ 전일 종가는 **직전 매매일 정규장 마지막 1분봉 종가(원주가)** 를 키워드로 받아서 쓰도록
바꾼다(예: `prev_close_u: int | None = None`). baseline 은 ATR%·거래량 z 등 **비율 지표**
용도로만 남긴다. 못 받으면 NaN.

### 3-3. [P0] `detect_events` 의 전일종가 대체가 금지된 방식이다 (사전등록 §2.3)
`labeling.py:217` 은 최종 대체로 **당일 첫 시가**를 쓴다. 사전등록은
**"당일 첫 시가 대체는 쓰지 않는다"**(갭업 구조적 과소평가) 이며, 대신
**직전 매매일 1분봉이 아예 없으면 당일 조건(+30%)을 판정하지 않고 윈도우 조건만으로
검출하되 `kind='win'`** 으로 두라고 규정한다.
→ 대체 사슬을 `prev_close_u` → 직전 매매일 **정규장** 마지막 봉 → 직전 매매일 마지막 봉 →
**(없으면 당일 조건 스킵)** 으로 바꾼다. 현재 2단계(`labeling.py:215`)는 직전 봉을 쓰는데
그게 애프터마켓 봉일 수 있어 "정규장 마지막 종가" 규정과 미세하게 다르다. 같이 정리할 것.

### 3-4. [P1] 분석 표본 유니버스 필터 (사전등록 §2.7)
T0 시점 기준 **보통주/ACTIVE, 종가 ∈ [$0.10, $20], 시총 ∈ [$10M, $300M]** 필터가 주 분석
표본 조건인데 내 모듈에는 없다. 제외 시 **사유별 카운트 보고**가 요구된다.
→ `evaluate.py` 에 `apply_sample_filter(events, meta) -> (kept, reasons_df)` 를 두고
`run_all` 이 그 카운트를 리포트 섹션으로 내보내는 형태를 권장. 심볼 메타는 W2 `Reader.symbols()`.

### 3-5. [P2] 그 밖
- 사전등록 §7-f 의 **봉 타임스탬프 시작/끝 감도 규칙** 반영 (아래 5번 미해결 항목).
- 사전등록 §2.6: `half_peak` 은 **어떤 판정·선택에도 쓰지 않는다** — 현재도 보고 전용이지만,
  q5 의 버킷 선택 로직이 향후 이를 참조하지 않도록 주석/테스트로 못 박아둘 것.

## 4. 막힌 것 / 코디네이터 답 대기 중이던 것

**없다.** 직전 태스크는 완료·머지됐고 대기 중인 질문은 없다.
(과거 `ask` 6건은 전부 A1 으로 답을 받아 반영 완료.)

## 5. 다음 사람이 모르면 손해 보는 사실

### 함정 (실제로 밟은 것들)
1. **`git checkout <ref> -- <path>` 는 인덱스에 스테이징까지 한다.** 다른 워커 산출물을
   로컬 참조용으로 가져오면 그대로 커밋되어 **소유권 위반**이 된다. 가져왔으면
   `git reset` + `git checkout -- <path>` + `git clean -fd <dir>` 로 반드시 되돌려라.
   (`tools/mock_server.py` 가 조용히 남아 있어 한 번 걸렸다.)
2. **가격×수량 누적은 int64 를 넘는다.** 봉당 ~6e16, 390봉이면 ~2e19 > 9.22e18.
   pandas/numpy 정수 누적은 **조용히 랩어라운드**한다. VWAP 류는 Python 임의정밀도 int 로
   누적할 것 (`baselines.session_vwap_u` 참고). 회귀테스트 있음.
3. **누적 RVOL 은 세션 상대량이라 세션을 넘어 비교하면 안 된다.** 얇은 세션(day/after)은
   분모가 작아 허위 돌파가 잦고, 세션 밖까지 스캔하면 리드타임 중앙값이 896분으로 붕괴한다.
   세션 경계를 넘는 측정은 봉 단위 z(`vol_surge_lead_min`)로 하라.
4. **RVOL 곡선의 분모에 평가일을 넣지 마라**(자기오염 → RVOL 이 1 쪽으로 축소).
   `curve = minute_of_session_volume_curve(df, truth['baseline_calendar'])` 후
   `rvol_series(df, curve, calendar=truth['calendar'])` 가 정석. `exclude_dates` 도 있다.
5. **C-6 `events` 테이블은 C-7 7컬럼 + `meta_json` 뿐이다.** A1 §4 추가 라벨 18개를
   `meta_json` 에 넣지 않으면 DB 경유 리포트에서 q6·기저율 대조가 **통째로 빈다**.
   `report.expand_meta_json()` 이 되돌려 펼친다.
6. **W2 `Reader` 는 생성자에서 커넥션을 연다** → DB 부재 시 메서드 호출 전에 예외.
   `generate_report` 는 생성자까지 `_safe` 로 감싸 '데이터 없음' 리포트를 쓴다.

### 환경
- venv: `.venv` (repo 안). 설치: `.venv\Scripts\pip install -e ".[dev]"`. pandas **3.0.5**.
- **콘솔 인코딩이 cp949 다.** 한글/em-dash 를 stdout 으로 print 하면 `UnicodeEncodeError`.
  일회성 스크립트는 `sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')`.
  파일 입출력은 항상 `encoding="utf-8"` 명시.
- `tests/fixtures/live/` 가 비면 `test_analysis_fixtures.py` 는 **skip** 하도록 설계돼 있다
  (픽스처는 W1 소유). 픽스처 있으면 15개가 실제로 돈다.
- **라이브 금지**: `TOSS_LIVE=0`, `TOSS_BASE_URL=http://127.0.0.1:8899`(mock)만.
  리스는 현재 아무도 안 갖고 있다. 토큰 발급·`openapi.tossinvest.com` 호출 금지.

### 설계상 알아둘 것
- `coil_score` **부호가 반직관적**이다: 양수 = 수축 진행, **강한 음수 = 방금 폭발 시작**
  (실측 coil_pop −2.6 vs noise +0.1). docs/07 §4.4 에 규약 명시.
- `rvol_curve_5` 같은 짧은 윈도우 피처는 분모가 작아 100배를 쉽게 넘는다.
  절대 스케일 말고 **순위/구간**으로 써라.
- **즉발형(instant)은 원리상 전조 탐지가 불가능하다.** T0 이전 RVOL 1.2(정상), T0 봉에서만
  7.5. q1 실측 precision 1.0 / recall 0.8 이고 놓친 2건이 정확히 instant 2건이다.
  이건 버그가 아니라 결과다(La Morgia 정합).
- 라벨 의미: `ret_30m`/`ret_close` = **진입(T0 종가) 기준**, `retrace_*` = **피크 기준**,
  '종가' = 정규장 마지막 봉. `kind` = 트리거 종류(win/day/both), 형태 = `shape`,
  결과 = `outcome`.
- 리포트 실행: `python tools/report.py --db data/tossmon.db --out ops/report.md --days 30`.

## 6. 테스트 현재 상태

HEAD `4869293` 기준, 이 워크트리에서 실측:

```
.venv\Scripts\python -m pytest -q
  → 694 passed in 337s (5분 37초)        # 전체, 실패 0, skip 0

.venv\Scripts\python -m pytest tests\test_analysis_synth.py tests\test_analysis_baselines.py ^
  tests\test_analysis_labeling.py tests\test_analysis_features.py ^
  tests\test_analysis_evaluate.py tests\test_analysis_fixtures.py -q
  → 207 passed in 66s                     # W3 소유분
```

**실패 0건.** 커밋 시점에 실패 중인 테스트는 없다.

## 7. 미해결 리스크 (docs/07 §7 편향표 15건 중 상위 3)

1. **봉 타임스탬프가 봉의 시작인지 끝인지 미확정**(docs/07 §7 편향 6). RVOL 은 일괄 이동이라
   영향 없지만 '개장 15분' 버킷 경계와 리드타임이 ±1분 흔들린다. 사전등록 §7-f 가 감도 규칙으로
   처리하도록 규정했으니 재개 시 그 규칙을 구현할 것. W1 실측으로 확정되면 분 위치 계산
   한 곳만 고치면 된다.
2. **랭킹은 과거 조회 불가**(계약 A2 §4) → 검증질문 2는 **실시간 수집 기간에만** 답할 수 있다.
   현재 q2 결론은 전부 합성데이터 기반이고, 백필 이벤트의 랭킹 라벨은 전부 NaN 이 된다.
   `q2_ranking_lead_lag` 는 빈 랭킹을 정상 경로로 처리한다(`available=False`, 예외 금지).
3. **`sharesOutstanding` 은 발행주식수이지 float 이 아니다** → `float_rotation` 이 구조적으로
   과소평가되고, 저플로트 러너(핵심 타깃)에서 괴리가 가장 크다. '1.0 초과 = 평단 리셋' 같은
   문헌 임계값을 그대로 쓰면 신호를 놓친다. 외부 float 소스 확보 전까지 해석 보류 권고.
   사전등록 §2.7 도 이 한계를 명시하고 "그대로 보고"하라고 규정했다.
