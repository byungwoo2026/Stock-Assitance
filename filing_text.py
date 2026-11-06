# -*- coding: utf-8 -*-
"""
filing_text.py — 사업보고서 원문(DART document.xml 이어붙인 텍스트)에서 서술 섹션을 뽑는다. streamlit·네트워크 비의존.

3단계(LLM 서술)의 재료 중 '공시 원문 발췌(Q)'를 만든다. LLM에겐 이 발췌와 숫자 사실(F)만 주고, 인용은 코드가 원문과 대조한다.
- 원문 zip에는 사업보고서·감사보고서·연결감사보고서 XML이 순서 없이 섞여 있어 `<?xml` 단위로 나눠 문서 종류를 구분한다.
- 사업보고서는 `<SECTION-n><TITLE>` 구조가 표준(II. 사업의 내용 > 1~7, IV. 이사의 경영진단 및 분석의견 > 1~6, XI. > 우발부채·제재 등)이라
  제목으로 구간을 잡는다. 제목이 다른 업종(은행·보험 등)은 못 찾은 섹션을 조용히 건너뛴다 — 비어 있는 것이 틀린 것보다 낫다.
- 표는 `칸 | 칸` 행으로 펴서 본문에 남긴다(숫자 서술이 표에만 있는 경우가 많다).
"""
import html
import re
from html.parser import HTMLParser

# (키, 화면 이름, 상위 제목 정규식, 제목 정규식) — 제목은 공백을 지운 뒤 앞 번호('1.'·'IV.')도 뗀 문자열에 적용한다
SECTION_SPECS = (
    ("overview", "사업의 개요", r"사업의내용", r"사업의개요"),
    ("products", "주요 제품 및 서비스", r"사업의내용", r"주요제품및서비스"),
    ("risk_mgmt", "위험관리 및 파생거래", r"사업의내용", r"위험관리및파생거래"),
    ("other_ref", "기타 참고사항", r"사업의내용", r"기타참고사항"),
    ("mda", "재무상태 및 영업실적(경영진단)", r"이사의경영진단및분석의견", r"재무상태및영업실적"),
    ("liquidity", "유동성 및 자금조달과 지출", r"이사의경영진단및분석의견", r"유동성및자금조달과지출"),
    ("contingent", "우발부채 등에 관한 사항", r"그밖에투자자보호", r"우발부채"),
    ("sanctions", "제재 등과 관련된 사항", r"그밖에투자자보호", r"제재등"),
    ("subsequent", "작성기준일 이후 발생한 주요사항", r"그밖에투자자보호", r"작성기준일이후"),
)
SECTION_LABELS = {k: label for k, label, _, _ in SECTION_SPECS}
# 경영진단 절은 앞쪽이 재무상태 표(숫자는 DART 재무에서 이미 가져옴)라 서술만 남기는 편이 LLM 재료로 낫다
NARRATIVE_ONLY = {"mda", "liquidity"}

_HEAD = re.compile(r"<SECTION-(\d)\b[^>]*>\s*<TITLE\b[^>]*>(.*?)</TITLE>", re.S)
_DOC_NAME = re.compile(r"<DOCUMENT-NAME\b[^>]*>(.*?)</DOCUMENT-NAME>", re.S)
_BLOCK_TAGS = {"P", "BR", "TITLE", "LIBRARY", "PGBRK", "TABLE-GROUP", "COVER-TITLE"}


