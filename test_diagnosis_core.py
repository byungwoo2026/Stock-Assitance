# -*- coding: utf-8 -*-
"""diagnosis_core 단위 테스트 — 합성 가격 데이터만 쓰므로 네트워크 없이 실행된다.

    python test_diagnosis_core.py
"""
import unittest

import numpy as np
import pandas as pd

import diagnosis_core as dc


def df_from_close(close, volume=None, start="2025-01-02"):
    """종가 배열 → fdr.DataReader와 같은 형태(Open/High/Low/Close/Volume, DatetimeIndex)의 DataFrame."""
    close = np.asarray(close, dtype=float)
    idx = pd.bdate_range(start, periods=len(close))
    vol = np.full(len(close), 1000.0) if volume is None else np.asarray(volume, dtype=float)
    return pd.DataFrame({"Open": close, "High": close * 1.005, "Low": close * 0.995,
                         "Close": close, "Volume": vol}, index=idx)


def double_bottom_df():
    """100 → 120(①) → 90(②) → 108(③ 넥라인) → 91(④) → 112(돌파) → 113. 마지막 봉 인덱스 80."""
    anchors = [(0, 100), (15, 120), (35, 90), (50, 108), (65, 91), (75, 112), (80, 113)]
    xs, ys = zip(*anchors)
    close = np.interp(np.arange(81), xs, ys)
    vol = np.full(81, 1000.0)
    vol[33:38] = 1500.0    # 첫 번째 저점 거래량
    vol[63:68] = 800.0     # 두 번째 저점 거래량 감소
    vol[74] = 2500.0       # 돌파일 거래량 급증
    return df_from_close(close, vol)


def prepared(df_raw, upto=None):
    df = dc.prepare(df_raw)
    return df if upto is None else df.iloc[:upto]


class PivotTests(unittest.TestCase):
    def test_zigzag_alternates_and_last_is_unconfirmed(self):
        piv = dc.zigzag_pivots(prepared(double_bottom_df()))
        self.assertGreaterEqual(len(piv), 5)
        self.assertTrue(all(a.kind != b.kind for a, b in zip(piv, piv[1:])))
        self.assertTrue(all(p.confirmed for p in piv[:-1]))
        self.assertFalse(piv[-1].confirmed)


class PatternTests(unittest.TestCase):
    def test_double_bottom_complete_with_five_points(self):
        df = prepared(double_bottom_df())
        bull = dc.detect_patterns(df, sign=+1)
        db = [p for p in bull if p.name == "쌍바닥"]
        self.assertEqual(len(db), 1)
        p = db[0]
        self.assertEqual(p.status, "완성")
        self.assertEqual(p.quality, "A")           # 대칭 + 2번째 저점 거래량 감소 + 돌파 거래량
        self.assertEqual([pt[0] for pt in p.points], [1, 2, 3, 4, 5])
        self.assertEqual(p.direction, "bull")

    def test_double_bottom_forming_before_breakout(self):
        df = prepared(double_bottom_df(), upto=74)   # 마지막 종가 107.8, 넥라인 108.5 직전
        db = [p for p in dc.detect_patterns(df, sign=+1) if p.name == "쌍바닥"]
        self.assertEqual([p.status for p in db], ["형성중"])

    def test_no_pattern_when_far_from_neckline(self):
        df = prepared(double_bottom_df(), upto=71)   # 종가 101.5 — 넥라인과 6% 이상 차이
        self.assertFalse([p for p in dc.detect_patterns(df, sign=+1) if p.name == "쌍바닥"])

    def test_mirrored_series_gives_double_top(self):
        base = double_bottom_df()
        mirror = df_from_close(220 - base["Close"].to_numpy(), base["Volume"].to_numpy())
        bear = dc.detect_patterns(prepared(mirror), sign=-1)
        dt = [p for p in bear if p.name == "쌍봉"]
        self.assertEqual(len(dt), 1)
        self.assertEqual((dt[0].status, dt[0].direction), ("완성", "bear"))

    def test_flat_noise_has_no_pattern(self):
        close = 100 + 0.3 * np.sin(np.arange(120) / 3)
        df = prepared(df_from_close(close))
        self.assertEqual(dc.detect_patterns(df, sign=+1), [])
        self.assertEqual(dc.detect_patterns(df, sign=-1), [])

    def test_box_breakout(self):
        close = np.append(105 + 4.5 * np.sin(2 * np.pi * np.arange(70) / 20), 113.0)
        vol = np.full(71, 1000.0)
        vol[70] = 3000.0
        df = prepared(df_from_close(close, vol))
        box = [p for p in dc.detect_patterns(df, sign=+1) if p.name == "박스권 돌파"]
        self.assertEqual(len(box), 1)
        self.assertEqual(box[0].status, "완성")

    def test_box_breakdown_is_bearish(self):
        close = np.append(105 + 4.5 * np.sin(2 * np.pi * np.arange(70) / 20), 97.0)
        df = prepared(df_from_close(close))
        names = [p.name for p in dc.detect_patterns(df, sign=-1)]
        self.assertIn("박스권 이탈", names)

    def test_inverse_head_and_shoulders(self):
        # 어깨 90 · 머리 80 · 어깨 91, 넥라인 ≈ 100. 마지막 종가가 넥라인 돌파.
        anchors = [(0, 110), (10, 90), (22, 100), (36, 80), (50, 100), (62, 91), (75, 103), (78, 104)]
        xs, ys = zip(*anchors)
        df = prepared(df_from_close(np.interp(np.arange(79), xs, ys)))
        hs = [p for p in dc.detect_patterns(df, sign=+1) if p.name == "역헤드앤숄더"]
        self.assertEqual(len(hs), 1)
        self.assertEqual(hs[0].status, "완성")


