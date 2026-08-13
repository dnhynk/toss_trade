# 61. 랭킹 진입을 승격 사유로 — **tier2 로는 테이프가 한 건도 안 는다** (D-21)

> **[현행]** 소유 W4 · 2026-08-13 · ★ **기본값 꺼짐으로 병합한다. 배포는 코디네이터가 한다**
> 상태 표기의 뜻과 전수 목록: [`docs/INDEX.md`](INDEX.md)

<details>
<summary><b>이 문서의 지도 — 절 9 개</b></summary>

- 0. 측정 조건 (먼저 읽을 것)
- 1. **지금 규칙을 코드에서 읽어 적었다** — 그리고 데이터로 대조했다
- 2. 설계안 셋 — 축은 넷이고, 하나는 이미 닫혀 있다
- 3. 시뮬레이션 — 9 정규장에 실제로 흘렸다
- 4. 비용 — req/s · 디스크 · 소진 일수
- 5. **가장 중요한 관측**: 좌석을 살 예산이 지금 없다
- 6. 구현 — 플래그 뒤에서, 기본값 꺼짐
- 7. 켤 때 확인할 것 (배포는 내 일이 아니다)
- 8. 이 문서가 **말할 수 없는 것**
- 9. 회귀 · 재현 명령

</details>

작성: W4, 2026-08-13
받은 것: 사용자 결정 D-21 — *"랭킹 진입을 승격 사유로 추가."*
근거 문서: `docs/59` §6-3 (랭킹 상위의 71~99% 가 테이프에 없다)

---

## 0. 측정 조건 (먼저 읽을 것)

| 항목 | 값 |
|---|---|
| 코드 | `feat/collector` (`origin/main` `7b91ff9` 병합 후) |
| **라이브 API 호출** | **0 건.** 확인용 요청도 안 쐈다 |
| **수집기** | **건드리지 않았다.** 재기동·설정 변경 0. `config/config.yaml` 미수정 |
| DB | `w5-ops/data/tossmon.db` — **`mode=ro` 전용** |
| 로그 | `w5-ops/data/collector.log` (읽기 전용) |
| 도구 | **`tools/ranking_promotion_sim.py`** (이 태스크에서 신설, 커밋함). 애드혹 아님 |
| 실행 | `python tools/ranking_promotion_sim.py --db <db> --log <log> --out data/ranking_promotion_sim.json` |
| 관측 시각 | **2026-08-13 16:2x KST.** DB·로그가 지금도 자라므로 재실행하면 소수점이 움직인다 |
| 시뮬레이션 창 | `rankings_snap` **정규장만**(09:30~16:00 ET, EDT 고정 −4h). 거래량 2 종 **9 세션**, `TOP_GAINERS` **4 세션**(08-07 22:26 수집 시작) |
| 텔레메트리 창 | **2026-08-12 22:30 이후 287 줄** — `docs/52` 송신시각 수정 경계 뒤만 본다 |
| 라이브 설정 | `usage_ratio 0.85` · `tier3_max 10` · `tier3_trades_s 3` · `tier3_orderbook_s 4` · `tier1_sweep_s 45` · `ranking_snap_s 12` (`w5-ops/config/config.yaml`, 텔레메트리 `config_sig` 와 일치) |
| 회귀 | **2,143 passed · 1 skipped · 4 deselected** (main 2,113 + 신규 30) · 변이 게이트 **19/19** |

> **`docs/59` 와 같은 자료를 보지만 창이 다르다.** 저쪽은 `--until-ms` 로 끝을 못 박았고
> 이쪽은 안 박았다. 사건 수를 두 문서 사이에서 소수점까지 비교하지 마라.

---

## 1. 지금 규칙을 코드에서 읽어 적었다 — 그리고 데이터로 대조했다

### 1-1. 승격·강등·축출 (파일:줄)

