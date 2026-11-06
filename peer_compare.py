# -*- coding: utf-8 -*-
"""
peer_compare.py — 같은 업종 비교 종목을 고르고, 이 종목과 같은 방식(company_report.build_report)으로 계산한 지표를 나란히 놓는다. streamlit 비의존.

원칙
- 업종 분류는 네이버 증권의 업종(industryCode)과 그 업종 종목 목록을 쓴다. 목록 조회(HTTP)는 호출하는 쪽이 하고 여기서는 JSON을 해석만 한다.
- 비교 종목은 같은 업종에서 시가총액이 비슷한 종목(로그 거리)을 고른다. 우선주·SPAC·코넥스·DART에 없는 종목은 뺀다.
- 이 종목과 비교 종목의 지표는 모두 company_report.build_report 한 함수로 계산한다(네이버가 보여주는 PER 등을 섞지 않는다).
- 중앙값은 비교 종목만으로 낸다(이 종목 제외). 적자(PER 없음)·자본잠식(PBR 없음)은 중앙값에서 뺀다.
"""
import math
import re
import statistics

MAX_PEERS = 8
DEFAULT_PEERS = 6
MAX_ABS_MARGIN = 100.0      # 영업이익률 절댓값이 이를 넘으면 비교에서 뺀다(매출 구조가 다른 회사)
MIN_FOR_MEDIAN = 3          # 값이 이보다 적으면 중앙값을 내지 않는다(한두 곳 값을 '업종 중앙값'이라 부르지 않는다)
SPAC_RE = re.compile(r"스팩|기업인수목적|SPAC", re.I)
METRICS = (("market_cap", "시가총액", "won"), ("per", "PER(후행)", "mult"), ("pbr", "PBR", "mult2"), ("psr", "PSR(후행)", "mult2"),
           ("op_margin", "영업이익률(후행)", "pct"), ("roe", "ROE", "pct"), ("rev_growth", "매출 증가율(전년비)", "pct"), ("debt_ratio", "부채비율", "pct0"))


def parse_industry(payload):
    """네이버 업종 종목 목록 JSON → {'name','code','stocks':[{code,name,market_cap,kind}]}. market_cap은 원."""
    group = (payload or {}).get("groupInfo") or {}
    stocks = []
    for s in (payload or {}).get("stocks") or []:
        cap = 0.0
        for field, unit in (("marketValueRaw", 1.0), ("marketValue", 1e8)):       # Raw는 원, 일반 필드는 억원 문자열
            try:
                cap = float(str(s.get(field) or "").replace(",", "")) * unit
            except ValueError:
                cap = 0.0
            if cap:
                break
        stocks.append({"code": s.get("itemCode"), "name": s.get("stockName") or "", "market_cap": cap, "kind": s.get("stockEndType")})
    return {"name": group.get("name"), "code": group.get("no"), "stocks": stocks}


def pick_peers(industry, subject_code, subject_cap, dart_codes, n=DEFAULT_PEERS):
    """같은 업종에서 시가총액이 가장 비슷한 n개(로그 거리). 반환은 시가총액 큰 순 종목코드 목록."""
    pool = []
    for s in industry.get("stocks", []):
        code = s.get("code")
        if (not code or code == subject_code or s.get("kind") != "stock" or code not in dart_codes
                or SPAC_RE.search(s.get("name", "")) or s.get("market_cap", 0) <= 0):
            continue
        pool.append(s)
    if subject_cap and subject_cap > 0:
        pool.sort(key=lambda s: abs(math.log(s["market_cap"] / subject_cap)))
    else:
        pool.sort(key=lambda s: -s["market_cap"])
    return [s["code"] for s in sorted(pool[:n], key=lambda s: -s["market_cap"])]


def metrics_from_report(rep):
    """build_report 결과 한 건 → 비교용 지표. 계산은 모두 보고서 안에서 끝난 값을 옮겨 담을 뿐이다."""
    h, rows = rep["header"], rep["annual"]["rows"]
    last, prev = rows[-1], rows[-2] if len(rows) > 1 else {}
    growth = None
    if last.get("revenue") is not None and prev.get("revenue"):
        growth = (last["revenue"] / prev["revenue"] - 1) * 100
    op_margin = h["op_margin_ttm"]
    if op_margin is not None and abs(op_margin) > MAX_ABS_MARGIN:      # 영업이익이 매출보다 큰 지주사·투자회사 등은 비교 대상 지표가 아니다
        op_margin = None
    return {"code": rep["code"], "name": rep["name"], "market_cap": h["market_cap_all"] or h["market_cap"], "per": h["per"], "per_status": h["per_status"],
            "pbr": h["pbr"], "psr": h["psr"], "op_margin": op_margin, "roe": last.get("roe"), "rev_growth": growth,
            "debt_ratio": last.get("debt_ratio"), "currency": rep["currency"], "financial_like": rep["financial_like"], "annual_year": rep["annual_year"]}


def median_of(values):
    vals = [v for v in values if v is not None]
    return statistics.median(vals) if len(vals) >= MIN_FOR_MEDIAN else None


def summarize(subject, peers):
    """비교 종목 중앙값과, 이 종목이 그 중앙값보다 얼마나 높거나 낮은지. 원화 보고 종목만 비교한다(외화 보고 회사는 단위가 달라 제외)."""
    ok = [p for p in peers if p.get("currency") == "KRW"]
    med = {}
    for key, _, _ in METRICS:
        vals = [p.get(key) for p in ok]
        if key in ("per", "pbr", "psr"):
            vals = [v for v in vals if v is not None and v > 0]
        med[key] = median_of(vals)
    gap = {}
    for key in ("per", "pbr", "psr", "op_margin", "roe", "rev_growth", "debt_ratio"):
        s, m = subject.get(key), med.get(key)
        if s is None or m is None:
            gap[key] = None
        elif key in ("per", "pbr", "psr"):
            gap[key] = (s / m - 1) * 100 if m > 0 else None         # 배수는 비율 차이(%)
        else:
            gap[key] = s - m                                         # 이미 %인 지표는 %p 차이
    return {"median": med, "gap": gap, "n": len(ok), "excluded": [p["name"] for p in peers if p.get("currency") != "KRW"]}
