"""외부 심볼 디렉토리 수집 — 소유: W2.

NASDAQ Trader symbol directory 등 공개 소스에서 US 상장 심볼 전체를 수집·파싱한다.
(토스 API에는 전 종목 리스트 엔드포인트가 없음 — docs/01 §4-4)
"""
from __future__ import annotations

import csv
import re
import tempfile
from pathlib import Path
from urllib.request import Request, urlopen

DIRECTORY_URLS = {
    "nasdaqlisted.txt": "https://www.nasdaqtrader.com/dynamic/SymDir/nasdaqlisted.txt",
    "otherlisted.txt": "https://www.nasdaqtrader.com/dynamic/SymDir/otherlisted.txt",
}
_SYMBOL_RE = re.compile(r"^[A-Z0-9][A-Z0-9.$-]*$")


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
            if not _SYMBOL_RE.fullmatch(symbol):
                continue
            symbols.add(symbol)
    return sorted(symbols)
