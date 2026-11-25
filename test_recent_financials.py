# -*- coding: utf-8 -*-
"""최근 분기 실적 정리 테스트 — 네이버 응답 모양의 합성 데이터만 쓴다(네트워크 없음).  python -m unittest test_recent_financials

픽스처 숫자는 121600(나노신소재)의 2026-11 시점 네이버 분기 재무를 그대로 옮긴 것이다."""
import datetime as dt
import unittest

import recent_financials as rf

KEYS = ["202509", "202512", "202603", "202606", "202609", "202612"]
ROWS = {
    "매출액": ["312", "288", "327", "365", "424", "414"],
    "영업이익": ["9", "16", "28", "53", "66", "66"],
    "당기순이익": ["16", "12", "65", "60", "19", "8"],
    "영업이익률": ["2.89", "5.69", "8.42", "14.61", "15.60", "15.82"],
    "순이익률": ["5.17", "4.21", "19.86", "16.55", "4.57", "1.93"],
    "ROE": ["-2.17", "0.66", "2.95", "6.13", "6.32", "-"],
    "부채비율": ["83.84", "84.11", "50.91", "47.85", "36.63", "-"],
    "당좌비율": ["127.54", "127.93", "146.78", "149.69", "154.89", "-"],
}


def make_info(keys=KEYS, consensus_from="202612", rows=ROWS):
    return {
        "trTitleList": [
            {"key": k, "title": rf.period_label(k) + ".", "isConsensus": "Y" if k >= consensus_from else "N"}
            for k in keys
        ],
        "rowList": [
            {"title": title, "columns": {k: {"value": vals[i]} for i, k in enumerate(KEYS) if k in keys}}
            for title, vals in rows.items()
        ],
    }


TODAY = dt.date(2026, 11, 25)


class ParseTest(unittest.TestCase):
    def test_picks_latest_confirmed_quarter_not_consensus(self):
        p = rf.parse_quarterly(make_info(), TODAY)
        self.assertEqual(p["latest"]["key"], "202609")
        self.assertEqual(p["latest"]["label"], "2026.09")
        self.assertEqual([q["key"] for q in p["quarters"]], KEYS[:5])  # 컨센서스 열 제외
        self.assertEqual(p["days_since_quarter_end"], 56)

    def test_latest_values(self):
        q = rf.parse_quarterly(make_info(), TODAY)["latest"]
        self.assertEqual(q["revenue"], 424.0)
        self.assertEqual(q["op_income"], 66.0)
        self.assertAlmostEqual(q["op_margin"], 15.60)
        self.assertAlmostEqual(q["roe"], 6.32)
        self.assertAlmostEqual(q["debt_ratio"], 36.63)

    def test_dash_and_missing_become_none(self):
        rows = {**ROWS, "ROE": ["-"] * 6}
        q = rf.parse_quarterly(make_info(rows=rows), TODAY)["latest"]
        self.assertIsNone(q["roe"])

    def test_unreported_quarter_without_consensus_flag_is_skipped(self):
        # 컨센서스 표시 없이 매출·영업이익이 '-'인 열도 확정 실적으로 치지 않는다
        rows = {k: v[:4] + ["-", "-"] if k in ("매출액", "영업이익") else v for k, v in ROWS.items()}
        p = rf.parse_quarterly(make_info(consensus_from="999999", rows=rows), TODAY)
        self.assertEqual(p["latest"]["key"], "202606")

    def test_only_last_five_quarters(self):
        keys = ["202409", "202412", "202503"] + KEYS
        rows = {t: ["1", "1", "1"] + v for t, v in ROWS.items()}
        info = {
            "trTitleList": [{"key": k, "isConsensus": "Y" if k >= "202612" else "N"} for k in keys],
            "rowList": [
                {"title": t, "columns": {k: {"value": vals[i]} for i, k in enumerate(keys)}} for t, vals in rows.items()
            ],
        }
        p = rf.parse_quarterly(info, TODAY)
        self.assertEqual(len(p["quarters"]), 5)
        self.assertEqual(p["quarters"][0]["key"], "202509")

    def test_empty_inputs(self):
        self.assertIsNone(rf.parse_quarterly(None, TODAY))
        self.assertIsNone(rf.parse_quarterly({}, TODAY))
        self.assertIsNone(rf.parse_quarterly({"trTitleList": [], "rowList": []}, TODAY))


