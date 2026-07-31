# HANDOFF — W6b (독립 감사자 B)

> 작성 시각: 2026-07-31. 오케스트레이션 런타임 리셋 예고에 따른 재개 지점 기록.
> 리셋 후 옛 taskId/dispatchId 는 폐기한다.

## 1. 신원

| 항목 | 값 |
|---|---|
| 워커 ID | **W6b** (적대적 감사 2인 중 B. A 는 `docs/10_audit.md` / `w6-audit`) |
| 브랜치 | `w6b-audit-opus` |
| 워크트리 | `C:\Users\dongh\orca\workspaces\toss_trade\w6b-audit-opus` |
| 현재 HEAD | **`ee6ecf9d209afae4a0782ea719282131a8d10808`** ("W6b: 적대적 감사 리포트") |
| 워킹트리 | **clean** (미커밋 변경 없음) |
| 브랜치 상태 | main 보다 뒤처져 있음. **단 HEAD 는 이미 main 에 머지됨** — 유실 위험 없음 |

`git merge-base --is-ancestor ee6ecf9 main` → **YES**. 내 산출물은 전부 main 에 들어가 있다.
이 브랜치를 되살릴 필요는 없다. 필요하면 그냥 삭제해도 된다.

## 2. 직전 태스크의 요지

`main = e1ed22d` (당시 636 tests green)에 대한 **읽기 전용 적대적 감사**.

- **반증 지향**: "이 코드가 맞다"를 확인하지 말고 "어떤 입력·타이밍·순서에서 틀리는가"를 찾을 것.
- 지정 감사 항목 11개: ① 토큰 단일성 파괴 ② rate limit 초과 ③ 룩어헤드 ④ 시간대·서머타임
  ⑤ 시크릿 유출 ⑥ 크래시 후 무결성 ⑦ 계약 위반·중복구현 ⑧ GET-only 우회
  ⑨ events 중복 2층 무력화 ⑩ 승격/강등 churn ⑪ 유니버스 오염.
- 산출물은 `docs/10_audit_b.md` **하나뿐**. 코드 수정 금지. 라이브 호출·`api_keys` 열람 금지.
- 확신 없는 것은 결함으로 올리지 말고 **"미확인" 절로 분리**할 것.
- **독립성 요구**: 다른 감사자의 `docs/10_audit.md` 와 `w6-audit` 브랜치를 열지 말 것.

## 3. 끝낸 것

**커밋 `ee6ecf9` — `docs/10_audit_b.md` (880줄) 단일 파일. 코드 변경 0건.**

- 확정 결함 **19건** (치명 1 / 높음 7 / 중간 8 / 낮음 3), **미확인 7건**(별도 절로 분리).
- 각 항목에 **심각도 / 재현 시나리오(입력→잘못된 결과) / 근거(파일:라인) / 제안**.
- 13건은 **실측 재현**했다(리포트 안에 E1~E13 로 출력 인용). 재현 스크립트는
  스크래치패드에서만 실행하고 **커밋하지 않았다**(코드 수정 금지 원칙).
- 반증에 실패한 것도 명시했다 — **음성 결과도 정보**라서:
  ⑧ GET-only 이중 차단(모든 각도에서 fail-closed), ⑦ W4의 W3 함수 재사용(중복구현 없음),
  ③ 베이스라인 자기오염(실시간 경로에서 당일이 정확히 제외됨).
- 마지막에 **"Phase 2 착수 전 반드시 해소해야 할 것" 18항목**을 심각도 순으로 정리.

독립성 준수: `docs/10_audit.md` 와 `w6-audit` 브랜치를 **열지 않았다**
(감사 시점 내 브랜치에 `docs/10_audit.md` 자체가 존재하지 않았다).

## 4. 다음에 무엇을 해야 하는가 (재개 지점)

**내 감사 태스크는 완료됐다. 재개할 미완 작업은 없다.**
다음 태스크는 코디네이터가 새로 지정하는 것이다. 다만 아래 두 가지가 자연스러운 후속이다.

### 4-A. main 4869293 기준 미해소 항목 재확인 (권장 다음 태스크)

리셋 예고 시점에 main(`4869293`)을 직접 읽어 대조했다. **내 19건 중 11건이 이미 고쳐졌다.**

**고쳐짐 (main 에서 코드로 확인)**

