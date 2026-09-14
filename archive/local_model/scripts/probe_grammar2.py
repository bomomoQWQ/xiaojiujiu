"""Bisect which construct inside our GBNF files the server rejects."""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request

BASE = "http://127.0.0.1:8080/v1/chat/completions"

CANDIDATES = {
    "spaces-in-alt": 'root ::= a | b\na ::= "x"   |   "y"\nb ::= "z"',
    "trailing-spaces": 'root ::= "x"   \n',
    "escaped-quote-in-lit": 'root ::= "\\""',
    "two-escaped-quotes": 'root ::= "\\"" a "\\""\na ::= "k"',
    "unicode-class": 'root ::= [^"\\\\\\x00-\\x1F0-9]+',
    "dash-in-class": 'root ::= [^\\x00-\\x1F]+',
    "repeat-plus-rule": 'root ::= a+\na ::= [a-z]',
    "mixed-literal-text": 'root ::= "\\"direction\\""',
    "long-literals": 'root ::= "\\"experience\\"" ws ":" ws t\nt ::= "\\"a\\""\nws ::= [ ]*',
}


def attempt(grammar: str) -> str:
    """Submit one grammar candidate."""
    body = {"model": "m", "messages": [{"role": "user", "content": "x"}], "max_tokens": 1, "grammar": grammar}
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
        return f"{exc.code}"
    except Exception as exc:  # noqa: BLE001
        return f"ERR {exc}"


def main() -> int:
    """Report which constructs parse."""
    for name, grammar in CANDIDATES.items():
        print(f"{name:22} -> {attempt(grammar)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
