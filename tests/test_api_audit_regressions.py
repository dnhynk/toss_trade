"""적대적 감사(W6 fable / W6b opus) 지적에 대한 회귀 테스트.

각 테스트는 **수정 전 코드에서 반드시 실패**하도록 썼다. 대응 항목:

| 테스트 그룹            | 감사 항목             | 무엇이 깨져 있었나 |
|------------------------|-----------------------|--------------------|
| 리스 (lease)           | F-1 / A-1 [치명]      | 락이 CWD 종속이라 워크트리마다 다른 락 → 둘 다 발급 → 상호 토큰 살해 |
| invalidate CAS         | H-1 / A-2 [높음]      | 무조건 폐기라 지연된 401 이 방금 발급된 유효 토큰을 죽임 |
| AUTH limiter           | H-1 / A-4             | 토큰 발급이 rate limiter 밖 |
| 버스트                 | H-3 / B-1 [높음]      | capacity == rate 라 유휴 후 첫 1초에 2×rate 통과 |
| 헤더 클램프            | H-2 / B-2 [높음]      | 서버 헤더가 rate 를 무제한 상향 (7.0 → 420 재현) |
| ForbiddenEndpoint 격리 | H-5 [높음]            | TossApiError 상속이라 광역 except 에 삼켜짐 |
| dot segment            | M-11 [중간]           | 1층 관문이 `..` 을 {symbol} 로 통과시킴 |
| 시크릿                 | M-1 [중간]            | 토큰 엔드포인트 error_description 이 평문 로그로 |
| live_probe 기본값      | B-5 / M-7             | live=True 하드코딩 + force429 가 기본 실행에 포함 |

⚠️ 이 파일의 어떤 테스트도 실서버를 건드리지 않는다. 라이브 발급 경로를 타는 테스트는
전부 mock 서버를 대상으로 하고, 리스 디렉터리는 `TOSSMON_LEASE_DIR` 로 tmp_path 에 가둔다.
"""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import httpx
import pytest

from tests.test_api_support import LIMITS, make_client, mock_server  # noqa: F401
from tossmon.api import endpoints, limiter as limiter_mod
from tossmon.api.errors import (
    AuthExpired,
    ForbiddenEndpoint,
    TossApiError,
)
from tossmon.api.limiter import GroupRateLimiter
from tossmon.api.tokens import TokenManager, lease_path_for_client

ROOT = Path(__file__).resolve().parent.parent

KEYS = "CLIENT_ID=audit-client\nCLIENT_SECRET=audit-secret\n"
OTHER_KEYS = "CLIENT_ID=different-client\nCLIENT_SECRET=audit-secret\n"


@pytest.fixture(autouse=True)
def _isolated_lease_dir(tmp_path, monkeypatch):
    """리스를 tmp_path 에 가둔다 — 실제 운영 리스(W5 가 보유 중)를 절대 건드리지 않도록."""
    monkeypatch.setenv("TOSSMON_LEASE_DIR", str(tmp_path / "leases"))
    yield


def _keys(tmp_path, name="api_keys", body=KEYS):
    p = tmp_path / name
    p.write_text(body, encoding="utf-8")
    return p


# ===================================================================== 리스
# 감사 F-1 / A-1 [치명] — BLOCKER-1


def test_lease_path_ignores_state_path_and_cwd(tmp_path):
    """핵심 회귀: 상태파일 경로가 달라도 **같은 자격증명이면 같은 락**이어야 한다.

    수정 전: 락 = `{state_path}.lock` 이라 상태파일이 다르면 락도 달랐다.
    """
    keys = _keys(tmp_path)
    a = TokenManager(keys, tmp_path / "wt_a" / "data" / "token_state.json", live=True)
    b = TokenManager(keys, tmp_path / "wt_b" / "data" / "token_state.json", live=True)
    assert a.lock_path() == b.lock_path(), \
        "상태파일 경로가 다르다는 이유로 서로 다른 락을 잡는다 (감사 A-1 재발)"


def test_lease_path_differs_per_credential(tmp_path):
    """다른 client_id 는 다른 리스 — 자격증명이 다르면 서버 측 토큰도 별개다."""
    a = TokenManager(_keys(tmp_path, "keys_a", KEYS), tmp_path / "s.json", live=True)
    b = TokenManager(_keys(tmp_path, "keys_b", OTHER_KEYS), tmp_path / "s.json", live=True)
    assert a.lock_path() != b.lock_path()


def test_lease_path_is_outside_the_repo(tmp_path):
    """락이 리포 안에 있으면 워크트리마다 갈라진다."""
    tm = TokenManager(_keys(tmp_path), tmp_path / "s.json", live=True)
    assert ROOT not in tm.lock_path().parents, "리스가 리포 안에 있다"
    assert tm.lock_path().is_absolute()


def test_relative_state_path_is_absolutised(tmp_path, monkeypatch):
    """상대경로 상태파일은 CWD 종속이라 워크트리마다 다른 파일을 가리켰다."""
    monkeypatch.chdir(tmp_path)
    tm = TokenManager(_keys(tmp_path), Path("data/token_state.json"), live=True)
    assert tm.state_path.is_absolute()
    assert tm.state_path == (tmp_path / "data" / "token_state.json").resolve()


