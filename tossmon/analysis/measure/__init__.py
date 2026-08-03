"""측정 스크립트 — 리포 자산으로 승격 (감사 4차 B5).

docs/17 §8 · docs/18 §7 · docs/19 가 재실행 절차로 지정하는 스크립트들이 스크래치패드에만
있어서, (i) 재실행 약속이 이행 불가였고 (ii) **핵심 정의를 확인할 수 없었다**.
감사 4차 B5 가 지적한 대로 그 상태에서는 docs/18 §2 의 "움직이는 중"이 과거 5분인지
미래 5분인지 확인할 수 없었다 — 미래면 그 축 전체가 무효가 된다.

## 조건화 창의 정본 정의 (B5 해소)

`CONDITIONING` (아래)이 정본이다. 요약:

- docs/18 §2 의 "동시간대 5분 절대수익률"은 **과거 5분**이다:
  `abs_ret_5m[m] = |close[m] / close[m-5] − 1|` (`pandas.shift(5)`).
  **미래 봉을 쓰지 않는다** — 이 사실은 `tests/test_measure_conditioning.py` 가
  합성 데이터로 강제한다(미래에만 움직임이 있으면 지표가 0 이어야 한다).
- 다만 원본 산출은 스냅샷을 **자기 분(minute)의 봉**에 붙였다. 10:00:15 의 스냅이
  10:01:00 에 마감하는 봉의 종가를 쓰므로 **최대 60초의 동시성 누출**이 있었다.
  `strict_prior=True` 는 이를 제거해 **직전 분까지의 종가만** 쓴다.
  두 값 모두 docs/18 §2.2 에 병기한다.

실행: `python -m tossmon.analysis.measure.execution_measure`
"""

#: B5 정본 — 조건화 창의 방향과 경계.
CONDITIONING = {
    "axis": "abs_ret_5m",
    "window": "past 5 bars, backward-looking",
    "formula": "abs(close[m] / close[m-5] - 1)",
    "uses_future_bars": False,
    "contemporaneous_leak_original": "<= 60 s (snapshot matched to its own minute bar)",
    "strict_prior_option": "match snapshot at minute m to the value computed at m-1",
}
