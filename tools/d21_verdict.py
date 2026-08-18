"""D-21 커버리지 판정 러너 — `coordination/D21-COVERAGE-PREREG.md` 를 집행한다.

이 도구는 **판정하지 않는다.** 사전등록 문서에 이미 얼려 놓은 정의(§1)·기준선(§2)·
밴드(§3)를 기계적으로 적용해서, 관측값이 어느 밴드에 떨어지는지만 찍는다. 밴드 경계는
전부 문서를 그대로 미러링한 상수이고 각 줄에 절 번호를 병기한다.

**개정 1 (D-23, 사용자 결정 2026-08-15) 반영** — 문서 §6:
- 무효 규칙이 §3-4.1(*"60 초 넘는 공백 1 건"*)에서 §6-2(**미관측 총량** `head + tail +
  Σmax(0, gap−60)` 이 창의 0.5% 초과)로 대체됐다
- 밴드를 뽑는 기준선 집합이 10 세션 -> **유효 6 세션**으로 줄었다 (§6-6).
  주 판정 아래 경계가 17.1 -> **19.0%**. **통과선 25.9% 는 안 움직였다** (§6-6a)
- 08-14 창은 **앞으로만 적용**(§6-4) 원칙에 따라 봉인한다. 새 규칙에서 0.198% 로 유효
  범위지만 **재판정하지 않는다** (§6-6c)

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
- **세션 하나다.** 기준선은 **유효 6 세션**(개정 1 로 10 -> 6)인데 관측은 1 세션이다
  (§4-1). 교환가능성 논거가 1/11 ≈ 0.09 에서 **1/7 ≈ 0.14** 로 약해졌다 (§6-6b)
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
- `3` 대상 창 데이터 부재 -> 판정 없음 (§3-4.4: 부분 데이터로 표를 만들지 않는다)
- `4` 무효 조건 -> 판정 없음, 밴드 없음. §6-2(미관측 총량)·§3-4.2(`config_sig`)·
      §6-4(봉인된 세션) 중 하나라도 걸린 경우다
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
DEFAULT_SESSION = "2026-08-15"

RT = "TOSS_SECURITIES_TRADING_VOLUME"
KST_OFFSET_H = 9              # 로그 시각은 로컬 벽시계 KST(UTC+9)다
WIN_START_H, WIN_START_M = 13, 30    # 정규장 = 13:30~20:00 UTC (§1)
WIN_END_H = 20

GAP_LIMIT_S = 60              # §6-2 — **정상 간격의 상한**. 폴 주기가 rk12s 라 60 초까지의
                              # 간격은 정상으로 보고 `interior_s` 에서 면제한다.
                              # **개정 1 에서 역할이 바뀌었다**: 이전에는 §3-4.1 의 *무효
                              # 문턱*이었다(60 초 초과 공백이 1 건이라도 있으면 무효). 이제
                              # 이 상수는 아무것도 판정하지 않는다 — 판정은 §6-2 의 미관측
                              # 총량이 하고, 여기서는 면제분의 크기와 진단용 공백 목록의
                              # 컷으로만 쓴다. **앞머리·꼬리에는 이 면제를 주지 않는다**
                              # (§6-2 설계 근거 3)
WINDOW_S = 23400.0            # §6-2 — 창 길이 (13:30~20:00 UTC = 6.5 시간)
UNOBS_FRAC_LIMIT = 0.005      # §6-2 — 미관측 총량이 창의 0.5%(117.0 초)를 넘으면 무효.
                              # 사용자가 0.5 / 1 / 2% 중에서 골랐다
COOLDOWN_S = 600              # 배포된 구성이 cd600s (§1d)
PCT_TOL = 0.05                # 자가검사 허용 오차: 백분율 +-0.05pp, 건수는 정확히

# --------------------------------------------------------------------------- #
# 사전등록 §6-4 — 개정은 **앞으로만** 적용된다. 아래 세션은 옛 규칙으로 이미 판정이 났고
# 새 자로 다시 재면 *"결과를 보고 자를 바꿔 다시 재기"* 가 된다 (사용자 결정).
# 러너가 숫자는 찍되 판정을 내지 않도록 여기서 막는다
# --------------------------------------------------------------------------- #
SEALED_SESSIONS = {
    "2026-08-14": "already judged INVALID under 3-4.1 (the pre-revision rule). 6-4 keeps it "
                  "INVALID and forbids re-judging it with the revised ruler.",
}

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
BASELINE_DAYS = tuple(r[0] for r in BASELINE_TOP10)

# --------------------------------------------------------------------------- #
# 사전등록 §6-6 — 개정 1 ②, 기준선을 새 규칙으로 재판정한 결과.
# **§2 와 출처가 다르므로 한 자료구조로 섞지 않는다**: §2 는 데이터가 존재하기 전에 얼렸고,
# 이 표는 규칙 커밋(`1ac0b61`, 2026-08-15 19:53:05 KST) **뒤** 19:54 에 계산됐다.
# 그 순서가 개정의 정당성이다 (§6-3). (session, unobserved%, valid)
# --------------------------------------------------------------------------- #
BASELINE_UNOBS = (
    ("2026-07-31", 2.262, False),
    ("2026-08-03", 0.419, True),
    ("2026-08-04", 0.637, False),
    ("2026-08-05", 16.903, False),     # tail 3,929.5s — 창이 닫히기 65 분 전에 수집이 끝났다
    ("2026-08-06", 0.069, True),
    ("2026-08-07", 0.876, False),
    ("2026-08-10", 0.064, True),       # 통과선 25.9% 를 만든 세션. 새 규칙에서도 유효하다
    ("2026-08-11", 0.026, True),
    ("2026-08-12", 0.030, True),
    ("2026-08-13", 0.055, True),
)

# top-100 은 사전등록에 요약만 있다 -> 요약만 대조한다.
# **개정 1**: 밴드를 뽑는 집합이 §6-6 의 유효 6 세션으로 줄었다. 아래가 현행 정본이다
BASELINE_SUMMARY = {10: (6, 8.6, 19.0, 25.9), 100: (6, 4.5, 6.0, 8.0)}
# 개정 전 (§2 요약, 10 세션 전체). **버리지 않는다** — 무엇이 어떻게 바뀌었는지가 사라진다.
# 10 행이 전부 맞으면 이 요약은 그 값들의 순수 함수라 여기서 어긋나면 min/median/max 쪽
# 버그라는 뜻이다. 그래서 계속 검사한다 (밴드는 여기서 뽑지 않는다)
BASELINE_SUMMARY_PREREV = {10: (10, 3.3, 17.1, 25.9), 100: (10, 2.6, 4.7, 8.0)}

# --------------------------------------------------------------------------- #
# 사전등록 §3 밴드 경계 — 문서를 그대로 미러링한 상수. 여기서만 끌어온다
# --------------------------------------------------------------------------- #
B_MAX, B_MED = 25.9, 19.0              # §6-6a 주 판정 (top-10, B). **개정 1 로 갱신**.
                                       # 통과선 25.9 는 한 톨도 안 움직였다 — 그것을 만든
                                       # 08-10 이 새 규칙에서도 유효하기 때문이다. 움직인
                                       # 것은 아래 경계 하나이고 더 엄격해진 방향이다
B_MED_PREREV = 17.1                    # 개정 전 아래 경계 (n=10). 병기용, 판정에 안 쓴다
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
    """`collector.log` + 회전본(`collector.<stamp>.log.gz`, `collector.log.N`)을 시간순으로.

    **회전 방식이 둘이고, 둘 다 봐야 한다.** `ops/rotate_logs.py` 는 스탬프 + `.gz` 를
    만들지만 Windows 에서 수집기가 파일을 잡고 있어 `rename` 이 거의 항상
    `PermissionError` 로 죽는다 (성공한 단 한 번은 프로세스가 죽어 있던 2026-08-04).
    **실제로 일어나는 회전**은 수집기 자신의 `RotatingFileHandler`
    (`tossmon/collector/notifier.py`, 32 MiB · backupCount=3) 이고 그 산출물은
    `collector.log.1` · `.2` · `.3` 이다. 번호 백업을 빼면 창이 회전 경계를 넘었을 때
    조용히 `telemetry=0` 이 되어 유효한 창이 무효로 나온다 (2026-08-17 창이 그랬다).

    `.gz` 는 회전 시각의 **로컬** 스탬프(`%Y%m%d-%H%M%S`)를 이름에 달고 그 시각까지의
    줄만 담으므로, stamp < 창 시작이면 창 밖이라 건너뛴다. **번호 백업은 이름에 시각이
    없다** — 건너뛰기를 흉내내지 않고 그냥 읽는다 (최대 3 개, 비용 무시 가능).
    mtime 으로 거르면 회전 뒤 mtime 이 바뀌는 환경에서 또 조용히 틀린다.

    두 경우 다 이름을 강제해야 `collector.stdout.log` · `collector.stdout.<stamp>.log.gz`
    같은 **다른 논리 파일**이 섞이지 않는다 (같은 디렉터리에 실제로 있다).

    순서: 오래된 것부터. `.gz`(스탬프 오름차순) -> 번호 백업(`.3`,`.2`,`.1`) -> 살아 있는
    로그. `RotatingFileHandler` 는 회전마다 번호를 밀어올리므로 **`.1` 이 `.2` 보다 새 것**
    이고, 번호 백업은 살아 있는 로그 바로 앞 구간이다.
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

    # 번호 백업 `collector.log.N` — `log.name` 전체를 강제해 `collector.stdout.log.N` 배제.
    num = re.compile(r"^" + re.escape(log.name) + r"\.(\d+)$")
    backups: list[tuple[int, Path]] = []
    for p in log.parent.glob(log.name + ".*"):
        m = num.match(p.name)
        if m:
            backups.append((int(m.group(1)), p))
    out += [p for _, p in sorted(backups, reverse=True)]   # .3 -> .2 -> .1 = 오래된 것부터

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
    """연속 차가 limit_s 를 **초과**하는 자리. (앞, 뒤, 초) 목록.

    **개정 1 이후로 이 함수는 판정하지 않는다** (§6-2). 공백 목록은 여전히 쓸모 있으므로
    진단 출력에 남긴다 — 어디서 얼마나 놓쳤는지는 총량만으로는 안 보인다.
    """
    limit_ms = limit_s * 1000
    return [(sorted_ms[i - 1], sorted_ms[i], (sorted_ms[i] - sorted_ms[i - 1]) / 1000.0)
            for i in range(1, len(sorted_ms))
            if sorted_ms[i] - sorted_ms[i - 1] > limit_ms]


