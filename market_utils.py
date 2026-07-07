# -*- coding: utf-8 -*-
import re


def format_index_change_value(change_text):
    """지수 변화 문자열을 '상승/하락/보합 + 수치' 형식으로 변환한다."""
    if not change_text:
        return ""

    text = change_text.strip()
    lowered = text.lower()

    if re.search(r"하락|down|하락세", text, flags=re.I):
        direction = "하락"
    elif re.search(r"상승|up|상승세", text, flags=re.I):
        direction = "상승"
    elif re.search(r"보합|flat|유지|unchanged", text, flags=re.I):
        direction = "보합"
    else:
        direction = ""

    matches = list(re.finditer(r"([+-]?\d+(?:\.\d+)?)(\s*%)?", text))
    if not matches:
        return direction or ""

    chosen = None
    for match in reversed(matches):
        raw = match.group(0)
        if "%" in raw or match.group(1).startswith(("+", "-")):
            chosen = match
            break

    if chosen is None:
        chosen = matches[-1]

    number = chosen.group(1)
    suffix = "%" if "%" in chosen.group(0) else ""

    if direction:
        return f"{direction} {number}{suffix}"
    return f"{number}{suffix}"
