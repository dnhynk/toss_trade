"""D-21 커버리지 판정 러너 — `coordination/D21-COVERAGE-PREREG.md` 를 집행한다.

이 도구는 **판정하지 않는다.** 사전등록 문서에 이미 얼려 놓은 정의(§1)·기준선(§2)·
밴드(§3)를 기계적으로 적용해서, 관측값이 어느 밴드에 떨어지는지만 찍는다. 밴드 경계는
전부 문서를 그대로 미러링한 상수이고 각 줄에 절 번호를 병기한다.

무엇을 재는가
------------
- **B (테이프 커버리지)** = 그 정규장에 `TOSS_SECURITIES_TRADING_VOLUME` 상위 N 위에
  한 번이라도 든 종목 중, **같은 창 안에** `trades_snap` 행을 1 건 이상 남긴 비율 (§1)
- **A (좌석 커버리지)** = 같은 모집단 중 그 창에 `promotions.reason='ranking_tier3'` 를
  받은 비율 (§1). **A 와 B 는 다른 값이고 어느 쪽도 다른 쪽을 포함하지 않는다** (§3-2)
- **비용** = 같은 창의 `promotions(reason='capacity_fill', to_tier=3)` 건수 (§1)
- N = **10**(주 판정)과 **100**(보조) 둘 다

무엇을 못 재는가
--------------
- **프로브를 안 쐈다는 것은 이 도구가 증명할 수 없다** (§3-4.3). 기계로 확인 가능한
  증거만 찍고 판단은 운영자에게 넘긴다. 수집기 로그에는 프로브 흔적이 애초에 남지 않는다
  (`tools/live_probe.py` 는 별개 프로세스다) — 그래서 "창 안 0 건"은 정보가 아니다
- **세션 하나다.** 기준선은 10 세션인데 관측은 1 세션이다 (§4-1)
- `md_peak_1s`·`rank_peak_1s` 는 텔레메트리 줄에 5 분마다 남는 **집계값**이다. "실제로
  초당 몇 건을 보냈나"가 아니라 "수집기가 스스로 보고한 첨두의 분포"다

읽기 전용
--------
DB 는 `mode=ro` URI 로만 연다. 라이브 API 호출 0 건. 라이브 워크트리(`w5-ops`)에 쓰기 0 건.

콘솔 출력은 **ASCII 만** 쓴다. Windows cp949 콘솔에서 비 ASCII 는 `UnicodeEncodeError` 를
내고 이 레포에서 실제로 그것 때문에 프로세스가 죽은 적이 있다. 소스의 한국어는 주석뿐이다.

종료 코드
--------
- `0` 판정을 냈다 (밴드까지 적용)
- `2` 기준선 자가검사 불일치 -> 판정 없음. 사전등록된 기준선을 재현 못 하면 자가 아니다
- `3` 대상 창 데이터 부재/부분 -> 판정 없음 (§3-4.4: 부분 데이터로 표를 만들지 않는다)
- `4` §3-4 무효 조건 -> 판정 없음, 밴드 없음
"""
from __future__ import annotations

import argparse
import datetime as dt
import gzip
import re
import sqlite3
import time
from collections import Counter
from pathlib import Path

# --------------------------------------------------------------------------- #
# 경로 기본값 — 라이브 워크트리다. 읽기만 한다
# --------------------------------------------------------------------------- #
DEFAULT_DB = "C:/Users/dongh/orca/workspaces/toss_trade/w5-ops/data/tossmon.db"
DEFAULT_LOG = "C:/Users/dongh/orca/workspaces/toss_trade/w5-ops/data/collector.log"
DEFAULT_SESSION = "2026-08-14"

RT = "TOSS_SECURITIES_TRADING_VOLUME"
KST_OFFSET_H = 9              # 로그 시각은 로컬 벽시계 KST(UTC+9)다
WIN_START_H, WIN_START_M = 13, 30    # 정규장 = 13:30~20:00 UTC (§1)
WIN_END_H = 20

GAP_LIMIT_S = 60              # §3-4.1 — 60 초 초과 공백
COOLDOWN_S = 600              # 배포된 구성이 cd600s (§1d)
PCT_TOL = 0.05                # 자가검사 허용 오차: 백분율 +-0.05pp, 건수는 정확히