def unobserved_breakdown(sorted_ms: list[int], lo: int, hi: int,
                         limit_s: int) -> tuple[float, float, float]:
    """§6-2 미관측 시간 분해 -> `(head_s, tail_s, interior_s)`.

    ```
    head_s     = (S[0]  - lo) / 1000
    tail_s     = (hi - S[-1]) / 1000
    interior_s = sum over consecutive pairs of  max(0, (S[i+1]-S[i])/1000 - limit_s)
    ```

    **앞머리·꼬리는 전액 센다** — `limit_s` 면제를 거기 주지 않는다. §6-2 설계 근거 3:
    사용자가 0.5% 문턱을 고를 때 본 숫자가 전액 계산 기준(08-14 = 0.20%)이었고, 뒤에 식을
    바꾸면 사용자가 판단한 근거와 다른 자가 된다. (면제를 주는 변형이면 08-14 는 0.144%
    이고 판정은 둘 다 같다 — 그래도 안 쓴다.)

    스냅이 하나도 없으면 `S[0]` 이 없다. 그때는 **창 전체가 미관측**이다 — 0 을 돌려주면
    *"완전히 관측했다"* 는 정반대 뜻이 되므로 head 에 창 길이를 싣는다.
    """
    if not sorted_ms:
        return (hi - lo) / 1000.0, 0.0, 0.0
    head_s = (sorted_ms[0] - lo) / 1000.0
    tail_s = (hi - sorted_ms[-1]) / 1000.0
    interior_s = sum(max(0.0, (sorted_ms[i] - sorted_ms[i - 1]) / 1000.0 - limit_s)
                     for i in range(1, len(sorted_ms)))
    return head_s, tail_s, interior_s


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

    짝수 n 이면 두 가운데 값 중 **위쪽**이다. 이 정의로 §2 의 17.1% / 4.7% (n=10) 와
    §6-6 의 19.0% / 6.0% (n=6) 가 얼려 있으므로 통계적 중앙값(두 값의 평균)으로 바꾸면
    사전등록 요약을 재현하지 못한다.
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
    """§3-1 주 판정 (top-10, 테이프 커버리지 B). **경계는 §6-6a 가 갱신했다.**"""
    if pct > B_MAX:
        return ("INCREASED (above baseline max %.1f%%) -- carry the 3-4 limits with it" % B_MAX,
                "6-6a")
    if pct >= B_MED:
        return ("VERDICT WITHHELD (inside baseline spread %.1f-%.1f%%) -- do NOT write "
                "'increased'" % (B_MED, B_MAX), "6-6a")
    return ("NOT INCREASED (below baseline median %.1f%%) -- itself demands an explanation "
            "(3-3)" % B_MED, "6-6a")


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
    """창 안 distinct `snap_ms`. `ranking_type=None` 이면 §6-2 가 승계한 전 타입 합집합."""
    if ranking_type is None:
        q, args = ("select distinct snap_ms from rankings_snap "
                   "where snap_ms between ? and ? order by snap_ms", (lo, hi))
    else:
        q, args = ("select distinct snap_ms from rankings_snap where ranking_type=? "
                   "and snap_ms between ? and ? order by snap_ms", (ranking_type, lo, hi))
    return [r[0] for r in cur.execute(q, args).fetchall()]


