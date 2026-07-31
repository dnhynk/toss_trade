# 코디네이터 상태 — 새 세션 인수인계용

갱신: 2026-07-31 15:45 KST · main = `6def9b5` · **749 passed, 1 skipped**
> (이 세션은 여기서 종료한다. 다음 코디네이터가 §0 절차로 이어받으면 된다.)

> **새 세션에서 이 프로젝트의 코디네이터를 이어받는 경우 이 문서부터 읽어라.**
> 배경·전략·아키텍처는 `docs/00_HANDOFF.md`, 계약 정본은 `docs/04_contracts.md`.

---

## 0. 인수 절차 (3분)

```bash
# 1) 내 터미널 핸들 확인 (루트 워크트리에서 실행 중인 에이전트 터미널)
orca terminal list --worktree "id:12e59c9d-6eff-4602-8e62-802907e489b4::C:/Users/dongh/toss_trade" --json

# 2) 기존 Run 에 바인딩 — 이것만 하면 워커·수집은 그대로 이어진다
orca orchestration run-use --id run_92948a1f80a5 --from <내_핸들> --json

# 3) 이제 표준 대기가 동작한다 (내용까지 담아 깨워준다)
orca orchestration check --wait --types worker_done,escalation,question,status --timeout-ms 570000 --json
```

**주의**
- `orchestration check` 는 `--from` 을 **안 받는다**. 다른 명령(`task-create`/`dispatch`/`reply`/`send`)은
  이 환경에서 `--from <핸들>` 을 **반드시 명시**해야 한다(자동 해석이 안 됨).
- `consumer_fenced` 가 나오면 바인딩이 풀린 것이다 → `run-use` 로 재바인딩.
- **감시 스크립트를 새로 만들지 마라.** 세션 초반에 `check --wait` 가 죽어서 우회 감시기를 만들었는데,
  런타임 재시작 이후에는 `run-use` 한 번으로 표준 방식이 정상 동작한다.
  우회 감시기는 "브랜치가 움직였다"만 알려줘 rebase 를 완료로 오인하게 만든다.

---

## 1. 지금 돌아가고 있는 것

| 대상 | 상태 |
|---|---|
| **W5 라이브 재수집** | **진행 중** (분리 프로세스). 15:32 기준 watch=1500, tier2=58, tier3=1, promotions=369, `watch_outside_universe=0`, `api_errors=0`. 데이마켓 → 프리(17:00) → **정규장(22:30)** → 애프터(~08:50) |
| **워커 태스크** | **전부 완료·머지됨. 진행 중인 워커 작업 없음** (W5 수집만 가동) |
| 라이브 리스 | **W5 단독 보유.** 다른 워커·코디네이터는 라이브 호출 금지 |

**정규장 개장(22:30) 이 오늘의 본 시험이다.** 확인할 것:
- **tier3 점유** (데이마켓 1/20 → 정규장에서 얼마나 차는지)
- 승격 사유 분포를 어제와 대조 — 특히 `evicted` 가 계속 0인지(어제 6,653),
  스코어 기반(`precursor`/`confirm`) 비중이 오르는지
- **개장 15분(22:30~22:45)** 이 가장 값진 구간 (LULD 밴드 2배, HOD 46.6% 형성)

**주의 — 돌고 있는 collector 는 기동 시점 코드다.** 15:43 현재도 로그에
`budget: RANKING predicted 0.33 req/s > target 3.50` 이라는 **가짜 ERROR** 가 찍히는데,
이건 W4 가 이미 고쳐 머지한 것(`e512463`)이고 **실행 중 프로세스에는 반영되지 않았을 뿐**이다.
수정이 실패한 것으로 오해하지 마라. 다음 재기동 때 사라진다.
같은 이유로 `ForbiddenEndpoint` 명시 처리도 이 프로세스에는 없다 — 프로세스가 조용히
사라지면 그것부터 의심하라.

검증 스크립트: `C:/Users/dongh/.claude/jobs/236dc45f/tmp/verify_recollect.py`
(단, `PYTHONIOENCODING=utf-8` 없이 실행하면 cp949 로 죽는다 — 콘솔 비ASCII 출력 문제)

---

## 2. 오늘(7/31) 완료된 것

- **오케스트레이션 리셋 후 복구** — `run_92948a1f80a5` 신설, 문법 변경(`run-create` → `task-create` → `dispatch`)
- **감사 blocker 3건 해소**: 토큰 리스 자격증명 단위(A6), 유니버스 필터 수집경로 연결, 룩어헤드/중복
- **유니버스 필터 실작동 검증**: `symbols` 1,678개, 대형주 5종목(NOK·AMD·ASML·META·GS) **전부 차단**,
  `$` 우선주 387개 거부, `watch_outside_universe=0` 유지
- **W1 감사 잔여 22항목 전수 판정** (해소 16 / 수정 2 / 미수정 4) + **새 시크릿 유출 U-4 발견·수정**
- **W3 M-3(겨울 매매일 분할)·M-4(반일장 곡선 오염)** — 감사는 "나중"으로 분류했으나
  백필 분석을 지금 오염시키므로 코디네이터가 blocker 로 격상
