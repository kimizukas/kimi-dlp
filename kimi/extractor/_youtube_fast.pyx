# cython: language_level=3
# cython: boundscheck=False
# cython: wraparound=False
# cython: initializedcheck=False
# cython: cdivision=True

"""Optional Cython helpers used by kimi/extractor/youtube.py."""

def walk_dicts(object data):
    cdef list stack = [data]
    cdef object item
    cdef list values
    cdef Py_ssize_t i
    while stack:
        item = stack.pop()
        if isinstance(item, dict):
            yield item
            values = list(item.values())
            for i in range(len(values) - 1, -1, -1):
                stack.append(values[i])
        elif isinstance(item, list):
            for i in range(len(item) - 1, -1, -1):
                stack.append(item[i])


def text_value(object value):
    cdef object simple, runs, item, part
    cdef list parts
    if isinstance(value, str):
        return value or None
    if not isinstance(value, dict):
        return None
    simple = value.get("simpleText")
    if isinstance(simple, str):
        return simple or None
    runs = value.get("runs")
    if not isinstance(runs, list):
        return None
    parts = []
    for item in runs:
        if not isinstance(item, dict):
            continue
        part = item.get("text", "")
        if isinstance(part, str):
            parts.append(part)
    result = "".join(parts).strip()
    return result or None


def parse_content_range(object value):
    cdef object parts, span, total_text
    cdef long long start, end, total
    if not isinstance(value, str):
        return None
    value = value.strip()
    if not value.lower().startswith("bytes "):
        return None
    try:
        parts = value[6:].split("/", 1)
        if len(parts) != 2:
            return None
        span, total_text = parts
        start_text, end_text = span.split("-", 1)
        start = int(start_text)
        end = int(end_text)
        if start < 0 or end < start:
            return None
        if total_text == "*":
            return start, end, None
        total = int(total_text)
        if total <= 0 or end >= total:
            return None
        return start, end, total
    except (TypeError, ValueError, OverflowError):
        return None


def parse_unsatisfied_range(object value):
    cdef long long total
    if not isinstance(value, str):
        return None
    value = value.strip()
    if not value.lower().startswith("bytes */"):
        return None
    try:
        total = int(value[8:])
        return total if total >= 0 else None
    except (TypeError, ValueError, OverflowError):
        return None
