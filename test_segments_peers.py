# -*- coding: utf-8 -*-
"""xbrl_segments · sales_table · segment_mix · peer_compare 단위 테스트 — 네트워크 없이 합성 XBRL·DART 표 조각으로 확인한다.

    python test_segments_peers.py
"""
import os
import tempfile
import unittest
from unittest import mock

import company_report as cr
import dart_core
import peer_compare as pc
import sales_table as st
import segment_mix as sm
import test_company_report as tcr
import xbrl_segments as xs

FS_AXIS = "ifrs-full:ConsolidatedAndSeparateFinancialStatementsAxis"
CONSOL, SEPARATE = "ifrs-full:ConsolidatedMember", "ifrs-full:SeparateMember"
SEG_AXIS, PROD_AXIS, GEO_AXIS = "ifrs-full:SegmentsAxis", "ifrs-full:ProductsAndServicesAxis", "ifrs-full:GeographicalAreasAxis"
CONS_ITEMS = "ifrs-full:SegmentConsolidationItemsAxis"
OPSEG = "ifrs-full:OperatingSegmentsMember"


class XbrlBuilder:
    """합성 contexts/facts. 컨텍스트는 (기간, 차원) 조합마다 하나씩 만든다."""

    def __init__(self):
        self.contexts, self.facts, self._n = {}, [], 0

    def ctx(self, year, dims):
        self._n += 1
        cid = f"c{self._n}"
        self.contexts[cid] = (f"{year}-01-01", f"{year}-12-31", dims)
        return cid

    def add(self, tag, value, dims=None, year=2025, unit="KRW", fs=CONSOL):
        d = dict(dims or {})
        if fs:
            d[FS_AXIS] = fs
        self.facts.append((tag, self.ctx(year, d), float(value), unit))


class XbrlParseTests(unittest.TestCase):
    def test_clean_label_and_local(self):
        self.assertEqual(xs.clean_label("DX 부문 [구성요소]"), "DX 부문")
        self.assertEqual(xs.clean_label("  미국  [멤버] "), "미국")
        self.assertEqual(xs.clean_label(None), "")
        self.assertEqual(xs.local("ifrs-full:USMember"), "USMember")
        self.assertEqual(xs.local("{http://x}Revenue"), "Revenue")

    def test_parse_instance_and_labels(self):
        inst = """<xbrl xmlns="http://www.xbrl.org/2003/instance" xmlns:xbrldi="http://xbrl.org/2006/xbrldi" xmlns:ifrs-full="x" xmlns:e="e">
          <context id="a"><period><startDate>2025-01-01</startDate><endDate>2025-12-31</endDate></period>
            <entity><segment><xbrldi:explicitMember dimension="ifrs-full:SegmentsAxis">e:DxMember</xbrldi:explicitMember></segment></entity></context>
          <context id="b"><period><instant>2025-12-31</instant></period>
            <entity><segment><xbrldi:typedMember dimension="e:ListAxis"><e:v>1</e:v></xbrldi:typedMember></segment></entity></context>
          <ifrs-full:Revenue contextRef="a" unitRef="KRW">1,000</ifrs-full:Revenue>
          <ifrs-full:Revenue contextRef="a" unitRef="KRW">1000</ifrs-full:Revenue>
          <ifrs-full:Revenue contextRef="b" unitRef="KRW">5</ifrs-full:Revenue>
          <ifrs-full:Note contextRef="a">텍스트</ifrs-full:Note>
        </xbrl>"""
        contexts, facts = xs.parse_instance(inst.encode("utf-8"))
        self.assertEqual(set(contexts), {"a"})                              # 타입 차원 컨텍스트는 버린다
        self.assertEqual(contexts["a"][2], {"ifrs-full:SegmentsAxis": "e:DxMember"})
        self.assertEqual([(t, v) for t, _, v, _ in facts], [("Revenue", 1000.0)])    # 쉼표 있는 값은 숫자가 아니라 건너뜀, 단위 없는 사실도 건너뜀
        lab = """<linkbase xmlns="http://www.xbrl.org/2003/linkbase" xmlns:xlink="http://www.w3.org/1999/xlink">
          <labelLink><loc xlink:label="l1" xlink:href="a.xsd#e_DxMember"/><label xlink:label="t1" xlink:role="http://www.xbrl.org/2003/role/label">DX 부문 [구성요소]</label>
          <label xlink:label="t2" xlink:role="http://www.xbrl.org/2003/role/documentation">설명</label>
          <labelArc xlink:from="l1" xlink:to="t2"/><labelArc xlink:from="l1" xlink:to="t1"/></labelLink></linkbase>"""
        self.assertEqual(xs.parse_labels(lab.encode("utf-8")), {"e_DxMember": "DX 부문"})

    def test_year_periods(self):
        b = XbrlBuilder()
        b.add("Revenue", 1, year=2025)
        b.add("Revenue", 1, year=2024)
        b.contexts["q"] = ("2025-10-01", "2025-12-31", {})              # 분기 기간은 무시
        self.assertEqual(xs.year_periods(b.contexts), ("2025-12-31", "2024-12-31"))
        self.assertEqual(xs.year_periods({}), (None, None))

    def test_select_members_avoids_double_counting(self):
        # 상위(해외 100)와 그 하위(유럽 60·아시아 40)가 함께 공시 → 합계 100을 넘지 않게 큰 것부터
        picked, covered = xs.select_members([("국내", 100), ("해외", 100), ("유럽", 60), ("아시아", 40)], 200)
        self.assertEqual({n for n, _ in picked}, {"국내", "해외"})
        self.assertEqual(covered, 200)
        picked, covered = xs.select_members([("a", 30), ("b", 20)], 100)       # 일부만 공시 → 있는 만큼
        self.assertEqual((len(picked), covered), (2, 50))
        self.assertEqual(xs.select_members([("a", -5), ("b", 0)], 10), ([], 0.0))


