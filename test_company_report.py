# -*- coding: utf-8 -*-
"""company_report 단위 테스트 — 네트워크 없이 파서·계산·조립·탐색 순서를 확인한다(DART는 모의).

    python test_company_report.py
"""
import datetime as dt
import os
import tempfile
import unittest
from unittest import mock

import company_report as cr
import dart_core

TODAY = dt.date(2026, 11, 5)


def mrow(fs, sj, name, cur, prev=None, pprev=None, add=None, prev_add=None, currency="KRW"):
    """주요계정(fnlttSinglAcnt) 한 행. 금액은 DART처럼 쉼표 문자열."""
    f = lambda v: None if v is None else f"{v:,}"
    r = {"fs_div": fs, "sj_div": sj, "account_nm": name, "thstrm_amount": f(cur), "frmtrm_amount": f(prev), "bfefrmtrm_amount": f(pprev), "currency": currency}
    if add is not None:
        r["thstrm_add_amount"], r["frmtrm_add_amount"] = f(add), f(prev_add)
    return r


def arow(sj, aid, name, cur, prev=None, pprev=None, add=None, prev_add=None):
    """전체 재무제표(fnlttSinglAcntAll) 한 행. 금액은 쉼표 없는 문자열."""
    s = lambda v: None if v is None else str(v)
    r = {"sj_div": sj, "account_id": aid, "account_nm": name, "thstrm_amount": s(cur), "frmtrm_amount": s(prev), "bfefrmtrm_amount": s(pprev)}
    if add is not None:
        r["thstrm_add_amount"], r["frmtrm_add_amount"] = s(add), s(prev_add)
    return r


