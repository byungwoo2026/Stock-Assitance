# -*- coding: utf-8 -*-
"""최근 분기 실적 수집·정리 (streamlit 비의존 — 테스트 가능).

'AI 심층 투자 분석 리포트'가 연간(최근 결산연도) 수치만 보고 쓰여서, 분기 실적이 나와도 리포트가
몇 달 전 숫자로 남던 문제를 막기 위한 모듈이다. 네이버 모바일 API(finance/quarter)에서
최근 분기들의 실적을 가져와 "실적이 확정된 가장 최신 분기"를 고르고, 전년 동기·전 분기 대비 변화를
**코드가** 계산해 프롬프트용 텍스트로 만든다(LLM이 직접 계산하거나 지어내지 않도록).

네이버 분기 표의 성격(121600 나노신소재 2026.09 분기로 확인):
- 매출액·영업이익·당기순이익은 억원, 영업이익률·순이익률은 그 분기 한 분기만의 값.
- ROE는 최근 4분기 순이익 합산 기준(2025.12 분기 값 0.66 = 2025 연간 ROE 0.66, 한 분기만의 ROE였다면 훨씬 작음).
- 마지막 열은 아직 발표 전 분기의 컨센서스(isConsensus='Y') → 실적으로 쓰지 않는다.
"""
import calendar
import datetime as dt

QUARTER_URL = "https://m.stock.naver.com/api/stock/{code}/finance/quarter"

# (내부 이름, 네이버 행 제목)
_ROWS = [
    ("revenue", "매출액"),
    ("op_income", "영업이익"),
    ("net_income", "당기순이익"),
    ("op_margin", "영업이익률"),
    ("net_margin", "순이익률"),
    ("roe", "ROE"),
    ("debt_ratio", "부채비율"),
    ("quick_ratio", "당좌비율"),
]

MAX_QUARTERS = 5          # 전년 동기 비교가 되려면 5개 분기(현재 + 4분기 전)가 필요
REPORT_GRACE_DAYS = 5     # 공시 기한 직후 반영 지연 여유


def _to_float(raw):
    if raw is None:
        return None
    text = str(raw).replace(",", "").replace("%", "").strip()
    if not text or text == "-":
        return None
    try:
        return float(text)
    except ValueError:
        return None


def quarter_end(key):
    """'202609' -> date(2026, 9, 30)"""
    year, month = int(key[:4]), int(key[4:6])
    return dt.date(year, month, calendar.monthrange(year, month)[1])


def _next_quarter_end(key):
    year, month = int(key[:4]), int(key[4:6])
    total = year * 12 + (month - 1) + 3
    ny, nm0 = divmod(total, 12)
    nm = nm0 + 1
    return dt.date(ny, nm, calendar.monthrange(ny, nm)[1])


def period_label(key):
    """'202609' -> '2026.09'"""
    return f"{key[:4]}.{key[4:6]}"


def staleness_note(latest_key, today):
    """더 최근 분기 실적이 이미 나왔어야 하는데 데이터에 없으면 안내 문구, 아니면 None.
    분기 보고서 제출 기한은 분기 종료 후 45일(사업보고서는 90일)이다."""
    nxt = _next_quarter_end(latest_key)
    deadline = nxt + dt.timedelta(days=90 if nxt.month == 12 else 45)
    if today > deadline + dt.timedelta(days=REPORT_GRACE_DAYS):
        return (f"{period_label(nxt.strftime('%Y%m'))} 분기 실적은 공시 기한({deadline})이 지났는데 "
                f"데이터에 아직 반영되지 않았습니다. 아래 최신 분기가 마지막으로 확인된 실적입니다.")
    return None


