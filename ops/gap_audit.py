"""결손 감사 — 우리 테이프가 초 단위 사건을 담고 있는가. 소유: W5.

`coordination/DATA-QUALITY-PROGRAM.md` 축 1 의 첫 항목. 배경은 `docs/31_gap_audit.md`.

## 왜 이 도구가 필요한가

`tape_gaps` 는 **누적 카운터만** 있다(07-31 부터, 재시작해도 안 리셋). 683 이라는 숫자는
있는데 **어디가 비었는지 모른다** — 시각도 종목도 세션도 모른다. 그리고 측정된 전제가
**틱 기준 슈팅 지속시간 중앙값 6.0초**(`docs/29`)이므로 "1분에 몇 건 빠졌나"는 의미가 없다.
6초 현상에 대해 30초 구멍은 전부다. 그래서 이 도구는 **초 단위로, 위치와 함께** 낸다.

## 설계 원칙 (이 프로젝트가 데인 지점들)

1. **구멍의 정의에 임계값을 박지 않는다.** "N초 이상이면 구멍"으로 시작하지 않는다.
   먼저 **간격 분포 자체**(p50/p90/p99/max)를 낸다. `min_rise` 임계가 답을 만들어냈던
   사고가 이미 있다(`docs/25`). 임계를 쓴 곳은 딱 한 군데이고 사유를 달았다
   (`RANKING_COVERAGE_MIN`).
2. **"원래 없는 것"과 "우리가 놓친 것"을 반드시 가른다.** 못 가르면 뭉치지 않고
   **"구분 불가"로 따로 출력한다.** 무체결 분에는 캔들이 원래 없다(`docs/06`).
3. **분류는 기계적 사실로만 한다.** 티어 소속은 `promotions` 전이 이력에서 정확히
   복원되고, 수집기 재기동은 로그의 `collector start` 줄이고, 수집기 생존은 랭킹 폴
   밀도다. 어느 것도 "느낌"이 아니다.
4. **판정하지 않는다.** 산출은 품질 지표이지 "수집이 충분하다/불충분하다"가 아니다.

## 무엇을 어디서 읽는가

| 계열 | 소스 | 무엇을 재는가 |
|---|---|---|
| 체결 | `collector.log` 의 `tape gap` 줄 | **위치가 있는 유일한 결손 기록**. 카운터가 아니라 (종목, prev_max, this_min, n) |
| 랭킹 | `rankings_snap.snap_ms` | 폴 간격 분포 (타입별) |
| 호가 | `orderbook_snap.snap_ms` | 스냅 간격 분포 (**티어별로 분리** — tier3 4초와 tier2 600초는 다른 계열이다) |
| 분봉 | `candles_1m` × `trades_snap` | 봉 없는 분 중 **체결이 있었던 분**(모순) vs **관측 불가**(구분 불가) |

라이브 API 호출 없음. DB 는 read-only URI + `PRAGMA query_only` (계약 C-6).
"""
from __future__ import annotations

import argparse
import datetime as dt
import gzip
import re
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path

from .healthcheck import _ro_connect
from .opsconfig import load_ops_config

# --------------------------------------------------------------------------- #
# 세션 경계 (KST). 08:50~09:00 은 **어떤 세션도 배정되지 않은 구간**이다 —
# 랭킹·체결이 멈추는 것이 설계대로이고 경보 대상이 아니다(docs/11 §20).
# --------------------------------------------------------------------------- #
SESSIONS: tuple[tuple[str, int, int], ...] = (
    ("day", 9 * 60, 17 * 60),
    ("pre", 17 * 60, 22 * 60 + 30),
    ("regular", 22 * 60 + 30, 24 * 60 + 5 * 60),   # 다음날 05:00 까지
    ("after", 5 * 60, 8 * 60 + 50),
)
NO_SESSION = "none"

#: `tape gap` 한 건이 "수집기가 살아서 폴링 중일 때 생긴 것"인지 판정할 때 쓰는 **유일한
#: 임계값**. 구간 안의 실측 랭킹 폴 수 / 기대 폴 수(구간 길이 ÷ 그 창의 랭킹 간격 중앙값).
#:
#: **왜 임계가 필요한가**: 재기동 줄이 없는 정지(행)를 재기동 로그만으로는 못 잡는다.
#: 실측으로 A 버킷 꼬리에 100분짜리 사건이 남았고, 그 구간에는 랭킹 폴이 아예 없었다.
#: **왜 0.5 인가**: 폴 주기가 2배까지 늘어지는 것은 정상 부하 변동(큐잉)으로 보고,
#: 그보다 성기면 수집기가 그 구간에 사실상 없었다고 본다. 절반이라는 값 자체에 근거가
#: 있는 것은 아니므로 **리포트에 이 값과 그 효과(재분류된 건수)를 항상 같이 찍는다.**
RANKING_COVERAGE_MIN = 0.5

#: `_poll_trades` 가 쓰는 `/trades` 응답 상한. n 이 이 값이면 응답이 **잘린 것**이고,
#: 그때만 "구간에 체결이 분명히 있었는데 못 받았다"가 성립한다. n 이 이보다 작으면
#: API 가 가진 것을 **전부** 준 것이라 결손의 증거가 아니다. (tossmon/collector/loops.py)
TRADES_COUNT_CAP = 50

_GAP_RX = re.compile(
    r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),\d+\s+WARNING\s+tape gap (\S+): "
    r"prev_max=(\d+) < this_min=(\d+) \(n=(\d+)\)")
_START_RX = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),\d+\s+INFO\s+collector start")
_SIG_RX = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),\d+\s+INFO\s+"
                     r"COLLECTION-CONFIG \S+ sig=(\S+)")


@dataclass(frozen=True)
class GapEvent:
    """`collector.log` 한 줄에서 복원한 체결 결손 1건. **위치가 있다.**"""
    wall_ms: int      # 폴이 일어난 벽시계 시각
    symbol: str
    prev_max_ms: int  # 직전에 본 마지막 체결 시각
    this_min_ms: int  # 이번 응답의 가장 오래된 체결 시각
    n: int            # 이번 응답의 체결 건수 (50 이면 응답이 잘린 것)

    @property
    def span_s(self) -> float:
        """결손 구간 길이(초). **잃은 테이프의 상한**이지 확정치가 아니다."""
        return (self.this_min_ms - self.prev_max_ms) / 1000.0


def kst_session(ms: int) -> str:
    """KST 기준 세션 이름. 08:50~09:00 은 `none`(세션 없음 — 정상 구간)."""
    t = dt.datetime.fromtimestamp(ms / 1000.0)
    mins = t.hour * 60 + t.minute
    for name, a, b in SESSIONS:
        lo, hi = a, b
        if hi > 24 * 60:                       # 자정을 넘는 세션(regular)
            if mins >= lo or mins < hi - 24 * 60:
                return name
            continue
        if lo <= mins < hi:
            return name
    return NO_SESSION


