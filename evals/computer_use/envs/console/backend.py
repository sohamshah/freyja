"""Server side of the Cirrolux Cloud console, an invented cloud console: its
model catalog, a model's page, the model enablement questionnaire, the terms
page and the project picker. The pages are one single-page app (site/app.js);
these routes hold what the console remembers for an episode: the
organization's projects, the selected project, the models enabled in each
project, questionnaire submissions, agreements, and every event.

The pages copy the layout and the behaviour of a real cloud console's model
catalog closely (see README.md). Every page pattern that can be switched is a
quirk in `episode.config["quirks"]`, on by default; with a quirk off the page
uses the plain version. Rewards read only this module's state and events.

Config keys, all optional:
  quirks    {name: bool}
  seed      seeds the generated project IDs and the timings (default 5)
  selected  the ID of the project selected when the episode starts (default: none)
  timings   {"first_ms", "swap_ms", "agree_s": [lo, hi], "more_s"} (a test shortens them)
"""

from __future__ import annotations

import copy
import random
import re
import time
import weakref
from pathlib import Path
from typing import Any

from ...server import Episode, Response

SITE = Path(__file__).resolve().parent / "site"

QUIRKS = {
    # Model catalog
    "lazy_content":
        "the catalog shows nothing until a project is selected, its content arrives seconds after "
        "the page loads, and after a route change the URL changes first and the old content stays "
        "on screen for 2-3 s",
    "title_only_links": "a model card is not a link; only the model name inside it is",
    "api_banner":
        "an 'Enable APIs' banner sits above the catalog, a look-alike of a model's Enable",
    # Model page
    "program_radio":
        "a grey box at the top of a model page offers 'Standard use cases' and a special "
        "verified program for high-risk dual-use work, as radio buttons",
    # Questionnaire
    "overlay_selects":
        "the selects are Material-style: a div with role combobox whose option panel is rendered "
        "only while open, in an overlay container at the end of <body> behind a transparent "
        "backdrop that closes it",
    "floating_labels":
        "outlined fields whose label rests across the middle of the empty control and floats up "
        "into the outline once it has a value",
    "preset_industry": "the Industry select opens preset to Agriculture",
    # Terms
    "slow_agree": "Agree answers after 2-4 s while the button shows Loading",
    # Project picker
    "paged_picker":
        "search results come 10 at a time (Next 10 rows / Previous 10 rows) and the All tab "
        "20 at a time behind a slow More results",
    "untitled_projects": "nine projects are all named 'Untitled project' and differ only by ID",
    "sticky_picker":
        "the picker ignores Escape and clicks outside it; Escape only leaves search mode, and "
        "only Cancel or a choice closes it",
}
DEFAULT_QUIRKS = {k: True for k in QUIRKS}

CONSOLE = "Cirrolux Cloud"
PLATFORM = "Cirrolux AI Platform"
ORG = "lumenrobotics.example"
USER = "Ada Lovelace"
EMAIL = "ada.lovelace@lumenrobotics.example"
TODAY = "Oct 14, 2026"

# Live episodes by id: the privileged oracle reads the quirk switches through
# this, so the page never has to carry them in its markup.
EPISODES: "weakref.WeakValueDictionary[str, Episode]" = weakref.WeakValueDictionary()

# ─── the organization's projects ──────────────────────────────────────────
# (display name, project ID). 27 of them match the word "lumen".
LUMEN = [
    ("Lumen Assistant", "lumen-assistant-207341"),
    ("Lumen Docs Site", "lumen-docs"),
    ("Lumen Fleet Telemetry", "fleet-telemetry-551209"),
    ("lumen-analytics", "lumen-analytics"),
    ("lumen-api", "lumen-api-318842"),
    ("lumen-billing-export", "lumen-billing-export"),
    ("lumen-ci", "lumen-ci"),
    ("lumen-data-lake", "lumen-data-lake-660412"),
    ("lumen-demo", "lumen-demo-118204"),
    ("lumen-demo-old", "lumen-demo-old"),
    ("lumen-dev", "lumen-dev-482913"),
    ("lumen-edge", "lumen-edge"),
    ("lumen-firmware", "lumen-firmware-731125"),
    ("lumen-hr-tools", "lumen-hr-tools"),
    ("lumen-labs", "lumen-labs-904417"),
    ("lumen-maps", "lumen-maps"),
    ("lumen-ml", "cx-lab-0412339871"),
    ("lumen-mobile", "lumen-mobile"),
    ("lumen-partner-portal", "lumen-partner-portal"),
    ("lumen-prod", "lumen-prod-482911"),
    ("lumen-prod-eu", "lumen-prod-eu"),
    ("lumen-research", "lumen-research-270053"),
    ("lumen-sandbox", "lumen-sandbox"),
    ("lumen-staging", "lumen-staging"),
    ("lumen-staging-eu", "lumen-staging-eu-530917"),
    ("lumen-support", "lumen-support"),
    ("lumen-vision", "lumen-vision-88214"),
]
OTHERS = [
    ("Default Assist Project", "assist-client-0918273645"),
    ("Holiday Party Planner", "holiday-party-planner"),
    ("Partner Demo 2025", "partner-demo-2025"),
    ("Payroll Export", "payroll-export-2231"),
    ("QA Sandbox", "qa-sandbox-55120"),
    ("Warehouse Vision Pilot", "warehouse-vision-pilot"),
]
UNTITLED = "Untitled project"
# With untitled_projects off, the same nine projects carry names of their own.
UNTITLED_PLAIN = [
    "Arm calibration", "Battery bench", "Conveyor sim", "Dock scheduler", "Edge cache test",
    "Floor mapping", "Gripper vision", "Hangar demo", "Inventory sync",
]
RECENT = ["lumen-dev-482913", "lumen-prod-482911", "lumen-staging", "@untitled2",
          "lumen-demo-118204", "lumen-staging-eu-530917"]