def parse_quarterly(finance_info, today):
    """네이버 financeInfo(JSON) → 최근 확정 분기들. 쓸 수 있는 분기가 없으면 None.

    반환: {"quarters": [오래된 순 dict...], "latest": 최신 dict, "days_since_quarter_end": int, "stale_note": str|None}
    각 분기 dict: key, label, revenue, op_income, net_income, op_margin, net_margin, roe, debt_ratio, quick_ratio (없으면 None)
    """
    if not finance_info:
        return None
    titles = finance_info.get("trTitleList") or []
    rows = {r.get("title"): (r.get("columns") or {}) for r in (finance_info.get("rowList") or [])}

    def cell(row_title, key):
        return _to_float((rows.get(row_title) or {}).get(key, {}).get("value"))

    quarters = []
    for t in sorted(titles, key=lambda t: t.get("key", "")):
        key = t.get("key")
        if not key or t.get("isConsensus") == "Y":
            continue
        q = {"key": key, "label": period_label(key)}
        for name, row_title in _ROWS:
            q[name] = cell(row_title, key)
        # 매출·영업이익이 모두 비어 있으면 아직 확정 실적이 아님
        if q["revenue"] is None and q["op_income"] is None:
            continue
        quarters.append(q)

    if not quarters:
        return None
    quarters = quarters[-MAX_QUARTERS:]
    latest = quarters[-1]
    return {
        "quarters": quarters,
        "latest": latest,
        "days_since_quarter_end": (today - quarter_end(latest["key"])).days,
        "stale_note": staleness_note(latest["key"], today),
    }


def fetch_quarterly(session, code, today, timeout=5):
    """네이버에서 분기 재무를 받아 parse_quarterly 결과를 돌려준다. 실패하면 None."""
    try:
        res = session.get(QUARTER_URL.format(code=code), timeout=timeout)
        if res.status_code != 200:
            return None
        return parse_quarterly(res.json().get("financeInfo"), today)
    except Exception:
        return None


# ---------- 변화율 계산(코드가 계산해서 문장으로 넘김) ----------
def _pct_change_text(cur, prev):
    """금액(매출·이익)의 변화율. 기준이 0 이하이면 %가 의미 없으므로 흑자/적자 전환으로 표기."""
    if cur is None or prev is None:
        return "비교 불가"
    if prev > 0:
        return f"{(cur - prev) / prev * 100:+.1f}%"
    if prev < 0:
        if cur > 0:
            return "적자→흑자 전환"
        return "적자 지속(적자 폭 축소)" if cur > prev else "적자 지속(적자 폭 확대)"
    return "비교 불가(기준 0)"


def _pp_change_text(cur, prev):
    if cur is None or prev is None:
        return "비교 불가"
    return f"{cur - prev:+.2f}%p"


def shift_key(key, months):
    """'202609' 에서 months개월 이동한 키 ('202606' = -3)"""
    year, month = int(key[:4]), int(key[4:6])
    ty, tm0 = divmod(year * 12 + (month - 1) + months, 12)
    return f"{ty:04d}{tm0 + 1:02d}"


def _quarter_back(quarters, latest_key, years=0, quarters_back=0):
    """latest 기준 N분기 전 데이터 (없으면 None). years*4+quarters_back 분기 전."""
    target = shift_key(latest_key, -3 * (years * 4 + quarters_back))
    return next((q for q in quarters if q["key"] == target), None)


# ---------- 종합점수용 값 ----------
def ttm_op_margin(parsed):
    """최근 4개 '연속' 분기의 영업이익 합 ÷ 매출 합(%). 4분기가 연속으로 없거나 값이 비면 None."""
    quarters = parsed["quarters"]
    if len(quarters) < 4:
        return None
    last4 = quarters[-4:]
    latest_key = last4[-1]["key"]
    if [q["key"] for q in last4] != [shift_key(latest_key, -3 * i) for i in (3, 2, 1, 0)]:
        return None
    if any(q["revenue"] is None or q["op_income"] is None for q in last4):
        return None
    revenue = sum(q["revenue"] for q in last4)
    if revenue <= 0:
        return None
    return sum(q["op_income"] for q in last4) / revenue * 100


def resolve_profitability(annual_op_margin, annual_roe, parsed):
    """종합점수의 경영지표(영업이익률·ROE)에 쓸 값과 그 기준 설명.

    최근 분기 실적이 있으면
    - 영업이익률 = 최근 4분기 합산 (분기 하나만 쓰면 계절성이 점수를 흔들고, 점수 문턱 10%·5%는 연간 규모 기준이라)
      4분기가 연속으로 없으면 최근 분기 값, 그것도 없으면 연간 값
    - ROE = 최근 분기 표의 값 (네이버가 최근 4분기 순이익 합산 기준으로 계산한 값). 비었으면 연간 값
    분기 실적이 없으면(조회 실패) 연간 값 그대로 — 호출한 화면이 기준을 밝힌다."""
    annual = "최근 결산연도(연간)"
    out = {"op_margin": annual_op_margin, "roe": annual_roe, "op_basis": annual, "roe_basis": annual}
    if not parsed:
        return out
    latest = parsed["latest"]
    ttm = ttm_op_margin(parsed)
    if ttm is not None:
        out["op_margin"], out["op_basis"] = ttm, f"최근 4분기 합산(~{latest['label']})"
    elif latest["op_margin"] is not None:
        out["op_margin"], out["op_basis"] = latest["op_margin"], f"최근 분기({latest['label']})"
    if latest["roe"] is not None:
        out["roe"], out["roe_basis"] = latest["roe"], f"{latest['label']} 분기 기준(최근 4분기 합산)"
    return out