| 무엇 | 값 | 자리 |
|---|---|---|
| tier2 승격/강등 임계 | **0.35 / 0.22** | `detector.py:69` `DEFAULT_THRESHOLDS` |
| **tier3 승격/강등 임계** | **0.60 / 0.42** | 같은 줄 |
| 정원 채우기 최저 자격 | 그 티어의 **유지선**(tier3 = 0.42) | `detector.py:_fill_floor` |
| 축출 마진 | **0.08** | `detector.py:72` `EVICTION_MARGIN` |
| 활동·랭킹 유래 고정 진입 점수 | **0.30** (= 0.22 + 0.08) | `detector.py:78` `ACTIVITY_ENTRY_SCORE` |
| 재진입 쿨다운 | **600 초** | `detector.py:94` `ACTIVITY_REENTRY_COOLDOWN_S` |
| dwell(히스테리시스) | **120 초** | `config.detector.promote_hysteresis_s` |
| tier2 정원 / tier3 정원 | **300 / 10** | `config.universe.tier2_max` / `tier3_max` |
| 랭킹 승격 컷 | **상위 10 위** | `loops.py:130` `RANKING_PROMOTE_TOP` |
| 랭킹 수집 깊이 | **100 위** | `loops.py:127` `RANKING_COUNT` |
| 한 번에 한 칸 | `min(target, st.tier + 1)` | `detector.TierStateMachine.on_new_data` |
| 정원 채우기 | `fill_to_capacity(3, …)` — 매 `run_tier3_micro` 사이클 | `loops.py` `run_tier3_micro` |

### 1-2. ★ `trades_snap` 은 **tier3 에서만** 채워진다

**코드**: `store.insert_trades` 를 부르는 자리는 `loops._poll_trades` **하나뿐**이고,
그 함수를 부르는 것은 `run_tier3_micro` **하나뿐**이다. tier2 루프(`run_tier2_candles`)는
`upsert_candles_1m` 만 부른다. tier1 스윕은 `/prices` 만 본다.

**데이터로 대조했다** (읽기만 한 것이 아니다):

```
distinct symbols ever promoted to tier3 : 985
distinct symbols with any trades_snap row: 985
the two sets are identical               : True     <- 차집합 양쪽 0
```

> **이것이 이 태스크의 뼈대다.** 테이프는 tier3 멤버십의 함수이고 **다른 입구가 없다.**

### 1-3. ★ 그래서 지금의 랭킹 승격은 **테이프를 한 건도 안 늘린다**

랭킹 승격 경로는 **이미 있다** (`loops._ranking_triggers`). 그런데 두 겹으로 막혀 있다:

1. **tier2 까지만 올린다** — `ctx.tiers.force(sym, 2, "ranking_entry", …)`.
2. `TOSS_SECURITIES_*` 타입에서만 동작한다(`toss = page.ranking_type.startswith(...)`),
   그리고 `TOP_GAINERS` 는 `rankings_once` 가 `_ranking_triggers` **앞에서** `continue`
   하므로 아예 이 함수에 닿지 않는다.

`promotions` 테이블 실측 (2026-07-31 ~ 08-13, 13 일):

| `to_tier` | 사유 | 행 수 |
|---:|---|---:|
| 3 | `capacity_fill` | 4,513 |
| 3 | `confirm` | 817 |
| 3 | `precursor` | 178 |
| 2 | `ranking_entry` | **1,011** |
| 2 | `price_activity` | 140,906 |
| 2 | `staleness_drop` | 44,751 |
| 2 | `first_print` | 8,028 |

> **`ranking_entry` 1,011 건이 전부 `to_tier=2` 다. tier3 로 간 것은 0 건이다.**
> tier3 입구는 `capacity_fill` · `confirm` · `precursor` **셋뿐**이고 전부 스코어 경로다.
> 랭킹은 그 셋 중 어디에도 없다.

그 경로가 닿은 종목은 **80 개**(13 일)이고, 그중 68 개가 **나중에** tier3 에 갔다.
그런데 그 68 개는 랭킹 때문에 간 것이 아니라 tier2 에서 봉 스코어를 받아 간 것이다 —
`to_tier=3` 의 사유가 전부 스코어 경로이므로 그렇게밖에 읽을 수 없다.

**`docs/59` §6-3 의 *"티어 승격은 랭킹이 아니라 별도 점수로 정해진다"* 는 정확했다.**

---

## 2. 설계안 셋 — 축은 넷이고, 하나는 이미 닫혀 있다

명세가 준 축 넷: **어느 랭킹 타입 · top-N · 정원 · 쿨다운/축출**.