STARRED = {"lumen-dev-482913", "lumen-prod-482911"}
PAGE_ROWS = 10  # search results per page in the picker
ALL_ROWS = 20  # projects per "More results" batch in the All tab


def untitled_ids(seed: int) -> list[str]:
    """The nine "Untitled project" IDs: "sys-" and 26 seeded digits."""
    rng = random.Random(f"console-sys-{seed}")
    return ["sys-" + "".join(str(rng.randrange(10)) for _ in range(26)) for _ in range(9)]


def _projects(seed: int, quirks: dict[str, bool]) -> list[dict[str, Any]]:
    sys_ids = untitled_ids(seed)
    rows = [{"name": n, "id": i, "untitled": False} for n, i in LUMEN + OTHERS]
    for k, pid in enumerate(sys_ids):
        name = UNTITLED if quirks.get("untitled_projects") else UNTITLED_PLAIN[k]
        rows.append({"name": name, "id": pid, "untitled": True})
    rng = random.Random(f"console-num-{seed}")
    for p in rows:
        p["number"] = str(rng.randrange(10**11, 10**12))
        p["starred"] = p["id"] in STARRED
    # The picker lists projects by name in code-point order (capitals first), then by ID.
    rows.sort(key=lambda p: (p["name"], p["id"]))
    return rows


# ─── the model catalog ────────────────────────────────────────────────────
PUBLISHERS = {
    "cirrolux": {"name": "Cirrolux", "color": "#3b5bdb", "mark": "C"},
    "anthropic": {"name": "Anthropic", "color": "#a3562f", "mark": "A", "family": "Claude"},
    "mistralai": {"name": "Mistral AI", "color": "#c2410c", "mark": "M", "family": "Mistral"},
    "ai21": {"name": "AI21 Labs", "color": "#7c3aed", "mark": "J", "family": "Jamba"},
    "fernhill": {"name": "Fernhill AI", "color": "#0f766e", "mark": "F", "family": "Fernhill"},
    "meta": {"name": "Meta", "color": "#2563eb", "mark": "L"},
    "deepseek": {"name": "DeepSeek", "color": "#4338ca", "mark": "D"},
    "qwen": {"name": "Qwen", "color": "#6d28d9", "mark": "Q"},
}
COLLECTIONS = {"first": "Cirrolux models", "partner": "Partner models", "open": "Open models"}

TEXT, CODE, VISION = "Text generation", "Code generation", "Image understanding"


def _m(slug: str, name: str, pub: str, kind: str, desc: str, overview: str, released: str,
       inputs: str, output: str, tasks: list[str], features: list[str], data_in: str,
       price: tuple[float, float] | None = None) -> dict[str, Any]:
    return {
        "slug": slug, "name": name, "pub": pub, "kind": kind, "desc": desc,
        "overview": overview, "released": released, "inputs": inputs, "output": output,
        "tasks": tasks, "features": features, "data_in": data_in, "data_out": "Text",
        "price": price,
    }


