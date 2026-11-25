# 문래·도림·신도림 실거래가 대시보드

국토교통부 실거래가 Open API(공공데이터포털)로 영등포구·구로구 자료를 매일 받아
문래동·도림동·신도림동의 매매·전세·월세 추이를 보여주는 페이지입니다.
블로그 [숫자로 읽는 경제](https://numeco.tistory.com)에 삽입해서 사용합니다.

## 구성
| 파일 | 역할 |
|---|---|
| `scripts/fetch_data.py` | API 호출 → `data/raw/<종류>/<시군구>/<년월>.json` 저장 |
| `scripts/build_summary.py` | 동·주택유형·월별 집계 → `data/summary.json` |
| `.github/workflows/update.yml` | 매일 06:17(KST) 자동 실행 후 데이터 커밋 |
| `index.html` | 대시보드 (GitHub Pages, Chart.js) |

## 설정
- 저장소 Secrets 에 `DATA_GO_KR_KEY` (공공데이터포털 일반 인증키, Decoding) 필요
- 처음 실행 시 최근 36개월, 이후에는 최근 3개월을 매일 다시 받음 (신고 기한 30일 반영)
- Actions 탭 → "실거래가 데이터 갱신" → Run workflow 로 수동 실행 가능

## 주의
실거래 신고 자료이며 호가가 아닙니다. 정보 제공용이며 투자 권유가 아닙니다.
