# 39. `TOP_GAINERS` 를 수집에 추가 — 만들었고, **배포는 안 했다**

소유: W4. 사용자 결정 2026-08-07 (D-11). 이 문서는 **무엇을 바꿨고 무엇이 남았는지**를 적는다.

---

## 0. 한 문장

**등락률 급상승 목록(`TOP_GAINERS`, `duration=1d`)을 세 번째 랭킹 타입으로 붙였고,
`realtime` 과 `1d` 가 한 테이블에 섞이는 것을 막는 장치 넷을 함께 넣었다.
코드는 완성이고 테스트는 통과했으며 — 배포는 안 했다. 재시작 시점은 코디네이터가 잡는다.**

---

## 1. 왜 넣는가 (사용자 결정, 재론 없음)

사용자는 실전에서 등락률순 "급상승" 과 거래량순을 왔다갔다 본다(거래량 쪽을 조금 더).
그런데 **우리 DB 에 `TOP_GAINERS` 는 단 한 건도 없다.** W1 실측(`docs/35` §5-6a)의 결정적 숫자:

> **`TOP_GAINERS` 100종목 중 35% 는 우리 랭킹 이력에 한 번도 등장하지 않는다.**
> (등장률: 거래량 목록 100% vs `TOP_GAINERS` 65%)

많이 올랐는데 거래량 순위엔 안 걸리는 종목이 3분의 1이고, 그게 우리가 못 보던 부분이다.

**W1 이 이미 확인해준 것** (다시 재지 않았다):

| 항목 | 값 |
|---|---|
| `duration=realtime` | **400 `unsupported-ranking-duration`** — `1d` 로만 200 |
| `order` 갱신 속도 | 4.77 회/분 (거래량 4.51) — 비슷하지만 **10초 격자 없음**, 제각각 |
| 우리 tier0 통과율 | 25% (거래량 28%) |
| 가격대 $2~5 비중 | 15% (거래량 9%) — 오히려 높다 |
| 머리는 고정 | 상위 3종목이 240폴(4분) 내내 `MB`·`DOCS`·`NAMI`. 변화는 **꼬리에서** |

---

## 2. 무엇을 바꿨는가

| 파일 | 변경 |
|---|---|
| `tossmon/api/models.py` | `RANKING_DURATIONS` + `duration_for()` 신설 (타입→duration 단일 출처) |
| `tossmon/collector/loops.py` | `FEATURE_RANKING_TYPES` 분리, `RANKING_TYPES` 3종, `RANKING_COUNT`, 루프·`config_signature` |
| `tossmon/collector/budget.py` | `RANKING_TYPES = 2 → 3` |
| `tossmon/collector/detector.py` | 맞춰야 할 상대를 `RANKING_TYPES` → `FEATURE_RANKING_TYPES` 로 (주석) |
| `config/config.example.yaml` | **RANKING 예산 산식 신설** (원래 없었다) |
| `tools/dryrun_night.py` | 리포트의 계산상 상한 `0.17 (2종)` → `0.25 (3종)` |
| 테스트 5개 파일 | 아래 §7 |

`tossmon/analysis/**`·`tossmon/store/**`·`ops/**` 는 **건드리지 않았다** (각각 D-10 안건 / W2 / W5).

---

## 3. ★ 섞이지 않게 하는 장치 — 넷을 넣었다

기존 두 타입은 `realtime`, 새 타입은 `1d` 다. 스키마에 `duration` 컬럼이 이미 있어 저장은
되지만, **저장이 된다는 것과 분석이 안 섞는다는 것은 다른 문제다.**

### 장치 1 (구조) — `duration` 을 **타입의 함수**로 못박았다

`api/models.RANKING_DURATIONS` 가 유일한 출처이고, 수집 루프는 duration 을 **고르지 않고
찾아 쓴다**(`duration_for(rtype)`). 미등록 타입은 기본값을 주지 않고 `KeyError` 로 죽는다.

