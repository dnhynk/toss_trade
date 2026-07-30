"""TokenManager 단일성 테스트 — 계약 C-4.

이 API 는 client 당 유효 토큰이 1개뿐이라 누가 재발급하면 기존 토큰이 즉시 죽는다.
따라서 "두 번째 프로세스는 발급을 강행하지 않고 실패해야 한다" 가 안전 요건이다.
"""
from __future__ import annotations

import json

import pytest

from test_api_support import mock_server  # noqa: F401
from tossmon.api.tokens import MOCK_TOKEN, REFRESH_MARGIN_MS, TokenManager


def _keys(tmp_path, body: str = "CLIENT_ID=test-id\nCLIENT_SECRET=test-secret\n"):
    p = tmp_path / "api_keys"
    p.write_text(body, encoding="utf-8")
    return p


# ---- mock 모드 -----------------------------------------------------------


async def test_mock_mode_returns_fixed_token_without_issuing(tmp_path):
    tm = TokenManager(tmp_path / "absent_keys", tmp_path / "state.json", live=False)
    assert await tm.get() == MOCK_TOKEN
    assert not (tmp_path / "state.json").exists(), "mock 모드가 상태파일을 만들면 안 된다"
    assert not (tmp_path / "state.json.lock").exists(), "mock 모드는 리스를 잡지 않는다"


async def test_mock_mode_refuses_real_issuance(tmp_path):
    """live=False 면 실발급 시도 자체가 RuntimeError (계약 C-4)."""
    tm = TokenManager(_keys(tmp_path), tmp_path / "state.json", live=False)
    with pytest.raises(RuntimeError, match="refuses to issue"):
        await tm._issue()


async def test_mock_mode_allows_many_concurrent_managers(tmp_path):
    """리스 없는 워커 여러 개가 동시에 mock 을 써도 서로 막지 않아야 한다."""
    managers = [TokenManager(tmp_path / "k", tmp_path / "state.json", live=False)
                for _ in range(5)]
    assert [await m.get() for m in managers] == [MOCK_TOKEN] * 5


# ---- 라이브 모드: 단일성 -------------------------------------------------


async def test_second_manager_fails_instead_of_reissuing(mock_server, tmp_path, monkeypatch):  # noqa: F811
    """핵심 안전 요건: 리스를 이미 누가 들고 있으면 발급을 강행하지 않고 즉시 실패한다."""
    monkeypatch.setenv("TOSS_BASE_URL", mock_server.url)
    keys, state = _keys(tmp_path), tmp_path / "state.json"

    first = TokenManager(keys, state, live=True)
    second = TokenManager(keys, state, live=True)
    try:
        assert await first.get() == "mock-access-token"

        with pytest.raises(RuntimeError, match="lease already held"):
            await second.get()

        # 실패한 쪽이 상태파일을 건드리지 않았는지 (= 첫 토큰이 살아있는지) 확인
        saved = json.loads(state.read_text(encoding="utf-8"))
        assert saved["token"] == "mock-access-token"
    finally:
        first.release()
        second.release()


async def test_lease_is_reusable_after_release(mock_server, tmp_path, monkeypatch):  # noqa: F811
    monkeypatch.setenv("TOSS_BASE_URL", mock_server.url)
    keys, state = _keys(tmp_path), tmp_path / "state.json"

    first = TokenManager(keys, state, live=True)
    await first.get()
    first.release()

    second = TokenManager(keys, state, live=True)
    try:
        assert await second.get() == "mock-access-token"
    finally:
        second.release()


async def test_valid_state_is_reused_not_reissued(mock_server, tmp_path, monkeypatch):  # noqa: F811
    """이미 유효한 토큰이 상태파일에 있으면 재발급하지 않는다 (재발급 = 기존 토큰 사망)."""
    monkeypatch.setenv("TOSS_BASE_URL", mock_server.url)
    keys, state = _keys(tmp_path), tmp_path / "state.json"
    state.write_text(json.dumps({"token": "already-alive",
                                 "expires_at_ms": 10**15}), encoding="utf-8")

    tm = TokenManager(keys, state, live=True)
    try:
        async def explode():
            raise AssertionError("재발급을 시도했다 — 기존 토큰을 죽이는 동작")
        tm._issue = explode                       # type: ignore[method-assign]
        assert await tm.get() == "already-alive"
    finally:
        tm.release()


async def test_expiring_token_is_refreshed_early(mock_server, tmp_path, monkeypatch):  # noqa: F811
    """만료 60초 전이면 선제 재발급 (계약 C-4)."""
    monkeypatch.setenv("TOSS_BASE_URL", mock_server.url)
    keys, state = _keys(tmp_path), tmp_path / "state.json"
    almost = TokenManager._now_ms() + REFRESH_MARGIN_MS - 5_000   # 55초 남음
    state.write_text(json.dumps({"token": "about-to-die", "expires_at_ms": almost}),
                     encoding="utf-8")

    tm = TokenManager(keys, state, live=True)
    try:
        assert await tm.get() == "mock-access-token", "만료 임박 토큰을 그대로 재사용했다"
    finally:
        tm.release()


async def test_invalidate_forces_reissue(mock_server, tmp_path, monkeypatch):  # noqa: F811
    monkeypatch.setenv("TOSS_BASE_URL", mock_server.url)
    tm = TokenManager(_keys(tmp_path), tmp_path / "state.json", live=True)
    try:
        assert await tm.get() == "mock-access-token"
        await tm.invalidate()
        assert tm._token is None
        assert not (tmp_path / "state.json").exists()
        assert await tm.get() == "mock-access-token"
    finally:
        tm.release()


# ---- keys 파싱 ----------------------------------------------------------


@pytest.mark.parametrize("body", [
    "CLIENT_ID=abc\nCLIENT_SECRET=def\n",
    "# comment\n\nclient_id = abc\nclient_secret = def\n",
    'CLIENT_ID="abc"\nCLIENT_SECRET=\'def\'\n',
    '{"client_id": "abc", "client_secret": "def"}',
])
def test_keys_parsing_formats(tmp_path, body):
    tm = TokenManager(_keys(tmp_path, body), tmp_path / "state.json", live=True)
    assert tm._read_keys() == ("abc", "def")


def test_keys_error_does_not_leak_secret(tmp_path):
    """오류 메시지에 시크릿 값이 실리면 안 된다 (계약 C-11 §5)."""
    tm = TokenManager(_keys(tmp_path, "WRONG_KEY=supersecretvalue\n"),
                      tmp_path / "state.json", live=True)
    with pytest.raises(RuntimeError) as ei:
        tm._read_keys()
    assert "supersecretvalue" not in str(ei.value)


async def test_live_requires_base_url(tmp_path, monkeypatch):
    monkeypatch.delenv("TOSS_BASE_URL", raising=False)
    tm = TokenManager(_keys(tmp_path), tmp_path / "state.json", live=True)
    try:
        with pytest.raises(RuntimeError, match="TOSS_BASE_URL"):
            await tm.get()
    finally:
        tm.release()
