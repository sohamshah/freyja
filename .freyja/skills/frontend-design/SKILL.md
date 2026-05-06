---
name: frontend-design
type: build
description: Build distinctive, production-grade frontend interfaces that fit the product, avoid generic layouts, and remain usable under real content.
triggers:
  - build frontend
  - improve UI
  - design interface
  - make this look better
  - polish visual design
tags:
  - frontend
  - ui
  - design
  - accessibility
source: "~/.claude/skills/frontend-design/SKILL.md"
confidence: unvalidated
---

# Frontend Design

Use this when creating or polishing product UI, pages, applications, design artifacts, or interactive components.

## Design Direction

1. Identify the real job.
   - Who uses this?
   - What are they trying to do repeatedly?
   - What information must be scannable?
   - What should feel fast, quiet, expressive, dense, playful, or editorial?

2. Match the domain.
   - Operational tools should be dense, calm, and predictable.
   - Creative tools can be more expressive, but controls must still be ergonomic.
   - Landing pages need strong first-viewport identity and real assets.
   - Games and simulations need immediate play, not marketing framing.

3. Choose a coherent aesthetic and commit.
   - Typography, spacing, color, iconography, surfaces, and motion should point in the same direction.
   - Avoid generic card grids, oversized empty heroes, gradient text, decorative blobs, and identical repeated panels.

## Implementation Checklist

- Use existing design tokens, components, and icon libraries.
- Build real controls for expected workflows: tabs, menus, toggles, sliders, segmented controls, toolbar buttons, search, filters, and empty states where useful.
- Make layout stable with fixed control dimensions, min/max constraints, grid tracks, and aspect ratios.
- Ensure long labels and dynamic content do not overflow.
- Keep UI cards for actual repeated items, modals, or framed tools. Do not nest cards.
- Use responsive behavior designed for the workflow, not just shrinking.
- Prefer transform and opacity for motion; respect reduced motion.

## Review Pass

Before calling the UI done:
- Inspect at desktop and mobile widths.
- Check text fit and overlap.
- Check hover, focus, empty, loading, selected, error, and disabled states.
- Confirm the first screen is the actual usable experience unless a landing page was explicitly requested.
- Scan CSS colors for one-note palettes or overused purple/blue gradients.
- Run type/build checks and browser screenshots for frontend changes.

## Quality Bar

- The UI should feel specific to this product.
- Visual decisions must improve comprehension or workflow.
- If a design choice harms readability, interaction speed, or performance, revise it.
