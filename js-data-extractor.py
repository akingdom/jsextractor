#!/usr/bin/env python3
r"""
Pure‑Python JavaScript object/array literal extractor.

Features:
- No regex, no eval, no external dependencies.
- Handles single/double quoted strings with full escape sequences (\n, \uXXXX, etc.).
- Parses hex (0x), octal (0o), binary (0b), decimal, float, exponent, NaN, Infinity, -Infinity.
- Maps `undefined` → None.
- Skips // and /* */ comments.
- Allows trailing commas in objects and arrays.
- Extracts first or all occurrences of one or more variable assignments.
- If no variable name given, finds the first object/array literal in the file.
- Line/column error reporting.
- Optional key memoization for large files.
- Fully recursive‑descent parser with a dedicated scanner.
- Output as JSON with --json / --pretty.
"""

import argparse
import sys
import json
from typing import Any, Dict, List, Optional, Union, Iterator


# ----------------------------------------------------------------------
# Custom JSON encoder to handle NaN / Infinity
# ----------------------------------------------------------------------
class JSDataEncoder(json.JSONEncoder):
    def iterencode(self, o, _one_shot=False):
        if isinstance(o, float):
            if o != o:          # NaN
                yield 'null'
            elif o == float('inf'):
                yield 'null'
            elif o == float('-inf'):
                yield 'null'
            else:
                yield from super().iterencode(o, _one_shot)
        else:
            yield from super().iterencode(o, _one_shot)


# ----------------------------------------------------------------------
# Lexer / Scanner – character‑by‑character, emits tokens
# ----------------------------------------------------------------------
class JSLexer:
    """Low‑level tokeniser that tracks position and skips comments/whitespace."""

    def __init__(self, source: str):
        self.source = source
        self.pos = 0
        self.length = len(source)
        self.line = 1
        self.column = 1

    def error(self, msg: str) -> None:
        raise SyntaxError(f"{msg} at line {self.line}, column {self.column}")

    def peek(self, offset: int = 0) -> Optional[str]:
        idx = self.pos + offset
        return self.source[idx] if idx < self.length else None

    def advance(self, n: int = 1) -> None:
        for _ in range(n):
            if self.pos >= self.length:
                return
            if self.source[self.pos] == '\n':
                self.line += 1
                self.column = 1
            else:
                self.column += 1
            self.pos += 1

    def consume(self, expected: Optional[str] = None) -> str:
        ch = self.peek()
        if expected is not None and ch != expected:
            self.error(f"Expected '{expected}', got '{ch}'")
        self.advance()
        return ch

    def skip_whitespace_and_comments(self) -> None:
        """Advance past whitespace and comments, updating line/column."""
        while self.pos < self.length:
            ch = self.peek()
            if ch.isspace():
                self.advance()
                continue
            # Line comment //
            if ch == '/' and self.peek(1) == '/':
                self.advance(2)
                while self.pos < self.length and self.peek() != '\n':
                    self.advance()
                continue
            # Block comment /* */
            if ch == '/' and self.peek(1) == '*':
                self.advance(2)
                depth = 1
                while depth > 0 and self.pos < self.length:
                    if self.peek() == '*' and self.peek(1) == '/':
                        self.advance(2)
                        depth -= 1
                    elif self.peek() == '/' and self.peek(1) == '*':
                        self.advance(2)
                        depth += 1
                    else:
                        self.advance()
                if depth > 0:
                    self.error("Unterminated block comment")
                continue
            break

    def read_identifier(self) -> str:
        """Read a JavaScript identifier (letters, digits, _, $, not starting with digit)."""
        self.skip_whitespace_and_comments()
        start = self.pos
        first = self.peek()
        if not (first.isalpha() or first in ('_', '$')):
            self.error("Expected identifier")
        while self.pos < self.length:
            ch = self.peek()
            if ch.isalnum() or ch in ('_', '$'):
                self.advance()
            else:
                break
        return self.source[start:self.pos]

    def read_string(self) -> str:
        """Read a string literal, handling escape sequences."""
        quote = self.consume()  # ' or "
        chars = []
        while True:
            ch = self.consume()
            if ch == quote:
                return ''.join(chars)
            if ch == '\\':
                esc = self.consume()
                if esc == 'n':
                    chars.append('\n')
                elif esc == 'r':
                    chars.append('\r')
                elif esc == 't':
                    chars.append('\t')
                elif esc == 'b':
                    chars.append('\b')
                elif esc == 'f':
                    chars.append('\f')
                elif esc == '/':
                    chars.append('/')
                elif esc in ('\\', '"', "'"):
                    chars.append(esc)
                elif esc == 'u':
                    hex_str = ''.join(self.consume() for _ in range(4))
                    try:
                        code = int(hex_str, 16)
                        chars.append(chr(code))
                    except ValueError:
                        self.error(f"Invalid Unicode escape \\u{hex_str}")
                else:
                    chars.append('\\' + esc)
            else:
                chars.append(ch)

    def read_number(self) -> Union[int, float]:
        """Read a numeric literal (hex, octal, binary, decimal, float, exponent)."""
        self.skip_whitespace_and_comments()
        start = self.pos
        # Detect 0x, 0o, 0b
        if self.peek() == '0':
            peek1 = self.peek(1)
            if peek1 in ('x', 'X', 'o', 'O', 'b', 'B'):
                self.advance(2)
                while self.pos < self.length and self.peek().isalnum():
                    self.advance()
                num_str = self.source[start:self.pos]
                try:
                    return int(num_str, 0)
                except ValueError:
                    self.error(f"Invalid numeric literal '{num_str}'")

        # Regular number: digits, optional '.', optional exponent
        while self.pos < self.length:
            ch = self.peek()
            if ch.isdigit() or ch == '.':
                self.advance()
            elif ch in ('e', 'E'):
                self.advance()
                if self.peek() in ('+', '-'):
                    self.advance()
            else:
                break
        num_str = self.source[start:self.pos]
        if not num_str:
            self.error("Expected number")
        if num_str.lower() == 'infinity':
            return float('inf')
        if num_str.lower() == 'nan':
            return float('nan')
        try:
            if '.' in num_str or 'e' in num_str or 'E' in num_str:
                return float(num_str)
            return int(num_str, 10)
        except ValueError:
            self.error(f"Invalid number '{num_str}'")


