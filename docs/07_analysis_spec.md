# 07 — 분석 지표 명세 (정의식·경계조건·알려진 편향)

> 소유: W3. 구현: `tossmon/analysis/{baselines,labeling,features,evaluate,report}.py`,
> 합성 생성기 `tests/synth.py`.
> 근거 문서: `docs/04_contracts.md`(C-7 + **개정 A1**, C-6/C-8 **개정 A2**),
> `docs/03_phase1_monitor_design.md` §3, `docs/02_theory_background.md` §1·§2.4·§4.
> **이 문서는 코드와 1:1로 대응한다.** 정의를 바꾸면 여기와 테스트를 함께 바꾼다.

기호
: $t$ — 1분봉의 타임스탬프(UTC epoch ms, 봉 시작). $C_t,O_t,H_t,L_t$ — 종가/시가/고가/저가
  (마이크로달러 `int`). $V_t$ — 거래량(마이크로주 `int`). $S(t)$ — $t$ 가 속한 세션.
  $m(t)$ — 세션 시작 이후 경과 분수. $\mathcal{D}(t)$ — $t$ 가 속한 매매일.

---

## 1. 전역 규약과 수치적 함정

### 1.1 단위
- 시간은 전부 UTC epoch ms `int` (계약 C-1). 표시용 변환조차 분석 레이어에서 하지 않는다.
- 가격·금액은 마이크로달러 `int`, 수량은 마이크로주 `int` (계약 C-2).
- **비율(RVOL, 수익률, z-score, 쏠림도)만 `float`** 로 계산한다. 가격 자체를 float 로
  경유하는 연산은 금지.

### 1.2 int64 오버플로 (실제로 밟은 함정)
VWAP 의 분자는 봉당 $tp_u \cdot V_t \approx 4\times10^6 \times 1.5\times10^{10} = 6\times10^{16}$
이고, 정규장 390봉을 누적하면 $\approx 2\times10^{19}$ 로 **int64 상한
$9.22\times10^{18}$ 을 넘는다**. numpy/pandas 의 정수 누적은 조용히 랩어라운드하므로
음수 VWAP 같은 값이 태연히 나온다.

→ **규칙**: 가격 × 수량의 누적은 반드시 Python 임의정밀도 `int` 로 한다
(`itertools.accumulate` + 리스트, `Series.cumsum()` 금지).
적용 위치: `baselines.session_vwap_u`, `tests/synth._vwap_last_u`.
회귀 테스트: `test_analysis_baselines.py::test_session_vwap_no_int64_overflow`.

### 1.3 입력 정렬
`/candles` 응답은 **최신순(newest-first)** 이다 (W1 실측). 순서에 의존하는 함수
(`true_range_u`, `session_vwap_u`, `detect_events`)는 내부에서 `sort_values("ts_ms")` 를
직접 수행한다. 회귀 테스트: `test_analysis_fixtures.py::test_*_order_independent`.

### 1.4 결측(캔들 공백)의 3가지 의미
API 는 **체결이 없는 분에 봉을 주지 않는다.** 공백의 원인은 구분 불가하며 셋 다 있다:
(a) 미체결(저유동성), (b) 거래정지/LULD 홀트, (c) 수집기 중단.
- **분 단위 지표**에서는 공백 = 거래량 0 으로 취급한다.
- **베이스라인 평균**에서는 `(날짜, 세션)` 전체가 비면 그 세션을 제외한다. 그러지 않으면
  분모가 작아져 RVOL 이 구조적으로 부풀려진다(§2.4).
- 길이 $\ge$ `halt_gap_min`(기본 5분)인 정규장 내 공백만 **홀트 프록시**로 센다(§3.7).

---

## 2. 베이스라인 (`baselines.py`)

### 2.1 일봉 베이스라인 `compute_daily_baseline(df_1d, window_days=20)`
마지막 $N=20$ 개 일봉(정렬 후)만 사용. $V_i>0$ 인 날만 거래량 통계에 넣는다.

$$\text{adv20\_qu} = \left\lfloor \frac{1}{N}\sum_{i} V_i \right\rfloor,\qquad
\mu_V=\frac1N\sum V_i,\quad \sigma_V=\sqrt{\frac{1}{N-1}\sum (V_i-\mu_V)^2}$$

$$\mu_{\ln V}=\frac1N\sum \ln V_i,\qquad \sigma_{\ln V}=\sqrt{\frac{1}{N-1}\sum(\ln V_i-\mu_{\ln V})^2}$$

$$\text{ret\_std}=\operatorname{sd}\left(\ln \frac{C_i}{C_{i-1}}\right)\ (\text{ddof}=1)$$

