# -*- coding: utf-8 -*-
"""
company_report.py — 종목 분석 보고서 1단계(사실만). DART 숫자로 헤더 지표·5개년 재무·현금흐름·주주·공시·밸류에이션 재료를 만든다. streamlit 비의존.

원칙
- 모든 숫자는 이 모듈의 한 함수에서만 계산한다(같은 지표를 화면마다 다시 계산하지 않는다). 화면은 build_report()가 준 dict만 그린다.
- LLM이 만든 숫자·전망·종합 점수는 넣지 않는다. 출처가 DART가 아닌 값(종가·네이버 연도별 PER)은 이름에 출처를 밝힌다.
- 빠진 데이터는 0이나 추정값으로 메우지 않고 None으로 두고 report["notes"]에 이유를 남긴다.

계산 정의 (화면 하단 '계산 기준'에 그대로 보여준다)
- 후행 12개월(TTM) = 직전 사업연도 + 올해 누적 − 전년 동기 누적. 아직 올해 분기·반기 보고서가 없으면 직전 사업연도 값.
- 순이익은 지배기업 소유주 몫(없으면 당기순이익 전체). 자본도 같은 기준.
- 시가총액 = 종가 × 최신 보고서의 보통주 발행주식수(자기주식 포함, 거래소 방식). 우선주가 있으면 배수 계산에는 우선주를 보통주 가격으로 환산해 합친다.
- PER = 시가총액 ÷ TTM 순이익, PBR = 시가총액 ÷ 최신 자본, PSR = 시가총액 ÷ TTM 매출.
- ROE = 지배주주 순이익 ÷ 평균 지배주주 자본(전년 값이 없으면 기말), 부채비율 = 부채총계 ÷ 자본총계.
- FCF = 영업활동현금흐름 − (유형자산 취득 + 무형자산 취득).
"""
import datetime as dt
import re
import statistics
from concurrent.futures import ThreadPoolExecutor

import dart_core
import segment_mix

DART_VIEW = "https://dart.fss.or.kr/dsaf001/main.do?rcpNo="
INTERIM_CODES = (("11014", "3분기"), ("11012", "반기"), ("11013", "1분기"))    # 최근 것부터 찾는다
YEARS_SHOWN = 5
FILING_DAYS = 90
FILING_LIMIT = 60
NOISE_TAG = "임원·주요주주 소유보고"


ERROR_LABELS = {"mix": "매출 구성", "old": "과거 연도 재무", "all_a": "전체 재무제표", "all_interim": "분기 전체 재무제표",
                "shares": "주식수", "holders": "최대주주", "treasury": "자기주식", "filings": "공시 목록"}


class NoDartData(Exception):
    """DART에 재무 데이터가 없는 종목(ETF·신규상장 등). 메시지를 화면에 그대로 보여준다."""


# --- 숫자·이름 정규화 ----------------------------------------------------------------------------
def to_num(v):
    """'1,234'·'-1234'·'12.5' → 숫자. 빈 값·'-'·변환 불가는 None."""
    if v is None:
        return None
    s = str(v).replace(",", "").strip()
    if s in ("", "-", "–"):
        return None
    try:
        return int(s) if re.fullmatch(r"-?\d+", s) else float(s)
    except ValueError:
        return None


def norm_name(s):
    """계정명 비교용: 공백과 '(손실)' 같은 부호 꼬리표를 지운다."""
    return re.sub(r"\s|\(손실\)|\(이익\)|\(손익\)", "", s or "")


def first_num(row, fields):
    for f in fields:
        v = to_num(row.get(f))
        if v is not None:
            return v
    return None


def fmt_won(v):
    """원 → '1,178조'·'3.01조'·'4,321억'·'5,000만'. None은 '—'."""
    if v is None:
        return "—"
    sign, a = ("-" if v < 0 else ""), abs(v)
    if a >= 1e14:
        return f"{sign}{a / 1e12:,.0f}조"
    if a >= 1e13:
        return f"{sign}{a / 1e12:,.1f}조"
    if a >= 1e12:
        return f"{sign}{a / 1e12:,.2f}조"
    if a >= 1e8:
        return f"{sign}{a / 1e8:,.0f}억"
    return f"{sign}{a / 1e4:,.0f}만"


def fmt_amount(v, currency="KRW"):
    """원화는 조·억 단위, 외화 보고 회사는 'USD 276.3M'처럼 통화를 붙인다."""
    if v is None or currency in (None, "KRW"):
        return fmt_won(v)
    sign, a = ("-" if v < 0 else ""), abs(v)
    for div, unit in ((1e9, "B"), (1e6, "M"), (1e3, "K")):
        if a >= div:
            return f"{sign}{currency} {a / div:,.1f}{unit}"
    return f"{sign}{currency} {a:,.0f}"


