import re


def format_index_change_value(change_text):
    """지수 변화 문자열을 '상승/하락/보합 + 수치' 형식으로 변환한다."""
    if not change_text:
        return ""

    text = change_text.strip()
    lowered = text.lower()

    if "하락" in text or "down" in lowered or "하락세" in text:
        direction = "하락"
    elif "상승" in text or "up" in lowered or "상승세" in text:
        direction = "상승"
    elif "보합" in text or "flat" in lowered or "유지" in text or "unchanged" in lowered:
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
