# -*- coding: utf-8 -*-
"""invest_sites_core 단위 테스트 — 네트워크 없이 HTML 조각·합성 시세로 실행된다.

    python test_invest_sites_core.py
"""
import unittest

import numpy as np
import pandas as pd

import invest_sites_core as isc

NAMES = {n: {"code": c, "market": m} for n, c, m in [
    ("삼성전자", "005930", "KOSPI"), ("SK", "034730", "KOSPI"), ("SK하이닉스", "000660", "KOSPI"), ("현대차", "005380", "KOSPI"),
    ("현대차증권", "001500", "KOSPI"), ("LG화학", "051910", "KOSPI"), ("LG에너지솔루션", "373220", "KOSPI"), ("풍산", "103140", "KOSPI"),
    ("대우건설", "047040", "KOSPI"), ("두산에너빌리티", "034020", "KOSPI"), ("산일전기", "062040", "KOSPI"), ("에코프로", "086520", "KOSDAQ")]}


class ParseKrwTests(unittest.TestCase):
    def test_korean_units(self):
        self.assertEqual(isc.parse_krw("84만원"), 840000)
        self.assertEqual(isc.parse_krw("56만2000원"), 562000)
        self.assertEqual(isc.parse_krw("5,700원"), 5700)
        self.assertEqual(isc.parse_krw("357,400원"), 357400)
        self.assertIsNone(isc.parse_krw("많이"))
        self.assertIsNone(isc.parse_krw(None))


class FactTests(unittest.TestCase):
    def test_target_change_with_both_prices(self):
        f = isc.extract_facts("[버핏 리포트] 현대차, 환율·비용 절감으로 지킨 3분기 이익 - NH",
                              "NH투자증권이 현대차(005380)에 대해 투자의견 '매수'를 유지하고 목표주가를 기존 54만원에서 44만원으로 하향 조정했다.")
        self.assertEqual((f["broker"], f["opinion"], f["target_prev"], f["target_price"], f["target_dir"]), ("NH투자증권", "매수", 540000, 440000, "하향"))

    def test_direction_from_percent_phrase(self):
        f = isc.extract_facts("[버핏 리포트] 제주항공, 단거리 수요 호조에도 경쟁 부담…'목표주가 5700원' - NH투자증권",
                              "투자의견 '보유(Hold)'를 유지하고 목표주가를 기존보다 4% 높인 5700원으로 제시했다.")
        self.assertEqual((f["opinion"], f["target_price"], f["target_dir"]), ("보유", 5700, "상향"))
        self.assertNotIn("target_prev", f)

    def test_target_maintained(self):
        f = isc.extract_facts("[버핏 리포트] 한화에어로스페이스, 3Q 영업익 46%↓에도 목표가 유지 - 유안타", "투자의견 '매수'와 목표주가 148만원을 유지했다.")
        self.assertEqual((f["target_price"], f["target_dir"], f["broker"]), (1480000, "유지", "유안타증권"))

    def test_smic_report_valuation(self):
        f = isc.extract_facts("Equity Research, 산일전기", "목표주가 357,400원, 상승여력 60.64%, 투자의견 Buy를 제시한다.")
        self.assertEqual((f["target_price"], f["opinion"]), (357400, "Buy"))

    def test_nothing_is_invented(self):
        self.assertEqual(isc.extract_facts("[이슈 체크] 한수원, 체코원전 시공사 재선정", "내용에 숫자가 없다."), {})

    def test_tone(self):
        self.assertEqual(isc.classify_tone("x", {"target_dir": "상향"}), "긍정")
        self.assertEqual(isc.classify_tone("x", {"target_dir": "하향"}), "부정")
        self.assertEqual(isc.classify_tone("[버핏 리포트] 삼성전자, 3분기 영업이익 컨센서스 하회...부진 - 대신", {}), "부정")
        self.assertEqual(isc.classify_tone("[버핏 리포트] 삼성전자, 3분기 영업이익 컨센서스 상회...호조 - 대신", {}), "긍정")
        self.assertEqual(isc.classify_tone("[버핏 리포트] 삼성전자, 3분기 실적 발표 - 대신", {}), "중립")


