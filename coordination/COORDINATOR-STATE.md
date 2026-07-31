# 코디네이터 상태 스냅샷 (오케스트레이션 리셋 대비)

작성: 2026-07-31 12:5x KST · main = `4869293` · **694 tests green**

> Orca 런타임 재시작으로 코디네이터 세션이 `legacy_read_only` 가 됐다
> (task-create·dispatch·check·reply 전부 거부, `terminal send`·읽기는 동작).
> `orca orchestration reset --all` 로 복구 예정 — **task/dispatch/message/gate 는 전부 소실**된다.
> 코드·git·워크트리·터미널은 보존된다. 이 문서가 재개의 유일한 기준점이다.

---

## 1. 리셋 시점의 태스크 목록

`task-list` 가 막혀 대화 컨텍스트로 재구성했다. **ID 는 리셋 후 무효**가 되며, 재개 시
`coordination/specs/` 의 원문으로 새 태스크를 만들면 된다.

| 태스크 ID | 워커 | 내용 | 상태 | 스펙 원문 |
|---|---|---|---|---|
| `task_8d1d6e2a6dd1` | W1 | API 코어 + 라이브 실측 + 픽스처/목서버 | 완료·머지 | `specs/w1_spec.md` |
| `task_83dfb38ecc1a` | W1 | A4 정밀도 반올림 | 완료·머지 | `specs/w1_a4.md` |
| `task_774e5908debd` | W1 | **감사 수정** (토큰 리스·rate limit·가드레일) | 완료·머지 `d787c41` | `specs/fix_w1.md` |
| `task_7d38d9ffba93` | W2 | 유니버스 빌더 + 스토리지 | 완료·머지 | `specs/w2_spec.md` |
| `task_efd430977bd7` | W2 | A3 `imbalance_signed` 개명 | 완료·머지 | `specs/w2_followup.md` |
| `task_f78c45ac82b3` | W2 | events 중복 방지(UNIQUE+UPSERT+마이그레이션) | 완료·머지 | `specs/w2_dedup.md` |
| `task_9d07db149b14` | W2 | **`build_universe` 진입점** | 완료·머지 `15022a2` | `specs/fix_w2.md` |
| `task_823a5fc90a8c` | W3 | analyzer(baselines/labeling/features/evaluate)+synth | 완료·머지 | `specs/w3_spec.md` |
| `task_2b2108cd8b6c` | W4 | 티어드 수집 루프 + 검출기 | 완료·머지 | `specs/w4_spec.md` |
| `task_8766fea31a7d` | W4 | A5 반영 + 정밀도 텔레메트리 | 완료·머지 | `specs/w4_a5.md` |
| `task_4d406f11280e` | W4 | 이벤트 재검출 억제 + 알림 등급 | 완료·머지 | `specs/w4_dedup.md` |
| `task_0bcb54607ee9` | W4 | 리플레이 불변식 수정(통합 실패) | 완료·머지 | (대화 내 지시) |
| `task_59b9b6e505d3` | W4 | **감사 blocker A/B** | 완료·머지 `cebf32b` | `specs/fix_w4.md` |
| `task_4b3c32767a89` | W5 | ops·런북·시크릿 위생·dryrun | 완료·머지 | `specs/w5_spec.md` |
| `task_4c8c49285757` | W5 | **라이브 리허설** | 수집 완주, **최종 리포트 진행 중** | `specs/w5_rehearsal.md` |
| `task_88a85411d02f` | W6 | 적대적 감사 (fable) | 완료·머지 | `specs/w6_audit.md` |
| `task_ebd95d1bd930` | W6b | 적대적 감사 (opus, 독립) | 완료·머지 | `specs/w6b_audit.md` |
| `task_6b169dfd7810` | W7 | 사전등록 문서 | 완료·머지 `2088982` | `specs/w7_prereg.md` |

## 2. 워커 배치 (워크트리·브랜치·모델)

| 워커 | 워크트리 | 브랜치 | 모델/effort |
|---|---|---|---|
| W1 | `w1-core-api` | `feat/core-api` | claude opus / xhigh |
| W2 | `w2-universe-store` | `w2-universe-store` | claude sonnet / high (codex 한도 소진으로 교체) |
| W3 | `w3-analyzer` | `feat/analyzer` | claude opus / high |
| W4 | `w4-collector` | `w4-collector` | opus/xhigh + **fable/xhigh**(수정 태스크) |
| W5 | `w5-ops` | `w5-ops` | claude sonnet / high |
| W6 | `w6-audit` | `w6-audit` | **claude fable / xhigh** |
| W6b | `w6b-audit-opus` | `w6b-audit-opus` | claude opus / xhigh |
| W7 | `w7-prereg` | `w7-prereg` | **claude fable / xhigh** |

터미널 핸들은 **재시작마다 바뀐다** — 재개 시
`orca terminal list --worktree id:<repo>::<path> --json` 으로 항상 재확인할 것.
repo id = `12e59c9d-6eff-4602-8e62-802907e489b4`,
워크트리 경로 = `C:/Users/dongh/orca/workspaces/toss_trade/<name>`.

## 3. 리셋 시점에 대기 중이던 것

