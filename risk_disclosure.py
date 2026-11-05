# -*- coding: utf-8 -*-
"""
risk_disclosure.py — 종목의 최근 DART 위험 공시를 찾아 앱 경고용 데이터로 돌려준다. streamlit 비의존.

'검증 후 반영' 규칙을 통과한 유형만 담는다. 근거는 event_study.py(2019-01~2026-09 위험 공시 10,473건, 상장폐지 종목 포함):
  공시 뒤 1·5·20거래일 시장 대비 초과수익을 같은 날 다른 종목·이벤트 없는 종목과 견주고, 두 기간·두 시장에서 방향이 같은지 확인.
  경고(level "경고") : 감자 결정(평균·큰 하락 모두 통과) · 관리종목 지정/상장폐지 사유 발생(평균 통과)
  주의(level "주의") : 감사(검토)의견 부적정·거절·자본잠식 사실확인(평균은 평범, 큰 하락 쏠림만 통과 — 사후 기준)
                       · 관리종목 지정 우려 안내(5·20일 평균만 통과, 큰 상승도 많아 변동성 확대형)
  경고하지 않음      : 상장적격성 실질심사 안내(428건, 기준선과 구분되지 않음), 유상증자·횡령배임·CB/BW·자본잠식·불성실공시 등
관리종목 계열을 하위 유형으로 나눈 것은 결과를 본 뒤의 사후 구분이다 → 근거 수치도 하위 유형별로 따로 담았다(EVIDENCE).
등급·신호에는 반영하지 않는다. '이 공시 뒤 평균적으로 시장보다 약했다'는 과거 통계이지 개별 종목의 하락 예측이 아니다.
"""
import datetime as dt
import re

import dart_core
from collect_risk_events import B_RULES, I_RULES, classify, pull

WINDOW_DAYS = 30        # 검증은 공시 뒤 20거래일까지 — 달력으로 약 28일
LOOKBACK_DAYS = 180
CLUSTER_DAYS = 30       # 같은 유형이 30일 안에 되풀이되면 처음 한 건만(검증과 같은 규칙)
DART_VIEW = "https://dart.fss.or.kr/dsaf001/main.do?rcpNo="

EVIDENCE_NOTE = "공시 2019-01~2026-09, 상장폐지 종목 포함, 시장 지수 대비 초과수익(2026-11-03 산출)"
CUT = {"1": -0.05, "5": -0.10, "20": -0.20}                    # '큰 하락' 기준(event_study.TAIL)
BASE = {"1": {"tail": 0.0308, "tail_up": 0.0445}, "5": {"tail": 0.0436, "tail_up": 0.0599}, "20": {"tail": 0.0445, "tail_up": 0.0658}}   # 공시 없는 종목·날짜
# {k: (표본, 평균 초과수익, 큰 하락 비율, 큰 상승 비율)} — dart_cache/event_study.json · risk_flag_evidence.json 에서 옮김(test_risk_disclosure가 대조)
EVIDENCE = {
    "reduction": {"day0": -0.0117, "k": {"1": (492, -0.0507, 0.2988, 0.0813), "5": (492, -0.05, 0.248, 0.063), "20": (492, -0.0504, 0.1626, 0.0528)}},
    "delist": {"day0": -0.0113, "k": {"1": (954, -0.0269, 0.1405, 0.0283), "5": (954, -0.0376, 0.1541, 0.0283), "20": (954, -0.0601, 0.1216, 0.0325)}},
    "watch": {"day0": 0.0071, "k": {"1": (292, 0.0001, 0.1233, 0.0925), "5": (292, -0.0186, 0.1884, 0.089), "20": (292, -0.0301, 0.137, 0.0788)}},
    "audit": {"day0": -0.0083, "k": {"1": (501, -0.017, 0.1178, 0.008), "5": (501, -0.0114, 0.1118, 0.014), "20": (501, -0.0475, 0.0938, 0.014)}},
}
CATEGORIES = {
    "reduction": {"label": "감자 결정", "short": "감자 결정", "level": "경고", "k": "5",
                  "caveat": "감자의 방식·목적(결손 보전 등)은 구분하지 않았습니다."},
    "delist": {"label": "관리종목 지정·상장폐지 사유 발생", "short": "관리종목·상장폐지 사유", "level": "경고", "k": "20",
               "caveat": "거래정지 중에는 가격이 멈춰 수익률 0으로 잡히므로 실제 하락은 더 클 수 있습니다. 코스피 종목은 43건뿐이라 대부분 코스닥 근거입니다."},
    "watch": {"label": "관리종목 지정 우려 안내", "short": "관리종목 지정 우려", "level": "주의", "k": "20",
              "caveat": "큰 하락과 함께 큰 상승도 많아 방향보다 변동성 확대에 가깝습니다. 하위 유형을 결과를 본 뒤 나눈 것이라 근거가 약합니다."},
    "audit": {"label": "감사(검토)의견 부적정·거절 또는 자본잠식 사실확인", "short": "감사의견·자본잠식 확인", "level": "주의", "k": "5",
              "caveat": "평균·중앙값은 평범하고 일부 종목의 급락이 문제인 유형입니다. 큰 하락 기준은 평균을 본 뒤 추가한 사후 기준이라 새 데이터로 재확인이 필요합니다."},
}


