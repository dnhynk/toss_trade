# archive — 끝난 것들

> **여기 있는 것은 전부 "그날의 기록" 이다. 현재 상태로 인용하지 마라.**
> 현재는 `coordination/COORDINATOR-STATE.md`, 문서 목록은 `docs/INDEX.md` 다.
>
> 2026-08-13 에 `coordination/` 최상위에서 옮겼다. **내용은 한 글자도 안 고쳤다.**

## `HANDOFF-W1` ~ `HANDOFF-W7` (10 개)

**전부 2026-07-31 에, 일어나지 않은 리셋에 대비해 쓴 문서다.**
`HANDOFF-W6.md` 자신이 이렇게 적는다:

> *"예고됐던 런타임 리셋은 **일어나지 않았다** — 리셋이 아니라 코디네이터 세션 교체였다."*

그런데도 각 파일은 *"이 파일이 재개의 유일한 근거다"* 라고 선언하고 **2 주 전 HEAD 를
못 박아 두었다** (`4869293`, `1e9bcae`, `15022a2`, `363b7f4`, `ee6ecf9`, `2088982` …).
그 상태로 재개하면 2 주치를 덮어쓴다.

`HANDOFF-W4.md` 는 이미 없는 파일을 가리킨다 —
*"상세 보고서: `W4_REPORT_task_59b9b6e505d3.md`"* (2026-08-13 에 지웠다,
행선지는 `docs/INDEX.md` §지운 것).

**워커 재개 지점의 현행 정본은 `COORDINATOR-STATE.md` §4 워커 배치표다.**

## `READY-FOR-RESET.md`

**스스로 낡았다고 적어 둔 문서다** — *"이 파일은 2026-07-31 리셋 시점 기준이며 이후
나흘치가 반영돼 있지 않다."*

## `HANDOFF-20260813.md`

**이 문서 정리 작업을 지시한 인계 문서다.** 내용은 전부 흡수됐다:

| 그 문서의 절 | 지금 어디 |
|---|---|
| 소유표 | `COORDINATOR-STATE.md` §4 |
| 오염 지도 · (다) 4 건 | `docs/INDEX.md` §재발 감시 대상 |
| 번호 충돌 (`docs/44` D-17·18 vs 큐) | `COORDINATOR-STATE.md` §3 · `docs/INDEX.md` |
| 훑기가 자기 자신을 잡은 사례 · 교훈 둘 | `docs/INDEX.md` §규칙 · `docs/00` §5 · `README.md` |
| 동작 확인됨 | `COORDINATOR-STATE.md` §1 |
| 미확인 (워커 보고만) | `COORDINATOR-STATE.md` §1-2 |
| 알려진 깨진 것 | `COORDINATOR-STATE.md` §2 |
| 확정된 결정 · 기각한 대안 | `COORDINATOR-STATE.md` §3-1 |
| 불변조건 | `README.md` §절대 하면 안 되는 것 |
| 열린 질문 | `COORDINATOR-STATE.md` §3 |
| **다음 한 걸음 (08-13 밤 재측정)** | **`COORDINATOR-STATE.md` §1-1** |

## `specs/` (104 개)

**일회성 워커 명세다.** 발행된 태스크 하나에 파일 하나이고, 그 태스크가 끝나면
결과는 해당 `docs/NN` 문서로 갔다. 명세 자체는 *"무엇을 시켰나"* 의 기록으로만 남는다.

> **주의**: `specs/w5_detector_windows.md:186` 이 *"새 문서 `docs/51_detector_windows.md`
> (51 은 전 브랜치에서 비어 있음을 확인했다)"* 라고 적는다. 같은 시각 W3 도 같은 판단을
> 했고 **둘 다 옳았는데 조합이 틀렸다.** 그 파일은 2026-08-13 에 `docs/57_detector_windows.md`
> 로 옮겨졌다 — 경위는 `docs/INDEX.md` §번호 충돌 이력.
