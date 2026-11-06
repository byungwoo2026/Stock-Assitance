# -*- coding: utf-8 -*-
"""
report_narrative.py — 종목 분석 보고서 3단계: LLM이 쓰는 서술(사업 요약·최근 변화·비판적 리스크·확인할 질문). streamlit 비의존.

원칙: LLM은 서술만 한다. 숫자는 코드가 한 곳에서 계산한 '사실(F)'과 공시 원문 발췌(Q)에만 있고, LLM 출력은 코드가 검증해
통과하지 못한 문장은 버린다(비어 있는 것이 틀린 것보다 낫다).
  재료  report_facts(rep)         → F1, F2 … (재무·구성·주주·위험 공시. 종가에 따라 변하는 시총·PER·목표가는 넣지 않는다)
        build_excerpts(sections)  → Q1, Q2 … (filing_text가 뽑은 사업의 개요·경영진단·제재·소송·핵심감사사항 발췌, 글자 예산 안에서 위험 단서 위주)
  검증  validate(parsed, …)       → ① 문장 속 모든 숫자가 인용한 근거(없으면 전체 재료)에 있는가 ② 인용문이 원문 발췌의 부분 문자열인가
                                    ③ 근거 ID가 실제로 있는가 ④ 전망·목표가·매수/매도·저평가 같은 금지 표현이 없는가
  호출  llm(messages) → (본문, 모델명). 함수로 주입해 가짜 LLM으로 테스트한다. groq_llm()이 실제 호출기.
Groq 무료 한도(2026-11 실측): gpt-oss-120b·20b 모두 분당 8,000토큰(입력+출력)·하루 1,000회. 한국어 공시문은 약 1.6자/토큰이라
발췌 예산을 EXCERPT_TOTAL자로 묶는다(초과하면 413).
"""
import hashlib
import json
import os
import re
import time
from decimal import Decimal, InvalidOperation

import company_report as crep
import filing_text as ft

PROMPT_VERSION = "3"
GROQ_MODELS = ("openai/gpt-oss-120b", "openai/gpt-oss-20b")      # 분당 한도가 모델별이라 120b가 막히면 20b로 넘어간다
MAX_OUTPUT_TOKENS = 2600
EXCERPT_TOTAL = 5500                  # 발췌 전체 글자 예산(≈3,400토큰)
EXCERPT_PLAN = (("overview", 1500), ("mda", 1500), ("sanctions", 600), ("contingent", 600), ("kam", 700), ("subsequent", 300))
CHUNK_CHARS = 380
LIMITS = {"business": 3, "changes": 3, "risks": 5, "questions": 3}
SECTION_NAMES = {"business": "사업 요약", "changes": "최근 변화", "risks": "비판적 리스크", "questions": "직접 확인할 질문"}

# 전망·가치판단·투자의견 표현. LLM이 쓴 문장(text·title)에서만 찾는다(인용문은 원문 그대로라 대상이 아니다).
BANNED = re.compile(r"목표\s*(?:주)?가|적정\s*(?:주)?가|매수|매도|추천|투자\s*의견|비중\s*(?:확대|축소)|상승\s*여력|하락\s*여력|저평가|고평가|"
                    r"전망|예상|예측|기대된다|기대할\s*수|유망|\bBUY\b|\bSELL\b|\bHOLD\b", re.I)
# '결과를 예측할 수 없습니다'는 전망이 아니라 불확실성(소송·수사 리스크의 전형적 문장)이라 금지어 검사에서 뺀다(실측: 카카오 검찰 수사 리스크가 '예측' 때문에 탈락했다)
NEGATED_FORECAST = re.compile(r"(?:예측|예상|전망)\s*(?:을\s*)?(?:하기\s*)?(?:할\s*수\s*없|이\s*어렵|이\s*불가|어렵|불가)")
REF_ID = re.compile(r"\(?\b[FQ]\d+\b\)?")
NO_IMPACT = re.compile(r"중요한\s*영향을\s*미치지\s*(?:아니|않)|영향(?:은|이)?\s*없")        # 경영진의 '영향 없음' 상투문 — 리스크가 아니다(프롬프트로 막아도 모델이 고른다)
NONE_LINE = re.compile(r"해당사항(?:이)?없(?:음|습니다|다)")
HEADING = re.compile(r"^(?:[가-힣]|\d{1,2}|\(\d{1,2}\)|[①-⑩]|[IVX]+)[.)]\s*\S")
CUES = ("증가", "감소", "하락", "상승", "영향", "원인", "둔화", "확대", "축소", "악화", "개선", "손실", "적자", "의존", "비중", "소송", "제재", "위반",
        "과태료", "벌금", "불확실", "위험", "리스크", "경쟁", "수요", "가격", "고객", "매출처", "원재료", "환율", "차입", "담보", "보증", "중단", "지연",
        "손상", "충당", "채무", "횡령", "배임", "경고", "시정", "신규", "투자", "가동")


class NarrativeError(Exception):
    """호출·응답 형식 문제. 메시지는 화면에 그대로 보여도 된다(키·URL 없음)."""


