"""TokenManager — 단일 토큰 보장 (계약 C-4).

client당 유효 토큰이 1개뿐이므로(재발급 시 기존 토큰 즉시 무효화),
OS 파일락(filelock) + 상태파일로 전 시스템에서 발급 주체를 1개로 강제한다.
live=False 면 실발급 시도 자체가 RuntimeError (mock 고정 토큰 사용).

락 정책: 라이브 모드에서 첫 get() 시 `{state_path}.lock` 을 **논블로킹으로 획득해
프로세스 수명 내내 보유**한다(리스). 다른 프로세스가 이미 보유 중이면 즉시 RuntimeError —
발급을 강행하면 그 프로세스의 토큰이 즉시 죽기 때문이다.
mock 모드는 락을 잡지 않는다(리스 없는 워커 여럿이 동시에 mock 을 쓰는 것이 정상 동작).

서버 주소는 계약 C-9 의 `TOSS_BASE_URL` env 로 받는다 (TokenManager 시그니처 불변).
"""
from __future__ import annotations

import asyncio
import json
import os
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


class TokenManager:
    def __init__(self, keys_path: Path, state_path: Path, live: bool):
        self.keys_path = Path(keys_path)
        self.state_path = Path(state_path)
        self.live = bool(live)
        self._lock_path = self.state_path.with_suffix(self.state_path.suffix + ".lock")
        self._flock: filelock.FileLock | None = None
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

    async def invalidate(self) -> None:
        """AuthExpired 수신 시 호출. 다음 get()이 재발급."""
        async with self._alock:
            self._token = None
            self._expires_at_ms = 0
            if self.live:
                try:
                    self.state_path.unlink()
                except FileNotFoundError:
                    pass

    def release(self) -> None:
        """리스(파일락) 반납. 프로세스 종료 시 호출. 토큰 자체는 서버에 살아있다."""
        if self._flock is not None:
            self._flock.release(force=True)
            self._flock = None

    # ---- lease / state --------------------------------------------------

    @staticmethod
    def _now_ms() -> int:
        return int(time.time() * 1000)

    def _is_stale(self, expires_at_ms: int) -> bool:
        return self._now_ms() >= expires_at_ms - REFRESH_MARGIN_MS

    def _acquire_lease(self) -> None:
        if self._flock is not None and self._flock.is_locked:
            return
        self._lock_path.parent.mkdir(parents=True, exist_ok=True)
        lock = filelock.FileLock(str(self._lock_path))
        try:
            lock.acquire(timeout=0)
        except filelock.Timeout as exc:
            raise RuntimeError(
                f"token lease already held by another process ({self._lock_path}); "
                "refusing to issue — a new token would immediately kill the live one"
            ) from exc
        self._flock = lock

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
        """원자적 교체. 내용(토큰)은 절대 로그에 남기지 않는다."""
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(self.state_path.parent), suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump({"token": token, "expires_at_ms": expires_at_ms}, fh)
            os.replace(tmp, self.state_path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    # ---- issuance -------------------------------------------------------

    def _read_keys(self) -> tuple[str, str]:
        """api_keys 파싱. 반환값은 시크릿 — 로그·메시지에 절대 싣지 않는다.

        지원 포맷: `KEY=VALUE` 줄 (dotenv 형태) 또는 JSON 오브젝트.
        """
        raw = self.keys_path.read_text(encoding="utf-8")
        data: dict[str, str] = {}
        stripped = raw.strip()
        if stripped.startswith("{"):
            data = {str(k).upper(): str(v) for k, v in json.loads(stripped).items()}
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
        """POST /oauth2/token. live=False 면 시도 자체가 RuntimeError (계약 C-4)."""
        if not self.live:
            raise RuntimeError(
                "TokenManager(live=False) refuses to issue a real token; "
                "use the mock fixed token via get()"
            )
        base_url = os.environ.get("TOSS_BASE_URL", "").rstrip("/")
        if not base_url:
            raise RuntimeError("TOSS_BASE_URL is not set (계약 C-9: 기본값 없음)")
        check_allowed("POST", TOKEN_PATH)
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
            # OAuth2 표준 에러 포맷 (envelope 아님). 시크릿은 담기지 않는 필드만 읽는다.
            err, desc = "unknown", ""
            try:
                body = resp.json()
                err = str(body.get("error", err))
                desc = str(body.get("error_description", ""))
            except ValueError:
                pass
            if resp.status_code == 403 or err == "access_denied":
                raise Forbidden(f"token issuance forbidden: {err} {desc}".strip())
            raise AuthExpired(f"token issuance rejected ({resp.status_code}): {err} {desc}".strip())

        try:
            body = resp.json()
            token = str(body["access_token"])
            expires_in = int(body["expires_in"])
        except (ValueError, KeyError, TypeError) as exc:
            raise AuthExpired(f"malformed token response: {type(exc).__name__}") from exc
        return token, self._now_ms() + expires_in * 1000
