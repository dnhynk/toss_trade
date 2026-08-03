"""알림 — 소유: W4. Phase 1 은 콘솔+파일 로그. 텔레그램은 인터페이스만.

무인 실행 전제이므로 (a) 콘솔은 사람이 붙어 있을 때, (b) 파일은 사후 추적용이다.
시크릿(토큰·api_keys)은 어떤 경로로도 들어오지 않는다 — 넘기지 않는 것이 호출측 규약이고
`Notifier` 는 받은 문자열을 그대로 쓴다 (계약 C-11).
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Protocol

LOGGER_NAME = "tossmon.collector"

#: 파일 로그 회전 상한. 장시간 무인 실행에서 디스크를 먹지 않게 한다.
MAX_BYTES = 32 * 1024 * 1024
BACKUP_COUNT = 3


class Sink(Protocol):
    """추가 알림 채널 인터페이스. Phase 1 에 실구현체는 없다."""

    def send(self, level: str, msg: str) -> None: ...


class TelegramSink:
    """텔레그램 채널 — **인터페이스만**. 호출되면 NotImplementedError.

    실제 전송은 Phase 2 에서 붙인다. 지금 있는 이유는 `Notifier` 의 확장점을 고정해
    채널이 늘어도 컬렉터 코드가 바뀌지 않게 하려는 것뿐이다.
    """

    def __init__(self, token: str | None = None, chat_id: str | None = None) -> None:
        self.configured = bool(token and chat_id)

    def send(self, level: str, msg: str) -> None:
        raise NotImplementedError("TelegramSink is an interface stub (Phase 1)")


class Notifier:
    """콘솔 + 파일 로그. `alert` 는 사람이 반드시 봐야 하는 사건에만 쓴다."""

    def __init__(self, log_path: Path | str | None = None, *,
                 console: bool = True, level: int = logging.INFO,
                 sinks: list[Sink] | None = None, name: str = LOGGER_NAME) -> None:
        self.sinks = list(sinks or [])
        self.counters: dict[str, int] = {"info": 0, "warn": 0, "alert": 0}
        self.log = logging.getLogger(name)
        self.log.setLevel(level)
        self.log.propagate = False
        fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(message)s")

        if console and not any(getattr(h, "_tossmon_console", False) for h in self.log.handlers):
            stream = logging.StreamHandler()
            stream.setFormatter(fmt)
            stream._tossmon_console = True                       # type: ignore[attr-defined]
            self.log.addHandler(stream)

        self.log_path = Path(log_path) if log_path else None
        if self.log_path is not None:
            target = str(self.log_path.resolve())
            if target not in {getattr(h, "baseFilename", None) for h in self.log.handlers}:
                from logging.handlers import RotatingFileHandler

                self.log_path.parent.mkdir(parents=True, exist_ok=True)
                fh = RotatingFileHandler(self.log_path, maxBytes=MAX_BYTES,
                                         backupCount=BACKUP_COUNT, encoding="utf-8")
                fh.setFormatter(fmt)
                self.log.addHandler(fh)

    # ---- 계약 표면 ------------------------------------------------------

    def info(self, msg: str) -> None:
        self.counters["info"] += 1
        self.log.info(msg)

    def alert(self, msg: str) -> None:
        """치명 상황 (Forbidden, 예산 초과, 수집 정지)."""
        self.counters["alert"] += 1
        self.log.error(msg)
        self._fanout("alert", msg)

    # ---- 부가 채널 ------------------------------------------------------

    def warn(self, msg: str) -> None:
        self.counters["warn"] += 1
        self.log.warning(msg)

    def debug(self, msg: str) -> None:
        self.log.debug(msg)

    def promotion(self, symbol: str, from_tier: int, to_tier: int, reason: str,
                  score: float) -> None:
        """티어 전이 1건 — **DEBUG** 다.

        개별 줄이 초당 수십 개면 로그가 아니라 소음이다 (2026-08-03 실측: 최근 1,000줄
        중 978줄이 티어 줄이라 워치독의 텔레메트리 탐지가 무력화됐다). 사람이 읽는 채널은
        주기 텔레메트리의 요약(`promotions_delta`/`demotions_delta`)이고, 개별 전이의
        영구 기록은 `promotions` 테이블이다 — 여기서 INFO 로 흘릴 이유가 없다.
        """
        arrow = "↑" if to_tier > from_tier else "↓"
        self.debug(f"tier {arrow} {symbol}: {from_tier}→{to_tier} "
                   f"reason={reason} score={score:.3f}")

    def event(self, symbol: str, t0_ms: int, kind: str, extra: str = "") -> None:
        self.alert(f"EVENT {symbol} kind={kind} t0_ms={t0_ms} {extra}".rstrip())

    def close(self) -> None:
        for handler in list(self.log.handlers):
            self.log.removeHandler(handler)
            try:
                handler.close()
            except (OSError, ValueError):
                pass

    def _fanout(self, level: str, msg: str) -> None:
        for sink in self.sinks:
            try:
                sink.send(level, msg)
            except NotImplementedError:
                pass                        # Phase 1 스텁 — 조용히 넘어간다
            except Exception as exc:        # 알림 실패가 수집을 죽이면 안 된다
                self.log.warning("notifier sink failed: %s: %s", type(exc).__name__, exc)


__all__ = ["Notifier", "Sink", "TelegramSink"]