**이것이 읽는 쪽의 안전장치다.** 한 `ranking_type` 이 정확히 한 `duration` 을 결정하므로

```sql
... FROM rankings_snap WHERE ranking_type = ?   -- duration 은 자동으로 하나
```

가 성립한다. `GROUP BY ranking_type` 만 해도 집계창이 섞이지 않는다. 반대로 **타입을 안 가르고
기간만으로 긁으면** 서로 다른 집계창의 행이 한 프레임에 들어온다 — 그게 유일한 위험 경로다.

`models.py` 에 둔 이유: 수집기와 분석이 **둘 다 순환 참조 없이** import 할 수 있는 곳이고,
`RankingRow` 의 `amount_u` 경고가 이미 거기 있다. 의미에 관한 사실은 한 군데 모은다.

### 장치 2 (데이터) — `config_sig` 에 타입·duration·깊이를 전부 적었다

```
이전:  rank2:MVOLUME+TVOLUME,t3max10,tr4s,ob4s,t2ob0s,t2c110s,rk12s
이후:  rank3:GAIN/1d+MVOLUME/rt+TVOLUME/rt@100,t3max10,tr4s,ob4s,t2ob0s,t2c110s,rk12s
```

(실측값 — mock 서버 상대 실행 출력 그대로. §6)

타입 수(`rank3`)·타입명·**duration**(`/1d`, `/rt`)·**깊이**(`@100`)가 전부 드러난다.
duration 이 없으면 분석은 경계 전후는 갈라도 **한 구간 안에서 집계창이 둘이라는 것**을 모른다.
깊이를 넣은 이유도 같다 — 51~100위가 있던 구간과 없던 구간은 다른 데이터다.

`DATA-QUALITY-PROGRAM` 규칙 3 그대로다. 이 프로젝트는 `usage_ratio` 가 `config_sig` 에 없어서
08-04 밤의 0.85→0.70→0.85 가 **데이터에 안 남은** 전례가 있다. 반복하지 않는다.

### 장치 3 (격리) — `1d` 목록은 **실시간 피처 경로에 아예 안 들어간다**

`FEATURE_RANKING_TYPES`(realtime 2종)와 `RANKING_TYPES`(수집 3종)를 분리했다.
`TOP_GAINERS` 는 **DB 까지만** 간다 — `RankingBuffer`·유니버스 판정·승격 트리거를 안 탄다.

이유는 §4 의 실측이다: 같은 순간 같은 심볼인데 `1d` 의 `vol_qu` 가 `realtime` 의 **중앙값
15.4배**다. 실시간 버퍼는 토스 쏠림도(두 realtime 목록의 **순위 대비**)를 계산하려고 존재하므로,
1d 행이 섞이면 그 피처가 **예외 하나 없이 조용히** 틀린다. 검사보다 안 들여보내는 쪽이 확실하다.

`detector.py` 가 맞춰야 할 상대도 `FEATURE_RANKING_TYPES` 로 바꿨다. 수집 목록으로 맞추면
"1d 를 쏠림도 분모에 넣어라" 가 된다.

### 장치 4 (경보) — 전제가 깨지면 조용히 넘어가지 않는다

응답의 `duration` 이 요청과 다르면 `ranking_duration_mismatch` 카운터 + `alert`.
저장은 한다(랭킹은 과거 조회 불가라 버리면 영구 유실). 다만 **읽는 쪽의 분리 전체가
"타입→duration 이 함수" 라는 전제 위에 서 있으므로**, 그 전제가 서버 쪽에서 깨지면 반드시 보여야 한다.

---

## 4. `amount_u` 확인 — 같다. 그런데 **더 중요한 게 나왔다**

### 4-1. 왜 라이브를 불렀는지 (부르기 전에 적은 것)

우리 DB 에 `TOP_GAINERS` 행이 0건이고, 저장된 픽스처
`tests/fixtures/live/rankings_top_gainers_1d.json` 은 `"source": "synthetic"`(openapi 예시 유래)라
`tradingAmount == tradingVolume × lastPrice` 로 **정확히 USD 일관되게 만들어져 있다** — 이 질문에
원리상 답할 수 없다. W1 프로브도 raw 행을 top3 심볼명까지만 남겼다(`docs/35` §5-7).
다른 경로가 없어 **라이브 호출**을 했다.

