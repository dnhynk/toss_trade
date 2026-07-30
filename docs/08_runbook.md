# 08 — 무인 운영 런북 (Runbook)

> 소유: W5. `ops/**` 스크립트의 사용법·정책 문서. 코드 상세는 `ops/README.md`, 시크릿 정책은
> `docs/09_secret_hygiene.md`. 라이브 리허설 결과는 §7에 기록한다(진행 전까지는 "대기" 상태).

## 0. 무인 운영의 전제

이 파이프라인은 **한국 저녁~새벽(미국 정규장)이 본게임**이라 사람이 지켜보지 않는 시간대에
가장 중요한 데이터가 쌓인다(docs/03 §4). 따라서 이 런북의 목표는 "사람이 없어도 (a) 죽으면
스스로 재시작하고 (b) 사고가 나면 조용히 넘어가지 않고 관측 가능한 흔적을 남기는 것"이다.

모든 ops 스크립트(`healthcheck.py`, `disk_guard.py`, `rotate_logs.py`)는 **라이브 API를 호출하지
않는다** — 로컬 SQLite(read-only)/로그/디스크만 본다. 따라서 라이브 리스가 없어도 상시 실행해도
안전하다. 라이브 API를 실제로 만지는 것은 `supervisor.py`가 띄우는 **collector 프로세스 자신**뿐이고,
그 프로세스가 `TOSS_LIVE=1` 로 도는 것은 §6·§7의 승인 절차를 거친 뒤에만이다.

## 1. 세션 시간표 (KST, 실측 기준 — docs/06 §6)

| 세션 | 시간(KST) | 비고 |
|---|---|---|
| day (데이마켓) | 09:00 – 17:00 | 유동성 최저 구간. `first_print`/`staleness_s` 신호의 최악 조건(§7-2 참고) |
| pre (프리마켓) | 17:00 – 22:30 | W1 최초 실측이 이 구간에서 이뤄짐(docs/06) |
| regular (정규장) | 22:30 – 05:00(익일) | 본게임. 무인 운영의 핵심 대상 |
| after (애프터마켓) | 05:00 – 08:50(익일) | |

**세션 판정은 하드코딩하지 않는다.** `/market-calendar/US` 응답(`UsMarketDay`)만 기준으로 삼고
(계약 C-1), 위 표는 사람이 스케줄을 눈으로 보기 위한 참고치다. 데이마켓과 프리마켓은 17:00
경계를 공유하는 반열림 구간 `[start, end)` 이므로 세션 판정 로직에서 겹치지 않게 주의한다(docs/06 §6).

## 2. 사전 준비 체크리스트

- [ ] `.venv` 생성 + `pip install -e ".[dev]"` 완료, `pytest` 전체 통과 확인
- [ ] `config/config.yaml` 존재(`config/config.example.yaml` 복사) — 소유 W4, W5는 건드리지 않는다
- [ ] `ops/ops_config.yaml` 존재(`ops/ops_config.example.yaml` 복사), `db_path`/`log_dir` 등 실제 경로로 조정
- [ ] `api_keys` 가 리포 루트에 존재하고 `.gitignore` 로 제외됨을 `git status` 로 확인 (내용 확인 금지 — 존재 여부만)
- [ ] `ops/ops_config.yaml` 의 `collector_cmd` 확인 — 확정된 엔트리포인트는
      `python -m tossmon.collector --config config/config.yaml` (`tossmon/collector/__main__.py`)
- [ ] `python -m ops.healthcheck` 를 mock 상태에서 한 번 실행해 오류 없이 뜨는지 확인 (DB 없어도 정상)

## 3. TokenManager `state_path` 단일화 — 운영 규율 (필독)

**근거**: W1 리스크 보고 [3]. `TokenManager` 의 토큰 단일성은 **OS 파일락**으로 보장되는데(계약 C-4),
파일락은 "같은 머신, 같은 상태파일 경로"를 전제로만 의미가 있다. 즉:

- 두 프로세스가 **다른 `state_path`** 를 가리키면 락이 서로 다른 파일에 걸리므로 **둘 다 락 획득에
  성공**하고, 둘 다 토큰을 발급하려 든다. 재발급은 기존 토큰을 즉시 죽이므로(계약 C-4), 두 프로세스가
  번갈아 서로의 토큰을 죽이는 사고가 난다 — 에러 메시지도 없이 "가끔 401/AuthExpired가 뜬다" 정도로만
  보여서 원인 추적이 매우 어렵다.
