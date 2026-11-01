# -*- coding: utf-8 -*-
"""
투자 분석 사이트 3곳(버핏연구소 · Stock Issue Service · SMIC)의 글을 모아 언급 종목을 뽑고, 핵심을 요약하고,
글이 나온 뒤 주가가 어떻게 움직였는지(영향도)를 계산하는 streamlit 비의존 모듈.

설계
- 수집은 robots.txt가 허용하는 공개 목록/글 페이지만, 요청 간 1초 간격으로 한다. 원문은 저장하지 않고 제목·날짜·링크와
  짧은 요약만 남긴다(요약은 규칙 기반이 기본, GROQ_API_KEY가 있으면 LLM 보강). 화면에는 항상 원문 링크를 함께 보여준다.
- HTML 파서(parse_*)는 문자열만 받는 순수 함수라 네트워크 없이 테스트한다.
- 종목 매칭: 글 제목의 첫 종목명을 주 종목으로, 제목 안의 다른 상장사명을 관련 종목으로 본다(가장 긴 이름 우선, 겹침 제외).
  본문의 '이름(6자리코드)'가 있으면 그 코드를 우선한다.
- 영향도: 공표일을 t0(주말이면 다음 거래일)로 보고 ① 공표 전 5·20일 수익률(글이 이미 오른 뒤 나오는지) ② 공표일 당일 수익률(d0)
  ③ 공표일 종가 이후 1·5·20일 수익률을 지수(코스피/코스닥) 대비 초과수익으로 계산한다. 글 시각을 알 수 없어 d0은 글을 읽고
  따라 살 수 있는 수익이 아니다 — 읽고 난 뒤의 수익은 post(공표일 종가 이후) 값이다.
"""
import io
import json
import os
import re
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import quote, urljoin

import numpy as np
import pandas as pd
import requests
from bs4 import BeautifulSoup

KST = timezone(timedelta(hours=9))
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
REQUEST_DELAY = 1.0

SOURCES = {
    "buffett": {"name": "버핏연구소", "list_url": "https://buffettlab.co.kr/news/list.php?mcode=m331evx&page={page}",
                "home": "https://buffettlab.co.kr/"},
    "eduinfom": {"name": "Stock Issue Service", "list_url": "https://finance.eduinfom.com/category/%ec%a3%bc%ea%b0%80%eb%b6%84%ec%84%9d/page/{page}/",
                 "home": "https://finance.eduinfom.com/"},
    "smic": {"name": "SMIC", "list_url": "http://snusmic.com/research/page/{page}/", "home": "http://snusmic.com/research/"},
}
# robots.txt에서 막힌 경로(2026-11-01 확인) — 수집기가 이 접두사로 시작하는 경로는 요청하지 않는다
ROBOTS_DISALLOW = {"buffettlab.co.kr": ("/adm/", "/cheditor5/", "/Daumeditor/"), "finance.eduinfom.com": ("/wp-json/", "/api/"),
                   "snusmic.com": ("/wp-admin/",)}
BUFFETT_TAGS = ("[버핏 리포트]", "[이슈 체크]")        # 종목 분석 글. 장마감·시황·원자재 등은 제외
GROQ_MODEL_CANDIDATES = ["openai/gpt-oss-120b", "llama-3.3-70b-versatile", "llama-3.1-8b-instant"]   # app.py와 같은 후보(가끔 갱신 필요)
LLM_MAX_CHARS = 6000


# ---------------------------------------------------------------------------
# 요청
# ---------------------------------------------------------------------------
class Fetcher:
    def __init__(self, delay=REQUEST_DELAY, timeout=30):
        self.s = requests.Session()
        self.s.headers.update({"User-Agent": UA})
        self.delay, self.timeout, self._last = delay, timeout, 0.0

    def allowed(self, url):
        from urllib.parse import urlparse
        u = urlparse(url)
        return not any(u.path.startswith(p) for p in ROBOTS_DISALLOW.get(u.netloc, ()))

    def get(self, url, binary=False, retries=3):
        if not self.allowed(url):
            return None
        for attempt in range(retries):
            wait = self.delay - (time.time() - self._last)
            if wait > 0:
                time.sleep(wait)
            self._last = time.time()
            try:
                r = self.s.get(requote(url), timeout=self.timeout)
                if r.status_code == 200:
                    if binary:
                        return r.content
                    if not r.encoding or r.encoding.lower() == "iso-8859-1":
                        r.encoding = r.apparent_encoding or "utf-8"
                    return r.text
                if r.status_code in (403, 404, 410):
                    return None
            except requests.RequestException:
                pass
            time.sleep(1.5 * (attempt + 1))
        return None


