"""완료 시각 계상 vs **송신 시각** 계상을 오프라인으로 대조한다 (docs/52 §4).

라이브 호출 0. 수집기 재시작 0. 로그는 **읽기 전용**.

무엇을 재는가 / 못 재는가 (먼저 읽을 것)
----------------------------------------
운영 로그에는 5분마다 `md_peak_1s` **집계값**만 남는다. **개별 송신 시각이 없다.**
그래서 "같은 로그 구간을 새 계상으로 다시 돌린다" 는 원리상 불가능하다 — 새 계상의
입력(송신 시각)이 로그에 존재하지 않기 때문이다. 이 스크립트는 그 자리를 두 개로 나눈다:

  **A. 모델 없는 부분 (로그에서 직접)**
     옛 계상의 첨두 분포를 그대로 세고, 그중 **구조적으로 불가능한** 표본을 센다.
     송신 시각 계상에서 `peak_1s <= window_cap = 10` 은 증명된 상계이므로
     (`docs/45` §2 + `tests/test_send_time_accounting.py`), 10 을 넘은 표본은
     **전부** 계상이 만든 것이고 새 계상에서는 반드시 10 이하로 내려온다.
     이 진술에는 모델이 없다.

  **B. 모델 있는 부분 (시뮬레이션)**
     같은 송신열 위에서 두 계상을 **프로덕션 코드 그대로** 돌려 첨두 분포를 대조한다.
     옛 경로는 "송신 시각을 못 주는 client" 로 내려가는 바로 그 경로이고, 새 경로는
     송신 시각을 주는 경로다 — 옛 코드를 재구현하지 않는다. 다른 것은 **시각 출처
     하나뿐**이다.

     여기에 모델이 하나 들어간다: **완료가 언제 도착하는가.** RTT 는 실측
     (54.6~141.0ms, p50 71.0, n=47 — docs/06)을 쓰지만, 이벤트루프가 DB 쓰기로
     막혔다 풀리는 시간(`--stall`)은 **관측이 없다.** 그래서 값을 하나 고르지 않고
     **쓸어서** 낸다: 새 계상은 이 값에 **불변**이고 옛 계상만 따라 오른다는 것이
     대조의 핵심이고, 운영에서 관측된 10~12 를 재현하는 stall 값을 같이 보고한다.
     그 값은 **적합(fit)이지 측정이 아니다** — 그렇게 읽어야 한다.

부하 모형 (B 의 입력)
---------------------
2026-08-10 정규장 표본의 실제 설정을 쓴다 (`config_sig` 에서 읽은 값):
tier3 체결 `tier3_cap/3s`, tier3 호가 `tier3_cap/4s`, tier1 스윕 8배치/45s(간격 벌림),
tier2 호가 `tier2_cap/600s`. tier3 루프는 만기 항목을 **연달아** 쏘므로 한 배치가
`2 x tier3_cap` 건이다 (`tools/replay_gating.py` 의 관측과 같은 모형).

재실행:
    python -m tools.replay_send_time --log ../w5-ops/data/collector.log \\
        --since "2026-08-10 22:30" --until "2026-08-11 05:00"
"""
from __future__ import annotations

import argparse
import random
import re
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tossmon.api.limiter import _Bucket                                  # noqa: E402
from tossmon.collector.budget import GROUP_MARKET_DATA                   # noqa: E402
from tossmon.collector.loops import CollectorContext                     # noqa: E402
from tossmon.collector.notifier import Notifier                          # noqa: E402
from tossmon.store import Store                                          # noqa: E402

RE_MD = re.compile(r"MARKET_DATA=peak(?P<peak>\d+)/p95:(?P<p95>[\d.]+)"
                   r"/avg(?P<avg>[\d.]+)/tgt(?P<tgt>[\d.]+)")
RE_TS = re.compile(r"^(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})")
RE_F = re.compile(r"\b(session|tier2_cap|tier3_cap|md_peak_1s|md_p95_1s)=([\w.]+)")

MD_LIMIT = 10           # 공시 한도 = 리미터 window_cap = 새 계상의 구조적 상계
USAGE_RATIO = 0.85      # config/config.example.yaml
#: 실측 RTT (docs/06 §9-4): 최소 54.6ms, p50 71.0ms, 최대 141.0ms, n=47.
RTT_MIN_S, RTT_P50_S, RTT_MAX_S = 0.0546, 0.0710, 0.1410


