"""마크다운 리포트 생성 — 소유: W3. evaluate 산출물을 사람이 읽을 보고서로.

DB 접근은 `Reader`(W2 소유)를 통해서만, **read-only** 로 한다 (계약 C-6).
렌더링(`render_report`)은 DataFrame 만 받는 순수 함수이므로 DB 없이 테스트할 수 있다.
"""
from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

from . import evaluate as E

#: 섹션별 (제목, 검증질문 원문, 해석 지침)
SECTION_META: dict[str, tuple[str, str, str]] = {
    "q1_volume_leadtime": (
        "Q1. 거래량 이상의 선행성",
        "거래량 이상은 가격 급등보다 평균 몇 분 선행하는가? 임계값별 정밀도/재현율은?",
        "리드타임 중앙값이 0 에 가깝고 검출률이 낮다면 '전조 탐지'보다 "
        "'시작 후 수 분 내 확인-진입'이 현실적이다 (La Morgia et al. 2020)."),
    "q2_ranking_lead_lag": (
        "Q2. 토스 랭킹 진입의 선행/후행",
        "토스 랭킹 진입은 가격 대비 선행인가 후행인가?",
        "verdict='lag' 이면 어텐션 피크는 매수 신호가 아니라 **청산 카운트다운**이다 "
        "(Barber et al. 2022). 랭킹은 과거 조회가 불가하므로 실시간 수집 기간에만 채워진다."),
    "q3_daymarket_persistence": (
        "Q3. 데이마켓 급등의 정규장 지속성",
        "데이마켓(한국 낮) 이상 급등이 정규장 개장 후 지속되는가, 소멸하는가?",
        "persist_rate 가 낮으면 데이마켓 급등은 유동성 공백 위 가격 = 신호 오염원이다. "
        "블루오션 체결취소 이력을 감안해 데이마켓은 신호로만 쓰고 체결을 전제하지 않는다."),
    "q4_dump_speed": (
        "Q4. 덤프 속도와 홀트",
        "급등 후 덤프의 속도 분포 — 피크에서 -20%까지 걸리는 시간, 홀트 개입 빈도.",
        "중앙값이 수 분 단위면 수동 손절이 불가능하다는 뜻이므로 사이징으로만 통제해야 한다. "
        "하방 LULD 홀트 중에는 어떤 주문도 체결되지 않는다."),
    "q5_expectancy": (
        "Q5. 비용 차감 후 기대수익",
        "전조 스코어 상위 신호의 기대 수익 분포 — 비용 차감 후 양수인가?",
        "mean_net > 0 이 아니면 전략은 성립하지 않는다. half_peak 정책은 "
        "**실행 가능성 상한**이며 달성 가정이 아니다."),
    "q6_time_of_day": (
        "Q6. 시간대 효과",
        "개장 15분 / 10:00 ET 전후 / 마감 전의 신호 성능 차이.",
        "미국 개장 15분(= 한국시간 23:30–23:45)은 LULD 밴드가 2배로 넓어지는 구간이다."),
    "base_rates": (
        "부록 A. 알려진 기저율 대조",
        "docs/02 §2.4 SmallCapLab 실측치와 우리 데이터의 차이.",
        "delta 가 크면 표본 정의(이벤트 임계값·유니버스)가 문헌과 다르다는 신호다 — "
        "먼저 표본을 의심하고 그 다음에 시장을 의심한다."),
}

LIMITATIONS = [
    "랭킹 스냅샷은 **과거 조회가 불가**하다 — Q2 는 실시간 수집 기간에만 답할 수 있다 (A2 §4).",
    "미국 호가는 최우선 1레벨만 제공된다 — 호가 깊이 기반 피처는 존재할 수 없다 (A2 §1).",
    "`/trades` 는 최대 50건이므로 체결 크기 분포는 **표본 통계**다 (A2 §2).",
    "1분봉은 체결이 없는 분에 봉을 주지 않는다 — 결측은 거래량 0 으로 해석하되, "
    "세션 전체 결측(수집 중단)은 베이스라인 평균에서 제외한다.",
    "`half_peak` 청산 정책은 실행 가능성 상한선이며 달성 가정이 아니다.",
    "이벤트 임계값(30분 +15% / 당일 +30% / RVOL 3)은 초기값이며 데이터로 보정할 대상이다.",
    "정의식·경계조건·편향의 전체 목록은 `docs/07_analysis_spec.md` 참조.",
]


