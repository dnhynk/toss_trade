# 코디네이터 상태 — 새 세션 인수인계용

갱신: 2026-07-31 16:05 KST · main = `69e2ffe` · **749 passed, 1 skipped**
> 새 코디네이터(term_7a86158d)가 §0 절차로 인수 완료. 밀린 delivery 14건 확인·ack 완료
> (전부 이전 세션에서 이미 처리된 보고였음).

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
| **W5 라이브 재수집** | **진행 중 — 22:39:18 기동본** (작업 스케줄러, 최신 main 코드). 오늘 사고 2건: **사고1** 18:07:30 Orca PTY 정리가 수집기 트리 살해 → 8분 공백 → 18:15 스케줄러 재기동. **사고2** 재기동 env 에 `TOSS_BASE_URL` 누락 → 20:48 토큰 만료 후 재발급 전부 실패(프로세스는 생존, API 만 사망 — 모니터 사각) → 22:39 env 수정 재기동. **공백: 20:48:20~22:30:59, 22:31:19~22:39:18** (docs/11 매니페스트 기록). 재기동 중간 발견: 툴콜 안에서 schtasks /Run 후 대기하면 샌드박스 정리가 새 트리도 죽임(3회 연속) — **fire-and-forget 후 별도 콜 검증**으로 해결. 애프터(~08:50)까지 계속 |
| **W5-b 관찰 교대** | 진행 중 — `task_999bd2c75b52`/term_9727154b. 체크포인트: 22:25 개장준비 → 22:30~22:50 본 시험 보고 → 심야 status → 내일 ~09:00 검증·worker_done. 원 태스크 `task_04201cf60649` 는 dispatched 로 남음 — W5-b 완료 시 코디네이터가 수동 정리 |
| **§3-(B) 정리 3건** | **전부 머지 완료** — W2 U-4(`0dc427f`, 실유출 1건 수정: `UnicodeDecodeError.args` 바이트 원문), W7 사전등록 개정(`a492810`, A6/A7 충돌 없음 판정), W3 사전등록 정합(`2961e7a`, P0 3+P1+P2 2, 신규 22테스트). **통합 773 passed·1 skipped 실측** |
| **W7 후속 개정** | **머지 완료** — §7-e 분할 탐지 문언 확정(r=1d수정/1m원주가 비율, 경계 급변 ≥1.5배, `split_dates` 인자, `split_excluded` 카운트, 미전달 시 주 분석 금지)·§2.2 일봉 as-of 앵커 의무·§2.7 시총 ±10% 밴드 |
| **W3 구현 후속** | **머지 완료** (`fa6b45a` 머지, **795 passed·1 skipped 실측**) — 분할 파이프라인·일봉 as-of 앵커·시총 밴드. 분석 착수 전 코드 작업 끝 |
| **W7 최종 추인** | **머지 완료** — A5 혼입 검사는 "현 데이터로 실행 불가 — 미실행+사유 보고"로 강등(순환참조: 혼입이 r 점프 자체를 지움), 대체 안전망 2개(백필 매니페스트에 `adjusted=false` 기록·전 심볼 r 분포 스팟체크=전면 혼입만 탐지), W3 해석 2건·scope 컬럼 추인, 사각 2개 명기. W7 터미널 핸들은 **term_01b11d1b** (재발급됨) |
| 라이브 리스 | **W5 단독 보유.** 다른 워커·코디네이터는 라이브 호출 금지 |

> 참고: w7-prereg 워크트리의 저수준 `terminal create`는 "Timed out waiting for terminal
> handle" 로 반복 실패했고 기존 셸 2개는 유령(PTY 무반응, 1개는 tab_not_found)이었다.
> `orchestration worker-start` 조합 경로는 정상 동작 — 같은 증상이 나오면 이쪽을 쓸 것.
> 스펙 본문에 큰따옴표가 있으면 PowerShell 5.1 이 native 인자를 깨뜨린다 —
> `task-create --spec` 은 Bash 로 실행할 것.

