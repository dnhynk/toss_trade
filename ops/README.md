# ops/ — 무인 운영 스크립트 (소유: W5)

절차/장애 대응/시간표는 `docs/08_runbook.md`, 시크릿 정책은 `docs/09_secret_hygiene.md` 를 본다.
이 파일은 스크립트 목록과 최소 사용법만 요약한다.

## 구성

| 파일 | 역할 |
|---|---|
| `opsconfig.py` | ops 스크립트 전용 설정 로더(`ops_config.yaml`). `tossmon/config.py`(W4)와 무관. |
| `ops_config.example.yaml` | 위 설정 예시. 실사용 시 `ops/ops_config.yaml` 로 복사(시크릿 없음 — 커밋 가능). |
| `supervisor.py` | collector 자식 프로세스 감시 + 크래시 시 지수 백오프 재시작 + 재시작 폭주 차단. |
| `healthcheck.py` | 마지막 수집 시각(테이블별)·DB 증가율·429/호출수(최선노력)·디스크 여유를 한 화면에. |
| `disk_guard.py` | 디스크 여유 공간만 별도 점검(경보 전용, 독립 스케줄 등록 가능). |
| `rotate_logs.py` | 로그 파일 크기 기준 회전(gzip) + 보관기간 경과분 삭제. |
| `watchdog.ps1` | 5분 주기 무인 감시(프로세스·텔레메트리·W4 계약 카운터·디스크·전원·상호 하트비트) + 자동 재기동. 역할 `-Role sentinel` 은 워치독 자체를 감시. 판정 근거·문턱은 `docs/11 §14~20`. |
| `watchdog_selftest.ps1` | 위 스크립트의 샌드박스 자기시험. 진짜 `watchdog.ps1` 을 임시 DataDir 에서 `-DryRunRestart` 로 돌려 경보/재시작 판정을 검증한다. **가동 중 수집기는 건드리지 않는다.** |
| `register_task_scheduler.ps1` | 위 3개를 Windows 작업 스케줄러에 등록/해제/상태조회. **기본은 DryRun.** |
| `dispatch_sweep.py` | 디스패치된 워커를 훑어 **하트비트·브랜치·보고 매니페스트를 대조**한다. 완료 보고를 믿지 않고 커밋을 기계가 확인한다. 읽기 전용(상태 변경 없음). 배경·판정표는 `docs/38_dispatch_sweep.md`. |
| `hooks/pre_commit_secret_scan.py` | 커밋 전 시크릿 문자열/파일 차단 (`docs/09_secret_hygiene.md`). |

## 빠른 시작 (Windows)

```powershell
copy ops\ops_config.example.yaml ops\ops_config.yaml    # 필요시 경로/임계값 수정
python -m ops.healthcheck                                # 현재 상태 확인 (DB 없어도 안전하게 동작)
python -m ops.disk_guard
python -m ops.rotate_logs

# 워치독을 고친 뒤에는 반드시 이걸 돌린다 (44 케이스, 라이브 무접촉)
powershell -NoProfile -ExecutionPolicy Bypass -File ops\watchdog_selftest.ps1

# 워커를 수거하기 직전에 돌린다 — 보고가 왔어도 커밋 안 됐으면 여기서 걸린다
python -m ops.dispatch_sweep            # 종료코드 0=조치불필요 1=조치필요 2=못 잰 것 있음

# 예약 작업 등록 계획만 확인(아무 것도 등록되지 않음)
powershell -File ops\register_task_scheduler.ps1 -Action Register
# 실제 등록 (되돌리기 번거로우므로 계획을 확인한 뒤에)
powershell -File ops\register_task_scheduler.ps1 -Action Register -Confirm:$true
```

## 설계 메모

- **모든 ops 스크립트는 라이브 API를 호출하지 않는다.** 로컬 SQLite(read-only)/로그 파일/
  디스크 사용량만 읽는다. 따라서 라이브 리스 없이도 항상 안전하게 실행할 수 있다.
- `healthcheck.py` 의 429/호출수 지표는 collector(W4)가 표준 포맷으로 로그를 남긴 뒤에야
  값이 채워진다. 그 전에는 `unavailable` 로 표시되는 것이 정상이다(오류 아님).
- `supervisor.py` 의 `collector_cmd` 는 `ops_config.yaml` 의 자리표시자다 — W4의 실제
  엔트리포인트가 확정되면 그 값을 갱신한다.