async def test_two_managers_in_different_cwds_cannot_both_hold_the_lease(
        mock_server, tmp_path, monkeypatch):  # noqa: F811
    """감사 A-1 의 실측 재현 그대로 — CWD 만 다른 두 매니저가 둘 다 발급하면 안 된다.

    수정 전 실측:
        procA lock=...\\procA\\data\\token_state.json.lock
        procB lock=...\\procB\\data\\token_state.json.lock
        -> 서로 다른 락 파일: True   (둘 다 발급 성공 = 상호 토큰 살해)
    """
    monkeypatch.setenv("TOSS_BASE_URL", mock_server.url)
    keys = _keys(tmp_path)
    cwd_a, cwd_b = tmp_path / "procA", tmp_path / "procB"
    for d in (cwd_a, cwd_b):
        (d / "data").mkdir(parents=True)

    monkeypatch.chdir(cwd_a)
    a = TokenManager(keys, Path("data/token_state.json"), live=True)
    monkeypatch.chdir(cwd_b)
    b = TokenManager(keys, Path("data/token_state.json"), live=True)

    try:
        assert a.lock_path() == b.lock_path(), "CWD 가 다르다고 락이 갈라졌다"
        await a.get()
        with pytest.raises(RuntimeError, match="lease already held"):
            await b.get()
    finally:
        a.release()
        b.release()


def test_lease_blocks_a_genuinely_separate_process(tmp_path):
    """같은 프로세스 안이 아니라 **진짜 별도 프로세스**도 막는지 (OS 파일락 확인)."""
    keys = _keys(tmp_path)
    lease_dir = tmp_path / "leases"
    holder = textwrap.dedent(f"""
        import sys, time
        sys.path.insert(0, r"{ROOT}")
        from pathlib import Path
        from tossmon.api.tokens import TokenManager
        tm = TokenManager(Path(r"{keys}"), Path(r"{tmp_path / 'other.json'}"), live=True)
        tm._acquire_lease()
        print("HELD", flush=True)
        time.sleep(30)
    """)
    script = tmp_path / "holder.py"
    script.write_text(holder, encoding="utf-8")
    env = {**os.environ, "TOSSMON_LEASE_DIR": str(lease_dir)}
    proc = subprocess.Popen([sys.executable, str(script)], stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True, env=env)
    try:
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            line = proc.stdout.readline()
            if "HELD" in line:
                break
            if proc.poll() is not None:
                pytest.fail(f"holder died: {proc.stderr.read()}")
        else:
            pytest.fail("holder did not acquire the lease in time")

        mine = TokenManager(keys, tmp_path / "mine.json", live=True)
        with pytest.raises(RuntimeError, match="lease already held"):
            mine._acquire_lease()
    finally:
        proc.kill()
        proc.wait(timeout=10)


def test_holder_fingerprint_is_written(tmp_path):
    """락이 잡혀 있을 때 누가 들고 있는지 사람이 알 수 있어야 한다."""
    tm = TokenManager(_keys(tmp_path), tmp_path / "s.json", live=True)
    try:
        tm._acquire_lease()
        fingerprint = json.loads(
            tm._holder_path(tm.lock_path()).read_text(encoding="utf-8"))
        assert fingerprint["pid"] == os.getpid()
        assert "cwd" in fingerprint and "state_path" in fingerprint
    finally:
        tm.release()


def test_holder_fingerprint_is_removed_on_release(tmp_path):
    """release() 후 지문이 남으면 다음 충돌 때 죽은 프로세스를 보유자로 보고한다."""
    tm = TokenManager(_keys(tmp_path), tmp_path / "s.json", live=True)
    tm._acquire_lease()
    holder = tm._holder_path(tm.lock_path())
    assert holder.exists()
    tm.release()
    assert not holder.exists(), "stale holder 지문이 남아 진단을 오도한다"


def test_mock_mode_still_takes_no_lease(tmp_path):
    """리스 없는 워커 여럿이 동시에 mock 을 쓰는 것은 정상 동작이어야 한다."""
    managers = [TokenManager(tmp_path / "nokeys", tmp_path / "s.json", live=False)
                for _ in range(4)]
    assert all(asyncio.run(m.get()) == "mock-access-token" for m in managers)
    assert not (tmp_path / "leases").exists()


# ========================================================== invalidate CAS
# 감사 H-1 / A-2 [높음]


async def test_invalidate_with_stale_token_does_not_kill_a_newer_one(
        mock_server, tmp_path, monkeypatch):  # noqa: F811
    """지연된 401 이 방금 다른 루프가 발급한 유효 토큰을 죽이면 안 된다."""
    monkeypatch.setenv("TOSS_BASE_URL", mock_server.url)
    tm = TokenManager(_keys(tmp_path), tmp_path / "s.json", live=True)
    try:
        await tm.get()
        tm._token = "T1-fresh"          # 다른 루프가 방금 발급한 것으로 가정
        tm._expires_at_ms = tm._now_ms() + 3_600_000
        tm._write_state("T1-fresh", tm._expires_at_ms)

        await tm.invalidate("T0-stale")  # 늦게 도착한 401 (구 토큰 기준)

        assert tm._token == "T1-fresh", "남의 새 토큰을 죽였다 (감사 A-2 재발)"
        assert json.loads((tmp_path / "s.json").read_text())["token"] == "T1-fresh"
    finally:
        tm.release()


