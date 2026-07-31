"""외부 심볼 디렉토리 수집 — 소유: W2.

NASDAQ Trader symbol directory 등 공개 소스에서 US 상장 심볼 전체를 수집·파싱한다.
(토스 API에는 전 종목 리스트 엔드포인트가 없음 — docs/01 §4-4)
"""
from __future__ import annotations

import csv
import logging
import re
import tempfile
from pathlib import Path
from urllib.request import Request, urlopen

log = logging.getLogger(__name__)

DIRECTORY_URLS = {
    "nasdaqlisted.txt": "https://www.nasdaqtrader.com/dynamic/SymDir/nasdaqlisted.txt",
    "otherlisted.txt": "https://www.nasdaqtrader.com/dynamic/SymDir/otherlisted.txt",
}
#: 토스 API 가 실제로 받는 심볼 문자셋 — 영문 대/소문자, 숫자, `.`, `-` 만 (docs/01_api_analysis.md
#: §3.1). NASDAQ 디렉토리는 우선주를 `ABR$D` 형식(`$` 포함)으로 표기하는데, `$` 는 이 문자셋
#: 밖이라 배치 콜에 하나만 섞여도 그 배치 전체가 400 으로 죽는다(라이브에서 실제로 발생).
#: 우선주는 어차피 `is_common=True` 필터로 걸러질 대상이므로 여기서 배제해도 손실이 아니다.
#: `build.py` 가 배치 전송 직전 방어적 재검증에도 그대로 재사용한다(다른 심볼 소스가 붙어도
#: 배치 하나가 통째로 죽는 사고를 반복하지 않기 위함).
TOSS_SYMBOL_RE = re.compile(r"^[A-Z0-9][A-Z0-9.-]*$")


def fetch_symbol_directory(cache_dir: Path) -> list[str]:
    """심볼 리스트 반환. 네트워크 실패 시 캐시 폴백."""
    cache_dir = Path(cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)
    symbols: set[str] = set()
    failures: list[str] = []
    for filename, url in DIRECTORY_URLS.items():
        cache_path = cache_dir / filename
        try:
            request = Request(url, headers={"User-Agent": "tossmon/0.1 symbol-directory"})
            with urlopen(request, timeout=15.0) as response:
                payload = response.read()
            # Validate before replacing a known-good cache.
            with tempfile.NamedTemporaryFile(
                dir=cache_dir, prefix=f".{filename}.", suffix=".tmp", delete=False
            ) as temp:
                temp.write(payload)
                temp_path = Path(temp.name)
            parsed = parse_directory_file(temp_path)
            if not parsed:
                raise ValueError("directory contained no eligible symbols")
            temp_path.replace(cache_path)
        except Exception as exc:
            if "temp_path" in locals() and temp_path.exists():
                temp_path.unlink()
            if not cache_path.exists():
                failures.append(f"{filename}: {exc}")
                continue
            parsed = parse_directory_file(cache_path)
        symbols.update(parsed)
    if failures:
        raise RuntimeError("symbol directory unavailable: " + "; ".join(failures))
    return sorted(symbols)


def parse_directory_file(path: Path) -> list[str]:
    """파일 포맷 파싱 (테스트 심볼·ETF 플래그 등 이상 케이스 처리)."""
    with Path(path).open("r", encoding="utf-8-sig", newline="") as handle:
        rows = csv.DictReader(handle, delimiter="|")
        if rows.fieldnames is None:
            raise ValueError("symbol directory has no header")
        fields = {field.strip() for field in rows.fieldnames if field}
        symbol_field = next(
            (name for name in ("Symbol", "ACT Symbol", "NASDAQ Symbol") if name in fields),
            None,
        )
        if symbol_field is None:
            raise ValueError("symbol directory has no recognized symbol column")

        symbols: set[str] = set()
        rejected_charset = 0
        for raw in rows:
            row = {
                (key.strip() if key else ""): (value.strip() if value else "")
                for key, value in raw.items()
            }
            symbol = row.get(symbol_field, "").upper()
            if not symbol or symbol.startswith("FILE CREATION TIME"):
                continue
            if row.get("Test Issue", "").upper() == "Y":
                continue
            if row.get("ETF", "").upper() == "Y":
                continue
            if not TOSS_SYMBOL_RE.fullmatch(symbol):
                rejected_charset += 1
                continue
            symbols.add(symbol)
    if rejected_charset:
        log.info(
            "%s: %d symbol(s) rejected — outside Toss API charset [A-Za-z0-9.-] "
            "(e.g. NASDAQ preferred-share '$' suffixes)",
            path, rejected_charset,
        )
    return sorted(symbols)