class _Text(HTMLParser):
    """DART XML 조각 → 줄 단위 텍스트. 표는 행마다 한 줄, 칸은 ' | '로 잇는다(중첩 표는 바깥 칸에 합친다)."""

    def __init__(self, tables=True):
        super().__init__(convert_charrefs=True)
        self.out, self.keep_tables = [], tables
        self.depth, self.rows, self.row, self.cell = 0, [], None, None

    def _emit(self, s):
        (self.cell if self.cell is not None else self.out).append(s)

    def handle_starttag(self, tag, attrs):
        tag = tag.upper()
        if tag == "TABLE":
            self.depth += 1
            if self.depth == 1:
                self.rows, self.row, self.cell = [], None, None
            else:
                self._emit(" ")
        elif self.depth == 1 and tag == "TR":
            self.row = []
        elif self.depth == 1 and tag in ("TD", "TH", "TE"):
            self.cell = []
        elif self.depth > 1 and tag in ("TD", "TH", "TE", "TR"):
            self._emit(" ")
        elif tag in _BLOCK_TAGS or tag.startswith("SECTION-"):
            self._emit(" " if self.cell is not None else "\n")

    def handle_endtag(self, tag):
        tag = tag.upper()
        if tag == "TABLE":
            if self.depth == 1:
                self._flush_table()
            self.depth = max(0, self.depth - 1)
        elif self.depth == 1 and tag in ("TD", "TH", "TE") and self.cell is not None:
            if self.row is None:
                self.row = []
            self.row.append(_squash("".join(self.cell)))
            self.cell = None
        elif self.depth == 1 and tag == "TR" and self.row is not None:
            self.rows.append(self.row)
            self.row = None
        elif tag in _BLOCK_TAGS and self.cell is None:
            self.out.append("\n")

    def handle_data(self, data):
        self._emit(data)

    def _flush_table(self):
        if self.keep_tables:
            self.out.append("\n")
            for row in self.rows:
                cells = []
                for c in row:                              # 병합으로 같은 값이 연달아 나온 칸은 한 번만
                    if c and (not cells or cells[-1] != c):
                        cells.append(c)
                if cells:
                    self.out.append(" | ".join(cells) + "\n")
        self.rows, self.row, self.cell = [], None, None


def _squash(s):
    return re.sub(r"\s+", " ", html.unescape(s)).replace("\xa0", " ").strip()


def to_text(fragment, tables=True):
    """XML 조각 → 읽을 수 있는 텍스트(문단마다 줄바꿈, 빈 줄 제거)."""
    p = _Text(tables)
    p.feed(fragment)
    p.close()
    lines = (_squash(x) for x in "".join(p.out).split("\n"))
    return "\n".join(x for x in lines if x)


def norm_title(raw):
    """'II. 사업의 내용'·'1. 사업의 개요' → '사업의내용'·'사업의개요'."""
    t = re.sub(r"\s+", "", re.sub(r"<[^>]+>", "", html.unescape(raw)))
    return re.sub(r"^(?:[IVX]+|\d+(?:-\d+)?)\.", "", t)


def split_documents(raw):
    """zip을 이어붙인 원문 → {'business': 사업보고서 XML, 'audit': [감사보고서 XML…(연결 먼저)]}."""
    business, audits = "", []
    for part in re.split(r"(?=<\?xml\b)", raw):
        if not part.strip():
            continue
        m = _DOC_NAME.search(part)
        name = re.sub(r"\s+", "", m.group(1)) if m else ""
        if name.endswith("사업보고서"):
            if len(part) > len(business):                  # 정정 등으로 둘 이상이면 큰 쪽
                business = part
        elif "감사보고서" in name:
            audits.append((name.startswith("연결"), part))
    audits.sort(key=lambda x: not x[0])
    return {"business": business, "audit": [p for _, p in audits]}


def headings(xml):
    """[(레벨, 정규화 제목, 원 제목, 시작, 본문 시작)] — 문서 순서."""
    return [(int(m.group(1)), norm_title(m.group(2)), _squash(re.sub(r"<[^>]+>", "", m.group(2))), m.start(), m.end())
            for m in _HEAD.finditer(xml)]


def _span_end(heads, i, xml_len):
    level = heads[i][0]
    for j in range(i + 1, len(heads)):
        if heads[j][0] <= level:
            return heads[j][3]
    return xml_len


def extract_sections(raw, specs=SECTION_SPECS, tables=True):
    """{키: {'label','title','text'}} — 찾은 섹션만. 같은 제목이 둘 이상이면 첫 번째(본 보고서 쪽)를 쓴다."""
    xml = split_documents(raw)["business"] or raw
    heads = headings(xml)
    out = {}
    for key, label, parent_pat, title_pat in specs:
        parent = None
        for i, (level, title, orig, start, body) in enumerate(heads):
            if level == 1:
                parent = title
                continue
            if level != 2 or not parent or not re.search(parent_pat, parent) or not re.search(title_pat, title):
                continue
            text = to_text(xml[body:_span_end(heads, i, len(xml))], tables and key not in NARRATIVE_ONLY)
            if text:
                out[key] = {"label": label, "title": orig, "text": text}
            break
    return out