def session_unobserved(cur, day: str) -> tuple[float, float, float, float, float]:
    """한 세션의 §6-2 값 -> `(head_s, tail_s, interior_s, unobserved_s, unobserved_frac)`.

    자가검사(§6-6 재판정 대조)와 대상 창이 **같은 함수**를 쓰게 해서 정의가 어긋나지
    않게 한다 — §6-6 의 계산도 이 러너의 함수를 그대로 import 해서 돌렸다.
    """
    lo, hi = window_ms(day)
    head_s, tail_s, interior_s = unobserved_breakdown(
        snap_ms_list(cur, lo, hi), lo, hi, GAP_LIMIT_S)
    unobs_s = head_s + tail_s + interior_s
    return head_s, tail_s, interior_s, unobs_s, unobs_s / WINDOW_S


# --------------------------------------------------------------------------- #
# [0] 기준선 자가검사 — 사전등록된 기준선을 재현 못 하는 자는 자가 아니다
# --------------------------------------------------------------------------- #
def _summary_line(label: str, vals: list[float], expected: tuple, note: str) -> bool:
    """요약 한 줄을 찍고 얼린 값과 맞는지 돌려준다. `vals` 가 비면 무조건 불일치다."""
    e_n, e_min, e_med, e_max = expected
    if vals:
        got = (len(vals), min(vals), prereg_median(vals), max(vals))
        ok = (got[0] == e_n and abs(got[1] - e_min) <= PCT_TOL
              and abs(got[2] - e_med) <= PCT_TOL and abs(got[3] - e_max) <= PCT_TOL)
    else:
        got, ok = (0, 0.0, 0.0, 0.0), False
    print("%-28s n=%-2d min=%.1f%% med=%.1f%% max=%.1f%%   expected n=%d/%.1f/%.1f/%.1f   %-8s %s"
          % (label, got[0], got[1], got[2], got[3], e_n, e_min, e_med, e_max,
             "OK" if ok else "MISMATCH", note))
    return ok


