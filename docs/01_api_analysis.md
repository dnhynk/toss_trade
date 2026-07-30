# 토스증권 Open API 분석 — 미국 소형주 유동성 감시 전략 관점

> 기준: OpenAPI 스펙 v1.2.5 (2026-07-30 확인)
> 공식 문서: https://developers.tossinvest.com/docs
> 기계용 스펙: https://openapi.tossinvest.com/openapi-docs/latest/openapi.json
> 개요 문서: https://openapi.tossinvest.com/openapi-docs/overview.md

## 1. 기본 구조

- **REST 전용. 웹소켓/스트리밍 없음** → 모든 감시는 폴링(polling) 기반으로 설계해야 한다.
- Base URL: `https://openapi.tossinvest.com`
- 인증: OAuth 2.0 Client Credentials (`POST /oauth2/token`, form-urlencoded)
  - WTS 설정에서 client_id/secret 발급, **IP 화이트리스트 필수** (미등록 IP → 403)
  - refresh token 없음. 만료 시 재발급.
  - **client당 유효 토큰 1개 — 재발급 시 기존 토큰 즉시 무효화.**
    → 여러 프로세스가 각자 토큰을 발급하면 서로 죽인다. 토큰 매니저를 단일화할 것.
- 계좌 관련 API는 `X-Tossinvest-Account: {accountSeq}` 헤더 추가 (`GET /api/v1/accounts`로 조회).
- 응답 헤더에 `X-RateLimit-Limit / -Remaining / -Reset`, 429 시 `Retry-After` 제공.
- 샌드박스/모의투자 환경: 문서상 없음.

## 2. Rate Limit (client × API 그룹별, req/s)

| 그룹 | 한도 | 비고 |
|---|---|---|
| AUTH | 5 | |
| STOCK | 5 | 종목정보/유의사항 |
| MARKET_DATA | **10** | 현재가·호가·체결·상하한가 (공유) |
| MARKET_DATA_CHART | **5** | 캔들 |
| RANKING | 5 | |
| MARKET_INFO | 3 | 환율·캘린더 |
| ORDER / ORDER_INFO | 6 | 09:00–09:10 KST에는 3 (KRX 개장, 미국장엔 무관) |
| ORDER_HISTORY | 5 | |
| CONDITIONAL_ORDER | 5 | 이력 조회는 10 |

## 3. 전략에 핵심적인 엔드포인트

### 3.1 시세/데이터

| 엔드포인트 | 핵심 사양 | 전략적 용도 |
|---|---|---|
| `GET /prices` | **한 번에 200종목 배치**, 심볼당 lastPrice+timestamp만. **거래량 없음!** | 광역 유니버스 가격 스윕 (10 req/s × 200 = 이론상 2,000종목/초) |
| `GET /candles` | 1m/1d 봉, 호출당 최대 200개, `before` 페이지네이션, 수정주가 옵션 | **거래량 확보의 주력 수단.** 워치리스트 종목별 1분봉 폴링 + 과거 일봉 200개(≈10개월)로 베이스라인 |
| `GET /trades` | 당일 최근 체결 최대 50건 (체결가·수량·시각) | 집중감시 종목의 테이프 리딩 (체결 크기 분포, 초단위 버스트) |
| `GET /orderbook` | 매수/매도 호가+잔량 배열 (미국 주식의 깊이는 미명시 — 실측 필요) | 집중감시 종목의 호가 불균형·스프레드 |
| `GET /rankings` | KR/US **상위 100** | ↓ 아래 별도 분석 |
| `GET /stocks` | 배치 200종목. market(NYSE/NASDAQ/AMEX/US_ETC), securityType, status, listDate, **sharesOutstanding(발행주식수)** | **시가총액 계산 가능** (lastPrice × sharesOutstanding). 유니버스 필터링. float은 미제공 |
| `GET /stocks/{symbol}/warnings` | VI·투자경고 등 — 사실상 KRX 전용 | 미국 주식엔 실효성 낮음 (실측 확인) |
| `GET /market-calendar/US` | 4세션 (KST): dayMarket 09:00–16:50 / preMarket 17:00–22:30 / regular 22:30–05:00 / after 05:00–07:00 | 세션 스케줄러 |
| `GET /exchange-rate` | KRW↔USD, 1분 갱신 참고용 | 원화 환산 손익 |

### 3.2 랭킹 API 상세 — 이 전략의 최중요 데이터

`GET /rankings?type=&marketCountry=US&duration=&count=100`

- type:
  - `MARKET_TRADING_AMOUNT` / `MARKET_TRADING_VOLUME` — **시장 전체** 거래대금/거래량 상위
  - `TOSS_SECURITIES_TRADING_AMOUNT` / `TOSS_SECURITIES_TRADING_VOLUME` — **토스증권 체결 기준** 상위
  - `TOP_GAINERS` / `TOP_LOSERS` — 등락률 상위/하위
- duration: `realtime, 1d, 1w, 1mo, 3mo, 6mo, 1y`
  - **`TOP_GAINERS`/`TOP_LOSERS`는 realtime 미지원** (400 에러) → 실시간 급등 감지는 직접 계산해야 함
  - 거래대금/거래량 계열은 realtime 지원