def requote(url):
    from requests.utils import requote_uri
    return requote_uri(url)


def clean(text):
    return re.sub(r"\s+", " ", text or "").strip()


# ---------------------------------------------------------------------------
# 파서 (문자열 → dict) — 네트워크 없이 테스트
# ---------------------------------------------------------------------------
def parse_buffett_list(html, base="https://buffettlab.co.kr/news/"):
    s = BeautifulSoup(html, "html.parser")
    out, seen = [], set()
    for li in s.select("li"):
        a = li.select_one("dt.title a")
        if not a:
            continue
        m = re.search(r"idx=(\d+)", a.get("href", ""))
        if not m or m.group(1) in seen:
            continue
        seen.add(m.group(1))
        date = li.select_one("dd.registDate span")
        lead = li.select_one("dd.content")
        out.append({"id": f"buffett:{m.group(1)}", "title": clean(a.get_text(" ")), "lead": clean(lead.get_text(" ")) if lead else "",
                    "date": clean(date.get_text()) if date else None, "url": f"https://buffettlab.co.kr/news/view.php?idx={m.group(1)}"})
    return out


def parse_buffett_article(html):
    s = BeautifulSoup(html, "html.parser")
    body = s.select_one("#viewContent .fr-view") or s.select_one("#viewContent")
    text = clean(body.get_text(" ")) if body else ""
    text = re.sub(r"^하루에도 수십개씩 쏟아지는 증권사 리포트.*?\[편집자주\]\s*", "", text)
    m = re.search(r"기사등록\s*(\d{4}-\d{2}-\d{2})", s.get_text(" "))
    og = s.find("meta", property="og:description")
    return {"text": text, "date": m.group(1) if m else None, "lead": clean(og.get("content")) if og and og.get("content") else ""}


def parse_eduinfom_list(html):
    s = BeautifulSoup(html, "html.parser")
    out, seen = [], set()
    for art in s.select("article.post-card") or s.find_all("article"):
        a = art.find("a", href=re.compile(r"/20\d\d/\d\d/\d+/?$"))
        if not a:
            continue
        pid = re.search(r"/(\d+)/?$", a["href"]).group(1)
        title = clean(a.get_text(" "))
        if pid in seen or len(title) < 8:
            for h in art.select("h2 a, h3 a"):
                title = clean(h.get_text(" "))
            if pid in seen or len(title) < 8:
                continue
        seen.add(pid)
        d = re.search(r"(20\d\d)\.\s*(\d{1,2})\.\s*(\d{1,2})\.", art.get_text(" "))
        out.append({"id": f"eduinfom:{pid}", "title": title, "url": urljoin("https://finance.eduinfom.com/", a["href"]),
                    "date": f"{d.group(1)}-{int(d.group(2)):02d}-{int(d.group(3)):02d}" if d else None})
    return out


def parse_eduinfom_article(html):
    s = BeautifulSoup(html, "html.parser")
    ec = s.select_one(".entry-content") or s.select_one("article")
    text = clean(ec.get_text(" ")) if ec else ""
    bullets = []
    if ec:
        node = ec.find(string=re.compile("핵심 요약"))
        box = node.find_parent("div") if node else None
        if box:
            bullets = [clean(li.get_text(" ")) for li in box.find_all("li")]
    date = None
    m = s.find("meta", property="article:published_time")
    if m and m.get("content"):
        try:
            date = datetime.fromisoformat(m["content"].replace("Z", "+00:00")).astimezone(KST).strftime("%Y-%m-%d")
        except ValueError:
            pass
    return {"text": text, "bullets": bullets, "date": date, "ai_assisted": "AI 보조" in (s.get_text(" ") or "")}


def _smic_date(text):
    m = re.search(r"(\d{1,2})월\s*(\d{1,2}),\s*(\d{4})", text or "")
    return f"{m.group(3)}-{int(m.group(1)):02d}-{int(m.group(2)):02d}" if m else None


