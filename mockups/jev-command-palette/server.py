#!/usr/bin/env python3
"""Jev Generative UI Prototype Server with Hierarchical Sub-routing.

Serves the standalone Command+K prototype app and provides a real-time
proxy to TypeSafe AI's Jev decision model endpoint with JSON-Render widget synthesis.
"""

from __future__ import annotations

import json
import os
import re
import time
import urllib.request
import urllib.error
from http.server import HTTPServer, SimpleHTTPRequestHandler
from pathlib import Path
from typing import Any, Dict

PORT = 9500
ROOT_DIR = Path(__file__).resolve().parent
ENV_PATH = ROOT_DIR.parents[1] / ".env"

TOOL_CATALOG = {
    "bash": {
        "name": "bash",
        "category": "Shell / Execution",
        "risk": "HIGH",
        "description": "Execute a bash shell command with timeout and risk classification (Low, Medium, High, Dangerous).",
        "example": 'bash(command="git status", summary="check git status")',
        "parameters": [
            {"name": "command", "type": "string", "required": True, "description": "The bash shell command to execute"},
            {"name": "summary", "type": "string", "required": True, "description": "Brief user-facing summary of what the command does"},
            {"name": "timeout", "type": "number", "required": False, "description": "Timeout in seconds (default: 120)"},
            {"name": "working_dir", "type": "string", "required": False, "description": "Working directory for the command"}
        ]
    },
    "jev_computer_use": {
        "name": "jev_computer_use",
        "category": "Computer Automation",
        "risk": "MEDIUM",
        "description": "Drive a macOS native app or browser DOM using the fast ~200ms Jev decision model loop with safety gates.",
        "example": 'jev_computer_use(app="Arc", goal="Open https://github.com and check notifications")',
        "parameters": [
            {"name": "goal", "type": "string", "required": True, "description": "App-scoped goal with visible success criteria"},
            {"name": "app", "type": "string", "required": False, "description": "Bundle id or app name (Arc, Chrome, Finder, Calculator)"},
            {"name": "max_steps", "type": "integer", "required": False, "description": "Cap on decision steps (default: 40, max: 120)"},
            {"name": "allow_irreversible", "type": "boolean", "required": False, "description": "Permit controls that look irreversible (default: false)"}
        ]
    },
    "twitter_search": {
        "name": "twitter_search",
        "category": "Web / Social Intelligence",
        "risk": "LOW",
        "description": "Search Twitter/X for posts, threads, user timelines, and topics with image and video frame analysis.",
        "example": 'twitter_search(query="Read and summarize https://x.com/user/status/123", include_videos=True)',
        "parameters": [
            {"name": "query", "type": "string", "required": True, "description": "Expert natural language question, topic, or tweet URL"},
            {"name": "include_images", "type": "boolean", "required": False, "description": "Enable vision analysis of images in posts"},
            {"name": "include_videos", "type": "boolean", "required": False, "description": "Enable video extraction and breakdown"}
        ]
    },
    "read_file": {
        "name": "read_file",
        "category": "Filesystem",
        "risk": "LOW",
        "description": "Read the contents of a text file from the filesystem with line numbering and optional line ranges.",
        "example": 'read_file(path="src/renderer/App.tsx", start_line=1, end_line=50)',
        "parameters": [
            {"name": "path", "type": "string", "required": True, "description": "The path to the file to read"},
            {"name": "start_line", "type": "integer", "required": False, "description": "Starting line number (1-indexed)"},
            {"name": "end_line", "type": "integer", "required": False, "description": "Ending line number (inclusive)"}
        ]
    },
    "sub_agent": {
        "name": "sub_agent",
        "category": "Coordination / Swarm",
        "risk": "MEDIUM",
        "description": "Spawn an autonomous sub-agent with specialized model, thinking level, and toolset in the background.",
        "example": 'sub_agent(label="review-pr", task="Critique PR #1321 for security bugs", agent_type="review")',
        "parameters": [
            {"name": "label", "type": "string", "required": True, "description": "Short human-friendly name (<40 chars)"},
            {"name": "task", "type": "string", "required": True, "description": "Self-contained task prompt for the worker"},
            {"name": "agent_type", "type": "enum", "required": False, "description": "Profile: general, code, review, explore, test, verify"}
        ]
    }
}

