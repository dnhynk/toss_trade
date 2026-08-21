# HANDOFF — **하드웨어 이동**

> 작성: 코디네이터 **2026-08-21 19:1x KST** · 기준 리비전 `9c2a858`
> 이 파일의 수치는 **이 세션에서 직접 확인한 것만** 적었다.

## 0. 이 파일이 무엇이고 무엇이 아닌가

**이것은 기계를 옮기기 위한 문서다.** 프로젝트 인계 문서가 아니다.

| 헷갈리기 쉬운 것 | 무엇인가 |
|---|---|
| `docs/00_HANDOFF.md` | 프로젝트 배경. 이 이동과 무관 |
| `coordination/archive/HANDOFF-W*.md` | **읽지 마라.** 2026-07-31 에 *"일어나지 않은 리셋"* 에 대비해 쓴 것이고 2 주 전 HEAD 를 못 박고 있다 |
| **이 파일** | **이번 이동 한 번을 위한 것.** 이동이 끝나면 **지워라** — 안 지우면 다음 사람이 낡은 경로를 믿는다 |

> **현재 상태의 정본은 `coordination/COORDINATOR-STATE.md` 다.** 이 파일은 *"기계가 바뀔 때
> 무엇이 깨지는가"* 만 다룬다.

---

## 1. ★ 끄기 전에 — 순서를 지켜라

**끄는 것보다 데이터를 지키는 게 먼저다.** 지나간 랭킹·호가는 **다시 못 받는다.**

```
① data/ops_state/PLANNED 를 만든다   (내용 예시는 §1-1)
② data/ops_state/STOP 을 만든다
③ 멈춘 것을 파일이 아니라 데이터로 확인한다   (§1-2)
④ 그때 복사한다                              (§2)
⑤ ★ PLANNED 를 반드시 걷는다                 (§1-3 — 2026-08-19 에 여기서 사고가 났다)
```

### 1-1. 표식 형식

`<w5-ops>/data/ops_state/PLANNED` (ASCII 로):

```
reason=hardware migration
until=2026-08-21 23:59:00
```

`until` 이 없으면 30 분 뒤 자동 만료된다. **이동이 길어질 것 같으면 넉넉히 잡아라.**

### 1-2. 멈춘 것을 **데이터로** 확인한다

파일이 사라졌다고 멈춘 게 아니다. 둘 다 봐라:

```bash
# 프로세스 (부모→자식 사슬 넷이 사라져야 한다)
powershell -Command "@(Get-CimInstance Win32_Process -Filter \"Name='python.exe'\") | Select ProcessId,CommandLine"

# 데이터 (마지막 스냅이 더 안 는다)
python -c "import sqlite3,time; c=sqlite3.connect('file:<w5-ops>/data/tossmon.db?mode=ro',uri=True); \
print((time.time()*1000 - c.execute('select max(snap_ms) from rankings_snap').fetchone()[0])/1000, '초 전')"
```

> **`Win32_Process` 의 `CommandLine` 이 비어 보일 수 있다** (S4U 세션이면 그렇다).
> 그때 프로세스가 없다고 읽으면 안 된다 — **로그가 자라는지 함께 봐라.**
> 2026-08-19 에 내가 그걸로 오판했다.

### 1-3. ★ `PLANNED` 를 반드시 걷어라

`PLANNED` 가 남아 있으면 **워치독이 수집 부재를 계획된 것으로 읽고 재기동을 안 한다.**
2026-08-19 에 내가 `STOP` 만 치우고 `PLANNED` 를 안 걷어서 **17 분 동안 자가 복구를
내 손으로 막았다.** 새 기계에서 살아난 것을 확인한 **뒤에** 걷는 게 아니라, **정지 절차를
떠날 때** 걷는다.

---

## 2. 물리적으로 옮겨야 하는 것 — **레포에 없는 것들**

`git clone` 으로는 하나도 안 따라온다. `.gitignore` 가 전부 뺀다.

| 무엇 | 어디 | 크기 | 잃으면 |
|---|---|---|---|
| **수집 DB** | `w5-ops/data/tossmon.db` (+`-wal`, `-shm`) | **5.81 GB** | **영구 손실.** 지나간 랭킹·호가는 다시 못 받는다 |
| 데이터 폴더 전체 | `w5-ops/data/` | **6.2 GB** | 로그·경보·상태파일까지. 통째로 옮기는 게 낫다 |
| API 키 | `toss_trade/api_keys`, `w5-ops/api_keys` | 1 KB × 2 | 재발급 |
| 수집 설정 | `w5-ops/config/config.yaml` | 4 KB | **`config_sig` 가 바뀌면 사전등록 무효 조건에 걸린다** |
| 운영 설정 | `w5-ops/ops/ops_config.yaml` | 4 KB | 경로·문턱 전부 |
| 토큰 상태 | `w5-ops/data/token_state.json` | 4 KB | 재발급하면 되지만, **가동 중 재발급은 토큰 살해**다 |