- 응답: rank, symbol, lastPrice, basePrice, changeRate, tradingVolume, tradingAmount, rankedAt
- `excludeInvestmentCaution` 파라미터 (투자유의 제외 여부 — 우리는 당연히 **포함**해서 조회)

**전략적 함의**: `TOSS_SECURITIES_*` realtime 랭킹은 **토스 개인 자금이 지금 어떤 미국 종목으로 흐르는지를 직접 관측하는 창**이다. 같은 종목의
`TOSS_SECURITIES_TRADING_AMOUNT ÷ MARKET_TRADING_AMOUNT` 비율(토스 쏠림도)을 시계열로 쌓으면,
"토스 커뮤니티/급상승 리스트발 유동성"이 시장 유동성 대비 얼마나 비대해지는지를 정량화할 수 있다.
이는 앱 화면의 급상승 리스트를 크롤링하지 않고도 사용자의 엣지 가설을 검증할 수 있는 합법적·공식적 데이터 소스다.

### 3.3 주문

`POST /orders` (수량 기반 | 금액 기반):
- orderType: `LIMIT` / `MARKET` 만. **네이티브 스탑 주문 없음.**
- timeInForce: `DAY`(기본) / `CLS`(LOC, 미국+LIMIT만). OPG는 미지원.
- 소수점 수량: 미국 **시장가 매도**만, 정규장만. 소수점 매수는 `orderAmount`(금액 주문, US MARKET 전용, 정규장만).
- `clientOrderId` 멱등키 (10분 유효) — 재시도 안전성 확보에 반드시 사용.
- 1억원 이상 주문은 `confirmHighValueOrder: true` 필요.
- 세션 지정 파라미터 없음 → 프리/애프터/데이마켓 주문 접수 규칙은 실측 확인 필요.

`POST /conditional-orders` — **서버측 가격 감시 자동주문. 스탑로스의 실질 수단**:
- `SINGLE`: 조건 1개. **orderType MARKET 허용 → 트리거가 도달 시 시장가 매도 = 사실상 stop-market.**
- `OCO`: SELL 2개 (익절 지정가 위 + 손절 지정가 아래), **LIMIT만** → 갭다운 시 미체결(stop-limit) 리스크 존재.
- `OTO`: BUY 체결 후 SELL 감시 시작, LIMIT만.
- 조건 타입: `STOP`(가격 트리거) / `PROFIT_RATE`(수익률 트리거). `expireDate` 필수.
- 앱에서 등록한 조건주문도 목록 API에 함께 반환됨.

### 3.4 계좌/원가

- `GET /holdings`, `GET /buying-power?currency=USD`, `GET /sellable-quantity`
- `GET /commissions` — 계좌별 시장별 수수료율 조회 가능 (고회전 전략의 실효 비용 산정에 사용)

## 4. 확인된 공백 (전략 설계 시 전제할 것)

1. **현재가 배치 응답에 거래량 없음** → 광역 거래량 감시는 1분봉 폴링(5 req/s)으로 해결. 워치리스트 300종목 = 최단 60초 주기.
2. **실시간 등락률 랭킹 없음** → 자체 계산 (배치 /prices 스윕 + 전일 종가 캐시).
3. **float(유통주식수)·시가총액·스크리너·뉴스·커뮤니티 데이터 미제공** → sharesOutstanding으로 시총 근사, float은 외부 소스 필요.
4. **전체 종목 리스트 엔드포인트 없음** (`/stocks`는 심볼을 알아야 조회) → 유니버스 시드는 외부(예: NASDAQ Trader symbol directory)에서, 이후 랭킹 출현 종목을 누적 수집.
5. 미국 시세의 실시간/지연 여부 문서 미명시 (토스 앱은 실시간 무료 제공 — API도 동일할 가능성 높으나 **실측 확인**).
6. 1분봉 과거 보관 기간 미명시 → 실측 확인. 짧다면 **모니터가 직접 1분봉을 쌓는 것 자체가 자산**이 된다.
7. 미국 주식 호가 깊이(레벨 수) 미명시 → 실측 확인.
8. 웹소켓 없음 → 이벤트 감지 지연은 폴링 주기에 종속. 티어링으로 해결.

## 5. 미국장 세션 구조 (토스 특유)

토스는 KST 기준 거의 22시간 미국 주식 거래를 제공한다:

| 세션 | KST (서머타임 기준) | 특성 |
|---|---|---|
| 데이마켓 | 09:00–16:50 | 토스 자체 제공(대체거래소 경유). **한국 낮 시간 = 토스 개인 활동 최대 시간대. 유동성 얇음** |
| 프리마켓 | 17:00–22:30 | 미국 프리장 |
| 정규장 | 22:30–05:00 | 본장. 유동성 최대 |
| 애프터마켓 | 05:00–07:00 | |

주의: 커뮤니티/급상승발 한국 개인 쏠림은 **한국 낮(데이마켓)** 과 **정규장 초반(22:30–24:00)** 에 이중 피크가 생길 가능성이 높다. 데이마켓은 유동성이 얇아 가격 왜곡이 커질 수 있고, 정규장 개장 시 미국 유동성과 만나며 해소/증폭된다 — 이 자체가 감시 가설 중 하나.
