# 50. 실시간 검출기와 C-7 개정 A2 — 인용은 낡았고, 동작은 맞았다

> W4, 2026-08-09. 요구 출처: `docs/49` §7-4 (W3 → 코디네이터/W4).
> 소유 범위: `tossmon/collector/**`, `tossmon/api/**`, 테스트, 이 문서.
> **라이브 API 호출 0건.** 전부 합성 프레임 + mock HTTP 서버다.
> **코드 동작 변경 0건 → 수집기 재시작 불필요.**

---

## 0. 한 문단

`detector.py` 의 주석은 폐기된 **A1 §1** 을 인용하며 `include_t0=True` 를 *"실시간이라
합법"* 으로 설명하고 있었다. 인용을 **C-7 개정 A2** 로 갱신하고, 무엇이 바뀌었는지
(봉은 넓어지고 랭킹은 **조용히 엄격해졌다**) 를 그 자리에 적었다. 그리고 **주석이 아니라
동작을 실측했다** — 검출기는 이미 A2 를 지키고 있다. 랭킹 컷 `snap_ms < t0_ms` 누출은
**0** 이고, 캔들 컷은 t0 봉에서 정확히 끝난다(`cutoff_lag_min = 0`). 그 자물쇠가 비어
있지 않다는 것도 함께 쟀다: 컷을 `<=` 로 밀면 채택 스코어가 **+0.140** 움직이고
(tier2 승격선 0.35 의 40%), 라이브 버퍼는 검출 1회당 평균 **7행**을 이 컷 앞에 들이민다.

---

## 1. A1 인용 지점 — **몇 곳 중 몇 곳**

### 1-1. 먼저 구분해야 할 것: A2 는 A1 **§1만** 대체했다

`docs/04` C-7 개정 A2 는 A1 **§1(룩어헤드 컷오프)** 만 대체했다. A1 §2(선택 인자 확장)·
§3(`events.kind`)·§4(추가 라벨 18종)·§6(RVOL 게이트 규약)은 **그대로 살아 있다.**
따라서 "A1 을 인용한다"가 곧 "낡았다"가 아니다. **낡은 것은 §1 인용뿐이다.**
이 구분 없이 일괄 치환했다면 아직 살아 있는 계약 근거 29곳을 함께 지웠을 것이다.

### 1-2. 세어 본 결과

| 범위 | A1 인용 총계 | 폐기된 **§1** 인용 | 갱신함 |
|---|---:|---:|---:|
| `tossmon/**` (`*.py`) | **16** | **1** | **1** |
| `tests/` + `tools/` (`*.py`) | **16** | **2** | **2** |
| **코드 합계** | **32** | **3** | **3 / 3** |

갱신한 3곳:

| 파일 | 무엇이 낡았나 |
|---|---|
| `tossmon/collector/detector.py` (클래스 docstring) | `(계약 A1 §1)` — `include_t0=True` 의 근거를 *"실시간이라 합법"* 으로 설명 |
| `tests/test_analysis_features.py` (모듈 docstring) | *"룩어헤드 부재의 증명 (계약 A1 §1)"* — 그 의무는 이제 **A2 §3** |
| `tests/test_cutoff_amendment_a2.py:142` | `(A1 §1 3항 의무)` — **A2 테스트 안에서** 폐기된 조항을 인용하고 있었다 |

나머지 29곳은 A1 §2·§3·§4·§6 인용이라 **전부 유효하다. 건드리지 않았다.**
(그중 1곳만 절 번호가 없다 — §1-3.)

### 1-3. 절 번호 없는 인용 1곳 (모호 — 안 고쳤다)

`tossmon/analysis/labeling.py:1` 이 `계약 C-7 + **C-7 개정 A1**` 이라고만 적는다.
그 파일이 실제로 쓰는 것은 §3·§4·§6(전부 유효)이라 **틀리지 않았지만**, 절 번호가 없어
"A1 전체가 살아 있다" 로 읽힌다. `tossmon/analysis/**` 는 W3 소유라 손대지 않았다 →
아래 §5 요구 1.

### 1-4. 코드 밖 — 문서 19곳 (내 소유 아님, 요구로 넘긴다)

