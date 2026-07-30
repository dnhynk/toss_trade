"""컬렉터 실행 진입점 — `python -m tossmon.collector`.

    python -m tossmon.collector --config config/config.yaml --symbols SNTI,BTAI,CRKN

무인 실행 전제다 (docs/03 §4): 세션은 `/market-calendar/US` 로 스스로 켜고 끈다.
심볼을 주지 않으면 워치리스트는 **랭킹 출현 종목이 누적**되며 스스로 채워진다.
라이브 호출은 `api.live: true` + `TOSS_LIVE=1` 이면서 토큰 리스를 잡을 수 있을 때만 일어난다.
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import signal
import sys
from pathlib import Path

from ..api.client import TossClient
from ..api.limiter import GroupRateLimiter
from ..api.tokens import TokenManager
from ..config import load_config
from ..store.writer import Store
from .loops import CollectorContext, run_all
from .notifier import Notifier


async def main_async(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    store_cfg = cfg.require_store()

    notifier = Notifier(Path(store_cfg.db_path).parent / "collector.log")
    tokens = TokenManager(cfg.api.keys_path, cfg.api.token_state_path, cfg.api.live)
    limiter = GroupRateLimiter(dict(cfg.limits), cfg.api.usage_ratio)
    client = TossClient(cfg.api.base_url, tokens, limiter, timeout_s=cfg.api.timeout_s)
    store = Store(Path(store_cfg.db_path))

    symbols = [s.strip().upper() for s in (args.symbols or "").split(",") if s.strip()]
    ctx = CollectorContext.create(client, store, cfg, notifier=notifier, symbols=symbols,
                                  resume=not args.fresh)
    notifier.info(f"collector start base_url={cfg.api.base_url} live={cfg.api.live} "
                  f"db={store_cfg.db_path} watch={len(ctx.watchlist)}")

    loop = asyncio.get_running_loop()
    for signame in ("SIGINT", "SIGTERM"):
        sig = getattr(signal, signame, None)
        if sig is None:
            continue
        with contextlib.suppress(NotImplementedError, ValueError):
            loop.add_signal_handler(sig, lambda: ctx.shutdown("signal"))

    try:
        await run_all(ctx, cycles=args.cycles)
    except KeyboardInterrupt:
        ctx.shutdown("keyboard interrupt")
    finally:
        ctx.save_state(force=True)
        notifier.info(f"collector stopped: {ctx.counters}")
        await client.aclose()
        store.close()
        tokens.release()
        notifier.close()
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="tossmon collector (Phase 1, no trading code)")
    ap.add_argument("--config", default="config/config.yaml")
    ap.add_argument("--symbols", default="", help="쉼표 구분 초기 워치리스트")
    ap.add_argument("--cycles", type=int, default=None,
                    help="루프별 최대 반복 수 (연기·디버그용). 기본은 무한")
    ap.add_argument("--fresh", action="store_true", help="상태파일을 무시하고 새로 시작")
    args = ap.parse_args(argv)
    try:
        return asyncio.run(main_async(args))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
