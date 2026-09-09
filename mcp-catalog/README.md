# Freyja MCP catalog

Curated, pinned MCP servers that `/mcp catalog install <name>` can drop into
`~/.freyja/mcp.json`. One directory per entry: `mcp-catalog/<name>/manifest.yaml`.
Loader / lint / install logic lives in `bridge/mcp/catalog.py`;
`tests/test_mcp_catalog.py` lints every shipped manifest.

## Attribution

The manifest schema and a number of manifests are adapted from
[hermes-agent](https://github.com/NousResearch/hermes-agent)
(`hermes_cli/mcp_catalog.py`, `optional-mcps/*/manifest.yaml`),
Copyright (c) 2025 Nous Research, MIT License. Copied manifests carry an
attribution header; pins and URLs were re-verified for Freyja on the
`verified` date recorded in each file.

## Policy

- **Presence = approval.** Entries land by PR review only. No community tier.
- **Pinned supply chain.** `npx -y pkg@X.Y.Z`, `uvx pkg==X.Y.Z`, docker
  images with an explicit tag/digest. Never `@latest`, never a bare package.
  Pinned releases should be ≥ 2 weeks old at pin time. Nothing auto-updates:
  re-run `install` after a catalog update to apply a new pin.
- **`verified: YYYY-MM-DD` is mandatory** — the date the package/URL was
  checked against the vendor (npm/PyPI page, vendor docs, or a live
  unauthenticated `initialize` probe for remote servers).
- **No secrets, ever.** Manifests declare env var *names*; install writes
  `${VAR}` references into mcp.json and reports which vars are unset (presence
  only — values are never read). Operators set them in `~/.freyja/.env`.
- **Idempotent install.** Re-installing preserves operator edits to
  `enabled`, `trust`, `tier`, `tools`, `timeouts`, extra `env` keys and any
  recorded OAuth state; only manifest-owned fields (transport/url/command/
  args/auth/source) are refreshed.
- **User overrides.** `~/.freyja/mcp-catalog/<name>/manifest.yaml` (or
  `<name>.yaml`) replaces the shipped entry of the same name.

## Manifest schema (v1)

```yaml
manifest_version: 1
name: linear                    # == directory name; [a-z0-9_-]
description: One-line summary.
homepage: https://vendor/docs   # https:// required (hermes `source` accepted as alias)
license: MIT | Apache-2.0 | proprietary   # licence of the server software / hosted service
verified: 2026-09-03            # ISO date the pin/URL was checked
tags: [issues, remote, oauth]   # lowercase [a-z0-9-]; ≥1

transport:
  type: http | stdio
  url: https://mcp.linear.app/mcp          # http only
  command: npx                             # stdio only
  args: ["-y", "@scope/pkg@1.2.3"]         # pinned
  package: "@scope/pkg"                    # informational
  version: "1.2.3"                         # informational; required for launcher entries
  env:                                     # static NON-secret env for the subprocess
    DISABLE_TELEMETRY: "1"

auth:
  type: oauth | api_key | none
  env:                                     # env the server needs; names only
    - name: LINEAR_API_KEY
      description: Where to get it.
      required: true
      secret: true                         # -> ${LINEAR_API_KEY} reference, never a value
      default: ""                          # non-secret only
  header: Authorization                    # http + api_key
  header_format: "Bearer ${LINEAR_API_KEY}"
  oauth:                                   # hints copied into mcp.json `oauth`
    client_name: "Claude Code"
    scope: "mcp:connect"
  provider: google                         # informational (third-party OAuth)
  scopes: [...]

tools:                                     # default filter, first install only
  default_enabled: [...]                   # -> tools.include
  default_excluded: [...]                  # -> tools.exclude   (mutually exclusive)

suggest:                                   # optional UI hints
  keywords: [linear]
  hosts: [linear.app]

post_install: |
  Setup notes shown after install.
```

### What install writes

```json
"linear": {
  "transport": "http",
  "url": "https://mcp.linear.app/mcp",
  "enabled": false,
  "scope": "user",
  "trust": "standard",
  "tier": "warm",
  "auth": "oauth",
  "oauth": {},
  "source": {"catalog": "linear@2026-09-03"}
}
```

stdio entries get `command`/`args`/`env` with `${VAR}` references;
http + api_key entries get `headers: {"Authorization": "Bearer ${VAR}"}`.

## Lint rules (`manifest_lint()`)

schema · name matches directory · `verified` present, ISO, not in the future ·
https homepage · ≥1 lowercase tag · license present · http url is https and
carries no credential query param · launcher pins are exact (npm `pkg@X.Y.Z`,
python `pkg==X.Y.Z`, docker tag/digest) and `transport.version` is set ·
no floating `@latest/@next/@canary` anywhere in args · secret-shaped static env
values are `${VAR}` · secret env specs ship no default · no credential-shaped
literals anywhere · every `${VAR}` referenced is declared in `auth.env` ·
the generated entry round-trips through `McpServerSpec.validate()` (covers
shell-egress and inline-secret rejection).
