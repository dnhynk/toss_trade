# Orca 오케스트레이션 운영 메모 (v1.4.167 기준, 2026-08-04 갱신)

**이 파일은 요약이다. 정본은 항상 `orca skills get orchestration` 이다** — 바이너리가
직접 뱉으므로 버전과 절대 어긋나지 않는다. 새 세션은 그것을 먼저 읽어라.

---

## 1. 우리 Run

```
run_92948a1f80a5   toss_trade Phase 1 — 감사 후속 수정 및 분석 준비 (리셋 후 복구)
coordinator        term_73097d88-5d25-4722-821e-9076e4075c69
```

**업데이트 흡수 확인 완료**: v1.4.167 은 업데이트 직전의 오케스트레이션 배정을
`run_3ce503ffe1e6`("Recovered orchestration work from a contract update") 로 흡수하는데,
**우리 것은 흡수 대상이 아니었다**(그 Run 은 태스크 0건). 우리 Run 은 그대로 살아 있고
태스크 74건·열린 것 5건을 유지한다. `run_legacy_local` 은 빈 감사 묘비다.

## 2. ★ 바인딩이 호출마다 풀린다 — `--run` 을 직접 넘겨라

업데이트 전후 모두 `run-use` 가 **프로세스 경계를 넘어 유지되지 않는다.**
매번 재바인딩하지 말고 **명령에 Run 을 명시**하는 것이 정답이다.

```bash
orca orchestration task-list  --run run_92948a1f80a5 --brief --json
orca orchestration check      --run run_92948a1f80a5 --json
orca orchestration check      --run run_92948a1f80a5 --ack <delivery_id> --json
```

`run-use --id <run>` 은 같은 셸 호출 안에서만 유효하다. (플래그는 `--run` 이 아니라 `--id` 다.)

## 3. `check` 읽기 모드 — 하나만 골라야 한다

- 인자 없음 = **소비형**(FIFO Delivery 를 받고 `--ack` 할 때까지 같은 배치를 재생)
- `--peek` = 소비하지 않고 미읽음 보기
- `--all` = 히스토리 전체
- **셋을 동시에 쓰면 에러**다(`Choose at most one message read mode`).

과거 메시지가 `run_legacy_local` 에 있으면 인자 없는 `check` 가
`legacy_read_only` 로 실패한다. 그때는 `--peek`/`--all` 로 읽는다.

## 4. ★ 폴링 대신 `check --wait` 를 써라 (가장 큰 개선)

지금까지 sleep 루프와 별도 감시기로 워커 완료를 기다렸다. **더 이상 필요 없다.**

```bash
orca orchestration check --run run_92948a1f80a5 --wait \
  --types worker_done,escalation,question --timeout-ms 900000 --json
# 배치를 전부 처리한 뒤 원자적으로 ack + 계속 대기:
orca orchestration check --run run_92948a1f80a5 --ack <delivery_id> --wait \
  --types worker_done,escalation,question --timeout-ms 900000 --json
```

- **타임아웃이나 `{count:0}` 은 실패가 아니라 체크포인트다.** 코딩 작업은 15~60분이 예사다.
- **하트비트와 터미널 활동은 "살아 있다"이지 "끝났다"가 아니다.** 완료 메시지가 없다고
  워커를 죽이거나 재시작하지 마라.

## 5. 워커 붙이기 — `worker-start` 가 새 정식 경로

```bash
orca orchestration worker-start --task <task_id> --worktree current --agent claude --json
orca orchestration worker-start --task <task_id> --terminal <handle> --json   # 기존 에이전트 재사용
```

`dispatch --inject` 는 여전히 유효하지만 저수준 경로다. 우리 워커는 이미 살아 있는
터미널이므로 **`--terminal <handle>` 재사용**이 맞다.

복구:
- `worker-show --dispatch <id>` 가 `ready` → 계속 기다린다
- `failed`/`stopped` → `worker-start --task <t> --retry-of <id>` + 배치 명시(자동 상속 안 됨)
- `outcome_unknown` → `worker-stop` 후 재점검, 또는 `worker-abandon`
- `worker-read --dispatch <id> --limit 50` 으로 워커 출력을 본다

## 6. 워커 완료 보고 형식 (워커에게 알릴 것)

```bash
orca orchestration send --type worker_done --subject "<status>" --body "<...>" \
  --task-id <task_id> --dispatch-id <dispatch_id> --outcome succeeded \
  --files-modified "path/a,path/b" --json
# 실패는 --outcome failed. 산문에만 실패를 적지 마라.
```

유효한 `worker_done` 은 **태스크·디스패치를 자동으로 완료 처리**한다.
뒤에 `task-update --status completed` 를 붙이지 마라.

## 7. 우리 워커 핸들 (2026-08-04)

```
coordinator        term_73097d88-5d25-4722-821e-9076e4075c69
w1-core-api        term_3a8a0ae2-a403-44a6-bb27-9dc4391c5264
w2-universe-store  term_3d90398c-b6c2-4abc-961f-95b88809db95
w3-analyzer        term_2c49ee20-13e4-4900-b578-f71aa7386cf0
w4-collector       term_8017b557-2c9f-4f22-bf36-06c919c66d58
w5-ops             term_deeb38ea-2ef0-4ad1-ae54-a20ffd5681ed
w6-audit           term_ab4cbd9a-8c78-4432-bb92-cf2cddff4b27
w7-prereg          term_40994a88-b928-41e2-8510-dc9541649e00
```

핸들은 **라우팅 메타데이터일 뿐 영속 신원이 아니다.** 어긋나면
`orca terminal list --json` 으로 재확인하고 **새 핸들 하나만** 쓴다(옛것과 동시 발송 금지).

## 8. 은퇴한 명령

`coordinator-start`, `coordinator-stop`, `run`, `run-stop` 은 **아무 효과가 없다.**
가벼운 Run 생성/바인딩의 별칭이 아니다.
