"""국토교통부 아파트 매매 실거래가 (공공데이터포털) 조회 + 캐시 + 매물 매칭."""
from __future__ import annotations

import difflib
import json
import os
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from urllib.parse import unquote

from .common import Listing, PoliteSession, log, name_tokens, norm_name

API = "https://apis.data.go.kr/1613000/RTMSDataSvcAptTrade/getRTMSDataSvcAptTrade"
RECENT_REFRESH_SEC = 6 * 3600   # 최근 2개월치는 6시간마다 다시 받음 (신고 지연 반영)


@dataclass
class Trade:
    sgg: str
    umd_cd: str
    umd: str
    jibun: str
    apt: str
    area: float
    price: int      # 만원
    ymd: str        # YYYY-MM-DD
    floor: str

    @staticmethod
    def from_row(r: list) -> "Trade":
        return Trade(*r)

    def row(self) -> list:
        return [self.sgg, self.umd_cd, self.umd, self.jibun, self.apt,
                self.area, self.price, self.ymd, self.floor]


def _months_back(n: int, today: date | None = None) -> list[str]:
    d = today or date.today()
    y, m = d.year, d.month
    out = []
    for _ in range(n):
        out.append(f"{y}{m:02d}")
        m -= 1
        if m == 0:
            y, m = y - 1, 12
    return out