def fmt_mult(v, digits=1):
    if v is None:
        return "—"
    return ">999배" if v > 999 else f"{v:,.{digits}f}배"


def fmt_pct(v, digits=1, signed=False):
    if v is None:
        return "—"
    return f"{v:+,.{digits}f}%" if signed else f"{v:,.{digits}f}%"


# --- 주요계정(fnlttSinglAcnt): 계정명으로 찾는다. DART가 회사별 표기를 맞춰 준 표라 회사 간 비교가 쉽다 -------------------
MAIN = {
    "revenue": (("IS", "CIS"), ("매출액", "수익(매출액)", "영업수익", "매출", "수익")),
    "op": (("IS", "CIS"), ("영업이익", "영업손익")),
    "ni": (("IS", "CIS"), ("당기순이익", "분기순이익", "반기순이익", "당기순손익", "연결당기순이익")),
    "assets": (("BS",), ("자산총계",)),
    "liab": (("BS",), ("부채총계",)),
    "equity": (("BS",), ("자본총계",)),
}


def main_get(rows, fs, item, field="thstrm_amount"):
    sjs, names = MAIN[item]
    cand = [r for r in rows if r.get("fs_div") == fs and r.get("sj_div") in sjs]
    for nm in names:
        for r in cand:
            if norm_name(r.get("account_nm")) == nm:
                v = to_num(r.get(field))
                if v is not None:
                    return v
    return None


def main_cum(rows, fs, item):
    """분기·반기 보고서의 올해 누적과 전년 동기 누적. 1분기 보고서는 누적 칸이 없어 분기 금액이 곧 누적이다."""
    sjs, names = MAIN[item]
    for nm in names:
        for r in rows:
            if r.get("fs_div") == fs and r.get("sj_div") in sjs and norm_name(r.get("account_nm")) == nm:
                cur = first_num(r, ("thstrm_add_amount", "thstrm_amount"))
                prev = first_num(r, ("frmtrm_add_amount", "frmtrm_amount", "frmtrm_q_amount"))
                if cur is not None:
                    return cur, prev
    return None, None


def pick_fs(rows):
    """연결(CFS)이 있으면 연결, 없으면 별도(OFS). 둘 다 없으면 None."""
    for fs in ("CFS", "OFS"):
        if any(r.get("fs_div") == fs and norm_name(r.get("account_nm")) == "자산총계" and to_num(r.get("thstrm_amount")) is not None for r in rows):
            return fs
    return None


# --- 전체 재무제표(fnlttSinglAcntAll): 계정ID로 찾는다. 현금흐름·지배주주 몫은 여기에만 있다 ---------------------------------
OWNER_NI_ID = "ifrs-full_ProfitLossAttributableToOwnersOfParent"
OWNER_EQ_ID = "ifrs-full_EquityAttributableToOwnersOfParent"
OCF_ID = "ifrs-full_CashFlowsFromUsedInOperatingActivities"


def all_by_id(rows, id_part, sjs, field="thstrm_amount", exact=True):
    for r in rows:
        aid = r.get("account_id") or ""
        if r.get("sj_div") in sjs and (aid == id_part if exact else id_part in aid):
            v = to_num(r.get(field))
            if v is not None:
                return v
    return None


def all_by_name(rows, sjs, pattern, field="thstrm_amount", exclude=None):
    for r in rows:
        nm = re.sub(r"\s", "", r.get("account_nm") or "")
        if r.get("sj_div") in sjs and re.search(pattern, nm) and not (exclude and re.search(exclude, nm)):
            v = to_num(r.get(field))
            if v is not None:
                return v
    return None


ALL_FALLBACK = {      # 항목 → (재무제표 구분, 표준 계정ID, 이름 정규식)
    "revenue": (("IS", "CIS"), ("ifrs-full_Revenue",), r"^(매출액|수익\(매출액\)|영업수익)$"),
    "op": (("IS", "CIS"), ("dart_OperatingIncomeLoss",), r"^영업(이익|손실|손익)(\(손실\))?$"),
    "ni": (("IS", "CIS"), ("ifrs-full_ProfitLoss",), r"^(당기순이익|당기순손익|반기순이익|분기순이익)(\(손실\))?$"),
    "assets": (("BS",), ("ifrs-full_Assets",), r"^자산총계$"),
    "liab": (("BS",), ("ifrs-full_Liabilities",), r"^부채총계$"),
    "equity": (("BS",), ("ifrs-full_Equity",), r"^자본총계$"),
}