def parse_smic_list(html):
    s = BeautifulSoup(html, "html.parser")
    out, seen = [], set()
    for a in s.find_all("a", href=re.compile(r"/equity-research-")):
        title = clean(a.get_text(" "))
        if not title.startswith("Equity Research"):
            continue
        href = a["href"]
        if href in seen:
            continue
        seen.add(href)
        box = a
        date = None
        for _ in range(4):
            box = box.parent
            date = _smic_date(box.get_text(" "))
            if date:
                break
        slug = re.sub(r"^.*/(equity-research-[^/]+)/?$", r"\1", href)
        out.append({"id": f"smic:{slug}", "title": title, "url": href, "date": date})
    return out


def parse_smic_post(html):
    s = BeautifulSoup(html, "html.parser")
    pdf = next((a["href"] for a in s.find_all("a", href=True) if ".pdf" in a["href"].lower()), None)
    return {"pdf": pdf}


def extract_pdf_text(content, max_pages=3):
    from pypdf import PdfReader
    rd = PdfReader(io.BytesIO(content))
    return clean("\n".join((rd.pages[i].extract_text() or "") for i in range(min(max_pages, len(rd.pages)))))


# ---------------------------------------------------------------------------
# 종목 추출
# ---------------------------------------------------------------------------
def build_name_map():
    """KRX 상장사 이름 → {'code', 'market'}. fdr StockListing('KRX')의 Code·Name·Market만 사용(시세 병합 컬럼은 쓰지 않음)."""
    import FinanceDataReader as fdr
    df = fdr.StockListing("KRX")[["Code", "Name", "Market"]].dropna()
    return {str(n).strip(): {"code": str(c).strip(), "market": str(m)} for c, n, m in zip(df["Code"], df["Name"], df["Market"])}


def find_names(text, name_map, max_names=4):
    """text 안의 상장사 이름을 등장 순서대로(가장 긴 이름 우선, 겹치는 짧은 이름 제외) 찾는다."""
    if not text:
        return []
    taken, found = [], []
    for name in sorted(name_map, key=len, reverse=True):
        if len(name) < 2:
            continue
        start = 0
        while True:
            i = text.find(name, start)
            if i < 0:
                break
            j = i + len(name)
            if not any(i < b and a < j for a, b in taken):
                # 영문·숫자 이름은 영문·숫자 한가운데서 끊어 맞지 않게(예: 'SK'가 'SKC'의 일부로 잡히지 않도록)
                prev_c = text[i - 1] if i > 0 else " "
                next_c = text[j] if j < len(text) else " "
                if not (name[0].isascii() and name[0].isalnum() and prev_c.isascii() and prev_c.isalnum()) and \
                        not (name[-1].isascii() and name[-1].isalnum() and next_c.isascii() and next_c.isalnum()):
                    taken.append((i, j))
                    found.append((i, name))
            start = j
    found.sort()
    seen, out = set(), []
    for _, n in found:
        if n not in seen:
            seen.add(n)
            out.append(n)
    return out[:max_names]


def subject_text(source, title):
    """제목에서 종목명이 들어 있는 부분(증권사 꼬리표·태그 제외)."""
    t = re.sub(r"^\[[^\]]+\]\s*", "", title)
    t = re.sub(r"\s+-\s+[^-]{1,12}$", "", t) if source == "buffett" else t
    t = re.sub(r"^Equity Research,\s*", "", t)
    return t


def extract_stocks(source, title, text, name_map):
    """주 종목(제목 첫 종목)과 관련 종목 목록. 본문의 '이름(6자리코드)'는 이름이 맞으면 코드를 확정한다."""
    subj = subject_text(source, title)
    names = find_names(subj, name_map)
    coded = {n: c for n, c in re.findall(r"([가-힣A-Za-z0-9&·\-]{2,20})\((\d{6})\)", (text or "")[:1500])}
    stocks = []
    for n in names:
        code = name_map[n]["code"]
        stocks.append({"code": code, "name": n, "market": name_map[n]["market"], "role": "primary" if not stocks else "related"})
    if not stocks:
        for n, c in coded.items():
            hit = next((k for k, v in name_map.items() if v["code"] == c), None)
            if hit:
                stocks.append({"code": c, "name": hit, "market": name_map[hit]["market"], "role": "primary"})
                break
    m = re.search(r"\(KRX:(\d{6})\)", text or "")
    if not stocks and m:
        hit = next((k for k, v in name_map.items() if v["code"] == m.group(1)), None)
        if hit:
            stocks.append({"code": m.group(1), "name": hit, "market": name_map[hit]["market"], "role": "primary"})
    return stocks


