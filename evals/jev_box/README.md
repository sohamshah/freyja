# Jev images and Box: grounding experiments

Jev 1.14 (2026-10-14) takes images anywhere a request takes text and adds a
`box` question type: the answer is the four edges of a box around what the
instructions describe, as fractions of the image, with a confidence. TypeSafe
ships both behind a `TypeSafe-Danger-Zone-Images: true` header and calls them
"dreamer-ready", not production. These scripts measure what that is worth for
the operator (`bridge/tools/jev_operator`), whose only pixel path was one
planner click per replan. Results and the design they led to are in
`RESULTS.md`; the code they justify is `bridge/tools/jev_operator/vision.py`
and the plan handling in `loop.py`.

Every script loads `TYPESAFE_AI_API_KEY` (and, for the planner baselines, the
LLM provider keys) from the repo's `.env`. Nothing here is imported by the app.

| Script | What it measures | Truth |
|---|---|---|
| `smoke.py` | one live screenshot of this Mac through Noul, Choice and Box; draws the boxes | eyeball |
| `screenspot.py` | ScreenSpot-v2 (1,272 desktop, web and mobile targets), the public GUI-grounding benchmark, so the number is comparable with published grounders | the benchmark's boxes |
| `web_envs.py ground` | every labelled control in view on the mock web environments' start pages, by a short description, with the planner model's own pixel guess as the baseline | DOM rectangles |
| `web_envs.py blind` | whole tasks through the real operator loop with a surface that exposes no elements and no text (an app with an empty accessibility tree), old code vs new, Jev vs the planner as the grounder | the environments' rewards |
| `macos_ax.py` | labelled controls of real macOS windows (Calculator, Finder, System Settings, ...) captured as the operator captures them | accessibility frames; needs the screen unlocked |

`api.py` is a minimal client for the image request format, independent of the
bridge, used by `smoke.py` and `screenspot.py`. The operator's own path is
`vision.locate`, which `web_envs.py` and `macos_ax.py` call directly.

```sh
uv run python evals/jev_box/smoke.py
uv run python evals/jev_box/screenspot.py --variant plain,plain+crop --workers 8      # needs the dataset, see below
uv run python evals/jev_box/web_envs.py ground --pages 12 --per-page 25 --llm claude-opus-5-5 --llm-limit 80
uv run python evals/jev_box/web_envs.py blind --tag new --reps 2
uv run python evals/jev_box/web_envs.py blind --jev-root /path/to/old/checkout --tag main --reps 2
uv run python evals/jev_box/macos_ax.py --apps Calculator,Finder,"System Settings",Notes --per-app 30
```

ScreenSpot-v2 lives outside the repo: the three annotation files and
`screenspotv2_image.zip` from `huggingface.co/datasets/OS-Copilot/ScreenSpot-v2`,
unpacked under `~/work/eval-benchmark-data/screenspot_v2/` (`--data` to point
elsewhere). Per-item results are cached in `data/screenspot_cache.jsonl`, so
`--report` is free. `data/` is not committed.
