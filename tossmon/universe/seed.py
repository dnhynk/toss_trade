"""외부 심볼 디렉토리 수집 — 소유: W2.

NASDAQ Trader symbol directory 등 공개 소스에서 US 상장 심볼 전체를 수집·파싱한다.
(토스 API에는 전 종목 리스트 엔드포인트가 없음 — docs/01 §4-4)
"""
from __future__ import annotations

from pathlib import Path


def fetch_symbol_directory(cache_dir: Path) -> list[str]:
    """심볼 리스트 반환. 네트워크 실패 시 캐시 폴백."""
    raise NotImplementedError


def parse_directory_file(path: Path) -> list[str]:
    """파일 포맷 파싱 (테스트 심볼·ETF 플래그 등 이상 케이스 처리)."""
    raise NotImplementedError
