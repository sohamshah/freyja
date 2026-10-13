"""Prompt helpers for Freyja knowledge stores."""

from __future__ import annotations

from bridge.knowledge.memory_store import MemoryStore
from bridge.knowledge.models import MemoryItem
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


def memory_prompt_key(item: MemoryItem) -> str:
    """Identity of a memory as listed in a prompt: an edit counts as a new
    listing."""
    return f"memory:{item.id}:{item.updated_at}"


def build_knowledge_reminder(
    *,
    memory_store: MemoryStore,
    skill_store: SkillStore,
    query: str = "",
    shown: frozenset[str] | set[str] = frozenset(),
    memory_limit: int = 8,
    skill_limit: int = 12,
) -> tuple[str, list[str]]:
    """The memories and skills that match ``query``, as a ``<system-reminder>``
    block for the turn's message, and the keys it lists.

    Items whose key is in ``shown`` are left out: an earlier message in the
    transcript already lists them. Knowledge never goes in the system prompt.
    Retrieval depends on the message, so a system prompt that carried it would
    change on most turns, and every change makes the provider re-read the whole
    conversation behind it.
    """
    memories = [
        item
        for item in memory_store.relevant(query, limit=memory_limit)
        if memory_prompt_key(item) not in shown
    ]
    sections: list[str] = []
    keys: list[str] = []
    if memories:
        sections.append(memory_store.format_prompt(memories))
        keys.extend(memory_prompt_key(item) for item in memories)
    skill_section, skill_keys = skill_store.build_prompt_excluding(
        query, limit=skill_limit, exclude=shown
    )
    if skill_section:
        sections.append(skill_section)
        keys.extend(skill_keys)
    if not sections:
        return "", []
    body = "\n\n".join(sections)
    return (
        "<system-reminder>\n"
        "Memory and skills retrieved for this message. Earlier messages in "
        "this conversation may list more; they still apply.\n\n"
        f"{body}\n"
        "</system-reminder>",
        keys,
    )