`docs/` + `coordination/` 에 A1 §1 인용이 **19곳** 있다. 대부분은 **당시의 기록**
(`docs/10`·`docs/10_audit_b`·`docs/14` 감사, `docs/48`·`docs/49` 개정 경위)이라
그대로 두는 것이 맞다. 문제는 **살아 있는 스펙 2곳**이다 → §5 요구 3·4.

---

## 2. 검출기가 A2 와 맞는가 — **실측**

> 가설: A1 §1 의 근거가 틀렸다면(종료 라벨이면 T0 봉은 누구에게나 관측 가능),
> 그 근거 위에 서 있던 `include_t0=True` 도 흔들리고, 특히 **랭킹이 플래그를 따라갔다면**
> 그것이 라이브 룩어헤드다.
> 반증 관측: 검출기에 `snap_ms >= t0` 스냅을 요란하게 심고 결과가 움직이는지 본다.
> 움직이면 누출, 안 움직이면 컷이 서 있다.

### 2-1. 캔들 (A2 §1 `ts_ms <= t0_ms`) — **맞다**

`EventDetector.evaluate()` 에 T0 봉까지 4봉을 주고 t0 = 마지막 봉 라벨로 판정:

| | 값 |
|---|---|
| `cutoff_ms` | `1762180260000` = **`t0_ms` 와 같다** |
| `cutoff_lag_min` | **0.0** |
| `n_bars_pre` | **4.0** (준 봉 4개 전부 — T0 봉 포함) |
| `include_t0` (모드 태그) | **1.0** |

즉 검출기는 T0 봉에서 정확히 멈춘다. A1 이 이것을 *"실시간이라 봐준다"* 로 정당화한 것은
틀렸지만, **결과 동작은 A2 §1 이 요구하는 것과 같다** — 근거가 바뀌었을 뿐이다.

### 2-2. 랭킹 (A2 §2 `snap_ms < t0_ms`) — **지키고 있다. 누출 0.**

t0 이전은 조용하고(90위, 쏠림 1%) **t0 정각부터 폭발**하는(1위, 쏠림 80→99%) 랭킹을
심었다. 심은 스냅: `t0` 정각 / `t0+10초` / `t0+30초`.

| 피처 | 깨끗 | 오염 | |
|---|---|---|---|
| `toss_share` | 0.01 | 0.01 | 불변 |
| `toss_share_max` | 0.01 | 0.01 | 불변 |
| `toss_share_slope_30` | 0.0 | 0.0 | 불변 |
| `toss_in_ranking` | 1.0 | 1.0 | 불변 |
| `toss_rank_best` | 90.0 | 90.0 | 불변 |
| `market_rank_best` | 90.0 | 90.0 | 불변 |
| `minutes_since_toss_entry` | 1.0 | 1.0 | 불변 |
| `ranking_snaps_pre` | 4.0 | 4.0 | 불변 |

**움직인 피처 키: 전체 중 0개.** 채택 스코어 `0.156692` → `0.156692`, 경로 `precursor`,
이벤트 수 동일. **라이브 수집 경로의 랭킹 룩어헤드 크기 = 0.**

경로도 확인했다: `detector.py` 는 랭킹 컷 인자를 **넘기지 않는다.**
`features.extract_precursor_features` 안에서 `include_t0=False` 가 박혀 나간다
(`features.py:259`). 즉 검출기 쪽에서 실수로 밀 수 있는 손잡이가 **없다.**

### 2-3. 이 측정이 죽은 측정이 아니라는 확인 (자기시험)

"아무 일도 안 일어나는 것" 을 확인하는 테스트는 컷을 지워도 통과한다. 두 가지로 확인했다.

**(a) 같은 행을 t0 이전으로 옮기면 실제로 움직인다** — 6개 피처가 반응:
`toss_share_max` 0.01→0.99, `toss_share_slope_30` 0.0→−0.581, `toss_rank_best` 90→1,
`market_rank_best` 90→1, `minutes_since_toss_entry` 1→2, `ranking_snaps_pre` 4→10.