# ----------------------------------------------------------------------
# Parser – recursive descent on top of the lexer
# ----------------------------------------------------------------------
class JSParser:
    def __init__(self, source: str, memoize_keys: bool = True):
        self.lexer = JSLexer(source)
        self.memoize_keys = memoize_keys
        self._key_cache = {}

    def parse(self) -> Any:
        self.lexer.skip_whitespace_and_comments()
        result = self._parse_value()
        self.lexer.skip_whitespace_and_comments()
        if self.lexer.peek() == ';':
            self.lexer.advance()
            self.lexer.skip_whitespace_and_comments()
        if self.lexer.pos < self.lexer.length:
            self.lexer.error(f"Unexpected character '{self.lexer.peek()}'")
        return result

    def _parse_value(self) -> Any:
        self.lexer.skip_whitespace_and_comments()
        ch = self.lexer.peek()
        if ch == '{':
            return self._parse_object()
        if ch == '[':
            return self._parse_array()
        if ch in ('"', "'"):
            return self.lexer.read_string()
        if ch == '-' or ch.isdigit():
            return self.lexer.read_number()
        ident = self.lexer.read_identifier()
        if ident == 'true':
            return True
        if ident == 'false':
            return False
        if ident in ('null', 'undefined'):
            return None
        if ident == 'NaN':
            return float('nan')
        if ident == 'Infinity':
            return float('inf')
        if ident == '-Infinity':
            return float('-inf')
        self.lexer.error(f"Unexpected identifier '{ident}'")

    def _parse_object(self) -> Dict:
        self.lexer.consume('{')
        obj = {}
        self.lexer.skip_whitespace_and_comments()
        if self.lexer.peek() == '}':
            self.lexer.advance()
            return obj

        while True:
            self.lexer.skip_whitespace_and_comments()
            if self.lexer.peek() in ('"', "'"):
                key = self.lexer.read_string()
            else:
                key = self.lexer.read_identifier()
                if self.memoize_keys:
                    key = self._key_cache.setdefault(key, key)

            self.lexer.skip_whitespace_and_comments()
            self.lexer.consume(':')
            value = self._parse_value()
            obj[key] = value

            self.lexer.skip_whitespace_and_comments()
            ch = self.lexer.peek()
            if ch == '}':
                self.lexer.advance()
                break
            if ch == ',':
                self.lexer.advance()
                self.lexer.skip_whitespace_and_comments()
                if self.lexer.peek() == '}':
                    self.lexer.advance()
                    break
                continue
            self.lexer.error("Expected ',' or '}'")
        return obj

    def _parse_array(self) -> List:
        self.lexer.consume('[')
        arr = []
        self.lexer.skip_whitespace_and_comments()
        if self.lexer.peek() == ']':
            self.lexer.advance()
            return arr

        while True:
            value = self._parse_value()
            arr.append(value)
            self.lexer.skip_whitespace_and_comments()
            ch = self.lexer.peek()
            if ch == ']':
                self.lexer.advance()
                break
            if ch == ',':
                self.lexer.advance()
                self.lexer.skip_whitespace_and_comments()
                if self.lexer.peek() == ']':
                    self.lexer.advance()
                    break
                continue
            self.lexer.error("Expected ',' or ']'")
        return arr


