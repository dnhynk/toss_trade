= W4 — 두 개의 카운터가 거짓말을 하고 있다 (`schema_mismatch`, 429 대응)

둘 다 오늘 아침 실측이다. 둘 다 "조용한 실패"가 아니라 **시끄러운 거짓 경보**이며,
아침에 상태를 읽는 사람(사람·자동화 코디네이터 공통)을 잘못된 방향으로 보낸다.

## (1) `schema_mismatch` 가 "없는 종목"을 "API 계약 변경"으로 센다

```
2026-07-31 20:40:55 WARNING tier2:AVAT: schema mismatch (skip): http-404 code=stock-not-found
2026-08-03 23:16:12 WARNING tier2:BLRK: ... 23:16:26 tier2:CABR: ...
2026-08-03 23:18:13 WARNING tier2:MACI: ... 23:21:31 tier2:JSM: ...
```

`loops.py` 의 `_guarded` 가 `SchemaMismatch` 를 통째로 `schema_mismatch` 로 센다.
그런데 그 안에는 **성격이 완전히 다른 두 가지**가 섞여 있다:

- **응답 모양이 바뀐 것** — 진짜 사고. 재시작으로 안 고쳐지고, 그날 데이터를 의심해야 한다.
- **`http-404 code=stock-not-found`** — 랭킹에 뜬 심볼을 상세 조회했는데 없더라는 것.
  상장폐지·거래정지·심볼 변경이면 **정상적으로 일어나는 일**이고 그 종목만 건너뛰면 된다.

지금은 후자가 전자의 옷을 입고 CRIT 경보를 띄운다. 경보 문구가
"the API response shape changed. A restart will NOT fix this. Inspect the endpoint
contract before trusting today's data" 라서, **멀쩡한 하루치 데이터를 의심하게 만든다.**

**해야 할 일**: `SchemaMismatch` 중 `stock-not-found` 계열(404 + 그 code)을
별도 카운터(`symbol_not_found` 등)로 분리해 텔레메트리에 내보내라.
- 어떤 조건으로 가르는지 **테스트로 고정**하라 — 404 지만 code 가 다른 경우,
  code 는 같은데 상태코드가 다른 경우 둘 다.
- **`schema_mismatch` 는 진짜 모양 변경일 때만 오르도록** 남겨라. 이 카운터는
  "그날 데이터를 믿지 마라"의 근거이므로 **묽어지면 안 된다.**
- 어떤 심볼이 얼마나 자주 404 인지 알 수 있게 하라(반복 404 심볼은 워치리스트에서
  빼는 게 맞을 수 있으나, **그 판단·구현은 이 태스크 범위 밖이다** — 보고만 하라).
- 경보 쪽(문구·심각도)은 **W5 소유**다. 코디네이터가 W5 에 별도 발행했다.
  네 카운터가 나오면 W5 가 NOTE 로 떨어뜨린다. **`ops/**` 를 건드리지 마라.**

## (2) 429 를 맞으면 티어 정원을 줄이는데, 우리 사용량은 한도의 1/5 이다

오늘 09:00~09:31 실측 (세션 closed/day, 즉 미국장 마감 상태):

```
09:00:34 WARNING budget: 429 on MARKET_DATA (count=1) - forcing tier shrink
09:00:42 WARNING budget shrink {MARKET_DATA: 2} -> caps={tier2:120, tier3:6} demoted=2
09:01:22 WARNING budget shrink {MARKET_DATA: 2} -> caps={tier2:120, tier3:4} demoted=0
09:11:24 WARNING budget shrink {MARKET_DATA: 1} -> caps={tier2:120, tier3:3} demoted=1
09:20:09 WARNING budget shrink {MARKET_DATA_CHART: 24} -> caps={tier2:96, tier3:3} demoted=0
09:30:26 WARNING budget shrink {MARKET_DATA: 1} -> caps={tier2:96, tier3:2} demoted=1
```

같은 시각 텔레메트리: `budget MARKET_DATA=1.05~1.40/7.00`, `api_errors=0`, `fetch_success_pct=100.0`.
**예산의 15~20% 만 쓰는데 429 를 맞는다.** 이미 네가 넣은 정직한 문구가 이걸 말하고 있다
— "랭킹은 축소 대상이 아니며 **한도 인식이 틀렸을 수 있다**".

그리고 **발생 시각에 뚜렷한 패턴이 있다**(collector.log 전수):

```
07-31 14시(2) 15시(3)   08-03 09시(4) 10시(1) 11시(3) 12시(4) 13시(6) 14시(3)   08-04 09시(7)
```

**전부 KST 09~15시** — 미국장이 닫혀 있는 한국 낮 시간이다.
**미국 정규장·프리·애프터(22:30~08:50 KST) 구간에는 429 가 단 한 건도 없다.**

**조사·판단할 것** (수정 의무 아님 — 근거 없이 고치지 마라):
1. 우리 사용량 추정이 서버가 세는 것과 다른가, 아니면 **한도가 시간대에 따라 다른가**.
   후자라면 우리 `target` 값이 시간대와 무관하게 하나인 것이 틀렸다.
2. **429 의 대응 수단이 "티어 정원 축소"인 것이 맞는가.** 티어 정원은 곧 수집 범위다.
   오늘 tier3 정원이 6 -> 2 로 깎였다. 지금은 장이 닫혀 무해했지만,
   **같은 일이 22:30 개장 직후에 일어나면 정확히 어제 우리를 태운 tier3 고사가 재발한다.**
   백오프·주기 조절 같은 **수집 범위를 안 줄이는 수단**이 먼저여야 하지 않은가.
3. 축소된 정원이 **어떻게 회복되는가.** 오늘 tier3 은 6->4->3->2 로 내려간 뒤
   2~3 사이를 오갔다. 회복 경로가 있다면 그 근거를, 없다면 그것이 결함임을 보고하라.

**막힌 부분이 있으면 그렇게 보고하라.** (2)는 조사 결과가 "우리가 틀렸다"든
"토스가 시간대별 한도를 둔다"든 **어느 쪽이든 유용하다.** 추측으로 숫자를 바꾸지 마라.

## 규율

- **가동 중 수집기를 직접 죽이거나 띄우지 마라.** 라이브 API 호출 금지(수집기만 한다).
- 소유 경로(`tossmon/collector/**`, `tossmon/config.py`, `tests/test_collector*.py`)만.
- 브랜치 `w4-collector`, pytest green, 콘솔 출력 비ASCII 금지.
- 수정 전 실패를 **직접 확인**하고 회귀 테스트를 붙여라 (stash 검증).
- `worker_done` 1회(ASCII), (a) 무엇을 했나 (b) 어떻게 검증했나 (c) 무엇을 못 했나
  (d) 소유권 밖 발견 (e) 다음 사람이 알아야 할 것.