def _as_date(d):
    return d.date() if hasattr(d, "date") else d


def categorize(typ, report):
    """collect_risk_events 가 붙인 유형(typ)과 보고서명으로 경고 범주를 정한다. 경고하지 않는 공시는 None."""
    nm = re.sub(r"\s", "", report)
    if typ == "감자":
        return "reduction"
    if typ == "감사의견":
        return "audit"
    if typ == "관리종목·상폐":
        if "우려" in nm:
            return "watch"
        if re.search(r"상장폐지사유|형식적상장폐지|투자유의안내", nm):
            return "delist"
        if "상장적격성" in nm:             # 실질심사 안내 — 거래정지 중인 종목이 대부분이고 기준선과 구분되지 않음
            return None
        return "delist"
    return None


def evidence_line(cat):
    """과거 같은 공시 뒤 주가 통계 한 줄. 숫자는 EVIDENCE에서만 온다."""
    k = CATEGORIES[cat]["k"]
    n, mean, tail, up = EVIDENCE[cat]["k"][k]
    cut = abs(CUT[k]) * 100
    mean_txt = f"{mean * 100:+.1f}%".replace("-", "−")
    return (f"과거 같은 공시 {n}건: 공시 뒤 {k}거래일 시장 대비 평균 {mean_txt}, −{cut:.0f}% 이하 급락 {tail * 100:.1f}%"
            f"(공시 없는 종목 {BASE[k]['tail'] * 100:.1f}%), +{cut:.0f}% 이상 급등 {up * 100:.1f}%")


def flags_from_rows(b_rows, i_rows, asof):
    """DART 목록 행(주요사항 B, 수시공시·시장조치 I)에서 경고 대상을 뽑는다. 최신순."""
    asof = _as_date(asof)
    found = []
    for e in classify(b_rows, B_RULES) + classify(i_rows, I_RULES):
        cat = categorize(e["type"], e["report"])
        if not cat:
            continue
        d = dt.datetime.strptime(e["dt"], "%Y%m%d").date()
        if d > asof:
            continue
        found.append({"cat": cat, "type": e["type"], "date": d, "report": re.sub(r"\s+", " ", e["report"]).strip(), "rno": e["rno"]})
    found.sort(key=lambda f: f["date"])
    kept, last = [], {}
    for f in found:                         # 검증과 같은 규칙: 같은 유형이 30일 안에 되풀이되면 처음 한 건만
        if f["type"] in last and (f["date"] - last[f["type"]]).days < CLUSTER_DAYS:
            continue
        last[f["type"]] = f["date"]
        kept.append(f)
    out = []
    for f in reversed(kept):
        days = (asof - f["date"]).days
        meta = CATEGORIES[f["cat"]]
        out.append({**f, "label": meta["label"], "short": meta["short"], "level": meta["level"], "days_ago": days, "in_window": days <= WINDOW_DAYS,
                    "evidence": evidence_line(f["cat"]), "caveat": meta["caveat"], "url": DART_VIEW + f["rno"]})
    return out


def fetch_flags(code, asof=None, lookback=LOOKBACK_DAYS):
    """종목코드의 최근 위험 공시. DART 키가 없거나 요청이 실패하면 dart_core.DartError."""
    asof = _as_date(asof) if asof else dt.date.today()
    corp = dart_core.corp_codes().get(code)
    if not corp:                            # DART 상장사 목록에 없는 종목(ETF 등)
        return []
    bgn, end = (asof - dt.timedelta(days=lookback)).strftime("%Y%m%d"), asof.strftime("%Y%m%d")
    cc = corp["corp_code"]
    b_rows = pull(bgn, end, pblntf_ty="B", corp_code=cc)
    i_rows = pull(bgn, end, pblntf_detail_ty="I001", corp_code=cc) + pull(bgn, end, pblntf_detail_ty="I003", corp_code=cc)
    return flags_from_rows(b_rows, i_rows, asof)


def short_text(flags):
    """표 한 칸용 요약. 검증 구간 안의 가장 위험한 공시 하나만(경고 > 주의, 최근 우선)."""
    recent = [f for f in flags if f["in_window"]]
    if not recent:
        return ""
    recent.sort(key=lambda f: (f["level"] != "경고", f["days_ago"]))
    f = recent[0]
    return f"{'🔴' if f['level'] == '경고' else '🟠'} {f['short']} ({f['days_ago']}일 전)"