- **규율**: `token_state_path` 값은 **`config/config.yaml` 한 곳에만** 존재해야 하고, 이 리포에서
  라이브 모드로 뜨는 모든 프로세스(`supervisor.py`가 띄우는 collector, `tools/live_probe.py`,
  `tools/dryrun_night.py` 는 라이브 호출을 하지 않으므로 해당 없음)는 **그 값을 그대로 참조**해야 한다.
  스크립트마다 `--state` 를 임의로 다른 경로로 넘기지 않는다(`tools/live_probe.py --state` 기본값도
  반드시 `config/config.yaml` 의 값과 같은 경로로 맞춰서 호출할 것).
- 같은 머신에서 라이브 collector 프로세스가 **동시에 두 개 뜨는 상황 자체**(예: Task Scheduler 재시작
  타이밍과 수동 실행이 겹침)를 막는 것도 이 규율의 일부다. `ops/supervisor.py` 는 자식 프로세스가
  종료된 뒤에만 재기동하므로 자기 자신은 이중 기동을 만들지 않지만, **사람이 수동으로 또 하나
  띄우면** 여전히 사고가 난다 — 리허설 중에는 `python -m ops.healthcheck` 로 활성 여부를 먼저 확인하고
  수동 실행하는 습관을 들인다.
- 락 파일(`{state_path}.lock`)이 남아있는 것 자체는 비정상이 아니다(정상 종료 시에도 `release()`가
  `force=True` 로 락 파일을 지우지 않고 잠금만 해제할 수 있다 — 파일 존재 여부가 아니라 **잠김 여부**가
  중요하다). §7-4에서 리허설 종료 후 확인 절차를 다룬다.

## 4. 상시 실행 등록 (Windows 작업 스케줄러)

`ops/register_task_scheduler.ps1` 이 3개 작업을 만든다: `tossmon-collector`(supervisor 상시 실행),
`tossmon-healthcheck`(5분 주기), `tossmon-logrotate`(매일 00:10). **기본은 DryRun**(계획만 출력,
아무 것도 등록하지 않음) — 실제 등록은 `-Confirm:$true` 를 명시해야 한다.

```powershell
# 계획 확인 (안전, 아무 것도 등록 안 함)
powershell -File ops\register_task_scheduler.ps1 -Action Register
# 실제 등록
powershell -File ops\register_task_scheduler.ps1 -Action Register -Confirm:$true
# 상태 확인 / 해제
powershell -File ops\register_task_scheduler.ps1 -Action Status
powershell -File ops\register_task_scheduler.ps1 -Action Unregister -Confirm:$true
```

`AtLogOn`/`AtStartup` 트리거만으로는 **사용자가 로그인해 있어야** 실행된다. 로그인 없이도
돌리려면(완전 무인 서버) `schtasks /Change /TN tossmon-collector /RU <user> /RP <password>` 로
계정 자격증명을 등록해야 하는데, 이는 시크릿 취급 사항이라 이 스크립트가 자동으로 하지 않는다
— 필요하면 운영자가 직접 실행하고, 비밀번호를 스크립트/로그에 남기지 않는다.

## 5. 프로세스 감시·자동 재시작 (`ops/supervisor.py`)

- `ops_config.yaml` 의 `collector_cmd` 를 자식 프로세스로 실행, 표준출력/에러를
  `log_dir/collector.stdout.log` 로 리다이렉트.
- 죽으면(종료코드 ≠ 0) 지수 백오프(`restart.backoff_base_s`~`backoff_cap_s`) 후 재시작.
- `restart_window_s` 안에 `max_restarts_per_window` 회 넘게 재시작하면 **폭주로 간주해 포기**하고
  0이 아닌 종료코드로 끝난다 — Task Scheduler의 재시작 정책(§4의 `RestartCount`)이 다음 단계로
  넘어가되, 그마저도 반복 실패하면 healthcheck의 `age(min)` 지표가 계속 벌어지므로 사람이 알아챌 수 있다.
- 정상 종료(코드 0)면 재시작하지 않는다 — collector가 의도적으로 자기 자신을 끝낸 경우(예:
  세션 종료 후 자체 종료 로직이 있다면)까지 억지로 되살리지 않기 위함.
- **정지 방법**: `ops/state/STOP`(정확히는 `ops_config.yaml`의 `state_dir`/STOP) 파일을 만들면
  다음 체크 시점에 정상 종료한다. 콘솔 실행 중이면 Ctrl+C 도 된다.

## 6. 헬스체크 한 화면 (`ops/healthcheck.py`)

