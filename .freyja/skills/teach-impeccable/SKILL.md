---
name: teach-impeccable
type: build
description: Establish durable design context for a project before doing frontend or visual work.
triggers:
  - establish design context
  - learn this project's design language
  - make future UI work consistent
  - create design guidelines
tags:
  - design
  - onboarding
  - memory
source: "~/.claude/skills/teach-impeccable/SKILL.md"
confidence: unvalidated
---

# Teach Impeccable

Use this when a project needs durable design context before UI work.

## Workflow

1. Inspect the project before asking questions.
   - Read README and docs for audience, purpose, and product vocabulary.
   - Inspect package/config files for frontend stack and design libraries.
   - Scan existing components for layout, spacing, typography, color, motion, and interaction patterns.
   - Inspect CSS variables, tokens, assets, icons, and screenshots if present.

2. Identify what is known vs unclear.
   - Known: facts directly visible in code or docs.
   - Unclear: brand personality, target users, anti-references, accessibility requirements, and emotional tone.

3. Ask only high-value questions.
   - Who uses this and what job are they doing?
   - What should the interface feel like?
   - What should it definitely not look like?
   - Are there specific accessibility or brand constraints?

4. Persist the result.
   - If the user gives a stable personal preference, call `record_user_preference`.
   - If the context is project-specific, create or update `docs/DESIGN-CONTEXT.md`.
   - Keep the document concise enough that future agents can read it quickly.

## Output Shape

```markdown
# Design Context

## Users
[Who they are, where they are, what they need to accomplish]

## Product Personality
[Voice, tone, emotional goal]

## Aesthetic Direction
[Visual language, references, anti-references]

## Design Principles
- [Principle]
- [Principle]
- [Principle]

## Existing System Notes
[Observed components, tokens, constraints, and gotchas]
```

## Quality Bar

- Do not invent brand direction when the project already contains evidence.
- Do not ask questions whose answers are visible in the repository.
- Separate user-wide preferences from project-specific guidance.
- Keep the saved context operational, not poetic.