def all_get(rows, item, field="thstrm_amount"):
    sjs, ids, pat = ALL_FALLBACK[item]
    for aid in ids:
        v = all_by_id(rows, aid, sjs, field)
        if v is not None:
            return v
    return all_by_name(rows, sjs, pat, field)


def cf_ocf(rows, field="thstrm_amount"):
    v = all_by_id(rows, OCF_ID, ("CF",), field)
    if v is None:
        v = all_by_name(rows, ("CF",), r"^영업활동.*(현금흐름|순현금)", field, exclude=r"조정|자산|부채|기타|이자|법인세|배당|증감")
    return v


def cf_capex(rows, field="thstrm_amount"):
    """유형자산 취득 + 무형자산 취득(둘 다 지출이라 절댓값). 둘 다 못 찾으면 None."""
    ppe = all_by_id(rows, "PurchaseOfPropertyPlantAndEquipment", ("CF",), field, exact=False)
    if ppe is None:
        ppe = all_by_name(rows, ("CF",), r"^유형자산(의)?(취득|증가|투자)$", field)
    intang = all_by_id(rows, "PurchaseOfIntangibleAssets", ("CF",), field, exact=False)
    if intang is None:
        intang = all_by_name(rows, ("CF",), r"^(영업권이외의)?무형자산(의)?(취득|증가)$", field)
    parts = [abs(x) for x in (ppe, intang) if x is not None]
    return sum(parts) if parts else None


def owner_ni(rows, field="thstrm_amount"):
    return all_by_id(rows, OWNER_NI_ID, ("IS", "CIS"), field)


def owner_equity(rows, field="thstrm_amount"):
    return all_by_id(rows, OWNER_EQ_ID, ("BS",), field)


def owner_ni_cum(rows):
    for r in rows:
        if r.get("account_id") == OWNER_NI_ID and r.get("sj_div") in ("IS", "CIS"):
            cur = first_num(r, ("thstrm_add_amount", "thstrm_amount"))
            if cur is not None:
                return cur, first_num(r, ("frmtrm_add_amount", "frmtrm_amount", "frmtrm_q_amount"))
    return None, None


# --- 주식수·주주·자기주식 ------------------------------------------------------------------------
def share_class(label):
    """주식총수 표의 구분명 → 'common'|'pref'|None. 회사마다 '보통주'·'의결권 있는 주식(보통주)'·'의결권 있는 주식'(줄바꿈 포함)처럼 적는다."""
    t = re.sub(r"\s", "", label or "")
    if "우선" in t or "종류" in t or "의결권없는" in t:
        return "pref"
    if "보통" in t or "의결권있는" in t:
        return "common"
    return None


def parse_shares(rows):
    """주식의 총수 현황 → 보통주·우선주 발행주식수와 자기주식수. 숫자가 없는 행('-')은 None."""
    out = {"common": None, "pref": None, "treasury_common": None, "treasury_pref": None, "date": None}
    for r in rows:
        out["date"] = out["date"] or r.get("stlm_dt")
        kind = share_class(r.get("se"))
        if kind == "common" and out["common"] is None:
            out["common"], out["treasury_common"] = to_num(r.get("istc_totqy")), to_num(r.get("tesstk_co"))
        elif kind == "pref" and out["pref"] is None:
            out["pref"], out["treasury_pref"] = to_num(r.get("istc_totqy")), to_num(r.get("tesstk_co"))
    return out


TOTAL_NAMES = ("계", "합계", "소계")


def parse_holders(rows):
    """최대주주 및 특수관계인(보통주). 합계 행이 있으면 그 값, 없으면 행 합산."""
    common = [r for r in rows if (r.get("stock_knd") or "").startswith("보통")]
    people, total = [], None
    for r in common:
        ratio = to_num(r.get("trmend_posesn_stock_qota_rt"))
        item = {"name": re.sub(r"\s+", " ", r.get("nm") or "").strip(), "relate": re.sub(r"\s+", " ", r.get("relate") or "").strip(), "shares": to_num(r.get("trmend_posesn_stock_co")),
                "ratio": ratio, "prev_ratio": to_num(r.get("bsis_posesn_stock_qota_rt"))}
        if item["name"] in TOTAL_NAMES:
            total = item
        elif item["name"]:
            people.append(item)
    major = next((p for p in people if "최대주주" in p["relate"] and "본인" in p["relate"]), people[0] if people else None)
    summed = round(sum(p["ratio"] for p in people if p["ratio"] is not None), 2) if people else None
    group = total["ratio"] if total and total["ratio"] is not None else summed
    people.sort(key=lambda p: -(p["ratio"] or 0))
    return {"major": major, "group_ratio": group, "group_shares": total["shares"] if total else None, "people": people,
            "date": next((r.get("stlm_dt") for r in rows if r.get("stlm_dt")), None)}