# ---------------------------------------------------------------------------
# 팩트(목표가·투자의견·증권사)와 논조
# ---------------------------------------------------------------------------
def parse_krw(s):
    """'84만원' '56만2000원' '5,700원' '357,400원' → 원 단위 정수. 해석할 수 없으면 None."""
    if not s:
        return None
    s = s.replace(",", "").replace(" ", "")
    m = re.fullmatch(r"(\d+)만(\d+)?원?", s)
    if m:
        return int(m.group(1)) * 10000 + (int(m.group(2)) if m.group(2) else 0)
    m = re.fullmatch(r"(\d+)원?", s)
    return int(m.group(1)) if m else None


_AMT = r"(\d[\d,]*(?:\s*만\s*\d*)?\s*원)"
OPINIONS = r"(매수|중립|보유|비중확대|비중축소|시장수익률|매도|Trading\s*Buy|Strong\s*Buy|Buy|Hold|Sell|Outperform|Neutral)"
BROKER_FULL = {"NH": "NH투자증권", "DS": "DS투자증권", "iM": "iM증권", "KB": "KB증권", "하나": "하나증권", "대신": "대신증권", "유안타": "유안타증권",
               "키움": "키움증권", "삼성": "삼성증권", "신한": "신한투자증권", "다올": "다올투자증권", "SK": "SK증권", "한투": "한국투자증권",
               "현대차": "현대차증권", "메리츠": "메리츠증권", "IBK": "IBK투자증권", "LS": "LS증권", "교보": "교보증권", "BNK": "BNK투자증권",
               "한화": "한화투자증권", "미래에셋": "미래에셋증권", "유진": "유진투자증권", "상상인": "상상인증권", "토스": "토스증권"}


def extract_facts(title, text):
    """제목+본문에서 규칙으로 뽑는 사실값. 못 찾으면 키가 없다(추측하지 않는다)."""
    blob = f"{title} {text or ''}"[:4000]
    f = {}
    m = re.search(r"\s-\s([^-\[\]]{1,14})$", title.strip())
    if m:
        f["broker"] = BROKER_FULL.get(m.group(1).strip(), m.group(1).strip())
    else:
        b = re.search(r"([가-힣A-Za-z&]{2,10}(?:투자증권|증권|투자))[은는이가]", blob)
        if b:
            f["broker"] = b.group(1)
    o = re.search(r"투자의견\s*[‘'“\"]?\s*" + OPINIONS, blob, re.I)
    if o:
        f["opinion"] = re.sub(r"\s+", " ", o.group(1)).strip()
    c = re.search(r"목표(?:주가|가)[를은는]?\s*(?:기존\s*)?" + _AMT + r"\s*(?:에서|→|->)\s*" + _AMT + r"\s*(?:으로|로)?\s*(상향|하향|조정|유지)?", blob)
    if c:
        f["target_prev"], f["target_price"] = parse_krw(c.group(1)), parse_krw(c.group(2))
        if f["target_prev"] and f["target_price"]:
            f["target_dir"] = "상향" if f["target_price"] > f["target_prev"] else ("하향" if f["target_price"] < f["target_prev"] else "유지")
    else:
        t = re.search(r"목표(?:주가|가)[는은를을]?\s*(?:을\s*)?" + _AMT, blob)
        if t:
            f["target_price"] = parse_krw(t.group(1))
        # '기존보다 4% 높인 5700원'·'기존 대비 3% 낮춘 …' 형태(이전 목표가는 없고 방향만 알 수 있다)
        d = re.search(r"기존(?:보다|\s*대비)\s*[\d.]+%\s*(높인|낮춘|상향한|하향한)", blob)
        if d:
            f["target_dir"] = "상향" if d.group(1) in ("높인", "상향한") else "하향"
        elif re.search(r"목표(?:주가|가)[를은는]?\s*(?:기존\s*)?(?:수준\s*)?유지", blob) or re.search(r"목표(?:주가|가)\s*[^.]{0,30}유지했", blob):
            f["target_dir"] = "유지"
    p = re.search(r"전일\s*종가는?\s*" + _AMT, blob)
    if p:
        f["prev_close_in_text"] = parse_krw(p.group(1))
    return {k: v for k, v in f.items() if v is not None}


