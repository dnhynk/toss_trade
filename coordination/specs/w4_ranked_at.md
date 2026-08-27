# W4 명세 — **`rankedAt` 을 저장한다.** 우리 지연을 상시로 보게 한다

> 발행: 코디네이터 2026-08-27 · 기준 `main` `b33a54a`
> 소유: `tossmon/store/**` · `tossmon/collector/**` · `tossmon/api/**`(필요 시) ·
> `tests/**` · `coordination/specs/**` · **`docs/77`**(신설) · `docs/INDEX.md` 등록 한 줄
> **에이전트: codex · 모델: `gpt-5.6-sol` · effort max.**
> ⚠ **배포는 이 과제가 하지 않는다.** 코디네이터가 별도 창에서 한다.

## 0. 사용자 결정과 왜 지금인가

`docs/75` 가 **G-1a 를 (라) 못 갈랐다**로 냈고, 못 가른 이유가 이것이다:

> **현재 DB 는 `rankedAt` 을 저장하지 않는다.** 운영 로그에도 없다.
> 그래서 *"우리가 얼마나 늙은 랭킹을 받는가"* 를 **상시로 못 본다** —
> 2026-08-18 프로브 보존 JSON 에만 남아 있다.

사용자 결정: **저장한다.** *"수집 품질의 최적화는 모든 트레이드오프를 감수하고 나아가야 한다."*

### 0-1. ★ 그런데 트레이드오프가 생각보다 작다 — 확인하고 시작해라

**(가) 값을 이미 손에 들고 있다.** 새로 받는 게 아니다:

```
tossmon/api/client.py:602    ranked_at_ms=_opt_ms(result.get("rankedAt"))
tossmon/api/models.py:254    ranked_at_ms: int | None
tossmon/collector/loops.py:1763  "rankedAt 은 12~23초 뒤처지므로 참고값으로만 쓴다"
```

파싱해서 모델에 담아 놓고 **저장 단계에서 버린다.** 이 과제는 **버리지 않게** 하는 것이다.

**(나) `config_sig` 가 안 바뀐다.** `loops.py:985` 의 `config_signature()` 독스트링:

> *"의도적으로 **수집 밀도·폭**에 영향을 주는 값만 담는다. …그 외 설정은 지문을 흔들지 않는다."*

랭킹 타입·깊이·폴 주기·티어 상한·승격 정책으로만 만든다. **저장 컬럼은 안 들어간다.**
→ **`G2G3-PREREG` §2-3 의 제외 조건에 안 걸린다. 확증 팔 1 세션은 그대로 유효하다.**

**이 둘을 네가 직접 확인하고 `docs/77` 에 근거와 함께 적어라.** 내 말이 아니라 코드로.

## 1. 할 일

1. **스키마**: `rankings_snap` 에 `ranked_at_ms INTEGER` (**NULL 허용** — 기존 수천만 행은 값이 없다)
2. **마이그레이션**: `store/migrations.py` 의 `MIGRATIONS` 에 다음 버전 추가.
   **`schema.sql` 도 새 설치가 같은 모양이 되게 맞춰라** (둘이 갈리면 조용히 틀린다)
3. **`writer.py`**: `insert_rankings` 가 `ranked_at_ms` 를 함께 넣는다. **멱등·upsert 규약 유지**
4. **`loops.py`**: 1763 근처의 *"참고값으로만 쓴다"* 주석을 현실에 맞게 고친다
5. **텔레메트리**(권장): 창 게이지로 `rank_age_ms` 같은 것을 한 줄 낼 수 있으면 낸다.
   ⚠ **`counter_scope` 규약을 지켜라** — 창 값이면 `window:` 절에 넣는다(`docs/56` §9 의 교훈)

## 2. ★ 반드시 확인할 것

- **`ALTER TABLE ADD COLUMN` 이 7.3 GB DB 에서 얼마나 걸리나.** SQLite 에서 NULL 허용
  컬럼 추가는 O(1) 로 알려져 있지만 **가정하지 말고 재라** — 사본이나 같은 크기 더미로.
  오래 걸리면 배포 창 계획이 달라진다
- **기존 행이 안 깨지나** — 읽는 쪽(`reader.py`, 분석 모듈들)이 새 컬럼에 안 놀라는지
- **`ranked_at_ms` 가 실제로 채워지나** — mock 서버나 저장된 응답으로 왕복 시험
- **NULL 이 올 때** — `_opt_ms` 가 `None` 을 낼 수 있다. 그 경로가 죽지 않는지

## 3. 절대 금지

- **배포하지 마라.** 라이브 워크트리(`w5-ops`)·`config/config.yaml`·워치독 무접촉.
  이 과제는 **코드와 마이그레이션까지**다
- **`config_sig` 를 바꾸지 마라.** 바꾸면 확증 팔이 갈린다
- **수집 밀도·폭·주기·티어 상한·승격 정책을 건드리지 마라.** 이번 창의 변경은 **저장 하나**다
- **라이브 API 호출 금지** · DB `mode=ro`(쓰기 시험은 사본으로)
- **홀드아웃 2026-05-01 ~ 07-29 열람 금지**
- 새 문서 `docs/77` · **콘솔 비 ASCII 금지**(cp949)
- **문서는 한국어. 커밋은 제목 + 한국어 본문** — `git log` 를 읽고 따라라

## 4. 환경

```
워크트리  C:\Users\dongh\orca\workspaces\toss_trade\w4-collector  (브랜치 feat/ranked-at)
라이브 DB C:\Users\dongh\orca\workspaces\toss_trade\w5-ops\data\tossmon.db  (읽기 전용·사본으로 시험)
```

## 5. 먼저 읽을 것

1. **`docs/75_g1a.md`** — 왜 이게 필요한가. **여기부터**
2. `docs/35_ranking_cadence.md` §7 — `rankedAt` 으로 무엇을 재는가
3. `tossmon/collector/loops.py` `config_signature()` (985 근처) — 지문의 계약
4. `tossmon/store/migrations.py` · `schema.sql` — 기존 마이그레이션 형식
5. `docs/04_contracts.md` C-1 · C-2 · C-6 — 시간은 UTC epoch ms 정수, 쓰기는 collector 단일 프로세스
6. `docs/56_limit_clamp_visibility.md` §9 — 텔레메트리 스코프를 잘못 선언한 사례

## 6. 게이트

`pytest tests/ -q` 전체 통과. **마이그레이션 왕복 테스트**(적용 전/후 읽기)와
**`ranked_at_ms` 가 실제로 저장되는 테스트**를 붙이고 **돌연변이로 red 확인.**

## 7. 태도

- 이 변경은 작아야 한다. **작지 않으면 뭔가 잘못 잡은 것이다** — 멈추고 보고해라
- 근거 없는 성능 주장 금지. `ALTER TABLE` 소요는 **재서** 적어라
- 실행 안 했으면 **"실행하지 않았음"**

## 8. 끝내는 법

`feat/ranked-at` 에 커밋하고 PR. **보고에 반드시**: `config_sig` 불변 확인 근거,
`ALTER TABLE` 실측 소요, 기존 행·읽는 쪽이 안 깨진다는 근거, 그리고 **배포 시 주의할 것.**