def parse_treasury(rows, shares=None):
    """자기주식 보유·기간 중 취득·처분·소각(보통주). 총계 행이 없으면 주식총수 현황의 자기주식수만 쓴다."""
    row = next((r for r in rows if (r.get("stock_knd") or "").startswith("보통") and re.sub(r"\s", "", r.get("acqs_mth1") or "") == "총계"), None)
    z = lambda k: to_num(row.get(k)) or 0 if row else None
    out = {"begin": z("bsis_qy"), "acquired": z("change_qy_acqs"), "disposed": z("change_qy_dsps"), "cancelled": z("change_qy_incnr"),
           "end": z("trmend_qy"), "date": row.get("stlm_dt") if row else None}
    if out["end"] is None and shares and shares.get("treasury_common") is not None:
        out["end"] = shares["treasury_common"]
    issued = shares.get("common") if shares else None
    out["ratio"] = out["end"] / issued * 100 if out["end"] is not None and issued else None
    return out


# --- 계산 ----------------------------------------------------------------------------------------
def ttm_value(annual, cur, prev):
    return None if None in (annual, cur, prev) else annual + cur - prev


def _div(a, b):
    return a / b if a is not None and b not in (None, 0) else None


def _pct(a, b):
    r = _div(a, b)
    return None if r is None else r * 100


def annual_table(main_by_year, all_by_year, fs, last_year, n=YEARS_SHOWN):
    """5개년 표. main_by_year/all_by_year = {호출한 사업연도: 응답 행}. 한 호출은 그 해·전년·전전년 세 해를 준다."""
    cols = ("thstrm_amount", "frmtrm_amount", "bfefrmtrm_amount")

    def pull(src, fn):                          # {연도: 값}
        out = {}
        for call_year, rows in src.items():
            for i, col in enumerate(cols):
                out.setdefault(call_year - i, fn(rows, col))
        return out

    def basic(item):                            # 주요계정(이름)에 있으면 그것, 없으면 전체 재무제표(ID·이름)에서 보충
        main = pull(main_by_year, lambda r, c: main_get(r, fs, item, c))
        extra = pull(all_by_year, lambda r, c: all_get(r, item, c))
        return {y: (main.get(y) if main.get(y) is not None else extra.get(y)) for y in set(main) | set(extra)}

    d = {
        "revenue": basic("revenue"), "op": basic("op"), "ni_total": basic("ni"),
        "assets": basic("assets"), "liab": basic("liab"), "equity_total": basic("equity"),
        "ni_owner": pull(all_by_year, lambda r, c: owner_ni(r, c)),
        "equity_owner": pull(all_by_year, lambda r, c: owner_equity(r, c)),
        "ocf": pull(all_by_year, lambda r, c: cf_ocf(r, c)),
        "capex": pull(all_by_year, lambda r, c: cf_capex(r, c)),
    }
    years = list(range(last_year - n + 1, last_year + 1))
    g = lambda k, y: d[k].get(y)
    # 지배주주 몫이 모든 해에 있으면 그 기준, 하나라도 없으면 전체 기준으로 통일(해마다 기준이 섞이지 않게)
    live = [y for y in years if g("ni_total", y) is not None or g("equity_total", y) is not None]      # 상장 전·미조회로 통째로 빈 해는 기준 판단에서 뺀다
    owner_ok = bool(live) and all(g("ni_owner", y) is not None and g("equity_owner", y) is not None for y in live)
    rows = []
    for y in years:
        ni = g("ni_owner", y) if owner_ok else g("ni_total", y)
        eq = g("equity_owner", y) if owner_ok else g("equity_total", y)
        eq_prev = (g("equity_owner", y - 1) if owner_ok else g("equity_total", y - 1))
        avg_eq = (eq + eq_prev) / 2 if eq is not None and eq_prev is not None else eq
        ocf, capex = g("ocf", y), g("capex", y)
        rev = g("revenue", y)
        rows.append({
            "year": y, "revenue": rev, "op": g("op", y), "op_margin": _pct(g("op", y), rev),
            "ni": ni, "assets": g("assets", y), "liab": g("liab", y), "equity": g("equity_total", y),
            "debt_ratio": _pct(g("liab", y), g("equity_total", y)),
            "roe": _pct(ni, avg_eq) if avg_eq and avg_eq > 0 else None,
            "ocf": ocf, "capex": capex, "fcf": ocf - capex if ocf is not None and capex is not None else None,
        })
    return {"rows": rows, "ni_basis": "지배주주" if owner_ok else "전체"}