class XbrlExtractTests(unittest.TestCase):
    def labels(self):
        return {"e_Dx": "DX 부문", "e_Ds": "DS 부문", "e_Elim": "조정", "e_Phone": "스마트폰", "e_Mem": "메모리", "e_Us": "미국", "e_Kr": "한국"}

    def test_segments_with_total_and_operating_income(self):
        b = XbrlBuilder()
        b.add("Revenue", 1000, {CONS_ITEMS: OPSEG})                                 # 부문 합계(내부거래 제거 전) — 연결 매출보다 먼저 나와도 총매출로 오인되면 안 된다
        b.add("Revenue", 900)                                                       # 연결 매출
        for m, cur, prev, op in (("e:Dx", 600, 500, 60), ("e:Ds", 400, 300, 40)):
            dims = {SEG_AXIS: m, CONS_ITEMS: OPSEG}
            b.add("Revenue", cur, dims)
            b.add("Revenue", prev, dims, year=2024)
            b.add("OperatingIncomeLoss", op, dims)
        b.add("Revenue", -100, {CONS_ITEMS: "ifrs-full:MaterialReconcilingItemsMember"})
        out = xs.extract(b.contexts, b.facts, self.labels())
        seg = out["axes"]["segment"]
        self.assertEqual(out["period_end"], "2025-12-31")
        self.assertEqual([m["label"] for m in seg["members"]], ["DX 부문", "DS 부문"])
        self.assertEqual(seg["total"], 1000)                                        # 비중 기준은 부문 합계
        self.assertAlmostEqual(seg["members"][0]["share"], 60.0)
        self.assertEqual((seg["members"][0]["prev"], seg["members"][0]["op_cur"]), (500, 60))
        self.assertEqual(seg["consolidated_revenue"], 900)

    def test_segment_view_rejected_when_main_segment_missing(self):
        b = XbrlBuilder()
        b.add("Revenue", 10000)                                                     # 연결 매출 1만인데 공시된 부문은 합 1,000뿐 → 주력 부문 누락
        b.add("Revenue", 700, {SEG_AXIS: "e:Dx", CONS_ITEMS: OPSEG})
        b.add("Revenue", 300, {SEG_AXIS: "e:Ds", CONS_ITEMS: OPSEG})
        out = xs.extract(b.contexts, b.facts, self.labels())
        self.assertNotIn("segment", out["axes"])
        self.assertIn("10%", out["rejected"]["segment"])                          # 사유가 기록된다

    def test_product_axis_falls_back_to_other_revenue_tags_and_dedupes(self):
        b = XbrlBuilder()
        b.add("Revenue", 1400)
        b.add("RevenueFromSaleOfGoodsProduct", 600, {PROD_AXIS: "e:Phone"})        # 요소가 Revenue가 아닌 회사
        b.add("Revenue", 400, {PROD_AXIS: "e:Mem"})
        b.add("Revenue", 400, {PROD_AXIS: "e:MemDup"})                              # 같은 라벨·금액을 다른 멤버로 한 번 더 → 한 번만
        labels = {**self.labels(), "e_MemDup": "메모리"}
        out = xs.extract(b.contexts, b.facts, labels)
        prod = out["axes"]["product"]
        self.assertEqual([(m["label"], m["cur"]) for m in prod["members"]], [("스마트폰", 600), ("메모리", 400)])
        self.assertAlmostEqual(prod["coverage"], 1000 / 1400)                       # 중복 제거 뒤 합 1,000 / 연결 1,400

    def test_extra_axes_and_other_statement_scope_are_ignored(self):
        b = XbrlBuilder()
        b.add("Revenue", 1000)
        b.add("Revenue", 1000, {GEO_AXIS: "e:Kr"})
        b.add("Revenue", 777, {GEO_AXIS: "e:Us", SEG_AXIS: "e:Dx"})               # 축이 더 붙은 값(부문 안의 지역)은 버림
        b.add("Revenue", 555, {GEO_AXIS: "e:Us"}, fs=SEPARATE)                      # 별도 재무제표 값은 연결 보기에 안 섞임
        out = xs.extract(b.contexts, b.facts, self.labels(), fs="CFS")
        self.assertEqual([m["label"] for m in out["axes"]["region"]["members"]], ["한국"])
        sep = xs.extract(b.contexts, b.facts, self.labels(), fs="OFS")
        self.assertEqual([m["label"] for m in sep["axes"]["region"]["members"]], ["미국"])

    def test_no_axes_or_no_period(self):
        b = XbrlBuilder()
        b.add("Revenue", 1000)
        self.assertEqual(xs.extract(b.contexts, b.facts, {})["axes"], {})
        self.assertEqual(xs.extract({}, [], {})["axes"], {})


