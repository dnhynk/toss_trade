# HANDOFF — W1 (API 코어 · 라이브 프로브 · mock/픽스처)

> 오케스트레이션 런타임 리셋 대비 재개 문서. 작성 2026-07-31.
> 리셋 후 옛 taskId/dispatchId 는 무효다. 이 파일이 재개의 유일한 근거다.

## 1. 신원

| 항목 | 값 |
|---|---|
| 워커 ID | **W1** |
| 브랜치 | `feat/core-api` |
| 워크트리 | `C:\Users\dongh\orca\workspaces\toss_trade\w1-core-api` |
| **현재 HEAD** | **`1e9bcae`** (부모 `4869293` = main) |
| 워킹트리 | clean (미커밋 없음) |
| 소유 경로 | `tossmon/api/**`, `tools/live_probe.py`, `tools/mock_server.py`, `tests/fixtures/**`, `tests/test_api_*.py`, `docs/06_live_facts.md` |
| 라이브 리스 | **없음.** 보유한 적 있으나 반납 완료. 현재 아무도 보유하지 않음 |

## 2. 직전 태스크의 요지 (내 말로)

적대적 감사 2건(`docs/10_audit.md` W6, `docs/10_audit_b.md` W6b)이 독립적으로 재현한
결함 중 **내 소유 경로에 해당하는 것**을 고치라는 지시였다. 핵심은 토큰 리스가 실제로는
아무것도 보장하지 못했다는 것(BLOCKER-1). 각 항목마다 "수정 전이면 실패하는" 회귀
테스트를 붙이고, 동의하지 않는 항목은 고치지 말고 근거와 함께 보고하라는 조건이 붙었다.

## 3. 끝낸 것

전부 **main 에 머지 완료**(`4869293`)이고, 그 뒤 후속 결함 1건을 추가로 고쳤다(`1e9bcae`, 미머지).

| 커밋 | 내용 | 상태 |
|---|---|---|
| `8429fb1` | 감사 지적 수정 본체 (아래 표) | main 에 머지됨 |
| `d787c41` | live_probe 라이브 호스트 상수를 판별 전용 마커로 정리 | main 에 머지됨 |
| **`1e9bcae`** | **리스 변경의 후속 결함 2건** (§4 참조) | **미머지 — 머지 필요** |
| (이전) `a042412` | 계약 A4 초과 정밀도 반올림 | main 에 머지됨 |
| (이전) `eb21e3d` 등 | API 코어·라이브 실측·mock·픽스처 | main 에 머지됨 |

### 감사 항목별 처리 (13/13 재현 → 수정 확인)

수정 전 소스(`git checkout main -- tossmon/api tools/live_probe.py`)로 되돌려 재현
스크립트를 돌린 결과 **13 DEFECT / 13 checks**, 수정 후 **13/13 해소**를 대조 확인했다.

| 감사 항목 | 무엇이 깨져 있었나 | 수정 |
|---|---|---|
| F-1 / A-1 **[치명]** | `state_path` 상대경로 → 파일락이 CWD 종속 → 워크트리마다 다른 락 → **둘 다 발급 → 상호 토큰 살해** | 리스를 `sha256(client_id)` 유도 + 리포 밖 고정 경로로. `TOSSMON_LEASE_DIR` 로 재정의 가능 |
| H-1 / A-2 | `invalidate()` 무조건 폐기 → 지연된 401 이 방금 발급된 유효 토큰을 죽임 | `invalidate(token=None)` CAS. client 가 실제 사용 토큰을 넘김 |
| H-1 / A-4 | 토큰 발급이 rate limiter 밖 | `_issue()` 에 `limiter.acquire("AUTH")`. TossClient 가 limiter 자동 주입 |
| H-3 / B-1 | capacity==rate + 가득 찬 시작 → 유휴 후 첫 1초 **14회**(공시 10/s) | capacity=rate×0.3, 빈 상태 시작 → 첫 1초 9회 |
| H-2 / B-2 | 서버 헤더가 rate 를 무제한 상향 (**7.0→420** 재현) | 공시 한도 천장으로 클램프. `counters["limit_header_clamped"]` 로 관측 |
| H-4 | 429 감속이 성공 **건수** 기준 → 수명 약 1초 | **경과 시간** 기준(60초당 ×0.8) |
| H-5 | `ForbiddenEndpoint` 가 `TossApiError` 상속 → 광역 except 에 삼켜짐 | `Exception` 직속으로 이동 |
| M-11 | 1층 관문이 `..`/`%2e`/`%2f` 를 `{symbol}` 로 통과 | 정규화하지 않고 거부 |
| M-1 | 토큰 `error_description` 이 평문 로그로 (시크릿 에코 위험) | 길이+sha256 지문만. OAuth2 enum 외 코드 미기재, 개행 제거 |
| B-5 / M-7 | `live=True` 하드코딩 + `force429` 가 기본 실행 포함 + 라이브 호스트 상수 | 기본값을 안전한 쪽으로, `force429` opt-in, `--base-url` 은 env 유래 |