# ----------------------------------------------------------------------
# Extractor – finds variable assignments in a file
# ----------------------------------------------------------------------
def find_assignment_start(content: str, var_name: str, start_pos: int = 0) -> Optional[int]:
    """
    Scan from start_pos to find a complete assignment of the form:
        [const|let|var] var_name = { ... }   or   var_name = [ ... ]
    Returns the index of the opening '{' or '[' after the '=', or None.
    Skips comments, strings, and regex literals (if any) to avoid false positives.
    """
    n = len(content)
    pos = start_pos

    def skip_ws_and_comments(i: int) -> int:
        while i < n:
            c = content[i]
            if c.isspace():
                i += 1
                continue
            if c == '/' and i + 1 < n:
                nxt = content[i+1]
                if nxt == '/':
                    i += 2
                    while i < n and content[i] != '\n':
                        i += 1
                    continue
                if nxt == '*':
                    i += 2
                    while i + 1 < n and not (content[i] == '*' and content[i+1] == '/'):
                        i += 1
                    if i + 1 < n:
                        i += 2
                    continue
            break
        return i

    def skip_string(i: int, quote: str) -> int:
        i += 1
        while i < n:
            c = content[i]
            if c == '\\':
                i += 2
                continue
            if c == quote:
                return i + 1
            i += 1
        return n

    while pos < n:
        pos = skip_ws_and_comments(pos)
        if pos >= n:
            break

        if content.startswith(var_name, pos):
            next_pos = pos + len(var_name)
            if next_pos < n:
                nxt = content[next_pos]
                if nxt.isalnum() or nxt in ('_', '$'):
                    pos = next_pos
                    continue
            else:
                return None
            j = skip_ws_and_comments(next_pos)
            if j >= n:
                return None
            if content[j] == '=':
                k = skip_ws_and_comments(j + 1)
                if k >= n:
                    return None
                return k
            else:
                pos = next_pos
                continue
        else:
            c = content[pos]
            if c in ('"', "'", '`'):
                pos = skip_string(pos, c)
                continue
            if c == '/' and pos + 1 < n and content[pos+1].isalpha():
                pos += 2
                while pos < n:
                    if content[pos] == '\\':
                        pos += 2
                    elif content[pos] == '/':
                        pos += 1
                        break
                    else:
                        pos += 1
                continue
            pos += 1
    return None


def extract_js_variable(
    file_path: str,
    var_name: str,
    occurrence: str = 'first',
    memoize_keys: bool = True
) -> Union[Any, List[Any]]:
    """
    Extract the JavaScript variable assignment(s) from file.

    Args:
        file_path: path to .js file
        var_name: variable name to extract
        occurrence: 'first' or 'all'
        memoize_keys: cache object keys to reduce memory

    Returns:
        If 'first': the parsed Python value.
        If 'all': a list of parsed values (one per assignment).
    """
    with open(file_path, 'r', encoding='utf-8') as f:
        content = f.read()

    results = []
    search_pos = 0
    while True:
        start_idx = find_assignment_start(content, var_name, search_pos)
        if start_idx is None:
            break

        opening = content[start_idx]
        if opening not in ('{', '['):
            search_pos = start_idx + 1
            continue

        closing = '}' if opening == '{' else ']'
        depth = 0
        i = start_idx
        in_string = False
        string_char = None
        escaped = False

        while i < len(content):
            ch = content[i]
            if escaped:
                escaped = False
                i += 1
                continue
            if ch == '\\':
                escaped = True
                i += 1
                continue
            if in_string:
                if ch == string_char:
                    in_string = False
                    string_char = None
                i += 1
                continue
            if ch in ('"', "'", '`'):
                in_string = True
                string_char = ch
                i += 1
                continue
            if ch == '/' and i + 1 < len(content):
                nxt = content[i+1]
                if nxt == '/':
                    i += 2
                    while i < len(content) and content[i] != '\n':
                        i += 1
                    continue
                if nxt == '*':
                    i += 2
                    while i + 1 < len(content) and not (content[i] == '*' and content[i+1] == '/'):
                        i += 1
                    if i + 1 < len(content):
                        i += 2
                    continue
            if ch == opening:
                depth += 1
            elif ch == closing:
                depth -= 1
                if depth == 0:
                    raw = content[start_idx:i+1]
                    parser = JSParser(raw, memoize_keys=memoize_keys)
                    try:
                        parsed = parser.parse()
                    except SyntaxError as e:
                        raise SyntaxError(f"Failed to parse '{var_name}': {e}") from None
                    if occurrence == 'first':
                        return parsed
                    results.append(parsed)
                    search_pos = i + 1
                    break
            i += 1
        else:
            raise ValueError(f"Unterminated assignment for '{var_name}' starting at {start_idx}")

        if occurrence == 'first':
            break

    if occurrence == 'first':
        raise ValueError(f"Variable '{var_name}' not found.")
    return results


