"""
국토교통부 실거래가 API(공공데이터포털)로 서울 25개 구의 매매·전월세 자료를 받아
'월별 집계'만 data/agg/<종류>/<YYYYMM>.json 으로 저장하는 스크립트.

왜 집계만 저장하나?
  서울 전체 3년치 거래를 한 건씩 저장하면 수백 MB가 되어 저장소가 무거워진다.
  그래서 한 달치를 받으면 바로 '서울 전체 / 구별 / 동별' 중위값과 건수로 줄여서 저장한다.
  (최근 거래 목록만 구별로 따로 data/recent/ 에 남긴다)

동작 순서
  1) 최근 N개월(기본 3)은 매번 다시 받는다 → 늦게 신고된 거래 반영 (신고 기한 30일)
  2) 남은 시간 안에서, 아직 없는 과거 달(최대 36개월)을 최신 달부터 채운다
  3) 승인 안 된 API(403 등)는 그 종류만 건너뛰고 나머지는 계속 진행한다
"""

import argparse
import datetime as dt
import json
import os
import re
import shutil
import statistics
import sys
import time
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import requests

# ── 기본 설정 ──────────────────────────────────────────────
BASE_URL = "https://apis.data.go.kr/1613000"
ROOT = Path(__file__).resolve().parent.parent
AGG_DIR = ROOT / "data" / "agg"
RECENT_DIR = ROOT / "data" / "recent"

