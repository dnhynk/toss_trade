# W4 — 사용자 결정 반영: 랭킹 2종 + tier3 10종목 × 호가 4초 (task_8cab8f2af043)

브랜치 `w4-collector`. 라이브 호출 0건, 가동 중 수집기 무접촉, 기존 데이터 무수정.
배포는 코디네이터가 한다 — 내 작업은 커밋까지다.

---

## 1. 요구사항 1 — `validate_plan` 이 여유를 포함해 통과하는가

**통과한다. 경고도 없다.** (`PLAN_RESERVE_FRAC = 0.10` 기준)

```
MARKET_DATA   한도 10 × usage_ratio 0.7 = target 7.000 req/s
  축소 천장 shrink_ceiling = 7.000 × 0.95         = 6.650
  계획 천장 plan_ceiling   = 7.000 × (0.95−0.10)  = 5.950
  계획 = tier1 0.178 + 체결 10/4 2.500 + 호가 10/4 2.500 = 5.178
      → 축소 트리거까지          +1.472 req/s
      → 계획 여유 (보고 요청값)  +0.772 req/s   ✅ 경고 없음
```

| 그룹 | 계획 | 축소천장 | 계획천장 | 여유 | 판정 |
|---|---:|---:|---:|---:|---|
| MARKET_DATA | 5.178 | 6.650 | 5.950 | **+0.772** | 통과 |
| MARKET_DATA_CHART | 2.727 | 3.325 | 2.975 | +0.248 | 통과 |
| RANKING | **0.167** | 3.325 | 2.975 | +2.808 | 통과 (4종 시절 0.333) |

`validate_plan() == {}`, `reserve_deficit() == {}`, `should_shrink() is None` — 셋 다 코드로 확인했다.
직전 설정(20종목/16s = 6.428)은 계획 여유가 **−0.478** 이라 매 개장 축소를 맞았다. 그 원인이 사라진다.

**코디네이터 확인용 — 라이브 `config.yaml` 에 넣을 값** (example 과 어긋나면 드러나야 한다고 했으므로 명시):

```yaml
universe:  { tier3_max: 10 }
polling:   { tier3_orderbook_s: 4 }   # tier3_trades_s 는 4 그대로 — 건드리지 않았다
```
이 두 값 + 랭킹 2종이면 MARKET_DATA 계획 합계는 **5.178 req/s** 여야 한다. 다르면 설정이 어긋난 것이다.

## 2. 무엇을 바꿨나

**랭킹 4종 → 2종** (`loops.RANKING_TYPES`): `MARKET_TRADING_VOLUME`, `TOSS_SECURITIES_TRADING_VOLUME`.
예산 계산도 2로 반영했다. 상수를 복제하지 않고 **수집기가 실제로 도는 목록을 세어** 계획에
넘긴다(`TierPlan.from_config(ranking_types=len(RANKING_TYPES))`) — 목록과 예산이 어긋나는 드리프트를
구조적으로 없앴다.

**디스크 효과**: 랭킹 쓰기는 `종류수 × 100행 / 12s`. 4종 33.3행/s → 2종 **16.7행/s**.
하루 커버리지(pre+regular+after ≈ 14.5h) 기준 약 174만 → **87만 행**. 실측 161만 행과 같은 크기다.

**tier3 10종목 × 호가 4초**: `config/config.example.yaml` 의 두 값과 하단 예산 산식 주석을 갱신했다
(코디네이터가 A안으로 승인). 체결 주기 4s 는 그대로다.

## 3. 요구사항 3 — 전환 경계 기록

수집 설정 지문을 만들어 **로그와 텔레메트리 양쪽**에 남긴다.

```
COLLECTION-CONFIG start sig=rank2:MVOLUME+TVOLUME,t3max10,tr4s,ob4s,t2ob0s,t2c110s,rk12s
```

- 로그: `run_all` 진입 직후 = **수집이 시작되기 전**에 한 줄. 이 줄의 시각이 곧 경계다.
- 텔레메트리: `config_sig` 키로 5분마다. **지문이 바뀐 첫 리포트 시각**을 찾으면 경계를 알 수 있다.

