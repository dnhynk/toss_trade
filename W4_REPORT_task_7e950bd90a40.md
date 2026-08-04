# W4 — 검출기 랭킹 타입 명시 (task_7e950bd90a40)

브랜치 `w4-collector`. 라이브 호출 0건, 가동 중 수집기 무접촉.
`features.py` 무수정(W3 소유). 배포는 코디네이터가 한다.

---

## (a) 무엇을 넘겼나 — 상수 문자 그대로

`tossmon/collector/detector.py` 에 상수 2개를 두고 호출부에서 명시해 넘긴다.

```python
RANKING_TOSS_TYPE   = "TOSS_SECURITIES_TRADING_VOLUME"
RANKING_MARKET_TYPE = "MARKET_TRADING_VOLUME"
```

상수는 detector.py:61-62, 호출부는 `EventDetector.evaluate` (detector.py:732-736):

```python
        feats = extract_precursor_features(
            df_1m, rk, t0_ms, include_t0=True, symbol=symbol, curve=curve,
            calendar=calendar, baseline=baseline,
            shares_outstanding_qu=shares_outstanding_qu,
            toss_type=RANKING_TOSS_TYPE, market_type=RANKING_MARKET_TYPE)
```

수집기 안에서 `extract_precursor_features` 를 부르는 곳은 **여기 하나뿐**임을 확인했다
(`grep` 전수). 다른 경로로 새지 않는다.

`loops.RANKING_TYPES` 를 import 해서 맞추지 **않았다** — `loops -> detector` 방향의
순환 참조가 된다. 대신 두 목록이 어긋나면 테스트가 잡는다((c) 두 번째 항목).

## (b) 고쳐졌다는 증거

대조군을 같은 데이터로 돌려서 확인했다 (`test_toss_concentration_survives_on_volume_rankings`).
거래량 목록 랭킹을 주고:

| 호출 방식 | `toss_in_ranking` | `toss_share` |
|---|---:|---:|
| 옛 기본값(금액 2종) — 고치기 전 상태 | **0.0** | 0.0 / NaN |
| 명시해 넘긴 지금 | **1.0** | > 0 |

즉 소실될 뻔한 `score_paths` 가중치 **0.18**
(`toss_share` 0.10 + `toss_share_slope_30` 0.04 + `toss_in_ranking` 0.04)이 살아 있다.
티어3 임계 0.60 이 잡힌 조건이 그대로 유지된다.

## (c) 회귀 테스트 (신규 3건)

| 테스트 | 무엇을 막나 |
|---|---|
| `test_detector_passes_the_volume_ranking_types_it_actually_collects` | 호출부에서 인자가 다시 빠지는 것 (kwarg 를 직접 포착해 단언) |
| `test_detector_ranking_types_match_what_the_collector_polls` | detector 상수와 `loops.RANKING_TYPES` 의 어긋남 — import 로 못 묶으니 여기서 잡는다 |
| **`test_toss_concentration_survives_on_volume_rankings`** | **피처가 실제로 0 이 되는 것.** kwarg 전달만 보지 않고 값까지 본다 (대조군 포함) |

세 번째가 핵심이다. 인자만 확인하는 테스트는 features.py 쪽이 바뀌면 통과한 채로 조용히 죽는다.

## (d) 건드리지 않은 것

- `tossmon/analysis/features.py` — 무수정. 기본값은 여전히 금액 2종이고, 그 기본값에
  의존하는 **W3 의 분석/테스트 경로는 지금 그대로 동작한다.**
- `vol_qu` 기준 전환은 하지 않았다 (지시대로 W3 후속). 코디네이터 실측대로 금액비와
  수량비는 사실상 같은 값이라(중앙 0.2564 vs 0.2558) 오늘 밤 신호가 달라지지 않는다.
- `tests/synth.py` 는 여전히 금액 2종만 만든다(W3 소유). 내 테스트 안에서만 이름을
  매핑했다 — 직전 커밋 `1391c6e` 의 리플레이 테스트와 같은 방식이다.

**남은 것**: 없다. 이 커밋이 배포를 막고 있던 마지막 항목이다.

## (e) 규율

- 호출부만 수정, `features.py` 무수정 ✅
- 가동 중 수집기 무접촉, 라이브 호출 0건 ✅
- 브랜치 `w4-collector`, main 직접 커밋 없음 ✅
- pytest green (전체) ✅
- `worker_done` 1회 ✅

**오염 우려에 대한 정정**: 직전 태스크에서 내가 `amount_u` 비율을 계약 위반 소지로 올렸는데,
코디네이터 판단이 맞다. C-2 가 금지하는 것은 **달러 금액으로 쓰는 것**이고, 같은 스냅의 두
`amount_u` 비는 무차원이라 micro-KRW 계수가 상쇄된다. 그 근거를 상수 주석에 남겼다.
