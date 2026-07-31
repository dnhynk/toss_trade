"""데이터 모델·변환 헬퍼 — 계약 C-1(시간)·C-2(가격)·C-3(모델).

시간: UTC epoch ms int. 가격/금액: 마이크로달러 int. 수량: 마이크로주 int.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import ROUND_HALF_EVEN, Decimal, InvalidOperation, localcontext
from zoneinfo import ZoneInfo

from .errors import SchemaMismatch

MICRO = 1_000_000

_KST = ZoneInfo("Asia/Seoul")
_ET = ZoneInfo("America/New_York")

# 시간 변환을 정수 연산으로만 하기 위한 상수 (감사 M-2).
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
_ONE_MICROSECOND = timedelta(microseconds=1)

# dec_to_u 는 tradingAmount(KRW 조 단위) 까지 다루므로 기본 컨텍스트 정밀도(28)로는 부족하다.
_DEC_PREC = 60

# ---- 초과 정밀도 반올림 관측 (계약 C-2 개정 A4) -------------------------
#
# 마이크로달러보다 미세한 값을 만나면 거부하지 않고 반올림한다. 다만 **조용히** 넘어가면
# 안 되므로 발생 건수를 여기 누적한다. 조용한 누락이 에러보다 나쁘다는 것이 A4 의 요지다.
# asyncio 단일 스레드에서 갱신되는 것을 전제로 락 없이 쓴다(수집기는 단일 프로세스·단일 루프).
_PRECISION_STATS: dict[str, object] = {
    "rounded": 0,        # 반올림이 실제로 일어난 파싱 횟수
    "parsed": 0,         # dec_to_u 성공 호출 총 횟수 (비율 계산용)
    "last_raw": None,    # 마지막으로 반올림된 원문 (진단용, 시크릿 아님)
    "max_digits": 0,     # 관측된 최대 소수 자릿수
}


def precision_rounded_total() -> int:
    """지금까지 초과 정밀도로 반올림된 파싱 건수."""
    return int(_PRECISION_STATS["rounded"])


def precision_stats() -> dict:
    """반올림 관측치 스냅샷 (리포트·테스트용)."""
    return dict(_PRECISION_STATS)


def reset_precision_stats() -> None:
    _PRECISION_STATS.update(rounded=0, parsed=0, last_raw=None, max_digits=0)


def iso_to_ms(s: str) -> int:
    """오프셋 포함 ISO 8601 → UTC epoch ms. naive 문자열은 SchemaMismatch."""
    if not isinstance(s, str):
        raise SchemaMismatch(f"timestamp must be str, got {type(s).__name__}")
    raw = s.strip()
    if not raw:
        raise SchemaMismatch("empty timestamp")
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise SchemaMismatch(f"unparseable timestamp {raw!r}: {exc}") from exc
    if dt.tzinfo is None or dt.tzinfo.utcoffset(dt) is None:
        raise SchemaMismatch(f"naive timestamp (offset required): {raw!r}")
    return _dt_to_ms(dt)


def _dt_to_ms(dt: datetime) -> int:
    """aware datetime → UTC epoch ms, **부동소수점을 전혀 쓰지 않고** (감사 M-2).

    이전 구현은 `int(dt.timestamp() * 1000)` 이었다. `timestamp()` 가 float 이라
    `...357.9998` 같은 값이 나오고 `int()` 가 357 로 **깎아버린다**.
    `timedelta` 끼리의 나눗셈은 정수 연산이므로 이 오차 자체가 생기지 않는다
    (`round()` 로 고치는 것보다 낫다 — 반올림할 오차를 애초에 만들지 않는다).

    밀리초 미만이 실려 오면 가장 가까운 ms 로 반올림한다(경계는 +∞ 방향).
    토스 API 는 ms 정밀도까지만 주므로 실사용에서는 나머지가 항상 0 이다.
    """
    delta = dt - _EPOCH
    us = delta // _ONE_MICROSECOND      # 정확한 정수 마이크로초
    ms, rem = divmod(us, 1000)          # rem 은 항상 0..999 (Python floor divmod)
    return ms + 1 if rem >= 500 else ms


def ms_to_iso(ts_ms: int) -> str:
    """UTC ISO 8601. 요청 파라미터(`before` 등) 직렬화 전용."""
    return datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).isoformat(
        timespec="milliseconds"
    )


def ms_to_iso_kst(ts_ms: int) -> str:
    """표시/로그 전용. 저장·비교에 사용 금지."""
    return datetime.fromtimestamp(ts_ms / 1000, tz=_KST).isoformat(timespec="milliseconds")


def ms_to_iso_et(ts_ms: int) -> str:
    """표시/로그 전용."""
    return datetime.fromtimestamp(ts_ms / 1000, tz=_ET).isoformat(timespec="milliseconds")


def dec_to_u(s: str | Decimal) -> int:
    """decimal 문자열 → 마이크로 단위 int. 초과 정밀도는 ROUND_HALF_EVEN 반올림 (계약 A4).

    저가 동전주는 `"0.10461614"` 처럼 소수 8자리 가격을 돌려준다(수정주가 파생값·sub-penny
    호가). 이를 거부하면 C-5 정책상 caller 가 그 심볼을 스킵하는데, 하필 그게 이 전략의
    표적 종목군이라 **표적에서만 조용히 데이터가 비는** 최악의 실패가 된다. 그래서 거부 대신
    반올림하고, 반올림 사실은 `precision_stats()` 로 관측 가능하게 남긴다.

    `SchemaMismatch` 는 **파싱 자체가 불가능한 값**에만 쓴다 — 빈 문자열, 숫자 아님, None,
    float(정밀도 손실), NaN/Inf, 그리고 음수(가격·수량·금액에 음수는 유효하지 않다).

    ⚠️ 반환값은 **표시·분석용**이다. 마이크로달러 격자에 맞춰 반올림된 값이므로
    **주문 가격 산출에 쓰지 말 것** (계약 A4 §3). Phase 2 에서 주문가가 필요하면 응답 원문
    문자열을 별도로 보존해 써야 한다.
    """
    if isinstance(s, Decimal):
        d = s
    elif isinstance(s, str):
        raw = s.strip()
        if not raw:
            raise SchemaMismatch("empty decimal string")
        try:
            d = Decimal(raw)
        except InvalidOperation as exc:
            raise SchemaMismatch(f"not a decimal: {s!r}") from exc
    elif isinstance(s, int) and not isinstance(s, bool):
        d = Decimal(s)
    else:
        # float 는 계약 C-2 위반 (정밀도 손실). 정수/문자열/Decimal 만 허용.
        raise SchemaMismatch(f"decimal must be str/Decimal/int, got {type(s).__name__}")
    if not d.is_finite():
        raise SchemaMismatch(f"non-finite decimal: {s!r}")
    if d < 0:
        raise SchemaMismatch(f"negative price/quantity: {s!r}")

    with localcontext() as ctx:
        ctx.prec = _DEC_PREC
        scaled = d.scaleb(6)
        micro = scaled.to_integral_value(rounding=ROUND_HALF_EVEN)
        _PRECISION_STATS["parsed"] = int(_PRECISION_STATS["parsed"]) + 1
        if micro != scaled:
            # 마이크로달러보다 미세한 자리가 있었다 — 반올림했음을 기록한다.
            _PRECISION_STATS["rounded"] = int(_PRECISION_STATS["rounded"]) + 1
            _PRECISION_STATS["last_raw"] = str(s)[:32]
            exponent = d.as_tuple().exponent
            digits = -int(exponent) if isinstance(exponent, int) and exponent < 0 else 0
            if digits > int(_PRECISION_STATS["max_digits"]):
                _PRECISION_STATS["max_digits"] = digits
        return int(micro)


def u_to_dec(u: int) -> Decimal:
    with localcontext() as ctx:
        ctx.prec = _DEC_PREC
        return Decimal(int(u)).scaleb(-6)


@dataclass(frozen=True, slots=True)
class Price:
    symbol: str
    ts_ms: int | None
    last_u: int


@dataclass(frozen=True, slots=True)
class Candle:
    symbol: str
    ts_ms: int
    open_u: int
    high_u: int
    low_u: int
    close_u: int
    vol_qu: int


@dataclass(frozen=True, slots=True)
class CandlePage:
    candles: list[Candle]
    next_before_ms: int | None


@dataclass(frozen=True, slots=True)
class Trade:
    symbol: str
    ts_ms: int
    price_u: int
    qty_u: int


@dataclass(frozen=True, slots=True)
class OrderbookLevel:
    price_u: int
    qty_u: int


@dataclass(frozen=True, slots=True)
class Orderbook:
    symbol: str
    ts_ms: int | None
    bids: list[OrderbookLevel]
    asks: list[OrderbookLevel]


@dataclass(frozen=True, slots=True)
class RankingRow:
    rank: int
    symbol: str
    last_u: int
    base_u: int
    change_rate: float | None
    vol_qu: int
    amount_u: int


@dataclass(frozen=True, slots=True)
class RankingPage:
    ranking_type: str
    duration: str
    ranked_at_ms: int | None
    rows: list[RankingRow]


@dataclass(frozen=True, slots=True)
class StockMeta:
    symbol: str
    name: str
    market: str
    security_type: str
    is_common: bool
    status: str
    list_date: str | None
    shares_outstanding_qu: int


@dataclass(frozen=True, slots=True)
class SessionWindow:
    start_ms: int
    end_ms: int


@dataclass(frozen=True, slots=True)
class UsMarketDay:
    date: str
    day: SessionWindow | None
    pre: SessionWindow | None
    regular: SessionWindow | None
    after: SessionWindow | None
