# -*- coding: utf-8 -*-
"""
xbrl_segments.py — DART 사업보고서 XBRL 원본(fnlttXbrl)에서 사업부문·제품/서비스·지역별 매출을 뽑는다. streamlit 비의존.

본문 HTML 표를 긁는 대신 XBRL 주석 팩트를 쓴다: 회사가 IFRS 8·15 주석에 태그한 '매출(Revenue)'을 차원 축으로 나눈 값이다.
  사업부문 SegmentsAxis · 제품/서비스 ProductsAndServicesAxis · 지역 GeographicalAreasAxis
회사 고유 축을 쓰거나 주석을 태그하지 않은 회사는 해당 보기가 비므로 '일부 실패 허용'이다(없으면 없다고 화면에 알린다).

계산 규칙
- 팩트는 (연결/별도 구분 축 + 구분 축 하나)만 가진 것만 쓴다. 부문 안의 제품처럼 축이 더 붙은 값은 버린다.
- 상·하위 구성이 함께 공시되면 이중 계산이 되므로, 합계(총매출 또는 내부거래 제거 전 부문 합)를 넘지 않는 선에서 큰 것부터 담는다.
- 사업부문은 내부거래 제거 전 금액이라 합이 연결 매출보다 클 수 있다(비중은 부문 합계 기준, 화면에 밝힘).
"""
import collections
import io
import json
import os
import re
import xml.etree.ElementTree as ET
import zipfile

import dart_core

AXES = {"segment": "SegmentsAxis", "product": "ProductsAndServicesAxis", "region": "GeographicalAreasAxis"}
AXIS_TITLES = {"segment": "사업부문", "product": "제품·서비스", "region": "지역"}
REVENUE_TAGS = ("Revenue", "RevenueFromContractsWithCustomers")
# 구성(축)별 값은 회사마다 요소가 다르다(에코프로비엠은 지역별 매출을 RevenueFromSaleOfGoodsProduct로 태그). 멤버마다 앞쪽 요소를 우선한다.
AXIS_REVENUE_TAGS = REVENUE_TAGS + ("RevenueFromSaleOfGoodsProduct", "RevenueFromSaleOfGoods", "RevenueFromRenderingOfServices")
OP_TAGS = ("OperatingIncomeLoss", "ProfitLossFromOperatingActivities")
OVERSHOOT = 1.01
SEGMENT_MIN_OF_REVENUE = 0.9       # 부문 합계가 연결 매출의 90%에도 못 미치면 주력 부문이 빠진 것 → 그 보기는 버린다

NS_XBRLI = "{http://www.xbrl.org/2003/instance}"
NS_XBRLDI = "{http://xbrl.org/2006/xbrldi}"
NS_LINK = "{http://www.xbrl.org/2003/linkbase}"
NS_XLINK = "{http://www.w3.org/1999/xlink}"


def local(name):
    """'ifrs-full:USMember' / '{ns}Revenue' → 'USMember' / 'Revenue'."""
    return re.split(r"[:}]", name)[-1]


def clean_label(text):
    """'DX 부문 [구성요소]' 같은 꼬리표를 지운다."""
    t = re.sub(r"\s*\[[^\]]*\]\s*$", "", text or "").strip()
    return re.sub(r"\s+", " ", t)


def parse_labels(lab_xml):
    """lab-ko.xml → {요소 id('entity00126380_Foo' 또는 'ifrs-full_USMember'): 한글 라벨}. 표준 라벨 역할만."""
    root = ET.fromstring(lab_xml)
    labels, loc, arcs = {}, {}, collections.defaultdict(list)
    for el in root.iter(NS_LINK + "label"):
        labels[el.get(NS_XLINK + "label")] = (el.get(NS_XLINK + "role") or "", el.text or "")
    for el in root.iter(NS_LINK + "loc"):
        loc[el.get(NS_XLINK + "label")] = el.get(NS_XLINK + "href", "").split("#")[-1]
    for el in root.iter(NS_LINK + "labelArc"):
        arcs[el.get(NS_XLINK + "from")].append(el.get(NS_XLINK + "to"))
    out = {}
    for lk, eid in loc.items():
        for to in arcs.get(lk, []):
            role, text = labels.get(to, ("", ""))
            if role.endswith("/label") and text.strip():
                out[eid] = clean_label(text)
                break
    return out


