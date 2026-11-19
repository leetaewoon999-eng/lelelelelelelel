"""당근 부동산 (realty.daangn.com).

동 단위 지도 페이지에 최신 매물 20건이 서버 렌더링되어 있어 이를 파싱합니다.
(30분~1시간 주기로 돌리면 새 매물을 놓칠 일은 거의 없음)
아파트 매매 후보만 상세 페이지를 열어 단지명·지번·층을 가져옵니다.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from urllib.parse import quote

from ..common import Listing, PoliteSession, log

BASE = "https://realty.daangn.com"
_ART = re.compile(
    r'originalId:"(\d+)",salesType:"(\w+)",tradeType:"(\w+)",tradePrice:(\d+),'
    r'monthlyPay:(\d+),area:"([\d.]+)",regionName3:"([^"]*)"')
_COMPLEX = re.compile(r'__typename:"PropComplex",id:"[^"]*",type:"\w+",name:"([^"]*)"')
_BLDG = re.compile(r'buildingName:"([^"]*)"')
_JIBUN = re.compile(r'jibunAddress:"([^"]+)"')
_FLOOR = re.compile(r'floor:"([\d.]+)",topFloor:"?(\d*)')
_AREA = re.compile(r'area:"([\d.]+)",supplyArea')


def parse_list(html: str) -> list[dict]:
    seen, out = set(), []
    for oid, st, tt, price, monthly, area, dong in _ART.findall(html):
        if oid in seen:
            continue
        seen.add(oid)
        out.append({"id": oid, "sales_type": st, "trade_type": tt, "price": int(price),
                    "area": float(area), "dong": dong})
    return out


def parse_detail(html: str) -> dict:
    g = lambda rx: (rx.search(html).groups() if rx.search(html) else None)
    complex_name = (g(_COMPLEX) or [""])[0]
    bldg = (g(_BLDG) or [""])[0]
    jibun_addr = (g(_JIBUN) or [""])[0]
    floor = g(_FLOOR)
    area = (g(_AREA) or [None])[0]
    jibun, dong = "", ""
    toks = jibun_addr.split()
    if toks and re.fullmatch(r"(산)?\d+(-\d+)?", toks[-1]):
        jibun = toks[-1]
        dong = toks[-2] if len(toks) >= 2 else ""
    fl = ""
    if floor:
        fl = str(int(float(floor[0]))) + (f"/{floor[1]}" if floor[1] else "")
    return {"complex": complex_name, "building_name": bldg, "jibun_address": jibun_addr,
            "jibun": jibun, "dong": dong, "floor": fl,
            "area": float(area) if area else None}


class Daangn:
    name = "daangn"

    def __init__(self, cfg: dict, state_dir: Path):
        self.http = PoliteSession(min_interval=0.4)
        self.cache_path = state_dir / "daangn_details.json"
        try:
            self.details = json.loads(self.cache_path.read_text("utf-8"))
        except Exception:
            self.details = {}

    def _detail(self, oid: str) -> dict:
        if oid not in self.details:
            html = self.http.get(f"{BASE}/articles/{oid}").text
            self.details[oid] = parse_detail(html)
        return self.details[oid]

    def fetch(self, regions: list[dict], flt: dict, dongs_by_region: dict[str, list[str]]) -> list[Listing]:
        out: list[Listing] = []
        done: set[str] = set()       # 인접 동 페이지에 같은 매물이 중복 노출됨
        for region in regions:
            dongs = region.get("dongs") or dongs_by_region.get(region["name"], [])
            n = 0
            for dong in dongs:
                url = f"{BASE}/map/{quote(region['sido'])}/{quote(region['sigungu'])}/{quote(dong)}"
                try:
                    r = self.http.get(url)
                    if r.status_code != 200:
                        continue
                    arts = parse_list(r.text)
                except Exception as e:
                    log.warning("당근 %s %s 실패: %s", region["name"], dong, e)
                    continue
                n += len(arts)
                for a in arts:
                    if a["sales_type"] != "APART" or a["trade_type"] != "BUY":
                        continue
                    if a["price"] > flt["max_price"] or a["area"] < flt["min_area"] or a["id"] in done:
                        continue
                    done.add(a["id"])
                    try:
                        d = self._detail(a["id"])
                    except Exception as e:
                        log.warning("당근 매물 %s 상세 실패: %s", a["id"], e)
                        continue
                    # 상세 주소가 다른 시군구면 제외 (지도 페이지에 인접 지역 매물이 섞여 나옴)
                    if d["jibun_address"] and region["sigungu"].split()[-1] not in d["jibun_address"]:
                        continue
                    if region.get("dongs") and (d["dong"] or a["dong"]) not in region["dongs"]:
                        continue
                    name = d["complex"] or d["building_name"]
                    if d["building_name"] and d["building_name"] != name:
                        name = f"{name} ({d['building_name']})"
                    out.append(Listing(
                        source=self.name, id=a["id"], name=name,
                        sido=region["sido"], sigungu=region["sigungu"],
                        dong=d["dong"] or a["dong"], area=d["area"] or a["area"],
                        price=a["price"], url=f"{BASE}/articles/{a['id']}",
                        floor=d["floor"], jibun=d["jibun"], region=region["name"],
                    ))
            log.info("당근 %s: 동 %d곳, 매물 %d건 확인", region["name"], len(dongs), n)
        # 상세 캐시는 최근 3000건만 유지
        if len(self.details) > 3000:
            self.details = dict(list(self.details.items())[-3000:])
        self.cache_path.write_text(json.dumps(self.details, ensure_ascii=False), "utf-8")
        return out