def percentiles(values) -> dict:
    """분포 요약. **임계 판정 전에 분포부터 낸다**는 원칙의 구현체."""
    vals = sorted(float(v) for v in values)
    if not vals:
        return {"n": 0, "p50": None, "p90": None, "p99": None, "max": None, "min": None}

    def at(p: float) -> float:
        return vals[min(int(p / 100.0 * len(vals)), len(vals) - 1)]

    return {"n": len(vals), "p50": round(at(50), 3), "p90": round(at(90), 3),
            "p99": round(at(99), 3), "max": round(vals[-1], 3), "min": round(vals[0], 3)}


def read_log_lines(log_dir: Path):
    """`collector.log` + 회전된 `.gz` 를 시간순으로 흘려준다.

    회전(`ops/rotate_logs.py`)이 지나가면 결손 이력이 잘리므로 회전본도 읽는다.
    **이 도구의 조회 가능 범위는 로그 보존기간이 정한다** — 리포트에 그렇게 적는다.
    """
    paths = sorted(log_dir.glob("collector.log.*.gz")) + [log_dir / "collector.log"]
    for p in paths:
        if not p.exists():
            continue
        try:
            opener = gzip.open if p.suffix == ".gz" else open
            with opener(p, "rt", encoding="utf-8", errors="replace") as fh:  # type: ignore[operator]
                yield from fh
        except OSError:
            continue