async def test_invalidate_with_current_token_does_discard(
        mock_server, tmp_path, monkeypatch):  # noqa: F811
    """내가 쓴 토큰이 실제로 죽었으면 폐기해야 한다 (CAS 가 과잉 방어면 안 됨)."""
    monkeypatch.setenv("TOSS_BASE_URL", mock_server.url)
    tm = TokenManager(_keys(tmp_path), tmp_path / "s.json", live=True)
    try:
        token = await tm.get()
        await tm.invalidate(token)
        assert tm._token is None
        assert not (tmp_path / "s.json").exists()
    finally:
        tm.release()


async def test_invalidate_without_argument_is_unconditional(
        mock_server, tmp_path, monkeypatch):  # noqa: F811
    """구 동작 보존 — 인자가 없으면 무조건 폐기."""
    monkeypatch.setenv("TOSS_BASE_URL", mock_server.url)
    tm = TokenManager(_keys(tmp_path), tmp_path / "s.json", live=True)
    try:
        await tm.get()
        await tm.invalidate()
        assert tm._token is None
    finally:
        tm.release()


async def test_client_passes_the_token_it_actually_used(mock_server, tmp_path):  # noqa: F811
    """client 가 CAS 인자를 실제로 넘기는지 (넘기지 않으면 CAS 가 무의미하다)."""
    seen: list = []

    class RecordingTokens:
        limiter = None

        async def get(self):
            return "T-used"

        async def invalidate(self, token=None):
            seen.append(token)

    c = make_client(mock_server.url, tmp_path)
    c.tokens = RecordingTokens()
    try:
        with pytest.raises(AuthExpired):
            await c._request("GET", "/api/v1/prices", params={"symbols": "AAPL"},
                             headers={"X-Mock-Inject": "401"})
    finally:
        await c.aclose()
    assert seen == ["T-used"], f"client 가 사용 토큰을 안 넘겼다: {seen}"


# ============================================================ AUTH limiter
# 감사 H-1 / A-4


async def test_token_issuance_goes_through_the_auth_limiter(
        mock_server, tmp_path, monkeypatch):  # noqa: F811
    """발급 경로가 rate limiter 를 통과해야 한다 (401 폭풍 시 무제어 연발 방지)."""
    monkeypatch.setenv("TOSS_BASE_URL", mock_server.url)
    acquired: list[str] = []

    class SpyLimiter(GroupRateLimiter):
        async def acquire(self, group):
            acquired.append(group)
            await super().acquire(group)

    limiter = SpyLimiter(LIMITS)
    tm = TokenManager(_keys(tmp_path), tmp_path / "s.json", live=True, limiter=limiter)
    try:
        await tm.get()
    finally:
        tm.release()
    assert "AUTH" in acquired, "토큰 발급이 AUTH limiter 를 우회했다 (감사 A-4 재발)"


async def test_client_injects_its_limiter_into_the_token_manager(
        mock_server, tmp_path):  # noqa: F811
    """기존 호출자가 코드를 안 고쳐도 AUTH limit 이 걸리도록 client 가 주입한다."""
    c = make_client(mock_server.url, tmp_path)
    try:
        assert c.tokens.limiter is c.limiter
    finally:
        await c.aclose()


# ================================================================== 버스트
# 감사 H-3 / B-1 [높음]


async def test_idle_burst_stays_within_the_published_limit():
    """유휴 직후 첫 1초 통과량이 **공시 한도**를 넘으면 안 된다.

    수정 전 실측: MARKET_DATA 공시 10/s, 목표 7/s 인데 첫 1초에 **14회** 통과.
    (capacity == rate 이고 버킷이 가득 찬 상태로 시작했기 때문)
    """
    published = 10.0
    lim = GroupRateLimiter({"MARKET_DATA": published}, usage_ratio=0.7)
    await asyncio.sleep(1.5)          # 유휴 — 버킷이 최대로 찬다

    passed = 0
    start = time.monotonic()
    while time.monotonic() - start < 1.0:
        try:
            await asyncio.wait_for(lim.acquire("MARKET_DATA"), timeout=0.05)
        except asyncio.TimeoutError:
            continue
        passed += 1
    assert passed <= published, \
        f"유휴 후 첫 1초에 {passed}회 통과 — 공시 한도 {published}/s 초과 (감사 B-1 재발)"


def test_bucket_capacity_is_below_rate():
    lim = GroupRateLimiter({"MARKET_DATA": 10.0}, usage_ratio=0.7)
    snap = lim.snapshot("MARKET_DATA")
    assert snap["capacity"] < snap["rate"], "capacity == rate 면 첫 1초에 2배가 나간다"


def test_bucket_starts_empty_not_full():
    """기동 직후 버스트를 막으려면 빈 상태로 시작해야 한다."""
    lim = GroupRateLimiter({"MARKET_DATA": 10.0}, usage_ratio=0.7)
    assert lim.snapshot("MARKET_DATA")["tokens"] == pytest.approx(0.0)


# ============================================================ 헤더 클램프
# 감사 H-2 / B-2 [높음]