**(b) 옛 버그를 실제로 심어 봤다.** `features.py` 의 랭킹 컷을
`include_t0=False` → `include_t0=include_t0` 로 되돌렸다 — **A2 §2 위반이었던 바로 그
합쳐진 플래그**다. 결과: **5개 테스트가 죽는다** (내 2개 + W3 의 `test_ranking_stays_strict_*`
3개). 심은 뒤 되돌렸고(`git checkout`), 되돌린 뒤 17개 전부 통과를 재확인했다.

> ⚠️ **그 심기에서 W3 의 `test_poisoning_the_t0_snapshot_moves_nothing` 은 통과했다.**
> 이름과 달리 그 테스트는 §2 위반을 못 잡는다 — 실제로 잡는 것은
> `test_ranking_stays_strict_*` 3개다. §5 요구 4.

---

## 3. 컷이 라이브에서 실제로 걸리는가 — **걸린다**

자물쇠가 라이브에서 한 번도 안 걸린다면 장식이다. mock HTTP 전 구간
(`rankings_once` → `tier2_symbol_once` → `_detect`)을 실제로 돌려 검출 시점 버퍼를 셌다.

구조가 원인이다: `snap_ms` 는 **우리 관측 시각**(`rankings_once` 안의
`snap_ms = ctx.clock.now_ms()`)이고 t0 는 **직전 완성봉**이다.
그래서 봉이 닫힌 뒤 받은 스냅은 **정의상 전부 `> t0`** 다.

| | 값 |
|---|---|
| 검출 호출 (버퍼 비어 있지 않음) | 30회 |
| 검출 시점 `now − t0` | 최소 1초 / **중앙 31초** / 최대 51초 |
| `snap_ms >= t0_ms` 로 **버려진** 행 | **210행 = 검출 1회당 평균 7.0행** |
| 10분 워밍된 버퍼(120행/심볼) 대비 비율 | **5.5%** (3810행 중 210행) |
| 버퍼를 비운 직후로 한정하면 | **210 / 210 = 100%** |

**5.5% 라는 비율은 워밍 길이에 의존한다.** 버퍼 상한은 720행/심볼
(`RANKING_ROWS_PER_SYMBOL`), 관측된 적재율 2행/폴(10초 격자) 기준 **약 60분**이므로,
포화 상태에서는 같은 7행이 **약 0.8%** 가 된다. 비율은 흔들리지만 **불변인 사실**은
비율이 아니라 이것이다: **버려지는 것은 언제나 가장 최신 행들**이고, 그것이 쏠림도를
가장 크게 움직일 정보다. 아래 §4 가 그 크기다.

---

## 4. 컷을 `<=` 로 밀면 얼마나 움직이나 (반사실)

t0 정각에 "1위 · 쏠림 80%" 스냅 하나를 놓고, 엄격 `<` 과 완화 `<=` 를 같은 입력에서 비교:

| 피처 | 엄격 `<` | 완화 `<=` |
|---|---|---|
| `ranking_snaps_pre` | **4.0** | **6.0** |
| `toss_share` | 0.01 | **0.80** |
| `toss_share_max` | 0.01 | **0.80** |
| `toss_share_slope_30` | 0.0 | **+0.441** |
| `toss_rank_best` | 90.0 | **1.0** |
| `market_rank_best` | 90.0 | **1.0** |
| **채택 스코어** | **0.156692** | **0.296692** (**+0.140**) |

`ranking_snaps_pre` **4.0 대 6.0** 은 W3 이 `docs/49` 에서 잰 것과 **같은 수**다 —
서로 다른 픽스처·다른 레이어에서 같은 숫자가 나왔다.

+0.140 의 뜻: tier2 승격선이 **0.35**, tier3 이 **0.60** 이다. 승격 문턱의 **40%** 를
t0 에 손에 없던 스냅 하나로 얻는다. 이것이 A2 §2 가 지키는 것의 크기다.

---

## 5. 넘기는 요구 (내가 안 고친 것)

**W3 소유 (`tossmon/analysis/**`, W3 테스트):**

1. **`labeling.py:1` 의 절 번호 없는 `C-7 개정 A1`.** 그 파일이 쓰는 것은 §3·§4·§6
   (유효)이지만, 절 번호가 없어 폐기된 §1까지 살아 있는 것으로 읽힌다. **§ 를 명시하거나
   "§1 은 A2 로 대체됨" 한 줄을 붙일 것.**