> **이 중 어느 것도 커밋하지 마라.** 비밀이거나 기계 고유값이다.
> `.gitignore` 가 막고 있으니 실수로 들어갈 일은 없지만, 강제로 `git add -f` 하지 마라.

**복사는 수집기가 멈춘 뒤에.** WAL 이 있어 강제 종료도 안전하지만(계약 C-6),
**멈춘 뒤 복사하면 `-wal` 정합을 신경 쓸 필요가 없다.**

---

## 3. ★ 새 기계에서 **깨지는 것 넷**

### 3-1. venv 가 절대경로를 박고 있다

`tossmon` 은 editable 설치이고 각 venv 가 경로를 **하드코딩**한다 (실측):

```
w5-ops/.venv   -> 'tossmon': 'C:\Users\dongh\orca\workspaces\toss_trade\w5-ops\tossmon'
w1/w3/w4 도 각각 자기 경로
```

**경로가 조금이라도 바뀌면 `import tossmon` 이 엉뚱한 곳을 가리키거나 죽는다.**
→ **새 기계에서는 venv 를 복사하지 말고 다시 만들어라**: 각 워크트리에서
`python -m venv .venv && .venv/Scripts/pip install -e ".[dev]"`.

> 이게 왜 중요한가: **조용히 틀린다.** 옛 경로가 우연히 존재하면 그쪽 코드를 테스트하게 된다.

### 3-2. 예약 작업은 **다시 만들어야 하고, 관리자가 필요하다**

살아 있어야 하는 것 다섯 (2026-08-21 실측):

| 작업 | 로그온 | 주기 | 실행 |
|---|---|---|---|
| `tossmon-watchdog` | **S4U** | 5 분 | `powershell -File <w5-ops>\ops\watchdog.ps1` |
| `tossmon-sentinel` | **S4U** | 30 분 | 같은 방식 |
| `tossmon-logrotate` | **S4U** | 매일 00:10 | `<w5-ops>\.venv\Scripts\python.exe -m ops.rotate_logs` |
| `tossmon-dailyhealth` | **S4U** | 매일 08:52 | `... -m ops.daily_health` |
| `tossmon-collector-oneshot` | **S4U** | 수동 | `cmd /c <w5-ops>\ops\launch_collector.cmd` |

**등록 스크립트가 있다**: `ops/register_task_scheduler.ps1`.

> ### ★ 반드시 `S4U` 로 등록해라 — 그리고 **관리자 PowerShell 이 필요하다**
> `Interactive` 로 등록하면 **대화형 세션 안에서 돌아 콘솔 이벤트를 같이 맞는다**
> (`LastTaskResult=3221225786 = STATUS_CONTROL_C_EXIT`). 2026-08-19 에 그걸로 복구가
> 실패했다. 그리고 **에이전트는 `S4U` 등록을 못 한다 — `Access is denied` 다.**
> **사람이 관리자 창에서 해야 한다.**
>
> 버릴 것 둘: `tossmon-coord-d21verdict`·`tossmon-w1-probe-d` 는 **소진된 일회성**이다.

### 3-3. ★ 토스 포털 **IP 화이트리스트**

`docs/06_live_facts.md` §IP: *"현재 실행 IP 는 등록되어 있음(403 없음)"*.
**기계·회선이 바뀌면 IP 가 바뀌고, 등록 전까지 API 가 403 을 낸다.**

**이건 자동화가 안 된다 — 사람이 토스 포털에서 해야 한다** (`README` §5-1 이
*"사용자 개입은 진짜 필요한 것: 토스 포털 IP 등록"* 이라고 못 박은 그 하나다).

> **새 기계에서 첫 기동이 403 이면 코드를 의심하지 말고 여기부터 봐라.**

### 3-4. Orca 워크트리 등록

repo id `12e59c9d-6eff-4602-8e62-802907e489b4`, 워크트리 여섯:

```
C:/Users/dongh/toss_trade                      main
C:/Users/dongh/orca/workspaces/toss_trade/w1-core-api   feat/core-api
                                          /w3-analyzer   feat/analyzer
                                          /w4-collector  feat/collector
                                          /w5-ops        feat/ops     ★ 라이브
                                          /w7-prereg     feat/preregistration
```

새 기계에서 `git worktree add` 로 다시 만들고 Orca 에 등록해야 한다.
**`w7-prereg` 는 원래 `.venv` 가 없다** (알고 있는 상태).

---

## 4. ★★ 라이브 워크트리가 `main` 보다 **런타임 코드가 뒤져 있다** — 일부러 그렇다

