"""
data/raw 에 쌓인 실거래 자료를 동네·주택유형·월별로 집계해서
대시보드가 읽는 data/summary.json 하나로 만드는 스크립트.

집계 지표 (월별)
- 매매: 거래 건수, 중위 거래가(만원), 중위 3.3㎡당 가격(만원)
- 전세: 건수, 중위 보증금, 중위 3.3㎡당 보증금
- 월세: 건수, 중위 보증금, 중위 월세
평균 대신 중위값(가운데 값)을 쓰는 이유: 거래가 적은 동네는 비싼 거래 한두 건에
평균이 크게 흔들리기 때문.
"""

import datetime as dt
import json
import statistics
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RAW_DIR = ROOT / "data" / "raw"
OUT_FILE = ROOT / "data" / "summary.json"

PYEONG = 3.3058  # 1평 = 3.3058㎡

# 대시보드에 보여줄 지역: (이름, 시군구코드, 동 이름 조건)
# 동 조건이 None 이면 구 전체
AREAS = [
    ("문래동",     "11560", lambda d: d.startswith("문래동")),  # 문래동1가~6가 합산
    ("도림동",     "11560", lambda d: d == "도림동"),
    ("신도림동",   "11530", lambda d: d == "신도림동"),
    ("영등포구 전체", "11560", None),
]

HOUSE_TYPES = {"apt": "아파트", "offi": "오피스텔", "rh": "연립다세대"}

RECENT_LIMIT = 30  # 지역·유형별로 보여줄 최근 거래 수


# ── 읽기 ───────────────────────────────────────────────────
def load_records(kind, lawd_cd):
    """한 종류·한 지역의 모든 월 파일을 읽어 하나의 리스트로."""
    folder = RAW_DIR / kind / lawd_cd
    records = []
    for f in sorted(folder.glob("*.json")):
        records.extend(json.loads(f.read_text(encoding="utf-8")))
    return records


def filter_area(records, cond):
    """동 이름 조건에 맞는 거래만 남긴다."""
    if cond is None:
        return records
    return [r for r in records if cond(r.get("dong", ""))]


# ── 계산 도우미 ─────────────────────────────────────────────
def median(values):
    """None 을 빼고 중위값. 값이 없으면 None. 만원 단위 정수로 반올림."""
    vals = [v for v in values if v is not None]
    return round(statistics.median(vals)) if vals else None


def per_pyeong(amount, area):
    """금액(만원)을 3.3㎡(1평)당 금액으로."""
    if amount is None or not area:
        return None
    return amount / (area / PYEONG)


def month_of(rec):
    """'2026-11-03' → '2026-11'"""
    return rec["date"][:7]


# ── 월별 집계 ──────────────────────────────────────────────
def monthly_trade(records):
    """매매: 해제된 거래를 빼고 월별 건수·중위가·평당 중위가."""
    by_month = {}
    for r in records:
        if r.get("cancelled"):
            continue
        by_month.setdefault(month_of(r), []).append(r)
    out = {}
    for m, rs in sorted(by_month.items()):
        out[m] = {
            "n": len(rs),
            "price": median(r["price"] for r in rs),
            "ppy": median(per_pyeong(r["price"], r.get("area")) for r in rs),
        }
    return out


def monthly_rent(records):
    """전월세: 월세가 0이면 전세, 아니면 월세로 나눠 월별 집계."""
    jeonse, wolse = {}, {}
    for r in records:
        target = jeonse if not r.get("rent") else wolse
        target.setdefault(month_of(r), []).append(r)

    j_out = {
        m: {
            "n": len(rs),
            "deposit": median(r["deposit"] for r in rs),
            "ppy": median(per_pyeong(r["deposit"], r.get("area")) for r in rs),
        }
        for m, rs in sorted(jeonse.items())
    }
    w_out = {
        m: {
            "n": len(rs),
            "deposit": median(r["deposit"] for r in rs),
            "rent": median(r["rent"] for r in rs),
        }
        for m, rs in sorted(wolse.items())
    }
    return j_out, w_out


def recent_list(records, kind):
    """최근 거래 목록 (날짜 최신순)."""
    rs = sorted(records, key=lambda r: r["date"], reverse=True)[:RECENT_LIMIT]
    out = []
    for r in rs:
        row = {"date": r["date"], "dong": r["dong"], "name": r["name"],
               "area": r.get("area"), "floor": r.get("floor")}
        if kind == "trade":
            row["price"] = r["price"]
            row["cancelled"] = r.get("cancelled", False)
        else:
            row["deposit"] = r["deposit"]
            row["rent"] = r.get("rent", 0)
            row["contract"] = r.get("contract", "")
        out.append(row)
    return out


# ── 전체 조립 ──────────────────────────────────────────────
def build():
    summary = {
        "updated": dt.datetime.now(dt.timezone(dt.timedelta(hours=9))).strftime("%Y-%m-%d %H:%M"),
        # 이 달 이후는 신고 기한(30일)이 안 지나 거래가 덜 잡혀 있다
        "incomplete_from": (dt.date.today() - dt.timedelta(days=30)).strftime("%Y-%m"),
        "areas": {},
    }

    for area_name, lawd_cd, cond in AREAS:
        area_obj = {}
        for ht, ht_name in HOUSE_TYPES.items():
            trades = filter_area(load_records(f"{ht}_trade", lawd_cd), cond)
            rents = filter_area(load_records(f"{ht}_rent", lawd_cd), cond)
            jeonse, wolse = monthly_rent(rents)
            area_obj[ht] = {
                "label": ht_name,
                "trade": monthly_trade(trades),
                "jeonse": jeonse,
                "wolse": wolse,
                "recent_trade": recent_list(trades, "trade"),
                "recent_rent": recent_list(rents, "rent"),
            }
        summary["areas"][area_name] = area_obj

    OUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    OUT_FILE.write_text(json.dumps(summary, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    print(f"summary.json 생성: {OUT_FILE.stat().st_size / 1024:.0f} KB")


if __name__ == "__main__":
    build()
