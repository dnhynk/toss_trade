# HANDOFF — W6 (적대적 감사자)

> 런타임 리셋 대비 인수인계. 작성 시각 2026-07-31, 리셋 예고 직후.
> 옛 taskId(`task_88a85411d02f`)·dispatchId(`ctx_411a9fd3357c`)는 **버릴 것**.

## 신원

| 항목 | 값 |
|---|---|
| 워커 ID | **W6** (적대적 감사, fable/xhigh) |
| 브랜치 | `w6-audit` |
| 워크트리 | `C:\Users\dongh\orca\workspaces\toss_trade\w6-audit` |
| 내 HEAD | **`7992aca`** — `W6: 적대적 감사 결과 (docs/10_audit.md)` |
| 머지 상태 | **머지 완료.** main 의 `603b583 Merge W6: adversarial audit (fable/xhigh)` |
| 워킹트리 | **깨끗함** (uncommitted 없음) |
| 라이브 리스 | 없음. 감사 내내 라이브 호출·토큰 발급 0건, `api_keys` 읽지 않음 |

⚠️ 내 브랜치는 `e1ed22d` 기준이라 **main 보다 한참 뒤처져 있다** (main = `4869293`).
내 작업은 이미 머지됐으므로 **이 브랜치를 다시 쓸 이유는 없다.**
W6 를 다시 부를 때는 `main` 에서 새 브랜치를 따는 편이 낫다.

## 직전 태스크의 요지

"**반증하려는 태도로** 코드를 감사하라. 코드는 고치지 마라. 산출물은 `docs/10_audit.md` 하나."
지정된 감사 영역 11개: ① 토큰 단일성 파괴 ② rate limit 초과 ③ 룩어헤드 편향
④ 시간대·서머타임·세션 경계 ⑤ 시크릿 유출 ⑥ 크래시 후 데이터 무결성 ⑦ 계약 위반·중복 구현
⑧ GET-only 차단 우회 ⑨ events 중복 2층이 함께 뚫리는 경로 ⑩ 승격/강등 churn ⑪ 유니버스 오염.

제약: 확신 없는 것은 **"미확인"** 으로 분류할 것. 각 항목에 **재현 시나리오**를 붙일 것.
실패하는 테스트를 써 보되 **커밋하지 말 것**.

## 끝낸 것

**감사 완료. `7992aca` 하나로 끝났다** (`docs/10_audit.md`, 1128행, 코드 수정 0건).

결함 32건 + 미확인 11건. 전부 샌드박스에서 실제로 실행한 재현과 출력을 인용한다.

- **치명 3**: F-1 토큰 리스가 CWD 다른 프로세스를 못 막음 / F-2 유니버스 필터가 수집 경로에
  전혀 적용 안 됨(`build_universe` 호출자 없음) / F-3 전일 이벤트 자기오염 재판정 + UPSERT 덮어쓰기
- **높음 9**: H-1 토큰 다중발급·AUTH 무제한 / H-2 헤더 상한 부재(7→420 req/s) /
  H-3 1초 버스트가 공시한도 1.4배 / H-4 429 감속 1초 소멸 / H-5 `ForbiddenEndpoint` 삼킴 /
  H-6 베이스라인 영구 동결 / H-7 랭킹 승격 플래핑 / H-8 ops 기본 `collector_cmd` 오류 /
  H-9 재시작 시 `candles_1m` 무경고 영구 구멍
- **중간 12 / 낮음 7 / 미확인 11**

## 다음에 할 일 (재개 지점)

내 감사 자체는 **끝났다.** 자연스러운 다음 태스크는 **"수정 검증(fix verification)"** 이다.
리셋 직전에 main(`4869293`)의 소스를 직접 대조해 아래까지 확인해 뒀다 —
**이 표가 재개 지점이다.**

### 수정 확인됨 (main 소스에서 직접 대조)

