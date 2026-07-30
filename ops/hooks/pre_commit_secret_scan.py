"""pre-commit 시크릿 스캔 훅 — 소유: W5 (docs/09_secret_hygiene.md 참고).

스테이징된 파일에서 `api_keys` 자체, 토큰 상태파일, 그리고 client_id/secret/
access_token/Bearer 토큰처럼 보이는 문자열을 커밋 전에 차단한다.

설치 (택 1):
  1) 리포 전역: `git config core.hooksPath ops/hooks` (Windows Git Bash 포함 동작).
     주의: worktree 간 `.git`이 공유되면 이 설정도 공유된다 — 코디네이터와 상의 후 실행할 것.
  2) 개별 worktree: `ops/hooks/pre-commit` 을 해당 worktree의 `.git/hooks/pre-commit` 으로 복사.

이 파일은 git 없이도(`find_secrets`) 단위 테스트 가능하도록 스캔 로직과 git 연동을 분리했다.
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

# 파일명 자체가 시크릿인 것들 — 내용과 무관하게 스테이징만으로 차단.
BLOCKED_FILENAMES = re.compile(
    r"(^|/)(api_keys|token_state\.json|.*token_state.*\.json|\.env(\..+)?|.*\.pem|.*\.key)$"
)

# 내용 기반 패턴. 그룹 1이 있으면 매칭 스니펫에서 실제 시크릿값 위치를 가리는 데 쓴다.
SECRET_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("client_secret 대입", re.compile(r"client[_-]?secret['\"]?\s*[:=]\s*['\"]?([A-Za-z0-9_\-]{12,})", re.IGNORECASE)),
    ("access_token 대입", re.compile(r"access[_-]?token['\"]?\s*[:=]\s*['\"]?([A-Za-z0-9_\-.]{16,})", re.IGNORECASE)),
    ("Bearer 토큰", re.compile(r"Bearer\s+([A-Za-z0-9_\-.]{16,})")),
    ("CLIENT_SECRET dotenv", re.compile(r"^\s*CLIENT_SECRET\s*=\s*(\S{8,})", re.MULTILINE)),
    ("CLIENT_ID dotenv", re.compile(r"^\s*CLIENT_ID\s*=\s*(\S{6,})", re.MULTILINE)),
    ("AWS access key", re.compile(r"\b(AKIA[0-9A-Z]{16})\b")),
]

# 스캐너 자신의 예시/패턴/테스트 픽스처는 오검출 대상에서 제외 — 실제 값이 아니라 패턴 문자열이므로.
EXEMPT_PATH_PARTS = ("ops/hooks/pre_commit_secret_scan.py",)


def is_blocked_filename(path: str) -> bool:
    norm = path.replace("\\", "/")
    return bool(BLOCKED_FILENAMES.search(norm))


def find_secrets(text: str, path: str = "") -> list[str]:
    """text 안에서 시크릿으로 보이는 패턴을 찾아 사람이 읽을 설명 목록으로 반환."""
    norm = path.replace("\\", "/")
    if any(norm.endswith(p) for p in EXEMPT_PATH_PARTS):
        return []
    findings: list[str] = []
    for label, pattern in SECRET_PATTERNS:
        for m in pattern.finditer(text):
            findings.append(f"{label} 의심 문자열 발견 (길이 {len(m.group(0))})")
    return findings


def _staged_files() -> list[str]:
    out = subprocess.run(
        ["git", "diff", "--cached", "--name-only", "--diff-filter=ACM"],
        capture_output=True, text=True, check=True,
    )
    return [line.strip() for line in out.stdout.splitlines() if line.strip()]


def _staged_content(path: str) -> str | None:
    result = subprocess.run(
        ["git", "show", f":{path}"], capture_output=True, text=True,
    )
    if result.returncode != 0:
        return None
    return result.stdout


def scan_staged() -> dict[str, list[str]]:
    """path -> 위반 사유 목록. 비어있으면 통과."""
    violations: dict[str, list[str]] = {}
    for path in _staged_files():
        if is_blocked_filename(path):
            violations.setdefault(path, []).append("차단된 파일명(시크릿 파일로 간주)")
            continue
        content = _staged_content(path)
        if content is None:
            continue  # 바이너리 등 diff 불가 — 스킵(파일명 차단 규칙이 주 방어선)
        hits = find_secrets(content, path)
        if hits:
            violations[path] = hits
    return violations


def main(argv: list[str] | None = None) -> int:
    violations = scan_staged()
    if not violations:
        return 0
    print("커밋 차단: 시크릿으로 의심되는 내용이 스테이징되어 있습니다.", file=sys.stderr)
    for path, reasons in violations.items():
        print(f"  - {path}:", file=sys.stderr)
        for r in reasons:
            print(f"      {r}", file=sys.stderr)
    print(
        "\napi_keys/token_state.json은 절대 커밋하지 않습니다. "
        "실제 값이 아니라 오검출이면 ops/hooks/pre_commit_secret_scan.py의 "
        "SECRET_PATTERNS를 조정하고 이유를 docs/09_secret_hygiene.md에 남기세요.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
