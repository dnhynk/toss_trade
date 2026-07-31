# W5 후속 — 라이브 리허설 실행 (라이브 리스 부여됨)

너는 W5(ops 소유자)다. 이전 태스크는 머지됐다(main 594802a). **사용자 승인이 떨어졌고,
코디네이터가 너에게 라이브 리스를 부여한다 — 지금부터 너는 전 시스템에서 유일하게
실서버를 호출할 수 있는 워커다.**

## 리스 규약 (가장 중요)
- 이 API 는 **client당 유효 토큰 1개**다. 네가 토큰을 발급하면 기존 토큰은 즉시 죽는다.
  현재 다른 리스 보유자는 없다(W1 이 반납 완료, 상태파일도 삭제됨).
- `TOSS_LIVE=1`, `TOSS_BASE_URL=https://openapi.tossinvest.com` 로만 라이브를 켠다.
- **최소 호출 원칙**을 지켜라. 프로브는 필요한 만큼만.
- 작업이 끝나면 파일락 반납(`.lock` 부재)을 확인하고 코디네이터에 보고하라.
- `api_keys` 내용은 어떤 경로로도 출력·로그·커밋·메시지에 넣지 마라.

## 시작 절차
1. `git rebase main` (main = 8056da9. W4 collector 머지 + config 예산 수정 포함)
2. `.venv\Scripts\pip install -e ".[dev]"` 로 의존성 확인
3. 읽을 것: `docs/08_runbook.md` §11~12(네가 쓴 절차), `docs/06_live_facts.md` 전체.

## 소유 경로 (이 밖 수정 금지)
`ops/**`, `docs/08_runbook.md`, `docs/09_secret_hygiene.md`, `tools/dryrun_night.py`
+ 이번 한정으로 **`docs/11_live_rehearsal.md` 신규 작성 권한**을 준다.
(`docs/06_live_facts.md` 는 W1 소유이므로 수정 금지 — 실측 결과는 11 에 쓰고 코디데이터가 반영한다.)

## 작업 A — 미확인 실측 4건 (프로브)
`tools/live_probe.py` 를 사용하되, 세션이 맞아야 하는 것은 세션을 기다렸다 실행하라.
W1 인수인계: `quote_realtime` 은 6폴링×12초≈60초, `rankings` 는 2타입×4폴링×20초≈160초다.

1. **정규장 + 데이마켓 시세/호가/체결 거동**
   `--probe quote_realtime,orderbook_depth,trades` 를 **정규장(22:30–05:00 KST)과
   데이마켓(09:00–17:00 KST)에 각각** 실행.
   ★ **데이마켓은 세션 초반(09:00~10:00)과 중반을 나눠 두 번 측정하라.**
   유동성이 가장 얇은 구간이 `first_print` 트리거의 최악 조건이다.
   ★ **성공 기준**: 최악 조건에서도 `first_print`/`staleness` 신호가 의미 있게 동작해야 한다.
   소형주 `no_print` 비율을 반드시 수치로 기록하라. 동작하지 않으면 A2 §3 의 Tier1 승격 설계
   자체를 재검토해야 하므로 **즉시 escalate** 하라.
2. **1분봉 보관 경계 이분탐색** — W1 이 1024일 hit / 2048일 miss 까지만 좁혔다.
   약 10콜로 경계를 좁혀라. **대형주 1 + 소형주 1** 각각 측정(종목별로 다를 수 있다).
   백필 계획(docs/03 §1-A)의 규모 산정에 직결된다.
3. **1분봉 timestamp 가 봉의 시작인지 끝인지 확정** — 스펙은 "봉 시작 시각"이라 하나 실측이 없다.
   방법: 활발한 종목의 `/trades` 체결 시각을 받아 어느 1분봉에 매핑되는지 대조
   (체결 t 가 봉 T 에 대해 `T <= t < T+60s` 이면 시작, `T-60s < t <= T` 이면 끝). 1~2콜.
   이 값이 틀리면 이벤트 T0 와 리드타임이 전부 1분씩 밀린다.
4. 여력이 되면 429 재현 시 헤더 실측(W1 미완). 무리하지 마라.

## 작업 B — 야간 1세션 무인 수집 리허설
정규장 1세션을 collector 로 무인 수집한다.
- 엔트리포인트(확정): `python -m tossmon.collector --config config/config.yaml`
- `config/config.yaml` 은 gitignore 다. `config.example.yaml` 을 복사해 만들고
  `api.live: true`, `api.base_url: https://openapi.tossinvest.com` 로 설정하라. **커밋 금지.**
- `ops/supervisor.py` 로 감시 실행하고, 종료 후 `tools/dryrun_night.py` 로 리포트를 만들어라.
- 리포트에 반드시 담을 것: 수집 커버리지, **그룹별 실제 rate limit 사용률과 여유**,
  429 발생 횟수, 결측 구간, 티어 승격/강등 횟수(경로별 — precursor/confirm 구분),
  이벤트 후보 목록, 세션 중 크래시·재시작 여부.
- ★ **예산 실측이 핵심이다**: config 의 계산상 MARKET_DATA 6.42/7.0, CHART 2.73/3.5 인데
  실제로 얼마가 나오는지, BudgetGuard 가 티어를 줄였는지를 수치로 보고하라.

## 산출물
`docs/11_live_rehearsal.md` — 작업 A 실측 결과(항목별 "확인됨/미확인 + 근거 스니펫(마스킹)")와
작업 B 세션 리포트. 추측을 사실로 쓰지 마라. `docs/08_runbook.md` 는 실제 운영에서 틀린 부분이
있으면 갱신하라.

## 불변 규칙
1. 리스 보유자는 너 하나다. 작업 종료 후 반납 상태를 보고하라.
2. 계약 변경 금지. 필요하면 `ask`.
3. 소유 경로 밖 수정 금지. 완료 시 `git diff --name-only $(git merge-base main HEAD)..HEAD` 로 증명.
4. **거래 코드 금지** — 주문/조건주문/계좌변경을 어떤 형태로도 호출하지 않는다.
   `TossClient` 는 GET-only 로 하드 차단돼 있으니 우회하지 마라.
5. 시크릿을 출력·로그·커밋·메시지에 넣지 마라. 픽스처는 마스킹.
6. `main` 직접 커밋·머지 금지. 자기 브랜치에만.
7. **보고 전 `git log` 로 커밋을 확인하라.**
8. 설계 전제를 깨는 발견이면 자체 판단 말고 즉시 escalate (특히 작업 A-1 성공 기준 실패).
9. `worker_done` 정확히 1회, 보고 포맷 (a)~(e), 이후 idle.