# --- 사실(F) -------------------------------------------------------------------------------------
def _chg(cur, prev):
    """전년 대비 문구. 기준이 0 이하이면 퍼센트가 의미 없어 '흑자 전환'처럼 말로 쓴다."""
    if cur is None or prev is None:
        return None
    if prev > 0:
        return "적자 전환" if cur <= 0 else f"{(cur / prev - 1) * 100:+.1f}%"
    if prev < 0:
        return "흑자 전환" if cur > 0 else "적자 지속"
    return None


def report_facts(rep):
    """build_report 결과 → [{'id': 'F1', 'text': …}]. 종가가 들어가는 값(시총·PER·PBR·PSR·시뮬레이터)은 일부러 뺀다 —
    서술이 밸류에이션 판단으로 번지지 않게 하고, 종가가 바뀌어도 같은 서술 캐시를 쓰기 위해서다."""
    cur = rep.get("currency") or "KRW"
    amt = lambda v: crep.fmt_amount(v, cur)
    lines = []
    rows = [r for r in rep["annual"]["rows"] if any(r.get(k) is not None for k in ("revenue", "op", "ni", "assets", "equity", "ocf"))]
    for r in rows:
        parts = []
        if r["revenue"] is not None:
            parts.append(f"매출 {amt(r['revenue'])}")
        if r["op"] is not None:
            parts.append(f"영업이익 {amt(r['op'])}" + (f"(영업이익률 {crep.fmt_pct(r['op_margin'])})" if r["op_margin"] is not None else ""))
        if r["ni"] is not None:
            parts.append(f"순이익 {amt(r['ni'])}")
        if r["ocf"] is not None:
            parts.append(f"영업현금흐름 {amt(r['ocf'])}")
        if r["fcf"] is not None:
            parts.append(f"FCF {amt(r['fcf'])}")
        if r["roe"] is not None:
            parts.append(f"ROE {crep.fmt_pct(r['roe'])}")
        if r["debt_ratio"] is not None:
            parts.append(f"부채비율 {crep.fmt_pct(r['debt_ratio'])}")
        if parts:
            lines.append(f"{r['year']} 사업연도: " + " · ".join(parts))
    if len(rows) >= 2:        # 전년 대비는 가장 최근 해 하나만 — 두 해를 주면 모델이 지난해 증가율을 올해 것처럼 쓴다(실측)
        a, b = rows[-1], rows[-2]
        # 현금흐름까지 방향을 명시한다 — 안 주면 모델이 FCF 6,032억→2,438억을 '회복'이라 쓴다(실측, 숫자 대조는 방향을 못 본다)
        ch = [f"{name} {c}" for name, k in (("매출", "revenue"), ("영업이익", "op"), ("순이익", "ni"), ("영업현금흐름", "ocf"), ("FCF", "fcf")) if (c := _chg(a[k], b[k]))]
        if ch:
            lines.append(f"{a['year']} 사업연도 전년({b['year']}) 대비: " + " · ".join(ch))
    ttm = rep["header"].get("ttm") or {}
    if ttm.get("is_ttm"):
        parts = [f"{n} {amt(ttm[k])}" for n, k in (("매출", "revenue"), ("영업이익", "op"), ("순이익", "ni")) if ttm.get(k) is not None]
        if parts:
            lines.append(f"후행 12개월({ttm['label']}): " + " · ".join(parts))
    for view in (rep.get("mix") or {}).get("views", []):
        mem = sorted((m for m in view["members"] if not m.get("derived") and m.get("share") is not None), key=lambda m: -m["share"])[:5]
        if len(mem) >= 2:       # 연도를 붙인다 — 안 붙이면 모델이 연도를 지어낸다(실측: 2025 보고서의 구성을 '2023년'으로 씀)
            lines.append(f"{rep['annual_year']} 사업연도 매출 구성({view['title']}, {view['source']}): " + ", ".join(f"{m['label']} {m['share']:.1f}%" for m in mem))
    h = rep.get("holders")
    if h and h.get("major"):
        m = h["major"]
        txt = f"최대주주 {m['name']}" + (f" 지분율 {m['ratio']:.2f}%" if m.get("ratio") is not None else "")
        if h.get("group_ratio") is not None:
            txt += f" · 특수관계인 합계 {h['group_ratio']:.2f}%"
        lines.append(txt + (f" ({h['period']})" if h.get("period") else ""))
    t = rep.get("treasury")
    if t and t.get("ratio") is not None:
        txt = f"자기주식 보유 {t['ratio']:.2f}%"
        extra = [f"{lab} {t[k]:,.0f}주" for lab, k in (("기중 취득", "acquired"), ("기중 소각", "cancelled")) if t.get(k)]
        lines.append(txt + (" · " + " · ".join(extra) if extra else "") + (f" ({t['period']})" if t.get("period") else ""))
    for f in [f for f in rep.get("filings", []) if f.get("risk")][:3]:
        lines.append(f"최근 90일 위험 공시({f['risk']}): {f['date']:%Y-%m-%d} {f['title']}")
    return [{"id": f"F{i}", "text": s} for i, s in enumerate(lines, 1)]


# --- 원문 발췌(Q) --------------------------------------------------------------------------------
def _is_none_line(ln):
    """'해당사항 없음'만 말하는 짧은 줄(소제목이 붙어 있어도). 큰 숫자가 있으면 내용이 있는 줄로 본다."""
    t = re.sub(r"\s", "", ln)
    return bool(NONE_LINE.search(t)) and len(t) <= 120 and not re.search(r"\d{3,}", t)