def _fmt(v: object) -> str:
    if v is None:
        return "-"
    if isinstance(v, bool):
        return "yes" if v else "no"
    if isinstance(v, float):
        if v != v:                      # NaN
            return "-"
        if abs(v) >= 1e11:              # ts_ms 류는 정수로
            return f"{int(v)}"
        if abs(v) >= 1000:
            return f"{v:,.0f}"
        return f"{v:.4g}"
    return str(v)


def df_to_markdown(df: pd.DataFrame, max_cols: int = 14) -> str:
    """의존성 없이 DataFrame → 마크다운 표 (tabulate 미사용 — 계약 C-10 의존성 고정).

    컬럼이 많으면 앞쪽 `max_cols` 개만 싣되, `note` 는 **항상 유지한다** — 미가용 사유
    같은 결정적 정보가 폭이 넓다는 이유로 잘려나가면 리포트가 오해를 만든다.
    """
    if df is None or len(df) == 0:
        return "_(데이터 없음)_"
    keep = [c for c in df.columns if c != "gate_policy"]
    cols = keep[:max_cols]
    if "note" in keep and "note" not in cols and df["note"].astype(str).str.len().gt(0).any():
        cols = cols[:max_cols - 1] + ["note"]
    lines = ["| " + " | ".join(cols) + " |",
             "|" + "|".join("---" for _ in cols) + "|"]
    for _i, row in df.iterrows():
        lines.append("| " + " | ".join(_fmt(row[c]) for c in cols) + " |")
    return "\n".join(lines)


def render_report(sections: dict[str, pd.DataFrame], *,
                  t_from_ms: int, t_to_ms: int,
                  events: pd.DataFrame | None = None,
                  title: str = "Phase 1 전조 감시 분석 리포트",
                  extra_notes: list[str] | None = None) -> str:
    """`evaluate.run_all()` 결과를 마크다운 문서로. DB 없이 테스트 가능한 순수 함수."""
    span_days = (t_to_ms - t_from_ms) / 86_400_000
    out: list[str] = [f"# {title}", ""]
    out.append(f"- 분석 구간: `{t_from_ms}` ~ `{t_to_ms}` (UTC epoch ms) — "
               f"약 {span_days:.1f}일")
    out.append("- 시간 표기는 전부 UTC epoch ms (계약 C-1).")

    n_ev = 0 if events is None else len(events)
    out.append(f"- 이벤트 수: **{n_ev}**")
    if events is not None and len(events):
        if "rvol_gated" in events.columns:
            ungated = int((~events["rvol_gated"].fillna(False).astype(bool)).sum())
            out.append(f"- RVOL 게이트 미적용 이벤트: **{ungated}** "
                       "(계약 A1 §6 — 기본 집계에서 제외)")
        if "symbol" in events.columns:
            out.append(f"- 심볼 수: {events['symbol'].nunique()}")
    out += ["", "> 비용 규약: `cost_roundtrip` 기본 1% 는 **왕복 수수료 0.2%"
            "(US 0.1%/체결) + 환전 스프레드 + 저유동성 슬리피지**를 포함한 보수적 총비용이다. "
            "수수료만의 0.2% 와 혼동하지 말 것 (계약 A2 §5).", ""]

    for key, df in sections.items():
        title_, question, guidance = SECTION_META.get(key, (key, "", ""))
        out.append(f"## {title_}")
        if question:
            out.append(f"> {question}")
        out += ["", df_to_markdown(df), ""]
        if guidance:
            out += [f"**해석 지침**: {guidance}", ""]

    if events is not None and len(events):
        cols = [c for c in ("symbol", "t0_ms", "kind", "session", "shape", "outcome",
                            "peak_ret", "ret_30m", "ret_close", "retrace_close",
                            "rvol_at_t0", "float_rotation", "ranking_lead_lag_min",
                            "halt_gap_count") if c in events.columns]
        out += ["## 부록 B. 이벤트 목록 (상위 30건)", "",
                df_to_markdown(events[cols].head(30), max_cols=len(cols)), ""]

    out += ["## 부록 C. 한계와 알려진 편향", ""]
    out += [f"- {line}" for line in LIMITATIONS + list(extra_notes or [])]
    out.append("")
    return "\n".join(out)


