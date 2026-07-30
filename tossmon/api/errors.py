"""에러 분류 체계 — 계약 C-5. 재시도 책임 표는 docs/04_contracts.md 참조."""
from __future__ import annotations


class TossApiError(Exception):
    """모든 토스 API 오류의 베이스."""


class RateLimited(TossApiError):
    """429. client 내부에서 Retry-After 대기 후 1회 재시도."""

    def __init__(self, retry_after_s: float, message: str = ""):
        super().__init__(message or f"rate limited, retry after {retry_after_s}s")
        self.retry_after_s = retry_after_s


class AuthExpired(TossApiError):
    """401. client 내부에서 tokens.invalidate() 후 1회 재시도."""


class TransientHTTP(TossApiError):
    """5xx/타임아웃/연결오류. client 내부 지수 백오프 최대 3회."""

    def __init__(self, status: int, message: str = ""):
        super().__init__(message or f"transient http {status}")
        self.status = status


class SchemaMismatch(TossApiError):
    """응답 파싱 불가/필수필드 결손. 재시도 금지 — caller가 로그+스킵."""

    def __init__(self, detail: str):
        super().__init__(detail)
        self.detail = detail


class Forbidden(TossApiError):
    """403 (IP 미등록/권한). 치명 — 수집 중단 + 경보."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class ForbiddenEndpoint(TossApiError):
    """allowlist 위반. 코드 버그 — 절대 잡지 말 것."""