class ParseTests(unittest.TestCase):
    def test_to_num(self):
        self.assertEqual(cr.to_num("1,234"), 1234)
        self.assertEqual(cr.to_num("-5,000"), -5000)
        self.assertEqual(cr.to_num("12.5"), 12.5)
        for bad in (None, "", "-", " - ", "abc", "주4)"):
            self.assertIsNone(cr.to_num(bad))

    def test_norm_name(self):
        self.assertEqual(cr.norm_name("당기순이익(손실)"), "당기순이익")
        self.assertEqual(cr.norm_name(" 영업 이익(손실) "), "영업이익")
        self.assertEqual(cr.norm_name("수익(매출액)"), "수익(매출액)")

    def test_fmt(self):
        self.assertEqual(cr.fmt_won(1178.0251e12), "1,178조")
        self.assertEqual(cr.fmt_won(30.081e12), "30.1조")
        self.assertEqual(cr.fmt_won(3.0081e12), "3.01조")
        self.assertEqual(cr.fmt_won(4321e8), "4,321억")
        self.assertEqual(cr.fmt_won(-2.5e12), "-2.50조")
        self.assertEqual(cr.fmt_won(5e7), "5,000만")
        self.assertEqual(cr.fmt_won(None), "—")
        self.assertEqual(cr.fmt_amount(276327740, "USD"), "USD 276.3M")
        self.assertEqual(cr.fmt_amount(4321e8, "KRW"), "4,321억")
        self.assertEqual(cr.fmt_mult(3166.7), ">999배")
        self.assertEqual(cr.fmt_mult(None), "—")
        self.assertEqual(cr.fmt_pct(12.345, signed=True), "+12.3%")

    def test_main_get_prefers_fs_and_name_variants(self):
        rows = [mrow("OFS", "IS", "매출액", 100), mrow("CFS", "IS", "매출액", 300, 200, 150),
                mrow("CFS", "IS", "영업이익(손실)", -5), mrow("CFS", "IS", "당기순이익(손실)", 7, 6, 5),
                mrow("CFS", "IS", "영업수익", 999), mrow("CFS", "BS", "자본총계", 50)]
        self.assertEqual(cr.main_get(rows, "CFS", "revenue"), 300)          # 연결만, 별도 값은 안 섞인다
        self.assertEqual(cr.main_get(rows, "CFS", "revenue", "bfefrmtrm_amount"), 150)
        self.assertEqual(cr.main_get(rows, "CFS", "op"), -5)
        self.assertEqual(cr.main_get(rows, "CFS", "ni", "frmtrm_amount"), 6)
        self.assertEqual(cr.main_get(rows, "OFS", "revenue"), 100)
        self.assertIsNone(cr.main_get(rows, "CFS", "assets"))

    def test_pick_fs(self):
        both = [mrow("CFS", "BS", "자산총계", 10), mrow("OFS", "BS", "자산총계", 8)]
        self.assertEqual(cr.pick_fs(both), "CFS")
        self.assertEqual(cr.pick_fs([both[1]]), "OFS")
        self.assertIsNone(cr.pick_fs([]))
        self.assertIsNone(cr.pick_fs([mrow("CFS", "BS", "자산총계", None)]))

    def test_all_fallback_for_custom_revenue_id(self):
        # 주요계정에 매출 행이 없고, 전체 재무제표에는 회사 고유 ID(dart_Revenue...)로 있는 경우(실제 티엘비)
        rows = [arow("CIS", "dart_RevenueFromSaleOfGoodsProduct", "매출액", 258497722389), arow("CIS", "ifrs-full_CostOfSales", "매출원가", 1)]
        self.assertEqual(cr.all_get(rows, "revenue"), 258497722389)
        self.assertEqual(cr.all_get([arow("CIS", "ifrs-full_Revenue", "수익", 5)], "revenue"), 5)

    def test_cashflow_by_id_and_name(self):
        rows = [arow("CF", cr.OCF_ID, "영업활동현금흐름", 100, 80, 60),
                arow("CF", "ifrs-full_PurchaseOfPropertyPlantAndEquipmentClassifiedAsInvestingActivities", "유형자산의 취득", 30, 25, 20),
                arow("CF", "ifrs-full_PurchaseOfIntangibleAssetsClassifiedAsInvestingActivities", "무형자산의 취득", -5, 4, 3)]
        self.assertEqual(cr.cf_ocf(rows), 100)
        self.assertEqual(cr.cf_capex(rows), 35)                     # 부호가 음수로 와도 지출은 절댓값
        self.assertEqual(cr.cf_capex(rows, "frmtrm_amount"), 29)
        by_name = [arow("CF", "entity_x", "영업활동으로 인한 현금흐름", 70), arow("CF", "entity_y", "유형자산의 취득", 20), arow("CF", "entity_z", "무형자산 취득", 4)]
        self.assertEqual(cr.cf_ocf(by_name), 70)
        self.assertEqual(cr.cf_capex(by_name), 24)
        self.assertIsNone(cr.cf_capex([arow("CF", cr.OCF_ID, "영업활동현금흐름", 1)]))
        # 이름 폴백이 '영업활동 자산부채의 변동' 같은 하위 항목을 집지 않는다
        self.assertIsNone(cr.cf_ocf([arow("CF", "x", "영업활동 자산부채의 변동", 9), arow("CF", "y", "영업활동관련 조정", 9)]))

    def test_share_class_variants(self):
        for label, want in (("보통주", "common"), ("의결권 있는 주식\n(보통주)", "common"), ("의결권 있는\n주식", "common"),
                            ("우선주", "pref"), ("의결권 없는 주식\n(우선주)", "pref"), ("의결권 없는 주식", "pref"), ("합계", None), ("비고", None)):
            self.assertEqual(cr.share_class(label), want, label)

    def test_parse_shares(self):
        rows = [{"se": "의결권 있는 주식\n(보통주)", "istc_totqy": "10,732,731", "tesstk_co": "272,580", "stlm_dt": "2026-06-30"},
                {"se": "의결권 없는 주식\n(우선주)", "istc_totqy": "-", "tesstk_co": "-", "stlm_dt": "2026-06-30"},
                {"se": "합계", "istc_totqy": "10,732,731", "tesstk_co": "272,580", "stlm_dt": "2026-06-30"}]
        s = cr.parse_shares(rows)
        self.assertEqual((s["common"], s["pref"], s["treasury_common"], s["date"]), (10732731, None, 272580, "2026-06-30"))
        self.assertIsNone(cr.parse_shares([{"se": "합계", "istc_totqy": "-"}])["common"])     # 1분기 보고서처럼 숫자가 없는 응답

    def test_parse_holders_total_row_and_sum(self):
        h = lambda nm, rel, rt, knd="보통주", prev=None: {"stock_knd": knd, "nm": nm, "relate": rel, "trmend_posesn_stock_co": "1,000", "trmend_posesn_stock_qota_rt": rt,
                                                        "bsis_posesn_stock_qota_rt": prev, "stlm_dt": "2026-06-30"}
        with_total = cr.parse_holders([h("갑", "최대주주 본인", "20.46", prev="39.42"), h("계", "", "20.46"), h("갑", "최대주주 본인", "0.5", knd="우선주")])
        self.assertEqual(with_total["major"]["name"], "갑")
        self.assertEqual(with_total["major"]["prev_ratio"], 39.42)
        self.assertEqual(with_total["group_ratio"], 20.46)
        self.assertEqual(len(with_total["people"]), 1)                      # 합계·우선주 행은 사람 목록에서 빠진다
        no_total = cr.parse_holders([h("을", "최대주주의 특수관계인", "1.49"), h("병", "최대주주 본인", "8.51"), h("정", "계열회사 임원", "0.71")])
        self.assertEqual(no_total["major"]["name"], "병")
        self.assertAlmostEqual(no_total["group_ratio"], 10.71)
        self.assertEqual([p["name"] for p in no_total["people"]], ["병", "을", "정"])    # 지분 큰 순
        self.assertEqual(cr.parse_holders([])["major"], None)

    def test_parse_treasury(self):
        rows = [{"stock_knd": "보통주", "acqs_mth1": "배당가능이익범위 이내 취득", "bsis_qy": "1", "trmend_qy": "9"},
                {"stock_knd": "우선주", "acqs_mth1": "총계", "bsis_qy": "7", "trmend_qy": "7"},
                {"stock_knd": "보통주", "acqs_mth1": "총 계", "bsis_qy": "91,828,987", "change_qy_acqs": "73,465,072", "change_qy_dsps": "9,848,040",
                 "change_qy_incnr": "73,359,314", "trmend_qy": "82,086,705", "stlm_dt": "2026-06-30"}]
        t = cr.parse_treasury(rows, {"common": 5846278608})
        self.assertEqual((t["begin"], t["acquired"], t["disposed"], t["cancelled"], t["end"]), (91828987, 73465072, 9848040, 73359314, 82086705))
        self.assertAlmostEqual(t["ratio"], 82086705 / 5846278608 * 100)
        t2 = cr.parse_treasury([], {"common": 1000, "treasury_common": 50})          # 총계 행이 없으면 주식총수 현황의 자기주식수
        self.assertEqual((t2["end"], t2["ratio"], t2["acquired"]), (50, 5.0, None))
        self.assertIsNone(cr.parse_treasury([], {"common": 1000, "treasury_common": None})["end"])


