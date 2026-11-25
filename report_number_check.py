# -*- coding: utf-8 -*-
"""AI 리포트 숫자 검증기 (streamlit 비의존 — 테스트 가능).

AI가 프롬프트에 없던 숫자(현금 1.2억, 부채비율 310%, 임의의 목표가…)를 지어내거나, 제공된 값으로 새로 계산해
쓰는 일이 실제로 있었다. 프롬프트의 '데이터 부분'에 나온 숫자를 허용 목록으로 삼아, 리포트에서
**단위가 붙었거나 소수점이 있는 숫자**가 그 목록에 없으면 그 자리에 ⚠️를 붙이고 목록으로 돌려준다.

검증하는 것: 그 숫자가 제공 데이터에 '존재하는지'(반올림·버림 한 자리 차이까지 허용).
검증하지 못하는 것: 존재하는 숫자를 엉뚱한 항목·기간에 붙인 경우(예: 분기 값을 연간 값처럼 서술), 방향(증가/감소) 오류.

검사에서 빼는 것(오탐 방지): 날짜·'2026.09' 같은 기간 표기, 목차 번호, 단위 없는 정수(RSI(14)·20일선 등),
'≤ 0.3'·'30 이하'처럼 기준을 말하는 숫자, 매수 비중·익절 비율 같은 전략 제안(%).
"""
import re
from decimal import Decimal, ROUND_DOWN, ROUND_HALF_UP

_DASHES = "\\-\u2010\u2011\u2012\u2013\u2014\u2212"   # 모델이 쓰는 여러 가지 하이픈·마이너스

# 기간·날짜 표기: 숫자로 읽히지 않게 지운다
_DATE = re.compile(
    rf"(?<![\d,.])(?:19|20)\d{{2}}\s?[.{_DASHES}/]\s?(?:0?[1-9]|1[0-2])(?:\s?[.{_DASHES}/]\s?\d{{1,2}})?(?!\d)"
    r"|(?<![\d,.])(?:19|20)\d{2}\s?년(?:\s?\d{1,2}\s?월)?(?:\s?\d{1,2}\s?일)?"
    r"|\d{1,2}\s?월\s?\d{1,2}\s?일"
)
# 줄 맨 앞 목차 번호 ('1. ', '4‑1. ', '2) ') 와 마크다운 소제목 번호 ('### 4.3 리스크')
_LIST_NO = re.compile(
    rf"(?m)^[ \t>*#|\-]*\**\d+(?:[.{_DASHES}]\d+)*[.)]\s"
    rf"|^[ \t>]*#+\s*\**\d+(?:[.{_DASHES}]\d+)*\.?(?=[\s*])"
)

_NUM = re.compile(
    rf"(?<![\w.,])(?P<sign>[+{_DASHES}]?)\s?"
    r"(?P<num>\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)"
    r"(?P<unit>\s?(?:%p|%|배(?!당)|억\s?원|억|조(?![건직합정사치금])|만\s?원|원(?![가금])))?"
)

_THRESHOLD_AFTER = re.compile(r"\s?(?:이하|이상|미만|초과|이내)")
_CMP_BEFORE = re.compile(r"[≤≥<>]\s?$")
_APPROX_BEFORE = re.compile(r"[≈~약]\s?$")
_APPROX_AFTER = re.compile(r"\s?(?:부근|근처|가량|내외|수준|안팎)")
# 매수 비중·익절 비율 같은 전략 제안(%)을 알아보는 말. 숫자 바로 뒤/앞에 있을 때만 본다
# ('부채 비중이 낮아져'처럼 멀리 있는 '비중'이 데이터 주장까지 면제해 주지 않도록).
_STRATEGY_AFTER = ("비중", "익절", "청산", "실현", "포지션", "자금")
_STRATEGY_BEFORE = ("분할", "포지션", "잔여", "예:")
APPROX_PRICE_TOLERANCE = 0.01   # '≈81,200원'처럼 반올림해 적은 가격은 사실과 1% 안이면 인정
APPROX_MIN_VALUE = Decimal(1000)