**동의하지 않아 고치지 않은 항목: 없다.** 13건 전부 재현됐다.
다만 두 가지는 감사 제안과 **다르게** 구현했다 (근거는 §6).

## 4. 다음에 할 일 (재개 지점)

1. **`1e9bcae` 를 main 에 머지**하는 것이 첫 순서다. 내용:
   - `tests/test_api_tokens.py` 가 `TOSSMON_LEASE_DIR` 를 격리하지 않아
     **전체 실행에서만 3건 실패**하고 있었다(파일 단독 실행은 통과 → 숨어 있던 종류).
     리스가 자격증명 유도 경로로 바뀐 뒤 이 파일 테스트들이 같은 더미 `client_id` 를 써서
     **머신 전역 리스 파일 하나를 공유**하게 된 탓이다. autouse 픽스처로 격리했다.
   - `release()` 가 보유자 지문(`.holder.json`)을 안 지워서, 다음 리스 충돌 때
     **이미 죽은 프로세스**를 보유자로 보고해 진단을 오도했다. 함께 삭제 + 회귀 테스트.
   - 머지 후 기대 테스트 수: **695 passed** (main 694 + holder 정리 회귀 1).
2. 그 외 **W1 에 진행 중인 미완 작업은 없다.** 지시받은 항목은 전부 끝났다.
3. 새 태스크가 오면 그때 착수. 아래 §7 의 미해결 리스크가 후보다.

## 5. 막힌 것 / 기다리던 답 — **모두 해소됨**

리셋 직전 기준으로 **대기 중인 질문은 없다.**

- 계약 문구 갱신 4건을 `ask` 로 요청했고 900초 타임아웃으로 답을 못 받았으나,
  이후 **계약 개정 A6 에서 4건 전부 반영된 것을 확인**했다:
  `ForbiddenEndpoint(Exception)`, `invalidate(self, token=...)`,
  `TokenManager(..., limiter=...)`, `TOSSMON_LEASE_DIR` — 코드와 계약이 일치한다.

## 6. 다음 사람이 모르면 손해 보는 사실

### ★ 리스 전환 위험 (운영 절차 — 코드로 못 막는다)

토큰 리스 위치가 `{state_path}.lock`(CWD 종속) → `sha256(client_id)` 유도 고정 경로로
**바뀌었다**. 따라서 **구코드로 뜬 프로세스가 잡은 락은 신코드 프로세스에게 보이지 않는다.**
구코드 프로세스가 살아 있는 상태에서 신코드로 컬렉터/프로브를 띄우면 서로 다른 락을 잡고
둘 다 발급 → **내가 고친 그 상호 토큰 살해가 그대로 재발한다.**

→ 전환 시 **구코드 프로세스를 완전히 종료한 뒤** 신코드를 기동할 것.
현재는 W5 종료·락 해제가 확인됐으므로 위험 구간은 지나갔지만, 옛 체크아웃으로 뭔가를
띄울 일이 생기면 다시 해당된다.

### 리스 관련 실무

- 락 경로: `tossmon.api.tokens.lease_path_for_client(client_id)`
  리스 디렉터리: `tossmon.api.tokens.lease_dir()`
  (기본: `%LOCALAPPDATA%\tossmon` → `XDG_STATE_HOME` → `~/.local/state/tossmon`)
- **테스트를 쓰는 워커는 반드시 `TOSSMON_LEASE_DIR` 를 tmp 로 지정하라.**
  안 하면 머신 전역 리스 디렉터리를 오염시킨다 (§4-1 이 정확히 그 사고였다).
- 리스는 **단일 머신 가정**이다. `filelock` 은 로컬 OS 락이라 다른 머신/컨테이너에서
  같은 자격증명으로 띄우면 둘 다 획득한다. 보유자 지문으로 사후 진단만 된다.

### 라이브 안전

- `tools/live_probe.py` 기본값은 이제 **안전한 쪽**이다. 실토큰 발급은
  `--live` + `TOSS_LIVE=1` 이 **둘 다** 있을 때만. 그 둘 없이 실서버를 대상으로 하면
  실행 자체를 거부한다. `--base-url` 은 기본값이 없다(`TOSS_BASE_URL` 유래, 계약 C-9).
- **`force429` 는 `--probe all` 에 포함되지 않는다.** 의도적으로 라이브 429 를 유발하고,
  같은 자격증명으로 컬렉터가 돌면 서버 측 같은 버킷의 페널티를 공유한다.
  필요하면 `--probe force429` 로 명시해야 한다.