class StockMatchTests(unittest.TestCase):
    def test_longest_name_wins_and_ascii_boundary(self):
        self.assertEqual(isc.find_names("SK하이닉스, 3분기 영업익", NAMES), ["SK하이닉스"])          # 'SK'가 같이 잡히면 안 된다
        self.assertEqual(isc.find_names("SKC 급등", NAMES), [])                                    # 'SK'가 'SKC' 일부로 잡히면 안 된다
        self.assertEqual(isc.find_names("현대차증권 리포트", NAMES), ["현대차증권"])                 # '현대차'보다 긴 이름 우선
        self.assertEqual(isc.find_names("대우건설·두산에너빌리티엔 직접 타격", NAMES), ["대우건설", "두산에너빌리티"])

    def test_primary_and_related_from_titles(self):
        s = isc.extract_stocks("buffett", "[버핏 리포트] 삼성전자, 3분기 실적에 시장 반응 냉소적 - 대신", "", NAMES)
        self.assertEqual([(x["name"], x["role"]) for x in s], [("삼성전자", "primary")])         # 꼬리표 '대신'은 종목이 아니다
        s = isc.extract_stocks("eduinfom", "LG화학 주가분석: LG에너지솔루션 지분 매각 보도, 현금 확보", "", NAMES)
        self.assertEqual([(x["name"], x["role"]) for x in s], [("LG화학", "primary"), ("LG에너지솔루션", "related")])
        s = isc.extract_stocks("smic", "Equity Research, 산일전기", "산일전기 (KRX:062040)", NAMES)
        self.assertEqual(s[0]["code"], "062040")
        self.assertEqual(isc.extract_stocks("smic", "Equity Research, nebius group N.V.", "", NAMES), [])    # 해외 종목은 비어 있다

    def test_code_in_body_is_used_when_title_has_no_name(self):
        s = isc.extract_stocks("buffett", "[이슈 체크] 반도체 대형주 일제히 급등", "삼성전자(005930)와 ...", NAMES)
        self.assertEqual(s[0]["code"], "005930")


class ParserTests(unittest.TestCase):
    def test_buffett_list_and_article(self):
        html = """<ul><li><a class="image" href="../news/view.php?idx=57548&mcode=m331evx"><img/></a><dl>
          <dt class="title"><a href="../news/view.php?idx=57548&mcode=m331evx">[버핏 리포트] 삼성전자, 3분기 실적 - 대신</a></dt>
          <dd class="content"><a href="x">대신증권은 삼성전자(005930)에 대해…</a></dd>
          <dd class="registDate"><span>2026-10-30</span><span class="writer">기자</span></dd></dl></li>
          <li><a href="../news/view.php?idx=57548">중복</a><dl><dt class="title"><a href="../news/view.php?idx=57548">같은 글</a></dt></dl></li></ul>"""
        items = isc.parse_buffett_list(html)
        self.assertEqual(len(items), 1)
        self.assertEqual((items[0]["id"], items[0]["date"], items[0]["url"]), ("buffett:57548", "2026-10-30", "https://buffettlab.co.kr/news/view.php?idx=57548"))
        art = """<html><body>기사등록 2026-10-30 09:27:18<div id="viewContent"><div class="fr-view">하루에도 수십개씩 쏟아지는 증권사 리포트. 이 가운데 선별해 소개합니다. [편집자주]
          대신증권은 삼성전자(005930)에 대해 매수를 유지했다. 목표주가는 50만원이다.</div><div>관련기사 [버핏 리포트] 다른 글</div></div></body></html>"""
        d = isc.parse_buffett_article(art)
        self.assertEqual(d["date"], "2026-10-30")
        self.assertTrue(d["text"].startswith("대신증권은 삼성전자"))
        self.assertNotIn("관련기사", d["text"])

    def test_eduinfom_list_and_article(self):
        html = """<main><article class="post-card"><a href="/2026/10/14247/">풍산 주가분석: 실적 전망을 좌우하는 구리 가격</a><span>2026. 10. 30.</span></article>
                  <article class="post-card"><a href="/2026/10/14246/">서울반도체 주가분석: 양산 일정</a><span>2026. 10. 29.</span></article></main>"""
        items = isc.parse_eduinfom_list(html)
        self.assertEqual([(i["id"], i["date"]) for i in items], [("eduinfom:14247", "2026-10-30"), ("eduinfom:14246", "2026-10-29")])
        art = """<html><head><meta property="article:published_time" content="2026-10-30T06:51:07.641Z"></head><body>이도현 · AI 보조 작성
          <div class="entry-content"><div class="box"><p>📌 핵심 요약</p><ul><li>첫째 요점이다.</li><li>둘째 요점이다.</li></ul></div><h2>본문</h2><p>내용</p></div></body></html>"""
        d = isc.parse_eduinfom_article(art)
        self.assertEqual((d["bullets"], d["date"], d["ai_assisted"]), (["첫째 요점이다.", "둘째 요점이다."], "2026-10-30", True))

    def test_smic_list_with_korean_dates(self):
        html = """<div><article><a href="http://snusmic.com/equity-research-%ec%82%b0%ec%9d%bc/">Equity Research, 산일전기</a><span>10월 18, 2026</span>
                  <a href="http://snusmic.com/equity-research-%ec%82%b0%ec%9d%bc/">Read More</a></article>
                  <article><a href="http://snusmic.com/equity-research-natera-inc/">Equity Research, natera, inc.</a><span>6월 5, 2026</span></article></div>"""
        items = isc.parse_smic_list(html)
        self.assertEqual([(i["title"], i["date"]) for i in items], [("Equity Research, 산일전기", "2026-10-18"), ("Equity Research, natera, inc.", "2026-06-05")])
        self.assertEqual(isc.parse_smic_post('<a href="http://snusmic.com/wp-content/uploads/2026/10/a.pdf">download</a>')["pdf"], "http://snusmic.com/wp-content/uploads/2026/10/a.pdf")

    def test_robots_paths_are_not_requested(self):
        f = isc.Fetcher(delay=0)
        self.assertFalse(f.allowed("https://buffettlab.co.kr/adm/login.php"))
        self.assertFalse(f.allowed("https://finance.eduinfom.com/wp-json/wp/v2/posts"))
        self.assertTrue(f.allowed("https://buffettlab.co.kr/news/list.php?mcode=m331evx"))


