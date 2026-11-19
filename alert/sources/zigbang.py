"""직방 아파트 매매 매물.

직방은 호갱노노와 같은 회사(직방이 2018년 인수)라 아파트 중개 매물 풀을 공유합니다.
흐름: 지역 중심좌표(검색 API) → geohash 격자별 단지 목록(주 1회 캐시) → 단지별 매매 매물.
"""
from __future__ import annotations

import json
import re
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import requests

from ..common import Listing, PoliteSession, log

API = "https://apis.zigbang.com"
DANJI_TTL = 7 * 86400
_B32 = "0123456789bcdefghjkmnpqrstuvwxyz"
CELL = 0.0439  # geohash 5자리 격자 한 칸(약 4.9km)
WORKERS = 4
MIN_SUPPLY_M2 = 95   # 공급면적 최대값이 이보다 작으면 전용 84㎡ 평형이 없는 단지


def _max_supply_m2(catalog: dict) -> int:
    best = 0
    for t in catalog.get("danjiRoomTypes", []):
        m = re.match(r"\d+", str((t.get("roomTypeTitle") or {}).get("m2", "")))
        if m:
            best = max(best, int(m.group()))
    return best or 999


def geohash(lat: float, lon: float, prec: int = 5) -> str:
    la, lo = [-90.0, 90.0], [-180.0, 180.0]
    out, bit, ch, even = "", 0, 0, True
    while len(out) < prec:
        rng, v = (lo, lon) if even else (la, lat)
        mid = (rng[0] + rng[1]) / 2
        if v > mid:
            ch, rng[0] = ch * 2 + 1, mid
        else:
            ch, rng[1] = ch * 2, mid
        even, bit = not even, bit + 1
        if bit == 5:
            out, bit, ch = out + _B32[ch], 0, 0
    return out


def _gugun(sigungu: str) -> str:
    """'수원시 영통구' -> '영통구', '군포시' -> '군포시' (직방 gugun 표기)."""
    return sigungu.split()[-1]