# ----------------------------------------------------------------------
# Helper to extract the first literal without a variable name
# ----------------------------------------------------------------------
def extract_first_literal(file_path: str, memoize_keys: bool = True) -> Any:
    with open(file_path, 'r', encoding='utf-8') as f:
        content = f.read()

    pos = 0
    n = len(content)
    in_string = False
    string_char = None
    escaped = False

    while pos < n:
        ch = content[pos]
        if escaped:
            escaped = False
            pos += 1
            continue
        if ch == '\\':
            escaped = True
            pos += 1
            continue
        if in_string:
            if ch == string_char:
                in_string = False
                string_char = None
            pos += 1
            continue
        if ch in ('"', "'", '`'):
            in_string = True
            string_char = ch
            pos += 1
            continue
        if ch == '/' and pos + 1 < n:
            nxt = content[pos+1]
            if nxt == '/':
                pos += 2
                while pos < n and content[pos] != '\n':
                    pos += 1
                continue
            if nxt == '*':
                pos += 2
                while pos + 1 < n and not (content[pos] == '*' and content[pos+1] == '/'):
                    pos += 1
                if pos + 1 < n:
                    pos += 2
                continue
        if ch in ('{', '['):
            opening = ch
            closing = '}' if opening == '{' else ']'
            depth = 0
            i = pos
            in_string = False
            string_char = None
            escaped = False
            while i < n:
                c = content[i]
                if escaped:
                    escaped = False
                    i += 1
                    continue
                if c == '\\':
                    escaped = True
                    i += 1
                    continue
                if in_string:
                    if c == string_char:
                        in_string = False
                        string_char = None
                    i += 1
                    continue
                if c in ('"', "'", '`'):
                    in_string = True
                    string_char = c
                    i += 1
                    continue
                if c == '/' and i + 1 < n:
                    nxt2 = content[i+1]
                    if nxt2 == '/':
                        i += 2
                        while i < n and content[i] != '\n':
                            i += 1
                        continue
                    if nxt2 == '*':
                        i += 2
                        while i + 1 < n and not (content[i] == '*' and content[i+1] == '/'):
                            i += 1
                        if i + 1 < n:
                            i += 2
                        continue
                if c == opening:
                    depth += 1
                elif c == closing:
                    depth -= 1
                    if depth == 0:
                        raw = content[pos:i+1]
                        parser = JSParser(raw, memoize_keys=memoize_keys)
                        return parser.parse()
                i += 1
            raise ValueError("Unterminated literal")
        pos += 1
    raise ValueError("No object or array literal found")


# ----------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(
        description="Extract JavaScript variable assignments (pure Python, no regex/eval)"
    )
    parser.add_argument('file', help='Path to the JavaScript file')
    parser.add_argument('varnames', nargs='*', help='Variable names to extract (if none, tries to find first object/array)')
    parser.add_argument('--all', action='store_true', help='Extract all occurrences (default: first)')
    parser.add_argument('--first', dest='all', action='store_false', help='Extract only first occurrence (default)')
    parser.add_argument('--no-memoize', action='store_true', help='Disable key memoization (use if memory is tight)')
    parser.add_argument('--json', action='store_true', help='Output extracted data as JSON')
    parser.add_argument('--pretty', action='store_true', help='Pretty‑print JSON output (implies --json)')
    parser.set_defaults(all=False)

    args = parser.parse_args()

    occurrence = 'all' if args.all else 'first'
    memoize = not args.no_memoize
    output_json = args.json or args.pretty
    indent = 2 if args.pretty else None

    try:
        if not args.varnames:
            # Extract first literal
            data = extract_first_literal(args.file, memoize)
            if output_json:
                print(json.dumps(data, cls=JSDataEncoder, indent=indent))
            else:
                print(f"Extracted first literal: type={type(data).__name__}, "
                      f"size={len(data) if hasattr(data, '__len__') else 'N/A'}")
            sys.exit(0)

        # Extract named variables
        output_data = {}
        for var in args.varnames:
            data = extract_js_variable(args.file, var, occurrence, memoize)
            output_data[var] = data

        if output_json:
            # If only one variable, we could output its value directly (optional)
            # But for consistency we output an object always.
            print(json.dumps(output_data, cls=JSDataEncoder, indent=indent))
        else:
            for var, data in output_data.items():
                if occurrence == 'first':
                    print(f"Extracted '{var}': type={type(data).__name__}, "
                          f"size={len(data) if hasattr(data, '__len__') else 'N/A'}")
                else:
                    print(f"Extracted '{var}': {len(data)} occurrences")

    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == '__main__':
    main()