POS_WORDS = ("상회", "호조", "서프라이즈", "개선", "수혜", "성장", "확대", "흑자", "돌파", "최대", "기대", "회복", "강세", "수주")
NEG_WORDS = ("하회", "부진", "악화", "둔화", "적자", "리스크", "우려", "급락", "쇼크", "감소", "약세", "지연", "손실", "부담", "경쟁")


def classify_tone(title, facts):
    """제목 키워드와 목표가 방향으로 매기는 거친 논조(긍정/중립/부정). 글 내용을 읽은 판단이 아니다."""
    if facts.get("target_dir") == "상향":
        return "긍정"
    if facts.get("target_dir") == "하향":
        return "부정"
    t = subject_text("x", title)
    score = sum(w in t for w in POS_WORDS) - sum(w in t for w in NEG_WORDS)
    return "긍정" if score >= 1 else ("부정" if score <= -1 else "중립")


# ---------------------------------------------------------------------------
# 요약
# ---------------------------------------------------------------------------
BYLINE = re.compile(r"^\s*(?:\[[^\]]{2,20}=[^\]]{1,30}\]|출처:\s*[^\s,]{2,12},?\s*\d{4}년\s*\d{1,2}월\s*\d{1,2}일)\s*")


def split_sentences(text):
    """마침표·물음표·느낌표 뒤에서만 끊는다(한국어는 '다' 뒤에도 문장이 이어지는 경우가 많다). 기자 표기는 제거."""
    text = BYLINE.sub("", clean(text))
    parts = re.split(r"(?<=[.!?])\s+", text)
    return [BYLINE.sub("", p.strip()) for p in parts if len(p.strip()) > 20]


def rule_summary(source, title, text, bullets=None, max_items=3, max_len=150):
    """LLM 없이 만드는 요약: 원 글의 핵심 요약 블록(Stock Issue Service) 또는 본문 앞 문장들.
    SMIC 보고서는 표지 문구보다 밸류에이션(목표주가) 문장을 우선 보여준다."""
    items = list(bullets or [])
    if not items:
        sents = split_sentences(text)
        if source == "smic":
            val = next((s for s in sents if "목표주가" in s), None)
            intro = [s for s in sents if s != val and s.endswith("다.") and len(s) > 40][:2]
            items = intro + ([val] if val else [])
        else:
            items = sents
    return [i[:max_len] + ("…" if len(i) > max_len else "") for i in items[:max_items]]


def llm_summary(title, text, api_key=None, models=None):
    """Groq로 3줄 요약 + 논조. 키가 없거나 호출이 실패하면 None(호출부가 규칙 요약으로 대체). 401/403은 즉시 중단."""
    api_key = api_key or os.environ.get("GROQ_API_KEY")
    if not api_key or not text:
        return None
    try:
        from groq import Groq
    except ImportError:
        return None
    prompt = (f"당신은 주식 리서치 요약가입니다. 아래는 '{title}' 글입니다. 다음 JSON만 출력하세요.\n"
              '{"summary": ["핵심 1", "핵심 2", "핵심 3"], "tone": "긍정|중립|부정", "drivers": ["주가에 영향을 줄 요인 1~2개"]}\n'
              "- 글에 없는 수치·전망·원인은 절대 지어내지 말고, 한국어로 쓰세요. 각 항목은 80자 이내.\n"
              "- tone은 글이 해당 종목 주가에 대해 취하는 논조입니다.\n\n[글]\n" + text[:LLM_MAX_CHARS])
    for model in models or GROQ_MODEL_CANDIDATES:
        for attempt in range(2):
            try:
                r = Groq(api_key=api_key).chat.completions.create(model=model, messages=[{"role": "user", "content": prompt}], temperature=0.2)
                m = re.search(r"\{.*\}", r.choices[0].message.content, re.S)
                data = json.loads(m.group(0)) if m else None
                if data and isinstance(data.get("summary"), list):
                    tone = data.get("tone") if data.get("tone") in ("긍정", "중립", "부정") else None
                    return {"summary": [str(x)[:120] for x in data["summary"][:3]], "tone": tone,
                            "drivers": [str(x)[:100] for x in (data.get("drivers") or [])[:2]]}
            except Exception as e:                      # noqa: BLE001 — 외부 API 오류는 종류가 다양하다
                if getattr(e, "status_code", None) in (401, 403):
                    return None
                time.sleep(0.5 * (attempt + 1))
    return None


