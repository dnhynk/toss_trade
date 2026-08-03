from __future__ import annotations

import sys
from dataclasses import replace
from pathlib import Path

import pandas as pd
import pytest

from tossmon.api.errors import SchemaMismatch
from tossmon.api.models import Candle, CandlePage, Price, StockMeta
from tossmon.config import UniverseConfig
from tossmon.store.reader import Reader
from tossmon.store.writer import Store
from tossmon.universe import __main__ as universe_main
from tossmon.universe import build as build_module
from tossmon.universe import seed as seed_module
from tossmon.universe.build import build_universe
from tossmon.universe.filters import market_cap_u, passes_tier0
from tossmon.universe.runners import detect_former_runners, tag_dilution_stub
from tossmon.universe.seed import fetch_symbol_directory, parse_directory_file

TESTS_DIR = Path(__file__).resolve().parent
if str(TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(TESTS_DIR))

from tests.test_api_support import mock_server  # noqa: E402,F401


@pytest.fixture
def cfg() -> UniverseConfig:
    return UniverseConfig(
        price_min_u=100_000,
        price_max_u=20_000_000,
        mcap_min_u=10_000_000_000_000,
        mcap_max_u=300_000_000_000_000,
        tier1_max=200,
        tier2_max=50,
        tier3_max=10,
    )


def _meta(symbol: str = "ABCD", shares: int = 10_000_000) -> StockMeta:
    return StockMeta(
        symbol=symbol,
        name=f"{symbol} Corp",
        market="NASDAQ",
        security_type="STOCK",
        is_common=True,
        status="ACTIVE",
        list_date="2020-01-01",
        shares_outstanding_qu=shares * 1_000_000,
    )


@pytest.mark.parametrize(
    ("price_u", "shares", "expected"),
    [
        (100_000, 100_000_000, True),   # exact $0.10 and $10M
        (20_000_000, 15_000_000, True), # exact $20 and $300M
        (99_999, 100_000_000, False),
        (20_000_001, 15_000_000, False),
        (100_000, 99_999_999, False),
        (20_000_000, 15_000_001, False),
    ],
)
def test_tier0_filter_boundaries(cfg, price_u, shares, expected):
    meta = _meta(shares=shares)
    price = Price(meta.symbol, None, price_u)
    assert passes_tier0(meta, price, cfg) is expected


def test_filter_rejects_non_common_inactive_and_funds(cfg):
    price = Price("ABCD", None, 1_000_000)
    base = _meta(shares=20_000_000)
    assert market_cap_u(base, price) == 20_000_000_000_000
    assert not passes_tier0(replace(base, is_common=False), price, cfg)
    assert not passes_tier0(replace(base, status="HALTED"), price, cfg)
    assert not passes_tier0(
        replace(base, security_type="ETF", is_common=True), price, cfg
    )
    assert not passes_tier0(
        replace(base, security_type="ETN", is_common=True), price, cfg
    )


def test_parse_nasdaq_directory_handles_footer_etf_tests_and_duplicates(tmp_path):
    directory = tmp_path / "nasdaqlisted.txt"
    directory.write_text(
        "\ufeffSymbol|Security Name|Market Category|Test Issue|Financial Status|"
        "Round Lot Size|ETF|NextShares\n"
        "GOOD|Good Inc|Q|N|N|100|N|N\n"
        "GOOD|Duplicate|Q|N|N|100|N|N\n"
        "TEST|Test issue|Q|Y|N|100|N|N\n"
        "FUND|Fund ETF|G|N|N|100|Y|N\n"
        "BAD SYMBOL|Malformed|Q|N|N|100|N|N\n"
        "File Creation Time: 0730202618|||||||\n",
        encoding="utf-8",
    )
    assert parse_directory_file(directory) == ["GOOD"]


