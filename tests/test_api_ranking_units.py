"""랭킹 `amount_u` 의 단위·통화 회귀 테스트 (docs/06 §13).

W3 가 docs/23 §1.1 에서 "amount_u 가 원화로 보인다"고 제기했고 W1 이 실측으로 확정했다.
**`RankingRow.amount_u` 는 마이크로달러가 아니라 마이크로원(KRW)이다.**

이 파일은 그 사실을 라이브 캡처 픽스처로 고정한다. 누가 파서를 바꾸거나 API 가 통화를
바꾸면 여기서 걸린다 — 조용히 1440배 틀린 금액이 분석에 들어가는 것을 막는 것이 목적이다.
라이브 호출은 하지 않는다(픽스처만 사용).
"""
from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "live"

# 같은 프로브 세션(2026-07-30 18:14~18:16 KST)에서 캡처한 실응답.
RANKING_FIXTURES = [
    "live_rankings_market_trading_amount.json",
    "live_rankings_toss_securities_trading_amount.json",
]
FX_FIXTURE = "live_exchange_rate.json"

# 환율 대비 허용 오차. 잔차는 종목별 VWAP/현재가 차이라 몇 % 안이면 정상이다.
FX_TOLERANCE = 0.05


def _body(name: str) -> dict:
    doc = json.loads((FIXTURES / name).read_text(encoding="utf-8"))
    assert doc["source"] == "live", f"{name} 은 라이브 캡처여야 한다 (source={doc['source']})"
    return doc["body"]


def _measured_fx() -> Decimal:
    r = _body(FX_FIXTURE)["result"]
    assert r["baseCurrency"] == "USD" and r["quoteCurrency"] == "KRW"
    return Decimal(r["rate"])


def _ratios(name: str) -> list[Decimal]:
    """tradingAmount / (tradingVolume x lastPrice) — 통화가 같다면 1 이어야 한다."""
    out = []
    for row in _body(name)["result"]["rankings"]:
        vol = Decimal(row["tradingVolume"])
        last = Decimal(row["price"]["lastPrice"])
        amt = Decimal(row["tradingAmount"])
        if vol > 0 and last > 0:
            out.append(amt / (vol * last))
    assert out, f"{name} 에 사용 가능한 행이 없다"
    return sorted(out)


def _median(xs: list[Decimal]) -> Decimal:
    return xs[len(xs) // 2]


@pytest.mark.parametrize("name", RANKING_FIXTURES)
def test_trading_amount_is_not_denominated_in_usd(name):
    """가장 중요한 단언: amount 를 달러로 읽으면 안 된다.

    통화가 같다면 amount / (vol x last) 는 1 근처여야 한다. 실측은 1440 대다.
    """
    med = _median(_ratios(name))
    assert med > 100, (
        f"{name}: amount/(vol*last) 중앙값이 {med:.2f} 다. "
        "1 근처면 달러라는 뜻이고, 그렇다면 docs/06 §13 과 모델 주석을 갱신해야 한다")


@pytest.mark.parametrize("name", RANKING_FIXTURES)
def test_trading_amount_matches_the_measured_fx_rate(name):
    """비율이 같은 세션에서 실측한 USD/KRW 환율과 일치해야 한다 — 이것이 KRW 판정 근거다."""
    fx = _measured_fx()
    med = _median(_ratios(name))
    rel = abs(med - fx) / fx
    assert rel < Decimal(str(FX_TOLERANCE)), (
        f"{name}: 중앙 비율 {med:.2f} 가 실측 환율 {fx} 에서 {rel:.2%} 벗어났다. "
        "통화 표기가 바뀌었을 수 있다")


@pytest.mark.parametrize("name", RANKING_FIXTURES)
def test_ratio_is_tight_across_symbols(name):
    """환율은 종목에 무관하므로 한 스냅샷 안에서 비율이 좁게 모여야 한다.

    넓게 흩어지면 그것은 환율이 아니라 다른 무언가라는 뜻이다.
    """
    rs = _ratios(name)
    p10, p90 = rs[len(rs) // 10], rs[9 * len(rs) // 10]
    assert p90 / p10 < Decimal("1.10"), (
        f"{name}: p10={p10:.2f} p90={p90:.2f} 로 산포가 크다 — 환율 해석이 흔들린다")


def test_both_ranking_types_agree_on_the_same_rate():
    """서로 다른 랭킹 타입이 같은 환율을 보여야 한다 (타입별 통화 차이가 없음을 확인)."""
    meds = [_median(_ratios(n)) for n in RANKING_FIXTURES]
    assert abs(meds[0] - meds[1]) / meds[0] < Decimal("0.02"), \
        f"랭킹 타입별로 비율이 다르다: {meds}"


def test_dollar_reading_is_absurd_by_magnitude():
    """단위 착오를 크기 감각으로도 못박는다.

    amount 를 달러로 읽으면 13초 갱신 창의 거래대금이 vol x last 의 1000배를 넘는다.
    """
    body = _body(RANKING_FIXTURES[0])
    worst = max(body["result"]["rankings"],
                key=lambda r: Decimal(r["tradingAmount"]))
    amt = Decimal(worst["tradingAmount"])
    usd = Decimal(worst["tradingVolume"]) * Decimal(worst["price"]["lastPrice"])
    assert amt / usd > 1000, (
        "달러 해석이 터무니없음을 보이는 단언이 깨졌다 — 실제 통화를 재확인하라")


def test_row_currency_field_says_usd_while_amount_is_krw():
    """함정 자체를 고정한다: 응답의 currency 는 USD 라고 말하지만 amount 는 원화다.

    이 불일치가 이번 오류의 원인이므로, 사라지면(=API 가 고쳤으면) 알아야 한다.
    """
    body = _body(RANKING_FIXTURES[0])
    rows = body["result"]["rankings"]
    assert all(r["currency"] == "USD" for r in rows), "currency 필드가 USD 가 아니게 됐다"
    assert _median(_ratios(RANKING_FIXTURES[0])) > 100, \
        "currency=USD 인데 amount 도 달러가 됐다면 docs/06 §13 을 갱신하라"


def test_model_documents_the_krw_unit():
    """모델 docstring 이 단위를 명시해야 한다 — 다음 사람이 주석 없이 오해하지 않도록."""
    from tossmon.api.models import RankingRow
    doc = RankingRow.__doc__ or ""
    assert "KRW" in doc and "amount_u" in doc, \
        "RankingRow docstring 에서 KRW 경고가 사라졌다"