```
=== tossmon healthcheck @ 1785400000000 ===  overall: WARN

table               count  age(min)    rows/s  status
candles_1m           1234       0.8      3.10      OK
orderbook_snap        321       1.2      0.90      OK
rankings_snap         210      12.4      0.00    WARN
trades_snap          4321       0.3      5.40      OK

logs: 429=2 request_lines=118 (files=1, window=300s)

disk[data/tossmon.db]: free=42.1GB / 500.0GB  status=OK
disk[data/logs]: free=42.1GB / 500.0GB  status=OK
```

- **`age(min)` 이 `stale_minutes_warn`/`critical` 을 넘으면** 그 테이블이 갱신을 멈췄다는 뜻 —
  단, **데이터가 아예 없던 테이블(count=0)은 WARN이 아니라 OK로 표시된다**(수집 미시작/휴장일
  수 있어 오탐 방지, §1 세션표로 사람이 판단). 즉 WARN/CRIT 은 "한 번이라도 쌓이던 게 멈췄다"는
  뜻으로만 해석한다.
- **429/request_lines 가 `unavailable`** 로 나오면 오류가 아니라 W4 collector가 아직 표준 로그
  포맷을 쓰지 않는다는 뜻이다(§8 참고). DB 기반 지표(count/age/rows_per_sec)는 이 문제와 무관하게
  항상 동작한다.
- 종료코드: `0=OK, 1=WARN, 2=CRIT` — Task Scheduler `LastTaskResult` 로 이력 확인 가능(§4 Status).
- 대응: WARN이 세션 중(§1 표 기준 지금이 열려 있어야 하는 시간)에 떴으면 supervisor 로그
  (`data/logs/collector.stdout.log`) 확인 → 크래시 반복이면 §5의 재시작 폭주 여부 확인 →
  폭주 아니면 네트워크/토큰 문제(§9 표) 의심.

## 7. 로그 로테이션 (`ops/rotate_logs.py`)

- `log_max_bytes`(기본 20MB) 넘는 `*.log` 를 타임스탬프 접미사 + gzip 압축으로 회전.
- 회전 중 파일이 다른 프로세스에 점유돼 rename 이 실패하면(Windows 흔함) **조용히 스킵**하고
  다음 주기에 재시도한다 — 데이터 유실 없음, 회전만 늦어진다.
- `log_retention_days`(기본 14일) 지난 압축 로그는 삭제.
- Task Scheduler `tossmon-logrotate` 로 매일 00:10 실행 권장(정규장 종료 직후라 그 시점 로그가
  이미 안정적으로 flush 돼 있을 가능성이 높음).

## 8. 디스크 용량 경보 (`ops/disk_guard.py`)

- `db_path`/`log_dir`/`archive_dir` 가 위치한 드라이브의 여유 공간을 확인, `warn_free_gb`/
  `critical_free_gb` 미만이면 경고.
- CRIT 시 대응 순서: (1) `ops/rotate_logs.py` 로 로그부터 정리 → (2) `tossmon/store/retention.py`
  (W2 소유)로 오래된 `candles_1m`/`trades_snap` 을 Parquet 아카이브 후 DB에서 제거 → (3) 그래도
  부족하면 티어 축소(collector `budget.py`, W4 소유)를 통해 수집량 자체를 줄이는 것을 코디네이터에
  `ask`.

## 9. 장애 유형별 대응

| 장애 | 증상 | 대응 |
|---|---|---|
| 프로세스 크래시 반복 | supervisor 로그에 재시작 반복, 결국 "restart budget exceeded"로 종료 | 로그에서 마지막 예외 확인 → 코드 버그면 해당 워커(W4/W1)에 보고, 일시적 네트워크면 수동 재기동 |
| DB 파일 잠금/손상 | Store 생성 시 `sqlite3.OperationalError` | WAL 체크포인트 확인(`PRAGMA wal_checkpoint`), 다른 프로세스가 동시에 쓰기 모드로 열지 않았는지 확인(계약 C-6: 쓰기는 collector 단일 프로세스) |
| 429 지속 | healthcheck `logs.count_429` 급증(로그 포맷 확정 후) | `usage_ratio` 하향 조정 또는 티어 축소(`ask` to 코디네이터) — limiter 자체 백오프는 자동(계약 C-5) |
| AuthExpired 반복 | 토큰 재발급이 계속 필요 | §3 점검 — `state_path` 이 config 단일값과 일치하는지, 동시에 뜬 라이브 프로세스가 없는지 |
| 디스크 부족 | disk_guard CRIT | §8 순서대로 |
| 네트워크 단절 | healthcheck 전 테이블 age 급증 | collector 재시작 로그로 `TransientHTTP` 반복 확인, 장기화되면 사람 개입 |
| 세션 경계 오판 | 폐장 시간에도 폴링 지속/개장 후 스윕 미시작 | `/market-calendar/US` 하드코딩 여부 재확인(계약 C-1) — W4 scheduler.py 버그 가능성, `ask` |

