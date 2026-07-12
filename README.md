# stock-brief — 미국주식 워치리스트 브리핑

매일 아침 워치리스트(134종목) 시세와 **섹터를 움직일 뉴스·발표**를
자동 수집해서 애플 스타일 대시보드로 보여준다. 뉴스가 메인, 시세는 보조.

## 구성

| 파일 | 역할 |
|---|---|
| `tickers.json` | 워치리스트(테마별 그룹) + 그룹별/테마 뉴스 검색어. **종목 추가/삭제는 여기서** |
| `crawler.py` | yfinance 시세(1개월 일봉) + Google News RSS 수집 → `data.js` 생성 |
| `enrich.py` | 뉴스 영어 헤드라인 → 한국어 번역+요약 (Gemini무료 > Claude유료 > 무료번역 순) |
| `financials.py` | 종목별 매출구조·재무제표 수집(7일 캐시) → `financials.js` 생성 |
| `notify_telegram.py` | 아침 브리핑(급등락+뉴스)을 텔레그램으로 전송 (telegram-notify 모듈 재사용) |
| `config.json` | `anthropic_api_key` 를 넣으면 뉴스가 Claude 요약+해석으로 업그레이드 |
| `index.html` | 대시보드. `data.js`/`financials.js`를 읽으므로 file:// 로 바로 열림 |
| `run_crawler.bat` | 스케줄러용 배치 (crawler → enrich → financials → notify_telegram) |
| `fin_cache/` | 종목별 재무 캐시 · `crawler.log` 수집 로그 |

## 뉴스 한국어 번역 + 요약 (엔진 3종)

`config.json` 에서 엔진 우선순위대로 처리:
1. **Gemini (무료·카드 불필요, 추천)** — `gemini_api_key`. [aistudio.google.com](https://aistudio.google.com/apikey)에서 키 발급. 번역 + 1~2문장 요약
2. **Claude (유료)** — `anthropic_api_key`. [console.anthropic.com](https://console.anthropic.com/). 번역 + 요약
3. **무료 구글 번역 (폴백)** — 키 없을 때. 제목 번역만, 요약 없음

대시보드 뉴스에 파란 글씨(요약)가 보이면 LLM 엔진(Gemini/Claude)이 붙은 것.
모델은 `gemini_model`(기본 `gemini-2.0-flash`)로 바꿀 수 있다.

## 기업 상세 (카드/행 클릭)

종목 카드나 테이블 행을 클릭하면 모달이 열린다:
- **사업부문별 매출** (주요 16종목) — 부문/제품/지역별 매출 비중 가로 막대. 무료 API로 안 나오는 부문 매출을 `segments.json` 에 손입력. 예: TSM = HPC 51%·스마트폰 34%…, AAPL = 아이폰 51%·서비스 25%…, NVDA = 데이터센터 88%…
- **이익 구조 (워터폴)** — 최근 회계연도의 손익 흐름: 매출 → 매출원가 → 매출총이익 → R&D → 판관비 → 영업이익 → 법인세 → (기타) → 순이익. 어느 단계에서 얼마의 이익(초록)이 나고 얼마의 비용(빨강)이 빠지는지 막대 높이로 표시. 적자는 0선 아래 빨강, 은행류는 매출→비용·세금→순이익, 매출 전 단계 기업은 안내 문구.
- **재무제표** — 매출·매출원가·매출총이익·영업이익·순이익·희석EPS × 4개 회계연도 + 최근 분기(파란 열)
- **재무상태표** — 총자산·총부채·자기자본·현금 (기준 회계연도 명시)
- **관련 뉴스** — 급등락 종목 한정
- 모든 재무는 몇 년/몇 분기 기준인지 헤더에 표시(예: `FY2025`, `2026 Q1`)

### 부문별 매출 종목 추가하기

`segments.json` 을 편집하면 된다(financials.py가 다음 실행 때 `segments.js`로 반영):
```json
"티커": {"period": "FY2024", "by": "부문별", "seg": [["한글명", "English", 매출_백만달러], ...]}
```
현재 18종목: AAPL MSFT GOOGL AMZN META NVDA TSM AVGO AMD QCOM ARM TSLA ADBE UBER PLTR NFLX MOD VRT.
(단일사업·사전매출 종목 OKLO·SMR·UEC·RDW 등은 부문 분해가 불가능 — 이익 구조 워터폴만 나옴)
값은 공시(10-K/연차보고서) 기준 $M. 비중(%)은 입력값 합으로 자동 계산되므로 총액이 안 맞아도 OK.

## 공개 배포 (GitHub Pages)

- 사이트: **https://clddy.github.io/stock-brief** (친구·링크 공유용)
- 리포: github.com/clddy/stock-brief (main/root)
- `publish.py` 가 매일 버전(version.json build+1)을 기록하고 변경분을 자동 커밋·푸시
- **비공개 유지**(`.gitignore`): `config.json`(API키), `favorites.js`(보유종목), `data.json`, 로그/캐시 → 공개엔 워치리스트·뉴스·재무만 올라감. 보유종목/즐겨찾기는 로컬에만.

## 자동 실행

Windows 작업 스케줄러 `StockBriefCrawler` — 매일 06:30
(미국장 마감 = 한국시간 05:00 서머타임 기준, 겨울엔 06:00 마감이라 06:30이면 항상 마감 후).

수동 실행: `python crawler.py`

## 대시보드

바탕화면 "미국주식 브리핑" 바로가기(Edge 앱모드) 또는
`index.html` 더블클릭.

- **섹터 뉴스**: 그룹마다 `news_query`로 주가에 영향 줄 뉴스·발표 수집(최근 72시간, 6건) → 섹션 상단 패널
- **관심 테마 뉴스**: AI 전력 인프라 / SMR / 데이터센터 냉각 / 태양광 / 핵융합 (최근 72시간)
- **종목 뉴스**: 전일比 ±2.5% 이상 급등락 종목만(최근 48시간) — 파란 점 카드 클릭
- 카드/테이블 뷰 토글, 종목 검색(티커·한글)
- **즐겨찾기(★)** — 카드나 상세화면의 별을 눌러 등록. 헤더의 "★ 즐겨찾기" 버튼으로 등록 종목만 필터. localStorage 저장(브라우저별 유지). 검색과 조합 가능
- 상승=빨강 / 하락=파랑 (한국식), 스파크라인 = 최근 1개월 종가(호버 시 날짜·가격)

## 주의

- Yahoo Finance 데이터는 지연 시세일 수 있음. 투자 판단은 본인 책임.
- 주말엔 섹터에 따라 72시간 내 뉴스가 없어 패널이 비어 있을 수 있음(평일 정상).