def run_self_check(cur) -> tuple[bool, list[float], list[float]]:
    """§2 기준선 + §6-6 재판정을 재계산해 얼린 값과 대조.

    반환하는 백분율 목록은 **유효 세션만**이다 — 밴드를 뽑는 집합이 §6-6 에서 유효 6 세션
    으로 바뀌었기 때문이다. 표는 10 행을 전부 찍는다: 그건 *"DB 가 안 변했는가"* 를 보는
    데이터 무결성 검사이고 유효/무효와 무관하다.

    유효/무효는 **얼린 §6-6 의 라벨**로 가른다. 재계산값은 그 라벨과 대조만 하고 덮어쓰지
    않는다 — 어긋나면 그건 사전등록 값이 틀렸다는 뜻이고, 조용히 고칠 일이 아니라
    보고할 사건이다 (판정 거부 + 종료 코드 2).
    """
    print("--- [0] BASELINE SELF-CHECK vs PREREG 2 (frozen before the data existed) ---")
    print("    + PREREG 6-6 re-judgement (computed AFTER the rule commit 1ac0b61, 19:53:05)")
    print("    tolerance: cover%% / unobs%% +-%.2fpp, counts exact. bounds inclusive (BETWEEN),"
          % PCT_TOL)
    print("    median = sorted[n//2] (upper of the two middles) -- the definition that froze")
    print("    17.1%/4.7% at n=10 and 19.0%/6.0% at n=6.")
    print("    The 10-row table is a DATA-INTEGRITY check and is independent of valid/invalid;")
    print("    only the SUMMARY is drawn from the valid subset (6-6). That is why 10 rows are")
    print("    printed but 6 feed the bands -- the valid column below shows which.")
    print("")
    print("%-12s %7s %7s %8s %9s %9s %8s   %-24s %s"
          % ("session", "ranked", "w/tape", "cover%", "capfill3", "unobs%", "valid",
             "expected cov/cap/unobs", "check"))
    frozen_unobs = {d: (u, v) for d, u, v in BASELINE_UNOBS}
    bad: list[str] = []
    pct10: list[float] = []
    pct100: list[float] = []
    valid10: list[float] = []
    valid100: list[float] = []
    for day, e_ranked, e_tape, e_pct, e_cap in BASELINE_TOP10:
        lo, hi = window_ms(day)
        cap = capacity_fill_count(cur, lo, hi)
        syms = population(cur, 10, lo, hi)
        tape = tape_covered(cur, syms, lo, hi)
        pct = 100.0 * tape / len(syms) if syms else 0.0
        pct10.append(pct)

        syms100 = population(cur, 100, lo, hi)
        tape100 = tape_covered(cur, syms100, lo, hi)
        pct100_v = 100.0 * tape100 / len(syms100) if syms100 else 0.0
        pct100.append(pct100_v)

        head, tail, inter, _unobs_s, frac = session_unobserved(cur, day)
        unobs_pct = 100.0 * frac
        got_valid = frac <= UNOBS_FRAC_LIMIT

        why = []
        if len(syms) != e_ranked:
            why.append("ranked %d!=%d" % (len(syms), e_ranked))
        if tape != e_tape:
            why.append("w/tape %d!=%d" % (tape, e_tape))
        if abs(pct - e_pct) > PCT_TOL:
            why.append("cover%% %.2f vs %.1f" % (pct, e_pct))
        if cap != e_cap:
            why.append("capfill3 %d!=%d" % (cap, e_cap))
        e_unobs, e_valid = frozen_unobs.get(day, (None, None))
        if e_unobs is None:
            why.append("no 6-6 row for this session")
        else:
            if abs(unobs_pct - e_unobs) > PCT_TOL:
                why.append("unobs%% %.3f vs %.3f (head %.1f tail %.1f interior %.1f)"
                           % (unobs_pct, e_unobs, head, tail, inter))
            if got_valid != e_valid:
                why.append("valid %s!=%s" % (got_valid, e_valid))
            if e_valid:                       # 집합은 **얼린 라벨**로 고른다
                valid10.append(pct)
                valid100.append(pct100_v)
        print("%-12s %7d %7d %7.1f%% %9d %8.3f%% %8s   %-24s %s"
              % (day, len(syms), tape, pct, cap, unobs_pct,
                 "VALID" if got_valid else "INVALID",
                 "%.1f%%/%d/%s" % (e_pct, e_cap,
                                   "?" if e_unobs is None else "%.3f%%" % e_unobs),
                 "OK" if not why else "MISMATCH: " + "; ".join(why)))
        if why:
            bad.append(day)

    print("")
    print("valid subset (6-6): %d of %d sessions. invalid ones are excluded from the summary"
          % (len(valid10), len(BASELINE_TOP10)))
    print("and therefore from the bands -- that is the whole point of revision 1.")
    for topn, vals, allvals in ((10, valid10, pct10), (100, valid100, pct100)):
        if not _summary_line("summary top-%d VALID-only" % topn, vals,
                             BASELINE_SUMMARY[topn], "<- bands come from here (6-6)"):
            bad.append("summary top-%d" % topn)
        if not _summary_line("   pre-revision all-session", allvals,
                             BASELINE_SUMMARY_PREREV[topn],
                             "[superseded by 6-6a; kept as a check on min/median/max]"):
            bad.append("pre-revision summary top-%d" % topn)

    ok = not bad
    print("")
    print("SELF-CHECK: %s%s" % ("PASS" if ok else "FAIL",
                                "" if ok else "  (offending: " + ", ".join(bad) + ")"))
    if not ok:
        print("  A runner that cannot reproduce the preregistered baseline is not a ruler.")
        print("  No verdict will be emitted. Fix the runner or explain the DB change first.")
        print("  If the 6-6 columns are what disagree, do NOT edit the constants: 6-6 is the")
        print("  authority and a disagreement is itself the finding (W3 spec 3).")
    print("")
    return ok, valid10, valid100


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
    ap.add_argument("--log", default=DEFAULT_LOG, help="collector.log (rotated siblings -- "
                                                       ".gz stamps and .1/.2/.3 -- are merged in)")
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
        ok, pct10_valid, _ = run_self_check(cur)
        if not ok:
            print("RESULT: NO VERDICT (baseline self-check failed). exit 2")
            return 2
        # 밴드를 뽑는 집합은 §6-6 의 유효 6 세션이다 (개정 1). 정확값은 그 집합에서 낸다
        baseline_max_exact = max(pct10_valid)
        baseline_med_exact = prereg_median(pct10_valid)

        if args.session in BASELINE_DAYS:
            print("*** NOTE: %s is a PREREG 2 BASELINE session. Everything below is a DRY RUN"
                  % args.session)
            print("*** of the target path on real rows -- it is NOT a verdict on D-21.")
            print("")

        # ---- [1] 대상 창 데이터 존재 + §6-2 미관측 시간 ----
        print("--- [1] TARGET WINDOW: DATA PRESENCE (3-4.4) + UNOBSERVED TIME (6-2) ---")
        snaps = snap_ms_list(cur, lo, hi)
        print("rankings_snap distinct snap_ms in window : %d  (all ranking_type)" % len(snaps))
        if not snaps:
            print("first/last snap : (none)")
            print("")
            print("The target window has NO rows. 3-4.4 says do not build a table from")
            print("partial data, so no coverage table and no bands are emitted.")
            print("(The %s window opens at %s KST; before that this is expected.)"
                  % (args.session, kst_str(lo)[11:16]))
            print("")
            print("RESULT: NO VERDICT (target window data absent). exit 3")
            print("elapsed: %.1fs" % (time.time() - t0))
            return 3

        head_s, tail_s, interior_s = unobserved_breakdown(snaps, lo, hi, GAP_LIMIT_S)
        unobs_s = head_s + tail_s + interior_s
        unobs_frac = unobs_s / WINDOW_S
        unobs_bad = unobs_frac > UNOBS_FRAC_LIMIT
        print("first snap : %sZ   (window_lo + %.1fs)" % (utc_str(snaps[0]), head_s))
        print("last  snap : %sZ   (window_hi - %.1fs)" % (utc_str(snaps[-1]), tail_s))
        print("")
        print("6-2 unobserved-time accounting -- every term is hand-checkable:")
        print("  window_s        = %10.1f   13:30-20:00 UTC computed; frozen 6-2 value %.1f"
              % ((hi - lo) / 1000.0, WINDOW_S))
        print("  head_s          = %10.1f   = (S[0] - lo)/1000        counted in FULL"
              % head_s)
        print("  tail_s          = %10.1f   = (hi - S[-1])/1000       counted in FULL"
              % tail_s)
        print("  interior_s      = %10.1f   = sum of max(0, gap_s - %d) over %d pairs"
              % (interior_s, GAP_LIMIT_S, max(0, len(snaps) - 1)))
        print("  unobserved_s    = %10.1f   = head + tail + interior" % unobs_s)
        print("  unobserved_frac = %10.6f   = %.1f / %.1f  ->  %.3f%%"
              % (unobs_frac, unobs_s, WINDOW_S, 100.0 * unobs_frac))
        print("  rule (6-2)      : INVALID iff unobserved_frac > %.3f  (%.1f%% = %.1fs)"
              % (UNOBS_FRAC_LIMIT, 100.0 * UNOBS_FRAC_LIMIT, UNOBS_FRAC_LIMIT * WINDOW_S))
        print("=> %s" % ("OVER BUDGET (6-2 INVALID)" if unobs_bad else "WITHIN BUDGET"))
        print("   head/tail get NO %ds exemption (6-2 rationale 3): the user chose 0.5%%"
              % GAP_LIMIT_S)
        print("   against numbers computed that way, and changing the formula afterwards")
        print("   would make it a different ruler than the one the choice was made on.")
        print("")

        # 3-4.4 가장자리 규칙과의 겹침 — W3 결정 (명세 2-1). 근거를 출력에 남긴다
        edge_over = head_s > GAP_LIMIT_S or tail_s > GAP_LIMIT_S
        print("W3 DECISION on the 3-4.4 edge rule (spec 2-1): FOLDED INTO 6-2, KEPT AS A LABEL")
        print("  Before revision 1 this runner invalidated when head or tail exceeded %ds."
              % GAP_LIMIT_S)
        print("  That test was DERIVED, not frozen: 3-4.4 fixes no number, and the runner")
        print("  borrowed 3-4.1's to close the hole where 3-4.1 saw only interior gaps.")
        print("  6-2 now closes that same hole directly and by name (6-1 cites the 08-05")
        print("  tail of 3,929.5s). Keeping both would stack 'and no single edge > %ds' on"
              % GAP_LIMIT_S)
        print("  top of the 0.5% the user picked -- a stricter rule nobody wrote: a 100s")
        print("  late start is 0.427%, inside budget, yet would still be invalidated.")
        print("  6-5 keeps 3-4.4 unchanged, and 3-4.4 as written is a consequence clause")
        print("  ('do not build a table from partial data'), which this runner still obeys:")
        print("  an INVALID window emits no coverage table and no band. See [2](4).")
        print("  label only, decides nothing: head/tail vs the %ds normal interval -> %s"
              % (GAP_LIMIT_S, "AN EDGE EXCEEDS IT" if edge_over else "both within it"))
        print("")

        # ---- [2] 무효 조건 (§3-4, §3-4.1 은 §6-2 로 대체) ----
        print("--- [2] INVALIDATION CONDITIONS (3-4, with 3-4.1 replaced by 6-2) ---")
        sealed = SEALED_SESSIONS.get(args.session)
        print("(0) 6-4 forward-only application : %-26s %s"
              % ("SEALED SESSION" if sealed else "not a sealed session",
                 "TRIGGERED" if sealed else "PASS"))
        if sealed:
            print("      %s %s" % (args.session, sealed))
            print("      6-6c records that the revised rule scores this window inside the")
            print("      valid range. That line exists to CHECK THE RULE'S EFFECT, not to")
            print("      reverse the verdict. Numbers are printed above; NO verdict follows.")

        print("(1) unobserved time (6-2) : %.3f%% of window vs %.1f%% limit   %s"
              % (100.0 * unobs_frac, 100.0 * UNOBS_FRAC_LIMIT,
                 "PASS" if not unobs_bad else "TRIGGERED"))
        gaps = find_gaps(snaps, GAP_LIMIT_S)
        print("      gap list (DIAGNOSTIC, decides nothing): %d gap(s) over %ds"
              % (len(gaps), GAP_LIMIT_S))
        for a, b, secs in sorted(gaps, key=lambda g: -g[2])[:5]:
            print("        %sZ -> %sZ   %.1fs   charged %.1fs"
                  % (utc_str(a), utc_str(b), secs, secs - GAP_LIMIT_S))
        print("      the struck 3-4.1 invalidated on ANY single gap here; 6-2 charges each")
        print("      max(0, gap_s - %d) and judges the total instead. 6-1: a single-gap"
              % GAP_LIMIT_S)
        print("      ruler catches 61s in a 6.5h window but misses a 65-minute early stop.")
        tv = snap_ms_list(cur, lo, hi, RT)
        tv_gaps = find_gaps(tv, GAP_LIMIT_S)
        print("      diagnostic (does NOT decide): %s snaps=%d, gaps>%ds=%d."
              % (RT, len(tv), GAP_LIMIT_S, len(tv_gaps)))
        print("      6-2 inherits 3-4.1's population (all ranking_type together), which can")
        print("      mask a gap in the one type the population is drawn from. Above is that")
        print("      check.")

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

        print("(4) not partial data : CONSEQUENCE CLAUSE, not an independent test")
        print("      3-4.4 reads 'if any of the above trigger, do not build a table from")
        print("      partial data'. It freezes no threshold of its own. This runner honours")
        print("      it by emitting no coverage table and no band whenever (0)/(1)/(2) fire,")
        print("      and by exiting 3 when the window has no rows at all. The 60s edge test")
        print("      it used before revision 1 is folded into (1) -- rationale in [1].")
        triggered = []
        if sealed:
            triggered.append("6-4 sealed session (kept INVALID, not re-judged)")
        if unobs_bad:
            triggered.append("6-2 unobserved %.3f%% > %.1f%%"
                             % (100.0 * unobs_frac, 100.0 * UNOBS_FRAC_LIMIT))
        if sig_bad:
            triggered.append("3-4.2 config_sig distinct=%d" % len(sigs))
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
            print("RESULT: NO VERDICT (INVALID). exit 4")
            print("elapsed: %.1fs" % (time.time() - t0))
            return 4

        # ---- [4] 커버리지 (§1) + 밴딩 (§3, 경계는 §6-6a) ----
        print("--- [4] COVERAGE (PREREG 1) + BANDING (PREREG 3, edges from 6-6a) ---")
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
        print("[6-6a] band edges come from the VALID-6 baseline. The pass line %.1f%% did not"
              % B_MAX)
        print("       move in revision 1; only the lower edge did, %.1f%% -> %.1f%%, i.e. it"
              % (B_MED_PREREV, B_MED))
        print("       got STRICTER. Do not quote the pre-revision %.1f%% (6-5)." % B_MED_PREREV)
        # 얼린 상수는 1 자리로 반올림된 값이라 정확한 기준선 통계와 미세하게 다르다.
        # 그 틈에 관측이 떨어지면 밴드는 문서 그대로 적용하되 틈을 숨기지 않는다.
        # 정확값은 **유효 6 세션**에서 낸다 (개정 1).
        #
        # **개정 1 에서 틈의 방향이 뒤집혔다.** 개정 전 중앙은 얼린 17.1 < 정확 17.1428
        # 이었는데 새 중앙은 얼린 19.0 > 정확 18.9655 (= 11/58, 08-13) 다. 방향을 하나로
        # 가정한 옛 비교는 이제 영영 발화하지 않으므로 **양방향**으로 바꾼다. 판정 구조는
        # 그대로다 — "얼린 자와 정확한 자가 서로 다른 밴드를 주는 구간이면 운영자에게
        # 넘긴다". 경계의 열림/닫힘은 밴드 정의를 따라간다 (`> max`, `>= median`).
        for what, frozen, exact, in_gap in (
                ("max", B_MAX, baseline_max_exact,
                 min(B_MAX, baseline_max_exact) < b10 <= max(B_MAX, baseline_max_exact)),
                ("median", B_MED, baseline_med_exact,
                 min(B_MED, baseline_med_exact) <= b10 < max(B_MED, baseline_med_exact))):
            if not in_gap:
                continue
            print("[6-6a] BOUNDARY at the baseline %s: %.4f%% falls between the frozen"
                  % (what, b10))
            print("      constant %.1f%% and the exact recomputed value %.4f%% (the frozen one"
                  % (frozen, exact))
            print("      is the %s of the two). The two rulers give different bands here; the"
                  % ("higher" if frozen > exact else "lower"))
            print("      frozen constant was applied as written above. OPERATOR MUST DECIDE.")
        print("")
        print("Limits that travel with any number above (PREREG 4):")
        print("  4-1 one session against a 6-session baseline (revision 1 dropped 4 of the 10).")
        print("      Do not write 'confirmed'. 6-6b: the exchangeability argument weakens from")
        print("      1/11 ~ 0.09 to 1/7 ~ 0.14. Do NOT cite either as a CI.")
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