경계조건: 빈 입력 → `n_days=0` + 나머지 `None`/NaN (예외 없음 — 수천 종목 루프에서
한 종목 때문에 죽지 않게). 표본 1개 → 표준편차 NaN. 다심볼 입력 → `ValueError`.

### 2.2 ATR `atr_u(df, n=20)`
$$TR_i=\max\big(H_i-L_i,\ |H_i-C_{i-1}|,\ |L_i-C_{i-1}|\big),\qquad TR_0=H_0-L_0$$
$$\text{ATR}(n)=\left\lfloor \frac1n\sum_{i=k-n+1}^{k} TR_i \right\rfloor$$

**단순이동평균(SMA)이며 Wilder 평활이 아니다.** 값이 다르므로 외부 차트와 비교할 때 주의.
$\text{atr20\_pct} = \text{ATR}(20)/C_{\text{last}}$.

### 2.3 일봉 거래량 z-score `daily_volume_z`
**기본은 로그 공간**:
$$z_{\ln}=\frac{\ln V_{\text{today}}-\mu_{\ln V}}{\sigma_{\ln V}}$$

이유: 거래량은 강한 우편향(대략 로그정규)이다. 원공간 z 는 $\sigma_V$ 가 소수의 스파이크
일에 끌려 커지므로 "$z\ge3$" 임계가 사실상 도달 불가가 되고, 반대로 조용한 종목에서는
과민해진다. `log=False` 로 원공간도 계산 가능하되 문헌 임계값(Kamps 균형 세트의
"거래량 3~4배")과의 대응은 로그 공간에서 본다.

$V\le0$ 또는 $\sigma$ 미정의 → NaN.

### 2.4 시간대 보정 곡선 `minute_of_session_volume_curve(df_1m, calendar, *, exclude_dates)`
키는 **`(세션명, 세션 시작 이후 경과분)`** 이며 절대 시각이 아니다. 따라서 서머타임 전환,
조기폐장(반나절), 세션 길이 차이에 자동 대응한다.

$$\bar V(s,m)=\frac{1}{|D_{s,m}|}\sum_{d\in D_{s,m}} V_{d,s,m},\qquad
\overline{CV}(s,m)=\frac{1}{|D_{s,m}|}\sum_{d\in D_{s,m}} \sum_{j\le m} V_{d,s,j}$$

- $D_{s,m}$ = 그 세션·분 위치가 **관측 가능했던 날**의 집합. 조기폐장일은 뒤쪽 $m$ 에
  기여하지 않으므로 분모가 자동으로 줄어든다.
- 봉이 없는 분은 $V=0$ 으로 **포함**한다.
- $(d,s)$ 전체가 비면 그 세션은 $D$ 에서 **제외**한다(수집 중단 보정).
- `exclude_dates` 로 지정한 날짜는 평균에서 빠지되 세션 윈도우는 `attrs["sessions"]` 에
  등록된다 — 계약 C-7 의 `rvol(df, curve, ts_ms)` 가 calendar 를 받지 않으므로 세션 판정
  정보는 곡선이 운반한다.

> **분모 자기오염 (중요)**: 평가 대상일을 분모에 넣으면 그날의 폭증이 기대값을 끌어올려
> RVOL 이 1 쪽으로 축소된다. 연구 경로는 반드시 `exclude_dates` 또는 이력일만으로
> 곡선을 만들고, 적용 시 `rvol_series(..., calendar=오늘)` 로 세션을 넘겨준다.

### 2.5 RVOL
**기본 정의는 세션 누적** (계약 A1 §5):
$$\mathrm{RVOL}(t)=\frac{\sum_{u\in[\,\text{sess\_start},\,t\,]} V_u}{\overline{CV}\big(S(t),m(t)\big)}$$

근거: docs/02 §4.1 의 임계값(1.5x 주목 / 2x 진입 최소 / 3~5x 스캐너)은 전부 **장중 누적**
기준 지표다. 단일 분봉 기준은 `rvol_bar` 로 별도 제공:
$\mathrm{RVOL}_{bar}(t)=V_t/\bar V(S(t),m(t))$.

경계조건: 세션 밖 $t$ → NaN. 기대값 0 또는 미관측 → NaN (0 나눗셈 금지).
`rvol_series` 는 세션이 바뀔 때 누적을 리셋하며, 세션 탐색은 이진탐색이라 1024일
백필에서도 $O(n\log n)$.

> **세션 간 비교 금지**: 누적 RVOL 은 세션 상대량이므로 서로 다른 세션의 값을 한 분포에
> 넣으면 안 된다. 얇은 세션(day/after)은 분모가 작아 분산이 크고 허위 돌파가 잦다.
> 그래서 리드타임(§5.3)은 **T0 세션 내부**로 제한하고, 세션을 넘는 측정은 봉 단위
> 로그거래량 z(`vol_surge_lead_min`)로 한다.