MODELS = [
    _m("claude-opus-5-5", "Claude Opus 5.5", "anthropic", "partner",
       "Anthropic's most capable model, for long-running agents, large codebases and research.",
       "Claude Opus 5.5 is Anthropic's most capable model. It plans and carries out long, "
       "multi-step work, such as agents that run for hours, changes across large codebases and "
       "research that reads hundreds of sources.",
       "October 7, 2026", "1M", "128,000 tokens", [TEXT, CODE, VISION],
       ["API available", "Batch prediction", "Prompt caching"], "Image, PDF, Text", (6.0, 30.0)),
    _m("claude-sonnet-5-5", "Claude Sonnet 5.5", "anthropic", "partner",
       "A faster Sonnet for coding, agents and everyday knowledge work, at a lower cost per task.",
       "Claude Sonnet 5.5 is a faster, more efficient Sonnet. It handles coding, agents and "
       "everyday knowledge work well, and it costs less per task than earlier Sonnet models.",
       "September 30, 2026", "1M", "96,000 tokens", [TEXT, CODE, VISION],
       ["API available", "Batch prediction", "Prompt caching"], "Image, PDF, Text", (3.5, 17.5)),
    _m("claude-opus-5", "Claude Opus 5", "anthropic", "partner",
       "The previous Opus generation, for complex reasoning and long documents.",
       "Claude Opus 5 handles complex reasoning, long documents and careful writing. Claude Opus "
       "5.5 replaces it for most new work.",
       "June 24, 2026", "1M", "64,000 tokens", [TEXT, CODE, VISION],
       ["API available", "Batch prediction", "Prompt caching"], "Image, PDF, Text", (15.0, 75.0)),
    _m("claude-sonnet-5", "Claude Sonnet 5", "anthropic", "partner",
       "A balanced model for coding and data work with a large context window.",
       "Claude Sonnet 5 balances speed and quality for coding, data extraction and support "
       "agents.",
       "May 21, 2026", "1M", "64,000 tokens", [TEXT, CODE, VISION],
       ["API available", "Batch prediction", "Prompt caching"], "Image, PDF, Text", (3.0, 15.0)),
    _m("claude-haiku-4-5", "Claude Haiku 4.5", "anthropic", "partner",
       "Anthropic's fastest model, for real-time assistants and sub-agents.",
       "Claude Haiku 4.5 answers fast at a low price. It suits real-time assistants, "
       "classification and sub-agents that a larger model directs.",
       "October 15, 2025", "200,000 tokens", "64,000 tokens", [TEXT, CODE, VISION],
       ["API available", "Batch prediction", "Prompt caching"], "Image, PDF, Text", (1.0, 5.0)),
    _m("mistral-large-3", "Mistral Large 3", "mistralai", "partner",
       "Mistral AI's large multilingual model for reasoning and tool use.",
       "Mistral Large 3 is a multilingual model for reasoning, function calling and long "
       "documents.",
       "December 2, 2025", "256,000 tokens", "32,000 tokens", [TEXT, CODE],
       ["API available", "Batch prediction"], "Image, Text", (2.0, 6.0)),
    _m("mistral-medium-3", "Mistral Medium 3", "mistralai", "partner",
       "A mid-size Mistral model with a good balance of cost and quality.",
       "Mistral Medium 3 gives strong quality for its price on chat, extraction and coding.",
       "May 7, 2025", "128,000 tokens", "32,000 tokens", [TEXT, CODE],
       ["API available"], "Image, Text", (0.4, 2.0)),
    _m("codestral-2", "Codestral 2", "mistralai", "partner",
       "A code model for completion, fill-in-the-middle and test generation.",
       "Codestral 2 completes code, fills in the middle of files and writes tests in more than "
       "80 programming languages.",
       "January 13, 2026", "256,000 tokens", "32,000 tokens", [CODE],
       ["API available"], "Text", (0.3, 0.9)),
    _m("jamba-2-large", "Jamba 2 Large", "ai21", "partner",
       "A hybrid model for grounded answers over long enterprise documents.",
       "Jamba 2 Large answers questions over long enterprise documents and cites its sources.",
       "March 3, 2026", "256,000 tokens", "16,000 tokens", [TEXT],
       ["API available"], "Text", (2.0, 8.0)),
    _m("prompt-router-2", "Prompt Router 2", "fernhill", "partner",
       "Sends each prompt to the cheapest model that answers it well, such as Claude, Llama "
       "or Cirro.",
       "Prompt Router 2 scores each prompt and sends it to the cheapest model that can answer it "
       "well, such as Claude, Llama or Cirro models. It is not a model of its own.",
       "August 19, 2026", "200,000 tokens", "32,000 tokens", [TEXT],
       ["API available"], "Text", (0.2, 0.2)),
    _m("llama-4-maverick", "Llama 4 Maverick", "meta", "open",
       "Meta's multimodal mixture-of-experts model for chat and image understanding.",
       "Llama 4 Maverick is an open-weight multimodal model for chat, image understanding and "
       "agents.",
       "April 5, 2025", "1M", "16,000 tokens", [TEXT, VISION],
       ["Notebook support", "One-click deployment", "Fine-tuning"], "Image, Text"),
    _m("llama-4-scout", "Llama 4 Scout", "meta", "open",
       "A smaller Llama 4 model with a very long context window.",
       "Llama 4 Scout is a smaller open-weight model with a very long context window.",
       "April 5, 2025", "10M", "16,000 tokens", [TEXT, VISION],
       ["Notebook support", "One-click deployment", "Fine-tuning"], "Image, Text"),
    _m("llama-3-3-70b", "Llama 3.3 70B", "meta", "open",
       "A 70B text model for chat and instruction following.",
       "Llama 3.3 70B is an open-weight text model for chat and instruction following.",
       "December 6, 2024", "128,000 tokens", "8,000 tokens", [TEXT],
       ["Notebook support", "One-click deployment", "Fine-tuning"], "Text"),
    _m("deepseek-v3-2", "DeepSeek V3.2", "deepseek", "open",
       "An open mixture-of-experts model for reasoning and coding.",
       "DeepSeek V3.2 is an open-weight model for reasoning, coding and tool use.",
       "September 29, 2025", "128,000 tokens", "8,000 tokens", [TEXT, CODE],
       ["Notebook support", "One-click deployment"], "Text"),
    _m("deepseek-r1", "DeepSeek R1", "deepseek", "open",
       "A reasoning model that thinks step by step before it answers.",
       "DeepSeek R1 is an open-weight reasoning model.",
       "January 20, 2025", "128,000 tokens", "32,000 tokens", [TEXT],
       ["Notebook support", "One-click deployment"], "Text"),
    _m("qwen3-coder", "Qwen3 Coder", "qwen", "open",
       "An agentic coding model for repositories and tools.",
       "Qwen3 Coder is an open-weight model for agentic coding across whole repositories.",
       "July 22, 2025", "256,000 tokens", "64,000 tokens", [CODE],
       ["Notebook support", "One-click deployment"], "Text"),
    _m("qwen3-235b", "Qwen3 235B", "qwen", "open",
       "A large multilingual model with a thinking mode.",
       "Qwen3 235B is an open-weight multilingual model with a switchable thinking mode.",
       "April 29, 2025", "128,000 tokens", "32,000 tokens", [TEXT],
       ["Notebook support", "One-click deployment"], "Text"),
    _m("cirro-3-pro", "Cirro 3 Pro", "cirrolux", "first",
       "Cirrolux's most capable model for reasoning, coding and long documents.",
       "Cirro 3 Pro is the most capable Cirrolux model, for reasoning, coding and long documents.",
       "August 26, 2026", "2M", "64,000 tokens", [TEXT, CODE, VISION],
       ["API available", "Batch prediction", "Prompt caching", "Fine-tuning"],
       "Audio, Image, PDF, Text, Video"),
    _m("cirro-3-flash", "Cirro 3 Flash", "cirrolux", "first",
       "A fast, low-cost model for high-volume tasks.",
       "Cirro 3 Flash is a fast, low-cost model for high-volume chat and extraction.",
       "August 26, 2026", "1M", "64,000 tokens", [TEXT, CODE, VISION],
       ["API available", "Batch prediction", "Prompt caching", "Fine-tuning"],
       "Audio, Image, PDF, Text, Video"),
    _m("cirro-3-flash-lite", "Cirro 3 Flash-Lite", "cirrolux", "first",
       "The lowest-cost Cirro model, for classification and routing.",
       "Cirro 3 Flash-Lite is the lowest-cost Cirro model.",
       "September 9, 2026", "1M", "32,000 tokens", [TEXT],
       ["API available", "Batch prediction"], "Image, Text"),
    _m("cirro-embed-2", "Cirro Embed 2", "cirrolux", "first",
       "Text embeddings for search, clustering and retrieval.",
       "Cirro Embed 2 turns text into embeddings for search and retrieval.",
       "March 18, 2026", "8,000 tokens", "3,072 dimensions", ["Embeddings"],
       ["API available", "Batch prediction"], "Text"),
    _m("cirro-image-2", "Cirro Image 2", "cirrolux", "first",
       "Image generation and editing from text prompts.",
       "Cirro Image 2 generates and edits images from text prompts.",
       "June 10, 2026", "4,000 tokens", "Image", ["Image generation"],
       ["API available"], "Image, Text"),
    _m("cirro-speech-2", "Cirro Speech 2", "cirrolux", "first",
       "Speech recognition in 90 languages.",
       "Cirro Speech 2 transcribes speech in 90 languages.",
       "February 24, 2026", "8 hours of audio", "Text", ["Speech recognition"],
       ["API available", "Batch prediction"], "Audio"),
]
BY_SLUG = {m["slug"]: m for m in MODELS}
# Trending order of the catalog's default view.
TRENDING = [
    "claude-sonnet-5-5", "cirro-3-pro", "claude-opus-5-5", "cirro-3-flash", "llama-4-maverick",
    "mistral-large-3", "deepseek-v3-2", "claude-haiku-4-5", "qwen3-coder", "cirro-3-flash-lite",
    "claude-sonnet-5", "codestral-2", "llama-4-scout", "cirro-embed-2", "claude-opus-5",
    "prompt-router-2", "mistral-medium-3", "jamba-2-large", "deepseek-r1", "qwen3-235b",
    "cirro-image-2", "llama-3-3-70b", "cirro-speech-2",
]

