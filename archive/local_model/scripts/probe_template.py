"""Probe llama.cpp chat-template options so the model emits bare JSON."""

from __future__ import annotations

import json
import urllib.request

BASE = "http://127.0.0.1:8080/v1/chat/completions"

VARIANTS = {
    "plain": {},
    "kw_enable_thinking_false": {"chat_template_kwargs": {"enable_thinking": False}},
    "kw_add_generation_prompt": {"chat_template_kwargs": {"add_generation_prompt": True}},
    "both": {"chat_template_kwargs": {"enable_thinking": False, "add_generation_prompt": True}},
}


def call(extra: dict) -> str:
    """Send one request and return the completion text."""
    body = {
        "model": "m",
        "messages": [
            {"role": "system", "content": "只输出一个 JSON 对象。"},
            {"role": "user", "content": "评价这句话：今晚可能不来了。"},
        ],
        "max_tokens": 48,
        "temperature": 0.0,
        "stream": False,
        **extra,
    }
    request = urllib.request.Request(
        BASE,
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=300) as response:  # noqa: S310
        payload = json.loads(response.read().decode("utf-8"))
    return payload["choices"][0]["message"].get("content") or ""


def main() -> int:
    """Print the first 120 characters of each variant's output."""
    for name, extra in VARIANTS.items():
        try:
            text = call(extra)
        except Exception as exc:  # noqa: BLE001
            print(f"{name:28} -> ERROR {exc}")
            continue
        print(f"{name:28} -> {text[:120]!r}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