def _is_heading(ln):
    return len(ln) <= 40 and bool(HEADING.match(ln)) and not ln.rstrip().endswith(("다.", "다"))


def _clean_lines(text):
    """'해당사항 없음' 줄과, 그 때문에 내용 없이 남은 소제목을 지운다."""
    lines = [ln for ln in text.split("\n") if ln.strip() and not _is_none_line(ln)]
    return [ln for i, ln in enumerate(lines) if not (_is_heading(ln) and (i + 1 >= len(lines) or _is_heading(lines[i + 1])))]


def _split_line(ln, size):
    """한 줄을 문장 단위로(DART는 '…입니다.다음 문장'처럼 마침표 뒤에 공백이 없다), 그래도 size를 넘으면 억지로 자른다."""
    out = []
    for p in re.split(r"(?<=[다음임함됨]\.)\s*(?=\S)", ln):
        while len(p) > size:
            out.append(p[:size])
            p = p[size:]
        if p:
            out.append(p)
    return out


def _chunks(text, size=CHUNK_CHARS):
    """줄·문장을 size자 안팎의 발췌 조각으로 묶는다(줄 사이는 줄바꿈, 같은 줄의 문장 사이는 공백). 인용 대조는 공백을 무시하므로 원문과 어긋나지 않는다."""
    chunks, cur = [], ""
    for ln in _clean_lines(text):
        for k, p in enumerate(_split_line(ln, size)):
            sep = "\n" if k == 0 else " "
            if cur and len(cur) + len(sep) + len(p) > size:
                chunks.append(cur)
                cur = p
            else:
                cur = cur + sep + p if cur else p
    if cur:
        chunks.append(cur)
    for i in range(1, len(chunks)):          # 표가 청크 경계에서 쪼개져 '(단위: 천원)' 머리말이 앞 청크에만 남으면, 바로 앞 청크의 선언을 이어 붙인다(숫자 칸의 단위 근거)
        decl = TABLE_UNIT.findall(chunks[i - 1])
        if decl and not TABLE_UNIT.search(chunks[i]) and "|" in chunks[i]:
            chunks[i] = f"(단위: {decl[-1]}) " + chunks[i]
    return chunks


MIN_RAW_SCORE = 1.0       # 위험 단서도 숫자도 거의 없는 조각(가족친화 인증, '○○ 참조' 같은 문장)은 예산이 남아도 넣지 않는다
KAM_HOW = re.compile(r"핵심감사사항이\s*감사에서\s*(?:어떻게\s*)?(?:다루어진\s*방법|다루어졌는지)[^.]*\.")


def _raw_score(chunk):
    return sum(chunk.count(w) for w in CUES) + (0.5 if re.search(r"\d", chunk) else 0.0)


RISK_NATIVE = {"kam", "sanctions", "contingent"}      # 내용 자체가 위험 신호인 섹션 — 단서어가 없어도 거르지 않는다(실제 KAM 문장엔 단서어가 거의 없다)


def _select(chunks, budget, first_bonus=0.0, min_score=MIN_RAW_SCORE):
    """점수 높은 조각부터 budget자 안에서 고른다(첫 조각엔 first_bonus, min_score 미달은 제외). 원문 순서대로 돌려준다."""
    def score(i):
        return _raw_score(chunks[i]) + (first_bonus if i == 0 else 0.0) - i * 0.01
    ranked = [i for i in sorted(range(len(chunks)), key=lambda i: -score(i)) if _raw_score(chunks[i]) + (first_bonus if i == 0 else 0.0) >= min_score]
    used, picked = 0, []
    for i in ranked:
        if used + len(chunks[i]) <= budget:
            picked.append(i)
            used += len(chunks[i])
    return [chunks[i] for i in sorted(picked)]


POINTER = re.compile(r"(?:참조|참고)\s*하시기\s*바랍니다")


def _prepare(key, text):
    """섹션별 사전 정리.
    - 핵심감사사항: '감사에서 다루어진 방법'(감사절차 목록)이 예산을 먹으므로 앞의 '결정한 이유'만 남긴다.
    - 작성기준일 이후 발생한 주요사항: 표준 서식에서 '가.'만 후속 사건이고 '나.' 이하(합병 사후정보·녹색경영·가족친화 인증·정부 인증)는 홍보성이라 '나.' 앞에서 자른다."""
    if key == "kam":
        text = KAM_HOW.sub("", text)
        text = "\n".join(ln for ln in text.split("\n") if not ln.lstrip().startswith(("-", "·", "•", "※")))
    elif key == "subsequent":
        lines = text.split("\n")
        cut = next((i for i, ln in enumerate(lines) if re.match(r"^나\.\s", ln)), len(lines))
        text = "\n".join(lines[:cut])
    return text


def _is_pointer(chunk):
    """'○○ 참조하시기 바랍니다'뿐인 짧은 조각(다른 장을 가리키는 문장)."""
    return len(chunk) < 150 and bool(POINTER.search(chunk))