class RuleTests(unittest.TestCase):
    def test_grade_table(self):
        # 핵심 항목 3개(240일선·수급·거래량): 3/3 → A, 2/3(67%) → B, 1/3(33%) → D
        self.assertEqual([dc.grade_from_counts(n, 3) for n in range(3, -1, -1)], ["A", "B", "D", "D"])
        # 수급 평가 불가로 분모가 2가 되면: 2/2 → A, 1/2(50%) → C, 0 → D
        self.assertEqual([dc.grade_from_counts(n, 2) for n in range(2, -1, -1)], ["A", "C", "D"])
        self.assertIsNone(dc.grade_from_counts(0, 0))

    def test_gate_caps_grade_at_c(self):
        self.assertEqual(dc._apply_gate("A", False), "C")      # 240일선 아래면 점수와 무관하게 지켜보기 이하
        self.assertEqual(dc._apply_gate("B", False), "C")
        self.assertEqual(dc._apply_gate("D", False), "D")      # 이미 낮은 등급은 건드리지 않는다
        self.assertEqual(dc._apply_gate("A", True), "A")
        self.assertEqual(dc._apply_gate("A", None), "A")       # 이력 부족으로 평가 불가면 게이트 미적용
        self.assertEqual([dc.GRADE_VERDICT[g] for g in "ABCD"], ["매수 고려", "매수 고려", "지켜보기", "사지 않기"])

    def test_sell_verdict(self):
        self.assertEqual([dc._sell_verdict(n, 3) for n in range(4)], ["보유 유지", "지켜보기", "매도 고려", "매도 고려"])
        self.assertEqual([dc._sell_verdict(n, 2) for n in range(3)], ["보유 유지", "지켜보기", "매도 고려"])

    def test_only_scored_checks_count(self):
        checks = [dc.Check("추세", "a", True), dc.Check("수급", "b", False), dc.Check("수급", "c", None),
                  dc.Check("추세", "ref1", True, scored=False), dc.Check("패턴", "ref2", False, scored=False)]
        self.assertEqual(dc._count(checks), (1, 2))             # 참고 신호·평가 불가는 점수·분모에서 제외
        self.assertEqual(dc._first_fail(checks).label, "b")     # 참고 신호의 실패는 행동 문장 근거가 되지 않는다
        self.assertIsNone(dc._first_fail([dc.Check("추세", "ref", False, scored=False)]))

    def test_volume_rule_vs_20day_average(self):
        def ctx(mult, change):
            return {"vol_mult": mult, "close": 100.0 + change, "prev_close": 100.0}
        self.assertTrue(dc._volume_check(ctx(2.0, +1), +1).passed)        # 20일 평균의 2배 이상 → 통과
        self.assertFalse(dc._volume_check(ctx(1.99, +1), +1).passed)
        self.assertFalse(dc._volume_check(ctx(3.0, -1), +1).passed)       # 폭증이어도 하락봉이면 매수 신호 아님
        self.assertTrue(dc._volume_check(ctx(3.0, -1), -1).passed)        # 매도 진단에선 통과
        self.assertIsNone(dc._volume_check(ctx(float("nan"), +1), +1).passed)

    def test_alignment_check_is_reference_only(self):
        def ctx(*mas):
            return {f"ma{w}": m for w, m in zip(dc.ALIGN_WINDOWS, mas)}
        self.assertTrue(dc._align_check(ctx(5, 4, 3, 2), +1).passed)          # 5>10>20>60 정배열
        self.assertFalse(dc._align_check(ctx(5, 4, 3, 2), -1).passed)
        self.assertTrue(dc._align_check(ctx(2, 3, 4, 5), -1).passed)          # 역배열
        self.assertFalse(dc._align_check(ctx(5, 3, 4, 2), +1).passed)         # 10<20 → 정배열 아님
        self.assertIsNone(dc._align_check(ctx(5, 4, 3, float("nan")), +1).passed)
        self.assertFalse(dc._align_check(ctx(5, 4, 3, 2), +1).scored)         # 참고 신호: 점수 미반영

    def test_hold_verdict_priority(self):
        hv = dc._hold_verdict
        # (pnl, stop_breach, trend_break, flow_ok, above240, above_ma10, take_profit_hit)
        self.assertEqual(hv(-0.10, True, False, True, True, True, False), "손절 고려")
        self.assertEqual(hv(-0.03, False, True, True, True, True, False), "손절 고려")      # 손실 중 추세 이탈
        self.assertEqual(hv(+0.25, False, False, True, True, False, True), "분할 익절 고려")
        self.assertEqual(hv(+0.10, False, False, True, True, False, True), "보유 유지")     # +20% 미만은 익절 구간 아님
        self.assertEqual(hv(-0.04, False, False, True, True, True, False), "물타기 고려")
        self.assertEqual(hv(-0.04, False, False, False, True, True, False), "보유 유지")    # 수급 없으면 물타기 금지
        self.assertEqual(hv(-0.04, False, False, True, False, True, False), "보유 유지")    # 240일선 아래면 물타기 금지
        self.assertEqual(hv(-0.04, False, False, True, True, False, False), "보유 유지")    # 10일선 아래면 물타기 금지

    def test_flow_net(self):
        dates = pd.bdate_range("2025-06-02", periods=6)[::-1]
        flow = [{"date": d, "foreign": 100, "organ": -30} for d in dates]
        net = dc.flow_net(flow, dates[0])
        self.assertEqual((net["foreign"], net["organ"], net["sum"]), (500, -150, 350))
        self.assertIsNone(dc.flow_net(flow[:4], dates[0]))                      # 5일치가 안 되면 평가 불가
        self.assertIsNone(dc.flow_net(flow, dates[0] + pd.Timedelta(days=30)))  # 과거/먼 미래 시점은 평가 불가
        self.assertIsNone(dc.flow_net(None, dates[0]))

    def test_signal_summary_priority(self):
        def res(buy_verdict, sell_verdict):
            mk = lambda v: dc.ModeResult("x", v, None, 0, 0, [], "", "")
            return {"매수": mk(buy_verdict), "매도": mk(sell_verdict), "보유": mk("보유 유지")}
        self.assertEqual(dc.summarize_signal(res("매수 고려", "보유 유지")), "매수 고려")
        self.assertEqual(dc.summarize_signal(res("사지 않기", "지켜보기")), "사지 않기")
        self.assertEqual(dc.summarize_signal(res("지켜보기", "보유 유지")), "지켜보기")
        self.assertEqual(dc.summarize_signal(res("판정 불가", "판정 불가")), "지켜보기")
        self.assertEqual(dc.summarize_signal(res("매수 고려", "매도 고려")), "매도 고려")   # 위험 우선
        self.assertEqual(set(dc.SIGNAL_ORDER), {"매수 고려", "지켜보기", "사지 않기", "매도 고려"})