### 2.6 세션 VWAP `session_vwap_u(df_1m, session)`
$$tp_u(t)=\left\lfloor \frac{H_t+L_t+C_t}{3}\right\rfloor,\qquad
\mathrm{VWAP}(t)=\left\lfloor\frac{\sum_{u\le t} tp_u(u)V_u}{\sum_{u\le t} V_u}\right\rfloor$$

세션 시작에서 리셋. 누적 거래량이 0 인 선두 구간은 그 봉의 $tp_u$ 를 쓴다.
반환 dtype 은 `int64`(마이크로달러), index 는 `ts_ms`.

> 편향: 기관 볼륨이 없는 초저유동성 종목에서 VWAP 은 소수 체결에 지배되어 의미가 약하다
> (docs/02 §4.1). "VWAP 상실 = 롱 무효화"는 유동성이 어느 정도 있는 종목에서만 유효.

---

## 3. 이벤트 라벨링 (`labeling.py`)

### 3.1 매매일 경계
`calendar` 가 있으면 `UsMarketDay` 의 **첫 세션 시작 ~ 마지막 세션 종료**.
없으면 **UTC 날짜**로 묶는다. 근거: 토스 4세션은 KST 09:00 ~ 다음날 08:50
(= ET 20:00(D-1) ~ 19:50(D) = UTC 00:00 ~ 22:00) 이라 4세션 전체가 하나의 UTC 날짜 안에
들어간다. 이 전제는 W1 의 실제 캘린더 응답으로 검증한다
(`test_analysis_fixtures.py::test_real_calendar_market_day_fits_one_utc_date`).

### 3.2 이벤트 조건 (docs/03 §3, `EventParams` 로 파라미터화)
$$\text{(a) 윈도우 조건:}\quad r^{win}_t=\frac{C_t}{\min\{C_u:\ u\in[t-W,\,t]\}}-1 \ \ge\ r_{\min}$$
$$\text{(b) 당일 조건:}\quad r^{day}_t=\frac{C_t}{C_{\text{prev}}}-1 \ \ge\ r^{day}_{\min}$$
$$\text{(c) 게이트:}\quad \mathrm{RVOL}(t)\ \ge\ \mathrm{RVOL}_{\min}$$

$T_0$ = $\big((a)\lor(b)\big)\land(c)$ 를 **최초로 만족하는 봉**. 기본값
$W=30$분, $r_{\min}=0.15$, $r^{day}_{\min}=0.30$, $\mathrm{RVOL}_{\min}=3$.
`kind` = `both`/`win`/`day` (A1 §3).

- 윈도우는 **닫힌 구간 $[t-W, t]$** 이고 **봉 개수가 아니라 시간**으로 자른다 (공백 내성).
  구현은 pandas 시간 rolling(`closed="both"`), 합성데이터의 참조 구현은 좌측 포인터 스캔 —
  서로 다른 방식이라 교차 검증이 된다.
- 매매일당 최대 `max_per_day`(기본 1)건. 즉 기본은 그날의 **최초** 트리거만 이벤트다.
- 기준가 $C_{\text{prev}}$: 인자 → df 내 직전 봉 종가 → 당일 첫 시가 순으로 대체.
  **마지막 대체는 갭업을 구조적으로 과소평가한다** (당일 시가가 이미 갭업된 값이므로).
  갭업 러너 분석에서는 전일 종가를 반드시 명시로 넘긴다.

### 3.3 RVOL 게이트 미가용 (A1 §6)
곡선이 없으면 예외를 던지지 않고 (a)·(b)만으로 검출하되 `rvol_at_t0=NaN`,
`rvol_gated=False` 로 **명시**한다. `evaluate.py` 는 그런 이벤트를 기본 집계에서 제외한다
(§6.1). 이 규약이 없으면 게이트 미적용 이벤트가 정밀도/재현율을 조용히 오염시킨다.

### 3.4 수익률 라벨 (기준점이 서로 다르므로 혼동 금지)
$T_0$ 종가 $C_0$ 를 진입 참조점으로 본다("$T_0$ 봉을 보고 진입").

| 컬럼 | 정의식 | 기준 |
|---|---|---|
| `peak_ret` | $\max\{H_u:\ u\ge T_0,\ u\in \mathcal{D}\}/C_0-1$ | T0 이후 피크 |
| `hod_ret` | $\max\{H_u:\ u\in\mathcal{D}\}/C_{\text{prev}}-1$ | 매매일 전체 HOD, 전일 종가 대비 |
| `ret_30m` | $C_{T_0+30\text{m}}/C_0-1$ | **진입 기준** (A1 §4 고정) |
| `ret_close` | $C_{\text{close}}/C_0-1$ | 진입 기준, 종가까지 |
| `retrace_30m` | $C_{\text{peak}+30\text{m}}/H_{\text{peak}}-1$ | **피크 기준** 되돌림 |
| `retrace_close` | $C_{\text{close}}/H_{\text{peak}}-1$ | 피크 기준 되돌림 |