**축출은 열지 않는다.** 2026-08-04 에 무너진 자리가 정확히 거기다
(`STRATEGY-VERDICTS` §4.4-E). 세 안 모두 랭킹 차선은 **빈자리에만 들어가고 기존 멤버를
절대 밀어내지 않는다**(`compete=False`). 그래서 남은 축은 셋이다.

**세 안은 같은 기계의 세 설정이다** — 좌석 수 `K`, 유지 시간 `hold_s`, 놓는 규칙 `policy`.

| | **안 A — 재배분** | **안 B — 순증 1** | **안 C — 순증 2** |
|---|---|---|---|
| `tier3_slots` | 1~2 | 1 | 2 |
| `tier3_max` | **10 그대로** | **11** | **12** |
| 좌석의 출처 | 기존 10 석에서 뗀다 | 순증 | 순증 |
| MARKET_DATA 계획 | **6.011 req/s (불변)** | 6.594 | 7.178 |
| 계획 천장(7.225) 대비 | **83.2%** | 91.3% | **99.4%** |
| 디스크 | **+0 GiB/일** | +0.0082 | +0.0164 |
| 잃는 것 | `capacity_fill` 자리 1~2 석 (스코어 상위 후보) | 예산 여유 | **예산 여유가 사실상 0** |

`policy` 와 랭킹 타입·top-N 은 세 안에 **직교**한다 — §3 이 그 조합을 잰다.

> **안 C 는 계획 천장의 99.4% 다.** 계획 천장은 *"계획 밖 호출(재시도·승격 직후 백필·
> 베이스라인·tier2 호가)의 몫"* 을 남기려고 있는 값이다(`config.example.yaml` 하단).
> 그 몫을 0 으로 만드는 설정이라 **권고하지 않는다.** 숫자는 §4 에 있다.

---

## 3. 시뮬레이션 — 9 정규장에 실제로 흘렸다

`tools/ranking_promotion_sim.py` 가 `rankings_snap` 을 시각 순으로 재생하며 좌석 차선을
돌린다. **라이브에 아무것도 안 쐈다.**

성공 기준은 08-04 가 강제한 것 그대로다:

> *"**전이 0 을 안정화로 읽지 마라.** 건강한 시스템은 정당한 신규 승격을 계속 낸다.
> 성공 기준을 **진동 0 그리고 신규 승격 > 0** 으로 바꿨다."* (`STRATEGY-VERDICTS` §4.4-E)

### 3-1. `policy` 가 결과를 4~10 배 가른다

상위 10 위 · `hold_s` 300 초 · 정규장만. `커버%` = 좌석에 앉아 본 종목 / 그 창에서 상위
10 위에 **한 번이라도** 오른 종목.

| 랭킹 타입 | policy | K | 승격/일 | 상위 10 에 온 종목 | 앉아 본 종목 | **커버%** |
|---|---|---:|---:|---:|---:|---:|
| `TOP_GAINERS` | rotate | 1 | 77.8 | 134 | 28 | **20.9** |
| `TOP_GAINERS` | rotate | 2 | 155.5 | 134 | 59 | **44.0** |
| `TOP_GAINERS` | sticky | 1 | 1.0 | 134 | 4 | 3.0 |
| `TOP_GAINERS` | sticky | 2 | 2.0 | 134 | 8 | 6.0 |
| `TOSS_..._VOLUME` | rotate | 1 | 75.6 | 234 | 87 | **37.2** |
| `TOSS_..._VOLUME` | rotate | 2 | 151.1 | 234 | 129 | **55.1** |
| `TOSS_..._VOLUME` | sticky | 1 | 1.7 | 234 | 14 | 6.0 |
| `TOSS_..._VOLUME` | sticky | 2 | 4.1 | 234 | 33 | 14.1 |
| `MARKET_..._VOLUME` | rotate | 1 | 68.1 | 422 | 150 | **35.5** |
| `MARKET_..._VOLUME` | rotate | 2 | 136.2 | 423 | 241 | **57.0** |
| `MARKET_..._VOLUME` | sticky | 1 | 4.3 | 419 | 37 | 8.8 |
| `MARKET_..._VOLUME` | sticky | 2 | 10.6 | 419 | 75 | 17.9 |