## 10. 중단·재개 체크리스트

**정상 중단**:
1. `ops/state/STOP` 파일 생성 (supervisor가 다음 체크 시점에 정상 종료)
2. `python -m ops.healthcheck` 로 최종 상태 스냅샷 확인(가능하면 로그로 남김)
3. 라이브 모드였다면 `TokenManager.release()` 가 호출돼 락이 반납됐는지 `{state_path}.lock` 확인

**재개 전 확인**:
1. `python -m ops.healthcheck` — DB가 예상대로 마지막에 멈췄는지(age가 중단 시점과 일치)
2. `python -m ops.disk_guard` — 디스크 여유 확인
3. `{state_path}.lock` 이 잠겨 있지 않은지(다른 프로세스가 들고 있으면 즉시 재기동 실패 — 정상,
   §3 규율 위반이 없었는지 먼저 확인)
4. Task Scheduler 상태(`register_task_scheduler.ps1 -Action Status`)로 마지막 실행 결과 확인

**긴급 중단(장애 대응 중)**: STOP 파일 생성 대신 바로 프로세스 종료가 필요하면 작업관리자/
`Stop-ScheduledTask` 사용 가능하나, **DB 쓰기 도중 강제 종료는 WAL 특성상 안전**하다(계약 C-6,
다음 시작 시 자동 복구) — 다만 가능하면 STOP 파일 방식을 우선한다.

## 11. 라이브 리허설 절차 (승인 게이트 — 실행 전 코디네이터 리스 + 사용자 승인 필수)

**절대 규칙**: 아래 절차는 코디네이터가 라이브 리스를 명시적으로 부여하고 사용자가 승인한
뒤에만 실행한다. 그 전까지 `TOSS_LIVE=0`, mock 고정.

1. 코디네이터에게 리스 요청 → 부여받으면 `config/config.yaml` 의 `api.live: true`,
   `api.base_url`, `api.token_state_path` 가 §3 규율대로 설정돼 있는지 확인.
2. `python -m ops.healthcheck` 로 다른 라이브 프로세스가 이미 떠 있지 않은지 확인(활성 락 여부).
3. collector를 (W4 엔트리포인트 확정 후) `ops/supervisor.py` 로 기동 — **1세션**(§1 표 기준 세션
   하나) 동안 방치.
4. §12의 추가 실측 프로브를 **세션 시간에 맞춰** 실행(아래 §12 참고 — 세션 안에 끝나도록 프로브
   시작 시각을 미리 계산해 둘 것).
5. 세션 종료 후 `python tools/dryrun_night.py --hours-back <세션 길이>` 로 리포트 생성 →
   결과를 이 문서 §13(리허설 결과 기록)에 붙여넣고 코디네이터에 보고.
6. `ops/state/STOP` 으로 정상 종료 → §10 재개 전 확인 절차의 "재개"가 아니라 "종료" 확인으로 수행:
   `{state_path}.lock` 이 반납됐는지(파일 자체 존재 여부가 아니라 다른 프로세스가 잠그고 있지
   않은지) 확인.
7. **토큰 위생**: 로컬 `data/token_state.json` 을 지워도 **서버측 토큰은 무효화되지 않는다** —
   삭제는 로컬 사본 정리일 뿐이다. 다음 라이브 세션에서 파일이 없으면 `TokenManager` 가 새
   토큰을 발급하며, 이전 토큰은 만료 시각까지 서버에서 유효한 채로 남는다(재사용 안 해도 보안
   문제는 아니지만, 리스를 코디네이터에게 반납할 때 "로컬 상태파일 삭제 = 서버 로그아웃"이
   아니라는 점을 반드시 공유한다).
8. 코디네이터에 리스 반납 통보.

## 12. 라이브 리허설 시 추가 실측 (W1 미완 항목 — 절차만 명시, 결과는 §13에 추후 기록)

W1의 실측은 전부 프리마켓이었다(docs/06 §측정조건). 아래는 리스를 받은 뒤에만 실행한다.
**프로브 소요시간(W1 인수인계)**: `quote_realtime` 기본 6폴링×12초≈60초, `rankings` 2타입×4폴링×
20초≈160초 — 세션 안에 끝나도록 시작 시각을 역산해 둔다.