def test_parse_directory_rejects_dollar_sign_preferred_shares(tmp_path, caplog):
    """라이브 사고 재현: NASDAQ 디렉토리는 우선주를 `ABR$D` 형식으로 적는데 `$` 는
    토스 API 문자셋 밖이다 — 배치에 하나만 섞여도 그 배치 전체가 400 으로 죽는다."""
    directory = tmp_path / "nasdaqlisted.txt"
    directory.write_text(
        "Symbol|Security Name|Market Category|Test Issue|Financial Status|"
        "Round Lot Size|ETF|NextShares\n"
        "GOOD|Good Inc|Q|N|N|100|N|N\n"
        "ABR$D|Arbor Realty Pfd D|Q|N|N|100|N|N\n"
        "ABR$E|Arbor Realty Pfd E|Q|N|N|100|N|N\n"
        "ABR$F|Arbor Realty Pfd F|Q|N|N|100|N|N\n"
        "ACP$A|AllianzGI Pfd A|Q|N|N|100|N|N\n",
        encoding="utf-8",
    )
    with caplog.at_level("INFO", logger="tossmon.universe.seed"):
        result = parse_directory_file(directory)
    assert result == ["GOOD"]
    assert any("4 symbol" in record.message for record in caplog.records)


def test_parse_otherlisted_uses_act_symbol_and_rejects_bad_header(tmp_path):
    directory = tmp_path / "otherlisted.txt"
    directory.write_text(
        "ACT Symbol|Security Name|Exchange|CQS Symbol|ETF|Round Lot Size|"
        "Test Issue|NASDAQ Symbol\n"
        "BRK.B|Berkshire|N|BRK.B|N|100|N|BRK.B\n",
        encoding="utf-8",
    )
    assert parse_directory_file(directory) == ["BRK.B"]
    directory.write_text("Unknown|ETF\nX|N\n", encoding="utf-8")
    with pytest.raises(ValueError, match="symbol column"):
        parse_directory_file(directory)


def test_former_runner_detection_and_dilution_stub():
    day = 86_400_000
    frame = pd.DataFrame(
        [
            {"symbol": "UP", "ts_ms": day, "open_u": 100, "high_u": 130, "low_u": 90},
            {"symbol": "UP", "ts_ms": 2 * day, "open_u": 100, "high_u": 129, "low_u": 70},
            {"symbol": "FLAT", "ts_ms": 2 * day, "open_u": 100, "high_u": 129, "low_u": 71},
            {"symbol": "OLD", "ts_ms": 0, "open_u": 100, "high_u": 200, "low_u": 50},
        ]
    )
    result = detect_former_runners(frame, lookback_days=1)
    assert result.to_dict("records") == [
        {"symbol": "UP", "event_count": 2, "last_event_ms": 2 * day}
    ]
    assert tag_dilution_stub(["UP", "UP", "FLAT"]) == {"UP": {}, "FLAT": {}}


class FakeClient:
    def __init__(self):
        self.stock_batch_sizes: list[int] = []
        self.price_batch_sizes: list[int] = []

    async def get_stocks(self, symbols):
        self.stock_batch_sizes.append(len(symbols))
        return [_meta(symbol, shares=20_000_000) for symbol in symbols]

    async def get_prices(self, symbols):
        self.price_batch_sizes.append(len(symbols))
        return [Price(symbol, 1, 1_000_000) for symbol in symbols]

    async def get_candles(self, symbol, interval, count=200, before_ms=None, adjusted=True):
        high = 1_400_000 if symbol == "S000" else 1_100_000
        row = Candle(symbol, 1, 1_000_000, high, 900_000, 1_000_000, 1_000_000)
        return CandlePage([row], None)


@pytest.mark.asyncio
async def test_build_batches_200_and_persists_tiers(monkeypatch, tmp_path, cfg):
    symbols = [f"S{i:03d}" for i in range(201)]
    monkeypatch.setattr(build_module, "fetch_symbol_directory", lambda _path: symbols)
    client = FakeClient()
    db_path = tmp_path / "monitor.db"
    with Store(db_path) as store:
        summary = await build_universe(client, store, cfg)

    assert client.stock_batch_sizes == [200, 1]
    assert client.price_batch_sizes == [200, 1]
    assert summary == {
        "tier0": 201, "tier1": 200, "former_runners": 1,
        "rejected_charset": 0, "skipped_batches": 0, "skipped_symbols": 0,
    }
    with Reader(db_path) as reader:
        symbols_frame = reader.symbols()
        assert len(symbols_frame) == 201
        assert symbols_frame.loc[
            symbols_frame["symbol"] == "S000", "is_former_runner"
        ].item() == 1
        assert len(reader.symbols(tier=1)) == 200


