---
name: ghostwrite-as-operator
description: Use ONLY when the operator has explicitly asked you to write something they will post under their own name, or to post as them ("post this as me", "draft a reply I can send", "ghostwrite this"). Covers GitHub PR reviews and inline comments, issue replies, Slack messages to colleagues, design-review notes. Do NOT load this skill merely because the surface is Slack or GitHub, or because the operator asked you to investigate or summarize something — answering the operator as yourself is the default. Strips AI tells (em-dashes, bold headers, bullet scaffolding, "Here's my take"), enforces terse opinionated prose, correct PR HEAD anchoring, and no buttering up / no praise-first framing in critical reviews.
type: build
triggers:[]
tags:[]
source: "freyja-drafter:session-mrtllfoy"
confidence: experimental
created_by: agent
created_from: freyja-drafter
created_at: 1784656263489
---

# Ghostwriting in the operator's voice

When the operator asks for text they will post under their own name (a GitHub PR review, an issue reply, a Slack message to a colleague), the goal is output indistinguishable from something they typed. The model's defaults — em-dashes, bold section headers, nested bullet scaffolding, "Here's my take:" framing — are tells that read as AI-generated. Strip them.

## When this skill applies (read this first)

This skill governs HOW to write in the operator's voice. It is not permission to post as them, and it is not triggered by the surface you happen to be on.

Load it only when the operator explicitly asked for text they will post under their own name, in this turn. "Post this as me", "draft a reply I can send to her", "ghostwrite a comment for EBI-1048" — those qualify.

These do NOT qualify, and are the common false trigger:

- "Deep dive into this thread", "look for dupes", "investigate this alert" — the deliverable is your analysis, returned to the operator as yourself.
- "Walk me through what you found", "summarize this channel" — likewise.
- Being in a Slack thread that contains colleagues. A thread with other people in it is not a request to speak as the operator to those people.
- Having a user-scoped tool available that could post as them. Capability is not instruction.

When in doubt, produce the draft and hand it to the operator. A draft they have to send themselves costs them one click. A message sent as them that they did not want cannot be unsent, and they may not find out until someone replies to it.

## Hard rules (the operator has corrected these explicitly)

- No em-dashes anywhere in the prose. Use commas, periods, or parentheses. (This is the operator's single most consistent preference across surfaces.)
- No unnecessary formatting. The ONLY things that get markup are links and inline `code`/identifiers in backticks. No bold headers inside a comment, no bullet lists unless a real enumeration is natural, no horizontal rules, no "## What" scaffolding.
- Plain, conversational, opinionated prose. Terse. Trusts the reader. Sounds like a senior colleague leaving a quick note, not a report.
- Do not announce structure or meta ("Here are my comments", "A few thoughts:"). Just say the thing.
- No buttering up. Do not open a review with flattery or a praise sandwich ("This is a solid, well-tested PR, just a few thoughts..."). Lead with the substance. If something genuinely deserves credit, state it flatly and briefly (one clause), never as a warm-up to soften the criticism. The operator has called this out directly ("no buttering up / no fluff").

## Posting workflow

Nothing is posted without the operator seeing the draft first. This applies to every surface, not just PRs: Slack messages sent through a user-scoped MCP tool, `gh` calls made as their account, and email are all the same rule. Produce the draft, let the operator approve, and only then post — and only if they asked you to post at all rather than just to write it.

For a PR review specifically, produce the comments as a reviewable list, labelled by file and target line, and let the operator approve before any `gh`/API call posts them.

For a PR review, the operator typically wants a mix: several **inline** comments anchored to specific lines plus a few **high-level** comments on the overall direction.

## Anchoring inline PR comments correctly

Inline comments must reference the PR HEAD commit, not your working tree, or they land on the wrong line or fail to attach. Before drafting:

```
git fetch origin pull/<N>/head        # gets FETCH_HEAD
git rev-parse FETCH_HEAD              # the HEAD SHA inline comments anchor against
```

Then read the post-change file at that SHA to pull exact line numbers for each anchor, so the line you cite is the line the reviewer sees in the diff. Cite the line by its content (the code on it), not just a number, so the operator can sanity-check placement.

Confirm which identity the token posts as before posting (the review appears under that account's name); surface it to the operator so there's no surprise about attribution.

## Reviewing-as-the-operator content notes

When the substance is a code/design review, the operator values a higher-level pass on top of line notes: is this even the right thing to build, are two independent features bundled into one PR, is there a structural ceiling the PR description does not reckon with. Lead inline notes with the concrete observation and end with the open question ("Intentional?") rather than a verdict — it reads as a colleague probing, not a gate.

When a longer-form strategy comment is wanted alongside line notes, keep it about the bet itself: what the design gets right stated plainly and without flattery, then the structural risk or ceiling the author has not reckoned with. Rank concrete blocking issues above nitpicks so the author sees the load-bearing objection first.