def parse_instance(inst_xml):
    """인스턴스 → (contexts, facts). contexts[id] = (start, end, {축: 멤버}), facts = [(태그 로컬명, contextRef, 숫자)]."""
    root = ET.fromstring(inst_xml)
    contexts = {}
    for c in root.iter(NS_XBRLI + "context"):
        period = c.find(NS_XBRLI + "period")
        if period is None:
            continue
        end = period.findtext(NS_XBRLI + "endDate") or period.findtext(NS_XBRLI + "instant")
        start = period.findtext(NS_XBRLI + "startDate")
        dims = {m.get("dimension"): (m.text or "").strip() for m in c.iter(NS_XBRLDI + "explicitMember")}
        if c.find(".//" + NS_XBRLDI + "typedMember") is not None:           # 개별 항목 목록(타입 차원)은 구성 비교에 못 쓴다
            continue
        contexts[c.get("id")] = (start, end, dims)
    facts = []
    for el in root:
        cid, text = el.get("contextRef"), (el.text or "").strip()
        if not cid or cid not in contexts or not text or not el.get("unitRef"):
            continue
        try:
            facts.append((local(el.tag), cid, float(text), el.get("unitRef")))
        except ValueError:
            continue
    return contexts, facts


def _days(a, b):
    from datetime import date
    y1, m1, d1 = map(int, a.split("-"))
    y2, m2, d2 = map(int, b.split("-"))
    return (date(y2, m2, d2) - date(y1, m1, d1)).days


def year_periods(contexts):
    """매출 같은 1년짜리 기간 중 가장 늦은 끝 날짜(당기)와 그 1년 전(전기). 못 찾으면 (None, None)."""
    ends = collections.Counter()
    for start, end, _ in contexts.values():
        if start and end and 330 <= _days(start, end) <= 400:
            ends[end] += 1
    if not ends:
        return None, None
    cur = max(ends)
    prev = next((e for e in sorted(ends, reverse=True) if 330 <= _days(e, cur) <= 400), None)
    return cur, prev


def select_members(items, total):
    """상·하위가 섞여 공시된 구성 중 합계를 넘지 않게 큰 것부터 담는다. items = [(이름, 값)]. 반환 (선택, 담긴 합계)."""
    pos = sorted([(n, v) for n, v in items if v and v > 0], key=lambda x: -x[1])
    if not pos:
        return [], 0.0
    full = sum(v for _, v in pos)
    if total is None or full <= total * OVERSHOOT:
        return pos, full
    picked, run = [], 0.0
    for n, v in pos:
        if run + v <= total * OVERSHOOT:
            picked.append((n, v))
            run += v
    return picked, run


