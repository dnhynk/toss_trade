"""W1 API 테스트 공용 헬퍼 — 테스트 아님 (수집되는 test_* 함수 없음).

conftest.py 는 W1 소유 경로가 아니므로(배치표: `tests/test_api_*.py`) 공용 픽스처를
이 모듈에 두고 각 테스트 모듈이 이름을 import 해 쓴다:

    from test_api_support import mock_server, client   # noqa: F401
"""
from __future__ import annotations

import asyncio
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.mock_server import build_server            # noqa: E402
from tossmon.api.client import TossClient             # noqa: E402
from tossmon.api.limiter import GroupRateLimiter      # noqa: E402
from tossmon.api.tokens import TokenManager           # noqa: E402

LIMITS = {"AUTH": 5, "STOCK": 5, "MARKET_DATA": 10,
          "MARKET_DATA_CHART": 5, "RANKING": 5, "MARKET_INFO": 3}


def mock_args(**over) -> SimpleNamespace:
    base = dict(strict=False, inject=None, inject_every=1, inject_latency_s=0.2,
                enforce_limits=False, verbose=False)
    base.update(over)
    return SimpleNamespace(**base)


class MockServer:
    """테스트용 mock 서버 (ephemeral 포트, 데몬 스레드)."""

    def __init__(self, **over) -> None:
        self.httpd = build_server(0, mock_args(**over))
        self.port = self.httpd.server_address[1]
        self.url = f"http://127.0.0.1:{self.port}"
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def close(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=5)


@pytest.fixture(scope="session")
def mock_server():
    srv = MockServer()
    try:
        yield srv
    finally:
        srv.close()


def make_client(base_url: str, tmp_path: Path, *, live: bool = False,
                usage_ratio: float = 0.7, timeout_s: float = 5.0) -> TossClient:
    tokens = TokenManager(keys_path=tmp_path / "api_keys",
                          state_path=tmp_path / "token_state.json", live=live)
    return TossClient(base_url, tokens, GroupRateLimiter(LIMITS, usage_ratio), timeout_s)


@pytest.fixture
async def client(mock_server, tmp_path):
    c = make_client(mock_server.url, tmp_path)
    try:
        yield c
    finally:
        await c.aclose()


def run(coro):
    """동기 테스트에서 코루틴 한 번 돌리기."""
    return asyncio.run(coro)