def box_after_uptrend_df(breakout_vol=3000.0):
    """250봉 상승(50→100) 후 100±4.5% 박스 70봉, 마지막 봉이 114로 돌파. 240일선·20일선·60일선 모두 아래."""
    close = np.concatenate([np.linspace(50, 100, 250), 100 + 4.5 * np.sin(2 * np.pi * np.arange(70) / 20), [114.0]])
    vol = np.full(len(close), 1000.0)
    vol[-1] = breakout_vol
    return df_from_close(close, vol)


class EndToEndTests(unittest.TestCase):
    def _flow_for(self, df_raw, foreign=100, organ=50):
        return [{"date": d, "foreign": foreign, "organ": organ} for d in df_raw.index[-5:][::-1]]

    def _checks(self, result):
        return {c.key: c for c in result.checks}

    def test_scored_keys_match_the_checklist(self):
        raw = box_after_uptrend_df()
        d = dc.diagnose("TEST", df_raw=raw, flow=self._flow_for(raw))
        for mode in ("매수", "매도"):
            scored = [c.key for c in d.results[mode].checks if c.scored]
            self.assertEqual(scored, list(dc.SCORED_KEYS))       # validate_grades.py가 같은 키로 점수를 재계산한다

    def test_patterns_are_reference_only(self):
        """쌍바닥은 완성돼도 차트·참고 신호로만 보이고 점수에는 들어가지 않는다(검증에서 초과수익이 음수)."""
        raw = double_bottom_df()
        d = dc.diagnose("TEST", df_raw=raw, flow=self._flow_for(raw))
        buy = self._checks(d.results["매수"])
        self.assertTrue(buy["pattern"].passed)                  # 패턴 자체는 감지·표시
        self.assertFalse(buy["pattern"].scored)                 # 그러나 점수 미반영
        self.assertIn("쌍바닥", buy["pattern"].detail)
        self.assertEqual(d.overlay_patterns("매수")[0].name, "쌍바닥")       # 차트에는 표시
        self.assertIn("참고용", d.pillars["패턴"]["detail"])
        # 이력이 80봉뿐이라 240일선은 평가 불가 → 분모에서 빠지고 게이트도 적용되지 않는다. 수급✓·거래량✗ → 1/2
        self.assertIsNone(buy["ma240"].passed)
        self.assertEqual((d.results["매수"].passed, d.results["매수"].total), (1, 2))

    def test_reference_signals_do_not_change_the_grade(self):
        raw = box_after_uptrend_df()
        d = dc.diagnose("TEST", df_raw=raw, flow=self._flow_for(raw))
        buy = d.results["매수"]
        self.assertEqual([c.passed for c in buy.checks if c.scored], [True, True, True])
        self.assertEqual((buy.grade, buy.verdict, buy.score_text), ("A", "매수 고려", "3/3"))
        # 참고 신호가 전부 실패해도 같은 등급: 참고 신호를 인위적으로 False로 바꿔 다시 센다
        for c in buy.checks:
            if not c.scored:
                c.passed = False
        self.assertEqual(dc._count(buy.checks), (3, 3))

    def test_240_gate_blocks_buy_even_with_other_signals(self):
        # 장기 하락 후 박스 돌파: 수급·거래량이 모두 좋아도 종가가 240일선 아래면 '매수 고려'가 될 수 없다
        close = np.concatenate([np.linspace(300, 100, 250), 100 + 4.5 * np.sin(2 * np.pi * np.arange(70) / 20), [114.0]])
        vol = np.full(len(close), 1000.0)
        vol[-1] = 3000.0
        raw = df_from_close(close, vol)
        d = dc.diagnose("TEST", df_raw=raw, flow=self._flow_for(raw))
        buy = d.results["매수"]
        checks = self._checks(buy)
        self.assertFalse(checks["ma240"].passed)
        self.assertTrue(checks["volume"].passed and checks["flow"].passed)
        self.assertEqual((buy.grade, buy.verdict), ("C", "지켜보기"))      # 2/3 → B → 게이트로 C
        self.assertIn("240일선", buy.action)

    def test_missing_flow_is_excluded_from_denominator(self):
        raw = box_after_uptrend_df()
        d = dc.diagnose("TEST", df_raw=raw, flow=None)
        buy = d.results["매수"]
        self.assertEqual((buy.passed, buy.total), (2, 2))
        self.assertIsNone(self._checks(buy)["flow"].passed)
        self.assertEqual(d.pillars["수급"]["state"], "확인 불가")

    def test_downtrend_is_not_a_buy(self):
        close = np.linspace(200, 90, 320)            # 240일선이 계산되는 긴 하락
        raw = df_from_close(close)
        d = dc.diagnose("TEST", df_raw=raw, flow=self._flow_for(raw, -100, -50))
        self.assertEqual(d.results["매수"].verdict, "사지 않기")
        self.assertEqual(d.results["매도"].verdict, "매도 고려")             # 240일선 아래 + 순매도 → 2/3
        self.assertIn("240일선", d.results["매수"].action)
        # 평단가가 없어도 240일선 아래 하락추세면 수급이 좋아도 '보유 유지'가 아니다
        self.assertEqual(dc.diagnose("TEST", df_raw=raw, flow=self._flow_for(raw)).results["보유"].verdict, "추세 점검")

    def test_hold_mode_needs_buy_price_for_pnl(self):
        raw = double_bottom_df()
        no_price = dc.diagnose("TEST", df_raw=raw, flow=self._flow_for(raw)).results["보유"]
        self.assertIsNone(no_price.extra["pnl"])
        self.assertIsNone(no_price.checks[0].passed)
        with_price = dc.diagnose("TEST", buy_price=140.0, df_raw=raw, flow=self._flow_for(raw)).results["보유"]
        self.assertAlmostEqual(with_price.extra["pnl"], 113 / 140 - 1)
        self.assertEqual(with_price.verdict, "손절 고려")     # 평단 대비 -19% → 손절선(-8%) 이탈

    def test_as_of_slices_history(self):
        raw = double_bottom_df()
        d = dc.diagnose("TEST", as_of=raw.index[73], df_raw=raw, flow=None)
        self.assertEqual(d.as_of, raw.index[73])
        self.assertEqual([p.status for p in d.patterns["bull"] if p.name == "쌍바닥"], ["형성중"])

    def test_to_row_for_watchlist_table(self):
        raw = box_after_uptrend_df()
        d = dc.diagnose("TEST", buy_price=90.0, df_raw=raw, flow=self._flow_for(raw))
        row = dc.to_row(d, "테스트")
        self.assertEqual((row["종목명"], row["코드"], row["신호"], row["매수 등급"], row["핵심 통과"]),
                         ("테스트", "TEST", "매수 고려", "A", "3/3"))
        self.assertAlmostEqual(row["손익%"], (114 / 90 - 1) * 100)
        self.assertEqual((row["_grade_rank"], row["_ratio"]), (0, 1.0))
        self.assertEqual(dc.to_row(dc.diagnose("TEST", df_raw=raw, flow=None))["보유 판정"], "-")   # 평단가 없으면 '-'


