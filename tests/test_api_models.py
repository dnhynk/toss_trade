"""계약 C-1(시간)·C-2(가격) 변환 헬퍼 테스트."""
from __future__ import annotations

from decimal import Decimal

import pytest

from tossmon.api.errors import SchemaMismatch
from tossmon.api.models import (
    dec_to_u,
    iso_to_ms,
    ms_to_iso,
    ms_to_iso_et,
    ms_to_iso_kst,
    u_to_dec,
)


@pytest.mark.parametrize("raw,expect_ms", [
    ("2026-03-25T09:30:00.123+09:00", 1774398600123),
    ("2026-03-25T00:30:00.123+00:00", 1774398600123),
    ("2026-03-25T00:30:00.123Z", 1774398600123),
    ("2026-03-24T20:30:00.123-04:00", 1774398600123),   # 같은 순간, 다른 오프셋
])
def test_iso_to_ms_offsets_agree(raw, expect_ms):
    assert iso_to_ms(raw) == expect_ms


@pytest.mark.parametrize("raw", [
    "2026-03-25T09:30:00",        # naive — 계약 C-1 위반
    "2026-03-25 09:30",           # naive
    "not-a-time",
    "",
    "   ",
])
def test_iso_to_ms_rejects_naive_and_garbage(raw):
    with pytest.raises(SchemaMismatch):
        iso_to_ms(raw)


def test_iso_to_ms_rejects_non_string():
    with pytest.raises(SchemaMismatch):
        iso_to_ms(1774398600123)


def test_display_helpers_are_same_instant():
    ts = 1774398600123
    assert ms_to_iso(ts).endswith("+00:00")
    assert ms_to_iso_kst(ts).endswith("+09:00")
    # ET 는 서머타임에 따라 -04:00/-05:00 — 하드코딩하지 않고 왕복만 확인 (계약 C-1)
    for rendered in (ms_to_iso(ts), ms_to_iso_kst(ts), ms_to_iso_et(ts)):
        assert iso_to_ms(rendered) == ts


@pytest.mark.parametrize("raw,expect_u", [
    ("72000", 72_000_000_000),
    ("185.70", 185_700_000),
    ("0.000001", 1),
    ("0", 0),
    (Decimal("1.234567"), 1_234_567),
    ("1041436650000", 1_041_436_650_000_000_000),      # KRW 거래대금 — 기본 정밀도 초과 구간
    ("1E+3", 1_000_000_000),
])
def test_dec_to_u(raw, expect_u):
    assert dec_to_u(raw) == expect_u


@pytest.mark.parametrize("raw", [
    "NaN", "Infinity", "-Infinity", "abc", "", "   ", "1.2.3", "$1.50",
    "-1.5",          # 음수 가격·수량은 유효하지 않다 (계약 A4)
])
def test_dec_to_u_rejects_unparseable(raw):
    """A4 이후에도 '파싱 불가능한 값'은 여전히 SchemaMismatch 다."""
    with pytest.raises(SchemaMismatch):
        dec_to_u(raw)


def test_dec_to_u_rejects_float():
    """float 은 계약 C-2 금지 — 정밀도 손실 원천."""
    with pytest.raises(SchemaMismatch):
        dec_to_u(1.87)


def test_u_to_dec_roundtrip_is_exact():
    for raw in ("0.01", "185.70", "1234567.891234", "0.000001"):
        assert u_to_dec(dec_to_u(raw)) == Decimal(raw)


def test_big_amount_precision_is_not_lost():
    """조 단위 거래대금이 마이크로 단위로도 정확히 왕복해야 한다."""
    raw = "9876543210987.654321"
    assert u_to_dec(dec_to_u(raw)) == Decimal(raw)