> **`sticky` 는 머리에 눌러앉는다.** `docs/35` §5-5 가 적은 *"머리는 안 움직인다 —
> 분당 4.3 회의 `order` 변화는 **꼬리에서** 일어난다"* 가 그대로 나온다. 좌석 하나를
> 9 정규장 내내 4 종목이 나눠 가진 칸도 있다(`TOP_GAINERS` sticky K1: 4 종목).
> **지금 라이브의 tier2 랭킹 경로가 사실상 `sticky` 다** — 80 종목/13 일과 같은 크기대다.

`hold_s` 를 900 초로 늘리면 rotate 의 커버%가 대략 **절반**이 된다
(예: `TOSS_..._VOLUME` K2 55.1% → 40.2%). 좌석 회전이 그만큼 느려진다.

### 3-2. 진동 — **48 개 조합 전부 0 이다**

```
plans failing the 4.4-E gate: 0 of 48
min promotions/day over every plan and every session: 1
worst re-promotion gap seen: 600.3 s   (cooldown is 600 s)
```

- **진동 0**: 같은 종목이 좌석을 놓았다가 **쿨다운(600 초) 안에** 다시 앉은 경우가
  48 조합 전부에서 **0 건**이다. 가장 짧은 재진입 간격이 600.3 초 — 쿨다운이 실제로
  경계에서 물고 있다.
- **동결 아님**: 어느 조합, 어느 세션에서도 **하루 최소 1 건 이상** 신규 승격이 났다.

**08-04 와 크기를 비교한다**: 그날의 진동은 **승격 136 건/분**이었고 배포 후 동결은
**1 건/분 미만**이었다. 여기서 가장 공격적인 조합(rotate K2 D300)이 **155 건/일 ≈
0.11 건/분**이다. **세 자릿수 아래다.**

### 3-3. 말라죽음 — 좌석은 **항상 100% 차 있다**

모든 조합에서 `full_snapshot_pct = 100`, `empty_snapshot_pct = 0`. 상위 10 위에
하루 35~91 종목이 지나가는데 좌석은 1~2 석이라 **수요가 공급을 압도한다.**

> **그래서 좌석이 비어 낭비되는 시나리오는 없다.** 반대로 **좌석 수가 곧 커버리지**다 —
> K 를 1 에서 2 로 올리면 커버%가 거의 정확히 2 배가 된다(20.9→44.0, 37.2→55.1,
> 35.5→57.0). 이 선형성이 §4 의 비용 표를 그대로 커버리지 표로 읽게 해 준다.

---

## 4. 비용 — req/s · 디스크 · 소진 일수

### 4-1. 요청 — **좌석 하나가 0.5833 req/s 다**

산식은 `budget.TierPlan.rates()` 그대로다 (숫자를 두 곳에 두지 않았다):

```
MARKET_DATA = ceil(tier1_max/200)/tier1_sweep_s + tier3/tier3_trades_s + tier3/tier3_orderbook_s
            = 8/45 + tier3/3 + tier3/4
좌석 1 개    = 1/3 + 1/4 = 0.58333 req/s
```

| | tier3 | req/s | Δ | 계획 천장(7.225) 대비 | 천장 아래? |
|---|---:|---:|---:|---:|---|
| 지금 | 10 | **6.0111** | — | 83.2% | 예 |
| +1 (안 B) | 11 | 6.5944 | +0.5833 | **91.3%** | 예 |
| +2 (안 C) | 12 | 7.1778 | +1.1667 | **99.4%** | 예 (겨우) |
| +3 | 13 | 7.7611 | +1.7500 | 107.4% | **아니오** |

```
budget 8.500  plan_ceiling 7.225  shrink_ceiling 8.075
headroom 1.214 req/s  ->  max extra tier3 slots at current periods: 2
```

**주기를 안 바꾸면 순증은 최대 2 석이다.** 그 이상을 원하면 `tier3_trades_s` /
`tier3_orderbook_s` 를 늦춰야 하고, 그건 밀도를 파는 일이라 **별도 결정**이다
(D-9 가 이미 그 축의 안건이다). **이 문서는 주기를 건드리지 않았다.**