class CalcTests(unittest.TestCase):
    def test_ttm(self):
        self.assertEqual(cr.ttm_value(100, 60, 40), 120)
        self.assertIsNone(cr.ttm_value(100, None, 40))
        self.assertIsNone(cr.ttm_value(None, 60, 40))

    def test_header_metrics(self):
        ttm = {"ni": 100.0, "revenue": 1000.0}
        h = cr.header_metrics(10.0, {"common": 100, "pref": 20}, ttm, 400.0)
        self.assertEqual(h["market_cap"], 1000.0)
        self.assertEqual(h["market_cap_all"], 1200.0)                       # 우선주를 보통주 가격으로 환산해 합친다
        self.assertAlmostEqual(h["per"], 12.0)
        self.assertAlmostEqual(h["pbr"], 3.0)
        self.assertAlmostEqual(h["psr"], 1.2)
        loss = cr.header_metrics(10.0, {"common": 100}, {"ni": -5.0, "revenue": 10.0}, -3.0)
        self.assertEqual((loss["per"], loss["per_status"], loss["pbr"], loss["pbr_status"]), (None, "loss", None, "neg"))
        self.assertIsNone(cr.header_metrics(None, {"common": 100}, ttm, 1.0)["market_cap"])
        self.assertIsNone(cr.header_metrics(10.0, {"common": None}, ttm, 1.0)["market_cap"])

    def test_scenario_is_consistent_with_per(self):
        # 현재 시총이 목표 시총과 같으면 현재가가 그대로 나오고, PER이 두 배면 시총도 목표주가도 두 배
        cap, price, ni = 1200.0, 10.0, 100.0
        now = cr.scenario(ni, cap, price, cap / ni)
        self.assertAlmostEqual(now["price"], price)
        self.assertAlmostEqual(now["upside"], 0.0)
        up = cr.scenario(ni, cap, price, 24.0)
        self.assertAlmostEqual(up["price"], 20.0)
        self.assertAlmostEqual(up["upside"], 1.0)
        for bad in ((0, cap, price, 10), (-5, cap, price, 10), (ni, None, price, 10), (ni, cap, None, 10), (ni, cap, price, None)):
            self.assertIsNone(cr.scenario(*bad))

    def test_naver_per_history_and_range(self):
        fin = {"financeInfo": {"trTitleList": [{"key": "202312", "isConsensus": "N"}, {"key": "202412", "isConsensus": "N"},
                                                {"key": "202512", "isConsensus": "N"}, {"key": "202612", "isConsensus": "Y"}],
                               "rowList": [{"title": "PER", "columns": {"202312": {"value": "36.84"}, "202412": {"value": "10.75"},
                                                                         "202512": {"value": "18.27"}, "202612": {"value": "4.77"}}}]}}
        hist = cr.parse_naver_annual(fin)
        self.assertEqual([h["year"] for h in hist], [2023, 2024, 2025, 2026])
        self.assertTrue(hist[-1]["consensus"])
        rng = cr.per_range(hist)
        self.assertEqual((rng["low"], rng["mid"], rng["high"], rng["n"]), (10.75, 18.27, 36.84, 3))    # 추정치(2026)는 제외
        self.assertIsNone(cr.per_range([{"year": 2025, "per": 18.0, "consensus": False}]))           # 1개 연도로는 범위를 만들지 않는다
        neg = [{"year": y, "per": p, "consensus": False} for y, p in ((2023, -5.0), (2024, 12.0), (2025, 20.0))]
        self.assertEqual(cr.per_range(neg)["low"], 12.0)                                              # 적자 연도(음의 PER)는 뺀다
        self.assertEqual(cr.parse_naver_annual(None), [])

    def test_parse_filings(self):
        rows = [{"rcept_dt": "20261104", "report_nm": "자기주식처분결과보고서", "rcept_no": "20261104000001", "flr_nm": "회사"},
                {"rcept_dt": "20261105", "report_nm": "임원ㆍ주요주주특정증권등소유상황보고서", "rcept_no": "20261105000001", "flr_nm": "임원"},
                {"rcept_dt": "20260501", "report_nm": "오래된 공시", "rcept_no": "20260501000001"},                       # 90일 밖
                {"rcept_dt": "20261020", "report_nm": "[기재정정]주요사항보고서(감자결정)", "rcept_no": "20261020000001"},
                {"rcept_dt": "잘못된날짜", "report_nm": "x", "rcept_no": "1"}]
        out = cr.parse_filings(rows, TODAY, {"20261020000001": "경고"})
        self.assertEqual([f["title"] for f in out], ["임원ㆍ주요주주특정증권등소유상황보고서", "자기주식처분결과보고서", "[기재정정]주요사항보고서(감자결정)"])
        self.assertEqual(out[0]["tag"], cr.NOISE_TAG)
        self.assertEqual(out[1]["tag"], "자기주식")
        self.assertEqual((out[2]["risk"], out[2]["tag"], out[2]["correction"]), ("경고", "감자", True))
        self.assertTrue(out[1]["url"].endswith("20261104000001"))
        self.assertEqual(len(cr.parse_filings(rows * 40, TODAY, limit=7)), 7)