**실제 호출 3콜** (RANKING 그룹, 429 0건, 21:46~21:47 KST). 계획은 2콜이었는데 두 번째
스크립트를 출력 확인 때문에 두 번 돌려 1콜이 더 나갔다 — 필요했던 건 2콜이다.
토큰은 가동 중 수집기의 것을 **읽어서만** 썼다(`ReusedTokenManager`, 재발급 없음).

### 4-2. 결과 — `amount_u` 는 `TOP_GAINERS` 에서도 **micro-KRW** 다

`docs/06` §13 산식 `amount_u / (vol_qu/1e6 × last_u/1e6) / 1e6`:

| 목록 | n | p10 | **중앙값** | p90 |
|---|---|---|---|---|
| **`TOP_GAINERS` 1d** (라이브) | 100 | 1357.7 | **1401.6** | 1489.4 |
| `TOSS_..._VOLUME` realtime (DB, 같은 순간) | 100 | 1387.4 | **1417.3** | 1446.4 |
| `MARKET_..._VOLUME` realtime (DB, 같은 순간) | 100 | 1391.0 | **1412.5** | 1440.6 |

전부 **1400 대**다. 달러였다면 1.0 이 나왔어야 한다. → **기존 가드가 이 타입에도 그대로 걸린다**
(계약 C-2 상 금액 용도 금지, `RankingRow` docstring 경고). 별도 조치 불필요.

중앙값이 1.1% 낮고 분포가 두 배 넓지만(±4.7% vs ±2.1%), realtime 두 목록끼리도 1417.3 vs
1412.5 로 갈리므로 **단위가 다르다는 증거는 아니다.** 원인은 특정하지 못했다 — §4-3 의 가설은
반증됐다.

### 4-3. 반증된 가설 하나, 확인된 사실 하나

가설: `1d` 는 당일 누적이라 비율 = FX × (당일 VWAP / 현재가) 이고, 급등 목록은 전부
VWAP<last 라 체계적으로 FX 아래로 치우친다. → **치우침이 `changeRate` 와 음의 상관을 보여야 한다.**

- **반증됐다.** Pearson r = **+0.062** (n=100). `changeRate` 하위 절반과 상위 절반의 비율
  중앙값이 1399.2 vs 1398.9 로 사실상 같다. 1.1% 차이의 원인은 **모른다.**
- **그런데 같은 실험의 다른 관측이 훨씬 중요했다.** 두 목록에 **동시에** 올라 있던 35종의
  같은 순간 `vol_qu` 를 직접 비교했다:

  | | 값 |
  |---|---|
  | 짝 수 | 35 |
  | **`vol_qu`(1d) / `vol_qu`(realtime) 중앙값** | **15.4배** |
  | 최대 | 172.6배 (`DOCS`) |

  → **`realtime` 은 롤링 창, `1d` 는 훨씬 긴 창이다. 같은 컬럼인데 뜻이 다르다.**
  두 duration 의 행을 한 프레임에서 거래량·체결대금으로 비교·정렬·집계하면 그대로 틀린다.

**이것이 §3 장치들의 진짜 근거다.** "duration 라벨이 다르니 조심하자" 가 아니라
**측정된 15.4배**가 근거다.

---

## 5. 예산·디스크 — 미리 계산한 값

### 5-1. API 예산 (`RANKING` 그룹, 한도 5 req/s)

| | 이전(2종) | **이후(3종)** | 증분 |
|---|---|---|---|
| 계획 호출률 (`types / ranking_snap_s`) | 2/12 = 0.167 | **3/12 = 0.250** | **+0.083 req/s** |
| 한 폴의 초당 첨두 (3콜이 연달아 나감) | 2 | **3** | +1 |
| 계획 천장 (`5 × 0.85 × (0.95−0.10)`) | 3.61 | 3.61 | — |
| 여유 | +3.44 | **+3.36** | — |

