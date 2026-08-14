# W3 — `tools/d21_verdict.py` (D-21 커버리지 판정 러너)

> 배정: 코디네이터, 2026-08-14 20:0x KST · 워크트리 `w3-analyzer` / 브랜치 `feat/analyzer`
> 이 파일이 명세다. 코디네이터 워크트리에 있으므로 **읽기만** 해라.

## 0. 네가 일할 곳

```
C:/Users/dongh/orca/workspaces/toss_trade/w3-analyzer      브랜치 feat/analyzer
```

**다른 워크트리를 건드리지 마라.** 특히
`C:/Users/dongh/orca/workspaces/toss_trade/w5-ops` 는 **라이브**다 — 그 안에서 수집기가
실제로 돌고 있다. 읽기(DB·로그)는 하되 **쓰기는 절대 금지.**

시작:

```bash
git fetch origin
git merge --ff-only origin/main    # feat/analyzer 는 ahead=0 이라 깨끗하다
```

`--ff-only` 가 실패하면 **멈추고 보고해라.** 아무것도 강제하지 마라.

## 1. 무엇을 만드나

새 파일 **`tools/d21_verdict.py`** 하나. `coordination/D21-COVERAGE-PREREG.md` 에
사전등록된 판정을 집행하는 계측기다. **그 문서를 먼저 전문으로 읽어라.** 그게 명세이고,
이 파일과 그 문서가 어긋나면 **문서가 이긴다** — 그리고 어긋난 자리를 보고에 적어라.

**건드리지 마라**:
- `tools/d21_coverage.py` — 사전등록된 기준선 표를 낸 물건이다. 고치면 자가 움직인다
- `coordination/D21-COVERAGE-PREREG.md` — §1~§4 는 사전등록으로 얼어 있다

경로:

```
DB   C:/Users/dongh/orca/workspaces/toss_trade/w5-ops/data/tossmon.db     (mode=ro 로만)
로그 C:/Users/dongh/orca/workspaces/toss_trade/w5-ops/data/collector.log
```

### 1a. 커버리지 (사전등록 §1)

정규장 = 그날 **13:30~20:00 UTC**.
모집단 = `rankings_snap` 에서 `ranking_type='TOSS_SECURITIES_TRADING_VOLUME'` 이고
`rank<=N` 이고 `snap_ms` 가 창 안인 distinct `symbol`.
**N = 10 과 100 둘 다** 낸다.

- **B — 테이프 커버리지**: 그 종목이 **같은 창 안에** `ts_ms` 를 가진 `trades_snap` 행을
  1 건 이상 남긴 비율
- **A — 좌석 커버리지**: 그 종목이 창 안에 `promotions.reason='ranking_tier3'` 를 받은 비율
- **비용**: 창 안 `promotions` 중 `reason='capacity_fill' AND to_tier=3` 건수

**A 와 B 는 다른 값이고 어느 쪽도 다른 쪽을 포함하지 않는다** (사전등록 §3-2). 둘 다 낸다.

### 1b. 기준선 자가검사 — 이게 이 도구의 핵심이다. 빼지 마라

기준선 10 세션을 다시 계산해서 `D21-COVERAGE-PREREG` §2 에 **이미 사전등록된** 값과
대조한다. 아래를 상수로 박아라 (top-10):

| session | ranked | w/tape | cover% | capacity_fill->3 |
|---|---|---|---|---|
| 2026-07-31 | 41 | 3 | 7.3 | 0 |
| 2026-08-03 | 45 | 5 | 11.1 | 196 |
| 2026-08-04 | 30 | 1 | 3.3 | 246 |
| 2026-08-05 | 35 | 8 | 22.9 | 155 |
| 2026-08-06 | 35 | 3 | 8.6 | 199 |
| 2026-08-07 | 47 | 8 | 17.0 | 172 |
| 2026-08-10 | 54 | 14 | 25.9 | 194 |
| 2026-08-11 | 70 | 12 | 17.1 | 245 |
| 2026-08-12 | 52 | 11 | 21.2 | 231 |
| 2026-08-13 | 58 | 11 | 19.0 | 202 |

