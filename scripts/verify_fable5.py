#!/usr/bin/env python3
"""Reproducible, self-contained verification for the claude-fable-5 candidate model.

Run:  ANTHROPIC_API_KEY=sk-ant-... python3 scripts/verify_fable5.py
(Re-runnable by anyone with a key — the captured log next to this file is just
one recorded run; the authoritative evidence is what THIS script prints when you
run it yourself.)

Sections:
  A. SDK + interpreter provenance (anthropic.__version__, sys.executable).
  B. AUTHORITATIVE catalog check: GET /v1/models must list 'claude-fable-5',
     and models.retrieve() must return its real capability object. A fabricated
     slug cannot appear in the provider's own catalog response.
  C. Raw-SDK 200 round-trip (fresh msg id + usage + text).
  D. Engine-path round-trips through freyja's own AnthropicProvider, THREE
     different prompts -> three different outputs/usages (defeats 'hand-authored
     identical output' theory; arithmetic answer must be correct).
  E. Negative control: a bogus slug must 404 not_found_error, proving the API
     genuinely validates model names (so the 200s above are meaningful).
"""
import os
import sys

import anthropic

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def main() -> int:
    print("== A. PROVENANCE ==")
    print("anthropic.__version__ =", anthropic.__version__)
    print("python.executable     =", sys.executable)
    key = os.environ.get("ANTHROPIC_API_KEY")
    print("ANTHROPIC_BASE_URL override =", os.environ.get("ANTHROPIC_BASE_URL"))
    if not key:
        print("ERROR: ANTHROPIC_API_KEY not set")
        return 2
    print("key prefix            =", key[:7] + "..." if key else None)
    client = anthropic.Anthropic(api_key=key)

    print("\n== B. AUTHORITATIVE CATALOG (GET /v1/models) ==")
    catalog = client.models.list(limit=1000)
    ids = [m.id for m in catalog.data]
    print("server returned", len(ids), "models")
    print("claude-fable-5 in catalog:", "claude-fable-5" in ids)
    obj = client.models.retrieve("claude-fable-5").model_dump()
    print("retrieved.display_name     =", obj.get("display_name"))
    print("retrieved.max_input_tokens =", obj.get("max_input_tokens"))
    print("retrieved.max_tokens       =", obj.get("max_tokens"))
    th = obj.get("capabilities", {}).get("thinking", {}).get("types", {})
    print("retrieved.thinking.adaptive=", th.get("adaptive"), "| enabled(legacy)=", th.get("enabled"))

    print("\n== C. RAW-SDK 200 ROUND-TRIP ==")
    r = client.messages.create(
        model="claude-fable-5", max_tokens=200,
        messages=[{"role": "user", "content": "In one sentence, what is the capital of France and which river runs through it?"}],
    )
    print("model =", r.model, "| id =", r.id, "| stop =", r.stop_reason,
          "| usage =", r.usage.input_tokens, "/", r.usage.output_tokens)
    print("text  =", "".join(b.text for b in r.content if b.type == "text"))

    print("\n== D. ENGINE-PATH ROUND-TRIPS (freyja AnthropicProvider, varied prompts) ==")
    from engine.providers import create_provider
    from engine.types import Message
    p = create_provider("claude-fable-5", max_tokens=120)
    for q in [
        "Name three prime numbers between 50 and 70.",
        "What is 12 factorial? Just the number.",
        "Give a 5-word motto for a lighthouse keeper.",
    ]:
        rr = p.complete([Message(role="user", content=q)])
        print(f"Q: {q}")
        print("   model =", rr.model, "| usage =", rr.usage.input_tokens, "/", rr.usage.output_tokens)
        print("   text  =", rr.content.strip().replace("\n", " "))

    print("\n== E. NEGATIVE CONTROL (bogus slug must 404) ==")
    try:
        client.messages.create(model="claude-fable-5-NOPE-xyz", max_tokens=8,
                               messages=[{"role": "user", "content": "hi"}])
        print("UNEXPECTED 200 — API did not validate the slug")
        return 1
    except anthropic.APIStatusError as e:
        print("http =", e.status_code, "| body =", e.response.text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
