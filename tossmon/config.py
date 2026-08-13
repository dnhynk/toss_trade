"""설정 단일 출처 — 계약 C-9. config/config.yaml + env 오버라이드 → Config dataclass.

env 오버라이드: TOSS_BASE_URL, TOSS_LIVE ("1"만 참), TOSS_DB_PATH.
워커는 키를 임의 추가하지 말 것 (ask 필요). 소유: W4.

키 규약
------
`config/config.example.yaml` 의 키와 **1:1**이다. YAML 은 사람이 읽는 단위(USD 문자열)로,
`Config` 는 계약 C-2 의 저장 단위(마이크로달러 int)로 들고 있다 — 변환은 여기 한 곳에서만
일어난다 (`universe.price_min_usd` → `UniverseConfig.price_min_u`).

결손 처리
--------
- 섹션이 통째로 없으면 그 필드는 `None` (dataclass 기본값). 컬렉터가 필요로 하는 섹션은
  `require_*()` 가 기동 시점에 ValueError 로 잡는다.
- 섹션이 있는데 키가 빠졌으면 **ValueError** — 기본값 자동 보정은 하지 않는다 (계약 C-9).
  조용한 기본값은 "예산 초과는 버그가 아니라 사고" 원칙과 충돌한다.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Mapping

import yaml

from .api.models import dec_to_u

log = logging.getLogger(__name__)

DEFAULT_CONFIG_PATH = Path("config/config.yaml")

#: env 오버라이드 이름 (계약 C-9). 이 3개 외에는 추가하지 않는다.
ENV_BASE_URL = "TOSS_BASE_URL"
ENV_LIVE = "TOSS_LIVE"
ENV_DB_PATH = "TOSS_DB_PATH"


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
    """폴링 주기(초).

    `tier3_trades_s` / `tier3_orderbook_s` 는 구 `tier3_micro_s` 를 대체한다 (main e33d7a8).
    A2 §1: 호가는 1레벨뿐이라 성기게, 테이프는 조밀하게 본다. 두 주기가 분리돼 있어야
    BudgetGuard 가 서로 독립적으로 축소할 수 있다.
    """
    tier1_sweep_s: int
    tier2_candle_s: int
    tier3_trades_s: int
    tier3_orderbook_s: int
    ranking_snap_s: int
    #: tier2 호가 라운드로빈 주기(초, 심볼당 1회). **0 또는 미설정이면 비활성** —
    #: 되돌리기 쉬우라고 선택 키다 (W5 에스컬레이션 2026-08-03: 호가가 tier3 승격
    #: 이후에만 수집돼 승격 전후 스프레드 궤적을 원리상 측정할 수 없었다).
    tier2_orderbook_s: int = 0


@dataclass(frozen=True)
class RankingPromotionConfig:
    """랭킹 진입을 **tier3 승격 사유**로 쓰는 차선 (D-21). **기본값은 전부 꺼짐.**

    왜 tier3 인가: `trades_snap` 을 쓰는 것은 tier3 뿐이다 (`loops._poll_trades` 는
    `run_tier3_micro` 에서만 불린다. 실측으로도 tier3 를 거친 종목 집합과 체결 행이 있는
    종목 집합이 **정확히 같다** — docs/61 §1). 랭킹 진입을 tier2 로 올리는 경로는 이미
    있는데(`loops._ranking_triggers`), tier2 는 1 분봉만 받으므로 **테이프 커버리지를
    한 건도 늘리지 못한다.** docs/59 가 센 "랭킹 상위의 71~99% 가 테이프에 없다" 는
    그 사실의 결과다.

    다섯 값이 **전부** 유효해야 켜진다(`enabled`). 어느 하나가 기본값이면 꺼진 것이고,
    꺼진 상태의 동작은 이 절이 없던 때와 **완전히 같다** — `config_sig` 도 안 바뀐다
    (`tests/test_ranking_promotion.py` 가 그것을 잰다).

    * `types` — 승격 사유로 볼 랭킹 타입. 빈 목록이면 꺼짐
    * `top_n` — 그 목록의 몇 위까지를 진입으로 볼 것인가
    * `tier3_slots` — 차선이 차지할 수 있는 tier3 좌석 수. **정원을 늘리지 않는다** —
      `tier3_max` 를 같이 올리면 순증, 안 올리면 기존 좌석의 재배분이다 (docs/61 §2)
    * `hold_s` — 좌석 유지 시간
    * `policy` — `rotate`(`hold_s` 뒤 무조건 놓아 준다, 폭 우선) 또는
      `sticky`(상위 N 에 남아 있는 동안 계속 쥔다, 깊이 우선). 둘의 커버리지 차이는
      실측 4~10 배다 (docs/61 §3)
    * `cooldown_s` — 좌석을 놓은 종목이 다시 앉기까지의 대기. 진동 방지
    """
    types: tuple[str, ...] = ()
    top_n: int = 0
    tier3_slots: int = 0
    hold_s: int = 0
    policy: str = "rotate"
    cooldown_s: int = 600

    POLICIES = ("rotate", "sticky")

    @property
    def enabled(self) -> bool:
        return bool(self.types) and self.top_n > 0 and self.tier3_slots > 0 \
            and self.hold_s > 0

    def signature(self) -> str:
        """`config_sig` 꼬리. **꺼져 있으면 빈 문자열** — 지문을 흔들지 않는다.

        꺼진 상태에서 지문이 바뀌면 데이터에 **없는 경계**가 생긴다. 반대로 켜는 순간
        지문이 바뀌어야 한다 — 그 경계가 이 변경의 앞뒤를 가르는 유일한 표식이고,
        `usage_ratio` 가 지문에 없어서 D-8 변경이 데이터에 안 남았던 전례가 있다.
        """
        if not self.enabled:
            return ""
        kinds = "+".join(sorted(
            t.replace("TOSS_SECURITIES_TRADING_", "T").replace("MARKET_TRADING_", "M")
            .replace("TOP_GAINERS", "GAIN").replace("TOP_LOSERS", "LOSE")
            for t in self.types))
        return (f",rkp{kinds}@{self.top_n}/k{self.tier3_slots}"
                f"/h{self.hold_s}s/{self.policy}/cd{self.cooldown_s}s")


@dataclass(frozen=True)
class Config:
    api: ApiConfig
    limits: dict[str, float] = field(default_factory=dict)
    store: StoreConfig | None = None
    universe: UniverseConfig | None = None
    detector: DetectorConfig | None = None
    polling: PollingConfig | None = None
    #: 절이 없으면 **전부 꺼짐** — 지금 동작 그대로다 (D-21, docs/61).
    ranking_promotion: RankingPromotionConfig = field(
        default_factory=RankingPromotionConfig)

    # ---- 컬렉터가 필요로 하는 섹션 강제 (기동 시점에 실패시키기 위한 것) ----

    def require_store(self) -> StoreConfig:
        return _require_section(self.store, "store")

    def require_universe(self) -> UniverseConfig:
        return _require_section(self.universe, "universe")

    def require_detector(self) -> DetectorConfig:
        return _require_section(self.detector, "detector")

    def require_polling(self) -> PollingConfig:
        return _require_section(self.polling, "polling")


def _require_section(value, name: str):
    if value is None:
        raise ValueError(f"config: section {name!r} is required by the collector")
    return value


# --------------------------------------------------------------------------- #
# 파싱 헬퍼 — 결손/타입 오류는 전부 ValueError (조용한 보정 금지)
# --------------------------------------------------------------------------- #
def _section(data: Mapping[str, Any], name: str) -> Mapping[str, Any] | None:
    node = data.get(name)
    if node is None:
        return None
    if not isinstance(node, Mapping):
        raise ValueError(f"config: section {name!r} must be a mapping, got {type(node).__name__}")
    return node


def _req(node: Mapping[str, Any], key: str, section: str) -> Any:
    if key not in node or node[key] is None:
        raise ValueError(f"config: missing key {section}.{key}")
    return node[key]


def _as_int(node: Mapping[str, Any], key: str, section: str) -> int:
    raw = _req(node, key, section)
    if isinstance(raw, bool) or not isinstance(raw, int):
        raise ValueError(f"config: {section}.{key} must be an int, got {raw!r}")
    return raw


def _as_float(node: Mapping[str, Any], key: str, section: str) -> float:
    raw = _req(node, key, section)
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        raise ValueError(f"config: {section}.{key} must be a number, got {raw!r}")
    return float(raw)


def _as_bool(node: Mapping[str, Any], key: str, section: str) -> bool:
    raw = _req(node, key, section)
    if not isinstance(raw, bool):
        raise ValueError(f"config: {section}.{key} must be a bool, got {raw!r}")
    return raw


def _as_path(node: Mapping[str, Any], key: str, section: str) -> Path:
    raw = _req(node, key, section)
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError(f"config: {section}.{key} must be a non-empty path string")
    return Path(raw)


def _as_str(node: Mapping[str, Any], key: str, section: str) -> str:
    raw = _req(node, key, section)
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError(f"config: {section}.{key} must be a non-empty string")
    return raw.strip()


def _as_u(node: Mapping[str, Any], key: str, section: str) -> int:
    """USD 표기(문자열/정수) → 마이크로달러 int (계약 C-2). float 표기는 거부한다."""
    raw = _req(node, key, section)
    if isinstance(raw, float):
        raise ValueError(f"config: {section}.{key} must be a decimal *string* "
                         f"(float loses precision — 계약 C-2), got {raw!r}")
    try:
        return dec_to_u(raw)
    except Exception as exc:                      # SchemaMismatch 포함
        raise ValueError(f"config: {section}.{key} is not a decimal ({raw!r}): {exc}") from exc


def _positive(value: int, section: str, key: str) -> int:
    if value <= 0:
        raise ValueError(f"config: {section}.{key} must be > 0, got {value}")
    return value


# --------------------------------------------------------------------------- #
# 섹션별 파서
# --------------------------------------------------------------------------- #
def _parse_api(data: Mapping[str, Any]) -> ApiConfig:
    node = _section(data, "api")
    if node is None:
        raise ValueError("config: section 'api' is required")
    usage_ratio = _as_float(node, "usage_ratio", "api")
    if not 0.0 < usage_ratio <= 1.0:
        raise ValueError(f"config: api.usage_ratio must be in (0, 1], got {usage_ratio}")
    timeout_s = _as_float(node, "timeout_s", "api")
    if timeout_s <= 0:
        raise ValueError(f"config: api.timeout_s must be > 0, got {timeout_s}")
    return ApiConfig(
        base_url=_as_str(node, "base_url", "api").rstrip("/"),
        live=_as_bool(node, "live", "api"),
        keys_path=_as_path(node, "keys_path", "api"),
        token_state_path=_as_path(node, "token_state_path", "api"),
        timeout_s=timeout_s,
        usage_ratio=usage_ratio,
    )


def _parse_limits(data: Mapping[str, Any]) -> dict[str, float]:
    node = _section(data, "limits")
    if node is None:
        return {}
    out: dict[str, float] = {}
    for group, raw in node.items():
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            raise ValueError(f"config: limits.{group} must be a number, got {raw!r}")
        if float(raw) <= 0:
            raise ValueError(f"config: limits.{group} must be > 0, got {raw!r}")
        out[str(group)] = float(raw)
    return out


def _parse_store(data: Mapping[str, Any]) -> StoreConfig | None:
    node = _section(data, "store")
    if node is None:
        return None
    return StoreConfig(db_path=_as_path(node, "db_path", "store"),
                       archive_dir=_as_path(node, "archive_dir", "store"))


def _parse_universe(data: Mapping[str, Any]) -> UniverseConfig | None:
    node = _section(data, "universe")
    if node is None:
        return None
    cfg = UniverseConfig(
        price_min_u=_as_u(node, "price_min_usd", "universe"),
        price_max_u=_as_u(node, "price_max_usd", "universe"),
        mcap_min_u=_as_u(node, "mcap_min_usd", "universe"),
        mcap_max_u=_as_u(node, "mcap_max_usd", "universe"),
        tier1_max=_positive(_as_int(node, "tier1_max", "universe"), "universe", "tier1_max"),
        tier2_max=_positive(_as_int(node, "tier2_max", "universe"), "universe", "tier2_max"),
        tier3_max=_positive(_as_int(node, "tier3_max", "universe"), "universe", "tier3_max"),
    )
    if cfg.price_min_u >= cfg.price_max_u:
        raise ValueError("config: universe.price_min_usd must be < price_max_usd")
    if cfg.mcap_min_u >= cfg.mcap_max_u:
        raise ValueError("config: universe.mcap_min_usd must be < mcap_max_usd")
    if not cfg.tier1_max >= cfg.tier2_max >= cfg.tier3_max:
        raise ValueError("config: universe tier sizes must satisfy "
                         "tier1_max >= tier2_max >= tier3_max (깔때기 구조 — docs/03 §1)")
    return cfg


def _parse_detector(data: Mapping[str, Any]) -> DetectorConfig | None:
    node = _section(data, "detector")
    if node is None:
        return None
    return DetectorConfig(
        event_window_min=_positive(_as_int(node, "event_window_min", "detector"),
                                   "detector", "event_window_min"),
        event_ret_min=_as_float(node, "event_ret_min", "detector"),
        event_day_ret_min=_as_float(node, "event_day_ret_min", "detector"),
        event_rvol_min=_as_float(node, "event_rvol_min", "detector"),
        promote_hysteresis_s=_positive(_as_int(node, "promote_hysteresis_s", "detector"),
                                       "detector", "promote_hysteresis_s"),
    )


def _parse_polling(data: Mapping[str, Any]) -> PollingConfig | None:
    node = _section(data, "polling")
    if node is None:
        return None
    if "tier3_micro_s" in node:
        raise ValueError(
            "config: polling.tier3_micro_s is retired (main e33d7a8) — use "
            "polling.tier3_trades_s and polling.tier3_orderbook_s (계약 A2 §1)")
    keys = ("tier1_sweep_s", "tier2_candle_s", "tier3_trades_s", "tier3_orderbook_s",
            "ranking_snap_s")
    values: dict[str, int] = {k: _positive(_as_int(node, k, "polling"), "polling", k)
                              for k in keys}
    # 선택 키 — 미설정이면 0(비활성). 0 을 허용해야 설정 한 줄로 되돌릴 수 있다.
    raw = node.get("tier2_orderbook_s")
    if raw is None:
        values["tier2_orderbook_s"] = 0
    else:
        if isinstance(raw, bool) or not isinstance(raw, int):
            raise ValueError("config: polling.tier2_orderbook_s must be an int "
                             f"(0 disables), got {raw!r}")
        if raw < 0:
            raise ValueError("config: polling.tier2_orderbook_s must be >= 0 "
                             f"(0 disables), got {raw}")
        values["tier2_orderbook_s"] = raw
    return PollingConfig(**values)


def _parse_ranking_promotion(data: Mapping[str, Any]) -> RankingPromotionConfig:
    """절이 없으면 **기본 인스턴스 = 전부 꺼짐**. 값이 있으면 전부 검증한다.

    되돌릴 수 없는 수집 변경이라(안 받은 체결은 나중에 못 만든다) 오타 하나가 조용히
    켜지거나 조용히 꺼지면 안 된다. 그래서 없는 키는 기본값으로 두되, **있는 키는
    타입·범위를 전부 막는다.**
    """
    node = _section(data, "ranking_promotion")
    if node is None:
        return RankingPromotionConfig()
    unknown = set(node) - {"types", "top_n", "tier3_slots", "hold_s", "policy",
                           "cooldown_s"}
    if unknown:
        raise ValueError("config: ranking_promotion has unknown keys "
                         f"{sorted(unknown)} — 임의 키 추가 금지")
    raw_types = node.get("types") or ()
    if isinstance(raw_types, str) or not isinstance(raw_types, (list, tuple)):
        raise ValueError("config: ranking_promotion.types must be a list of ranking "
                         f"type names, got {raw_types!r}")
    types = tuple(str(t) for t in raw_types)
    ints: dict[str, int] = {}
    for key, default in (("top_n", 0), ("tier3_slots", 0), ("hold_s", 0),
                         ("cooldown_s", 600)):
        raw = node.get(key)
        if raw is None:
            ints[key] = default
            continue
        if isinstance(raw, bool) or not isinstance(raw, int):
            raise ValueError(f"config: ranking_promotion.{key} must be an int "
                             f"(0 disables), got {raw!r}")
        if raw < 0:
            raise ValueError(f"config: ranking_promotion.{key} must be >= 0, got {raw}")
        ints[key] = raw
    policy = str(node.get("policy") or "rotate")
    if policy not in RankingPromotionConfig.POLICIES:
        raise ValueError("config: ranking_promotion.policy must be one of "
                         f"{list(RankingPromotionConfig.POLICIES)}, got {policy!r}")
    cfg = RankingPromotionConfig(types=types, policy=policy, **ints)
    # 반쯤 켜진 설정은 **조용히 꺼진 것처럼** 보인다 — 그게 제일 나쁜 상태다.
    given = [k for k in ("types", "top_n", "tier3_slots", "hold_s") if node.get(k)]
    if given and not cfg.enabled:
        raise ValueError(
            "config: ranking_promotion is half-configured — "
            f"{given} 만 설정됐다. types/top_n/tier3_slots/hold_s 는 **넷 다** 있어야 "
            "켜지고, 하나라도 비면 전체가 꺼진다 (docs/61 §4)")
    return cfg


# --------------------------------------------------------------------------- #
# 공개 API
# --------------------------------------------------------------------------- #
def parse_config(data: Mapping[str, Any], *, env: Mapping[str, str] | None = None) -> Config:
    """매핑 → Config. env 오버라이드는 파싱 **전에** 적용되어 같은 검증을 받는다."""
    if not isinstance(data, Mapping):
        raise ValueError(f"config: root must be a mapping, got {type(data).__name__}")
    merged = _apply_env(data, os.environ if env is None else env)
    return Config(
        api=_parse_api(merged),
        limits=_parse_limits(merged),
        store=_parse_store(merged),
        universe=_parse_universe(merged),
        detector=_parse_detector(merged),
        polling=_parse_polling(merged),
        ranking_promotion=_parse_ranking_promotion(merged),
    )


def _apply_env(data: Mapping[str, Any], env: Mapping[str, str]) -> dict[str, Any]:
    out = {k: (dict(v) if isinstance(v, Mapping) else v) for k, v in data.items()}

    api = out.get("api")
    if isinstance(api, dict):
        base_url = env.get(ENV_BASE_URL)
        if base_url:
            api["base_url"] = base_url
        # 계약 C-9: "1"만 참. env 가 있으면 양방향으로 덮어쓴다 — 리스 없는 워커가 실수로
        # live:true 인 yaml 을 물고 기동하는 것을 TOSS_LIVE=0 으로 막을 수 있어야 한다.
        live = env.get(ENV_LIVE)
        if live is not None:
            api["live"] = live.strip() == "1"

    store = out.get("store")
    db_path = env.get(ENV_DB_PATH)
    if isinstance(store, dict) and db_path:
        store["db_path"] = db_path
    return out


def load_config(path: Path | str = DEFAULT_CONFIG_PATH) -> Config:
    """YAML 로드 + env 오버라이드. 필수 키 결손 시 ValueError (기본값 자동 보정 금지).

    `config/config.yaml` 이 없으면 같은 이름의 `.example.yaml` 로 폴백한다(경고 로그).
    예시 파일은 mock 기본값(`live: false`, mock base_url)이라 폴백이 라이브를 켤 일은 없다.
    """
    p = Path(path)
    if not p.exists():
        example = p.with_name(f"{p.stem}.example{p.suffix}")
        if not example.exists():
            raise FileNotFoundError(f"config file not found: {p} (and no {example.name})")
        log.warning("config: %s not found — falling back to %s", p, example)
        p = example
    try:
        raw = yaml.safe_load(p.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ValueError(f"config: {p} is not valid YAML: {exc}") from exc
    if raw is None:
        raw = {}
    if not isinstance(raw, Mapping):
        raise ValueError(f"config: {p} must contain a mapping at the top level")
    return parse_config(raw)


def with_overrides(cfg: Config, **sections: Any) -> Config:
    """테스트·운영 도구용 얕은 치환 (`dataclasses.replace` 래퍼)."""
    return replace(cfg, **sections)


__all__ = [
    "ApiConfig", "Config", "DetectorConfig", "PollingConfig", "StoreConfig", "UniverseConfig",
    "DEFAULT_CONFIG_PATH", "load_config", "parse_config", "with_overrides",
]