# --------------------------------------------------------------------------- #
# DB 경유 엔트리포인트
# --------------------------------------------------------------------------- #
def _safe(fn):
    """DB 부재/스키마 미비에도 리포트 생성이 죽지 않게 감싼다."""
    try:
        return fn()
    except Exception:                     # sqlite3.OperationalError, KeyError 등
        return None


def expand_meta_json(events: pd.DataFrame) -> pd.DataFrame:
    """`events.meta_json` 안의 A1 §4 추가 라벨을 다시 컬럼으로 펼친다.

    C-6 의 `events` 테이블은 계약 C-7 의 7컬럼 + `meta_json` 만 갖는다. 따라서 DB 를 거쳐
    오면 `t0_min_from_open`·`rvol_gated`·`hod_ms` 같은 컬럼이 사라지고 q6·기저율 대조가
    통째로 비어버린다. 이미 존재하는 컬럼은 덮어쓰지 않는다.
    """
    if events is None or len(events) == 0 or "meta_json" not in events.columns:
        return events if events is not None else pd.DataFrame()
    parsed: list[dict] = []
    for raw in events["meta_json"].tolist():
        if isinstance(raw, str) and raw.strip():
            try:
                got = json.loads(raw)
            except (TypeError, ValueError):
                got = {}
        else:
            got = raw if isinstance(raw, dict) else {}
        parsed.append({k: v for k, v in got.items()} if isinstance(got, dict) else {})
    extra = pd.DataFrame(parsed, index=events.index)
    new_cols = [c for c in extra.columns if c not in events.columns]
    return pd.concat([events, extra[new_cols]], axis=1) if new_cols else events


def generate_report(db_path: Path, out_path: Path, t_from_ms: int, t_to_ms: int) -> Path:
    """DB(read-only) → 평가 → 마크다운 파일. 반환: 기록한 경로.

    `Reader`(W2 소유)만 사용하고 쓰기 연결은 열지 않는다. 이벤트가 없거나 Reader 가 아직
    미구현이어도 예외를 던지지 않고 "데이터 없음" 리포트를 쓴다 — 무인 실행 파이프라인이
    리포트 생성 때문에 죽지 않도록.
    """
    from ..store.reader import Reader     # 지연 import (analysis → store 단방향)

    notes: list[str] = []
    # Reader 는 생성자에서 read-only 커넥션을 연다 → DB 부재 시 여기서 이미 예외.
    reader = _safe(lambda: Reader(Path(db_path)))
    if reader is None:
        notes.append(f"DB 를 열 수 없어 빈 리포트를 생성했다: {db_path}")
    events = _safe(lambda: reader.read_events(t_from_ms, t_to_ms)) if reader else None
    rankings = (_safe(lambda: reader.read_rankings("TOSS_SECURITIES_TRADING_AMOUNT",
                                                  t_from_ms, t_to_ms))
                if reader else None)
    if events is None:
        notes.append("`Reader.read_events` 를 사용할 수 없어 빈 리포트를 생성했다.")
        events = pd.DataFrame()
    else:
        events = expand_meta_json(events)
    if rankings is None:
        notes.append("`Reader.read_rankings` 를 사용할 수 없어 Q2 는 미가용으로 표기된다.")
        rankings = pd.DataFrame()

    df_1m = pd.DataFrame()
    if reader is not None and len(events) and "symbol" in events.columns:
        frames = []
        for sym in events["symbol"].dropna().unique():
            got = _safe(lambda s=sym: reader.read_candles_1m(str(s), t_from_ms, t_to_ms))
            if got is not None and len(got):
                frames.append(got if "symbol" in got.columns
                              else got.assign(symbol=str(sym)))
        if frames:
            df_1m = pd.concat(frames, ignore_index=True)
        else:
            notes.append("1분봉을 읽을 수 없어 Q4(덤프 속도)는 비어 있다.")

    sections = E.run_all(events, pd.DataFrame(), rankings, df_1m)
    text = render_report(sections, t_from_ms=t_from_ms, t_to_ms=t_to_ms,
                         events=events, extra_notes=notes)
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text, encoding="utf-8")
    return out


__all__ = ["render_report", "generate_report", "df_to_markdown",
           "expand_meta_json", "SECTION_META", "LIMITATIONS"]
