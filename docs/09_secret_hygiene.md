# 09 — 시크릿 위생 (Secret Hygiene)

> **[현행]** 소유 W5 · 2026-07-30 · 시크릿 정책
> 상태 표기의 뜻과 전수 목록: [`docs/INDEX.md`](INDEX.md)

> 소유: W5. 스캐너 구현은 `ops/hooks/pre_commit_secret_scan.py`.

## 1. 시크릿 목록 (이 리포에서 절대 커밋되면 안 되는 것)

| 대상 | 위치 | 비고 |
|---|---|---|
| `api_keys` | 리포 루트 (경로는 `config/config.yaml` 의 `api.keys_path`) | client_id/secret. `.gitignore` 이미 적용됨 |
| `data/token_state.json` | `TokenManager.state_path` | 발급된 access_token + 만료시각. `.gitignore` 의 `data/` 규칙으로 이미 제외됨 |
| `.env`, `.env.*` | 어디든 | 관례적으로 시크릿을 담는 파일 형식 — 존재 자체를 차단 |
| `*.pem`, `*.key` | 어디든 | 인증서/개인키 형식 파일명 자체를 차단 |
| 코드/문서 내 실제 client_secret·access_token 리터럴 | 어디든 | 테스트 픽스처에도 절대 넣지 않는다(§3) |

## 2. 방어선 2단계

1. **`.gitignore`** (기존, W5 관리 아님) — `api_keys`, `config/config.yaml`, `data/token_state.json`,
   `data/` 전체, `*.log` 를 이미 제외한다. 이것이 1차 방어선이지만 `git add -f` 로 우회 가능하다.
2. **pre-commit 훅** (`ops/hooks/pre_commit_secret_scan.py`, 이번에 추가) — 스테이징된 파일을
   커밋 직전에 검사해 다음을 차단한다:
   - 차단 파일명: `api_keys`, `*token_state*.json`, `.env*`, `*.pem`, `*.key`
   - 차단 내용 패턴: `client_secret=`/`access_token=`/`Bearer <token>`/dotenv 형태의
     `CLIENT_ID=`·`CLIENT_SECRET=`/AWS 스타일 액세스 키(`AKIA...`)
   - 매칭되면 커밋을 **거부**(exit 1)하고 어느 파일의 어떤 패턴인지 stderr에 출력한다.
     (값 자체는 출력하지 않는다 — 길이 정보만.)

### 설치 방법 (택 1 — 이 스크립트는 자동으로 설치하지 않는다)

```powershell
# A) 리포 전역 (주의: git worktree 간 .git 설정이 공유되면 다른 워커의 worktree에도 적용된다.
#    여러 worktree로 작업 중이면 코디네이터와 상의 후 적용할 것)
git config core.hooksPath ops/hooks

# B) 이 worktree만 (worktree별 .git/hooks 는 보통 독립적이다)
copy ops\hooks\pre-commit .git\hooks\pre-commit
copy ops\hooks\pre_commit_secret_scan.py .git\hooks\pre_commit_secret_scan.py
```

Windows에서 `ops/hooks/pre-commit` 은 `#!/bin/sh` 셔뱅으로 Git Bash 를 통해 실행된다(Git for
Windows 는 훅 실행에 항상 내장 sh를 쓰므로 별도 설정 없이 동작한다).

## 3. 테스트 픽스처·문서 예시 작성 규칙

- `tests/fixtures/live/*.json`(W1 소유)은 이미 `MASK_FIELDS`/`MASK_HEADERS` 로 계좌/토큰류를
  마스킹한다(`tools/live_probe.py` 의 `mask()`) — 이 규약을 다른 워커도 새 픽스처에 유지할 것.
- 이 문서·`ops/hooks/pre_commit_secret_scan.py` 의 테스트(`tests/test_ops_secret_scan.py`)에
  쓰는 예시 값은 전부 **명백히 가짜인 패턴**(`FAKE...`, `fake...`)만 사용한다 — 계약 C-11 §5
  ("시크릿 스캔 훅을 만들면서 실제 시크릿을 테스트 픽스처에 넣지 마라") 준수.
- 스크린샷/로그 붙여넣기를 문서에 포함할 때도 `requestId`/`Authorization` 헤더/`access_token`
  값은 사람이 직접 마스킹한 뒤 붙여넣는다(자동 마스킹은 `tools/live_probe.py` 경로에만 있다).

## 4. 오탐(false positive) 대응

패턴이 정상 코드를 잘못 차단하면(예: 우연히 20자 넘는 식별자가 `access_token=` 형태로 보임),
`ops/hooks/pre_commit_secret_scan.py` 의 `SECRET_PATTERNS` 를 조정하고 **이 문서에 이유를 한 줄
추가**한다 — 패턴을 느슨하게 만드는 변경은 히스토리가 남아야 나중에 "왜 이렇게 헐거워졌는지"
추적 가능하다.

현재까지 조정 이력: (없음 — 최초 작성)

## 5. 사고 발생 시(시크릿이 이미 커밋된 경우)

이 훅은 **커밋 전** 방어선이다. 이미 커밋/푸시된 뒤 발견됐다면:

1. 즉시 코디네이터에 escalate(사람 판단 필요 — 히스토리 재작성은 워커 권한 밖).
2. 커밋 재작성(`git filter-repo` 등)은 **이 워커가 자체 판단으로 하지 않는다** — 브랜치 히스토리를
   바꾸는 되돌리기 어려운 작업이며, 다른 워커의 worktree에도 영향을 줄 수 있다(불변 규칙 §3·§6).
3. 노출된 시크릿(토큰/클라이언트 시크릿)은 **로컬 파일 삭제로 무효화되지 않는다** — 서버측
   재발급/폐기 절차가 필요하며 이는 토스증권 API 콘솔에서 사람이 해야 한다(docs/08 §11-7 참고).