1. **W5 최종 리포트** — 야간 리허설 결과. 요구 항목:
   세션별 커버리지, 세션 전환 4회, **그룹별 예산 실측 vs 계산치**, 429·결측·재시작 이력,
   **승격 사유별 정밀도**(promotions 17,144건의 reason 별 분해 + 평균 체류시간),
   **events 216건의 `rvol_gated` 별 분리**, tape_gaps 세션별 발생률,
   그리고 **"이 데이터는 표적 모집단이 아니다"** 는 한계 명시.
2. 전 워커 `coordination/HANDOFF-<ID>.md` 작성 (리셋 직전 지시).

## 4. 리셋 후 즉시 디스패치할 예정이던 것

### (A) W4 — 감사 후속 2건 [최우선]
1. **`ForbiddenEndpoint` 미처리 예외** — A6 로 `TossApiError` 상속을 끊었더니
   `loops.py` 의 `except (TossApiError, OSError)` 에 안 잡혀 **수집 루프가 죽는다.**
   `except ForbiddenEndpoint: ctx.shutdown() + 경보` 를 명시적으로 넣어야 한다. (W1 이 넘긴 건)
2. **가짜 ERROR 로그** — `budget: RANKING predicted 0.33 req/s > target 3.50 —
   랭킹은 축소 대상이 아니다`. **0.33 은 3.50 보다 크지 않다.** 비교/메시지 로직 버그.
   무인 운영에서 가짜 경보는 경보 무시 습관을 만든다.

### (B) 감사 잔여 항목 (두 감사 합쳐 20여 건)
`docs/10_audit.md` §5, `docs/10_audit_b.md` 말미의 "Phase 2 착수 전 해소" 목록.
blocker 3건은 해소됐고, **강력권고 다수가 미처리**다. 특히:
- H-9 재시작 구멍 탐지(W4 가 처리했다고 보고 — 검증 필요)
- **미확인 1번: `X-RateLimit-Limit` 단위 실측** — 다음 라이브 리스에서 헤더 한 줄 찍으면 확정.
  가장 싼 미확인 해소.
- **M-3 겨울 UTC 날짜 분할 — 2026-11-01 부터 발현**. 그 전에 끝내야 한다.

### (C) 분석 단계 (Phase 1-C)
`docs/12_preregistration.md` 를 **먼저 읽고** 그대로 따를 것.
`docs/13_trial_registry.md` 는 **첫 시행 전에** 생성해야 한다(형식은 사전등록 문서에 규정).
분석 착수 전 필수 선행: **유니버스 필터가 적용된 상태로 재수집** (야간 데이터는 표적 모집단이 아님).

## 5. 라이브 리스 상태

**아무도 보유하지 않음.** W5 구코드 collector 정상 종료 + `token_state.json.lock` 해제 확인 완료.

⚠️ **전환 규칙 (계약 A6)**: 리스 위치가 `{state_path}.lock`(CWD 종속) →
`sha256(client_id)` 기반 리포 밖 고정 위치(`TOSSMON_LEASE_DIR` 또는 LOCALAPPDATA)로 바뀌었다.
**구코드 프로세스의 락은 신코드에게 보이지 않는다.** 구코드 라이브 프로세스를 완전히 종료하고
락 해제를 확인한 뒤에만 신코드 라이브 프로세스를 띄울 것.

## 6. 운영 규율 (재개하는 코디네이터가 반드시 지킬 것)

- **백그라운드 `check --wait` 에 의존하지 마라.** 이 환경에서 반복적으로 죽는다
  (내가 이것 때문에 완료 보고 4건과 워커 질문 1건을 놓쳤다).
  **매 턴 `orca orchestration inbox --limit N --json` 을 직접 확인**하라.
- **워커 주장을 믿지 말고 게이트를 직접 재실행**하라: ① pytest 재실행 ② **merge-base 기준**
  소유권 diff(`git diff main..branch` 는 main 이 앞서면 오탐) ③ 계약 시그니처 ④ 금지사항 스캔
  ⑤ **머지 후 통합 스모크**(브랜치 단독 통과가 조합 통과를 보장하지 않는다 — 실제로 걸린 적 있다).
- **터미널 핸들은 재시작마다 바뀐다.** 항상 `terminal list` 로 재확인.
- **idle 워커는 오케스트레이션 메시지를 안 읽는다.** 긴급 지시는 `terminal send`.
- codex 워커가 지출 한도에 걸리면 **증액 요청하지 말고 claude 로 교체**.
- `git checkout <ref> -- <path>` 는 **인덱스에 스테이징까지** 한다 — 워커에게 안내할 때
  반드시 `git reset` 으로 언스테이지하라고 함께 알릴 것(소유권 위반 오탐의 원인).

## 7. 문서 지도

- **`docs/00_HANDOFF.md`** — Phase 2 착수자용 종합 인수인계 (여기부터 읽을 것)
- `docs/04_contracts.md` — **정본 계약** (개정 A1~A6)
- `docs/10_audit.md` / `docs/10_audit_b.md` — 적대적 감사 2건 (fable / opus 독립)
- `docs/12_preregistration.md` — 분석 사전등록 (분석 착수 전 필독)
- `docs/06_live_facts.md`, `docs/11_live_rehearsal.md` — 라이브 실측
- `coordination/specs/` — **워커 스펙 원문** (리셋 후 태스크 재생성용)
- `coordination/HANDOFF-*.md` — 워커별 재개 지점