**부수 요청 둘** — 계획에 없지만 실재한다:

- `/stocks`(GROUP_STOCK): 차선 후보의 tier0 판정. 심볼당 **1 회 캐시**라 정상 상태에서
  추가 호출은 0 이고, 신규 유입은 상위 10 위 기준 하루 **26~47 종목**이다
  (`TOP_GAINERS` 33.5 / `TOSS_..._VOLUME` 26.0 / `MARKET_..._VOLUME` 46.6).
  배치 200 이므로 폴당 최대 1 콜, STOCK 천장 4.25 req/s 대비 무시할 수 있다.
- 승격 직후 **1 분봉 백필**(GROUP_CHART): 버퍼가 없는 신규 종목에만 붙는다.
  rotate K2 D300 에서 **하루 14~24 종목**이다. CHART 계획 여유가 +0.89 req/s 이고
  그 여유의 용도가 바로 이 백필이다(`config.example.yaml` 하단).

### 4-2. 디스크 — **여기는 병목이 아니다**

행당 바이트는 `docs/60` §4-1 과 같은 방법으로 **쟀다**(같은 스키마·같은 인덱스의 빈
복제본에 실제 행을 밀어 넣고 페이지 수를 읽는다. `length()` 로는 B-트리도 인덱스도 안 잡힌다):

| | 바이트/행 | 표본 |
|---|---:|---:|
| `trades_snap` (+`ix_trades_ts`) | **97.17** | 300,000 행 |
| `orderbook_snap` (+`ix_ob_symbol_ms`) | **260.64** | 300,000 행 |

멤버당 행수는 최근 3 일 실적을 텔레메트리의 tier3 멤버 수 평균으로 나눈 것이다:

| 항목 | 값 |
|---|---:|
| `trades_snap` 행/일 (최근 3 일) | 229,926 |
| `orderbook_snap` 행/일 | 130,027 |
| tier3 멤버 평균 (텔레메트리 287 표본) | **6.38** |
| **멤버당 `trades` 행/일** | **36,039** |
| **멤버당 `orderbook` 행/일** | **20,380** |
| **멤버당 GiB/일** | **0.00821** |

| 안 | 추가 GiB/일 | DB 증가(0.398 GB/일) 대비 |
|---|---:|---:|
| A (재배분) | **0** | 0% |
| B (+1) | 0.0082 | **2.2%** |
| C (+2) | 0.0164 | **4.4%** |

C: 여유 **91.66 GB**, DB 지금 **3.56 GiB**. `docs/60` 의 214 일 추정에 대해
안 C 는 **약 4% 짧아지는 정도**다 — 폴 5 초(214→109 일)와 **크기가 다르다.**

> **디스크로는 셋 다 살 수 있다.** 이 변경의 제약은 디스크가 아니라 **1 초 창**이다.

---

## 5. ★ 가장 중요한 관측 — **좌석을 살 예산이 지금 없다**

표를 만들다 로그에서 나온 것이고, 위 어느 표보다 이것이 먼저다.

`docs/52` 송신시각 수정 이후 창(2026-08-12 22:30~, 텔레메트리 **287 줄**):

| | 값 |
|---|---|
| `md_peak_1s` | 평균 8.56 · **p50 9 · p95 10 · max 10** (한도 **10**) |
| 한도에 닿은 표본 | **48.4%** |
| `md_limiter_peak` | 평균 8.84 · **p50 10** |
| `tier3_cap` | 평균 6.69 · **p50 8** · max 10 |
| **`tier3_cap` 이 설정값 10 아래인 표본** | **72.8%** |
| `tier3` 실제 멤버 | 평균 6.38 · p50 5 |

> ### 두 줄로
> 1. **1 초 창은 이미 한도에 붙어 있다.** 표본의 절반에서 `md_peak_1s` 가 10 이다.
> 2. **정원 10 은 명목일 뿐이다.** 예산 가드가 이미 표본의 72.8% 에서 그 아래로 깎고
>    있고, 실제 멤버 중앙값은 **5** 다.