2. **`test_poisoning_the_t0_snapshot_moves_nothing` 이 §2 위반을 못 잡는다** (§2-3(b) 실측).
   이름이 방어를 약속하는데 실제 방어는 옆 테스트 3개가 한다. **이름을 바꾸거나 프로브를
   강화할 것** — 지금 상태로는 "포이즈닝 테스트가 있다" 가 잘못된 안심을 준다.
   (인용은 내가 A2 §3 으로 고쳤다. 프로브 강도는 W3 판단이다.)

**코디네이터 소유:**

3. ★ **`docs/07_analysis_spec.md` §4.1 — 이번 건에서 제일 위험한 잔존물이다.**
   제목이 `### 4.1 컷오프 (계약 A1 §1)` 이고, 본문이 **`랭킹도 같은 규칙을 snap_ms 에
   적용한다`** 라고 적는다. 이것은 **A2 §2 가 금지한 합쳐진 플래그를 스펙이 지시하고
   있는 것**이다. 기본값 표기도 옛것이다(`include_t0=False (기본, 연구)` — A2 는 True 가
   기본). `docs/07` 은 내 소유가 아니라 손대지 않았다.
   **다음 사람이 코드가 아니라 이 스펙을 읽고 짜면 옛 버그를 그대로 재현한다.**
4. **`coordination/specs/w4_spec.md:31`** 이 `include_t0=True` 의 근거로 `(계약 개정 A1-1)`
   을 인용한다. 지시 자체는 A2 에서도 맞지만 근거 조항이 폐기됐다.
5. **`docs/04` C-7 개정 A2 는 `tossmon/analysis/` 를 대상으로 쓰여 있다.** 그런데 룩어헤드가
   실제 매매 판단으로 새는 경로는 `collector/detector.py` 다. 계약 문언이 실시간 경로를
   명시적으로 포함하는지 한 줄 정리를 요청한다(내가 `docs/04` 를 고칠 수 없어 요구로 올린다).

**판정이 아니라 관측으로만 남기는 것:**

6. `labeling.py` 의 `ranking_first_entry_ms` / `ranking_lead_lag_min` 은 매매일 구간
   `[t_from, t_to)` 전체를 보므로 **t0 이후 스냅을 읽는다.** 이것은 컷오프가 아니라
   **라벨(정답)** 이고 `score_paths` 에 들어가지 않으므로 A2 §2 대상이 아니라고 읽었다.
   **판정하지 않는다** — `snap_ms` 비교를 훑는 사람이 반드시 마주칠 자리라 적어 둔다.

---

## 6. 혼자 내린 판단 — 자기 신고

물어보지 않고 정한 것들이다. 되돌릴 근거가 있으면 되돌려야 한다.

1. **살아 있는 A1 §2·§3·§4·§6 인용 29곳을 건드리지 않았다.** A2 가 §1만 대체했기
   때문이다(§1-1). 일괄 치환이 더 "깨끗해" 보이지만 유효한 계약 근거를 지운다.
2. **W3 이 방금 쓴 `tests/test_cutoff_amendment_a2.py` 의 한 줄을 고쳤다.** 테스트는 내
   소유고 A2 테스트 안의 A1 §1 인용은 그냥 두면 안 된다고 봤다. **docstring 한 줄뿐이고
   단언은 손대지 않았다.**
3. **`loops.py` `RankingBuffer` docstring 에 없던 경고를 새로 넣었다.** 요구에 없던
   추가다. 근거: §3 실측대로 이 버퍼의 최신 행은 **항상** 컷에 걸리는데, 그 사실이
   어디에도 적혀 있지 않아 "왜 최신 스냅을 버리지" 로 읽고 고칠 위험이 있다.
4. **`detector.py` 호출부(`evaluate`)에도 한 줄 주석을 넣었다.** 클래스 docstring 에
   이미 있지만, 플래그를 실수로 미는 손이 닿는 자리는 호출부다.
5. **비율(5.5%) 을 결론 숫자로 삼지 않았다.** 워밍 길이에 의존해서다(§3). 결론으로 삼은
   것은 **검출 1회당 7행**과 **"항상 최신 행"** 이라는 구조다.
