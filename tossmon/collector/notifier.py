"""알림 — 소유: W4. Phase 1 은 콘솔+파일 로그. 텔레그램은 인터페이스만."""
from __future__ import annotations


class Notifier:
    def info(self, msg: str) -> None:
        raise NotImplementedError

    def alert(self, msg: str) -> None:
        """치명 상황 (Forbidden, 예산 초과, 수집 정지)."""
        raise NotImplementedError
