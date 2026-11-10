"""Find @path import tokens in CLAUDE.md / AGENTS.md text.

Lexical rules follow Claude Code's documented import syntax
(https://code.claude.com/docs/en/memory):

- ``@path`` is an import anywhere in the file, not only on line 1.
- A path wrapped in quotes is not an import.
- Parsing skips Markdown code spans and fenced code blocks
  (``` and ~~~, including indented fences of 0–3 spaces).
- A backslash escapes the next character, including spaces.
- The path ends at the first unescaped whitespace.
- Trailing punctuation that commonly ends a sentence is stripped.

v0 does not treat indented (4-space) code blocks as fences. An unclosed
fence or code span masks the rest of that region (through EOF if it never
closes). HTML comments are not special-cased.
"""

from __future__ import annotations

from dataclasses import dataclass
import re

_FENCE_OPEN = re.compile(r"^( {0,3})(`{3,}|~{3,})(.*)$")
_TRAIL = set(".,;:!?)]}>'\"*_")


@dataclass(frozen=True)
class ImportToken:
    line: int
    token: str


def find_imports(text: str) -> list[ImportToken]:
    if text.startswith("\ufeff"):
        text = text[1:]
    found: list[ImportToken] = []
    in_fence = False
    fence_char = ""
    fence_len = 0
    span_run = 0
    for lineno, line in enumerate(text.splitlines(), 1):
        if span_run:
            rest, span_run = _continue_span(line, span_run)
            if span_run:
                continue
            line = rest
        if in_fence:
            if _is_fence_close(line, fence_char, fence_len):
                in_fence = False
            continue
        opener = _fence_open(line)
        if opener is not None:
            fence_char, fence_len = opener
            in_fence = True
            continue
        tokens, span_run = _scan_line(line)
        for token in tokens:
            found.append(ImportToken(line=lineno, token=token))
    return found


def _fence_open(line: str) -> tuple[str, int] | None:
    match = _FENCE_OPEN.match(line)
    if match is None:
        return None
    marks = match.group(2)
    info = match.group(3)
    if marks[0] == "`" and "`" in info:
        return None
    return marks[0], len(marks)


def _is_fence_close(line: str, char: str, length: int) -> bool:
    match = re.match(r"^( {0,3})(" + re.escape(char) + r"{3,})[ \t]*$", line)
    return match is not None and len(match.group(2)) >= length


def _continue_span(line: str, run: int) -> tuple[str, int]:
    index = 0
    size = len(line)
    while index < size:
        if line[index] != "`":
            index += 1
            continue
        end = index
        while end < size and line[end] == "`":
            end += 1
        if end - index == run:
            return line[end:], 0
        index = end
    return "", run


def _scan_line(line: str) -> tuple[list[str], int]:
    tokens: list[str] = []
    index = 0
    size = len(line)
    while index < size:
        char = line[index]
        if char == "`":
            end = index
            while end < size and line[end] == "`":
                end += 1
            run = end - index
            close = _find_backtick_run(line, end, run)
            if close is None:
                return tokens, run
            index = close
            continue
        if char == "@" and _valid_predecessor(line, index):
            token, next_index = _read_path(line, index + 1)
            if token is not None:
                tokens.append(token)
                index = next_index
                continue
        index += 1
    return tokens, 0


def _find_backtick_run(line: str, start: int, run: int) -> int | None:
    index = start
    size = len(line)
    while index < size:
        if line[index] != "`":
            index += 1
            continue
        end = index
        while end < size and line[end] == "`":
            end += 1
        if end - index == run:
            return end
        index = end
    return None


def _valid_predecessor(line: str, index: int) -> bool:
    if index == 0:
        return True
    prev = line[index - 1]
    return prev.isspace() or prev in "([>*_-+{:"


def _read_path(line: str, start: int) -> tuple[str | None, int]:
    if start >= len(line):
        return None, start
    if line[start] in {"'", '"'}:
        return None, start
    chars: list[str] = []
    index = start
    size = len(line)
    while index < size:
        char = line[index]
        if char == "\\":
            if index + 1 >= size:
                break
            chars.append(line[index + 1])
            index += 2
            continue
        if char.isspace():
            break
        chars.append(char)
        index += 1
    token = _strip_trailing("".join(chars))
    if not token or token in {".", ".."}:
        return None, index
    return token, index


def _strip_trailing(token: str) -> str:
    while token and token[-1] in _TRAIL:
        token = token[:-1]
    return token