# Models enabled in each project when an episode starts (an agreement stands behind each).
ENABLED_START = {
    "lumen-dev-482913": ["claude-sonnet-5-5", "claude-haiku-4-5", "mistral-medium-3"],
    "lumen-prod-482911": ["claude-sonnet-5-5"],
    "lumen-staging": ["claude-haiku-4-5"],
    "lumen-staging-eu-530917": ["claude-sonnet-5-5"],
}

COUNTRIES = [
    "Afghanistan", "Albania", "Algeria", "Andorra", "Angola", "Argentina", "Armenia", "Australia",
    "Austria", "Azerbaijan", "Bahamas", "Bahrain", "Bangladesh", "Barbados", "Belarus", "Belgium",
    "Belize", "Benin", "Bhutan", "Bolivia", "Bosnia and Herzegovina", "Botswana", "Brazil",
    "Brunei", "Bulgaria", "Burkina Faso", "Cambodia", "Cameroon", "Canada", "Chile", "China",
    "Colombia", "Costa Rica", "Croatia", "Cyprus", "Czechia", "Denmark", "Dominican Republic",
    "Ecuador", "Egypt", "El Salvador", "Estonia", "Ethiopia", "Fiji", "Finland", "France", "Gabon",
    "Georgia", "Germany", "Ghana", "Greece", "Guatemala", "Honduras", "Hong Kong", "Hungary",
    "Iceland", "India", "Indonesia", "Iraq", "Ireland", "Israel", "Italy", "Jamaica", "Japan",
    "Jordan", "Kazakhstan", "Kenya", "Kuwait", "Kyrgyzstan", "Laos", "Latvia", "Lebanon",
    "Lithuania", "Luxembourg", "Madagascar", "Malawi", "Malaysia", "Maldives", "Malta",
    "Mauritius", "Mexico", "Moldova", "Monaco", "Mongolia", "Montenegro", "Morocco", "Mozambique",
    "Namibia", "Nepal", "Netherlands", "New Zealand", "Nicaragua", "Nigeria", "North Macedonia",
    "Norway", "Oman", "Pakistan", "Panama", "Paraguay", "Peru", "Philippines", "Poland",
    "Portugal", "Qatar", "Romania", "Rwanda", "Saudi Arabia", "Senegal", "Serbia", "Singapore",
    "Slovakia", "Slovenia", "South Africa", "South Korea", "Spain", "Sri Lanka", "Sweden",
    "Switzerland", "Taiwan", "Tajikistan", "Tanzania", "Thailand", "Trinidad and Tobago",
    "Tunisia", "Turkey", "Uganda", "Ukraine", "United Arab Emirates", "United Kingdom",
    "United States of America", "Uruguay", "Uzbekistan", "Vietnam", "Zambia", "Zimbabwe",
]
INDUSTRIES = [
    "Agriculture", "Automotive", "Banking", "Chemicals", "Construction", "Consumer goods",
    "Education", "Energy and utilities", "Entertainment", "Financial services", "Government",
    "Healthcare and life sciences", "Hospitality", "Insurance", "Legal services",
    "Manufacturing", "Media and publishing", "Mining", "Nonprofit", "Pharmaceuticals",
    "Professional services", "Real estate", "Retail", "Technology", "Telecommunications",
    "Transportation and logistics", "Other",
]
USERS = ["Internal employees", "External customers or users",
         "Internal employees and external users"]