def build_excerpts(sections, plan=EXCERPT_PLAN, total=EXCERPT_TOTAL):
    """섹션 → [{'id': 'Q1', 'key', 'label', 'text'}]. 내용이 없는 섹션('해당사항 없음'뿐)은 건너뛰고, 남는 예산은 사업 개요·경영진단에 더 준다."""
    pools = {}
    for key, _ in plan:
        sec = (sections or {}).get(key)
        ch = [c for c in _chunks(_prepare(key, sec["text"])) if not _is_pointer(c)] if sec else []
        if ch:
            pools[key] = (sec.get("label") or ft.SECTION_LABELS.get(key, key), ch)
    floor = lambda k: 0.0 if k in RISK_NATIVE else MIN_RAW_SCORE
    picked = {k: _select(ch, dict(plan)[k], first_bonus=3.0 if k == "overview" else 0.0, min_score=floor(k)) for k, (_, ch) in pools.items()}
    leftover = total - sum(len(c) for v in picked.values() for c in v)
    greedy = [k for k in ("overview", "mda") if k in pools]
    if leftover > CHUNK_CHARS and greedy:
        share = leftover // len(greedy)
        for k in greedy:       # 새 예산 = 지금 실제로 쓴 글자 + 몫(계획 예산에 더하면 첫 패스에서 덜 쓴 만큼이 이중 계상돼 전체 예산을 넘는다 — 실측 5,686자)
            picked[k] = _select(pools[k][1], sum(len(c) for c in picked[k]) + share, first_bonus=3.0 if k == "overview" else 0.0, min_score=floor(k))
    out, n = [], 0
    for key, _ in plan:
        if key not in picked:
            continue
        for c in picked[key]:
            n += 1
            out.append({"id": f"Q{n}", "key": key, "label": pools[key][0], "text": c})
    return out


# --- 프롬프트 ------------------------------------------------------------------------------------
SYSTEM_PROMPT = """당신은 한국 상장사의 사업보고서를 읽고 투자자에게 사실만 정리해 주는 보수적인 보조자입니다. 아래 [사실]과 [공시 원문]에 있는 내용만 근거로 JSON 객체 하나만 출력하세요.

규칙
1. 숫자는 근거에 적힌 표기 그대로 단위까지 옮깁니다('9,133억', '8.1%'). 계산·반올림·단위 변환·추정을 하지 않습니다. 근거에 없는 숫자는 쓰지 않습니다.
2. 금액·비율을 쓸 때는 문장에 연도를 밝히고 그 연도 줄의 값만 씁니다. 다른 연도의 값을 섞지 마세요(예: 2024년 증가율을 2025년 것으로 쓰면 안 됩니다).
3. 모든 항목에 근거 ID를 refs에 1개 이상 답니다(F=사실, Q=공시 원문 발췌). 근거에 없는 내용은 쓰지 않습니다.
4. 전망·예상·목표가·투자의견·매수/매도/추천·저평가/고평가 표현을 쓰지 않습니다. 과거와 현재의 사실만 씁니다.
5. risks에는 투자자에게 불리할 수 있는 사실만 고릅니다. 점검할 범주: 고객·매출처 쏠림, 원재료·공급 의존, 제재·소송, 차입금·부채 구조(예: 매출채권 할인으로 늘어난 단기차입금), 이익·이익률 감소, 현금흐름(FCF) 부진, 중단영업·자산손상, 감사인이 핵심감사사항으로 짚은 위험. 모든 기업에 공통인 일반 문구(환율·금리 위험 관리 방침 등)는 이 회사만의 구체적 수치나 사건이 없으면 고르지 않습니다. 경영진이 '중요한 영향이 없다'고 쓴 문장이나 이미 해결됐다는 문장은 리스크가 아닙니다. 근거가 약하면 3개 미만이어도 됩니다.
6. 모든 risk에는 quote_id(Q 번호)와, 그 Q 원문에서 한 글자도 바꾸지 않고 복사한 quote(20~120자)를 반드시 답니다. 원문 인용이 없는 risk는 쓰지 않습니다.
7. questions는 [사실]만 봐서는 답이 나오지 않아 투자자가 더 조사해야 하는 것만 씁니다(예: 이익이 줄어든 원인이 일시적인지, 특정 고객 의존이 어떻게 변해 왔는지). 사실에서 바로 읽히는 숫자를 되묻지 않습니다.
8. 항목당 한국어 평서문 1~2문장(questions는 의문문). '당사' 대신 '회사'라고 쓰고, 회사의 홍보 문구는 옮기지 않습니다.

출력 형식 (이 키만 사용):
{"business":[{"text":"…","refs":["F1","Q1"]}],
 "changes":[{"text":"…","refs":["F2"]}],
 "risks":[{"title":"10자 이내 제목","text":"…","refs":["Q4"],"quote_id":"Q4","quote":"원문 그대로"}],
 "questions":[{"text":"투자자가 직접 확인해 볼 질문","refs":["F3"]}]}
개수: business 3개(무슨 사업으로 어떻게 버는지) · changes 최대 3개(최근 1~2년의 실적·사업 변화) · risks 3~5개 · questions 2~3개."""


