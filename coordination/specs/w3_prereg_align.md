# W3 — 사전등록 정합 3건 + 표본 필터 (분석 착수 전 최종 blocker)

너는 W3(`tossmon/analysis/**`, `tools/report.py`, `tests/synth.py`, `tests/test_analysis*.py`,
`docs/07_analysis_spec.md` 소유자)다. 직전 태스크(M-3/M-4)는 머지 완료(`c6f1886`).
이번 건은 **네가 `coordination/HANDOFF-W3.md` §3에 스스로 기록한 사전등록 미정합**이다 —
그 문서의 3-1~3-4를 그대로 집행한다. 정본은 `docs/12_preregistration.md`(읽기만 — 수정은 W7 소유).

**라이브 호출 금지**(`TOSS_LIVE=0`, mock). W5가 라이브 리스로 재수집 중이다 —
토큰 발급 금지, **W5 DB 접근 금지.**

## 시작 절차

`git rebase main` 먼저 (main = `6def9b5`, 749 passed·1 skipped — 네 M-3/M-4 머지 포함).

## 작업

1. **[P0] §2.2 곡선 20일 창** — `minute_of_session_volume_curve`에
   `window_days: int = 20` (평가일 D에 대해 **엄격히 과거 최근 20 매매일**만).
   `min_days=10`과 함께 §2.2 규약 전체가 코드로 성립해야 한다.
   주의: M-4로 곡선이 3단 색인이 됐다 — 20일 창은 **매매일** 기준이며 (세션,길이) 버킷별
   관측일 카운트와 상호작용한다. 정의·경계조건을 `docs/07` §2.4에 명시.
2. **[P0] §2.3 `gap_from_prev_close` 원주가×수정주가 혼합 제거** — `features.py`가
   `close_cut`(1분봉 = 원주가)을 `baseline["close_last_u"]`(일봉 = 수정주가)로 나눈다.
   `prev_close_u` 키워드(직전 매매일 **정규장 마지막 1분봉 종가, 원주가**)를 받아 쓰고,
   못 받으면 NaN. baseline은 ATR%·거래량 z 등 **비율 지표** 용도로만.
3. **[P0] §2.3 `detect_events` 전일종가 대체 사슬** —
   `prev_close_u` → 직전 매매일 **정규장** 마지막 봉 → 직전 매매일 마지막 봉 →
   (없으면 **당일 조건(+30%) 판정 스킵**, 윈도우 조건만, `kind='win'`).
   **당일 첫 시가 대체 금지**(§2.3 명문). 현재 2단계 대체가 애프터마켓 봉일 수 있는
   문제도 §2.3 문언대로 같이 정리.
4. **[P1] §2.7 표본 필터** — `evaluate.py`에
   `apply_sample_filter(events, meta) -> (kept, reasons_df)`:
   보통주(STOCK/FOREIGN_STOCK, `is_common`)·ACTIVE, T0 봉 종가(원주가) ∈ [$0.10, $20.00],
   시총(`sharesOutstanding × T0 종가`) ∈ [$10M, $300M]. **사유별 제외 카운트** 보고,
   `run_all` 리포트 섹션으로 노출. 심볼 메타는 W2 `Reader.symbols()`.
   "반일장(세션 길이 표본 부족)" 사유도 카운트에 포함
   (출처 `curve.attrs["dropped_below_min_days"]`).
5. **[P2 — 여력 있으면]** §7-f ±1분 감도 규칙(Q6 버킷 경계), §2.6 `half_peak`
   비사용 못박기(주석+테스트). 못 하면 (c)에 남겨라.

## 요구

- 각 P0 항목에 **수정 전이면 실패하는 회귀 테스트** (stash 검증 방식 권장 — 네가 지난번에 쓴 방법).
- `docs/07_analysis_spec.md` 정의식 갱신.
- 사전등록과 어긋나는 지점이 **더** 발견되면 고치지 말고 (c)에 적어라 — docs/12는 W7 소유.
- 계약 C-7 위치인자 시그니처 불변, 확장은 키워드 전용 + 기본값(A1 §2 규약).

## 불변 규칙

1. 라이브 호출 금지, `api_keys` 읽기 금지, W5 DB 접근 금지.
2. 소유 경로 밖 수정 금지. 완료 시 `git diff --name-only $(git merge-base main HEAD)..HEAD`로 증명.
3. `main` 직접 커밋·머지 금지. `feat/analyzer`에만 커밋. 보고 전 `git log` 확인.
4. `worker_done` 정확히 1회, 보고 (a)~(e), 이후 idle.
5. 콘솔에 비ASCII 출력 금지(cp949). 파일 입출력은 `encoding="utf-8"` 명시.