FIELDS = ["name", "website", "email", "hq", "industry", "users", "use_cases", "aup",
          "aup_detail"]
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

TIMINGS = {"first_ms": 2600, "swap_ms": 2200, "agree_s": [2.0, 4.0], "more_s": 1.6}


def key_of(m: dict[str, Any]) -> str:
    return f"publishers/{m['pub']}/models/{m['slug']}"


def slug_of(model: str) -> str:
    """'publishers/anthropic/models/claude-sonnet-5-5' (or the bare slug) -> its slug."""
    return str(model or "").strip().strip("/").split("/")[-1]


def needs_of(m: dict[str, Any]) -> str:
    """What Enable leads to: the questionnaire (Anthropic), the terms (other partners),
    nothing for Cirrolux's own models (ready to use) and open models (deployed instead)."""
    if m["kind"] != "partner":
        return "none"
    return "questionnaire" if m["pub"] == "anthropic" else "terms"


def init(ep: Episode) -> None:
    cfg = ep.config
    q = dict(DEFAULT_QUIRKS)
    q.update(cfg.get("quirks") or {})
    cfg["quirks"] = q
    seed = int(cfg.get("seed", 5))
    timings = dict(TIMINGS)
    rng = random.Random(f"console-t-{seed}")
    timings["first_ms"] += rng.randrange(0, 700)
    timings["swap_ms"] += rng.randrange(0, 800)
    timings.update(cfg.get("timings") or {})
    projects = _projects(seed, q)
    sys_ids = untitled_ids(seed)
    recent = [sys_ids[2] if r == "@untitled2" else r for r in RECENT]
    selected = cfg.get("selected")
    enabled = {pid: list(v) for pid, v in ENABLED_START.items()}
    agreements = {
        f"{pid}|{s}": {"project": pid, "model": s, "t": None, "by": "Alan Turing",
                       "date": "Aug 3, 2026"}
        for pid, v in ENABLED_START.items()
        for s in v
    }
    ep.state.update(
        {
            "projects": projects,
            "recent": recent,
            "selected": selected,
            "start_selected": selected,
            "enabled": enabled,
            "enabled_start": copy.deepcopy(enabled),
            "agreements": agreements,
            "agreements_start": sorted(agreements),
            "submissions": {},
            "apis": {},
            "use_case": {},
            "program": [],
            "last_view": None,
            "timings": timings,
            "rng": random.Random(f"console-agree-{seed}"),
        }
    )
    EPISODES[ep.id] = ep


# ─── helpers ──────────────────────────────────────────────────────────────


def _project(st: dict[str, Any], pid: str | None) -> dict[str, Any] | None:
    return next((p for p in st["projects"] if p["id"] == pid), None) if pid else None


def _brief(p: dict[str, Any] | None) -> dict[str, Any] | None:
    return {"id": p["id"], "name": p["name"], "number": p["number"]} if p else None


def _select(ep: Episode, pid: str, via: str) -> None:
    st = ep.state
    if st["selected"] == pid:
        return
    ep.log("select_project", frm=st["selected"], to=pid, via=via)
    st["selected"] = pid
    st["recent"] = [pid] + [r for r in st["recent"] if r != pid]


def _row(st: dict[str, Any], p: dict[str, Any]) -> dict[str, Any]:
    return {"id": p["id"], "name": p["name"], "type": "Project", "starred": p["starred"],
            "current": p["id"] == st["selected"]}


def _enabled(st: dict[str, Any], pid: str | None, slug: str) -> bool:
    return bool(pid) and slug in st["enabled"].get(pid, [])


def _card(st: dict[str, Any], m: dict[str, Any], pid: str | None) -> dict[str, Any]:
    pub = PUBLISHERS[m["pub"]]
    out = {
        "key": key_of(m), "slug": m["slug"], "name": m["name"], "publisher": pub["name"],
        "pub": m["pub"], "color": pub["color"], "mark": pub["mark"], "desc": m["desc"],
        "kind": m["kind"], "collection": COLLECTIONS[m["kind"]], "tasks": m["tasks"],
        "features": m["features"],
    }
    if m["kind"] == "partner":
        out["enabled"] = _enabled(st, pid, m["slug"])
    return out


def _matches(m: dict[str, Any], text: str) -> bool:
    hay = " ".join([m["name"], PUBLISHERS[m["pub"]]["name"], m["desc"], m["slug"],
                    *m["tasks"]]).lower()
    return all(tok in hay for tok in text.lower().split())


FACETS = [
    ("collection", "Model collections", lambda m: [COLLECTIONS[m["kind"]]]),
    ("task", "Tasks", lambda m: m["tasks"]),
    ("publisher", "Publishers", lambda m: [PUBLISHERS[m["pub"]]["name"]]),
    ("feature", "Features", lambda m: m["features"]),
]