```
main    9c2a858
w5-ops  e7c9437     tossmon/collector/loops.py  +77
                    tossmon/collector/scheduler.py  +20
```

이 차이는 **W4 의 PR #39 계측**(`loop_lag_max_ms`·`db_write_max_ms` 게이지)이고
**동작 변경 0 줄이지만 아직 배포 안 했다.**

> **새 기계에서 `main` 을 그대로 체크아웃해 띄우면 그 계측이 같이 들어간다.**
> 그 자체로 수집이 바뀌지는 않지만, **한 창에 하나만 배포한다**는 규율에 걸리고
> *"이동 때문인지 계측 때문인지"* 를 못 가르게 된다.
>
> **권고**: 이동 직후 첫 기동은 **`e7c9437` 그대로** 띄워 이동이 깨끗한지 확인하고,
> 계측 배포는 **그다음 휴장 창에 따로** 한다.

---

## 5. 살아 있는 작업 — 이동으로 끊긴다

| | 상태 |
|---|---|
| **W5** (`task_5e2073cc2256`) | **활동 중** — 수집기 기동 경로 수리. 명세 `coordination/specs/w5_supervisor_pipe.md`. **두 번 죽은 적 있다.** 이동하면 다시 배정해야 한다 |
| **W3** | PR #45 병합 완료. 지금 대기 |
| 열린 PR | **0** (2026-08-21 19:1x) |
| 미커밋 | 루트 0. `w5-ops` 는 미추적 셋(`.cache/`, `config.yaml.bak-*`, `ops_config.yaml`) — **의도된 것** |

**끊기기 전에 W5 에게 지금까지 찾은 것을 PR 로 올리게 하는 편이 낫다** — 안 그러면
그 워크트리의 미커밋 작업이 이동 중에 사라질 수 있다.

---

## 6. 멈추면 잃는 것 — 숫자로

| | |
|---|---|
| **오늘 밤 정규장** (08-21 22:30 ~ 08-22 05:00) | 끄면 **통째로 잃는다.** 다시 못 받는다 |
| D-21 커버리지 | 08-17·08-18·08-20 **세 번 연속 통과**. 반복 관측이 하나 덜 쌓인다 |
| G-2 확증 팔 | 08-13 이후 세션이 표본이다. 빠진 밤은 **영영 안 채워진다** |
| 수집 공백 원인 | 조용한 정지가 **네 번** 났고 아직 미규명. **재현 표본이 하나 줄어든다** |

> **급하지 않으면 오늘 밤 창(05:00)이 끝난 뒤에 옮기는 게 낫다.**
> 급하면 그냥 끄고, 잃은 밤을 `coordination/daily/` 에 적어라 — **나중에 표본 수를
> 셀 때 그 구멍을 모르면 안 된다.**

---

## 7. 새 기계 기동 순서

```
① 레포 clone + 워크트리 여섯 생성
② §2 의 파일들을 제자리에 복사          (data/ 통째로가 안전하다)
③ 워크트리마다 venv 새로 만들기          (복사 금지 — §3-1)
④ 토스 포털에 새 IP 등록                 (사람이, §3-3)
⑤ 예약 작업 다섯을 S4U 로 등록           (관리자 창에서, §3-2)
⑥ w5-ops 를 e7c9437 로 두고 첫 기동      (§4)
⑦ 데이터로 확인: 랭킹 스냅이 다시 늘어나는가
⑧ PLANNED 걷기 (아직 남아 있다면)
```

### 7-1. 기동 뒤 확인 셋

```bash
# 1) 수집기가 살아 있나 — 로그와 함께 봐라
tail -2 <w5-ops>/data/watchdog.log          # sup=1 col=1 이어야 한다

# 2) 설정 서명이 그대로인가 — 바뀌면 사전등록 무효 조건에 걸린다
grep -o 'config_sig=[^ ]*' <w5-ops>/data/collector.log | tail -1

# 3) 판정 도구가 DB 를 제대로 읽나 (기준선 자가검사가 무결성 검사도 겸한다)
python tools/d21_verdict.py --session 2026-08-20     # SELF-CHECK: PASS 여야 한다
```

**③ 이 `SELF-CHECK: PASS` 를 내면 DB 가 이동을 무사히 넘긴 것이다.**
기준선 10 세션을 다시 계산해 얼린 표와 대조하기 때문이다.

---

## 8. 이동이 끝나면

1. `coordination/daily/<날짜>.md` 에 **잃은 밤과 공백 길이**를 적는다
2. **이 파일을 지운다** — 낡은 경로가 남아 있으면 다음 사람이 그걸 믿는다
3. `coordination/COORDINATOR-STATE.md` 의 경로·워크트리 표를 새 기계 기준으로 고친다
4. W5 를 다시 배정한다 (§5)
