# W3 — 판정 러너가 **회전된 로그를 못 본다**. 그래서 유효한 창이 무효로 나왔다

> 작성: 2026-08-18 일일 코디네이터. **진단은 끝났다. 아래는 재현 절차와 고칠 자리다.**
> **판정하지 마라** — D-21 판정은 이미 났다(아래 §0). 너는 러너가 그 파일을 보게만 만든다.

## 0. 무슨 일이 있었나 (사실만)

오늘 아침 `--session 2026-08-17` 판정이 **`exit 4` (INVALID)** 로 끝났다. 이유는
`3-4.2 config_sig distinct=0` — *"창 안에 telemetry 가 한 줄도 없다"*.

**그런데 데이터는 멀쩡했다.** 창 안 telemetry 는 **77 줄**이고 `config_sig` 는
**정확히 1 종**이다. 다만 그 줄들이 `collector.log` 가 아니라 **`collector.log.1`** 에 있었다.

```
2026-08-18 06:44   collector.log 가 32 MiB 에 닿아 collector.log.1 로 회전됐다
                   (창은 08-17 22:30 ~ 08-18 05:00 KST 이므로 통째로 .1 에 들어갔다)
```

코디네이터가 `--log <...>/collector.log.1` 로 다시 돌리니 **`exit 0`, 판정이 나왔다.**
그 판정이 정본이다. **다시 판정하지 마라.**

## 1. 원인 — `log_sources()` 가 **일어나지 않는 회전만** 안다

`tools/d21_verdict.py:215-242` 의 `log_sources()` 는 이 패턴만 찾는다:

```python
pat = re.compile(r"^" + re.escape(log.stem) + r"\.(\d{8}-\d{6})" + re.escape(log.suffix) + r"\.gz$")
```

즉 **`ops/rotate_logs.py` 방식**(`collector.20260818-064409.log.gz`)만 본다.

**그 회전은 이 프로젝트에서 한 번도 성공한 적이 없다** (`COORDINATOR-STATE` §2-3:
Windows 에서 수집기가 파일을 잡고 있어 `rename` 이 `PermissionError` 로 죽는다.
성공한 단 한 번은 프로세스가 죽어 있던 2026-08-04 다).

**실제로 일어나는 회전은 수집기 자신의 것이다** —
`tossmon/collector/notifier.py:16-17,66-67`:

```python
MAX_BYTES = 32 * 1024 * 1024
BACKUP_COUNT = 3
fh = RotatingFileHandler(self.log_path, maxBytes=MAX_BYTES, backupCount=BACKUP_COUNT, ...)
```

→ 산출물은 **`collector.log.1` · `.2` · `.3`** 이고 `.gz` 도 타임스탬프도 없다.
**러너는 이 이름을 모른다.** 그래서 창이 회전 경계를 넘으면 조용히 `distinct=0` 이 된다.

> **이것이 이 프로젝트가 네 번째로 겪는 같은 모양이다** — 계측기가 자기 자신에 대해
> 거짓말했다. 러너는 *"config 가 바뀌었다"* 가 아니라 *"못 보겠다"* 를 말했는데,
> 종료코드는 둘을 구분하지 않는다.

## 2. 네가 할 일

**워크트리**: `C:/Users/dongh/orca/workspaces/toss_trade/w3-analyzer` · 브랜치 `feat/analyzer`
**다른 워크트리를 건드리지 마라.** `w5-ops` 는 라이브다 — **읽기만** 해라.
**소유 범위**: `tools/d21_verdict.py` 와 그 테스트만. `tossmon/**` 손대지 마라.

1. `git fetch origin && git merge --ff-only origin/main`
2. `log_sources()` 가 **번호 백업도** 시간순으로 포함하게 고쳐라.
   - `collector.log.1` 이 `collector.log.2` 보다 **새 것**이다 (RotatingFileHandler 는
     회전할 때마다 번호를 밀어올린다). 정렬을 뒤집어 먹지 마라.
   - **`collector.stdout.log` 계열이 섞이면 안 된다.** 지금 코드가 `stem` 을 강제해
     그것을 막고 있다 — 번호 백업에도 같은 강제를 유지해라. 같은 디렉터리에
     `collector.stdout.log` 와 `collector.stdout.20260804-001003.log.gz` 가 실재한다.
   - 기존 `.gz` 경로를 **없애지 마라.** 둘 다 지원해야 한다.
   - `.gz` 는 스탬프로 창 밖을 건너뛸 수 있지만 **번호 백업은 이름에 시각이 없다.**
     건너뛰기를 흉내내지 말고 그냥 읽어라(파일 3 개, 비용 무시 가능). 억지로
     mtime 으로 거르면 회전 뒤 mtime 이 바뀌는 환경에서 또 조용히 틀린다.
3. **회귀 테스트를 새로 넣어라**: 임시 디렉터리에 `collector.log`,
   `collector.log.1`, `collector.log.2`, `collector.stdout.log`,
   `collector.<stamp>.log.gz` 를 만들고 `log_sources()` 가
   **무엇을 어떤 순서로** 돌려주는지 못박아라. `stdout` 계열이 안 섞이는 것도 함께.

## 3. 게이트 — 돌리고 **출력을 붙여넣어라**

```bash
cd C:/Users/dongh/orca/workspaces/toss_trade/w3-analyzer
.venv/Scripts/python.exe -m pytest -q                 # 전체. 기준선 2,202 passed
.venv/Scripts/python.exe tools/d21_verdict.py --session 2026-08-17     # --log 없이!
```

**합격 조건 — 이 네 수치가 그대로 나와야 한다** (코디네이터가 `--log` 로 이미 얻은 값):

| | 값 |
|---|---|
| 종료코드 | **0** |
| `config_sig` | **distinct=1, telemetry lines=77** |
| B top-10 | **29.1%** (INCREASED) |
| A top-10 · cost | **21.8%** · **106** |

**하나라도 다르면 고치지 말고 멈추고 보고해라.** 숫자를 맞추러 가지 마라 —
다르면 그 자체가 새 사실이다.

## 4. 병합 뒤 내가 grep 할 것

`tools/d21_verdict.py` 에 아래 두 가지가 있어야 한다.
- 번호 백업을 찾는 코드 (예: `\.\d+$` 를 다루는 자리)
- 주석 한 줄: **왜** 두 방식을 다 봐야 하는지 (`rotate_logs` 는 거의 실패하고
  실제 회전은 `RotatingFileHandler` 다)

## 5. 하지 말 것

- **판정하지 마라.** D-21 결론을 다시 쓰거나 해석하지 마라.
- **`coordination/D21-COVERAGE-PREREG.md` 를 건드리지 마라.** 사전등록 개정은 사용자 단독이다.
- 기대값 상수·자가검사 표를 만지지 마라. 그게 러너의 안전장치다.
- 요청 안 한 리팩터링 금지.

## 6. 보고

한 문단으로: 무엇을 고쳤나 / 게이트 출력(위 네 수치 포함) / **안 돌린 것이 있으면
"실행하지 않았음"** 이라고 명시. 결론 대신 **측정 조건**을 붙여라.
끝나면 PR 을 올리고 `worker_done` 을 보내라.
