# -*- coding: utf-8 -*-
"""
segment_mix.py — 종목의 '매출 구성'(사업부문·제품/서비스·지역) 화면용 자료. streamlit 비의존.

출처 우선순위(보기마다 출처를 밝힌다)
  1. 사업보고서 XBRL 주석(xbrl_segments) — 회사가 태그한 값. 구조적이라 가장 믿을 만하다.
  2. 제품/서비스 보기가 XBRL에 없거나 일부만(<95%) 있으면 본문 '매출실적' 표(sales_table) — 자동 해석이라
     합계가 재무제표 매출과 ±3% 안에 들 때만 채택한다.
못 읽은 보기는 만들지 않는다(틀린 구성을 보여주지 않는다). 구분되지 않은 나머지는 '그 외' 행으로 따로 표시한다.
"""
import json
import os

import dart_core
import sales_table
import xbrl_segments

XBRL_SOURCE = "사업보고서 XBRL 주석"
TABLE_SOURCE = "사업보고서 본문 '매출실적' 표(자동 해석)"
REST_LABEL = "그 외(공시에서 구분되지 않은 부분)"
FULL_COVERAGE = 0.95       # 이 이상이면 '구성 전체'로 본다
USABLE_COVERAGE = 0.80     # XBRL 제품 구성이 이 이상이면 본문 표보다 XBRL을 쓴다(나머지는 '그 외' 행)


def _view(key, title, source, members, total, covered, note=None):
    rest = total - covered if total else 0
    rows = [dict(m) for m in members]
    if total and rest > total * 0.005:
        rows.append({"label": REST_LABEL, "cur": rest, "prev": None, "share": rest / total * 100, "derived": True})
    return {"key": key, "title": title, "source": source, "members": rows, "total": total, "covered": covered,
            "coverage": covered / total if total else None, "note": note,
            "has_prev": any(m.get("prev") is not None for m in members), "has_op": any(m.get("op_cur") is not None for m in members)}


MIN_MEMBERS = 2            # 구성이 하나뿐인 보기('반도체 장비 등 100%')는 정보가 없어 만들지 않는다


def _informative(view):
    return sum(1 for m in view["members"] if not m.get("derived")) >= MIN_MEMBERS


def views_from_xbrl(xb):
    out = {}
    for key, ax in (xb.get("axes") or {}).items():
        note = None
        if key == "segment" and ax.get("consolidated_revenue") and ax["total"] and abs(ax["total"] - ax["consolidated_revenue"]) / ax["consolidated_revenue"] > 0.01:
            note = "사업부문 매출은 부문 간 내부거래를 제거하기 전 금액이라 합계가 연결 매출과 다릅니다. 비중은 부문 합계 기준입니다."
        elif ax.get("coverage") is not None and ax["coverage"] < FULL_COVERAGE:
            note = f"회사가 이 구분으로 공시한 매출은 연결 매출의 {ax['coverage'] * 100:.0f}%입니다. 나머지는 '그 외'로 묶었습니다."
        view = _view(key, ax["title"], XBRL_SOURCE, ax["members"], ax["total"], ax["covered"], note)
        if _informative(view):
            out[key] = view
    return out


def compact_members(members, top=10):
    """긴 구성 목록은 큰 순 top개만 두고 나머지를 한 줄로 묶는다('그 외' 파생 행은 마지막에 그대로)."""
    real = sorted([m for m in members if not m.get("derived")], key=lambda m: -m["cur"])
    derived = [m for m in members if m.get("derived")]
    if len(real) <= top:
        return real + derived
    head, tail = real[:top], real[top:]
    prev_ok = all(m.get("prev") is not None for m in tail)
    rest = {"label": f"나머지 {len(tail)}개 합계", "cur": sum(m["cur"] for m in tail), "prev": sum(m["prev"] for m in tail) if prev_ok else None,
            "share": sum(m["share"] for m in tail if m.get("share") is not None), "derived": True}
    return head + [rest] + derived


def view_from_table(res):
    members = [{"label": i["label"], "cur": i["cur"], "prev": i["prev"], "share": i["share"]} for i in res["items"]]
    note = None if res.get("prev_ok") else "전기 금액이 일부 비어 있어 증감률은 표시하지 않습니다."
    return _view("product", "제품·서비스", TABLE_SOURCE, members, res["sum_won"], res["sum_won"], note)


def build_mix(xb, table_res=None):
    """XBRL 결과(+선택적 본문 표 결과) → {'views': [화면 순서대로], 'period_end', 'prev_end'}."""
    views = views_from_xbrl(xb or {})
    prod = views.get("product")
    if table_res and (not prod or (prod["coverage"] or 0) < USABLE_COVERAGE):
        table_view = view_from_table(table_res)
        if _informative(table_view):
            views["product"] = table_view
        elif not prod:
            views.pop("product", None)
    order = [k for k in ("segment", "product", "region") if k in views]
    return {"views": [views[k] for k in order], "period_end": (xb or {}).get("period_end"), "prev_end": (xb or {}).get("prev_end")}


def fetch_mix(rcept_no, fs, revenue_won=None):
    """접수번호 → 매출 구성. 계산 결과(작은 JSON)를 dart_cache에 보관한다. 본문 표는 XBRL로 제품 보기를 못 채운 경우에만 받는다."""
    path = os.path.join(dart_core.CACHE_DIR, f"mix_{rcept_no}_{fs}.json")
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        pass
    xb, errors = {}, []
    try:
        xb = xbrl_segments.fetch_segments(rcept_no, fs)
    except Exception as e:                                     # XBRL이 없거나 형식이 달라도 본문 표로 이어간다
        if "zip이 아닌" not in str(e):                         # XBRL 원본이 아예 없는 회사는 정상 상태 → 오류로 치지 않고 결과를 캐시한다
            errors.append(f"XBRL {type(e).__name__}: {str(e)[:80]}")
    prod = views_from_xbrl(xb).get("product")
    table_res = None
    if revenue_won and (not prod or (prod["coverage"] or 0) < USABLE_COVERAGE):
        try:
            doc = dart_core.download_document(rcept_no, cache=False)
            res = sales_table.parse_sales(doc, revenue_won)
            table_res = res if res and res.get("items") else None
        except Exception as e:
            errors.append(f"본문 {type(e).__name__}: {str(e)[:80]}")
    mix = build_mix(xb, table_res)
    mix["rcept_no"], mix["errors"] = rcept_no, errors
    if not errors:                                             # 일시 오류는 캐시에 굳히지 않는다
        try:
            os.makedirs(dart_core.CACHE_DIR, exist_ok=True)
            tmp = f"{path}.{os.getpid()}.tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(mix, f, ensure_ascii=False)
            os.replace(tmp, path)
        except OSError:
            pass
    return mix
