"""`SchemaMismatch` 를 두 부류로 가른다 — 소유: W4.

`client._classify` 는 재시도해도 결과가 같은 4xx 를 **전부** `SchemaMismatch` 로 올린다
(계약 C-5 표에 4xx 칸이 없어 "재시도 금지 + caller 가 로그·스킵" 정책이 같은 예외에 매핑됐다).
그래서 성격이 완전히 다른 두 가지가 카운터 하나에 섞였다:

- **응답 모양이 바뀐 것** — 진짜 사고다. 재시작으로 안 고쳐지고, 그날 데이터를 의심해야 한다.
- **`http-404 code=stock-not-found`** — 랭킹에 뜬 심볼을 상세 조회했더니 없더라는 것.
  상장폐지·거래정지·심볼 변경이면 **정상적으로 일어나는 일**이고 그 종목만 건너뛰면 된다.

실측(collector.log 2026-07-31~08-04): `schema_mismatch` 로 집계된 5건이 **전부** 후자였다
(`tier2:AVAT`, `BLRK`, `CABR`, `MACI`, `JSM` — 모두 `http-404 code=stock-not-found`).
진짜 계약 변경은 한 번도 없었는데 경보 문구는 "그날 데이터를 믿지 마라"였다.
`schema_mismatch` 는 바로 그 판단의 근거이므로 **묽어지면 안 된다** — 여기서 갈라낸다.

가르는 조건을 문자열 부분일치로 두면 안 된다. 중첩 필드 오류는
`f"{where}.{key}: {exc.detail}"` 로 감싸이고 응답 본문 값이 그대로 detail 에 실릴 수 있어,
`"stock-not-found" in detail` 같은 검사는 **진짜 모양 변경을 404 로 오분류**한다.
그래서 detail 이 `http-<status> ` 로 **시작**할 때만 상태코드/에러코드를 읽는다.
"""
from __future__ import annotations

import re

#: 이 조합만 "없는 종목"으로 본다. 상태코드와 에러코드 **둘 다** 맞아야 한다.
SYMBOL_NOT_FOUND_STATUS = 404
SYMBOL_NOT_FOUND_CODE = "stock-not-found"

#: 진짜 응답 모양 변경 카운터 (묽어지면 안 되는 쪽).
COUNTER_SCHEMA_MISMATCH = "schema_mismatch"
#: 없는/상장폐지/거래정지 종목 카운터 (정상적으로 일어나는 일).
COUNTER_SYMBOL_NOT_FOUND = "symbol_not_found"

#: `client._classify` 가 만드는 형태: `f"http-{status} {_err_code(resp)}"`.
#: 앵커(`^`)가 핵심이다 — 감싸인 detail(`candles.price: ...`)은 여기서 걸러진다.
_HTTP_DETAIL = re.compile(r"^http-(\d{3})(?=$|\s)(.*)$", re.DOTALL)
#: `_err_code` 의 dict 분기 출력: `code=<slug> message=<...>`. 공백 구분 토큰만 읽는다.
_CODE_TOKEN = re.compile(r"(?:^|\s)code=(\S*)")

#: 루프 이름의 마지막 조각이 심볼인지. 토스 API 심볼 문자집합은 `[A-Za-z0-9.-]` 이고
#: 이 코드베이스는 심볼을 대문자로 정규화하므로, 소문자 조각(`trades`/`book`)은 걸러진다.
_SYMBOL = re.compile(r"^[A-Z0-9][A-Z0-9.\-]*$")


def parse_http_detail(detail: str) -> tuple[int | None, str | None]:
    """`SchemaMismatch.detail` → (상태코드, 에러코드). HTTP 유래가 아니면 (None, None).

    상태코드는 있고 에러코드만 없는 경우가 있다 — `_err_code` 가 `error=<str>`,
    `<no error field>`, `<unparseable>` 를 돌려주는 분기다. 그때 코드는 None 이고,
    **본문을 읽을 수 없는 404 는 "없는 종목"으로 넘기지 않는다** (판단 근거가 없으므로).
    """
    m = _HTTP_DETAIL.match(detail or "")
    if m is None:
        return None, None
    status = int(m.group(1))
    code_match = _CODE_TOKEN.search(m.group(2))
    return status, (code_match.group(1) if code_match else None)


