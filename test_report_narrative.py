# -*- coding: utf-8 -*-
"""3단계(LLM 서술) 테스트 — 합성 DART XML·가짜 LLM만 쓴다(네트워크 없음).  python -m unittest test_report_narrative"""
import datetime as dt
import json
import os
import sys
import tempfile
import types
import unittest
from unittest import mock

import dart_core
import filing_text as ft
import report_narrative as rn


# --- 합성 사업보고서 ------------------------------------------------------------------------------
def sec(level, title, body=""):
    return f"<SECTION-{level}><TITLE>{title}</TITLE>{body}</SECTION-{level}>"


def para(text):
    return f"<P>{text}</P>"


BUSINESS_XML = (
    '<?xml version="1.0" encoding="utf-8"?><DOCUMENT><DOCUMENT-NAME ACODE="11011">사업보고서</DOCUMENT-NAME><BODY>'
    + sec(1, "I. 회사의 개요", sec(2, "1. 회사의 개요", para("회사 소개입니다.")))
    + sec(1, "II. 사업의 내용",
          sec(2, "1. 사업의 개요", para("당사는 MLCC를 만듭니다. 2025년 매출액은 전년 대비 9.9% 증가한 11조 3,145억원입니다.") + para("주요 매출처는 삼성전자이며 비중은 약 27%입니다."))
          + sec(2, "2. 주요 제품 및 서비스", para("MLCC, 기판"))
          + sec(2, "7. 기타 참고사항", sec(3, "가. 지적재산권", para("특허 11,296건"))))
    + sec(1, "IV. 이사의 경영진단 및 분석의견",
          sec(2, "1. 예측정보에 대한 유의사항", para("예측정보 주의"))
          + sec(2, "3. 재무상태 및 영업실적",
                "<TABLE><TR><TD>구분</TD><TD>제53기</TD></TR><TR><TD>자산</TD><TD>14,595,959</TD></TR></TABLE>"
                + para("영업이익은 전년 대비 24% 증가한 9,133억원이며 AI 서버 수요 증가가 원인입니다.")))
    + sec(1, "XI. 그 밖에 투자자 보호를 위하여 필요한 사항",
          sec(2, "2. 우발부채 등에 관한 사항", para("가. 중요한 소송사건") + para("해당사항 없음."))
          + sec(2, "3. 제재 등과 관련된 사항",
                para("가. 행정기관의 제재현황")
                + "<TABLE><TR><TD>일자</TD><TD>기관</TD><TD>내용</TD></TR><TR><TD>2025.10.21</TD><TD>고용노동부</TD><TD>산업안전보건법 위반 과태료 3.2백만원</TD></TR></TABLE>")
          + sec(2, "4. 작성기준일 이후 발생한 주요사항 등 기타사항", para("해당사항 없음")))
    + "</BODY></DOCUMENT>")

AUDIT_XML = (
    '<?xml version="1.0" encoding="utf-8"?><DOCUMENT><DOCUMENT-NAME ACODE="00760">연결감사보고서</DOCUMENT-NAME><BODY>'
    + sec(1, "독립된 감사인의 감사보고서",
          para("감사의견을 위한 근거로서 충분하고 적합하다고 우리는 믿습니다.")
          + para("핵심감사사항")
          + para("핵심감사사항은 우리의 전문가적 판단에 따라 당기 연결재무제표감사에서 가장 유의적인 사항들입니다. 우리는 이런 사항에 대하여 별도의 의견을 제공하지는 않습니다.")
          + para("영업권 손상검사") + para("영업권 금액의 규모(118,215백만원)가 유의적이며 미래현금흐름 추정에 경영진의 판단이 포함되므로 손상검사에 관심을 기울였습니다. " * 3)
          + para("재무제표에 대한 경영진과 지배기구의 책임") + para("경영진은 재무제표를 작성할 책임이 있습니다."))
    + "</BODY></DOCUMENT>")
RAW = AUDIT_XML + "\n" + BUSINESS_XML