여유가 천장의 93% 라 압박이 없다. `usage_ratio` 0.70 을 가정해도 계획 천장 2.98 로 여전히 무관하다.
**랭킹은 축소 대상이 아니다**(과거 조회 불가) — 넘치면 자동 조정이 아니라 경보만 나가므로
사전 계산이 유일한 방어다. 그래서 회귀 테스트로 못박았다(§7).

> ⚠️ **W1·코디네이터에게**: `docs/35` §4 의 겹침 계산이 바뀐다.
> "우리 1 + 수집기 2 = **3/5**" 였는데 이제 **1 + 3 = 4/5** 다. 한도까지 1 남는다.
> 정규장 재측정(`--cadence-profile open`, 1 req/s 팔)을 이 배포 **뒤에** 쏠 거라면
> 그 여유로 판단해야 한다.

### 5-2. 디스크

측정 조건: `rankings_snap` 스키마 그대로, 라이브 DB 최근 2만 행을 표본으로 새 DB 에 10만 행
삽입 후 파일 크기 차 (테이블+인덱스 2개 포함, page_size 4096).

| 항목 | 값 |
|---|---|
| **행당** | **214.4 B** (21,438,464 B / 100,000 행) |
| 하루 스냅/타입 (실측: 08-04 6616, 08-05 6614) | **약 6,600** (= 22.0h 수집 / 12s) |
| **세 번째 타입 하루** | **660,000 행 ≈ 141.5 MB** |
| 랭킹 전체 하루 | 283.0 MB (2종) → **424.5 MB** (3종) |
| 현재 여유 | 97.9 GB (실측 105,148,522,496 B) |

**"모르고 늘리는 것과 알고 늘리는 것은 다르다" 에 대한 정직한 답**:

- 랭킹만으로 계산한 잔여 수명이 **약 346일 → 약 231일**로 줄어든다.
- ⚠️ **랭킹은 지금 아무것도 지우지 않는다.** `store/retention.py` 는 `candles_1m` 만 다루고
  `rankings_snap` 은 손대지 않는다(grep 0건). 즉 단조 증가다. 지금 DB 2.16 GB 중 랭킹
  8.89M 행 ≈ 1.9 GB 로 사실상 전부다.
- 당장 문제는 아니지만 **W2(store 소유)에게 넘길 안건**이다: 랭킹 보존 정책이 없다.

---

## 6. 타입 수 하드코딩 — **전수 조사 9곳 중 9곳 수정**

`grep -rn "RANKING_TYPES\|ranking_types\|rank2\|2종"` 을 `tossmon/ tests/ config/ tools/ ops/`
전체에 돌려 실제 값에 영향을 주는 지점만 추렸다(산문 언급 제외).

| # | 위치 | 성격 | 처리 |
|---|---|---|---|
| 1 | `collector/budget.py:42` `RANKING_TYPES = 2` | **예산 상수** | → 3 |
| 2 | `collector/detector.py:48` 주석 "`loops.RANKING_TYPES` 와 같아야" | 불변식 진술 | → `FEATURE_RANKING_TYPES` |
| 3 | `tools/dryrun_night.py:659` `"0.17 (2종/12s)"` | **리포트의 예산 계산치** | → `0.25 (3종/12s)` |
| 4 | `config/config.example.yaml` | **RANKING 산식이 아예 없었다** | 신설 (3/12=0.25) |
| 5 | `tests/test_collector_budget.py:453` `two/four` | 회귀 | 목록 수 정비례로 일반화 + 3종 전용 테스트 추가 |
| 6 | `tests/test_collector_budget.py:53` `2/12` | 회귀 | → `3/12` |
| 7 | `tests/test_collector_loops.py:1655,1677,1688` | 회귀 | 3종 + duration + `rank3:` |
| 8 | `tests/test_collector_detector.py:731` | 회귀 | → `FEATURE_RANKING_TYPES` (+ 부분집합 단언) |
| 9 | `tests/test_collector_replay.py:69` | 회귀 | → `FEATURE_RANKING_TYPES` (synth 는 1d 를 못 만든다) |

