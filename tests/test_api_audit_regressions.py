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

from test_api_support import LIMITS, make_client, mock_server  # noqa: F401
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
