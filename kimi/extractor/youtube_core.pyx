
# cython: language_level=3
# cython: boundscheck=False
# cython: wraparound=False
# cython: initializedcheck=False
# cython: cdivision=True

from libc.stdint cimport int64_t
from libc.string cimport memcmp

cdef inline int _digit(unsigned char value) noexcept:
    return value >= 48 and value <= 57


cdef inline int _valid_video_char(unsigned char value) noexcept:
    return (
        (value >= 48 and value <= 57) or
        (value >= 65 and value <= 90) or
        (value >= 97 and value <= 122) or
        value == 45 or
        value == 95
    )


cpdef bint is_video_id(object value):
    """Validate an 11-character YouTube video ID."""
    cdef bytes raw

    if not isinstance(value, str):
        return False

    if len(value) != 11:
        return False

    try:
        raw = value.encode("ascii")
    except UnicodeEncodeError:
        return False

    cdef Py_ssize_t i

    for i in range(11):
        if not _valid_video_char(<unsigned char>raw[i]):
            return False

    return True


cpdef object parse_content_range(object value):
    """
    Parse:
        bytes 0-1023/12345
        bytes 0-1023/*
    Return:
        (start, end, total)
    """
    cdef str text
    cdef Py_ssize_t i, n
    cdef int64_t start = 0
    cdef int64_t end = 0
    cdef int64_t total = 0
    cdef bint has_total = False
    cdef bint has_digit = False

    if not isinstance(value, str):
        return None

    text = value.strip()

    if len(text) < 8:
        return None

    if text[:6].lower() != "bytes ":
        return None

    i = 6
    n = len(text)

    while i < n and text[i] == " ":
        i += 1

    has_digit = False

    while i < n and _digit(ord(text[i])):
        has_digit = True
        start = start * 10 + (ord(text[i]) - 48)
        if start > 9223372036854775807:
            return None
        i += 1

    if not has_digit or i >= n or text[i] != "-":
        return None

    i += 1
    has_digit = False

    while i < n and _digit(ord(text[i])):
        has_digit = True
        end = end * 10 + (ord(text[i]) - 48)
        if end > 9223372036854775807:
            return None
        i += 1

    if not has_digit or i >= n or text[i] != "/":
        return None

    i += 1

    if i < n and text[i] == "*":
        i += 1
        if i != n or end < start:
            return None
        return (start, end, None)

    has_digit = False

    while i < n and _digit(ord(text[i])):
        has_digit = True
        has_total = True
        total = total * 10 + (ord(text[i]) - 48)
        if total > 9223372036854775807:
            return None
        i += 1

    if not has_digit or i != n:
        return None

    if end < start or total <= end:
        return None

    return (start, end, total)


cpdef object parse_unsatisfied_range(object value):
    """
    Parse:
        bytes */12345
    Return total size or None.
    """
    cdef str text
    cdef Py_ssize_t i, n
    cdef int64_t total = 0
    cdef bint has_digit = False

    if not isinstance(value, str):
        return None

    text = value.strip()

    if len(text) < 9 or text[:7].lower() != "bytes *":
        return None

    i = 7
    n = len(text)

    if i >= n or text[i] != "/":
        return None

    i += 1

    while i < n and _digit(ord(text[i])):
        has_digit = True
        total = total * 10 + (ord(text[i]) - 48)

        if total > 9223372036854775807:
            return None

        i += 1

    if not has_digit or i != n:
        return None

    return total


cpdef object safe_int(object value, object default=None):
    """Convert common numeric values safely without raising."""
    cdef long long result

    if value is None:
        return default

    if isinstance(value, bool):
        return int(value)

    try:
        if isinstance(value, float):
            if value != value:
                return default
            result = <long long>value
        else:
            result = int(value)
    except (TypeError, ValueError, OverflowError):
        return default

    return result


cpdef object text_value(object value):
    """
    Extract text from YouTube's simpleText/runs structures.
    """
    cdef list runs
    cdef list parts
    cdef object item
    cdef object text

    if isinstance(value, str):
        return value or None

    if not isinstance(value, dict):
        return None

    text = value.get("simpleText")

    if isinstance(text, str):
        return text or None

    runs = value.get("runs")

    if not isinstance(runs, list):
        return None

    parts = []

    for item in runs:
        if not isinstance(item, dict):
            continue

        text = item.get("text")

        if isinstance(text, str):
            parts.append(text)

    if not parts:
        return None

    text = "".join(parts).strip()

    return text or None


cpdef tuple format_sort_key(object item):
    """
    Stable format ordering:
    progressive A/V first, then direct URLs, then resolution/bitrate.
    """
    cdef object height
    cdef object bitrate

    if not isinstance(item, dict):
        return (0, 0, 0, 0)

    height = safe_int(item.get("height"), 0)
    bitrate = safe_int(item.get("bitrate"), 0)

    return (
        int(bool(item.get("has_audio") and item.get("has_video"))),
        int(bool(item.get("is_direct"))),
        height,
        bitrate,
    )


cpdef object normalize_content_length(object value):
    """Return a positive content length or None."""
    cdef object result = safe_int(value)

    if result is None or result <= 0:
        return None

    return result


cpdef object calculate_range(object start, object chunk_size, object total=None):
    """
    Calculate an inclusive HTTP byte range.
    Returns (start, end), or None for invalid arguments.
    """
    cdef object first = safe_int(start)
    cdef object size = safe_int(chunk_size)
    cdef object last

    if first is None or size is None:
        return None

    if first < 0 or size <= 0:
        return None

    last = first + size - 1

    if total is not None:
        total = safe_int(total)

        if total is None or total <= 0:
            return None

        if first >= total:
            return None

        if last >= total:
            last = total - 1

    return (first, last)


cpdef object deduplicate_ids(object values):
    """Preserve order while removing duplicate valid video IDs."""
    cdef list output = []
    cdef set seen = set()
    cdef object value

    if not isinstance(values, (list, tuple)):
        return output

    for value in values:
        if not is_video_id(value):
            continue

        if value in seen:
            continue

        seen.add(value)
        output.append(value)

    return output