- $C_{\text{close}}$("종가") = **정규장 마지막 봉의 종가**. 정규장 체결이 아예 없는 종목
  (데이마켓 전용)은 매매일 마지막 봉으로 대체한다.
- `peak_ms` 는 $T_0$ **이후** 최고가 봉, `hod_ms` 는 매매일 전체 최고가 봉. 프리마켓 T0 처럼
  둘이 다를 수 있다.
- $T_0+30\text{m}$ 지점에 봉이 없으면 그 이전 마지막 봉을 쓴다. 구간에 봉이 하나도 없으면 NaN.

### 3.5 지속시간 `duration_min`
$$\text{duration}=\min\{u-T_0:\ u>T_0,\ C_u\le C_0\}/60000$$
즉 **상승분을 전부 반납하기까지**의 분수. 매매일 끝까지 $C_0$ 위에 머물면 **NaN(우측 절단)**.
0 이나 최대값으로 채우지 않는다 — 그러면 중앙값이 왜곡된다.

### 3.6 그 밖의 라벨
- `session` — `UsMarketDay` 로 판정. calendar 없으면 `"unknown"`.
- `t0_min_from_open` = $(T_0-\text{정규장 개장})/60000$. **개장 전이면 음수** (프리·데이마켓).
- `vwap_close_rel` = $C_{\text{close}}/\mathrm{VWAP}_{\text{close}}-1$,
  `closed_below_vwap` = $C_{\text{close}} < \mathrm{VWAP}_{\text{close}}$ (정규장 VWAP 기준,
  없으면 매매일 전체 VWAP — `ret_close` 의 대체 규칙과 일치시켜야 비율이 의미를 갖는다).
- `float_rotation` = (매매일 누적 거래량) / `shares_outstanding_qu`.
  **`sharesOutstanding` 은 발행주식수이고 유통주식수(float)가 아니다** — API 가 float 을
  제공하지 않으므로(docs/01 §4) 실제 로테이션은 **과소평가**된다. 저플로트 러너에서
  괴리가 가장 크다.
- `ranking_first_entry_ms` / `ranking_lead_lag_min` = (진입 − $T_0$)/60000, **음수 = 선행**.
  `ranking_type` 을 지정하지 않으면 넘겨준 전체에서의 최초 진입.
- `next_day_gap` = (다음 매매일 정규장 첫 시가) / $C_{\text{close}}$ − 1. 정규장↔정규장 기준.

### 3.7 홀트 프록시 `halt_gap_count`
정규장 내 길이 $\ge$ `halt_gap_min`(기본 5분) 캔들 공백의 개수.
> 편향: LULD 홀트, 미체결, 수집 중단을 **구분할 수 없다**. 정규장으로 한정한 이유는
> LULD 가 정규장에만 적용되고 얇은 세션의 공백은 대부분 단순 미체결이기 때문이다.
> 홀트 여부의 신뢰 가능한 확인은 별도 소스가 필요하다(Phase 2).

### 3.8 형태·결과 분류 (`shape` / `outcome`)
경험적 규칙이며 통계적으로 학습된 분류기가 아니다. 임계값은 모듈 상수로 노출.

**`shape`** (우선순위 순):
1. `instant` — $C_{T_0}/O_{T_0}-1 \ge 0.5\,r_{\min}$ (T0 봉 하나가 조건 대부분을 만듦)
2. 관찰 구간 $[T_0-60\text{m},\ T_0-5\text{m})$ 에 봉이 10개 미만 → `mixed`
3. `ramp` — 그 구간의 드리프트 $\ge 5\%$
4. `coil` — 그 구간의 $(\max H-\min L)/C \le 5\%$
5. 그 외 `mixed`

**`outcome`** (우선순위 순):
1. `dump` — 피크 후 30분 내 $\min L/H_{\text{peak}}-1\le-20\%$
2. `fade` — `peak_ret` $>0$ 이고 `ret_close` $< 0.5\times$ `peak_ret`
3. `hold`

---

## 4. 전조 피처 (`features.py`) — 룩어헤드 금지

### 4.1 컷오프 (계약 A1 §1)
$$\text{include\_t0}=\text{False (기본, 연구)}:\ \text{사용 가능 데이터} = \{u: u < T_0\}$$
$$\text{include\_t0}=\text{True (W4 실시간)}:\ \{u: u \le T_0\}$$

근거: $T_0$ 봉의 종가·거래량은 그 분이 **끝난 뒤에만** 관측된다. 리드타임을 연구하려면
$T_0$ 봉을 빼야 하고(엄격), 실시간 검출기는 봉 종료 시점에 판정하므로 봐도 된다.
랭킹도 같은 규칙을 `snap_ms` 에 적용한다.