def test_header_cannot_raise_rate_above_the_published_limit():
    """서버 헤더 한 줄로 rate 가 60배가 되던 경로 (7.0 → 420 실측)."""
    lim = GroupRateLimiter({"MARKET_DATA": 10.0}, usage_ratio=0.7)
    before = lim.snapshot("MARKET_DATA")["rate"]
    assert before == pytest.approx(7.0)

    # 헤더 의미가 '분당 쿼터' 로 바뀌기만 해도 이 값이 온다 (600/min == 10/s, 같은 뜻)
    lim.update_from_headers("MARKET_DATA", {"X-RateLimit-Limit": "600",
                                            "X-RateLimit-Remaining": "600"})
    after = lim.snapshot("MARKET_DATA")["rate"]
    assert after == pytest.approx(before), \
        f"헤더가 rate 를 {before} → {after} 로 끌어올렸다 (감사 B-2 재발)"
    assert lim.limits["MARKET_DATA"] <= 10.0, "오염된 한도가 기록으로 남았다"
    assert lim.counters["limit_header_clamped"] == 1, "클램프가 조용히 일어났다"


def test_header_may_still_lower_the_rate():
    """내리는 방향은 계속 채택해야 한다 (서버가 한도를 줄인 경우)."""
    lim = GroupRateLimiter({"MARKET_DATA": 10.0}, usage_ratio=0.7)
    lim.update_from_headers("MARKET_DATA", {"X-RateLimit-Limit": "4",
                                            "X-RateLimit-Remaining": "4"})
    assert lim.snapshot("MARKET_DATA")["rate"] == pytest.approx(2.8)
    assert lim.counters["limit_header_clamped"] == 0


def test_operator_configured_limit_is_respected_as_the_ceiling():
    """운영자가 config 로 공시값보다 높게 잡았다면 그건 사람의 결정이므로 존중한다."""
    lim = GroupRateLimiter({"MARKET_DATA": 20.0}, usage_ratio=1.0)
    lim.update_from_headers("MARKET_DATA", {"X-RateLimit-Limit": "20"})
    assert lim.snapshot("MARKET_DATA")["rate"] == pytest.approx(20.0)
    lim.update_from_headers("MARKET_DATA", {"X-RateLimit-Limit": "9999"})
    assert lim.snapshot("MARKET_DATA")["rate"] == pytest.approx(20.0)


# ================================================== ForbiddenEndpoint 격리
# 감사 H-5 [높음]


def test_forbidden_endpoint_is_not_a_toss_api_error():
    """가장 잡히면 안 되는 예외가 광역 except 에 삼켜지면 안 된다."""
    assert not issubclass(ForbiddenEndpoint, TossApiError), \
        "ForbiddenEndpoint 가 TossApiError 를 상속한다 (감사 H-5 재발)"
    assert issubclass(ForbiddenEndpoint, Exception)


def test_broad_toss_api_error_handler_does_not_swallow_it():
    """컬렉터의 `except (TossApiError, OSError)` 패턴을 그대로 재현."""
    swallowed = False
    try:
        try:
            endpoints.check_allowed("POST", "/api/v1/orders")
        except (TossApiError, OSError):
            swallowed = True
    except ForbiddenEndpoint:
        pass
    assert not swallowed, "주문 엔드포인트 도달이 warn 한 줄로 삼켜졌다 (감사 H-5 재발)"


async def test_forbidden_endpoint_escapes_the_client_retry_loop(mock_server, tmp_path):  # noqa: F811
    """client 의 재시도 루프도 이 예외를 잡거나 재시도하면 안 된다."""
    c = make_client(mock_server.url, tmp_path)
    try:
        with pytest.raises(ForbiddenEndpoint):
            await c._request("POST", "/api/v1/orders", json={"side": "BUY"})
        assert c.counters["requests"] == 0
        assert c.counters["retries"] == 0
    finally:
        await c.aclose()


# ============================================================= dot segment
# 감사 M-11 [중간]


@pytest.mark.parametrize("path", [
    "/api/v1/stocks/../warnings",
    "/api/v1/stocks/./warnings",
    "/api/v1/stocks/%2e%2e/warnings",
    "/api/v1/stocks/..%2f../warnings",
    "/api/v1/../v1/orders",
])
def test_dot_segments_are_rejected_by_the_first_gate(path):
    """1층과 2층이 같은 판단을 해야 한다 — 1층이 `..` 을 {symbol} 로 받아들이면 안 된다."""
    with pytest.raises(ForbiddenEndpoint):
        endpoints.check_allowed("GET", path)


def test_legitimate_templated_path_still_resolves():
    assert endpoints.check_allowed("GET", "/api/v1/stocks/AAPL/warnings") == \
        "/api/v1/stocks/{symbol}/warnings"
    assert endpoints.check_allowed("GET", "/api/v1/stocks/BRK.B/warnings")


# ================================================================== 시크릿
# 감사 M-1 [중간]


