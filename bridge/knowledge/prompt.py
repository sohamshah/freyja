"""Prompt helpers for Freyja knowledge stores."""

from __future__ import annotations

from bridge.knowledge.memory_store import MemoryStore
from bridge.knowledge.skill_store import SkillStore


def build_knowledge_prompt(
    *,
    memory_store: MemoryStore,
    skill_store: SkillStore,
    query: str = "",
    memory_limit: int = 8,
    skill_limit: int = 12,
) -> str:
    sections = []
    memory_section = memory_store.build_prompt(query, limit=memory_limit)
    if memory_section:
        sections.append(memory_section)
    skill_section = skill_store.build_prompt(query, limit=skill_limit)
    if skill_section:
        sections.append(skill_section)
    if not sections:
        return ""
    return "\n\n".join(sections)