# ---------------------------------------------------------------------------
# 글 레코드 만들기
# ---------------------------------------------------------------------------
def build_article(source, item, detail, name_map, use_llm=False, api_key=None):
    """목록 항목(item)과 상세(detail: parse_*_article 결과, 없을 수 있음)로 저장용 레코드를 만든다."""
    detail = detail or {}
    text = detail.get("text", "")
    title = item["title"]
    base_text = " ".join(x for x in (item.get("lead", ""), detail.get("lead", ""), text) if x)
    facts = extract_facts(title, base_text)
    stocks = extract_stocks(source, title, base_text, name_map)
    rec = {"id": item["id"], "source": source, "title": title, "url": item["url"], "date": detail.get("date") or item.get("date"),
           "kind": {"buffett": "증권사 리포트 요약", "eduinfom": "종목 이슈 분석", "smic": "리서치 보고서"}[source],
           "stocks": stocks, **facts}
    if source == "eduinfom":
        rec["ai_assisted"] = bool(detail.get("ai_assisted"))
    rec["tone"] = classify_tone(title, facts)
    summary_text = text or item.get("lead", "")
    rec["summary"] = rule_summary(source, title, summary_text, detail.get("bullets")) if summary_text else []
    rec["summary_by"] = "source" if detail.get("bullets") else ("rule" if rec["summary"] else "none")
    if use_llm and summary_text:
        llm = llm_summary(title, summary_text, api_key)
        if llm:
            rec["summary"], rec["summary_by"] = llm["summary"], "llm"
            rec["drivers"] = llm["drivers"]
            if llm["tone"] and "target_dir" not in facts:
                rec["tone"] = llm["tone"]
    rec["has_detail"] = bool(detail)
    return rec


# ---------------------------------------------------------------------------
# 주가 영향도
# ---------------------------------------------------------------------------
POST_DAYS = (1, 5, 20)


def compute_reaction(close, index_close, pub_date):
    """공표일 기준 주가 반응. close/index_close: 날짜 인덱스 종가 Series. 거래일이 모자라면 해당 값은 키에서 빠진다.
    t0 = 공표일(주말·휴일이면 다음 거래일). pre = t0 직전 종가까지의 수익률, d0 = t0 당일, post_k = t0 종가 이후 k거래일."""
    if close is None or len(close) < 30:
        return None
    close = close.dropna().astype(float).sort_index()
    pos = int(close.index.searchsorted(pd.Timestamp(pub_date)))
    if pos < 21 or pos >= len(close):
        return None
    idx = index_close.reindex(close.index, method="ffill").astype(float) if index_close is not None else None
    r = {}

    def ret(s, a, b):
        return float(s.iloc[b] / s.iloc[a] - 1)

    r["pre5"], r["pre20"] = ret(close, pos - 6, pos - 1), ret(close, pos - 21, pos - 1)
    r["d0"] = ret(close, pos - 1, pos)
    if idx is not None:
        r["abn_d0"] = r["d0"] - ret(idx, pos - 1, pos)
        r["abn_pre20"] = r["pre20"] - ret(idx, pos - 21, pos - 1)
    for k in POST_DAYS:
        if pos + k < len(close):
            r[f"post{k}"] = ret(close, pos, pos + k)
            if idx is not None:
                r[f"abn_post{k}"] = r[f"post{k}"] - ret(idx, pos, pos + k)
    return {k: round(v, 5) for k, v in r.items() if v == v}


def nw_t(x, lag):
    """평균의 Newey-West t값(시계열 기준). 표본이 20개 미만이거나 분산이 0이면 NaN."""
    x = np.asarray(pd.Series(x).dropna(), dtype=float)
    n = len(x)
    if n < 20:
        return float("nan")
    e = x - x.mean()
    v = e @ e / n
    for k in range(1, lag + 1):
        v += 2 * (1 - k / (lag + 1)) * (e[k:] @ e[:-k]) / n
    return float(x.mean() / np.sqrt(v / n)) if v > 0 else float("nan")