class Zigbang:
    name = "zigbang"

    def __init__(self, cfg: dict, state_dir: Path):
        self.cfg = cfg
        self.http = PoliteSession(min_interval=0.2)
        self.cache_path = state_dir / "zigbang_danjis.json"
        try:
            self.cache = json.loads(self.cache_path.read_text("utf-8"))
        except Exception:
            self.cache = {}
        self.cache.setdefault("regions", {})
        self.cache.setdefault("details", {})
        self.cache.setdefault("roomtypes", {})   # 단지별 최대 공급면적 (84㎡ 평형 없는 단지 건너뛰기)
        self.pool = PoliteSession(min_interval=0)
        self.pool.mount("https://", requests.adapters.HTTPAdapter(pool_maxsize=WORKERS, max_retries=2))

    def _save(self):
        self.cache_path.write_text(json.dumps(self.cache, ensure_ascii=False), "utf-8")

    # ── 단지 목록 ─────────────────────────────────────────
    def _center(self, region: dict) -> tuple[float, float]:
        q = region["dongs"][0] if region.get("dongs") else region["sigungu"]  # 세종처럼 시군구가 없으면 동으로 검색
        r = self.http.get(f"{API}/v2/search",
                          params={"q": q, "serviceType": "아파트", "leaseYn": "N"}).json()
        for it in r.get("items", []):
            desc = it.get("description", "")
            if it.get("type") == "address" and region["sigungu"] in desc and region["sido"] in desc:
                return it["lat"], it["lng"]
        raise RuntimeError(f"직방에서 지역 좌표를 못 찾음: {region['name']}")

    def _danjis(self, region: dict) -> list[dict]:
        key = region["name"]
        ent = self.cache["regions"].get(key)
        if ent and time.time() - ent["t"] < DANJI_TTL:
            return ent["danjis"]
        lat, lng = self._center(region)
        radius = 1 if region.get("dongs") else 2
        cells = {geohash(lat + i * CELL, lng + j * CELL)
                 for i in range(-radius, radius + 1) for j in range(-radius, radius + 1)}
        gugun = region.get("zigbang_gugun") or _gugun(region["sigungu"])
        dongs = set(region.get("dongs") or [])
        found = {}
        for c in sorted(cells):
            d = self.http.get(f"{API}/apt/locals/prices/on-danjis", params={"geohash": c}).json()
            for x in d.get("filtered", []) + d.get("unfiltered", []):
                if x.get("sido") != region["sido"] or x.get("gugun") != gugun:
                    continue
                if dongs and x.get("dong") not in dongs:
                    continue
                sales_min = ((x.get("price") or {}).get("sales") or {}).get("min")
                found[x["id"]] = {"id": x["id"], "name": x["name"], "dong": x["dong"],
                                  "sales_min": sales_min}
        danjis = list(found.values())
        self.cache["regions"][key] = {"t": time.time(), "danjis": danjis}
        self._save()
        log.info("직방 %s: 단지 %d곳 갱신", key, len(danjis))
        return danjis

    def _catalog(self, dj: dict) -> dict | None:
        try:
            r = self.pool.get(f"{API}/apt/danjis/{dj['id']}/item-catalogs",
                              params={"tranTypeIn[]": "trade", "limit": 500, "offset": 0}, timeout=20)
            r.raise_for_status()
            return r.json()
        except Exception as e:
            log.warning("직방 단지 %s 매물 조회 실패: %s", dj["id"], e)
            return None

    def _detail(self, danji_id: int) -> dict:
        k = str(danji_id)
        if k not in self.cache["details"]:
            d = self.http.get(f"{API}/v2/danjis/{danji_id}").json()
            self.cache["details"][k] = {"bcode": d.get("bcode", ""), "bunji": d.get("bunji", ""),
                                        "local3": d.get("local3", "")}
        return self.cache["details"][k]

    # ── 매물 ──────────────────────────────────────────────
    def fetch(self, regions: list[dict], flt: dict) -> list[Listing]:
        max_price, min_area = flt["max_price"], flt["min_area"]
        # 가장 싼 실거래가 이 값보다 비싼 단지는 '9억 이하 84㎡ 매물'이 나올 가능성이 사실상 없음
        skip_above = max_price * 1.35
        out: list[Listing] = []
        for region in regions:
            try:
                danjis = self._danjis(region)
            except Exception as e:
                log.warning("직방 %s 단지 목록 실패: %s", region["name"], e)
                continue
            rt = self.cache["roomtypes"]
            only = [k.replace(" ", "") for k in region.get("danjis") or []]
            cands = [d for d in danjis
                     if (not only or any(k in d["name"].replace(" ", "") for k in only))
                     and (not d["sales_min"] or d["sales_min"] <= skip_above)
                     and rt.get(str(d["id"]), 999) >= MIN_SUPPLY_M2]
            with ThreadPoolExecutor(max_workers=WORKERS) as ex:
                results = list(ex.map(self._catalog, cands))
            n_items = 0
            for dj, data in zip(cands, results):
                if data is None:
                    continue
                rt[str(dj["id"])] = _max_supply_m2(data)
                for it in data.get("list", []):
                    n_items += 1
                    price, area = it.get("depositMin") or 0, it.get("sizeM2") or 0
                    if it.get("tranType") != "trade" or not price or price > max_price or area < min_area:
                        continue
                    det = self._detail(dj["id"])
                    ids = [str(x["itemId"]) for x in it.get("itemIdList", [])]
                    out.append(Listing(
                        source=self.name,
                        id=str(it.get("areaHoId") or ids[0]),
                        name=it.get("areaDanjiName") or dj["name"],
                        sido=region["sido"], sigungu=region["sigungu"],
                        dong=it.get("local3") or dj["dong"],
                        area=float(area), price=int(price),
                        url=f"https://www.zigbang.com/home/apt/danjis/{dj['id']}",
                        floor=it.get("floor") or "", building=it.get("dong") or "",
                        title=it.get("itemTitle") or "",
                        bcode=det["bcode"], jibun=det["bunji"], region=region["name"],
                        extra={"item_ids": ids, "price_range": it.get("isPriceRange")},
                    ))
            log.info("직방 %s: 후보단지 %d/%d, 매매매물 %d건 확인", region["name"],
                     len(cands), len(danjis), n_items)
        self._save()
        return out