| 감사 # | 수정 근거 (main) |
|---|---|
| F-1 | `tokens.py:75-77` `lease_path_for_client()` — client_id sha256, 리포 밖 고정 위치 |
| H-1 | `tokens.py:116` `invalidate(token=None)` CAS 시그니처, `:83,89` limiter 주입 |
| H-2 | `limiter.py:157-158` 상한 클램프 + `limit_header_clamped` 카운터 |
| H-3 | `limiter.py:35,74` `_capacity_for()` — capacity 를 rate 비율로 축소 |
| H-4 | `limiter.py:33,67-71` `RECOVER_INTERVAL_S=60` 경과시간 기준 회복 |
| H-5 | `errors.py:45` `class ForbiddenEndpoint(Exception)` — `TossApiError` 밖으로 |
| F-2 | `loops.py:52,971` `passes_tier0` 게이트 + W2 의 `build_universe` 진입점(`15022a2`) |
| F-3 | `loops.py:1153` 실시간 판정 당일 제한 |
| H-6 | `loops.py:1451-1453` `baselines.clear()`/`prev_close.clear()`/`history_days=[]` |
| H-7 | `detector.py:362-364` `force()` 가 dwell 을 더는 우회하지 않음 |
| H-9 | `loops.py:1223-1235` gap 기반 페이지 산정, `:1300-1303` 미달 시 warn |
| M-1 | `tokens.py:299-312` `error_description` 을 fingerprint 로 대체 |
| M-11 | `endpoints.py:75-80` dot segment 정규화 대신 **거부** |
| M-7 | `d787c41` live_probe 호스트 상수 정리 |

### **아직 안 고쳐진 것 (main 에서 확인)** ← 여기서 재개

1. **H-8 (높음) — `ops/opsconfig.py:64` 가 여전히 `["python","-m","tossmon.collector.main"]`.**
   그런 모듈은 없다(진입점은 `tossmon.collector`). `ops_config.yaml` 에 `collector_cmd` 가
   없으면 supervisor 가 즉사하는 자식을 반복 spawn 하다 재시작 폭주 가드에 걸려 **조용히 포기**한다.
   **남은 것 중 가장 심각하다. 한 줄 수정이다.**
2. **M-12 (중간)** — `last_ranking_snap_ms` 가 `state_snapshot`(`loops.py:703`)에 저장되지만
   `load_state` 는 복원하지 않고, 코드 어디서도 읽지 않는다. `rankings_snap` 은 사후 조회가
   불가능한 유일한 데이터인데 정전 구간 경고가 없다.
3. **M-8 (중간)** — `evaluate.py:63` `_gate` 가 `rvol_gated` 컬럼이 없으면 **조용히 전부 통과**시키고
   `n_ungated_excluded=0` 을 보고한다. `Reader.read_events` 는 `meta_json` 을 펼치지 않으므로
   `expand_meta_json` 없이 q1~q6 를 부르면 A1 §6 게이트가 통째로 무력화된다.
4. **M-2 (중간)** — `models.py:62` `int(dt.timestamp()*1000)` 절삭 그대로. `round()` 로 바꾸면 끝.
5. **M-10 (중간)** — `tools/dryrun_night.py:42-46` `EXPECTED_INTERVAL_S` 가 `trades:8/orderbook:8`,
   config 는 `4/16`. 테이프 공백을 놓치고 호가 공백을 허위 경보한다.
6. **M-3 / M-4 (중간)** — 수정 흔적을 못 찾았다(재확인 필요).
   ⚠️ **M-4 는 특히 주의**: `baselines.py:5-7` 의 docstring 이 "조기폐장·세션 길이 차이에
   **자동 대응한다**"고 적어 두었는데 **내 재현이 그 반대를 보였다** (반일장 종가 스파이크가
   `rvol_bar` 100배). 문서가 틀린 채로 남아 있으면 다음 사람이 믿는다.
   **M-3 은 2026-11-01 서머타임 종료부터 발현한다 — 기한이 있다.**