**정규장 개장(22:30) 본 시험 결과 (22:50 보고 — 통과)**:
- tier3 **13→17/20** 실 소형주로 채워짐 (어제 4/20 대형주 쏠림과 대조). 11분간 신규 승격 12건:
  confirm 8 (ACRE·CIGL·RANI·REBN·VFF·CAAS·EARN·LAKE), **precursor 4 (PMA·TYGO·WALD·AMCI —
  전조 경로 첫 발화)**
- `evicted`-as-promotion **0 유지** (어제 6,653 병리 소멸). 승격 1,164건 중 price_activity 93.6%
- 예산 거버너: 개장 풀부하에서 MARKET_DATA 최대 6.27/7.00, **429 0건**, api_errors=0
- 유니버스 게이트: `watch_outside_universe=0` 시종 유지, INTC 등 대형주 tier0 거부
- 실 이벤트: **EVENT CIGL kind=win path=confirm score=0.712** (22:40:05)
- **유의**: 개장 첫 분 유실 — 아래 사고 2 참조. 커버는 22:31:06~19 + 22:39:18~

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

### (B) 분석 착수 전 정리 — **전부 완료·머지됨** (17:00 기준)
1. ~~사전등록 반영~~ → W7 개정 3회 머지(M-3/M-4 반영 → 문언 공백 3건 → 최종 추인).
2. ~~HANDOFF-W3 §3 미정합~~ → W3 2회 머지(20일 창·가격 계열·전일종가 사슬·표본 필터,
   그리고 분할 파이프라인·일봉 as-of 앵커·시총 밴드). **795 passed·1 skipped.**
3. ~~U-4 점검~~ → W2 머지. 실유출 1건 발견·수정(`UnicodeDecodeError.args` 바이트 원문).

### (C) Phase 1-C 분석 — **다음 작업. 단 백필은 라이브 리스가 필요하다**

> **사용자 지시 (7/31 밤, 구속력 있음): 아침에 백필 라이브 실행을 승인하기 전에
> W6 3차 감사(`task_bdbca590ecbe`, docs/14) 결과를 사용자에게 먼저 보고할 것.**
> 순서: ① W6 감사 결과 수령 ② (W4 백필 러너 완성 시 그 코드까지 감사 범위에 추가 검토)
> ③ 사용자에게 감사 요약 보고(턴 마지막 텍스트로) ④ blocker 없으면 그때 백필 승인.
> blocker 가 있으면 승인 보류하고 수정 먼저.

**밤사이 진행 — 전부 머지 완료 (08-01 01:40 기준, main **821 passed·1 skipped**)**:
① W6 3차 감사(docs/14 — 치명 F-1·F-2, 전부 재현 실측) ② W4 **대량 백필 러너**
(`tools/backfill.py`, §2.8 집행, mock E2E, 3단계 실행 절차·호출량 추정은
`W4_REPORT_task_e99983f09016.md` (e)) ③ W7 문언 추인(§7-e 보수 등록·§2.2 date-단위)
④ W3 blocker 수정(F-1 date-단위 소속·F-2 (마지막관측,관측] 보수 등록 + fixture 실규약화
+ F-3/F-4/F-5/F-8 — 감사 repro 로 자체 검증, `split_scan_report` 에 n_r_uncomputable·
n_widened 노출).
**추가 (01:10)**: **E2E 리허설 디스패치** — `task_2c12d19559dc`/`ctx_3822bbb36eaa`/W3.
백필(mock)→Reader→분할스캔→검출→prereg 베이스라인→필터→run_all→리포트 전 순서 총연습.
오늘 밤 머지 조각들의 조합 실행이 0회라는 공백을 메운다. 산출: 내일 재사용할 실행
스크립트 + Phase 1-C 절차 초안. 메일함 감시는 하니스 Monitor(`bg852623o`, peek 폴링)로
전환(PTY 백그라운드 대기 반복 즉살 우회).

**E2E 리허설 결과 (01:45 머지, 824 passed)**: 전 순서 green (provenance 행 0).
**실행 순서 blocker 2건 발견·해소** — 내일 절차에 필수 반영:
- **유니버스 빌드가 백필보다 먼저다** (백필은 `symbols` 를 안 채움 → Reader.symbols()
  비면 §2.7 필터가 전 이벤트를 meta_missing 으로 버림)
