# 서울 실거래가 대시보드

국토교통부 실거래가 Open API(공공데이터포털)로 서울 25개 구의 아파트·오피스텔·연립다세대
매매·전세·월세 자료를 매일 받아, 구·동별 추이를 보여주는 페이지입니다.
블로그 [숫자로 읽는 경제](https://numeco.tistory.com)에 삽입해서 사용합니다.

## 구성
| 파일 | 역할 |
|---|---|
| `scripts/fetch_data.py` | API 호출 → 월별로 서울·구·동 집계 `data/agg/<종류>/<년월>.json`, 구별 최근 거래 `data/recent/` |
| `scripts/build_site.py` | 대시보드용 `data/site/index.json`, `seoul.json`, `<구코드>.json` 생성 |
| `.github/workflows/update.yml` | 매일 06:17(KST) 자동 실행 후 데이터 커밋 |
| `index.html` | 대시보드 (GitHub Pages, Chart.js) |

## 사용
- 주소로 지역 지정: `?gu=양천구&dong=목동&type=apt&deal=jeonse` (type: apt/offi/rh, deal: trade/jeonse/wolse)
- 방문자가 고른 설정은 브라우저(localStorage)에 저장되어 다음 방문 때 그대로 보임

## 설정
- 저장소 Secrets 에 `DATA_GO_KR_KEY` (공공데이터포털 일반 인증키, Decoding) 필요
- 매번 최근 3개월을 다시 받고(신고 기한 30일 반영), 남는 시간에 36개월까지 빈 달을 채움
- 승인되지 않은 API(403)는 건너뛰고, 승인되면 다음 실행 때 자동으로 과거 자료를 채움
- Actions 탭 → "실거래가 데이터 갱신" → Run workflow 로 수동 실행 가능

## 주의
실거래 신고 자료이며 호가가 아닙니다. 정보 제공용이며 투자 권유가 아닙니다.