def fiscal_rows(fs="CFS", rev=(100, 90, 80), op=(10, 9, 8), ni=(7, 6, 5), assets=(300, 280, 260), liab=(100, 100, 100), eq=(200, 180, 160), currency="KRW"):
    return [mrow(fs, "IS", "매출액", *rev, currency=currency), mrow(fs, "IS", "영업이익", *op, currency=currency), mrow(fs, "IS", "당기순이익(손실)", *ni, currency=currency),
            mrow(fs, "BS", "자산총계", *assets, currency=currency), mrow(fs, "BS", "부채총계", *liab, currency=currency), mrow(fs, "BS", "자본총계", *eq, currency=currency)]


def all_rows(ocf=(20, 18, 16), ppe=(8, 7, 6), intang=(2, 1, 1), owner_ni=(6, 5, 4), owner_eq=(150, 140, 130)):
    return [arow("CF", cr.OCF_ID, "영업활동현금흐름", *ocf),
            arow("CF", "ifrs-full_PurchaseOfPropertyPlantAndEquipmentClassifiedAsInvestingActivities", "유형자산의 취득", *ppe),
            arow("CF", "ifrs-full_PurchaseOfIntangibleAssetsClassifiedAsInvestingActivities", "무형자산의 취득", *intang),
            arow("IS", cr.OWNER_NI_ID, "지배기업 소유주지분", *owner_ni), arow("BS", cr.OWNER_EQ_ID, "지배기업 소유주지분", *owner_eq)]


class AnnualTableTests(unittest.TestCase):
    def test_five_years_two_calls_and_derived_ratios(self):
        main = {2025: fiscal_rows(rev=(500, 400, 300), op=(50, 40, 30), ni=(40, 30, 20), eq=(200, 150, 100), liab=(100, 90, 80)),
                2022: fiscal_rows(rev=(200, 150, 100), op=(20, 15, 10), ni=(10, 8, 6), eq=(80, 70, 60))}
        alls = {2025: all_rows(ocf=(60, 50, 40), ppe=(20, 15, 10), intang=(5, 5, 5), owner_ni=(38, 28, 18), owner_eq=(190, 140, 95)),
                2022: all_rows(ocf=(30, 25, 20), ppe=(9, 8, 7), intang=(1, 1, 1), owner_ni=(9, 7, 5), owner_eq=(75, 65, 55))}
        t = cr.annual_table(main, alls, "CFS", 2025)
        self.assertEqual([r["year"] for r in t["rows"]], [2021, 2022, 2023, 2024, 2025])
        self.assertEqual(t["ni_basis"], "지배주주")
        r25, r24, r21 = t["rows"][4], t["rows"][3], t["rows"][0]
        self.assertEqual((r25["revenue"], r25["op"], r25["ni"]), (500, 50, 38))
        self.assertAlmostEqual(r25["op_margin"], 10.0)
        self.assertAlmostEqual(r25["debt_ratio"], 50.0)                     # 부채 100 ÷ 자본(전체) 200
        self.assertAlmostEqual(r25["roe"], 38 / ((190 + 140) / 2) * 100)    # 평균 지배주주 자본
        self.assertEqual((r25["ocf"], r25["capex"], r25["fcf"]), (60, 25, 35))
        self.assertEqual(r24["revenue"], 400)                               # 같은 호출의 전기 열
        self.assertEqual(r21["revenue"], 150)                               # 2022년 호출의 전기 열 = 2021년
        self.assertEqual(t["rows"][1]["revenue"], 200)                      # 2022년 호출의 당기 열 = 2022년

    def test_owner_basis_dropped_when_any_year_missing(self):
        main = {2025: fiscal_rows(ni=(40, 30, 20), eq=(200, 150, 100))}
        alls = {2025: all_rows(owner_ni=(38, None, 18), owner_eq=(190, 140, 95))}
        t = cr.annual_table(main, alls, "CFS", 2025)
        self.assertEqual(t["ni_basis"], "전체")                              # 해마다 기준이 섞이지 않게 전체로 통일
        self.assertEqual(t["rows"][4]["ni"], 40)

    def test_young_company_keeps_owner_basis(self):
        # 2023년 상장: 2021·2022년은 통째로 비어 있어도 있는 해는 지배주주 기준을 유지해야 한다(예전엔 전체 기준으로 후퇴했다)
        main = {2025: fiscal_rows(ni=(40, 30, 20), eq=(200, 150, 100))}
        alls = {2025: all_rows(owner_ni=(38, 28, 18), owner_eq=(190, 140, 95))}
        t = cr.annual_table(main, alls, "CFS", 2025)
        self.assertEqual(t["ni_basis"], "지배주주")
        self.assertEqual([r["ni"] for r in t["rows"]], [None, None, 18, 28, 38])
        self.assertIsNone(t["rows"][0]["revenue"])

    def test_missing_years_stay_none(self):
        t = cr.annual_table({2025: fiscal_rows()}, {}, "CFS", 2025)
        self.assertIsNone(t["rows"][0]["revenue"])
        self.assertIsNone(t["rows"][4]["ocf"])
        self.assertIsNone(t["rows"][4]["fcf"])
        self.assertEqual(t["rows"][4]["revenue"], 100)


