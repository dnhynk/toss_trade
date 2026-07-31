"""유니버스 빌드 실행 진입점 — `python -m tossmon.universe`.

    python -m tossmon.universe --config config/config.yaml

일 1회 실행 전제다 (docs/03 §1 Tier 0). collector(`python -m tossmon.collector` 와
대칭)와 같은 설정 파일을 읽고, 결과(tier0/tier1/former_runners 수)를 표준출력과
로그 양쪽에 남긴다. collector 가 이 결과를 워치리스트 시드로 읽는 계약은
`store/reader.py::Reader.symbols()` docstring 을 참조 (docs/10_audit.md F-2).

라이브 호출은 collector 와 동일하게 `api.live: true` + `TOSS_LIVE=1` 이면서 토큰
리스를 잡을 수 있을 때만 일어난다 — mock 모드(`TOSS_LIVE=0`)는 고정 토큰을 쓰고
`api_keys` 를 건드리지 않는다 (계약 C-9, `tossmon/api/tokens.py`).
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

from ..api.client import TossClient
from ..api.limiter import GroupRateLimiter
from ..api.tokens import TokenManager
from ..config import load_config
from ..store.writer import Store
from .build import build_universe

log = logging.getLogger("tossmon.universe")


def _configure_logging(log_path: Path) -> None:
    if log.handlers:
        return
    log.setLevel(logging.INFO)
    log.propagate = False
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(message)s")
    stream = logging.StreamHandler(sys.stdout)
    stream.setFormatter(fmt)
    log.addHandler(stream)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    file_handler = logging.FileHandler(log_path, encoding="utf-8")
    file_handler.setFormatter(fmt)
    log.addHandler(file_handler)


async def main_async(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    store_cfg = cfg.require_store()
    universe_cfg = cfg.require_universe()

    _configure_logging(Path(store_cfg.db_path).parent / "universe_build.log")

    tokens = TokenManager(cfg.api.keys_path, cfg.api.token_state_path, cfg.api.live)
    limiter = GroupRateLimiter(dict(cfg.limits), cfg.api.usage_ratio)
    client = TossClient(cfg.api.base_url, tokens, limiter, timeout_s=cfg.api.timeout_s)
    store = Store(Path(store_cfg.db_path))

    log.info("universe build start base_url=%s live=%s db=%s",
              cfg.api.base_url, cfg.api.live, store_cfg.db_path)
    try:
        summary = await build_universe(client, store, universe_cfg)
    except Exception:
        # Whatever ran before the failure has already been committed (each
        # store write is its own transaction) — a rerun picks up from there
        # via upsert, it does not need to undo anything here.
        log.exception("universe build failed")
        raise
    finally:
        await client.aclose()
        store.close()
        tokens.release()

    log.info("universe build done tier0=%d tier1=%d former_runners=%d",
              summary["tier0"], summary["tier1"], summary["former_runners"])
    print(f"tier0={summary['tier0']} tier1={summary['tier1']} "
          f"former_runners={summary['former_runners']}")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="tossmon universe builder — Tier 0/1 seed (docs/03 §1, no trading code)"
    )
    ap.add_argument("--config", default="config/config.yaml")
    args = ap.parse_args(argv)
    try:
        return asyncio.run(main_async(args))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
