"""TokenManager — 단일 토큰 보장 (계약 C-4).

client당 유효 토큰이 1개뿐이므로(재발급 시 기존 토큰 즉시 무효화),
OS 파일락(filelock) + 상태파일로 전 시스템에서 발급 주체를 1개로 강제한다.
live=False 면 실발급 시도 자체가 RuntimeError (mock 고정 토큰 사용).

락 정책 (감사 A-1/F-1 반영 — 이전 방식은 리스를 **전혀 보장하지 못했다**):

리스는 **자격증명(client_id) 단위**로, **리포 밖 고정 위치**에 건다.
이전에는 `{state_path}.lock` 을 썼는데 `state_path` 가 상대경로(`data/token_state.json`)라
락 파일이 **CWD 종속**이었다. 워크트리가 8개인 이 프로젝트에서 각 워크트리의 프로세스는
서로 다른 락 파일을 잡고 **둘 다 발급에 성공**했다 — 그리고 이 API 는 client 당 토큰이
1개라 두 번째 발급이 첫 번째를 즉시 죽인다. 상호 토큰 살해가 실측 재현됐다.

경로를 절대화하는 것만으로는 부족하다: 설정을 다르게 준 두 프로세스는 여전히 다른 락을 잡는다.
그래서 **락 이름을 client_id 해시에서 유도**한다 — 상태파일 경로가 무엇이든, CWD 가 무엇이든,
같은 자격증명이면 반드시 같은 락 파일이다. 리스 디렉터리는 `TOSSMON_LEASE_DIR` 로 재정의할 수
있다(테스트 격리용).

mock 모드는 락을 잡지 않는다(리스 없는 워커 여럿이 동시에 mock 을 쓰는 것이 정상 동작).

서버 주소는 계약 C-9 의 `TOSS_BASE_URL` env 로 받는다.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import subprocess
import tempfile
import time
from pathlib import Path

import filelock
import httpx

from .endpoints import check_allowed
from .errors import AuthExpired, Forbidden, TransientHTTP

# 만료 이 시간 전에 선제 재발급 (계약 C-4).
REFRESH_MARGIN_MS = 60_000

# mock 모드에서 사용하는 고정 토큰 (계약 C-9). 실서버에서는 절대 통하지 않는다.
MOCK_TOKEN = "mock-access-token"

TOKEN_PATH = "/oauth2/token"

# 리스 디렉터리 재정의 env (테스트 격리 / 운영 배치용).
LEASE_DIR_ENV = "TOSSMON_LEASE_DIR"

_log = logging.getLogger(__name__)

# icacls 호출 시 콘솔 창이 튀지 않게 (Windows 전용 플래그, 다른 OS 에서는 0).
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

# 권한 제한 시도 관측 (계약 A7 §2 — 실패해도 죽지 않되 조용히 넘어가지도 않는다).
PERMISSION_STATS: dict[str, object] = {"applied": 0, "failed": 0, "last_error": None}

# OAuth2 표준 error 코드 enum. 이 밖의 값은 서버가 넣은 임의 텍스트로 보고 로그에 싣지 않는다.
_OAUTH_ERRORS = frozenset({
    "invalid_request", "invalid_client", "invalid_grant",
    "unauthorized_client", "unsupported_grant_type", "access_denied",
})


def lease_dir() -> Path:
    """리스 파일이 놓이는 **리포 밖 절대경로** 디렉터리.

    워크트리·CWD 와 무관해야 하므로 리포 안을 절대 쓰지 않는다.
    """
    override = os.environ.get(LEASE_DIR_ENV)
    if override:
        return Path(override).expanduser().resolve()
    base = os.environ.get("LOCALAPPDATA") or os.environ.get("XDG_STATE_HOME")
    if base:
        return Path(base).expanduser().resolve() / "tossmon"
    home = Path.home()
    if home and str(home) not in ("", "/"):
        return (home / ".local" / "state" / "tossmon").resolve()
    return Path(tempfile.gettempdir()).resolve() / "tossmon"


def restrict_to_owner(path: str | Path) -> bool:
    """파일을 **소유자 전용**으로 제한한다 (계약 A7 §2). 성공하면 True.

    - POSIX: `chmod 0600`.
    - Windows: `icacls /inheritance:r` 로 상속을 끊고 현재 사용자 + SYSTEM 만 남긴다.
      Windows 의 `os.chmod` 는 읽기전용 비트만 만지고 **ACL 에는 아무 영향이 없다** —
      그래서 실제 ACL 조작이 필요하다.

    ⚠️ **실패해도 예외를 던지지 않는다** (계약 A7 §2 명시).
    권한 강화는 심층방어이지 기능이 아니다. 플랫폼별 ACL 조작은 도메인 계정·이상한
    파일시스템·정책 제한 등으로 깨질 수 있는데, 그것 때문에 토큰 발급이 실패하면
    **보안 강화가 가용성을 무너뜨리는** 결과가 된다. 실패는 경고로만 남긴다.

    관측: 실패 건수는 `PERMISSION_STATS` 에 누적된다(조용히 넘어가지 않도록).
    """
    p = Path(path)
    try:
        if os.name != "nt":
            os.chmod(p, 0o600)
            PERMISSION_STATS["applied"] += 1
            return True
        return _restrict_to_owner_windows(p)
    except Exception as exc:                      # noqa: BLE001 — 어떤 실패도 치명이 아니다
        _note_permission_failure(p, exc)
        return False


def _restrict_to_owner_windows(p: Path) -> bool:
    """icacls 로 상속을 끊고 현재 사용자·SYSTEM 만 남긴다."""
    user = os.environ.get("USERNAME") or ""
    domain = os.environ.get("USERDOMAIN") or ""
    principal = f"{domain}\\{user}" if domain and user else user
    if not principal:
        _note_permission_failure(p, RuntimeError("USERNAME/USERDOMAIN not set"))
        return False
    cmd = ["icacls", str(p), "/inheritance:r",
           "/grant:r", f"{principal}:(F)",
           "/grant:r", "*S-1-5-18:(F)"]           # SID 로 지정 — 로캘 무관 (SYSTEM)
    proc = subprocess.run(cmd, capture_output=True, text=True, errors="replace",
                          timeout=15, creationflags=_NO_WINDOW)
    if proc.returncode != 0:
        _note_permission_failure(
            p, RuntimeError(f"icacls rc={proc.returncode}: {proc.stderr.strip()[:200]}"))
        return False
    PERMISSION_STATS["applied"] += 1
    return True


def _note_permission_failure(p: Path, exc: BaseException) -> None:
    PERMISSION_STATS["failed"] += 1
    # 경로만 남긴다 — 파일 내용(토큰)은 절대 싣지 않는다.
    PERMISSION_STATS["last_error"] = f"{type(exc).__name__}: {exc}"[:300]
    _log.warning(
        "could not restrict permissions on %s (%s) — 토큰 흐름은 계속한다. "
        "이 파일에 소유자 외 접근이 가능한지 확인할 것 (계약 A7 §2)",
        p, type(exc).__name__)


def lease_path_for_client(client_id: str) -> Path:
    """client_id → 리스 파일 경로. 같은 자격증명이면 어디서 실행하든 같은 파일."""
    digest = hashlib.sha256(client_id.encode("utf-8")).hexdigest()[:16]
    return lease_dir() / f"token-{digest}.lease"


class TokenManager:
    def __init__(self, keys_path: Path, state_path: Path, live: bool,
                 limiter=None):
        # 절대화 — 상대경로는 CWD 종속이라 워크트리마다 다른 파일을 가리킨다 (감사 A-1).
        self.keys_path = Path(keys_path).expanduser().resolve()
        self.state_path = Path(state_path).expanduser().resolve()
        self.live = bool(live)
        # AUTH 그룹 rate limit 적용용 (감사 A-4). TossClient 가 자동으로 주입한다.
        self.limiter = limiter
        self._flock: filelock.FileLock | None = None
        self._lock_path: Path | None = None
        self._alock = asyncio.Lock()
        self._token: str | None = None
        self._expires_at_ms: int = 0

    # ---- public ---------------------------------------------------------

    async def get(self) -> str:
        """유효 토큰 반환. 만료 60초 전 선제 재발급. 파일락 실패 시 즉시 예외(발급 강행 금지)."""
        if not self.live:
            return MOCK_TOKEN
        async with self._alock:
            if self._token is not None and not self._is_stale(self._expires_at_ms):
                return self._token
            self._acquire_lease()
            # 같은 리스를 공유하는 이전 실행이 남긴 토큰이 아직 유효하면 재발급하지 않는다.
            # (재발급은 그 토큰을 죽이므로 가능한 한 피한다.)
            state = self._read_state()
            if state is not None and not self._is_stale(state[1]):
                self._token, self._expires_at_ms = state
                return self._token
            self._token, self._expires_at_ms = await self._issue()
            self._write_state(self._token, self._expires_at_ms)
            return self._token

    async def invalidate(self, token: str | None = None) -> None:
        """AuthExpired 수신 시 호출. 다음 get()이 재발급.

        `token` 을 주면 **compare-and-swap** 으로 동작한다 — 지금 보유한 토큰이 그 값일 때만
        버린다 (감사 A-2/H-1). 여러 루프가 하나의 TokenManager 를 공유하므로, 지연된 401 이
        방금 다른 루프가 발급한 **유효한 토큰을 죽이는** 연쇄가 실제로 재현됐다:
        루프1(T0) 401 지연 → 루프2 재발급(T1) → 루프1 의 늦은 401 이 T1 을 폐기 → T2 발급 →
        T1 사망 → 루프2 401 → ... 401 한 건이 폭풍의 씨앗이 된다.

        `token=None` 은 무조건 폐기(구 동작)다. 호출자를 못 믿는 경로에서만 쓸 것.
        """
        async with self._alock:
            if token is not None and self._token is not None and token != self._token:
                # 이미 더 새 토큰으로 교체됐다 — 남의 유효 토큰을 죽이지 않는다.
                return
            if token is not None and self._token is None:
                # 메모리에는 없지만 다른 프로세스/이전 실행이 남긴 상태가 더 새로울 수 있다.
                state = self._read_state()
                if state is not None and state[0] != token:
                    return
            self._token = None
            self._expires_at_ms = 0
            if self.live:
                try:
                    self.state_path.unlink()
                except (FileNotFoundError, NotADirectoryError):
                    pass

    def release(self) -> None:
        """리스(파일락) 반납. 프로세스 종료 시 호출. 토큰 자체는 서버에 살아있다."""
        if self._flock is not None:
            self._flock.release(force=True)
            self._flock = None
            # 보유자 지문도 함께 지운다. 남겨두면 다음 충돌 때 **이미 죽은 프로세스**를
            # 보유자로 보고해 장애 진단을 오도한다.
            if self._lock_path is not None:
                try:
                    self._holder_path(self._lock_path).unlink()
                except (FileNotFoundError, NotADirectoryError, OSError):
                    pass

    # ---- lease / state --------------------------------------------------

    @staticmethod
    def _now_ms() -> int:
        return int(time.time() * 1000)

    def _is_stale(self, expires_at_ms: int) -> bool:
        return self._now_ms() >= expires_at_ms - REFRESH_MARGIN_MS

    def lock_path(self) -> Path:
        """이 매니저가 잡을 리스 파일 경로 (자격증명 유도, CWD/워크트리 무관)."""
        if self._lock_path is None:
            client_id, _secret = self._read_keys()
            self._lock_path = lease_path_for_client(client_id)
        return self._lock_path

    def _acquire_lease(self) -> None:
        if self._flock is not None and self._flock.is_locked:
            return
        path = self.lock_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        lock = filelock.FileLock(str(path))
        try:
            lock.acquire(timeout=0)
        except filelock.Timeout as exc:
            holder = self._read_holder(path)
            raise RuntimeError(
                f"token lease already held by another process ({path}); "
                f"holder={holder}; refusing to issue — a new token would immediately "
                "kill the live one"
            ) from exc
        self._flock = lock
        self._write_holder(path)

    # 리스 보유자 지문 — 락이 왜 잡혀 있는지 사람이 즉시 알 수 있게 (감사 A-1 제안 3).
    def _holder_path(self, lock_path: Path) -> Path:
        return lock_path.with_suffix(lock_path.suffix + ".holder.json")

    def _write_holder(self, lock_path: Path) -> None:
        try:
            self._holder_path(lock_path).write_text(json.dumps({
                "pid": os.getpid(),
                "cwd": str(Path.cwd()),
                "state_path": str(self.state_path),
                "acquired_at_ms": self._now_ms(),
            }), encoding="utf-8")
        except OSError:
            pass   # 지문은 진단 편의일 뿐 — 실패해도 리스 자체는 유효하다.

    def _read_holder(self, lock_path: Path) -> str:
        try:
            return self._holder_path(lock_path).read_text(encoding="utf-8")
        except OSError:
            return "<unknown>"

    def _read_state(self) -> tuple[str, int] | None:
        try:
            raw = self.state_path.read_text(encoding="utf-8")
        except (FileNotFoundError, NotADirectoryError):
            return None
        try:
            data = json.loads(raw)
            return str(data["token"]), int(data["expires_at_ms"])
        except (ValueError, KeyError, TypeError):
            return None

    def _write_state(self, token: str, expires_at_ms: int) -> None:
        """원자적 교체 + 소유자 전용 권한 (계약 A7 §2). 내용(토큰)은 절대 로그에 남기지 않는다.

        권한을 **임시 파일 단계에서 먼저** 건다. `os.replace` 는 대상 경로의 기존 권한이
        아니라 원본(임시 파일)의 권한을 그대로 가져오므로, 이 순서라면 토큰이 디스크에
        존재하는 어떤 순간에도 넓은 권한으로 노출되는 창이 없다.
        (`mkstemp` 자체가 POSIX 0600 으로 만들지만, 그건 구현 세부라 명시적으로 다시 건다.)
        """
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(self.state_path.parent), suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump({"token": token, "expires_at_ms": expires_at_ms}, fh)
            restrict_to_owner(tmp)          # 교체 **전**에 제한 — 노출 창 없음
            os.replace(tmp, self.state_path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        # 교체 후에도 유지되는지 확인 사살 (일부 파일시스템/도구가 모드를 건드릴 수 있다).
        restrict_to_owner(self.state_path)

    # ---- issuance -------------------------------------------------------

    def _read_keys(self) -> tuple[str, str]:
        """api_keys 파싱. 반환값은 시크릿 — 로그·메시지에 절대 싣지 않는다.

        지원 포맷: `KEY=VALUE` 줄 (dotenv 형태) 또는 JSON 오브젝트.
        """
        raw = self.keys_path.read_text(encoding="utf-8")
        data: dict[str, str] = {}
        stripped = raw.strip()
        if stripped.startswith("{"):
            # json.JSONDecodeError 는 `str(e)` 에는 위치만 담지만 **`e.doc` 에 파일 전문**을
            # 들고 다닌다 (감사 U-4 실측 확인). 예외 객체를 깊게 찍는 로거·리포터가 있으면
            # 그대로 시크릿이 샌다. 우리 문구로 갈아끼우고 `from None` 으로 원본을 끊는다.
            # `from None` 만으로는 부족하다 — 그건 traceback **표시**만 억제하고
            # `__context__` 에 원본 예외(=`.doc` 에 파일 전문)가 그대로 매달려 있다.
            # except 블록 **밖**에서 raise 해야 __context__ 가 아예 None 이 된다.
            parsed = None
            bad_json = False
            try:
                parsed = json.loads(stripped)
            except ValueError:
                bad_json = True
            if bad_json:
                raise RuntimeError(
                    f"{self.keys_path} is not valid JSON "
                    "(내용은 시크릿이라 표시하지 않는다 — 감사 U-4)")
            # 진입 조건이 `startswith("{")` 이므로 파싱에 성공했다면 반드시 dict 다.
            data = {str(k).upper(): str(v) for k, v in parsed.items()}
        else:
            for line in raw.splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, _, v = line.partition("=")
                data[k.strip().upper()] = v.strip().strip('"').strip("'")
        try:
            return data["CLIENT_ID"], data["CLIENT_SECRET"]
        except KeyError as exc:
            raise RuntimeError(
                f"{self.keys_path} missing CLIENT_ID/CLIENT_SECRET (keys found: "
                f"{sorted(data)})"
            ) from exc

    async def _issue(self) -> tuple[str, int]:
        """POST /oauth2/token. live=False 면 시도 자체가 RuntimeError (계약 C-4).

        전송 레벨 이중 차단(GuardedTransport)의 **유일한 예외**다 — 토큰 발급은
        TossClient 가 만들어지기 전에 필요하므로 자체 httpx 클라이언트를 쓴다. 그래서
        `check_allowed` 를 명시적으로 호출하고, 경로는 상수(TOKEN_PATH)로 고정한다.
        """
        if not self.live:
            raise RuntimeError(
                "TokenManager(live=False) refuses to issue a real token; "
                "use the mock fixed token via get()"
            )
        base_url = os.environ.get("TOSS_BASE_URL", "").rstrip("/")
        if not base_url:
            raise RuntimeError("TOSS_BASE_URL is not set (계약 C-9: 기본값 없음)")
        check_allowed("POST", TOKEN_PATH)
        # AUTH 그룹(5 req/s)도 rate limit 대상이다 (감사 A-4). 401 폭풍이 나면 발급 POST 가
        # 아무 제어 없이 연속 발사되던 경로였다.
        if self.limiter is not None:
            await self.limiter.acquire("AUTH")
        client_id, client_secret = self._read_keys()
        try:
            async with httpx.AsyncClient(timeout=15.0) as http:
                resp = await http.post(
                    base_url + TOKEN_PATH,
                    data={
                        "grant_type": "client_credentials",
                        "client_id": client_id,
                        "client_secret": client_secret,
                    },
                    headers={"Content-Type": "application/x-www-form-urlencoded"},
                )
        except httpx.HTTPError as exc:
            raise TransientHTTP(0, f"token request failed: {type(exc).__name__}") from exc

        if resp.status_code >= 500:
            raise TransientHTTP(resp.status_code, "token endpoint 5xx")
        if resp.status_code == 429:
            raise TransientHTTP(429, "token endpoint rate limited")
        if resp.status_code != 200:
            # OAuth2 표준 에러 포맷 (envelope 아님).
            # `error` 는 스펙상 고정 enum 이라 안전하지만 `error_description` 은 **서버가
            # 제어하는 임의 텍스트**다 — 일부 OAuth 구현은 제출한 client_secret 을 그대로
            # 에코하고, 이 메시지는 notifier 를 거쳐 평문 로그 파일에 남는다 (감사 M-1).
            # 그래서 본문은 싣지 않고 길이·해시만 남긴다.
            err = "unknown"
            desc_fingerprint = "none"
            try:
                body = resp.json()
                raw_err = str(body.get("error", err))
                # enum 이외의 값은 그대로 싣지 않는다.
                err = raw_err if raw_err in _OAUTH_ERRORS else "unrecognized"
                desc = body.get("error_description")
                if desc is not None:
                    text = str(desc)
                    digest = hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()[:12]
                    desc_fingerprint = f"len={len(text)} sha256:{digest}"
            except ValueError:
                pass
            detail = (f"error={err} error_description[{desc_fingerprint}] "
                      "(본문 비공개 — 시크릿 에코 방지, 감사 M-1)")
            if resp.status_code == 403 or err == "access_denied":
                raise Forbidden(f"token issuance forbidden: {detail}")
            raise AuthExpired(f"token issuance rejected ({resp.status_code}): {detail}")

        try:
            body = resp.json()
            token = str(body["access_token"])
            expires_in = int(body["expires_in"])
        except (ValueError, KeyError, TypeError) as exc:
            raise AuthExpired(f"malformed token response: {type(exc).__name__}") from exc
        return token, self._now_ms() + expires_in * 1000
