# Orca 결함 리포트 — 장시간 다중 에이전트 오케스트레이션 (실사용 3일)

**환경**
| 항목 | 값 |
|---|---|
| Orca 앱 | **1.4.162** (runtime ready) |
| 에이전트 CLI | Claude Code 2.1.220 |
| OS / 셸 | Windows 11 Home 10.0.26200 / PowerShell 5.1 |
| 관련 capability | `orchestration.contract.v1`, `terminal.multiplex.v1`, `agent-session.host-authority.v1` |
| 사용 규모 | 1 코디네이터 + 워커 7종(W1~W7), 태스크 34개, 3일 연속(2026-07-31 ~ 08-02), 무인 장기 실행 다수 |

**요약**: 단발성 작업에서는 드러나지 않고 **장시간·다워커 운용에서만 나타나는 결함 5종**.
공통 증상은 *조용한 실패* — 명령은 성공을 반환하는데 의도한 효과가 없거나, 정상 완료가
반려되거나, 살아 있어야 할 프로세스가 통보 없이 사라진다. 각 항목에 실제 ID·에러 문자열·
재현 조건을 붙였다.

---

## 결함 1 (치명) — 정상 `worker_done` 이 lifecycle 에서 반려됨

**증상**: 워커가 올바른 `taskId`/`dispatchId` 로, 지정된 assignee pane 에서 `worker_done`
을 보내는데 Orca 가 거부한다. 오늘 하루에만 **5회 이상**, 서로 다른 3개 워커에서 발생.

관측된 에러 문자열 3종(같은 계열, 문구만 다름):
```
The caller is not the Dispatch pane.
The Dispatch capability is missing.
Dispatch capability is invalid.
worker_done dispatch ctx_7972a5f767ef belongs to task_d75fc08ed020, not task_02a4aafd5b6d.
```

**구체 사례**
| 워커 | task / dispatch | 비고 |
|---|---|---|
| W5-b | `task_999bd2c75b52` / `ctx_6ed2dc896b31` | 3회 시도 전부 반려. 워커가 pane key 가 dispatch 와 일치함을 확인했고, launch token 을 `--dispatch-capability` 로 넘겨도 무효 |
| W4-b | `task_b0bae554f20c` / `ctx_4e5ff1ec845e` | 2회 반려. "capability 가 이 터미널에 애초에 주입된 적이 없다"고 보고 |
| W5 | `task_02a4aafd5b6d` | **한 터미널에 두 태스크가 동시에 살아 있을 때**, worker_done 이 다른 태스크의 dispatch 에 묶여 반려 |

**영향**: 설계된 완료 신호가 신뢰 불가 → 코디네이터가 `task-update` 로 수동 마감해야 하고,
자동 감시가 완료를 놓친다.

**완화(현장 대응)**: 반려돼도 본문은 `Rejected worker_done:` 제목의 메일로 run 인박스에
보존된다 → 워커는 재시도 스팸 대신 `--type status` 1통 후 idle, 코디네이터가 수동 마감.

**재현 조건 추정**: 세션이 길어져 pane/PTY 가 한 번이라도 교체된 뒤, 또는 한 터미널에
활성 dispatch 가 둘 이상일 때.

---

## 결함 2 (중대) — `worker-show` 가 `dispatch --inject` 경로 디스패치에 동작하지 않음

가이드는 완료·생존 확인 수단으로 `worker-show --dispatch <id>` 를 안내하지만, 실측:

```
$ orca orchestration worker-show --dispatch ctx_9103c1dc63a6
{"ok":false,"error":{"code":"dispatch_not_found",
 "message":"Worker Dispatch ctx_9103c1dc63a6 was not found."}}

$ orca orchestration dispatch-show --task task_a9544e0f3282
{"ok":true, ... "status":"dispatched", "completed_at":null ...}   ← 같은 디스패치가 여기선 보임
```

`worker-start` 로 만든 디스패치에는 동작하고, `dispatch --inject` 로 만든 것에는
**존재하지 않는 것으로 취급**된다. 결함 1로 메일이 끊긴 상황에서 **유일한 대안 확인 수단이
경로에 따라 사라지는** 것이 문제다.

**제안**: 두 생성 경로를 동일하게 취급하거나, 가이드에 비대칭을 명시할 것.

---

## 결함 3 (중대) — 분리 기동한 무인 프로세스가 트리째 살해됨

`Start-Process` 로 분리해 띄운 장시간 프로세스(데이터 수집기)가 **에이전트 세션 정리와
함께 사망**. Windows job object 가 트리를 회수하는 것으로 보인다.

- 2026-07-31 18:07:30 — 수집기(collector) + supervisor 동시 사망, traceback 없음,
  supervisor 재시작도 안 됨(부모·자식 동시 사망) → **8분 06초 데이터 공백**