def build_messages(name, facts, excerpts):
    parts = [f"회사: {name}", "", "[사실]"] + [f"{f['id']} {f['text']}" for f in facts]
    parts += ["", "[공시 원문 발췌]"] + [f"{q['id']} ({q['label']}) {q['text']}" for q in excerpts]
    return [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": "\n".join(parts)}]


# --- 검증 ----------------------------------------------------------------------------------------
_UNITS = r"분기|백만|천만|조|억|만|천|%p|%|배|주|건|명|원|달러|개|곳|년|월|일|bp|점|위|세"
_NUM = re.compile(rf"(?<![\w.])(\d[\d,]*(?:\.\d+)?)({_UNITS})?")
_DATE = re.compile(r"(?<!\d)((?:19|20)\d{2})[.\-/](\d{1,2})(?:[.\-/](\d{1,2}))?(?!\d)")


TABLE_UNIT = re.compile(r"단위\s*[:：]?\s*(백만원|천만원|천원|만원|억원|조원|원)")
_TABLE_UNIT_CANON = {"백만원": "백만", "천만원": "천만", "천원": "천", "만원": "만", "억원": "억", "조원": "조", "원": "원"}


def num_tokens(s, infer_table_units=False):
    """문장 속 (숫자, 단위) 쌍의 집합: '9,133억'→('9133','억'), '9.90%'→('9.9','%'), 단위 없는 숫자는 단위 ''.
    숫자만 비교하면 '1조 2,000억'처럼 서로 다른 줄의 숫자 둘을 이어 붙인 조작을 못 잡는다 → 단위까지 한 묶음으로 본다.
    '2024.03.11'은 (연·월·일 × 단위 있음/없음) 조합을 모두 넣어 '2025년 10월 21일' 같은 다른 표기와도 맞는다.
    infer_table_units=True(근거 쪽에만 씀): 공시 표는 숫자 칸에 단위가 없고 '(단위: 천원)' 머리말에 따로 있다. 그 선언 뒤에 나오는 단위 없는 숫자에
    그 단위를 붙인 쌍도 넣어, 모델이 머리말에서 단위를 가져와 쓴 '소송가액 2,500,000천원'을 근거로 인정한다(문장 쪽 추론은 하지 않는다)."""
    s = REF_ID.sub(" ", s or "")
    out = set()

    def add(t, unit=""):
        try:
            out.add((format(Decimal(t.replace(",", "")).normalize(), "f"), unit or ""))
        except InvalidOperation:
            pass

    for m in _DATE.finditer(s):
        if 1 <= int(m.group(2)) <= 12 and (m.group(3) is None or 1 <= int(m.group(3)) <= 31):
            for g, unit in zip(m.groups(), ("년", "월", "일")):
                if g:
                    add(g)
                    add(g, unit)
    decls = [(m.end(), _TABLE_UNIT_CANON[m.group(1)]) for m in TABLE_UNIT.finditer(s)] if infer_table_units else []
    for m in _NUM.finditer(s):
        add(m.group(1), m.group(2))
        if not m.group(2) and decls:
            prior = [u for pos, u in decls if pos <= m.start()]
            if prior:
                add(m.group(1), prior[-1])
    return out


YEAR_LINE = re.compile(r"^(\d{4}) 사업연도")          # 연도별 사실 줄('2025 사업연도: …', '2025 사업연도 전년(2024) 대비: …')
YEAR_IN_TEXT = re.compile(r"(?<!\d)((?:19|20)\d{2})(?!\d)")
PREV_WORDS = re.compile(r"전년|전기|작년|지난해|직전")
MONEY_UNITS = {"조", "억", "백만", "천만", "만", "%", "배"}


def year_mismatch(text, refs, src, nums):
    """문장이 연도를 밝혔는데, 거기 쓴 금액·비율이 (문장이 인용한 근거 중) '다른 사업연도 줄'에만 있으면 그 토큰을 돌려준다(없으면 None).
    숫자가 근거에 있어도 지난해 증가율을 올해 것처럼 쓰는 오귀속은 숫자 대조로 못 잡는다(실측). 연도를 밝히지 않은 문장, 인용 근거에 연도 줄이 아닌 곳(원문·구성·주주)이
    있는 숫자는 판단하지 않는다. 인용하지 않은 근거에 우연히 같은 숫자가 있어도 면제하지 않는다(실측: 2025년 -4.9%를 '2024년 4.9% 증가'로 쓴 문장이 통과).
    '전년'이 있으면 언급한 해의 직전 연도 줄도 허용한다."""
    mentioned = {int(y) for y in YEAR_IN_TEXT.findall(text)}
    if not mentioned:
        return None
    allowed = set(mentioned)
    if PREV_WORDS.search(text):
        allowed |= {y - 1 for y in mentioned}
    for tok in sorted(num_tokens(text)):
        if tok[1] not in MONEY_UNITS:
            continue
        owners = [i for i in refs if _covered(tok, nums[i])]
        labels = [YEAR_LINE.match(src[i]) for i in owners]
        if owners and all(labels) and not ({int(m.group(1)) for m in labels} & allowed):
            return tok[0] + tok[1], sorted({int(m.group(1)) for m in labels})
    return None


QUOTE_OVERLAP_MIN = 0.05


STEM_STOP = {"있습", "있다", "있으", "있는", "없습", "없다", "합니", "입니", "됩니", "하였", "했습", "하는", "되어", "이며", "으로", "통해", "따라", "대한", "위한", "경우",
             "현재", "당사", "회사", "연결", "기업", "것으", "등을", "등이", "및", "관련"}       # 문장마다 나오는 어미·상투어 — 겹쳐도 관련 있다는 증거가 못 된다


def _stems(s):
    """어휘 겹침용 어간: 한글 2자 이상 단어는 앞 2글자(조사·어미 무시, 상투어 제외), 영문 3자 이상은 소문자, 숫자 그대로."""
    out = {t[:2] if re.match(r"[가-힣]", t) else t.lower() for t in re.findall(r"[가-힣]{2,}|[A-Za-z]{3,}|\d[\d.,]*", s or "")}
    return out - STEM_STOP


def quote_overlap(text, quote):
    """문장의 어간 중 인용문에도 나오는 비율. 화면에 근거로 나란히 보여 주는 인용이 문장과 전혀 무관한 경우(실측: '고객·매출처 집중'이라 쓰고 환율 위험 문장을 인용)만
    걸러내려는 값이라 임계값을 아주 낮게 잡는다 — 표 행을 풀어 쓴 정당한 문장도 0.10~0.20이 나온다."""
    a = _stems(text)
    return len(a & _stems(quote)) / len(a) if a else 1.0


UP_WORDS, DOWN_WORDS = ("증가", "상승", "늘", "성장"), ("감소", "하락", "줄", "축소")
_PCT_WORD = re.compile(r"(\d[\d,]*(?:\.\d+)?)\s*%\s*(" + "|".join(UP_WORDS + DOWN_WORDS) + ")")
_SIGNED_PCT = re.compile(r"([+\-−])(\d[\d,]*(?:\.\d+)?)%")


def direction_mismatch(text, refs, src):
    """'N% 증가/감소'의 방향이 인용한 사실(F) 줄의 부호와 반대이면 (숫자, 말)을 돌려준다. 부호가 있는 건 우리가 만든 사실 줄뿐이라 그것만 본다.
    실측: F에 '-4.9%'인 매출을 '4.9% 증가'로 옮겼다(숫자 토큰은 부호를 보지 않아 통과)."""
    for m in _PCT_WORD.finditer(text):
        n = format(Decimal(m.group(1).replace(",", "")).normalize(), "f")
        signs = {"-" if s in "-−" else "+" for i in refs if i.startswith("F")
                 for s, v in _SIGNED_PCT.findall(src[i]) if format(Decimal(v.replace(",", "")).normalize(), "f") == n}
        up = m.group(2) in UP_WORDS
        if (signs == {"-"} and up) or (signs == {"+"} and not up):
            return m.group(1) + "%", m.group(2)
    return None


def _covered(tok, have, years=()):
    """토큰이 근거(have)에 있는가. 단위가 있으면 같은 숫자+단위여야 하고, 단위가 없으면 같은 숫자면 된다. 연도(±)는 근거 없이도 허용."""
    n, u = tok
    if n in years and u in ("", "년"):
        return True
    return tok in have if u else any(n == hn for hn, _ in have)


def _parse_json(content):
    if not content or not content.strip():
        raise NarrativeError("모델이 빈 응답을 돌려줬습니다(출력 토큰 한도 안에 끝내지 못했을 수 있습니다).")
    t = re.sub(r"^```(?:json)?\s*|\s*```$", "", content.strip())
    i, j = t.find("{"), t.rfind("}")
    if i < 0 or j <= i:
        raise NarrativeError("모델 응답에서 JSON을 찾지 못했습니다.")
    try:
        data = json.loads(t[i:j + 1])
    except ValueError:
        raise NarrativeError("모델 응답이 올바른 JSON이 아닙니다(중간에 잘렸을 수 있습니다).")
    if not isinstance(data, dict):
        raise NarrativeError("모델 응답 형식이 객체가 아닙니다.")
    return data


def validate(parsed, facts, excerpts, annual_year=None):
    """LLM 출력 검증. → {'business':[…], 'changes':[…], 'risks':[…], 'questions':[…], 'dropped':[{'section','text','reason'}]}
    통과 항목은 {'text','refs'(+risks: 'title','quote','quote_id')}. 숫자가 인용한 근거엔 없고 다른 근거에 있으면 그 근거를 refs에 보태 준다."""
    src = {f["id"]: f["text"] for f in facts}
    src.update({q["id"]: q["text"] for q in excerpts})
    order = [f["id"] for f in facts] + [q["id"] for q in excerpts]
    nums = {i: num_tokens(t, infer_table_units=True) for i, t in src.items()}
    years = {str(y) for y in range(annual_year - 6, annual_year + 2)} if annual_year else set()      # 연도는 근거 없이도 허용(단위 '년' 또는 없음)
    out = {k: [] for k in LIMITS}
    out["dropped"] = []

    def drop(section, item, reason):
        text = item.get("text") if isinstance(item, dict) else str(item)
        out["dropped"].append({"section": section, "text": str(text or "")[:200], "reason": reason})

    for section, limit in LIMITS.items():
        items = parsed.get(section)
        if not isinstance(items, list):
            continue
        for item in items:
            if item in ("", None, {}):                           # 모델이 JSON 사이에 빈 문자열을 끼우는 경우(실측) — 내용이 없으니 탈락으로 세지도 않는다
                continue
            if len(out[section]) >= limit:
                drop(section, item, "개수 초과")
                continue
            if not isinstance(item, dict) or not isinstance(item.get("text"), str):
                drop(section, item, "형식 오류")
                continue
            text = REF_ID.sub("", item["text"])
            text = re.sub(r"\s+", " ", text).strip()
            title = re.sub(r"\s+", " ", REF_ID.sub("", str(item.get("title") or ""))).strip()[:30]
            if len(text) < 10:
                drop(section, item, "문장이 너무 짧음")
                continue
            ban = BANNED.search(NEGATED_FORECAST.sub(" ", text)) or BANNED.search(NEGATED_FORECAST.sub(" ", title))
            if ban:
                drop(section, item, f"금지 표현('{ban.group()}')")
                continue
            refs = [r for r in dict.fromkeys(str(r).strip().upper() for r in (item.get("refs") or []) if r) if r in src]
            quote, quote_id = str(item.get("quote") or "").strip(), str(item.get("quote_id") or "").strip().upper()
            if section == "risks":      # 리스크는 공시 원문 인용이 있어야 한다 — 숫자(F)만으로 '위험하다'고 단정한 문장은 해석이 검증되지 않는다(실측: 부채비율이 줄었는데 '부채 증가' 리스크)
                if not quote:
                    drop(section, item, "원문 인용이 없음(리스크는 공시 원문 인용이 있어야 함)")
                    continue
                if NO_IMPACT.search(quote):
                    drop(section, item, "경영진이 '영향 없음'이라 쓴 문장은 리스크가 아님")
                    continue
                hit = quote_id if quote_id in src and quote_id.startswith("Q") and ft.contains_quote(src[quote_id], quote) else \
                    next((q["id"] for q in excerpts if ft.contains_quote(q["text"], quote)), None)
                if not hit:
                    drop(section, item, "인용문이 원문 발췌에 없음")
                    continue
                if quote_overlap(text, quote) < QUOTE_OVERLAP_MIN:
                    drop(section, item, "인용문이 문장과 무관함(겹치는 단어 거의 없음)")
                    continue
                quote_id = hit
                if quote_id not in refs:
                    refs.append(quote_id)
            else:
                quote = quote_id = ""
            have = set().union(*(nums[r] for r in refs)) if refs else set()
            bad = []
            for tok in sorted(num_tokens(text)):
                if _covered(tok, have, years):
                    continue
                owner = next((i for i in order if _covered(tok, nums[i])), None)       # 다른 근거에 있으면 refs를 바로잡는다
                if owner is None:
                    bad.append(tok[0] + tok[1])
                else:
                    refs.append(owner)
                    have |= nums[owner]
            if bad:
                drop(section, item, "근거에 없는 숫자: " + ", ".join(bad))
                continue
            mism = year_mismatch(text, refs, src, nums)
            if mism:
                drop(section, item, f"연도 불일치 의심: {mism[0]}은(는) {', '.join(map(str, mism[1]))} 사업연도 값인데 문장은 다른 연도를 말함")
                continue
            wrong_way = direction_mismatch(text, refs, src)
            if wrong_way:
                drop(section, item, f"증감 방향 불일치 의심: {wrong_way[0]}은(는) 사실 줄에서 반대 부호인데 문장은 '{wrong_way[1]}'이라 함")
                continue
            if not refs:
                drop(section, item, "근거(refs) 없음")
                continue
            refs = [i for i in order if i in set(refs)]
            row = {"text": text, "refs": refs}
            if section == "risks":
                row.update({"title": title, "quote": quote, "quote_id": quote_id})
            out[section].append(row)
    return out


# --- 조립 ----------------------------------------------------------------------------------------
def facts_hash(rep, facts=None):
    """서술 캐시 키: 프롬프트 버전 + 사실 목록. 같은 보고서·같은 숫자면 종가가 바뀌어도 같다(원문은 접수번호가 정한다)."""
    facts = facts if facts is not None else report_facts(rep)
    raw = PROMPT_VERSION + "|" + "|".join(f["text"] for f in facts)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]


