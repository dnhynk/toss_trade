# HANDOFF — W5 (ops)

> 오케스트레이션 런타임 리셋 공지에 따라 작성. `coordination/` 쓰기는 이번 한 건 예외 허가분.

## 신원

- **ID**: W5
- **브랜치**: `w5-ops`
- **워크트리**: `C:\Users\dongh\orca\workspaces\toss_trade\w5-ops`
- **현재 HEAD**: `7deb292` — "W5: dryrun_night.py — §1/§3 리포트 출력 캡핑"
- **직전 rebase 기준 main**: `6b6fb4a` (A4+A5+정밀도 텔레메트리) — **주의: 코디네이터 공지에
  따르면 main이 그 뒤로 `4869293`까지 진행됐고(W1 감사수정, W4 blocker A/B, W2 유니버스
  진입점, 감사 2건, 사전등록, 계약 A6) 이 브랜치는 아직 그 지점까지 rebase 안 함.**

## 직전 태스크 요지 (내 말로 요약)

라이브 리스를 부여받아 "W5 후속 — 라이브 리허설 실행" 태스크를 수행했다:
1. 세션 무관 프로브(A-2 1분봉 보관 경계 이분탐색, A-3 봉 timestamp 시작/끝 판정) 실행.
2. A-3 도중 CRKN(리버스 스플릿 종목) 1분봉 `adjusted=true`가 계약 C-2(마이크로달러) 정밀도를
   깨는 것을 발견·에스컬레이션 → 코디네이터가 계약 A4(반올림)·A5(1분봉 원주가/일봉 수정주가)
   신설, W1/W4가 반영.
3. 프리마켓부터 즉시 `ops/supervisor.py`로 collector 상시 수집 시작(당초 정규장부터 계획했으나
   코디네이터가 "프리마켓 자체가 데이터로서 값지다"며 즉시 시작 지시).
4. 22:30(pre→regular), 05:00(regular→after), 08:50(→closed), 09:00(→day) 세션 전환 4회를
   코디네이터가 로그로 확인, 무인 운영 목적 달성.
5. 23:30 체크포인트: 계획된 정지 → rebase → pytest → **DB 마이그레이션(v1→v2, events
   UNIQUE)** 적용 → live_probe A-1(정규장) → 재기동, 전부 완료·docs/11 기록.
6. 최종 종료 지시(12:34 KST): collector 정상 종료 + 파일락 해제 확인 완료. **최종 리포트
   (`tools/dryrun_night.py`) 작업 중 오케스트레이션 리셋 공지가 와서 중단.**

## 지금까지 끝낸 것 (커밋 해시)

- `5da390f` 이전: ops/ 최초 구현(전 태스크, 이미 병합됨 — main 히스토리 참고)
- 이번 라이브 리허설 세션 커밋들(전부 `w5-ops` 브랜치, 시간순):
  - `7d7b769` supervisor.py STOP 파일이 실행 중 자식도 감지하도록 수정(라이브 리허설 전 사전 발견)
  - `a398bd8`~`bbe3038` docs/11 초안(A-2/A-3 결과, CRKN 정밀도 이슈, A4/A5 반영)
  - `5da390f` healthcheck.py POLLING_TABLES 분리(candles_1d/events/promotions 오탐 CRIT 수정) +
    supervisor.py em-dash/cp949 UnicodeEncodeError 수정 (둘 다 라이브 중 발견)
  - `45b0c0b` healthcheck.py 429 카운트 오탐 수정(밀리초 타임스탬프의 "429" 오검출)
  - `e3edf97` docs/11 23:30 체크포인트 기록(A-1 정규장, DB 마이그레이션, 관측 3건)
  - `f877a83` **dryrun_night.py 대거 확장** — 세션전환/예산실측/승격사유별정밀도+체류시간/
    rvol_gated분리/재시작이력/tape_gaps세션별/가짜budget에러카운트 전부 구현+테스트 8건
  - `7deb292`(HEAD) dryrun_night.py §1/§3 출력 캡핑(238심볼 전개로 리포트가 1600줄+ 됐던 것 수정)

## 다음에 할 일 (재개 지점 — 구체적으로)

