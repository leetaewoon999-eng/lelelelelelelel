"""네이버 부동산 (m.land.naver.com 모바일 지도 API).

⚠ 네이버는 해외·데이터센터 IP(GitHub Actions 포함 가능성 높음)를 연결 단계에서 끊습니다.
   국내 가정/사무실 PC에서 실행할 때 config.yaml 의 sources.naver 를 true 로 켜세요.
   개발 환경에서 차단되어 실제 응답으로 검증하지 못했으므로, 처음 켤 때
   `python -m alert --dry-run --only naver` 로 동작을 확인해 주세요.
"""
from __future__ import annotations

from pathlib import Path

from ..common import MOBILE_UA, Listing, PoliteSession, log

BASE = "https://m.land.naver.com"


class NaverBlocked(RuntimeError):
    pass


class Naver:
    name = "naver"

    def __init__(self, cfg: dict, state_dir: Path):
        self.http = PoliteSession(min_interval=1.0, ua=MOBILE_UA)
        self.http.headers.update({"Referer": f"{BASE}/", "Accept": "application/json, text/plain, */*"})

    def _json(self, path: str, params: dict) -> dict:
        try:
            r = self.http.get(f"{BASE}{path}", params=params, allow_redirects=False)
        except Exception as e:
            raise NaverBlocked(f"네이버 연결 실패(IP 차단 가능성): {e}") from e
        if r.status_code in (302, 307, 401, 403, 429):
            raise NaverBlocked(f"네이버 접근 제한 HTTP {r.status_code}")
        r.raise_for_status()
        return r.json()

    def _dongs(self, region: dict) -> list[dict]:
        sgg = region["lawd_cd"] + "00000"
        res = self._json("/map/getRegionList", {"cortarNo": sgg}).get("result", {})
        dongs = res.get("list", [])
        if region.get("dongs"):
            dongs = [d for d in dongs if d.get("CortarNm") in region["dongs"]]
        return dongs

    def articles(self, d: dict, params: dict):
        """법정동 하나의 매물 목록 (페이지 순회)."""
        lat, lon = float(d["MapYCrdn"]), float(d["MapXCrdn"])
        base = {"z": 15, "lat": lat, "lon": lon,
                "btm": lat - 0.03, "top": lat + 0.03, "lft": lon - 0.04, "rgt": lon + 0.04,
                "cortarNo": d["CortarNo"], "sort": "dateDesc", **params}
        for page in range(1, 31):
            data = self._json("/cluster/ajax/articleList", {**base, "page": page})
            body = data.get("body") or []
            yield from body
            if not data.get("more") or not body:
                break

    def fetch(self, regions: list[dict], flt: dict) -> list[Listing]:
        out: list[Listing] = []
        for region in regions:
            n = 0
            for d in self._dongs(region):
                for a in self.articles(d, {"rletTpCd": "APT", "tradTpCd": "A1", "dprcMax": flt["max_price"]}):
                    n += 1
                    try:
                        area = float(a.get("spc2") or 0)       # spc2 = 전용면적
                        price = int(a.get("prc") or 0)
                    except ValueError:
                        continue
                    if not price or price > flt["max_price"] or area < flt["min_area"]:
                        continue
                    out.append(Listing(
                        source=self.name, id=str(a["atclNo"]), name=a.get("atclNm", ""),
                        sido=region["sido"], sigungu=region["sigungu"],
                        dong=d.get("CortarNm", ""), area=area, price=price,
                        url=f"https://m.land.naver.com/article/info/{a['atclNo']}",
                        floor=a.get("flrInfo", ""), building=a.get("bildNm", ""),
                        title=a.get("atclFetrDesc", ""), bcode=str(a.get("cortarNo") or d["CortarNo"]),
                        region=region["name"],
                        extra={"realtor": a.get("rltrNm"), "same_addr": a.get("sameAddrCnt")},
                    ))
            log.info("네이버 %s: 매물 %d건 확인", region["name"], n)
        return out