증명 방식(테스트): ① 미래 봉을 잘라낸 입력과 전체 입력의 결과 dict 가 완전히 동일,
② 미래 구간을 50배/1000배로 변조해도 결과 불변, ③ 미래 랭킹 변조에도 쏠림도 불변,
④ `include_t0=True` 에서도 $T_0$ **이후**는 여전히 무시.

`cutoff_ms` 는 컷오프 조건을 만족하는 **실제로 관측된 마지막 봉**이며,
`cutoff_lag_min` = $(T_0-\text{cutoff})/60000 \ge 1$ (엄격 모드). 공백 때문에 1보다 클 수 있다.

### 4.2 키 집합 불변
반환 키는 `feature_names(windows_min)` 로 고정이며 **입력 가용성과 무관하게 항상 동일**하다
(미가용은 NaN). 하류(`detector.precursor_score`)가 키 존재를 가정할 수 있어야 하기 때문.

### 4.3 거래량 피처 (윈도우 $w\in\{5,15,30,60\}$)
$\mathcal{W}_w=[\,\text{cutoff}-(w-1)\text{m},\ \text{cutoff}\,]$ 의 분 단위 거래량 벡터
(봉 없는 분은 0), $\mathcal{B}_w$ = 그 직전 240분 (`VOL_Z_BASELINE_MIN`).

$$\text{vol\_z}_w=\frac{\overline{\ln(1+V)}\big|_{\mathcal{W}_w}-\overline{\ln(1+V)}\big|_{\mathcal{B}_w}}
{\operatorname{sd}\big(\ln(1+V)\big)\big|_{\mathcal{B}_w}},\qquad
\text{vol\_ratio}_w=\frac{\bar V|_{\mathcal{W}_w}}{\bar V|_{\mathcal{B}_w}}$$

$|\mathcal{B}_w|<30$ 이면 NaN. `log1p` 를 쓰는 이유는 0 거래량 분을 버리지 않기 위함.

$$\text{rvol\_curve}_w=\frac{\sum_{\mathcal{W}_w} V}{\sum_{t\in\mathcal{W}_w}\bar V(S(t),m(t))}$$

`vol_slope_30` = $\ln(1+V)$ 의 최근 30분 OLS 기울기(분당).
`vol_bar_z_max_60` = 최근 60분 봉 중 최대 로그거래량 z (직전 240분 기준).
`rvol_at_cutoff` = §2.5 누적 RVOL.

> 편향: `rvol_curve_5` 처럼 짧은 윈도우는 분모가 작아 값이 쉽게 100배를 넘는다. 스케일이
> 아니라 **순위/구간**으로 쓰는 것이 안전하다.

### 4.4 가격 궤적 피처
`ret_w` = $C_{\text{cutoff}}/C_{\text{cutoff}-w}-1$,
`atr_pct_w` = $\overline{TR}|_{\mathcal{W}_w}/C_{\text{cutoff}}$,
`range_pct_w` = $(\max H-\min L)|_{\mathcal{W}_w}/C_{\text{cutoff}}$.

$$\text{coil\_score}=\ln\frac{\text{atr\_pct}\big|_{[\text{cutoff}-60,\ \text{cutoff}-15)}}
{\text{atr\_pct}\big|_{(\text{cutoff}-15,\ \text{cutoff}]}}$$

**부호 규약**: 양수 = 최근이 조용해짐(수축 진행 중), **강한 음수 = 방금 폭발이 시작됨**.
$T_0$ 직전에서 재면 폭발 구간이 최근 15분에 들어오므로 coil 형이 오히려 큰 음수가 된다
(합성데이터 실측: coil_pop $\approx-2.6$ vs noise $\approx+0.1$). 즉 이 피처는
"코일 여부"가 아니라 **"조용함 대비 최근 확장 정도"** 를 재는 값으로 해석해야 한다.
겹치지 않는 두 구간을 쓰는 이유는 분모·분자가 같은 봉을 공유하면 비율이 1 로 끌려가기 때문.

`nr_ratio` = `range_pct_15`/`range_pct_60`. `dist_from_vwap`, `dist_from_hod`(≤0),
`up_bar_ratio_30`, `new_high_count_30`(최근 30분 세션 신고가 갱신 횟수),
`gap_from_prev_close`(= $C_{\text{cutoff}}/C_{\text{prev}}-1$, baseline 필요).

### 4.5 체결 활동 피처 — `no_print`/`staleness_s`/`first_print` 의 봉 프록시 (A2 §3)
실시간 경로(W4)는 `/prices.timestamp` 파생상태를 직접 쓴다. 과거 백필(연구)에는 그 값이
없으므로 **봉의 부재**로 같은 상태를 복원한다.