@pytest.mark.asyncio
async def test_build_universe_is_idempotent_on_rerun(monkeypatch, tmp_path, cfg):
    """일 1회 실행 전제이지만 재실행(수동 재시도 포함)해도 상태가 불어나선 안 된다."""
    symbols = [f"S{i:03d}" for i in range(5)]
    monkeypatch.setattr(build_module, "fetch_symbol_directory", lambda _path: symbols)
    client = FakeClient()
    db_path = tmp_path / "monitor.db"
    with Store(db_path) as store:
        first = await build_universe(client, store, cfg)
        second = await build_universe(client, store, cfg)
        third = await build_universe(client, store, cfg)

        expected = {
            "tier0": 5, "tier1": 5, "former_runners": 1,
            "rejected_charset": 0, "skipped_batches": 0, "skipped_symbols": 0,
        }
        assert first == second == third == expected
        assert store._conn.execute("SELECT COUNT(*) FROM symbols").fetchone()[0] == 5
        assert store._conn.execute("SELECT COUNT(*) FROM candles_1d").fetchone()[0] == 5
        assert store._conn.execute("SELECT COUNT(*) FROM promotions").fetchone()[0] == 0


class FailingCandleClient(FakeClient):
    """get_candles 가 첫 호출에서 실패한다 — 파이프라인 중간 실패 시뮬레이션."""

    def __init__(self):
        super().__init__()
        self.candle_calls = 0

    async def get_candles(self, symbol, interval, count=200, before_ms=None, adjusted=True):
        self.candle_calls += 1
        if self.candle_calls == 1:
            raise RuntimeError("simulated network failure mid-build")
        return await super().get_candles(
            symbol, interval, count=count, before_ms=before_ms, adjusted=adjusted
        )


@pytest.mark.asyncio
async def test_build_universe_partial_failure_preserves_state_and_recovers(
    monkeypatch, tmp_path, cfg
):
    """tier0 upsert 는 candle 루프보다 먼저 커밋된다 — 중간 실패 후에도 그 상태는
    보존돼야 하고, 다음 성공 실행이 정상 최종 상태로 회복해야 한다."""
    symbols = [f"S{i:03d}" for i in range(3)]
    monkeypatch.setattr(build_module, "fetch_symbol_directory", lambda _path: symbols)
    db_path = tmp_path / "monitor.db"

    with Store(db_path) as store:
        failing_client = FailingCandleClient()
        with pytest.raises(RuntimeError, match="simulated network failure"):
            await build_universe(failing_client, store, cfg)

        # tier0 upsert (build.py:50) ran before the candle loop (build.py:52-67)
        # that failed — it must have survived the aborted run untouched.
        assert store._conn.execute("SELECT COUNT(*) FROM symbols").fetchone()[0] == 3
        assert store._conn.execute("SELECT COUNT(*) FROM candles_1d").fetchone()[0] == 0
        assert store._conn.execute(
            "SELECT COUNT(*) FROM symbols WHERE is_former_runner=1"
        ).fetchone()[0] == 0

        working_client = FakeClient()
        summary = await build_universe(working_client, store, cfg)
        assert summary == {
            "tier0": 3, "tier1": 3, "former_runners": 1,
            "rejected_charset": 0, "skipped_batches": 0, "skipped_symbols": 0,
        }
        assert store._conn.execute("SELECT COUNT(*) FROM symbols").fetchone()[0] == 3


@pytest.mark.asyncio
async def test_build_universe_defensively_rejects_bad_charset_symbols(
    monkeypatch, tmp_path, cfg
):
    """seed.py 가 이미 걸러내지만, 심볼 소스가 바뀌거나 캐시가 오염돼도 build.py 자체가
    /stocks 호출 전에 문자셋을 다시 검증해 배치 하나가 통째로 죽지 않게 하는 두 번째
    방어선을 증명한다."""
    symbols = ["S000", "S001", "ABR$D", "ACP$A"]
    monkeypatch.setattr(build_module, "fetch_symbol_directory", lambda _path: symbols)
    client = FakeClient()
    db_path = tmp_path / "monitor.db"
    with Store(db_path) as store:
        summary = await build_universe(client, store, cfg)

    assert summary["rejected_charset"] == 2
    assert summary["tier0"] == 2
    # The rejected symbols must never even reach the client.
    assert client.stock_batch_sizes == [2]
    assert client.price_batch_sizes == [2]