def table_doc(rows_html, unit="백만원"):
    return (f'<SECTION-2><TITLE ATOC="Y">4. 매출 및 수주상황</TITLE><P>가. 매출실적</P>'
            f'<TABLE><TBODY><TR><TD>(단위: {unit})</TD></TR></TBODY></TABLE>'
            f'<TABLE><TBODY>{rows_html}</TBODY></TABLE><P>나. 판매경로</P></SECTION-2><TITLE>5. 위험관리</TITLE>')


YC_STYLE = """
<TR><TD>매출유형</TD><TD COLSPAN="2">품 목</TD><TD>2025년(제25기)</TD><TD>2024년(제24기)</TD></TR>
<TR><TD ROWSPAN="8">제 품</TD><TD ROWSPAN="4">A 소재</TD><TD>수 출</TD><TD>100</TD><TD>90</TD></TR>
<TR><TD>수 출</TD><TD>($70)</TD><TD>($63)</TD></TR>
<TR><TD>내 수</TD><TD>300</TD><TD>270</TD></TR>
<TR><TD>소 계</TD><TD>400</TD><TD>360</TD></TR>
<TR><TD ROWSPAN="4">B 소재</TD><TD>수 출</TD><TD>-</TD><TD>-</TD></TR>
<TR><TD>수 출</TD><TD>-</TD><TD>-</TD></TR>
<TR><TD>내 수</TD><TD>200</TD><TD>180</TD></TR>
<TR><TD>소 계</TD><TD>200</TD><TD>180</TD></TR>
<TR><TD COLSPAN="2">합 계</TD><TD>소 계</TD><TD>600</TD><TD>540</TD></TR>
"""