- `no_print_ratio_w` = $\mathcal{W}_w$ 중 봉이 없는 분의 비율 ($\approx$ `no_print` 빈도)
- `minutes_since_last_print` = 마지막 두 봉 사이의 공백 분수 ($\approx$ `staleness_s`/60)
- `session_first_print_lead_min` = (세션 첫 체결 − 세션 시작)/60000 ($\approx$ `first_print` 지연)
- `session_print_age_min` = (cutoff − 세션 첫 체결)/60000
- `dormant_ratio_prior_day` = 세션 시작 직전 24시간의 무체결 분 비율 (**휴면도**)

> 한계: 해상도가 1분이라 초 단위 staleness 를 복원할 수 없고, 봉이 있어도 체결이 1건뿐일
> 수 있다. `first_print` 의 정확한 순간은 실시간 경로만 알 수 있다.
> 그래도 "휴면 동전주가 깨어나는 순간"(A2 §3 이 Tier 1 승격 1순위로 지정)은
> `dormant_ratio_prior_day` 가 높고 `no_print_ratio_5` 가 0 으로 떨어지는 조합으로 잡힌다.

### 4.6 토스 쏠림도 (docs/01 §3.2)
같은 스냅샷에서 두 랭킹 type 의 거래대금 비율:
$$\text{toss\_share}(s)=\frac{\text{amount\_u}^{TOSS}(s)}{\text{amount\_u}^{MARKET}(s)}$$
`toss_share` = 컷오프 이전 마지막 값, `toss_share_max`, `toss_share_slope_30`(최근 30분
OLS 기울기, 분당), `toss_rank_best`(최소 rank), `market_rank_best`,
`minutes_since_toss_entry`, `ranking_snaps_pre`.

> **클리핑하지 않는다**: 두 랭킹은 각각 상위 100 종목만 주므로 모집단이 다르고, 실제
> 응답에서 TOSS 금액이 MARKET 금액을 넘는 경우가 관측된다(스펙 예시 픽스처의 AAPL).
> 1 을 넘는 값은 데이터의 성질이므로 잘라내지 말고 그대로 기록한다.
> 또한 **두 type 중 하나에만 들면 비율이 정의되지 않는다**(NaN) — 선택 편향이 있다.

### 4.7 이력 피처
`prior_events` 가 주어지면 $[\text{cutoff}-20\text{d},\ \text{cutoff})$ 의 건수로
`prior_event_count_20d`, 최근 이벤트까지의 일수로 `days_since_prior_event`.
주어지지 않으면 df 내 **이전 UTC 날짜**들 중 $\max H/\min L-1\ge15\%$ 인 날을 세는
**프록시**를 쓴다(진짜 이벤트 정의와 다르므로 과대계상 가능).
`former_runner` = `prior_event_count_20d > 0`.
`float_rotation_pre` = 세션 시작~컷오프 누적 거래량 / 발행주식수 (진행분).

---

## 5. 리드타임 측정의 두 정의 (검증질문 1의 핵심)

| 피처 | 측정 범위 | 정의 |
|---|---|---|
| `rvol_first_cross_{2,3,5}_lead_min` | **T0 세션 내부** | 누적 RVOL 이 임계를 처음 넘은 시각의 $T_0$ 대비 리드(분) |
| `vol_surge_lead_min` | **T0 매매일 전체** | 봉 단위 $\ln(1+V)$ z 가 3 을 처음 넘은 시각의 리드(분) |

세션 제한의 이유는 §2.5 마지막 문단과 같다. 누적 RVOL 을 세션 밖까지 스캔하면 얇은
day/after 세션의 작은 분모가 만든 허위 돌파가 리드타임 중앙값을 수백 분으로 왜곡한다
(개발 중 실측: 3개 임계 전부 896분으로 붕괴). 봉 단위 z 는 자기 이력(직전 240분) 대비이므로
세션을 넘어도 정의가 일관되어 "프리마켓 전조 → 정규장 T0" 를 잡을 수 있다.

> 완만한 램프형(예: 90분에 걸친 +34%)은 봉 단위 z 가 3 을 못 넘어 `vol_surge_lead_min` 이
> NaN 이 될 수 있다. 미검출이며 오류가 아니다 — 검출률(`detect_rate`)로 함께 보고한다.

---

## 6. 평가 (`evaluate.py`)

### 6.1 게이트 정책 (A1 §6 의무)
기본 `gate="exclude"`: `rvol_gated=False` 이벤트를 집계에서 제외.
모든 반환 DataFrame 에 `gate_policy`, `n_ungated_excluded` 를 실어 **제외 사실을 드러낸다**.
`gate="separate"` 는 전부 집계하되 제외 대상 수를 함께 보고.

