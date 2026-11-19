"""텔레그램 알림."""
from __future__ import annotations

import html
import os

import requests

from .common import Listing, fmt_price, log

SOURCE_KO = {"zigbang": "직방", "daangn": "당근", "naver": "네이버"}
BASE_KO = {"latest": "최근 실거래", "avg3": "최근 3건 평균", "max": "최고 실거래"}


def format_listing(l: Listing, b: dict) -> str:
    e = html.escape
    pct = (1 - l.price / b["base"]) * 100
    lines = [
        f"🏠 <b>[{SOURCE_KO.get(l.source, l.source)}] {e(l.name)}</b>",
        f"📍 {e(l.sigungu or l.sido)} {e(l.dong)} · 전용 {l.area:g}㎡"
        + (f" · {e(l.building)}" if l.building else "")
        + (f" · {e(l.floor)}층" if l.floor and not l.floor.endswith("층") else (f" · {e(l.floor)}" if l.floor else "")),
        f"💰 <b>매매 {fmt_price(l.price)}</b>  ({BASE_KO.get(b['mode'], '실거래')} {fmt_price(b['base'])} 대비 <b>-{pct:.1f}%</b>)",
    ]
    tr = "; ".join(f"{t[7]} {fmt_price(t[6])}({t[8]}층)" for t in b["trades"])
    lines.append(f"📊 실거래({e(b['apt'])} {b['area']:g}㎡): {e(tr)}")
    if not b.get("within_lookback"):
        lines.append("⚠️ 최근 6개월 내 거래 없음 → 더 오래된 거래 기준")
    if l.title:
        lines.append(f"📝 {e(l.title[:80])}")
    lines.append(f'🔗 <a href="{e(l.url)}">매물 보기</a>')
    return "\n".join(lines)


class Telegram:
    def __init__(self):
        self.token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
        self.chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()

    @property
    def enabled(self) -> bool:
        return bool(self.token and self.chat_id)

    def send(self, text: str) -> bool:
        if not self.enabled:
            print("\n" + text + "\n" + "─" * 40)
            return True
        r = requests.post(
            f"https://api.telegram.org/bot{self.token}/sendMessage",
            json={"chat_id": self.chat_id, "text": text[:4096], "parse_mode": "HTML",
                  "disable_web_page_preview": True},
            timeout=20,
        )
        if not r.ok:
            log.error("텔레그램 전송 실패 %s: %s", r.status_code, r.text[:200])
        return r.ok