지금까지 경계는 사람이 손으로 적어 왔다(08-04 00:07:28, 02:26, 10:09). 빠뜨리면 그만인 기록이라,
이제 경계가 데이터 안에 남는다. 밀도·폭에 영향을 주는 값만 담아서 무관한 설정 변경에는 흔들리지 않는다.

**W5 워치독 안전성 확인함** (읽기만 했다). `ops/watchdog.ps1` 은 텔레메트리를
`session=(\S+)\s+(.*)$` 로 뜯고 `Parse-Counters` 가 **숫자 값만** 매칭한다
(`([a-z0-9_]+)=(-?[0-9]+...)`). `config_sig` 는 값이 숫자가 아니라 그냥 건너뛰어지므로
`counters_sig`(정지 감지용 서명)에 섞이지 않는다 — 오탐 재시작을 부르지 않는다.
값에 공백·`=`·`|` 를 넣지 않은 이유가 이것이다. state.json 경로에도 영향 없다(카운터가 아니다).

## 4. 회귀 테스트

신규 11건. 핵심은 굵게.

| 테스트 | 무엇을 막나 |
|---|---|
| **`test_shipped_config_boots_without_shrinking_and_keeps_reserve`** | **출하 설정이 기동 즉시 축소되는 것 (요구사항 1)** |
| `test_shipped_tier3_settings_are_the_decided_ones` | 설정만 조용히 되돌아가 위 여유 계산이 무의미해지는 것 |
| `test_ranking_budget_counts_two_lists_not_four` | 예산이 옛 4종으로 계산되는 것 |
| **`test_budget_ranking_count_matches_the_list_the_collector_actually_polls`** | **예산이 세는 수와 실제 호출 목록의 드리프트** |
| **`test_rankings_loop_calls_exactly_the_two_lists`** | **상수만 줄이고 루프는 옛 목록을 계속 부르는 것 (디스크 절감 무효화)** |
| `test_only_the_two_count_based_ranking_lists_are_polled` | 금액 목록의 조용한 부활 |
| `test_config_signature_records_the_collection_shape` | 지문이 텔레메트리에서 누락되는 것 |
| `test_config_signature_changes_when_the_collection_shape_changes` | 지문이 안 바뀌어 경계를 못 찾는 것 |
| `test_startup_logs_the_config_boundary` / `..._marks_the_boundary_before_collecting` | 경계 줄 누락, 또는 수집 뒤에 찍혀 첫 구간이 미표시로 남는 것 |
| `test_quote_density_is_what_the_decision_bought` | 호가:체결 비율이 도로 벌어지는 것 |

**기존 테스트 8건이 옛 값을 하드코딩하고 있어 갱신했다.** 값을 다시 박아넣는 대신 설정에서 읽게 바꿨다
(`caps == {tier2_max: uni.tier2_max, ...}`, `ranking_snaps == len(RANKING_TYPES)`) — 다음 설정 변경 때
같은 일을 반복하지 않기 위해서다. 실질 검증이 걸린 곳(`tier3_max=30` 초과, 축소 복구)은 옛 주기를
**명시**해 원래 의도를 보존했다(`plan(tier3=30, book_s=16)`).

## 5. ⚠ 정정 — "호가당 체결 109건 → 약 4건" 은 맞지 않는다

지시문의 근거 중 이 수치만 다르다. 결정 자체는 그대로 이행했고 여전히 최선의 선택지지만,
데이터 품질 판단이 걸린 숫자라 정확히 남긴다.

호가는 **종목별 스케줄**이라(`due[(symbol,"book")]`) 종목당 호가 주기 = `tier3_orderbook_s` 다.
종목 수와 무관하다. 즉 16s → 4s 는 **4배**지 27배가 아니다.

| 호가 주기 | 호가당 체결 | 판정 시 호가 평균 나이 |
|---:|---:|---:|
| 16s (이전) | 109건 | 8.0s |
| **4s (결정)** | **약 27건** | **2.0s** |
| 2s | 약 14건 | 1.0s |

호가당 체결 4건이 되려면 주기가 **0.59초**여야 하고, 10종목이면 17 req/s 로 축소천장(6.65)의
2.5배다 — 이 API 로는 불가능하다.

