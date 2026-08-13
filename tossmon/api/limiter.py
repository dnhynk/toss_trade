"""GroupRateLimiter — 그룹별 토큰버킷 + **고정 1초 창 하드캡** (계약 C-4).

기본 사용률 = 공시 한도 × usage_ratio(0.7). X-RateLimit-* 헤더로 자기보정,
429 시 Retry-After 준수.

서버 계약 (2026-08-04 실측 확정, docs/06 §9-4·§9-5):
- 창은 **서버 벽시계 초에 정렬된 고정 1초**다. 슬라이딩도 토큰버킷도 아니다.
  같은 서버 초 안의 k 번째 호출이 정확히 `remaining = limit-k` 였다 (20/20 적중).
- 한도는 **그룹 공유**다. 같은 그룹의 다른 엔드포인트가 같은 카운터를 깎는다.
  그룹끼리는 독립이다.

그래서 **연속 시간 버킷만으로는 부족하다** (이 파일의 옛 결함):
버킷은 "1초에 평균 r 개" 를 보장하지만 서버는 "벽시계 초마다 최대 L 개" 를 센다.
경계 앞뒤로 몰리면 아주 짧은 구간에 2×L 이 나간다 — 공시 3 req/s 그룹에
**6회를 0.561초 안에 전부 200 으로 통과시킨 것이 실측**이다 (docs/06 §9-5).
반대로 우리가 그 상태에 **의도치 않게** 빠지면 그게 곧 429다.

해결: 버킷은 그대로 두되(초 이하 평활화), 그 위에 **어떤 1초 구간에서도 L 개를 넘지 않는
슬라이딩 하드캡**을 얹는다. 슬라이딩 캡은 모든 1초 구간을 덮으므로 서버의 고정 창도
자동으로 덮는다 — 창 위상을 몰라도 안전하다.

캡 구간을 1.0초가 아니라 `WINDOW_HORIZON_S`(1.15초)로 잡는 이유: 우리가 재는 것은 **보낸 시각**,
서버가 세는 것은 **도착 시각**이다. 실측 RTT 는 54.6~141.0ms(p50 71.0, n=47)이고 편도 지연폭은
약 43ms 이므로, 1.0초 동안 보낸 것들이 서버에는 0.957초 안에 도착할 수 있다.
지연폭보다 넉넉한 0.15초를 더해두면 그 압축을 흡수한다.

자기보정 규칙 (overview.md "Rate Limits" 절 + 감사 B-1/B-2/H-4 반영):
- `X-RateLimit-Limit`      : 서버가 알려주는 초당 한도. **한도를 내리는 데만 쓴다.**
                             공시값(SPEC_LIMITS)보다 큰 값은 채택하지 않는다 — 헤더 의미가
                             바뀌면(예: 분당 쿼터 600) rate 가 60배로 뛰어 429 폭풍이 난다(감사 B-2).
                             **이 규칙은 옳지만 이틀 반 동안 보이지 않았다** — 서버가 08-11
                             12:51 부터 `/api/v1/candles` 에 20/s 를 줬는데 우리는 5/s 로 돌았고
                             그 사실이 어느 줄에도 안 남았다. 그래서 클램프를 **방향별로** 세고
                             (`limit_header_clamped` vs `limit_header_lowered`) 상태가 바뀔 때
                             한 줄을 남긴다 (docs/56). 규칙은 그대로다 — 보이게만 만든다.
- `X-RateLimit-Remaining`  : 버킷 잔량. 우리 추정 잔량보다 작으면 **서버값으로 끌어내린다**
                             (다른 프로세스가 같은 client 를 쓰고 있을 수 있으므로 위로는 올리지 않는다).
- `X-RateLimit-Reset`      : 토큰 1개 재충전까지 예상 초. Remaining=0 일 때 다음 시도 시각으로 쓴다.
- 429 `Retry-After`        : 그 시간만큼 그룹 전체를 정지 + 지수 백오프 배수(2x, 최대 8x).
                             회복은 **경과 시간 기준**이다 — 응답 건수 기준이면 초당 7콜 하는
                             그룹에서 감속 수명이 1초로 소멸한다(감사 H-4).

버스트 정책 (감사 B-1): 용량 == rate 이면 유휴 직후 첫 1초에 `capacity + rate` = 2×rate 가
통과한다(MARKET_DATA 실측 14회, 공시 10/s 초과). 이 프로젝트는 **429 를 사고로 규정**하므로
버스트 여유를 남길 이유가 없다. 용량을 rate 의 일부로 줄이고 버킷을 **빈 상태로 시작**한다.

usage_ratio 의 안전 상한 (W4 예산 모델 입력):
- 하드캡이 생기기 전까지 이 버킷의 1초 최대 통과량은 `capacity + rate = rate × (1+BURST_FRACTION)`
  = `limit × usage_ratio × 1.3` 였다. 이것이 한도를 넘지 않으려면 usage_ratio ≤ 1/1.3 = **0.769**.
  즉 옛 구조에서는 0.7 을 조금만 올려도(0.8) 설계상 한도를 넘었다 — 0.7 은 우연히 안전했다.
  (게다가 `_capacity_for` 의 하한 1.0 때문에 limit ≤ 3 인 그룹은 0.7 에서도 넘었다:
   MARKET_INFO 는 1.0+2.1 = 3.1 > 3, ACCOUNT 는 1.0+0.7 = 1.7 > 1.)
- 하드캡이 생긴 뒤로는 한도 초과가 **구조적으로 불가능**하다. 사용률의 상한은
  `WINDOW_CAP / WINDOW_HORIZON_S = limit / 1.15` 이므로 usage_ratio ≤ **0.87** 이면
  버킷이 계속 병목이고, 그 위로는 하드캡이 병목이 된다.
- 따라서 0.7 → 0.85 로 올릴 여지가 있다 (MARKET_DATA 7.0 → 8.5 req/s, **+21%**).
  다만 이 값은 수집 예산을 바꾸므로 **W4 의 2단계에서 결정**한다. 여기서는 기본값을
  건드리지 않고 근거와 상한만 남긴다.
"""
from __future__ import annotations