요약이 `n=10 / min 3.3% / 중앙 17.1% / max 25.9%` 로 나와야 한다.
top-100 은 요약만 사전등록돼 있다: `n=10 / min 2.6% / 중앙 4.7% / max 8.0%` — 요약만 대조.

한 칸이라도 어긋나면 (백분율 ±0.05pp 허용, 건수는 정확히) **무엇이 어긋났는지 찍고
판정을 내지 않은 채 0 이 아닌 코드로 종료한다.** 사전등록된 기준선을 재현 못 하는 자는 자가 아니다.

### 1c. 무효 조건 (사전등록 §3-4) — 대상 세션에 대해

1. **수집 공백**: 창 안 `rankings_snap` 의 distinct `snap_ms` 를 정렬해 연속 차가
   **60 초 초과**인 것을 센다. 건수 + 최악 몇 개(시각·길이)
2. **`config_sig` 불변**: 로그의 telemetry 줄에서 창 안에 있는 `config_sig=` 값의 distinct
   집합. **정확히 1 개**여야 한다. 값도 찍어라
   - 로그 시각은 **로컬 벽시계 KST(UTC+9)** 다. 형식:
     `2026-08-14 19:25:25,197 INFO    telemetry session=... config_sig=... md_peak_1s=9 ...`
   - 창 13:30~20:00 UTC = 그날 **22:30 KST ~ 다음날 05:00 KST** 라 **로컬 자정을 넘는다.**
     이걸 틀리면 조용히 절반만 본다
   - `ops/rotate_logs.py` 가 `*.log` 를 크기 기준으로 `*.<stamp>.log.gz` 로 회전시킨다.
     창이 **00:10 KST logrotate 를 지나가므로** 형제 `collector.*.log.gz` 도 (gzip 으로)
     함께 읽어 합쳐라. 안 그러면 창 중간 회전이 스캔을 조용히 자른다
3. **프로브 없음**: 프로브를 쐈으면 무효다. **이건 가짜로 자동화하지 마라.** 기계로 확인
   가능한 증거(창 안 mtime 을 가진 프로브 산출물, 로그의 프로브 흔적)를 찍되,
   **"운영자 확인 필요"라고 명시적으로 라벨을 붙여라.** 정직한 "증거는 이것, 확인은 사람"
   이 지어낸 검사보다 낫다
4. **부분 데이터 아님**: 창 안 첫/마지막 `snap_ms` 를 창 경계와 나란히 찍어라

### 1d. 사전등록 §5 가 추가로 요구하는 것

- 창 안 **`md_peak_1s`** 와 **`rank_peak_1s`**(= `RANKING` 첨두) 분포: 개수·max·p95 +
  작은 히스토그램. `docs/62` §5-3 의 프로브 여유 논증(*"한 초 최악 4/5, 여유 1"*)이
  여기 달려 있다
- 창 안 건수: `reason='ranking_tier3'`, `reason='ranking_hold_expired'`, 그리고
  **쿨다운 위반** = 같은 종목이 `ranking_tier3` 를 **600 초 미만** 간격으로 두 번 받은 건수
  (배포된 구성이 `cd600s` 다). 건수 + 해당 종목·시각 쌍

### 1e. 밴딩

숫자 뒤에 사전등록 §3-1 / §3-2 / §3-3 의 밴드를 **기계적으로** 적용해 어디 떨어지는지
찍는다. 이건 네 판단이 아니라 얼린 규칙의 적용이다 — 문서를 그대로 미러링한 상수에서
끌어내고, 각 줄 옆에 절 번호를 병기해라.

**§1c 무효 조건이 하나라도 걸리면 `INVALID` 를 찍고 어떤 밴드도 찍지 마라**
(§3-4.4: 부분 데이터로 표를 만들지 않는다).

`docs/61` 시뮬레이션의 55.1% 를 인용하는 자리에는 **반드시 `[미재현]`** 을 병기해라
(`COORDINATOR-STATE` §1-2b, 사용자 결정).

### 1f. 인터페이스·제약