class MomentumReferenceTests(unittest.TestCase):
    """6~2개월 모멘텀(지수 대비)은 참고 신호다: 계산이 정의대로이고, 240일선 아래·지수 없음 처리가 맞고, 점수는 안 바꾼다."""

    N = 400

    def _index(self, values, start="2025-01-02"):
        return pd.Series(np.asarray(values, dtype=float), index=pd.bdate_range(start, periods=len(values)))

    def _buy(self, stock_close, index_close):
        raw = df_from_close(stock_close)
        d = dc.diagnose("TEST", df_raw=raw, flow=None, index_close=index_close)
        return d, {c.key: c for c in d.results["매수"].checks}["mom62"]

    def test_rel62_matches_the_definition(self):
        stock = np.geomspace(100, 600, self.N)
        idx = np.linspace(100, 110, self.N)
        df = dc.prepare(df_from_close(stock), self._index(idx))
        mom = stock[-1 - dc.MOM_NEAR_BARS] / stock[-1 - dc.MOM_FAR_BARS] - 1
        idx_mom = idx[-1 - dc.MOM_NEAR_BARS] / idx[-1 - dc.MOM_FAR_BARS] - 1
        self.assertAlmostEqual(df["MOM62"].iloc[-1], mom)
        self.assertAlmostEqual(df["REL62"].iloc[-1], mom - idx_mom)

    def test_passes_only_above_240_and_with_relative_strength(self):
        d, chk = self._buy(np.geomspace(100, 600, self.N), self._index(np.linspace(100, 110, self.N)))
        self.assertTrue(chk.passed)
        self.assertFalse(chk.scored)
        self.assertIn("240일선 위", chk.detail)
        # 240일선 아래: 종목이 하락 추세여도 지수가 더 크게 빠져 '지수 대비'는 크지만, 이 경우는 통과가 아니다
        d, chk = self._buy(np.linspace(200, 150, self.N), self._index(np.linspace(200, 60, self.N)))
        self.assertGreaterEqual(d.df["REL62"].iloc[-1], dc.MOM_REL_THRESHOLD)
        self.assertFalse(chk.passed)
        self.assertIn("240일선 아래", chk.detail)
        # 240일선 위여도 지수 대비 강하지 않으면 실패
        d, chk = self._buy(np.geomspace(100, 600, self.N), self._index(np.geomspace(100, 600, self.N)))
        self.assertFalse(chk.passed)

    def test_missing_index_or_short_history_is_not_evaluable(self):
        _, chk = self._buy(np.geomspace(100, 600, self.N), None)
        self.assertIsNone(chk.passed)
        self.assertIn("지수 데이터 없음", chk.detail)
        _, chk = self._buy(np.geomspace(100, 130, 100), self._index(np.linspace(100, 110, 100)))
        self.assertIsNone(chk.passed)                       # 240일선·6개월 이력이 없으면 평가 불가

    def test_reference_signal_never_changes_score_or_grade(self):
        stock = np.geomspace(100, 600, self.N)
        raw = df_from_close(stock)
        a = dc.diagnose("TEST", df_raw=raw, flow=None)
        b = dc.diagnose("TEST", df_raw=raw, flow=None, index_close=self._index(np.linspace(100, 110, self.N)))
        ra, rb = a.results["매수"], b.results["매수"]
        self.assertEqual((ra.grade, ra.verdict, ra.passed, ra.total), (rb.grade, rb.verdict, rb.passed, rb.total))
        self.assertEqual([c.key for c in rb.checks if c.scored], list(dc.SCORED_KEYS))
        self.assertNotIn("mom62", [c.key for c in a.results["매도"].checks])      # 매도 쪽은 검증하지 않아 두지 않았다

    def test_no_look_ahead_in_relative_momentum(self):
        rng = np.random.default_rng(2)
        stock = 100 * np.exp(np.cumsum(rng.normal(0.001, 0.02, self.N)))
        idx = self._index(100 * np.exp(np.cumsum(rng.normal(0.0005, 0.01, self.N))))
        raw = df_from_close(stock)
        t = 300
        a = dc.prepare(raw, idx).iloc[:t + 1]
        b = dc.prepare(raw.iloc[:t + 1], idx.iloc[:t + 1])           # 지수도 그 시점까지만 준다
        pd.testing.assert_series_equal(a["REL62"], b["REL62"], check_exact=False, rtol=1e-9, atol=1e-9)


