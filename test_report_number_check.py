# -*- coding: utf-8 -*-
"""AI 리포트 숫자 검증기 테스트(네트워크 없음).  python -m unittest test_report_number_check

문장 예시는 실제 리포트(나노신소재 121600, 2026-11-25)에서 AI가 낸 표현을 옮긴 것이다."""
import unittest

import report_number_check as rnc

FACTS = """
[최근 분기 실적]
2026.09 | 424 | 66 | 19 | 15.60 | 4.57 | 6.32 | 36.63 | 154.89
- 전년 동기 대비(2025.09 → 2026.09): 매출액 +35.9%, 영업이익 +633.3%, 영업이익률 +12.71%p, 부채비율 -47.21%p
- 최근 결산연도(2025.12) 연간: 영업이익률 4.19%, ROE 0.66%
- 후행 PER: 48.71배, 동일업종 PER 중앙값: 59.27배, PBR: 2.95배
- 현재가: 62,500원, RSI (14): 48.81, 볼린저밴드 %B: 0.34 (하단 55,010원 / 상단 76,889원)
- 참고 손절가: 50,020원, 참고 목표가: 81,218원
- [2026-11-24] [비건뉴스] 보유 보통주 15만주 26일 소각
"""


def flagged(report):
    return [u["token"] for u in rnc.check_report(report, FACTS)["unverified"]]


class MatchTest(unittest.TestCase):
    def test_numbers_present_in_facts_pass(self):
        report = ("영업이익률 **15.60 %** (+12.71 %p), 부채비율 36.63 %, ROE 6.32 %, 연간 4.19 %.\n"
                  "후행 PER 48.71배, PBR 2.95배, 현재가 62,500원, 목표가 81,218원, 손절가 50,020원.")
        r = rnc.check_report(report, FACTS)
        self.assertEqual(r["unverified"], [])
        self.assertGreaterEqual(r["checked"], 10)
        self.assertEqual(r["annotated"], report)

    def test_rounding_and_truncation_one_digit_are_allowed(self):
        self.assertEqual(flagged("영업이익률 15.6 %, 부채비율 36.6 %, 전년 대비 +12.7 %p, 부채비율 1,282 %"), ["1,282 %"])
        # 1,282 %는 위 FACTS에 없는 값이라 걸리고, 15.6·36.6·12.7은 소수 한 자리 반올림이라 통과

    def test_sign_and_dash_variants_ignored(self):
        self.assertEqual(flagged("부채비율 ‑47.21 %p, 부채비율 −47.21 %p, 부채비율 -47.21 %p, +633.3 %"), [])

    def test_fabricated_numbers_are_flagged(self):
        report = "현금성 자산 1.2억 원, 총부채 3.7억 원, 부채비율 ≈310 %"
        tokens = flagged(report)
        self.assertIn("1.2억 원", tokens)
        self.assertIn("3.7억 원", tokens)
        self.assertIn("310 %", tokens)

    def test_self_computed_numbers_are_flagged(self):
        self.assertEqual(flagged("영업이익률이 전년 대비 11.4배 상승, PER은 중앙값의 약 82 % 수준"), ["11.4배", "82 %"])
        self.assertEqual(flagged("목표가 약 75,000원, 볼린저 하단 ≈54,000원"), ["75,000원", "54,000원"])

    def test_annotation_marks_each_unverified_in_place(self):
        r = rnc.check_report("PER은 약 82 % 수준이고 PBR은 2.95배, 목표 75,000원.", FACTS)
        self.assertEqual(r["annotated"], "PER은 약 82 %⚠️ 수준이고 PBR은 2.95배, 목표 75,000원⚠️.")

    def test_duplicates_listed_once_but_all_marked(self):
        r = rnc.check_report("약 82 % 이고 다시 82 % 라고 함", FACTS)
        self.assertEqual(len(r["unverified"]), 1)
        self.assertEqual(r["annotated"].count("⚠️"), 2)


