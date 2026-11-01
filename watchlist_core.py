# -*- coding: utf-8 -*-
"""
관심종목 그룹의 구조·검증·JSON 직렬화(streamlit 비의존 → 테스트 가능).

Streamlit Cloud는 디스크가 휘발성이라 관심종목은 session_state에만 두고, 사용자가 JSON으로
내려받아 보관했다가 다시 불러오는 방식으로 저장한다. 외부에서 온 파일이므로 불러올 때 항상 normalize()로
구조·값을 검증/정리한다(코드 형식, 중복, 음수 평단가 등).

스키마
    {"version": 1,
     "groups": {"그룹이름": [{"code": "005490", "name": "POSCO홀딩스",
                              "buy_price": 300000.0 | null, "qty": 10 | null, "memo": ""}]}}
"""
import json
import math
import re

SCHEMA_VERSION = 1
DEFAULT_GROUP = "기본"
MAX_PER_GROUP = 50        # 일괄 진단은 종목당 시세·수급 조회가 필요해 그룹당 상한을 둔다
MAX_GROUPS = 20
MAX_MEMO_LEN = 200


def empty_watchlist():
    return {"version": SCHEMA_VERSION, "groups": {DEFAULT_GROUP: []}}


def normalize_code(value):
    """숫자만 있으면 6자리로 0을 채우고(005490), 영숫자 6자리(신규 종목코드)도 허용."""
    s = str(value).strip().upper()
    if s.isdigit():
        s = s.zfill(6)
    if not re.fullmatch(r"[0-9A-Z]{6}", s):
        raise ValueError(f"종목코드 형식이 올바르지 않습니다: {value!r}")
    return s


def _positive_float(value):
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) and v > 0 else None


def _positive_int(value):
    v = _positive_float(value)
    return int(v) if v is not None else None


def normalize_item(raw):
    if not isinstance(raw, dict):
        raise ValueError("종목 항목이 객체가 아닙니다.")
    code = normalize_code(raw.get("code"))
    return {
        "code": code,
        "name": str(raw.get("name") or code).strip() or code,
        "buy_price": _positive_float(raw.get("buy_price")),
        "qty": _positive_int(raw.get("qty")),
        "memo": str(raw.get("memo") or "")[:MAX_MEMO_LEN],
    }


def normalize(data):
    """외부 입력(dict) → 검증된 관심종목. 구조가 틀리면 ValueError(사용자에게 보여줄 한국어 메시지).
    개별 종목 항목의 오류(잘못된 코드 등)는 파일 전체를 거부하지 않고 그 항목만 건너뛴다."""
    if not isinstance(data, dict) or not isinstance(data.get("groups"), dict):
        raise ValueError("관심종목 파일 형식이 아닙니다(최상위에 'groups' 객체가 필요).")
    if len(data["groups"]) > MAX_GROUPS:
        raise ValueError(f"그룹은 최대 {MAX_GROUPS}개까지 지원합니다.")
    groups = {}
    for gname, items in data["groups"].items():
        if not isinstance(items, list):
            raise ValueError(f"그룹 '{gname}'의 종목 목록이 배열이 아닙니다.")
        seen, clean = set(), []
        for raw in items:
            try:
                item = normalize_item(raw)
            except ValueError:
                continue
            if item["code"] in seen:
                continue
            seen.add(item["code"])
            clean.append(item)
        groups[str(gname).strip() or DEFAULT_GROUP] = clean[:MAX_PER_GROUP]
    if not groups:
        groups[DEFAULT_GROUP] = []
    return {"version": SCHEMA_VERSION, "groups": groups}


def dumps(wl):
    return json.dumps(wl, ensure_ascii=False, indent=2)


def loads(text):
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        raise ValueError(f"JSON을 읽을 수 없습니다: {e}") from e
    return normalize(data)


# --- 편집 헬퍼 (wl을 제자리에서 수정) -------------------------------------------------
def add_stock(wl, group, code, name):
    """추가되면 True, 이미 있거나 그룹이 가득 차면 False."""
    items = wl["groups"][group]
    code = normalize_code(code)
    if any(i["code"] == code for i in items) or len(items) >= MAX_PER_GROUP:
        return False
    items.append(normalize_item({"code": code, "name": name}))
    return True


def remove_stocks(wl, group, codes):
    codes = set(codes)
    wl["groups"][group] = [i for i in wl["groups"][group] if i["code"] not in codes]


def update_item(item, buy_price=None, qty=None, memo=None):
    """편집 표의 값(NaN·빈값 포함)을 검증해 항목에 반영."""
    item["buy_price"] = _positive_float(buy_price)
    item["qty"] = _positive_int(qty)
    item["memo"] = str(memo or "")[:MAX_MEMO_LEN]


def add_group(wl, name):
    name = str(name).strip()
    if not name or name in wl["groups"] or len(wl["groups"]) >= MAX_GROUPS:
        return False
    wl["groups"][name] = []
    return True


def delete_group(wl, name):
    """마지막 하나 남은 그룹은 지우지 않는다."""
    if name not in wl["groups"] or len(wl["groups"]) <= 1:
        return False
    del wl["groups"][name]
    return True