def change_lines(parsed):
    """최신 분기의 전년 동기·전 분기 대비 변화. 비교 대상이 없으면 그 줄은 생략."""
    quarters, latest = parsed["quarters"], parsed["latest"]
    out = []
    for title, prev in (
        ("전년 동기 대비", _quarter_back(quarters, latest["key"], years=1)),
        ("전 분기 대비", _quarter_back(quarters, latest["key"], quarters_back=1)),
    ):
        if not prev:
            continue
        out.append(
            f"- {title}({prev['label']} → {latest['label']}): "
            f"매출액 {_pct_change_text(latest['revenue'], prev['revenue'])}, "
            f"영업이익 {_pct_change_text(latest['op_income'], prev['op_income'])}, "
            f"당기순이익 {_pct_change_text(latest['net_income'], prev['net_income'])}, "
            f"영업이익률 {_pp_change_text(latest['op_margin'], prev['op_margin'])}, "
            f"부채비율 {_pp_change_text(latest['debt_ratio'], prev['debt_ratio'])}"
        )
    return out


# ---------- 프롬프트·화면용 텍스트 ----------
def _num(v, suffix="", digits=2, none="-"):
    if v is None:
        return none
    if digits == 0:
        return f"{v:,.0f}{suffix}"
    return f"{v:,.{digits}f}{suffix}"


def quarter_table_rows(parsed):
    """화면 표용: [{'분기':..., '매출액(억)':..., ...}] (오래된 순)"""
    return [
        {
            "분기": q["label"],
            "매출액(억원)": _num(q["revenue"], digits=0),
            "영업이익(억원)": _num(q["op_income"], digits=0),
            "당기순이익(억원)": _num(q["net_income"], digits=0),
            "영업이익률(%)": _num(q["op_margin"]),
            "순이익률(%)": _num(q["net_margin"]),
            "ROE(%·4분기합산)": _num(q["roe"]),
            "부채비율(%)": _num(q["debt_ratio"]),
            "당좌비율(%)": _num(q["quick_ratio"]),
        }
        for q in parsed["quarters"]
    ]


def format_prompt_block(parsed):
    """LLM 프롬프트에 그대로 넣을 최근 분기 실적 블록."""
    latest = parsed["latest"]
    lines = [
        "[최근 분기 실적 — 출처: 네이버 금융 분기 재무, 매출·이익 단위 억원, 오래된 분기 → 최신 분기 순]",
        f"- 가장 최근 확정 분기: {latest['label']} (분기 종료 후 {parsed['days_since_quarter_end']}일 경과)",
    ]
    if parsed.get("stale_note"):
        lines.append(f"- ⚠ {parsed['stale_note']}")
    lines.append("분기 | 매출액 | 영업이익 | 당기순이익 | 영업이익률(%) | 순이익률(%) | ROE(%,최근4분기합산) | 부채비율(%) | 당좌비율(%)")
    for q in parsed["quarters"]:
        lines.append(
            f"{q['label']} | {_num(q['revenue'], digits=0)} | {_num(q['op_income'], digits=0)} | "
            f"{_num(q['net_income'], digits=0)} | {_num(q['op_margin'])} | {_num(q['net_margin'])} | "
            f"{_num(q['roe'])} | {_num(q['debt_ratio'])} | {_num(q['quick_ratio'])}"
        )
    changes = change_lines(parsed)
    if changes:
        lines.append("[최신 분기 변화 — 코드가 계산한 값, 그대로 인용할 것]")
        lines.extend(changes)
    return "\n".join(lines)


def format_multiple(value, none_text):
    """PER·PBR 같은 배수. 0 이하·결측은 '산출 불가'로(0.0배로 적으면 AI가 '0배'로 오해한다)."""
    if value is None or value <= 0:
        return none_text
    return f"{value:.2f}배"
