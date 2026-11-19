"""공통: HTTP 세션, 매물 모델, 이름/면적 정규화."""
from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

log = logging.getLogger("alert")

DESKTOP_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
)
MOBILE_UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1"
)


class PoliteSession(requests.Session):
    """재시도 + 요청 간 최소 간격을 지키는 세션."""

    def __init__(self, min_interval: float = 0.25, ua: str = DESKTOP_UA):
        super().__init__()
        retry = Retry(total=3, backoff_factor=1.5,
                      status_forcelist=(429, 500, 502, 503, 504),
                      allowed_methods=("GET", "POST"))
        self.mount("https://", HTTPAdapter(max_retries=retry))
        self.headers.update({"User-Agent": ua, "Accept-Language": "ko-KR,ko;q=0.9"})
        self.min_interval = min_interval
        self._last = 0.0

    def request(self, *a, **kw):
        kw.setdefault("timeout", 20)
        wait = self.min_interval - (time.monotonic() - self._last)
        if wait > 0:
            time.sleep(wait)
        try:
            return super().request(*a, **kw)
        finally:
            self._last = time.monotonic()


@dataclass
class Listing:
    source: str               # zigbang / daangn / naver
    id: str                   # 소스 내 고유 ID
    name: str                 # 단지명
    sido: str
    sigungu: str
    dong: str                 # 법정동
    area: float               # 전용면적 ㎡
    price: int                # 만원
    url: str
    floor: str = ""
    building: str = ""        # 101동 등
    title: str = ""
    bcode: str = ""           # 법정동코드 10자리 (있으면 매칭 정확도↑)
    jibun: str = ""           # 지번 (있으면 매칭 정확도↑)
    region: str = ""          # config 상 지역 이름
    extra: dict = field(default_factory=dict)

    @property
    def key(self) -> str:
        return f"{self.source}:{self.id}"


_PAREN = re.compile(r"\(.*?\)|\[.*?\]")
_DROP = re.compile(r"아파트|apt|apartment|단지$|[\s\-·.,]")


def norm_name(name: str) -> str:
    """'래미안 대치 팰리스(1단지)' -> '래미안대치팰리스' 같은 비교용 이름."""
    s = _PAREN.sub("", name or "").lower()
    s = _DROP.sub("", s)
    return s


def name_tokens(name: str) -> set[str]:
    """괄호 안 내용도 포함한 정규화 토큰 집합 (예: '반포3차(상지리츠빌)')."""
    out = {norm_name(name)}
    for inner in re.findall(r"\((.*?)\)", name or ""):
        out.add(norm_name(inner))
    return {t for t in out if t}


def fmt_price(manwon: int | float) -> str:
    manwon = int(round(manwon))
    eok, rest = divmod(manwon, 10000)
    if eok and rest:
        return f"{eok}억 {rest:,}"
    if eok:
        return f"{eok}억"
    return f"{rest:,}만"