class FilingTextTest(unittest.TestCase):
    def test_split_documents_by_name(self):
        d = ft.split_documents(RAW)
        self.assertIn("II. 사업의 내용", d["business"])
        self.assertEqual(len(d["audit"]), 1)
        self.assertIn("핵심감사사항", d["audit"][0])

    def test_extract_sections_by_title(self):
        s = ft.extract_sections(RAW)
        self.assertIn("당사는 MLCC를 만듭니다", s["overview"]["text"])
        self.assertNotIn("회사 소개입니다", s["overview"]["text"])          # 'I. 회사의 개요'는 다른 장
        self.assertEqual(s["products"]["text"], "MLCC, 기판")
        self.assertIn("특허 11,296건", s["other_ref"]["text"])               # 3단계 소절은 상위 절에 포함
        self.assertNotIn("예측정보", s["mda"]["text"])                       # 1. 예측정보 절은 섞이지 않는다
        self.assertIn("고용노동부", s["sanctions"]["text"])
        self.assertNotIn("risk_mgmt", s)                                     # 없는 절은 건너뛴다

    def test_section_stops_at_next_heading(self):
        s = ft.extract_sections(RAW)
        self.assertNotIn("MLCC, 기판", s["overview"]["text"])
        self.assertNotIn("재무상태", s["overview"]["text"])

    def test_tables_become_rows_and_mda_drops_tables(self):
        s = ft.extract_sections(RAW)
        self.assertIn("2025.10.21 | 고용노동부 | 산업안전보건법 위반 과태료 3.2백만원", s["sanctions"]["text"])
        self.assertNotIn("14,595,959", s["mda"]["text"])                     # 경영진단은 서술만
        self.assertIn("AI 서버 수요 증가", s["mda"]["text"])

    def test_to_text_table_merges_duplicate_cells_and_nested(self):
        t = ft.to_text("<TABLE><TR><TD>가</TD><TD>가</TD><TD>나</TD></TR><TR><TD>다 <TABLE><TR><TD>중첩</TD></TR></TABLE></TD><TD>1,234</TD></TR></TABLE>")
        self.assertEqual(t.split("\n"), ["가 | 나", "다 중첩 | 1,234"])

    def test_extract_kam_uses_intro_sentence_and_stops_before_responsibility(self):
        k = ft.extract_kam(RAW)
        self.assertIn("영업권 손상검사", k["text"])
        self.assertNotIn("작성할 책임", k["text"])
        self.assertNotIn("별도의 의견", k["text"])

    def test_kam_heading_glued_to_intro(self):
        glued = AUDIT_XML.replace("<P>핵심감사사항</P><P>핵심감사사항은", "<P>핵심감사사항</P>핵심감사사항은")
        self.assertIsNotNone(ft.extract_kam(glued + BUSINESS_XML))

    def test_no_audit_documents_means_no_kam(self):
        self.assertIsNone(ft.extract_kam(BUSINESS_XML))

    def test_contains_quote_ignores_whitespace_and_quotes_but_not_words(self):
        text = "연결회사는 “MLCC”를 생산하며\n주요 매출처는 삼성전자 입니다."
        self.assertTrue(ft.contains_quote(text, "MLCC를 생산하며 주요 매출처는 삼성전자입니다"))
        self.assertFalse(ft.contains_quote(text, "MLCC를 생산하며 주요 매출처는 애플입니다"))
        self.assertFalse(ft.contains_quote(text, "MLCC"))                    # 8자 미만 인용은 인정하지 않는다

    def test_clip_cuts_at_line(self):
        self.assertEqual(ft.clip("가나다라\n마바사아\n자차카타", 12), "가나다라\n마바사아")
        self.assertEqual(ft.clip("짧음", 100), "짧음")

    def test_fetch_sections_caches_only_extracted_sections(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.object(dart_core, "CACHE_DIR", d), \
                mock.patch.object(dart_core, "download_document", return_value=RAW) as dl:
            a = ft.fetch_sections("20260101000001")
            b = ft.fetch_sections("20260101000001")
            self.assertEqual(a, b)
            self.assertEqual(dl.call_count, 1)                               # 두 번째는 캐시
            self.assertEqual(dl.call_args.kwargs.get("cache"), False)        # 원문(수 MB)은 디스크에 남기지 않는다
            self.assertIn("kam", a)


# --- 사실·발췌 ------------------------------------------------------------------------------------
def make_rep(**over):
    rows = [
        {"year": 2023, "revenue": 8.0e12, "op": 4.0e11, "op_margin": 5.0, "ni": 3.0e11, "assets": 1e13, "liab": 4e12, "equity": 6e12, "debt_ratio": 66.7,
         "roe": 5.0, "ocf": 9e11, "capex": 8e11, "fcf": 1e11},
        {"year": 2024, "revenue": 1.01e13, "op": 7.35e11, "op_margin": 7.3, "ni": 6.8e11, "assets": 1.2e13, "liab": 4.2e12, "equity": 7.8e12, "debt_ratio": 53.8,
         "roe": 8.0, "ocf": 1.2e12, "capex": 1.0e12, "fcf": 2.0e11},
        {"year": 2025, "revenue": 1.131e13, "op": 9.133e11, "op_margin": 8.1, "ni": 7.06e11, "assets": 1.4e13, "liab": 4.8e12, "equity": 9.8e12, "debt_ratio": 49.0,
         "roe": 7.5, "ocf": None, "capex": None, "fcf": None},
    ]
    rep = {"code": "009150", "name": "테스트전자", "annual_year": 2025, "currency": "KRW", "rcept_no": "20260310003071",
           "annual": {"rows": rows, "ni_basis": "지배주주"}, "header": {"ttm": {"is_ttm": False}, "price": 100000, "per": 12.3, "market_cap_all": 9e14},
           "mix": {"views": [{"title": "사업부문", "source": "XBRL", "members": [
               {"label": "컴포넌트", "share": 45.92}, {"label": "패키지", "share": 20.34}, {"label": "광학", "share": 33.71},
               {"label": "그 외", "share": 1.0, "derived": True}]}]},
           "holders": {"major": {"name": "삼성전자", "ratio": 23.7}, "group_ratio": 23.7, "period": "2025 사업보고서"},
           "treasury": {"ratio": 0.0, "acquired": 0, "cancelled": 0, "period": "2025 사업보고서"},
           "filings": [{"date": dt.date(2026, 10, 1), "title": "감자결정", "risk": "경고"}, {"date": dt.date(2026, 10, 2), "title": "배당", "risk": None}]}
    rep.update(over)
    return rep


class FactsTest(unittest.TestCase):
    def test_facts_have_numbers_but_no_price_dependent_values(self):
        facts = rn.report_facts(make_rep())
        text = "\n".join(f["text"] for f in facts)
        self.assertEqual([f["id"] for f in facts][:2], ["F1", "F2"])
        self.assertIn("2025 사업연도: 매출 11.3조", text)
        self.assertIn("영업이익 9,133억(영업이익률 8.1%)", text)
        for banned in ("PER", "시가총액", "PBR", "100,000"):
            self.assertNotIn(banned, text)

    def test_yoy_lines(self):
        text = "\n".join(f["text"] for f in rn.report_facts(make_rep()))
        self.assertIn("2025 사업연도 전년(2024) 대비: 매출 +12.0% · 영업이익 +24.3% · 순이익 +3.8%", text)
        self.assertNotIn("전년(2023)", text)                  # 지난해 증가율까지 주면 모델이 올해 것처럼 쓴다(실측) → 최근 해 하나만

    def test_yoy_words_when_base_not_positive(self):
        self.assertEqual(rn._chg(5, -3), "흑자 전환")
        self.assertEqual(rn._chg(-5, 3), "적자 전환")
        self.assertEqual(rn._chg(-5, -3), "적자 지속")
        self.assertIsNone(rn._chg(5, 0))
        self.assertIsNone(rn._chg(None, 3))

    def test_mix_holders_treasury_and_risk_filings(self):
        text = "\n".join(f["text"] for f in rn.report_facts(make_rep()))
        self.assertIn("컴포넌트 45.9%, 광학 33.7%, 패키지 20.3%", text)          # 큰 순, 파생 '그 외' 제외
        self.assertNotIn("그 외", text)
        self.assertIn("최대주주 삼성전자 지분율 23.70%", text)
        self.assertIn("자기주식 보유 0.00%", text)
        self.assertIn("최근 90일 위험 공시(경고): 2026-10-01 감자결정", text)
        self.assertNotIn("배당", text)                                          # 위험 표시가 없는 공시는 넣지 않는다

    def test_foreign_currency_label(self):
        rep = make_rep(currency="USD")
        self.assertIn("USD", rn.report_facts(rep)[0]["text"])

    def test_facts_hash_ignores_price_but_not_numbers(self):
        a, b = make_rep(), make_rep()
        b["header"] = dict(b["header"], price=1)
        self.assertEqual(rn.facts_hash(a), rn.facts_hash(b))
        c = make_rep()
        c["annual"]["rows"][2]["revenue"] = 9e12
        self.assertNotEqual(rn.facts_hash(a), rn.facts_hash(c))


class ExcerptTest(unittest.TestCase):
    def sections(self):
        return ft.extract_all(RAW)

    def test_ids_in_order_and_sections_without_content_skipped(self):
        ex = rn.build_excerpts(self.sections())
        self.assertEqual([q["id"] for q in ex], [f"Q{i}" for i in range(1, len(ex) + 1)])
        keys = [q["key"] for q in ex]
        self.assertEqual(keys, sorted(keys, key=[k for k, _ in rn.EXCERPT_PLAN].index))          # 계획 순서
        self.assertNotIn("contingent", keys)                                   # '해당사항 없음'뿐 → 내용 없음
        self.assertNotIn("subsequent", keys)
        self.assertIn("sanctions", keys)
        self.assertIn("kam", keys)

    def test_none_statements_and_orphan_headings_removed(self):
        lines = rn._clean_lines("가. 중요한 소송사건\n해당사항 없음.\n나. 담보\n당사는 부동산 10,000백만원을 담보로 제공했습니다.")
        self.assertEqual(lines, ["나. 담보", "당사는 부동산 10,000백만원을 담보로 제공했습니다."])

    def test_none_line_with_large_number_is_kept(self):
        self.assertFalse(rn._is_none_line("해당사항 없음. 다만 미사용 한도 12,345백만원이 있습니다."))
        self.assertTrue(rn._is_none_line("(2) 공정거래위원회의 제재현황해당사항 없음."))

    def test_kam_procedure_lists_removed_keeping_the_reason(self):
        text = ("수익인식 기간귀속\n핵심감사사항으로 결정한 이유 수출매출의 기간귀속 오류 가능성이 있어 1,000억원 규모를 유의적으로 보았습니다.\n"
                "핵심감사사항이 감사에서 다루어진 방법핵심감사사항에 대하여 우리가 수행한 주요 감사절차는 다음과 같습니다.\n- 내부통제 이해 및 평가\n- 샘플 추출 확인")
        out = " ".join(q["text"] for q in rn.build_excerpts({"kam": {"label": "KAM", "text": text}}))
        self.assertIn("기간귀속 오류 가능성", out)
        self.assertNotIn("샘플 추출", out)
        self.assertNotIn("다루어진 방법", out)

    def test_subsequent_events_cut_before_promotional_items_and_pointer_lines_dropped(self):
        text = ("가. 작성기준일 이후 발생한 주요사항\n2026년 1월 공장 화재로 생산이 중단되어 300억원의 손실이 발생했습니다.\n"
                "나. 합병 등의 사후 정보\n해당사항 없음\n다. 녹색경영\n가족친화경영으로 2024년 재인증을 받았으며 경쟁력 향상에 기여했습니다.")
        out = " ".join(q["text"] for q in rn.build_excerpts({"subsequent": {"label": "이후", "text": text}}))
        self.assertIn("공장 화재", out)
        self.assertNotIn("가족친화", out)
        pointer = "※ 자산손상의 상세내용은 Ⅲ. 재무에 관한 사항 중 3. 연결재무제표 주석의 10. 유형자산을 참조하시기 바랍니다."
        self.assertEqual(rn.build_excerpts({"mda": {"label": "경영진단", "text": pointer}}), [])
        self.assertTrue(rn._is_pointer(pointer))
        self.assertFalse(rn._is_pointer("자세한 내용은 주석을 참고하시기 바랍니다. " + "매출이 감소했습니다. " * 12))     # 긴 조각은 내용이 있다

    def test_risk_native_sections_are_not_filtered_by_cue_words(self):
        # 실제 핵심감사사항 문장엔 '위험·감소' 같은 단서어가 거의 없다 — 점수 필터가 이를 지우면 안 된다(실측 회귀)
        kam = "수출매출의 기간 귀속\n회사는 계약에서 재화의 통제가 이전되었을 때 수익을 인식하며 경영진의 판단이 요구됩니다. 이를 핵심감사사항으로 선정하였습니다."
        self.assertEqual(len(rn.build_excerpts({"kam": {"label": "KAM", "text": kam}})), 1)
        self.assertEqual(rn.build_excerpts({"mda": {"label": "경영진단", "text": kam}}), [])      # 같은 문장도 일반 섹션에선 거른다

    def test_low_signal_chunks_are_not_added_even_with_spare_budget(self):
        sections = {"subsequent": {"label": "작성기준일 이후", "text": "당사는 가족친화제도를 운영하며 여성가족부로부터 인증을 받았습니다."}}
        self.assertEqual(rn.build_excerpts(sections), [])
        self.assertEqual(len(rn.build_excerpts({"sanctions": {"label": "제재", "text": "2025.10.21 | 고용노동부 | 과태료 | 산업안전보건법 위반"}})), 1)

    def test_budget_and_risk_cues_win(self):
        filler = "일반적인 회사 소개 문장입니다. " * 20
        risky = "소송에서 패소하여 손실 500억원이 발생했고 제재를 받았습니다."
        sections = {"overview": {"label": "사업의 개요", "text": "\n".join([filler] * 8 + [risky] + [filler] * 8)}}
        ex = rn.build_excerpts(sections, plan=(("overview", 800),), total=800)
        self.assertLessEqual(sum(len(q["text"]) for q in ex), 800)
        self.assertTrue(any("패소" in q["text"] for q in ex))

    def test_leftover_budget_goes_to_overview_and_mda(self):
        long_text = "\n".join(f"{i}번째 문단입니다. 매출이 증가했습니다. " * 6 for i in range(30))
        sections = {"overview": {"label": "개요", "text": long_text}}
        small = sum(len(q["text"]) for q in rn.build_excerpts(sections, plan=(("overview", 600),), total=600))
        big = sum(len(q["text"]) for q in rn.build_excerpts(sections, plan=(("overview", 600),), total=3000))
        self.assertGreater(big, small)

    def test_total_excerpt_budget_is_never_exceeded(self):
        # 실측 회귀: 남는 예산을 '계획 예산 + 몫'으로 나눠 주면 첫 패스에서 덜 쓴 만큼이 이중 계상돼 5,500자 예산에서 5,500자를 넘었다(실제 금융지주 보고서 5,686자;
        # Groq 분당 토큰 한도 계산이 어긋남). 아래 조합은 수정 전 코드에서 5,544자가 나온 것이다(수정 후 5,174자).
        def line(n):
            return (("매출이 증가했고 소송 위험이 있습니다. " * 20)[:n - 5] + "123억원")[:n]

        def block(n, count):
            return {"label": "x", "text": "\n".join(line(n) for _ in range(count))}
        for over, mda, other in ((250, 250, 150), (250, 300, 150), (300, 300, 150), (250, 370, 370), (300, 300, 370), (370, 370, 370)):
            sections = {"overview": block(over, 40), "mda": block(mda, 40), "sanctions": block(other, 1), "contingent": block(other, 1), "kam": block(other, 1)}
            total = sum(len(q["text"]) for q in rn.build_excerpts(sections))
            self.assertLessEqual(total, rn.EXCERPT_TOTAL, (over, mda, other, total))

    def test_chunks_fit_size_and_preserve_text(self):
        text = "첫 문장입니다.둘째 문장입니다.셋째 문장입니다.\n표 | 행 | 1,234\n" + "가" * 900
        ch = rn._chunks(text, 100)
        self.assertTrue(all(len(c) <= 100 for c in ch))
        joined = ft.norm_match("".join(ch))
        for piece in ("첫문장입니다", "둘째문장입니다", "표|행|1,234"):
            self.assertIn(piece, joined)


# --- 검증기 ---------------------------------------------------------------------------------------
class ValidateTest(unittest.TestCase):
    def setUp(self):
        self.rep = make_rep()
        self.facts = rn.report_facts(self.rep)
        self.ex = rn.build_excerpts(ft.extract_all(RAW))
        self.q = {q["key"]: q for q in self.ex}
        self.fid = {f["text"].split(":")[0]: f["id"] for f in self.facts}

    def ids(self, key):
        return [q["id"] for q in self.ex if q["key"] == key]

    def run_validate(self, parsed):
        return rn.validate(parsed, self.facts, self.ex, 2025)

    def reasons(self, out):
        return [d["reason"] for d in out["dropped"]]

    def test_grounded_sentence_passes(self):
        f_latest = self.facts[2]["id"]
        out = self.run_validate({"business": [{"text": "2025 사업연도 영업이익은 9,133억원으로 영업이익률은 8.1%입니다.", "refs": [f_latest]}]})
        self.assertEqual(len(out["business"]), 1)
        self.assertEqual(out["dropped"], [])

    def test_invented_number_is_dropped(self):
        out = self.run_validate({"changes": [{"text": "영업이익이 1조 2,000억원으로 늘었습니다.", "refs": [self.facts[2]["id"]]}]})
        self.assertEqual(out["changes"], [])
        self.assertIn("근거에 없는 숫자", self.reasons(out)[0])
        self.assertIn("1조", self.reasons(out)[0])
        out = self.run_validate({"changes": [{"text": "영업이익이 3조 5,500억원으로 늘었고 증가율은 77.7%입니다.", "refs": [self.facts[2]["id"]]}]})
        self.assertEqual(out["changes"], [])
        for tok in ("3조", "5500억", "77.7%"):
            self.assertIn(tok, self.reasons(out)[0])

    def test_amount_with_unit_taken_from_table_header_is_accepted(self):
        # 실측: 표엔 '2,500,000'만 있고 단위는 '(단위: 천원)' 머리말에 있어, 모델이 단위를 붙이면 거부되던 정당한 소송 리스크
        ex = [{"id": "Q1", "key": "contingent", "label": "우발부채", "text": "(단위: 천원)\n소송 | 한화세미텍 특허 침해 | 소송가액 | 2,500,000 | 계류 중"}]
        out = rn.validate({"risks": [{"title": "특허 소송", "text": "특허 침해 소송의 소송가액이 2,500,000천원입니다.", "refs": ["Q1"], "quote_id": "Q1",
                                      "quote": "한화세미텍 특허 침해 | 소송가액 | 2,500,000"}]}, self.facts, ex, 2025)
        self.assertEqual(len(out["risks"]), 1, out["dropped"])
        converted = rn.validate({"risks": [{"title": "특허 소송", "text": "특허 침해 소송의 소송가액이 25억원입니다.", "refs": ["Q1"], "quote_id": "Q1",
                                            "quote": "한화세미텍 특허 침해 | 소송가액 | 2,500,000"}]}, self.facts, ex, 2025)
        self.assertEqual(converted["risks"], [])                                  # 단위 환산(25억)은 계속 거부
        self.assertIn("25억", converted["dropped"][0]["reason"])

    def test_stitched_numbers_from_different_lines_are_dropped(self):
        # '1'과 '2,000'은 사실 목록 어딘가에 따로 있지만 '1조'·'2,000억'을 이어 붙인 문장은 어느 줄에도 없다
        text = "\n".join(f["text"] for f in self.facts)
        self.assertIn("2,000억", text)
        out = self.run_validate({"changes": [{"text": "영업이익이 1조 2,000억원으로 늘었습니다.", "refs": [f["id"] for f in self.facts]}]})
        self.assertEqual(out["changes"], [])
        self.assertIn("1조", self.reasons(out)[0])

    def test_unit_must_match_but_unitless_number_is_lenient(self):
        f = self.facts[2]["id"]                                              # 영업이익률 8.1%
        self.assertEqual(self.run_validate({"changes": [{"text": "영업이익률은 8.1억원입니다.", "refs": [f]}]})["changes"], [])
        self.assertEqual(len(self.run_validate({"changes": [{"text": "영업이익률은 8.1 수준입니다.", "refs": [f]}]})["changes"]), 1)

    def test_year_mismatch_catches_other_years_values(self):
        # 실측 사례: 숫자는 근거에 있지만 지난해 값을 올해 것처럼 쓴 문장
        f = lambda year: next(x["id"] for x in self.facts if x["text"].startswith(f"{year} 사업연도:"))
        out = self.run_validate({"changes": [{"text": "2025년 영업이익률은 5.0%였습니다.", "refs": [f(2023)]}]})
        self.assertEqual(out["changes"], [])
        self.assertIn("연도 불일치 의심", self.reasons(out)[0])
        self.assertIn("2023", self.reasons(out)[0])
        for ok in ("2025년 영업이익률은 8.1%로 2023년 5.0%보다 높습니다.",       # 두 해를 모두 밝히면 각 줄이 허용된다
                   "2025년 영업이익률은 8.1%이고 전년은 7.3%였습니다.",            # '전년'이면 직전 연도 줄 허용
                   "영업이익률은 5.0%였습니다.",                                    # 연도를 안 밝힌 문장은 판단하지 않는다
                   "2025년 컴포넌트 매출 비중은 45.9%입니다."):                      # 연도 줄이 아닌 근거(구성)에서 확인되는 숫자
            out = self.run_validate({"changes": [{"text": ok, "refs": [f(2025)]}]})
            self.assertEqual(len(out["changes"]), 1, (ok, out["dropped"]))

    def test_coincidental_same_number_elsewhere_does_not_excuse_a_year_or_sign_error(self):
        # 실측(현대건설): F의 '2025년 매출 -4.9%'를 '2024년 ... 2023 대비 4.9% 증가'로 옮겼는데, 같은 4.9%가 다른 근거에도 있어 면제돼 통과했다
        facts = [{"id": "F1", "text": "2024 사업연도: 매출 32.7조 · 영업이익률 -3.9%"},
                 {"id": "F2", "text": "2025 사업연도 전년(2024) 대비: 매출 -4.9% · 영업이익 흑자 전환"}]
        ex = [{"id": "Q1", "key": "overview", "label": "개요", "text": "주택 부문 비중은 4.9%입니다."}]
        check = lambda text, refs: rn.validate({"changes": [{"text": text, "refs": refs}]}, facts, ex, 2025)
        out = check("2024 사업연도 매출은 32.7조원으로 2023 대비 4.9% 증가했습니다.", ["F1", "F2"])
        self.assertEqual(out["changes"], [])
        self.assertIn("연도 불일치 의심", out["dropped"][0]["reason"])
        # 연도는 맞지만 부호가 반대(-4.9%를 '증가'라고 씀)
        out = check("2025년 매출은 전년 대비 4.9% 증가했습니다.", ["F2"])
        self.assertEqual(out["changes"], [])
        self.assertIn("증감 방향 불일치", out["dropped"][0]["reason"])
        self.assertEqual(len(check("2025년 매출은 전년 대비 4.9% 감소했습니다.", ["F2"])["changes"]), 1)

    def test_direction_check_only_applies_to_signed_fact_lines(self):
        facts = [{"id": "F1", "text": "2025 사업연도 전년(2024) 대비: 매출 +9.9% · FCF -59.6%"}]
        ex = [{"id": "Q1", "key": "overview", "label": "개요", "text": "판매가격은 전년 대비 14.0% 상승하였습니다. 영업이익은 14% 감소했습니다."}]
        check = lambda text, refs: rn.validate({"changes": [{"text": text, "refs": refs}]}, facts, ex, 2025)
        self.assertEqual(check("2025년 매출은 9.9% 감소했습니다.", ["F1"])["changes"], [])                 # +9.9%를 감소로
        self.assertEqual(len(check("2025년 FCF는 59.6% 감소했습니다.", ["F1"])["changes"]), 1)
        self.assertEqual(len(check("2025년 매출은 9.9% 증가했고 FCF는 59.6% 감소했습니다.", ["F1"])["changes"]), 1)
        self.assertEqual(len(check("판매가격이 전년 대비 14.0% 상승하였습니다.", ["Q1"])["changes"]), 1)      # 원문(Q)엔 부호가 없어 판단하지 않는다

    def test_yoy_line_is_labelled_by_its_year(self):
        yoy = next(x for x in self.facts if "전년(2024) 대비" in x["text"])
        self.assertTrue(rn.YEAR_LINE.match(yoy["text"]))
        out = self.run_validate({"changes": [{"text": "2024년 매출은 전년 대비 +12.0% 늘었습니다.", "refs": [yoy["id"]]}]})   # +12.0%는 2025년 줄 값
        self.assertEqual(out["changes"], [])
        out = self.run_validate({"changes": [{"text": "2025년 매출은 전년 대비 +12.0% 늘었습니다.", "refs": [yoy["id"]]}]})
        self.assertEqual(len(out["changes"]), 1)

    def test_empty_placeholders_between_items_are_skipped_silently(self):
        rid = self.ids("overview")[0]
        out = self.run_validate({"business": ["", {"text": "회사는 MLCC 사업을 영위합니다.", "refs": [rid]}, None]})
        self.assertEqual(len(out["business"]), 1)
        self.assertEqual(out["dropped"], [])

    def test_number_in_other_source_fixes_refs(self):
        wrong = self.facts[0]["id"]                                           # 2023년 줄을 인용했지만 숫자는 2025년 줄에 있다
        out = self.run_validate({"changes": [{"text": "영업이익률은 8.1%입니다.", "refs": [wrong]}]})
        self.assertEqual(len(out["changes"]), 1)
        self.assertIn(self.facts[2]["id"], out["changes"][0]["refs"])

    def test_number_format_variants_match(self):
        out = self.run_validate({"changes": [{"text": "영업이익률은 8.10%이고 영업이익은 9133억원입니다.", "refs": [self.facts[2]["id"]]}]})
        self.assertEqual(len(out["changes"]), 1)

    def test_dates_match_in_either_format(self):
        out = self.run_validate({"risks": [{"title": "제재", "text": "2025년 10월 21일 고용노동부 과태료를 받았습니다.", "refs": self.ids("sanctions"),
                                            "quote_id": self.ids("sanctions")[0], "quote": "산업안전보건법 위반 과태료 3.2백만원"}]})
        self.assertEqual(len(out["risks"]), 1, out["dropped"])

    def test_banned_words_dropped(self):
        for word in ("목표주가를 올릴 만합니다", "매수 의견이 적절합니다", "향후 실적 전망이 밝습니다", "저평가 구간입니다", "상승 여력이 큽니다", "BUY 의견입니다"):
            out = self.run_validate({"business": [{"text": f"이 회사는 {word}.", "refs": [self.facts[0]["id"]]}]})
            self.assertEqual(out["business"], [], word)
            self.assertIn("금지 표현", self.reasons(out)[0])

    def test_quote_unrelated_to_the_claim_is_dropped_but_paraphrased_table_rows_pass(self):
        fx = "연결기업은 정상적인 거래로부터 기인하는 환율변동위험에 노출되어 있습니다. 동 위험의 대부분은 기능통화와는 다른 통화로 거래되는 구매대금 등으로부터 발생합니다."
        row = "소송가액 | 제 1소송: CAD 110,000,000 (원고 청구액 기준) 제 2소송: CAD 610,000,000 (원고 청구액 기준)"
        ex = [{"id": "Q1", "key": "contingent", "label": "우발부채", "text": fx}, {"id": "Q2", "key": "contingent", "label": "우발부채", "text": row}]
        risk = lambda qid, quote, text: {"risks": [{"title": "t", "text": text, "refs": [qid], "quote_id": qid, "quote": quote}]}
        # 실측(셀트리온 20b): 주장은 '고객·매출처 집중', 인용은 환율 위험 문장
        out = rn.validate(risk("Q1", fx[:60], "외국 매출 비중이 높고 북미가 큰 비중을 차지하는 고객 쏠림이 있습니다."), self.facts, ex, 2025)
        self.assertEqual(out["risks"], [])
        self.assertIn("무관", out["dropped"][0]["reason"])
        out = rn.validate(risk("Q2", row, "캐나다 여객 민사집단소송이 진행 중이며 소송가액이 수십억 달러에 달합니다."), self.facts, ex, 2025)
        self.assertEqual(len(out["risks"]), 1, out["dropped"])                     # 표 행을 풀어 쓴 문장은 겹침이 적어도 통과
        self.assertGreater(rn.quote_overlap("환율변동위험에 노출됩니다", fx), 0.5)
        self.assertEqual(rn.quote_overlap("", fx), 1.0)

    def test_no_impact_boilerplate_is_not_a_risk(self):
        ex = [{"id": "Q1", "key": "contingent", "label": "우발부채", "text": "당사는 소송 등을 진행 중에 있으나, 경영진은 이러한 소송 등의 결과가 재무상태에 중요한 영향을 미치지 않을 것으로 판단하고 있습니다."},
              {"id": "Q2", "key": "contingent", "label": "우발부채", "text": "7건의 특허 사건이 계류중에 있고, 피고로 계류된 사건은 없습니다."}]
        risk = lambda qid, quote, text: {"risks": [{"title": "소송", "text": text, "refs": [qid], "quote_id": qid, "quote": quote}]}
        out = rn.validate(risk("Q1", "소송 등의 결과가 재무상태에 중요한 영향을 미치지 않을 것으로 판단하고 있습니다", "회사는 소송을 진행 중입니다."), self.facts, ex, 2025)
        self.assertEqual(out["risks"], [])
        self.assertIn("영향 없음", out["dropped"][0]["reason"])
        out = rn.validate(risk("Q2", "7건의 특허 사건이 계류중에 있고", "회사에는 특허 사건 7건이 계류 중입니다."), self.facts, ex, 2025)
        self.assertEqual(len(out["risks"]), 1, out["dropped"])                     # '없습니다'가 들어가도 영향 없음 문구가 아니면 통과

    def test_yoy_line_includes_cash_flow_direction(self):
        rep = make_rep()
        rows = rep["annual"]["rows"]
        rows[1].update({"ocf": 1.0e12, "fcf": 6.0e11})
        rows[2].update({"ocf": 1.2e12, "fcf": 2.4e11})
        line = next(f["text"] for f in rn.report_facts(rep) if "전년(2024) 대비" in f["text"])
        self.assertIn("영업현금흐름 +20.0%", line)
        self.assertIn("FCF -60.0%", line)                                           # 줄어든 FCF를 모델이 '회복'이라 쓰지 않도록 방향을 명시

    def test_uncertainty_statements_are_not_forecasts(self):
        sid = self.ids("sanctions")[0]
        quote = "산업안전보건법 위반 과태료 3.2백만원"
        ok = {"title": "제재", "text": "산업안전보건법 위반 과태료가 재무제표에 미치는 영향은 예측할 수 없습니다.", "refs": [sid], "quote_id": sid, "quote": quote}
        self.assertEqual(len(self.run_validate({"risks": [ok]})["risks"]), 1)
        for text in ("향후 영업이익은 크게 증가할 것으로 예상됩니다.", "실적 개선이 전망됩니다.", "다음 분기 실적을 예측해 보면 증가합니다."):
            out = self.run_validate({"risks": [dict(ok, text=text)]})
            self.assertEqual(out["risks"], [], text)
            self.assertIn("금지 표현", self.reasons(out)[0])

    def test_unknown_ref_ids_are_ignored_and_empty_refs_dropped(self):
        out = self.run_validate({"business": [{"text": "당사는 MLCC를 만드는 회사입니다.", "refs": ["F99", "Q99"]}]})
        self.assertEqual(out["business"], [])
        self.assertIn("근거(refs) 없음", self.reasons(out)[0])
        q1 = self.ids("overview")[0]
        out = self.run_validate({"business": [{"text": "당사는 MLCC를 만드는 회사입니다.", "refs": ["F99", q1]}]})
        self.assertEqual(out["business"][0]["refs"], [q1])

    def test_quote_must_be_verbatim_substring(self):
        sid = self.ids("sanctions")[0]
        good = {"title": "제재", "text": "고용노동부로부터 산업안전보건법 위반 과태료를 받았습니다.", "refs": [sid], "quote_id": sid, "quote": "산업안전보건법 위반 과태료 3.2백만원"}
        self.assertEqual(len(self.run_validate({"risks": [good]})["risks"]), 1)
        fake = dict(good, quote="산업안전보건법 위반으로 벌금 3억원을 선고받았다")
        out = self.run_validate({"risks": [fake]})
        self.assertEqual(out["risks"], [])
        self.assertIn("인용문이 원문 발췌에 없음", self.reasons(out)[0])

    def test_wrong_quote_id_is_corrected_when_quote_exists_elsewhere(self):
        sid = self.ids("sanctions")[0]
        out = self.run_validate({"risks": [{"title": "제재", "text": "산업안전보건법 위반 과태료를 받았습니다.", "refs": [self.ids("overview")[0]],
                                            "quote_id": self.ids("overview")[0], "quote": "산업안전보건법 위반 과태료 3.2백만원"}]})
        self.assertEqual(out["risks"][0]["quote_id"], sid)
        self.assertIn(sid, out["risks"][0]["refs"])

    def test_risks_always_need_a_verified_quote(self):
        # 실측: 숫자(F)만 인용한 '부채 증가' 리스크는 부채비율이 줄었는데도 통과했다 → 리스크는 원문 인용 필수
        for refs in ([self.ids("overview")[0]], [self.facts[0]["id"]]):
            out = self.run_validate({"risks": [{"title": "의존", "text": "특정 고객 의존도가 높습니다.", "refs": refs}]})
            self.assertEqual(out["risks"], [])
            self.assertIn("원문 인용이 없음", self.reasons(out)[0])
        out = self.run_validate({"business": [{"text": "2025 사업연도 영업이익률은 8.1%입니다.", "refs": [self.facts[2]["id"]]}]})
        self.assertEqual(len(out["business"]), 1)                              # 사업 요약·변화·질문은 인용 없이 근거 ID만으로 충분

    def test_mix_lines_carry_the_year_so_the_model_cannot_invent_one(self):
        mix = next(f for f in self.facts if "매출 구성" in f["text"])
        self.assertTrue(mix["text"].startswith("2025 사업연도 매출 구성"))
        out = self.run_validate({"business": [{"text": "2023년 매출 구성은 컴포넌트 45.9%입니다.", "refs": [mix["id"]]}]})
        self.assertEqual(out["business"], [])
        self.assertIn("연도 불일치 의심", self.reasons(out)[0])
        out = self.run_validate({"business": [{"text": "2025년 매출 구성은 컴포넌트 45.9%입니다.", "refs": [mix["id"]]}]})
        self.assertEqual(len(out["business"]), 1)

    def test_quote_numbers_count_as_evidence(self):
        sid = self.ids("sanctions")[0]
        out = self.run_validate({"risks": [{"title": "제재", "text": "과태료 3.2백만원이 부과됐습니다.", "refs": [], "quote_id": sid, "quote": "산업안전보건법 위반 과태료 3.2백만원"}]})
        self.assertEqual(len(out["risks"]), 1, out["dropped"])

    def test_years_are_allowed_without_evidence(self):
        out = self.run_validate({"business": [{"text": "2024년과 비교하면 변화가 있습니다.", "refs": [self.facts[2]["id"]]}]})
        self.assertEqual(len(out["business"]), 1)
        out = self.run_validate({"business": [{"text": "1987년에 설립됐습니다.", "refs": [self.facts[2]["id"]]}]})
        self.assertEqual(out["business"], [])

    def test_limits_and_bad_shapes(self):
        item = lambda i: {"text": f"당사는 MLCC 사업을 영위합니다 {'가' * i}.", "refs": [self.ids("overview")[0]]}
        out = self.run_validate({"business": [item(i) for i in range(5)], "changes": "문자열", "questions": [123, {"refs": ["F1"]}]})
        self.assertEqual(len(out["business"]), 3)
        self.assertEqual(self.reasons(out).count("개수 초과"), 2)
        self.assertEqual(out["changes"], [])
        self.assertEqual(sum(1 for r in self.reasons(out) if r == "형식 오류"), 2)

    def test_ref_markers_removed_from_text(self):
        out = self.run_validate({"business": [{"text": "당사는 MLCC를 만듭니다(F1, Q1). [Q2]", "refs": [self.ids("overview")[0]]}]})
        self.assertNotRegex(out["business"][0]["text"], r"[FQ]\d")

    def test_short_sentence_dropped(self):
        out = self.run_validate({"business": [{"text": "좋음", "refs": ["F1"]}]})
        self.assertEqual(self.reasons(out), ["문장이 너무 짧음"])


class ParseAndGenerateTest(unittest.TestCase):
    def setUp(self):
        self.rep = make_rep()
        self.sections = ft.extract_all(RAW)
        self.q = {q["key"]: q["id"] for q in rn.build_excerpts(self.sections)}

    def good_output(self):
        return json.dumps({
            "business": [{"text": "당사는 MLCC를 만듭니다.", "refs": [self.q["overview"]]}],
            "changes": [{"text": "2025 사업연도 영업이익은 9,133억원입니다.", "refs": ["F3"]}],
            "risks": [{"title": "제재", "text": "산업안전보건법 위반으로 과태료를 받았습니다.", "refs": [self.q["sanctions"]], "quote_id": self.q["sanctions"],
                       "quote": "고용노동부 | 산업안전보건법 위반 과태료 3.2백만원"},
                      {"title": "가짜", "text": "소송 패소로 5,000억원 손실이 났습니다.", "refs": [self.q["sanctions"]], "quote_id": self.q["sanctions"], "quote": "소송 패소로 5,000억원"}],
            "questions": [{"text": "삼성전자 매출 비중이 계속 유지되는지 확인해 보세요.", "refs": [self.q["overview"]]}],
        }, ensure_ascii=False)

    def test_json_in_code_fence_and_chatter(self):
        self.assertEqual(rn._parse_json("```json\n{\"a\": 1}\n```"), {"a": 1})
        self.assertEqual(rn._parse_json("결과입니다: {\"a\": 1} 끝"), {"a": 1})

    def test_bad_responses_raise_readable_errors(self):
        for bad in ("", "  ", "JSON 없음", "{\"a\": ", "[1, 2]"):
            with self.assertRaises(rn.NarrativeError):
                rn._parse_json(bad)

    def test_generate_with_fake_llm_keeps_good_and_drops_fabricated(self):
        seen = {}

        def llm(messages):
            seen["messages"] = messages
            return self.good_output(), "fake-model"
        res = rn.generate(self.rep, self.sections, llm)
        self.assertEqual(res["model"], "fake-model")
        self.assertEqual((res["kept"], res["asked"]), (4, 5))
        self.assertEqual(len(res["risks"]), 1)
        self.assertIn("인용문이 원문 발췌에 없음", res["dropped"][0]["reason"])
        user = seen["messages"][1]["content"]
        self.assertIn("F1 2023 사업연도", user)
        self.assertIn("Q1 (사업의 개요)", user)
        self.assertNotIn("시가총액", user)
        self.assertLess(res["prompt_chars"], 9000)                           # Groq 분당 8,000토큰 안에 들어오는 크기

    def test_broken_items_counts_fragments_not_empty_placeholders(self):
        self.assertEqual(rn.broken_items({"risks": ["title", ":", "노동소송", {"text": "정상"}, "", None, {}]}), 3)
        self.assertEqual(rn.broken_items({"business": [{"text": "a"}], "risks": "문자열이면 세지 않음", "questions": []}), 0)

    def test_structurally_broken_output_is_regenerated_once(self):
        # 실측(롯데케미칼): 리스크 항목이 '"title"', '":"', '"노동소송"' 같은 조각 문자열로 흩어진 출력 — JSON 문법은 맞지만 항목이 깨졌다
        broken = json.dumps({"business": [{"text": "당사는 MLCC를 만듭니다.", "refs": [self.q["overview"]]}],
                             "risks": ["title", ":", "노동소송", "text", ":", "소송가액은 2", "000"]}, ensure_ascii=False)
        calls = []

        def llm(messages):
            calls.append(1)
            llm.errors = []
            return (broken if len(calls) == 1 else self.good_output()), "fake-model"
        res = rn.generate(self.rep, self.sections, llm)
        self.assertEqual(len(calls), 2)
        self.assertEqual(res["attempts"], 2)
        self.assertEqual(res["kept"], 4)                                           # 두 번째(정상) 출력으로 검증
        self.assertTrue(any("깨져" in e for e in res["llm_errors"]))
        self.assertEqual(res["dropped"][0]["reason"], "인용문이 원문 발췌에 없음")      # 조각 문자열 탈락 기록이 남지 않는다

    def test_regeneration_is_capped_and_clean_output_is_called_once(self):
        broken = json.dumps({"risks": ["title", ":", "노동소송"]}, ensure_ascii=False)
        calls = []

        def llm(messages):
            calls.append(1)
            return broken, "fake-model"
        res = rn.generate(self.rep, self.sections, llm)
        self.assertEqual((len(calls), res["attempts"], res["kept"]), (2, 2, 0))     # 두 번 다 깨지면 거기서 멈추고 마지막 것을 검증(전부 탈락)
        self.assertEqual(sum(1 for d in res["dropped"] if d["reason"] == "형식 오류"), 3)
        calls.clear()
        res = rn.generate(self.rep, self.sections, lambda m: (calls.append(1) or self.good_output(), "fake-model"))
        self.assertEqual((len(calls), res["attempts"]), (1, 1))

    def test_generate_without_excerpts_raises(self):
        with self.assertRaises(rn.NarrativeError):
            rn.generate(self.rep, {}, lambda m: ("{}", "x"))

    def test_cache_roundtrip_only_when_something_kept(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.object(dart_core, "CACHE_DIR", d):
            self.assertIsNone(rn.load_cached(self.rep))
            rn.save_cached(self.rep, {"kept": 0})
            self.assertIsNone(rn.load_cached(self.rep))
            rn.save_cached(self.rep, {"kept": 2, "business": []})
            self.assertEqual(rn.load_cached(self.rep)["kept"], 2)
            changed = make_rep()
            changed["annual"]["rows"][2]["revenue"] = 1.0e13
            self.assertIsNone(rn.load_cached(changed))                       # 숫자가 바뀌면 캐시를 쓰지 않는다

    def test_cache_survives_price_change(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.object(dart_core, "CACHE_DIR", d):
            rn.save_cached(self.rep, {"kept": 1})
            moved = make_rep()
            moved["header"] = dict(moved["header"], price=55555)
            self.assertIsNotNone(rn.load_cached(moved))


class NumTokensTest(unittest.TestCase):
    def test_normalization(self):
        self.assertEqual(rn.num_tokens("1조 8,035억원(14%)"), {("1", "조"), ("8035", "억"), ("14", "%")})
        self.assertEqual(rn.num_tokens("영업이익률 9.90%"), {("9.9", "%")})
        self.assertEqual(rn.num_tokens("3.2백만원 · 10건 · 25.5배"), {("3.2", "백만"), ("10", "건"), ("25.5", "배")})
        self.assertEqual(rn.num_tokens("근거 F12, Q3 참고"), set())          # 근거 ID는 숫자로 세지 않는다

    def test_table_unit_declaration_gives_bare_numbers_a_unit_on_the_source_side_only(self):
        table = "(단위: 천원)\n소송가액 | 2,500,000\n구분 | 10"
        self.assertIn(("2500000", "천"), rn.num_tokens(table, infer_table_units=True))
        self.assertNotIn(("2500000", "천"), rn.num_tokens(table))                # 기본(문장 쪽)은 추론하지 않는다
        self.assertNotIn(("2500000", "백만"), rn.num_tokens(table, infer_table_units=True))
        before = "소송가액 | 3,000\n(단위: 백만원)\n벌금 | 10"                        # 선언 앞의 숫자는 단위를 얻지 않는다
        toks = rn.num_tokens(before, infer_table_units=True)
        self.assertNotIn(("3000", "백만"), toks)
        self.assertIn(("10", "백만"), toks)

    def test_chunk_boundary_carries_previous_table_unit(self):
        rows = "\n".join(f"소송{i} | 원고{i} | 피고{i} | 청구 {i * 1000},000" for i in range(1, 12))
        chunks = rn._chunks("(단위: 천원)\n" + rows, size=200)
        self.assertGreater(len(chunks), 1)
        for c in chunks[1:]:
            self.assertRegex(c, r"단위\s*:\s*천원")

    def test_dates_cover_both_notations(self):
        dotted = rn.num_tokens("2024.03.11 경고")
        words = rn.num_tokens("2024년 3월 11일")
        self.assertTrue({("2024", "년"), ("3", "월"), ("11", "일")} <= dotted)
        self.assertTrue(words <= dotted)                                     # '2024년 3월 11일'로 풀어 써도 근거와 맞는다


# --- Groq 호출기(가짜 SDK) ------------------------------------------------------------------------
class FakeStatusError(Exception):
    def __init__(self, status, retry_after=None):
        super().__init__(f"status {status}")
        self.status_code = status
        self.response = types.SimpleNamespace(headers={"retry-after": str(retry_after)} if retry_after is not None else {})


def fake_groq_module(script):
    """script: 호출마다 (모델 → 결과) 를 돌려주는 함수. 예외 인스턴스면 raise, 문자열이면 정상 응답."""
    calls = []

    class Completions:
        def create(self, **kw):
            calls.append(kw)
            res = script(kw["model"], len(calls))
            if isinstance(res, Exception):
                raise res
            return types.SimpleNamespace(choices=[types.SimpleNamespace(message=types.SimpleNamespace(content=res))])

    class Groq:
        def __init__(self, api_key=None):
            self.chat = types.SimpleNamespace(completions=Completions())
    return types.SimpleNamespace(Groq=Groq), calls


class GroqLlmTest(unittest.TestCase):
    def call(self, script, **kw):
        mod, calls = fake_groq_module(script)
        with mock.patch.dict(sys.modules, {"groq": mod}), mock.patch("time.sleep") as sleep:
            try:
                return rn.groq_llm("key", **kw)([{"role": "user", "content": "x"}]), calls, sleep
            except rn.NarrativeError as e:
                return e, calls, sleep

    def test_success_passes_json_mode_and_limits(self):
        (text, model), calls, _ = self.call(lambda m, n: '{"ok": 1}')
        self.assertEqual((text, model), ('{"ok": 1}', "openai/gpt-oss-120b"))
        self.assertEqual(calls[0]["response_format"], {"type": "json_object"})
        self.assertEqual(calls[0]["max_tokens"], rn.MAX_OUTPUT_TOKENS)

    def test_rate_limit_waits_retry_after_then_succeeds_on_same_model(self):
        res, calls, sleep = self.call(lambda m, n: FakeStatusError(429, 12) if n == 1 else "{}")
        self.assertEqual(res[1], "openai/gpt-oss-120b")
        sleep.assert_called_once_with(12.0)

    def test_wait_is_capped(self):
        _, _, sleep = self.call(lambda m, n: FakeStatusError(429, 600) if n == 1 else "{}", wait_cap=70)
        sleep.assert_called_once_with(70)

    def test_missing_model_falls_through_to_next(self):
        res, calls, _ = self.call(lambda m, n: FakeStatusError(404) if m.endswith("120b") else "{}")
        self.assertEqual(res[1], "openai/gpt-oss-20b")

    def test_failure_reasons_are_recorded_for_the_screen(self):
        mod, _ = fake_groq_module(lambda m, n: FakeStatusError(404) if m.endswith("120b") else "{}")
        with mock.patch.dict(sys.modules, {"groq": mod}), mock.patch("time.sleep"):
            llm = rn.groq_llm("key")
            llm([{"role": "user", "content": "x"}])
        self.assertEqual(len(llm.errors), 1)
        self.assertIn("gpt-oss-120b", llm.errors[0])
        self.assertIn("404", llm.errors[0])

    def test_bad_request_retries_same_model_without_json_mode(self):
        # 실측: gpt-oss-120b가 JSON 모드에서 'json_validate_failed'(400)를 자주 낸다(같은 호출 반복도 실패) → 같은 모델을 JSON 모드 없이 다시, 그래도 400이면 다음 모델
        res, calls, _ = self.call(lambda m, n: FakeStatusError(400) if n == 1 else "{}")
        self.assertEqual(res[1], "openai/gpt-oss-120b")
        self.assertEqual(len(calls), 2)
        self.assertIn("response_format", calls[0])
        self.assertNotIn("response_format", calls[1])
        res, calls, _ = self.call(lambda m, n: FakeStatusError(400) if m.endswith("120b") else "{}")
        self.assertEqual(res[1], "openai/gpt-oss-20b")
        self.assertEqual([c["model"] for c in calls], ["openai/gpt-oss-120b", "openai/gpt-oss-120b", "openai/gpt-oss-20b"])
        self.assertIn("response_format", calls[2])                              # 모델이 바뀌면 JSON 모드로 다시 시작

    def test_error_code_from_body_is_recorded(self):
        class WithBody(FakeStatusError):
            body = {"error": {"code": "json_validate_failed", "message": "x"}}
        mod, _ = fake_groq_module(lambda m, n: WithBody(400) if n <= 2 else "{}")
        with mock.patch.dict(sys.modules, {"groq": mod}), mock.patch("time.sleep"):
            llm = rn.groq_llm("key")
            llm([{"role": "user", "content": "x"}])
        self.assertIn("400:json_validate_failed", llm.errors[0])

    def test_auth_errors_stop_immediately_without_leaking_anything(self):
        for status in (401, 403):
            res, calls, _ = self.call(lambda m, n, s=status: FakeStatusError(s))
            self.assertIsInstance(res, rn.NarrativeError)
            self.assertEqual(len(calls), 1)
            self.assertNotIn("key", str(res).replace("GROQ_API_KEY", ""))

    def test_request_too_large_is_explained(self):
        res, calls, _ = self.call(lambda m, n: FakeStatusError(413))
        self.assertIn("8,000", str(res))
        self.assertEqual(len(calls), 1)

    def test_all_models_failing_raises(self):
        res, calls, _ = self.call(lambda m, n: FakeStatusError(500))
        self.assertIsInstance(res, rn.NarrativeError)
        self.assertEqual(len(calls), 2)                                      # 모델마다 한 번, 5xx는 같은 모델 재시도 없음


if __name__ == "__main__":
    unittest.main()
