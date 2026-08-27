# 77. `rankedAt` 저장 — 우리가 받은 랭킹의 나이를 계속 잰다

작성: 2026-08-27 · 소유: W4 · 기준 `feat/ranked-at` / `50030dc`

## 0. 결론

`rankings_snap`에 NULL 허용 `ranked_at_ms INTEGER`를 추가하고, API가 이미 파싱한
`RankingPage.ranked_at_ms`를 각 랭킹 행에 저장한다. 이제 `snap_ms - ranked_at_ms`로
**우리 수신 시점에서 서버 랭킹이 얼마나 늙었는지**를 모든 새 스냅에서 다시 계산할 수 있다.

- 기존 v3 DB는 v4 마이그레이션으로 열 하나만 더한다. 기존 행은 `NULL`이다.
- 새 설치의 `schema.sql`과 v3→v4 설치의 `PRAGMA table_info(rankings_snap)` 모양은 같다.
- upsert 키 `UNIQUE(snap_ms, ranking_type, duration, rank)`는 그대로이고, 충돌 시
  `ranked_at_ms`도 다른 값들과 함께 최신 페이지 값으로 갱신한다.
- `config_sig`, 폴 주기, 랭킹 깊이, 티어 상한, 승격 정책은 바꾸지 않았다.
- 별도 텔레메트리 게이지는 추가하지 않았다. 이번 변경의 원천은 스냅마다 남는 DB 열이며,
  `counter_scope`와 `WINDOW_SCOPED_GAUGES`도 그대로다.
- 배포, 라이브 API 호출, `config/config.yaml`, 워치독 변경은 하지 않았다.

## 1. 값은 이미 저장 직전까지 와 있었다

기준 코드와 변경 뒤 코드를 직접 따라갔다.

| 위치 | 확인한 사실 |
|---|---|
| `tossmon/api/client.py:602` | `result.get("rankedAt")`을 `_opt_ms`로 UTC epoch ms 또는 `None`으로 바꿔 `ranked_at_ms`에 넣는다. |
| `tossmon/api/models.py:250-255` | `RankingPage`가 `ranked_at_ms: int | None`을 이미 선언한다. |
| `tossmon/collector/loops.py:1760-1776` | 페이지를 받은 뒤 우리 관측 시각 `snap_ms`를 잡고 같은 `page`를 `insert_rankings`에 넘긴다. 기존 주석도 `rankedAt`을 알고 있었다. |
| 변경 전 `tossmon/store/writer.py:140-175` | 행 값과 INSERT/UPDATE 목록에 `ranked_at_ms`만 없었다. 여기서 처음 버려졌다. |

따라서 새 요청, 새 파서, 새 모델은 필요하지 않았다. 변경은 저장 경계에서 값을 버리지 않는
것뿐이다. `loops.py`의 주석도 `rankedAt`을 단순 참고값이라고 하지 않고, 서버 발행 시각을
저장해 `snap_ms - ranked_at_ms`를 잰다는 실제 동작으로 고쳤다.

## 2. `config_sig`는 바뀌지 않는다

`CollectorContext.config_signature()` (`loops.py:985-1017`)의 독스트링과 반환식을 확인했다.
입력은 다음 수집 **밀도·폭**뿐이다.

- 랭킹 타입과 타입별 `duration`, 깊이 `RANKING_COUNT`
- `tier3_max`
- tier3 체결·호가, tier2 호가·봉, 랭킹 폴 주기
- 켜져 있을 때만 붙는 랭킹 승격 정책 시그니처

Store, 스키마 버전, DB 열 목록, writer 값은 입력에 없다. 더 강하게 확인하려고
`origin/main:tossmon/collector/loops.py`와 작업 트리에서 AST로 이 함수 원문만 뽑아 비교했다.

```text
config_signature_equal=True
base_sha256=62aaeed09c6ed3f33fb86db2819b176782d81f2a5a3a3b3bec68d0cde7002b3e
current_sha256=62aaeed09c6ed3f33fb86db2819b176782d81f2a5a3a3b3bec68d0cde7002b3e
```

관련 회귀 검사도 2건 통과했다.

```text
2 passed in 0.77s
```

`coordination/G2G3-PREREG.md` §2-3은 창 안에서 `config_sig`가 바뀐 세션만 확증 팔에서
제외한다. 이 변경은 지문을 움직이지 않으므로 그 제외 조건에 걸리지 않으며, 이미 있는
확증 팔 1세션은 그대로 유효하다.

## 3. 스키마와 멱등 저장

### 3-1. v4와 새 설치의 모양을 맞춘 방법

v4는 다음 한 문장이다.

```sql
ALTER TABLE rankings_snap ADD COLUMN ranked_at_ms INTEGER;
```

NOT NULL, DEFAULT, CHECK는 없다. 기존 수천만 행에 원값이 없기 때문이다. `schema.sql`에도
같은 열을 테이블의 마지막 열로 선언했다. ALTER로 붙인 기존 DB와 열 순서까지 같게 하기
위해서다.

새 DB는 `schema.sql`을 v1로 실행한 뒤 v2~v4를 순서대로 돈다. 그래서 v4 적용 전에
`PRAGMA table_info(rankings_snap)`로 열 존재를 확인한다. 새 DB는 ALTER를 건너뛰고 버전
표식만 v4로 전진하며, 기존 v3 DB만 ALTER를 실행한다. 테스트에서 두 경로의 열 이름·타입·
NULL 규약·기본값·PK 표식을 전부 비교해 같음을 확인했다.

