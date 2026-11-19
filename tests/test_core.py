import pickle, sys, tempfile
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from alert.common import Listing, norm_name, name_tokens
from alert.molit import RealPriceDB, Trade
from alert.notify import format_listing
from alert.sources.daangn import parse_list, parse_detail

SAMPLE = """<?xml version="1.0" encoding="UTF-8"?><response><header><resultCode>000</resultCode><resultMsg>OK</resultMsg></header>
<body><items>
<item><aptNm>매탄위브하늘채</aptNm><cdealType> </cdealType><dealAmount>98,000</dealAmount><dealDay>3</dealDay><dealMonth>10</dealMonth><dealYear>2026</dealYear><excluUseAr>84.81</excluUseAr><floor>12</floor><jibun>1352</jibun><sggCd>41117</sggCd><umdCd>10100</umdCd><umdNm>매탄동</umdNm></item>
<item><aptNm>매탄위브하늘채</aptNm><cdealType>O</cdealType><dealAmount>120,000</dealAmount><dealDay>9</dealDay><dealMonth>10</dealMonth><dealYear>2026</dealYear><excluUseAr>84.81</excluUseAr><floor>3</floor><jibun>1352</jibun><sggCd>41117</sggCd><umdCd>10100</umdCd><umdNm>매탄동</umdNm></item>
</items><numOfRows>1000</numOfRows><pageNo>1</pageNo><totalCount>2</totalCount></body></response>"""


class FakeResp:
    content = SAMPLE.encode()
    text = SAMPLE


def make_db(tmp):
    db = RealPriceDB("k", Path(tmp), 6, 12)
    db.http.get = lambda *a, **k: FakeResp()
    return db


def test_molit_parse_skips_cancelled(tmp_path):
    rows = make_db(tmp_path)._fetch_month("41117", "202610")
    assert len(rows) == 1 and rows[0][6] == 98000 and rows[0][5] == 84.81


def test_baseline_by_jibun_and_name(tmp_path):
    db = make_db(tmp_path)
    db.by_sgg["41117"] = [Trade("41117", "10100", "매탄동", "1352", "매탄위브하늘채", 84.81, 98000, "2026-10-03", "12")]
    l = Listing("zigbang", "1", "매탄위브하늘채", "경기도", "수원시 영통구", "매탄동", 84.81, 89000, "u",
                bcode="4111710100", jibun="1352")
    b = db.baseline(l, "41117", "latest", 1.0)
    assert b["base"] == 98000 and b["match"] == "지번"
    l.jibun, l.bcode, l.name = "", "", "매탄 위브 하늘채 아파트"
    assert db.baseline(l, "41117", "latest", 1.0)["match"].startswith("단지명")
    l.area = 59.9
    assert db.baseline(l, "41117", "latest", 1.0) is None
    print(format_listing(Listing("zigbang", "1", "매탄위브하늘채", "경기도", "수원시 영통구", "매탄동", 84.81,
                                 89000, "https://x", floor="1", building="122동"), b))


def test_names():
    assert norm_name("래미안 대치 팰리스(1단지)") == "래미안대치팰리스"
    assert "상지리츠빌" in name_tokens("반포3차 (상지리츠빌)")


def test_daangn_parse():
    html = ('originalId:"11",salesType:"APART",tradeType:"BUY",tradePrice:80000,monthlyPay:0,area:"84.87",regionName3:"신흥동"'
            ' __typename:"PropComplex",id:"x",type:"APARTMENT",name:"삼부르네상스파크Ⅱ", buildingName:"삼부",'
            ' area:"84.87",supplyArea:"110",floor:"5.0",topFloor:"20", jibunAddress:"경기도 성남시 수정구 신흥동 2478"')
    assert parse_list(html)[0]["price"] == 80000
    d = parse_detail(html)
    assert d["jibun"] == "2478" and d["dong"] == "신흥동" and d["floor"] == "5/20"


def test_rent_interp():
    from alert.rentwatch import Complex, _interp
    t = [[5000, 230], [10000, 205], [15000, 180]]
    assert _interp(t, 7500) == 217.5 and _interp(t, 20000) == 155
    c = Complex({"name": "광교해모로", "tables": [t]}, 95)
    assert c.matches("광교 해모로") and c.matches("해모로(광교)") and not c.matches("광교") and not c.matches("광교해모로2차")
    assert not c.size_ok(84, 80) and c.size_ok(99, 130)
