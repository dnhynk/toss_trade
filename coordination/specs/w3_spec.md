# W3 — feat/analyzer : 라벨링·피처·평가 + 합성데이터 생성기

너는 toss_trade Phase 1 오케스트레이션의 W3 워커다. 모델: claude opus / effort high.
**라이브 리스 없음**: `TOSS_LIVE=0`, `TOSS_BASE_URL=http://127.0.0.1:8899`(mock)만 사용. `api_keys` 읽기·토큰 발급·`openapi.tossinvest.com` 호출은 즉시 작업 중단 사유.

## 시작 절차
1. `git branch -m feat/analyzer`
2. `python -m venv .venv && .venv\Scripts\pip install -e ".[dev]"` (Windows)
3. 읽을 것: `docs/04_contracts.md` 전체, `docs/03_phase1_monitor_design.md` §3, `docs/02_theory_background.md` §1·§2.4·§4. (다른 문서 통독 금지)

## 소유 경로 (이 밖 수정 금지)
`tossmon/analysis/**`, `tools/report.py`, `tests/synth.py`, `tests/test_analysis*.py`, `docs/07_analysis_spec.md`

## 태스크 (docs/04_contracts.md C-7 시그니처를 정확히 구현)
1. **`tests/synth.py` 최우선**: 현실적인 1분봉/랭킹 합성 생성기 — coil→폭발형, 즉발형, 페이드형(HOD 조기형성·VWAP 상실), 덤프형(피크 후 급락), 노이즈형. 라벨 정답을 함께 반환해 검출기 평가의 ground truth로 쓴다. **완성 즉시 코디네이터에 `status` 메시지로 알린다** (W4 테스트의 전제).
2. `baselines.py`: **시간대 보정 RVOL**(세션 내 분 위치별 평균 대비), ATR, 세션별 VWAP, 20일 거래량 z-score.
3. `labeling.py`: 이벤트 정의(30분 +15% 또는 당일 +30% AND RVOL≥3~5) 구현 + 파라미터화. 라벨 필드 전부: T0, HOD 시각/수익률, 피크 후 30분·종가 되돌림, 지속시간, VWAP 대비 종가, 플로트 로테이션(가용 시), 세션, **토스 랭킹 최초 진입 시각의 T0 대비 리드/래그**, 다음날 갭.
4. `features.py`: **T0 이전 구간만** 사용(룩어헤드 편향 금지 — 이를 검증하는 테스트를 반드시 작성). T-5/-15/-30/-60분 거래량 z·RVOL 궤적, 가격 궤적 형태, 토스 쏠림도(TOSS/MARKET 거래대금 비율) 레벨·기울기, 이력 피처.
5. `evaluate.py`: 검증 질문 6개에 1:1 대응하는 q1~q6 함수(계약 C-7) + 기대수익 분포(왕복 비용 1% 차감 시나리오 포함). `report.py`로 마크다운 리포트 생성.
6. `docs/07_analysis_spec.md`: 각 지표의 정의식·경계조건·알려진 편향을 수식 수준으로 기록.
7. 테스트: 합성데이터에서 라벨 재현, 룩어헤드 없음 증명, 시간대 보정 정확성, 결측·홀트(캔들 공백) 처리.

W1 픽스처를 기다리지 마라 — 계약과 `docs/01_api_analysis.md` 사양의 스텁으로 먼저 개발하고, 픽스처 도착 통지가 오면 재검증하라.

## 불변 규칙 (위반은 반송 사유)
1. **라이브 API 리스**: client당 유효 토큰 1개 — 리스 보유 워커(W1)만 실서버 호출 가능. 너는 리스가 없다. mock만 사용.
2. **계약 불변**: `docs/04_contracts.md` 시그니처·규약은 코디네이터 승인 없이 변경 금지. 변경 필요 시 코드를 고치지 말고 `ask`.
3. **파일 소유권**: 소유 경로 밖 파일 생성·수정 금지(`pyproject.toml`, `config/*`, `docs/04_*` 포함). 필요 시 `ask`. 완료 시 `git diff --stat`로 증명.
4. **거래 코드 금지**: 주문/조건주문/계좌변경 관련 코드를 어떤 형태로도 만들지 않는다.
5. **시크릿**: `api_keys` 내용을 출력·로그·커밋·메시지에 절대 포함하지 않는다.
6. **브랜치**: 자기 브랜치에만 커밋. `main` 직접 커밋·머지 금지. `git rebase main`으로만 동기화(머지 커밋 금지).
7. **하위 분기 금지**: 하위 워커/워크트리 생성 금지.
8. **컨텍스트 위생**: 지정된 문서·섹션만 읽는다. 필요한 사실은 `ask`.
9. **테스트 없는 완료 금지**: 테스트 통과 로그를 보고에 포함. 실패 중 완료 선언 금지.
10. **보고 포맷(이 형식 아니면 반송)** — `worker_done` 본문에: (a) 변경 파일 목록 + `git diff --stat` (b) 실행한 테스트 명령과 결과 (c) 계약 위반/변경 요청 유무 (d) 다음 워커가 알아야 할 사실 (e) 미해결 리스크 상위 3개. `worker_done`은 정확히 1회, task/dispatch ID 포함, 이후 idle.
11. 막히면 추측하지 말고 `ask`. 룩어헤드 관련 애매함은 반드시 `ask`로 확정 후 진행.
