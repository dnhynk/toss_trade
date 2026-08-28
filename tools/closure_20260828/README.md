# closure_20260828 — `docs/80` 의 탐색 스크립트

`docs/80_closure.md` §2 의 수치를 낸 스크립트를 **그대로** 보존한다. 판정 러너가 아니다 —
사전등록·가드·테스트 없음, 신뢰구간 없음. 이 디렉터리에서 실행하며 산출물은 `./out/`(git 밖).

- DB: `C:/Users/dongh/orca/workspaces/toss_trade/w5-ops/data/tossmon.db`, `mode=ro` + `query_only`
- 창: `2026-08-18T00:00Z ≤ t < 2026-08-26T00:00Z` (탐색 B). 확증 팔·홀드아웃은 읽지 않는다
- 인터프리터: `w3-analyzer/.venv` (pandas·pyarrow). 콘솔 ASCII

순서(의존): `q2_tape_side.py` → `q4_markout.py` → `q9b.py`/`q10_share_markout.py`;
`q7_entry_origin.py` → `q5_event_mech.py`/`q6_arrival_profile.py`; 나머지는 독립.
`q19_variants.py` 는 환경변수 `MAX_HOLD_MIN`·`CALM`·`PARTIAL`·`NO_THROUGH`, 인자 = 공백 허용 초.
