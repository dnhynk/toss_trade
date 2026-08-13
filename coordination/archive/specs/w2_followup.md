# W2 후속 태스크 — 계약 개정 A3 반영 (호가 불균형 컬럼명·규약)

너는 W2(store/universe 소유자)다. 짧은 후속 작업 1건이다. 이전 태스크는 이미 머지됐다(main e218c0f).

## 배경
W4 가 계약 이탈을 발견했다: `tossmon/store/writer.py` 의 `imbalance` 는 부호형 `(bid-ask)/total`
`[-1,1]` 인데, 계약 A2 §1 은 비율형 `bid1/(bid1+ask1)` `[0,1]` 로 적혀 있었다.
중립점이 다르므로(0 vs 0.5) 소비자가 조용히 틀릴 수 있다.

코디네이터 결정 = **부호형을 정본으로 채택**하고 컬럼명이 규약을 드러내도록 이름을 바꾼다.
정본은 main 커밋 269683a 의 `docs/04_contracts.md` **"C-6 개정 A3"** 절이다.

## 시작 절차
1. `git fetch` 불필요. `git rebase main` (main 은 269683a).
   `git branch -m` 은 시도하지 마라(Windows 파일 락). 현재 브랜치 `w2-universe-store` 그대로 쓴다.
2. `git checkout main -- docs/04_contracts.md` 로 계약을 받되, **받은 뒤 `git reset` 으로 언스테이지하라**
   (checkout 은 인덱스에 스테이징까지 한다 — 그대로 커밋하면 소유권 위반이 된다).
3. `docs/04_contracts.md` 의 "C-6 개정 A3" 절만 읽어라. 다른 문서 통독 금지.

## 작업 (소유 경로: `tossmon/store/**`, `tests/test_store*.py` 만)
1. `schema.sql`: `orderbook_snap.imbalance` → **`imbalance_signed`** 로 컬럼명 변경.
   **수집 시작 전이라 저장된 데이터가 없으므로 마이그레이션 없이 직접 수정한다. 스키마 버전은 유지.**
2. `writer.py`: 계산식은 그대로 두되(이미 부호형이 맞다) 컬럼명·변수명을 `imbalance_signed` 로 맞추고,
   **정의를 docstring 에 명시하라** — `(bid_qty_u - ask_qty_u) / (bid_qty_u + ask_qty_u)`,
   범위 `[-1,1]`, **중립 0**, 양수 = 매수 우위, 잔량 0 이면 NULL.
   미국 호가는 1레벨뿐이라 `bid_qty_u == bid1_qu` 이지만, 다레벨 시장 확장을 위해 전 레벨 합산은 유지한다.
3. `reader.py` 가 이 컬럼을 노출한다면 함께 갱신하라.
4. `tests/test_store*.py`: 컬럼명 갱신 + **부호 규약 회귀 테스트를 추가하라** —
   매수 잔량이 많을 때 값이 양수, 균형일 때 0, 매도 우위일 때 음수임을 단언하는 테스트.
   이런 규약은 테스트가 없으면 다음 사람이 또 뒤집는다.
5. 전체 `pytest` 통과 확인.

## 불변 규칙 (이전과 동일)
- 소유 경로 밖 파일 수정 금지. 완료 시 `git diff --name-only $(git merge-base main HEAD)..HEAD` 로 증명.
- 계약 변경 금지(이번 건은 코디네이터가 이미 계약에 반영했다). 이견 있으면 `ask`.
- 라이브 API 호출·`api_keys` 읽기 금지. mock 전용.
- `main` 직접 커밋·머지 금지. 자기 브랜치에만 커밋.
- **보고 전 `git log` 로 커밋을 확인하라.**
- `worker_done` 정확히 1회, 보고 포맷 (a)~(e), 이후 idle.
