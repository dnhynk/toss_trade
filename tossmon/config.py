"""설정 단일 출처 — 계약 C-9. config/config.yaml + env 오버라이드 → Config dataclass.

env 오버라이드: TOSS_BASE_URL, TOSS_LIVE ("1"만 참), TOSS_DB_PATH.
워커는 키를 임의 추가하지 말 것 (ask 필요). 소유: W4.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class ApiConfig:
    base_url: str
    live: bool
    keys_path: Path
    token_state_path: Path
    timeout_s: float
    usage_ratio: float


@dataclass(frozen=True)
class StoreConfig:
    db_path: Path
    archive_dir: Path


@dataclass(frozen=True)
class UniverseConfig:
    price_min_u: int
    price_max_u: int
    mcap_min_u: int
    mcap_max_u: int
    tier1_max: int
    tier2_max: int
    tier3_max: int


@dataclass(frozen=True)
class DetectorConfig:
    event_window_min: int
    event_ret_min: float
    event_day_ret_min: float
    event_rvol_min: float
    promote_hysteresis_s: int


@dataclass(frozen=True)
class PollingConfig:
    tier1_sweep_s: int
    tier2_candle_s: int
    tier3_micro_s: int
    ranking_snap_s: int


@dataclass(frozen=True)
class Config:
    api: ApiConfig
    limits: dict[str, float] = field(default_factory=dict)
    store: StoreConfig | None = None
    universe: UniverseConfig | None = None
    detector: DetectorConfig | None = None
    polling: PollingConfig | None = None


def load_config(path: Path | str = "config/config.yaml") -> Config:
    """YAML 로드 + env 오버라이드. 필수 키 결손 시 ValueError (기본값 자동 보정 금지)."""
    raise NotImplementedError