**그래서 안 B·C(순증)는 명목 정원을 올리는 것이지 실제 좌석을 사는 것이 아닐 수 있다.**
가드가 먼저 깎는 대상이 tier3 정원이기 때문이다(`budget.should_shrink` → `set_capacity`).
게다가 계획률을 올리면 축소 판정이 **더 자주** 걸린다.

**반면 안 A(재배분)는 그 문제를 안 만든다** — 계획률이 불변이라 가드의 판정이 안 바뀌고,
좌석은 `capacity_fill` 이 가져갔을 자리를 랭킹이 대신 쓰는 것뿐이다.

> **권고는 하되 결정하지 않는다** (명세 §4). 내 권고는 **안 A 를 먼저, `rotate` 로,
> 랭킹 타입 하나만** 켜는 것이다. 근거 셋:
> (ㄱ) 예산 증가 0 이라 08-04 형 축소 연쇄를 만들 수 없다,
> (ㄴ) `rotate` 가 `sticky` 대비 커버리지 4~10 배인데 진동은 둘 다 0 이다,
> (ㄷ) 타입 하나면 `config_sig` 경계가 하나라 앞뒤 구간을 가르기 쉽다.
> **어느 타입인지는 고르지 않는다** — `docs/59` §7-2 가 남긴 *"표적 층 사건 최다 vs
> 잴 수 있는 것 최다"* 의 선택이고, 그건 분석 목적이 정할 문제다.

**안 A 가 잃는 것도 적어 둔다**: 좌석 1~2 석은 `capacity_fill`(스코어 상위 후보)이
가져갔을 자리다. 13 일간 `capacity_fill` 4,513 건이 tier3 를 채웠으므로, 좌석 2 석은
그중 대략 **1/5 을 랭킹으로 바꾸는 것**이다(정원 10 중 2). 그 교환이 옳은지는
**결과를 재기 전에는 모른다** — 그것이 `docs/59` §9-1 의 열린 질문이다.

---

## 6. 구현 — 플래그 뒤에서, 기본값 꺼짐

### 6-1. 무엇이 들어갔나

| 파일 | 무엇 | 왜 |
|---|---|---|
| `config.py` | `RankingPromotionConfig` (전 필드 꺼짐 기본값) + 파서 | 절이 없으면 = 꺼짐 |
| `detector.py` | `TierStateMachine.release()` | 빌린 좌석을 **돌려받는** 경로. `_maybe_demote`(약함 판정)와 뜻이 다르다 |
| `detector.py` | `fill_to_capacity(..., reserve=0)` | 예약이 없으면 정원이 매 사이클 꽉 차서 차선이 **영원히 못 들어온다** |
| `loops.py` | `_ranking_tier3_lane()` + `rankings_once` 배선 | 좌석 규칙 본체 |
| `loops.py` | `config_sig` 꼬리 · 텔레메트리 `rk_t3_seats`·`rk_t3_promotions` | 경계와 관측 |
| `loops.py` | 좌석을 **상태파일에 저장/복원** | 저장 안 하면 재시작마다 **고아 좌석**이 남는다 — 티어는 `seed` 로 살아나는데 좌석 원장이 비어 아무도 놓아 주지 않고, 그대로 좌석 수만큼 예산이 샌다 |
| `config.example.yaml` | 주석 처리된 `ranking_promotion` 절 | 켜는 법과 위험을 같은 자리에 |

**워치리스트도 는다** — 차선은 상위 N 종목을 `ctx.watch()` 로 tier1 에 올린다(기존
`_ranking_triggers` 와 같다). 예산 계획은 `tier1_max`(1,500)로 계산하므로 **계획 req/s 는
안 변한다**(`refresh_plan` 은 실제 인원이 아니라 정원을 쓴다). 정원에 닿으면
`watch()` 가 거절한다.

`config/config.yaml` 은 **건드리지 않았다** (gitignore 이고 배포는 내 일이 아니다).

### 6-2. 꺼짐이 기본값이라는 것을 **테스트가 증명한다**

`tests/test_ranking_promotion.py` §A (27 건 중 6 건):