async def test_token_error_description_is_not_echoed_into_the_message(
        tmp_path, monkeypatch):
    """서버가 제어하는 error_description 이 평문으로 로그에 흘러가면 안 된다.

    일부 OAuth 구현은 `invalid_client` 응답에 제출한 client_secret 을 에코한다.
    그 메시지는 notifier → 평문 로그 파일로 간다.
    """
    secret = "SUPERSECRET_DO_NOT_LOG"

    async def handler(request):
        return httpx.Response(400, json={
            "error": "invalid_client",
            "error_description": f"client audit-client secret {secret} rejected\nFAKE LOG LINE",
        })

    monkeypatch.setenv("TOSS_BASE_URL", "http://127.0.0.1:9")
    tm = TokenManager(_keys(tmp_path), tmp_path / "s.json", live=True)

    # tokens.py 는 `import httpx` 후 `httpx.AsyncClient` 를 쓰므로 패치 대상이 전역이다.
    # 진짜 클래스를 먼저 붙잡아 두지 않으면 fake 가 자기 자신을 호출해 무한재귀가 된다.
    real_client = httpx.AsyncClient

    def fake_client(**kw):
        kw.pop("transport", None)
        return real_client(transport=httpx.MockTransport(handler), **kw)

    monkeypatch.setattr("tossmon.api.tokens.httpx.AsyncClient", fake_client)
    try:
        with pytest.raises(AuthExpired) as ei:
            await tm._issue()
    finally:
        tm.release()

    message = str(ei.value)
    assert secret not in message, "client_secret 이 예외 메시지에 실렸다 (감사 M-1 재발)"
    assert "FAKE LOG LINE" not in message, "서버 제어 텍스트로 로그 라인 위조가 가능하다"
    assert "\n" not in message, "개행이 그대로 실려 로그 위조가 가능하다"
    assert "invalid_client" in message, "진단에 필요한 error 코드까지 지우면 안 된다"


# ======================================================= live_probe 기본값
# 감사 B-5 / M-7


def test_force429_is_not_in_the_default_probe_set():
    """`--probe all` 이 의도적 라이브 429 를 유발하면 안 된다."""
    import tools.live_probe as lp
    assert "force429" in lp.PROBES, "프로브 자체는 남아 있어야 한다"
    assert "force429" not in lp.ALL_PROBES, \
        "force429 가 기본 실행에 포함된다 (감사 B-5 재발)"


def test_live_probe_does_not_build_a_live_url_or_default_to_one():
    """계약 C-11 §3 / C-9 — 라이브 URL 을 만들거나 기본값으로 두면 안 된다.

    호스트 문자열 자체는 **판별 가드**(`LIVE_HOST_MARKER`)로 남아 있다 — base_url 이
    실서버인지 알아야 라이브 플래그 없는 실행을 거부할 수 있기 때문이다. 그건 우회가 아니라
    안전장치이므로 금지 대상이 아니다. 금지 대상은 (a) grep 회피용 문자열 분할,
    (b) 실행 가능한 라이브 URL 리터럴, (c) 그것을 argparse 기본값으로 두는 것이다.
    """
    src = (ROOT / "tools" / "live_probe.py").read_text(encoding="utf-8")
    # 금지 검사는 **주석을 뺀 코드**에만 적용한다 — 옛 결함을 설명하는 주석에 그 패턴이
    # 등장하는 것은 문서화이지 회피가 아니다.
    code = "\n".join(ln for ln in src.splitlines() if not ln.lstrip().startswith("#"))

    assert '"https://" + "openapi' not in code, "스킴/호스트 분할 회피가 남아 있다"
    assert "LIVE_BASE_URL" not in code, "라이브 base URL 상수가 코드로 남아 있다"
    assert '"https://openapi.tossinvest.com' not in code, "실행 가능한 라이브 URL 리터럴"

    # --base-url 기본값은 env 에서만 온다 (계약 C-9: 기본값 없음)
    assert 'default=os.environ.get("TOSS_BASE_URL")' in src, \
        "--base-url 기본값이 env 유래가 아니다"


def test_live_probe_defaults_to_safe_mode():
    """live 는 기본이 False 여야 하고, state 기본값은 절대경로여야 한다."""
    import tools.live_probe as lp
    parser_args = lp.main.__doc__  # noqa: F841  (문서화 목적)
    assert lp.default_state_path().is_absolute()
    assert ROOT not in lp.default_state_path().parents, "상태파일 기본값이 리포 안이다"


def test_live_probe_refuses_live_host_without_flags(monkeypatch, capsys):
    """실서버를 대상으로 하는데 --live/TOSS_LIVE 가 없으면 실행을 거부한다."""
    import tools.live_probe as lp
    monkeypatch.delenv("TOSS_LIVE", raising=False)
    rc = lp.main(["--base-url", "https://openapi.tossinvest.com", "--probe", "fx"])
    assert rc == 3
    assert "refusing to run" in capsys.readouterr().err


def test_live_probe_refuses_without_base_url(monkeypatch, capsys):
    """계약 C-9 — base URL 에 기본값이 없어야 한다."""
    import tools.live_probe as lp
    monkeypatch.delenv("TOSS_BASE_URL", raising=False)
    rc = lp.main(["--probe", "fx"])
    assert rc == 3
    assert "base-url" in capsys.readouterr().err


# ================================================= iso_to_ms 절삭 (감사 M-2)
#
# `int(dt.timestamp() * 1000)` 은 float 오차로 `...357.9998` 을 357 로 **깎았다**.
# 영향 범위는 실측상 시대 의존적이다 — 2004 년대에서는 24%가 어긋났고 우리 운용 구간
# (2023~2027)에서는 500만 건 연속 스캔에서 0건이었다. 그래도 고친 이유는 (1) 값이
# 싸고 (2) `trades_snap` PK 가 ts_ms 를 포함해 1ms 오차가 같은 체결을 두 행으로 만들며
# (3) 정수 연산은 시대 의존성 자체를 없애기 때문이다.