- 계약 개정 **A6**(토큰 리스·ForbiddenEndpoint·CAS·limiter) **A7**(KR 제거·상태파일 권한)
- **A7 코드 반영 완료**(`6def9b5`) — ALLOWLIST 13개로 축소.
  Windows 의 `os.chmod` 는 읽기전용 비트만 만지고 **ACL 에는 영향이 없다**는 것을 W1 이 짚어
  `icacls /inheritance:r` 로 분기했다. `os.chmod` 만 썼으면 "권한을 걸었다"고 믿으며
  실제로는 아무 보호가 없었을 것이다.

---

## 3. 다음에 할 일 (우선순위)

### (A) 머지 게이트 5종 (워커 완료 보고를 받을 때마다)
① pytest 재실행 ② **merge-base 기준** 소유권 diff ③ 계약 준수 ④ 금지사항 스캔
⑤ **머지 후 통합 스모크** (브랜치 단독 통과가 조합 통과를 보장하지 않는다 — 실제로 걸린 적 있다)

> W1 의 A7(KR 제거 + 상태파일 권한)은 `6def9b5` 로 **머지 완료**. 현재 열린 태스크 없음.

### (B) 분석 착수 전 정리 3건 (아직 미착수)
1. **사전등록 반영** — W3 가 넘긴 오염목록 해소 2건 + §2.2 문장 + §2.7 제외사유.
   `docs/12_preregistration.md` 는 **W7 소유**라 W7 에 디스패치할 것.
2. **HANDOFF-W3 §3 의 사전등록 미정합 3건** (곡선 20일 창 미구현 등).
3. **U-4 패턴 점검** — `raise X from None` 은 `__context__` 를 지우지 않는다.
   **W2 의 유니버스 시드 파일 로더**가 같은 유출을 가질 수 있다(W1 지적, 소유 밖이라 미수정).
   판정법: 예외 체인을 깊이 순회해 원문이 남는지 확인.

### (C) Phase 1-C 분석
**`docs/12_preregistration.md` 를 먼저 읽고 그대로 따를 것.**
`docs/13_trial_registry.md` 는 **첫 시행 전에** 생성해야 한다(형식은 사전등록에 규정).
분석 대상은 오늘 재수집분 + 백필. **어제(7/30) DB(`tossmon_20260730_polluted.db`)는
표적 모집단이 아니므로 분석에 쓰지 마라** — 파이프라인 검증 증거로만 보존.

---

## 4. 워커 배치 (워크트리·브랜치·모델)

repo id = `12e59c9d-6eff-4602-8e62-802907e489b4`,
워크트리 = `C:/Users/dongh/orca/workspaces/toss_trade/<name>`

| 워커 | 워크트리 | 브랜치 | 모델 |
|---|---|---|---|
| W1 | `w1-core-api` | `feat/core-api` | opus/xhigh |
| W2 | `w2-universe-store` | `w2-universe-store` | sonnet/high |
| W3 | `w3-analyzer` | `feat/analyzer` | opus/high |
| W4 | `w4-collector` | `w4-collector` | opus/xhigh + fable/xhigh |
| W5 | `w5-ops` | `w5-ops` | sonnet/high |
| W6 / W6b / W7 | `w6-audit` / `w6b-audit-opus` / `w7-prereg` | 동명 | fable / opus / fable |

**터미널 핸들은 재시작마다 바뀐다.** 항상 `terminal list --worktree ... --json` 으로 재확인하고,
워크트리당 핸들이 여러 개면 **에이전트가 아닌 셸에 디스패치하면 `no recognized agent detected`** 가 난다
— 순서대로 시도해 성공하는 것을 쓰면 된다.

스펙 원문: `coordination/specs/` (17+개). 워커별 재개 지점: `coordination/HANDOFF-*.md`.

---

## 5. 이 세션에서 비싸게 배운 운영 규율

- **워커 주장을 믿지 말고 게이트를 직접 재실행하라.** "전체 통과"라는 보고가 실제로는
  환경 문제로 일부 미실행이었던 적이 있다.
- **소유권 검증은 `git diff main..branch` 가 아니라 merge-base 기준.** main 이 앞서면 오탐이 난다.
- **idle 워커는 오케스트레이션 메시지를 안 읽는다.** 긴급 지시는 `orca terminal send`.
- **`git checkout <ref> -- <path>` 는 인덱스에 스테이징까지 한다** — 워커에게 안내할 때
  `git reset` 으로 언스테이지하라고 함께 알릴 것.
- **콘솔 출력에 비ASCII 금지** — Windows cp949 에서 `UnicodeEncodeError` 로 죽는다.
  실제로 supervisor 가 이것 때문에 STOP 직후 죽어 collector 가 고아 프로세스로 남을 뻔했다.
- **무인 프로세스를 에이전트 세션의 자식으로 띄우지 마라** — 세션 정리 때 함께 죽는다.
  `Start-Process` 분리 또는 작업 스케줄러.
- **환경이 바뀌면 우회 수단을 굳히지 말고 원래 방식을 재시험하라** (§0 의 `check --wait` 사례).
- codex 워커가 지출 한도에 걸리면 **증액 요청하지 말고 claude 로 교체**.