| 테스트 | 무엇을 고정하나 |
|---|---|
| `test_the_section_is_absent_by_default_and_that_means_off` | 절이 없으면 `enabled=False` |
| `test_config_signature_is_byte_identical_while_the_flag_is_off` | **꺼짐에서 `config_sig` 가 한 글자도 안 바뀐다** |
| `test_config_signature_changes_the_moment_it_is_turned_on` | 켜는 순간은 **반드시** 바뀐다 |
| `test_the_lane_does_nothing_at_all_while_the_flag_is_off` | 랭킹 스냅을 흘려도 티어가 한 칸도 안 움직인다 |
| `test_capacity_fill_reserves_nothing_while_the_flag_is_off` | `reserve=0` — 정원을 지금처럼 꽉 채운다 |
| `test_telemetry_carries_the_lane_fields_and_they_read_zero_when_off` | 키는 있고 값은 0 (0 과 "키 없음" 은 다르다) |
| `test_a_stale_state_file_cannot_resurrect_seats_while_the_flag_is_off` | 옛 상태파일의 좌석이 **꺼진 채로 살아나지 않는다** |

> **`config_sig` 를 꺼짐에서 안 바꾸는 것이 중요하다.** 바꾸면 데이터에 **없는 경계**가
> 생긴다. `usage_ratio` 가 지문에 없어서 D-8 변경이 데이터에 안 남았던 것의 **반대 실패**다.

### 6-3. 08-04 재발 방지를 코드와 테스트 양쪽에 심었다

- **축출 금지**: 차선은 `compete=False` 로만 들어간다.
  `test_the_lane_never_evicts_an_existing_tier3_member` 가 고정한다.
- **동결 금지**: 좌석을 놓는 것이 `_ranking_tier3_lane` 의 **첫 일**이다.
  `test_the_lane_keeps_promoting_instead_of_freezing` 이 20 스냅에서 승격 20·반납 19 를 센다.
- **진동 금지**: 쿨다운 600 초. `test_a_released_symbol_cannot_sit_down_again_inside_the_cooldown`.
- **상한 준수**: `test_the_lane_can_never_hold_more_than_its_slots` — 예산 산식이 이 상한
  위에 서 있다.
- **재시작 고아 금지**: `test_seats_survive_a_restart_so_no_orphan_holds_a_tier3_slot`.
- **반쯤 켜진 설정은 기동 실패**: 넷 중 하나만 적으면 `ValueError`. 조용히 꺼진 것처럼
  보이는 상태가 제일 나쁘다.

---

## 7. 켤 때 확인할 것 (배포는 내 일이 아니다)

**배포 전**
1. **한 창에 하나만**, 휴장 창에 (D-18).
2. `tier3_max` 를 **같이 올릴지** 먼저 정한다 — 안 올리면 재배분(예산 불변), 올리면 순증.
3. 순증을 택했다면 §5 를 다시 읽어라. 지금 `tier3_cap` 은 표본의 72.8% 에서 이미 10 아래다.

**배포 직후 (첫 정규장)**
| 볼 것 | 기대 |
|---|---|
| `config_sig` | `rkp…` 꼬리가 붙었다. **이 시각이 데이터 경계다** |
| `rk_t3_seats` | 0 이 아니고 `tier3_slots` 를 안 넘는다 |
| `rk_t3_promotions` | **계속 는다.** 멈추면 동결이다 (§3-2 의 기준) |
| `tier3_cap` · `budget_shrinks` | 배포 전 대비 **안 나빠졌다** |
| `md_peak_1s` · `http_429` | 안 나빠졌다 |
| `promotions` 의 `ranking_tier3` / `ranking_hold_expired` | 둘 다 0 이 아니다 |

**되돌리기**: `ranking_promotion` 절을 지우거나 `tier3_slots: 0` 으로 두고 재기동.
코드 경로가 통째로 죽는다. **다만 그 사이에 안 받은 체결은 못 되돌린다.**

---

## 8. 이 문서가 **말할 수 없는 것**

1. **커버리지가 늘면 알파를 볼 수 있는지 말할 수 없다.** §3 의 커버%는 *"좌석에
   앉아 본 종목 비율"* 이지 *"결과를 잴 수 있는 사건"* 이 아니다. 후자는 `docs/59`
   §6-2 의 자이고 이 문서는 그걸 안 쟀다.
