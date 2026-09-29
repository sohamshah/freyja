"""Task-type prediction from real Freyja sessions.

State: the user's opening request only. Truth: which capability the assistant
actually used in that turn, from the tool calls in the session log. This is the
routing decision Freyja would make before acting (which tools/skills to load,
whether to spawn research or computer-use workers), with a real outcome and no
possibility of contamination.

Two kinds of question on the same state:
  task_type (Choice, 11 options + none): the distinctive capability, by a fixed
    priority so a research task that ends by writing a report counts as
    web_research, not file_edit;
  needs_* (Noul ×4): whether each family was used at all (multi-label).
"""
from __future__ import annotations

import random
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from freyja import SESSIONS_DIR, load_turns  # noqa: E402
from schema import CASES_DIR, Case, Question, write_cases  # noqa: E402

SEED = 20260920
CAP_PER_CLASS = 200

FAMILIES: dict[str, set[str]] = {
    "computer_use": {"screenshot", "click", "type_text", "press_key", "scroll", "key_down", "key_up", "move_mouse",
                     "find_element", "read_ax_tree", "focus_window", "list_windows", "list_displays", "computer_use", "computer", "cursor_position", "wait"},
    "browser": {"browser_execute_js", "browser_screenshot"},
    "delegation": {"sub_agent", "subagents", "talk", "list_agent_sessions"},
    "web_research": {"web_search", "web_fetch", "web_research", "twitter_search"},
    "media_generation": {"generate_image", "generate_svg", "generate_sound_effect", "analyze_video"},
    "slack": set(),  # filled by prefix match mcp__slack__
    "widget": {"show_widget", "widget_spec"},
    "file_edit": {"write_file", "edit_file", "edit_json"},
    "shell": {"bash"},
    "read_explore": {"read_file", "grep", "glob", "list_directory", "artifacts", "view_image"},
}
PRIORITY = ["computer_use", "browser", "delegation", "web_research", "media_generation", "slack", "widget", "file_edit", "shell", "read_explore"]
IGNORED = {"memory", "session_memory", "working_memory", "recall", "tasks", "list_skills", "search_skills", "load_skill",
           "summarize_context", "tool_search", "schedule", "record_user_preference", "kanban", "read_findings", "publish_finding"}

DESCRIPTIONS = {
    "computer_use": "drive native macOS apps by screenshot, click, and keyboard",
    "browser": "inspect or drive a web page through the browser (DevTools)",
    "delegation": "spawn or coordinate sub-agents",
    "web_research": "search or fetch the web",
    "media_generation": "generate images, SVG, or audio",
    "slack": "read or post in Slack",
    "widget": "render an interactive widget or dashboard in the chat",
    "file_edit": "create or edit files",
    "shell": "run shell commands (tests, git, scripts) without editing files",
    "read_explore": "read or search files only",
    "answer_only": "answer directly with no tools",
    "none_of_these": "cannot tell from the request",
}


def family_of(tool: str) -> str | None:
    if tool.startswith("mcp__slack__"):
        return "slack"
    for fam, names in FAMILIES.items():
        if tool in names:
            return fam
    return None


def label_turn(tools: list[str]) -> tuple[str, set[str]]:
    used = {family_of(t) for t in tools if t not in IGNORED}
    used.discard(None)
    for fam in PRIORITY:
        if fam in used:
            return fam, used
    return "answer_only", used


def main() -> None:
    turns, stats = load_turns(SESSIONS_DIR, min_users=1, include_sub=True)
    first = [t for t in turns if t.turn_index == 0]
    print(f"{len(turns)} turns, {len(first)} opening turns")
    options = {k: DESCRIPTIONS[k] for k in PRIORITY + ["answer_only", "none_of_these"]}
    q_type = Question("task_type", "choice",
                      "Which capability will the assistant most distinctively need to handle this request? Pick one. "
                      "Order of precedence when several apply: computer_use > browser > delegation > web_research > "
                      "media_generation > slack > widget > file_edit > shell > read_explore > answer_only.", options=options)
    nouls = [
        Question("needs_web", "noul", "Will handling this request require searching or fetching the web?"),
        Question("needs_edit", "noul", "Will handling this request require creating or editing files?"),
        Question("needs_browser_or_computer", "noul", "Will handling this request require driving a browser or a native app (screenshots, clicks, DevTools)?"),
        Question("needs_delegation", "noul", "Will the assistant spawn sub-agents to handle this request?"),
    ]
    by_class: dict[str, list[Case]] = {}
    for t in first:
        tools = [tc.get("name", "") for tc in t.tool_calls]
        label, used = label_turn(tools)
        labels = {
            "task_type": label,
            "needs_web": "web_research" in used,
            "needs_edit": "file_edit" in used,
            "needs_browser_or_computer": bool(used & {"browser", "computer_use"}),
            "needs_delegation": "delegation" in used,
        }
        c = Case(f"freyja_tasktype-{t.session_id[-8:]}", "freyja_tasktype", "classification",
                 {"user_request": t.user_text[:1500]}, [q_type] + nouls, labels, "trace_tools",
                 {"n_tool_calls": len(tools), "families_used": sorted(used), "session_title": t.session_title,
                  "origin": "subagent" if t.session_id.startswith("sub_") else "human"})
        by_class.setdefault(label, []).append(c)
    rng = random.Random(SEED)
    cases = []
    for label, cs in by_class.items():
        rng.shuffle(cs)
        cases.extend(cs[:CAP_PER_CLASS])
    rng.shuffle(cases)
    write_cases(cases, CASES_DIR / "freyja_tasktype.jsonl")
    dist = Counter(c.labels["task_type"] for c in cases)
    print(len(cases), "cases; class distribution:", dict(dist.most_common()))
    print("origin:", dict(Counter(c.meta["origin"] for c in cases)))
    for q in nouls:
        print(q.name, "positive rate", round(sum(c.labels[q.name] for c in cases) / len(cases), 3))


if __name__ == "__main__":
    main()