def parse_log(log_dir: Path) -> tuple[list[GapEvent], list[int], list[tuple[int, str]]]:
    """(결손 사건들, 재기동 시각들, (시각, config_sig) 들)."""
    gaps: list[GapEvent] = []
    restarts: list[int] = []
    sigs: list[tuple[int, str]] = []
    for line in read_log_lines(log_dir):
        m = _GAP_RX.match(line)
        if m:
            wall = int(dt.datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S").timestamp() * 1000)
            gaps.append(GapEvent(wall, m.group(2), int(m.group(3)), int(m.group(4)),
                                 int(m.group(5))))
            continue
        m = _START_RX.match(line)
        if m:
            restarts.append(int(dt.datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S")
                                .timestamp() * 1000))
            continue
        m = _SIG_RX.match(line)
        if m:
            sigs.append((int(dt.datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S")
                             .timestamp() * 1000), m.group(2)))
    return gaps, restarts, sigs


def tier_timeline(conn) -> dict[str, list[tuple[int, int, int]]]:
    """종목별 (시각, from_tier, to_tier) 전이 이력.

    `promotions` 는 전이를 **양쪽 티어와 함께** 남기므로 어느 시점의 티어든 정확히
    복원된다 — 시작 상태를 가정할 필요가 없다. 추정이 아니라 기록이다.
    """
    out: dict[str, list[tuple[int, int, int]]] = defaultdict(list)
    for sym, ts, ft, tt in conn.execute(
            "SELECT symbol, ts_ms, from_tier, to_tier FROM promotions ORDER BY ts_ms"):
        out[sym].append((int(ts), int(ft), int(tt)))
    return dict(out)


def tier_at(timeline: dict, symbol: str, ms: int) -> int | None:
    """그 시각의 티어. 전이 이력이 없으면 `None`(모름 — 0 으로 뭉치지 않는다)."""
    ev = timeline.get(symbol)
    if not ev:
        return None
    if ms < ev[0][0]:
        return ev[0][1]
    for ts, _ft, tt in reversed(ev):
        if ms >= ts:
            return tt
    return None


def stayed_in_tier(timeline: dict, symbol: str, tier: int, a_ms: int, b_ms: int) -> bool:
    """[a,b] **내내** 그 티어였는가. 중간에 한 번이라도 벗어나면 False."""
    if tier_at(timeline, symbol, a_ms) != tier:
        return False
    return not any(a_ms < ts <= b_ms and tt != tier
                   for ts, _ft, tt in timeline.get(symbol, ()))


def tier_population(timeline: dict, tier: int, start_ms: int, end_ms: int) -> dict:
    """창 동안의 **시간가중 평균 소속 종목 수**와 최소/최대.

    이것 없이는 "결손 없음"이 아무 말도 하지 않는다. tier3 가 1종목으로 쪼그라든 창에서
    결손이 0인 것은 수집이 좋아서가 아니라 **볼 것이 없어서**다. 그리고 결손은 호가
    주기가 아니라 tier3 종목 수를 따라간다는 관측이 이미 있다(`docs/30` §4). 그래서
    이 값은 결론에 반드시 따라붙는 측정 조건이다(태스크 지정 형식).

    시간가중이라 "그 순간 몇 종목"이 아니라 "창 전체에 걸쳐 평균 몇 종목"이다.

    구현은 **한 번의 스윕**이다. 구간마다 전 종목을 다시 세는 방식으로 짰더니 하루치
    창에서 133초가 걸렸고(전체 감사 148초의 90%), 승격 이벤트가 하루 ~6,800건씩 쌓이므로
    갈수록 나빠진다. 아침 08:52 자동 실행이 그만큼 늦어질 이유가 없다. 스윕은 같은 값을
    낸다 — 시작 시점 소속을 한 번 구하고, 전이마다 카운트를 ±1 한다.
    """
    span = max(end_ms - start_ms, 1)
    cur = {sym: tier_at(timeline, sym, start_ms) for sym in timeline}
    n = sum(1 for v in cur.values() if v == tier)
    # 정렬 키에 **원래 순서(idx)** 를 넣는다. 한 종목에 같은 시각 전이가 여러 건 올 수
    # 있고(실측: 22:33:14 에 3->2 와 2->3 이 같은 ms 에), 그때는 DB 순서의 **마지막이
    # 유효한 티어**다 (`tier_at` 이 그렇게 읽는다). ts 로만 정렬하면 그 순서가 뒤집혀
    # 조용히 다른 값이 나온다 — 대조 테스트가 이걸 잡았다.
    events = sorted((ts, idx, sym, tt) for sym, ev in timeline.items()
                    for idx, (ts, _ft, tt) in enumerate(ev) if start_ms < ts < end_ms)
    total = 0
    lo = hi = n
    prev = start_ms
    i = 0
    while i < len(events):
        ts = events[i][0]
        total += n * (ts - prev)
        while i < len(events) and events[i][0] == ts:      # 같은 시각 전이는 한꺼번에
            _ts, _idx, sym, tt = events[i]
            was, now = cur.get(sym) == tier, tt == tier
            n += (1 if now else 0) - (1 if was else 0)
            cur[sym] = tt
            i += 1
        lo, hi = min(lo, n), max(hi, n)
        prev = ts
    total += n * (end_ms - prev)
    return {"avg": round(total / span, 2), "min": lo, "max": hi}


def ranking_poll_times(conn, start_ms: int, end_ms: int) -> list[int]:
    """구간 안 랭킹 폴 시각(고유). 수집기가 **살아서 폴링 중이었는지**의 근거로 쓴다."""
    return [int(r[0]) for r in conn.execute(
        "SELECT DISTINCT snap_ms FROM rankings_snap WHERE snap_ms BETWEEN ? AND ? "
        "ORDER BY snap_ms", (start_ms, end_ms))]


def ranking_intervals(conn, start_ms: int, end_ms: int) -> dict[str, dict]:
    """랭킹 폴 간격 분포 — 타입별 + 전체 풀링.

    타입별로 나누는 이유: 두 목록은 같은 주기로 **연달아** 찍히므로 풀링하면
    "0.3초, 11.7초, 0.3초..." 가 섞여 중앙값이 실제 폴 주기를 안 나타낸다.
    """
    out: dict[str, dict] = {}
    types = [r[0] for r in conn.execute(
        "SELECT DISTINCT ranking_type FROM rankings_snap WHERE snap_ms BETWEEN ? AND ?",
        (start_ms, end_ms))]
    for rt in sorted(types):
        ts = [int(r[0]) for r in conn.execute(
            "SELECT DISTINCT snap_ms FROM rankings_snap WHERE ranking_type=? "
            "AND snap_ms BETWEEN ? AND ? ORDER BY snap_ms", (rt, start_ms, end_ms))]
        out[rt] = percentiles((b - a) / 1000.0 for a, b in zip(ts, ts[1:]))
    pooled = ranking_poll_times(conn, start_ms, end_ms)
    out["(pooled, all types)"] = percentiles((b - a) / 1000.0
                                             for a, b in zip(pooled, pooled[1:]))
    return out


# --------------------------------------------------------------------------- #
# 창 **가장자리** 구멍 — 간격만 세는 도구가 구조적으로 못 보는 것
#
# 위의 모든 간격 통계는 **연속한 두 관측의 차이**로 만들어진다. 그래서 구멍이 창
# 가장자리에 있으면 비교할 상대가 창 밖이라 **간격이 아예 생기지 않는다.** 08-05 아침에
# 정확히 이 일이 났다: 08:01:46 에 수집이 멈추고 창은 08:50 에 끝났는데, 48.2분짜리
# 구멍이 목록에 없으니 리포트는 `max_gap=3.4min` 이라고 적었다. **수집이 창 끝에서
# 죽으면 이 도구는 이상 없다고 말한다** — 이 프로젝트가 다섯 번째로 만난 "성공을
# 반환하는 조용한 실패"다.
#
# 그래서 창 시작~첫 관측, 마지막 관측~창 끝을 **따로 이름 붙여** 잰다. 이름을 나누는
# 이유는 원인이 다르기 때문이다 — 가운데 구멍은 수집기 문제, 가장자리 구멍은 기계가
# 자거나 창 경계를 잘못 잡은 것이다. 뭉쳐서 한 숫자로 내면 그 구분이 사라진다.
# --------------------------------------------------------------------------- #

#: `ops/watchdog.ps1` 의 `-PlannedDefaultMin` 기본값. `until=` 이 없는 라이브 마커의
#: 유효기간이다. 값이 두 곳에 있는 것은 중복이지만 PowerShell 파라미터를 파이썬이 읽을
#: 방법이 없다 — 대신 **가정했다는 사실을 리포트에 찍는다**(`edge_hole_lines`).
PLANNED_DEFAULT_MIN = 30

_PLANNED_UNTIL_RX = re.compile(r"window until (\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})")
_PLANNED_REASON_RX = re.compile(r"^reason:\s*(.+?)\s*$", re.M)
_PLANNED_NAME_RX = re.compile(r"^PLANNED_(\d{8}_\d{6})_")
_MARKER_REASON_RX = re.compile(r"^\s*reason\s*=\s*(.+?)\s*$")
_MARKER_UNTIL_RX = re.compile(r"^\s*until\s*=\s*(.+?)\s*$")


@dataclass(frozen=True)
class Hole:
    """관측이 하나도 없었던 구간 1건. **이름이 붙어 있다**(어느 가장자리인가)."""
    name: str        # "leading_hole" | "trailing_hole" | "whole_window"
    start_ms: int
    end_ms: int

    @property
    def seconds(self) -> float:
        return max(0.0, (self.end_ms - self.start_ms) / 1000.0)

    @property
    def minutes(self) -> float:
        return self.seconds / 60.0


def _ts(ms: int) -> str:
    return f"{dt.datetime.fromtimestamp(ms / 1000.0):%Y-%m-%d %H:%M:%S}"


def _dur(seconds: float) -> str:
    """분 단위로 찍되 1분 미만은 초로. `0.0min` 은 '0'으로도 '아주 작음'으로도 읽혀서
    가장자리 구멍이 실제로 0인지 0.8초인지 구분되지 않는다 — 그 구분이 여기서는 중요하다."""
    return f"{seconds:.1f}s" if seconds < 60 else f"{seconds / 60.0:.1f}min"


def edge_holes(ts: list[int], start_ms: int, end_ms: int) -> dict:
    """관측 시각 목록에서 **가장자리 구멍과 관측 사이 간격을 따로** 낸다.

    `max_hole_s` 는 셋(앞 구멍/뒤 구멍/최대 간격) 중 최대다 — 이 값만이 "이 창의 최대
    공백"이라고 불릴 자격이 있다. 동률이면 가장자리 이름을 먼저 쓴다(숨어 있던 쪽이다).
    """
    obs = sorted(t for t in ts if start_ms <= t <= end_ms)
    inter = percentiles((b - a) / 1000.0 for a, b in zip(obs, obs[1:]))
    if not obs:
        # 관측이 0개면 가장자리라는 개념 자체가 없다 — 창 전체가 하나의 구멍이다.
        holes = [Hole("whole_window", start_ms, end_ms)]
    else:
        holes = [Hole("leading_hole", start_ms, obs[0]),
                 Hole("trailing_hole", obs[-1], end_ms)]
    worst = max([h.seconds for h in holes] + ([inter["max"]] if inter["max"] is not None else []))
    kind = next((h.name for h in holes if h.seconds >= worst), "inter_poll")
    return {"n_obs": len(obs), "holes": holes, "inter_poll": inter,
            "max_hole_s": worst, "max_hole_kind": kind,
            "first_ms": obs[0] if obs else None, "last_ms": obs[-1] if obs else None}


def planned_windows(log_dir: Path, state_dir: Path) -> list[tuple[int, int, str]]:
    """계획 정비 창을 **디스크에 남은 기록**에서 복원한다. 겹치는 창은 합친다.

    라이브 마커(`<state_dir>/PLANNED`)는 만료되면 워치독이 **지운다** — 잊힌 마커가
    진짜 장애를 며칠씩 침묵시키지 않게 하려는 설계다. 그래서 아침 리포트가 도는 시점에
    이미 없는 것이 정상이고, 마커만 보면 어젯밤의 계획 정비를 영영 못 본다.

    지워지지 않는 기록은 그 창 동안 워치독이 쓴 `PLANNED_*.txt` 의 머리말
    (`window until ...` + `reason:`)뿐이다. 그것을 정본으로 쓴다. 파일 시각은 창 **안의
    한 점**이므로 `[파일 시각, until]` 이 우리가 증명할 수 있는 **최소** 구간이다 —
    실제 창은 더 일찍 시작했을 수 있지만 그건 기록에 없으므로 주장하지 않는다.

    머리말에 `window until` 이 없는 `PLANNED_` 파일(STOP 파일 같은 운영자 행위)은
    **구간을 주장하지 않는다.** 사람이 그 순간 뭔가 했다는 증거일 뿐 창이 아니다.
    """
    raw: list[tuple[int, int, str]] = []
    marker = Path(state_dir) / "PLANNED"
    if marker.exists():
        try:
            reason, until = "", None
            for line in marker.read_text(encoding="utf-8", errors="replace").splitlines():
                m = _MARKER_REASON_RX.match(line)
                if m:
                    reason = m.group(1)
                m = _MARKER_UNTIL_RX.match(line)
                if m:
                    try:
                        until = dt.datetime.fromisoformat(m.group(1))
                    except ValueError:
                        until = None
            a = int(marker.stat().st_mtime * 1000)
            b = (int(until.timestamp() * 1000) if until
                 else a + PLANNED_DEFAULT_MIN * 60_000)
            raw.append((min(a, b), max(a, b),
                        f"라이브 마커: {reason or '(사유 미기재)'}"))
        except OSError:
            pass
    for p in sorted(Path(log_dir).glob("PLANNED_*.txt")):
        nm = _PLANNED_NAME_RX.match(p.name)
        if not nm:
            continue
        try:
            head = p.read_text(encoding="utf-8", errors="replace")[:512]
        except OSError:
            continue
        mu = _PLANNED_UNTIL_RX.search(head)
        if not mu:
            continue
        try:
            a = int(dt.datetime.strptime(nm.group(1), "%Y%m%d_%H%M%S").timestamp() * 1000)
            b = int(dt.datetime.strptime(mu.group(1), "%Y-%m-%d %H:%M:%S").timestamp() * 1000)
        except ValueError:
            continue
        mr = _PLANNED_REASON_RX.search(head)
        why = (mr.group(1).strip() if mr else "") or "(사유 미기재)"
        raw.append((min(a, b), max(a, b), why))
    raw.sort()
    merged: list[tuple[int, int, list[str]]] = []
    for a, b, why in raw:
        if merged and a <= merged[-1][1]:
            pa, pb, pw = merged[-1]
            merged[-1] = (pa, max(pb, b), pw + ([why] if why not in pw else []))
        else:
            merged.append((a, b, [why]))
    return [(a, b, " / ".join(w)) for a, b, w in merged]


def uncovered_span_ms(a_ms: int, b_ms: int, windows: list[tuple[int, int, str]]) -> int:
    """`[a,b]` 중 계획 창에 **안 덮인** 길이(ms). 창들은 겹치지 않는다고 본다(합쳐서 온다)."""
    covered = sum(max(0, min(b_ms, wb) - max(a_ms, wa)) for wa, wb, _ in windows)
    return max(0, (b_ms - a_ms) - covered)


def grade_hole(hole: Hole, windows: list[tuple[int, int, str]],
               normal_gap_s: float | None) -> tuple[str, str]:
    """(등급, 사유). `docs/34` 의 4등급 체계 그대로 — 새 등급을 만들지 않는다.

    **왜 `normal_gap_s` 로 먼저 거르는가**: 창을 어디서 자르든 가장자리에는 최대 한 폴
    주기만큼의 공백이 생긴다. 그건 결손이 아니라 **자른 위치가 만든 것**이다. 그래서
    "같은 창의 정상 폴 간격 상단보다 크지 않으면" 구멍이라고 부르지 않는다.

    **왜 p99 이고 max 가 아닌가**: max 를 쓰면 창 **가운데**의 큰 정지 하나가 가장자리
    판정 기준까지 끌어올려 진짜 가장자리 구멍을 삼킨다. 오늘 창이 정확히 그렇다 —
    가운데에 3.4분짜리가 있다. 다만 폴 수가 적은 창에서는 p99 가 사실상 max 라 이
    비교가 느슨해진다. 그때는 등급이 아니라 `max_hole` 값 자체를 보라(리포트가 늘 찍는다).
    """
    if normal_gap_s is not None and hole.seconds <= normal_gap_s:
        return ("NOTE_", f"이 창의 정상 폴 간격 상단(p99={normal_gap_s}s) 이내 — "
                         "창을 자른 위치가 만든 것이지 결손이 아니다")
    if not windows:
        return ("ALERT_", "계획 정비 창 기록이 없다 — 설명되지 않은 공백")
    hit = [w for w in windows if min(hole.end_ms, w[1]) > max(hole.start_ms, w[0])]
    left = uncovered_span_ms(hole.start_ms, hole.end_ms, windows)
    if left <= 0:
        return ("PLANNED_", "계획 정비 창 안 — " + "; ".join(w[2] for w in hit))
    why = f"{round(left / 60000.0, 1)}분이 계획 창 **밖** — 설명되지 않은 공백"
    if hit:
        why += " (일부만 덮임: " + "; ".join(w[2] for w in hit) + ")"
    return ("ALERT_", why)


def edge_hole_lines(label: str, eh: dict, windows: list[tuple[int, int, str]]) -> list[str]:
    """가장자리 구멍 절. `max_hole` 을 먼저 찍고, 그 아래에 무엇으로 이루어졌는지 편다."""
    inter = eh["inter_poll"]
    normal = inter["p99"]
    lines = [
        f"[{label} 공백] 가장자리와 가운데를 **따로** 센다 "
        "— 간격만 세면 창 끝에서 죽은 수집이 안 보인다",
        f"  max_hole        : {_dur(eh['max_hole_s'])}  "
        f"[{eh['max_hole_kind']}]   <= 이 창의 진짜 최대 공백",
    ]
    graded = [(h, grade_hole(h, windows, normal)) for h in eh["holes"]]
    for h, (grade, why) in graded:
        lines.append(f"  {h.name:16s}: {_dur(h.seconds):>8s}  "
                     f"({_ts(h.start_ms)} -> {_ts(h.end_ms)})")
        lines.append(f"    -> {grade} {why}")
    if inter["max"] is None:
        lines.append("  max_inter_poll  : (관측이 2개 미만 — 간격이라는 것이 없다)")
    else:
        lines.append(f"  max_inter_poll  : {_dur(inter['max']):>8s}  "
                     f"(연속한 두 관측 사이 최대, p99={inter['p99']}s) "
                     "— **가장자리 구멍은 여기 절대 안 들어온다**")
    lines.append(f"  관측 {eh['n_obs']}개 / 대조한 계획 정비 창 {len(windows)}개"
                 + ("  (기록 없음 — 가장자리 공백은 전부 ALERT_)" if not windows else "")
                 + f"  [until 없는 마커는 {PLANNED_DEFAULT_MIN}분으로 가정]")
    if any(g == "ALERT_" for _h, (g, _w) in graded):
        lines.append("  !! 설명되지 않은 가장자리 공백이 있다 — 기계가 잤거나 수집이 창 "
                     "끝에서 죽었다. collector.log 와 ALERT 파일을 대조할 것.")
    return lines


def orderbook_intervals(conn, start_ms: int, end_ms: int,
                        timeline: dict) -> dict[str, dict]:
    """호가 스냅 간격 분포 — **티어별로 분리**해서 낸다.

    한 테이블에 목표 주기가 다른 두 계열이 섞여 있다(tier3 4초, tier2 600초). 풀링하면
    둘 다 거짓말이 된다. 또 **소속이 끊긴 구간의 간격은 결손이 아니다** — 안 보던
    시간이다. 그래서 연속 두 스냅 사이 내내 같은 티어였던 간격만 그 티어로 센다.
    """
    rows = conn.execute(
        "SELECT symbol, snap_ms FROM orderbook_snap WHERE snap_ms BETWEEN ? AND ? "
        "ORDER BY symbol, snap_ms", (start_ms, end_ms)).fetchall()
    by_sym: dict[str, list[int]] = defaultdict(list)
    for sym, ms in rows:
        by_sym[sym].append(int(ms))
    buckets: dict[str, list[float]] = defaultdict(list)
    for sym, ms_list in by_sym.items():
        for a, b in zip(ms_list, ms_list[1:]):
            placed = False
            for tier in (3, 2):
                if stayed_in_tier(timeline, sym, tier, a, b):
                    buckets[f"tier{tier}"].append((b - a) / 1000.0)
                    placed = True
                    break
            if not placed:
                # 티어가 바뀌었거나 이력이 없는 구간. 결손으로도 정상으로도 못 읽는다.
                buckets["tier-changed-or-unknown"].append((b - a) / 1000.0)
    return {k: percentiles(v) for k, v in sorted(buckets.items())}


MINUTE_MS = 60_000
#: 봉 라벨 오프셋을 **데이터에서 잰다**. 가정하지 않는 이유는 처음 짤 때 가정했다가
#: 틀렸기 때문이다 — "체결은 있는데 봉이 없는 분"이 40% 나왔고 그건 결손이 아니라
#: **내 가정이 틀린 것**이었다(`docs/31` §2). 매 실행마다 다시 재므로 API 관례가
#: 바뀌면 리포트가 먼저 안다.
OFFSET_PROBE_RANGE = (-2, -1, 0, 1, 2)


def minute_floor(ms: int) -> int:
    return int(ms) // MINUTE_MS * MINUTE_MS


def trade_minutes(conn, start_ms: int, end_ms: int) -> dict[tuple[str, int], tuple[int, int]]:
    """(종목, 분) -> (우리가 받은 체결 수량 합, 건수)."""
    out: dict[tuple[str, int], tuple[int, int]] = {}
    for sym, ms, qty in conn.execute(
            "SELECT symbol, ts_ms, qty_u FROM trades_snap WHERE ts_ms BETWEEN ? AND ?",
            (start_ms, end_ms)):
        key = (str(sym), minute_floor(ms))
        v, n = out.get(key, (0, 0))
        out[key] = (v + int(qty), n + 1)
    return out


def candle_minutes(conn, start_ms: int, end_ms: int) -> dict[tuple[str, int], int]:
    """(종목, 봉 라벨 분) -> 봉 거래량."""
    return {(str(sym), minute_floor(ms)): int(vol) for sym, ms, vol in conn.execute(
        "SELECT symbol, ts_ms, vol_qu FROM candles_1m WHERE ts_ms BETWEEN ? AND ?",
        (start_ms, end_ms))}


def candle_label_offset(trades: dict, candles: dict) -> dict:
    """봉 라벨이 체결 분보다 몇 분 뒤인지를 **실측**한다.

    두 증거를 같이 본다: (a) 체결이 있던 분에 대해 오프셋 k 의 봉이 존재하는 비율,
    (b) 그 봉의 거래량이 우리가 받은 체결 합과 **정확히** 같은 비율. 거래량 일치가
    결정적이다 — 존재율만 보면 봉 밀도가 높을 때 아무 오프셋이나 그럴듯해 보인다.
    """
    scores = []
    for off in OFFSET_PROBE_RANGE:
        hit = exact = compared = 0
        for (sym, mn), (vol, _n) in trades.items():
            bar = candles.get((sym, mn + off * MINUTE_MS))
            if bar is None:
                continue
            hit += 1
            compared += 1
            if bar == vol:
                exact += 1
        scores.append({"offset_min": off, "present": hit,
                       "present_pct": round(100.0 * hit / len(trades), 1) if trades else None,
                       "vol_exact": exact,
                       "vol_exact_pct": round(100.0 * exact / compared, 1) if compared else None})
    best = max(scores, key=lambda s: (s["vol_exact"], s["present"]))
    return {"scores": scores, "best_offset_min": best["offset_min"] if trades else None,
            "best": best}


def candle_coverage(trades: dict, candles: dict, timeline: dict, offset_min: int | None) -> dict:
    """봉 없는 분을 **모순**과 **구분 불가**로 가른다. 뭉쳐서 한 숫자로 내지 않는다.

    무체결 분에는 1분봉이 원래 없다(`docs/06`). 그래서 "봉이 없다"만으로는 결손을
    주장할 수 없다. 우리가 말할 수 있는 것은 하나다 — **그 분에 체결을 실제로 받아
    놓고 (실측 오프셋 자리에) 봉은 없는 경우.** 두 소스가 어긋난 것이다.

    체결도 봉도 없는 분은 **원리적으로 구분 불가**다. 체결은 tier3 만 받으므로
    tier2-only 종목은 애초에 대조 자체가 불가능하다 — 그 규모도 같이 낸다.
    """
    if offset_min is None:
        return {"comparable_minutes": 0, "missing": 0, "by_symbol_top": [],
                "by_session": {}, "symbols_with_candles": len({s for s, _ in candles}),
                "symbols_with_trades": 0, "trailing_edge_skipped": 0}
    off = offset_min * MINUTE_MS
    # 봉은 `tier2_candle_s` 주기로 뒤늦게 받아온다. 그래서 **가장 최근 분은 아직 안 온
    # 것이 정상**이고 그걸 결손으로 세면 창을 지금까지 잡을수록 결손이 늘어나는 가짜
    # 지표가 된다. 종목별로 **실제 받은 마지막 봉 라벨**을 경계로 쓴다 — 상수가 아니라
    # 그 종목의 관측치이므로 폴 주기를 몰라도 정확하다.
    newest: dict[str, int] = {}
    for sym, mn in candles:
        if mn > newest.get(sym, -1):
            newest[sym] = mn
    comparable = [(sym, mn) for (sym, mn) in trades
                  if sym in newest and mn + off <= newest[sym]]
    skipped = len(trades) - len(comparable)
    missing = [(sym, mn) for (sym, mn) in comparable if (sym, mn + off) not in candles]
    return {
        "symbols_with_candles": len({s for s, _ in candles}),
        "symbols_with_trades": len({s for s, _ in trades}),
        "comparable_minutes": len(comparable),
        "trailing_edge_skipped": skipped,
        "missing": len(missing),
        "by_symbol_top": Counter(s for s, _ in missing).most_common(8),
        "by_session": dict(Counter(kst_session(mn) for _, mn in missing)),
    }


def tape_completeness(trades: dict, candles: dict, timeline: dict,
                      offset_min: int | None) -> dict:
    """**테이프가 실제로 얼마나 담겼는가** — 봉 거래량을 독립 기준으로 쓴 대조.

    결손 사건을 세는 것보다 이쪽이 강하다. 사건 수는 "몇 번 놓쳤나"이지만, 이것은
    **"그 분에 시장에서 일어난 것 중 몇 %를 우리가 가지고 있나"** 다. 봉 거래량은
    우리 폴링과 독립이므로 우리 자신을 기준으로 우리를 재는 순환이 없다.

    tier3 소속이 그 분 내내 유지된 (종목,분)만 센다 — 안 보던 분을 결손으로 세면
    거짓말이 된다. 봉 거래량 0 인 분은 비율이 정의되지 않아 제외한다.
    """
    if offset_min is None:
        return {"minutes": 0, "complete": 0, "short": [], "note": "오프셋 미측정"}
    off = offset_min * MINUTE_MS
    rows = []
    for (sym, mn), (got, n) in trades.items():
        bar = candles.get((sym, mn + off))
        if bar is None or bar <= 0:
            continue
        if not stayed_in_tier(timeline, sym, 3, mn, mn + MINUTE_MS):
            continue
        rows.append({"symbol": sym, "minute_ms": mn, "got": got, "bar": bar,
                     "ratio": got / bar, "n_trades": n})
    complete = [r for r in rows if r["ratio"] >= 0.999]
    short = sorted((r for r in rows if r["ratio"] < 0.999), key=lambda r: r["ratio"])
    over = [r for r in rows if r["ratio"] > 1.001]
    bar_total = sum(r["bar"] for r in rows)
    missed = sum(r["bar"] - r["got"] for r in short)
    return {
        "minutes": len(rows),
        "complete": len(complete),
        "complete_pct": round(100.0 * len(complete) / len(rows), 2) if rows else None,
        "short": short,
        "over": len(over),
        "ratio_stats": percentiles(r["ratio"] for r in rows),
        "missed_share_pct": round(100.0 * missed / bar_total, 3) if bar_total else None,
        "by_session": dict(Counter(kst_session(r["minute_ms"]) for r in short)),
        "by_symbol_top": Counter(r["symbol"] for r in short).most_common(6),
    }


def classify_gaps(events: list[GapEvent], timeline: dict, restarts: list[int],
                  ranking_ts: list[int], ranking_median_s: float | None) -> dict:
    """체결 결손을 **기계적 사실 세 가지**로 세 버킷에 나눈다. 뭉치지 않는다.

    - `A_in_watch`  응답이 잘렸고(n=50), 구간 내내 tier3 였고, 그 구간에 수집기가
      살아 있었다 → **우리가 보던 중에 실제로 잃은 테이프.** 이 숫자만이 결손이다.
    - `B_not_watched` 응답은 잘렸지만 그 구간에 티어가 끊겼거나 수집기가 없었다
      → 잃은 게 아니라 **안 보던 시간**. 길이를 결손에 더하면 거짓말이 된다.
    - `C_not_cap_bound` n<50 → API 가 가진 것을 다 줬다. **결손의 증거가 아니다.**
    """
    def alive(a_ms: int, b_ms: int) -> bool:
        """구간에 수집기가 폴링 중이었는가 — 랭킹 폴 밀도로 본다.

        구간 **안쪽만** 보면 안 된다. 결손 대부분은 1~3초인데 랭킹 폴은 12초 간격이라,
        멀쩡한 수집기라도 3초 구간 안에는 폴이 0회인 것이 정상이다. 그대로 재면 진짜
        결손이 전부 "수집기 부재"로 밀려나 A 버킷이 비고, **결손 없음을 만들어내는
        도구**가 된다(테스트가 이걸 잡았다). 그래서 양옆으로 랭킹 간격만큼 넓힌 창에서
        밀도를 본다 — 짧은 구간도 판정 가능해지고, 진짜 정지는 여전히 성기다.
        """
        if not ranking_median_s or ranking_median_s <= 0:
            return any(a_ms < t <= b_ms for t in ranking_ts)
        pad = int(ranking_median_s * 1000)
        lo, hi = a_ms - pad, b_ms + pad
        seen = sum(1 for t in ranking_ts if lo < t <= hi)
        expected = (hi - lo) / 1000.0 / ranking_median_s
        return expected <= 0 or (seen / expected) >= RANKING_COVERAGE_MIN

    out: dict[str, list[GapEvent]] = {"A_in_watch": [], "B_not_watched": [],
                                      "C_not_cap_bound": []}
    reclassified_by_liveness = 0
    for ev in events:
        if ev.n < TRADES_COUNT_CAP:
            out["C_not_cap_bound"].append(ev)
            continue
        tier_ok = stayed_in_tier(timeline, ev.symbol, 3, ev.prev_max_ms, ev.this_min_ms)
        if not tier_ok:
            out["B_not_watched"].append(ev)
            continue
        if not alive(ev.prev_max_ms, ev.this_min_ms):
            reclassified_by_liveness += 1
            out["B_not_watched"].append(ev)
            continue
        out["A_in_watch"].append(ev)
    return {"buckets": out, "reclassified_by_liveness": reclassified_by_liveness}


def gap_report_lines(events: list[GapEvent], timeline: dict, restarts: list[int],
                     ranking_ts: list[int], ranking_median_s: float | None) -> list[str]:
    """체결 결손 절 — 분포 먼저, 그 다음 위치."""
    res = classify_gaps(events, timeline, restarts, ranking_ts, ranking_median_s)
    b = res["buckets"]
    a_ev = b["A_in_watch"]
    spans = [e.span_s for e in a_ev]
    lines = [
        "[체결 tape gap] 소스: collector.log 의 tape gap 줄 (위치가 있는 유일한 결손 기록)",
        f"  A 관측 중 실제 결손 (n=50 & 내내 tier3 & 수집기 생존) : {len(a_ev)}건",
        f"  B 안 보던 시간 (티어 끊김 또는 수집기 부재)            : {len(b['B_not_watched'])}건"
        f"  [그중 생존검사로 재분류 {res['reclassified_by_liveness']}건]",
        f"  C 응답 미포화 (n<50, API 가 가진 것을 다 줌 = 결손 증거 아님): "
        f"{len(b['C_not_cap_bound'])}건",
    ]
    if not a_ev:
        lines.append("  A 버킷 비어 있음 — 이 창에서는 관측 중 결손이 관측되지 않았다.")
        return lines
    p = percentiles(spans)
    lines += [
        f"  A 결손 길이(초) n={p['n']} p50={p['p50']} p90={p['p90']} p99={p['p99']} "
        f"max={p['max']} min={p['min']}",
        f"  A 결손 총합: {round(sum(spans), 1)}초",
    ]
    # 6.0초는 우리가 재기로 한 현상의 크기(docs/29 틱 기준 슈팅 지속시간 중앙값)이지
    # 결손의 임계가 아니다. 결손을 그 자로 재보는 것뿐이다.
    over = [e for e in a_ev if e.span_s > 6.0]
    lines.append(f"  A 중 6.0초(슈팅 지속 중앙값)보다 긴 결손: {len(over)}건")
    by_sym = Counter(e.symbol for e in a_ev)
    lines.append("  A 종목별 상위: " + ", ".join(f"{s}={c}" for s, c in by_sym.most_common(8)))
    by_sess = Counter(kst_session(e.wall_ms) for e in a_ev)
    lines.append("  A 세션별: " + ", ".join(f"{s}={c}" for s, c in sorted(by_sess.items())))
    worst = sorted(a_ev, key=lambda e: -e.span_s)[:5]
    lines.append("  A 최장 5건 (시각 KST / 종목 / 초):")
    lines += [f"    {dt.datetime.fromtimestamp(e.wall_ms/1000):%Y-%m-%d %H:%M:%S}  "
              f"{e.symbol:6s} {e.span_s:.1f}s" for e in worst]
    return lines


def config_sig_lines(sigs: list[tuple[int, str]], start_ms: int, end_ms: int) -> list[str]:
    """이 창에서 **실제로 유효했던** 설정 지문 전부. 하나가 아니면 크게 말한다.

    `COLLECTION-CONFIG` 는 기동 때만 찍히므로, 창 안에 그 줄이 없어도 창 시작 시점에는
    **직전에 찍힌 지문**이 유효하다. 창 안의 줄만 보면 (a) 설정이 안 바뀐 긴 창에서
    "못 찾음"이 나오거나 (b) 창 끝의 최신 지문 하나만 실려 **경계가 다른 데이터를 한
    지문으로 뭉치게** 된다. 실제로 08-03~08-04 창에서 그렇게 나왔다 — 그 창은 랭킹 4종
    + 호가 16초 구간과 랭킹 2종 + 호가 4초 구간이 섞여 있었는데 지문은 후자만 실렸다.
    분석이 그걸 한 덩어리로 읽으면 조용히 틀린다.
    """
    before = [(ms, s) for ms, s in sigs if ms <= start_ms]
    inside = [(ms, s) for ms, s in sigs if start_ms < ms <= end_ms]
    active: list[tuple[int, str]] = []
    if before:
        active.append((start_ms, before[-1][1]))       # 창 시작 시점의 유효 지문
    for ms, s in inside:
        if not active or s != active[-1][1]:
            active.append((ms, s))
    if not active:
        # `COLLECTION-CONFIG` 는 08-04 11:22:42 배포부터 찍힌다. 그 이전 창에는 지문이
        # 아예 없다 — 없는 것을 있는 척하지 않는다. 대신 데이터 자체가 드러내는 표지를
        # 가리킨다: 랭킹 타입 수(4종=구/2종=신)와 tier3 호가 p50(16초=구/4초=신).
        return ["config_sig: (로그에 없음 — 이 창은 지문 기능 배포 08-04 11:22:42 이전이거나 "
                "회전 보존기간 밖이다)",
                "  대신 아래 [랭킹 폴 간격] 타입 수와 [호가] tier3 p50 을 사실상의 지문으로 "
                "대조할 것 — 4종·16초면 구 설정, 2종·4초면 신 설정이다."]
    out = [f"config_sig: {len(active)}종"]
    out += [f"  {dt.datetime.fromtimestamp(ms/1000):%Y-%m-%d %H:%M:%S} 부터  {s}"
            for ms, s in active]
    if len(active) > 1:
        out.append("  !! 이 창은 설정 경계를 가로지른다 — 아래 수치를 한 덩어리로 읽지 마라. "
                   "구간을 나눠 다시 돌릴 것(--start/--end).")
    return out


def audit(cfg, start_ms: int, end_ms: int, label: str) -> str:
    """한 창에 대한 감사 본문. **측정 조건을 결론과 같이 적는다**(구속력 있는 형식)."""
    lines = [
        f"=== gap audit {label} ===",
        f"window (KST): {dt.datetime.fromtimestamp(start_ms/1000):%Y-%m-%d %H:%M:%S}"
        f" ~ {dt.datetime.fromtimestamp(end_ms/1000):%Y-%m-%d %H:%M:%S}"
        f"  ({round((end_ms-start_ms)/3_600_000, 2)}h)",
        f"generated: {dt.datetime.now():%Y-%m-%d %H:%M:%S}",
        "판정하지 않는다 — 이것은 품질 지표이지 '수집이 충분한가'에 대한 답이 아니다.",
        "",
    ]
    gaps, restarts, sigs = parse_log(cfg.log_dir)
    lines += config_sig_lines(sigs, start_ms, end_ms)
    lines.append(f"수집기 재기동: {sum(1 for r in restarts if start_ms <= r <= end_ms)}회")
    lines.append("")

    conn = _ro_connect(cfg.db_path)
    try:
        timeline = tier_timeline(conn)
        rk = ranking_intervals(conn, start_ms, end_ms)
        lines.append("[랭킹 폴 간격(초)] 소스: rankings_snap.snap_ms")
        for rt, st in rk.items():
            lines.append(f"  {rt:34s} n={st['n']:<7d} p50={st['p50']} p90={st['p90']} "
                         f"p99={st['p99']} max={st['max']}")
        pooled = rk.get("(pooled, all types)", {})
        med = None
        for rt, st in rk.items():
            if rt != "(pooled, all types)" and st["p50"]:
                med = st["p50"]
                break
        lines.append("")

        # 위의 간격 분포는 **연속한 두 폴의 차이**만 본다 — 창 가장자리 구멍은 비교할
        # 상대가 창 밖이라 애초에 목록에 안 들어온다. 그래서 따로 잰다.
        eh = edge_holes(ranking_poll_times(conn, start_ms, end_ms), start_ms, end_ms)
        pw = planned_windows(cfg.log_dir, cfg.state_dir)
        lines += edge_hole_lines("랭킹 폴", eh, pw)
        lines += [
            "  ** 호가·분봉에도 같은 구조적 사각이 있다(전부 연속 관측의 차이로 잰다). "
            "다만 그쪽 가장자리 공백은 **티어 소속 없이는 읽을 수 없다** — 창 시작 시점에 "
            "tier3 가 아니던 종목에 스냅이 없는 것은 결손이 아니라 안 보던 것이다. "
            "여기서는 재지 않는다(뭉치지 않는다는 원칙).",
            "",
        ]

        ob = orderbook_intervals(conn, start_ms, end_ms, timeline)
        lines.append("[호가 스냅 간격(초)] 소스: orderbook_snap.snap_ms, 티어별 분리")
        if not ob:
            lines.append("  (구간에 호가 스냅 없음)")
        for tier, st in ob.items():
            lines.append(f"  {tier:24s} n={st['n']:<7d} p50={st['p50']} p90={st['p90']} "
                         f"p99={st['p99']} max={st['max']}")
        lines.append("")

        # 봉 라벨 오프셋은 **매번 실측**한다. 이 값을 가정했다가 틀린 적이 있다.
        tr = trade_minutes(conn, start_ms, end_ms)
        cd = candle_minutes(conn, start_ms - MINUTE_MS * 3, end_ms + MINUTE_MS * 3)
        off = candle_label_offset(tr, cd)
        best = off["best"]
        lines.append("[1분봉 라벨 오프셋 — 실측] 소스: candles_1m × trades_snap")
        for s in off["scores"]:
            mark = " <= 채택" if s["offset_min"] == off["best_offset_min"] else ""
            lines.append(f"  offset {s['offset_min']:+d}min  봉 존재 {s['present_pct']}%  "
                         f"거래량 정확일치 {s['vol_exact_pct']}% ({s['vol_exact']}건){mark}")
        lines.append(f"  => 봉 ts_ms 는 체결 분보다 {off['best_offset_min']}분 뒤에 찍힌다. "
                     f"오프셋 +1 이면 봉이 **구간 종료 시각**으로 라벨된 것이다.")
        lines.append("")

        cc = candle_coverage(tr, cd, timeline, off["best_offset_min"])
        lines += [
            "[1분봉 커버리지] 실측 오프셋 적용",
            f"  봉이 있는 종목 {cc['symbols_with_candles']} / 체결을 받은 종목 "
            f"{cc['symbols_with_trades']} (대조 가능한 것은 후자뿐)",
            f"  대조 가능한 (종목,분) : {cc['comparable_minutes']}  "
            f"[아직 봉이 안 온 최신 구간 {cc['trailing_edge_skipped']}개 제외 — "
            f"종목별 마지막 봉 라벨 기준]",
            f"  체결은 있는데 봉이 없는 분 (모순): {cc['missing']}",
            f"    세션별: {cc['by_session'] or '(없음)'}",
            f"    종목별 상위: {cc['by_symbol_top'] or '(없음)'}",
            "  구분 불가: 체결이 없는 분은 봉이 없는 것이 정상이라 결손과 구분되지 않는다. "
            "체결은 tier3 만 받으므로 tier2-only 종목은 대조 자체가 불가능하다.",
            "",
        ]

        tc = tape_completeness(tr, cd, timeline, off["best_offset_min"])
        lines.append("[테이프 완결성 — 봉 거래량 대조] 우리 폴링과 **독립인** 기준으로 잰다")
        if not tc["minutes"]:
            lines.append("  대조 가능한 분 없음 (tier3 소속 & 봉 거래량>0 인 분이 없다)")
        else:
            lines += [
                f"  대조 가능한 (종목,분): {tc['minutes']}  "
                f"[tier3 소속이 그 분 내내 유지 & 봉 거래량>0]",
                f"  체결 합 == 봉 거래량 (완전 포착): {tc['complete']} "
                f"= {tc['complete_pct']}%",
                f"  포착 비율 분포: p50={tc['ratio_stats']['p50']} "
                f"p90={tc['ratio_stats']['p90']} min={tc['ratio_stats']['min']}",
                f"  놓친 수량 비중: {tc['missed_share_pct']}% (전체 봉 거래량 대비)",
                f"  미달 분 세션별: {tc['by_session'] or '(없음)'}  "
                f"종목별: {tc['by_symbol_top'] or '(없음)'}",
            ]
            if tc["over"]:
                lines.append(f"  체결 합 > 봉 거래량: {tc['over']}건 "
                             "(봉 라벨 경계에 걸친 체결 등 — 결손이 아니다)")
            for r in tc["short"][:5]:
                lines.append(
                    f"    {dt.datetime.fromtimestamp(r['minute_ms']/1000):%m-%d %H:%M} "
                    f"{r['symbol']:6s} 받은={r['got']:,} 봉={r['bar']:,} "
                    f"비율={r['ratio']:.3f}")
        lines.append("")

        pop3 = tier_population(timeline, 3, start_ms, end_ms)
        pop2 = tier_population(timeline, 2, start_ms, end_ms)
        rk_ts = ranking_poll_times(conn, min(start_ms, min((g.prev_max_ms for g in gaps),
                                                           default=start_ms)), end_ms)
    finally:
        conn.close()

    win_gaps = [g for g in gaps if start_ms <= g.wall_ms <= end_ms]
    lines += gap_report_lines(win_gaps, timeline, restarts, rk_ts, med)
    lines += [
        "",
        f"[측정 조건] 창 {round((end_ms-start_ms)/3_600_000, 2)}h, "
        f"tier3 평균 {pop3['avg']}종목(min {pop3['min']} max {pop3['max']}), "
        f"tier2 평균 {pop2['avg']}종목, "
        f"랭킹 폴 {pooled.get('n', 0)}회, 호가 스냅 {sum(s['n'] for s in ob.values())}개, "
        f"체결 대조 가능 분 {cc['comparable_minutes']}개 "
        f"(그중 거래량 대조 가능 {tc['minutes']}개), "
        f"tape gap 원시 {len(win_gaps)}건",
        "  ** tier3 종목 수 없이 '결손 없음'을 읽지 마라. tier3 가 쪼그라든 창에서 결손이 "
        "0인 것은 수집이 좋아서가 아니라 볼 것이 없어서일 수 있다 (docs/30 §4: 결손은 호가 "
        "주기가 아니라 tier3 종목 수를 따라간다).",
        "  ** config_sig 에 usage_ratio 는 들어 있지 않다 — 예산 파라미터가 바뀌어도 지문은 "
        "그대로다. 창 안에서 그 값이 바뀌었는지는 collector.log 와 대조해야 한다.",
        f"[생존 판정 임계] RANKING_COVERAGE_MIN={RANKING_COVERAGE_MIN} "
        f"(랭킹 간격 중앙 {med}초 기준). 이 값이 바뀌면 A/B 경계가 바뀐다.",
        "[조회 한계] 체결 결손은 collector.log 에만 남는다 — 로그 회전 보존기간 밖은 못 본다.",
    ]
    return "\n".join(lines) + "\n"


def window_from_args(date_str: str | None, hours: float | None,
                     start_s: str | None = None, end_s: str | None = None
                     ) -> tuple[int, int, str]:
    """창 결정. 우선순위: 명시 구간 > --hours > 수집일(전일 09:00 ~ 당일 08:50).

    명시 구간(`--start/--end`)이 있는 이유: 리포트에 **정확히 어느 창을 쟀는지** 적어야
    하고(구속력 있는 형식), 세션 경계에 딱 맞춘 재현이 가능해야 하기 때문이다.
    """
    now = dt.datetime.now()
    if start_s:
        a = dt.datetime.fromisoformat(start_s)
        b = dt.datetime.fromisoformat(end_s) if end_s else now
        return (int(a.timestamp() * 1000), int(b.timestamp() * 1000),
                f"{a:%Y%m%d-%H%M}~{b:%H%M}")
    if hours:
        start = now - dt.timedelta(hours=hours)
        return (int(start.timestamp() * 1000), int(now.timestamp() * 1000),
                f"last{hours:g}h")
    end_day = dt.datetime.strptime(date_str, "%Y%m%d") if date_str else now
    end = end_day.replace(hour=8, minute=50, second=0, microsecond=0)
    start = (end_day - dt.timedelta(days=1)).replace(hour=9, minute=0, second=0,
                                                     microsecond=0)
    return int(start.timestamp() * 1000), int(end.timestamp() * 1000), end.strftime("%Y%m%d")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="결손 감사 (읽기 전용, 라이브 API 호출 없음)")
    ap.add_argument("--config", default=None)
    ap.add_argument("--date", default=None, help="YYYYMMDD (수집일이 끝나는 날)")
    ap.add_argument("--hours", type=float, default=None, help="지금부터 N시간 전까지")
    ap.add_argument("--start", default=None, help="ISO 시각 (예 2026-08-04T22:30)")
    ap.add_argument("--end", default=None, help="ISO 시각 (기본 지금)")
    ap.add_argument("--out", default=None, help="파일로도 저장할 경로")
    args = ap.parse_args(argv)
    for stream in (sys.stdout, sys.stderr):      # Windows 콘솔 cp949 대응
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass
    cfg = load_ops_config(args.config)
    start_ms, end_ms, label = window_from_args(args.date, args.hours, args.start, args.end)
    text = audit(cfg, start_ms, end_ms, label)
    print(text)
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
        print(f"written: {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