import asyncio
import random
import time
from collections import deque
from typing import Callable, Mapping

from .endpoints import DEFAULT_LIMIT, SPEC_LIMITS

# 429 이후 rate 에 곱하는 감속 배수의 상한/회복.
MAX_BACKOFF = 8.0
RECOVER_FACTOR = 0.8      # 회복 1스텝당 배수를 이만큼씩 1.0 쪽으로 되돌린다.
RECOVER_INTERVAL_S = 60.0  # 회복 1스텝의 최소 경과 시간 (감사 H-4: 건수 기준 금지).

# 버킷 용량 = rate × 이 비율 (최소 1.0). 유휴 후 첫 1초 통과량 = capacity + rate.
BURST_FRACTION = 0.3

# 서버 창 길이 (실측: 벽시계 초 정렬 고정 1초).
SERVER_WINDOW_S = 1.0
# 보낸 시각 → 도착 시각 압축을 흡수하는 여유. 실측 편도 지연폭 ≈ 43ms 의 약 3.5배.
CLOCK_SKEW_MARGIN_S = 0.15
# 하드캡을 적용하는 구간 길이. 이 구간 안에서 캡을 지키면 서버의 어떤 1초 창에서도 지켜진다.
WINDOW_HORIZON_S = SERVER_WINDOW_S + CLOCK_SKEW_MARGIN_S