def _exact_ms(dt) -> int:
    """부동소수점을 쓰지 않는 기준값."""
    from datetime import datetime as _dt, timedelta as _td, timezone as _tz
    return (dt - _dt(1970, 1, 1, tzinfo=_tz.utc)) // _td(milliseconds=1)


@pytest.mark.parametrize("iso,expect_ms", [
    # 수정 전 int() 가 각각 1ms 씩 깎던 실제 케이스
    ("2004-02-05T20:22:01.074+09:00", 1075980121074),
    ("2004-04-15T17:02:52.824+09:00", 1082016172824),
    ("2004-05-21T15:55:49.274+09:00", 1085122549274),
    ("2004-10-05T14:31:33.666+09:00", 1096954293666),
    ("2004-03-10T08:14:08.011+09:00", 1078874048011),
])
def test_iso_to_ms_does_not_truncate(iso, expect_ms):
    from tossmon.api.models import iso_to_ms
    assert iso_to_ms(iso) == expect_ms


def test_iso_to_ms_is_exact_over_a_dense_span_in_the_affected_era():
    """감사가 200만 건 무작위로 잡은 것을 축소·결정론 버전으로 고정.

    2004-03 구간은 수정 전 **240,000/1,000,000** 이 어긋났다. 0 이어야 한다.
    """
    from datetime import datetime, timedelta, timezone
    from tossmon.api.models import iso_to_ms

    kst = timezone(timedelta(hours=9))
    base = datetime(2004, 3, 15, 12, 0, 0, tzinfo=timezone.utc).astimezone(kst)
    bad = 0
    for i in range(0, 200_000, 7):          # 결정론적 표본
        dt = base + timedelta(milliseconds=i)
        if iso_to_ms(dt.isoformat(timespec="milliseconds")) != _exact_ms(dt):
            bad += 1
    assert bad == 0, f"{bad}건 절삭 — 감사 M-2 재발"


def test_iso_to_ms_is_exact_in_the_operational_era():
    from datetime import datetime, timedelta, timezone
    from tossmon.api.models import iso_to_ms

    kst = timezone(timedelta(hours=9))
    base = datetime(2026, 7, 30, 18, 14, 11, tzinfo=timezone.utc).astimezone(kst)
    for i in range(0, 50_000, 3):
        dt = base + timedelta(milliseconds=i)
        assert iso_to_ms(dt.isoformat(timespec="milliseconds")) == _exact_ms(dt)


def test_iso_to_ms_uses_no_float_arithmetic():
    """구현이 다시 float 경로로 돌아가면 시대 의존 결함이 되살아난다.

    문자열 검색은 docstring 에 `timestamp()` 를 언급하기만 해도 걸리므로 AST 로 본다 —
    실제 **호출**이 있는지만 판정한다.
    """
    import ast
    import inspect
    from tossmon.api import models

    tree = ast.parse(inspect.getsource(models._dt_to_ms))
    called = {
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }
    assert "timestamp" not in called, "float timestamp() 호출 경로로 회귀했다"
    assert any(isinstance(n, ast.FloorDiv) for n in ast.walk(tree)), \
        "정수 나눗셈(//)이 사라졌다"


def test_ms_to_iso_round_trips_exactly():
    from tossmon.api.models import iso_to_ms, ms_to_iso
    for base in (1_075_980_121_074, 1_785_402_855_314):   # 2004, 2026
        for i in range(0, 20_000, 11):
            assert iso_to_ms(ms_to_iso(base + i)) == base + i


def test_sub_millisecond_input_rounds_to_nearest():
    """API 는 ms 까지만 주지만, 더 정밀한 값이 와도 깎지 말고 반올림해야 한다."""
    from tossmon.api.models import iso_to_ms
    assert iso_to_ms("2026-07-30T00:00:00.000400+00:00") == 1785369600000
    assert iso_to_ms("2026-07-30T00:00:00.000500+00:00") == 1785369600001
    assert iso_to_ms("2026-07-30T00:00:00.000600+00:00") == 1785369600001


# ============================================ api_keys 파싱 오류 유출 (U-4)


def test_malformed_json_api_keys_never_leaks_content(tmp_path):
    """`json.JSONDecodeError` 는 `str(e)` 에는 위치만 담지만 **`e.doc` 에 파일 전문**을
    들고 다닌다. 예외 체인 어디에도 시크릿이 남으면 안 된다 (감사 U-4).
    """
    import traceback

    secret = "SUPERSECRET_LEAKME"
    keys = tmp_path / "api_keys"
    keys.write_text('{"client_id": "abc", "client_secret": "%s" BROKEN}' % secret,
                    encoding="utf-8")
    tm = TokenManager(keys, tmp_path / "s.json", live=True)

    with pytest.raises(RuntimeError) as ei:
        tm._read_keys()
    exc = ei.value

    assert secret not in str(exc)
    assert secret not in traceback.format_exc()
    # `raise ... from None` 은 표시만 억제한다 — __context__ 자체가 끊겨 있어야 한다.
    assert exc.__context__ is None, "__context__ 에 원본(.doc=파일 전문)이 매달려 있다"
    assert exc.__cause__ is None
    for attr in ("doc", "msg", "args"):
        assert secret not in str(getattr(exc, attr, ""))