IMPACT_COLS = (("abn_pre20", "공표 전 20일 초과", 20), ("abn_d0", "공표 당일 초과", 1), ("abn_post1", "공표 후 1일 초과", 1),
               ("abn_post5", "공표 후 5일 초과", 5), ("abn_post20", "공표 후 20일 초과", 20))


def aggregate_impact(articles, group_keys=("source", "tone"), min_dates=30):
    """글별 반응(rec['reaction'])을 그룹별로 요약.
    같은 날 나온 글은 시장 충격을 공유하므로 날짜별 평균을 먼저 구하고, 그 날짜 시계열의 평균과 Newey-West t를 낸다(글 수로 t를 부풀리지 않는다).
    같은 종목·같은 날·같은 출처의 중복은 한 번만 센다. 날짜가 min_dates개 미만인 칸은 비운다."""
    rows = []
    for a in articles:
        rx = a.get("reaction")
        if not rx or not a.get("stocks"):
            continue
        rows.append({"source": a["source"], "tone": a.get("tone", "중립"), "target_dir": a.get("target_dir", "-"),
                     "code": a["stocks"][0]["code"], "date": a["date"], **rx})
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows).drop_duplicates(["code", "date", "source"])
    out = []
    for key, g in [("전체", df)] + [(f"{k}={v}", sub) for k in group_keys for v, sub in df.groupby(k)]:
        row = {"그룹": key, "글 수": len(g), "날짜 수": g["date"].nunique()}
        for col, label, lag in IMPACT_COLS:
            per_day = g.groupby("date")[col].mean().dropna().sort_index() if col in g else pd.Series(dtype=float)
            ok = len(per_day) >= min_dates
            row[f"{label}%"] = per_day.mean() * 100 if ok else np.nan
            row[f"{label} t"] = nw_t(per_day, lag) if ok else np.nan
        x = g["abn_post5"].dropna() if "abn_post5" in g else pd.Series(dtype=float)
        row["5일 승률%"] = (x > 0).mean() * 100 if len(x) >= 20 else np.nan
        out.append(row)
    return pd.DataFrame(out)


# ---------------------------------------------------------------------------
# 데이터 파일
# ---------------------------------------------------------------------------
def load_data(path):
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data.get("articles"), list):
            return data
    except (OSError, ValueError):
        pass
    return {"version": 1, "updated": None, "articles": []}


def _dump(obj):
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


def save_data(path, data):
    data["updated"] = datetime.now(KST).isoformat(timespec="seconds")
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(_dump(data))
    os.replace(tmp, path)


# 저장소 비대화 방지: 최근 글만 일 단위 파일(invest_sites_data.json)에 전체 필드로 두고, 그보다 오래된 글은 영향도 계산에 필요한 필드만 남겨
# 월별 샤드(invest_sites_history/YYYY-MM.json)로 보낸다. 지난 달 샤드는 내용이 같으면 다시 쓰지 않으므로 매일 커밋에 섞이지 않는다.
def slim(a):
    out = {k: a[k] for k in ("id", "source", "date", "tone", "target_dir", "reaction") if k in a}
    out["stocks"] = [{k: s[k] for k in ("code", "name", "market") if k in s} for s in (a.get("stocks") or [])[:1]]
    out["slim"] = True
    return out


def load_history(directory):
    out = []
    try:
        for name in sorted(os.listdir(directory)):
            if name.endswith(".json"):
                with open(os.path.join(directory, name), encoding="utf-8") as f:
                    out += json.load(f)
    except (OSError, ValueError):
        pass
    return out


def save_history(directory, old_articles):
    """오래된 글을 월별 샤드로 저장한다. 내용이 달라진 샤드만 다시 쓴다. 반환: 새로 쓴 샤드 수."""
    os.makedirs(directory, exist_ok=True)
    months = {}
    for a in old_articles:
        months.setdefault((a.get("date") or "0000-00")[:7], []).append(slim(a))
    written = 0
    for ym, items in months.items():
        text = _dump(sorted(items, key=lambda a: a["id"]))
        path = os.path.join(directory, f"{ym}.json")
        try:
            with open(path, encoding="utf-8") as f:
                if f.read() == text:
                    continue
        except OSError:
            pass
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
        written += 1
    return written
