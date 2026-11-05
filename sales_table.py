# -*- coding: utf-8 -*-
"""
sales_table.py — 사업보고서 본문 '매출 및 수주상황 > 매출실적' 표에서 품목별 매출을 읽는다(XBRL 주석이 없는 회사용 폴백). streamlit 비의존.

서식이 회사마다 달라(행·열 병합, 수출/내수/소계 하위 행, 달러 병기) 완벽할 수 없다. 그래서 '검증 관문'을 둔다:
  읽은 품목 매출의 합이 DART 재무제표의 매출액과 ±3% 안에 들어올 때만 채택하고, 아니면 아무것도 보여주지 않는다(틀린 숫자를 보여주느니 비운다).
읽는 법: 병합을 펼쳐 격자로 만들고 → 기간 머리글(2025년/제25기) 열을 찾고 → 왼쪽 라벨 열로 행을 묶어
  소계 행이 있으면 그 값, 없으면 수출+내수 합, 단일 행이면 그 값 → 상위 소계·합계 행은 버리고 잎(leaf) 품목만 남긴다.
"""
import html
import re
from html.parser import HTMLParser

GATE_LOW, GATE_HIGH = 0.97, 1.03
UNIT_FACTORS = (("조원", 1e12), ("억원", 1e8), ("백만원", 1e6), ("천만원", 1e7), ("만원", 1e4), ("천원", 1e3), ("원", 1.0))
TOTAL_WORDS = {"합계", "총계", "계", "총매출", "총매출액", "매출총계", "매출합계", "매출액합계", "합계(연결)"}
SUB_WORDS = {"소계"}
CHANNEL_WORDS = {"수출", "내수", "국내", "해외", "직수출", "로컬", "수출(외화)", "국내(내수)", "해외(수출)", "local", "export"}
TYPE_WORDS = {"제품", "상품", "용역", "서비스", "기타", "기타매출", "임대", "공사", "제품매출", "상품매출", "용역매출"}