def broken_items(parsed):
    """목록 안에 객체가 아닌 비어 있지 않은 값(문자열 조각 등)이 몇 개 있는가. 실측: 모델이 리스크 항목을 '\"title\"', '\":\"', '\"노동소송\"'처럼 키·값 조각으로 흩뿌려
    JSON 문법은 맞지만 항목이 통째로 깨진 출력을 낸다(빈 문자열 \"\"을 끼우는 경미한 형태는 세지 않는다)."""
    return sum(1 for k in LIMITS if isinstance(parsed.get(k), list) for it in parsed[k] if it not in ("", None, {}) and not isinstance(it, dict))


def generate(rep, sections, llm, max_attempts=2):
    """보고서 + 원문 섹션 → 검증된 서술. 호출 실패·형식 오류는 NarrativeError. 항목이 전부 탈락해도 결과는 돌려준다('dropped'로 이유 확인).
    출력 구조가 깨졌으면(broken_items) 한 번 더 호출한다(모델이 비결정적이라 다시 뽑으면 대개 정상). 두 번 다 깨지면 마지막 것으로 검증한다."""
    facts = report_facts(rep)
    excerpts = build_excerpts(sections)
    if not excerpts:
        raise NarrativeError("사업보고서 본문에서 서술 재료(사업의 개요·경영진단 등)를 찾지 못했습니다(은행·보험 등 서식이 다른 보고서일 수 있습니다).")
    messages = build_messages(rep["name"], facts, excerpts)
    attempts, errors = 0, []
    while True:
        attempts += 1
        content, model = llm(messages)
        errors += list(getattr(llm, "errors", None) or [])
        parsed = _parse_json(content)
        if not broken_items(parsed) or attempts >= max_attempts:
            break
        errors.append(f"{model.split('/')[-1]}: 출력 구조가 깨져 다시 호출")
    result = validate(parsed, facts, excerpts, rep.get("annual_year"))
    result.update({"model": model, "facts": facts, "excerpts": excerpts, "rcept_no": rep.get("rcept_no"), "prompt_chars": sum(len(m["content"]) for m in messages),
                   "usage": getattr(llm, "usage", None), "llm_errors": errors, "attempts": attempts,
                   "generated_at": time.strftime("%Y-%m-%d %H:%M"), "prompt_version": PROMPT_VERSION,
                   "kept": sum(len(result[k]) for k in LIMITS),
                   "asked": sum(1 for k in LIMITS if isinstance(parsed.get(k), list) for it in parsed[k] if isinstance(it, dict))})
    return result