1. **정규장(22:30–05:00 KST) 실측**: `python tools/live_probe.py --probe quote_realtime,orderbook_depth,trades`
2. **데이마켓(09:00–17:00 KST) 실측 — 세션 초반과 중반 두 번**:
   - 09:00~10:00 KST 구간에 1회 (유동성 최저 구간 — `first_print`/`staleness_s` 트리거의 **최악
     조건**), 세션 중반에 1회 더.
   - **성공 기준**: 최악 조건(데이마켓 초반)에서도 소형주의 `first_print`(null→값 전이) 또는
     `staleness_s` 급감이 의미 있게 관측돼야 한다. 관측 안 되면 A2의 Tier1 승격 설계(첫 체결
     신호 기반) 자체가 무너지는 것이므로 **결과를 기다리지 말고 즉시 코디네이터에 escalate**한다.
3. **1분봉 보관 경계 이분탐색**: `--probe candle_deep` 확장, **대형주 1종목 + 소형주 1종목** 각각
   측정(종목별로 경계가 다를 수 있음 — docs/06 §11-4). 약 10콜 예산.
4. **1분봉 timestamp 가 봉 시작인지 끝인지 확정** (코디네이터 추가 지시, 2026-07-30):
   스펙 설명은 "봉 시작 시각"이나 실측 확인이 없다. 활발한 종목의 `/trades` 체결 시각들을
   받아 각 체결이 어느 1분봉에 매핑되는지 대조한다:
   - 체결 시각 `t` 가 봉 `timestamp=T` 에 대해 `T <= t < T+60s` 이면 **봉 시작**.
   - `T-60s < t <= T` 이면 **봉 끝**.
   - 유동성 있는 종목(예: AAPL) 기준 1~2콜(`/trades` 1콜 + 겹치는 `/candles` 1콜)이면 충분하다.
   - **중요도**: 이 값이 틀리면 이벤트 T0 시각과 전조 리드타임 측정이 전부 1분씩 밀린다
     (검증 질문 1·6, docs/03 §3). 결과는 아래 §13에 기록하고 코디네이터에 보고한다.
5. 결과는 이 문서 §13에 기록한다. **`docs/06_live_facts.md` 는 W1 소유이므로 직접 수정하지
   않는다** — 코디네이터에게 보고해 반영을 요청한다.

## 13. 라이브 리허설 결과 (대기 — 리스 부여 후 채움)

> 아직 라이브 리허설이 실행되지 않았다. 이 절은 §11·§12 실행 후 실측값으로 채운다.
> 그 전까지는 이 표를 "미실행"으로 유지한다.

| 항목 | 상태 | 비고 |
|---|---|---|
| 정규장 시세/호가/테이프 실측 | 미실행 | §12-1 |
| 데이마켓 초반(09:00~10:00) `first_print`/`staleness_s` 동작 | 미실행 | §12-2 — 실패 시 즉시 escalate 대상 |
| 데이마켓 중반 실측 | 미실행 | §12-2 |
| 1분봉 보관 경계(대형주) | 미실행 | §12-3 |
| 1분봉 보관 경계(소형주) | 미실행 | §12-3 |
| 1분봉 timestamp 시작/끝 판정 | 미실행 | §12-4 |
| 토큰 위생(락 반납 확인) | 미실행 | §11-6 |
| `tools/dryrun_night.py` 리포트 | 미실행 | §11-5 |

## 14. 미해결 리스크 (상위 3개)

1. **collector 엔트리포인트/로그 포맷 미확정(W4 Wave 2 진행 중)** — `ops/supervisor.py` 의
   `collector_cmd` 는 자리표시자다. `ops/healthcheck.py` 의 429/호출수 지표는 W4가 표준 로그
   포맷을 확정하기 전까지 `unavailable` 로만 표시된다. W4 머지 후 `ops/ops_config.yaml` 갱신 +
   로그 포맷 확인이 필요하다.
2. **라이브 리허설 미실행** — §12의 항목들(특히 데이마켓 최악 조건에서의 `first_print` 동작)이
   검증되지 않으면 A2 Tier1 승격 설계의 실효성이 미확인 상태로 남는다.
3. **완전 무인(로그인 없이) 재부팅 대응 미검증** — §4에서 언급한 `schtasks /RU /RP` 계정 자격증명
   등록은 시크릿 취급이라 이 스크립트가 자동화하지 않으며, 실제 재부팅 시나리오로 검증되지 않았다.
   운영자가 실제 무인 서버로 쓸 계획이면 별도로 테스트가 필요하다.
