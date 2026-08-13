# W3 — 분석 파이프라인 E2E 리허설 (mock 전용, 내일 Phase 1-C 의 총연습)

너는 W3(`tossmon/analysis/**`, `tools/report.py`, `tests/synth.py`, `tests/test_analysis*.py`,
`docs/07_analysis_spec.md` 소유자)다. 오늘 밤 머지된 조각들(네 시그니처 변경 2회 + W4 의
신규 `tools/backfill.py`)이 **한 번도 전체 순서로 조합 실행된 적이 없다.** 내일 아침 라이브
백필 직후 분석이 시작되므로, 인터페이스 불일치는 오늘 밤 mock 에서 잡아야 한다.
이 프로젝트의 반복 교훈: 브랜치 단독 통과가 조합 통과를 보장하지 않는다.

**라이브 호출 금지**(`TOSS_LIVE=0`, mock 전용), `api_keys` 금지, W5 수집 DB 금지.
산출물은 스크래치패드 + 보고뿐 — 리포 수정은 네 소유 경로에서 실결함 수정 시에만.

## 시작 절차

`git rebase main` (main = `bf80db5`, 821 passed·1 skipped).

## 리허설 시나리오 (docs/12 §8 열람 순서의 총연습)

1. **mock 서버 기동** → `tools/backfill.py` 를 mock 에 대해 실행해 **백필 DB 생성**
   (W4 의 E2E 테스트가 이미 하는 일 — 그 결과 DB 를 그대로 다음 단계 입력으로).
   `--estimate`/`--screen-only`/본실행 3단계 전부 순서대로.
2. 그 DB 를 **W2 Reader 로 열어** 심볼 메타·1분봉·일봉을 읽는다 — 스키마/컬럼/단위
   불일치가 있으면 여기서 드러난다.
3. **분할 스캔**: `detect_split_dates`/`split_scan_report` 를 백필 DB 데이터로 실행,
   `n_r_uncomputable`·`n_widened` 확인.
4. **검출**: `labeling.detect_events` 를 §2.1 파라미터 + `calendar=` + `split_dates=` 로.
5. **베이스라인·피처**: `prereg_volume_curve`(20/10)·`prereg_daily_baseline`(date,
   calendar 새 서명) 경유 — 앵커/서명이 호출 경로 전체에서 성립하는지.
6. **표본 필터 + 평가 + 리포트**: `apply_sample_filter`(meta=Reader.symbols()) →
   `run_all(…, meta=, curve=, calendar=)` → `tools/report.py` 마크다운 생성.
   `(…not_applied)`/`(…not_run)` provenance 행이 안 떠야 정상이다 — 뜨면 그 자체가 발견.
7. **매니페스트 인계 검증**: W4 backfill_manifest 를 읽어 §7-b/§7-e 오염 처리 패스가
   요구하는 입력(공백 구간, adjusted=false 증거, 커버리지)이 실제로 다 있는지 대조.

## 산출물

- 리허설 스크립트(스크래치패드— 내일 실데이터에 경로만 바꿔 재사용 가능하게 작성).
- 발견 목록: (i) 인터페이스 불일치/실결함 — **네 소유면 고치고 회귀 테스트**, 남의 소유면
  재현 증거와 함께 (c)에 (W4 러너 결함이면 특히 상세히 — 아침에 즉시 디스패치한다).
  (ii) 러너 규율 위반 가능 지점 — 감사 §VII 의 강제조항 3줄이 충분한지 실행자 관점 검증.
- (d)에 **내일 Phase 1-C 실행 절차 초안**: 리허설 스크립트 기준으로 실데이터 경로·순서·
  예상 소요를 적어라. 코디네이터가 그걸로 디스패치 스펙을 확정한다.

## 불변 규칙

1. 라이브·`api_keys`·W5 DB 금지. 소유 경로 밖 수정 금지 (merge-base diff 증명).
2. `main` 직접 커밋 금지. 수정이 생기면 `feat/analyzer` 에만. 보고 전 `git log` 확인.
3. 전체 pytest green 유지. `worker_done` 1회, **본문 ASCII**, 보고 (a)~(e), 이후 idle.
4. 콘솔 비ASCII 금지(cp949). 파일 입출력 `encoding="utf-8"`.