def make_raw(interim=None, all_interim=None, **kw):
    raw = {"code": "000001", "name": "테스트", "corp_code": "1", "today": TODAY, "annual_year": 2025, "fs": "CFS",
           "main_a": fiscal_rows(rev=(1000, 900, 800), op=(100, 90, 80), ni=(70, 60, 50), eq=(500, 450, 400)),
           "old": {"year": 2022, "main": fiscal_rows(rev=(700, 600, 500), op=(70, 60, 50), ni=(40, 30, 20)), "all": all_rows()},
           "all_a": all_rows(owner_ni=(66, 56, 46), owner_eq=(480, 430, 380)), "interim": interim, "all_interim": all_interim,
           "shares": {"year": 2026, "rc": "11012", "rows": [{"se": "보통주", "istc_totqy": "100", "tesstk_co": "5", "stlm_dt": "2026-06-30"}]},
           "holders": None, "treasury": None, "filings": [], "errors": {}}
    raw.update(kw)
    return raw


class BuildReportTests(unittest.TestCase):
    H1 = lambda self: {"year": 2026, "rc": "11012", "label": "반기", "main": [
        mrow("CFS", "IS", "매출액", 300, 200, add=600, prev_add=400), mrow("CFS", "IS", "영업이익", 30, 20, add=60, prev_add=40),
        mrow("CFS", "IS", "당기순이익(손실)", 20, 10, add=40, prev_add=30), mrow("CFS", "BS", "자본총계", 700, 500)]}

    def test_without_interim_uses_fiscal_year(self):
        rep = cr.build_report(make_raw(), 100.0, TODAY)
        h = rep["header"]
        self.assertFalse(h["ttm"]["is_ttm"])
        self.assertEqual(h["ttm"]["ni"], 66)                                # 지배주주 순이익
        self.assertEqual(h["market_cap"], 100.0 * 100)
        self.assertAlmostEqual(h["per"], 10000 / 66)
        self.assertEqual(h["equity"], 480)                                  # 지배주주 자본
        self.assertEqual(rep["fs"], "연결")

    def test_ttm_with_interim_owner_basis(self):
        all_interim = [arow("IS", cr.OWNER_NI_ID, "지배", 18, 9, add=36, prev_add=27), arow("BS", cr.OWNER_EQ_ID, "지배", 650, 480)]
        rep = cr.build_report(make_raw(self.H1(), all_interim), 100.0, TODAY)
        h = rep["header"]
        self.assertTrue(h["ttm"]["is_ttm"])
        self.assertEqual(h["ttm"]["revenue"], 1000 + 600 - 400)
        self.assertEqual(h["ttm"]["op"], 100 + 60 - 40)
        self.assertEqual(h["ttm"]["ni"], 66 + 36 - 27)                      # 지배주주끼리만 더하고 뺀다
        self.assertEqual(h["equity"], 650)
        self.assertIn("2026 반기", h["ttm"]["label"])
        self.assertAlmostEqual(h["per"], 10000 / 75)
        self.assertAlmostEqual(h["psr"], 10000 / 1200)

    def test_owner_basis_without_interim_detail_falls_back_not_mixes(self):
        # 사업연도는 지배주주 기준인데 분기 전체 재무제표를 못 받았다 → 전체 기준 누적과 섞지 않고 사업연도 값으로 되돌아간다
        rep = cr.build_report(make_raw(self.H1(), None), 100.0, TODAY)
        self.assertFalse(rep["header"]["ttm"]["is_ttm"])
        self.assertEqual(rep["header"]["ttm"]["ni"], 66)
        self.assertTrue(any("누적 순이익을 읽지 못해" in n for n in rep["notes"]))

    def test_total_basis_ttm_uses_main_rows(self):
        raw = make_raw(self.H1(), None, all_a=None, old={"year": 2022, "main": fiscal_rows(), "all": None})
        rep = cr.build_report(raw, 100.0, TODAY)
        self.assertEqual(rep["annual"]["ni_basis"], "전체")
        self.assertEqual(rep["header"]["ttm"]["ni"], 70 + 40 - 30)
        self.assertTrue(any("전체 재무제표를 받지 못해" in n for n in rep["notes"]))

    def test_one_quarter_report_has_no_add_columns(self):
        q1 = {"year": 2026, "rc": "11013", "label": "1분기", "main": [
            mrow("CFS", "IS", "매출액", 300, 200), mrow("CFS", "IS", "영업이익", 30, 20), mrow("CFS", "IS", "당기순이익(손실)", 20, 10), mrow("CFS", "BS", "자본총계", 600, 500)]}
        all_q1 = [arow("IS", cr.OWNER_NI_ID, "지배", 18, 9)]
        rep = cr.build_report(make_raw(q1, all_q1), 100.0, TODAY)
        self.assertEqual(rep["header"]["ttm"]["revenue"], 1000 + 300 - 200)  # 1분기는 분기 금액이 곧 누적
        self.assertEqual(rep["header"]["ttm"]["ni"], 66 + 18 - 9)

    def test_loss_company_has_no_per(self):
        raw = make_raw(main_a=fiscal_rows(ni=(-70, -60, -50)), all_a=None, old=None)
        rep = cr.build_report(raw, 100.0, TODAY)
        self.assertEqual(rep["header"]["per_status"], "loss")
        self.assertIsNone(rep["header"]["per"])
        self.assertEqual(rep["valuation"]["scenarios"], [])

    def test_preferred_shares_note(self):
        rows = [{"se": "보통주", "istc_totqy": "100", "tesstk_co": "0", "stlm_dt": "2026-06-30"}, {"se": "우선주", "istc_totqy": "20", "tesstk_co": "0"}]
        rep = cr.build_report(make_raw(shares={"year": 2026, "rc": "11012", "rows": rows}), 100.0, TODAY)
        h = rep["header"]
        self.assertEqual(h["market_cap"], 10000)
        self.assertEqual(h["market_cap_all"], 12000)
        self.assertIn("우선주", h["pref_note"])

    def test_missing_shares_blocks_multiples_but_not_report(self):
        rep = cr.build_report(make_raw(shares=None), 100.0, TODAY)
        self.assertIsNone(rep["header"]["market_cap"])
        self.assertIsNone(rep["header"]["per"])
        self.assertIsNotNone(rep["annual"]["rows"][-1]["revenue"])
        self.assertTrue(any("시가총액" in n for n in rep["notes"]))

    def test_foreign_currency_company_gets_no_multiples(self):
        raw = make_raw(main_a=fiscal_rows(currency="USD"))
        rep = cr.build_report(raw, 100.0, TODAY)
        self.assertEqual(rep["currency"], "USD")
        self.assertIsNone(rep["header"]["market_cap"])
        self.assertIsNone(rep["header"]["per"])
        self.assertEqual(rep["header"]["price"], 100.0)                     # 배수는 못 내도 원화 종가는 그대로 보여준다
        self.assertTrue(any("USD" in n for n in rep["notes"]))

    def test_empty_years_explained_once(self):
        rep = cr.build_report(make_raw(old=None), 100.0, TODAY)             # 3년 전 보고서가 없어 2021·2022년이 비는 경우
        empty = [n for n in rep["notes"] if "사업보고서 재무가 없어 비워" in n]
        self.assertEqual(len(empty), 1)
        self.assertIn("2021, 2022년", empty[0])
        self.assertFalse(any("매출액 계정을 찾지 못했습니다" in n for n in rep["notes"]))     # 같은 이유로 중복 안내하지 않는다
        self.assertEqual(rep["annual"]["ni_basis"], "지배주주")

    def test_financial_company_hides_fcf(self):
        no_rev = [r for r in fiscal_rows() if r["account_nm"] != "매출액"]
        raw = make_raw(main_a=no_rev, old={"year": 2022, "main": [r for r in fiscal_rows() if r["account_nm"] != "매출액"], "all": all_rows()})
        rep = cr.build_report(raw, 100.0, TODAY)
        self.assertTrue(rep["financial_like"])
        self.assertTrue(all(r["fcf"] is None for r in rep["annual"]["rows"]))
        self.assertIsNotNone(rep["annual"]["rows"][-1]["ocf"])
        self.assertIsNone(rep["header"]["psr"])

    def test_scenarios_from_naver_history(self):
        fin = {"financeInfo": {"trTitleList": [{"key": k, "isConsensus": "N"} for k in ("202312", "202412", "202512")],
                               "rowList": [{"title": "PER", "columns": {"202312": {"value": "30"}, "202412": {"value": "10"}, "202512": {"value": "20"}}}]}}
        rep = cr.build_report(make_raw(), 100.0, TODAY, naver_annual=fin)
        sc = {s["label"]: s for s in rep["valuation"]["scenarios"]}
        self.assertEqual(set(sc), {"Bear", "Base", "Bull"})
        self.assertAlmostEqual(sc["Base"]["cap"], 66 * 20)
        self.assertAlmostEqual(sc["Base"]["upside"], 66 * 20 / 10000 - 1)
        self.assertLess(sc["Bear"]["price"], sc["Base"]["price"])
        self.assertLess(sc["Base"]["price"], sc["Bull"]["price"])
        self.assertEqual(cr.build_report(make_raw(), 100.0, TODAY)["valuation"]["scenarios"], [])        # 근거(과거 PER)가 없으면 만들지 않는다

    def test_partial_fetch_errors_become_notes(self):
        rep = cr.build_report(make_raw(errors={"holders": "DartError: 요청 실패"}), 100.0, TODAY)
        self.assertTrue(any(n.startswith("최대주주 조회 실패") for n in rep["notes"]))

    def test_filings_flow_through(self):
        raw = make_raw(filings=[{"rcept_dt": "20261030", "report_nm": "현금ㆍ현물배당결정", "rcept_no": "20261030000001", "flr_nm": "회사"}])
        rep = cr.build_report(raw, 100.0, TODAY, risk_levels={"20261030000001": "주의"})
        self.assertEqual((rep["filings"][0]["tag"], rep["filings"][0]["risk"]), ("배당", "주의"))