class OneBadBatchClient(FakeClient):
    """첫 `/stocks` 배치가 SchemaMismatch(라이브의 http-400) 로 실패한다."""

    async def get_stocks(self, symbols):
        self.stock_batch_sizes.append(len(symbols))
        if len(self.stock_batch_sizes) == 1:
            raise SchemaMismatch("http-400 code=invalid-request message=요청 필드가 올바르지 않습니다")
        return [_meta(symbol, shares=20_000_000) for symbol in symbols]


@pytest.mark.asyncio
async def test_build_universe_isolates_a_bad_batch_instead_of_aborting(
    monkeypatch, tmp_path, cfg
):
    """라이브 실패 재현: 배치 하나가 400 으로 죽어도 유니버스 빌드 전체가 중단되지
    않고, 나머지 배치는 계속 진행돼 결과에 반영돼야 한다. 스킵은 요약에 드러나야 한다
    (조용한 스킵 금지)."""
    symbols = [f"S{i:03d}" for i in range(201)]  # 2 batches: 200 + 1
    monkeypatch.setattr(build_module, "fetch_symbol_directory", lambda _path: symbols)
    client = OneBadBatchClient()
    db_path = tmp_path / "monitor.db"
    with Store(db_path) as store:
        summary = await build_universe(client, store, cfg)

    assert summary["skipped_batches"] == 1
    assert summary["skipped_symbols"] == 200
    assert summary["tier0"] == 1
    with Reader(db_path) as reader:
        assert len(reader.symbols()) == 1


def test_seed_falls_back_to_cache_when_network_unavailable(tmp_path, monkeypatch):
    """무인 운영: nasdaqtrader.com 이 죽어도 캐시가 있으면 유니버스 빌드가 멈추면 안 된다."""
    cache_dir = tmp_path / "nasdaq-trader"
    cache_dir.mkdir()
    (cache_dir / "nasdaqlisted.txt").write_text(
        "Symbol|Security Name|Market Category|Test Issue|Financial Status|"
        "Round Lot Size|ETF|NextShares\n"
        "GOOD|Good Inc|Q|N|N|100|N|N\n",
        encoding="utf-8",
    )
    (cache_dir / "otherlisted.txt").write_text(
        "ACT Symbol|Security Name|Exchange|CQS Symbol|ETF|Round Lot Size|"
        "Test Issue|NASDAQ Symbol\n"
        "OTHR|Other Inc|N|OTHR|N|100|N|OTHR\n",
        encoding="utf-8",
    )

    def _boom(*_args, **_kwargs):
        raise OSError("network unavailable")

    monkeypatch.setattr(seed_module, "urlopen", _boom)
    assert fetch_symbol_directory(cache_dir) == ["GOOD", "OTHR"]


def test_seed_raises_when_network_and_cache_both_unavailable(tmp_path, monkeypatch):
    cache_dir = tmp_path / "nasdaq-trader"

    def _boom(*_args, **_kwargs):
        raise OSError("network unavailable")

    monkeypatch.setattr(seed_module, "urlopen", _boom)
    with pytest.raises(RuntimeError, match="unavailable"):
        fetch_symbol_directory(cache_dir)


def _assert_no_leak_in_chain(exc: BaseException, sentinel: str) -> None:
    """감사 U-4 통과 기준: __context__/__cause__ 둘 다 None이고, str/traceback/
    doc·msg·args 어디에도 원문이 없어야 한다 (체인 전체 재귀 확인)."""
    import traceback

    assert sentinel not in str(exc)
    assert sentinel not in traceback.format_exc()
    for attr in ("doc", "msg", "args"):
        assert sentinel not in str(getattr(exc, attr, "")), f"leaked via .{attr}"
    assert any(
        sentinel.encode() in a for a in exc.args if isinstance(a, bytes)
    ) is False, "leaked via raw bytes in .args"
    assert exc.__context__ is None, "__context__ still holds the original exception"
    assert exc.__cause__ is None, "__cause__ still holds the original exception"


