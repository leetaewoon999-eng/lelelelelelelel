"""반전세(보증금+월세) 감시: 단지별 '보증금 → 최대 월세' 표보다 싸게 나오면 알림.

표 사이 보증금은 직선 보간, 표 범위 밖은 양 끝 기울기로 연장합니다.
"""
from __future__ import annotations

import html
import re
from pathlib import Path

from .common import Listing, PoliteSession, log, norm_name
from .notify import SOURCE_KO
from .sources import zigbang as zb

PY = 3.3058


def _interp(table: list[list[int]], deposit: int) -> float:
    pts = sorted((int(d), float(r)) for d, r in table)
    if len(pts) == 1:
        return pts[0][1]
    if deposit <= pts[0][0]:
        (x0, y0), (x1, y1) = pts[0], pts[1]
    elif deposit >= pts[-1][0]:
        (x0, y0), (x1, y1) = pts[-2], pts[-1]
    else:
        for (x0, y0), (x1, y1) in zip(pts, pts[1:]):
            if x0 <= deposit <= x1:
                break
    return y0 + (y1 - y0) * (deposit - x0) / (x1 - x0)


class Complex:
    def __init__(self, c: dict, min_supply_m2: float):
        self.name = c["name"]
        self.aliases = [norm_name(a) for a in [c["name"], *c.get("aliases", [])]]
        self.zigbang_apt = c.get("zigbang_apt")
        self.zigbang_officetel = c.get("zigbang_officetel")
        self.min_excl = float(c["min_exclusive_py"]) * PY - 1.5 if c.get("min_exclusive_py") else 0.0
        self.min_supply = float(c.get("min_supply_m2", min_supply_m2))
        self.tables = c["tables"]

    def limit(self, deposit: int) -> float:
        """이 보증금일 때 알림을 보낼 최대 월세(만원)."""
        return max(_interp(t, deposit) for t in self.tables)

    def size_ok(self, excl: float, supply: float) -> bool:
        if excl and excl < self.min_excl:
            return False
        if supply and supply < self.min_supply:
            return False
        return True

    def matches(self, name: str) -> bool:
        """단지명 일치 ('광교' 접두어·괄호·공백 무시). 비슷한 다른 단지(…트라엘 등)는 제외."""
        n = _key(name)
        return bool(n) and any(n == _key(a) for a in self.aliases)


def _key(name: str) -> str:
    n = norm_name(name)
    for w in ("광교", "오피스텔"):
        n = n.replace(w, "")
    return n