| ID | 심각도 | main 의 처리 |
|---|---|---|
| A-1 | 치명 | `lease_path_for_client(client_id)` + 리포 밖 `lease_dir()`, 경로 `resolve()`. 리스가 **자격증명 기준**으로 바뀌었다 (계약 A6). CWD 종속성 해소 |
| A-2 | 높음 | `invalidate(token: str \| None = None)` — compare-and-swap 도입 |
| B-1 | 높음 | `_capacity_for(rate)` 도입, capacity < rate. 유휴 후 2×rate 버스트 해소 |
| B-2 | 높음 | `effective_limit = min(limit, ceiling)` (SPEC_LIMITS 상한) + `limit_header_clamped` 카운터 |
| B-3 | 높음 | `_unaccounted_attempts()` 를 `after_call` **과** `sync_rate_limits` 양쪽에서 계상 → 재시도·실패 호출이 예산에 잡힌다 |
| B-5 | 중간 | `OPT_IN_ONLY = {"force429"}`, `ALL_PROBES` 에서 제외 |
| C-1 | 높음 | `_curve_for` 가 실패(None)를 TTL 캐시하지 않는다 |
| C-2 | 높음 | `ctx.baselines.clear()` 추가 (세션 전환 지점) |
| F-1 | 중간 | `seed_suppression(symbol, t0_ms)` 신설 + 재개 경로(`loops.py:856`)에서 DB 로부터 억제 상태 재구성. 상태파일 영속화보다 나은 처방이다 |
| I-1 | 높음 | 억제 키가 `(symbol, t0 // DAY_MS)` **일 단위 dict** 로 변경 + `t0_shift_suppressed` 카운터. 내 제안 그대로 |
| K-1 | 높음 | `watch()` 가 `universe_status` tier0 게이트를 통과한 심볼만 받는다 + `watch_rejected_universe` / `watch_unknown_universe` 카운터 |

**아직 안 고쳐짐 (main 4869293 에서 코드로 확인)**

| ID | 심각도 | 근거 (main 기준) | 비고 |
|---|---|---|---|
| **A-3** | 높음→**중간** | `tossmon/config.py:318-320` 그대로: `if live is not None:` — `TOSS_LIVE` 미설정이면 yaml `live:` 가 이긴다. 계약 C-9 문구("1이 아니면 거부")와 불일치 | **A-1 수정으로 폭발 반경이 줄었다** — 같은 자격증명이면 이제 리스를 공유하므로 상호 살해가 아니라 기동 거부가 된다. 그래서 심각도를 낮췄다. 계약/구현 불일치 자체는 남아 있다 |
| **D-1** | 중간 | `analysis/features.py:102, 479, 489-493` 그대로 UTC 날짜 버킷 | ⚠️ **2026-11-01(미국 표준시 복귀)부터 자동으로 틀리기 시작한다.** 여름에 짠 테스트로는 원리상 안 잡힌다. 시한폭탄이라 우선순위를 올릴 것 |
| **D-2** | 중간 | `detector.py` 에 `last_data_ms` 없음. `last_ts_ms` 하나가 벽시계(`force`/`seed`)와 봉 시각(`on_new_data`)을 섞어 담고, `sweep` 은 벽시계와 비교 | 정상 폴링 중인 휴면 동전주가 stale 강등된다 = 전략의 표적을 골라서 내쫓는다 |
| **D-3 / B-4 / J-1** | 중간 | `SESSION_TIER_SCALE[CLOSED] = 0.0` (`loops.py:128-129`), `reconfigure_tiers` 가 caps 를 1 로 (`:1441-1442`) | 셋이 같은 뿌리. 세션 전환마다 tier2/3 전멸 → 재승격 → 재백필. churn 의 최대 단일 원인 |
| **J-2** | 중간 | `loops.py:1119-1129` — `cursor` 가 매 반복 재정렬되는 리스트를 인덱싱 | 심볼 집합이 바뀌면 회전이 어긋나 일부가 건너뛰어진다 → D-2 와 결합 |
| **F-3** | 중간 | `store/retention.py` 에 STOP/lease 가드 없음 (grep 결과 0건) | 계약 C-6 "쓰기 주체는 collector 단일 프로세스뿐" 위반 가능. 지금은 VACUUM 이 우연히 막아준다 |
| **G-1** | 낮음 | `docs/04_contracts.md:210` 여전히 `imbalance REAL` (A3 는 `imbalance_signed` 로 개명) | C-6 만 읽는 사람이 폐기된 이름 + 잘못된 중립점(0.5)을 가져간다 |
| **G-2** | 낮음 | `docs/04_contracts.md:211` events 줄에 UNIQUE(symbol,t0_ms) 없음 | 중복 방지 2층의 근거가 계약에 없다 |
| **E-1** | 낮음 | pre-commit 훅 미설치 (환경 문제, 코드 아님) | `git config core.hooksPath` 미설정, `.git/hooks/pre-commit` 없음 |

미확인 7건(U-1~U-7)은 `docs/10_audit_b.md` §12 에 확인 방법과 함께 남아 있다. 손대지 않았다.

### 4-B. 두 감사의 커버리지 공백 비교

