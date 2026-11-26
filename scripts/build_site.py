"""
data/agg (월별 집계)와 data/recent (최근 거래)를 모아
대시보드가 읽는 파일을 data/site/ 에 만든다.

  data/site/index.json   : 갱신 시각, 구 목록과 구별 동 목록
  data/site/seoul.json   : 서울 전체 추이 + 구별 추이(비교표용) + 서울 최근 거래
  data/site/<구코드>.json : 구 전체 추이 + 동별 추이 + 그 구의 최근 거래

화면은 처음에 index·seoul 만 받고, 구를 고르면 그 구 파일만 받는다 → 빠르게 열림.
"""

import datetime as dt
import json
from pathlib import Path

from fetch_data import APIS, SEOUL_GU, dong_key

ROOT = Path(__file__).resolve().parent.parent
AGG_DIR = ROOT / "data" / "agg"
RECENT_DIR = ROOT / "data" / "recent"
SITE_DIR = ROOT / "data" / "site"

HOUSE_TYPES = ["apt", "offi", "rh"]
SEOUL_RECENT = 150  # 서울 전체 화면용 최근 거래 수 (평형대로 걸러도 충분히 남도록)


def read_json(path, default=None):
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else default


def write_json(path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")


def empty_series():
    """{주택유형: {거래유형: {월: 통계}}} 틀"""
    return {ht: {"trade": {}, "jeonse": {}, "wolse": {}} for ht in HOUSE_TYPES}


def load_months():
    """data/agg 의 모든 월 파일을 (주택유형, 월, 내용)으로 돌려준다."""
    for kind in APIS:
        ht = kind.split("_")[0]
        for f in sorted((AGG_DIR / kind).glob("*.json")):
            yield ht, read_json(f)


def build():
    seoul = empty_series()
    gu_series = {code: empty_series() for code in SEOUL_GU}
    dong_series = {code: {} for code in SEOUL_GU}

    # 예전 형식(평형 구분 없이 통계 하나)이 섞여 있으면 화면에 평형 버튼을 숨기기 위해 표시
    all_banded = True

    def as_bands(st):
        """예전 형식 {'n':..} 은 {'all': {'n':..}} 으로 맞춘다."""
        nonlocal all_banded
        if "n" in st:
            all_banded = False
            return {"all": st}
        return st

    # ── 월별 집계를 지역별 시계열로 재배치 ──
    for ht, m in load_months():
        month = f"{m['ym'][:4]}-{m['ym'][4:]}"
        for deal in ("trade", "jeonse", "wolse"):
            block = m.get(deal)
            if not block:
                continue
            if block.get("seoul"):
                seoul[ht][deal][month] = as_bands(block["seoul"])
            for code, st in block.get("gu", {}).items():
                gu_series[code][ht][deal][month] = as_bands(st)
            for code, dongs in block.get("dong", {}).items():
                for dong, st in dongs.items():
                    dong_series[code].setdefault(dong, empty_series())[ht][deal][month] = as_bands(st)

    # ── 최근 거래 목록 ──
    recent = {code: {ht: {"trade": [], "rent": []} for ht in HOUSE_TYPES} for code in SEOUL_GU}
    for kind in APIS:
        ht, side = kind.split("_")
        for code in SEOUL_GU:
            recs = read_json(RECENT_DIR / kind / f"{code}.json", [])
            for r in recs:
                r["gu"] = SEOUL_GU[code]
            recent[code][ht]["trade" if side == "trade" else "rent"] = recs

    # ── 구별 파일 ──
    index_gu = []
    for code, name in SEOUL_GU.items():
        # 동 목록: 3년간 아파트 매매가 많은 순 → 많이 찾는 동이 위로
        def weight(d):
            s = dong_series[code][d]
            return sum(v["all"]["n"] for ht in HOUSE_TYPES for deal in s[ht] for v in s[ht][deal].values()
                       if "all" in v)
        dongs = sorted(dong_series[code], key=weight, reverse=True)
        index_gu.append({"code": code, "name": name, "dongs": dongs})
        write_json(SITE_DIR / f"{code}.json", {
            "series": gu_series[code],
            "children": {d: dong_series[code][d] for d in dongs},
            "recent": recent[code],
        })

    # ── 서울 파일 ──
    seoul_recent = {}
    for ht in HOUSE_TYPES:
        seoul_recent[ht] = {}
        for side in ("trade", "rent"):
            allr = [r for code in SEOUL_GU for r in recent[code][ht][side]]
            seoul_recent[ht][side] = sorted(allr, key=lambda r: r["date"], reverse=True)[:SEOUL_RECENT]
    write_json(SITE_DIR / "seoul.json", {
        "series": seoul,
        "children": {code: gu_series[code] for code in SEOUL_GU},
        "recent": seoul_recent,
    })

    now = dt.datetime.now(dt.timezone(dt.timedelta(hours=9)))
    write_json(SITE_DIR / "index.json", {
        "updated": now.strftime("%Y-%m-%d %H:%M"),
        # 이 달부터는 신고 기한(30일)이 안 지나 거래가 덜 잡혀 있다
        "incomplete_from": (now.date() - dt.timedelta(days=30)).strftime("%Y-%m"),
        "gu": index_gu,
        # True 이면 화면에 평형대(소형·59㎡형·84㎡형·대형) 버튼을 보여준다
        "bands": all_banded,
    })
    size = sum(f.stat().st_size for f in SITE_DIR.glob("*.json")) / 1024
    print(f"data/site 생성: 파일 {len(list(SITE_DIR.glob('*.json')))}개, {size:,.0f} KB")


if __name__ == "__main__":
    build()