class SummaryTests(unittest.TestCase):
    def test_sentence_split_does_not_cut_inside_a_sentence(self):
        text = "[버핏연구소=김지원 기자] NH투자증권은 제주항공에 대해 투자의견 보유를 유지하고 목표주가를 기존보다 4% 높인 5700원으로 제시했다. 단거리 수요는 견조하다."
        s = isc.split_sentences(text)
        self.assertEqual(len(s), 1)                                  # 두 번째 문장은 20자 미만이라 제외, 첫 문장은 '4% 높인'에서 끊기지 않는다
        self.assertTrue(s[0].startswith("NH투자증권은"))
        self.assertTrue(s[0].endswith("제시했다."))

    def test_smic_summary_prefers_valuation_sentence(self):
        text = ("2026년 10월 10일 산일전기 (KRX:062040) 천하제일! 그동안 전력기기 시장은 초고압변압기의 독주 무대였지만 특수변압기 시장에도 새로운 기회가 열리고 있다. "
                "동사의 2028E EPS에 Target PER을 곱한 목표주가 357,400원, 상승여력 60.64%, 투자의견 Buy를 제시한다.")
        out = isc.rule_summary("smic", "t", text)
        self.assertIn("목표주가 357,400원", out[-1])

    def test_source_bullets_are_used_as_is(self):
        self.assertEqual(isc.rule_summary("eduinfom", "t", "긴 본문", bullets=["a" * 30, "b" * 30]), ["a" * 30, "b" * 30])