# --- 핵심감사사항(KAM) ----------------------------------------------------------------------------
_KAM_END = re.compile(r"(?:연결)?재무제표에\s*대한\s*(?:경영진과\s*)?(?:지배기구의\s*)?책임|감사인의\s*책임|기타사항|내부회계관리제도")


_KAM_INTRO = re.compile(r"핵심감사사항은\s*우리의\s*전문가적\s*판단.{0,500}?(?:않|아닙)\s*습?\s*니\s*다\s*\.?", re.S)
AUDIT_HEAD_CHARS = 250_000     # 감사의견·핵심감사사항은 감사보고서 맨 앞 — 뒤의 재무제표·주석 표는 변환하지 않는다(속도)


def extract_kam(raw):
    """연결감사보고서(없으면 별도)의 '핵심감사사항' 서술. 제목 줄이 다음 문장에 붙어 나오는 서식이 있어 고정 문구('핵심감사사항은 우리의 전문가적 판단…')를 기준으로 삼는다."""
    for xml in split_documents(raw)["audit"]:
        text = to_text(xml[:AUDIT_HEAD_CHARS], tables=False)
        intro = _KAM_INTRO.search(text)
        if not intro:
            continue
        body = text[intro.end():]
        end = _KAM_END.search(body, 100)
        body = (body[:end.start()] if end else body).strip()
        if len(body) > 150:
            return {"label": "핵심감사사항(감사인이 가장 유의적이라 본 사항)", "text": body}
    return None


def extract_all(raw, tables=True):
    """사업보고서 서술 섹션 + 핵심감사사항."""
    out = extract_sections(raw, tables=tables)
    kam = extract_kam(raw)
    if kam:
        out["kam"] = kam
    return out


SECTION_CACHE_CHARS = 30_000     # 한 섹션을 캐시에 남기는 최대 글자(기타 참고사항처럼 수만 자짜리가 있음 — 발췌는 앞쪽·키워드 위주라 충분)


def fetch_sections(rcept_no):
    """접수번호 → extract_all 결과. 원문(수 MB)은 디스크에 남기지 않고 뽑은 섹션만 dart_cache/sect_<접수번호>.json에 보관한다.
    일시 오류(다운로드 실패)는 예외로 올려 캐시에 굳히지 않는다. 섹션이 하나도 없으면(형식이 다른 보고서) 빈 dict를 캐시한다."""
    import json
    import os

    import dart_core
    path = os.path.join(dart_core.CACHE_DIR, f"sect_{rcept_no}.json")
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        pass
    out = extract_all(dart_core.download_document(rcept_no, cache=False))
    for v in out.values():
        v["text"] = v["text"][:SECTION_CACHE_CHARS]
    try:
        os.makedirs(dart_core.CACHE_DIR, exist_ok=True)
        tmp = f"{path}.{os.getpid()}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False)
        os.replace(tmp, path)
    except OSError:
        pass
    return out


# --- 대조·자르기 ----------------------------------------------------------------------------------
def norm_match(s):
    """인용 대조용: 공백·따옴표 종류·가운뎃점 변형을 지운 문자열."""
    s = html.unescape(s or "")
    s = re.sub(r"[\s​﻿]+", "", s)
    return re.sub(r"[“”‘’\"'`]", "", s)


def contains_quote(text, quote):
    q = norm_match(quote)
    return len(q) >= 8 and q in norm_match(text)


def clip(text, max_chars):
    """max_chars를 넘으면 줄(문단) 경계에서 자른다. 어디서 잘렸는지 표시하지 않는다(발췌 ID 대조에는 영향 없음)."""
    if len(text) <= max_chars:
        return text
    cut = text.rfind("\n", 0, max_chars)
    return text[:cut if cut > max_chars * 0.6 else max_chars].rstrip()