class SalesTableTests(unittest.TestCase):
    def test_parse_amount(self):
        self.assertEqual(st.parse_amount("1,234"), 1234.0)
        self.assertEqual(st.parse_amount("△1,234"), -1234.0)
        self.assertEqual(st.parse_amount("(1,234)"), -1234.0)
        for bad in ("", "-", "($7,397)", "41.67%", "합계", None):
            self.assertIsNone(st.parse_amount(bad))

    def test_find_unit(self):
        self.assertEqual(st.find_unit("(단위: L, Gal, 백만원, 천달러)"), 1e6)
        self.assertEqual(st.find_unit("단위 : 천원"), 1e3)
        self.assertEqual(st.find_unit("(단위:억원)"), 1e8)
        self.assertIsNone(st.find_unit("단위: USD"))

    def test_grid_expands_spans(self):
        grid = st.to_grid(st.parse_tables('<TABLE><TR><TD ROWSPAN="2">가</TD><TD COLSPAN="2">나</TD></TR><TR><TD>다</TD><TD>라</TD></TR></TABLE>')[0])
        self.assertEqual(grid, [["가", "나", "나"], ["가", "다", "라"]])
        nested = st.parse_tables('<TABLE><TR><TD>바깥<TABLE><TR><TD>안쪽</TD></TR></TABLE></TD><TD>x</TD></TR></TABLE>')
        self.assertEqual(len(nested), 1)                                           # 중첩 표는 바깥 칸의 글자로 합친다

    def test_reads_export_domestic_subtotal_table(self):
        res = st.parse_sales(table_doc(YC_STYLE), 600e6)
        self.assertEqual([(i["label"], i["cur"], i["prev"]) for i in res["items"]], [("A 소재", 400e6, 360e6), ("B 소재", 200e6, 180e6)])
        self.assertAlmostEqual(res["coverage"], 1.0)
        self.assertAlmostEqual(res["items"][0]["share"], 400 / 600 * 100)
        self.assertTrue(res["prev_ok"])

    def test_validation_gate_rejects_mismatched_revenue(self):
        res = st.parse_sales(table_doc(YC_STYLE), 1000e6)                          # 읽은 합 600 vs 재무제표 매출 1,000
        self.assertFalse(res and res.get("items"))
        self.assertIsNone(st.parse_sales(table_doc(YC_STYLE), None))
        self.assertIsNone(st.parse_sales("<P>매출 없음</P>", 600e6))

    def test_adjustment_row_with_negative_value_is_rejected(self):
        rows = """<TR><TD>사업부문</TD><TD>주요 제품</TD><TD>2025년</TD><TD>2024년</TD></TR>
        <TR><TD>가</TD><TD>x</TD><TD>400</TD><TD>300</TD></TR><TR><TD>나</TD><TD>y</TD><TD>200</TD><TD>100</TD></TR>
        <TR><TD>조정</TD><TD>부문간 내부거래 제거</TD><TD>△100</TD><TD>△50</TD></TR>"""
        res = st.parse_sales(table_doc(rows), 500e6)                               # 400+200-100=500으로 관문은 넘지만 음수 행이 있으면 품목 구성표가 아니다
        self.assertFalse(res and res.get("items"))

    def test_multilevel_table_does_not_double_count_subtotals(self):
        rows = """<TR><TD>사업부문</TD><TD>품목</TD><TD></TD><TD>제33기</TD><TD>제32기</TD></TR>
        <TR><TD ROWSPAN="7">금융</TD><TD ROWSPAN="3">제품</TD><TD>수출</TD><TD>30</TD><TD>10</TD></TR>
        <TR><TD>내수</TD><TD>70</TD><TD>40</TD></TR><TR><TD>합계</TD><TD>100</TD><TD>50</TD></TR>
        <TR><TD ROWSPAN="3">용역</TD><TD>수출</TD><TD>-</TD><TD>-</TD></TR>
        <TR><TD>내수</TD><TD>60</TD><TD>20</TD></TR><TR><TD>합계</TD><TD>60</TD><TD>20</TD></TR>
        <TR><TD>소계</TD><TD>수출</TD><TD>30</TD><TD>10</TD></TR>
        <TR><TD ROWSPAN="3">물류</TD><TD>소계</TD><TD>합계</TD><TD>40</TD><TD>5</TD></TR>"""
        res = st.parse_sales(table_doc(rows), 200e6)
        labels = {i["label"]: i["cur"] for i in res["items"]}
        self.assertEqual(sum(labels.values()), 200e6)                                # 100+60+40, 사업부문 소계(160)는 이중 계산되지 않는다
        self.assertEqual(labels["금융 · 제품"], 100e6)

    def test_prefixed_subtotal_label_is_not_an_item(self):
        rows = """<TR><TD>구분</TD><TD>구분</TD><TD>2025년</TD><TD>2024년</TD></TR>
        <TR><TD>매출액</TD><TD>플랫폼</TD><TD>100</TD><TD>80</TD></TR><TR><TD>매출액</TD><TD>유지보수</TD><TD>200</TD><TD>190</TD></TR>
        <TR><TD>매출액</TD><TD>소프트웨어매출 소계</TD><TD>300</TD><TD>270</TD></TR><TR><TD>매출액</TD><TD>유통</TD><TD>100</TD><TD>90</TD></TR>
        <TR><TD>매출액</TD><TD>합계</TD><TD>400</TD><TD>360</TD></TR>"""
        res = st.parse_sales(table_doc(rows), 400e6)
        self.assertEqual(sum(i["cur"] for i in res["items"]), 400e6)                # 소계(300)가 품목으로 들어가면 700이 된다
        self.assertFalse(any("소계" in i["label"] for i in res["items"]))
        self.assertEqual(len(res["items"]), 3)

    def test_drop_equal_sums_retry(self):
        items = [{"label": "a", "cur": 100.0}, {"label": "b", "cur": 200.0}, {"label": "S/W", "cur": 300.0}, {"label": "c", "cur": 50.0}]
        kept = st._drop_subtotals(items)
        self.assertEqual([i["label"] for i in kept], ["a", "b", "c"])


