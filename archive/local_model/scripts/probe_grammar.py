"""Find the smallest GBNF the llama.cpp server accepts, then bisect our file.

Development aid: the server reports only ``failed to parse grammar``, so the
fastest way to localise a GBNF defect is to submit candidates and see which one
is accepted.
"""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

BASE = "http://127.0.0.1:8080/v1/chat/completions"

CANDIDATES = {
    "trivial": 'root ::= "hi"',
    "ws-rule": 'root ::= "a" ws "b"\nws ::= [ \\t\\n]*',
    "multi-line-alt": 'root ::= a | b\na ::= "x"\nb ::= "y"',
    "char-class-neg": 'root ::= [^"]+',
    "char-class-range": 'root ::= [\\x00-\\x1F]+',
    "quoted-escapes": 'root ::= "\\"" a "\\""\na ::= [a-z]+',
}


def attempt(name: str, grammar: str) -> str:
    """Submit one grammar and report whether the server accepted it."""
    body = {
        "model": "m",
        "messages": [{"role": "user", "content": "x"}],
        "max_tokens": 1,
        "grammar": grammar,
    }
    request = urllib.request.Request(
        BASE,
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:  # noqa: S310
            response.read()
        return "OK"
    except urllib.error.HTTPError as exc:
        return f"{exc.code} {exc.read().decode('utf-8', 'replace')[:120]}"
    except Exception as exc:  # noqa: BLE001
        return f"ERR {exc}"


def main() -> int:
    """Test the built-in candidates and any file passed on the command line."""
    for name, grammar in CANDIDATES.items():
        print(f"{name:18} -> {attempt(name, grammar)}")
    for path in sys.argv[1:]:
        text = Path(path).read_text(encoding="utf-8")
        print(f"{Path(path).name:18} -> {attempt(Path(path).name, text)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