# --------------------------------------------------------------------------- #
# 사전등록 §2 — 얼린 기준선. **이 상수를 고치면 자가검사가 아니다**
# (session, ranked, w/tape, cover%, capacity_fill->3)
# --------------------------------------------------------------------------- #
BASELINE_TOP10 = (
    ("2026-07-31", 41, 3, 7.3, 0),
    ("2026-08-03", 45, 5, 11.1, 196),
    ("2026-08-04", 30, 1, 3.3, 246),
    ("2026-08-05", 35, 8, 22.9, 155),
    ("2026-08-06", 35, 3, 8.6, 199),
    ("2026-08-07", 47, 8, 17.0, 172),
    ("2026-08-10", 54, 14, 25.9, 194),
    ("2026-08-11", 70, 12, 17.1, 245),
    ("2026-08-12", 52, 11, 21.2, 231),
    ("2026-08-13", 58, 11, 19.0, 202),
)
# top-100 은 사전등록에 요약만 있다 -> 요약만 대조한다
BASELINE_SUMMARY = {10: (10, 3.3, 17.1, 25.9), 100: (10, 2.6, 4.7, 8.0)}
BASELINE_DAYS = tuple(r[0] for r in BASELINE_TOP10)

# --------------------------------------------------------------------------- #
# 사전등록 §3 밴드 경계 — 문서를 그대로 미러링한 상수. 여기서만 끌어온다
# --------------------------------------------------------------------------- #
B_MAX, B_MED = 25.9, 17.1              # §3-1 주 판정 (top-10, B)
A_SIM_HI, A_SIM_LO, A_WRONG = 65.0, 45.0, 25.0   # §3-2 시뮬 대조 (top-10, A)
COST_LO, COST_HI = 155, 246            # §3-3 비용 (07-31 의 0 은 제외한 범위)

# `docs/61` §3-1 시뮬레이션 예측 (좌석 커버리지 A) = 55.1%.
# **인용하는 자리에는 반드시 `[미재현]` 을 병기한다** (COORDINATOR-STATE §1-2b, 사용자 결정).
# 콘솔은 ASCII 만 쓸 수 있으므로(§1f) 출력에는 아래 ASCII 표기를 쓴다 — 같은 표시다.
SIM_PRED_A_PCT = 55.1
SIM_PRED_TAG = "[UNREPRODUCED]"        # = [미재현]

# `docs/62` §5-3 의 `RANKING` 초당 천장. 프로브 여유 논증("한 초 최악 4/5, 여유 1")이
# 수집기 첨두 + 프로브 1 콜 <= 이 값에 달려 있다. D-21 이후에도 유효한지가 §5 안건이다.
RANKING_CEILING_1S = 5

PREREG_PATH = "coordination/D21-COVERAGE-PREREG.md"

_TS = re.compile(r"^(\d{4})-(\d{2})-(\d{2}) (\d{2}):(\d{2}):(\d{2})")
_SIG = re.compile(r"config_sig=(\S+)")
_PROBE = re.compile(r"probe|saturation", re.I)


# --------------------------------------------------------------------------- #
# 창 산술
# --------------------------------------------------------------------------- #
def window_ms(day: str) -> tuple[int, int]:
    """정규장 창을 epoch ms 로. 13:30~20:00 UTC 는 UTC 날짜 안에 온전히 들어간다 (§1)."""
    d = dt.datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=dt.timezone.utc)
    lo = int((d + dt.timedelta(hours=WIN_START_H, minutes=WIN_START_M)).timestamp() * 1000)
    hi = int((d + dt.timedelta(hours=WIN_END_H)).timestamp() * 1000)
    return lo, hi


def kst_to_utc_ms(naive_kst: dt.datetime) -> int:
    """로그의 naive 로컬 시각(KST=UTC+9)을 epoch ms 로. KST 는 DST 가 없다."""
    return int((naive_kst - dt.timedelta(hours=KST_OFFSET_H))
               .replace(tzinfo=dt.timezone.utc).timestamp() * 1000)


