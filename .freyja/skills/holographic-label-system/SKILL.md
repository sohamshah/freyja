---
name: holographic-label-system
type: build
description: Create high-craft holographic, industrial, technical, and print-inspired label/card systems as standalone HTML or app-native UI.
triggers:
  - holographic design
  - industrial label system
  - technical poster
  - halftone card
  - dithered graphic
  - print inspired digital design
tags:
  - design
  - frontend
  - visual-system
  - css
source: "~/.claude/skills/holographic-label-system/SKILL.md"
confidence: unvalidated
---

# Holographic Label System

Use this when the user wants a crafted visual system with holographic, industrial, halftone, dithered, technical, packaging, ticket, or poster-like treatments.

## Design Process

1. Extract the visual vocabulary.
   - Typography: weight, width, case, tracking, type hierarchy, mono labels.
   - Color: dominant ratios, accent placement, background temperature, contrast.
   - Texture: grain, scan lines, halftone density, contour lines, engraving, dither.
   - Composition: ticket, card, specimen sheet, packaging label, data plate, poster, wide banner.
   - Authentic details: serials, coordinates, seals, barcodes, rulers, crop marks, status labels.

2. Establish a small system before making variants.
   - Define CSS custom properties for palette, type, border, grain, and motion.
   - Create reusable classes for label shells, metadata strips, technical rules, illustration wells, seals, and data rows.
   - Keep the effect vocabulary consistent across every card.

3. Create variety through composition, not random decoration.
   - Change focal subject, aspect ratio, rhythm, illustration style, and information density.
   - Do not repeat the same card with different text.
   - Every variant should feel related, but each should have a distinct reason to exist.

## Core Techniques

- Halftone: layered `radial-gradient` dots with masks or clipped containers.
- Grain: SVG `feTurbulence` filter or subtle fixed overlay, pointer-events none.
- Scan lines: `repeating-linear-gradient` or SVG horizontal line groups.
- Dither: ASCII block characters or tightly spaced SVG/canvas dots.
- Holographic shimmer: multi-stop pastel gradients on black with `mix-blend-mode: screen`.
- Industrial labels: boxed metadata, dot leaders, hatches, barcodes, crop marks, dense uppercase microcopy.
- Technical overlays: coordinate readouts, corner brackets, measurement lines, serial ids.

## Performance Rules

- Animate only `opacity`, `transform`, `background-position`, and cheap SVG attributes.
- Do not animate layout properties.
- Use `pointer-events: none` on overlays.
- Keep blur and backdrop-filter use restrained on large surfaces.
- Respect `prefers-reduced-motion`.

## Output Guidance

For standalone HTML:
- Put all CSS in one `<style>` block.
- Use inline SVG for illustrations.
- Avoid runtime JavaScript unless interaction requires it.
- Make the layout responsive, but preserve the designed aspect ratios.

For Freyja UI:
- Use existing tokens and components first.
- Keep cards at 8px radius or less unless the local design system says otherwise.
- Do not let ornamental effects reduce text legibility.
- Verify with screenshots at desktop and narrow widths.

## Quality Bar

- The result should look authored, not templated.
- Decorative systems must support the subject matter.
- Text remains readable before the effect is considered successful.
- If a new section is requested, add below existing work unless the user asks to replace it.
