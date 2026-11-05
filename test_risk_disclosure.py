# -*- coding: utf-8 -*-
"""risk_disclosure 단위 테스트 — 네트워크 없이 분류·구간·중복 제거·근거 수치를 확인한다.

    python test_risk_disclosure.py
"""
import datetime as dt
import json
import os
import unittest

import risk_disclosure as rd

D = dt.date(2026, 11, 3)


def row(report, date, rno="20260101000001", cls="K"):
    return {"corp_cls": cls, "stock_code": "123456", "corp_name": "테스트", "rcept_dt": date, "rcept_no": rno, "report_nm": report}


class CategorizeTests(unittest.TestCase):
    def test_real_report_names(self):
        cases = [
            ("감자", "주요사항보고서(감자결정)", "reduction"),
            ("관리종목·상폐", "주권매매거래정지기간변경(상장폐지 사유 발생)", "delist"),
            ("관리종목·상폐", "내부결산시점관리종목지정ㆍ형식적상장폐지ㆍ상장적격성실질심사사유발생", "delist"),
            ("관리종목·상폐", "기타시장안내(관리종목지정우려종목)              (시가총액 200억원 미달)", "watch"),
            ("관리종목·상폐", "기타시장안내              (상장적격성 실질심사 대상결정 기한 안내)", None),
            ("관리종목·상폐", "주권매매거래정지기간변경(상장적격성 실질심사 대상 결정)", None),
            ("감사의견", "반기검토(감사)의견부적정등사실확인(자본잠식률100분의50이상또는자기자본10억원미만포함)", "audit"),
            ("유상증자", "주요사항보고서(유상증자결정)", None),
            ("횡령배임", "횡령ㆍ배임혐의발생", None),
        ]
        for typ, name, want in cases:
            self.assertEqual(rd.categorize(typ, name), want, name)


class FlagTests(unittest.TestCase):
    def test_window_and_order(self):
        b = [row("주요사항보고서(감자결정)", "20261020")]                       # 14일 전
        i = [row("주권매매거래정지기간변경(상장폐지 사유 발생)", "20260801")]    # 94일 전
        flags = rd.flags_from_rows(b, i, D)
        self.assertEqual([f["cat"] for f in flags], ["reduction", "delist"])    # 최신순
        self.assertEqual([f["in_window"] for f in flags], [True, False])
        self.assertEqual(flags[0]["days_ago"], 14)
        self.assertTrue(flags[0]["url"].endswith("20260101000001"))

    def test_window_boundary(self):
        edge = (D - dt.timedelta(days=rd.WINDOW_DAYS)).strftime("%Y%m%d")
        over = (D - dt.timedelta(days=rd.WINDOW_DAYS + 1)).strftime("%Y%m%d")
        self.assertTrue(rd.flags_from_rows([row("주요사항보고서(감자결정)", edge)], [], D)[0]["in_window"])
        self.assertFalse(rd.flags_from_rows([row("주요사항보고서(감자결정)", over)], [], D)[0]["in_window"])

    def test_corrections_and_future_and_unflagged_are_skipped(self):
        b = [row("[기재정정]주요사항보고서(감자결정)", "20261020"), row("주요사항보고서(감자결정)", "20261110"),
             row("주요사항보고서(유상증자결정)", "20261020")]
        i = [row("기타시장안내(상장적격성 실질심사 대상결정 기한 안내)", "20261020"), row("자본잠식", "20261020")]
        self.assertEqual(rd.flags_from_rows(b, i, D), [])

    def test_repeated_notice_within_30_days_keeps_first(self):
        i = [row("기타시장안내(관리종목지정우려종목)(주가 1,000원 미달)", "20261001", rno="A"),
             row("기타시장안내(관리종목지정우려종목)(주가 1,000원 미달)", "20261015", rno="B"),
             row("기타시장안내(관리종목지정우려종목)(주가 1,000원 미달)", "20261101", rno="C")]
        flags = rd.flags_from_rows([], i, D)
        self.assertEqual(sorted(f["rno"] for f in flags), ["A", "C"])           # 10/15는 10/1의 30일 안, 11/1은 밖

    def test_non_listed_class_is_ignored(self):
        self.assertEqual(rd.flags_from_rows([row("주요사항보고서(감자결정)", "20261020", cls="E")], [], D), [])

    def test_short_text_prefers_warning_over_caution(self):
        i = [row("기타시장안내(관리종목지정우려종목)(주가 1,000원 미달)", "20261030"),
             row("주권매매거래정지기간변경(상장폐지 사유 발생)", "20261010")]
        self.assertEqual(rd.short_text(rd.flags_from_rows([], i, D)), "🔴 관리종목·상장폐지 사유 (24일 전)")
        self.assertEqual(rd.short_text(rd.flags_from_rows([], [], D)), "")


class EvidenceTests(unittest.TestCase):
    def test_line_uses_stored_numbers(self):
        line = rd.evidence_line("reduction")
        for part in ("492건", "5거래일", "평균 −5.0%","−10% 이하 급락 24.8%", "4.4%", "+10% 이상 급등 6.3%"):
            self.assertIn(part, line)

    def test_every_category_has_evidence(self):
        for cat, meta in rd.CATEGORIES.items():
            self.assertIn(meta["k"], rd.EVIDENCE[cat]["k"])
            self.assertTrue(rd.evidence_line(cat))

    def test_matches_saved_study_when_present(self):
        """산출 파일(dart_cache, gitignore)이 있으면 코드에 옮긴 수치와 대조한다. 없으면 건너뜀."""
        base = os.path.join(os.path.dirname(os.path.abspath(__file__)), "dart_cache")
        path = os.path.join(base, "risk_flag_evidence.json")
        if not os.path.exists(path):
            self.skipTest("risk_flag_evidence.json 없음")
        with open(path, encoding="utf-8") as f:
            saved = json.load(f)
        names = {"reduction": "감자 결정", "delist": "관리종목 지정·상장폐지 사유", "watch": "제외/지정우려 안내", "audit": "감사의견 부적정·거절·자본잠식 확인"}
        for cat, g in names.items():
            for k, (n, mean, tail, up) in rd.EVIDENCE[cat]["k"].items():
                s = saved["groups"][g]["k"][k]
                self.assertEqual(n, s["n"])
                for a, b in ((mean, s["mean"]), (tail, s["tail"]), (up, s["tail_up"])):
                    self.assertAlmostEqual(a, b, delta=6e-5, msg=f"{cat} k={k}")
        for k in rd.BASE:
            self.assertAlmostEqual(rd.BASE[k]["tail"], saved["bench"][k]["tail"], delta=6e-5)
            self.assertAlmostEqual(rd.BASE[k]["tail_up"], saved["bench"][k]["tail_up"], delta=6e-5)


if __name__ == "__main__":
    unittest.main()