def header_metrics(price, shares, ttm, equity_latest):
    """시가총액·PER·PBR·PSR. 이익이 0 이하면 배수 대신 상태 문구를 낸다."""
    common, pref = shares.get("common"), shares.get("pref") or 0
    out = {"price": price, "market_cap": None, "market_cap_all": None, "pref_shares": pref or None,
           "per": None, "pbr": None, "psr": None, "per_status": "na", "pbr_status": "na"}
    if not price or not common:
        return out
    out["market_cap"] = price * common
    cap = out["market_cap_all"] = price * (common + pref)
    ni, rev = ttm.get("ni"), ttm.get("revenue")
    if ni is not None:
        out["per_status"], out["per"] = ("ok", cap / ni) if ni > 0 else ("loss", None)
    if equity_latest is not None:
        out["pbr_status"], out["pbr"] = ("ok", cap / equity_latest) if equity_latest > 0 else ("neg", None)
    out["psr"] = _div(cap, rev) if rev and rev > 0 else None
    return out


# --- 밸류에이션 시뮬레이터 ----------------------------------------------------------------------
def parse_naver_annual(fin_json):
    """네이버 finance/annual 응답 → 연도별 PER [{year, per, consensus}] (PER 칸이 숫자인 연도만, 오래된 순)."""
    info = (fin_json or {}).get("financeInfo") or {}
    cons = {t["key"]: t.get("isConsensus") == "Y" for t in info.get("trTitleList", []) if t.get("key")}
    per_row = next((r for r in info.get("rowList", []) if r.get("title") == "PER"), None)
    out = []
    for key, cell in sorted(((per_row or {}).get("columns") or {}).items()):
        v = to_num((cell or {}).get("value"))
        if v is not None and key[:4].isdigit():
            out.append({"year": int(key[:4]), "per": v, "consensus": cons.get(key, False)})
    return out


def per_range(history):
    """실적 확정 연도의 양(+)의 PER만으로 낮음·중앙·높음. 2개 미만이면 None(근거 부족)."""
    vals = [h for h in history if not h["consensus"] and h["per"] > 0]
    if len(vals) < 2:
        return None
    per = [h["per"] for h in vals]
    return {"low": min(per), "mid": statistics.median(per), "high": max(per), "n": len(per), "years": [h["year"] for h in vals]}


def scenario(ni, market_cap_all, price, per):
    """순이익 × PER = 목표 시가총액. 목표 주가는 현재가 × (목표/현재 시총) — 주식수 기준과 무관하게 일치한다."""
    if not ni or ni <= 0 or not market_cap_all or not price or per is None:
        return None
    cap = ni * per
    return {"per": per, "cap": cap, "price": price * cap / market_cap_all, "upside": cap / market_cap_all - 1}


# --- 공시 ----------------------------------------------------------------------------------------
FILING_TAGS = (
    (r"임원.?주요주주특정증권", NOISE_TAG),
    (r"사업보고서|반기보고서|분기보고서", "정기보고서"), (r"영업\(잠정\)실적|매출액또는손익구조|연결재무제표기준영업", "실적"),
    (r"단일판매|공급계약", "수주·계약"), (r"자기주식|자사주", "자기주식"), (r"현금.?현물배당|배당", "배당"),
    (r"유상증자|무상증자|전환사채|신주인수권부사채|교환사채", "자금조달"), (r"감자", "감자"),
    (r"합병|분할|영업양수|영업양도|주식교환|주식이전", "구조변경"), (r"타법인주식|출자증권|유형자산", "투자·자산"),
    (r"최대주주|대량보유|임원.?주요주주|주식등의", "지분"), (r"조회공시|풍문|공정공시|불성실", "거래소"),
)


def tag_filing(title):
    t = re.sub(r"\s", "", title)
    return next((tag for pat, tag in FILING_TAGS if re.search(pat, t)), "기타")