class RentWatch:
    def __init__(self, cfg: dict):
        rw = cfg.get("rent_watch") or {}
        self.region = rw.get("region", {})
        self.complexes = [Complex(c, float(rw.get("min_supply_py", 29)) * PY - 1) for c in rw.get("complexes", [])]
        self.http = PoliteSession(min_interval=0.2)

    @property
    def enabled(self) -> bool:
        return bool(self.complexes)

    def find(self, name: str) -> Complex | None:
        for c in self.complexes:
            if c.matches(name):
                return c
        return None

    def _lst(self, src, id_, cx, deposit, rent, excl, supply, url, floor="", building="", title=""):
        return Listing(source=src, id=str(id_), name=cx.name, sido=self.region.get("sido", ""),
                       sigungu=self.region.get("sigungu", ""), dong="", area=excl or 0, price=int(deposit),
                       url=url, floor=str(floor or ""), building=building or "", title=title or "",
                       extra={"rent": int(rent), "supply": supply, "complex": cx})

    # ── 직방 ──────────────────────────────────────────────
    def zigbang(self) -> list[Listing]:
        out = []
        for cx in self.complexes:
            if not cx.zigbang_apt:
                continue
            try:
                d = self.http.get(f"{zb.API}/apt/danjis/{cx.zigbang_apt}/item-catalogs",
                                  params={"tranTypeIn[]": "rental", "limit": 500, "offset": 0}).json()
            except Exception as e:
                log.warning("직방 반전세 %s 실패: %s", cx.name, e)
                continue
            for it in d.get("list", []):
                if it.get("tranType") != "rental":
                    continue
                out.append(self._lst("zigbang", f"r{it.get('areaHoId')}", cx, it.get("depositMin") or 0,
                                     it.get("rentMin") or 0, it.get("sizeM2") or 0, it.get("sizeContractM2") or 0,
                                     f"https://www.zigbang.com/home/apt/danjis/{cx.zigbang_apt}",
                                     it.get("floor"), it.get("dong"), it.get("itemTitle")))
        # 오피스텔: 건물 좌표 주변 geohash 의 월세 매물 → 상세
        by_bld = {cx.zigbang_officetel: cx for cx in self.complexes if cx.zigbang_officetel}
        if by_bld:
            cells = set()
            for cx in by_bld.values():
                r = self.http.get(f"{zb.API}/v2/search",
                                  params={"q": cx.name, "serviceType": "오피스텔", "leaseYn": "N"}).json()
                for it in r.get("items", []):
                    if it.get("id") == cx.zigbang_officetel:
                        cells.add(zb.geohash(it["lat"], it["lng"]))
            ids = []
            for g in sorted(cells):
                try:
                    d = self.http.get(f"{zb.API}/house/property/v1/items/officetels",
                                      params={"geohash": g, "salesTypes[0]": "월세", "depositMin": 0, "rentMin": 0}).json()
                    ids += [(x["id"], x["buildingId"]) for x in d.get("items", []) if x.get("buildingId") in by_bld]
                except Exception as e:
                    log.warning("직방 오피스텔 목록 실패: %s", e)
            bld_of = dict(ids)
            for i in range(0, len(ids), 50):
                chunk = [x[0] for x in ids[i:i + 50]]
                try:
                    items = self.http.post(f"{zb.API}/house/property/v1/items/list",
                                           json={"itemIds": chunk}).json().get("items", [])
                except Exception as e:
                    log.warning("직방 오피스텔 상세 실패: %s", e)
                    continue
                for it in items:
                    cx = by_bld.get(bld_of.get(it.get("item_id")))
                    if not cx or it.get("sales_type") != "월세":
                        continue
                    excl = (it.get("전용면적") or {}).get("m2") or 0
                    supply = (it.get("계약면적") or {}).get("m2") or (it.get("공급면적") or {}).get("m2") or 0
                    out.append(self._lst("zigbang", f"o{it['item_id']}", cx, it.get("deposit") or 0,
                                         it.get("rent") or 0, excl, supply,
                                         f"https://www.zigbang.com/home/officetel/items/{it['item_id']}",
                                         it.get("floor_string"), "", it.get("title")))
        log.info("직방 반전세: 매물 %d건 확인", len(out))
        return out

    # ── 당근 ──────────────────────────────────────────────
    def daangn(self, dg) -> list[Listing]:
        from .sources.daangn import BASE, parse_list
        from urllib.parse import quote
        out = []
        for dong in self.region.get("dongs", []):
            parts = [self.region["sido"], self.region["sigungu"], dong]
            try:
                arts = parse_list(dg.page("/".join(quote(x) for x in parts if x)))
            except Exception as e:
                log.warning("당근 반전세 %s 실패: %s", dong, e)
                continue
            for a in arts:
                if a["sales_type"] not in ("APART", "OFFICETEL") or a["trade_type"] != "MONTH":
                    continue
                d = dg.detail(a["id"])
                cx = self.find(d["complex"]) or self.find(d["building_name"])
                if not cx:
                    continue
                out.append(self._lst("daangn", f"r{a['id']}", cx, a["price"], a["monthly"],
                                     d["area"] or a["area"], 0, f"{BASE}/articles/{a['id']}", d["floor"]))
        log.info("당근 반전세: 매물 %d건 확인", len(out))
        return out

    # ── 네이버 ────────────────────────────────────────────
    def naver(self, nv) -> list[Listing]:
        out = []
        reg = {"lawd_cd": self.region["lawd_cd"], "dongs": self.region.get("dongs")}
        for d in nv._dongs(reg):
            for a in nv.articles(d, {"rletTpCd": "APT:OPST", "tradTpCd": "B2"}):
                cx = self.find(a.get("atclNm", ""))
                if not cx:
                    continue
                try:
                    excl, supply = float(a.get("spc2") or 0), float(a.get("spc1") or 0)
                    deposit, rent = int(a.get("prc") or 0), int(a.get("rentPrc") or 0)
                except ValueError:
                    continue
                out.append(self._lst("naver", f"r{a['atclNo']}", cx, deposit, rent, excl, supply,
                                     f"https://m.land.naver.com/article/info/{a['atclNo']}",
                                     a.get("flrInfo"), a.get("bildNm"), a.get("atclFetrDesc")))
        log.info("네이버 반전세: 매물 %d건 확인", len(out))
        return out

    # ── 부동산뱅크 ────────────────────────────────────────
    def neonet(self, nn) -> list[Listing]:
        out = []
        for gbn in ("AT", "OP"):          # 아파트, 오피스텔
            for dong, cname, c in nn.scan(self.region["lawd_cd"], self.region.get("dongs"), gbn,
                                          lambda nm: self.find(nm) is not None):
                cx = self.find(c["name"]) or self.find(cname)
                if not cx or c["kind"] != "월세":
                    continue
                out.append(self._lst("neonet", f"r{c['id']}", cx, c["deposit"], c["rent"], c["excl"],
                                     c["supply"], f"{nn.detail_url(c['id'], gbn)}", c["floor"], c["building"]))
        nn.save()
        log.info("부동산뱅크 반전세: 매물 %d건 확인", len(out))
        return out

    # ── 판정 ──────────────────────────────────────────────
    def check(self, listings: list[Listing]) -> list[tuple[Listing, float]]:
        hits = []
        for l in listings:
            cx: Complex = l.extra["complex"]
            rent = l.extra["rent"]
            if rent <= 0 or not cx.size_ok(l.area, l.extra.get("supply") or 0):
                continue
            lim = cx.limit(l.price)
            if rent <= lim:
                hits.append((l, lim))
        return hits


def format_rent(l: Listing, lim: float) -> str:
    e = html.escape
    from .common import fmt_price
    rent = l.extra["rent"]
    size = f"전용 {l.area:g}㎡({l.area / PY:.0f}평)" if l.area else ""
    lines = [
        f"🔑 <b>[{SOURCE_KO.get(l.source, l.source)} 반전세] {e(l.name)}</b>",
        "📍 " + " · ".join(x for x in [size, e(l.building), f"{e(l.floor)}층" if l.floor and l.floor[-1].isdigit() else e(l.floor)] if x),
        f"💰 <b>보증금 {fmt_price(l.price)} / 월 {rent}만</b>  (기준 월 {lim:.0f}만" + (f" → {lim - rent:.0f}만 저렴)" if lim - rent >= 0.5 else " 이하)") + "",
    ]
    if l.title:
        lines.append(f"📝 {e(l.title[:80])}")
    lines.append(f'🔗 <a href="{e(l.url)}">매물 보기</a>')
    return "\n".join(lines)