def extract(contexts, facts, labels, fs="CFS"):
    """파싱된 XBRL → {'period_end', 'prev_end', 'axes': {축: {...}}}. 못 찾은 축은 빠진다."""
    cur_end, prev_end = year_periods(contexts)
    if not cur_end:
        return {"period_end": None, "prev_end": None, "axes": {}}
    want_fs = "ConsolidatedMember" if fs == "CFS" else "SeparateMember"
    fs_axis = "ConsolidatedAndSeparateFinancialStatementsAxis"

    def clean_dims(dims, axis):
        """연결/별도 축·'부문 합계' 축을 뺀 나머지가 정확히 target 축 하나인지. 맞으면 멤버 id."""
        rest = {local(k): local(v) for k, v in dims.items()}
        if rest.pop(fs_axis, want_fs) != want_fs:
            return None
        if axis is not None and rest.get("SegmentConsolidationItemsAxis") == "OperatingSegmentsMember":
            rest.pop("SegmentConsolidationItemsAxis")             # 부문별 값은 '영업부문 합산' 축이 붙어 있어도 받는다. 총매출(axis=None)에는 이 축이 붙은 값(부문 합계)을 섞지 않는다
        if axis is None:
            return "" if not rest else None
        return dims_member(dims, axis) if list(rest) == [axis] else None

    def dims_member(dims, axis):
        for k, v in dims.items():
            if local(k) == axis:
                return v
        return None

    by_tag = collections.defaultdict(list)
    for tag, cid, val, unit in facts:
        by_tag[tag].append((cid, val, unit))

    def series(tags, axis, pool=False):
        """{멤버: {cur, prev}} — 해당 축 하나만 가진 팩트의 당기·전기 값. 태그는 앞쪽 우선.
        pool=False면 값이 나오는 첫 요소만 쓰고, pool=True면 멤버마다 앞쪽 요소의 값을 모은다."""
        pooled = collections.defaultdict(dict)
        for tag in tags:
            out = collections.defaultdict(dict)
            for cid, val, unit in by_tag.get(tag, []):
                start, end, dims = contexts[cid]
                if end not in (cur_end, prev_end) or not start or not (330 <= _days(start, end) <= 400):
                    continue
                if axis is None:
                    if clean_dims(dims, None) is None:
                        continue
                    key = ""
                else:
                    key = clean_dims(dims, axis)
                    if key is None:
                        continue
                out[key].setdefault("cur" if end == cur_end else "prev", val)
                out[key]["unit"] = unit
            if out and not pool:
                return out
            for key, d in out.items():
                if key not in pooled:
                    pooled[key] = d
        return pooled

    total_rev = series(REVENUE_TAGS, None).get("", {})
    result = {"period_end": cur_end, "prev_end": prev_end, "axes": {}}
    for kind, axis in AXES.items():
        rev = series(AXIS_REVENUE_TAGS, axis, pool=True)
        if not rev:
            continue
        op = series(OP_TAGS, axis) if kind == "segment" else {}
        # 부문은 내부거래 제거 전 금액 → 합계 기준은 '부문 합계 팩트'(있으면), 없으면 모두 더한 값
        seg_total = None
        if kind == "segment":
            for cid, val, unit in by_tag.get("Revenue", []):
                start, end, dims = contexts[cid]
                rest = {local(k): local(v) for k, v in dims.items()}
                if end == cur_end and start and 330 <= _days(start, end) <= 400 and rest.get("SegmentConsolidationItemsAxis") == "OperatingSegmentsMember" \
                        and "SegmentsAxis" not in rest and rest.get(fs_axis, want_fs) == want_fs:
                    seg_total = val
                    break
        total = seg_total if kind == "segment" else total_rev.get("cur")
        cur_items = [(m, d.get("cur")) for m, d in rev.items()]
        picked, covered = select_members(cur_items, total)
        if not picked:
            continue
        seen, kept = set(), []
        for m, v in picked:                                   # 같은 라벨·같은 금액이 상·하위 멤버로 두 번 나오는 경우(대한항공) 한 번만
            label = labels.get(m.replace(":", "_"), local(m))
            if (label, round(v)) in seen:
                continue
            seen.add((label, round(v)))
            kept.append((m, label, v))
        covered = sum(v for _, _, v in kept)
        basis = total if total else covered
        cons = total_rev.get("cur")
        if kind == "segment" and cons and basis < cons * SEGMENT_MIN_OF_REVENUE:
            result.setdefault("rejected", {})[kind] = f"부문 합계가 연결 매출의 {basis / cons * 100:.0f}%뿐"
            continue
        members = []
        for m, label, v in kept:
            row = {"label": label, "cur": v, "prev": rev[m].get("prev"), "share": v / basis * 100 if basis else None}
            if m in op:
                row["op_cur"], row["op_prev"] = op[m].get("cur"), op[m].get("prev")
            members.append(row)
        result["axes"][kind] = {"title": AXIS_TITLES[kind], "members": members, "covered": covered, "total": basis,
                                "coverage": covered / basis if basis else None,
                                "consolidated_revenue": total_rev.get("cur"), "unit": next(iter(rev.values())).get("unit")}
    return result


def parse_zip(zf, fs="CFS"):
    names = zf.namelist()
    inst = next((n for n in names if n.endswith(".xbrl")), None)
    lab = next((n for n in names if n.endswith("lab-ko.xml")), None)
    if not inst:
        raise ValueError("XBRL 인스턴스 파일이 없습니다.")
    labels = parse_labels(zf.read(lab)) if lab else {}
    contexts, facts = parse_instance(zf.read(inst))
    return extract(contexts, facts, labels, fs)


def fetch_segments(rcept_no, fs="CFS", reprt_code="11011"):
    """사업보고서 접수번호 → 부문 구성. 결과(작은 JSON)는 dart_cache에 영구 보관한다(정정 공시는 접수번호가 달라진다)."""
    path = os.path.join(dart_core.CACHE_DIR, f"seg_{rcept_no}_{fs}.json")
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        pass
    zf = dart_core.get_zip("fnlttXbrl.xml", rcept_no=rcept_no, reprt_code=reprt_code)
    result = parse_zip(zf, fs)
    try:
        os.makedirs(dart_core.CACHE_DIR, exist_ok=True)
        tmp = f"{path}.{os.getpid()}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False)
        os.replace(tmp, path)
    except OSError:
        pass
    return result