6. **테스트를 새 파일로 만들었다** (`test_a2_collector_alignment.py`).
   `test_collector_detector.py` 에 끼우지 않은 이유는, 그 파일은 스코어 캘리브레이션이
   목적이라 임계값이 바뀌면 같이 흔들리는데 **컷 자물쇠는 그것과 독립이어야** 하기 때문이다.

---

## 7. 바꾼 것 / 안 바꾼 것

**바꾼 것 (전부 주석·문서·테스트. 실행 경로 0줄):**

- `tossmon/collector/detector.py` — 클래스 docstring(A1 §1 → C-7 개정 A2 + 무엇이 바뀌었나),
  `confirm_score` docstring, `evaluate()` 호출부 주석 3줄
- `tossmon/collector/loops.py` — `RankingBuffer` docstring 경고
- `tests/test_analysis_features.py` · `tests/test_cutoff_amendment_a2.py` — 인용 갱신(docstring)
- `tests/test_a2_collector_alignment.py` — **신규**, 자물쇠 5개
- `docs/50_a2_alignment.md` — 이 문서

**안 바꾼 것:** 검출기 동작. **A2 위반이 없으므로 고칠 것이 없다.**
따라서 **수집기 재시작이 필요 없다.**

"주석만 바꿨다" 는 말은 근거가 있어야 한다. 네 파일 전부 **docstring 을 제거한 AST 가
HEAD 와 완전히 동일**함을 확인했다(§8 의 재현 스크립트). 즉 실행되는 코드는 0줄 바뀌었다.

**이름 충돌 주의 (이 갱신에서 실제로 걸린 함정):** `collector/` 안의 `A2 §n` 은 전부
**C-6/C-8 개정 A2**(호가 1레벨 / `/trades` 50건 / `/prices` 파생 상태)를 가리킨다
(개정 전 기준 `detector.py` 6곳, `loops.py` 7곳, `config.py` 2곳 — 합 15곳).
그래서 새 인용은 전부
**`C-7 개정 A2`** 로 풀어 썼다. 짧게 `A2` 라고만 쓰면 같은 파일에서 두 개정이 섞인다.

---

## 8. 재현

```bash
python -m pytest tests/test_a2_collector_alignment.py -q          # 5 passed
python -m pytest tests/test_a2_collector_alignment.py \
                tests/test_cutoff_amendment_a2.py -q              # 17 passed
python -m pytest -q                                               # 전체 회귀
```

전체 스위트 실측(2026-08-09, 이 커밋 기준): **1954 passed, 1 skipped, 2 deselected**
(6분 47초). 실패 0.

"실행 코드 0줄" 확인 (§7):

```bash
python - <<'PY'
import ast, subprocess
def strip(src):
    t = ast.parse(src)
    for n in ast.walk(t):
        if isinstance(n, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            b = n.body
            if b and isinstance(b[0], ast.Expr) and isinstance(b[0].value, ast.Constant) \
               and isinstance(b[0].value.value, str):
                n.body = b[1:] or [ast.Pass()]
    return ast.dump(ast.fix_missing_locations(t))
for f in ("tossmon/collector/detector.py", "tossmon/collector/loops.py",
          "tests/test_analysis_features.py", "tests/test_cutoff_amendment_a2.py"):
    old = subprocess.run(["git","show",f"HEAD~1:{f}"],capture_output=True).stdout.decode("utf-8")
    print(f, strip(old) == strip(open(f,encoding="utf-8").read()))
PY
```

옛 버그 심기(§2-3(b)) 재현 — `tossmon/analysis/features.py` 의 랭킹 컷을
`include_t0=False` → `include_t0=include_t0` 로 바꾸고 위 두 파일을 돌리면
**5 failed**(내 2 + W3 3). 반드시 `git checkout -- tossmon/analysis/features.py` 로 되돌릴 것.

라이브 API 호출은 **0건**이다 — §2 는 합성 프레임, §3 은 mock HTTP 서버
(`tests/test_collector_helpers.mock_server`, 계약 C-9 대로 `live=False` 고정 토큰)다.
