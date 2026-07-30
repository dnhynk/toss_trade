"""TokenManager — 단일 토큰 보장 (계약 C-4).

client당 유효 토큰이 1개뿐이므로(재발급 시 기존 토큰 즉시 무효화),
OS 파일락(filelock) + 상태파일로 전 시스템에서 발급 주체를 1개로 강제한다.
live=False 면 실발급 시도 자체가 RuntimeError (mock 고정 토큰 사용).
"""
from __future__ import annotations

from pathlib import Path


class TokenManager:
    def __init__(self, keys_path: Path, state_path: Path, live: bool):
        self.keys_path = keys_path
        self.state_path = state_path
        self.live = live

    async def get(self) -> str:
        """유효 토큰 반환. 만료 60초 전 선제 재발급. 파일락 실패 시 즉시 예외(발급 강행 금지)."""
        raise NotImplementedError

    async def invalidate(self) -> None:
        """AuthExpired 수신 시 호출. 다음 get()이 재발급."""
        raise NotImplementedError