def utc_str(ms: int) -> str:
    return dt.datetime.fromtimestamp(ms / 1000, dt.timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def kst_str(ms: int) -> str:
    return (dt.datetime.fromtimestamp(ms / 1000, dt.timezone.utc)
            + dt.timedelta(hours=KST_OFFSET_H)).strftime("%Y-%m-%d %H:%M:%S")


def parse_log_ts_ms(line: str) -> int | None:
    """로그 줄 앞머리의 로컬 KST 시각을 epoch ms 로. 형식이 아니면 None."""
    m = _TS.match(line)
    if not m:
        return None
    y, mo, d, h, mi, s = (int(x) for x in m.groups())
    try:
        return kst_to_utc_ms(dt.datetime(y, mo, d, h, mi, s))
    except ValueError:
        return None


def _ascii(s: str) -> str:
    """콘솔 출력 안전장치 (§1f). cp949 에서 죽지 않도록 비 ASCII 를 '?' 로."""
    return s.encode("ascii", "replace").decode("ascii")


# --------------------------------------------------------------------------- #
# 로그 — 창이 로컬 자정(00:10 KST logrotate)을 지나가므로 회전본도 함께 읽는다
# --------------------------------------------------------------------------- #
def log_sources(log: Path, lo_ms: int) -> list[Path]:
    """`collector.log` + 그 회전본 `collector.<stamp>.log.gz` 를 시간순으로.

    `ops/rotate_logs.py` 는 회전 시각의 **로컬** 스탬프(`%Y%m%d-%H%M%S`)를 붙이고, 그 파일은
    그 시각까지의 줄만 담는다. 그러므로 stamp < 창 시작이면 창 밖이라 건너뛴다.
    스탬프 형식을 강제해야 `collector.stdout.<stamp>.log.gz` 같은 **다른 논리 파일**이
    섞이지 않는다 (같은 디렉터리에 실제로 있다).
    """
    pat = re.compile(r"^" + re.escape(log.stem) + r"\.(\d{8}-\d{6})"
                     + re.escape(log.suffix) + r"\.gz$")
    rotated: list[tuple[str, Path]] = []
    for p in log.parent.glob(log.stem + ".*" + log.suffix + ".gz"):
        m = pat.match(p.name)
        if not m:
            continue
        stamp = m.group(1)
        try:
            end_ms = kst_to_utc_ms(dt.datetime.strptime(stamp, "%Y%m%d-%H%M%S"))
        except ValueError:
            rotated.append((stamp, p))     # 못 읽으면 버리지 않고 읽는다
            continue
        if end_ms < lo_ms:
            continue
        rotated.append((stamp, p))
    out = [p for _, p in sorted(rotated)]
    if log.exists():
        out.append(log)
    return out


def _open_log(p: Path):
    if p.suffix == ".gz":
        return gzip.open(p, "rt", encoding="utf-8", errors="replace")
    return p.open("r", encoding="utf-8", errors="replace")


def scan_log(log: Path, lo_ms: int, hi_ms: int) -> dict:
    """창 안 텔레메트리에서 `config_sig`·첨두를 모으고, 프로브 흔적을 센다. 한 번만 훑는다."""
    out = {
        "files": [], "lines": 0, "telemetry": 0,
        "sigs": Counter(), "md": [], "rank": [],
        "probe_in_window": [], "probe_anywhere": 0,
    }
    for p in log_sources(log, lo_ms):
        n = 0
        try:
            with _open_log(p) as fh:
                for line in fh:
                    n += 1
                    if _PROBE.search(line):
                        out["probe_anywhere"] += 1
                        ts0 = parse_log_ts_ms(line)
                        if ts0 is not None and lo_ms <= ts0 <= hi_ms \
                                and len(out["probe_in_window"]) < 10:
                            out["probe_in_window"].append(_ascii(line.rstrip()[:160]))
                    if " telemetry " not in line:
                        continue
                    ts = parse_log_ts_ms(line)
                    if ts is None or not (lo_ms <= ts <= hi_ms):
                        continue
                    out["telemetry"] += 1
                    m = _SIG.search(line)
                    if m:
                        out["sigs"][m.group(1)] += 1
                    for key, bucket in (("md_peak_1s", "md"), ("rank_peak_1s", "rank")):
                        mm = re.search(r"\b" + key + r"=(\d+)", line)
                        if mm:
                            out[bucket].append(int(mm.group(1)))
        except OSError as exc:
            out["files"].append((str(p), -1, str(exc)))
            continue
        out["lines"] += n
        out["files"].append((str(p), n, ""))
    return out


# --------------------------------------------------------------------------- #
# 순수 로직 — 공백·쿨다운·분포
# --------------------------------------------------------------------------- #
def find_gaps(sorted_ms: list[int], limit_s: int) -> list[tuple[int, int, float]]:
    """연속 차가 limit_s 를 **초과**하는 자리. (앞, 뒤, 초) 목록."""
    limit_ms = limit_s * 1000
    return [(sorted_ms[i - 1], sorted_ms[i], (sorted_ms[i] - sorted_ms[i - 1]) / 1000.0)
            for i in range(1, len(sorted_ms))
            if sorted_ms[i] - sorted_ms[i - 1] > limit_ms]


def cooldown_violations(rows: list[tuple[str, int]],
                        cooldown_s: int) -> list[tuple[str, int, int, float]]:
    """같은 종목이 cooldown_s **미만** 간격으로 두 번 받은 인접 쌍. (종목, 앞, 뒤, 초)."""
    limit_ms = cooldown_s * 1000
    prev: dict[str, int] = {}
    out = []
    for sym, ts in sorted(rows, key=lambda r: (r[0], r[1])):
        if sym in prev and ts - prev[sym] < limit_ms:
            out.append((sym, prev[sym], ts, (ts - prev[sym]) / 1000.0))
        prev[sym] = ts
    return out


def prereg_median(values: list[float]) -> float:
    """사전등록 §2 요약이 쓴 중앙값 정의 — `tools/d21_coverage.py` 와 같은 `sorted[n//2]`.

    n=10 이면 두 가운데 값 중 **위쪽**이다. 이 정의로 17.1% / 4.7% 가 얼려 있으므로
    통계적 중앙값(두 값의 평균)으로 바꾸면 사전등록 요약을 재현하지 못한다.
    """
    vs = sorted(values)
    return vs[len(vs) // 2]


def p95_nearest_rank(values: list[int]) -> int:
    """최근접 순위 p95: 오름차순 정렬 후 ceil(0.95*n)-1 번째."""
    vs = sorted(values)
    idx = -(-95 * len(vs) // 100) - 1
    return vs[max(0, min(len(vs) - 1, idx))]


def histogram(values: list[int]) -> str:
    return " ".join("%d:%d" % kv for kv in sorted(Counter(values).items()))


# --------------------------------------------------------------------------- #
# 사전등록 §3 밴드 — 판단이 아니라 얼린 규칙의 적용이다
# --------------------------------------------------------------------------- #
def band_tape_b(pct: float) -> tuple[str, str]:
    """§3-1 주 판정 (top-10, 테이프 커버리지 B)."""
    if pct > B_MAX:
        return ("INCREASED (above baseline max %.1f%%) -- carry the 3-4 limits with it" % B_MAX,
                "3-1")
    if pct >= B_MED:
        return ("VERDICT WITHHELD (inside baseline spread %.1f-%.1f%%) -- do NOT write "
                "'increased'" % (B_MED, B_MAX), "3-1")
    return ("NOT INCREASED (below baseline median %.1f%%) -- itself demands an explanation "
            "(3-3)" % B_MED, "3-1")


def band_seat_a(pct: float) -> tuple[str, str]:
    """§3-2 시뮬레이션 대조 (top-10, 좌석 커버리지 A)."""
    if A_SIM_LO <= pct <= A_SIM_HI:
        return ("SIM MATCHED LIVE (%.0f-%.0f%%) -- grounds to trust that tool"
                % (A_SIM_LO, A_SIM_HI), "3-2")
    if A_WRONG <= pct < A_SIM_LO:
        return ("HALF MATCHED (%.0f-%.0f%%) -- must find the sim/live gap (cap? cooldown? "
                "tier0 refusal?)" % (A_WRONG, A_SIM_LO), "3-2")
    if pct < A_WRONG:
        return ("SIM WAS WRONG (<%.0f%%) -- next agenda is what ranking_promotion_sim.py "
                "missed" % A_WRONG, "3-2")
    return ("NO BAND -- 3-2 defines none above %.0f%%; operator must decide" % A_SIM_HI, "3-2")


def band_cost(n: int) -> tuple[str, str]:
    """§3-3 비용 (`capacity_fill`->3 건수)."""
    if n < COST_LO:
        return ("CLEARLY DECREASED (below baseline min %d) -- read with 3-1: was the cost "
                "traded for coverage?" % COST_LO, "3-3")
    if n <= COST_HI:
        return ("INSIDE BASELINE RANGE (%d-%d) -- may mean the 2 seats used idle room; "
                "then confirm tier3 was not full" % (COST_LO, COST_HI), "3-3")
    return ("INCREASED (above baseline max %d) -- unexpected; do not pass over without an "
            "explanation" % COST_HI, "3-3")


# --------------------------------------------------------------------------- #
# DB — 사전등록 §1 정의 그대로. 질의 모양은 기준선을 낸 러너와 같게 유지한다
# --------------------------------------------------------------------------- #
def population(cur, topn: int, lo: int, hi: int) -> list[str]:
    return [r[0] for r in cur.execute(
        "select distinct symbol from rankings_snap "
        "where ranking_type=? and rank<=? and snap_ms between ? and ?",
        (RT, topn, lo, hi)).fetchall()]


def tape_covered(cur, syms: list[str], lo: int, hi: int) -> int:
    if not syms:
        return 0
    ph = ",".join("?" * len(syms))
    return cur.execute(
        "select count(distinct symbol) from trades_snap "
        "where symbol in (%s) and ts_ms between ? and ?" % ph,
        syms + [lo, hi]).fetchone()[0]


def seat_covered(cur, syms: list[str], lo: int, hi: int) -> int:
    """§1 좌석 커버리지의 분자. `reason='ranking_tier3'` 만 본다 (to_tier 조건 없음)."""
    if not syms:
        return 0
    ph = ",".join("?" * len(syms))
    return cur.execute(
        "select count(distinct symbol) from promotions "
        "where reason='ranking_tier3' and symbol in (%s) and ts_ms between ? and ?" % ph,
        syms + [lo, hi]).fetchone()[0]


def capacity_fill_count(cur, lo: int, hi: int) -> int:
    return cur.execute(
        "select count(*) from promotions where reason='capacity_fill' "
        "and to_tier=3 and ts_ms between ? and ?", (lo, hi)).fetchone()[0]


def reason_count(cur, reason: str, lo: int, hi: int) -> int:
    return cur.execute(
        "select count(*) from promotions where reason=? and ts_ms between ? and ?",
        (reason, lo, hi)).fetchone()[0]


def reason_rows(cur, reason: str, lo: int, hi: int) -> list[tuple[str, int]]:
    return cur.execute(
        "select symbol, ts_ms from promotions where reason=? and ts_ms between ? and ? "
        "order by symbol, ts_ms", (reason, lo, hi)).fetchall()


def snap_ms_list(cur, lo: int, hi: int, ranking_type: str | None = None) -> list[int]:
    """창 안 distinct `snap_ms`. `ranking_type=None` 이면 §3-4.1 이 말하는 전 타입 합집합."""
    if ranking_type is None:
        q, args = ("select distinct snap_ms from rankings_snap "
                   "where snap_ms between ? and ? order by snap_ms", (lo, hi))
    else:
        q, args = ("select distinct snap_ms from rankings_snap where ranking_type=? "
                   "and snap_ms between ? and ? order by snap_ms", (ranking_type, lo, hi))
    return [r[0] for r in cur.execute(q, args).fetchall()]


# --------------------------------------------------------------------------- #
# [0] 기준선 자가검사 — 사전등록된 기준선을 재현 못 하는 자는 자가 아니다
# --------------------------------------------------------------------------- #
def run_self_check(cur) -> tuple[bool, list[float], list[float]]:
    """§2 기준선 재계산 후 얼린 값과 대조. (통과 여부, top-10 %, top-100 %)."""
    print("--- [0] BASELINE SELF-CHECK vs PREREG 2 (frozen before the data existed) ---")
    print("    tolerance: cover%% +-%.2fpp, counts exact. window bounds inclusive (BETWEEN),"
          % PCT_TOL)
    print("    median = sorted[n//2] (upper of the two middles at n=10) -- the definition")
    print("    that produced the frozen 17.1% / 4.7%.")
    print("")
    print("%-12s %7s %7s %8s %10s   %-22s %s"
          % ("session", "ranked", "w/tape", "cover%", "capfill3", "expected", "check"))
    bad: list[str] = []
    pct10: list[float] = []
    pct100: list[float] = []
    for day, e_ranked, e_tape, e_pct, e_cap in BASELINE_TOP10:
        lo, hi = window_ms(day)
        cap = capacity_fill_count(cur, lo, hi)
        syms = population(cur, 10, lo, hi)
        tape = tape_covered(cur, syms, lo, hi)
        pct = 100.0 * tape / len(syms) if syms else 0.0
        pct10.append(pct)

        syms100 = population(cur, 100, lo, hi)
        tape100 = tape_covered(cur, syms100, lo, hi)
        pct100.append(100.0 * tape100 / len(syms100) if syms100 else 0.0)

        why = []
        if len(syms) != e_ranked:
            why.append("ranked %d!=%d" % (len(syms), e_ranked))
        if tape != e_tape:
            why.append("w/tape %d!=%d" % (tape, e_tape))
        if abs(pct - e_pct) > PCT_TOL:
            why.append("cover%% %.2f vs %.1f" % (pct, e_pct))
        if cap != e_cap:
            why.append("capfill3 %d!=%d" % (cap, e_cap))
        print("%-12s %7d %7d %7.1f%% %10d   %-22s %s"
              % (day, len(syms), tape, pct, cap, "%.1f%% / %d" % (e_pct, e_cap),
                 "OK" if not why else "MISMATCH: " + "; ".join(why)))
        if why:
            bad.append(day)

    for topn, vals in ((10, pct10), (100, pct100)):
        e_n, e_min, e_med, e_max = BASELINE_SUMMARY[topn]
        got = (len(vals), min(vals), prereg_median(vals), max(vals))
        ok = (got[0] == e_n and abs(got[1] - e_min) <= PCT_TOL
              and abs(got[2] - e_med) <= PCT_TOL and abs(got[3] - e_max) <= PCT_TOL)
        print("summary top-%-3d n=%d min=%.1f%% med=%.1f%% max=%.1f%%   "
              "expected n=%d/%.1f/%.1f/%.1f   %s"
              % (topn, got[0], got[1], got[2], got[3], e_n, e_min, e_med, e_max,
                 "OK" if ok else "MISMATCH"))
        if not ok:
            bad.append("summary top-%d" % topn)

    ok = not bad
    print("")
    print("SELF-CHECK: %s%s" % ("PASS" if ok else "FAIL",
                                "" if ok else "  (offending: " + ", ".join(bad) + ")"))
    if not ok:
        print("  A runner that cannot reproduce the preregistered baseline is not a ruler.")
        print("  No verdict will be emitted. Fix the runner or explain the DB change first.")
    print("")
    return ok, pct10, pct100


# --------------------------------------------------------------------------- #
# 본체
# --------------------------------------------------------------------------- #
def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="D-21 coverage verdict runner -- executes coordination/"
                    "D21-COVERAGE-PREREG.md (read-only)")
    ap.add_argument("--session", default=DEFAULT_SESSION,
                    help="target session YYYY-MM-DD (UTC date; default %s)" % DEFAULT_SESSION)
    ap.add_argument("--db", default=DEFAULT_DB, help="sqlite db (opened mode=ro)")
    ap.add_argument("--log", default=DEFAULT_LOG, help="collector.log (rotated .gz siblings "
                                                       "are merged in)")
    args = ap.parse_args(argv)

    t0 = time.time()
    lo, hi = window_ms(args.session)
    db_path, log_path = Path(args.db), Path(args.log)

    print("=== d21_verdict: D-21 coverage verdict runner (read-only) ===")
    print("prereg   : %s   (rules frozen; this runner only applies them)" % PREREG_PATH)
    print("session  : %s" % args.session)
    print("window   : %sZ .. %sZ  UTC   (= %s .. %s KST, crosses local midnight)"
          % (utc_str(lo), utc_str(hi), kst_str(lo), kst_str(hi)))
    print("db       : %s   (mode=ro)" % db_path)
    print("log      : %s" % log_path)
    print("")

    con = sqlite3.connect("file:%s?mode=ro" % db_path.as_posix(), uri=True)
    cur = con.cursor()
    try:
        ok, pct10, _ = run_self_check(cur)
        if not ok:
            print("RESULT: NO VERDICT (baseline self-check failed). exit 2")
            return 2
        baseline_max_exact = max(pct10)

        if args.session in BASELINE_DAYS:
            print("*** NOTE: %s is a PREREG 2 BASELINE session. Everything below is a DRY RUN"
                  % args.session)
            print("*** of the target path on real rows -- it is NOT a verdict on D-21.")
            print("")

        # ---- [1] 대상 창 데이터 존재 (§3-4.4) ----
        print("--- [1] TARGET WINDOW: DATA PRESENCE (3-4.4) ---")
        snaps = snap_ms_list(cur, lo, hi)
        print("rankings_snap distinct snap_ms in window : %d  (all ranking_type)" % len(snaps))
        if not snaps:
            print("first/last snap : (none)")
            print("")
            print("The target window has NO rows. 3-4.4 says do not build a table from")
            print("partial data, so no coverage table and no bands are emitted.")
            print("(The 2026-08-14 window opens at 22:30 KST; before that this is expected.)")
            print("")
            print("RESULT: NO VERDICT (target window data absent). exit 3")
            print("elapsed: %.1fs" % (time.time() - t0))
            return 3

        head_s, tail_s = (snaps[0] - lo) / 1000.0, (hi - snaps[-1]) / 1000.0
        print("first snap : %sZ   (window_lo + %.1fs)" % (utc_str(snaps[0]), head_s))
        print("last  snap : %sZ   (window_hi - %.1fs)" % (utc_str(snaps[-1]), tail_s))
        partial = head_s > GAP_LIMIT_S or tail_s > GAP_LIMIT_S
        print("edge rule  : PARTIAL if either edge offset > %ds." % GAP_LIMIT_S)
        print("             3-4.4 freezes no number, so the 3-4.1 threshold (%ds) is applied"
              % GAP_LIMIT_S)
        print("             to the edges -- a session that starts late IS a collection gap.")
        print("=> %s" % ("PARTIAL DATA" if partial else "NOT PARTIAL"))
        print("")

        # ---- [2] 무효 조건 (§3-4) ----
        print("--- [2] INVALIDATION CONDITIONS (3-4) ---")
        gaps = find_gaps(snaps, GAP_LIMIT_S)
        print("(1) collection gaps > %ds : %d gap(s)                       %s"
              % (GAP_LIMIT_S, len(gaps), "PASS" if not gaps else "TRIGGERED"))
        for a, b, secs in sorted(gaps, key=lambda g: -g[2])[:5]:
            print("      %sZ -> %sZ   %.1fs" % (utc_str(a), utc_str(b), secs))
        tv = snap_ms_list(cur, lo, hi, RT)
        tv_gaps = find_gaps(tv, GAP_LIMIT_S)
        print("      diagnostic (does NOT decide): %s snaps=%d, gaps>%ds=%d."
              % (RT, len(tv), GAP_LIMIT_S, len(tv_gaps)))
        print("      3-4.1 counts all ranking_type together, which can mask a gap in the one")
        print("      type the population is drawn from. The line above is that check.")

        scan = scan_log(log_path, lo, hi)
        sigs = scan["sigs"]
        sig_bad = len(sigs) != 1
        print("(2) config_sig invariance : distinct=%d, telemetry lines=%d      %s"
              % (len(sigs), scan["telemetry"], "PASS" if not sig_bad else "TRIGGERED"))
        for sig, n in sigs.most_common():
            print("      n=%-5d %s" % (n, _ascii(sig)))
        if not sigs:
            print("      no config_sig in window -- cannot show the config was unchanged")
        print("      log files read: %s"
              % ", ".join("%s(%s lines)" % (Path(f).name, n) for f, n, _ in scan["files"]))
        for f, n, err in scan["files"]:
            if n == -1:
                print("      READ FAILED %s: %s" % (f, _ascii(err)))

        print("(3) no probe : EVIDENCE ONLY -- OPERATOR CONFIRMATION REQUIRED")
        art_dir = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "live"
        arts = []
        for p in sorted(art_dir.glob("live_*.json")):
            ms = int(p.stat().st_mtime * 1000)
            if lo <= ms <= hi:
                arts.append((p.name, utc_str(ms)))
        print("      probe artifacts (%s/live_*.json) with mtime in window : %d"
              % (art_dir.as_posix(), len(arts)))
        for name, when in arts[:10]:
            print("        %s  mtime %sZ" % (name, when))
        print("      log lines matching /probe|saturation/i in window : %d  "
              "(file-wide: %d)" % (len(scan["probe_in_window"]), scan["probe_anywhere"]))
        for ln in scan["probe_in_window"][:5]:
            print("        %s" % ln)
        if scan["probe_anywhere"] == 0:
            print("      NOTE: that pattern never appears anywhere in this log, so 0 in the")
            print("      window carries NO information -- the collector does not log probe")
            print("      activity (tools/live_probe.py is a separate process).")
        print("      NOTE: --out is operator-chosen and cannot be enumerated; a probe fired")
        print("      from another worktree writes to that worktree's fixture dir.")
        print("      => machine evidence above is all this runner can see. It CANNOT prove")
        print("      no probe was fired. OPERATOR MUST CONFIRM (3-4.3; docs/62 8-2 says")
        print("      tonight is a no-probe night).")

        print("(4) not partial data : see [1]                                %s"
              % ("PASS" if not partial else "TRIGGERED"))
        triggered = []
        if gaps:
            triggered.append("3-4.1 gaps>%ds (%d)" % (GAP_LIMIT_S, len(gaps)))
        if sig_bad:
            triggered.append("3-4.2 config_sig distinct=%d" % len(sigs))
        if partial:
            triggered.append("3-4.4 partial data")
        print("INVALIDATION: %s"
              % ("none triggered by machine checks (still subject to (3) operator confirmation)"
                 if not triggered else "TRIGGERED -- " + "; ".join(triggered)))
        print("")

        # ---- [3] 창 진단 (사전등록 §5) — 판정이 아니다 ----
        print("--- [3] WINDOW DIAGNOSTICS (PREREG 5) -- reported even when INVALID ---")
        for label, vals, ceiling in (("md_peak_1s", scan["md"], None),
                                     ("rank_peak_1s", scan["rank"], RANKING_CEILING_1S)):
            if not vals:
                print("%-13s n=0  (no telemetry samples in window)" % label)
                continue
            print("%-13s n=%-4d max=%-3d p95=%-3d   hist %s"
                  % (label, len(vals), max(vals), p95_nearest_rank(vals), histogram(vals)))
            if ceiling is not None:
                print("              docs/62 5-3 argues RANKING worst second = collector peak")
                print("              + 1.0 probe call <= %d ceiling. observed peak %d + 1 = %d."
                      % (ceiling, max(vals), max(vals) + 1))
        n_t3 = reason_count(cur, "ranking_tier3", lo, hi)
        n_exp = reason_count(cur, "ranking_hold_expired", lo, hi)
        viol = cooldown_violations(reason_rows(cur, "ranking_tier3", lo, hi), COOLDOWN_S)
        print("ranking_tier3        promotions in window : %d" % n_t3)
        print("ranking_hold_expired releases in window   : %d" % n_exp)
        print("cooldown violations (same symbol, ranking_tier3 twice < %ds) : %d"
              % (COOLDOWN_S, len(viol)))
        for sym, a, b, secs in viol[:20]:
            print("      %-8s %sZ -> %sZ   %.1fs" % (_ascii(sym), utc_str(a), utc_str(b), secs))
        print("")

        if triggered:
            print("INVALID")
            print("  3-4.4: no table is built from partial data, and no band is applied.")
            print("  Defer the primary verdict to the next regular session.")
            print("")
            print("RESULT: NO VERDICT (INVALID by 3-4). exit 4")
            print("elapsed: %.1fs" % (time.time() - t0))
            return 4

        # ---- [4] 커버리지 (§1) + 밴딩 (§3) ----
        print("--- [4] COVERAGE (PREREG 1) + BANDING (PREREG 3) ---")
        cost = capacity_fill_count(cur, lo, hi)
        result: dict[int, tuple[int, int, float, int, float]] = {}
        print("%-8s %8s %8s %9s %8s %9s" % ("topN", "ranked", "A seats", "A cover%",
                                            "B tape", "B cover%"))
        for topn in (10, 100):
            syms = population(cur, topn, lo, hi)
            a_n = seat_covered(cur, syms, lo, hi)
            b_n = tape_covered(cur, syms, lo, hi)
            a_pct = 100.0 * a_n / len(syms) if syms else 0.0
            b_pct = 100.0 * b_n / len(syms) if syms else 0.0
            result[topn] = (len(syms), a_n, a_pct, b_n, b_pct)
            print("top-%-4d %8d %8d %8.1f%% %8d %8.1f%%"
                  % (topn, len(syms), a_n, a_pct, b_n, b_pct))
        print("cost: capacity_fill->3 in window = %d" % cost)
        print("A and B are different values and neither contains the other (3-2). Both above.")
        print("")

        b10, a10 = result[10][4], result[10][2]
        for name, value, band in (
                ("B top-10 %.1f%%" % b10, b10, band_tape_b(b10)),
                ("A top-10 %.1f%%" % a10, a10, band_seat_a(a10)),
                ("cost %d" % cost, cost, band_cost(cost))):
            text, clause = band
            print("[%s] %-18s -> %s" % (clause, name, text))
        print("[3-2] sim prediction for A was %.1f%% %s -- docs/61 3-1, held as a "
              "pre-registered forecast" % (SIM_PRED_A_PCT, SIM_PRED_TAG))
        if B_MAX < b10 <= baseline_max_exact:
            print("[3-1] BOUNDARY: %.4f%% is above the frozen constant %.1f%% but NOT above the"
                  % (b10, B_MAX))
            print("      exact recomputed baseline max %.4f%%. The frozen constant was applied"
                  % baseline_max_exact)
            print("      as written. OPERATOR MUST DECIDE whether that is the intended reading.")
        print("")
        print("Limits that travel with any number above (PREREG 4):")
        print("  4-1 one session against a 10-session baseline. Do not write 'confirmed'.")
        print("  4-2 coverage rising does not mean alpha exists (docs/58 G-2/G-3 is separate).")
        print("  4-3 tonight is D-21's first regular session; the holiday tier3_cap=4 reading")
        print("      (docs/61 11-5, 50%) does not stand in for regular (tier3_cap=10, 20%).")
        print("  4-4 do not compare with the 1.78% of docs/59 -- event grid vs symbol set.")
        print("")
        print("RESULT: VERDICT EMITTED (bands applied above). exit 0")
        print("elapsed: %.1fs" % (time.time() - t0))
        return 0
    finally:
        con.close()


if __name__ == "__main__":
    raise SystemExit(main())
