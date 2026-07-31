# W5-b — 재수집 관찰 재개 (에이전트 유실 복구 — 수집기는 무사하다)

너는 W5(`ops/**`, `docs/08_runbook.md`, `docs/09_secret_hygiene.md`, `tools/dryrun_night.py`,
`docs/11_live_rehearsal.md` 소유자)다. 원래 W5 에이전트 세션이 Orca 런타임 문제로 죽었고
(터미널 PTY 유실), 너는 그 **관찰 임무만** 이어받는 교대 요원이다.

## 최우선 불변 — 수집기를 건드리지 마라

- **collector/supervisor 프로세스(파이썬 PID 29236·29704, 14:25:03 기동)는 분리 프로세스로
  정상 가동 중이다. 절대 재시작·정지·시그널 금지.** STOP 파일 생성 금지.
- **DB·수집 설정(ops_config.yaml 등) 수정 금지.** 읽기는 로그 파일만
  (DB 파일은 잠금 경합 위험이 있으니 열지 마라 — 카운터는 로그에 다 찍힌다).
- **라이브 API 호출 금지, 토큰 발급 금지** — 라이브 리스는 돌고 있는 collector 프로세스가
  쥐고 있다. 네가 토큰을 발급하면 그 수집이 죽는다. `api_keys` 읽기 금지.
- 기동 시점 코드 특성 2가지를 알고 있어라(오해 금지):
  (i) 로그의 `budget: RANKING predicted ... > target` ERROR 는 **가짜 경보**다 — 수정본은
  머지됐지만(`e512463`) 실행 중 프로세스에는 없다. 무시하라.
  (ii) `ForbiddenEndpoint` 명시 처리도 이 프로세스엔 없다 — **프로세스가 조용히 사라지면
  그것부터 의심**하고 즉시 escalation 하라.

## 임무 (관찰·보고만)

1. **기동 확인**: `git rebase main` 은 하지 말고(워크트리 상태 유지) 로그 위치부터 파악:
   `ops/` 아래 supervisor/collector 로그. 최근 로그로 17:00 프리마켓 전환이 정상인지,
   카운터(watch/tier2/tier3/promotions/`watch_outside_universe`/`api_errors`)를 요약해
   `status` 로 1회 보고.
2. **22:30 정규장 개장 = 오늘의 본 시험.** 22:30~22:50 로그를 관찰해 보고할 것:
   - tier3 점유(데이마켓 1/20 대비 얼마나 차는지), tier2 추이
   - 승격 사유 분포 — 특히 `evicted` 가 계속 0인지(어제 6,653), `precursor`/`confirm`
     비중이 오르는지
   - 개장 15분 구간 특이사항, 429/백오프 동작, `api_errors`
   - `watch_outside_universe` 가 0 을 유지하는지
3. 이후 **1~2시간 간격**으로 짧은 `status` (heartbeat 아님, 카운터 포함).
4. **프로세스 감시**: 두 PID 가 사라지거나 로그가 5분 이상 멈추면 즉시 `escalation`
   (재시작 시도 금지 — 코디네이터 판단 사항이다).
5. **애프터 마감(내일 ~08:50) 후**: 검증 스크립트
   `C:/Users/dongh/.claude/jobs/236dc45f/tmp/verify_recollect.py` 를
   `PYTHONIOENCODING=utf-8` 환경변수로 실행(없으면 cp949 로 죽는다), 결과 요약 +
   `docs/11_live_rehearsal.md` 에 오늘 재수집 기록 갱신(네 소유 문서다) 커밋 후
   `worker_done` 1회 (보고 (a)~(e)).

## 불변 규칙

1. 소유 경로 밖 수정 금지. 커밋은 `w5-ops` 브랜치에만, `main` 직접 커밋 금지.
2. 콘솔에 비ASCII 출력 금지(cp949). 파일 입출력은 `encoding="utf-8"` 명시.
3. 블로킹 판단이 필요하면 `ask`, 긴급이면 `escalation`.
4. 셸 인자 보고문에 백틱 금지(명령 치환 사고 전례).