def is_symbol_not_found(detail: str) -> bool:
    """"랭킹에 있던 심볼이 상세 조회에는 없다" 인가 (상장폐지·거래정지·심볼 변경)."""
    status, code = parse_http_detail(detail)
    return status == SYMBOL_NOT_FOUND_STATUS and code == SYMBOL_NOT_FOUND_CODE


def counter_for(detail: str) -> str:
    """이 detail 을 올려야 할 카운터 이름."""
    return (COUNTER_SYMBOL_NOT_FOUND if is_symbol_not_found(detail)
            else COUNTER_SCHEMA_MISMATCH)


def symbol_from_loop_name(name: str) -> str | None:
    """`_guarded` 의 루프 이름에서 심볼을 뽑는다 (`tier3:trades:RGNT` → `RGNT`).

    404 를 심볼별로 세려면 어느 심볼이었는지 알아야 하는데, `SchemaMismatch` 는 그 정보를
    들고 오지 않는다 (detail 은 서버 문구뿐이다). 호출자가 이미 이름에 심볼을 넣어 두므로
    거기서 읽는다 — 심볼 없는 루프(`tier1`, `rankings`, `session`)는 None 이다.
    """
    parts = [p for p in (name or "").split(":") if p]
    if len(parts) < 2:
        return None                          # 심볼 없는 루프 이름
    last = parts[-1]
    return last if _SYMBOL.match(last) else None


class NotFoundTally:
    """심볼별 `stock-not-found` 횟수.

    왜 세는가: 한두 번은 상장폐지·거래정지라 정상이지만, **같은 심볼이 계속 404** 면
    워치리스트에서 빼는 것이 맞을 수 있다. 그 판단·구현은 이 태스크 범위 밖이라
    (코디네이터 지시) 여기서는 **판단 재료만** 만든다.

    무인 장시간 실행이므로 상한이 필요하다. 상한에 닿으면 새 심볼은 개별 추적을 포기하고
    `overflow` 로 뭉친다 — 이미 추적 중인 심볼(= 반복 404, 우리가 찾는 것)은 계속 센다.
    상한에 닿는 것 자체가 "전 종목이 404" 라는 신호이므로 `overflow` 를 숨기지 않는다.
    """

    __slots__ = ("max_symbols", "counts", "overflow")

    def __init__(self, max_symbols: int = 512) -> None:
        self.max_symbols = int(max_symbols)
        self.counts: dict[str, int] = {}
        self.overflow = 0

    def add(self, symbol: str) -> int:
        """1건 기록. 그 심볼의 누적 횟수를 돌려준다 (추적 못 하면 0)."""
        sym = (symbol or "").upper()
        if not sym:
            return 0
        if sym not in self.counts and len(self.counts) >= self.max_symbols:
            self.overflow += 1
            return 0
        self.counts[sym] = self.counts.get(sym, 0) + 1
        return self.counts[sym]

    def total(self) -> int:
        return sum(self.counts.values()) + self.overflow

    def repeat_symbols(self, min_count: int = 2) -> list[tuple[str, int]]:
        """`min_count` 이상 404 인 심볼 — 워치리스트 정리 후보 (횟수 내림차순)."""
        return sorted(((s, n) for s, n in self.counts.items() if n >= min_count),
                      key=lambda kv: (-kv[1], kv[0]))

    def top(self, n: int = 5) -> list[tuple[str, int]]:
        return sorted(self.counts.items(), key=lambda kv: (-kv[1], kv[0]))[:max(0, n)]

    def describe(self, n: int = 5) -> str:
        """ASCII 한 줄 요약 (콘솔 안전). 빈 상태면 빈 문자열."""
        if not self.counts and not self.overflow:
            return ""
        parts = [f"{sym}={cnt}" for sym, cnt in self.top(n)]
        if len(self.counts) > n:
            parts.append(f"+{len(self.counts) - n} more")
        if self.overflow:
            parts.append(f"untracked={self.overflow}")
        return " ".join(parts)


__all__ = ["COUNTER_SCHEMA_MISMATCH", "COUNTER_SYMBOL_NOT_FOUND", "NotFoundTally",
           "SYMBOL_NOT_FOUND_CODE", "SYMBOL_NOT_FOUND_STATUS", "counter_for",
           "is_symbol_not_found", "parse_http_detail", "symbol_from_loop_name"]
