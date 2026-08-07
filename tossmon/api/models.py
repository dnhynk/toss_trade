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
    """랭킹 1행.

    ⚠️ **`amount_u` 는 마이크로달러가 아니라 마이크로'원'(KRW)이다.** 실측 확정 사항이며
    자세한 근거는 `docs/06_live_facts.md` §13 에 있다. 요약:

    - `amount_u / (vol_qu/1e6 x last_u/1e6)` 의 중앙값이 한 스냅샷 안에서 **1440 대**로
      고정되고, 같은 프로브 세션에서 실측한 USD/KRW 환율(1438.56)과 0.7% 안에서 일치한다.
      날짜가 바뀌면 이 비율도 함께 움직인다(2026-07-31 1445.4 -> 08-03 1432.8) — 즉
      고정 상수가 아니라 **환율**이다.
    - 크기 감각: SOXL 한 스냅샷의 amount 는 1.73e9 다. 달러로 읽으면 13초 창에 17억 달러라
      말이 안 되고, 원으로 읽으면 약 120만 달러로 `vol x last`(119.8만 달러)와 맞는다.

    따라서 **이 값을 달러 금액으로 쓰면 약 1440배 과대평가**된다. 달러가 필요하면 환율로
    나눠야 하는데, 우리는 환율을 저장하지 않으므로(수집 DB 에 fx 테이블 없음) 사후 복원이
    불가능하다. 금액 지표가 필요하면 `vol_qu x last_u` 로 직접 만들어 쓰는 편이 정확하다.

    ⚠️ **그 1440배 때문에 int64 를 넘긴다.** `rankings_snap.amount_u` 의 상한(2^63-1)은
    환율 1433 기준 약 **$6.43e9** 이고, 2026-08-05 05:00~05:30 에 대형주 500행이 실제로
    넘겨 클램프됐다(EA/AAPL/NVDA/SPY/MSFT). 마이크로달러였다면 1,201배 여유가 있어 넘지
    않는다 — 오버플로는 통화 표기의 결과다. 클램프된 행은 `amount_u == 9223372036854775807`
    로 골라낼 수 있고, **배제에는 충분하지만 원값 복원은 불가능**하다.
    자세히는 `docs/06_live_facts.md` §13-6·§13-7.

    ⚠️ **`vol_qu` 와 `amount_u` 는 둘 다 누적이 아니다** — 같은 심볼-일 안에서 연속 차분의
    약 28%가 음수다(rolling window 집계). 차분을 "구간 거래량/체결대금"으로 쓰면 안 된다.

    계약 C-2 는 amount 를 마이크로달러로, "KRW 는 등장하지 않는다"고 규정하므로 이 필드는
    현재 계약과 어긋난다. 필드명 변경은 계약 C-3 사항이라 코디네이터 승인 대기 중이며,
    그 전까지는 이 주석과 회귀 테스트로 의미를 고정한다.
    """

    rank: int
    symbol: str
    last_u: int
    base_u: int
    change_rate: float | None
    #: 마이크로주. **누적 아님** (rolling window). 계약 C-2 대로 수량 단위는 맞다.
    vol_qu: int
    #: ⚠️ 마이크로**원(KRW)**. 마이크로달러가 아니다. 위 docstring 과 docs/06 §13 참조.
    amount_u: int


@dataclass(frozen=True, slots=True)
class RankingPage:
    ranking_type: str
    duration: str
    ranked_at_ms: int | None
    rows: list[RankingRow]


#: 랭킹 타입 → **그 타입이 실제로 서비스되는 유일한 `duration`.**
#:
#: `duration` 은 자유 인자가 아니라 **타입의 함수**다. `TOP_GAINERS`·`TOP_LOSERS` 에
#: `realtime` 을 넣으면 400 `unsupported-ranking-duration` 이고(W1 실측, `docs/35` §5-5),
#: 거래량·거래대금 4종은 `realtime` 으로만 받아 왔다.
#:
#: ★ **읽는 쪽의 안전장치가 이 표다.** 한 `ranking_type` 이 정확히 한 `duration` 을
#: 결정하므로, `rankings_snap` 을 `ranking_type` 으로 가르면 `duration` 은 **자동으로**
#: 갈린다 — 한 타입 안에 두 duration 이 섞이는 일이 구조적으로 불가능하다.
#: 반대로 타입을 안 가르고 기간만으로 긁으면 서로 다른 집계창의 행이 한 프레임에 들어온다.
#:
#: ⚠️ **`duration` 이 다르면 `vol_qu`·`amount_u` 의 뜻이 다르다 — 라벨만 다른 게 아니다.**
#: 2026-08-07 21:46 KST 실측(두 목록에 동시에 올라 있던 35종, 같은 순간):
#: `1d` 의 `vol_qu` 가 `realtime` 의 **중앙값 15.4배**, 최대 172.6배였다.
#: `realtime` 은 롤링 창 집계이고 `1d` 는 그보다 훨씬 긴 창이다. 두 duration 의 행을
#: 한 프레임에서 거래량·체결대금으로 비교·정렬·집계하면 **그대로 틀린다.**
#: (`amount_u` 가 micro-KRW 라는 위 경고는 두 duration **모두**에 걸린다 — 같은 실측에서
#: `TOP_GAINERS` 의 비율 중앙값 1401.6 으로 realtime 두 목록 1417.3 / 1412.5 와 같은 척도다.)
RANKING_DURATIONS: dict[str, str] = {
    "MARKET_TRADING_VOLUME": "realtime",
    "TOSS_SECURITIES_TRADING_VOLUME": "realtime",
    "MARKET_TRADING_AMOUNT": "realtime",
    "TOSS_SECURITIES_TRADING_AMOUNT": "realtime",
    "TOP_GAINERS": "1d",
    "TOP_LOSERS": "1d",
}


def duration_for(ranking_type: str) -> str:
    """`ranking_type` 이 서비스되는 `duration`. 미등록 타입은 즉시 실패한다.

    기본값을 주지 않는 것이 요점이다 — 모르는 타입에 `realtime` 을 몰래 끼워 넣으면
    400 을 맞거나(등락률 계열) 뜻이 다른 집계를 조용히 같은 컬럼에 쌓는다.
    """
    try:
        return RANKING_DURATIONS[ranking_type]
    except KeyError:
        raise KeyError(
            f"unknown ranking_type {ranking_type!r} — duration 을 추측하지 않는다. "
            f"RANKING_DURATIONS 에 실측값을 먼저 등록하라") from None


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
