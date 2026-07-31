"""백필 러너 E2E — 실제 HTTP(tools/mock_server.py) + TossClient + limiter + SQLite.

스크린 -> 후보 -> [D-25, D+2] 백필 -> 매니페스트 전 과정을 mock 서버로 돌린다.
mock 픽스처는 심볼·페이지를 구분하지 않으므로, 통합 테스트 관례대로
실행 중인 서버의 FixtureStore 에 SPKY 전용 픽스처(일봉 스파이크 + 1분봉 + 달력 체인)를
주입한다 — tools/ 파일 자체는 건드리지 않는다.

전선(wire) 검증이 핵심이다: 1분봉 요청의 쿼리스트링이 실제로 adjusted=false 인지
(§7-e 출처 증명), --estimate 가 정말 한 호출도 보내지 않는지.
"""
from __future__ import annotations

import importlib.util
import json
import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path

from tests.test_collector_helpers import mock_server

REPO_ROOT = Path(__file__).resolve().parent.parent


def _load_backfill():
    if "tossmon_backfill" in sys.modules:
        return sys.modules["tossmon_backfill"]
    spec = importlib.util.spec_from_file_location(
        "tossmon_backfill", REPO_ROOT / "tools" / "backfill.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules["tossmon_backfill"] = module
    spec.loader.exec_module(module)
    return module


BF = _load_backfill()

SPIKE_D = "2026-07-30"                                 # 홀드아웃(~07-29) 밖 — 근거 공개 가능


def _weekdays(start: str, end: str) -> list[str]:
    cur = datetime.strptime(start, "%Y-%m-%d")
    stop = datetime.strptime(end, "%Y-%m-%d")
    out = []
    while cur <= stop:
        if cur.weekday() < 5:
            out.append(cur.strftime("%Y-%m-%d"))
        cur += timedelta(days=1)
    return out


def _next_day(date: str) -> str:
    return (datetime.strptime(date, "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d")


def _cal_day(date: str) -> dict:
    """실서버 모양의 세션 블록 (KST, 정규장·애프터는 다음 달력일로 넘어간다)."""
    nxt = _next_day(date)
    return {
        "date": date,
        "dayMarket": {"startTime": f"{date}T09:00:00+09:00",
                      "endTime": f"{date}T16:50:00+09:00"},
        "preMarket": {"startTime": f"{date}T17:00:00+09:00",
                      "endTime": f"{date}T22:30:00+09:00"},
        "regularMarket": {"startTime": f"{date}T22:30:00+09:00",
                          "endTime": f"{nxt}T05:00:00+09:00"},
        "afterMarket": {"startTime": f"{nxt}T05:00:00+09:00",
                        "endTime": f"{nxt}T08:50:00+09:00"},
    }


def _daily_item(date: str, close: float, *, high: float | None = None,
                low: float | None = None, vol: int = 5_000_000) -> dict:
    return {"timestamp": f"{date}T13:00:00+09:00",
            "openPrice": f"{close:.4f}", "highPrice": f"{(high or close * 1.001):.4f}",
            "lowPrice": f"{(low or close * 0.999):.4f}", "closePrice": f"{close:.4f}",
            "volume": str(vol)}


def _minute_item(date: str, hh: int, mm: int, price: float = 1.0) -> dict:
    return {"timestamp": f"{date}T{hh:02d}:{mm:02d}:00+09:00",
            "openPrice": f"{price:.4f}", "highPrice": f"{price * 1.001:.4f}",
            "lowPrice": f"{price * 0.999:.4f}", "closePrice": f"{price:.4f}",
            "volume": "1000"}


def _inject_fixtures(httpd) -> int:
    """달력 체인 + SPKY 일봉(스파이크)·1분봉 픽스처를 FixtureStore 에 주입."""
    store = httpd.RequestHandlerClass.store
    chain = _weekdays("2026-06-08", "2026-08-10")
    cal_route = store.by_route.setdefault(("GET", "/api/v1/market-calendar/US"), [])
    for i, date in enumerate(chain):
        prev_d = chain[max(0, i - 1)]
        next_d = chain[min(len(chain) - 1, i + 1)]
        cal_route.append({
            "case": f"bf-cal-{date}", "match": {"date": date}, "status": 200,
            "body": {"result": {"today": _cal_day(date),
                                "previousBusinessDay": _cal_day(prev_d),
                                "nextBusinessDay": _cal_day(next_d)}}})

    days = _weekdays("2026-06-15", "2026-07-31")
    dailies = []
    for d in days:
        if d == SPIKE_D:
            dailies.append(_daily_item(d, 1.30, high=1.31, low=1.29))   # +30%
        else:
            dailies.append(_daily_item(d, 1.00))
    minutes = ([_minute_item("2026-07-29", 23, i) for i in range(30)]
               + [_minute_item("2026-07-30", 22, 30 + i) for i in range(30)]
               + [_minute_item("2026-07-30", 23, i) for i in range(60)])
    candle_route = store.by_route.setdefault(("GET", "/api/v1/candles"), [])
    candle_route.append({
        "case": "bf-spky-1d", "match": {"symbol": "SPKY", "interval": "1d"},
        "status": 200, "body": {"result": {"candles": dailies, "nextBefore": None}}})
    candle_route.append({
        "case": "bf-spky-1m", "match": {"symbol": "SPKY", "interval": "1m"},
        "status": 200, "body": {"result": {"candles": minutes, "nextBefore": None}}})
    return len(minutes)


def _spy_queries(httpd) -> list[tuple[str, dict]]:
    """mock 서버가 실제로 받은 (path, query) 기록 (tools/ 는 건드리지 않는다)."""
    seen: list[tuple[str, dict]] = []
    handler = httpd.RequestHandlerClass
    original = handler._resolve

    def spy(self, path, query):
        seen.append((path, {k: v[0] for k, v in query.items()}))
        return original(self, path, query)

    handler._resolve = spy
    return seen


def test_backfill_e2e_over_http_screen_to_manifest(tmp_path, monkeypatch):
    with mock_server() as (base_url, httpd):
        n_minutes = _inject_fixtures(httpd)
        seen = _spy_queries(httpd)
        monkeypatch.setenv("TOSS_BASE_URL", base_url)
        monkeypatch.setenv("TOSS_LIVE", "0")            # 라이브 이중 잠금 (계약 C-9)
        db = tmp_path / "bf.db"
        out = tmp_path / "out"

        rc = BF.main(["--config", "config/config.yaml", "--db", str(db),
                      "--out", str(out), "--symbols", "SPKY"])
        assert rc == 0

        manifest = json.loads((out / "backfill_manifest.json").read_text("utf-8"))
        # 스크린: 스파이크 하루만, 근거는 ret (홀드아웃 밖이라 공개된다)
        assert manifest["candidates_total"] == 1
        cand = manifest["symbols"]["SPKY"]["candidate_days"][0]
        assert cand["date"] == SPIKE_D and cand["reasons"] == ["ret"]
        # §7-e 출처 증명 — 매니페스트의 호출 파라미터 증거
        assert manifest["call_params"]["candles_1m"]["adjusted"] == "false"
        win = manifest["symbols"]["SPKY"]["windows"][0]
        assert win["adjusted"] == "false"
        # 픽스처는 단일 페이지라 창 시작(D-25)에 못 닿는다 — 프로브·partial 로 정직하게 기록
        assert win["status"] == "partial"
        assert win["skip_reason"] == "retention_exhausted"
        assert manifest["symbols"]["SPKY"]["probed_oldest_1m_utc"] is not None
        assert win["bars_in_window"] == n_minutes

        # DB: 1분봉이 실제로 저장됐다 (무체결 분은 없는 그대로)
        conn = sqlite3.connect(db)
        try:
            m = conn.execute("SELECT COUNT(*) FROM candles_1m WHERE symbol='SPKY'"
                             ).fetchone()[0]
            d = conn.execute("SELECT COUNT(*) FROM candles_1d WHERE symbol='SPKY'"
                             ).fetchone()[0]
        finally:
            conn.close()
        assert m == n_minutes
        assert d == len(_weekdays("2026-06-15", "2026-07-31"))

        # 전선 검증: 1분봉 요청은 전부 adjusted=false, 일봉은 전부 true (§7-e)
        candle_queries = [q for path, q in seen if path == "/api/v1/candles"]
        assert candle_queries, "candle requests did not reach the wire"
        assert {q["adjusted"] for q in candle_queries if q["interval"] == "1m"} == {"false"}
        assert {q["adjusted"] for q in candle_queries if q["interval"] == "1d"} == {"true"}
        # 달력 걷기가 실제로 있었다 (매매일 산정은 /market-calendar/US — §2.5)
        assert any(path.endswith("/market-calendar/US") for path, _q in seen)

        # --estimate 는 단 한 호출도 보내지 않는다 (내일 아침 판단 근거 모드)
        wire_before = len(seen)
        rc2 = BF.main(["--config", "config/config.yaml", "--db", str(db),
                       "--out", str(out), "--symbols", "SPKY", "--estimate"])
        assert rc2 == 0
        assert len(seen) == wire_before                  # HTTP 트래픽 0
        est = json.loads((out / "backfill_estimate.json").read_text("utf-8"))
        assert est["screen_done_symbols"] == 1
        assert "checkpoint windows=1" in est["basis"]
        assert est["scenarios"]