**최종 리포트가 아직 미완성이다.** 순서:

1. `git log --oneline main -5`로 main 최신 확인 → **`git rebase main`** 먼저 하고
   (지금은 `4869293`까지 와 있다고 들었다 — W1/W2/W4 변경이 섞여 있으니 rebase 후
   `pytest -q` 로 전량 재확인 필수).
2. `.venv\Scripts\python -m pytest -q` 전량 통과 확인. **직전 확인치는 645 passed
   (커밋 f877a83 시점) — 이후 7deb292 는 `tests/test_dryrun_night.py` 19개만 개별 확인했고
   전체 스위트는 리셋 공지 때문에 결과를 기다리지 못했다(백그라운드로 돌리던 중 중단).**
3. 최종 리포트 재생성:
   ```
   .venv\Scripts\python tools\dryrun_night.py \
     --config ops\ops_config.yaml \
     --start "2026-07-30T21:35:30+09:00" \
     --end "2026-07-31T12:34:23+09:00" \
     --data-caveat "<유니버스 필터 미적용 경고 문구 — 아래 §미해결 참고>" \
     --out data\reports\dryrun_2026-07-30_night.md
   ```
   (`ops/ops_config.yaml`은 gitignore 대상 아니지만 이 워크트리에 로컬로만 존재 — 없으면
   `ops/ops_config.example.yaml` 참고해 새로 만들 것. `collector_cmd`의 절대경로는 이
   워크트리 기준이라 그대로 써도 된다.)
4. 리포트를 열어서 §1(커버리지 요약+최하위25) / §2(429) / §3(결측, 상위40) / §4(이벤트 후보) /
   §5(텔레메트리 요약) / §6(세션전환 4회) / §7(예산 실측 vs 계산 6.42/2.73) / §8(승격
   사유별 정밀도+체류시간 — churn 분석, promotions 이 마지막 확인 시점 34,353건까지 자람) /
   §9(events rvol_gated 분리) / §10(재시작 이력 — 계획 1+무계획 2) / §11(tape_gaps 세션별) /
   §12(BudgetGuard RANKING 가짜 ERROR 빈도) 전부 값이 제대로 나오는지 육안 확인.
5. `data/tossmon.db.backup_pre_v2migration` 백업 파일이 워크트리에 남아있다 — 리포트에
   마이그레이션 전/후 행수(171→16)를 이미 docs/11 §4-2에 적어뒀으니 참고만 하면 됨(재작업 불요).
6. 완료되면 `worker_done` — 단, **주의: 지금 orchestration 자체가 legacy_read_only라 안 될
   수 있다.** 코디네이터가 리셋 후 새 dispatch로 다시 부를 때까지는 `orca orchestration
   send/check`를 시도하지 말 것(사이드이펙트 없다고 하지만, 새 taskId/dispatchId를 받은
   뒤에만 재개하라는 코디네이터 지시가 있었다).

## 막힌 것 / 코디네이터 결정 대기 중이던 것

없음 — 마지막으로 받은 지시(collector 종료 + 최종 리포트 생성)는 명확했고, 답을 기다리던
질문은 없었다. 리포트 작성 도중 리셋 공지가 와서 중단했을 뿐이다.

## 다음 사람이 모르면 손해 보는 사실

1. **라이브 리스는 지금 아무도 없다** — 이 워커가 종료 시 `data/token_state.json.lock`이
   해제됐음을 `filelock.FileLock(...).acquire(timeout=0)`로 직접 확인했다(방금 재확인도
   LOCK FREE). collector 프로세스도 전부 종료 확인함(`Win32_Process` 조회로 재확인 완료).
2. **collector를 다시 띄우려면 라이브 리스가 재부여돼야 한다** — 이 워커는 리스 없이는
   `TOSS_LIVE=0`으로만 동작해야 한다(불변 규칙). 다음 사람이 실수로 라이브를 켜지 않도록
   `config/config.yaml`(gitignore, 로컬 전용)의 `api.live: true`가 아직 남아있을 수 있다 —
   리스 없이 재개한다면 이 파일을 `live: false`로 되돌리거나 삭제할 것.