원래 태스크가 밝힌 **부차 목적**이다. 이제 `docs/10_audit.md`(A)와 `docs/10_audit_b.md`(B)가
둘 다 main 에 있으므로 비교가 가능하다. **단, 이건 코디네이터가 지시해야 할 일이다** —
독립성 제약이 아직 유효한지 내가 판단할 수 없다. 지시가 오면 그때 `docs/10_audit.md` 를 연다.
(지금까지는 열지 않았다.)

## 5. 미해결 / 막힌 것

**없다.** 코디네이터 답을 기다리던 질문도 없다. `ask` 를 쓴 적이 없다.
`worker_done` 은 정확히 1회 발송했다 (`msg_45054f57edcf`).

## 6. 다음 사람이 모르면 손해 보는 사실

### 6-1. 테스트 실행 — 시스템 파이썬으로는 **수집 자체가 실패한다** (함정)

```
$ python -m pytest -q
ImportError: cannot import name 'synth' from 'tests'
  (C:\Users\dongh\AppData\Local\Programs\Python\Python313\Lib\site-packages\tests\__init__.py)
ModuleNotFoundError: No module named 'filelock'
→ 107 tests collected, 20 errors
```

**원인 2가지**: (a) 시스템 `site-packages` 에 `tests` 패키지가 설치돼 있어 리포의 `tests/` 를
**가린다**. (b) 시스템 파이썬에 런타임 의존성(filelock 등)이 없다.
리포가 깨진 게 아니다. **격리 venv 를 쓰면 전부 통과한다.**

```bash
python -m venv <scratch>/venv
<scratch>/venv/Scripts/python.exe -m pip install \
  "httpx>=0.27" "pyyaml>=6.0" "filelock>=3.13" "pandas>=2.2" "pyarrow>=16.0" \
  "pytest>=8.0" "pytest-asyncio>=0.23" "anyio>=4.0"
cd <worktree> && <scratch>/venv/Scripts/python.exe -m pytest -q
```

### 6-2. 테스트 현재 상태

| 대상 | 결과 | 비고 |
|---|---|---|
| `e1ed22d` (내가 감사한 커밋) | **636 passed, 0 failed** (2분 25초) | 위 venv 로 내가 직접 실행해 확인 |
| `4869293` (현재 main) | **694 green** | 코디네이터 공지값. 내가 직접 실행하지는 않았다 |

실행 시간이 2분 이상이므로 백그라운드로 돌리고 기다릴 것.

### 6-3. mock 서버 사용법 (라이브 없이 토큰 경로까지 검증하는 법)

```bash
<venv>/python.exe tools/mock_server.py --port 8899     # 백그라운드
TOSS_BASE_URL=http://127.0.0.1:8899
```
mock 은 `POST /oauth2/token` 을 지원하므로 **`live=True` 로도 안전하게** 토큰 발급 경로를
재현할 수 있다 — 스크래치패드에 더미 `api_keys`(`CLIENT_ID=dummy`)를 만들어 쓰면
실서버·실키를 전혀 건드리지 않는다. 내 A-1/A-2 재현이 이 방식이었다.
**주의**: 끝나면 mock 서버 프로세스를 반드시 종료할 것 (8899 포트 점유).

### 6-4. Windows 콘솔 인코딩

Git Bash 로 파이썬 출력을 볼 때 한글이 깨진다(cp949). `PYTHONIOENCODING=utf-8` 을 앞에 붙일 것.
출력이 깨져 보인다고 스크립트가 실패한 게 아니다.

### 6-5. 라이브 리스

**아무도 갖고 있지 않다** (W5 종료·락 해제 확인됨, 코디네이터 공지).
리셋 전후로 **라이브 호출·토큰 발급 금지. 예외 없다.** 나는 이 태스크 내내 라이브 호출 0회,
`api_keys` 열람 0회였다.

### 6-6. 감사 리포트를 읽을 때

`docs/10_audit_b.md` 의 실측 인용(E1~E13)은 전부 실제 스크립트 출력이다.
**추론만 있고 실측이 없는 항목은 그렇게 명시했거나 §12 "미확인" 으로 분리했다** —
§12 항목을 결함으로 취급하지 말 것. 그게 그 절이 존재하는 이유다.
라인 번호는 전부 **`e1ed22d` 기준**이다. main 은 이후 대폭 수정됐으므로 §4-A 의 대조표를 볼 것.

## 7. 이 파일 자체에 대해

코디네이터가 이 한 건에 한해 `coordination/` 쓰기를 허가했다. 그 외 소유 경로 밖 파일은
건드리지 않았다. 이 커밋도 `main` 이 아니라 내 브랜치 `w6b-audit-opus` 에만 올린다.
