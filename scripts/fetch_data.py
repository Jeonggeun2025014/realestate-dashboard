"""
국토교통부 실거래가 API(공공데이터포털)에서 매매·전월세 자료를 받아
data/raw/<종류>/<시군구코드>/<계약년월>.json 으로 저장하는 스크립트.

- 인증키는 환경변수 DATA_GO_KR_KEY 에서 읽는다 (GitHub Secrets 로 전달).
- 실거래 신고 기한이 계약 후 30일이므로, 최근 몇 달은 매일 다시 받아서 덮어쓴다.
- 처음 실행(저장된 자료가 없음)이면 과거 36개월치를 한 번에 받는다.
"""

import argparse
import datetime as dt
import json
import os
import sys
import time
import xml.etree.ElementTree as ET
from pathlib import Path

import requests

# ── 기본 설정 ──────────────────────────────────────────────
BASE_URL = "https://apis.data.go.kr/1613000"

# 받아올 시군구 (법정동 코드 앞 5자리)
REGIONS = {
    "11560": "영등포구",  # 문래동, 도림동 포함
    "11530": "구로구",    # 신도림동 포함
}

# 받아올 API 목록: 키 = 저장 폴더 이름
APIS = {
    "apt_trade":  "RTMSDataSvcAptTradeDev/getRTMSDataSvcAptTradeDev",  # 아파트 매매(상세)
    "apt_rent":   "RTMSDataSvcAptRent/getRTMSDataSvcAptRent",          # 아파트 전월세
    "offi_trade": "RTMSDataSvcOffiTrade/getRTMSDataSvcOffiTrade",      # 오피스텔 매매
    "offi_rent":  "RTMSDataSvcOffiRent/getRTMSDataSvcOffiRent",        # 오피스텔 전월세
    "rh_trade":   "RTMSDataSvcRHTrade/getRTMSDataSvcRHTrade",          # 연립다세대 매매
    "rh_rent":    "RTMSDataSvcRHRent/getRTMSDataSvcRHRent",            # 연립다세대 전월세
}

DATA_DIR = Path(__file__).resolve().parent.parent / "data" / "raw"
ROWS_PER_PAGE = 1000
FIRST_RUN_MONTHS = 36


# ── 날짜 도우미 ─────────────────────────────────────────────
def recent_months(n):
    """오늘 기준 최근 n개월의 'YYYYMM' 목록 (이번 달 포함, 오래된 순)."""
    today = dt.date.today()
    y, m = today.year, today.month
    out = []
    for _ in range(n):
        out.append(f"{y}{m:02d}")
        m -= 1
        if m == 0:
            y, m = y - 1, 12
    return list(reversed(out))


# ── 숫자 변환 도우미 ─────────────────────────────────────────
def to_int(text):
    """'120,000' 같은 문자열을 정수로. 비어 있으면 None."""
    if text is None:
        return None
    text = text.strip().replace(",", "")
    if text == "":
        return None
    try:
        return int(float(text))
    except ValueError:
        return None


def to_float(text):
    """'84.97' 같은 문자열을 실수로. 비어 있으면 None."""
    if text is None or text.strip() == "":
        return None
    try:
        return float(text.strip().replace(",", ""))
    except ValueError:
        return None


# ── API 호출 ───────────────────────────────────────────────
def call_api(path, key, lawd_cd, ym, page):
    """API를 한 번 호출해서 XML 루트를 돌려준다. 실패하면 3번까지 재시도."""
    params = {
        "serviceKey": key,
        "LAWD_CD": lawd_cd,
        "DEAL_YMD": ym,
        "pageNo": page,
        "numOfRows": ROWS_PER_PAGE,
    }
    last_err = None
    for attempt in range(3):
        try:
            res = requests.get(f"{BASE_URL}/{path}", params=params, timeout=30)
            res.raise_for_status()
            root = ET.fromstring(res.content)
            check_result_code(root)
            return root
        except Exception as e:  # 네트워크 오류, XML 오류, API 오류 모두 재시도
            last_err = e
            time.sleep(2 * (attempt + 1))
    raise RuntimeError(f"{path} {lawd_cd} {ym} p{page}: {last_err}")