### 6.2 q1 — 리드타임 + 정밀도/재현율
§5 의 두 지표의 분포(n/mean/median/p10/p25/p75/p90)와 `detect_rate`.
정밀도/재현율은 **음성 표본이 있어야** 정의된다: `feats` 에 `is_event`(0/1)가 있으면
$\text{pred}=(\text{rvol\_at\_cutoff}\ge\theta)$ 로 TP/FP/FN 을 세고, 없으면
`note='no_controls'` 로 표시하고 NaN. 합성데이터에서는 noise 시나리오가 음성 표본이다.

### 6.3 q2 — 랭킹 리드/래그
`lead_share` = $\Pr[\text{lead\_lag}<0]$, `lag_share` = $\Pr[>0]$.
`verdict`: lag_share > 0.6 → `lag`, lead_share > 0.6 → `lead`, 그 외 `mixed`.

> **랭킹은 과거 조회가 불가하다** (A2 §4). 빈 입력은 오류가 아니라 정상 경로이며
> `available=False` + `verdict='unavailable'` 한 줄을 반환한다(예외 금지).
> 따라서 q2 는 **실시간 수집 기간에만** 답할 수 있는 유일한 질문이다.

### 6.4 q3 / q4
q3: 세션별 `persist_rate` = $\Pr[\text{ret\_close}>0]$ 등. 데이마켓 $T_0$ 에 대해
`ret_close` 가 "같은 매매일 정규장 종가까지의 수익률"이 되는 것은 §3.1 의 매매일 구성
(day→pre→regular→after) 덕분이다.

q4: `low_u` $\le H_{\text{peak}}(1-dd)$ 최초 봉까지의 분수. **미도달은 우측 절단**이므로
분포에서 빼고 `reach_rate` 로 따로 보고한다. `horizon_min`(기본 390 = 정규장 1세션)으로
탐색을 제한한다 — 제한이 없으면 **다음 매매일의 하락까지 "덤프 속도"로 집계되어** 중앙값이
수백 분으로 왜곡된다(개발 중 실측: -50% 중앙값 1083분).

### 6.5 q5 — 기대수익과 비용 모형 (A2 §5)
$$\text{net}=\text{gross}-\text{cost\_roundtrip},\qquad
\text{cost\_roundtrip}=\underbrace{0.002}_{\text{수수료 왕복}}+\underbrace{0.003}_{\text{환전}}+\underbrace{0.005}_{\text{슬리피지}}=0.01$$

- US 수수료는 `/commissions` 에서 **퍼센트 단위 `0.1`(=0.1%)** 로 온다. KR 은 비율
  `0.00015`. 단위가 시장마다 다르므로 혼동 주의.
- **기본값 1% 는 보수적 총비용이며 수수료만의 0.2% 가 아니다.** 리포트는 내역을 분해해
  표기한다(`cost_commission`/`cost_fx`/`cost_slippage`).
- 청산 정책: `t0_to_30m`(=`ret_30m`), `t0_to_close`(=`ret_close`),
  `half_peak`(=$0.5\times$`peak_ret`).
  **`half_peak` 은 실행 가능성 상한선**이다 — 피크의 절반을 항상 잡는다는 가정은 비현실적이며
  승률이 구조적으로 1.0 이 된다. 상한과 실제의 간격을 보는 용도로만 쓴다.
- 슬리피지를 고정 상수로 두는 것은 단순화다. 실제로는 호가 1레벨만 관측 가능하고(A2 §1)
  덤프 구간에서 스프레드가 폭발하므로 **하방에서 과소평가**된다.

### 6.6 q6 / 기저율 대조
버킷 기준은 `t0_min_from_open`: 개장 전 / 0–15 / 15–30 / 30–180 / 180–330 / 330–390 /
애프터. 정규장 개장 시각은 $T_0 - \text{t0\_min\_from\_open}\times 60000$ 으로 역산하고,
`hod_min_from_open` 을 그 기준으로 계산한다.

`base_rate_comparison` 은 docs/02 §2.4 실측치(페이드율 71.5%, HOD 대비 -20% 붕괴 50%,
VWAP 아래 마감 73%, HOD 63% 가 10:00 ET 전, 46.6% 가 15분 내)와 대조한다.
> `delta` 가 크면 **먼저 우리 표본 정의를 의심한다**: 문헌 표본은 "프리마켓 5M주+ 갭퍼"이고
> 우리 이벤트 정의는 "30분 +15% 또는 당일 +30% + RVOL 3" 이다. 유니버스와 임계값이 다르면
> 기저율은 당연히 다르다.

---

## 7. 알려진 편향·한계 총목록

