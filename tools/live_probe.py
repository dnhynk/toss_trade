"""라이브 실측 프로브 — 소유: W1. 라이브 리스 보유 시에만 실행.

최소 호출 수로 docs/06_live_facts.md 의 미확인 항목을 실측한다 (목록: ORCHESTRATION_PROMPT §4-W1-4).
각 항목은 "확인됨/미확인 + 근거 응답 스니펫(마스킹)" 으로 기록. 추측 금지.

    # mock 드라이런 (기본값 — 안전한 쪽)
    TOSS_BASE_URL=http://127.0.0.1:8899 python tools/live_probe.py --probe all

    # 라이브 실측 (리스 보유자만)
    TOSS_LIVE=1 TOSS_BASE_URL=https://... python tools/live_probe.py --live --keys api_keys
    python tools/live_probe.py --list

**기본값은 안전한 쪽이다** (감사 B-5). 실토큰 발급은 `--live` 플래그 + `TOSS_LIVE=1` 이
둘 다 있을 때만 일어나고, 그 둘 없이 실서버를 대상으로 하면 실행 자체를 거부한다.
base URL 은 기본값이 없다 (계약 C-9) — `--base-url` 또는 `TOSS_BASE_URL` 필수.

`force429`·`force429_client` 은 **의도적으로 라이브 429 를 유발**하므로 `--probe all` 에
포함되지 않는다. 이름을 명시해야만 실행된다. 같은 자격증명으로 컬렉터가 돌고 있으면
서버 측 같은 버킷의 페널티를 공유하므로, 컬렉터 가동 중에는 실행하지 말 것.

**컬렉터 가동 중에 프로브를 돌려야 하면** `--reuse-token-state <컬렉터의 token_state.json>`
를 쓴다. 이 API 는 client 당 유효 토큰이 1개라 재발급이 곧 가동 중 토큰 살해다 —
이 모드는 발급도, 리스 획득도, 무효화도 하지 않고 **읽어서만** 쓴다.
레이트리밋 계약 프로브(`ratelimit_contract`·`ratelimit_boundary`·`ratelimit_groups`·
`limiter_holds`)는 429 를 내지 않도록 설계돼 있어 이 조합으로 안전하게 돌릴 수 있다.

결과는 stdout(JSON) + `--out` 경로에 쓰고, 응답 스냅샷은 tests/fixtures/live/live_*.json 으로
마스킹해 저장한다.

호출 예산: `--probe all` 기준 약 130회 (그룹별 한도의 70% 이내로 자동 조절됨).
그중 약 60회가 레이트리밋 계약 프로브 4종이고, 이들은 컬렉터가 쓰지 않는
MARKET_INFO(3/s)·STOCK(5/s) 에서만 돈다.
"""
from __future__ import annotations

import argparse
import asyncio
import copy
import datetime as _dt
import hashlib
import json
import os
import re
import sys
import time
from decimal import Decimal
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from tossmon.api.client import TossClient                    # noqa: E402
from tossmon.api.errors import (                             # noqa: E402
    Forbidden,
    RateLimited,
    SchemaMismatch,
    TossApiError,
    TransientHTTP,
)
from tossmon.api.client import GuardedTransport              # noqa: E402
from tossmon.api.endpoints import SPEC_LIMITS, check_allowed  # noqa: E402
from tossmon.api.limiter import GroupRateLimiter             # noqa: E402
from tossmon.api.models import iso_to_ms, ms_to_iso, ms_to_iso_et, ms_to_iso_kst  # noqa: E402
from tossmon.api.tokens import TokenManager, lease_dir       # noqa: E402

FIXTURE_DIR = REPO_ROOT / "tests" / "fixtures" / "live"

# base URL 은 **기본값이 없다** (계약 C-9). `--base-url` 또는 `TOSS_BASE_URL` 로만 받는다.
# 이전의 `LIVE_BASE_URL = "https://" + "openapi..."` 는 기본값이자 grep 회피였다 (감사 M-7).
#
# 아래 상수는 그것과 용도가 다르다 — **오직 판별용**이며 요청 URL 을 만드는 데 쓰지 않는다.
# base_url 이 실서버를 가리키는지 검사해
#   (a) 라이브 플래그 없이 실서버를 때리는 실행을 거부하고
#   (b) 캡처 픽스처를 source="live" 로 표기할지 정한다.
# 이 상수를 없애면 "실서버인지 모르는 채로 때리는" 상태가 되어 오히려 더 위험하다.
LIVE_HOST_MARKER = "openapi.tossinvest.com"

LIMITS = {"AUTH": 5, "STOCK": 5, "MARKET_DATA": 10, "MARKET_DATA_CHART": 5,
          "RANKING": 5, "MARKET_INFO": 3}

# 유동성이 확실한 대형주(시세 진행 관측용) + 소형주(전략 대상 실측용)
LIQUID = "AAPL"
SMALL = ["SNTI", "BTAI", "CRKN"]

DAY_MS = 86_400_000

# 폴링형 프로브의 기본 파라미터. --fast 로 줄여 mock 드라이런에 쓴다.
POLL = {"quote_polls": 6, "quote_gap_s": 12.0, "rank_polls": 4, "rank_gap_s": 20.0}

# group_coupling 의 예산. 승인받은 값이다 (docs/32 §6): 총 120콜 = 30콜 × 4그룹, 2초에 1콜.
# --fast 는 mock 드라이런에서 **줄이는 방향으로만** 쓴다 (라이브 결론에 쓰지 말 것).
COUPLING = {"samples_per_group": 30, "gap_s": 2.0, "off_s": 60.0}
# md_peak_1s 게이트가 닫혀 있을 때 그룹당 최대 대기. telemetry 주기(300초)보다 길어야
# 최소 한 번은 **새** 줄을 보고 판단한다. 넘으면 그 그룹은 스킵으로 기록한다.
GATE_WAIT_MAX_S = 360.0

# ranking_cadence 의 예산. **사전에 고정한 숫자**다 (docs/35 §2).
# 콜 수 = dur_s / gap_s. vol 540 + gain 330 + 단발 6 = 876, 상한 1000 에서 12% 여유.
# 최고 밀도는 1 req/s — 수집기 RANKING 첨두 2 와 겹쳐도 한 초에 3/5 라 한도까지 2 남는다.
CADENCE = {
    # (라벨, 폴 간격 s, 지속 s)
    "vol_arms": [("1s", 1.0, 360.0), ("2s", 2.0, 240.0), ("4s", 4.0, 240.0)],
    "gain_arms": [("1s", 1.0, 240.0), ("2s", 2.0, 120.0), ("4s", 4.0, 120.0)],
    "count": 100,
    # 1s 팔을 솎아 같은 현실을 굵은 자로 다시 잰다. 답이 stride 를 따라 움직이면 우리 자다.
    "strides": (1, 2, 4, 8, 12),
    "call_cap": 1000,      # 하드 상한. 넘으면 그 자리에서 중단한다.
    "arm_gap_s": 5.0,      # 팔 사이 숨돌리기
}
CADENCE_PROFILE = "full"

# `--cadence-profile open` — 정규장(22:30 KST 개장) 재측정용 축소판.
# 프리마켓 결과와 **같은 계측**이라야 세션 비교가 되지 방식 비교가 되지 않는다.
# 그래서 지문·중단조건·분석은 그대로 두고 팔의 길이만 줄인다.
#   - `1s` 300콜: 서버 스탬프 델타를 세션별로 비교하는 본체.
#   - `12s` 25콜: 수집기가 실제로 쓰는 주기를 **같은 세션에서 실제로** 돌려,
#     솎기(같은 응답열 재해석)가 재현하지 못하는 "요청 속도에 대한 서버 반응"을 가른다.
#   - 등락률은 `1s` 120콜만 — 세션 의존성 유무만 보면 되고, 목록 성격은 프리에서 이미 떴다.
# 총 300+25+120+단발 4+겹침 4 = 453콜, 상한 550.
CADENCE_PROFILES: dict[str, dict] = {
    "open": {
        "vol_arms": [("1s", 1.0, 300.0), ("12s", 12.0, 300.0)],
        "gain_arms": [("1s", 1.0, 120.0)],
        "call_cap": 550,
    },
}

# 마스킹 대상 필드 (계약 C-11 §5)
MASK_FIELDS = {"accountNo", "accountSeq", "access_token", "requestId", "isinCode"}
MASK_HEADERS = {"authorization", "set-cookie", "x-amz-cf-id", "x-request-id"}

# save_fixture 가 source 를 판정하기 위한 플래그 (run() 에서 설정).
_IS_LIVE_TARGET = [False]

# group_coupling 이 배경 429 대조군을 뜨기 위해 **읽기만** 하는 가동 중 컬렉터 로그 (run() 에서 설정).
COLLECTOR_LOG: Path | None = None
BASELINE_MIN = 180.0


def default_state_path() -> Path:
    """토큰 상태파일 기본 경로 — 리스와 같은 리포 밖 절대경로 (감사 A-1).

    상대경로 기본값(`data/token_state.json`)은 CWD 종속이라 워크트리마다 다른 파일을
    가리켰다. 리스는 이제 자격증명에서 유도되므로 상태파일이 갈라져도 토큰 살해로는
    이어지지 않지만, 갈라질 이유 자체가 없다.
    """
    return lease_dir() / "token_state.json"


# ------------------------------------------------------------------ 유틸


def now_ms() -> int:
    return int(time.time() * 1000)


def _as_int(raw) -> int | None:
    """헤더 값을 int 로. 없거나 비수치면 None."""
    try:
        return int(str(raw).strip())
    except (TypeError, ValueError):
        return None