class RealPriceDB:
    def __init__(self, service_key: str, cache_dir: Path, months: int, fallback_months: int):
        self.key = service_key
        self.cache_path = cache_dir / "molit_cache.json"
        self.months = months
        self.fallback_months = max(months, fallback_months)
        self.http = PoliteSession(min_interval=0.1)
        self.cache: dict = {}
        if self.cache_path.exists():
            try:
                self.cache = json.loads(self.cache_path.read_text("utf-8"))
            except Exception:
                self.cache = {}
        self.by_sgg: dict[str, list[Trade]] = {}

    # ── 수집 ──────────────────────────────────────────────
    def _fetch_month(self, lawd: str, ym: str) -> list[list]:
        rows, page = [], 1
        while True:
            r = self.http.get(API, params={
                "serviceKey": self.key, "LAWD_CD": lawd, "DEAL_YMD": ym,
                "pageNo": page, "numOfRows": 1000,
            })
            root = ET.fromstring(r.content)
            code = root.findtext(".//resultCode") or root.findtext(".//returnReasonCode")
            if code not in ("00", "000"):
                msg = root.findtext(".//resultMsg") or root.findtext(".//returnAuthMsg") or r.text[:200]
                raise RuntimeError(f"실거래가 API 오류 ({lawd} {ym}): {code} {msg}")
            items = root.findall(".//item")
            for it in items:
                g = lambda t: (it.findtext(t) or "").strip()
                if g("cdealType"):           # 해제(취소)된 거래 제외
                    continue
                try:
                    price = int(g("dealAmount").replace(",", ""))
                    area = float(g("excluUseAr"))
                except ValueError:
                    continue
                ymd = f"{g('dealYear')}-{int(g('dealMonth') or 0):02d}-{int(g('dealDay') or 0):02d}"
                rows.append([g("sggCd") or lawd, g("umdCd"), g("umdNm"), g("jibun"),
                             g("aptNm"), area, price, ymd, g("floor")])
            total = int(root.findtext(".//totalCount") or 0)
            if page * 1000 >= total or not items:
                break
            page += 1
        return rows

    def load(self, lawd_codes: list[str]) -> None:
        now = time.time()
        months = _months_back(self.fallback_months)
        recent = set(months[:2])
        fetched = 0
        for lawd in lawd_codes:
            trades: list[Trade] = []
            for ym in months:
                ck = f"{lawd}:{ym}"
                ent = self.cache.get(ck)
                stale = ent is None or (ym in recent and now - ent["t"] > RECENT_REFRESH_SEC)
                if stale:
                    try:
                        rows = self._fetch_month(lawd, ym)
                        self.cache[ck] = ent = {"t": now, "rows": rows}
                        fetched += 1
                    except Exception as e:
                        log.warning("%s", e)
                        if ent is None:
                            continue
                trades.extend(Trade.from_row(r) for r in ent["rows"])
            self.by_sgg[lawd] = trades
        # 오래된 캐시 정리
        keep = {f"{l}:{m}" for l in lawd_codes for m in months}
        self.cache = {k: v for k, v in self.cache.items() if k in keep}
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        self.cache_path.write_text(json.dumps(self.cache, ensure_ascii=False), "utf-8")
        log.info("실거래가: API %d건 호출, 시군구 %d곳 로드 (총 %d건)", fetched, len(lawd_codes),
                 sum(len(v) for v in self.by_sgg.values()))

    # ── 매칭 ──────────────────────────────────────────────
    def _complex_trades(self, lst: Listing, lawd: str) -> tuple[list[Trade], str]:
        """매물과 같은 단지의 거래 목록과 매칭 방식."""
        trades = self.by_sgg.get(lawd, [])
        umd_cd = lst.bcode[5:10] if len(lst.bcode) == 10 else ""
        same_dong = [t for t in trades if (umd_cd and t.umd_cd == umd_cd) or t.umd == lst.dong]

        # 1) 지번 일치 (직방/당근은 지번을 줌)
        if lst.jibun:
            hit = [t for t in same_dong if t.jibun == lst.jibun]
            if hit:
                return hit, "지번"

        # 2) 단지명 일치 / 포함 / 유사
        targets = name_tokens(lst.name)
        if not targets:
            return [], ""
        by_apt: dict[str, list[Trade]] = {}
        for t in same_dong:
            by_apt.setdefault(t.apt, []).append(t)
        best, best_score = None, 0.0
        for apt, ts in by_apt.items():
            a = norm_name(apt)
            if not a:
                continue
            for n in targets:
                if a == n:
                    score = 1.0
                elif len(n) >= 3 and len(a) >= 3 and (a in n or n in a):
                    score = 0.9
                else:
                    score = difflib.SequenceMatcher(None, a, n).ratio()
                if score > best_score:
                    best, best_score = apt, score
        if best and best_score >= 0.8:
            return by_apt[best], f"단지명({best_score:.2f})"
        return [], ""

    def baseline(self, lst: Listing, lawd: str, mode: str, tol: float) -> dict | None:
        """매물에 대응하는 실거래 기준가 정보. 없으면 None."""
        trades, how = self._complex_trades(lst, lawd)
        if not trades:
            return None
        # 면적이 가장 가까운 평형 그룹 선택
        areas = sorted({t.area for t in trades}, key=lambda a: abs(a - lst.area))
        if not areas or abs(areas[0] - lst.area) > tol:
            return None
        target = areas[0]
        same = [t for t in trades if abs(t.area - target) < 0.05]
        same.sort(key=lambda t: t.ymd, reverse=True)

        cutoff = _months_back(self.months)[-1]
        in_window = [t for t in same if t.ymd.replace("-", "")[:6] >= cutoff]
        recent = in_window or same      # 기간 내 거래가 없으면 fallback 기간까지
        if mode == "avg3":
            pick = recent[:3]
            base = sum(t.price for t in pick) / len(pick)
        elif mode == "max":
            pick = [max(recent, key=lambda t: t.price)]
            base = pick[0].price
        else:
            pick = recent[:1]
            base = pick[0].price
        return {
            "base": base,
            "mode": mode,
            "match": how,
            "apt": same[0].apt,
            "area": target,
            "trades": [t.row() for t in recent[:3]],
            "within_lookback": bool(in_window),
        }


def from_env(cache_dir: Path, cfg: dict) -> RealPriceDB:
    key = os.environ.get("MOLIT_API_KEY", "").strip()
    if "%" in key:                       # '인코딩' 키를 넣은 경우 디코딩 키로 변환
        key = unquote(key)
    if not key:
        raise SystemExit("MOLIT_API_KEY 환경변수(공공데이터포털 일반 인증키)가 필요합니다.")
    rp = cfg.get("real_price", {})
    return RealPriceDB(key, cache_dir, int(rp.get("lookback_months", 6)),
                       int(rp.get("fallback_months", 12)))