# --------------------------------------------------------------------------- #
# A. 로그 (모델 없음)
# --------------------------------------------------------------------------- #
def parse_log(log: Path, since: str | None, until: str | None) -> list[dict]:
    rows: list[dict] = []
    with log.open(encoding="utf-8", errors="replace") as fh:
        for line in fh:
            if " telemetry " not in line or "md_peak_1s=" not in line:
                continue
            ts, md = RE_TS.match(line), RE_MD.search(line)
            if not (ts and md):
                continue
            stamp = ts.group("ts")
            if (since and stamp < since) or (until and stamp > until):
                continue
            row = {"ts": stamp, "peak": int(md.group("peak")),
                   "p95": float(md.group("p95")), "avg": float(md.group("avg"))}
            row.update({k: v for k, v in RE_F.findall(line)})
            rows.append(row)
    return rows


def hist(values) -> dict[int, int]:
    out: dict[int, int] = {}
    for v in values:
        out[int(v)] = out.get(int(v), 0) + 1
    return out


def show_hist(h: dict[int, int], n: int, *, mark_over: int | None = None) -> str:
    parts = []
    for k in sorted(h):
        flag = "*" if mark_over is not None and k > mark_over else ""
        parts.append(f"{k}{flag}:{h[k]}({100.0 * h[k] / n:.0f}%)")
    return "  ".join(parts)


def table_a(rows: list[dict]) -> None:
    n = len(rows)
    print("## A. 로그에서 직접 — 모델 없음")
    if not n:
        print("  표본 0건\n")
        return
    sessions = sorted({r.get("session", "?") for r in rows})
    print(f"  표본 {n}건  ({rows[0]['ts']} ~ {rows[-1]['ts']}, session={'/'.join(sessions)})")
    h = hist(r["peak"] for r in rows)
    over = sum(c for k, c in h.items() if k > MD_LIMIT)
    print(f"  옛 계상 md_peak_1s 분포: {show_hist(h, n, mark_over=MD_LIMIT)}")
    print(f"  * = 새 계상에서 **구조적으로 불가능**한 값 (송신은 1.15초에 {MD_LIMIT}건이 상계)")
    print(f"  한도 {MD_LIMIT} 초과 표본: {over}/{n} = {100.0 * over / n:.1f}% "
          "→ 새 계상에서는 전부 10 이하로 내려온다 (이 진술에는 모델이 없다)")
    avg = [r["avg"] for r in rows]
    print(f"  같은 표본의 지속률 avg: 중앙 {sorted(avg)[n // 2]:.2f} / 최대 {max(avg):.2f} req/s "
          f"(목표 8.50) — 지속률은 건수/창 이라 이번 수정과 **무관하게 그대로**다")
    print()


# --------------------------------------------------------------------------- #
# B. 시뮬레이션 (프로덕션 코드 그대로, 시각 출처만 다름)
# --------------------------------------------------------------------------- #
def offered_calls(seconds: float, tier3_cap: int, tier2_cap: int,
                  trades_s: float, book_s: float, sweep_s: float,
                  t2book_s: float, batches: int) -> list[float]:
    """MARKET_DATA 호출이 **제출되는** 시각들 (리미터 대기 전).

    tier3 루프는 만기 항목을 연달아 쏘므로 한 주기에 `tier3_cap` 건이 같은 순간에
    제출된다 (`tools/replay_gating.py` 와 같은 관측). tier1 은 배치 간격을 벌린다.
    """
    out: list[float] = []
    t = 0.0
    while t < seconds:                                   # tier3 체결
        out.extend([t] * tier3_cap)
        t += trades_s
    t = 0.5
    while t < seconds:                                   # tier3 호가 (위상 어긋남)
        out.extend([t] * tier3_cap)
        t += book_s
    t = 0.0
    spacing = min(1.0, (sweep_s * 0.5) / batches)        # tier1 스윕 (간격 벌림)
    while t < seconds:
        out.extend(t + i * spacing for i in range(batches))
        t += sweep_s
    if tier2_cap > 0 and t2book_s > 0:                   # tier2 저빈도 호가
        step = t2book_s / max(tier2_cap, 1)
        t = 0.0
        while t < seconds:
            out.append(t)
            t += step
    return sorted(x for x in out if x < seconds)