# 서울 25개 구 (법정동 코드 앞 5자리)
SEOUL_GU = {
    "11110": "종로구", "11140": "중구", "11170": "용산구", "11200": "성동구", "11215": "광진구",
    "11230": "동대문구", "11260": "중랑구", "11290": "성북구", "11305": "강북구", "11320": "도봉구",
    "11350": "노원구", "11380": "은평구", "11410": "서대문구", "11440": "마포구", "11470": "양천구",
    "11500": "강서구", "11530": "구로구", "11545": "금천구", "11560": "영등포구", "11590": "동작구",
    "11620": "관악구", "11650": "서초구", "11680": "강남구", "11710": "송파구", "11740": "강동구",
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

HISTORY_MONTHS = 36     # 보관할 기간
ROWS_PER_PAGE = 1000
WORKERS = 5             # 동시에 호출할 구 개수 (너무 많으면 서버가 거절할 수 있음)
RECENT_PER_GU = 150     # 구별로 남길 최근 거래 수
PYEONG = 3.3058         # 1평 = 3.3058㎡


class AuthError(Exception):
    """인증키 미등록·권한 없음(403 등) — 이 API는 이번 실행에서 건너뛴다."""


# ── 날짜 / 숫자 도우미 ──────────────────────────────────────
def recent_months(n):
    """오늘 기준 최근 n개월의 'YYYYMM' 목록 (최신 달이 맨 앞)."""
    today = dt.date.today()
    y, m = today.year, today.month
    out = []
    for _ in range(n):
        out.append(f"{y}{m:02d}")
        m -= 1
        if m == 0:
            y, m = y - 1, 12
    return out


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


def dong_key(name):
    """'문래동3가' → '문래동' 처럼 'n가'를 떼서 같은 동으로 묶는다."""
    return re.sub(r"\d+가$", "", (name or "").strip())


# ── API 호출 ───────────────────────────────────────────────
def check_result_code(root):
    """API 결과 코드가 정상(00/000)인지 확인. 인증 문제면 AuthError."""
    code = root.findtext(".//resultCode") or root.findtext(".//returnReasonCode")
    msg = root.findtext(".//resultMsg") or root.findtext(".//returnAuthMsg") or ""
    if code is None:
        raise RuntimeError(f"알 수 없는 응답: {ET.tostring(root)[:200]!r}")
    code = code.strip()
    if code in ("00", "000"):
        return
    if code in ("20", "22", "30", "31", "32"):  # 접근거부·트래픽초과·키 미등록·기한만료
        raise AuthError(f"API 오류 {code}: {msg}")
    raise RuntimeError(f"API 오류 {code}: {msg}")


def call_api(path, key, lawd_cd, ym, page):
    """API 한 번 호출 → XML 루트. 일시적 오류는 3번까지 재시도, 권한 오류는 즉시 중단."""
    params = {"serviceKey": key, "LAWD_CD": lawd_cd, "DEAL_YMD": ym,
              "pageNo": page, "numOfRows": ROWS_PER_PAGE}
    last_err = None
    for attempt in range(3):
        try:
            res = requests.get(f"{BASE_URL}/{path}", params=params, timeout=40)
            if res.status_code in (401, 403):
                raise AuthError(f"HTTP {res.status_code} (활용신청 승인 여부 확인)")
            res.raise_for_status()
            root = ET.fromstring(res.content)
            check_result_code(root)
            return root
        except AuthError:
            raise
        except Exception as e:
            last_err = e
            time.sleep(2 * (attempt + 1))
    # 오류 메시지에 인증키가 섞여 나오지 않도록 종류만 남긴다
    raise RuntimeError(f"{lawd_cd} {ym} p{page}: {type(last_err).__name__}")


# ── 응답 정리 ──────────────────────────────────────────────
def parse_item(item, kind):
    """XML <item> 하나 → 필요한 값만 담은 딕셔너리."""
    get = lambda tag: item.findtext(tag)
    y, m, d = to_int(get("dealYear")), to_int(get("dealMonth")), to_int(get("dealDay"))
    if not (y and m and d):
        return None
    rec = {
        "date": f"{y:04d}-{m:02d}-{d:02d}",
        "dong": (get("umdNm") or "").strip(),
        "name": (get("aptNm") or get("offiNm") or get("mhouseNm") or "").strip(),  # 종류마다 태그가 다름
        "area": to_float(get("excluUseAr")),
        "floor": to_int(get("floor")),
    }
    if kind.endswith("_trade"):
        rec["price"] = to_int(get("dealAmount"))                        # 만원
        rec["cancelled"] = (get("cdealType") or "").strip() == "O"     # 계약 해제
        if rec["price"] is None:
            return None
    else:
        rec["deposit"] = to_int(get("deposit"))                        # 보증금, 만원
        rec["rent"] = to_int(get("monthlyRent")) or 0                  # 월세, 만원 (0 = 전세)
        if rec["deposit"] is None:
            return None
    return rec


def fetch_gu_month(kind, key, lawd_cd, ym):
    """한 종류·한 구·한 달치를 페이지를 넘겨가며 모두 받는다."""
    path = APIS[kind]
    records, page = [], 1
    while True:
        root = call_api(path, key, lawd_cd, ym, page)
        items = root.findall(".//items/item")
        records += [r for r in (parse_item(it, kind) for it in items) if r]
        total = to_int(root.findtext(".//totalCount")) or 0
        if page * ROWS_PER_PAGE >= total or not items:
            break
        page += 1
    return records


# ── 집계 ───────────────────────────────────────────────────
def median(values):
    vals = [v for v in values if v is not None]
    return round(statistics.median(vals)) if vals else None


def per_pyeong(amount, area):
    return amount / (area / PYEONG) if amount is not None and area else None


def make_stats(kind, recs):
    """거래 묶음 → {'trade': {...}} 또는 {'jeonse': {...}, 'wolse': {...}}"""
    if kind.endswith("_trade"):
        rs = [r for r in recs if not r["cancelled"]]
        if not rs:
            return {}
        return {"trade": {"n": len(rs),
                          "price": median(r["price"] for r in rs),
                          "ppy": median(per_pyeong(r["price"], r["area"]) for r in rs)}}
    out = {}
    j = [r for r in recs if not r["rent"]]
    w = [r for r in recs if r["rent"]]
    if j:
        out["jeonse"] = {"n": len(j), "deposit": median(r["deposit"] for r in j),
                         "ppy": median(per_pyeong(r["deposit"], r["area"]) for r in j)}
    if w:
        out["wolse"] = {"n": len(w), "deposit": median(r["deposit"] for r in w),
                        "rent": median(r["rent"] for r in w)}
    return out


def aggregate_month(kind, ym, recs_by_gu):
    """한 달치(25개 구) → 서울 전체·구별·동별 집계."""
    deals = ["trade"] if kind.endswith("_trade") else ["jeonse", "wolse"]
    out = {"ym": ym, **{d: {"seoul": None, "gu": {}, "dong": {}} for d in deals}}

    def put(level_setter, stats):
        for d, st in stats.items():
            level_setter(out[d], st)

    all_recs = [r for rs in recs_by_gu.values() for r in rs]
    put(lambda o, st: o.__setitem__("seoul", st), make_stats(kind, all_recs))
    for code, rs in recs_by_gu.items():
        put(lambda o, st, c=code: o["gu"].__setitem__(c, st), make_stats(kind, rs))
        by_dong = {}
        for r in rs:
            by_dong.setdefault(dong_key(r["dong"]), []).append(r)
        for dong, drs in by_dong.items():
            put(lambda o, st, c=code, dn=dong: o["dong"].setdefault(c, {}).__setitem__(dn, st),
                make_stats(kind, drs))
    return out


# ── 저장 ───────────────────────────────────────────────────
def write_json(path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")


def save_recent(kind, recent_by_gu):
    """구별 최근 거래 목록 저장 (최신순)."""
    for code, recs in recent_by_gu.items():
        recs = sorted(recs, key=lambda r: r["date"], reverse=True)[:RECENT_PER_GU]
        write_json(RECENT_DIR / kind / f"{code}.json", recs)


def remove_legacy_files():
    """예전 방식(거래 원본 저장)의 파일은 지운다."""
    for p in [ROOT / "data" / "raw"]:
        if p.exists():
            shutil.rmtree(p)
    for p in [ROOT / "data" / "summary.json"]:
        if p.exists():
            p.unlink()


# ── 한 달치 처리 ────────────────────────────────────────────
def process_month(kind, ym, key, pool):
    """25개 구를 동시에 받아 집계. 모두 성공했을 때만 저장(빠진 구가 있으면 다음에 다시)."""
    futures = {code: pool.submit(fetch_gu_month, kind, key, code, ym) for code in SEOUL_GU}
    recs_by_gu, failed = {}, []
    for code, fu in futures.items():
        try:
            recs_by_gu[code] = fu.result()
        except AuthError:
            raise
        except Exception as e:
            failed.append(f"{SEOUL_GU[code]}({e})")
    if failed:
        print(f"[실패] {kind} {ym}: {', '.join(failed[:3])} 등 {len(failed)}개 구 → 저장 안 함")
        return None
    write_json(AGG_DIR / kind / f"{ym}.json", aggregate_month(kind, ym, recs_by_gu))
    print(f"[OK] {kind:10s} {ym}: {sum(len(v) for v in recs_by_gu.values()):,}건")
    return recs_by_gu


# ── 실행 ───────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--months", type=int, default=3, help="매번 다시 받을 최근 개월 수")
    parser.add_argument("--budget", type=int, default=50, help="과거 자료 채우기에 쓸 최대 시간(분)")
    args = parser.parse_args()

    key = os.environ.get("DATA_GO_KR_KEY", "").strip()
    if not key:
        sys.exit("환경변수 DATA_GO_KR_KEY 가 없습니다 (GitHub Secrets 확인).")

    remove_legacy_files()
    start = time.time()
    all_months = recent_months(HISTORY_MONTHS)
    refresh = all_months[:args.months]
    disabled, saved = set(), 0

    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        # 1) 최근 몇 달은 매번 새로 받기 + 최근 거래 목록 갱신
        for kind in APIS:
            recent_by_gu = {code: [] for code in SEOUL_GU}
            try:
                for ym in refresh:
                    got = process_month(kind, ym, key, pool)
                    if got:
                        saved += 1
                        for code, rs in got.items():
                            recent_by_gu[code] += rs
                save_recent(kind, recent_by_gu)
            except AuthError as e:
                disabled.add(kind)
                print(f"[건너뜀] {kind}: {e}")

        # 2) 남은 시간 동안 빠진 과거 달 채우기 (최신 달부터)
        for ym in all_months[args.months:]:
            for kind in APIS:
                if kind in disabled or (AGG_DIR / kind / f"{ym}.json").exists():
                    continue
                if (time.time() - start) / 60 > args.budget:
                    print(f"시간 예산({args.budget}분) 소진 — 나머지는 다음 실행 때 채웁니다.")
                    break
                try:
                    if process_month(kind, ym, key, pool):
                        saved += 1
                except AuthError as e:
                    disabled.add(kind)
                    print(f"[건너뜀] {kind}: {e}")
            else:
                continue
            break

    missing = sum(1 for k in APIS for ym in all_months if not (AGG_DIR / k / f"{ym}.json").exists())
    print(f"완료: 이번에 저장 {saved}개 달, 아직 빈 달 {missing}개, 건너뛴 종류 {sorted(disabled) or '없음'}")
    if len(disabled) == len(APIS):
        sys.exit("모든 API가 권한 오류입니다. 인증키나 활용신청 상태를 확인하세요.")


if __name__ == "__main__":
    main()