def _blank(text, pattern):
    """패턴에 걸린 부분을 같은 길이의 공백으로 바꿔 위치를 보존한 채 지운다."""
    return pattern.sub(lambda m: " " * len(m.group(0)), text)


def _prepare(text):
    return _blank(_blank(text, _DATE), _LIST_NO)


def _decimal(num_text):
    return Decimal(num_text.replace(",", ""))


def _decimals(num_text):
    return len(num_text.split(".")[1]) if "." in num_text else 0


def extract_facts(facts_text):
    """허용 숫자 목록(절댓값). 단위·소수점 유무와 상관없이 데이터에 적힌 숫자를 모두 모은다."""
    return sorted({_decimal(m.group("num")) for m in _NUM.finditer(_prepare(facts_text))})


def _matches(value, decimals, facts):
    """value(소수 decimals자리로 적힌 숫자)가 사실 중 하나의 반올림 또는 버림과 같으면 True."""
    q = Decimal(1).scaleb(-decimals)
    for fact in facts:
        if fact.quantize(q, rounding=ROUND_HALF_UP) == value or fact.quantize(q, rounding=ROUND_DOWN) == value:
            return True
    return False


def _is_exempt(text, match):
    unit = (match.group("unit") or "").strip()
    start = match.start()
    if _CMP_BEFORE.search(text[:start]) or _THRESHOLD_AFTER.match(text, match.end()):
        return True
    if unit == "%":   # 매수 비중·익절 비율 같은 전략 제안은 데이터 값이 아니다
        after = text[match.end(): match.end() + 12]
        before = text[max(0, start - 10): start]
        if any(w in after for w in _STRATEGY_AFTER) or any(w in before for w in _STRATEGY_BEFORE):
            return True
    return False


def _is_rounded_price(value, unit, text, match, facts):
    """'≈81,200원'·'약 147,000원'처럼 어림했다고 밝힌 큰 금액이 사실 중 하나와 1% 안이면 True."""
    if unit not in ("원", "억", "억원", "만원") or value < APPROX_MIN_VALUE:
        return False
    if not (_APPROX_BEFORE.search(text[:match.start()]) or _APPROX_AFTER.match(text, match.end())):
        return False
    return any(f > 0 and abs(value - f) / f <= Decimal(str(APPROX_PRICE_TOLERANCE)) for f in facts)


def _snippet(text, match, width=22):
    s = text[max(0, match.start() - width): match.end() + width]
    return re.sub(r"\s+", " ", s).strip()


def check_report(report_text, facts_text, mark="⚠️"):
    """리포트 숫자를 facts_text의 숫자와 대조한다.

    반환: {"annotated": 미확인 숫자 뒤에 mark를 붙인 리포트,
           "unverified": [{"token": '약 20 %' 같은 원문 숫자, "context": 앞뒤 문맥}...],
           "checked": 검사한 숫자 개수}"""
    facts = extract_facts(facts_text)
    prepared = _prepare(report_text)
    flagged, checked = [], 0
    for m in _NUM.finditer(prepared):
        num_text = m.group("num")
        if not m.group("unit") and "." not in num_text:
            continue                      # 단위 없는 정수는 검사하지 않는다
        if _is_exempt(prepared, m):
            continue
        checked += 1
        value = _decimal(num_text)
        if _matches(value, _decimals(num_text), facts):
            continue
        if _is_rounded_price(value, re.sub(r"\s", "", m.group("unit") or ""), prepared, m, facts):
            continue
        flagged.append(m)

    annotated = report_text
    for m in reversed(flagged):           # 뒤에서부터 끼워 넣어야 앞쪽 위치가 안 밀린다
        annotated = annotated[: m.end()] + mark + annotated[m.end():]

    unverified, seen = [], set()
    for m in flagged:
        token = report_text[m.start(): m.end()].strip()   # 지우기 전 원문(위치는 그대로 보존됨)
        if token in seen:
            continue
        seen.add(token)
        unverified.append({"token": token, "context": _snippet(report_text, m)})
    return {"annotated": annotated, "unverified": unverified, "checked": checked}