def limiter_send_times(offered: list[float], *, cap: int = MD_LIMIT,
                       rate: float | None = None) -> list[float]:
    """제출 시각열을 **실제 리미터**(`acquire` 의 세 관문)에 통과시켜 송신 시각을 얻는다."""
    b = _Bucket(rate=(cap * USAGE_RATIO) if rate is None else rate, window_cap=cap)
    b.last, b.tokens = 0.0, 0.0
    out: list[float] = []
    t = 0.0
    for want in offered:
        t = max(t, want)
        while True:
            wait = b.window_wait(t)
            if wait > 0.0:
                t += wait + 1e-6            # sleep 은 일찍 깨지 않는다
                continue
            b.refill(t)
            if b.tokens >= 1.0:
                b.tokens -= 1.0
                b.note_sent(t)
                out.append(t)
                break
            t += max((1.0 - b.tokens) / b.effective_rate(), 1e-6)
    return out


def completion_times(sends: list[float], *, stall_s: float, stall_every_s: float,
                     seed: int) -> list[float]:
    """완료 시각 = 송신 + RTT, 그리고 이벤트루프 stall 동안의 완료는 stall 끝으로 밀린다.

    ⚠️ **stall 은 관측이 아니라 모형이다.** 실측은 RTT 뿐이다 (docs/06).
    """
    rng = random.Random(seed)
    out: list[float] = []
    for t in sends:
        # 삼각분포: 실측 최소/중앙/최대를 그대로 쓴다.
        done = t + rng.triangular(RTT_MIN_S, RTT_MAX_S, RTT_P50_S)
        if stall_s > 0.0 and stall_every_s > 0.0:
            slot = int(done // stall_every_s)
            stall_start = slot * stall_every_s
            if done < stall_start + stall_s:      # stall 구간에 걸렸다 → 끝으로 밀린다
                done = stall_start + stall_s
        out.append(done)
    return sorted(out)


class _SimClient:
    """`TossClient._send` 의 계상 seam. `with_times=False` 면 **옛 경로**로 내려간다."""

    def __init__(self, *, with_times: bool) -> None:
        self.counters = {"requests": 0, "http_429": 0, "retries": 0}
        self.sent_by_group: dict[str, int] = {}
        self.sent_at: list[float] = []
        self.mono = 0.0
        self.last_headers: dict[str, str] = {}
        self.last_status = None
        self.last_429 = None
        if not with_times:
            self.recent_send_ages = None      # 송신 시각을 못 주는 client

    def send(self, group: str, at: float) -> None:
        self.mono = at
        self.counters["requests"] += 1
        self.sent_by_group[group] = self.sent_by_group.get(group, 0) + 1
        self.sent_at.append(at)

    def recent_send_ages(self, group: str, n: int) -> list[float]:   # noqa: F811
        take = self.sent_at[-n:]
        return [max(self.mono - t, 0.0) for t in take]

    async def get_us_calendar(self, date=None):
        from tests.test_collector_helpers import calendar_dict, simple_day
        return calendar_dict([simple_day("2026-07-30", DAY0)], 0)


DAY0 = 1753833600000


def _ctx(tmp: Path, client):
    from tests.test_collector_helpers import (FrozenClock, calendar_dict,
                                              make_config, simple_day)
    cfg = make_config(tmp)
    day = simple_day("2026-07-30", DAY0)
    clock = FrozenClock(day.regular.start_ms + 60_000)
    ctx = CollectorContext.create(client, Store(cfg.store.db_path), cfg,
                                  notifier=Notifier(console=False), clock=clock,
                                  symbols=())
    ctx.scheduler.calendar = calendar_dict([day], 0)
    ctx.scheduler.fetched_ms = clock.now_ms()
    ctx.session = "regular"
    return ctx


class _ServerCorrectedClock:
    """`Clock.observe_headers` 가 만드는 **비단조 시각**을 그대로 모형화한다.

    이것은 모형이 아니라 **코드에서 읽은 것**이다 (`scheduler.py:170-184`):

      * `Date` 헤더는 **초 해상도**라 표본마다 참값이 [t, t+1) 에 균일 분포한다.
        `_DATE_HEADER_BIAS_MS = 500` 이 **중앙값**은 잡지만 **분산은 못 잡는다** —
        표본 오차는 여전히 [-500, +500] ms 다.
      * 오프셋은 최근 9 표본의 **중앙값**이고, `after_call` 마다 갱신된다 (초당 ~6회).
        표본이 굴러가면서 중앙값이 앞뒤로 움직인다.
      * 그래서 `clock.now_ms()` 는 **뒤로 갈 수 있다.** 예산의 사건 시각이 이 시계
        위에 찍히므로, 뒤로 간 만큼 나중 사건이 앞선 사건보다 앞에 놓인다.

    송신 간격이 1/8.5 = 118ms 인데 시계 지터가 그보다 크면 **순서가 섞이고 압축된다.**
    """

    def __init__(self, enabled: bool, max_samples: int = 9) -> None:
        self.enabled = enabled
        self.max_samples = max_samples
        self._samples: list[float] = []
        self.offset_ms = 0.0

    def observe(self, t: float) -> None:
        """완료 시각 `t` 에서 `Date` 헤더 1건을 관측한다 (초 절삭 + 500ms 보정)."""
        if not self.enabled:
            return
        import math
        sample = math.floor(t) * 1000.0 + 500.0 - t * 1000.0
        self._samples.append(sample)
        if len(self._samples) > self.max_samples:
            self._samples.pop(0)
        ordered = sorted(self._samples)
        self.offset_ms = ordered[len(ordered) // 2]

    def now_ms(self, t: float) -> float:
        return t * 1000.0 + self.offset_ms


def replay(sends: list[float], dones: list[float], *, with_times: bool,
           sample_every_s: float, clock_jitter: bool = False) -> list[int]:
    """송신열·완료열을 **프로덕션 계상 코드**에 흘리고 `peak_1s` 를 표본한다."""
    # Windows 는 열린 sqlite 핸들이 있는 디렉터리를 못 지운다 — 정리 실패가 결과를
    # 가리지 않게 무시한다 (임시 디렉터리라 남아도 무해하다).
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        tmp = Path(td)
        client = _SimClient(with_times=with_times)
        ctx = _ctx(tmp, client)
        base_ms = ctx.clock.now_ms()
        wall = _ServerCorrectedClock(clock_jitter)
        peaks: list[int] = []
        events = ([(t, 0) for t in sends] + [(t, 1) for t in dones]
                  + [(t, 2) for t in _ticks(max(dones), sample_every_s)])
        for at, kind in sorted(events):
            client.mono = at                  # 송신 시각은 monotonic — 지터가 없다
            if kind == 1:
                wall.observe(at)              # after_call 이 `Date` 헤더를 관측한다
            ctx.clock._now = base_ms + int(wall.now_ms(at))
            if kind == 0:
                client.send(GROUP_MARKET_DATA, at)
            elif kind == 1:
                ctx.after_call(GROUP_MARKET_DATA)
            else:
                peaks.append((int(ctx.budget.peak_1s(GROUP_MARKET_DATA)),
                              ctx.budget.p95_1s(GROUP_MARKET_DATA),
                              ctx.budget.measured_rate(GROUP_MARKET_DATA)))
        booked = int(ctx.budget.counters.get(GROUP_MARKET_DATA, 0))
        assert booked == len(sends), f"계상 {booked} != 송신 {len(sends)}"
        return peaks


def _ticks(end: float, step: float) -> list[float]:
    out, t = [], 60.0            # 관측 창(60초)이 찬 뒤부터 표본한다
    while t <= end:
        out.append(t)
        t += step
    return out


def table_b(args) -> None:
    offered = offered_calls(args.minutes * 60.0, args.tier3_cap, args.tier2_cap,
                            args.trades_s, args.book_s, args.sweep_s,
                            args.t2book_s, args.batches)
    sends = limiter_send_times(offered)
    true_peak = _peak(sends)
    span = sends[-1] - sends[0]
    print("## B. 시뮬레이션 — 같은 송신열, 프로덕션 계상 코드, 시각 출처만 다름")
    print(f"  제출 {len(offered):,}건 → 송신 {len(sends):,}건 / {span / 60:.1f}분 "
          f"= {len(sends) / span:.2f} req/s   (tier3_cap={args.tier3_cap}, "
          f"tier2_cap={args.tier2_cap}, tr{args.trades_s:g}s/ob{args.book_s:g}s)")
    print(f"  **진짜 송신 첨두 (1초 슬라이딩) = {true_peak}**  — 리미터가 낸 값이고 "
          "계상과 무관하다\n")
    print(f"{'시계':>10} {'stall':>6} | {'옛 계상 (완료시각)':>30} | "
          f"{'새 계상 (송신시각)':>30}")
    print(f"{'':>10} {'(s)':>6} | {'peak중앙':>5}{'최대':>5}{'>10':>10}"
          f"{'p95':>7}{'avg':>7} | {'peak중앙':>5}{'최대':>5}{'>10':>10}"
          f"{'p95':>7}{'avg':>7}")
    for jitter in (False, True):
        label = "지터 있음" if jitter else "이상적"
        for stall in args.stalls:
            dones = completion_times(sends, stall_s=stall,
                                     stall_every_s=args.stall_every_s, seed=args.seed)
            old = replay(sends, dones, with_times=False, clock_jitter=jitter,
                         sample_every_s=args.sample_every_s)
            new = replay(sends, dones, with_times=True, clock_jitter=jitter,
                         sample_every_s=args.sample_every_s)
            print(f"{label:>10} {stall:>6.2f} | {_stat(old)} | {_stat(new)}")
    print()
    print("  '시계' 열: **예산이 사건을 찍는 시계**다. `Date` 헤더는 초 해상도라")
    print("  오프셋(9표본 중앙값)이 앞뒤로 흔들리고, 그 폭이 송신 간격(118ms)보다 크면")
    print("  나중 송신이 앞선 송신보다 앞에 놓여 **압축**된다 (scheduler.py:170-184).")
    print("  읽는 법: **새 계상 열은 stall 에 불변**이다 — 완료가 언제 오든 송신 시각은")
    print("  같기 때문이다. 옛 계상만 stall 을 따라 오른다. 즉 옛 첨두가 재던 것은")
    print("  '초당 몇 건 보냈나' 가 아니라 '초당 몇 건 **완료됐나**' 였다.")


def _peak(times: list[float]) -> int:
    peak = left = 0
    for right in range(len(times)):
        while times[right] - times[left] >= 1.0:
            left += 1
        peak = max(peak, right - left + 1)
    return peak


def _stat(samples: list[tuple[int, float, float]]) -> str:
    n = len(samples)
    peaks = sorted(v[0] for v in samples)
    p95 = sorted(v[1] for v in samples)
    avg = sorted(v[2] for v in samples)
    over = sum(1 for v in peaks if v > MD_LIMIT)
    return (f"{peaks[n // 2]:>5}{peaks[-1]:>5}{f'{over}/{n}':>10}"
            f"{p95[n // 2]:>7.1f}{avg[n // 2]:>7.2f}")


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--log", type=Path, default=Path("../w5-ops/data/collector.log"))
    p.add_argument("--since", default="2026-08-10 22:30")
    p.add_argument("--until", default="2026-08-11 05:00")
    p.add_argument("--minutes", type=float, default=30.0)
    p.add_argument("--tier3-cap", type=int, default=10)
    p.add_argument("--tier2-cap", type=int, default=210)
    p.add_argument("--trades-s", type=float, default=3.0)
    p.add_argument("--book-s", type=float, default=4.0)
    p.add_argument("--sweep-s", type=float, default=45.0)
    p.add_argument("--t2book-s", type=float, default=600.0)
    p.add_argument("--batches", type=int, default=8)
    p.add_argument("--stalls", type=float, nargs="+",
                   default=[0.0, 0.05, 0.10, 0.20, 0.40])
    p.add_argument("--stall-every-s", type=float, default=1.0)
    p.add_argument("--sample-every-s", type=float, default=10.0)
    p.add_argument("--seed", type=int, default=20260811)
    p.add_argument("--skip-log", action="store_true")
    args = p.parse_args()

    print("# 완료 시각 계상 vs 송신 시각 계상 (라이브 0, 재시작 0)\n")
    if not args.skip_log:
        if args.log.exists():
            table_a(parse_log(args.log, args.since, args.until))
        else:
            print(f"## A. 건너뜀 — 로그 없음: {args.log}\n")
    table_b(args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
