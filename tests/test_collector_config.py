"""config 단일 출처 (계약 C-9) — 키 1:1, 단위 변환, env 오버라이드, 결손 시 실패."""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from tests.test_collector_helpers import config_dict, make_config
from tossmon.config import DEFAULT_CONFIG_PATH, load_config, parse_config

REPO_ROOT = Path(__file__).resolve().parent.parent
EXAMPLE = REPO_ROOT / "config" / "config.example.yaml"


def test_example_yaml_parses_and_keys_match_dataclass():
    """계약: `config.example.yaml` 의 키와 Config 는 1:1 이어야 한다."""
    raw = yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))
    cfg = parse_config(raw, env={})

    assert set(raw) == {"api", "limits", "store", "universe", "detector", "polling"}
    assert set(raw["polling"]) == {"tier1_sweep_s", "tier2_candle_s", "tier3_trades_s",
                                   "tier3_orderbook_s", "ranking_snap_s"}
    assert set(raw["api"]) == {"base_url", "live", "keys_path", "token_state_path",
                               "timeout_s", "usage_ratio"}
    assert set(raw["universe"]) == {"price_min_usd", "price_max_usd", "mcap_min_usd",
                                    "mcap_max_usd", "tier1_max", "tier2_max", "tier3_max"}
    assert cfg.api.live is False                      # 예시는 mock 고정
    # 2026-08-04 사용자 결정: 폭(20->10)을 줄여 호가 밀도(16s->4s)를 샀다. 체결은 그대로 4s.
    # 2026-08-28 D-35 (나) 관측 모드: 둘 다 10s. 매매 논제가 닫혔다 (docs/80).
    assert cfg.polling.tier3_trades_s == 10 and cfg.polling.tier3_orderbook_s == 10
    assert cfg.universe.tier3_max == 10                # 예산 역산값 (2.18 <= 5.95)


def test_usd_strings_become_micro_dollars():
    cfg = make_config()
    assert cfg.universe.price_min_u == 100_000          # $0.10
    assert cfg.universe.price_max_u == 20_000_000       # $20.00
    assert cfg.universe.mcap_min_u == 10_000_000_000_000


def test_float_price_is_rejected():
    """계약 C-2: 가격은 문자열 decimal 이어야 한다 (float 는 정밀도 손실)."""
    with pytest.raises(ValueError, match="decimal"):
        parse_config(config_dict(universe={"price_min_usd": 0.1}), env={})


@pytest.mark.parametrize("section,key", [
    ("api", "base_url"), ("api", "usage_ratio"), ("polling", "ranking_snap_s"),
    ("universe", "tier3_max"), ("detector", "event_rvol_min"), ("store", "db_path"),
])
def test_missing_key_raises(section, key):
    data = config_dict()
    data[section].pop(key)
    with pytest.raises(ValueError, match=f"{section}.{key}"):
        parse_config(data, env={})


def test_retired_tier3_micro_s_is_rejected_with_pointer():
    data = config_dict(polling={"tier3_micro_s": 8})
    with pytest.raises(ValueError, match="tier3_micro_s"):
        parse_config(data, env={})


def test_tier_sizes_must_form_a_funnel():
    with pytest.raises(ValueError, match="tier1_max >= tier2_max"):
        parse_config(config_dict(universe={"tier2_max": 20, "tier3_max": 300}), env={})


def test_env_overrides_base_url_and_live():
    data = config_dict(api={"live": True})
    cfg = parse_config(data, env={"TOSS_BASE_URL": "http://127.0.0.1:9999/",
                                  "TOSS_LIVE": "0"})
    assert cfg.api.base_url == "http://127.0.0.1:9999"   # 후행 슬래시 제거
    # 계약 C-9: "1" 이 아니면 거짓 — 리스 없는 워커가 live:true yaml 을 물어도 막힌다.
    assert cfg.api.live is False
    assert parse_config(data, env={"TOSS_LIVE": "1"}).api.live is True


def test_env_db_path_override(tmp_path):
    cfg = parse_config(config_dict(), env={"TOSS_DB_PATH": str(tmp_path / "x.db")})
    assert cfg.store.db_path == tmp_path / "x.db"


def test_missing_section_is_none_but_require_raises():
    cfg = parse_config(config_dict(polling=None, store=None), env={})
    assert cfg.polling is None
    with pytest.raises(ValueError, match="section 'polling'"):
        cfg.require_polling()


def test_load_config_falls_back_to_example(tmp_path, caplog):
    missing = tmp_path / "config.yaml"
    (tmp_path / "config.example.yaml").write_text(
        yaml.safe_dump(config_dict()), encoding="utf-8")
    cfg = load_config(missing)
    assert cfg.api.live is False
    with pytest.raises(FileNotFoundError):
        load_config(tmp_path / "nope" / "config.yaml")


def test_default_path_is_repo_relative():
    assert DEFAULT_CONFIG_PATH == Path("config/config.yaml")