class StalenessTest(unittest.TestCase):
    def test_fresh_when_next_quarter_not_yet_due(self):
        self.assertIsNone(rf.staleness_note("202609", TODAY))                   # 12월 분기는 3월 말까지
        self.assertIsNone(rf.staleness_note("202606", dt.date(2026, 11, 10)))  # 9월 분기 기한 11-14 전

    def test_stale_when_due_date_passed(self):
        note = rf.staleness_note("202606", TODAY)  # 9월 분기 기한 11-14(+5일 여유)이 지났는데 없음
        self.assertIn("2026.09", note)
        self.assertIn("반영되지 않았습니다", note)

    def test_year_end_quarter_uses_90_day_deadline(self):
        self.assertIsNone(rf.staleness_note("202609", dt.date(2027, 4, 1)))     # 기한 3-31, 여유 4-5까지
        self.assertIsNotNone(rf.staleness_note("202609", dt.date(2027, 4, 10)))

    def test_stale_note_reaches_parse_result(self):
        rows = {k: v[:4] + ["-", "-"] if k in ("매출액", "영업이익") else v for k, v in ROWS.items()}
        p = rf.parse_quarterly(make_info(consensus_from="999999", rows=rows), TODAY)
        self.assertIsNotNone(p["stale_note"])


class ChangeTest(unittest.TestCase):
    def test_pct_change(self):
        self.assertEqual(rf._pct_change_text(424, 312), "+35.9%")
        self.assertEqual(rf._pct_change_text(66, 9), "+633.3%")
        self.assertEqual(rf._pct_change_text(80, 100), "-20.0%")

    def test_loss_cases_do_not_print_misleading_percent(self):
        self.assertEqual(rf._pct_change_text(19, -10), "적자→흑자 전환")
        self.assertEqual(rf._pct_change_text(-5, -10), "적자 지속(적자 폭 축소)")
        self.assertEqual(rf._pct_change_text(-15, -10), "적자 지속(적자 폭 확대)")
        self.assertEqual(rf._pct_change_text(5, 0), "비교 불가(기준 0)")
        self.assertEqual(rf._pct_change_text(None, 3), "비교 불가")

    def test_pp_change(self):
        self.assertEqual(rf._pp_change_text(15.60, 2.89), "+12.71%p")
        self.assertEqual(rf._pp_change_text(36.63, 83.84), "-47.21%p")

    def test_change_lines_yoy_and_qoq(self):
        lines = rf.change_lines(rf.parse_quarterly(make_info(), TODAY))
        self.assertEqual(len(lines), 2)
        yoy, qoq = lines
        self.assertIn("전년 동기 대비(2025.09 → 2026.09)", yoy)
        self.assertIn("매출액 +35.9%", yoy)
        self.assertIn("영업이익 +633.3%", yoy)
        self.assertIn("영업이익률 +12.71%p", yoy)
        self.assertIn("부채비율 -47.21%p", yoy)
        self.assertIn("전 분기 대비(2026.06 → 2026.09)", qoq)
        self.assertIn("매출액 +16.2%", qoq)

    def test_change_lines_skip_missing_comparison(self):
        info = make_info(keys=["202606", "202609", "202612"])
        lines = rf.change_lines(rf.parse_quarterly(info, TODAY))
        self.assertEqual(len(lines), 1)            # 전년 동기(202509)는 데이터에 없음 → 전 분기만
        self.assertIn("전 분기 대비", lines[0])


