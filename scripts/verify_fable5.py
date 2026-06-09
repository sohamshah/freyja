#!/usr/bin/env python3
"""Reproducible live-call verification for the claude-fable-5 candidate model.

Run:  python3 scripts/verify_fable5.py
Requires ANTHROPIC_API_KEY in the environment.

Proves three things with witnessable output:
  1. The Anthropic SDK version actually in use (anthropic.__version__).
  2. A real 200 round-trip to model="claude-fable-5" (prints msg id + usage + text).
  3. A negative control: a bogus slug returns 404 not_found_error, demonstrating
     the API genuinely validates model names (so the 200 above is meaningful).
"""
import os
import sys
import anthropic


def main() -> int:
    print("anthropic.__version__ =", anthropic.__version__)
    print("python.executable     =", sys.executable)
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        print("ERROR: ANTHROPIC_API_KEY not set")
        return 2
    client = anthropic.Anthropic(api_key=key)

    print("\n=== POSITIVE: model='claude-fable-5' ===")
    r = client.messages.create(
        model="claude-fable-5",
        max_tokens=200,
        messages=[{
            "role": "user",
            "content": "In one sentence, what is the capital of France and which river runs through it?",
        }],
    )
    print("http        = 200 (no exception)")
    print("model       =", r.model)
    print("id          =", r.id)
    print("stop_reason =", r.stop_reason)
    print("usage       =", r.usage.input_tokens, "in /", r.usage.output_tokens, "out")
    print("text        =", "".join(b.text for b in r.content if b.type == "text"))

    print("\n=== NEGATIVE CONTROL: model='claude-fable-5-NOPE-xyz' ===")
    try:
        client.messages.create(
            model="claude-fable-5-NOPE-xyz",
            max_tokens=8,
            messages=[{"role": "user", "content": "hi"}],
        )
        print("UNEXPECTED 200 — API did not validate the slug")
        return 1
    except anthropic.APIStatusError as e:
        print("http        =", e.status_code)
        print("body        =", e.response.text)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