`len(RANKING_TYPES)` 로 세는 곳(`loops.py:653` `refresh_plan`, 통합·리플레이 테스트 다수)은
**고칠 필요가 없다** — 실제 목록을 세도록 이미 돼 있어 자동으로 따라간다. 상수 #1 과 실제 목록의
일치는 `test_budget_ranking_count_matches_the_list_the_collector_actually_polls` 가 계속 고정한다.

---

## 7. 검증 — 실행한 것과 그 출력

**전체 스위트**:

```
$ .venv/Scripts/python.exe -X utf8 -m pytest tests/ -q
1486 passed, 1 skipped, 2 deselected, 8 warnings in 313.00s
$ .venv/Scripts/python.exe -X utf8 -m pytest tests/test_analysis_synth.py -q
63 passed in 26.60s
```

(두 번째는 첫 실행에서 `--ignore` 했던 파일을 따로 돌린 것이다. 합쳐서 전 파일 통과.)

**mock HTTP 서버 상대 실측** (`loops.rankings_once` 1회):

```
config_sig = rank3:GAIN/1d+MVOLUME/rt+TVOLUME/rt@100,t3max10,tr4s,ob4s,t2ob0s,t2c110s,rk12s

  ('MARKET_TRADING_VOLUME', 'realtime', 100, 1, 100)
  ('TOP_GAINERS', '1d', 100, 1, 100)
  ('TOSS_SECURITIES_TRADING_VOLUME', 'realtime', 100, 1, 100)

req_RANKING = 3 | ranking_snaps = 3 | duration_mismatch = 0
```

세 타입이 각자의 duration 으로 100행씩 들어갔고, 한 타입 안에 duration 이 하나뿐이다.

**신설 테스트 5개**:

| 테스트 | 무엇을 막는가 |
|---|---|
| `test_duration_is_a_function_of_ranking_type_not_a_free_argument` | 장치 1 의 뿌리. 미등록 타입 추측 금지, 피처 목록은 단일 duration |
| `test_rankings_loop_asks_each_list_with_its_own_duration` | 타입·duration·깊이를 한꺼번에 고정 (`TOP_GAINERS`+realtime = 400 → 0행) |
| `test_a_duration_the_server_did_not_honour_is_alerted` | 장치 4 |
| `test_the_1d_list_never_enters_the_realtime_feature_path` | 장치 3. 버퍼에 **건네지는 것**을 spy 로 본다 — 최종 상태를 보면 `prune` 이 섞여 "안 들어갔다" 와 "정리됐다" 를 못 가른다 |
| `test_config_signature_spells_out_the_duration_of_every_list` | 장치 2 |
| `test_third_ranking_type_stays_far_under_the_ranking_budget` | 예산. 랭킹은 축소 불가라 사전 고정이 유일한 방어 |

---

## 8. ★ 배포 준비 상태 — **남은 것**

**만들고 멈췄다.** `schtasks /run`·`Start-ScheduledTask` 실행 안 함. `config/config.yaml`(라이브)
건드리지 않음.

| 항목 | 상태 |
|---|---|
| 코드 | ✅ 완료, 커밋됨 |
| 테스트 | ✅ 전체 통과 (§7) |
| **설정 변경 필요 여부** | ✅ **불필요.** 랭킹 타입 목록은 `loops.py` 상수라 `config.yaml` 에 없다. `ranking_snap_s: 12` 도 그대로 |
| **재시작 필요 여부** | ⚠️ **필요.** 상수 변경이라 프로세스를 다시 띄워야 적용된다 |
| 배포 시점 | **코디네이터 결정.** 지금 정규장(22:30 개장) 직전이라 하지 않았다 |

### 재시작 뒤 30초 안에 확인할 것