2. **시뮬레이션은 스코어 채널을 모른다.** 오프라인에 봉 스코어가 없어서 `capacity_fill`
   ·`confirm`·`precursor` 와의 상호작용을 못 넣었다. 잰 것은 **차선 자체의 거동**이다.
3. **`TOP_GAINERS` 는 정규장 4 세션뿐이다** (08-07 22:26 수집 시작). 거래량 2 종의
   9 세션과 **나란히 두지 마라**.
4. **금액 2 종은 안 봤다.** 2026-08-04 11:22 이후 수집이 없다.
5. **예산 가드와 차선의 상호작용을 라이브에서 안 쟀다.** 코드를 읽으면 이렇다:
   `set_capacity` 는 **점수 최약체부터** 내리고, 차선 멤버는 `compete=False` 로 들어와
   `st.score` 가 올라가지 않으므로(`force` 의 `if compete:` 안에 있다) 보통 **0.0** 이다.
   즉 가드가 정원을 깎으면 **차선이 제일 먼저 자리를 돌려준다** — 방향은 안전한 쪽이다.
   **이 상호작용에서 버그를 하나 찾아 고쳤다**: 가드가 먼저 내려놓은 종목을 좌석 만료가
   한 칸 더 내려 tier2 자리까지 뺏었다. 이제 `tier_of(sym) >= 3` 일 때만 내린다
   (`test_a_seat_that_the_budget_guard_already_took_is_not_charged_twice`).
   **그래도 이건 합성 시험이지 라이브 관측이 아니다** — 배포 후 `rk_t3_seats` 와
   `tier3_cap` 을 나란히 봐야 진짜로 갈린다.
6. **`docs/59` 의 1.78% 가 얼마나 오르는지 말할 수 없다.** 그건 배포 후 같은 러너를
   다시 돌려야 나온다.
7. **어느 랭킹 타입이 옳은지 안 골랐다.** §5 의 권고는 정책(A/rotate)까지이고 타입은
   비워 뒀다.

---

## 9. 회귀 · 재현 명령

```bash
# 시뮬레이션 + 비용 (읽기 전용, 라이브 호출 0)
python tools/ranking_promotion_sim.py \
  --db  C:/Users/dongh/orca/workspaces/toss_trade/w5-ops/data/tossmon.db \
  --log C:/Users/dongh/orca/workspaces/toss_trade/w5-ops/data/collector.log \
  --slots 1,2 --dwell 300,900 --top 10,20 \
  --out data/ranking_promotion_sim.json

# 이 변경의 테스트 (기본값 꺼짐 증명 6 건 포함)
.venv/Scripts/python.exe -m pytest tests/test_ranking_promotion.py -q

# 전체 회귀 · 변이 게이트
.venv/Scripts/python.exe -m pytest -q
python tools/mutation_accounting.py
```

출력은 §9-1 에 붙인다.

### 9-1. 실행 출력

```
$ .venv/Scripts/python.exe -m pytest tests/test_ranking_promotion.py -q
30 passed in 1.77s

$ .venv/Scripts/python.exe -m pytest -q
2143 passed, 1 skipped, 4 deselected, 23 warnings in 333.32s (0:05:33)
```

(main 기준 **2,113** + 신규 **30** = **2,143**. 차이 없음.)

### 9-2. 변이 게이트

```
$ python tools/mutation_accounting.py
  S9   KILLED   [loops.py] client -> budget 배선을 끊는다 (관측은 살아 있는데 아무도 안 가져간다)
       2 failed, 131 passed in 15.64s

19/19 mutations killed
all planted accounting defects were caught
```

### 9-3. 안 돌린 것

- **라이브에서 켜 보지 않았다.** 이 문서의 모든 수치는 `rankings_snap`·`promotions`·
  `trades_snap`·`orderbook_snap`·`collector.log` 를 읽어서 나온 것이고,
  차선의 라이브 거동은 **관측된 바 없다.**
- **`tools/poll5s_cost.py` 를 다시 돌리지 않았다.** 디스크 기준선(0.398 GB/일 · C: 여유)은
  `docs/60` 값을 인용했고, 여유만 이 세션에서 다시 읽었다(91.66 GB).
- **`config/config.yaml` 을 열어 보기만 하고 고치지 않았다.**