class CollectorWatch:
    """가동 중 컬렉터 로그를 **읽기만** 해서 배경 429 를 초 단위로 뜬다 (docs/32 §9).

    왜 telemetry 카운터가 아니라 로그 줄인가:
    `TELEMETRY_EVERY_S = 300` 이라 `http_429` 카운터는 **5분에 한 번** 갱신된다.
    4분짜리 프로브는 그 해상도로는 통째로 한 칸 안에 들어가 분해되지 않는다.
    반면 `budget: 429 on <group> (count=N)` 은 429 **한 건마다 ms 타임스탬프**로 찍히므로
    프로브 구간과 대조 구간을 초 단위로 가를 수 있다. 현상의 시간축은 초다.
    """

    # 로그 포맷은 레벨 이름을 폭 맞춰 채운다 ("INFO    ") — 공백을 하나로 보면 안 된다.
    _EVT = re.compile(
        r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d),(\d{3}) \w+\s+budget: 429 on ([A-Z_]+) \(count=(\d+)\)")
    _TEL = re.compile(r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d),(\d{3}) \w+\s+telemetry (.*)$")
    _TAIL_BYTES = 8 << 20

    def __init__(self, path: Path):
        self.path = path

    def _tail_lines(self) -> list[str]:
        with open(self.path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            fh.seek(max(0, size - self._TAIL_BYTES))
            raw = fh.read()
        text = raw.decode("utf-8", errors="replace")
        lines = text.splitlines()
        return lines[1:] if size > self._TAIL_BYTES else lines

    @staticmethod
    def _stamp(m: re.Match) -> float:
        t = _dt.datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S")
        return t.timestamp() + int(m.group(2)) / 1000.0

    def events(self, since_epoch: float = 0.0) -> list[dict]:
        """`since_epoch` 이후의 배경 429 이벤트 (epoch 초, 로컬시계 = 컬렉터와 같은 시계)."""
        out = []
        for line in self._tail_lines():
            m = self._EVT.match(line)
            if not m:
                continue
            ts = self._stamp(m)
            if ts >= since_epoch:
                out.append({"epoch": ts, "kst": m.group(1) + f".{m.group(2)}",
                            "group": m.group(3), "count": int(m.group(4))})
        return out

    def latest_telemetry(self) -> dict | None:
        """가장 최근 telemetry 줄의 파싱 결과 + 그 줄의 나이(초). 최대 300초 낡을 수 있다."""
        found = None
        for line in self._tail_lines():
            m = self._TEL.match(line)
            if m:
                found = m
        if not found:
            return None
        fields = {}
        for tok in found.group(3).split("|")[0].split():
            if "=" in tok:
                k, v = tok.split("=", 1)
                fields[k] = v
        ts = self._stamp(found)
        return {"at_kst": found.group(1), "age_s": round(time.time() - ts, 1),
                "md_peak_1s": _as_int(fields.get("md_peak_1s")),
                "chart_peak_1s": _as_int(fields.get("chart_peak_1s")),
                "http_429": _as_int(fields.get("http_429")),
                "over_limit_1s": _as_int(fields.get("over_limit_1s")),
                "tier2": _as_int(fields.get("tier2")), "tier3": _as_int(fields.get("tier3"))}

    def baseline(self, minutes: float) -> dict:
        """프로브 **이전** 구간의 배경 429 발생률. 대조군의 정의다.

        기록해 두지 않으면 나중에 "프로브가 429 를 늘렸나" 에 아무도 답할 수 없다.
        임계값도 여기서 나온다 — 관측된 배경 최대치보다 **엄격히 위**로 잡아야
        배경 버스트에 걸려 헛종료하지 않는다.
        """
        now = time.time()
        evs = self.events(now - minutes * 60.0)
        n = len(evs)
        span_min = minutes if n < 2 else (evs[-1]["epoch"] - evs[0]["epoch"]) / 60.0
        # 관측된 배경의 롤링 최대치 (임계값의 근거)
        worst = {}
        for w in (60, 480):
            best = 0
            for e in evs:
                c = sum(1 for e2 in evs if 0 <= e2["epoch"] - e["epoch"] < w)
                best = max(best, c)
            worst[f"max_in_{w}s"] = best
        return {
            "window_min": minutes, "n": n,
            "rate_per_min": round(n / minutes, 4),
            "first_kst": evs[0]["kst"] if evs else None,
            "last_kst": evs[-1]["kst"] if evs else None,
            "observed_span_min": round(span_min, 1),
            "groups": sorted({e["group"] for e in evs}),
            **worst,
        }


def _use_utf8_stdio() -> None:
    """Windows 콘솔 기본 cp949 대응. **출력이 있는 모든 경로에서 먼저 부른다.**

    `--list` 는 이걸 안 거치고 바로 출력해서, 프로브 docstring 첫 줄에 em dash 하나만
    들어가도 UnicodeEncodeError 로 죽었다. 출력 인코딩은 무엇을 찍느냐와 무관해야 한다.
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass


def _masked_value(v):
    """타입 보존 마스킹 — 스키마가 깨지면 픽스처로 못 쓴다."""
    if isinstance(v, bool):
        return False
    if isinstance(v, int):
        return 0
    if isinstance(v, float):
        return 0.0
    return "***MASKED***"


def mask(obj):
    """응답에서 계좌/토큰/추적 식별자를 마스킹 (타입은 유지)."""
    if isinstance(obj, dict):
        return {k: (_masked_value(v) if k in MASK_FIELDS else mask(v))
                for k, v in obj.items()}
    if isinstance(obj, list):
        return [mask(x) for x in obj]
    return obj


def mask_headers(headers: dict) -> dict:
    out = {}
    for k, v in headers.items():
        if k.lower() in MASK_HEADERS:
            out[k] = "***MASKED***"
        elif k.lower().startswith(("x-ratelimit", "retry-after", "content-type", "date")):
            out[k] = v
    return out


def save_fixture(name: str, endpoint: str, case: str, status: int, body,
                 headers: dict | None = None, note: str = "", match: dict | None = None) -> None:
    FIXTURE_DIR.mkdir(parents=True, exist_ok=True)
    fx = {
        "endpoint": endpoint,
        "case": case,
        "status": status,
        # mock 드라이런 결과가 라이브 실측으로 둔갑하지 않도록 base_url 로 판정한다.
        "source": "live" if _IS_LIVE_TARGET[0] else "synthetic",
        "captured_at_ms": now_ms(),
        "note": note,
    }
    if match:
        fx["match"] = match
    if headers:
        fx["headers"] = mask_headers(headers)
    fx["body"] = mask(body)
    (FIXTURE_DIR / name).write_text(
        json.dumps(fx, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def snippet(obj, limit: int = 700) -> str:
    """docs/06 근거용 스니펫 (마스킹 후 잘라내기)."""
    return json.dumps(mask(obj), ensure_ascii=False)[:limit]


class ReusedTokenManager:
    """**이미 발급된 토큰을 읽어서만 쓰는** TokenManager 대체물 (`--reuse-token-state`).

    컬렉터가 가동 중일 때 프로브를 돌려야 하는 상황을 위한 것이다. 이 API 는 client 당
    유효 토큰이 1개뿐이라 **새로 발급하면 컬렉터의 토큰이 즉시 죽는다.** 그래서:

    - 리스(파일락)를 **잡지 않는다** — 보유자는 컬렉터다.
    - 발급을 **하지 않는다** (`_issue` 자체가 없다).
    - `invalidate()` 는 **아무 것도 하지 않는다** — 특히 상태파일을 지우지 않는다.
      지우면 컬렉터가 다음 갱신 때 재발급을 하게 되어 결국 남의 토큰을 죽인 것과 같다.

    만료가 임박했으면 실행을 거부한다 — 프로브 도중 만료되면 401 이 나고, 그 401 은
    컬렉터의 정상 갱신과 구분되지 않는 소음이 된다.
    """

    MIN_REMAINING_MS = 120_000

    def __init__(self, state_path: Path):
        self.state_path = Path(state_path).expanduser().resolve()
        self.limiter = None
        raw = json.loads(self.state_path.read_text(encoding="utf-8"))
        self._token = str(raw["token"])
        self.expires_at_ms = int(raw["expires_at_ms"])
        left = self.expires_at_ms - now_ms()
        if left < self.MIN_REMAINING_MS:
            raise RuntimeError(
                f"reused token expires in {left / 1000:.0f}s (< "
                f"{self.MIN_REMAINING_MS / 1000:.0f}s) — 프로브 중 만료된다. "
                "재발급은 가동 중 컬렉터의 토큰을 죽이므로 하지 않는다.")

    async def get(self) -> str:
        return self._token

    async def invalidate(self, token: str | None = None) -> None:
        print("[probe] 401 received but this run reuses another process' token — "
              "not invalidating (상태파일을 건드리지 않는다).", file=sys.stderr)

    def release(self) -> None:
        pass


def trunc_result(body: dict, n: int = 3) -> dict:
    """근거 스니펫용으로 result 배열을 앞 n개만 남긴다."""
    b = copy.deepcopy(body)
    r = b.get("result")
    if isinstance(r, list):
        b["result"] = r[:n]
        b["_truncated_from"] = len(r)
    elif isinstance(r, dict):
        for key in ("candles", "rankings", "bids", "asks"):
            if isinstance(r.get(key), list):
                r[f"_{key}_len"] = len(r[key])
                r[key] = r[key][:n]
    return b


# ---------------------------------------------------------------- 프로브


async def probe_calendar(c: TossClient) -> dict:
    """`/market-calendar/US` 실제 응답 + 현재 세션 판정."""
    body = await c._request("GET", "/api/v1/market-calendar/US")
    save_fixture("live_market_calendar_us.json", "GET /api/v1/market-calendar/US",
                 "default", 200, body, c.last_headers, "라이브 캡처")
    cal = await c.get_us_calendar()
    t = now_ms()
    session = "closed"
    today = cal["today"]
    for name in ("day", "pre", "regular", "after"):
        w = getattr(today, name)
        if w and w.start_ms <= t < w.end_ms:
            session = name
    return {
        "verdict": "확인됨",
        "session_now": session,
        "now_kst": ms_to_iso_kst(t),
        "now_et": ms_to_iso_et(t),
        "today_date": today.date,
        "sessions_today": {
            n: (None if getattr(today, n) is None else
                [ms_to_iso_kst(getattr(today, n).start_ms), ms_to_iso_kst(getattr(today, n).end_ms)])
            for n in ("day", "pre", "regular", "after")
        },
        "keys": sorted(cal),
        "evidence": snippet(trunc_result(body)),
    }


async def probe_quote_realtime(c: TossClient, polls: int | None = None,
                               gap_s: float | None = None) -> dict:
    """미국 시세 실시간 여부/지연 — 같은 심볼을 시간차 폴링해 timestamp 진행을 본다."""
    polls = POLL["quote_polls"] if polls is None else polls
    gap_s = POLL["quote_gap_s"] if gap_s is None else gap_s
    symbols = [LIQUID] + SMALL
    obs: list[dict] = []
    first_body = None
    for i in range(polls):
        wall = now_ms()
        body = await c._request("GET", "/api/v1/prices",
                                params={"symbols": ",".join(symbols)})
        if first_body is None:
            first_body = body
        row = {"wall_ms": wall}
        for item in body.get("result", []):
            ts = item.get("timestamp")
            row[item["symbol"]] = {
                "ts": ts,
                "lag_s": None if ts is None else round((wall - iso_to_ms(ts)) / 1000, 1),
                "last": item.get("lastPrice"),
            }
        obs.append(row)
        if i < polls - 1:
            await asyncio.sleep(gap_s)

    save_fixture("live_prices_us.json", "GET /api/v1/prices", "default", 200,
                 first_body, c.last_headers,
                 "라이브 캡처 — mock 의 심볼 카탈로그로도 쓰인다")

    per_symbol = {}
    for sym in symbols:
        tss = [o[sym]["ts"] for o in obs if sym in o and o[sym]["ts"]]
        lags = [o[sym]["lag_s"] for o in obs if sym in o and o[sym]["lag_s"] is not None]
        lasts = [o[sym]["last"] for o in obs if sym in o]
        uniq_ts = sorted(set(tss))
        per_symbol[sym] = {
            "distinct_timestamps": len(uniq_ts),
            "timestamp_advanced": len(uniq_ts) > 1,
            "price_changed": len(set(lasts)) > 1,
            "lag_s_min": min(lags) if lags else None,
            "lag_s_max": max(lags) if lags else None,
            "lag_s_median": sorted(lags)[len(lags) // 2] if lags else None,
            "sample_ts": uniq_ts[-3:],
        }
    liquid = per_symbol.get(LIQUID, {})
    med = liquid.get("lag_s_median")
    if med is None:
        verdict, note = "미확인", "timestamp 가 전부 null (해당 세션에 체결 없음)"
    elif med > 600:
        verdict, note = "확인됨", f"지연 시세로 관측됨 (중앙 지연 {med}s ≈ {med/60:.1f}분)"
    elif liquid.get("timestamp_advanced"):
        verdict, note = "확인됨", f"실시간으로 관측됨 (중앙 지연 {med}s, 폴링마다 timestamp 진행)"
    else:
        verdict, note = "부분확인", f"중앙 지연 {med}s 이나 관측 구간에서 timestamp 진행 없음"
    return {
        "verdict": verdict, "note": note,
        "polls": polls, "gap_s": gap_s,
        "per_symbol": per_symbol,
        "evidence": snippet(trunc_result(first_body)),
    }


async def _candles_at(c: TossClient, symbol: str, interval: str, before_ms: int | None,
                      count: int = 1) -> tuple[list, dict]:
    params = {"symbol": symbol, "interval": interval, "count": count, "adjusted": "true"}
    if before_ms is not None:
        params["before"] = ms_to_iso(before_ms)
    body = await c._request("GET", "/api/v1/candles", params=params)
    return (body.get("result") or {}).get("candles") or [], body


async def probe_candle_retention(c: TossClient) -> dict:
    """`/candles 1m` 과거 보관 기간 — `before` 역방향 지수탐색 + 이분탐색 (Phase 1 핵심 변수)."""
    calls = 0
    newest, newest_body = await _candles_at(c, LIQUID, "1m", None, count=200)
    calls += 1
    if not newest:
        return {"verdict": "미확인", "note": "최신 1m 페이지가 비어 있음", "calls": calls}
    newest_ts = max(iso_to_ms(x["timestamp"]) for x in newest)
    oldest_in_page = min(iso_to_ms(x["timestamp"]) for x in newest)
    next_before = (newest_body.get("result") or {}).get("nextBefore")

    save_fixture("live_candles_1m.json", "GET /api/v1/candles", "default", 200,
                 newest_body, c.last_headers,
                 f"{LIQUID} 1m x{len(newest)} 라이브 캡처", match={"interval": "1m"})

    # `before` 가 UTC 오프셋을 받아주는지 확인 (우리는 ms→UTC ISO 로 직렬화한다)
    utc_probe, _ = await _candles_at(c, LIQUID, "1m", newest_ts - 3_600_000, count=2)
    calls += 1
    utc_offset_ok = bool(utc_probe)

    # before 경계가 inclusive 인지 (스펙은 inclusive 라고 명시 — 실측으로 확인)
    edge, _ = await _candles_at(c, LIQUID, "1m", newest_ts, count=1)
    calls += 1
    before_inclusive = bool(edge) and iso_to_ms(edge[0]["timestamp"]) == newest_ts

    # 지수 탐색: 며칠 전까지 봉이 나오는가
    hits: dict[int, str] = {}
    misses: list[int] = []
    lo_days, hi_days = 0, None
    for days in (1, 2, 4, 8, 16, 32, 64, 128, 256, 512):
        got, _ = await _candles_at(c, LIQUID, "1m", newest_ts - days * DAY_MS, count=1)
        calls += 1
        if got:
            hits[days] = got[0]["timestamp"]
            lo_days = days
        else:
            misses.append(days)
            hi_days = days
            break

    # 이분 탐색으로 경계 좁히기 (하루 단위)
    if hi_days is not None:
        lo, hi = lo_days, hi_days
        while hi - lo > 1:
            mid = (lo + hi) // 2
            got, _ = await _candles_at(c, LIQUID, "1m", newest_ts - mid * DAY_MS, count=1)
            calls += 1
            if got:
                hits[mid] = got[0]["timestamp"]
                lo = mid
            else:
                misses.append(mid)
                hi = mid
        boundary = {"last_day_with_data": lo, "first_day_without_data": hi}
        verdict = "확인됨"
        note = f"1m 봉은 약 {lo}일 전까지 조회됨 ({lo+1}일 전부터 빈 응답)"
    else:
        boundary = {"last_day_with_data": lo_days, "first_day_without_data": None}
        verdict = "확인됨"
        note = f"512일 전까지도 1m 봉이 반환됨 (상한 미발견, 최소 {lo_days}일 보관)"

    oldest_seen_ms = None
    if hits:
        oldest_seen_ms = min(iso_to_ms(v) for v in hits.values())

    return {
        "verdict": verdict, "note": note, "calls": calls,
        "symbol": LIQUID,
        "newest_page_bars": len(newest),
        "newest_bar_kst": ms_to_iso_kst(newest_ts),
        "page_span_minutes": round((newest_ts - oldest_in_page) / 60000, 1),
        "nextBefore_raw": next_before,
        "before_param_utc_offset_accepted": utc_offset_ok,
        "before_is_inclusive": before_inclusive,
        "retention_probe_days_hit": sorted(hits),
        "retention_probe_days_miss": sorted(misses),
        "boundary_days": boundary,
        "oldest_bar_seen_kst": None if oldest_seen_ms is None else ms_to_iso_kst(oldest_seen_ms),
        "evidence": snippet(trunc_result(newest_body)),
    }


async def probe_candle_1d(c: TossClient) -> dict:
    """1일봉 보관 기간(베이스라인용 200봉 확보 가능 여부)."""
    bars, body = await _candles_at(c, LIQUID, "1d", None, count=200)
    if not bars:
        return {"verdict": "미확인", "note": "1d 응답 비어 있음"}
    ts = sorted(iso_to_ms(x["timestamp"]) for x in bars)
    save_fixture("live_candles_1d.json", "GET /api/v1/candles", "daily", 200, body,
                 c.last_headers, f"{LIQUID} 1d x{len(bars)} 라이브 캡처",
                 match={"interval": "1d"})
    return {
        "verdict": "확인됨",
        "bars": len(bars),
        "oldest_kst": ms_to_iso_kst(ts[0]),
        "newest_kst": ms_to_iso_kst(ts[-1]),
        "span_days": round((ts[-1] - ts[0]) / DAY_MS, 1),
        "nextBefore_raw": (body.get("result") or {}).get("nextBefore"),
        "evidence": snippet(trunc_result(body)),
    }


async def probe_orderbook(c: TossClient) -> dict:
    """미국 `/orderbook` 레벨 수 + 스프레드."""
    out = {}
    saved = False
    for sym in [LIQUID] + SMALL[:2]:
        try:
            body = await c._request("GET", "/api/v1/orderbook", params={"symbol": sym})
        except TossApiError as exc:
            out[sym] = {"error": f"{type(exc).__name__}: {exc}"}
            continue
        result = body.get("result") or {}
        bids, asks = result.get("bids") or [], result.get("asks") or []
        if not saved and (bids or asks):
            save_fixture("live_orderbook_us.json", "GET /api/v1/orderbook", "default", 200,
                         body, c.last_headers, f"{sym} 라이브 캡처")
            saved = True
        entry = {
            "bid_levels": len(bids), "ask_levels": len(asks),
            "timestamp": result.get("timestamp"),
            "bids_price_order": _order_of([b.get("price") for b in bids]),
            "asks_price_order": _order_of([a.get("price") for a in asks]),
        }
        if bids and asks:
            best_bid = max(Decimal(str(b["price"])) for b in bids)
            best_ask = min(Decimal(str(a["price"])) for a in asks)
            entry["best_bid"] = str(best_bid)
            entry["best_ask"] = str(best_ask)
            entry["spread"] = str(best_ask - best_bid)
        out[sym] = entry
    levels = [v["bid_levels"] for v in out.values() if isinstance(v.get("bid_levels"), int)]
    if not levels:
        verdict, note = "미확인", "모든 심볼에서 호가 조회 실패"
    elif max(levels) == 0:
        verdict, note = "부분확인", "응답은 오지만 이 세션에서 호가 배열이 비어 있음"
    else:
        verdict = "확인됨"
        note = (f"미국 호가 레벨 수 = {sorted(set(levels))} (관측 심볼 {len(levels)}개)")
    return {"verdict": verdict, "note": note, "per_symbol": out}


def _order_of(prices: list) -> str:
    if len(prices) < 2:
        return "n/a"
    vals = [Decimal(str(p)) for p in prices]
    if all(vals[i] > vals[i + 1] for i in range(len(vals) - 1)):
        return "descending"
    if all(vals[i] < vals[i + 1] for i in range(len(vals) - 1)):
        return "ascending"
    return "unsorted"


async def probe_trades(c: TossClient) -> dict:
    """`/trades` 실제 반환 건수·지연."""
    out = {}
    saved = False
    for sym in [LIQUID] + SMALL[:2]:
        wall = now_ms()
        try:
            body = await c._request("GET", "/api/v1/trades",
                                    params={"symbol": sym, "count": 50})
        except TossApiError as exc:
            out[sym] = {"error": f"{type(exc).__name__}: {exc}"}
            continue
        rows = body.get("result") or []
        if not saved and rows:
            save_fixture("live_trades_us.json", "GET /api/v1/trades", "default", 200,
                         body, c.last_headers, f"{sym} 라이브 캡처")
            saved = True
        tss = [iso_to_ms(r["timestamp"]) for r in rows if r.get("timestamp")]
        out[sym] = {
            "requested": 50, "returned": len(rows),
            "newest_lag_s": round((wall - max(tss)) / 1000, 1) if tss else None,
            "span_s": round((max(tss) - min(tss)) / 1000, 1) if len(tss) > 1 else None,
            "order": ("descending" if len(tss) > 1 and tss[0] > tss[-1]
                      else "ascending" if len(tss) > 1 else "n/a"),
            "sub_second_ts": any("." in (r.get("timestamp") or "") for r in rows[:5]),
        }
    counts = [v["returned"] for v in out.values() if isinstance(v.get("returned"), int)]
    if not counts or max(counts) == 0:
        verdict, note = "부분확인", "이 세션에서 체결 내역이 비어 있음 (엔드포인트는 200 응답)"
    else:
        verdict, note = "확인됨", f"count=50 요청 시 실제 반환 {sorted(set(counts))}건"
    return {"verdict": verdict, "note": note, "per_symbol": out}


async def probe_rankings(c: TossClient, polls: int | None = None,
                         gap_s: float | None = None) -> dict:
    """TOP_GAINERS realtime 400 여부 + 거래대금/거래량 realtime 갱신 주기."""
    polls = POLL["rank_polls"] if polls is None else polls
    gap_s = POLL["rank_gap_s"] if gap_s is None else gap_s
    findings: dict = {}

    # 1) TOP_GAINERS / TOP_LOSERS realtime → 400 여부
    for rtype in ("TOP_GAINERS", "TOP_LOSERS"):
        try:
            body = await c._request("GET", "/api/v1/rankings", params={
                "type": rtype, "marketCountry": "US", "duration": "realtime", "count": 10})
            findings[f"{rtype}_realtime"] = {
                "verdict": "확인됨", "http": c.last_status,
                "note": "400 이 아니라 정상 응답 — docs/01 §3.2 전제와 다름!",
                "evidence": snippet(trunc_result(body)),
            }
            save_fixture(f"live_rankings_{rtype.lower()}_realtime.json",
                         "GET /api/v1/rankings", f"{rtype.lower()}_realtime", 200, body,
                         c.last_headers, "realtime 이 허용됨(!)",
                         match={"type": rtype, "duration": "realtime"})
        except SchemaMismatch as exc:
            findings[f"{rtype}_realtime"] = {
                "verdict": "확인됨", "http": c.last_status,
                "note": f"거부됨 — {exc.detail}",
                "evidence": exc.detail,
            }
            save_fixture(f"live_rankings_{rtype.lower()}_realtime_400.json",
                         "GET /api/v1/rankings", f"{rtype.lower()}_realtime_400",
                         c.last_status or 400,
                         {"error": {"requestId": "***MASKED***",
                                    "code": "unsupported-ranking-duration",
                                    "message": exc.detail}},
                         c.last_headers, "realtime 미지원 확인",
                         match={"type": rtype, "duration": "realtime"})

    # 2) realtime 랭킹 갱신 주기 — rankedAt 이 언제 바뀌는지
    for rtype in ("MARKET_TRADING_AMOUNT", "TOSS_SECURITIES_TRADING_AMOUNT"):
        seen: list[tuple[int, str | None, str]] = []
        first_body = None
        for i in range(polls):
            wall = now_ms()
            body = await c._request("GET", "/api/v1/rankings", params={
                "type": rtype, "marketCountry": "US", "duration": "realtime", "count": 100})
            if first_body is None:
                first_body = body
            result = body.get("result") or {}
            rows = result.get("rankings") or []
            seen.append((wall, result.get("rankedAt"),
                         ",".join(r["symbol"] for r in rows[:5])))
            if i < polls - 1:
                await asyncio.sleep(gap_s)
        if first_body is not None:
            save_fixture(f"live_rankings_{rtype.lower()}.json", "GET /api/v1/rankings",
                         "default" if rtype == "MARKET_TRADING_AMOUNT" else rtype.lower(),
                         200, first_body, c.last_headers, "라이브 캡처",
                         match={"type": rtype})
        stamps = [s[1] for s in seen]
        uniq = [s for i, s in enumerate(stamps) if i == 0 or s != stamps[i - 1]]
        deltas = []
        for i in range(1, len(seen)):
            if seen[i][1] and seen[i - 1][1] and seen[i][1] != seen[i - 1][1]:
                deltas.append(round((iso_to_ms(seen[i][1]) - iso_to_ms(seen[i - 1][1])) / 1000, 1))
        rows0 = (first_body.get("result") or {}).get("rankings") or []
        findings[rtype] = {
            "verdict": "확인됨" if stamps and stamps[0] else "부분확인",
            "rows_returned": len(rows0),
            "polls": polls, "gap_s": gap_s,
            "rankedAt_sequence": stamps,
            "distinct_rankedAt": len(set(x for x in stamps if x)),
            "rankedAt_deltas_s": deltas,
            "top5_changed": len(set(s[2] for s in seen)) > 1,
            "rankedAt_lag_s": (round((seen[0][0] - iso_to_ms(stamps[0])) / 1000, 1)
                               if stamps and stamps[0] else None),
            "note": (f"{polls}회 폴링({gap_s}s 간격) 중 rankedAt 이 "
                     f"{len(set(x for x in stamps if x))}개 값으로 관측됨"),
            "evidence": snippet(trunc_result(first_body)),
        }
    return findings


def _h(parts: list[str]) -> str:
    return hashlib.sha1("|".join(parts).encode("utf-8")).hexdigest()[:12]


def _rank_digest(body: dict) -> dict:
    """응답 한 통을 **비교 가능한 지문들**로 줄인다.

    "바뀌었다" 의 정의가 하나가 아니라서 지문도 하나가 아니다 (docs/35 §3):
    `order` 는 순위 뒤바뀜(신규 진입·이탈 포함), `volume` 은 거래량 수치,
    `all` 은 행의 어떤 필드든. 셋은 서로 다른 속도로 바뀌므로 따로 세야 한다.
    `rankedAt` 은 우리 지문이 아니라 **서버가 스스로 찍은 계산 시각**이다 —
    우리 폴 주기와 무관한 유일한 눈금이라 여기서 가장 값진 필드다.
    """
    result = body.get("result") or {}
    rows = result.get("rankings") or []

    def px(r: dict, k: str) -> str:
        return str((r.get("price") or {}).get(k))

    syms = [str(r.get("symbol")) for r in rows]
    return {
        "n": len(rows),
        "rankedAt": result.get("rankedAt"),
        "order": _h(syms),
        "volume": _h([f"{r.get('symbol')}={r.get('tradingVolume')}" for r in rows]),
        "last": _h([f"{r.get('symbol')}={px(r, 'lastPrice')}" for r in rows]),
        "rate": _h([f"{r.get('symbol')}={px(r, 'changeRate')}" for r in rows]),
        "all": _h([json.dumps(r, sort_keys=True, ensure_ascii=False) for r in rows]),
        "top3": syms[:3],
    }


def _stats(xs: list[float]) -> dict | None:
    if not xs:
        return None
    s = sorted(xs)
    n = len(s)
    return {"n": n, "min": s[0], "p50": s[n // 2], "max": s[-1],
            "mean": round(sum(s) / n, 2)}


SIGNALS = ("rankedAt", "order", "volume", "last", "rate", "all")


def _analyze_arm(polls: list[dict], stride: int = 1) -> dict:
    """한 팔의 폴 열에서 신호별 변화 간격을 뜬다. `stride` 로 솎으면 자를 굵게 한 것."""
    seq = [p for p in polls if "error" not in p][::stride]
    out: dict = {"polls": len(seq), "stride": stride}
    if len(seq) < 2:
        out["note"] = "표본 부족"
        return out
    span_s = seq[-1]["sent_epoch"] - seq[0]["sent_epoch"]
    out["span_s"] = round(span_s, 1)
    out["actual_gap_s"] = _stats([round(seq[i]["sent_epoch"] - seq[i - 1]["sent_epoch"], 3)
                                  for i in range(1, len(seq))])
    for sig in SIGNALS:
        vals = [p.get(sig) for p in seq]
        idx = [i for i in range(1, len(vals)) if vals[i] != vals[i - 1]]
        ivs = [round(seq[idx[k]]["sent_epoch"] - seq[idx[k - 1]]["sent_epoch"], 2)
               for k in range(1, len(idx))]
        rec = {
            "distinct": len({v for v in vals if v is not None}),
            "changes": len(idx),
            "changes_per_min": round(len(idx) / (span_s / 60.0), 2) if span_s > 0 else None,
            # 관측 간격은 **우리 자보다 고울 수 없다.** 이 값이 stride 를 따라 늘면 별칭이다.
            "observed_interval_s": _stats(ivs),
        }
        if sig == "rankedAt":
            # 서버 스탬프끼리의 차 — 우리 자에 갇히지 않는 유일한 숫자.
            sd = []
            for k in range(1, len(idx)):
                a, b = vals[idx[k - 1]], vals[idx[k]]
                if a and b:
                    sd.append(round((iso_to_ms(b) - iso_to_ms(a)) / 1000.0, 2))
            rec["server_stamp_delta_s"] = _stats(sd)
            rec["server_stamp_delta_hist"] = dict(sorted(
                {v: sd.count(v) for v in set(sd)}.items())) if sd else None
            lags = [round(p["sent_epoch"] - iso_to_ms(p["rankedAt"]) / 1000.0, 1)
                    for p in seq if p.get("rankedAt")]
            rec["stamp_lag_s"] = _stats(lags)
        out[sig] = rec
    # rankedAt 이 내용 변화의 믿을 만한 표지인가 — 아니면 서버 스탬프를 못 믿는다.
    ra, al = [p.get("rankedAt") for p in seq], [p.get("all") for p in seq]
    tab = {"both": 0, "stamp_only": 0, "content_only": 0, "neither": 0}
    for i in range(1, len(seq)):
        rc, ac = ra[i] != ra[i - 1], al[i] != al[i - 1]
        tab["both" if (rc and ac) else "stamp_only" if rc
            else "content_only" if ac else "neither"] += 1
    out["stamp_vs_content"] = tab
    return out


async def _cadence_arm(c: TossClient, params: dict, gap_s: float, dur_s: float,
                       guard) -> tuple[list[dict], str | None]:
    """절대 격자(t0 + i·gap)에 맞춰 폴링한다.

    `sleep(gap)` 을 쓰면 매 회 RTT(실측 p50 71ms)가 누적돼 "1초 간격" 이 1.07초가 된다.
    변화 간격을 재는 일에서 자가 조금씩 늘어나는 것은 그대로 결과의 편향이다.
    """
    n = int(round(dur_s / gap_s))
    t0 = time.monotonic()
    polls: list[dict] = []
    for i in range(n):
        target = t0 + i * gap_s
        if (wait := target - time.monotonic()) > 0:
            await asyncio.sleep(wait)
        if (stop := guard()):
            return polls, stop
        sent = time.time()
        m0 = time.monotonic()
        try:
            body = await c._request("GET", "/api/v1/rankings", params=params)
        except TossApiError as exc:
            polls.append({"i": i, "sent_epoch": round(sent, 3),
                          "error": f"{type(exc).__name__}: {exc}"})
            continue
        d = _rank_digest(body)
        d.update(i=i, sent_epoch=round(sent, 3),
                 rtt_ms=round((time.monotonic() - m0) * 1000, 1))
        polls.append(d)
    return polls, None


async def probe_ranking_cadence(c: TossClient) -> dict:
    """서버가 랭킹을 **실제로** 몇 초마다 바꾸는가 + `TOP_GAINERS` 가 쓸 만한가 (docs/35).

    (D-8 을 숫자로 바꾸는 측정. 승인 후에만 실행한다.)

    **이 프로브가 존재하는 이유**: DB 로 잰 "12.5초마다 바뀐다" 는 우리가 12.4초마다
    찍기 때문에 나온 숫자다. 표본 간격보다 고운 변화는 원리상 보이지 않는다.
    게다가 `rankings_snap` 은 서버 스탬프 `rankedAt` 을 **저장하지 않는다** — 응답에
    답이 들어 있는데 쓰기 단계에서 버리므로, DB 만 보는 한 이 질문은 영원히 못 푼다.

    별칭(aliasing)을 가르는 장치를 **둘** 넣는다. 하나만으로는 부족하다:

    1. **솎기(decimation)** — 1초 팔 하나를 stride 2·4·8·12 로 솎아 같은 현실을 굵은
       자로 다시 잰다. 답이 stride 를 따라 움직이면 그건 자다. 같은 시각·같은 응답열을
       쓰므로 시간대 교란이 없다.
    2. **진짜 다른 주기의 팔** — 1s·2s·4s 를 실제로 따로 돈다. 솎기가 못 보는 것을 본다:
       서버·CDN 이 **요청 속도에 반응**해 캐시를 물리면 솎기에서는 흔적이 안 남는다.

    셋 다 따로 낸다: `order`(순위 뒤바뀜) / `volume`(거래량 수치) / `all`(어느 필드든).
    정의에 따라 답이 달라지기 때문이다.

    **중단조건은 귀속 가능한 것부터** (docs/32 §9-1 재사용). 1차는 **우리 요청의 429** —
    `client.counters["http_429"]` 증가로 잡는다(`_request` 가 429 를 한 번 삼키고
    재시도하므로 예외만 봐서는 놓친다). 2차는 컬렉터 배경 429 이되 임계는 **실측 배경
    최대치보다 엄격히 위**로만 잡는다 — "1이라도 오르면 종료" 는 배경이 스스로 오르는
    구간에서 헛종료하고 거짓 인과를 기록으로 남긴다. 3차는 사전 고정 콜 상한
    (`CADENCE["call_cap"]`), 그리고 `data/PROBE_STOP` 파일.

    판정은 하지 않는다 — 숫자와 그 측정 조건만 낸다 (COORDINATOR-STATE §4.7-5).
    """
    stop_file = REPO_ROOT / "data" / "PROBE_STOP"
    if COLLECTOR_LOG is None or not Path(COLLECTOR_LOG).exists():
        return {"verdict": "미확인",
                "error": "--collector-log 가 필요하다. 배경 429 기준선 없이 돌리면 "
                         "중단조건의 임계값을 근거 없이 정하게 된다."}
    watch = CollectorWatch(Path(COLLECTOR_LOG))
    baseline = watch.baseline(BASELINE_MIN)
    burst_stop = max(int(baseline.get("max_in_60s") or 0) + 1, 3)
    total_stop = max(int(baseline.get("max_in_480s") or 0) + 3, 5)

    run_t0 = time.time()
    calls_at_start = c.counters["requests"]
    own_429_at_start = c.counters["http_429"]
    cap = CADENCE["call_cap"]
    aborted: list[str | None] = [None]

    def guard() -> str | None:
        if aborted[0]:
            return aborted[0]
        if stop_file.exists():
            aborted[0] = "PROBE_STOP 파일"
        elif c.counters["http_429"] > own_429_at_start:
            aborted[0] = ("**우리 프로브가** 429 를 받음 "
                          f"({c.counters['http_429'] - own_429_at_start}건) — 1차 중단조건")
        elif (used := c.counters["requests"] - calls_at_start) >= cap:
            aborted[0] = f"사전 고정 콜 상한 도달 ({used}/{cap})"
        else:
            evs = [e for e in watch.events(run_t0) if e["epoch"] >= run_t0]
            if len(evs) >= total_stop:
                aborted[0] = (f"컬렉터 429 누적 {len(evs)}건 ≥ 임계 {total_stop} "
                              f"(배경 480초 최대 {baseline.get('max_in_480s')}건)")
            else:
                for e in evs:
                    n = sum(1 for e2 in evs if 0 <= e2["epoch"] - e["epoch"] < 60)
                    if n >= burst_stop:
                        aborted[0] = (f"컬렉터 429 60초 {n}건 ≥ 임계 {burst_stop} "
                                      f"({e['kst']}~)")
                        break
        return aborted[0]

    out: dict = {
        "profile": CADENCE_PROFILE,
        "arms_spec": {"vol": CADENCE["vol_arms"], "gain": CADENCE["gain_arms"]},
        "measured_at_kst": ms_to_iso_kst(now_ms()),
        "measured_at_et": ms_to_iso_et(now_ms()),
        "budget": {"call_cap": cap, "planned_calls": (
            sum(int(d / g) for _l, g, d in CADENCE["vol_arms"])
            + sum(int(d / g) for _l, g, d in CADENCE["gain_arms"]) + 6)},
        "collector_baseline_429": baseline,
        "stop_thresholds": {"our_429": 1, "collector_60s": burst_stop,
                            "collector_480s": total_stop, "call_cap": cap},
        "telemetry_at_start": watch.latest_telemetry(),
    }

    # --- 0. 단발 확인 (측정 2 항목 1·4·5) ------------------------------------
    oneshot: dict = {}
    for rtype in ("TOP_GAINERS", "TOP_LOSERS"):
        for dur in ("realtime", "1d"):
            key = f"{rtype}_{dur}"
            try:
                body = await c._request("GET", "/api/v1/rankings", params={
                    "type": rtype, "marketCountry": "US", "duration": dur,
                    "count": CADENCE["count"]})
                rows = (body.get("result") or {}).get("rankings") or []
                oneshot[key] = {"http": c.last_status, "rows": len(rows),
                                "rankedAt": (body.get("result") or {}).get("rankedAt"),
                                "evidence": snippet(trunc_result(body))}
                if dur == "1d":
                    # 프로파일을 파일명에 넣는다 — 정규장 재측정이 프리마켓 증거를
                    # 덮어쓰면 세션 비교의 한쪽이 사라진다.
                    sfx = "" if CADENCE_PROFILE == "full" else f"_{CADENCE_PROFILE}"
                    save_fixture(f"live_rankings_{rtype.lower()}_1d{sfx}.json",
                                 "GET /api/v1/rankings", f"{rtype.lower()}_1d{sfx}",
                                 200, body, c.last_headers,
                                 f"라이브 캡처 (docs/35, profile={CADENCE_PROFILE})",
                                 match={"type": rtype, "duration": "1d"})
            except TossApiError as exc:
                oneshot[key] = {"http": c.last_status,
                                "error": f"{type(exc).__name__}: {exc}"}
    out["oneshot"] = oneshot

    # --- 1. 거래량 랭킹 (measure 1) ------------------------------------------
    vol_params = {"type": "TOSS_SECURITIES_TRADING_VOLUME", "marketCountry": "US",
                  "duration": "realtime", "count": CADENCE["count"]}
    gain_params = {"type": "TOP_GAINERS", "marketCountry": "US",
                   "duration": "1d", "count": CADENCE["count"]}
    gain_ok = oneshot.get("TOP_GAINERS_1d", {}).get("rows", 0) > 0

    plan = [("volume_realtime", vol_params, CADENCE["vol_arms"])]
    if gain_ok:
        plan.append(("top_gainers_1d", gain_params, CADENCE["gain_arms"]))
    else:
        out["top_gainers_1d"] = {"skipped": "1d 단발 호출이 행을 주지 않아 주기 측정 생략"}

    last_pages: dict[str, list[str]] = {}
    for name, params, arms in plan:
        section: dict = {"params": dict(params), "arms": {}}
        for label, gap_s, dur_s in arms:
            if guard():
                section["arms"][label] = {"skipped": aborted[0]}
                continue
            print(f"[probe]   {name} arm {label}: {int(dur_s / gap_s)} calls @ {gap_s}s",
                  file=sys.stderr, flush=True)
            t_arm = time.time()
            polls, stop = await _cadence_arm(c, params, gap_s, dur_s, guard)
            a = _analyze_arm(polls)
            a["nominal_gap_s"] = gap_s
            a["start_kst"] = ms_to_iso_kst(int(t_arm * 1000))
            a["errors"] = sum(1 for p in polls if "error" in p)
            a["collector_429_during"] = len([e for e in watch.events(t_arm)
                                             if e["epoch"] >= t_arm])
            if stop:
                a["aborted"] = stop
            if label == "1s":
                # 같은 응답열을 굵은 자로 다시 재는 대조 — 이 프로브의 핵심.
                a["decimated"] = {f"stride_{k}": _analyze_arm(polls, stride=k)
                                  for k in CADENCE["strides"]}
                a["raw_polls"] = [
                    {k: p.get(k) for k in ("i", "sent_epoch", "rankedAt", "order",
                                           "volume", "all", "top3", "error")}
                    for p in polls]
            section["arms"][label] = a
            if polls and "error" not in polls[-1]:
                last_pages[name] = polls[-1].get("top3") or []
            await asyncio.sleep(CADENCE["arm_gap_s"])
        out[name] = section

    # --- 2. 유니버스 겹침 (measure 2 항목 3) ---------------------------------
    # 랭킹 목록 3종을 **같은 계산**으로 재서 docs/30 §1-1 의 86% 와 대조 가능하게 낸다.
    overlap: dict = {}
    pages: dict[str, list[dict]] = {}
    for key, params in (("TOSS_SECURITIES_TRADING_VOLUME",
                         {**vol_params}),
                        ("TOP_GAINERS", {**gain_params}),
                        ("TOP_LOSERS", {"type": "TOP_LOSERS", "marketCountry": "US",
                                        "duration": "1d", "count": CADENCE["count"]})):
        if guard():
            overlap[key] = {"skipped": aborted[0]}
            continue
        try:
            body = await c._request("GET", "/api/v1/rankings", params=params)
            pages[key] = (body.get("result") or {}).get("rankings") or []
        except TossApiError as exc:
            overlap[key] = {"error": f"{type(exc).__name__}: {exc}"}
    syms = sorted({str(r.get("symbol")) for rows in pages.values() for r in rows})
    metas = {}
    if syms:
        try:
            metas = {m.symbol: m for m in await c.get_stocks(syms)}
        except TossApiError as exc:
            overlap["_stocks_error"] = f"{type(exc).__name__}: {exc}"
    for key, rows in pages.items():
        overlap[key] = _universe_fit(rows, metas)
    overlap["_symbols_resolved"] = f"{len(metas)}/{len(syms)}"
    out["universe_overlap"] = overlap

    out["aborted"] = aborted[0]
    out["verdict"] = "부분확인" if aborted[0] else "확인됨"
    out["telemetry_at_end"] = watch.latest_telemetry()
    out["collector_429_total_during_probe"] = len(
        [e for e in watch.events(run_t0) if e["epoch"] >= run_t0])
    out["calls_used"] = c.counters["requests"] - calls_at_start
    return out


def _universe_fit(rows: list[dict], metas: dict) -> dict:
    """랭킹 목록 한 통이 우리 유니버스 조건에 얼마나 들어오는가.

    `86%` 의 원 계산식은 docs/30 에 남아 있지 않다. 그래서 **정의를 여기에 적고
    세 목록에 똑같이 적용**한다 — 원 숫자와 정의가 달라도 셋끼리는 대조 가능하다.
    """
    n = len(rows)
    if not n:
        return {"rows": 0}

    def price_usd(r: dict) -> Decimal | None:
        try:
            return Decimal(str((r.get("price") or {}).get("lastPrice")))
        except Exception:
            return None

    band_cfg = band_task = tier0 = 0
    mcap_known = 0
    for r in rows:
        p = price_usd(r)
        if p is None:
            continue
        if Decimal("0.10") <= p <= Decimal("20.00"):
            band_cfg += 1
        if Decimal("2") <= p <= Decimal("5"):
            band_task += 1
        m = metas.get(str(r.get("symbol")))
        if m is None:
            continue
        mcap_known += 1
        cap = p * Decimal(m.shares_outstanding_qu) / Decimal(1_000_000)
        if (m.is_common and m.security_type.strip().upper() in {"STOCK", "FOREIGN_STOCK"}
                and m.status.strip().upper() == "ACTIVE"
                and m.shares_outstanding_qu > 0
                and Decimal("0.10") <= p <= Decimal("20.00")
                and Decimal("10000000") <= cap <= Decimal("300000000")):
            tier0 += 1

    def pct(k: int) -> float:
        return round(100.0 * k / n, 1)

    return {
        "rows": n,
        "in_price_band_cfg_0.10_20": {"n": band_cfg, "pct": pct(band_cfg)},
        "in_price_band_task_2_5": {"n": band_task, "pct": pct(band_task)},
        "passes_tier0_full": {"n": tier0, "pct": pct(tier0)},
        "meta_resolved": mcap_known,
        # 심볼을 남긴다 — "조건 통과" 와 "우리 감시망(tier1 정원 1500)에 있음" 은 다른
        # 질문이고, 후자는 DB 와 대조해야 답이 나온다. 집계만 남기면 다시 부르게 된다.
        "symbols": [str(r.get("symbol")) for r in rows],
    }


async def probe_stocks(c: TossClient) -> dict:
    """`/stocks` 미국 종목 메타 — sharesOutstanding 등."""
    body = await c._request("GET", "/api/v1/stocks",
                            params={"symbols": ",".join([LIQUID] + SMALL)})
    save_fixture("live_stocks_us.json", "GET /api/v1/stocks", "default", 200, body,
                 c.last_headers, "라이브 캡처 (isinCode 마스킹)")
    rows = body.get("result") or []
    return {
        "verdict": "확인됨",
        "requested": len([LIQUID] + SMALL), "returned": len(rows),
        "missing": sorted(set([LIQUID] + SMALL) - {r.get("symbol") for r in rows}),
        "fields": sorted(rows[0]) if rows else [],
        "evidence": snippet(trunc_result(body)),
    }


async def probe_warnings(c: TossClient) -> dict:
    """미국 종목의 `/stocks/{symbol}/warnings` 실효성."""
    out = {}
    for sym in [LIQUID, SMALL[0]]:
        try:
            body = await c._request("GET", f"/api/v1/stocks/{sym}/warnings")
            out[sym] = {"http": c.last_status, "count": len(body.get("result") or []),
                        "body": snippet(body, 300)}
            save_fixture("live_warnings_us.json", "GET /api/v1/stocks/{symbol}/warnings",
                         "default", 200, body, c.last_headers, f"{sym} 라이브 캡처")
        except TossApiError as exc:
            out[sym] = {"http": c.last_status, "error": f"{type(exc).__name__}: {exc}"}
    empties = [v.get("count") for v in out.values() if "count" in v]
    return {
        "verdict": "확인됨" if empties else "미확인",
        "note": ("미국 종목은 빈 배열 — KRX 전용으로 확인" if empties and max(empties) == 0
                 else "일부 미국 종목에 유의사항 존재"),
        "per_symbol": out,
    }


async def probe_price_limits(c: TossClient) -> dict:
    """미국 종목 상/하한가 (null 예상)."""
    body = await c._request("GET", "/api/v1/price-limits", params={"symbol": LIQUID})
    save_fixture("live_price_limits_us.json", "GET /api/v1/price-limits", "default", 200,
                 body, c.last_headers, f"{LIQUID} 라이브 캡처")
    r = body.get("result") or {}
    return {
        "verdict": "확인됨",
        "upper": r.get("upperLimitPrice"), "lower": r.get("lowerLimitPrice"),
        "timestamp": r.get("timestamp"),
        "evidence": snippet(body),
    }


async def probe_fx(c: TossClient) -> dict:
    """`/exchange-rate` 실제 스프레드 관측 (rate vs midRate)."""
    body = await c._request("GET", "/api/v1/exchange-rate",
                            params={"baseCurrency": "USD", "quoteCurrency": "KRW"})
    save_fixture("live_exchange_rate.json", "GET /api/v1/exchange-rate", "default", 200,
                 body, c.last_headers, "라이브 캡처")
    r = body.get("result") or {}
    out = {"verdict": "확인됨", "evidence": snippet(body)}
    try:
        rate, mid = Decimal(str(r["rate"])), Decimal(str(r["midRate"]))
        out.update({
            "rate": str(rate), "midRate": str(mid),
            "basisPoint": str(r.get("basisPoint")),
            "spread_krw": str(rate - mid),
            "spread_pct": str(((rate - mid) / mid * 100).quantize(Decimal("0.0001"))),
            "valid_window": [r.get("validFrom"), r.get("validUntil")],
        })
    except (KeyError, ArithmeticError) as exc:
        out["verdict"] = "부분확인"
        out["note"] = f"필드 파싱 실패: {exc}"
    return out


async def probe_commissions(c: TossClient) -> dict:
    """`/commissions` 실제 수수료율 (accountSeq 필요)."""
    acc = await c._request("GET", "/api/v1/accounts")
    save_fixture("live_accounts.json", "GET /api/v1/accounts", "default", 200, acc,
                 c.last_headers, "accountNo/accountSeq 마스킹됨")
    rows = acc.get("result") or []
    if not rows:
        return {"verdict": "미확인", "note": "계좌 목록이 비어 있음 — /commissions 조회 불가"}
    seq = rows[0].get("accountSeq")
    body = await c._request("GET", "/api/v1/commissions",
                            headers={"X-Tossinvest-Account": str(seq)})
    save_fixture("live_commissions.json", "GET /api/v1/commissions", "default", 200,
                 body, c.last_headers, "라이브 캡처")
    per_market = {r.get("marketCountry"): r.get("commissionRate")
                  for r in (body.get("result") or [])}
    us = per_market.get("US")
    note = "수수료율 단위 미상"
    if us is not None:
        d = Decimal(str(us))
        note = (f"US 수수료율 원값 {us} — 퍼센트로 해석 시 편도 {d}% "
                f"(왕복 {d * 2}%), 비율로 해석 시 편도 {d * 100}%. 단위는 문서 미명시")
    return {"verdict": "확인됨", "per_market": per_market, "note": note,
            "evidence": snippet(body)}


async def probe_ratelimit_headers(c: TossClient) -> dict:
    """rate limit 헤더 실제 포맷."""
    await c._request("GET", "/api/v1/prices", params={"symbols": LIQUID})
    md = {k: v for k, v in c.last_headers.items() if k.lower().startswith("x-ratelimit")}
    await c._request("GET", "/api/v1/exchange-rate",
                     params={"baseCurrency": "USD", "quoteCurrency": "KRW"})
    mi = {k: v for k, v in c.last_headers.items() if k.lower().startswith("x-ratelimit")}
    return {
        "verdict": "확인됨" if md else "미확인",
        "MARKET_DATA_headers": md,
        "MARKET_INFO_headers": mi,
        "header_names_exact": sorted(md) or sorted(mi),
        "all_response_headers_sample": sorted(mask_headers(c.last_headers)),
        "note": ("정상 응답에도 X-RateLimit-* 가 실려 옴" if md
                 else "정상 응답에 X-RateLimit-* 없음 — limiter 자기보정 불가"),
    }


async def probe_group_coupling(c: TossClient) -> dict:
    """그룹이 서버에서 **한 창을 공유하는지** 를 가동 중 수집기를 부하원으로 삼아 관측한다.

    (docs/32 2단계 실험 A′. 승인 후에만 실행한다.)

    착상: 수집기가 이미 MARKET_DATA 를 초당 7~9 회 쓰고 있다. 우리가 **조용한 그룹**에
    1콜만 넣고 그 응답의 `x-ratelimit-remaining` 을 읽으면, 서버가 그 그룹을 MD 와 같은
    창으로 세는지가 그대로 드러난다:

    - **독립이면**: 그 초에 그 그룹은 우리 1콜뿐이므로 `remaining = limit - 1` 이 거의 항상.
    - **MD 와 공유면**: 같은 초에 수집기의 MD 7~9 콜이 같은 창을 깎았으므로 `remaining` 이
      훨씬 낮고 산포가 크다.

    이 설계가 이전안(한쪽을 한도 근처까지 밀고 다른 쪽을 읽는 능동 부하)보다 나은 점:
    **버스트를 만들지 않는다.** 부하는 이미 수집기가 만들고 있고 우리는 관측만 한다.
    그래서 429 유발 위험이 구조적으로 없고, 예산 소모도 그룹당 0.5 req/s 뿐이다.
    덤으로 같은 응답에서 RTT 꼬리(실험 C, 가설 라)를 공짜로 얻는다.

    **중단조건은 귀속 가능한 것부터 쓴다** (docs/32 §9-1). 1차는 **우리 응답의 429**다 —
    우리 요청, 우리 상태코드라 남의 탓과 섞이지 않는다. 컬렉터의 `http_429` 카운터를
    "1이라도 증가하면 종료" 로 쓰던 원안은 **폐기했다**: 그 카운터는 프로브와 무관하게
    스스로 오르고 있어(2026-08-05 낮 배경 0.239건/분) 착수 즉시 헛종료할 뿐 아니라
    "우리가 429 를 유발했다" 는 **거짓 인과**를 기록으로 남긴다. 컬렉터 쪽은 참고 지표로
    강등하고, 임계값은 **실측된 배경 최대치보다 엄격히 위**로만 잡는다.

    대조군도 같이 뜬다: 그룹마다 **콜 없는 OFF 구간(60s)** 을 콜 구간 앞에 끼워
    같은 시간대의 배경 발생률을 함께 잰다. `data/PROBE_STOP` 파일이 생겨도 중단한다.
    """
    stop_file = REPO_ROOT / "data" / "PROBE_STOP"
    if COLLECTOR_LOG is None or not Path(COLLECTOR_LOG).exists():
        return {"verdict": "미확인",
                "error": "--collector-log 가 필요하다. 배경 429 기준선(대조군) 없이 돌리면 "
                         "결과가 해석 불가다 — 이 프로젝트가 반복해서 데인 지점이다."}
    watch = CollectorWatch(Path(COLLECTOR_LOG))
    baseline = watch.baseline(BASELINE_MIN)

    # 임계값의 근거는 실측 배경이다. 배경 최대치와 같거나 낮게 잡으면 배경에 걸려 헛종료한다.
    burst_stop = max(int(baseline.get("max_in_60s") or 0) + 1, 3)
    total_stop = max(int(baseline.get("max_in_480s") or 0) + 3, 5)

    # 순서: **대조군 먼저**. STOCK 은 docs/06 §9-4 에서 독립이 이미 확인됐고 컬렉터가
    # 쓰지도 않는다(avg 0.00). 여기서 alone_pct 가 100 근처로 안 나오면 계측 자체가 틀린
    # 것이므로 배경 429 를 실제로 받고 있는 MARKET_DATA_CHART 를 건드리기 전에 멈춘다.
    quiet = [
        ("STOCK", "/api/v1/stocks", {"symbols": LIQUID}),
        ("MARKET_INFO", "/api/v1/exchange-rate",
         {"baseCurrency": "USD", "quoteCurrency": "KRW"}),
        ("RANKING", "/api/v1/rankings",
         {"type": "MARKET_TRADING_VOLUME", "marketCountry": "US",
          "duration": "realtime", "count": 1}),
        ("MARKET_DATA_CHART", "/api/v1/candles",
         {"symbol": LIQUID, "interval": "1d", "count": 1}),
    ]
    samples_per_group = COUPLING["samples_per_group"]
    gap_s = COUPLING["gap_s"]         # 그룹당 0.5 req/s
    off_s = COUPLING["off_s"]         # 대조 구간은 콜 구간과 같은 길이

    run_t0 = time.time()
    out: dict[str, dict] = {}
    arms: list[dict] = []
    aborted: str | None = None

    def bg_since(t0: float) -> list[dict]:
        return [e for e in watch.events(t0) if e["epoch"] >= t0]

    def gross_excursion() -> str | None:
        """배경으로 설명되지 않는 **큰** 이탈만 잡는다 (헛종료 방지)."""
        evs = bg_since(run_t0)
        if len(evs) >= total_stop:
            return (f"컬렉터 429 가 프로브 구간 누적 {len(evs)}건 — "
                    f"임계 {total_stop} (배경 480초 최대 {baseline.get('max_in_480s')}건 기준)")
        for e in evs:
            n = sum(1 for e2 in evs if 0 <= e2["epoch"] - e["epoch"] < 60)
            if n >= burst_stop:
                return (f"컬렉터 429 가 60초에 {n}건 ({e['kst']}~) — "
                        f"임계 {burst_stop} (배경 60초 최대 {baseline.get('max_in_60s')}건 기준)")
        return None

    for group, path, params in quiet:
        if aborted:
            break

        # --- OFF 구간: 콜 0. 같은 시간대의 배경 발생률을 짝지어 잰다 (대조군).
        # md_peak_1s>=9 면 라운드를 건너뛰고 OFF 를 한 번 더 돈다. 이 값은 telemetry
        # (300초 주기)에만 있어 최대 300초 낡으므로 **착수 게이트**로만 쓰고 나이를 함께 남긴다.
        # 한 번 걸렸다고 그룹을 버리면 게이트가 열려 있는 시간대를 통째로 놓친다.
        # 재시도는 **새 telemetry 줄**을 기다리는 것이어야 한다. telemetry 주기가 300초라
        # 60초짜리 재시도 3번은 같은 줄을 세 번 읽을 뿐 새 정보가 없다.
        tel = None
        gate_skips: list[dict] = []
        gate_deadline = time.time() + GATE_WAIT_MAX_S
        while True:
            off_t0 = time.time()
            await asyncio.sleep(off_s)
            arms.append({"group": group, "arm": "off", "calls": 0,
                         "start_kst": ms_to_iso_kst(int(off_t0 * 1000)),
                         "dur_s": round(time.time() - off_t0, 1),
                         "collector_429": len(bg_since(off_t0))})
            if stop_file.exists():
                aborted = "PROBE_STOP 파일"
                break
            if (why := gross_excursion()):
                aborted = why
                break
            tel = watch.latest_telemetry()
            if not tel or (tel["md_peak_1s"] or 0) < 9:
                break
            if not gate_skips or gate_skips[-1]["telemetry_at"] != tel["at_kst"]:
                gate_skips.append({"md_peak_1s": tel["md_peak_1s"],
                                   "telemetry_at": tel["at_kst"], "age_s": tel["age_s"]})
            if time.time() >= gate_deadline:
                break
        if aborted:
            break
        if tel and (tel["md_peak_1s"] or 0) >= 9:
            out[group] = {"verdict": "미확인",
                          "skipped": f"md_peak_1s>=9 라운드 스킵 ×{len(gate_skips)} — 게이트가 열리지 않음",
                          "gate_skips": gate_skips, "telemetry": tel}
            continue

        # --- ON 구간: 30콜 @ 2초.
        on_t0 = time.time()
        recs: list[dict] = []
        for _ in range(samples_per_group):
            if stop_file.exists():
                aborted = "PROBE_STOP 파일"
                break
            t0 = time.monotonic()
            sent_epoch = time.time()
            try:
                await c._request("GET", path, params=params)
            except RateLimited:
                aborted = f"{group}: **우리 프로브가** 429 를 받음 — 즉시 중단 (1차 중단조건)"
                break
            except TossApiError as exc:
                recs.append({"error": f"{type(exc).__name__}: {exc}"})
                continue
            h = {k.lower(): v for k, v in c.last_headers.items()}
            recs.append({
                "limit": _as_int(h.get("x-ratelimit-limit")),
                "remaining": _as_int(h.get("x-ratelimit-remaining")),
                "date": h.get("date"),
                "sent_epoch": round(sent_epoch, 3),
                "rtt_ms": round((time.monotonic() - t0) * 1000, 1),
            })
            await asyncio.sleep(gap_s)
        arms.append({"group": group, "arm": "on", "calls": len(recs),
                     "start_kst": ms_to_iso_kst(int(on_t0 * 1000)),
                     "dur_s": round(time.time() - on_t0, 1),
                     "collector_429": len(bg_since(on_t0)),
                     "telemetry_at_start": tel})
        if not aborted and (why := gross_excursion()):
            aborted = why

        ok = [r for r in recs if r.get("remaining") is not None]
        if not ok:
            out[group] = {"verdict": "미확인", "samples": recs}
            continue
        lim = ok[0]["limit"]
        # 우리 1콜만 들어간 초라면 remaining == limit-1 이어야 한다.
        alone = sum(1 for r in ok if r["remaining"] == (r["limit"] or 0) - 1)
        depressed = [r["remaining"] for r in ok if r["remaining"] < (r["limit"] or 0) - 1]
        rtts = sorted(r["rtt_ms"] for r in ok)
        out[group] = {
            "limit": lim,
            "n": len(ok),
            "alone_pct": round(100.0 * alone / len(ok), 1),
            "depressed_n": len(depressed),
            "min_remaining": min(r["remaining"] for r in ok),
            "rtt_min_ms": rtts[0],
            "rtt_p50_ms": rtts[len(rtts) // 2],
            "rtt_p90_ms": rtts[int(len(rtts) * 0.9)],
            "rtt_max_ms": rtts[-1],
            "verdict": ("MD 와 독립" if alone / len(ok) >= 0.9 else
                        "공유 의심 — 우리 1콜뿐인데 remaining 이 더 깎였다"),
            "samples": recs,
        }
        # 대조군이 깨지면 계측을 못 믿는다 — 배경 429 를 실제로 받고 있는
        # MARKET_DATA_CHART 에 콜을 더 넣기 전에 멈춘다.
        if group == "STOCK" and not aborted and out[group]["alone_pct"] < 90.0:
            aborted = (f"대조군 STOCK 이 alone_pct={out[group]['alone_pct']}% — "
                       "독립이 이미 확인된 그룹(docs/06 §9-4)에서조차 remaining 이 깎였다. "
                       "계측이 검증되지 않았으므로 CHART 로 진행하지 않는다.")

    # 실험 C: 편도 지연폭이 CLOCK_SKEW_MARGIN_S(0.15s)를 넘는가 (가설 라).
    # **그룹별 첫 콜은 뺀다** — 연결·TLS 수립이 섞여 실측 400~600ms 가 나오는데, 이것은
    # 웜 커넥션으로 도는 컬렉터의 요청당 지연이 아니다. 넣고 재면 꼬리를 통째로 과대평가한다.
    cold = [r["rtt_ms"] for g in out.values()
            for r in g.get("samples", [])[:1] if r.get("rtt_ms")]
    all_rtt = sorted(r["rtt_ms"] for g in out.values()
                     for r in g.get("samples", [])[1:] if r.get("rtt_ms"))
    skew = None
    if all_rtt:
        spread_one_way_ms = (all_rtt[-1] - all_rtt[0]) / 2.0
        p90 = all_rtt[int(len(all_rtt) * 0.9)]
        skew = {
            "n": len(all_rtt),
            "excluded_first_call_rtt_ms": sorted(cold),
            "rtt_min_ms": all_rtt[0], "rtt_p50_ms": all_rtt[len(all_rtt) // 2],
            "rtt_p90_ms": p90, "rtt_p99_ms": all_rtt[int(len(all_rtt) * 0.99)],
            "rtt_max_ms": all_rtt[-1],
            "one_way_spread_ms": round(spread_one_way_ms, 1),
            "one_way_spread_p90_ms": round((p90 - all_rtt[0]) / 2.0, 1),
            "over_margin_n": sum(1 for r in all_rtt if (r - all_rtt[0]) / 2.0 > 150.0),
            "margin_ms": 150.0,
            "verdict": ("여유 안 — 통상 구간에서는 하드캡 상계 증명 성립"
                        if spread_one_way_ms <= 150.0 else
                        "통상은 여유 안이나 **꼬리가 margin 을 넘는 콜이 있다** — "
                        "over_margin_n/n 으로 빈도를 볼 것 (가설 라)"),
        }
    # --- 배경 429 대조 (귀속용이 아니라 **반증용**이다).
    bg = bg_since(run_t0)
    on_arms = [a for a in arms if a["arm"] == "on"]
    off_arms = [a for a in arms if a["arm"] == "off"]
    on_s = sum(a["dur_s"] for a in on_arms) or 1.0
    off_s_tot = sum(a["dur_s"] for a in off_arms) or 1.0
    sent = sorted(r["sent_epoch"] for g in out.values()
                  for r in g.get("samples", []) if r.get("sent_epoch"))
    coincident = sum(1 for e in bg
                     if any(abs(e["epoch"] - s) <= 1.0 for s in sent))
    exp_on = baseline["rate_per_min"] * on_s / 60.0
    control = {
        "baseline_before_probe": baseline,
        "on_window_s": round(on_s, 1), "off_window_s": round(off_s_tot, 1),
        "collector_429_during_on": sum(a["collector_429"] for a in on_arms),
        "collector_429_during_off": sum(a["collector_429"] for a in off_arms),
        "collector_429_total_in_run": len(bg),
        "expected_from_baseline_during_on": round(exp_on, 2),
        "coincident_within_1s_of_our_call": coincident,
        "events": bg,
        "stop_thresholds": {"burst_60s": burst_stop, "run_total": total_stop,
                            "근거": "실측 배경의 롤링 최대치보다 엄격히 위"},
        "검정력": (f"배경 {baseline['rate_per_min']}/분 × ON {round(on_s / 60.0, 1)}분 "
                f"= 기대 {round(exp_on, 2)}건. **발생률 변화를 검정할 표본이 아니다** — "
                "이 대조는 큰 이탈을 잡고 거짓 인과를 막는 용도이지, "
                "'프로브가 429 를 늘리지 않았다'를 증명하지 않는다."),
    }
    # 표본이 실제로 하나라도 모인 그룹이 있어야 "확인됨" 이다. 전부 게이트 스킵이면
    # 아무것도 재지 않은 것이고, 그것을 확인됨으로 적으면 안 읽은 것을 읽었다고 쓰는 셈이다.
    measured = [g for g, d in out.items() if d.get("n")]
    return {
        "verdict": "확인됨" if measured and not aborted else "미확인",
        "measured_groups": measured,
        "aborted": aborted,
        "per_group": {g: {k: v for k, v in d.items() if k != "samples"}
                      for g, d in out.items()},
        "background_control": control,
        "arms": arms,
        "latency_skew": skew,
        "detail": out,
    }


async def probe_ratelimit_contract(c: TossClient) -> dict:
    """레이트리밋 **계약** 실측: 창 모양(고정/슬라이딩)·한도 적용 범위(그룹/엔드포인트).

    W4 의 예산 모델이 이 답 위에 세워지므로 추측이 아니라 실측이 필요하다.
    **한도를 넘기지 않는다** — 429 를 유발하지 않도록 설계했고, 429 를 받으면 즉시 중단한다.
    (`force429` 와 달리 컬렉터 가동 중에도 안전하다. 다만 컬렉터가 쓰지 않는 그룹
    — MARKET_INFO(3/s)·STOCK(5/s) — 만 쓴다.)

    측정 3종:

    1. **같은 그룹 / 다른 엔드포인트** — `/exchange-rate` 직후 `/market-calendar/US` 를
       같은 1초 안에 때린다. `remaining` 이 이어서 줄면 카운터가 **그룹 공유**,
       각자 limit-1 이면 **엔드포인트별**이다. W4 의 예산 분할 방식이 여기서 갈린다.
    2. **다른 그룹** — 이어서 `/stocks` 를 때린다. 자기 limit-1 로 나오면 그룹별 독립.
    3. **창 모양 (슬라이딩 배제)** — A(t=0), B(t=+0.08s), C(t=+1.00s) 세 발.
       고정 1초 창이면 C 는 **새 창의 첫 발**이라 `remaining = limit-1`.
       슬라이딩 1초 창이면 C 의 직전 1초 안에 B 가 아직 살아 있어 `remaining = limit-2`.
       세 발이므로 한도(3/s)를 넘지 않는다.
    4. **고정창 vs 토큰버킷** — 3번만으로는 이 둘이 구분되지 않는다(용량=rate 인 토큰버킷도
       1.0초 뒤엔 가득 차 있다). 한도의 83%(2.5/s)로 8초간 균등 샘플링해서
       `remaining` 이 **서버 date 초 안에서 몇 번째 호출인가**의 함수인지 본다.
       고정창이면 매 초 첫 발이 `limit-1`, 둘째 발이 `limit-2` 인 톱니가 **초 경계에 정렬**된다.
       토큰버킷이면 소비(2.5/s) < 충전(3/s) 이라 버킷이 늘 차 있어 정렬이 나타나지 않는다.
       (2번이 "그룹별 독립" 으로 나왔을 때만 실행한다 — 계정 전체 한도라면 가동 중
       컬렉터와 버킷을 나눠 쓰는 셈이라 이 부하를 주지 않는다.)
    """
    import httpx

    token = await c.tokens.get()
    check_allowed("GET", "/api/v1/exchange-rate")
    check_allowed("GET", "/api/v1/market-calendar/US")
    check_allowed("GET", "/api/v1/stocks")

    fx = ("/api/v1/exchange-rate", {"baseCurrency": "USD", "quoteCurrency": "KRW"})
    cal = ("/api/v1/market-calendar/US", None)
    stk = ("/api/v1/stocks", {"symbols": LIQUID})

    aborted: list[str] = []
    samples: list[dict] = []

    async def hit(raw, tag: str, ep: tuple) -> dict:
        path, params = ep
        t0 = time.monotonic()
        resp = await raw.get(path, params=params,
                             headers={"Authorization": f"Bearer {token}"})
        h = {k.lower(): v for k, v in resp.headers.items()}
        rec = {
            "tag": tag,
            "path": path,
            "status": resp.status_code,
            "limit": h.get("x-ratelimit-limit"),
            "remaining": h.get("x-ratelimit-remaining"),
            "reset": h.get("x-ratelimit-reset"),
            "retry_after": h.get("retry-after"),
            "server_date": h.get("date"),
            "sent_mono": round(t0, 4),
            "rtt_ms": round((time.monotonic() - t0) * 1000, 1),
        }
        samples.append(rec)
        if resp.status_code == 429:
            # 유발할 의도가 없었다. 즉시 멈춘다 — 가동 중 컬렉터와 버킷을 공유할 수 있다.
            aborted.append(f"{tag}: 429 (의도치 않음)")
        return rec

    async with httpx.AsyncClient(base_url=c.base_url, timeout=10.0,
                                 transport=GuardedTransport()) as raw:
        # 1. 같은 그룹 / 다른 엔드포인트 — 2회 반복(순서를 바꿔서)
        same_group = []
        for first, second in ((fx, cal), (cal, fx)):
            if aborted:
                break
            await asyncio.sleep(2.5)          # 창을 비운다
            a = await hit(raw, "same_group.a", first)
            await asyncio.sleep(0.08)
            b = await hit(raw, "same_group.b", second)
            same_group.append({"a": a, "b": b,
                               "same_server_second": a["server_date"] == b["server_date"]})

        # 2. 다른 그룹 (MARKET_INFO → STOCK)
        cross_group = None
        if not aborted:
            await asyncio.sleep(2.5)
            a = await hit(raw, "cross_group.market_info", fx)
            await asyncio.sleep(0.08)
            b = await hit(raw, "cross_group.stock", stk)
            cross_group = {"a": a, "b": b,
                           "same_server_second": a["server_date"] == b["server_date"]}

        # 3. 창 모양 — A, B(+0.08s), C(+1.00s)
        window_rounds = []
        for i in range(3):
            if aborted:
                break
            await asyncio.sleep(3.0)          # 이전 라운드가 완전히 빠져나가게
            a = await hit(raw, f"window{i}.a", fx)
            await asyncio.sleep(0.08)
            b = await hit(raw, f"window{i}.b", fx)
            # C 를 A 로부터 정확히 1.00초 뒤에 (전송 시각 기준)
            gap = 1.00 - (time.monotonic() - a["sent_mono"])
            if gap > 0:
                await asyncio.sleep(gap)
            cc = await hit(raw, f"window{i}.c", fx)
            window_rounds.append({
                "a": a, "b": b, "c": cc,
                "c_minus_a_s": round(cc["sent_mono"] - a["sent_mono"], 3),
                "ab_same_server_second": a["server_date"] == b["server_date"],
            })

    def _int(v):
        try:
            return int(str(v).strip())
        except (TypeError, ValueError):
            return None

    # --- 판정 -----------------------------------------------------------
    cross = "미확인"
    if cross_group and _int(cross_group["b"]["remaining"]) is not None:
        b = cross_group["b"]
        if _int(b["remaining"]) == _int(b["limit"]) - 1:
            cross = "그룹별 독립"
        else:
            cross = f"공유 의심 (STOCK 첫 호출인데 remaining={b['remaining']}/{b['limit']})"

    # 4. 고정창 vs 토큰버킷 — 한도의 83% 로 균등 샘플링. 그룹별 독립일 때만 (컬렉터 보호).
    sawtooth: list[dict] = []
    if not aborted and cross == "그룹별 독립":
        async with httpx.AsyncClient(base_url=c.base_url, timeout=10.0,
                                     transport=GuardedTransport()) as raw:
            await asyncio.sleep(2.5)
            step = 0.40                       # 2.5 req/s — MARKET_INFO 한도 3/s 의 83%
            t_next = time.monotonic()
            for i in range(20):
                if aborted:
                    break
                gap = t_next - time.monotonic()
                if gap > 0:
                    await asyncio.sleep(gap)
                sawtooth.append(await hit(raw, f"saw{i}", fx))
                t_next += step

    align = "미확인"
    align_note = ""
    by_second: dict[str, list[dict]] = {}
    for s in sawtooth:
        if s["status"] == 200 and s["server_date"]:
            by_second.setdefault(s["server_date"], []).append(s)
    graded = [(len(v), v) for v in by_second.values()]
    # 초 경계 정렬 판정: 각 서버 초의 k 번째 호출이 remaining == limit-k 인가.
    hits = miss = 0
    for _n, group in graded:
        for k, s in enumerate(group, start=1):
            lim = _int(s["limit"])
            rem = _int(s["remaining"])
            if lim is None or rem is None:
                continue
            if rem == lim - k:
                hits += 1
            else:
                miss += 1
    if hits + miss >= 8:
        ratio = hits / (hits + miss)
        multi = sum(1 for n, _g in graded if n >= 2)
        if ratio >= 0.9 and multi >= 2:
            align = "고정 1초 창 (서버 초 경계에 정렬)"
        elif ratio <= 0.5:
            align = "고정창 아님 (토큰버킷/연속 충전으로 보임)"
        align_note = (f"샘플 {hits + miss}개 중 '초 안 k번째 → remaining=limit-k' 적중 "
                      f"{hits} ({ratio:.0%}), 2발 이상 들어간 초 {multi}개")

    scope = "미확인"
    scope_note = ""
    ok_pairs = [p for p in same_group
                if p["same_server_second"]
                and _int(p["a"]["remaining"]) is not None
                and _int(p["b"]["remaining"]) is not None]
    if ok_pairs:
        shared = all(_int(p["b"]["remaining"]) == _int(p["a"]["remaining"]) - 1
                     for p in ok_pairs)
        per_ep = all(_int(p["b"]["remaining"]) == _int(p["b"]["limit"]) - 1
                     and _int(p["a"]["remaining"]) == _int(p["a"]["limit"]) - 1
                     for p in ok_pairs)
        if shared and not per_ep:
            scope, scope_note = "그룹 공유", "같은 그룹의 다른 엔드포인트가 같은 카운터를 깎는다"
        elif per_ep and not shared:
            scope, scope_note = "엔드포인트별", "엔드포인트마다 카운터가 따로다"
        elif shared and per_ep:
            scope = "미확인"
            scope_note = ("limit 이 2 라 두 해석이 같은 숫자를 낸다 — 판별 불가"
                          if ok_pairs and _int(ok_pairs[0]["a"]["limit"]) == 2
                          else "두 해석이 모두 성립 — 한도값이 작아 판별 불가")

    window = "미확인"
    window_note = ""
    usable = [r for r in window_rounds
              if r["ab_same_server_second"] and _int(r["c"]["remaining"]) is not None
              and _int(r["c"]["limit"]) is not None]
    if usable:
        fixed = sum(1 for r in usable
                    if _int(r["c"]["remaining"]) == _int(r["c"]["limit"]) - 1)
        sliding = sum(1 for r in usable
                      if _int(r["c"]["remaining"]) <= _int(r["c"]["limit"]) - 2)
        if fixed and not sliding:
            window = "고정 1초 창 (벽시계 정렬)"
        elif sliding and not fixed:
            window = "슬라이딩 1초 창"
        window_note = (f"라운드 {len(usable)}개 중 고정-신호 {fixed} / 슬라이딩-신호 {sliding}. "
                       "C 는 A 로부터 1.00초 뒤 — 고정창이면 새 창의 첫 발이라 remaining=limit-1.")

    return {
        "verdict": "확인됨" if (scope != "미확인" or window != "미확인") else "미확인",
        "window_shape": window,
        "window_note": window_note,
        "window_alignment": align,
        "window_alignment_note": align_note,
        "limit_scope_within_group": scope,
        "limit_scope_note": scope_note,
        "limit_scope_across_groups": cross,
        "aborted": aborted,
        "same_group_rounds": same_group,
        "cross_group_round": cross_group,
        "window_rounds": window_rounds,
        "sawtooth_by_second": {k: [(s["tag"], s["limit"], s["remaining"]) for s in v]
                               for k, v in by_second.items()},
        "all_samples": samples,
    }


async def probe_limiter_holds(c: TossClient) -> dict:
    """리미터를 **전속력으로** 돌려 실제 서버 초당 카운트가 한도를 넘지 않는지 확인한다.

    §9-5 가 보인 경계 2배 통과는 "우리도 그렇게 될 수 있다" 는 뜻이므로, 고친 리미터가
    실제 서버 앞에서 그것을 막는지 라이브로 확인한다. 리미터를 우회하지 않는다 —
    오히려 리미터가 허용하는 최대 속도로 밀어붙인다.

    판정: 어떤 서버 date 초에도 호출 수가 공시 한도를 넘지 않고, 429 가 0건이어야 한다.
    MARKET_INFO(3/s)만 쓴다.
    """
    per_second: dict[str, int] = {}
    calls = 0
    t_end = time.monotonic() + 8.0
    while time.monotonic() < t_end:
        try:
            await c._request("GET", "/api/v1/exchange-rate",
                             params={"baseCurrency": "USD", "quoteCurrency": "KRW"})
        except RateLimited as exc:
            return {"verdict": "미확인", "note": "429 발생 — 리미터가 계약을 못 지켰다",
                    "evidence": exc.evidence, "per_second": per_second}
        calls += 1
        date = c.last_headers.get("date") or "?"
        per_second[date] = per_second.get(date, 0) + 1
    worst = max(per_second.values()) if per_second else 0
    limit = int(c.limiter.limits.get("MARKET_INFO", 3))
    return {
        "verdict": "확인됨" if (worst <= limit and c.counters["http_429"] == 0) else "미확인",
        "calls": calls,
        "max_calls_in_one_server_second": worst,
        "declared_limit": limit,
        "http_429": c.counters["http_429"],
        "note": (f"{calls}콜 동안 한 서버 초 최대 {worst}회 (한도 {limit}), 429 {c.counters['http_429']}건. "
                 "리미터가 고정 1초 창을 지켰다." if worst <= limit
                 else f"한 서버 초에 {worst}회 — 한도 {limit} 초과"),
        "per_second": per_second,
    }


async def probe_ratelimit_groups(c: TossClient) -> dict:
    """그룹마다 **한 번씩** 호출해 `x-ratelimit-limit` 을 읽는다 (SPEC_LIMITS 대조표).

    `SPEC_LIMITS` 의 값들은 문서(docs/01 §2 + overview.md)에서 온 공시값이라 실측 대조가
    필요하다. 그룹당 1콜이라 가동 중 수집기에 영향이 없다. AUTH 는 토큰 발급 경로라
    제외한다(재발급 금지). ASSET 은 allowlist 에 엔드포인트가 없다.
    """
    targets = [
        ("MARKET_DATA", "/api/v1/prices", {"symbols": LIQUID}),
        ("MARKET_DATA_CHART", "/api/v1/candles",
         {"symbol": LIQUID, "interval": "1d", "count": 1}),
        ("RANKING", "/api/v1/rankings",
         {"type": "MARKET_TRADING_VOLUME", "marketCountry": "US",
          "duration": "realtime", "count": 1}),
        ("STOCK", "/api/v1/stocks", {"symbols": LIQUID}),
        ("MARKET_INFO", "/api/v1/exchange-rate",
         {"baseCurrency": "USD", "quoteCurrency": "KRW"}),
        ("ACCOUNT", "/api/v1/accounts", None),
        ("ORDER_INFO", "/api/v1/commissions", None),
    ]
    out: dict[str, dict] = {}
    for group, path, params in targets:
        try:
            await c._request("GET", path, params=params)
        except TossApiError as exc:
            out[group] = {"error": f"{type(exc).__name__}: {exc}"}
            continue
        h = {k.lower(): v for k, v in c.last_headers.items()}
        observed = h.get("x-ratelimit-limit")
        spec = SPEC_LIMITS.get(group)
        out[group] = {
            "path": path,
            "observed_limit": observed,
            "spec_limit": spec,
            "matches": (observed is not None and spec is not None
                        and float(observed) == float(spec)),
            "reset": h.get("x-ratelimit-reset"),
        }
        await asyncio.sleep(0.5)
    confirmed = [g for g, v in out.items() if v.get("matches")]
    mismatched = [g for g, v in out.items()
                  if v.get("observed_limit") is not None and not v.get("matches")]
    return {
        "verdict": "확인됨" if confirmed else "미확인",
        "confirmed_groups": confirmed,
        "mismatched_groups": mismatched,
        "note": (f"공시값과 일치 {len(confirmed)}개, 불일치 {len(mismatched)}개. "
                 "AUTH 는 토큰 발급 경로라 제외(재발급 금지), ASSET 은 allowlist 에 없음."),
        "per_group": out,
    }


async def probe_ratelimit_boundary(c: TossClient) -> dict:
    """고정 1초 창의 **경계 2배 통과**를 실측한다 (429 를 내지 않는 양성 대조).

    `ratelimit_contract` 가 창을 "서버 벽시계 초에 정렬된 고정 1초" 로 확정하면, 그 창의
    고전적 결함이 따라온다: 창 N 의 끝에 limit 발, 창 N+1 의 시작에 limit 발을 쏘면
    **아주 짧은 구간에 2×limit 이 통과**한다. 초당 평균으로는 한도를 지켰는데도
    서버 입장에서 순간 부하는 2배다 — 반대로 우리 쪽 버킷이 연속 시간 기준이면
    같은 이유로 **의도치 않게** 이 상태에 빠질 수 있다.

    그래서 경계를 찾아 양쪽에 limit 발씩 쏜다. 전부 200 이면 2배 통과가 실측된 것이고,
    429 가 나면 즉시 멈춘다(경계 추정이 빗나간 것이므로 결론을 내지 않는다).
    MARKET_INFO(3/s) 만 쓴다 — 컬렉터는 이 그룹을 캘린더 갱신에만, 그것도 TTL 로 드물게 쓴다.
    """
    import httpx

    token = await c.tokens.get()
    check_allowed("GET", "/api/v1/exchange-rate")
    path, params = "/api/v1/exchange-rate", {"baseCurrency": "USD", "quoteCurrency": "KRW"}
    limit_guess = 3
    samples: list[dict] = []

    async def hit(raw, tag: str) -> dict:
        t0 = time.monotonic()
        resp = await raw.get(path, params=params,
                             headers={"Authorization": f"Bearer {token}"})
        h = {k.lower(): v for k, v in resp.headers.items()}
        rec = {"tag": tag, "status": resp.status_code,
               "limit": h.get("x-ratelimit-limit"), "remaining": h.get("x-ratelimit-remaining"),
               "retry_after": h.get("retry-after"), "server_date": h.get("date"),
               "sent_mono": round(t0, 4), "rtt_ms": round((time.monotonic() - t0) * 1000, 1)}
        samples.append(rec)
        return rec

    async with httpx.AsyncClient(base_url=c.base_url, timeout=10.0,
                                 transport=GuardedTransport()) as raw:
        # 1) 보정: 0.4초 간격(2.5/s)으로 쏘며 서버 date 초가 넘어가는 지점을 잡는다.
        boundary = None
        prev = None
        for i in range(6):
            rec = await hit(raw, f"cal{i}")
            if rec["status"] != 200:
                return {"verdict": "미확인", "note": f"보정 중 status={rec['status']}",
                        "samples": samples}
            if prev is not None and rec["server_date"] != prev["server_date"]:
                # 경계는 (prev 수신, 이번 수신] 사이. 이번 호출의 수신 시각을 경계로 본다.
                boundary = rec["sent_mono"] + rec["rtt_ms"] / 2000.0
            prev = rec
            await asyncio.sleep(0.4)
        if boundary is None:
            return {"verdict": "미확인", "note": "서버 초 경계를 잡지 못했다", "samples": samples}

        # 2) 경계 양쪽에 limit 발씩. 여유 0.20초를 둬서 보정 오차를 흡수한다.
        target = boundary
        while target - time.monotonic() < 2.0:
            target += 1.0
        plan = [(-0.30 + 0.05 * i, f"pre{i}") for i in range(limit_guess)]
        plan += [(0.10 + 0.05 * i, f"post{i}") for i in range(limit_guess)]
        for offset, tag in plan:
            gap = (target + offset) - time.monotonic()
            if gap > 0:
                await asyncio.sleep(gap)
            rec = await hit(raw, tag)
            if rec["status"] == 429:
                return {"verdict": "미확인",
                        "note": f"{tag} 에서 429 — 경계 추정이 빗나갔다. 즉시 중단.",
                        "samples": samples}

    burst = [s for s in samples if s["tag"].startswith(("pre", "post"))]
    seconds = {s["server_date"] for s in burst}
    span_s = round(burst[-1]["sent_mono"] - burst[0]["sent_mono"], 3)
    ok = len(burst) == 2 * limit_guess and all(s["status"] == 200 for s in burst) \
        and len(seconds) == 2
    return {
        "verdict": "확인됨" if ok else "미확인",
        "calls": len(burst),
        "span_s": span_s,
        "distinct_server_seconds": len(seconds),
        "note": (f"공시 {limit_guess} req/s 인데 {len(burst)}회가 {span_s}초 안에 전부 200 — "
                 f"고정창 경계에서 2×limit 이 통과한다 (서버 초 {len(seconds)}개에 걸침)."
                 if ok else "경계 양쪽 배치가 깔끔히 나뉘지 않았다 — 결론 보류"),
        "samples": samples,
    }


async def probe_force_429(c: TossClient, burst: int = 12) -> dict:
    """429 재현 + 그때의 헤더값. MARKET_INFO(3 req/s) 를 limiter 우회로 버스트.

    ⚠️ **의도적으로 라이브 429 를 유발한다.** 같은 시각 컬렉터가 돌고 있으면 같은 자격증명
    = 서버 측 같은 버킷이라 그 페널티를 공유한다. 그래서 `--probe all` 에 포함되지 않고
    `--probe force429` 로 **명시했을 때만** 실행된다 (감사 B-5).
    """
    import httpx

    token = await c.tokens.get()
    got: dict = {"attempts": burst, "429_at": None}
    # limiter 는 의도적으로 우회하지만 **allowlist 는 우회하지 않는다** (감사 B-5).
    # 리포 안에 "이중 차단 우회 예제" 를 남기지 않기 위해 명시적으로 관문을 통과시킨다.
    check_allowed("GET", "/api/v1/exchange-rate")
    async with httpx.AsyncClient(base_url=c.base_url, timeout=10.0,
                                 transport=GuardedTransport()) as raw:
        for i in range(burst):
            resp = await raw.get("/api/v1/exchange-rate",
                                 params={"baseCurrency": "USD", "quoteCurrency": "KRW"},
                                 headers={"Authorization": f"Bearer {token}"})
            if resp.status_code == 429:
                got["429_at"] = i + 1
                got["status"] = 429
                got["headers"] = mask_headers(dict(resp.headers))
                got["retry_after_raw"] = resp.headers.get("Retry-After")
                try:
                    got["body"] = mask(resp.json())
                except ValueError:
                    got["body"] = resp.text[:300]
                save_fixture("live_429_exchange_rate.json", "GET /api/v1/exchange-rate",
                             "rate_limited", 429, got["body"], dict(resp.headers),
                             f"MARKET_INFO 그룹 {i+1}번째 연속 호출에서 429 재현")
                break
    if got["429_at"] is None:
        got["verdict"] = "미확인"
        got["note"] = f"{burst}회 연속 호출로 429 재현 실패 (버스트 허용치가 더 큼)"
    else:
        got["verdict"] = "확인됨"
        got["note"] = (f"MARKET_INFO(공시 3 req/s) 연속 호출 {got['429_at']}회째에 429. "
                       f"Retry-After={got.get('retry_after_raw')}")
    # 페널티 반영 — 이후 프로브가 곧바로 다시 때리지 않도록
    if got.get("retry_after_raw"):
        try:
            c.limiter.on_429("MARKET_INFO", float(got["retry_after_raw"]))
        except ValueError:
            c.limiter.on_429("MARKET_INFO", 2.0)
    await asyncio.sleep(2.0)
    return got


async def probe_force_429_client(c: TossClient) -> dict:
    """429 를 **TossClient 경로로** 받아 `client.last_429` 계측을 실증한다.

    ⚠️ **의도적으로 라이브 429 를 유발한다.** `--probe force429_client` 로 명시해야만 실행된다.
    `force429` 와 달리 raw httpx 가 아니라 `_request` 관문을 지나므로, 우리가 실제 운영에서
    쓰는 계측 경로(`_record_429`)가 진짜 429 를 제대로 찍는지 확인한다.

    MARKET_INFO(3/s)만 쓴다 — 실측상 한도는 **그룹별 독립**이라(docs/06 §9-4) 다른 그룹의
    가동 중 수집기에 영향이 없고, 컬렉터가 이 그룹을 쓰는 것은 TTL 기반 캘린더 갱신뿐이다.

    리미터는 이 프로브 동안만 **통째로 비활성**시킨다. 상한만 올리는 방식으로는 429 를 낼 수
    없다 — 첫 응답의 `X-RateLimit-Limit: 3` 을 보고 리미터가 즉시 자기보정해서 도로 3/s 로
    내려가기 때문이다(그 자체가 자기보정이 동작한다는 증거다). 여기서 확인하려는 것은
    리미터가 아니라 **429 를 받았을 때의 기록**이므로 전송 경로만 남기고 제어를 걷어낸다.
    """
    class _NoLimiter:
        limits: dict = {}

        async def acquire(self, group: str) -> None:
            return None

        def update_from_headers(self, group, headers, status=None) -> None:
            return None

        def on_429(self, group, retry_after_s) -> None:
            return None

    saved_limiter = c.limiter
    c.limiter = _NoLimiter()                            # type: ignore[assignment]
    got: dict = {"attempts": 0, "429_at": None}
    try:
        for i in range(8):
            got["attempts"] = i + 1
            try:
                await c._request("GET", "/api/v1/exchange-rate",
                                 params={"baseCurrency": "USD", "quoteCurrency": "KRW"})
            except RateLimited as exc:
                got["429_at"] = i + 1
                got["evidence_on_exception"] = exc.evidence
                break
    finally:
        c.limiter = saved_limiter

    rec = c.last_429
    got["client_last_429"] = rec
    got["counters"] = {k: v for k, v in c.counters.items() if "429" in k}
    if rec is None:
        got["verdict"] = "미확인"
        got["note"] = f"{got['attempts']}회로 429 재현 실패"
    else:
        got["verdict"] = "확인됨"
        # 429_at 이 None 이면 client 내부 재시도가 429 를 전부 흡수했다는 뜻이다
        # (호출자는 예외를 못 본다 — 그래서 카운터·기록이 유일한 흔적이다).
        where = (f"{got['429_at']}번째 호출에서 429 가 호출자까지 전파"
                 if got["429_at"] else
                 f"{got['attempts']}회 중 429 를 client 내부 재시도가 전부 흡수")
        got["note"] = (
            f"{where}. 기록된 status={rec['status']} "
            f"retry_after_present={rec['retry_after_present']} "
            f"error_code={rec['error_code']} "
            f"그 서버 초에 우리가 보낸 요청 수={rec['own_requests_in_that_server_second']}")
        # 픽스처로 남길 때는 기록 안의 헤더도 마스킹한다 — `mask()` 는 본문 필드만 보고
        # 헤더 이름은 모르기 때문에, 중첩된 x-request-id 가 그대로 새어나간다.
        safe = dict(rec)
        safe["headers"] = mask_headers(rec["headers"])
        save_fixture("live_429_client_record.json", "GET /api/v1/exchange-rate",
                     "rate_limited_via_client", 429, {"record": safe},
                     rec["headers"],
                     "TossClient._record_429 이 찍은 진짜 429 기록 (헤더는 그 429 응답의 것)")
    # 페널티를 반영해 두고 잠깐 쉰다 — 뒤따르는 프로브가 곧바로 다시 때리지 않도록.
    await asyncio.sleep(2.0)
    return got


async def probe_empty_symbol(c: TossClient) -> dict:
    """존재하지 않는 심볼 — 에러 형태 확인 (스키마 검증 경로 픽스처)."""
    out = {}
    try:
        body = await c._request("GET", "/api/v1/prices", params={"symbols": "ZZZZNOPE"})
        out = {"http": c.last_status, "verdict": "확인됨",
               "note": "미존재 심볼도 200 + 빈/누락 결과", "evidence": snippet(body)}
        save_fixture("live_prices_unknown_symbol.json", "GET /api/v1/prices",
                     "unknown_symbol", 200, body, c.last_headers,
                     "미존재 심볼 요청 결과")
    except TossApiError as exc:
        out = {"http": c.last_status, "verdict": "확인됨",
               "note": f"미존재 심볼은 {type(exc).__name__}: {exc}"}
    return out


async def probe_candle_deep(c: TossClient) -> dict:
    """1m 보관 상한 심층 확인 + 아주 오래된 구간이 진짜 '1분봉'인지 검증."""
    newest, _ = await _candles_at(c, LIQUID, "1m", None, count=1)
    if not newest:
        return {"verdict": "미확인", "note": "최신 1m 봉 없음"}
    anchor = iso_to_ms(newest[0]["timestamp"])

    hits, misses = {}, []
    for days in (512, 1024, 2048, 4096):
        got, _ = await _candles_at(c, LIQUID, "1m", anchor - days * DAY_MS, count=1)
        if got:
            hits[days] = got[0]["timestamp"]
        else:
            misses.append(days)
            break

    deepest = max(hits) if hits else None
    spacing = None
    sample = []
    if deepest is not None:
        bars, _ = await _candles_at(c, LIQUID, "1m", anchor - deepest * DAY_MS, count=5)
        ts = sorted(iso_to_ms(b["timestamp"]) for b in bars)
        sample = [b["timestamp"] for b in bars]
        if len(ts) > 1:
            gaps = [(ts[i + 1] - ts[i]) // 1000 for i in range(len(ts) - 1)]
            spacing = {"gaps_s": gaps, "is_1m_grid": all(g % 60 == 0 for g in gaps),
                       "all_exactly_60s": all(g == 60 for g in gaps)}

    # 소형주도 같은 기간만큼 1m 봉이 남아 있는지 (유니버스 전체 백필 가능성)
    small_hits = {}
    for days in (30, 180, 365):
        got, _ = await _candles_at(c, SMALL[0], "1m", anchor - days * DAY_MS, count=1)
        if got:
            small_hits[days] = got[0]["timestamp"]

    return {
        "verdict": "확인됨" if hits else "미확인",
        "anchor_kst": ms_to_iso_kst(anchor),
        "deep_days_hit": sorted(hits), "deep_days_miss": sorted(misses),
        "deepest_bar_ts": hits.get(deepest) if deepest else None,
        "deepest_bar_age_days": deepest,
        "spacing_at_deepest": spacing,
        "sample_at_deepest": sample,
        f"smallcap_{SMALL[0]}_1m_hits_days": sorted(small_hits),
        "smallcap_samples": small_hits,
        "note": (f"{LIQUID} 1m 봉이 {deepest}일 전까지 존재" if deepest else "심층 조회 실패"),
    }


async def probe_orderbook_depth(c: TossClient) -> dict:
    """호가 레벨 수가 '항상 1' 인지 유동성 최상위 종목들로 재확인 (설계 전제 검증)."""
    liquid = ["QQQ", "SOXL", "MSFT", "TSLA", "NVDA"]
    per = {}
    for sym in liquid:
        try:
            body = await c._request("GET", "/api/v1/orderbook", params={"symbol": sym})
        except TossApiError as exc:
            per[sym] = {"error": f"{type(exc).__name__}: {exc}"}
            continue
        r = body.get("result") or {}
        per[sym] = {"bid_levels": len(r.get("bids") or []),
                    "ask_levels": len(r.get("asks") or []),
                    "timestamp": r.get("timestamp"),
                    "raw": snippet(r, 300)}
    levels = [v["bid_levels"] for v in per.values() if isinstance(v.get("bid_levels"), int)]
    maxlv = max(levels) if levels else 0
    return {
        "verdict": "확인됨" if levels else "미확인",
        "max_levels_observed": maxlv,
        "distinct_level_counts": sorted(set(levels)),
        "note": ("미국 호가는 최우선 1레벨만 제공됨 — 호가창 깊이 기반 지표 불가"
                 if maxlv <= 1 else f"최대 {maxlv} 레벨 관측"),
        "session": "probe 실행 시점 세션은 calendar 프로브 결과 참조",
        "per_symbol": per,
    }


PROBES = {
    "calendar": probe_calendar,
    "candle_deep": probe_candle_deep,
    "orderbook_depth": probe_orderbook_depth,
    "quote_realtime": probe_quote_realtime,
    "candle_retention": probe_candle_retention,
    "candle_1d": probe_candle_1d,
    "orderbook": probe_orderbook,
    "trades": probe_trades,
    "rankings": probe_rankings,
    "ranking_cadence": probe_ranking_cadence,
    "stocks": probe_stocks,
    "warnings": probe_warnings,
    "price_limits": probe_price_limits,
    "fx": probe_fx,
    "commissions": probe_commissions,
    "ratelimit": probe_ratelimit_headers,
    "ratelimit_contract": probe_ratelimit_contract,
    "group_coupling": probe_group_coupling,
    "ratelimit_groups": probe_ratelimit_groups,
    "limiter_holds": probe_limiter_holds,
    "ratelimit_boundary": probe_ratelimit_boundary,
    "unknown_symbol": probe_empty_symbol,
    "force429": probe_force_429,
    "force429_client": probe_force_429_client,
}

# `--probe all` 에서 제외되는 프로브 — 명시적으로 이름을 적어야만 실행된다 (감사 B-5).
# force429 는 라이브 429 를 **의도적으로** 유발하고, 같은 자격증명을 쓰는 컬렉터가 돌고 있으면
# 서버 측 같은 버킷의 페널티를 공유한다. 기본 실행에 섞여 있을 물건이 아니다.
# ranking_cadence 도 여기 둔다 — 900콜 20분짜리라 `all` 에 섞여 우연히 돌 물건이 아니다.
OPT_IN_ONLY = frozenset({"force429", "force429_client", "group_coupling",
                         "ranking_cadence"})
ALL_PROBES = [n for n in PROBES if n not in OPT_IN_ONLY]


# ------------------------------------------------------------------ main


async def run(args) -> int:
    names = (list(ALL_PROBES) if args.probe == "all"
             else [n.strip() for n in args.probe.split(",") if n.strip()])
    unknown = [n for n in names if n not in PROBES]
    if unknown:
        print(f"unknown probes: {unknown}. available: {sorted(PROBES)}", file=sys.stderr)
        return 2

    global FIXTURE_DIR, COLLECTOR_LOG, BASELINE_MIN
    if args.fixture_dir:
        FIXTURE_DIR = Path(args.fixture_dir)
    if args.collector_log:
        COLLECTOR_LOG = Path(args.collector_log)
    BASELINE_MIN = args.baseline_min
    _IS_LIVE_TARGET[0] = LIVE_HOST_MARKER in args.base_url

    os.environ["TOSS_BASE_URL"] = args.base_url
    if args.reuse_token_state:
        # 가동 중 컬렉터의 토큰을 **읽어서만** 쓴다 — 발급도 무효화도 하지 않는다.
        print(f"[probe] reusing existing token from {args.reuse_token_state} "
              "(발급·리스 획득 안 함)", file=sys.stderr)
        tokens = ReusedTokenManager(Path(args.reuse_token_state))
    else:
        tokens = TokenManager(Path(args.keys), Path(args.state), live=args.live)
    limiter = GroupRateLimiter(LIMITS, usage_ratio=args.usage_ratio)
    client = TossClient(args.base_url, tokens, limiter, timeout_s=args.timeout_s)

    results: dict = {
        "meta": {
            "started_ms": now_ms(),
            "started_kst": ms_to_iso_kst(now_ms()),
            "started_et": ms_to_iso_et(now_ms()),
            "base_url": args.base_url,
            "probes": names,
            "usage_ratio": args.usage_ratio,
        }
    }
    try:
        for name in names:
            t0 = time.monotonic()
            before = client.counters["requests"]
            print(f"[probe] {name} ...", file=sys.stderr, flush=True)
            try:
                results[name] = await PROBES[name](client)
            except (TossApiError, RuntimeError) as exc:
                results[name] = {"verdict": "미확인",
                                 "error": f"{type(exc).__name__}: {exc}"}
            results[name]["_calls"] = client.counters["requests"] - before
            results[name]["_elapsed_s"] = round(time.monotonic() - t0, 1)
            print(f"[probe] {name} done in {results[name]['_elapsed_s']}s "
                  f"({results[name]['_calls']} calls)", file=sys.stderr, flush=True)
    finally:
        results["meta"]["counters"] = dict(client.counters)
        results["meta"]["finished_kst"] = ms_to_iso_kst(now_ms())
        await client.aclose()
        tokens.release()

    payload = json.dumps(results, ensure_ascii=False, indent=2)
    _use_utf8_stdio()
    if args.out:
        Path(args.out).write_text(payload, encoding="utf-8")
        print(f"[probe] wrote {args.out}", file=sys.stderr)
    print(payload)
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Toss Open API 라이브 실측 프로브 (W1 전용)")
    ap.add_argument("--probe", default="all",
                    help="쉼표구분 프로브 이름 또는 all")
    ap.add_argument("--list", action="store_true", help="프로브 목록 출력 후 종료")
    ap.add_argument("--keys", default="api_keys", help="client_id/secret 파일 경로")
    # 상대경로 기본값은 CWD 종속이라 워크트리마다 다른 파일을 가리킨다 (감사 A-1).
    # 리스 자체는 이제 자격증명 유도 경로에 잡히지만, 상태파일도 절대경로로 고정한다.
    ap.add_argument("--state", default=str(default_state_path()),
                    help="토큰 상태파일 (절대경로 권장)")
    # 컬렉터 가동 중 프로브용. 이 API 는 client 당 토큰이 1개라 재발급이 곧 남의 토큰 살해다.
    ap.add_argument("--reuse-token-state", default=None,
                    help="이미 발급된 토큰을 읽어서만 쓴다 (가동 중 컬렉터의 "
                         "token_state.json 경로). 발급·리스 획득·무효화를 하지 않는다.")
    # 기본값 없음 — 계약 C-9. TOSS_BASE_URL 이 없으면 실행을 거부한다 (감사 M-7).
    ap.add_argument("--base-url", default=os.environ.get("TOSS_BASE_URL"))
    ap.add_argument("--out", default=None, help="결과 JSON 저장 경로")
    ap.add_argument("--fixture-dir", default=None,
                    help="픽스처 저장 경로 (기본 tests/fixtures/live). 드라이런은 임시 경로를 쓸 것")
    ap.add_argument("--timeout-s", type=float, default=15.0)
    ap.add_argument("--usage-ratio", type=float, default=0.7)
    # group_coupling 의 대조군 입력. **읽기만** 한다 (컬렉터 미접촉).
    ap.add_argument("--collector-log", default=None,
                    help="가동 중 컬렉터 로그 경로. group_coupling 이 배경 429 기준선과 "
                         "OFF 구간 대조를 뜨는 데 쓴다 (읽기 전용).")
    ap.add_argument("--baseline-min", type=float, default=180.0,
                    help="배경 429 기준선을 뜰 직전 구간 길이(분). 기본 180.")
    # 기본값은 **안전한 쪽**이다 (감사 B-5 / 리스 사고 방지). live 는 --live 와 TOSS_LIVE=1 이
    # 모두 있을 때만 True 가 된다. 이전에는 TokenManager(live=True) 가 하드코딩이라
    # 리스 보유자가 컬렉터를 돌리는 중에 프로브를 띄우면 토큰을 죽였다.
    ap.add_argument("--live", action="store_true",
                    help="라이브 호출 확인 플래그. TOSS_LIVE=1 과 함께 있어야 실행됨")
    ap.add_argument("--fast", action="store_true",
                    help="폴링 간격 축소 (mock 드라이런 전용 — 라이브 결론에 쓰지 말 것)")
    # ranking_cadence 의 팔 구성. 세션별 재측정을 **코드 수정 없이** 발사하기 위한 것 —
    # 코드를 고쳐서 재면 두 세션이 같은 계측이라는 보장이 사라진다.
    ap.add_argument("--cadence-profile", default="full",
                    choices=["full", *CADENCE_PROFILES],
                    help="ranking_cadence 팔 구성. full=프리마켓 본편(876콜), "
                         "open=정규장 축소판(453콜)")
    args = ap.parse_args(argv)

    global CADENCE_PROFILE
    CADENCE_PROFILE = args.cadence_profile
    if args.cadence_profile != "full":
        CADENCE.update(CADENCE_PROFILES[args.cadence_profile])

    if args.fast:
        POLL.update(quote_polls=3, quote_gap_s=1.0, rank_polls=2, rank_gap_s=1.0)
        COUPLING.update(samples_per_group=3, gap_s=0.2, off_s=1.0)
        CADENCE.update(vol_arms=[("1s", 0.2, 2.0), ("2s", 0.4, 1.6), ("4s", 0.8, 1.6)],
                       gain_arms=[("1s", 0.2, 2.0), ("2s", 0.4, 1.6), ("4s", 0.8, 1.6)],
                       strides=(1, 2), call_cap=200, arm_gap_s=0.2)

    _use_utf8_stdio()
    if args.list:
        for n, fn in PROBES.items():
            opt = "  [--probe 로 명시해야 실행]" if n in OPT_IN_ONLY else ""
            print(f"{n:18} {(fn.__doc__ or '').splitlines()[0]}{opt}")
        return 0

    if not args.base_url:
        print("refusing to run: --base-url 또는 TOSS_BASE_URL 이 필요합니다 "
              "(계약 C-9: base URL 에 기본값 없음).", file=sys.stderr)
        return 3

    targets_live_host = LIVE_HOST_MARKER in args.base_url
    env_live = os.environ.get("TOSS_LIVE") == "1"

    # 라이브 발급은 --live 와 TOSS_LIVE=1 이 **둘 다** 있을 때만. 기본은 안전한 쪽이다.
    args.live = bool(args.live and env_live)

    # 토큰 재사용 모드는 **발급 권한이 아니라 실서버 접근 권한**이다. 여전히 명시적
    # 의사표시(TOSS_LIVE=1)를 요구하되, TokenManager 자체를 만들지 않으므로 발급은 불가능하다.
    if args.reuse_token_state:
        if args.live:
            print("refusing to run: --reuse-token-state 와 --live 는 함께 쓸 수 없습니다 "
                  "(재사용 모드는 발급을 하지 않는 것이 요점입니다).", file=sys.stderr)
            return 3
        if not env_live:
            print("refusing to run: --reuse-token-state 는 TOSS_LIVE=1 이 필요합니다.",
                  file=sys.stderr)
            return 3

    if targets_live_host and not (args.live or args.reuse_token_state):
        print("refusing to run: 실서버를 대상으로 하면서 라이브 모드가 아닙니다.\n"
              "라이브 실측은 --live 플래그와 TOSS_LIVE=1 이 모두 필요하고, 라이브 리스 보유\n"
              "워커만 실행할 수 있습니다 (계약 C-11 §2·§3).", file=sys.stderr)
        return 3
    if args.live and not targets_live_host:
        # mock 을 상대로 실발급 경로를 태우는 것은 드라이런에서 정상 — 경고만 남긴다.
        print(f"[probe] note: live=True 이지만 대상이 실서버가 아닙니다 ({args.base_url}) "
              "— 드라이런으로 간주합니다.", file=sys.stderr)
    if not args.live and not args.reuse_token_state:
        print(f"[probe] mock 모드 (live=False, 대상 {args.base_url}). "
              "실토큰 발급을 하지 않습니다.", file=sys.stderr)
    return asyncio.run(run(args))


if __name__ == "__main__":
    raise SystemExit(main())