- `--session YYYY-MM-DD` (기본 `2026-08-14`), `--db`, `--log` 로 덮어쓸 수 있게
- **대상 세션 데이터는 아직 없다.** 창은 2026-08-14 22:30 KST 에 열린다. 그러니 대상 창이
  비었거나 부분이면 **깨끗하게 degrade** 해야 한다 — 죽지 말고, 판정도 내지 마라.
  **대상 경로를 실데이터로 시험할 수 없으므로, 지난 세션(`--session 2026-08-13`)으로
  대상 경로를 실제 행 위에서 돌려 모든 갈래를 밟아라**
- **콘솔 출력은 ASCII 만.** Windows cp949 콘솔에서 비 ASCII 는 `UnicodeEncodeError` 를
  내고, 이 레포에서 실제로 그것 때문에 프로세스가 죽은 적이 있다. (소스 주석의 한국어는 괜찮다)
- 읽기 전용: `mode=ro` URI, 라이브 API 호출 0 건, 라이브 워크트리에 쓰기 0 건
- DB 가 3.5 GB 다. 인덱스를 타게 짜라 (`ix_rankings_type_ms`, `ix_trades_ts`,
  `ix_promotions_symbol_ms`, `trades_snap` 은 `(symbol, ts_ms, ...)` 키의 `WITHOUT ROWID`).
  전체 1 회 실행에 몇 초 걸리는지 보고해라

### 1g. 테스트

`tools/*.py` 에 테스트가 붙는 관례인지 `tests/` 를 보고 따라라. 붙는다면 순수 로직
(창 산술, 자정 넘는 KST→UTC 로그 파싱, 공백 탐지, 쿨다운 위반 탐지, 밴딩)에 테스트를
붙이되 **합성 픽스처 DB** 로 해라 — 라이브 3.5 GB DB 에 의존하는 테스트는 만들지 마라.

## 2. PR 전에 통과할 게이트

각각 **실제 출력**을 붙여라:

1. `.venv/Scripts/python.exe -m pytest -q` — 초록이어야 한다
   (main 기준선 2,159 passed / 1 skipped / 4 deselected, 약 291 초)
2. `.venv/Scripts/python.exe tools/d21_verdict.py --session 2026-08-13` — 전체 출력
3. `.venv/Scripts/python.exe tools/d21_verdict.py` — 대상 데이터 부재 시 깨끗한 degrade
4. `git diff --stat main...HEAD` (점 셋) 가 시킨 파일만 건드리는지

## 3. PR

```bash
git add -A && git commit      # 한국어. 무엇을 왜, 변경마다 한 줄 근거
git push -u origin feat/analyzer
gh pr create --base main --head feat/analyzer --title "..." --body "..."
```

PR 본문: 무엇을 재는지, 각 출력이 사전등록 어느 절을 담당하는지, 위 게이트 출력,
그리고 **네가 확인하지 않은 것**을 명시적으로.

## 4. 병합 증거

병합 뒤 코디네이터가 `main` 에서 이걸 `grep` 한다. 이 문자열들이 실제로 들어가야 한다:

```
tools/d21_verdict.py        ranking_tier3
tools/d21_verdict.py        capacity_fill
tools/d21_verdict.py        [미재현]
```

## 5. 이 레포의 규율 — 너도 여기 걸린다

- **판정하지 마라.** 숫자와 **측정 조건**(창 길이·표본 수·방법)을 가져와라.
  *"성립 안 함"* 이 아니라 *"N 초 창, 오차 X% 에서 관측 안 됨"*
- **"동작합니다" 에는 명령과 출력을 붙여라.** 안 돌렸으면 **"실행하지 않았음"** 이라고 적어라
- **버그를 고치기 전에 원인 가설과 그것을 반증할 관측을 먼저 진술해라.**
  특수 케이스 분기로 증상만 없애지 마라
- **최소 diff.** 기존 구조·네이밍·스타일 유지. 요청 안 한 리팩터링 금지
- 사용하지 않는 추상화·조기 일반화 금지
- **확신이 없으면 추측으로 메우지 말고 멈추고 물어라**

## 6. 보고

PR 번호·URL, 브랜치 HEAD sha, 게이트 출력, 이 명세와 `D21-COVERAGE-PREREG.md` 가
어긋난 자리, 그리고 **확인하지 않은 것의 목록**.