def parse_filings(rows, today, risk_levels=None, days=FILING_DAYS, limit=FILING_LIMIT):
    """공시 목록 → 화면용. risk_levels = {접수번호: '경고'|'주의'} (risk_disclosure가 이미 가려낸 것)."""
    risk_levels = risk_levels or {}
    cutoff = today - dt.timedelta(days=days)
    out = []
    for r in rows:
        try:
            d = dt.datetime.strptime(r.get("rcept_dt", ""), "%Y%m%d").date()
        except ValueError:
            continue
        if d < cutoff or d > today:
            continue
        title = re.sub(r"\s+", " ", r.get("report_nm", "")).strip()
        rno = r.get("rcept_no", "")
        out.append({"date": d, "title": title, "filer": r.get("flr_nm", ""), "tag": tag_filing(title), "url": DART_VIEW + rno,
                    "risk": risk_levels.get(rno), "correction": title.startswith("[")})
    out.sort(key=lambda f: f["date"], reverse=True)
    return out[:limit]


# --- DART 조회 ----------------------------------------------------------------------------------
def _first_valid(periods, fetch, valid):
    """periods 순서대로 조회해 처음으로 유효한 결과 {year, rc, rows}. 모두 비면 None."""
    for year, rc in periods:
        rows = fetch(year, rc)
        if rows and valid(rows):
            return {"year": year, "rc": rc, "rows": rows}
    return None


def fetch_raw(code, today=None, light=False):
    """종목 하나의 DART 원자료. 필수(사업보고서 재무)가 없으면 NoDartData, 부수 항목 실패는 raw['errors']에 남기고 계속한다.
    light=True면 피어 비교용으로 최근 3개 사업연도·후행·주식수만 받는다(과거 연도·주주·자기주식·공시·매출 구성은 건너뜀 → 호출 수가 절반)."""
    today = today or dt.date.today()
    corp = dart_core.corp_codes().get(code)
    if not corp:
        raise NoDartData("DART 상장사 목록에 없는 종목입니다(ETF·ETN·리츠 등은 사업보고서 재무가 없습니다).")
    cc = corp["corp_code"]
    A, main_a = None, []
    for y in (today.year - 1, today.year - 2):
        rows = dart_core.main_accounts_cached(cc, y)
        if rows:
            A, main_a = y, rows
            break
    fs = pick_fs(main_a) if A else None
    if not fs:
        raise NoDartData("DART에서 최근 사업보고서 재무 데이터를 찾지 못했습니다(상장폐지·신규 상장·SPAC이거나 보고서 형식이 달라 조회되지 않는 종목).")

    interim = None
    for rc, label in INTERIM_CODES:
        rows = dart_core.main_accounts_cached(cc, A + 1, rc)
        if rows and any(r.get("fs_div") == fs for r in rows):
            interim = {"year": A + 1, "rc": rc, "label": label, "main": rows}
            break

    periods = ([(A + 1, interim["rc"])] if interim else []) + [(A, "11011")]
    keep = 3600                                        # 지난 사업연도 값은 거의 안 바뀐다 → 길게 캐시

    def older():
        """5개년 중 앞쪽 해를 채울 보고서: 3년 전 → 안 나오면 2년 전(금융사는 DART가 2023년 이전 호출을 비워 둔다. 2023년 보고서의 전기·전전기 열이 2022·2021년을 담고 있다)."""
        for y in (A - 3, A - 2):
            m = dart_core.main_accounts_cached(cc, y, ttl_hours=keep)
            if m:
                return {"year": y, "main": m, "all": dart_core.all_statements(cc, y, "11011", fs, ttl_hours=keep)}
        return None

    krw = not any(r.get("currency") not in (None, "", "KRW") for r in main_a)
    tasks = {
        "all_a": lambda: dart_core.all_statements(cc, A, "11011", fs),
        "shares": lambda: _first_valid(periods, lambda y, rc: dart_core.share_totals(cc, y, rc), lambda rows: parse_shares(rows)["common"] is not None),
    }
    if not light:
        tasks.update({
            "old": older,
            "holders": lambda: _first_valid(periods, lambda y, rc: dart_core.shareholders(cc, y, rc), bool),
            "treasury": lambda: _first_valid(periods, lambda y, rc: dart_core.treasury_stock(cc, y, rc), bool),
            "filings": lambda: dart_core.recent_filings(cc, (today - dt.timedelta(days=FILING_DAYS)).strftime("%Y%m%d"), today.strftime("%Y%m%d")),
            # 매출 구성: XBRL 주석, 없으면 본문 표. 본문 표의 합계 검증에는 원화 매출액만 쓴다(외화 보고 회사는 표를 쓰지 않음)
            "mix": lambda: segment_mix.fetch_mix(main_a[0]["rcept_no"], fs, main_get(main_a, fs, "revenue") if krw else None),
        })
    if interim:
        tasks["all_interim"] = lambda: dart_core.all_statements(cc, A + 1, interim["rc"], fs)
    raw = {"code": code, "name": corp["corp_name"], "corp_code": cc, "today": today, "annual_year": A, "fs": fs,
           "main_a": main_a, "interim": interim, "errors": {}, "old": None, "holders": None, "treasury": None, "filings": None, "mix": None, "all_interim": None}
    with ThreadPoolExecutor(max_workers=6) as ex:
        futures = {k: ex.submit(f) for k, f in tasks.items()}
        for k, fut in futures.items():
            try:
                raw[k] = fut.result()
            except Exception as e:                      # DartError는 키를 담지 않는다. 부수 항목 하나 때문에 보고서 전체를 버리지 않는다.
                raw[k], raw["errors"][k] = None, f"{type(e).__name__}: {str(e)[:120]}"
    return raw


