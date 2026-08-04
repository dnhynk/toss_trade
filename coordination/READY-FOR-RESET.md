> # ⚠️ 낡은 문서 (2026-07-31 작성)
>
> **이 파일은 2026-07-31 리셋 시점 기준이며 이후 나흘치가 반영돼 있지 않다.**
> 현재 상태는 `COORDINATOR-STATE.md`, 그날의 인프라 변경은 `docs/30_infra_20260804.md`,
> Orca 운영법은 `ORCA-OPERATIONS.md` 를 봐라.
> 아래 내용은 **그때의 기록**으로만 읽어라.

# 리셋 준비 완료

작성: 2026-07-31 13:0x KST · **`orca orchestration reset --all` 을 실행해도 된다.**

---

## 준비 상태 요약

| 항목 | 상태 |
|---|---|
| main HEAD | 아래 §5 참조 · **704 tests green** (핸드오프 머지 후 재실행 확인) |
| 워커 핸드오프 | **8/8 작성 완료, 전부 main 에 머지됨** (`coordination/HANDOFF-*.md`) |
| 워커 미커밋 변경 | **0** (전 워커 커밋 완료) |
| 코디네이터 상태 | `coordination/COORDINATOR-STATE.md` |
| 워커 스펙 원문 | `coordination/specs/` **17개** — 리셋 후 태스크 재생성용 |
| 라이브 리스 | **아무도 보유하지 않음** (W5 collector 정상 종료 + 락 해제 확인) |
| 라이브 프로세스 | **없음** |

## 1. W1 decision_gate 처리 완료

reply 가 막혀 `orca terminal send` 로 직접 답을 전달했다. 판단 내역:

**(a) 전환 위험** — 워커 경고가 정확했고 실제로 위험한 상태였다. W5 의 **구코드 collector 가
오늘 12:34 까지 살아 있었다.** 처리 순서를 지켰다: W5 정상 종료 → `token_state.json.lock`
해제 확인 → 그 다음 머지. 전환 규칙을 **계약 A6** 에 명문화했다 —
구코드 라이브 프로세스를 완전히 종료하고 락 해제를 확인한 뒤에만 신코드 라이브 프로세스를 띄운다.

**(b) 계약 문구 4건** — 전부 승인, `docs/04_contracts.md` **개정 A6** 으로 반영:
1. `ForbiddenEndpoint(Exception)` — `TossApiError` 상속 제거
2. `TokenManager.invalidate(token=None)` — CAS
3. `TokenManager.__init__(..., limiter=None)` — AUTH rate limit
4. `TOSSMON_LEASE_DIR` env 신설 (폴백: LOCALAPPDATA / XDG_STATE_HOME → `~/.local/state/tossmon`)

추가로 워커가 감사 제안과 다르게 구현한 **B-2 변형도 승인**했다 — 헤더 상한 천장을
`min(header, SPEC)` 이 아니라 `max(SPEC, config값)` 으로. 운영자가 config 에 공시값보다 높게
잡은 것은 의도적 결정이고 코드가 조용히 덮어쓰면 안 되기 때문이다.

## 2. 리셋으로 잃는 것 / 잃지 않는 것

**잃는 것**: task·dispatch·message·gate (runtime-global).
**잃지 않는 것**: 코드, git 이력, 브랜치, 워크트리, 터미널, 그리고 이 `coordination/` 문서 전부.

리셋 시점에 **미완료 태스크는 W5 의 라이브 리허설 최종 리포트 하나뿐**이며,
그 산출물(`docs/11_live_rehearsal.md`, `tools/dryrun_night.py`)은 이미 커밋·머지됐다.
나머지 워커는 작업이 전부 main 에 머지돼 **재개할 것이 없다.**

## 3. 리셋 후 복구 절차

1. `coordination/COORDINATOR-STATE.md` §4 "리셋 후 즉시 디스패치할 예정이던 것" 을 읽는다.
2. 워커 터미널 핸들을 **재확인**한다 (핸들은 재시작마다 바뀐다):
   `orca terminal list --worktree id:12e59c9d-6eff-4602-8e62-802907e489b4::C:/Users/dongh/orca/workspaces/toss_trade/<name> --json`
3. `coordination/specs/` 의 원문으로 `task-create` → `dispatch --inject`.
4. 각 워커의 `coordination/HANDOFF-<ID>.md` 를 해당 워커에게 다시 읽히면 컨텍스트가 복원된다.

## 4. 리셋 직후 최우선 작업 (W4)

감사 후속 2건이 대기 중이다:

1. **`ForbiddenEndpoint` 미처리 예외** — A6 로 `TossApiError` 상속을 끊은 결과
   `loops.py` 의 `except (TossApiError, OSError)` 에 잡히지 않아 **수집 루프가 죽는다.**
   `except ForbiddenEndpoint: ctx.shutdown() + 경보` 를 명시적으로 넣어야 한다.
2. **가짜 ERROR 로그** — `budget: RANKING predicted 0.33 req/s > target 3.50` 은
   **0.33 이 3.50 보다 크지 않으므로 잘못된 경보**다. 무인 운영에서 가짜 경보는
   경보 무시 습관을 만든다.

그 다음은 감사 잔여 항목(강력권고 다수)과 Phase 1-C 분석이며,
**분석은 `docs/12_preregistration.md` 를 먼저 읽고 그대로 따라야 한다.**

## 5. 검증

리셋 직전 상태를 커밋으로 고정한다. 이 문서를 담은 커밋이 main 의 마지막이며,
그 시점에 `pytest` **704 passed** 를 코디네이터가 직접 재실행해 확인했다.