def _cache_file(rcept_no, fhash):
    import dart_core
    return os.path.join(dart_core.CACHE_DIR, f"narr_{rcept_no}_{fhash}.json")


def load_cached(rep):
    """같은 접수번호·같은 사실이면 이전에 만든 서술. 없으면 None."""
    if not rep.get("rcept_no"):
        return None
    try:
        with open(_cache_file(rep["rcept_no"], facts_hash(rep)), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def save_cached(rep, result):
    """통과 항목이 하나라도 있을 때만 캐시한다(전부 탈락한 결과를 굳히지 않는다)."""
    if not rep.get("rcept_no") or not result.get("kept"):
        return
    import dart_core
    try:
        os.makedirs(dart_core.CACHE_DIR, exist_ok=True)
        path = _cache_file(rep["rcept_no"], facts_hash(rep))
        tmp = f"{path}.{os.getpid()}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False)
        os.replace(tmp, path)
    except OSError:
        pass


# --- Groq ----------------------------------------------------------------------------------------
def groq_llm(api_key, models=GROQ_MODELS, max_tokens=MAX_OUTPUT_TOKENS, wait_cap=70):
    """llm(messages) → (본문, 모델명). 분당 토큰 한도(429)는 알려 준 시간만큼 기다려 한 번 다시, 모델이 없거나 한도가 막히면 다음 모델.
    401/403/413은 재시도해도 소용없어 바로 NarrativeError(키·URL은 메시지에 넣지 않는다)."""
    def call(messages):
        from groq import Groq
        client = Groq(api_key=api_key)
        last = "알 수 없는 오류"
        call.errors = []                                   # 다음 모델로 넘어간 이유(결과에 남겨 화면에 보여 준다)
        for model in models:
            json_mode = True                               # Groq JSON 모드는 gpt-oss-120b 출력을 'json_validate_failed'(400)로 자주 떨어뜨린다(실측) → 400이면 JSON 모드 없이 다시
            for attempt in range(2):
                try:
                    extra = {"response_format": {"type": "json_object"}} if json_mode else {}
                    r = client.chat.completions.create(model=model, messages=messages, temperature=0.2, max_tokens=max_tokens,
                                                       reasoning_effort="low", **extra)
                    u = getattr(r, "usage", None)
                    call.usage = {"prompt": getattr(u, "prompt_tokens", None), "completion": getattr(u, "completion_tokens", None)} if u else None
                    return r.choices[0].message.content, model
                except Exception as e:
                    status = getattr(e, "status_code", None)
                    if status in (401, 403):
                        raise NarrativeError(f"Groq 접근 오류({status}): GROQ_API_KEY가 유효한지, 서버 네트워크가 막히지 않았는지 확인해 주세요.")
                    body = getattr(e, "body", None)
                    err = body.get("error") if isinstance(body, dict) else None
                    code = err.get("code") if isinstance(err, dict) else None
                    last = f"{type(e).__name__}({status}{':' + str(code) if code else ''})"
                    call.errors.append(f"{model.split('/')[-1]}: {last}")
                    if status == 413:
                        raise NarrativeError("요청이 Groq 분당 토큰 한도(8,000)보다 큽니다. 발췌 예산(EXCERPT_TOTAL)을 줄여야 합니다.")
                    if status == 429 and attempt == 0:
                        try:
                            wait = float(e.response.headers.get("retry-after", 20))
                        except (AttributeError, TypeError, ValueError):
                            wait = 20.0
                        time.sleep(min(max(wait, 1.0), wait_cap))
                        continue
                    if status == 400 and attempt == 0 and json_mode:       # 같은 호출을 반복해도 또 실패한다(실측) → JSON 모드만 끄고 같은 모델로 다시(파서가 코드펜스·군더더기를 걷어낸다)
                        json_mode = False
                        continue
                    break                                  # 404(단종)·5xx·두 번째 400/429 → 다음 모델
        raise NarrativeError(f"Groq 호출에 실패했습니다: {last}")
    return call