class _Tables(HTMLParser):
    """DART 문서 조각 → 최상위 표 목록. 표 하나 = 행 목록, 행 = (텍스트, rowspan, colspan) 목록. 중첩 표는 바깥 칸의 텍스트로 합친다."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.tables, self.depth, self.row, self.cell = [], 0, None, None

    def handle_starttag(self, tag, attrs):
        tag = tag.upper()
        a = {k.upper(): v for k, v in attrs}
        if tag == "TABLE":
            self.depth += 1
            if self.depth == 1:
                self.tables.append([])
        elif self.depth == 1 and tag == "TR":
            self.row = []
            self.tables[-1].append(self.row)
        elif self.depth == 1 and tag in ("TD", "TH", "TE"):
            self.cell = {"text": [], "rs": _int(a.get("ROWSPAN")), "cs": _int(a.get("COLSPAN"))}
        elif tag == "BR" and self.cell is not None:
            self.cell["text"].append(" ")

    def handle_endtag(self, tag):
        tag = tag.upper()
        if tag == "TABLE":
            self.depth = max(0, self.depth - 1)
        elif self.depth == 1 and tag in ("TD", "TH", "TE") and self.cell is not None and self.row is not None:
            self.row.append((re.sub(r"\s+", " ", html.unescape("".join(self.cell["text"]))).replace("\xa0", " ").strip(), self.cell["rs"], self.cell["cs"]))
            self.cell = None

    def handle_data(self, data):
        if self.cell is not None:
            self.cell["text"].append(data)


def _int(v):
    try:
        return max(1, int(v))
    except (TypeError, ValueError):
        return 1


def parse_tables(fragment):
    p = _Tables()
    p.feed(fragment)
    p.close()
    return p.tables


def to_grid(rows):
    """행·열 병합을 펼친 2차원 문자열 격자(빈 칸은 '')."""
    occupied, grid = {}, []
    for r, row in enumerate(rows):
        c = 0
        for text, rs, cs in row:
            while (r, c) in occupied:
                c += 1
            for dr in range(rs):
                for dc in range(cs):
                    occupied[(r + dr, c + dc)] = text
            c += cs
    if not occupied:
        return []
    height, width = max(r for r, _ in occupied) + 1, max(c for _, c in occupied) + 1
    return [[occupied.get((r, c), "") for c in range(width)] for r in range(height)]


def parse_amount(text):
    """'1,234' → 1234.0, '△1,234'/'(1,234)' → -1234.0. 달러 병기('($7,397)')·비율(%)·빈 칸은 None."""
    t = (text or "").replace(",", "").replace(" ", "")
    if not t or t in ("-", "–", "—") or "$" in t or "%" in t or re.search(r"[A-Za-z가-힣]", t.replace("△", "")):
        return None
    neg = t.startswith(("△", "▲", "-", "(")) and not t.startswith("(-")
    m = re.search(r"\d+(?:\.\d+)?", t)
    if not m:
        return None
    v = float(m.group())
    return -v if neg else v


def find_unit(text_before):
    """표 앞쪽의 '(단위: L, Gal, 백만원, 천달러)'에서 원화 단위 배율. 못 찾으면 None."""
    for m in re.finditer(r"단위\s*[:：]?\s*([^)\]<]{1,60})", text_before):
        for name, factor in UNIT_FACTORS:
            if name in m.group(1):
                return factor
    return None


def _norm(s):
    return re.sub(r"\s+", "", s or "")


def period_columns(grid):
    """머리글 행과 (당기 열, 전기 열). 기간 라벨이 2개 이상 있는 첫 행에서 연도(없으면 '제N기')가 큰 순으로 정한다."""
    for r, row in enumerate(grid[:4]):
        cols = []
        for c, cell in enumerate(row):
            if c and cell == row[c - 1]:           # 병합으로 펼쳐진 같은 칸은 한 번만
                continue
            m = re.search(r"(20\d{2})\s*년", cell) or re.search(r"제\s*(\d+)\s*기", cell)
            if m:
                cols.append((int(m.group(1)), c))
        if len(cols) >= 2:
            ordered = sorted(cols, reverse=True)
            return r, ordered[0][1], ordered[1][1], min(c for _, c in cols)
    return None


def classify_row(texts):
    """라벨 경로 → (종류, 키). 종류: grand(표 전체 합계)·sub(상위 경로의 소계/합계)·chan(수출/내수 분해)·item(단일)·ignore(접두어 붙은 소계)."""
    norm = [_norm(t) for t in texts]
    if norm[0] in TOTAL_WORDS:
        return "grand", ()
    for i, n in enumerate(norm):
        if i > 0 and (n in SUB_WORDS or n in TOTAL_WORDS):
            return "sub", tuple(dict.fromkeys(x for x in norm[:i] if x))
        if n.endswith(("소계", "합계")) and n not in SUB_WORDS | TOTAL_WORDS:
            return "ignore", ()                          # '소프트웨어매출 소계'처럼 앞 품목들의 합 → 품목이 아니다
    if norm[-1] in CHANNEL_WORDS:
        return "chan", tuple(dict.fromkeys(x for x in norm[:-1] if x))
    return "item", tuple(dict.fromkeys(x for x in norm if x))


def read_items(grid, header_row, cur_col, prev_col, first_value_col, drop_equal_sums=False):
    """격자 → ([{'label','cur','prev'}] 잎 품목, 표의 합계 행 값). 모호하면 (None, total).
    drop_equal_sums: 앞선 연속 품목 둘 이상의 합과 같은 항목(소계)을 버린다 — 검증 관문을 못 넘었을 때만 켠다."""
    label_cols = list(range(first_value_col))
    groups, order, total = {}, [], None
    for r in range(header_row + 1, len(grid)):
        texts = [t for t in (grid[r][c] for c in label_cols) if t]
        if not texts:
            continue
        cur = parse_amount(grid[r][cur_col])
        prev = parse_amount(grid[r][prev_col]) if prev_col is not None else None
        if cur is None:
            continue                                      # 달러 병기·빈 칸 행은 건너뛴다
        kind, key = classify_row(texts)
        leaf_is_chan = _norm(texts[-1]) in CHANNEL_WORDS
        if kind == "grand":
            if not leaf_is_chan:                          # 합계의 수출/내수 분해 행은 쓰지 않고 소계·단일 행만 합계로
                total = (cur, prev)
            continue
        if kind == "ignore" or not key:
            continue
        display = tuple(dict.fromkeys(t for t in texts[:len(key)] if t)) if kind in ("sub", "chan") else tuple(dict.fromkeys(texts))
        g = groups.setdefault(key, {"display": display, "sub": None, "chan": [], "single": []})
        if key not in order:
            order.append(key)
        if kind == "sub":
            if leaf_is_chan:
                g["chan"].append((cur, prev))
            else:
                g["sub"] = (cur, prev)
        elif kind == "chan":
            g["chan"].append((cur, prev))
        else:
            g["single"].append((cur, prev))
    items = []
    for key in order:
        if any(len(o) > len(key) and o[:len(key)] == key for o in order):
            continue                                      # 하위 품목이 따로 있는 상위 소계는 버린다(이중 계산 방지)
        g = groups[key]
        if g["sub"] is not None:
            cur, prev = g["sub"]
        elif g["chan"]:
            cur = sum(c for c, _ in g["chan"])
            prev = sum(p for _, p in g["chan"]) if all(p is not None for _, p in g["chan"]) else None
        elif len(g["single"]) == 1:
            cur, prev = g["single"][0]
        else:
            return None, total                            # 같은 이름 행이 여럿인데 합칠 근거가 없다 → 읽기 포기
        if cur:
            items.append({"label": _label(g["display"]), "cur": cur, "prev": prev, "path": g["display"]})
    if drop_equal_sums:
        items = _drop_subtotals(items)
    _unique_labels(items)
    return items, total


def _drop_subtotals(items):
    keep = []
    for k, it in enumerate(items):
        run, acc, hit = 0.0, 0, False
        for back in range(k - 1, -1, -1):
            run += items[back]["cur"]
            acc += 1
            if acc >= 2 and abs(run - it["cur"]) <= max(1.0, abs(it["cur"]) * 0.005):
                hit = True
                break
        if not hit:
            keep.append(it)
    return keep


def _label(display):
    """품목명. 매출유형 단어(제품·상품·용역…)나 두 글자 이하 이름은 앞 칸과 이어 붙여 알아볼 수 있게 한다."""
    parts = list(display)
    last = parts[-1]
    if len(parts) > 1 and (_norm(last) in TYPE_WORDS or len(_norm(last)) <= 2):
        return f"{parts[-2]} · {last}"
    return last


def _unique_labels(items):
    seen = {}
    for it in items:
        seen[it["label"]] = seen.get(it["label"], 0) + 1
    for it in items:
        if seen[it["label"]] > 1:
            it["label"] = " · ".join(it["path"])


def section_slice(doc):
    """'매출 및 수주상황' 절 본문. 없으면 ''."""
    m = re.search(r"<TITLE[^>]*>[^<]*매출\s*및\s*수주\s*상황", doc)
    if not m:
        return ""
    start = m.start()
    nxt = doc.find("<TITLE", m.end())
    return doc[start: nxt if nxt > 0 else start + 200000]


def parse_sales(doc, revenue_won):
    """사업보고서 본문 → {'items','unit','sum_won','coverage','prev_ok'} 또는 None(못 읽었거나 검증 관문 불통과)."""
    sec = section_slice(doc)
    m = re.search(r"매출\s*실적", sec)
    if not m or not revenue_won:
        return None
    sub = sec[m.start():]
    stop = re.search(r"<P[^>]*>\s*나\s*\.", sub)
    if stop:
        sub = sub[:stop.start()]
    unit = find_unit(sub[:1500])
    best = None
    for rows in parse_tables(sub):
        grid = to_grid(rows)
        head = period_columns(grid) if grid else None
        if not head:
            continue
        header_row, cur_col, prev_col, first_val = head
        tbl_unit = unit or next((find_unit(c) for row in grid[:2] for c in row if "단위" in c and find_unit(c)), None)
        if not tbl_unit:
            continue
        for drop in (False, True):                       # 먼저 있는 그대로, 관문을 못 넘으면 소계 의심 항목을 빼고 한 번 더
            items, total = read_items(grid, header_row, cur_col, prev_col, first_val, drop_equal_sums=drop)
            if not items:
                break
            total_cur = sum(i["cur"] for i in items)
            cov = total_cur * tbl_unit / revenue_won
            if GATE_LOW <= cov <= GATE_HIGH:
                break
            best = best or {"rejected": round(cov, 3)}
        else:
            continue
        if not items or not (GATE_LOW <= cov <= GATE_HIGH):
            continue
        if any(i["cur"] < 0 for i in items):                 # 음수 행은 '부문 간 내부거래 제거' 같은 조정 행 → 품목 구성표가 아니다
            best = best or {"rejected": "negative"}
            continue
        prev_vals = [i["prev"] for i in items]
        prev_ok = all(p is not None for p in prev_vals)
        total_prev = sum(prev_vals) if prev_ok else None
        for i in items:
            i["cur"], i["prev"] = i["cur"] * tbl_unit, (i["prev"] * tbl_unit if prev_ok else None)
            i.pop("path", None)
        items.sort(key=lambda i: -i["cur"])
        basis = total_cur * tbl_unit
        for i in items:
            i["share"] = i["cur"] / basis * 100
        return {"items": items, "unit": tbl_unit, "sum_won": basis, "coverage": cov, "prev_ok": prev_ok,
                "prev_sum_won": total_prev * tbl_unit if prev_ok else None}
    return best if best and "rejected" in best else None