**실제로 사는 것은 "판정 시점 호가 나이 8s → 2s" 다.** 매수/매도 판정이 4배 정확해지는 것이지
해결되는 것은 아니다. 틱 단위 정확한 side 판정은 이 예산에서 도달할 수 없으므로, 분석은 방향 판정을
**확정값이 아니라 근사값**으로 다뤄야 한다. (2s 로 더 당기려면 5종목까지 줄여야 한다 — 여유 +2.02.)

## 6. 이 변경으로 조용히 굶는 코드 (고치지 않음, 목록만)

지시받은 5개 — 전부 실재를 확인했다. 분석 소유는 W3.

| 파일 | 참조 |
|---|---|
| `tossmon/analysis/features.py:43-44` | `TOSS_RANK_TYPE` / `MARKET_RANK_TYPE` |
| `tossmon/analysis/shots.py:73-74` | `TOSS_AMOUNT`, `DEFAULT_RANKING_TYPES` |
| `tossmon/analysis/report.py:203` | `read_rankings("TOSS_SECURITIES_TRADING_AMOUNT")` |
| `tossmon/analysis/measure/rotation_pilot.py:30-31` | `TOSS_TYPE` / `MARKET_TYPE` |
| `tossmon/analysis/measure/shot_structure.py:25-26` | `TOSS_AMOUNT` / `MARKET` |

**목록에 없던 것 3건을 추가로 찾았다.**

**(1) 라이브 검출기 — 별도 에스컬레이션함 (msg_337867257211).**
`tossmon/collector/detector.py:716` 이 `extract_precursor_features` 를 부르면서 랭킹 타입을 안 넘겨
features.py 기본값(금액 2종)을 쓴다. 전환 후 예외 없이 0 이 되고, `score_paths` 의 **가중치 0.18**
(`toss_share` 0.10 + `toss_share_slope_30` 0.04 + `toss_in_ranking` 0.04)이 사라진다. tier3 임계 0.60 은
이 항이 살아 있을 때 잡은 값이라 승격이 조용히 줄어든다. 호출부는 내 파일이지만 `toss_share` 가
**계약상 금지된 `amount_u`** 로 계산된다는 점(전환 전부터 그랬다)까지 얽혀 있어 W3 판단이 필요하다 —
결정 전까지 손대지 않았다.

**(2) 설계 문서의 정의 자체.** `docs/03_phase1_monitor_design.md:38` 은 랭킹 4종을 전제하고
**토스 쏠림도를 "Toss/Market 거래대금 비율"** 로 정의한다(`docs/01_api_analysis.md:66` 도 동일).
거래대금 목록을 안 받으므로 이 정의는 그대로는 계산 불가다 — 위 (1) 과 같은 문제의 설계 층위 표현이다.
쏠림도를 **건수 비율**로 재정의하는 것이 자연스러운 후속이고, 그러면 계약상 금지된 `amount_u` 도
같이 벗어난다. 문서는 공유/계약 소유라 수정하지 않았다(`docs/00_HANDOFF.md:153,176` 도 4종 표기).

**(3) 테스트 합성기.** `tests/synth.py:498` 의 `RANK_TYPES` 도 금액 2종뿐이다(W3 소유, 미수정).
그대로 두면 리플레이가 랭킹을 한 행도 못 받아 "랭킹 없이도 통과"하는 무의미한 테스트가 된다 —
내 `tests/test_collector_replay.py` 안에서만 이름을 매핑해 실경로를 유지했고, 매핑 결과가
`loops.RANKING_TYPES` 와 일치하는지 단언으로 고정했다. W3 의 피처 시뮬레이션은 여전히 **우리가 더는
수집하지 않는 목록**을 모델링하고 있다.

## 7. 기존 데이터

무수정. 스키마 변경 없고 마이그레이션 없다. 이미 받은 금액 목록 행은 그대로 남는다 —
`rankings_snap` 은 `ranking_type` 별 long format 이라 새 데이터가 안 들어올 뿐 기존 행과 충돌하지 않는다.