class SegmentMixTests(unittest.TestCase):
    def xb(self, product_cov):
        members = [{"label": "스마트폰", "cur": 600.0, "prev": 500.0, "share": 60.0}, {"label": "메모리", "cur": 300.0, "prev": 250.0, "share": 30.0}]
        covered = 900.0 * product_cov / 0.9
        return {"period_end": "2025-12-31", "prev_end": "2024-12-31", "axes": {
            "product": {"title": "제품·서비스", "members": members, "covered": covered, "total": 1000.0, "coverage": covered / 1000, "consolidated_revenue": 1000.0, "unit": "KRW"},
            "region": {"title": "지역", "members": [{"label": "미국", "cur": 600.0, "prev": None, "share": 60.0}, {"label": "중국", "cur": 400.0, "prev": None, "share": 40.0}],
                       "covered": 1000.0, "total": 1000.0,
                       "coverage": 1.0, "consolidated_revenue": 1000.0, "unit": "KRW"}}}

    TABLE = {"items": [{"label": "표 품목A", "cur": 700.0, "prev": None, "share": 70.0}, {"label": "표 품목B", "cur": 300.0, "prev": None, "share": 30.0}],
             "sum_won": 1000.0, "prev_ok": False}

    def test_rest_row_and_notes(self):
        mix = sm.build_mix(self.xb(0.9))
        prod = next(v for v in mix["views"] if v["key"] == "product")
        self.assertEqual(prod["members"][-1]["label"], sm.REST_LABEL)
        self.assertAlmostEqual(prod["members"][-1]["cur"], 100.0)
        self.assertIn("90%", prod["note"])
        self.assertEqual([v["key"] for v in mix["views"]], ["product", "region"])    # 화면 순서

    def test_xbrl_preferred_unless_coverage_low(self):
        good = next(v for v in sm.build_mix(self.xb(0.85), self.TABLE)["views"] if v["key"] == "product")
        self.assertEqual(good["source"], sm.XBRL_SOURCE)                              # 80% 이상이면 XBRL
        bad = next(v for v in sm.build_mix(self.xb(0.5), self.TABLE)["views"] if v["key"] == "product")
        self.assertEqual(bad["source"], sm.TABLE_SOURCE)
        none = next(v for v in sm.build_mix({"axes": {}}, self.TABLE)["views"] if v["key"] == "product")
        self.assertEqual(none["source"], sm.TABLE_SOURCE)
        self.assertEqual(sm.build_mix({"axes": {}})["views"], [])

    def test_single_member_views_are_dropped(self):
        xb = self.xb(0.9)
        xb["axes"]["region"]["members"] = xb["axes"]["region"]["members"][:1]          # 지역이 하나뿐(100%)
        keys = [v["key"] for v in sm.build_mix(xb)["views"]]
        self.assertEqual(keys, ["product"])
        one_item_table = {"items": [{"label": "단일", "cur": 1000.0, "prev": None, "share": 100.0}], "sum_won": 1000.0, "prev_ok": False}
        self.assertEqual(sm.build_mix({"axes": {}}, one_item_table)["views"], [])      # 본문 표도 품목 하나뿐이면 보기를 만들지 않는다
        xb2 = self.xb(0.9)
        xb2["axes"]["product"]["members"] = xb2["axes"]["product"]["members"][:1]      # XBRL 제품이 하나뿐이면 본문 표로 넘어간다
        keys = [(v["key"], v["source"]) for v in sm.build_mix(xb2, self.TABLE)["views"]]
        self.assertIn(("product", sm.TABLE_SOURCE), keys)

    def test_compact_members(self):
        members = [{"label": f"m{i}", "cur": float(100 - i), "prev": 1.0, "share": 1.0} for i in range(13)]
        members.append({"label": sm.REST_LABEL, "cur": 5.0, "prev": None, "share": 0.5, "derived": True})
        out = sm.compact_members(members, top=10)
        self.assertEqual(len(out), 12)
        self.assertEqual(out[10]["label"], "나머지 3개 합계")
        self.assertEqual(out[10]["cur"], 90 + 89 + 88)
        self.assertAlmostEqual(out[10]["share"], 3.0)
        self.assertEqual(out[-1]["label"], sm.REST_LABEL)
        self.assertEqual(len(sm.compact_members(members[:4], top=10)), 4)

    def test_fetch_mix_caches_and_does_not_cache_errors(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(dart_core, "CACHE_DIR", tmp):
            with mock.patch.object(sm.xbrl_segments, "fetch_segments", return_value=self.xb(0.95)) as fx, \
                 mock.patch.object(sm.sales_table, "parse_sales") as ps:
                m1 = sm.fetch_mix("20260101000001", "CFS", 1000.0)
                m2 = sm.fetch_mix("20260101000001", "CFS", 1000.0)
                self.assertEqual(fx.call_count, 1)                                     # 두 번째는 디스크 캐시
                ps.assert_not_called()                                                 # XBRL 제품 구성이 충분하면 본문을 받지 않는다
                self.assertEqual(m1["views"], m2["views"])
            with mock.patch.object(sm.xbrl_segments, "fetch_segments", side_effect=dart_core.DartError("DART 요청 실패")):
                with mock.patch.object(dart_core, "download_document", side_effect=dart_core.DartError("x")):
                    bad = sm.fetch_mix("20260101000002", "CFS", 1000.0)
                    self.assertTrue(bad["errors"])
                self.assertFalse(os.path.exists(os.path.join(tmp, "mix_20260101000002_CFS.json")))        # 일시 오류는 캐시하지 않는다
            with mock.patch.object(sm.xbrl_segments, "fetch_segments", side_effect=dart_core.DartError("DART가 zip이 아닌 응답을 보냈습니다(fnlttXbrl.xml).")), \
                 mock.patch.object(dart_core, "download_document", return_value="<P>x</P>"):
                none = sm.fetch_mix("20260101000003", "CFS", 1000.0)
                self.assertEqual((none["errors"], none["views"]), ([], []))                              # XBRL이 아예 없는 회사는 정상 상태
                self.assertTrue(os.path.exists(os.path.join(tmp, "mix_20260101000003_CFS.json")))

    def test_table_fallback_used_when_xbrl_has_no_product(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(dart_core, "CACHE_DIR", tmp), \
             mock.patch.object(sm.xbrl_segments, "fetch_segments", return_value={"axes": {}}), \
             mock.patch.object(dart_core, "download_document", return_value=table_doc(YC_STYLE)):
            mix = sm.fetch_mix("20260101000004", "CFS", 600e6)
            self.assertEqual([m["label"] for m in mix["views"][0]["members"]], ["A 소재", "B 소재"])
            self.assertEqual(mix["views"][0]["source"], sm.TABLE_SOURCE)
            no_rev = sm.fetch_mix("20260101000005", "CFS", None)                       # 매출액이 없으면 합계 검증을 할 수 없어 본문 표는 쓰지 않는다
            self.assertEqual(no_rev["views"], [])


INDUSTRY = {"groupInfo": {"no": 278, "name": "반도체"}, "stocks": [
    {"itemCode": "000001", "stockName": "대장", "marketValueRaw": "100000000000000", "stockEndType": "stock"},
    {"itemCode": "000002", "stockName": "비슷A", "marketValueRaw": "5000000000000", "stockEndType": "stock"},
    {"itemCode": "000003", "stockName": "비슷B", "marketValueRaw": "3000000000000", "stockEndType": "stock"},
    {"itemCode": "000004", "stockName": "작음", "marketValueRaw": "10000000000", "stockEndType": "stock"},
    {"itemCode": "000005", "stockName": "대장우", "marketValueRaw": "4000000000000", "stockEndType": "stock"},          # DART에 없는 우선주 코드
    {"itemCode": "000006", "stockName": "제5호스팩", "marketValueRaw": "4000000000000", "stockEndType": "stock"},
    {"itemCode": "000007", "stockName": "코넥스사", "marketValueRaw": "4000000000000", "stockEndType": "konex"},
    {"itemCode": "000008", "stockName": "시총없음", "marketValueRaw": "0", "marketValue": "", "stockEndType": "stock"},
    {"itemCode": "000009", "stockName": "억단위", "marketValue": "35,000", "stockEndType": "stock"},                   # 억원 문자열 폴백 = 3.5조
    {"itemCode": "999999", "stockName": "대상", "marketValueRaw": "4000000000000", "stockEndType": "stock"}]}
DART = {"000001", "000002", "000003", "000004", "000006", "000007", "000008", "000009", "999999"}


class PeerTests(unittest.TestCase):
    def test_parse_industry(self):
        ind = pc.parse_industry(INDUSTRY)
        self.assertEqual((ind["name"], len(ind["stocks"])), ("반도체", 10))
        self.assertEqual(next(s for s in ind["stocks"] if s["code"] == "000002")["market_cap"], 5e12)
        self.assertEqual(next(s for s in ind["stocks"] if s["code"] == "000009")["market_cap"], 3.5e12)
        self.assertEqual(pc.parse_industry(None), {"name": None, "code": None, "stocks": []})

    def test_pick_peers_filters_and_nearest_size(self):
        ind = pc.parse_industry(INDUSTRY)
        got = pc.pick_peers(ind, "999999", 4e12, DART, n=3)
        self.assertEqual(got, ["000002", "000009", "000003"])                      # 시총 큰 순. 우선주·SPAC·코넥스·시총 0·대상 자신·DART 없음 제외
        self.assertNotIn("000005", got)
        self.assertEqual(pc.pick_peers(ind, "999999", 4e12, DART, n=10).count("000004"), 1)       # 작은 종목도 n이 크면 포함
        far = pc.pick_peers(ind, "999999", 90e12, DART, n=1)
        self.assertEqual(far, ["000001"])                                          # 가장 비슷한 크기
        by_size = pc.pick_peers(ind, "999999", None, DART, n=2)
        self.assertEqual(by_size, ["000001", "000002"])                            # 시총 모르면 큰 순

    def test_metrics_from_report(self):
        rep = cr.build_report(tcr.make_raw(), 100.0, tcr.TODAY)
        m = pc.metrics_from_report(rep)
        h, rows = rep["header"], rep["annual"]["rows"]
        self.assertEqual((m["code"], m["currency"]), ("000001", "KRW"))
        self.assertAlmostEqual(m["rev_growth"], (rows[-1]["revenue"] / rows[-2]["revenue"] - 1) * 100)
        self.assertEqual((m["per"], m["pbr"], m["roe"]), (h["per"], h["pbr"], rows[-1]["roe"]))      # 새로 계산하지 않고 보고서 값을 옮긴다
        rep["header"]["op_margin_ttm"] = 2484.0
        self.assertIsNone(pc.metrics_from_report(rep)["op_margin"])                 # 지주사처럼 영업이익이 매출보다 큰 값은 비교에서 뺀다

    def test_summarize_median_needs_three_and_gap(self):
        def peer(code, per, pbr=1.0, roe=10.0, cur="KRW"):
            return {"code": code, "name": code, "market_cap": 1e12, "per": per, "pbr": pbr, "psr": None, "op_margin": 20.0, "roe": roe,
                    "rev_growth": 5.0, "debt_ratio": 50.0, "currency": cur}
        subject = peer("S", 30.0, 3.0, 15.0)
        peers = [peer("a", 10.0), peer("b", 20.0), peer("c", None), peer("d", -5.0), peer("e", 40.0, cur="USD")]
        summ = pc.summarize(subject, peers)
        self.assertEqual(summ["n"], 4)                                              # 외화 보고 회사는 제외
        self.assertEqual(summ["excluded"], ["e"])
        self.assertIsNone(summ["median"]["per"])                                    # 유효 PER이 10·20 두 개뿐 → 중앙값 없음
        self.assertEqual(summ["median"]["roe"], 10.0)
        self.assertIsNone(summ["gap"]["per"])
        self.assertAlmostEqual(summ["gap"]["roe"], 5.0)                             # 비율 지표는 %p 차이
        more = pc.summarize(subject, peers[:2] + [peer("f", 30.0)])
        self.assertEqual(more["median"]["per"], 20.0)
        self.assertAlmostEqual(more["gap"]["per"], 50.0)                            # 배수는 중앙값 대비 %
        self.assertIsNone(more["median"]["psr"])


class FetchRawStage2Tests(unittest.TestCase):
    def setUp(self):
        self.calls = []

        def main(cc, year, rc="11011", ttl_hours=0):
            self.calls.append(("main", year, rc))
            row = tcr.fiscal_rows()
            for r in row:
                r["rcept_no"] = "20260101000009"
            return row if (year, rc) in ((2025, "11011"), (2022, "11011")) else []

        for p in (mock.patch.object(dart_core, "corp_codes", return_value={"000001": {"corp_code": "C1", "corp_name": "테스트"}}),
                  mock.patch.object(dart_core, "main_accounts_cached", side_effect=main),
                  mock.patch.object(dart_core, "all_statements", side_effect=lambda *a, **k: self.calls.append(("all", a[1])) or tcr.all_rows()),
                  mock.patch.object(dart_core, "share_totals", side_effect=lambda *a, **k: self.calls.append(("shares",)) or [{"se": "보통주", "istc_totqy": "100", "tesstk_co": "0", "stlm_dt": "x"}]),
                  mock.patch.object(dart_core, "shareholders", side_effect=lambda *a, **k: self.calls.append(("holders",)) or [{"stock_knd": "보통주", "nm": "갑"}]),
                  mock.patch.object(dart_core, "treasury_stock", side_effect=lambda *a, **k: self.calls.append(("treasury",)) or []),
                  mock.patch.object(dart_core, "recent_filings", side_effect=lambda *a, **k: self.calls.append(("filings",)) or []),
                  mock.patch.object(cr.segment_mix, "fetch_mix", side_effect=lambda *a, **k: self.calls.append(("mix", a)) or {"views": [], "rcept_no": a[0]})):
            p.start()
            self.addCleanup(p.stop)

    def test_full_fetch_includes_mix_with_krw_revenue(self):
        raw = cr.fetch_raw("000001", tcr.TODAY)
        mix_calls = [c for c in self.calls if c[0] == "mix"]
        self.assertEqual(len(mix_calls), 1)
        self.assertEqual(mix_calls[0][1], ("20260101000009", "CFS", 100))             # 접수번호·연결 여부·원화 매출액(합계 검증용)
        self.assertEqual(cr.build_report(raw, 100.0, tcr.TODAY)["mix"]["rcept_no"], "20260101000009")

    def test_foreign_currency_company_skips_table_gate_revenue(self):
        usd = tcr.fiscal_rows(currency="USD")
        for r in usd:
            r["rcept_no"] = "20260101000009"
        with mock.patch.object(dart_core, "main_accounts_cached", side_effect=lambda cc, y, rc="11011", ttl_hours=0: usd if (y, rc) == (2025, "11011") else []):
            cr.fetch_raw("000001", tcr.TODAY)
        self.assertIsNone([c for c in self.calls if c[0] == "mix"][0][1][2])

    def test_light_mode_skips_heavy_items(self):
        raw = cr.fetch_raw("000001", tcr.TODAY, light=True)
        kinds = {c[0] for c in self.calls}
        self.assertTrue({"shares", "all"} <= kinds)
        self.assertFalse(kinds & {"holders", "treasury", "filings", "mix"})
        rep = cr.build_report(raw, 100.0, tcr.TODAY)                                # 빠진 항목이 있어도 조립된다
        self.assertIsNone(rep["holders"])
        self.assertIsNone(rep["mix"])
        self.assertEqual(rep["header"]["market_cap"], 10000)


if __name__ == "__main__":
    unittest.main(verbosity=1)