# --- 보고서 조립 --------------------------------------------------------------------------------
def build_report(raw, price, price_date=None, naver_annual=None, risk_levels=None):
    """원자료 + 종가(+ 네이버 연도별 PER) → 화면용 dict. 네트워크를 쓰지 않는다."""
    notes = []
    A, fs, today = raw["annual_year"], raw["fs"], raw["today"]
    interim = raw.get("interim")
    main_by_year, all_by_year = {A: raw["main_a"]}, {}
    if raw.get("all_a"):
        all_by_year[A] = raw["all_a"]
    if raw.get("old"):
        main_by_year[raw["old"]["year"]] = raw["old"]["main"]
        if raw["old"]["all"]:
            all_by_year[raw["old"]["year"]] = raw["old"]["all"]
    for k, why in raw.get("errors", {}).items():
        notes.append(f"{ERROR_LABELS.get(k, k)} 조회 실패 — {why}")
    if not all_by_year:
        notes.append("전체 재무제표를 받지 못해 현금흐름(영업CF·CAPEX·FCF)과 지배주주 기준 값이 비어 있습니다.")

    currency = next((r["currency"] for r in raw["main_a"] if r.get("currency") not in (None, "", "KRW")), "KRW")
    if currency != "KRW":
        notes.append(f"재무제표가 {currency} 단위로 보고되는 회사입니다. 원화 주가와 환율 없이 비교할 수 없어 시가총액 대비 배수(PER·PBR·PSR)와 밸류에이션 시뮬레이터는 제공하지 않습니다.")
    annual = annual_table(main_by_year, all_by_year, fs, A)
    last = annual["rows"][-1]
    financial_like = all(r["revenue"] is None for r in annual["rows"])
    if financial_like:
        for r in annual["rows"]:
            r["fcf"] = None
        notes.append("매출액 계정이 없는 업종(은행·보험·증권 등)입니다. 영업현금흐름에 예금·대출 증감이 섞여 FCF는 의미가 없어 표시하지 않고, 매출 기반 지표(영업이익률·PSR)도 비웁니다.")
    empty_years = [r["year"] for r in annual["rows"] if all(r[k] is None for k in ("revenue", "op", "ni", "assets", "equity", "ocf"))]
    if not financial_like and any(r["revenue"] is None for r in annual["rows"] if r["year"] not in empty_years):
        notes.append("일부 연도의 매출액 계정을 찾지 못했습니다(보고서 형식이 달라 조회되지 않은 연도).")
    if empty_years:
        notes.append(f"{', '.join(map(str, empty_years))}년은 사업보고서 재무가 없어 비워 두었습니다(상장 전이거나 DART가 값을 돌려주지 않은 연도).")
    if annual["ni_basis"] == "전체":
        notes.append("지배·비지배 구분이 없어(별도재무제표이거나 항목 누락) 순이익·ROE는 당기순이익 전체 기준입니다.")

    # TTM
    ttm = {"revenue": last["revenue"], "op": last["op"], "ni": last["ni"], "label": f"{A} 사업연도", "is_ttm": False}
    equity_latest, equity_label = (
        (owner_equity(raw["all_a"]) if raw.get("all_a") and annual["ni_basis"] == "지배주주" else last["equity"]), f"{A} 사업연도 말")
    if interim:
        y, rc, lab = interim["year"], interim["rc"], interim["label"]
        cur_rev, prev_rev = main_cum(interim["main"], fs, "revenue")
        cur_op, prev_op = main_cum(interim["main"], fs, "op")
        if annual["ni_basis"] == "지배주주":              # 사업연도 값이 지배주주 몫이면 누적도 같은 기준만 쓴다(전체 기준과 섞지 않음)
            cur_ni, prev_ni = owner_ni_cum(raw["all_interim"]) if raw.get("all_interim") else (None, None)
        else:
            cur_ni, prev_ni = main_cum(interim["main"], fs, "ni")
        ni_ttm = ttm_value(last["ni"], cur_ni, prev_ni)
        if ni_ttm is not None:
            ttm = {"revenue": ttm_value(last["revenue"], cur_rev, prev_rev), "op": ttm_value(last["op"], cur_op, prev_op), "ni": ni_ttm,
                   "label": f"{A} 사업연도 + {y} {lab} 누적 − {y - 1} 동기 누적", "is_ttm": True}
            eq_now = (owner_equity(raw["all_interim"]) if annual["ni_basis"] == "지배주주" and raw.get("all_interim") else None)
            if eq_now is None:
                eq_now = main_get(interim["main"], fs, "equity")
            if eq_now is not None:
                equity_latest, equity_label = eq_now, f"{y} {lab} 말"
        else:
            notes.append(f"{y} {lab} 보고서는 있으나 누적 순이익을 읽지 못해 후행 12개월 대신 {A} 사업연도 값을 씁니다.")
    if equity_latest is None:
        equity_latest = last["equity"]

    shares_pack = raw.get("shares")
    shares = parse_shares(shares_pack["rows"]) if shares_pack else {"common": None, "pref": None, "treasury_common": None, "treasury_pref": None, "date": None}
    if not shares["common"]:
        notes.append("보통주 발행주식수를 읽지 못해 시가총액과 PER·PBR·PSR을 계산하지 못했습니다.")
    head = header_metrics(price if currency == "KRW" else None, shares, ttm, equity_latest)
    head["price"] = price                                  # 배수는 못 내도 원화 종가 자체는 그대로 보여준다
    head.update({"price_date": price_date, "shares_common": shares["common"], "shares_date": shares["date"], "ttm": ttm,
                 "equity": equity_latest, "equity_label": equity_label,
                 "per_fy": _div(head["market_cap_all"], last["ni"]) if last["ni"] and last["ni"] > 0 else None,
                 "op_margin_ttm": _pct(ttm["op"], ttm["revenue"]) if ttm["revenue"] else None})
    if head["pref_shares"]:
        head["pref_note"] = (f"우선주 {head['pref_shares']:,.0f}주가 별도 상장돼 있습니다. 시가총액은 보통주만, PER·PBR·PSR은 우선주를 보통주 가격으로 환산해 합친 "
                             f"{fmt_won(head['market_cap_all'])} 기준입니다.")

    holders = parse_holders(raw["holders"]["rows"]) if raw.get("holders") else None
    treasury = parse_treasury(raw["treasury"]["rows"], shares) if raw.get("treasury") else (parse_treasury([], shares) if shares["common"] else None)
    if holders:
        holders["period"] = _period_label(raw["holders"])
    if treasury:
        treasury["period"] = _period_label(raw.get("treasury"))

    # 밸류에이션 재료: 이익 기준은 TTM(없으면 사업연도), 배수 범위는 네이버 연도별 PER
    history = parse_naver_annual(naver_annual) if naver_annual else []
    rng = per_range(history)
    valuation = {"ni_base": ttm["ni"], "ni_label": ttm["label"], "market_cap_all": head["market_cap_all"], "price": price,
                 "per_now": head["per"], "history": history, "range": rng, "scenarios": []}
    if rng and ttm["ni"] and ttm["ni"] > 0 and head["market_cap_all"]:
        for lab, key in (("Bear", "low"), ("Base", "mid"), ("Bull", "high")):
            s = scenario(ttm["ni"], head["market_cap_all"], price, rng[key])
            if s:
                valuation["scenarios"].append({"label": lab, **s})

    filings = parse_filings(raw.get("filings") or [], today, risk_levels)
    return {"code": raw["code"], "name": raw["name"], "fs": "연결" if fs == "CFS" else "별도", "annual_year": A, "financial_like": financial_like, "currency": currency,
            "rcept_no": next((r.get("rcept_no") for r in raw["main_a"] if r.get("rcept_no")), None),      # 사업보고서 접수번호(3단계 서술의 원문·캐시 키)
            "interim": {k: interim[k] for k in ("year", "rc", "label")} if interim else None,
            "header": head, "annual": annual, "holders": holders, "treasury": treasury, "valuation": valuation, "mix": raw.get("mix"),
            "filings": filings, "notes": notes}


def _period_label(pack):
    if not pack:
        return None
    if pack["rc"] == "11011":
        return f"{pack['year']} 사업보고서"
    return f"{pack['year']} {dict(INTERIM_CODES)[pack['rc']]}보고서"