| # | 항목 | 영향 | 완화 |
|---|---|---|---|
| 1 | 랭킹 과거 조회 불가 (A2 §4) | q2 는 실시간 수집 기간에만 가능. 백필 이벤트의 랭킹 라벨은 전부 NaN | 정상 경로로 처리, 미가용 명시 |
| 2 | `sharesOutstanding` ≠ float | `float_rotation` 과소평가 (저플로트에서 최악) | 외부 float 소스 확보 시 재계산 (Phase 2) |
| 3 | 호가 1레벨뿐 (A2 §1) | 호가 깊이·다단 불균형 피처 불가 | 테이프 중심 설계, `imbalance`=bid1/(bid1+ask1) 만 |
| 4 | `/trades` 최대 50건 (A2 §2) | 체결 크기 분포는 **표본 통계** | Phase 1 피처에서 테이프 미사용, 표본임을 명시 |
| 5 | 캔들 공백의 원인 구분 불가 | 홀트 프록시 부정확 | 정규장 + 5분 이상으로 한정, 프록시임을 명시 |
| 6 | 봉 타임스탬프가 봉의 시작인지 끝인지 미확정 | 분 위치 지표가 일괄 ±1분 이동 가능. RVOL 은 영향 없음(일관 이동), "개장 15분" 버킷 경계는 영향 있음 | **W1 실측 확인 필요** (미해결) |
| 7 | 분모 자기오염 (§2.4) | 평가일을 곡선에 넣으면 RVOL 축소 | `exclude_dates` / 이력일 전용 곡선 |
| 8 | 얇은 세션의 작은 분모 (§2.5) | day/after RVOL 고분산, 허위 돌파 | 리드타임을 T0 세션으로 제한 |
| 9 | 전일 종가 대체 (§3.2) | 갭업 과소평가 | 전일 종가 명시 전달 |
| 10 | `half_peak` 상한 (§6.5) | 기대수익 과대평가 | 상한선으로만 해석 |
| 11 | 고정 슬리피지 | 덤프 구간 하방 과소평가 | 실측 후 세션·유동성별 모형화 (Phase 2) |
| 12 | `shape`/`outcome` 은 규칙 기반 | 임계값 임의성 | 상수 노출, 데이터로 보정 예정 |
| 13 | 이력 이벤트 프록시 (§4.7) | 실제 이벤트 정의와 불일치 → 과대계상 | `prior_events` 명시 전달 시 해소 |
| 14 | 생존 편향 | 상장폐지·심볼 변경 종목이 백필에서 누락 | 유니버스 구성 시 고려 (W2) |
| 15 | 이벤트 임계값 자체가 초기값 | 이벤트 집합이 임계에 민감 | `EventParams` 파라미터화, 민감도 분석 예정 |

---

## 8. 합성 데이터 생성기 (`tests/synth.py`)

검출기 평가의 ground truth. 시나리오: `coil_pop`(코일→폭발, VWAP 상방 유지),
`instant`(전조 없는 단일봉 폭발), `fade`(프리마켓 갭업 → HOD 조기 형성 → VWAP 아래 마감),
`dump`(급등 직후 붕괴 + LULD 홀트 공백 + 재개 갭다운), `noise`(비이벤트),
`daymarket`(데이마켓 급등 후 정규장 소멸), `halt_gap`(홀트 2회 + 12분 수집 중단).

- 구성: `history_days`(기본 3) 개의 평범한 날 → 이벤트 당일 → 다음날.
  RVOL 곡선의 분모는 `truth["baseline_calendar"]`(이벤트 당일 제외)로 만든다.
- 모든 가격 전이는 ppm 정수 연산: $P' = \lfloor P(10^6+\text{ppm})/10^6 \rfloor$. float 가격 없음.
- 세션 시간표는 W1 실측 정정본(docs/01 §5)을 따른다. 얇은 세션은 22% 확률로 봉을 생성하지
  않아 실제와 같은 캔들 공백을 만든다.
- `truth["t0_expected_ms"]` 는 **나이브 참조 구현**(좌측 포인터 스캔)으로 가격 조건 최초
  충족 봉을 직접 찾은 값이다. `labeling` 의 벡터화 rolling 구현과 방식이 달라 교차 검증이
  된다. RVOL 게이트는 참조 구현에 없으므로 검출 $T_0$ 는 항상 이 시각 이후다.
- 결정론적: `random.Random` 을 문자열 안정 해시로 시드한다(`hash()` 는 PYTHONHASHSEED 로
  흔들리므로 금지).

테스트 대응: 라벨 재현(`test_analysis_labeling.py`), 룩어헤드 부재
(`test_analysis_features.py`), 시간대 보정 정확성·오버플로
(`test_analysis_baselines.py`), 평가·리포트(`test_analysis_evaluate.py`),
실응답 스키마·성능(`test_analysis_fixtures.py`), 생성기 자체(`test_analysis_synth.py`).