- mock 드라이런: `TOSS_BASE_URL=http://127.0.0.1:8899 python tools/live_probe.py --probe all`

### 감사 제안과 다르게 구현한 것 2건 (의도적)

1. **B-2 클램프 천장**: 감사는 `min(limit, SPEC_LIMITS[group])` 를 제안했다. 나는 천장을
   `max(SPEC_LIMITS, config값)` 으로 했다. 운영자가 config 에 공시값보다 높게 잡은 것은
   사람의 의도적 결정인데 `SPEC_LIMITS` 로만 min 하면 코드가 그 설정을 조용히 덮어쓴다.
   "서버 헤더가 천장을 넘기는 것만 막는다"는 목적은 동일하게 달성된다. 양방향 테스트 있음.
2. **M-7 라이브 호스트**: 상수를 완전히 없애지 않고 `LIVE_HOST_MARKER` 로 남겼다.
   요청 URL 생성에 쓰지 않고 "base_url 이 실서버인가"를 **판별**해 라이브 플래그 없는
   실행을 거부하는 가드 전용이다. 이걸 지우면 실서버인지 모르는 채로 때리게 되어 더 위험하다.
   금지 대상이던 것(스킴/호스트 분할 회피, argparse 기본값)은 제거했다.

### 그 외 함정 (라이브 실측 유래 — `docs/06_live_facts.md` 참조)

- 미존재 심볼은 404 가 아니라 **200 + 응답에서 조용히 누락**. 200개 요청이 180개 응답일 수 있다.
- 소형주는 프리마켓에 `timestamp=null` 이거나 과거 시각 고정인데 `lastPrice` 는 계속 온다.
- `/candles` 의 `before` 는 **inclusive** — `nextBefore` 를 그대로 넘기면 경계 봉 1개 중복.
- 일봉 당일 봉은 장중에도 존재하며 **진행형**이다. 완성봉으로 쓰면 베이스라인이 깨진다.
- 체결 없는 분은 1분봉 자체가 빠진다(결측 분 리샘플 필요).
- 미국 `/orderbook` 은 **최우선 1레벨만** 제공한다(계약 A2).
- `/trades` 응답에 `symbol` 필드가 없다(클라이언트가 주입).

### 관측 카운터 (W4 리포트에 넣을 값어치가 있음)

- `TossClient.counters["precision_rounded"]`, `models.precision_stats()["max_digits"]`
  — 계약 A4 반올림. `max_digits` 급증은 API 변화 신호.
- `GroupRateLimiter.counters["limit_header_clamped"]`, `.last_clamped`
  — 0 이 아니면 서버가 공시 한도보다 큰 `X-RateLimit-Limit` 을 보내고 있다는 뜻.

## 7. 테스트 현재 상태

```
실행: cd C:\Users\dongh\orca\workspaces\toss_trade\w1-core-api
      .venv\Scripts\python.exe -m pytest tests/ -q -p no:cacheprovider

결과: 695 passed in 227s   (HEAD 1e9bcae 기준)
      실패 0건.
```

- main(`4869293`) 단독에서는 **3 failed / 691 passed** 다 — `test_api_tokens.py` 의
  리스 격리 누락 때문이며 `1e9bcae` 가 그것을 고친다. **파일 단독 실행에서는 통과**하므로
  전체 실행으로만 재현된다는 점에 유의.
- 환경: `.venv` (Python 3.13.7). 의존성 설치는 `.venv\Scripts\python.exe -m pip install -e ".[dev]"`.
- mock 서버: `python tools/mock_server.py --port 8899` (표준 라이브러리만, 의존성 없음).
- 전체 실행 후 `%LOCALAPPDATA%\tossmon\` 이 비어 있어야 정상이다(리스 오염 없음).

## 8. 미해결 리스크

1. **리스는 단일 머신 전제** — 다중 호스트 운영 시 서버측 리스나 공유 스토리지 락 필요.
2. **CAS 는 호출자 신뢰에 의존** — `invalidate(token=None)` 의 기본값이 구동작(무조건 폐기)이라,
   앞으로 토큰을 쓰는 새 경로가 CAS 인자를 빼먹으면 조용히 예전 취약점으로 되돌아간다.
   인자를 필수로 만드는 편이 안전하나 계약 변경이라 하지 않았다.
3. **극단 리버스 스플릿 누적** — 수정주가 일봉에서 아주 오래된 봉의 가격이 1e-6 USD 미만이면
   `0u` 로 뭉개진다. A5 로 1분봉이 원주가가 되어 위험은 줄었으나 **일봉은 여전히 해당**된다.
   백필 시 `open_u == 0` 봉이 나오는지 확인 필요(코디네이터가 W5 에 전달하기로 했음).
