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


class ForbiddenEndpoint(Exception):
    """allowlist 위반. 코드 버그 — 절대 잡지 말 것.

    ⚠️ **의도적으로 `TossApiError` 를 상속하지 않는다** (감사 H-5).

    계약 C-5 표는 이 예외를 "처리 주체: 없음 / 코드 버그. 잡지 말 것" 으로 규정한다.
    그런데 `TossApiError` 를 상속하면 컬렉터의 `except (TossApiError, OSError)` 광역 핸들러에
    걸려 **주문 계열 엔드포인트에 도달했다는 사실이 warn 로그 한 줄로 끝나고 수집이 계속된다.**
    Phase 2 에 주문 코드가 들어오면 이것이 마지막 방어선이므로, "잡지 말 것" 이라는 규약을
    주석이 아니라 **타입 체계가 강제**하게 한다 — 상위 `except TossApiError` 는 이 예외를
    잡지 못하고 그대로 위로 터진다.

    이 예외를 넓은 `except Exception` 으로 삼키는 코드를 새로 만들지 말 것.
    """