- **캘린더 영속화** — W3 이 `save_calendar`/`load_calendar` 추가(day=None·반일장 왕복
  보존). API 가 살아 있는 백필 창에서 캘린더를 떠서 저장해야 분석이 오프라인으로 돈다
- W3 권고: 러너는 provenance 행이 하나라도 있으면 비정상 종료해야 함 (Phase 1-C 스펙에 반영할 것)
- Phase 1-C 단계별 절차 초안은 W3 리허설 보고서에 있음 (스크래치패드 w3_e2e 계열)

**아침 절차 — 진행됨 (09:20 기준)**: ① W5-b 최종 검증 green·worker_done 수동 복구 완료
(태스크 2건 completed, docs/11 머지 `63ac979`, main 826 passed) ② 감사 요약 사용자 보고
완료(09:05) ③ 수집기 STOP 파일로 정상 정지·리스 해제(09:15) ④ **W4 백필 실행 중** —
`task_bf64c4c19022`/`ctx_69b1466adb5b`(term_8017b557). 순서: dayMarket 실측 1콜 →
유니버스 빌드(backfill.db) → 캘린더 캡처(save_calendar) → screen-only → --estimate →
**정지선 도달 (11:27) — 본실행 GO 는 사용자가 직접 결정** (08-01 11:15 사용자 지시.
11:45 사용자: 노트북 닫았다가 재개 시 옵션+GO 를 함께 준다. 그 전까지 1분봉 호출 0 유지).
- **GO 메뉴** (1분봉 보관 ~320일이라 날짜 경계는 데이터 손실 0):
  [B]=--from 2025-09-01 전심볼 4,002창 17.6~35h (**권고** — 훈련 데이터 전부+§2.8 프레임 정합)
  [A]=2026-01-01 전심볼 2,651창 11.6~22.9h (훈련 구간 포기) [C]/[D]=러너 한정(프레임 이탈)
- **GO B 발사됐으나 403 ip-not-allowed 로 중단 (08-01 20:19~20:45)** — 기동 3연속
  "5초 사망"의 진짜 원인은 킬러가 아니라 **공인 IP 가 토스 허용목록에서 이탈**
  (스크린 2h 는 성공, 이후 전 호출 403 — 아침에 성공한 캘린더 콜도 지금은 403).
  현재 공인 IP **106.246.92.137** — **사용자가 토스 개발자 포털에 재등록해야 재개 가능**.
  데이터 손실 0: 체크포인트 21,850창 전부 todo, 스크린·캘린더·유니버스 디스크 보존,
  재개는 단일 명령(run.log/w4m.cmd). W4 가 잔여 프로세스·스케줄 태스크 3종 전부 정리.
  **08-02 05:30 현황**: IP 재등록(08-01 21:15)→재개→밤새 플랩으로 00:01 재사망(5h 미발견,
  죽은 STATUS 방출기가 위장)→05:00 재개→05:28 **자가 복구 슈퍼바이저로 전환 완료**
  (nohup, pid 20724; schtasks 는 리퍼에 죽어 불채택). 봉 1,632,922+ 수집됨.
  **정지 절차 변경**: 10:10 데드라인 자가 정지가 정본(무조치 시 자동). 수동 정지는
  `data/backfill/SUPERVISOR.STOP` 파일 생성(우아한 정지). 09:47 타이머 발화 시 행동 =
  생존·데드라인 무장 확인만. 정지 후 사용자 notify → 노트북 재개 후 잔여 GO →
  같은 슈퍼바이저 명령 재실행(체크포인트+403 프로브 대기 내장이라 IP 재등록만 하면
  코디네이터 개입도 불필요). 정지 후 창 완료/잔여 보고 → **사용자 notify** → 사용자가
  노트북 재개 후 잔여 GO → 같은 명령 재실행이면 체크포인트가 이어받음.
  태스크 `task_bf64c4c19022`/`ctx_69b1466adb5b`, W4 핸들 term_8017b557.
  완료 시 worker_done → 머지 게이트(러너 패치 7a4d0a0 포함) → Phase 1-C 분석 디스패치.
- 스크린 산출물: data/backfill.db (유니버스+일봉 313,934), data/backfill/calendar_us.json
  (W3 save_calendar 형식, 2,107 매매일), 스크린 캐시(재스크린 불요, 멱등 재시작 안전). 완료 후 Phase 1-C 분석 디스패치