def get_api_key() -> str:
    key = os.environ.get("TYPESAFE_AI_API_KEY") or os.environ.get("TYPESAFE_API_KEY")
    if key:
        return key
    if ENV_PATH.exists():
        for line in ENV_PATH.read_text().splitlines():
            line = line.strip()
            if line.startswith("TYPESAFE_AI_API_KEY=") or line.startswith("TYPESAFE_API_KEY="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    raise RuntimeError("TYPESAFE_AI_API_KEY not found in environment or .env")

def extract_entities(query: str, domain: str, sub_target: str) -> Dict[str, Any]:
    """Lightweight deterministic parameter extraction for slot filling."""
    entities: Dict[str, Any] = {}
    q_lower = query.lower()

    if domain == "tool_inspect":
        for tname in TOOL_CATALOG.keys():
            if tname in q_lower:
                entities["tool_name"] = tname
                break
        if "tool_name" not in entities:
            entities["tool_name"] = "bash"

    elif domain == "calendar":
        attendee_match = re.search(r"with\s+([a-zA-Z0-9_\-\.]+)", query, re.I)
        entities["attendee"] = attendee_match.group(1).capitalize() if attendee_match else "Konstantin"
        entities["attendee_email"] = f"{entities['attendee'].lower()}@freyja.internal"
        
        day_match = re.search(r"(next\s+(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday)|tomorrow|today)", query, re.I)
        entities["day"] = day_match.group(1).title() if day_match else ("Tomorrow" if "tomorrow" in q_lower else "Next Tuesday")
        
        time_match = re.search(r"(\d{1,2}(?::\d{2})?\s*(?:am|pm)?|\d{1,2}\s*(?:am|pm))", query, re.I)
        entities["time"] = time_match.group(1).upper() if time_match else "4:00 PM"
        entities["title"] = f"Sync with {entities['attendee']}"

    elif domain == "memory":
        person_match = re.search(r"(?:from|by|with)\s+([a-zA-Z0-9_\-\.]+)", query, re.I)
        entities["person"] = person_match.group(1).capitalize() if person_match else "Konstantin"
        place_match = re.search(r"(?:in|for|at)\s+([a-zA-Z0-9_\-\.]+)", query, re.I)
        entities["topic"] = place_match.group(1).capitalize() if place_match else "Lisbon"

    elif domain == "model_picker":
        if "sonnet" in q_lower:
            entities["target_model"] = "claude-sonnet-5-5"
            entities["model_name"] = "Claude 3.5 Sonnet"
        elif "opus" in q_lower:
            entities["target_model"] = "claude-opus-5-5"
            entities["model_name"] = "Claude 3 Opus"
        elif "luna" in q_lower:
            entities["target_model"] = "gpt-6-luna"
            entities["model_name"] = "GPT-6 Luna"
        elif "gemini" in q_lower:
            entities["target_model"] = "gemini-3.8-flash"
            entities["model_name"] = "Gemini 1.5 Pro"
        else:
            entities["target_model"] = "claude-sonnet-5-5"
            entities["model_name"] = "Claude 3.5 Sonnet"

    elif domain == "quick_control":
        if "dark" in q_lower:
            entities["requested_theme"] = "dark"
        elif "light" in q_lower:
            entities["requested_theme"] = "light"
        elif "system" in q_lower:
            entities["requested_theme"] = "system"

    elif domain == "agent_task":
        pr_match = re.search(r"#?(\d{3,5})", query)
        entities["pr_number"] = pr_match.group(1) if pr_match else "1321"
        repo_match = re.search(r"(agent-harness|ema-next|freyja)", query, re.I)
        entities["repo"] = repo_match.group(1) if repo_match else "agent-harness"

    return entities

def build_declarative_widget(domain: str, sub_target: str, query: str, entities: Dict[str, Any], confidence: float, readiness: float) -> Dict[str, Any]:
    """Build a json-render compliant declarative component tree with hierarchical sub-routing."""
    
    # Tool Inspector / Details Widget
    if domain == "tool_inspect" or "tool detail" in query.lower() or "tool info" in query.lower():
        tname = entities.get("tool_name", "bash")
        tdata = TOOL_CATALOG.get(tname, TOOL_CATALOG["bash"])
        return {
            "type": "ToolDetailWidget",
            "version": "1.0",
            "meta": {"title": f"Tool: {tdata['name']}", "badge": f"Risk: {tdata['risk']}", "ready": True},
            "data": tdata
        }

    # Diagnostic Logs Widget
    if domain == "diagnostics" or "diagnostic" in query.lower() or "debug log" in query.lower():
        return {
            "type": "DiagnosticLogsWidget",
            "version": "1.0",
            "meta": {"title": "Diagnostic Logs", "badge": "Healthy", "ready": True},
            "data": {
                "logs": [
                    {"time": "00:52:14.210", "level": "INFO", "subsystem": "BRIDGE", "message": "Daemon healthy on port 8114 · ping 0.8ms"},
                    {"time": "00:51:48.092", "level": "INFO", "subsystem": "RENDERER", "message": "Seam choreography mounted · 12 items folded"},
                    {"time": "00:50:11.834", "level": "INFO", "subsystem": "MCP", "message": "Slack MCP gateway authenticated (user scope) · 27 tools active"},
                    {"time": "00:48:32.411", "level": "INFO", "subsystem": "JEV", "message": "System One classifier p50 latency 142ms · 0 gate escalations"},
                    {"time": "00:44:19.002", "level": "INFO", "subsystem": "SCHEDULER", "message": "Cron daemon tick · 0 jobs due"}
                ]
            }
        }

    # Swarm Status Widget
    if domain == "swarm" or "swarm status" in query.lower() or "active subagent" in query.lower():
        return {
            "type": "SwarmStatusWidget",
            "version": "1.0",
            "meta": {"title": "Swarm Monitor", "badge": "Active Lanes", "ready": True},
            "data": {
                "running_count": 2,
                "lanes": [
                    {
                        "label": "review-pr-1321",
                        "profile": "review",
                        "model": "gpt-5.5",
                        "status": "running",
                        "elapsed": "42s",
                        "current_tool": "read_file(path='tests/knowledge/test_reference.py')"
                    },
                    {
                        "label": "explore-tier-routing",
                        "profile": "explore",
                        "model": "claude-sonnet-5-5",
                        "status": "done",
                        "elapsed": "88s",
                        "current_tool": "completed with memo"
                    }
                ]
            }
        }

    # Calendar View / Agenda Widget
    if domain == "calendar" and (sub_target == "calendar_view" or "what meetings" in query.lower() or "schedule" in query.lower()):
        day_str = entities.get("day", "Tomorrow")
        return {
            "type": "CalendarAgendaWidget",
            "version": "1.0",
            "meta": {"title": f"Agenda for {day_str}", "badge": "Google Calendar MCP", "ready": True},
            "data": {
                "day": day_str,
                "date_full": "Thursday, Oct 22, 2026",
                "summary": "3 meetings · 2.5h scheduled · 5.5h focus time",
                "events": [
                    {
                        "time": "09:30 – 10:00",
                        "title": "Autopilot Architecture Sync",
                        "attendees": ["Johnny Meng", "Soham Shah"],
                        "link": "meet.google.com/xyz-abc-def",
                        "status": "confirmed"
                    },
                    {
                        "time": "13:00 – 14:00",
                        "title": "Platform Tier Routing & Fusion Review",
                        "attendees": ["Anna", "Manju", "Soham Shah"],
                        "link": "meet.google.com/ema-tier-rev",
                        "status": "confirmed"
                    },
                    {
                        "time": "16:00 – 16:30",
                        "title": "1:1 with Konstantin",
                        "attendees": ["Konstantin", "Soham Shah"],
                        "link": "meet.google.com/kn-sync",
                        "status": "confirmed"
                    }
                ],
                "actions": [
                    {"id": "join_next", "label": "Join Next Meeting (09:30)", "variant": "primary"},
                    {"id": "open_gcal", "label": "Open Google Calendar", "variant": "secondary"}
                ]
            }
        }

    # Calendar Create / Scheduling Widget
    if domain == "calendar":
        return {
            "type": "CalendarWidget",
            "version": "1.0",
            "meta": {"title": "New Calendar Event", "badge": "Slot Parsed", "ready": True},
            "data": {
                "title": entities.get("title", "Quick Sync"),
                "date": f"{entities.get('day', 'Next Tuesday')}, Oct 27, 2026",
                "time": f"{entities.get('time', '4:00 PM')} – 4:30 PM PDT",
                "attendee": entities.get("attendee", "Konstantin"),
                "attendee_email": entities.get("attendee_email", "konstantin@freyja.internal"),
                "availability": [
                    {"time": "14:00", "status": "busy"},
                    {"time": "14:30", "status": "busy"},
                    {"time": "15:00", "status": "free"},
                    {"time": "15:30", "status": "free"},
                    {"time": "16:00", "status": "free", "selected": True},
                    {"time": "16:30", "status": "free"},
                    {"time": "17:00", "status": "busy"}
                ],
                "actions": [
                    {"id": "send_invite", "label": "Send Invite", "variant": "primary"},
                    {"id": "edit_details", "label": "Edit in Calendar", "variant": "secondary"}
                ]
            }
        }

    # Granular Control: Appearance Only
    if domain == "quick_control" and (sub_target == "appearance_toggle" or "appearance" in query.lower() or "dark mode" in query.lower() or "light mode" in query.lower()):
        req_theme = entities.get("requested_theme", "dark")
        return {
            "type": "AppearanceControlWidget",
            "version": "1.0",
            "meta": {"title": "Appearance", "badge": "Quick Switch", "ready": True},
            "data": {
                "active_theme": req_theme,
                "options": [
                    {"id": "dark", "label": "Dark (Obsidian)", "description": "Near-black #0a0a0a with sky accent", "selected": req_theme == "dark"},
                    {"id": "light", "label": "Light (Paper)", "description": "Warm bone #f8f5ef with steel-blue accent", "selected": req_theme == "light"},
                    {"id": "system", "label": "Follow System", "description": "Mirrors macOS day/night schedule", "selected": req_theme == "system"}
                ],
                "actions": [
                    {"id": "apply_theme", "label": f"Apply {req_theme.title()} Theme", "variant": "primary"}
                ]
            }
        }

    # Granular Control: Microphone Toggle Only
    if domain == "quick_control" and (sub_target == "mic_toggle" or "mic" in query.lower() or "mute" in query.lower()):
        return {
            "type": "MicControlWidget",
            "version": "1.0",
            "meta": {"title": "Audio Input", "badge": "Hardware Gate", "ready": True},
            "data": {
                "device": "MacBook Pro Microphone",
                "muted": True if "mute" in query.lower() else False,
                "level": "42%",
                "actions": [
                    {"id": "toggle_mic", "label": "Toggle Mute", "variant": "primary"},
                    {"id": "audio_settings", "label": "Audio Settings", "variant": "secondary"}
                ]
            }
        }

    # General Quick Controls Grid
    if domain == "quick_control":
        return {
            "type": "QuickControlWidget",
            "version": "1.0",
            "meta": {"title": "Environment Controls", "badge": "Instant", "ready": True},
            "data": {
                "controls": [
                    {"id": "dark_mode", "label": "Appearance", "type": "toggle", "value": True, "description": "Dark glass theme"},
                    {"id": "mute_mic", "label": "Microphone", "type": "toggle", "value": False, "description": "MacBook Pro Mic"},
                    {"id": "focus_mode", "label": "Do Not Disturb", "type": "toggle", "value": True, "description": "Notifications silenced"},
                    {"id": "pomodoro", "label": "Focus Timer", "type": "timer", "value": "24:50", "description": "Active session"}
                ],
                "actions": [{"id": "save_prefs", "label": "Done", "variant": "primary"}]
            }
        }

    # Morning Room / Daily Briefing Widget
    if domain == "briefing":
        return {
            "type": "MorningBriefingWidget",
            "version": "1.0",
            "meta": {"title": "Today's Briefing", "badge": "Morning Room", "ready": True},
            "data": {
                "date": "Wednesday, October 21, 2026",
                "staged_plan": [
                    {"step": "01", "task": "Jev Generative UI in Command Palette", "status": "active"},
                    {"step": "02", "task": "EMU Tier Routing benchmark pass", "status": "pending"},
                    {"step": "03", "task": "Agent harness token audit", "status": "pending"}
                ],
                "decisions": [
                    {"title": "TypeSafe image grounding", "note": "Keep planner coordinates on own captures"},
                    {"title": "Prose styling", "note": "No em-dashes in body prose (slop lint active)"}
                ],
                "active_sessions": 3,
                "actions": [
                    {"id": "open_morning_room", "label": "Open Morning Room (⌘⇧B)", "variant": "primary"},
                    {"id": "stage_tasks", "label": "Stage Daily Tasks", "variant": "secondary"}
                ]
            }
        }

    # Token Usage & Spend Widget
    if domain == "token_usage":
        return {
            "type": "TokenUsageWidget",
            "version": "1.0",
            "meta": {"title": "Token Usage & Spend", "badge": "Last 7 Days", "ready": True},
            "data": {
                "total_spend": "$42.18",
                "total_tokens": "14.8M",
                "cache_read_rate": "82%",
                "cache_savings": "$68.40",
                "models": [
                    {"name": "Claude 3.5 Sonnet", "spend": "$22.40", "tokens": "6.2M", "share": 53},
                    {"name": "Claude 3 Opus", "spend": "$14.10", "tokens": "1.4M", "share": 33},
                    {"name": "GPT-5.5", "spend": "$4.80", "tokens": "2.8M", "share": 11},
                    {"name": "Gemini 1.5 Pro", "spend": "$0.88", "tokens": "4.4M", "share": 3}
                ],
                "actions": [
                    {"id": "open_metrics", "label": "Open Metrics Dashboard", "variant": "primary"},
                    {"id": "export_spend_csv", "label": "Export CSV", "variant": "secondary"}
                ]
            }
        }

    # Model Picker Widget
    if domain == "model_picker":
        target = entities.get("target_model", "claude-sonnet-5-5")
        return {
            "type": "ModelPickerWidget",
            "version": "1.0",
            "meta": {"title": "Model Routing", "badge": "Active Session", "ready": True},
            "data": {
                "active_model": "claude-sonnet-5-5",
                "models": [
                    {"id": "claude-sonnet-5-5", "name": "Claude 3.5 Sonnet", "cost": "$3.00 / MTok", "latency": "180ms p50", "recommended": "Coding & tool loops", "selected": target == "claude-sonnet-5-5"},
                    {"id": "claude-opus-5-5", "name": "Claude 3 Opus", "cost": "$15.00 / MTok", "latency": "650ms p50", "recommended": "Deep architecture & research", "selected": target == "claude-opus-5-5"},
                    {"id": "gpt-5.5", "name": "GPT-5.5", "cost": "$5.00 / MTok", "latency": "220ms p50", "recommended": "Independent review & verification", "selected": target == "gpt-5.5"},
                    {"id": "gpt-6-luna", "name": "GPT-6 Luna", "cost": "$0.20 / MTok", "latency": "45ms p50", "recommended": "Fast triage & fanout", "selected": target == "gpt-6-luna"},
                    {"id": "gemini-3.8-flash", "name": "Gemini 1.5 Pro", "cost": "$1.25 / MTok", "latency": "210ms p50", "recommended": "Long context & synthesis", "selected": target == "gemini-3.8-flash"}
                ],
                "actions": [
                    {"id": "switch_model", "label": f"Switch to {entities.get('model_name', 'Claude 3.5 Sonnet')}", "variant": "primary"},
                    {"id": "benchmark_models", "label": "Compare Benchmarks", "variant": "secondary"}
                ]
            }
        }

    # Artifact Explorer Widget
    if domain == "artifact_explorer":
        return {
            "type": "ArtifactExplorerWidget",
            "version": "1.0",
            "meta": {"title": "Cross-Session Artifacts", "badge": "Verified Files", "ready": True},
            "data": {
                "artifacts": [
                    {"name": "voice-and-slop-guide.html", "session": "session-mufu8atv", "size": "24.2 KB", "type": "HTML Report", "age": "2d ago"},
                    {"name": "RESULTS.md", "session": "session-mvhrjdlr", "size": "8.4 KB", "type": "Benchmark", "age": "yesterday"},
                    {"name": "ema-security-v3.html", "session": "session-mve48", "size": "112 KB", "type": "Design Doc", "age": "3d ago"}
                ],
                "actions": [
                    {"id": "open_artifacts", "label": "Browse All Artifacts (⌘⇧A)", "variant": "primary"},
                    {"id": "copy_path", "label": "Copy Latest Path", "variant": "secondary"}
                ]
            }
        }

    # Voice Session Widget
    if domain == "voice_session":
        return {
            "type": "VoiceLauncherWidget",
            "version": "1.0",
            "meta": {"title": "Real-time Voice Channel", "badge": "Valkyrie", "ready": True},
            "data": {
                "persona": "Valkyrie (Technical Partner)",
                "input_device": "MacBook Pro Microphone",
                "latency": "120ms p50",
                "status": "Ready to connect",
                "actions": [
                    {"id": "start_voice", "label": "Start Voice Conversation", "variant": "primary"},
                    {"id": "voice_settings", "label": "Settings", "variant": "secondary"}
                ]
            }
        }

    # Memory Widget
    if domain == "memory":
        return {
            "type": "MemoryWidget",
            "version": "1.0",
            "meta": {"title": "Recalled Memory & Transcript", "badge": "High Relevance", "ready": True},
            "data": {
                "matches": [
                    {
                        "id": "mem_01",
                        "title": f"Dinner recommendation by {entities.get('person', 'Konstantin')}",
                        "source": "#trip-lisbon",
                        "date": "Sep 14, 2026",
                        "timestamp": "14:22",
                        "snippet": "You have to book Prado on Thursday evening. Get the sourdough ice cream with olive oil and sea salt. It's unforgettable.",
                        "tags": ["Lisbon", "Restaurants", "Konstantin"]
                    },
                    {
                        "id": "mem_02",
                        "title": f"Lisbon hotel & walking itinerary from {entities.get('person', 'Konstantin')}",
                        "source": "session-mu9i4his",
                        "date": "Sep 12, 2026",
                        "timestamp": "08:15",
                        "snippet": "Stay in Chiado or Alfama. Ramiro is great for casual seafood, but Prado requires booking 2 weeks ahead.",
                        "tags": ["Travel", "Hotels"]
                    }
                ],
                "actions": [
                    {"id": "copy_snippet", "label": "Copy Quote", "variant": "primary"},
                    {"id": "open_transcript", "label": "View Source", "variant": "secondary"}
                ]
            }
        }

    # Agent Task Widget
    if domain == "agent_task":
        pr_num = entities.get("pr_number", "1321")
        repo = entities.get("repo", "agent-harness")
        return {
            "type": "AgentTaskWidget",
            "version": "1.0",
            "meta": {"title": "Delegate to Sub-agent", "badge": "Freyja Swarm", "ready": True},
            "data": {
                "task_title": f"Review PR #{pr_num} on {repo}",
                "suggested_profile": "review",
                "model": "gpt-5.5 (independent review model)",
                "objective": f"Critical bug and contract review of PR #{pr_num} against main. Check prompt/tool schemas, test coverage, and regression risks.",
                "flags": ["read_only", "diff_anchors_validated", "no_em_dashes"],
                "actions": [
                    {"id": "spawn_agent", "label": "Spawn Sub-agent", "variant": "primary"},
                    {"id": "open_gh", "label": "View PR", "variant": "secondary"}
                ]
            }
        }

    return {
        "type": "StandardPalette",
        "version": "1.0",
        "meta": {"title": "Command Palette", "badge": "Standard Search", "ready": True},
        "data": {}
    }


class JevPaletteHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(ROOT_DIR), **kwargs)

    def do_POST(self):
        if self.path == "/api/jev/classify":
            self.handle_classify()
        elif self.path == "/api/actions/execute":
            self.handle_action()
        else:
            self.send_error(404, "Unknown API route")

    def handle_classify(self):
        content_len = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_len).decode("utf-8")
        try:
            req_data = json.loads(body)
            query = req_data.get("query", "").strip()
        except Exception:
            self.send_error(400, "Invalid JSON")
            return

        if not query:
            self.send_json({
                "domain": "palette_command",
                "sub_target": "none",
                "confidence": 1.0,
                "readiness": 0.0,
                "latency_ms": 0,
                "widget_spec": build_declarative_widget("none", "none", "", {}, 1.0, 0.0)
            })
            return

        api_key = get_api_key()
        t0 = time.perf_counter()

        # Construct Jev System One questions: hierarchical battery
        jev_payload = {
            "model": "jev-latest",
            "state": query,
            "questions": {
                "domain": {
                    "type": "choice",
                    "instructions": "What domain does this user query belong to in the Freyja assistant?",
                    "criteria": {
                        "tool_inspect": "Inspecting details, parameters, risk level, or schema of an agent tool (e.g. tool details for bash, how does tool_search work)",
                        "diagnostics": "Viewing diagnostic logs, debug events, errors, bridge health, subsystem logs",
                        "swarm": "Viewing active subagent swarms, agent lanes, worker status, multi-agent activity",
                        "briefing": "Morning briefing, today plan, decisions, active projects, daily agenda",
                        "token_usage": "Token consumption, cost, spend analytics, cache hit rates across models",
                        "model_picker": "Listing, selecting, switching LLM models (Sonnet, Opus, GPT-5, Luna)",
                        "artifact_explorer": "Searching, viewing, or opening generated files, reports, artifacts",
                        "voice_session": "Starting or configuring a real-time voice call / conversation",
                        "quick_control": "Toggling appearance, mute, timer, or system settings",
                        "calendar": "Meetings, scheduling, agenda, availability",
                        "memory": "Searching past notes, transcripts, or recommendations",
                        "agent_task": "Delegating a sub-agent task or reviewing code",
                        "palette_command": "Standard command palette search, navigation, git status, opening files, or typing keywords"
                    }
                },
                "sub_target": {
                    "type": "choice",
                    "instructions": "If this is a specific granular action or filter, which one is targeted?",
                    "criteria": {
                        "calendar_view": "Asking to VIEW, inspect, list, or check existing meetings or agenda (e.g. what meetings do i have tomorrow)",
                        "calendar_create": "Asking to CREATE, schedule, book, or set up a new meeting (e.g. set up a meeting next tuesday)",
                        "appearance_toggle": "Specifically about theme, dark mode, light mode, appearance",
                        "mic_toggle": "Specifically about muting/unmuting microphone or audio input",
                        "timer_set": "Specifically about timers, countdowns, or pomodoro",
                        "all_controls": "Broad or general system controls overview",
                        "model_switch": "Directly switching or selecting an active model",
                        "token_history": "Historical spend and token trends",
                        "recent_artifacts": "Browsing recent artifacts and deliverables",
                        "start_call": "Initiating an immediate voice conversation",
                        "daily_plan": "Reviewing today daily briefing and staged plan",
                        "none": "No specific sub-target / general domain query"
                    }
                },
                "readiness": {
                    "type": "score",
                    "instructions": "How complete and actionable is the query for generating a filled UI widget?",
                    "criteria": [
                        "Partial fragment, just started typing keywords",
                        "Vague intent, missing critical parameters",
                        "Clear intent with actionable entities/parameters"
                    ]
                }
            }
        }

        try:
            req = urllib.request.Request(
                "https://api.typesafe.ai/v1/systemone",
                data=json.dumps(jev_payload).encode("utf-8"),
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json"
                },
                method="POST"
            )
            with urllib.request.urlopen(req, timeout=8.0) as resp:
                jev_res = json.loads(resp.read().decode("utf-8"))
            
            dur_ms = round((time.perf_counter() - t0) * 1000, 1)

            domain_choice = jev_res["answers"]["domain"]["choice"]
            domain_conf = jev_res["answers"]["domain"]["confidence"]
            sub_choice = jev_res["answers"]["sub_target"]["choice"]
            readiness_score = jev_res["answers"]["readiness"]["score"]

            entities = extract_entities(query, domain_choice, sub_choice)
            widget_spec = build_declarative_widget(domain_choice, sub_choice, query, entities, domain_conf, readiness_score)

            self.send_json({
                "query": query,
                "widget": domain_choice,
                "domain": domain_choice,
                "sub_target": sub_choice,
                "confidence": domain_conf,
                "readiness": readiness_score,
                "entities": entities,
                "latency_ms": dur_ms,
                "widget_spec": widget_spec,
                "raw_jev": jev_res
            })

        except Exception as e:
            self.send_json({
                "error": str(e),
                "widget": "palette_command",
                "domain": "palette_command",
                "sub_target": "none",
                "confidence": 0.0,
                "readiness": 0.0,
                "latency_ms": round((time.perf_counter() - t0) * 1000, 1),
                "widget_spec": build_declarative_widget("none", "none", query, {}, 0.0, 0.0)
            }, status=500)

    def handle_action(self):
        content_len = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_len).decode("utf-8")
        req_data = json.loads(body)
        action_id = req_data.get("action_id")
        
        time.sleep(0.1)
        self.send_json({
            "ok": True,
            "action_id": action_id,
            "message": f"Action '{action_id}' executed successfully."
        })

    def send_json(self, data: dict, status: int = 200):
        body = json.dumps(data, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "POST, GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "POST, GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()


def run():
    HTTPServer.allow_reuse_address = True
    server = HTTPServer(("127.0.0.1", PORT), JevPaletteHandler)
    print(f"Jev Generative UI Prototype Server listening on http://127.0.0.1:{PORT}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down server.")
        server.server_close()


if __name__ == "__main__":
    run()