class CausalityTests(unittest.TestCase):
    """과거 시점 진단이 미래 데이터에 영향을 받지 않아야 등급 검증(백테스트)이 의미가 있다."""

    def test_indicators_do_not_look_ahead(self):
        rng = np.random.default_rng(0)
        close = 100 * np.exp(np.cumsum(rng.normal(0, 0.015, 320)))
        full = df_from_close(close, rng.integers(500, 1500, 320))
        t = 250
        a = dc.prepare(full).iloc[:t + 1]
        b = dc.prepare(full.iloc[:t + 1])
        cols = ["MA5", "MA10", "MA20", "MA60", "MA240", "VOL_MULT", "ATR", "RSI", "MACD"]
        pd.testing.assert_frame_equal(a[cols], b[cols], check_exact=False, rtol=1e-9, atol=1e-9)

    def test_diagnosis_same_with_or_without_future_rows(self):
        rng = np.random.default_rng(1)
        close = 100 * np.exp(np.cumsum(rng.normal(0, 0.02, 320)))
        full = df_from_close(close, rng.integers(500, 1500, 320))
        for t in (120, 200, 260):
            cut = full.index[t]
            a = dc.diagnose("T", as_of=cut, df_raw=full, flow=None)
            b = dc.diagnose("T", df_raw=full.loc[:cut], flow=None)
            for mode in dc.MODES:
                ra, rb = a.results[mode], b.results[mode]
                self.assertEqual((ra.grade, ra.verdict, ra.passed, ra.total),
                                 (rb.grade, rb.verdict, rb.passed, rb.total), f"{mode} @ {t}")
            self.assertEqual([p.title for p in a.patterns["bull"]], [p.title for p in b.patterns["bull"]])


if __name__ == "__main__":
    unittest.main(verbosity=2)