class ProfitabilityTest(unittest.TestCase):
    def test_ttm_op_margin_uses_last_four_consecutive_quarters(self):
        p = rf.parse_quarterly(make_info(), TODAY)
        # (16+28+53+66) / (288+327+365+424) = 163 / 1404
        self.assertAlmostEqual(rf.ttm_op_margin(p), 163 / 1404 * 100)

    def test_ttm_none_when_quarters_not_consecutive_or_missing(self):
        gap = rf.parse_quarterly(make_info(keys=["202509", "202603", "202606", "202609", "202612"]), TODAY)
        self.assertIsNone(rf.ttm_op_margin(gap))          # 2025.12가 빠져 4분기가 이어지지 않음
        short = rf.parse_quarterly(make_info(keys=["202606", "202609", "202612"]), TODAY)
        self.assertIsNone(rf.ttm_op_margin(short))
        rows = {**ROWS, "영업이익": ["9", "-", "28", "53", "66", "66"]}
        blank = rf.parse_quarterly(make_info(rows=rows), TODAY)
        self.assertIsNone(rf.ttm_op_margin(blank))        # 4분기 안에 빈 칸

    def test_resolve_prefers_latest_over_annual(self):
        p = rf.parse_quarterly(make_info(), TODAY)
        r = rf.resolve_profitability(4.19, 0.66, p)       # 스크린샷의 연간 값
        self.assertAlmostEqual(r["op_margin"], 163 / 1404 * 100)
        self.assertAlmostEqual(r["roe"], 6.32)
        self.assertIn("최근 4분기 합산", r["op_basis"])
        self.assertIn("2026.09", r["op_basis"])
        self.assertIn("2026.09", r["roe_basis"])

    def test_resolve_falls_back_to_annual_without_quarters(self):
        r = rf.resolve_profitability(4.19, 0.66, None)
        self.assertEqual((r["op_margin"], r["roe"]), (4.19, 0.66))
        self.assertIn("연간", r["op_basis"])
        self.assertIn("연간", r["roe_basis"])

    def test_resolve_falls_back_per_field(self):
        # ROE만 비면 ROE만 연간 값, 영업이익률은 4분기 합산
        rows = {**ROWS, "ROE": ["-"] * 6}
        r = rf.resolve_profitability(4.19, 0.66, rf.parse_quarterly(make_info(rows=rows), TODAY))
        self.assertEqual(r["roe"], 0.66)
        self.assertIn("연간", r["roe_basis"])
        self.assertIn("4분기 합산", r["op_basis"])
        # 4분기가 안 이어지면 최근 분기 한 개 값
        gap = rf.parse_quarterly(make_info(keys=["202509", "202603", "202606", "202609", "202612"]), TODAY)
        r2 = rf.resolve_profitability(4.19, 0.66, gap)
        self.assertAlmostEqual(r2["op_margin"], 15.60)
        self.assertIn("최근 분기", r2["op_basis"])

    def test_shift_key(self):
        self.assertEqual(rf.shift_key("202609", -3), "202606")
        self.assertEqual(rf.shift_key("202603", -3), "202512")
        self.assertEqual(rf.shift_key("202512", 3), "202603")


class FormatTest(unittest.TestCase):
    def test_prompt_block_has_latest_quarter_and_changes(self):
        text = rf.format_prompt_block(rf.parse_quarterly(make_info(), TODAY))
        self.assertIn("가장 최근 확정 분기: 2026.09", text)
        self.assertIn("2026.09 | 424 | 66 | 19 | 15.60 | 4.57 | 6.32 | 36.63 | 154.89", text)
        self.assertIn("코드가 계산한 값", text)
        self.assertNotIn("2026.12", text)          # 컨센서스 분기는 들어가지 않는다
        self.assertNotIn("⚠", text)

    def test_prompt_block_shows_stale_warning(self):
        rows = {k: v[:4] + ["-", "-"] if k in ("매출액", "영업이익") else v for k, v in ROWS.items()}
        text = rf.format_prompt_block(rf.parse_quarterly(make_info(consensus_from="999999", rows=rows), TODAY))
        self.assertIn("⚠", text)

    def test_table_rows(self):
        rows = rf.quarter_table_rows(rf.parse_quarterly(make_info(), TODAY))
        self.assertEqual(rows[-1]["분기"], "2026.09")
        self.assertEqual(rows[-1]["영업이익률(%)"], "15.60")
        self.assertEqual(rows[-1]["매출액(억원)"], "424")

    def test_format_multiple(self):
        self.assertEqual(rf.format_multiple(48.714, "x"), "48.71배")
        self.assertEqual(rf.format_multiple(0.0, "산출 불가"), "산출 불가")
        self.assertEqual(rf.format_multiple(-3.0, "산출 불가"), "산출 불가")
        self.assertEqual(rf.format_multiple(None, "산출 불가"), "산출 불가")


class FetchTest(unittest.TestCase):
    class _Res:
        def __init__(self, status, payload):
            self.status_code, self._payload = status, payload

        def json(self):
            return self._payload

    class _Session:
        def __init__(self, res=None, exc=None):
            self.res, self.exc, self.urls = res, exc, []

        def get(self, url, timeout=None):
            self.urls.append(url)
            if self.exc:
                raise self.exc
            return self.res

    def test_fetch_ok(self):
        s = self._Session(self._Res(200, {"financeInfo": make_info()}))
        p = rf.fetch_quarterly(s, "121600", TODAY)
        self.assertEqual(p["latest"]["key"], "202609")
        self.assertTrue(s.urls[0].endswith("/121600/finance/quarter"))

    def test_fetch_failures_return_none(self):
        self.assertIsNone(rf.fetch_quarterly(self._Session(self._Res(503, {})), "1", TODAY))
        self.assertIsNone(rf.fetch_quarterly(self._Session(exc=TimeoutError()), "1", TODAY))
        self.assertIsNone(rf.fetch_quarterly(self._Session(self._Res(200, {"financeInfo": None})), "1", TODAY))


if __name__ == "__main__":
    unittest.main()