1. 로그의 `COLLECTION-CONFIG start sig=rank3:GAIN/1d+...@100` — 경계가 찍혔는가.
2. 5분 리포트의 `config_sig` 가 같은 값인가.
3. `ranking_duration_mismatch` 가 0 인가 (0 이 아니면 §3 장치 4 발동 = 전제 붕괴).
4. `budget RANKING=peak3/p95:3/avg0.25/tgt4.25` 근처인가. **peak 이 3 을 넘으면** 랭킹 외
   호출이 같은 그룹에 섞인 것이다.
5. `SELECT ranking_type, duration, COUNT(*) FROM rankings_snap WHERE snap_ms > <재시작> GROUP BY 1,2`
   — 세 줄이 나오고 각 타입에 duration 이 하나씩인가.

### 되돌리는 법

`loops.RANKING_TYPES` 에서 `+ ("TOP_GAINERS",)` 를 지우고 `budget.RANKING_TYPES = 2`.
`config_sig` 가 자동으로 `rank2:` 로 돌아가므로 **되돌린 경계도 데이터에 남는다.**

---

## 9. 분석 쪽에 남기는 말 (**고치지 않았다**)

`tossmon/analysis/**` 는 D-10(1분봉 라벨) 안건이 걸려 있어 건드리지 않았다. 다음은 **제안**이다.

1. **`rankings_snap` 을 읽을 때 `ranking_type` 없이 긁지 마라.** 타입을 가르면 duration 은
   자동으로 갈린다(§3 장치 1). 타입 없이 기간만으로 긁는 쿼리가 유일한 사고 경로다.
2. **두 duration 의 `vol_qu`·`amount_u` 를 같은 축에 놓지 마라.** 실측 중앙값 15.4배다(§4-3).
   순위(`rank`)는 목록 안에서의 상대값이라 비교해도 되지만, 수량은 안 된다.
3. **`analysis/shots.py:74` `DEFAULT_RANKING_TYPES = (TOSS_VOLUME, TOSS_AMOUNT)`** 는
   `TOSS_AMOUNT` 를 포함하는데 **우리는 08-04 부터 그걸 안 받는다.** 이번 변경과 무관하게
   이미 어긋나 있다 — W3/분석 소유자 확인 필요.
4. 경계 특정은 `config_sig` 로: `rank2:MVOLUME+TVOLUME` 이 마지막으로 보인 리포트 다음이
   전환점이다. 손으로 적은 시각을 쓰지 마라.
5. `TOP_GAINERS` 는 **승격 트리거를 안 탄다**(§3 장치 3). "왜 이 종목이 tier2 로 안 올라왔나"
   를 이 목록으로 설명하면 틀린다.

---

## 10. 이 작업이 답하지 못한 것

1. **`amount_u` 비율이 realtime 대비 1.1% 낮은 이유를 모른다.** VWAP 가설은 반증됐다(§4-3).
   단위가 다르다는 증거는 아니므로 결론(micro-KRW)은 바뀌지 않는다.
2. **`1d` 집계창의 정확한 길이를 모른다.** "realtime 보다 중앙값 15.4배 길다" 까지만 안다.
   당일 누적인지 24시간 롤링인지는 안 쟀다 — 가르려면 개장 직후 값이 작게 시작하는지 보면 된다.
3. **정규장에서의 갱신 속도를 모른다.** W1 실측은 프리마켓 한 세션뿐이다(`docs/35` §5-7).
4. **`TOP_GAINERS` 가 실제로 전조를 주는지 모른다.** 이 작업은 수집만 했다. 판정은 데이터가
   쌓인 뒤 분석의 일이다 — 지금은 **못 보던 3분의 1을 보기 시작한 것**뿐이다.
5. **깊이 100 이 옳은지 실증하지 않았다.** 승격은 상위 10위만 보고, 51~100위를 실제로 쓰는
   분석은 아직 없다. 되돌릴 수 없는 데이터라 넓게 받는 쪽을 골랐다는 것이 근거의 전부다(§1 `RANKING_COUNT`).
