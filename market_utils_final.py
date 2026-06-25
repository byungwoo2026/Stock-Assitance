import re


def format_index_change_value(change_text):
    """지수 변화 문자열을 '+/- 수치 상승/하락' 형식으로 변환한다."""
    if not change_text:
        return ""

    text = change_text.strip()
    lowered = text.lower()

    is_down = '하락' in text or 'down' in lowered or '하락세' in text
    is_up = '상승' in text or 'up' in lowered or '상승세' in text

    matches = list(re.finditer(r'([+-]?\d+(?:\.\d+)?)(\s*%)?', text))
    if not matches:
        return ""

    chosen = None
    for match in reversed(matches):
        raw = match.group(0)
        if '%' in raw or match.group(1).startswith(('+', '-')):
            chosen = match
            break

    if chosen is None:
        chosen = matches[-1]

    number = chosen.group(1)
    suffix = '%' if '%' in chosen.group(0) else ''
    clean_number = number.lstrip('+').lstrip('-')

    if is_down or number.startswith('-'):
        return f'-{clean_number}{suffix} 하락'
    if is_up or number.startswith('+'):
        return f'+{clean_number}{suffix} 상승'

    return f'{clean_number}{suffix}'