def test_parse_directory_file_bad_encoding_never_leaks_content(tmp_path):
    """감사 U-4: `UnicodeDecodeError.args`(`object` 필드)는 읽던 바이트 원문 전체를
    그대로 들고 다닌다 — str(exc)만 보고 안전하다 판단하면 놓친다(실측 확인)."""
    sentinel = "SENTINEL_VALUE_LEAK_CHECK"
    directory = tmp_path / "nasdaqlisted.txt"
    payload = (
        f"Symbol|Name\n{sentinel}|x\n".encode("utf-8")
        + b"\xff\xfe"
        + sentinel.encode()
    )
    directory.write_bytes(payload)

    with pytest.raises(ValueError, match="not readable"):
        parse_directory_file(directory)

    # Re-raise to capture the exception object + a real traceback for the checklist.
    try:
        parse_directory_file(directory)
    except ValueError as exc:
        _assert_no_leak_in_chain(exc, sentinel)
    else:
        pytest.fail("expected ValueError")


def test_fetch_symbol_directory_cache_fallback_never_leaks_content(tmp_path, monkeypatch):
    """네트워크가 죽고 캐시도 깨졌을 때(라이브에서 있을 법한 조합) 최종 예외 체인에
    원문이 남지 않는지 — parse_directory_file 자체의 방어와, fetch_symbol_directory 가
    폴백 파싱을 except 블록 밖에서 하는 구조 둘 다를 함께 검증한다."""
    sentinel = "SENTINEL_VALUE_LEAK_CHECK"
    cache_dir = tmp_path / "nasdaq-trader"
    cache_dir.mkdir()
    payload = (
        f"Symbol|Name\n{sentinel}|x\n".encode("utf-8")
        + b"\xff\xfe"
        + sentinel.encode()
    )
    (cache_dir / "nasdaqlisted.txt").write_bytes(payload)
    (cache_dir / "otherlisted.txt").write_text(
        "ACT Symbol|Name\nGOOD|x\n", encoding="utf-8"
    )

    def _boom(*_args, **_kwargs):
        raise OSError("network unavailable")

    monkeypatch.setattr(seed_module, "urlopen", _boom)
    try:
        fetch_symbol_directory(cache_dir)
    except Exception as exc:
        _assert_no_leak_in_chain(exc, sentinel)
    else:
        pytest.fail("expected an exception (bad cache, no network)")


def _yaml_path(path: Path) -> str:
    return Path(path).as_posix()


def test_main_entrypoint_smoke_against_mock_server(monkeypatch, tmp_path, mock_server):
    """`python -m tossmon.universe` 진입점 전체 배선을 mock 서버로 스모크 테스트한다.
    TOSS_LIVE=0 등가(live: false) — 라이브 호출 없음, 토큰은 고정 mock 토큰."""
    symbols = ["S000", "S001", "S002"]
    monkeypatch.setattr(build_module, "fetch_symbol_directory", lambda _path: symbols)

    db_path = tmp_path / "monitor.db"
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        f"""
api:
  base_url: '{mock_server.url}'
  live: false
  keys_path: '{_yaml_path(tmp_path / "api_keys")}'
  token_state_path: '{_yaml_path(tmp_path / "token_state.json")}'
  timeout_s: 5.0
  usage_ratio: 0.7

store:
  db_path: '{_yaml_path(db_path)}'
  archive_dir: '{_yaml_path(tmp_path / "archive")}'

universe:
  price_min_usd: "0.01"
  price_max_usd: "10000"
  mcap_min_usd: "1"
  mcap_max_usd: "2000000000000"
  tier1_max: 10
  tier2_max: 5
  tier3_max: 2
""",
        encoding="utf-8",
    )

    exit_code = universe_main.main(["--config", str(config_path)])
    assert exit_code == 0

    # Every mock-synthesized symbol is a common ACTIVE stock (tools/mock_server.py
    # synth_stock) and the config bounds above are wide enough to admit all of
    # them regardless of the per-symbol synthetic price/mcap, so this is a
    # deterministic wiring check, not a filter-boundary test (those live in
    # test_tier0_filter_boundaries).
    with Reader(db_path) as reader:
        frame = reader.symbols()
        assert set(frame["symbol"]) == set(symbols)
        assert (frame["tier"] == 1).all()

    log_path = db_path.parent / "universe_build.log"
    assert log_path.exists()
    assert "universe build done" in log_path.read_text(encoding="utf-8")