(스펙은 W3 리허설 보고서의 절차 초안 + 감사 §VII 러너 강제조항 3줄 기반으로 작성할 것).
잔여 후속(비긴급): 랭킹 클램프 분기 미발화 감시, 토큰 재발급 실패 api_errors 사각,
모니터 closed 세션 예외, F-6~F-9.
잔여 리스크(수용): A5 혼입 검사 구조적 순환(§7-e 강등 문언대로 미실행+보고),
첫 매매일 분할 원리상 미탐지, 보수 등록의 n_widened 은 백필 후 점검.
**`docs/12_preregistration.md` 를 먼저 읽고 그대로 따를 것** (개정 4건 전부 머지된 최신판).
`docs/13_trial_registry.md` 는 **첫 시행 전에** 생성해야 한다(형식은 사전등록에 규정).
분석 대상은 오늘 재수집분 + 백필. **어제(7/30) DB(`tossmon_20260730_polluted.db`)는
표적 모집단이 아니므로 분석에 쓰지 마라** — 파이프라인 검증 증거로만 보존.
- **백필은 W5 수집 종료(내일 ~08:50 애프터 마감) 후 리스를 넘겨받아 실행.**
  사전등록 §6.1: 백필 지연은 훈련 기간 영구 손실 — 리스가 비는 즉시 착수할 것.
- 백필 매니페스트에 `adjusted=false` 파라미터 기록 의무(§7-e 추인 개정),
  주 분석 곡선은 `prereg_volume_curve`, 일봉은 `prereg_daily_baseline`(앵커 필수),
  검출은 `split_dates` 전달 필수 — 미전달 시 리포트에 `(split_dates_not_applied)` 가 찍힌다.

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

## 4.5 무인 장기 실행 표준 (08-02 사용자 지시 — 구속력 있음)

**"중단돼도 사용자 개입 없이 빠르게 복구"가 모든 무인 장기 실행의 기본 요건이다.**
백필 2연속 사망(PTY 킬 8분 공백, IP 플랩 5시간 미발견)에서 채택. 3층 구조:
1. **자가 복구 슈퍼바이저** — `tools/backfill_supervisor.py` (W4-b `task_b0bae554f20c` 구축):
   403 은 10분 프로브 대기 후 자동 재개(IP 플랩 자가 치유, 포털 재등록도 등록 즉시 자동
   재개), 일시 오류 백오프 재기동, 3연속 크래시만 ESCALATION. STATUS 는 러너 생존을
   함께 찍는다(죽은 숫자 위장 불가). schtasks 기동 + STOP 파일 + --deadline.
2. **코디네이터 독립 감시** — 로그 기반 Monitor(FATAL/RUNNER-DEAD/ESCALATION 문자열
   + 40분 침묵). 워커 전멸에도 작동. 새 코디네이터는 인수 시 재무장할 것.
3. **사용자 개입은 진짜 필요한 것만** — 현재 유일: 토스 포털 IP 등록(자동화 불가).
   그 외 모든 복구는 기계가 한다.

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
  **`Start-Process` 분리로는 부족하다는 것이  7/31 18:07 실증됐다** (Orca PTY job object 가
  트리째 죽임 — 수집기 8분 공백 사고). **반드시 작업 스케줄러(schtasks)로 띄워
  부모가 svchost(Schedule) 이 되게 하라.** `ops/register_task_scheduler.ps1` 참조.
- **에이전트 턴이 Monitor 대기로 열려 있으면 `terminal send` 프롬프트가 큐에 갇힌다** —
  ESC 를 보내면 턴이 끊기며 큐가 소비되지만 **큐된 긴 텍스트가 잘릴 수 있다**(실제로 잘렸다).
  ESC 후에는 상대가 받은 내용을 확인하고 전문을 재전송하라 (orchestration `reply` 가 안전).
- **환경이 바뀌면 우회 수단을 굳히지 말고 원래 방식을 재시험하라** (§0 의 `check --wait` 사례).
- codex 워커가 지출 한도에 걸리면 **증액 요청하지 말고 claude 로 교체**.