def check_result_code(root):
    """API가 돌려준 결과 코드가 정상(00/000)인지 확인. 아니면 예외."""
    code = root.findtext(".//resultCode") or root.findtext(".//returnReasonCode")
    msg = root.findtext(".//resultMsg") or root.findtext(".//returnAuthMsg") or ""
    if code is None:
        raise RuntimeError(f"알 수 없는 응답: {ET.tostring(root)[:200]!r}")
    if code.strip() not in ("00", "000"):
        raise RuntimeError(f"API 오류 {code}: {msg}")


# ── 응답 정리 ──────────────────────────────────────────────
def parse_item(item, kind):
    """XML <item> 하나를 블로그 대시보드에서 쓰기 좋은 딕셔너리로 바꾼다."""
    get = lambda tag: item.findtext(tag)

    year, month, day = to_int(get("dealYear")), to_int(get("dealMonth")), to_int(get("dealDay"))
    if not (year and month and day):
        return None

    rec = {
        "date": f"{year:04d}-{month:02d}-{day:02d}",
        "dong": (get("umdNm") or "").strip(),
        # 단지/건물 이름은 종류마다 태그 이름이 다르다
        "name": (get("aptNm") or get("offiNm") or get("mhouseNm") or "").strip(),
        "area": to_float(get("excluUseAr")),
        "floor": to_int(get("floor")),
        "built": to_int(get("buildYear")),
    }

    if kind.endswith("_trade"):
        rec["price"] = to_int(get("dealAmount"))          # 단위: 만원
        # 계약 해제된 거래는 표시(O) — 집계에서 뺀다
        rec["cancelled"] = (get("cdealType") or "").strip() == "O"
        if rec["price"] is None:
            return None
    else:
        rec["deposit"] = to_int(get("deposit"))           # 보증금, 만원
        rec["rent"] = to_int(get("monthlyRent")) or 0     # 월세, 만원 (0이면 전세)
        rec["contract"] = (get("contractType") or "").strip()  # 신규/갱신
        if rec["deposit"] is None:
            return None
    return rec


def fetch_month(kind, path, key, lawd_cd, ym):
    """한 종류·한 지역·한 달치를 페이지를 넘겨가며 모두 받는다."""
    records, page = [], 1
    while True:
        root = call_api(path, key, lawd_cd, ym, page)
        items = root.findall(".//items/item")
        for it in items:
            rec = parse_item(it, kind)
            if rec:
                records.append(rec)
        total = to_int(root.findtext(".//totalCount")) or 0
        if page * ROWS_PER_PAGE >= total or not items:
            break
        page += 1
    records.sort(key=lambda r: r["date"])
    return records


def save_month(kind, lawd_cd, ym, records):
    """받은 자료를 JSON 파일로 저장."""
    out = DATA_DIR / kind / lawd_cd / f"{ym}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(records, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")


# ── 실행 ───────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--months", type=int, default=3, help="다시 받을 최근 개월 수")
    args = parser.parse_args()

    key = os.environ.get("DATA_GO_KR_KEY", "").strip()
    if not key:
        sys.exit("환경변수 DATA_GO_KR_KEY 가 없습니다 (GitHub Secrets 확인).")

    failures, total_calls = 0, 0
    for kind, path in APIS.items():
        # 이 종류의 자료가 아직 하나도 없으면(첫 실행이거나 새로 승인된 API) 36개월치를 받는다
        months = args.months
        if not any((DATA_DIR / kind).rglob("*.json")):
            months = max(months, FIRST_RUN_MONTHS)
            print(f"{kind}: 저장된 자료가 없어 최근 {months}개월치를 받습니다.")
        for lawd_cd, gu in REGIONS.items():
            for ym in recent_months(months):
                total_calls += 1
                try:
                    recs = fetch_month(kind, path, key, lawd_cd, ym)
                    save_month(kind, lawd_cd, ym, recs)
                    print(f"[OK] {kind:10s} {gu} {ym}: {len(recs)}건")
                except Exception as e:
                    failures += 1
                    print(f"[실패] {kind:10s} {gu} {ym}: {e}")
                time.sleep(0.2)  # 서버에 부담 주지 않도록 잠깐 쉼

    print(f"완료. 실패 {failures}건")
    # 전부 실패했다면 키 문제일 가능성이 크므로 작업을 실패로 표시
    # (일부 API만 실패하면 403 = 해당 API 활용신청 미승인일 가능성이 큼)
    if failures == total_calls:
        sys.exit("모든 호출이 실패했습니다. 인증키나 활용신청 상태를 확인하세요.")


if __name__ == "__main__":
    main()