- 재기동 시도 3회 연속 사망: 종료 코드 **`0xC000013A` (STATUS_CONTROL_C_EXIT)**,
  즉 콘솔 ctrl 이벤트가 트리 전체에 전달됨. 로그에 리터럴 `^C` 만 남음
- 상관관계: 사망은 **에이전트가 그 프로세스를 고빈도 폴링하던 중**에만 발생.
  같은 명령을 fire-and-forget 하고 에이전트가 조용히 있으면 생존(2시간+ 실증)

**회피책(검증됨)**: 작업 스케줄러(`schtasks`)로 기동 → 부모가 `svchost(Schedule)` 이 되어
Orca 트리 밖에서 생존(9시간+ 2회 실증). **`Start-Process` 분리로는 불충분.**

---

## 결함 4 (중대) — 에이전트의 백그라운드 태스크가 간헐적으로 즉살됨

Claude Code `run_in_background` 로 띄운 대기 프로세스가 **15~45초 내 killed**.
버스트성(연속 4회 즉살 후 정상 완주와 혼재). 결함 3과 같은 회수 로직으로 추정.

```
2026-08-01 17:34  killed @ ~2분
2026-08-02 00:5x  killed @ 15초 / 30초 / 45초 (3연속)
```

**사용자 확인**: 이 프로젝트뿐 아니라 **다른 프로젝트 에이전트에서도 동일** → 기계 전역
(Orca 앱 수준) 현상.

**영향**: 장시간 대기·감시가 반복적으로 끊겨, 워커 완료 통보를 놓친다.
**회피책**: 하니스 Monitor(폴링형)는 7시간+ 생존 — 이쪽으로 감시를 옮겨야 했다.

---

## 결함 5 (중간) — `terminal send` / `dispatch --inject` 가 큐에 갇힘

에이전트 TUI 가 **자체 턴을 열어둔 상태**(예: Monitor 대기)면, 전송된 프롬프트가
입력창에 붙기만 하고 제출되지 않는다. 화면에 `Press up to edit queued messages` 가 뜬다.

- `--enter` 를 추가로 보내도 소비되지 않는 경우가 있음
- ESC 로 상대 턴을 끊으면 소비되지만, **큐에 있던 긴 텍스트가 잘려서 전달**된다
  (실제로 6단계 지시 중 1~5단계가 유실되고 6단계만 도착 → 워커가 "지시가 잘렸다"고 escalation)
- 하루 6~8회 발생, 매번 수동 개입 필요

**제안**: 전송된 프롬프트를 턴 종료 후 자동 제출하거나, 큐 상태를 API 응답으로 노출할 것
(`terminal send` 가 `ok:true` 를 반환하는데 실제로는 미전달인 것이 문제의 핵심).

---

## 결함 6 (경미) — 터미널 핸들이 통보 없이 재발급됨

같은 워크트리의 에이전트 터미널 핸들이 세션 중 조용히 바뀐다. 구 핸들로 dispatch 하면
`no recognized agent detected` 또는 `terminal_handle_stale`. 오늘만 4회(W7 2회, W5 2회).

**회피책**: 쓰기 직전마다 `terminal list --worktree ... --json` 으로 재확인.

---

## 종합 — 무엇이 가장 아픈가

1. **결함 1 + 2 + 4의 조합**이 치명적이다. 완료 신호가 반려되고(1), 대안 확인 수단이
   경로에 따라 없고(2), 감시 채널이 계속 죽으면(4) — **워커가 일을 끝냈는지 알 방법이
   구조적으로 사라진다.** 실제로 완료 45분 뒤에야 발견한 사례가 있다.
2. **공통 안티패턴은 "성공을 반환하는 조용한 실패"**다. `terminal send` 는 `ok:true` 인데
   미전달, `worker_done` 은 워커 입장에선 전송 성공인데 반려, 프로세스는 예외 없이 소멸.
   **호출자가 결과를 신뢰할 수 없다**는 점이 모든 항목의 뿌리다.

## 현장에서 굳힌 운용 규칙 (다른 사용자에게도 유효)

- 무인 프로세스는 **반드시 schtasks** (Start-Process 분리 불충분)
- 감시는 **하니스 Monitor 폴링** (백그라운드 대기 금지)
- 완료 판정은 **`task-list`/`dispatch-show` 폴링** (메일 도착에 의존 금지)
- 터미널 핸들은 **매 사용 직전 재확인**
- 툴콜 안에서 프로세스를 띄운 뒤 **같은 콜에서 대기하지 말 것**
- 워커 보고 본문은 **ASCII 로만** (비ASCII 본문이 인코딩 깨져 도착한 사례 있음)
