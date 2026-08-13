# Phase 1-C 부록 — evaluate.run_all 산출 (train+val, 홀드아웃 봉인)

> **[기록]** 소유 W3·W7 · 2026-08-11 · ★ 인용할 때 **1 분봉 자로 쟀다**를 병기한다
> 상태 표기의 뜻과 전수 목록: [`docs/INDEX.md`](INDEX.md)

- 분석 구간: `1756992660000` ~ `1777300800000` (UTC epoch ms) — 약 235.0일
- 시간 표기는 전부 UTC epoch ms (계약 C-1).
- 이벤트 수: **51**
- RVOL 게이트 미적용 이벤트: **0** (계약 A1 §6 — 기본 집계에서 제외)
- 심볼 수: 33

> 비용 규약: `cost_roundtrip` 기본 1% 는 **왕복 수수료 0.2%(US 0.1%/체결) + 환전 스프레드 + 저유동성 슬리피지**를 포함한 보수적 총비용이다. 수수료만의 0.2% 와 혼동하지 말 것 (계약 A2 §5).

> ★ **`cutoff_mode` = `strict_lt` (2026-08-11 표기 추가).** 이 부록은 **2026-08-09 컷오프
> 개정 이전** 산출이므로, 전조 피처에서 파생된 이 문서의 모든 수치는 컷오프 `ts_ms < t0_ms`
> 인 **`strict_lt` 값**이다. `docs/48` §6-1 의 딱지 의무를 뒤늦게 이행한 것이며 **수치는 한
> 개도 바꾸지 않았다.** 아래 표에서는 기존 `note` 칸에 딱지를 실었다(`docs/48` §6-2 가
> `docs/13` 에 정한 것과 같은 방식).
>
> **재실행 시 이 칸을 덮어쓰지 말 것.** `obs_le` 로 다시 돌리면 다른 값이 나오고
> `docs/48` §6-3 이 **두 모드를 같은 표에 섞는 것을 금지**한다 — 새 표를 만들어야 한다.
> 특히 `rvol_first_cross_{2,3}_lead_min` 의 `detect_rate` 는 `obs_le` 에서 **구조적 1.000**
> 이라 인용이 금지돼 있고(`docs/48` §11-4), **아래 `strict_lt` 값은 그 금지 대상이 아니다.**
> (요구 출처: `docs/51_anchor_boundary` §6-3, `docs/48` §11-7 #2.)

## Q1. 거래량 이상의 선행성
> 거래량 이상은 가격 급등보다 평균 몇 분 선행하는가? 임계값별 정밀도/재현율은?

| metric | n_events | detected | detect_rate | precision | recall | f1 | tp | fp | fn | lead_n | lead_mean | lead_median | note |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| vol_surge_lead_min | 49 | 44 | 0.898 | - | - | - | - | - | - | 44 | 257.3 | 96 | `cutoff_mode=strict_lt` |
| rvol_first_cross_2_lead_min | 49 | 42 | 0.8571 | - | - | - | - | - | - | 42 | 75.55 | 21 | `cutoff_mode=strict_lt` |
| rvol_first_cross_3_lead_min | 49 | 41 | 0.8367 | - | - | - | - | - | - | 41 | 74.8 | 20 | `cutoff_mode=strict_lt` |
| rvol_first_cross_5_lead_min | 49 | 34 | 0.6939 | - | - | - | - | - | - | 34 | 77.88 | 16.5 | `cutoff_mode=strict_lt` |
| precision_recall@rvol_at_cutoff>=2 | 49 | - | - | - | - | - | - | - | - | - | - | - | no_controls |
| precision_recall@rvol_at_cutoff>=3 | 49 | - | - | - | - | - | - | - | - | - | - | - | no_controls |
| precision_recall@rvol_at_cutoff>=5 | 49 | - | - | - | - | - | - | - | - | - | - | - | no_controls |

**해석 지침**: 리드타임 중앙값이 0 에 가깝고 검출률이 낮다면 '전조 탐지'보다 '시작 후 수 분 내 확인-진입'이 현실적이다 (La Morgia et al. 2020).

## Q2. 토스 랭킹 진입의 선행/후행
> 토스 랭킹 진입은 가격 대비 선행인가 후행인가?

| ranking_type | available | n_events | n_entered | entry_rate | lead_share | lag_share | verdict | leadlag_n | leadlag_mean | leadlag_median | leadlag_p10 | leadlag_p25 | note |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| (none) | no | 49 | 0 | - | - | - | unavailable | 0 | - | - | - | - | 랭킹 스냅샷 없음 — 과거 조회 불가(A2 §4). 실시간 수집 기간 필요. |

**해석 지침**: verdict='lag' 이면 어텐션 피크는 매수 신호가 아니라 **청산 카운트다운**이다 (Barber et al. 2022). 랭킹은 과거 조회가 불가하므로 실시간 수집 기간에만 채워진다.

## Q3. 데이마켓 급등의 정규장 지속성
> 데이마켓(한국 낮) 이상 급등이 정규장 개장 후 지속되는가, 소멸하는가?

| session | n | persist_rate | median_ret_close | median_peak_ret | median_retrace_close | median_time_to_peak_min | closed_below_vwap_share | fade_share | dump_share | retclose_n | retclose_mean | retclose_median | retclose_p10 |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| all | 49 | 0.2245 | -0.04947 | 0.004823 | -0.07103 | 0 | 0.3265 | 0.3061 | 0.102 | 49 | -0.06044 | -0.04947 | -0.16 |
| after | 6 | 0 | -0.1405 | 0.04542 | -0.1681 | 1 | 0.3333 | 0.3333 | 0.3333 | 6 | -0.1519 | -0.1405 | -0.1973 |
| regular | 43 | 0.2558 | -0.04423 | 0.004184 | -0.0587 | 0 | 0.3256 | 0.3023 | 0.06977 | 43 | -0.04768 | -0.04423 | -0.1586 |

**해석 지침**: persist_rate 가 낮으면 데이마켓 급등은 유동성 공백 위 가격 = 신호 오염원이다. 블루오션 체결취소 이력을 감안해 데이마켓은 신호로만 쓰고 체결을 전제하지 않는다.

## Q4. 덤프 속도와 홀트
> 급등 후 덤프의 속도 분포 — 피크에서 -20%까지 걸리는 시간, 홀트 개입 빈도.

| drawdown | metric | n | reached | reach_rate | horizon_min | minutes_n | minutes_mean | minutes_median | minutes_p10 | minutes_p25 | minutes_p75 | minutes_p90 | halt_any_share |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 0.2 | peak_to_-20pct | 25 | 0 | 0 | 390 | 0 | - | - | - | - | - | - | - |
| 0.5 | peak_to_-50pct | 25 | 0 | 0 | 390 | 0 | - | - | - | - | - | - | - |
| - | halt_gaps | 49 | - | - | - | 49 | 12.27 | 13 | 3 | 6 | 18 | 21.2 | 1 |

**해석 지침**: 중앙값이 수 분 단위면 수동 손절이 불가능하다는 뜻이므로 사이징으로만 통제해야 한다. 하방 LULD 홀트 중에는 어떤 주문도 체결되지 않는다.

## Q5. 비용 차감 후 기대수익
> 전조 스코어 상위 신호의 기대 수익 분포 — 비용 차감 후 양수인가?

| bucket | policy | score_col | n | mean_gross | median_gross | win_rate_gross | mean_net | median_net | win_rate_net | p25_net | p75_net | positive_expectancy | cost_roundtrip |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| all | t0_to_30m | rvol_at_cutoff | 44 | -0.06607 | -0.06562 | 0.1591 | -0.07607 | -0.07562 | 0.1364 | -0.12 | -0.02 | no | 0.01 |
| all | t0_to_close | rvol_at_cutoff | 49 | -0.06044 | -0.04947 | 0.2245 | -0.07044 | -0.05947 | 0.1633 | -0.1302 | -0.01 | no | 0.01 |
| all | half_peak | rvol_at_cutoff | 49 | 0.01613 | 0.002412 | 0.5714 | 0.006134 | -0.007588 | 0.4286 | -0.01 | 0.01742 | yes | 0.01 |
| rvol_at_cutoff(0.641,3.33] | t0_to_30m | rvol_at_cutoff | 12 | -0.06858 | -0.07804 | 0.1667 | -0.07858 | -0.08804 | 0.08333 | -0.1387 | -0.01369 | no | 0.01 |
| rvol_at_cutoff(0.641,3.33] | t0_to_close | rvol_at_cutoff | 12 | -0.07237 | -0.08793 | 0.1667 | -0.08237 | -0.09793 | 0.1667 | -0.135 | -0.0471 | no | 0.01 |
| rvol_at_cutoff(0.641,3.33] | half_peak | rvol_at_cutoff | 12 | 0.01133 | 0 | 0.3333 | 0.001325 | -0.01 | 0.3333 | -0.01 | 0.01043 | yes | 0.01 |
| rvol_at_cutoff(3.33,5.88] | t0_to_30m | rvol_at_cutoff | 10 | -0.08167 | -0.0791 | 0.2 | -0.09167 | -0.0891 | 0.2 | -0.142 | -0.02569 | no | 0.01 |
| rvol_at_cutoff(3.33,5.88] | t0_to_close | rvol_at_cutoff | 12 | -0.07253 | -0.04784 | 0.1667 | -0.08253 | -0.05784 | 0.08333 | -0.1114 | -0.01 | no | 0.01 |
| rvol_at_cutoff(3.33,5.88] | half_peak | rvol_at_cutoff | 12 | 0.004962 | 0 | 0.4167 | -0.005038 | -0.01 | 0.1667 | -0.01 | -0.006278 | no | 0.01 |
| rvol_at_cutoff(5.88,13.4] | t0_to_30m | rvol_at_cutoff | 11 | -0.03029 | -0.03104 | 0.2727 | -0.04029 | -0.04104 | 0.2727 | -0.0817 | -0.003744 | no | 0.01 |
| rvol_at_cutoff(5.88,13.4] | t0_to_close | rvol_at_cutoff | 12 | -0.02296 | -0.00435 | 0.4167 | -0.03296 | -0.01435 | 0.25 | -0.07456 | 0.0009202 | no | 0.01 |
| rvol_at_cutoff(5.88,13.4] | half_peak | rvol_at_cutoff | 12 | 0.01366 | 0.009302 | 0.75 | 0.003657 | -0.0006975 | 0.5 | -0.009842 | 0.0124 | yes | 0.01 |
| rvol_at_cutoff(13.4,138] | t0_to_30m | rvol_at_cutoff | 11 | -0.08493 | -0.03819 | 0 | -0.09493 | -0.04819 | 0 | -0.1213 | -0.04358 | no | 0.01 |
| rvol_at_cutoff(13.4,138] | t0_to_close | rvol_at_cutoff | 12 | -0.08912 | -0.07061 | 0.08333 | -0.09912 | -0.08061 | 0.08333 | -0.1549 | -0.05254 | no | 0.01 |
| rvol_at_cutoff(13.4,138] | half_peak | rvol_at_cutoff | 12 | 0.03084 | 0.02648 | 0.75 | 0.02084 | 0.01648 | 0.6667 | -0.003644 | 0.03371 | yes | 0.01 |

**해석 지침**: mean_net > 0 이 아니면 전략은 성립하지 않는다. half_peak 정책은 **실행 가능성 상한**이며 달성 가정이 아니다.

## Q6. 시간대 효과
> 개장 15분 / 10:00 ET 전후 / 마감 전의 신호 성능 차이.

| bucket | min_from_open_lo | min_from_open_hi | n | n_minus | n_plus | n_boundary_sensitive | boundary_sensitive | median_peak_ret | median_ret_30m | median_ret_close | median_time_to_peak_min | hod_within_15min_share | hod_before_1000et_share |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| pre_or_day | -100,000 | 0 | 0 | 1 | 0 | 0 | no | - | - | - | - | - | - |
| open_0_15 | 0 | 15 | 6 | 5 | 6 | 1 | yes | 0.0002106 | -0.1083 | -0.03034 | 37 | 0.5 | 0.5 |
| open_15_30 | 15 | 30 | 4 | 4 | 4 | 0 | no | 0.02151 | -0.02189 | -0.006728 | 5 | 0 | 0.75 |
| mid_30_180 | 30 | 180 | 21 | 21 | 21 | 0 | no | 0.01124 | -0.06976 | -0.04855 | 0 | 0 | 0 |
| late_180_330 | 180 | 330 | 9 | 9 | 9 | 0 | no | 0 | -0.04413 | -0.05405 | 0 | 0.1111 | 0.1111 |
| close_330_390 | 330 | 390 | 3 | 3 | 3 | 0 | no | 0.04082 | 0.02491 | 0.006494 | 14 | 0 | 0 |
| after_hours | 390 | 100,000 | 6 | 6 | 6 | 0 | no | 0.04542 | -0.09619 | -0.1405 | 1 | 0 | 0 |

**해석 지침**: 미국 개장 15분(= 한국시간 23:30–23:45)은 LULD 밴드가 2배로 넓어지는 구간이다.

## 부록 A. 알려진 기저율 대조
> docs/02 §2.4 SmallCapLab 실측치와 우리 데이터의 차이.

| base_rate | known | observed | n | delta | n_ungated_excluded |
|---|---|---|---|---|---|
| fade_rate | 0.715 | 0.4082 | 49 | -0.3068 | 0 |
| break_20pct_from_hod | 0.5 | 0.08163 | 49 | -0.4184 | 0 |
| close_below_vwap | 0.73 | 0.3265 | 49 | -0.4035 | 0 |
| hod_before_1000et | 0.63 | 0.1429 | 49 | -0.4871 | 0 |
| hod_within_15min | 0.466 | 0.08163 | 49 | -0.3844 | 0 |

**해석 지침**: delta 가 크면 표본 정의(이벤트 임계값·유니버스)가 문헌과 다르다는 신호다 — 먼저 표본을 의심하고 그 다음에 시장을 의심한다.

## 부록 A0. 표본 필터 (사전등록 §2.7)
> 주 분석 표본에 남은 이벤트와 사유별 제외 카운트.

| reason | n | share | scope | note | n_total | n_kept |
|---|---|---|---|---|---|---|
| not_common_stock | 0 | 0 | event | - | 51 | 49 |
| not_active | 0 | 0 | event | - | 51 | 49 |
| meta_missing | 0 | 0 | event | - | 51 | 49 |
| t0_price_unavailable | 0 | 0 | event | - | 51 | 49 |
| price_out_of_range | 0 | 0 | event | - | 51 | 49 |
| mcap_out_of_range | 2 | 0.03922 | event | - | 51 | 49 |
| half_day_length_sample | 0 | 0 | event | - | 51 | 49 |
| r_uncomputable | 24742 | - | symbol_day | 분할 신호 r 을 계산할 수 없었던 (심볼, 매매일) 수 (사전등록 §7-e 보고 의무) | 51 | 49 |
| mcap_band_low_kept | 0 | 0 | mcap_band_sensitivity | - | 51 | 49 |
| mcap_band_low_excluded | 1 | 0.01961 | mcap_band_sensitivity | - | 51 | 49 |
| mcap_band_high_kept | 0 | 0 | mcap_band_sensitivity | - | 51 | 49 |
| mcap_band_high_excluded | 0 | 0 | mcap_band_sensitivity | - | 51 | 49 |
| split_excluded | 0 | - | symbol_day | - | 51 | 49 |

**해석 지침**: `(filter_not_applied)` 행이 보이면 **필터를 돌리지 않은 것**이지 제외가 0건인 것이 아니다. `half_day_length_sample` 은 반일장이라 RVOL 표본이 부족해 빠진 건수다.

## 부록 B. 이벤트 목록 (상위 30건)

| symbol | t0_ms | kind | session | shape | outcome | peak_ret | ret_30m | ret_close | retrace_close | rvol_at_t0 | float_rotation | ranking_lead_lag_min | halt_gap_count |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| ADSEW | 1770131760000 | win | regular | mixed | hold | 0 | - | 0 | 0 | 11.94 | - | - | 2 |
| ADSEW | 1770908760000 | win | regular | mixed | dump | 0.1566 | -0.1565 | 0 | -0.1354 | 30.73 | - | - | 1 |
| ADSEW | 1771873200000 | day | regular | mixed | hold | 0 | - | -0.1958 | -0.1958 | 3.248 | - | - | 3 |
| ADSEW | 1772554860000 | win | regular | instant | hold | 0 | -0.07103 | -0.07103 | -0.07103 | 3.197 | - | - | 1 |
| ADSEW | 1773853080000 | win | regular | mixed | hold | 0.004823 | -0.07657 | 0.004823 | 0 | 3.331 | - | - | 9 |
| ADSEW | 1774537560000 | win | regular | ramp | dump | 0 | -0.2441 | -0.2731 | -0.2731 | 4.225 | - | - | 10 |
| ADSEW | 1774618200000 | win | regular | mixed | hold | 0.1223 | - | 0.1223 | 0 | 11.16 | - | - | 3 |
| AFRIW | 1773766500000 | win | regular | mixed | hold | 0.03187 | -0.04382 | 0.03187 | 0 | 4.053 | - | - | 5 |
| AFRIW | 1773930480000 | win | regular | mixed | fade | 0.3265 | - | 0.1429 | -0.1385 | 110.3 | - | - | 10 |
| AIHS | 1768941660000 | win | regular | mixed | fade | 0.05484 | 0.02491 | -0.03486 | -0.08504 | 3.527 | - | - | 22 |
| APWC | 1766419380000 | win | regular | ramp | hold | 0 | -0.06849 | -0.1598 | -0.1598 | 7.599 | - | - | 14 |
| BAERW | 1776349080000 | win | regular | mixed | hold | 0 | - | -0.04855 | -0.04855 | 32.93 | - | - | 6 |
| BLIV | 1761054660000 | win | regular | instant | hold | 0.03883 | 0.0242 | 0.0242 | -0.01408 | 10.96 | - | - | 9 |
| BLIV | 1766160300000 | win | regular | mixed | hold | 0 | -4.914e-05 | -0.04423 | -0.04423 | 24.74 | - | - | 10 |
| BLIV | 1766503980000 | win | regular | mixed | fade | 0.08128 | -0.0333 | -0.03748 | -0.1098 | 18.38 | - | - | 11 |
| BLIV | 1777300800000 | win | regular | mixed | hold | 0 | - | 0 | 0 | 4.735 | - | - | 4 |
| BMHL | 1765210140000 | win | regular | mixed | fade | 0.01531 | -0.08163 | -0.08163 | -0.09548 | 4.064 | - | - | 14 |
| BVC | 1774624080000 | win | regular | mixed | hold | 0 | -0.06274 | -0.06081 | -0.06081 | 5.232 | - | - | 16 |
| CDROW | 1762536660000 | win | regular | mixed | hold | 0 | 0 | 0 | 0 | 4.096 | - | - | 3 |
| CDROW | 1772463660000 | win | regular | mixed | hold | 0.02985 | -0.1272 | 0.02985 | 0 | 3.531 | - | - | 6 |
| CDROW | 1773415800000 | win | regular | mixed | hold | 0.07936 | 0.000164 | 0.06575 | -0.01261 | 3.908 | - | - | 6 |
| CDROW | 1773853140000 | win | regular | mixed | hold | 0 | -0.1342 | -0.02985 | -0.02985 | 5.393 | - | - | 13 |
| CLPS | 1767386700000 | win | regular | mixed | hold | 0.04082 | 0.03061 | 0.04082 | 0 | 4.271 | - | - | 9 |
| CLWT | 1759873920000 | both | after | instant | dump | 0.08696 | -0.288 | -0.2391 | -0.3 | 24.33 | - | - | 6 |
| CLWT | 1759951440000 | win | regular | mixed | fade | 0.02597 | -0.03104 | 0.006494 | -0.01899 | 14.54 | - | - | 19 |
| EBON | 1776694260000 | win | regular | mixed | hold | 0 | -0.03534 | -0.04947 | -0.04947 | 7.991 | - | - | 22 |
| EDTK | 1765290960000 | win | regular | mixed | hold | 0.0004212 | -0.1083 | 0.0004212 | 0 | 14.29 | - | - | 15 |
| EDTK | 1774889400000 | win | regular | instant | hold | 0 | 0 | 0 | 0 | 4.391 | - | - | 3 |
| GTN.A | 1766178120000 | win | after | mixed | fade | 0.07389 | -0.004926 | -0.1202 | -0.1807 | 9.963 | - | - | 8 |
| JL | 1772637840000 | win | regular | ramp | fade | 0.05311 | -0.05346 | -0.0087 | -0.0587 | 10.98 | - | - | 18 |

## 부록 C. 한계와 알려진 편향

- 랭킹 스냅샷은 **과거 조회가 불가**하다 — Q2 는 실시간 수집 기간에만 답할 수 있다 (A2 §4).
- 미국 호가는 최우선 1레벨만 제공된다 — 호가 깊이 기반 피처는 존재할 수 없다 (A2 §1).
- `/trades` 는 최대 50건이므로 체결 크기 분포는 **표본 통계**다 (A2 §2).
- 1분봉은 체결이 없는 분에 봉을 주지 않는다 — 결측은 거래량 0 으로 해석하되, 세션 전체 결측(수집 중단)은 베이스라인 평균에서 제외한다.
- `half_peak` 청산 정책은 실행 가능성 상한선이며 달성 가정이 아니다.
- 이벤트 임계값(30분 +15% / 당일 +30% / RVOL 3)은 초기값이며 데이터로 보정할 대상이다.
- 정의식·경계조건·편향의 전체 목록은 `docs/07_analysis_spec.md` 참조.
- 표본 정의 감사 발동(§1.7): 기저율 5개 전부 대역 밖 — 본문 §4 참조.
- 이벤트 카탈로그는 collector events 테이블이 아니라 candles_1m 에서 오프라인 재유도했다(§7-a).