class _Bucket:
    __slots__ = ("rate", "capacity", "tokens", "last", "blocked_until", "backoff",
                 "last_429", "last_recover", "lock", "window_cap", "sent")

    def __init__(self, rate: float, window_cap: int) -> None:
        self.rate = max(rate, 1e-3)
        self.capacity = _capacity_for(self.rate)
        # 빈 상태로 시작한다 — 기동 직후 버스트를 막는다 (감사 B-1).
        self.tokens = 0.0
        self.last = time.monotonic()
        self.blocked_until = 0.0
        self.backoff = 1.0
        self.last_429 = 0.0
        self.last_recover = time.monotonic()
        self.lock = asyncio.Lock()
        # 하드캡: 최근 WINDOW_HORIZON_S 안에 내보낸 요청 시각들. 길이가 window_cap 이면 대기.
        self.window_cap = max(1, int(window_cap))
        self.sent: deque[float] = deque()

    # ---- 고정창 하드캡 ---------------------------------------------------

    def prune(self, now: float) -> None:
        horizon = now - WINDOW_HORIZON_S
        while self.sent and self.sent[0] <= horizon:
            self.sent.popleft()

    def window_wait(self, now: float) -> float:
        """캡에 걸려 있으면 풀릴 때까지 남은 초. 여유가 있으면 0."""
        self.prune(now)
        if len(self.sent) < self.window_cap:
            return 0.0
        return max(self.sent[0] + WINDOW_HORIZON_S - now, 0.0)

    def note_sent(self, now: float) -> None:
        self.sent.append(now)

    def set_window_cap(self, cap: int) -> None:
        self.window_cap = max(1, int(cap))

    def effective_rate(self) -> float:
        return max(self.rate / self.backoff, 1e-3)

    def refill(self, now: float) -> None:
        elapsed = max(now - self.last, 0.0)
        self.last = now
        self.tokens = min(self.capacity, self.tokens + elapsed * self.effective_rate())

    def recover(self, now: float) -> None:
        """429 감속을 경과 시간 기준으로 푼다 (감사 H-4)."""
        if self.backoff <= 1.0:
            return
        steps = int((now - self.last_recover) // RECOVER_INTERVAL_S)
        if steps <= 0:
            return
        self.last_recover = now
        self.backoff = max(1.0, self.backoff * (RECOVER_FACTOR ** steps))


def _capacity_for(rate: float) -> float:
    return max(1.0, rate * BURST_FRACTION)


class GroupRateLimiter:
    def __init__(self, limits: dict[str, float], usage_ratio: float = 0.7):
        self.limits = dict(limits)
        self.usage_ratio = float(usage_ratio)
        self._buckets: dict[str, _Bucket] = {}
        # 헤더 자기보정의 **방향을 가른 관측치** (docs/56).
        #
        # 둘을 한 카운터로 세면 안 된다 — 뜻이 정반대다:
        #  · `limit_header_clamped` : 서버가 천장보다 **높게** 줬는데 우리가 잘랐다.
        #    **이게 보고 대상이다.** 서버 한도가 올라간 걸 우리만 모르고 있다는 뜻이고,
        #    (a) 실제로 사양이 올라갔거나 (b) 헤더 의미가 초당→분당으로 바뀌었거나 둘 중 하나다.
        #    어느 쪽이든 사람이 판단해야 하는 사건이지, 자동으로 따라 올라갈 일이 아니다.
        #  · `limit_header_lowered` : 서버가 천장보다 **낮게** 줘서 우리가 내려갔다.
        #    **정상 경로다.** 이걸 위와 같이 세면 평상시 값이 커져서 (a)/(b) 가 묻힌다.
        #    여기 남기는 이유는 하나 — 둘 다 0 이면 "클램프가 없다" 가 아니라
        #    "헤더를 아예 못 읽고 있다" 일 수 있기 때문이다. 그 구분이 안 되면
        #    이번(08-11~08-13, 서버 20/s 를 이틀 반 동안 모르고 지나간 건)이 반복된다.
        self.counters: dict[str, int] = {
            "limit_header_clamped": 0,
            "limit_header_lowered": 0,
            # 아래 `on_clamp_change` 콜백이 던진 예외 수. 관측 코드가 요청 경로를 죽이면
            # 안 되므로 삼키되, 삼킨 사실까지 조용해지지는 않게 센다.
            "clamp_log_failures": 0,
        }
        self.last_clamped: dict | None = None
        # 그룹별 클램프 상태. **그룹을 모르면 쓸모가 없다** — 어느 엔드포인트 묶음이
        # 잘리는지가 곧 얼마나 손해인지다. `header_max` 는 서버가 준 최대값이라
        # "5 로 잘렸다" 와 "20 이 5 로 잘렸다" 를 구분한다.
        self.clamped: dict[str, dict[str, float | int | bool]] = {}
        # 상태 전이(안 잘림↔잘림) 한 줄을 사람이 보는 채널로 내보내는 훅.
        # limiter 는 로거를 모른다 — `logging` 을 직접 쓰면 컬렉터가 root 핸들러를
        # 설정하지 않으므로 그 줄이 `collector.log` 에 **안 남는다**(실측). 그래서 주입한다.
        self.on_clamp_change: Callable[[str, dict], None] | None = None

    # ---- internals ------------------------------------------------------

    def _declared_limit(self, group: str) -> float:
        """공시 한도. config 에 없는 그룹은 스펙 표 → 보수적 기본값 순으로 보강."""
        if group in self.limits:
            return float(self.limits[group])
        return float(SPEC_LIMITS.get(group, DEFAULT_LIMIT))

    def _bucket(self, group: str) -> _Bucket:
        b = self._buckets.get(group)
        if b is None:
            declared = self._declared_limit(group)
            # 하드캡은 **서버 공시 한도 그대로**다. usage_ratio 를 곱하지 않는다 —
            # 이건 예산이 아니라 "서버가 절대 거부하지 않는 선" 이라는 안전망이고,
            # 예산 조절은 버킷 rate 가 맡는다. 둘을 섞으면 정수 내림 때문에
            # 작은 그룹(limit 3 → 2)이 이유 없이 손해를 본다.
            b = _Bucket(declared * self.usage_ratio, window_cap=int(declared))
            self._buckets[group] = b
        return b

    def _spec_ceiling(self, group: str) -> float:
        """서버 헤더로도 넘을 수 없는 공시 한도 상한 (감사 B-2).

        config 로 받은 값과 스펙 표 중 **큰 쪽**을 천장으로 쓴다 — 운영자가 config 로
        공시값보다 높게 잡았다면 그건 사람의 의도적 결정이므로 존중하되, 서버 헤더 한 줄이
        그 위로 밀어올리는 것은 막는다.
        """
        candidates = [float(SPEC_LIMITS.get(group, DEFAULT_LIMIT))]
        if group in self.limits:
            candidates.append(float(self.limits[group]))
        return max(candidates)

    # ---- 클램프 관측 (docs/56) ------------------------------------------

    def _note_header_limit(self, group: str, header_limit: float, ceiling: float) -> None:
        """헤더 한도 1건을 **방향별로** 계상하고, 상태가 바뀐 그룹만 한 줄 알린다.

        `header_limit == ceiling` 은 어느 쪽도 아니다 — 평시 정상이라 세지 않는다.
        """
        above = header_limit > ceiling
        st = self.clamped.get(group)
        if st is None:
            st = {"count": 0, "active": False, "ceiling": ceiling,
                  "header": 0.0, "header_max": 0.0}
            self.clamped[group] = st
        st["ceiling"] = ceiling

        if above:
            self.counters["limit_header_clamped"] += 1
            st["count"] = int(st["count"]) + 1
            st["header"] = header_limit
            st["header_max"] = max(float(st["header_max"]), header_limit)
            self.last_clamped = {"group": group, "header": header_limit, "ceiling": ceiling}
        elif header_limit < ceiling:
            self.counters["limit_header_lowered"] += 1

        # **전이일 때만** 내보낸다. 매 응답마다 찍으면 그건 로그가 아니라 소음이고,
        # 소음이 되는 순간 워치독의 텔레메트리 탐지가 다시 묻힌다 (notifier.promotion 의 교훈).
        if above != bool(st["active"]):
            st["active"] = above
            self._emit_clamp_change(group, st)

    def _emit_clamp_change(self, group: str, state: dict) -> None:
        cb = self.on_clamp_change
        if cb is None:
            return
        try:
            cb(group, dict(state))
        except Exception:       # 관측이 요청 경로를 죽이면 안 된다 — 삼키되 센다.
            self.counters["clamp_log_failures"] += 1

    def clamp_report(self) -> dict[str, object]:
        """텔레메트리 한 줄에 실을 요약.

        `limit_header_clamped_groups` 는 **한 번이라도 위로 잘린** 그룹만 담는다:
        `GROUP:서버최대>천장x횟수`. 정상 하향(`limit_header_lowered`)은 여기 안 들어온다.
        """
        parts = [
            f"{g}:{_short(float(st['header_max']))}>{_short(float(st['ceiling']))}"
            f"x{int(st['count'])}"
            for g, st in sorted(self.clamped.items()) if int(st["count"]) > 0
        ]
        return {
            "limit_header_clamped": int(self.counters["limit_header_clamped"]),
            "limit_header_clamped_groups": ",".join(parts) or "-",
            "limit_header_lowered": int(self.counters["limit_header_lowered"]),
        }

    def snapshot(self, group: str) -> dict[str, float]:
        """관측/테스트용 상태 덤프."""
        b = self._bucket(group)
        now = time.monotonic()
        b.prune(now)
        return {
            "rate": b.rate,
            "effective_rate": b.effective_rate(),
            "capacity": b.capacity,
            "tokens": b.tokens,
            "backoff": b.backoff,
            "blocked_for_s": max(b.blocked_until - now, 0.0),
            "window_cap": float(b.window_cap),
            "window_used": float(len(b.sent)),
            "window_horizon_s": WINDOW_HORIZON_S,
            "sustained_rate": self.sustained_rate(group),
        }

    def sustained_rate(self, group: str) -> float:
        """이 그룹에서 **지속 가능한** 초당 호출 수 (W4 예산 모델이 쓸 값).

        버킷 rate 와 하드캡(cap/horizon) 중 **작은 쪽**이 실제 한도다. 기본 설정에서는
        버킷이 병목이라 값이 예전과 같지만, usage_ratio 를 0.87 위로 올리면 하드캡이
        병목이 되므로 이 함수를 봐야 한다.
        """
        b = self._bucket(group)
        return min(b.rate, b.window_cap / WINDOW_HORIZON_S)

    # ---- contract surface ----------------------------------------------

    async def acquire(self, group: str) -> None:
        """해당 그룹 슬롯 확보까지 대기.

        세 관문을 모두 통과해야 나간다:
        1. 429 페널티(`blocked_until`),
        2. **고정 1초 창 하드캡** — 최근 WINDOW_HORIZON_S 안에 공시 한도만큼 이미 나갔으면 대기,
        3. 토큰버킷 — 초 이하 평활화 + 예산(usage_ratio).

        락은 대기 중에도 잡고 있는다. 여러 루프가 같은 그룹을 동시에 때려도 이 락 때문에
        직렬화되므로, "평균은 낮은데 같은 초에 겹쳐서 초과" 하는 일이 **한 프로세스 안에서는**
        일어나지 않는다. 프로세스가 여럿이면 이 락으로는 못 막는다 (docs/06 §9-6, W4 요건).
        """
        b = self._bucket(group)
        async with b.lock:
            while True:
                now = time.monotonic()
                if now < b.blocked_until:
                    await asyncio.sleep(b.blocked_until - now)
                    continue
                wait = b.window_wait(now)
                if wait > 0.0:
                    await asyncio.sleep(wait)
                    continue
                b.refill(now)
                if b.tokens >= 1.0:
                    b.tokens -= 1.0
                    b.note_sent(now)
                    return
                await asyncio.sleep((1.0 - b.tokens) / b.effective_rate())

    def update_from_headers(self, group: str, headers: Mapping[str, str],
                            status: int | None = None) -> None:
        """X-RateLimit-Limit/Remaining/Reset 실측 반영.

        `status=429` 인 응답의 `remaining` 은 **믿지 않는다**. 실측에서 429 응답이
        `limit=5, remaining=4` 를 달고 온 적이 있다 (docs/06 §9-3) — 창 경계에서 렌더되면
        헤더가 거절된 창이 아니라 **다음 창**의 상태를 가리킬 수 있다. 그 값을 잔량으로
        받아들이면 방금 거절당한 그룹에 오히려 여유가 있다고 착각한다.
        429 의 감속은 `on_429()` 가 전담한다.
        """
        b = self._bucket(group)
        limit = _num(headers, "X-RateLimit-Limit")
        remaining = _num(headers, "X-RateLimit-Remaining")
        reset = _num(headers, "X-RateLimit-Reset")

        if limit is not None and limit > 0:
            # 서버값은 한도를 **내리는 데만** 쓴다 (감사 B-2). 헤더 의미가 초당→분당 쿼터로
            # 바뀌기만 해도(600 = 10/s, 같은 뜻) rate 가 60배로 뛰기 때문이다.
            ceiling = self._spec_ceiling(group)
            effective_limit = min(limit, ceiling)
            self._note_header_limit(group, limit, ceiling)
            target = effective_limit * self.usage_ratio
            if abs(target - b.rate) > 1e-9:
                b.rate = max(target, 1e-3)
                b.capacity = _capacity_for(b.rate)
                b.tokens = min(b.tokens, b.capacity)
            # 하드캡도 서버가 알려준 한도로 맞춘다 (내리는 쪽으로만 — ceiling 이 이미 막는다).
            b.set_window_cap(int(effective_limit))
            # 기록도 클램프된 값으로 — 다음 버킷 생성이 오염되지 않게.
            self.limits[group] = effective_limit

        if remaining is not None and status != 429:
            # 서버 잔량은 우리 추정치의 상한. 위로 올리지 않는다.
            allowed = remaining * self.usage_ratio
            b.tokens = min(b.tokens, max(allowed, 0.0))
            if remaining <= 0 and reset is not None and reset > 0:
                b.blocked_until = max(b.blocked_until, time.monotonic() + reset)

        # 429 감속 회복은 **경과 시간** 기준이다 (감사 H-4). 성공 응답 건수로 풀면
        # 초당 7콜 하는 그룹에서 감속 수명이 1초가 된다.
        b.recover(time.monotonic())

    def on_429(self, group: str, retry_after_s: float) -> None:
        b = self._bucket(group)
        now = time.monotonic()
        wait = max(float(retry_after_s), 0.0)
        jitter = random.uniform(0.0, 0.25)
        b.blocked_until = max(b.blocked_until, now + wait + jitter)
        b.tokens = 0.0
        b.backoff = min(MAX_BACKOFF, b.backoff * 2.0)
        b.last_429 = now
        # 회복 타이머를 지금부터 다시 센다 — 429 직후 곧바로 풀리지 않도록.
        b.last_recover = now


def _short(x: float) -> str:
    """텔레메트리 한 줄은 공백으로 갈리므로 값에 공백이 없어야 한다. 20.0 → 20."""
    return str(int(x)) if float(x).is_integer() else f"{x:g}"


def _num(headers: Mapping[str, str], name: str) -> float | None:
    """헤더 값을 float 로. 대소문자·공백·비수치 값에 관대하게."""
    raw = headers.get(name)
    if raw is None:
        lowered = {k.lower(): v for k, v in headers.items()}
        raw = lowered.get(name.lower())
    if raw is None:
        return None
    try:
        return float(str(raw).strip())
    except (TypeError, ValueError):
        return None