3. **collector 자체 프로세스는 이 에이전트 세션의 Bash background 작업 트리에 두면 안 된다**
   — 이 세션 도중 최소 2회, 원인 불명의 이벤트로 Bash background 작업이 통째로 죽는 것을
   겪었다(collector 포함). 완화책으로 PowerShell `Start-Process -WindowStyle Hidden
   -RedirectStandardOutput/-RedirectStandardError`로 완전히 분리된 프로세스로 띄웠더니
   그 뒤로는 살아남았다. 다음에 다시 collector를 띄울 일이 있으면 이 방식을 쓸 것
   (`ops/register_task_scheduler.ps1`로 Task Scheduler 등록하는 게 더 근본적인 해법 —
   이번엔 시간 관계상 안 했다).
4. **테스트 실행은 시간이 걸린다** — 전체 스위트가 지금 694개(main 기준)까지 늘어서
   `pytest -q`가 3분 가까이 걸린다. Bash 도구 기본 타임아웃(120s)을 넘기니 `timeout` 파라미터를
   충분히 크게 주거나(180000ms+) 여의치 않으면 background로 돌릴 것.
5. **`ops/healthcheck.py`/`ops/supervisor.py`에 라이브 리허설 중 발견한 버그 3건을 이미
   고쳤다**(커밋 5da390f, 45b0c0b) — (a) candles_1d/events/promotions 오탐 CRIT,
   (b) supervisor의 em-dash가 cp949 콘솔에서 크래시, (c) 429 카운트가 타임스탬프 숫자를
   오검출. 전부 회귀 테스트 포함. 재발 여부는 신경 안 써도 됨(고정 완료).
6. **W4 소유 버그(수정 안 함, 관측만)**: `budget: RANKING predicted X req/s > target Y`
   비교 로직이 뒤집혀 있어 X≤Y인데도 ERROR로 찍힌다. `tools/dryrun_night.py`의
   `count_fake_budget_errors()`가 실제/가짜 건수를 구분해서 리포트 §12에 낸다 —
   W5 소유 코드가 아니므로 건드리지 않았다.
7. **`tests/fixtures/live/live_prices_us.json`, `live_trades_us.json`이 로컬에서 수정된
   채 커밋 안 돼 있다**(`git status`로 확인 가능) — 이번 A-1 라이브 프로브(`tools/live_probe.py`,
   W1 소유 도구를 그대로 실행) 실행의 정상적인 부작용(그 도구가 설계상 픽스처를 자동
   갱신한다)이다. W1 소유 경로라 이 워커가 커밋하지 않았다 — 그대로 두거나, W1이 검토 후
   커밋 여부를 판단할 것. 진짜 라이브 응답(정규장 기준)이라 픽스처로서 가치가 있을 수 있다.
8. **`docs/11_live_rehearsal.md`이 이번 리허설의 전체 기록**이다 — A-2/A-3 실측값, CRKN
   정밀도 이슈 진단(P1~P3), A4/A5 계약 개정 배경, 세션 전환, 계획/비계획 재시작 3건 전부,
   healthcheck/supervisor 버그 3건, tape_gaps/tier3/예산 관측 3건이 시간순으로 다 들어있다.
   최종 리포트 작성 시 이 문서와 교차 검증할 것.
9. **`ops/ops_config.yaml`은 gitignore 대상이 아니지만 이 워크트리에만 로컬로 존재**한다
   (git status에 `??`로 뜬다 — 의도된 것, `ops/ops_config.example.yaml`과 달리 이 워크트리의
   절대경로 `collector_cmd`를 담고 있어 커밋 안 함).

## 테스트 현재 상태

- `tests/test_dryrun_night.py` (내 최근 변경분 직접 대상): **19 passed** (HEAD 7deb292 기준,
  방금 확인).
- 전체 스위트: **HEAD(7deb292) 기준 645 passed 확인 완료**(294.86초, 백그라운드 실행 결과
  리셋 공지 직후 수신).
- 실행 명령: `cd w5-ops 워크트리 && .venv\Scripts\python -m pytest -q` (venv 이미 구성됨,
  `pip install -e ".[dev]"` 재실행 불필요하나 rebase 후에는 한 번 더 해두면 안전).
