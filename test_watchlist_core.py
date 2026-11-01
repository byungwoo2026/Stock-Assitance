# -*- coding: utf-8 -*-
"""watchlist_core 단위 테스트 — 외부에서 온 JSON을 안전하게 받아들이는지 확인한다.

    python test_watchlist_core.py
"""
import json
import unittest

import watchlist_core as wlc


class NormalizeTests(unittest.TestCase):
    def test_code_normalization(self):
        self.assertEqual(wlc.normalize_code("5490"), "005490")          # 숫자는 6자리로 채움
        self.assertEqual(wlc.normalize_code(" 005930 "), "005930")
        self.assertEqual(wlc.normalize_code("0001a0"), "0001A0")        # 신규 영숫자 코드 허용
        for bad in ("", "삼성전자", "1234567", None, "00 593"):
            with self.assertRaises(ValueError):
                wlc.normalize_code(bad)

    def test_item_values_are_sanitized(self):
        item = wlc.normalize_item({"code": 5490, "name": "  ", "buy_price": -5, "qty": "abc", "memo": None})
        self.assertEqual(item, {"code": "005490", "name": "005490", "buy_price": None, "qty": None, "memo": ""})
        item = wlc.normalize_item({"code": "005930", "name": "삼성전자", "buy_price": "70,000", "qty": 3.9})
        self.assertIsNone(item["buy_price"])                            # 천단위 쉼표 문자열은 거부(None)
        self.assertEqual(item["qty"], 3)
        self.assertEqual(wlc.normalize_item({"code": "005930", "buy_price": 71000.5})["buy_price"], 71000.5)
        self.assertIsNone(wlc.normalize_item({"code": "005930", "buy_price": float("nan")})["buy_price"])

    def test_normalize_skips_bad_items_but_keeps_the_file(self):
        data = {"groups": {"A": [{"code": "005930", "name": "삼성전자"}, {"code": "잘못"}, "문자열",
                                 {"code": "5930", "name": "중복"}, {"code": "000660", "name": "SK하이닉스"}]}}
        wl = wlc.normalize(data)
        self.assertEqual([i["code"] for i in wl["groups"]["A"]], ["005930", "000660"])   # 잘못된 항목·중복 제외

    def test_structure_errors_are_rejected_with_a_message(self):
        for bad in ([], {"groups": []}, {"groups": {"A": "x"}}, "text", None):
            with self.assertRaises(ValueError):
                wlc.normalize(bad)
        with self.assertRaises(ValueError):
            wlc.loads("{not json")
        with self.assertRaises(ValueError):
            wlc.normalize({"groups": {f"g{i}": [] for i in range(wlc.MAX_GROUPS + 1)}})

    def test_empty_groups_get_default_and_group_is_capped(self):
        self.assertEqual(wlc.normalize({"groups": {}}), wlc.empty_watchlist())
        many = [{"code": f"{i:06d}"} for i in range(wlc.MAX_PER_GROUP + 10)]
        self.assertEqual(len(wlc.normalize({"groups": {"A": many}})["groups"]["A"]), wlc.MAX_PER_GROUP)


class RoundTripAndEditTests(unittest.TestCase):
    def test_json_roundtrip_keeps_korean_and_values(self):
        wl = wlc.empty_watchlist()
        wlc.add_stock(wl, "기본", "005490", "POSCO홀딩스")
        wlc.update_item(wl["groups"]["기본"][0], buy_price=300000, qty=10, memo="장기 보유")
        text = wlc.dumps(wl)
        self.assertIn("POSCO홀딩스", text)                              # ensure_ascii=False
        self.assertEqual(wlc.loads(text), wl)
        self.assertEqual(json.loads(text)["version"], wlc.SCHEMA_VERSION)

    def test_add_remove_and_limits(self):
        wl = wlc.empty_watchlist()
        self.assertTrue(wlc.add_stock(wl, "기본", "5930", "삼성전자"))
        self.assertFalse(wlc.add_stock(wl, "기본", "005930", "삼성전자"))   # 중복
        for i in range(wlc.MAX_PER_GROUP):
            wlc.add_stock(wl, "기본", f"{i + 100:06d}", f"종목{i}")
        self.assertEqual(len(wl["groups"]["기본"]), wlc.MAX_PER_GROUP)
        self.assertFalse(wlc.add_stock(wl, "기본", "999999", "넘침"))        # 그룹 상한
        wlc.remove_stocks(wl, "기본", ["005930"])
        self.assertNotIn("005930", [i["code"] for i in wl["groups"]["기본"]])

    def test_update_item_cleans_editor_values(self):
        item = wlc.normalize_item({"code": "005930", "name": "삼성전자"})
        wlc.update_item(item, buy_price=float("nan"), qty=None, memo=None)  # data_editor의 빈 셀은 NaN/None
        self.assertEqual((item["buy_price"], item["qty"], item["memo"]), (None, None, ""))
        wlc.update_item(item, buy_price=70000, qty=5.0, memo="x" * 500)
        self.assertEqual((item["buy_price"], item["qty"], len(item["memo"])), (70000.0, 5, wlc.MAX_MEMO_LEN))

    def test_group_management(self):
        wl = wlc.empty_watchlist()
        self.assertTrue(wlc.add_group(wl, " 반도체 "))
        self.assertFalse(wlc.add_group(wl, "반도체"))                       # 중복
        self.assertFalse(wlc.add_group(wl, "   "))                          # 빈 이름
        self.assertTrue(wlc.delete_group(wl, "기본"))
        self.assertFalse(wlc.delete_group(wl, "반도체"))                    # 마지막 그룹은 지우지 않는다
        self.assertEqual(list(wl["groups"]), ["반도체"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
