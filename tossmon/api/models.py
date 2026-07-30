"""데이터 모델·변환 헬퍼 — 계약 C-1(시간)·C-2(가격)·C-3(모델).

시간: UTC epoch ms int. 가격/금액: 마이크로달러 int. 수량: 마이크로주 int.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, localcontext
from zoneinfo import ZoneInfo

from .errors import SchemaMismatch

MICRO = 1_000_000

_KST = ZoneInfo("Asia/Seoul")
_ET = ZoneInfo("America/New_York")

# dec_to_u 는 tradingAmount(KRW 조 단위) 까지 다루므로 기본 컨텍스트 정밀도(28)로는 부족하다.
_DEC_PREC = 60


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
    return int(dt.timestamp() * 1000)


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
    """decimal 문자열 → 마이크로 단위 int. 1e-6 초과 정밀도는 SchemaMismatch."""
    if isinstance(s, Decimal):
        d = s
    elif isinstance(s, str):
        try:
            d = Decimal(s.strip())
        except InvalidOperation as exc:
            raise SchemaMismatch(f"not a decimal: {s!r}") from exc
    elif isinstance(s, int) and not isinstance(s, bool):
        d = Decimal(s)
    else:
        # float 는 계약 C-2 위반 (정밀도 손실). 정수/문자열/Decimal 만 허용.
        raise SchemaMismatch(f"decimal must be str/Decimal/int, got {type(s).__name__}")
    if not d.is_finite():
        raise SchemaMismatch(f"non-finite decimal: {s!r}")
    with localcontext() as ctx:
        ctx.prec = _DEC_PREC
        scaled = d.scaleb(6)
        if scaled != scaled.to_integral_value():
            raise SchemaMismatch(f"precision finer than 1e-6: {s!r}")
        return int(scaled)


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
