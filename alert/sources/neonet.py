"""부동산뱅크 (m.neonet.co.kr) 모바일 매물 페이지.

흐름: 시군구 → 법정동 목록 → 동별 단지 목록(단지별 매물 수) → 매물 수가 바뀐 단지만 매물 목록 조회.
사이트가 연속 요청을 끊는 편이라 1초 간격, 한 번 실행에 페이지 수 상한(budget)을 둡니다.
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path

from ..common import Listing, PoliteSession, log

BASE = "https://m.neonet.co.kr/novo-mobile/view/offerings/"
BUDGET = 150                  # 한 번 실행에 매물 목록 페이지 최대 요청 수
DONG_BUDGET = 40              # 한 번 실행에 확인할 동 수 (나머지는 다음 실행에)
_DONG = re.compile(r'region_cd=(\d{10})["\'][^>]*>\s*([^<\s][^<]*?)\s*<')
_CPLX = re.compile(r'complex_cd=(\w+)["\'][^>]*>(.{0,300}?)</a>', re.S)
_PRICE = re.compile(r"(매매|전세|월세)\s*([\d,]+)(?:\s*/\s*([\d,]+))?\s*만")
_AREA = re.compile(r"([\d.]+)\s*/\s*([\d.]+)\s*㎡\s*,\s*([^,|]*)")


def _text(html: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " | ", html))


def parse_cards(html: str) -> list[dict]:
    out = []
    for chunk in html.split("OfferingsDetail.neo?is_premium=&offerings_cd=")[1:]:
        oid = re.match(r"(\d+)", chunk)
        t = _text(chunk[:2500])
        parts = [p.strip() for p in t.split("|") if p.strip()]
        p, a = _PRICE.search(t), _AREA.search(t)
        if not (oid and p and a):
            continue
        name = parts[1] if len(parts) > 1 else ""
        bld = ""
        m = re.match(r"(.*?)\s+(\S+동)$", name)
        if m:
            name, bld = m.group(1), m.group(2)
        kind, v1, v2 = p.group(1), int(p.group(2).replace(",", "")), p.group(3)
        out.append({"id": oid.group(1), "name": name, "building": bld, "kind": kind,
                    "deposit": v1, "rent": int(v2.replace(",", "")) if v2 else 0,
                    "supply": float(a.group(1)), "excl": float(a.group(2)),
                    "floor": a.group(3).strip()})
    return out


class Neonet:
    name = "neonet"

    def __init__(self, cfg: dict, state_dir: Path):
        self.http = PoliteSession(min_interval=1.0)
        self.cache_path = state_dir / "neonet.json"
        try:
            self.cache = json.loads(self.cache_path.read_text("utf-8"))
        except Exception:
            self.cache = {}
        self.cache.setdefault("counts", {})     # 단지별 마지막으로 읽은 매물 수
        self.cache.setdefault("dongs", {})
        self.budget = BUDGET
        self.run: dict[str, tuple[str, list[dict]]] = {}    # 이번 실행에 읽은 단지 (매매·반전세 공용)
        self.cplx_run: dict[str, list] = {}

    def _get(self, path: str, **params) -> str:
        r = self.http.get(BASE + path, params={"is_premium": "", "offer_gbn": "P", **params})
        r.encoding = "euc-kr"
        return r.text

    def detail_url(self, oid: str, gbn: str = "AT") -> str:
        return f"{BASE}OfferingsDetail.neo?offerings_cd={oid}&offerings_gbn={gbn}"

    def save(self):
        self.cache_path.write_text(json.dumps(self.cache, ensure_ascii=False), "utf-8")

    def dongs(self, lawd: str, gbn: str = "AT") -> list[tuple[str, str]]:
        k = f"{lawd}:{gbn}"
        ent = self.cache["dongs"].get(k)
        if not ent or time.time() - ent["t"] > 7 * 86400:
            html = self._get("OfferingsScodeList.neo", offerings_gbn=gbn, region_cd=lawd + "00000")
            lst = sorted({(c, n.strip()) for c, n in _DONG.findall(html)
                          if c.startswith(lawd) and not c.endswith("00000")})
            ent = self.cache["dongs"][k] = {"t": time.time(), "list": lst}
        return [tuple(x) for x in ent["list"]]

    def complexes(self, dong_cd: str, gbn: str) -> list[tuple[str, str, int]]:
        k = f"{gbn}:{dong_cd}"
        if k not in self.cplx_run:
            self.cplx_run[k] = self._complexes(dong_cd, gbn)
        return self.cplx_run[k]

    def _complexes(self, dong_cd: str, gbn: str) -> list[tuple[str, str, int]]:
        html = self._get("OfferingsComplexList.neo", offerings_gbn=gbn, region_cd=dong_cd)
        out = []
        for cd, body in _CPLX.findall(html):
            t = re.sub(r"<[^>]+>|\s+", " ", body).strip()
            m = re.search(r"^(.*?)\s*\((\d+)\)\s*$", t)
            if m:
                out.append((cd, m.group(1).strip(), int(m.group(2))))
        return out

    def listings(self, dong_cd: str, cd: str, gbn: str, count: int) -> list[dict]:
        cards: list[dict] = []
        for page in range(1, (count + 9) // 10 + 1):
            if self.budget <= 0:
                break
            self.budget -= 1
            html = self._get("OfferingsList.neo", offerings_gbn=gbn, region_cd=dong_cd,
                             complex_cd=cd, page=page)
            got = parse_cards(html)
            cards += got
            if len(got) < 10:
                break
        return cards

    def scan(self, lawd: str, dong_names: list[str] | None, gbn: str, name_filter=None):
        """시군구의 (지정) 동 전체를 훑음 → (동이름, 단지명, 카드)."""
        for dong_cd, dong in self.dongs(lawd, gbn):
            if dong_names and dong not in dong_names:
                continue
            yield from self.scan_dong(dong_cd, dong, gbn, name_filter)

    def scan_dong(self, dong_cd: str, dong: str, gbn: str, name_filter=None):
        """매물 수가 바뀐 단지만 읽음 (이번 실행에 읽은 단지는 재사용)."""
        try:
            cplx = self.complexes(dong_cd, gbn)
        except Exception as e:
            log.warning("부동산뱅크 %s 단지목록 실패: %s", dong, e)
            return
        self.cache.setdefault("checked", {})[f"{gbn}:{dong_cd}"] = time.time()
        for cd, cname, cnt in cplx:
            key = f"{gbn}:{cd}"
            if name_filter and not name_filter(cname):
                continue
            if key in self.run:
                for c in self.run[key][1]:
                    yield dong, cname, c
                continue
            if cnt == 0 or self.cache["counts"].get(key) == cnt:
                continue
            if self.budget <= 0:
                return
            try:
                cards = self.listings(dong_cd, cd, gbn, cnt)
            except Exception as e:
                log.warning("부동산뱅크 %s 매물 실패: %s", cname, e)
                continue
            self.cache["counts"][key] = cnt
            self.run[key] = (cname, cards)
            for c in cards:
                yield dong, cname, c

    def fetch(self, regions: list[dict], flt: dict) -> list[Listing]:
        """동이 많아 한 번에 다 못 보므로, 오래전에 확인한 동부터 DONG_BUDGET 개씩 돌아가며 확인."""
        todo = []
        for region in regions:
            try:
                dongs = self.dongs(region["lawd_cd"], "AT")
            except Exception as e:
                log.warning("부동산뱅크 %s 동목록 실패: %s", region["name"], e)
                continue
            for dong_cd, dong in dongs:
                if region.get("dongs") and dong not in region["dongs"]:
                    continue
                last = self.cache.get("checked", {}).get(f"AT:{dong_cd}", 0)
                todo.append((last, dong_cd, dong, region))
        todo.sort(key=lambda x: x[0])
        out: list[Listing] = []
        n = 0
        for _, dong_cd, dong, region in todo[:DONG_BUDGET]:
            only = [k.replace(" ", "") for k in region.get("danjis") or []]
            nf = (lambda nm, only=only: any(k in nm.replace(" ", "") for k in only)) if only else None
            for _, cname, c in self.scan_dong(dong_cd, dong, "AT", nf):
                n += 1
                if c["kind"] != "매매" or c["deposit"] > flt["max_price"] or c["excl"] < flt["min_area"]:
                    continue
                out.append(Listing(
                    source=self.name, id=c["id"], name=c["name"] or cname,
                    sido=region["sido"], sigungu=region["sigungu"], dong=dong,
                    area=c["excl"], price=c["deposit"],
                    url=self.detail_url(c["id"]),
                    floor=c["floor"], building=c["building"], region=region["name"],
                ))
            if self.budget <= 0:
                break
        log.info("부동산뱅크: 동 %d/%d곳 확인, 새로 읽은 매물 %d건 (남은 요청 %d)",
                 min(len(todo), DONG_BUDGET), len(todo), n, self.budget)
        self.save()
        return out