def test_valid_key_formats_still_parse(tmp_path):
    keys = tmp_path / "api_keys"
    keys.write_text('{"client_id":"abc","client_secret":"def"}', encoding="utf-8")
    assert TokenManager(keys, tmp_path / "s.json", live=True)._read_keys() == ("abc", "def")
    keys.write_text("CLIENT_ID=abc\nCLIENT_SECRET=def\n", encoding="utf-8")
    assert TokenManager(keys, tmp_path / "s.json", live=True)._read_keys() == ("abc", "def")



# ==================================== B-3 통합 계약 (client 측 — W4 가 여기서 청구)


async def test_client_counts_every_http_attempt_not_logical_calls(mock_server, tmp_path):  # noqa: F811
    """W4 의 `after_call` 이 재시도까지 예산에 계상하려면 client 가 **시도 수**를 노출해야 한다.

    감사 B-3 은 재시도가 0회로 계상되던 문제였고 W4 가 시도 수 계상으로 고쳤다.

    2026-08-08 (docs/46): 예산이 읽는 것이 전역 `counters["requests"]` 에서
    **그룹별 `sent_by_group`** 으로 바뀌었다 (전역 델타는 남의 그룹 송신이 섞여 이중
    계상을 만들었다). 그래서 둘 다 시도 수를 노출해야 한다 — **그룹별 쪽이 멈추면
    예산이 재시도를 못 보고 B-3 이 조용히 재발한다.**
    """
    from tossmon.api.errors import TransientHTTP

    c = make_client(mock_server.url, tmp_path, timeout_s=3)
    try:
        before = c.counters["requests"]
        before_md = c.sent_by_group.get("MARKET_DATA", 0)
        with pytest.raises(TransientHTTP):
            await c._request("GET", "/api/v1/prices", params={"symbols": "AAPL"},
                             headers={"X-Mock-Inject": "500"})
        assert c.counters["requests"] - before == 4, \
            "논리 호출 1건의 HTTP 시도 수(1+재시도3)가 노출되지 않는다 — B-3 재발 위험"
        assert c.sent_by_group.get("MARKET_DATA", 0) - before_md == 4, \
            "그룹별 송신 수가 시도를 다 세지 않는다 — 예산이 재시도를 못 본다 (B-3 재발)"
    finally:
        await c.aclose()


# ================================ 계약 A7 §1 — ALLOWLIST 축소 (감사 G-4)


def test_kr_market_calendar_is_no_longer_allowlisted():
    """미국 전용 프로젝트라 KR 캘린더는 허용 목록에서 빠졌다 (계약 A7 §1).

    감사 G-4 는 미사용 항목 5개를 지목했으나, 실제로 어떤 코드도 쓰지 않는 것은
    이것 하나뿐이었다(나머지 4개는 live_probe 가 _request 로 사용 중).
    """
    with pytest.raises(ForbiddenEndpoint):
        endpoints.check_allowed("GET", "/api/v1/market-calendar/KR")
    assert ("GET", "/api/v1/market-calendar/KR") not in endpoints.ALLOWLIST
    assert "/api/v1/market-calendar/KR" not in endpoints.GROUP_OF


def test_us_market_calendar_still_works():
    """축소가 US 경로를 건드리면 안 된다 (회귀)."""
    assert endpoints.check_allowed("GET", "/api/v1/market-calendar/US") == \
        "/api/v1/market-calendar/US"
    assert endpoints.group_of("/api/v1/market-calendar/US") == "MARKET_INFO"


async def test_kr_calendar_is_blocked_at_both_layers(mock_server, tmp_path):  # noqa: F811
    """1층(_request 관문)과 2층(전송 레벨) 모두에서 막혀야 한다."""
    c = make_client(mock_server.url, tmp_path)
    try:
        with pytest.raises(ForbiddenEndpoint):
            await c._request("GET", "/api/v1/market-calendar/KR")
        assert c.counters["requests"] == 0
        with pytest.raises(ForbiddenEndpoint):
            await c._http.get("/api/v1/market-calendar/KR")
    finally:
        await c.aclose()


async def test_us_calendar_end_to_end_after_removal(mock_server, tmp_path):  # noqa: F811
    c = make_client(mock_server.url, tmp_path)
    try:
        cal = await c.get_us_calendar()
        assert set(cal) == {"previous", "today", "next"}
        assert cal["today"].regular is not None
    finally:
        await c.aclose()


def test_allowlist_and_group_table_stay_in_sync():
    """허용 목록에서 뺐는데 GROUP_OF 에 남으면 다음에 되살릴 때 헷갈린다."""
    allow = {p for _m, p in endpoints.ALLOWLIST}
    assert set(endpoints.GROUP_OF) == allow, (
        f"불일치: only_in_GROUP_OF={set(endpoints.GROUP_OF) - allow} "
        f"only_in_ALLOWLIST={allow - set(endpoints.GROUP_OF)}")


# ========================= 계약 A7 §2 — 상태파일 소유자 전용 권한 (감사 E-2)


def _acl_principals(path) -> list[str]:
    """Windows ACL 에 등장하는 주체 목록 (icacls 파싱)."""
    import re
    import subprocess
    out = subprocess.run(["icacls", str(path)], capture_output=True, text=True,
                         errors="replace").stdout
    return re.findall(r"([A-Za-z0-9_\-\ ]+):\([A-Z)(,A-Z]+\)$", out, re.M)


