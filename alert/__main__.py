"""실행: python -m alert [--dry-run] [--only zigbang,daangn] [--config config.yaml]"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from datetime import date
from pathlib import Path

import yaml

from .common import Listing, fmt_price, log, norm_name
from .molit import from_env
from .notify import SOURCE_KO, Telegram, format_listing
from .sources.daangn import Daangn
from .sources.naver import Naver, NaverBlocked
from .sources.zigbang import Zigbang

ROOT = Path(__file__).resolve().parent.parent


def load_state(path: Path) -> dict:
    try:
        return json.loads(path.read_text("utf-8"))
    except Exception:
        return {}


def dedupe(items: list[Listing]) -> list[Listing]:
    """같은 매물이 여러 사이트/중개사로 올라온 경우 하나만 남김 (가장 싼 것)."""
    best: dict[tuple, Listing] = {}
    for l in items:
        k = (l.sigungu, l.dong, norm_name(l.name)[:6], round(l.area), l.price, l.building)
        if k not in best:
            best[k] = l
    return list(best.values())


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=str(ROOT / "config.yaml"))
    ap.add_argument("--state-dir", default=str(ROOT / "state"))
    ap.add_argument("--dry-run", action="store_true", help="알림 기록을 저장하지 않고 콘솔에만 출력")
    ap.add_argument("--only", default="", help="쉼표로 구분한 소스만 실행 (zigbang,daangn,naver)")
    ap.add_argument("--regions", default="", help="쉼표로 구분한 지역 이름만 실행 (테스트용)")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                        datefmt="%H:%M:%S")
    cfg = yaml.safe_load(Path(args.config).read_text("utf-8"))
    flt = cfg["filters"]
    rp = cfg.get("real_price", {})
    regions = cfg["regions"]
    if args.regions:
        want = {x.strip() for x in args.regions.split(",")}
        regions = [r for r in regions if r["name"] in want]
    state_dir = Path(args.state_dir)
    state_dir.mkdir(parents=True, exist_ok=True)
    seen_path = state_dir / "seen.json"
    state = load_state(seen_path)
    seen: dict = state.get("alerted", {})
    first_run = not state
    tg = Telegram()
    if args.dry_run:
        tg.token = ""  # 콘솔 출력

    enabled = {k for k, v in cfg.get("sources", {}).items() if v}
    if args.only:
        enabled = {x.strip() for x in args.only.split(",")}

    t0 = time.time()
    db = from_env(state_dir, cfg)
    db.load(sorted({r["lawd_cd"] for r in regions}))

    # 실거래 데이터에 등장하는 동 = 아파트가 있는 동 (당근 동별 페이지 순회용)
    dongs_by_region = {}
    for r in regions:
        dongs_by_region[r["name"]] = sorted({t.umd for t in db.by_sgg.get(r["lawd_cd"], []) if t.umd})

    listings: list[Listing] = []
    errors: list[str] = []
    for name in ("zigbang", "daangn", "naver"):
        if name not in enabled:
            continue
        try:
            if name == "zigbang":
                listings += Zigbang(cfg, state_dir).fetch(regions, flt)
            elif name == "daangn":
                listings += Daangn(cfg, state_dir).fetch(regions, flt, dongs_by_region)
            elif name == "naver":
                listings += Naver(cfg, state_dir).fetch(regions, flt)
        except NaverBlocked as e:
            errors.append(f"네이버: {e}")
            log.warning("%s", e)
        except Exception as e:
            errors.append(f"{SOURCE_KO.get(name, name)}: {e}")
            log.exception("%s 수집 실패", name)

    log.info("조건(%s 이하, 전용 %s㎡ 이상) 1차 통과 매물 %d건",
             fmt_price(flt["max_price"]), flt["min_area"], len(listings))

    lawd_by_region = {r["name"]: r["lawd_cd"] for r in regions}
    disc = float(flt["discount_pct"]) / 100
    hits, no_base = [], 0
    for l in dedupe(listings):
        b = db.baseline(l, lawd_by_region[l.region], rp.get("baseline", "latest"),
                        float(rp.get("area_tolerance", 1.0)))
        if not b:
            no_base += 1
            log.debug("실거래 매칭 실패: %s %s %s %.2f", l.source, l.dong, l.name, l.area)
            continue
        if l.price <= b["base"] * (1 - disc):
            hits.append((l, b))
    log.info("실거래 매칭 실패 %d건, 최종 조건 충족 %d건", no_base, len(hits))

    # 새로 조건을 만족했거나, 이전 알림보다 가격이 더 내려간 매물만 알림
    new = [(l, b) for l, b in hits if l.key not in seen or l.price < seen[l.key]["price"]]
    new.sort(key=lambda x: x[0].price / x[1]["base"])
    limit = int(cfg.get("max_individual_messages", 15))
    if first_run and new:
        tg.send(f"🔔 부동산 알림 시작: 현재 조건에 맞는 매물 {len(new)}건이 있습니다.")
    for l, b in new[:limit]:
        tg.send(format_listing(l, b))
    if len(new) > limit:
        rest = new[limit:]
        lines = [f"➕ 외 {len(rest)}건 (할인율 순)"]
        for l, b in rest:
            pct = (1 - l.price / b["base"]) * 100
            lines.append(f"• [{SOURCE_KO[l.source]}] {l.name} {l.area:g}㎡ {fmt_price(l.price)} "
                         f"(-{pct:.1f}%) {l.url}")
        chunk = ""
        for line in lines:
            if len(chunk) + len(line) > 3800:
                tg.send(chunk)
                chunk = ""
            chunk += line + "\n"
        tg.send(chunk)

    # 오류 알림은 하루 한 번만
    today = date.today().isoformat()
    if errors and state.get("error_notified") != today:
        tg.send("⚠️ 일부 사이트 수집 실패\n" + "\n".join(errors))
        state["error_notified"] = today

    if not args.dry_run:
        now = int(time.time())
        for l, b in hits:
            if l.key not in seen or l.price < seen[l.key]["price"]:
                seen[l.key] = {"price": l.price, "t": now}
            else:
                seen[l.key]["t"] = now          # 아직 올라와 있음
        # 30일 동안 안 보인 기록 정리 (다시 조건 충족하면 재알림)
        seen = {k: v for k, v in seen.items() if now - v["t"] < 30 * 86400}
        state["alerted"] = seen
        state["last_run"] = now
        seen_path.write_text(json.dumps(state, ensure_ascii=False, indent=0), "utf-8")
    log.info("완료: 신규 알림 %d건, %.0f초", len(new), time.time() - t0)
    return 0


if __name__ == "__main__":
    sys.exit(main())
