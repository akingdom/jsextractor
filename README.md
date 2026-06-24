# JS Data Extractor

A **pure‑Python** tool to extract JavaScript object and array literals from `.js` files – no regex, no `eval`, no external dependencies.

## Why?

- Your data is stored as JS variables (`const data = { ... }`) in auto‑generated files.
- You need to read that data into Python safely and reliably.
- JSON parsers fail on unquoted keys, trailing commas, comments, etc.
- Regular expressions corrupt strings and are hard to maintain.

This tool gives you a **production‑grade, hand‑written parser** that handles real‑world JavaScript data syntax.

## Features

- ✅ No regex – character‑by‑character scanning
- ✅ No `eval` – recursive‑descent parser, completely safe
- ✅ Single **and** double‑quoted strings with full escape sequences (`\n`, `\uXXXX`, etc.)
- ✅ Unquoted object keys (`{ foo: 1 }`)
- ✅ Trailing commas (`{ a: 1, }`)
- ✅ Comments – `//` and `/* ... */` are skipped
- ✅ Numeric literals: hex (`0x`), octal (`0o`), binary (`0b`), decimal, floats, exponents, `NaN`, `Infinity`, `-Infinity`
- ✅ `undefined` → `None`
- ✅ Extract **first** or **all** occurrences of one or more variables
- ✅ If no variable name is given, it finds the first object/array literal in the file
- ✅ Optional JSON output (`--json`, `--pretty`)
- ✅ Line/column error reporting for easy debugging
- ✅ Key memoization to reduce memory for large files

## Installation

Just download `jsextractor.py` and make it executable:

```bash
chmod +x jsextractor.py