@pytest.mark.skipif(os.name == "nt", reason="POSIX 모드 검사")
def test_state_file_is_0600_on_posix(tmp_path):
    tm = TokenManager(tmp_path / "k", tmp_path / "state.json", live=True)
    tm._write_state("DUMMY-NOT-REAL", 1)
    assert (tmp_path / "state.json").stat().st_mode & 0o777 == 0o600


@pytest.mark.skipif(os.name != "nt", reason="Windows ACL 검사")
def test_state_file_acl_drops_inheritance_on_windows(tmp_path):
    """상속 ACL 은 코드의 보장이 아니다 (감사 E-2) — 명시적으로 소유자/SYSTEM 만 남긴다."""
    tm = TokenManager(tmp_path / "k", tmp_path / "state.json", live=True)
    tm._write_state("DUMMY-NOT-REAL", 1)
    principals = _acl_principals(tmp_path / "state.json")
    assert principals, "icacls 출력을 파싱하지 못했다"
    lowered = " ".join(principals).lower()
    assert "system" in lowered, f"SYSTEM 이 없다: {principals}"
    # 상속으로 딸려오던 Administrators / Users / Everyone 이 남아 있으면 안 된다
    for unwanted in ("everyone", "users", "authenticated"):
        assert unwanted not in lowered, f"{unwanted} 가 ACL 에 남아 있다: {principals}"


def test_permissions_are_applied_before_the_atomic_replace(tmp_path, monkeypatch):
    """노출 창이 없어야 한다 — 임시 파일 단계에서 이미 제한돼 있어야 한다.

    `os.replace` 는 원본(임시 파일)의 권한을 그대로 가져가므로, 순서가 뒤바뀌면
    토큰이 넓은 권한으로 디스크에 존재하는 순간이 생긴다.
    """
    from tossmon.api import tokens as tok

    order: list[str] = []
    real_restrict = tok.restrict_to_owner
    real_replace = tok.os.replace

    def spy_restrict(path):
        order.append(f"restrict:{Path(path).suffix}")
        return real_restrict(path)

    def spy_replace(src, dst):
        order.append("replace")
        return real_replace(src, dst)

    monkeypatch.setattr(tok, "restrict_to_owner", spy_restrict)
    monkeypatch.setattr(tok.os, "replace", spy_replace)

    tm = TokenManager(tmp_path / "k", tmp_path / "state.json", live=True)
    tm._write_state("DUMMY-NOT-REAL", 1)

    assert order[0] == "restrict:.tmp", f"임시 파일을 먼저 제한하지 않았다: {order}"
    assert order[1] == "replace", f"제한 직후 교체가 아니다: {order}"


def test_permission_failure_does_not_break_state_writing(tmp_path, monkeypatch):
    """★ 보안 강화가 가용성을 무너뜨리면 안 된다 (계약 A7 §2).

    ACL/chmod 조작은 도메인 계정·정책·이상한 파일시스템에서 깨질 수 있다.
    깨지더라도 토큰 상태 저장은 성공해야 한다.
    """
    from tossmon.api import tokens as tok

    def boom(*a, **kw):
        raise OSError("injected permission failure")

    monkeypatch.setattr(tok.os, "chmod", boom)          # POSIX 경로
    monkeypatch.setattr(tok.subprocess, "run", boom)    # Windows 경로
    tok.PERMISSION_STATS.update(applied=0, failed=0, last_error=None)

    tm = TokenManager(tmp_path / "k", tmp_path / "state.json", live=True)
    tm._write_state("DUMMY-NOT-REAL", 12345)            # 예외가 나면 안 된다

    saved = json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))
    assert saved["token"] == "DUMMY-NOT-REAL"
    assert saved["expires_at_ms"] == 12345
    assert tok.PERMISSION_STATS["failed"] >= 1, "실패가 조용히 넘어갔다 — 관측 불가"
    assert tok.PERMISSION_STATS["last_error"]


async def test_token_issuance_survives_permission_failure(
        mock_server, tmp_path, monkeypatch):  # noqa: F811
    """전 경로 확인 — 권한 조작이 깨져도 발급→저장→반환이 끝까지 성공해야 한다."""
    from tossmon.api import tokens as tok

    def boom(*a, **kw):
        raise OSError("injected permission failure")

    monkeypatch.setattr(tok.os, "chmod", boom)
    monkeypatch.setattr(tok.subprocess, "run", boom)
    monkeypatch.setenv("TOSS_BASE_URL", mock_server.url)

    tm = TokenManager(_keys(tmp_path), tmp_path / "state.json", live=True)
    try:
        assert await tm.get() == "mock-access-token"
    finally:
        tm.release()


def test_restrict_to_owner_never_raises(tmp_path, monkeypatch):
    """어떤 예외가 나도 밖으로 새지 않아야 한다 (BaseException 계열 제외)."""
    from tossmon.api import tokens as tok

    target = tmp_path / "f.json"
    target.write_text("{}", encoding="utf-8")
    for exc in (OSError("x"), ValueError("y"), RuntimeError("z")):
        def boom(*a, _e=exc, **kw):
            raise _e
        monkeypatch.setattr(tok.os, "chmod", boom)
        monkeypatch.setattr(tok.subprocess, "run", boom)
        assert tok.restrict_to_owner(target) is False

    # 존재하지 않는 경로도 조용히 False
    assert tok.restrict_to_owner(tmp_path / "nope.json") is False