class FetchRawTests(unittest.TestCase):
    """dart_core를 모의해 어떤 보고서를 어떤 순서로 찾는지만 확인한다."""

    def patch(self, mains, shares=None):
        calls = []

        def main(cc, year, rc="11011", ttl_hours=0):
            calls.append(("main", year, rc))
            return mains.get((year, rc), [])

        patches = [
            mock.patch.object(dart_core, "corp_codes", return_value={"000001": {"corp_code": "C1", "corp_name": "테스트"}}),
            mock.patch.object(dart_core, "main_accounts_cached", side_effect=main),
            mock.patch.object(dart_core, "all_statements", return_value=all_rows()),
            mock.patch.object(dart_core, "share_totals", side_effect=lambda cc, y, rc, **k: (shares or {}).get((y, rc), [])),
            mock.patch.object(dart_core, "shareholders", return_value=[{"stock_knd": "보통주", "nm": "갑"}]),
            mock.patch.object(dart_core, "treasury_stock", return_value=[]),
            mock.patch.object(dart_core, "recent_filings", return_value=[]),
            mock.patch.object(cr.segment_mix, "fetch_mix", return_value=None),      # 매출 구성은 test_segments_peers에서 따로 검증(여기선 네트워크를 타지 않게)
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        return calls

    SH = lambda self, n="100": [{"se": "보통주", "istc_totqy": n, "tesstk_co": "0", "stlm_dt": "x"}]

    def test_finds_latest_interim_in_order(self):
        calls = self.patch({(2025, "11011"): fiscal_rows(), (2022, "11011"): fiscal_rows(), (2026, "11012"): fiscal_rows(), (2026, "11013"): fiscal_rows()},
                           {(2026, "11012"): self.SH("111")})
        raw = cr.fetch_raw("000001", TODAY)
        self.assertEqual(raw["annual_year"], 2025)
        self.assertEqual(raw["interim"]["rc"], "11012")                     # 3분기는 아직 없고 반기가 가장 최근
        self.assertEqual([c[2] for c in calls if c[1] == 2026], ["11014", "11012"])         # 반기를 찾으면 1분기는 묻지 않는다
        self.assertEqual(raw["shares"]["year"], 2026)

    def test_shares_fall_back_to_annual_when_interim_has_none(self):
        self.patch({(2025, "11011"): fiscal_rows(), (2022, "11011"): fiscal_rows(), (2026, "11013"): fiscal_rows()},
                   {(2026, "11013"): [{"se": "합계", "istc_totqy": "-"}], (2025, "11011"): self.SH("90")})
        raw = cr.fetch_raw("000001", TODAY)
        self.assertEqual((raw["shares"]["year"], raw["shares"]["rc"]), (2025, "11011"))     # 1분기 보고서엔 주식수가 비어 사업보고서 값

    def test_annual_falls_back_one_year_early_in_the_year(self):
        # 1~3월에는 직전 사업연도 보고서가 아직 없다 → 그 전 해 보고서와, 그 다음 해의 3분기 보고서를 쓴다
        self.patch({(2025, "11011"): fiscal_rows(), (2022, "11011"): fiscal_rows(), (2025, "11014"): fiscal_rows()})
        raw = cr.fetch_raw("000001", dt.date(2026, 2, 10))
        self.assertEqual(raw["annual_year"], 2025)                           # today.year-1=2025 에 사업보고서가 있으면 그대로
        self.patch({(2024, "11011"): fiscal_rows(), (2021, "11011"): fiscal_rows(), (2025, "11014"): fiscal_rows()})
        raw = cr.fetch_raw("000001", dt.date(2026, 2, 10))
        self.assertEqual((raw["annual_year"], raw["interim"]["year"], raw["interim"]["rc"]), (2024, 2025, "11014"))

    def test_older_falls_back_to_two_years_when_three_years_empty(self):
        self.patch({(2025, "11011"): fiscal_rows(), (2023, "11011"): fiscal_rows(rev=(300, 200, 100))})
        raw = cr.fetch_raw("000001", TODAY)
        self.assertEqual(raw["old"]["year"], 2023)                           # 금융사처럼 2022년 호출이 빈 경우
        rep = cr.build_report(raw, 100.0, TODAY)
        self.assertEqual(rep["annual"]["rows"][0]["revenue"], 100)           # 2021년 = 2023년 보고서의 전전기 열
        self.assertEqual(rep["annual"]["rows"][1]["revenue"], 200)

    def test_no_dart_data_raises(self):
        self.patch({})
        with self.assertRaises(cr.NoDartData):
            cr.fetch_raw("000001", TODAY)
        with mock.patch.object(dart_core, "corp_codes", return_value={}):
            with self.assertRaises(cr.NoDartData):
                cr.fetch_raw("999999", TODAY)

    def test_side_fetch_error_does_not_break_report(self):
        self.patch({(2025, "11011"): fiscal_rows(), (2022, "11011"): fiscal_rows()}, {(2025, "11011"): self.SH()})
        with mock.patch.object(dart_core, "shareholders", side_effect=dart_core.DartError("요청 한도 초과")):
            raw = cr.fetch_raw("000001", TODAY)
        self.assertIn("holders", raw["errors"])
        self.assertIsNone(raw["holders"])
        rep = cr.build_report(raw, 100.0, TODAY)
        self.assertIsNone(rep["holders"])
        self.assertTrue(any("최대주주 조회 실패" in n for n in rep["notes"]))


class CachedListTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        p = mock.patch.object(dart_core, "CACHE_DIR", self.tmp.name)
        p.start()
        self.addCleanup(p.stop)

    def test_second_call_is_served_from_disk_and_key_is_not_stored(self):
        with mock.patch.object(dart_core, "get_json", return_value={"list": [{"a": 1}]}) as gj:
            self.assertEqual(dart_core.cached_list("x.json", corp_code="1", bsns_year="2025"), [{"a": 1}])
            self.assertEqual(dart_core.cached_list("x.json", corp_code="1", bsns_year="2025"), [{"a": 1}])
            self.assertEqual(gj.call_count, 1)
            dart_core.cached_list("x.json", corp_code="2", bsns_year="2025")                  # 다른 인자는 별개 캐시
            self.assertEqual(gj.call_count, 2)
        for name in os.listdir(self.tmp.name):
            with open(os.path.join(self.tmp.name, name), encoding="utf-8") as f:
                self.assertNotIn("crtfc", f.read())

    def test_empty_result_expires_sooner_than_data(self):
        with mock.patch.object(dart_core, "get_json", return_value={}) as gj:                  # 013(데이터 없음)은 빈 dict
            self.assertEqual(dart_core.cached_list("x.json", ttl_hours=72, corp_code="1"), [])
            dart_core.cached_list("x.json", ttl_hours=72, corp_code="1")
            self.assertEqual(gj.call_count, 1)
            with mock.patch.object(dart_core.time, "time", return_value=dart_core.time.time() + (dart_core.EMPTY_TTL_HOURS + 1) * 3600):
                dart_core.cached_list("x.json", ttl_hours=72, corp_code="1")
            self.assertEqual(gj.call_count, 2)                                                 # 빈 응답은 6시간 뒤 다시 묻는다

    def test_errors_are_not_cached(self):
        with mock.patch.object(dart_core, "get_json", side_effect=dart_core.DartError("실패")):
            with self.assertRaises(dart_core.DartError):
                dart_core.cached_list("x.json", corp_code="1")
        with mock.patch.object(dart_core, "get_json", return_value={"list": [{"ok": 1}]}):
            self.assertEqual(dart_core.cached_list("x.json", corp_code="1"), [{"ok": 1}])


if __name__ == "__main__":
    unittest.main(verbosity=1)
