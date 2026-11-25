
# cython: language_level=3
# cython: boundscheck=False
# cython: wraparound=False
# cython: initializedcheck=False
# cython: cdivision=True
# cython: nonecheck=False

from libc.stdint cimport int64_t
from libc.math cimport isfinite


cdef int64_t _INT64_MAX = 9223372036854775807
cdef int64_t _INT64_MIN = -9223372036854775807 - 1


cdef inline int _digit(unsigned char value) noexcept:
    return 48 <= value <= 57


cdef inline int _valid_video_char(unsigned char value) noexcept:
    return (
        48 <= value <= 57 or
        65 <= value <= 90 or
        97 <= value <= 122 or
        value == 45 or
        value == 95
    )


cdef inline bint _append_digit(
    int64_t* number,
    unsigned char digit
) noexcept:
    cdef int64_t value = digit - 48

    if number[0] > (_INT64_MAX - value) // 10:
        return False

    number[0] = number[0] * 10 + value
    return True


cpdef bint is_video_id(object value):
    """Validate an 11-character ASCII YouTube video ID."""
    cdef bytes raw
    cdef Py_ssize_t i

    if not isinstance(value, str):
        return False

    if len(value) != 11:
        return False

    try:
        raw = value.encode("ascii")
    except UnicodeEncodeError:
        return False

    for i in range(11):
        if not _valid_video_char(<unsigned char>raw[i]):
            return False

    return True


cpdef object parse_content_range(object value):
    """
    Parse valid HTTP Content-Range values.

    Supported:
        bytes 0-1023/12345
        bytes 0-1023/*

    Returns:
        (start, end, total)
        None for invalid values.
    """
    cdef str text
    cdef Py_ssize_t i, n
    cdef int64_t start = 0
    cdef int64_t end = 0
    cdef int64_t total = 0
    cdef bint has_digit = False
    cdef unsigned char char

    if not isinstance(value, str):
        return None

    text = value.strip()

    if len(text) < 9:
        return None

    if text[:6].lower() != "bytes ":
        return None

    i = 6
    n = len(text)

    while i < n and text[i] == " ":
        i += 1

    while i < n:
        char = <unsigned char>ord(text[i])

        if not _digit(char):
            break

        has_digit = True

        if not _append_digit(&start, char):
            return None

        i += 1

    if not has_digit or i >= n or text[i] != "-":
        return None

    i += 1
    has_digit = False

    while i < n:
        char = <unsigned char>ord(text[i])

        if not _digit(char):
            break

        has_digit = True

        if not _append_digit(&end, char):
            return None

        i += 1

    if not has_digit or i >= n or text[i] != "/":
        return None

    if end < start:
        return None

    i += 1

    if i < n and text[i] == "*":
        i += 1

        if i != n:
            return None

        return (start, end, None)

    has_digit = False

    while i < n:
        char = <unsigned char>ord(text[i])

        if not _digit(char):
            break

        has_digit = True

        if not _append_digit(&total, char):
            return None

        i += 1

    if not has_digit or i != n:
        return None

    if total <= end:
        return None

    return (start, end, total)


cpdef object parse_unsatisfied_range(object value):
    """
    Parse:
        bytes */12345

    Returns:
        Total resource size or None.
    """
    cdef str text
    cdef Py_ssize_t i, n
    cdef int64_t total = 0
    cdef bint has_digit = False
    cdef unsigned char char

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

    while i < n:
        char = <unsigned char>ord(text[i])

        if not _digit(char):
            return None

        has_digit = True

        if not _append_digit(&total, char):
            return None

        i += 1

    if not has_digit:
        return None

    return total


cpdef object safe_int(object value, object default=None):
    """
    Safely convert numeric values to a signed 64-bit integer.

    Returns default for:
        None, invalid strings, NaN, infinity and overflow.
    """
    cdef double number
    cdef long long result

    if value is None:
        return default

    if isinstance(value, bool):
        return int(value)

    try:
        if isinstance(value, float):
            number = value

            if not isfinite(number):
                return default

            if number >= 9223372036854775808.0:
                return default

            if number < -9223372036854775808.0:
                return default

            result = <long long>number

        else:
            result = int(value)

    except (TypeError, ValueError, OverflowError):
        return default

    return result


cpdef object text_value(object value):
    """
    Extract text from YouTube renderer text structures.

    Supports:
        "simple text"
        {"simpleText": "..."}
        {"runs": [{"text": "..."}, ...]}
    """
    cdef list runs
    cdef list parts
    cdef object item
    cdef object text

    if isinstance(value, str):
        text = value.strip()
        return text or None

    if not isinstance(value, dict):
        return None

    text = value.get("simpleText")

    if isinstance(text, str):
        text = text.strip()
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
    Format priority:
        1. Progressive audio + video
        2. Direct URL
        3. Higher resolution
        4. Higher bitrate

    Intended for reverse=True sorting.
    """
    cdef object height
    cdef object bitrate
    cdef int progressive
    cdef int direct

    if not isinstance(item, dict):
        return (0, 0, 0, 0)

    height = safe_int(item.get("height"), 0)
    bitrate = safe_int(item.get("bitrate"), 0)

    if height is None or height < 0:
        height = 0

    if bitrate is None or bitrate < 0:
        bitrate = 0

    progressive = int(
        bool(item.get("has_audio")) and
        bool(item.get("has_video"))
    )

    direct = int(bool(item.get("is_direct")))

    return (
        progressive,
        direct,
        height,
        bitrate,
    )


cpdef object normalize_content_length(object value):
    """Return a positive content length or None."""
    cdef object result = safe_int(value)

    if result is None or result <= 0:
        return None

    return result


cpdef object calculate_range(
    object start,
    object chunk_size,
    object total=None
):
    """
    Calculate an inclusive HTTP byte range.

    Returns:
        (start, end)
        None for invalid arguments.
    """
    cdef object first = safe_int(start)
    cdef object size = safe_int(chunk_size)
    cdef object last
    cdef object resource_size

    if first is None or size is None:
        return None

    if first < 0 or size <= 0:
        return None

    if first > _INT64_MAX - size + 1:
        return None

    last = first + size - 1

    if total is not None:
        resource_size = safe_int(total)

        if resource_size is None or resource_size <= 0:
            return None

        if first >= resource_size:
            return None

        if last >= resource_size:
            last = resource_size - 1

    return (first, last)


cpdef object deduplicate_ids(object values):
    """
    Preserve input order and remove invalid or duplicate video IDs.
    """
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


__all__ = [
    "is_video_id",
    "parse_content_range",
    "parse_unsatisfied_range",
    "safe_int",
    "text_value",
    "format_sort_key",
    "normalize_content_length",
    "calculate_range",
    "deduplicate_ids",
]
