# 05 — 오케스트레이션 계획 (Phase 1)

> 원본 지시서: `ORCHESTRATION_PROMPT.md`. 이 문서는 배치표·DAG·리스 정책의 기록이다.

## 워커 배치표 (동시 실행 최대 3)

| ID | 브랜치 | 역할 | 에이전트/모델/effort | 소유 경로 (이 밖은 수정 금지) |
|---|---|---|---|---|
| W1 | `feat/core-api` | API 코어 + 라이브 실측 + 픽스처/목서버 | claude / opus / xhigh | `tossmon/api/**`, `tools/live_probe.py`, `tools/mock_server.py`, `tests/fixtures/**`, `docs/06_live_facts.md`, `tests/test_api_*.py` |
| W2 | `feat/universe-store` | 유니버스 빌더 + 스토리지 | codex (reasoning high) → 실패 시 claude/sonnet/high | `tossmon/store/**`, `tossmon/universe/**`, `tests/test_store*.py`, `tests/test_universe*.py` |
| W3 | `feat/analyzer` | 라벨링·피처·평가 + 합성데이터 | claude / opus / high | `tossmon/analysis/**`, `tools/report.py`, `tests/synth.py`, `tests/test_analysis*.py`, `docs/07_analysis_spec.md` |
| W4 | `feat/collector` | 티어드 수집 루프 + 검출기 (통합) | claude / opus / xhigh | `tossmon/collector/**`, `tossmon/config.py`, `tests/test_collector*.py` |
| W5 | `feat/ops` | 무인 운영·관측·라이브 리허설 | claude / sonnet / high | `ops/**`, `docs/08_runbook.md`, `docs/09_secret_hygiene.md`, `tools/dryrun_night.py` |
| W6 | (읽기전용) | 적대적 감사 | claude / opus / xhigh | `docs/10_audit.md`만 작성 |

## DAG

```
Wave 0 (coordinator)  계약+스켈레톤+초기 커밋 ─┬─> W1 ─┐
                                               ├─> W2 ─┼─> 머지 ─> Wave 2: W4, W5 ─> Wave 3: W6 ─> 패치
                                               └─> W3 ─┘
```

- W1: mock 서버+픽스처 최우선 완성 → 즉시 코디네이터 통지 → W2·W3에 브로드캐스트.
- W2·W3는 W1을 기다리지 않는다 (계약+01 문서 사양으로 스텁 개발, 픽스처 도착 후 재검증).
- W4는 W1·W2·W3 머지 후. W5는 W4와 동시 시작하되 **라이브 리허설 실행은 W4 머지 + 사용자 승인 후**.

## 라이브 리스 정책

- client당 유효 토큰 1개 → **라이브 리스 보유 워커 1개만** `api_keys` 읽기·토큰 발급·실서버 호출 가능.
- 리스 순서: Wave 1 **W1 단독** → (회수) → Wave 2 리허설 **W5 단독**.
- 리스 없는 워커: `TOSS_LIVE=0`, `TOSS_BASE_URL=http://127.0.0.1:8899` 고정.
- 코디네이터 자신도 라이브 호출 금지 (리스는 항상 정확히 한 곳).

## 머지 게이트 (매 브랜치, 코디네이터 직접 수행)

1. `pytest` 전체 통과 — 코디네이터가 재실행
2. `git diff --stat main...<branch>` 소유권 밖 변경 0건
3. `docs/04_contracts.md` 시그니처 준수
4. 금지사항 스캔 (주문 엔드포인트 문자열 / api_keys 참조 / 시크릿 / 라이브 URL 하드코딩)
5. 머지 후 엔드투엔드 스모크 (mock → collector 가속 리플레이 → DB → analyzer 리포트)

머지: main에서 `git merge --no-ff feat/<x>`. 충돌 시 워커에게 `git rebase main` 지시.

## 모델/effort 지정 방식 (§0-1 실측 결과)

`orca worktree create --agent claude`는 모델/effort 플래그 미지원 →
**2단계**: `orca worktree create --name <n> --json`(에이전트 없이) 후
`orca terminal create --worktree id:<fullId> --command "claude --dangerously-skip-permissions --model <m> --effort <e>" --json`
→ `terminal wait --for tui-idle` → `orchestration dispatch --task <id> --to <handle> --inject`.
Codex(W2)는 `--command 'codex -c model_reasoning_effort="high"'`.
