"""Locate Java method bodies without interpreting comments or string contents."""

from bisect import bisect_right
import re


_TOKEN_RE = re.compile(
    r'//[^\n]*|/\*[\s\S]*?\*/|"""(?:\\[\s\S]|(?!""")[^\\])*"""'
    r'|"(?:\\[\s\S]|[^"\\])*"|\'(?:\\[\s\S]|[^\'\\])*\''
    r'|[A-Za-z_$][A-Za-z0-9_$]*|[^\s]'
)
_NON_METHOD_NAMES = {"if", "for", "while", "switch", "catch", "synchronized", "try"}
_NON_DECLARATION_TOKENS = {"=", "new", "return", "throw", "class", "record", "enum"}


def enclosing_method_lines(source: str, line_number: int) -> tuple[int, int] | None:
    """Return the innermost method/constructor containing a one-based line.

    This recognizes balanced declarations, including overloads and lambda bodies.
    Initializers and unrecognized/incomplete declarations use the caller's fallback.
    """
    tokens = [
        (match.group(), match.start())
        for match in _TOKEN_RE.finditer(source)
        if not match.group().startswith(('//', '/*', '"', "'"))
    ]
    pairs: dict[int, int] = {}
    stack: list[int] = []
    closing = {")": "(", "]": "[", "}": "{"}
    for index, (token, _) in enumerate(tokens):
        if token in {"(", "[", "{"}:
            stack.append(index)
        elif token in closing:
            if not stack or tokens[stack[-1]][0] != closing[token]:
                return None
            opening = stack.pop()
            pairs[opening] = index
            pairs[index] = opening

    newlines = [match.start() for match in re.finditer("\n", source)]
    candidates: list[tuple[int, int]] = []
    for opening, (token, _) in enumerate(tokens):
        if token != "{" or opening not in pairs:
            continue
        end_line = bisect_right(newlines, tokens[pairs[opening]][1]) + 1
        if line_number > end_line:
            continue

        # A method body follows its parameter list, optional array dimensions,
        # and an optional throws clause. Calls/control blocks are excluded below.
        parameter_end = opening - 1
        while parameter_end >= 0 and tokens[parameter_end][0] not in {")", ";", "{", "}", "="}:
            parameter_end -= 1
        if parameter_end < 0 or tokens[parameter_end][0] != ")":
            continue
        parameter_start = pairs.get(parameter_end)
        if parameter_start is None or parameter_start < 1:
            continue
        name_index = parameter_start - 1
        name = tokens[name_index][0]
        if not re.fullmatch(r"[A-Za-z_$][A-Za-z0-9_$]*", name) or name in _NON_METHOD_NAMES:
            continue
        suffix = " ".join(value for value, _ in tokens[parameter_end + 1 : opening])
        if not re.fullmatch(r"(?:\[\s*\]\s*)*(?:throws\s+[A-Za-z0-9_$.,\s]+)?", suffix):
            continue

        boundary = name_index - 1
        while boundary >= 0 and tokens[boundary][0] not in {";", "{", "}"}:
            if tokens[boundary][0] in {")", "]"} and boundary in pairs:
                boundary = pairs[boundary]
            boundary -= 1
        prefix = []
        index = boundary + 1
        while index < name_index:
            value = tokens[index][0]
            if value == "(" and index in pairs:
                index = pairs[index] + 1
                continue
            prefix.append(value)
            index += 1
        if any(value in _NON_DECLARATION_TOKENS for value in prefix):
            continue
        if prefix and prefix[-1] in {".", ":"}:
            continue
        start_line = bisect_right(newlines, tokens[boundary + 1][1]) + 1
        if start_line <= line_number <= end_line:
            candidates.append((start_line, end_line))

    return min(candidates, key=lambda bounds: bounds[1] - bounds[0]) if candidates else None