7. **M-5, M-6, L-1~L-7** — 미확인/미재확인. `SESSION_TIER_SCALE[CLOSED]=0.0`(`loops.py:129`)은
   그대로라 M-5(세션 종료 시 티어 일괄 강등)는 남아 있을 것으로 보인다.
   `on_new_data(..., reason="score")`(`detector.py:342`)와
   `compute_daily_baseline(df_1d, window_days=20)`(`baselines.py:78`)도 그대로 → M-6 미해결.

### 미확인 중 값싸게 해소되는 것 (우선)

- **`X-RateLimit-Limit` 의 단위(초당/분당).** H-2 의 심각도가 여기 달려 있었다.
  main 이 상한 클램프를 넣어 위험은 막혔지만 **단위 자체는 여전히 미확인**이다.
  다음 라이브 리스 때 응답 헤더 한 줄만 찍으면 확정된다. **가장 싼 미확인 해소.**
- **`promotions` 테이블 `reason` 별 집계.** 라이브 강등 221/승격 266 이 H-7(랭킹 플래핑)과
  M-5(세션 경계 일괄 강등) 중 어느 쪽이 주범인지 확정한다. H-7 은 고쳐졌으므로
  **이 집계로 수정 효과를 실측할 수 있다.** DB 는 W5 소유라 나는 열지 않았다.

## 막힌 것 / 코디네이터 답 대기

**없다.** 태스크는 `worker_done` 으로 완료 보고했고 미해결 질문이나 대기 중인 `ask` 는 없다.
감사 중 `ask` 를 쓸 일도 없었다 (읽기 전용이라 결정을 요구하는 지점이 없었다).

## 다음 사람이 모르면 손해 보는 사실

### 1. 이 워크트리에서는 pytest 가 그냥 안 돈다 (환경 문제, 코드 문제 아님)

두 가지가 겹쳐 있다:

- `filelock` 이 설치돼 있지 않다 → `tossmon/api/tokens.py:23` 에서 collection error 20건.
- **전역 site-packages 에 무관한 `tests` 패키지가 있어** 리포의 `tests/` 를 가린다
  (리포 `tests/` 에는 `__init__.py` 가 없어 namespace package 라 정규 패키지에 진다).
  → `ImportError: cannot import name 'synth' from 'tests'
  (C:\...\Python313\Lib\site-packages\tests\__init__.py)`

**`__init__.py` 를 추가해서 고치려 하지 마라** — 그러면 `from test_api_support import ...`
같은 형제 임포트가 깨진다(두 방식이 공존한다). 내가 쓴 우회는 샌드박스 루트에 `conftest.py`
하나를 두는 것이다:

```python
import os, sys, types
root = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(root, "tests"))
sys.path.insert(0, root)
if "tests" not in sys.modules:
    m = types.ModuleType("tests"); m.__path__ = [os.path.join(root, "tests")]
    sys.modules["tests"] = m
```

`pip install --target <dir> filelock` 로 `filelock` 만 따로 깔면 나머지(pandas/httpx/yaml)는
전역에 이미 있다. **운영·CI 환경과 다른 점이라 "테스트가 깨졌다"고 오판하지 말 것.**

#### ⚠️ 그리고 filelock 경로는 반드시 **`PYTHONPATH` 로** 줘라 (내가 실제로 걸린 함정)

`conftest.py` 안에서 `sys.path.insert` 로 filelock 을 넣으면 **자식 프로세스가 물려받지 못한다.**
`tests/test_api_audit_regressions.py::test_lease_blocks_a_genuinely_separate_process` 는
`subprocess.Popen` 으로 **진짜 별도 프로세스**를 띄워 OS 파일락을 검증하는데(감사 F-1 의 회귀 테스트),
그 자식이 `filelock` 임포트에 실패해 죽는다:

```
E   Failed: holder died: Traceback (most recent call last):
E   ModuleNotFoundError: No module named 'filelock'
```