### 3-2. writer 규약

`insert_rankings`의 각 값 튜플에 `page.ranked_at_ms`를 붙였고, INSERT 열과 placeholder도
같이 늘렸다. 충돌절에는 `ranked_at_ms=excluded.ranked_at_ms`를 넣었다. 키와 트랜잭션,
행별 중복 제거(`row.rank`)는 건드리지 않았다.

같은 키를 세 번 쓰는 테스트에서 행 수는 계속 1이었고, 마지막 페이지의
`ranked_at_ms=1100`이 남았다. `None` 페이지도 그대로 SQL NULL로 저장된다.

## 4. 7.3 GiB 사본에서 ALTER 실측

라이브 DB는 URI `mode=ro`와 `PRAGMA query_only=ON`으로만 열고, Python SQLite online
backup으로 작업 트리 안의 일회용 사본을 만들었다. 데이터 행은 조회하지 않았다. 두 사본은
측정 직후 삭제했으며 라이브 DB는 바꾸지 않았다.

| 측정 | 사본 크기 | 적용 전 | 소요 | 적용 뒤 | 파일 크기 변화 |
|---|---:|---|---:|---|---:|
| raw `ALTER TABLE ... ADD COLUMN` | 7,917,838,336 B (7.374 GiB) | 열 없음 | **3.2441 ms** | NULL 허용 INTEGER 열 있음 | 0 B |
| 실제 `apply_migrations` v3→v4 | 7,917,109,248 B (7.373 GiB) | schema v3, 열 없음 | **2.9656 ms** | schema v4, 열 있음 | 0 B |

실제 크기에서 테이블 행 재작성은 관측되지 않았다. 배포 창에서 오래 걸릴 부분은 ALTER가
아니라 수집기 정지·단일 writer 확인·백업·재기동이다. 다만 `BEGIN IMMEDIATE`의 write lock
획득 시간은 다른 writer가 살아 있으면 달라질 수 있으므로, 배포 때는 collector를 완전히
정지한 뒤 마이그레이션해야 한다.

## 5. 기존 행·reader·왕복

### 5-1. 기존 행과 읽는 쪽

합성 v3 DB에 기존 랭킹 행을 먼저 넣고 v4를 적용했다. 행의 기존 값은 보존됐고 새 열은
`NULL`이었다. 같은 DB를 기존 `Reader.read_rankings`로 다시 열어 심볼과 `snap_ms`를
정상적으로 읽었다.

소스 전수 검색도 했다.

```text
reader_files=16
select_star_files=0
```

`tossmon/**`와 `tools/**`의 `rankings_snap` reader 16개는 모두 필요한 열을 명시한다.
열 끝에 NULL 허용 열이 하나 늘어도 tuple 폭이나 DataFrame 열이 조용히 바뀌는
`SELECT *` reader는 0개다. 전체 테스트가 이 분석 reader들의 기존 계약을 다시 돈다.

### 5-2. mock 응답에서 DB까지

mock HTTP 응답을 실제 `TossClient.get_rankings`로 파싱한 뒤 실제 `Store`에 썼다.

| 입력 `rankedAt` | 모델 | DB |
|---|---:|---:|
| `2026-08-27T12:34:56+09:00` | `1787801696000` | `1787801696000` |
| `null` | `None` | SQL `NULL` |

따라서 `_opt_ms`의 정상 정수 경로와 NULL 경로가 모두 API→모델→writer→SQLite를 왕복한다.

## 6. 빨강과 게이트

새 테스트를 구현 전에 먼저 돌렸을 때 마이그레이션 1건과 왕복 2칸이 모두 실패했다.

```text
3 failed in 2.72s
```

구현 뒤 writer의 `page.ranked_at_ms`를 임시로 `None`으로 바꾸는 돌연변이도 실행했다.
NULL 칸은 통과했지만 정수 왕복 칸이 정확히 실패했다.

```text
1 failed, 1 passed in 1.76s
```

돌연변이를 되돌린 뒤의 게이트:

```text
focused migration/upsert/round-trip/null: 4 passed in 1.56s
tests/test_store.py + tests/test_api_client.py: 50 passed in 29.50s
config_signature regressions: 2 passed in 0.77s
full tests/: 2467 passed, 1 skipped, 4 deselected, 23 warnings in 246.66s
```

## 7. 배포 창에서 확인할 것 — 이 과제는 실행하지 않았다

1. collector가 완전히 종료돼 SQLite writer가 0개인지 확인한다.
2. 라이브 DB 백업을 만든다.
3. v3→v4 마이그레이션을 실행한다.
4. `meta.schema_version=4`와 `PRAGMA table_info(rankings_snap)`의 NULL 허용
   `ranked_at_ms INTEGER`를 확인한다.
5. collector를 재기동한 뒤 **새 행만** `ranked_at_ms IS NOT NULL`인지 확인한다.
   기존 행의 NULL은 정상이다.

새 행의 나이는 중복 100행을 세지 않도록 rank 1만 보면 된다.

```sql
SELECT snap_ms, ranking_type, duration, ranked_at_ms,
       snap_ms - ranked_at_ms AS rank_age_ms
FROM rankings_snap
WHERE rank = 1 AND ranked_at_ms IS NOT NULL
ORDER BY snap_ms DESC;
```

이 배포는 별도 창의 한 변경으로 해야 한다. 이 작업에서는 w5-ops 코드·설정·워치독·DB에
쓰지 않았고, 라이브 API와 홀드아웃 데이터 행도 열지 않았다.