class ReactionTests(unittest.TestCase):
    def _series(self, n=120, start="2026-01-02"):
        idx = pd.bdate_range(start, periods=n)
        stock = pd.Series(100.0, index=idx)
        index = pd.Series(1000.0, index=idx)
        return stock, index, idx

    def test_windows_and_abnormal_returns(self):
        stock, index, idx = self._series()
        t0 = 60
        stock.iloc[t0:] = 110.0                       # 공표일에 +10%
        stock.iloc[t0 + 5:] = 121.0                   # 5거래일 뒤 추가 +10%
        index.iloc[t0:] = 1020.0                      # 지수 +2%
        rx = isc.compute_reaction(stock, index, idx[t0])
        self.assertAlmostEqual(rx["d0"], 0.10, places=4)
        self.assertAlmostEqual(rx["abn_d0"], 0.08, places=4)
        self.assertAlmostEqual(rx["post5"], 0.10, places=4)           # 공표일 종가(110) 대비 5일 뒤 121
        self.assertAlmostEqual(rx["abn_post5"], 0.10, places=4)       # 지수는 공표일 이후 변화 없음
        self.assertAlmostEqual(rx["pre20"], 0.0, places=6)

    def test_weekend_publication_uses_next_trading_day(self):
        stock, index, idx = self._series()
        sat = idx[60] + pd.Timedelta(days=(5 - idx[60].weekday()) % 7 + 0)       # 그 주 토요일
        pos = idx.searchsorted(sat)
        stock.iloc[pos:] = 105.0
        rx = isc.compute_reaction(stock, index, sat)
        self.assertAlmostEqual(rx["d0"], 0.05, places=4)             # 월요일(다음 거래일)이 공표 당일 반응

    def test_missing_future_windows_and_short_history(self):
        stock, index, idx = self._series()
        rx = isc.compute_reaction(stock, index, idx[-3])
        self.assertIn("post1", rx)
        self.assertNotIn("post5", rx)
        self.assertNotIn("post20", rx)
        self.assertIsNone(isc.compute_reaction(stock.iloc[:20], index.iloc[:20], idx[10]))
        self.assertIsNone(isc.compute_reaction(stock, index, idx[5]))   # 공표 전 20거래일 이력이 없으면 계산하지 않는다

    def test_aggregate_uses_date_level_means_and_skips_thin_groups(self):
        days = pd.bdate_range("2026-01-05", periods=40)
        arts = []
        for i, d in enumerate(days):
            for j in range(2):                                   # 같은 날 글 2건은 하루 평균으로 묶인다
                arts.append({"source": "buffett", "tone": "긍정", "date": d.strftime("%Y-%m-%d"), "stocks": [{"code": f"{i:03d}{j:03d}"}],
                             "reaction": {"abn_post5": 0.01 * (i - 20) + 0.02 * j, "abn_d0": 0.0, "abn_pre20": 0.0}})
        arts.append(dict(arts[0]))                                # 같은 종목·날짜·출처의 중복은 한 번만
        agg = isc.aggregate_impact(arts)
        total = agg[agg["그룹"] == "전체"].iloc[0]
        self.assertEqual((total["글 수"], total["날짜 수"]), (80, 40))
        self.assertAlmostEqual(total["공표 후 5일 초과%"], np.mean([0.01 * (i - 20) + 0.01 for i in range(40)]) * 100, places=4)
        self.assertTrue(np.isnan(total["공표 후 20일 초과%"]))      # 해당 열이 없으면 비운다
        thin = isc.aggregate_impact(arts[:20])                    # 날짜가 10개뿐이면 숫자를 내지 않는다
        self.assertTrue(np.isnan(thin.iloc[0]["공표 후 5일 초과%"]))

    def test_nw_t(self):
        self.assertTrue(np.isnan(isc.nw_t([1.0] * 10, 2)))        # 표본 부족
        x = np.r_[np.ones(30), -np.ones(30)] * 0.01 + 0.05
        self.assertGreater(isc.nw_t(x, 3), 5)


class StorageTests(unittest.TestCase):
    def test_history_shards_keep_only_impact_fields_and_skip_unchanged(self):
        import tempfile, os
        arts = [{"id": "buffett:1", "source": "buffett", "date": "2026-03-05", "title": "긴 제목", "url": "http://x", "summary": ["요약"], "tone": "긍정",
                 "stocks": [{"code": "005930", "name": "삼성전자", "market": "KOSPI", "role": "primary"}, {"code": "000660", "name": "SK하이닉스", "market": "KOSPI"}],
                 "reaction": {"d0": 0.01, "abn_post5": 0.02}},
                {"id": "buffett:2", "source": "buffett", "date": "2026-04-01", "tone": "중립", "stocks": [], "reaction": {}}]
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(isc.save_history(d, arts), 2)                      # 3월·4월 샤드 2개
            self.assertEqual(isc.save_history(d, arts), 0)                      # 같은 내용이면 다시 쓰지 않는다(매일 커밋에 섞이지 않게)
            loaded = isc.load_history(d)
            self.assertEqual(sorted(a["id"] for a in loaded), ["buffett:1", "buffett:2"])
            a1 = next(a for a in loaded if a["id"] == "buffett:1")
            self.assertNotIn("title", a1)
            self.assertNotIn("summary", a1)
            self.assertEqual([s["code"] for s in a1["stocks"]], ["005930"])      # 주 종목만 남긴다
            self.assertEqual(a1["reaction"]["abn_post5"], 0.02)
            self.assertEqual(sorted(os.listdir(d)), ["2026-03.json", "2026-04.json"])

    def test_data_roundtrip_is_compact_utf8(self):
        import tempfile, os
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "x.json")
            isc.save_data(path, {"version": 1, "articles": [{"id": "a", "title": "한글 제목"}]})
            text = open(path, encoding="utf-8").read()
            self.assertIn("한글 제목", text)
            self.assertNotIn("\n", text)                                       # 줄바꿈 없는 한 줄(컴팩트) JSON
            self.assertEqual(isc.load_data(path)["articles"][0]["title"], "한글 제목")
            self.assertEqual(isc.load_data(os.path.join(d, "missing.json"))["articles"], [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