def _facets(models: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    for fid, title, get in FACETS:
        counts: dict[str, int] = {}
        for m in models:
            for v in get(m):
                counts[v] = counts.get(v, 0) + 1
        order = sorted(counts, key=lambda v: (-counts[v], v))
        out.append({"id": fid, "title": title,
                    "values": [{"value": v, "count": counts[v]} for v in order]})
    return out


def _filtered(models: list[dict[str, Any]], filters: list[str]) -> list[dict[str, Any]]:
    keep = models
    for f in filters:
        fid, _, val = f.partition(":")
        get = next((g for i, _, g in FACETS if i == fid), None)
        if get is not None:
            keep = [m for m in keep if val in get(m)]
    return keep


def _skus(m: dict[str, Any]) -> list[dict[str, str]]:
    pin, pout = m["price"] or (0.0, 0.0)
    rows = [
        ("Input Tokens", pin), ("Output Tokens", pout),
        ("Batch Input Tokens", pin * 0.5), ("Batch Output Tokens", pout * 0.5),
        ("Cache Write Tokens (5 min)", pin * 1.25), ("Cache Write Tokens (1 hour)", pin * 2),
        ("Cache Read Tokens", pin * 0.1),
    ]
    if m["inputs"] == "1M":
        rows += [("Long Context Input Tokens", pin * 2), ("Long Context Output Tokens", pout * 1.5),
                 ("Long Context Cache Read Tokens", pin * 0.2)]
    out = [{"sku": f"{m['name']} {n}", "region": "global", "price": f"${p:,.2f} per 1M tokens"}
           for n, p in rows]
    out.append({"sku": f"{m['name']} Web Search Requests", "region": "global",
                "price": "$10.00 per 1,000 requests"})
    return out


def _model_payload(st: dict[str, Any], m: dict[str, Any], pid: str | None) -> dict[str, Any]:
    pub = PUBLISHERS[m["pub"]]
    out = _card(st, m, pid)
    out.update(
        {
            "overview": m["overview"], "released": m["released"], "inputs": m["inputs"],
            "output": m["output"], "data_in": m["data_in"], "data_out": m["data_out"],
            "family": pub.get("family", pub["name"]), "needs": needs_of(m),
            "enabled": _enabled(st, pid, m["slug"]) if m["kind"] == "partner" else None,
            "price": m["price"],
        }
    )
    return out


def _model_from(query: dict[str, str], body: dict[str, Any]) -> dict[str, Any] | None:
    return BY_SLUG.get(slug_of(body.get("model") or query.get("model") or ""))


def _pid_from(st: dict[str, Any], query: dict[str, str], body: dict[str, Any]) -> str | None:
    pid = body.get("project") or query.get("project") or st["selected"]
    return pid if _project(st, pid) else None


def _values(body: dict[str, Any]) -> dict[str, str]:
    raw = body.get("values") or {}
    return {k: str(raw.get(k) or "").strip() for k in FIELDS}


def _errors(v: dict[str, str]) -> dict[str, str]:
    err = {}
    for k in ("name", "website", "email", "use_cases"):
        if not v[k]:
            err[k] = "This field is required."
    if v["email"] and not EMAIL_RE.match(v["email"]):
        err["email"] = "Enter a valid email address."
    for k, allowed in (("hq", COUNTRIES), ("industry", INDUSTRIES), ("users", USERS)):
        if v[k] not in allowed:
            err[k] = "Select an option."
    if v["aup"] not in ("yes", "no"):
        err["aup"] = "Select Yes or No."
    if v["aup"] == "yes" and not v["aup_detail"]:
        err["aup_detail"] = "Describe how you plan to meet the additional requirements."
    return err


DOCS = [
    ("Use Claude models on Cirrolux AI Platform", "docs.html?topic=claude",
     "Request access to Claude models, then call them with the Messages API."),
    ("Partner models in the model catalog", "docs.html?topic=partner-models",
     "How partner models are enabled, billed and governed in a project."),
    ("Restrict internet access for models", "docs.html?topic=org-policy",
     "Use organization policy constraints to block web search and fetch tools."),
    ("Quotas for partner models", "docs.html?topic=quotas",
     "Default requests-per-minute and tokens-per-minute limits by region."),
]


def _search(text: str, pid: str | None) -> list[dict[str, str]]:
    """The console-wide search: documentation and pages, never the model catalog."""
    out = []
    for title, href, snippet in DOCS:
        if any(t in (title + " " + snippet).lower() for t in text.lower().split()):
            out.append({"kind": "Documentation", "title": title, "href": href,
                        "snippet": snippet})
    for title, href in (("Model catalog", "catalog.html"), ("Agreements", "agreements.html")):
        if text.lower() in title.lower():
            out.append({"kind": "Page", "title": title, "href": href, "snippet": PLATFORM})
    return out[:12]


# ─── the state a check reads ──────────────────────────────────────────────


def view(ep: Episode) -> dict[str, Any]:
    st = ep.state
    return {
        "quirks": ep.config["quirks"],
        "selected": st["selected"],
        "start_selected": st["start_selected"],
        "enabled": {k: sorted(v) for k, v in st["enabled"].items()},
        "enabled_start": {k: sorted(v) for k, v in st["enabled_start"].items()},
        "agreements": {k: dict(v) for k, v in st["agreements"].items()},
        "agreements_start": list(st["agreements_start"]),
        "submissions": copy.deepcopy(st["submissions"]),
        "apis": dict(st["apis"]),
        "use_case": dict(st["use_case"]),
        "program": list(st["program"]),
        "last_view": st["last_view"],
        "projects": [{"id": p["id"], "name": p["name"]} for p in st["projects"]],
        "untitled_ids": untitled_ids(int(ep.config.get("seed", 5))),
    }


def session(ep: Episode, notice: str | None = None) -> dict[str, Any]:
    st = ep.state
    t = st["timings"]
    return {
        "console": CONSOLE, "platform": PLATFORM, "org": ORG, "user": USER, "email": EMAIL,
        "selected": _brief(_project(st, st["selected"])), "quirks": ep.config["quirks"],
        "timings": {"first_ms": t["first_ms"], "swap_ms": t["swap_ms"]}, "notice": notice,
    }


# ─── the page API ─────────────────────────────────────────────────────────


def backend(ep: Episode, method: str, name: str, query: dict[str, str], body: Any) -> Response:
    st = ep.state
    q = ep.config["quirks"]
    body = body if isinstance(body, dict) else {}
    if name == "session":
        pid = (query.get("project") or "").strip()
        notice = None
        if pid:
            if _project(st, pid) is None:
                notice = (f"You don't have permission to view project {pid}, or it does not exist. "
                          "Select another project.")
                ep.log("unknown_project", project=pid)
            else:
                _select(ep, pid, via="url")
        return Response(200, session(ep, notice))
    if name == "projects":
        return _projects_route(ep, query)
    if name == "catalog":
        pid = _pid_from(st, query, {})
        if pid is None:
            return Response(200, {"needs_project": True, "models": [], "facets": [], "total": 0})
        text = (query.get("q") or "").strip()
        filters = [f for f in (query.get("f") or "").split(",") if ":" in f]
        found = [BY_SLUG[s] for s in TRENDING if not text or _matches(BY_SLUG[s], text)]
        shown = _filtered(found, filters)
        ep.log("catalog_view", project=pid, q=text, f=filters, n=len(shown))
        st["last_view"] = {"route": "catalog", "project": pid, "q": text}
        return Response(
            200,
            {"models": [_card(st, m, pid) for m in shown], "facets": _facets(found),
             "total": len(shown), "apis": bool(st["apis"].get(pid))},
        )
    if name == "suggest":
        text = (query.get("q") or "").strip()
        hits = [BY_SLUG[s] for s in TRENDING if text and _matches(BY_SLUG[s], text)]
        return Response(200, [{"key": key_of(m), "name": m["name"], "desc": m["desc"]}
                              for m in hits[:5]])
    if name == "model":
        m = _model_from(query, body)
        if m is None:
            return Response(404, {"error": "That model is not in the catalog."})
        pid = _pid_from(st, query, body)
        ep.log("model_view", model=m["slug"], project=pid)
        st["last_view"] = {"route": "model", "model": m["slug"], "project": pid}
        return Response(200, _model_payload(st, m, pid))
    if name == "questionnaire" and method == "GET":
        m = _model_from(query, body)
        pid = _pid_from(st, query, body)
        if m is None or needs_of(m) != "questionnaire":
            return Response(404, {"error": "This model has no enablement questionnaire."})
        ep.log("questionnaire_view", model=m["slug"], project=pid)
        st["last_view"] = {"route": "questionnaire", "model": m["slug"], "project": pid}
        return Response(
            200,
            {"model": _model_payload(st, m, pid), "project": _brief(_project(st, pid)),
             "countries": COUNTRIES, "industries": INDUSTRIES, "users": USERS,
             "preset": {"industry": "Agriculture"} if q["preset_industry"] else {}},
        )
    if name == "terms" and method == "GET":
        m = _model_from(query, body)
        pid = _pid_from(st, query, body)
        if m is None or m["kind"] != "partner":
            return Response(404, {"error": "This model has no terms to accept."})
        k = f"{pid}|{m['slug']}"
        if needs_of(m) == "questionnaire" and k not in st["submissions"] and k not in st[
            "agreements"
        ]:
            return Response(409, {"error": "Fill out the enablement questionnaire first.",
                                  "go": f"questionnaire.html?model={key_of(m)}&project={pid}"})
        ep.log("terms_view", model=m["slug"], project=pid)
        st["last_view"] = {"route": "terms", "model": m["slug"], "project": pid}
        a = st["agreements"].get(k)
        return Response(
            200,
            {"model": _model_payload(st, m, pid), "project": _brief(_project(st, pid)),
             "skus": _skus(m), "agreed": bool(a), "agreed_on": a and (a.get("date") or TODAY)},
        )
    if name == "agreements":
        pid = _pid_from(st, query, body)
        ep.log("agreements_view", project=pid)
        st["last_view"] = {"route": "agreements", "project": pid}
        rows = []
        for k, a in st["agreements"].items():
            if a["project"] != pid:
                continue
            m = BY_SLUG[a["model"]]
            rows.append({"name": m["name"], "key": key_of(m),
                         "publisher": PUBLISHERS[m["pub"]]["name"], "status": "Active",
                         "date": a.get("date") or TODAY, "by": a.get("by") or USER})
        return Response(200, {"project": _brief(_project(st, pid)), "rows": rows})
    if name == "search":
        text = (query.get("q") or "").strip()
        ep.log("global_search", q=text)
        return Response(200, {"q": text, "results": _search(text, st["selected"]) if text else []})
    if name == "program" and method == "GET":
        m = _model_from(query, body)
        if m is None:
            return Response(404, {"error": "That model is not in the catalog."})
        pid = _pid_from(st, query, body)
        st["last_view"] = {"route": "program", "model": m["slug"], "project": pid}
        return Response(200, {"model": _model_payload(st, m, pid)})
    if method != "POST":
        return Response(405, {"error": "POST only"})

    # ─── state changes ──────────────────────────────────────────────────
    if name == "select":
        pid = str(body.get("project") or "")
        if _project(st, pid) is None:
            return Response(404, {"error": "No such project."})
        _select(ep, pid, via=str(body.get("via") or "picker"))
        return Response(200, {"ok": True, "selected": _brief(_project(st, pid))})
    if name == "star":
        p = _project(st, str(body.get("project") or ""))
        if p is None:
            return Response(404, {"error": "No such project."})
        p["starred"] = bool(body.get("on"))
        ep.log("star", project=p["id"], on=p["starred"])
        return Response(200, {"ok": True, "starred": p["starred"]})
    if name == "apis":
        pid = _pid_from(st, query, body)
        if pid is None:
            return Response(409, {"error": "Select a project first."})
        st["apis"][pid] = True
        ep.log("apis_enabled", project=pid)
        return Response(200, {"ok": True}, delay_s=0.8)
    m = _model_from(query, body)
    if m is None:
        return Response(404, {"error": "That model is not in the catalog."})
    pid = _pid_from(st, query, body)
    if name == "use_case":
        choice = "program" if body.get("choice") == "program" else "standard"
        st["use_case"][f"{pid}|{m['slug']}"] = choice
        ep.log("use_case", model=m["slug"], project=pid, choice=choice)
        return Response(200, {"ok": True})
    if pid is None:
        return Response(409, {"error": "Select a project first."})
    k = f"{pid}|{m['slug']}"
    if name == "enable":
        use = "program" if body.get("use_case") == "program" else "standard"
        ep.log("enable_clicked", model=m["slug"], project=pid, use_case=use)
        if use == "program":
            st["program"].append({"model": m["slug"], "project": pid, "t": time.time()})
            ep.log("program_started", model=m["slug"], project=pid)
            return Response(200, {"go": f"program.html?model={key_of(m)}&project={pid}"})
        if _enabled(st, pid, m["slug"]):
            return Response(200, {"ok": True, "enabled": True})
        need = needs_of(m)
        if need == "questionnaire" and k not in st["agreements"]:
            return Response(200, {"go": f"questionnaire.html?model={key_of(m)}&project={pid}"})
        if need in ("questionnaire", "terms"):
            return Response(200, {"go": f"terms.html?model={key_of(m)}&project={pid}"})
        return Response(400, {"error": "This model does not need to be enabled."})
    if name == "questionnaire":
        v = _values(body)
        err = _errors(v)
        if err:
            ep.log("questionnaire_invalid", model=m["slug"], project=pid, fields=sorted(err))
            return Response(400, {"errors": err})
        st["submissions"][k] = {**v, "t": round(time.time() - ep.started, 3)}
        ep.log("questionnaire_submitted", model=m["slug"], project=pid, values=v)
        return Response(200, {"ok": True, "go": f"terms.html?model={key_of(m)}&project={pid}"})
    if name == "agree":
        if body.get("accepted") is not True:
            return Response(400, {"error": "Agree to the terms and agreements before continuing."})
        if needs_of(m) == "questionnaire" and k not in st["submissions"] and k not in st[
            "agreements"
        ]:
            return Response(409, {"error": "Fill out the enablement questionnaire first."})
        st["agreements"][k] = {"project": pid, "model": m["slug"],
                               "t": round(time.time() - ep.started, 3), "by": USER, "date": TODAY}
        if m["slug"] not in st["enabled"].setdefault(pid, []):
            st["enabled"][pid].append(m["slug"])
        ep.log("agree", model=m["slug"], project=pid)
        lo, hi = st["timings"]["agree_s"]
        delay = st["rng"].uniform(lo, hi) if q["slow_agree"] else 0.2
        return Response(200, {"ok": True, "model": m["name"]}, delay_s=delay)
    if name == "program":
        ep.log("program_submitted", model=m["slug"], project=pid)
        return Response(200, {"ok": True})
    return Response(404, {"error": f"no route {name}"})


def _projects_route(ep: Episode, query: dict[str, str]) -> Response:
    st = ep.state
    q = ep.config["quirks"]
    projects = st["projects"]
    text = (query.get("q") or "").strip().lower()
    if text:
        hits = [p for p in projects if text in p["name"].lower() or text in p["id"].lower()]
        page = max(0, int(query.get("page") or 0))
        size = PAGE_ROWS if q["paged_picker"] else max(1, len(hits))
        rows = hits[page * size:(page + 1) * size]
        ep.log("picker_search", q=text, page=page, total=len(hits), ids=[p["id"] for p in rows])
        return Response(200, {"mode": "search", "rows": [_row(st, p) for p in rows],
                              "total": len(hits), "page": page, "size": size})
    tab = query.get("tab") or "recent"
    if tab == "starred":
        rows = [p for p in projects if p["starred"]]
        return Response(200, {"mode": tab, "rows": [_row(st, p) for p in rows]})
    if tab == "all":
        offset = max(0, int(query.get("offset") or 0))
        size = ALL_ROWS if q["paged_picker"] else len(projects)
        rows = projects[offset:offset + size]
        ep.log("picker_all", offset=offset, ids=[p["id"] for p in rows])
        delay = float(st["timings"]["more_s"]) if offset and q["paged_picker"] else 0.0
        return Response(
            200,
            {"mode": tab, "org": ORG, "rows": [_row(st, p) for p in rows],
             "more": offset + size < len(projects), "offset": offset + len(rows)},
            delay_s=delay,
        )
    rows = [p for r in st["recent"] if (p := _project(st, r))]
    return Response(200, {"mode": "recent", "rows": [_row(st, p) for p in rows]})