class ExemptionTest(unittest.TestCase):
    def test_dates_periods_and_list_numbers_ignored(self):
        report = ("작성일: 2026-11-25\n주가 기준일: 2026‑11‑24\n재무 기준: 2026.09 분기, 2025.12 연간, 2026년 3분기\n"
                  "### 1‑1. 수익성\n1. **첫째**\n2) 둘째\n11월 24일")
        self.assertEqual(rnc.check_report(report, FACTS)["unverified"], [])
        self.assertEqual(rnc.check_report(report, FACTS)["checked"], 0)

    def test_unitless_integers_not_checked_but_unitless_decimals_are(self):
        self.assertEqual(flagged("RSI(14)가 30 이하, 20일선 아래, 5주/20주 이평, 1개월 동안 3건"), [])
        self.assertEqual(flagged("RSI는 48.81, %B는 0.34, 스토캐스틱은 41.7"), ["41.7"])

    def test_thresholds_are_not_data(self):
        self.assertEqual(flagged("볼린저밴드 %B ≤ 0.10 이면 매수, RSI가 30 이하, 손절 라인을 15 % 이하로"), [])
        self.assertEqual(flagged("%B < 0.25 일 때"), [])

    def test_position_sizing_percent_is_exempt_but_prices_are_not(self):
        self.assertEqual(flagged("분할 매수 (예: 30 % → 50 % 비중), 부분 청산(예: 50 % 이익 실현)"), [])
        self.assertEqual(flagged("(예: 1,875원당 30 % 비중)"), ["1,875원"])

    def test_markdown_heading_numbers_ignored(self):
        self.assertEqual(flagged("### 4.3 리스크 관리\n## 4.4 요약\n#### **2.1** 기술"), [])

    def test_remaining_position_percent_exempt(self):
        self.assertEqual(flagged("1차 매수 후 **잔여 40 %**는 목표가 근접 시 매수"), [])

    def test_far_away_word_does_not_exempt_data_claim(self):
        # '비중'이 숫자 바로 뒤가 아니라 멀리 있으면 데이터 주장으로 보고 검사한다
        self.assertEqual(flagged("현금 23 % 이고 이 정도면 부채가 차지하는 비중이 낮다"), ["23 %"])

    def test_rounded_price_within_one_percent_accepted_only_when_hedged(self):
        self.assertEqual(flagged("목표가 ≈81,200원, 약 62,400원, 손절 ~50,000원"), [])          # 81,218·62,500·50,020의 1% 안
        self.assertEqual(flagged("목표가 81,200원"), ["81,200원"])                             # 어림 표시 없으면 그대로 검사
        self.assertEqual(flagged("볼린저 하단 ≈54,000원"), ["54,000원"])                        # 55,010과 1% 넘게 다름
        self.assertEqual(flagged("목표가 약 75,000원"), ["75,000원"])                           # 81,218과 7% 차이 → 임의 목표가
        self.assertEqual(flagged("81,200원 부근에서 청산, 62,400원 수준"), [])                    # 뒤에 붙는 어림 표현도 인정

    def test_news_numbers_present_in_facts_pass(self):
        # 뉴스 제목의 숫자는 facts에 들어 있으므로 그대로 인용하면 통과
        self.assertEqual(flagged("자사주 15만주 소각"), [])

    def test_word_lookalikes_are_not_units(self):
        self.assertEqual(flagged("2 조건을 만족, 3 배당 정책, 5 원가 구조"), [])

    def test_no_facts_flags_everything_with_unit(self):
        r = rnc.check_report("영업이익률 15.6 %", "")
        self.assertEqual([u["token"] for u in r["unverified"]], ["15.6 %"])


class FactsTest(unittest.TestCase):
    def test_extract_facts_ignores_dates(self):
        facts = rnc.extract_facts("2026-11-25 작성, 2026.09 분기, 15.60 %, 62,500원")
        values = {str(f) for f in facts}
        self.assertIn("15.60", values)
        self.assertIn("62500", values)
        self.assertNotIn("2026", values)
        self.assertNotIn("11", values)


if __name__ == "__main__":
    unittest.main()