**이건 회귀가 아니라 내 샌드박스 구성 실패였다.** 처음 돌렸을 때 `1 failed, 693 passed` 가 나와서
"F-1 회귀 테스트가 깨졌나" 싶었는데, `PYTHONPATH` 로 다시 주니 그 파일 35개가 전부 통과했다.
**하필 가장 심각했던 감사 지적(F-1 토큰 리스)의 회귀 테스트라 오진하기 딱 좋다** — 조심할 것.

```
# 올바른 실행
PYTHONPATH="<filelock --target 디렉터리>" python -m pytest -q
```

### 2. 감사에 쓴 재현은 리포 밖에 있고, 커밋하지 않았다

코드 수정 금지 원칙 때문이다. 위치:
`...\scratchpad\sandbox\tests\test_w6_*.py` (내가 쓴 실패 테스트 6개),
`...\scratchpad\lookahead\`, `...\scratchpad\tz\`, `...\scratchpad\integrity\`,
`...\scratchpad\contracts\` (병렬 감사 재현 스크립트).
**이건 세션 임시 디렉터리라 리셋/재부팅으로 날아간다.** 재현이 다시 필요하면
`docs/10_audit.md` 본문의 출력 인용을 보고 다시 쓰는 편이 빠르다 — 그러라고 전부 인용해 뒀다.

### 3. 감사 방법론 — 재감사할 때 그대로 쓸 것

- 리포는 **읽기만** 하고, `git archive main | tar -x -C <sandbox>` 로 사본을 떠서 거기서 실행했다.
  이러면 실패 테스트를 마음대로 쓰면서 리포를 한 글자도 안 건드린다.
- 감사 영역을 4갈래로 나눠 병렬 서브에이전트에 돌리고(룩어헤드 / 크래시무결성+중복 /
  시간대 / 계약준수), 토큰·rate limit·allowlist·churn·유니버스는 직접 했다.
  각 서브에이전트에 "리포 파일 절대 수정 금지 + 샌드박스 경로 + 증명한 것과 의심하는 것을
  분리하라"를 명시적으로 넣었다. 그러지 않으면 추측이 결함 목록에 섞인다.
- **가장 값진 발견 2건은 "grep 으로 없는 것을 찾아서"** 나왔다:
  F-2 는 `grep -rn "build_universe"` 로 **호출자가 없다**는 걸 본 것이고,
  H-6 은 `grep -n "baselines"` 로 **지우는 코드가 없다**는 걸 본 것이다.
  있는 코드를 읽는 것만으로는 안 나온다.

### 4. 라이브 금지

리셋 전후로 라이브 호출·토큰 발급 금지(예외 없음). 라이브 리스는 아무도 안 갖고 있다.
mock 이 필요하면 `python tools/mock_server.py --port 8899`,
`TOSS_BASE_URL=http://127.0.0.1:8899`, `TOSS_LIVE=0`.

## 테스트 상태

| 대상 | 결과 | 비고 |
|---|---|---|
| 내 브랜치 `w6-audit` @ `c06b068` | **636 passed** | 기준선 `e1ed22d` 와 동일 — 내 커밋 2개가 전부 문서라 코드 영향 0 |
| main `4869293` | **694 green — 직접 재실행해 확인함** | 코디네이터 보고와 일치 |

```
# 이 워크트리에서 그냥 실행하면 collection error 로 실패한다 (§1 참조). 샌드박스에서:
git archive main | tar -x -C <sandbox>
cp <conftest.py — 위 §1>  <sandbox>/conftest.py
cd <sandbox> && PYTHONPATH="<filelock --target 디렉터리>" python -m pytest -q    # 약 5분
```

**PYTHONPATH 를 빼먹으면 `1 failed, 693 passed` 가 나오는데 그건 가짜다** — 위 §1 의 경고 참조.
내가 정확히 그렇게 한 번 오진할 뻔했다.

## 규칙 준수 확인

- 코드 수정 0건. `docs/10_audit.md` 외 리포 파일 미변경 (이 HANDOFF 는 예외 허가분).
- `main` 직접 커밋 없음. 전부 `w6-audit` 브랜치.
- 라이브 호출 0건, `api_keys` 미열람.
- 검증 테스트 미커밋.
