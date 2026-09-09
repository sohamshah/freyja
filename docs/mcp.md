# MCP servers and plugins

Operator guide for what shipped in MCP v0–v2. Every path, flag and state below
is taken from the code under `bridge/mcp/`, `bridge/plugins/`,
`bridge/gateway/mcp_slack.py`, `mcp-catalog/` and the renderer
(`src/renderer/lib/slash.ts`, `src/renderer/lib/mcp.ts`,
`src/renderer/components/mcp/`).

## Overview

An MCP server is an external tool provider (stdio subprocess, streamable HTTP,
or legacy SSE). Freyja connects to each enabled server, lists its tools, and
registers every tool as a proxy tool named `mcp__<server>__<tool>` that the
agent calls like any built-in tool. A plugin is a Claude-Code-format directory
whose skills, command templates and `.mcp.json` servers are fanned out into
Freyja's skill store and MCP catalog.

Configuration lives in one file, `~/.freyja/mcp.json`, read by two processes:
the desktop bridge (`bridge/freyja_bridge.py`) and the Slack gateway daemon
(`bridge/gateway/run.py`). Both share `~/.freyja/mcp-tokens/` (OAuth state) and
`~/.freyja/mcp-run/` (stdio pid files).

Three management surfaces, all driving the same handler
(`bridge/mcp/commands.py:handle_mcp_command`):

- Desktop: `/mcp …` in the chat input (results appear as a block in the
  conversation) and Settings → **MCP servers** panel
  (`src/renderer/components/mcp/McpServersPanel.tsx`).
- Slack gateway: `/mcp …` slash command (`bridge/gateway/mcp_slack.py`);
  `/mcp help` prints the verb list.
- By hand: edit `~/.freyja/mcp.json`, then `/mcp reload`.

The desktop bridge only starts the manager when mcp.json has ≥1 server; a
missing file is a no-op. The gateway builds its manager lazily on the first
`/mcp` use (`mcp_slack.ensure_manager`), not at daemon boot.

## Quick starts

### a. Slack remote server from the catalog, then log in

```
/mcp catalog info slack
/mcp catalog install slack --enable
/mcp login slack
/mcp status slack
```

`install` writes `{"transport":"http","url":"https://mcp.slack.com/mcp",
"auth":"oauth","oauth":{},"trust":"standard","source":{"catalog":"slack@2026-09-03"}}`
(`enabled` false unless `--enable`). Until you log in the server sits in
`needs-auth`. `login` runs the OAuth 2.1 flow (desktop opens the browser;
Slack posts a link), stores tokens under `~/.freyja/mcp-tokens/slack/`, then
reconnects (or enables the server if it was disabled). Tools appear as
`mcp__slack__<tool>`.

### b. Atlassian by URL (discovery-first add)

```
/mcp add https://mcp.atlassian.com/v2/mcp
```

`add` POSTs an unauthenticated JSON-RPC `initialize` (5 s timeout,
`commands.probe_http_server`) and writes what it detects:

- 2xx → reachable, `auth` none; `serverInfo`/`protocolVersion` reported.
- 401/403 with `WWW-Authenticate: … resource_metadata="…"` → `"auth":"oauth"`
  and `"oauth":{"resource_metadata_url":"…"}`.
- 401/403 without resource metadata → added without auth plus a warning to
  re-add with `--header 'Authorization=Bearer ${<NAME>_TOKEN}'`.
- 404/405 → SSE GET probe; 200 `text/event-stream` → `"transport":"sse"`.
- connection error → added **disabled** with a warning, even with `--enable`.

Default name is the host's registrable label (`mcp.atlassian.com` →
`atlassian`); override with `--name`. The entry is saved disabled unless
`--enable`; the reply ends with `next:` steps, e.g.
`/mcp login atlassian → /mcp enable atlassian → /mcp test atlassian`.

### c. stdio server with secrets via `${VAR}`

```
/mcp add npx --env GITHUB_TOKEN='${GITHUB_TOKEN}' -- -y some-mcp-server@1.2.3
```

Everything after `--` is passed to the command verbatim (so `--enable` after
`--` goes to npx, not to Freyja). Equivalent mcp.json:

```json
{
  "servers": {
    "some-mcp-server": {
      "transport": "stdio",
      "command": "npx",
      "args": ["-y", "some-mcp-server@1.2.3"],
      "env": { "GITHUB_TOKEN": "${GITHUB_TOKEN}" },
      "enabled": true
    }
  }
}
```

`${VAR}` (or `${VAR:-default}`) is stored verbatim and expanded at connect
time against the bridge process environment. A literal value on a
secret-shaped key (name contains `TOKEN`, `KEY`, `SECRET` or `PASSWORD`) is
rejected by `McpServerSpec.validate()`. An unset secret-shaped var puts the
server in `needs-auth` (`missing secret env var(s): … — add to ~/.freyja/.env`);
an unset non-secret var expands to empty with a warning.

Where the variable must live: `~/.freyja/.env`. The gateway daemon merges it
into `os.environ` at startup (`run.py:_load_env_into_os_environ`); the desktop
bridge is spawned by Electron with the project/harness `.env`, then
`~/.freyja/.env` layered on top, then `process.env` (`src/main/bridge.ts`).
The env is read when the bridge starts: after adding a key, `/restart-bridge`
on the desktop or restart the gateway daemon.

The child process gets only `PATH`, `HOME`, `LANG` plus the keys in `env`
(`connection.SPAWN_ENV_BASE`), never the full parent environment.

### d. Install a Claude-Code-style plugin

```
/plugin install /path/to/plugin
/plugin install https://github.com/org/plugin.git
/plugin list
/plugin remove <name>
```

Source layout (`bridge/plugins/loader.py`): `.claude-plugin/plugin.json`
(manifest, required), `skills/<name>/SKILL.md`, `commands/<name>.md`,
`.mcp.json`. Install copies the plugin to `~/.freyja/plugins/<name>/` and
records it in `~/.freyja/plugins/installed.json`. What it registers:

- Skills: `SkillStore` scans `~/.freyja/plugins/*/skills/**` and prefixes every
  skill name with `<plugin>:`.
- Commands: each `commands/<x>.md` is wrapped into a synthetic skill at
  `<plugin>/skills/commands/<x>/SKILL.md` (`name: <plugin>:<x>`, `type: command`).
- MCP servers: each `.mcp.json` entry (`mcpServers` or `servers`) is merged into
  `~/.freyja/mcp.json` with `enabled: false`, `trust: "standard"`,
  `source: {"plugin": "<name>@<source>"}`; `type: sse|streamable-http` maps to
  `transport: http`; `${CLAUDE_PLUGIN_ROOT}` is expanded to the installed path
  at install time. Same-named entries owned by someone else are reported as
  conflicts and never overwritten. Enable with `/mcp enable <server>`.
- Unsupported manifest sections (hooks, agents, lsp, monitors, settings, bin)
  are listed in the reply, not applied.

`remove` deletes the directory, the record and any catalog server whose
`source.plugin` names the plugin. Link mode (`install(path, link=True)`) exists
in the loader API but is not exposed by either `/plugin` surface.

## mcp.json reference

Top level: `{"version": 1, "servers": {...}}`. `mcpServers` is accepted as an
alias for `servers` on read (native key wins if both present); saves always
write `servers`. Unknown top-level keys are preserved. Optional
`settings.keepalive_interval_s` (top level) sets the keepalive; config values
are clamped to a 5 s floor.

Per-server keys (`bridge/mcp/config.py:McpServerSpec`). Unknown keys are kept
verbatim and written back.

| Key | Values / default | Notes |
|---|---|---|
| `transport` | `stdio` (default), `http`, `sse` | `http` auto-falls back to SSE once on 404/405; `sse` never probes HTTP |
| `command`, `args` | stdio | exec'd as argv, never shell-parsed; `sh -c`, `| < > ; \` && || $(` rejected |
| `env` | stdio, `{K: "${VAR}"}` | literal secret-shaped values rejected |
| `url` | http/sse | may contain `${VAR}` |
| `headers` | http/sse, `{K: "Bearer ${VAR}"}` | literal secret-shaped values rejected; `/mcp add` additionally requires `${…}` in `Authorization`, `Proxy-Authorization`, `X-Api-Key` |
| `enabled` | `true` | flipped by `/mcp enable|disable` |
| `scope` | `"user"` | project scope (`<workspace>/.freyja/mcp.json`) exists in `load_catalog(include_project=)` but is not wired in the bridge |
| `trust` | `trusted`, `standard` (default), `untrusted` | permission prompt level, see below |
| `tier` | `hot`, `warm` (default), `cold` | tool visibility tier |
| `tools.include` / `tools.exclude` | fnmatch patterns, default `["*"]` / `[]` | applied before registration |
| `tools.permissions` | `{<remote_tool>: none|low|medium|high}` | per-tool override of the trust mapping |
| `tools.schema_ttl_s` | `0` | periodic `tools/list` diff refresh; 0 = only on `tools/list_changed` |
| `tools.allow_quarantined` | `[]` | remote tool names the operator reviewed and approved despite an injection-scan hit (written by `/mcp approve`) |
| `timeouts.connect_s` / `timeouts.call_s` | `30` / `120` | seconds |
| `limits.max_result_chars` | `100000` | hard cap on one tool result, truncation notice appended |
| `auth` | absent, `"oauth"`, `"api_key"` | only `"oauth"` (or `{"type":"oauth"}`) changes behaviour: the OAuth provider is attached; `"api_key"` is informational (catalog writes it). Absent `auth` with an `oauth` block is read as `"oauth"` |
| `oauth` | object | see below; also holds `resource_metadata_url` written by `/mcp add`. Claude Code plugin spelling is accepted and normalized on load: `clientId`→`client_id`, `clientSecret`→`client_secret`, `callbackPort`→`redirect_port` (+ `redirect_host: localhost`, matching Claude Code's registered redirect URI), `scopes: [..]`→`scope` |
| `source` | object | provenance: `{"catalog": "<name>@<verified>"}` or `{"plugin": "<name>@<source>"}` |

Trust → permission (`bridge/mcp/proxy_tool.py:TRUST_TO_LEVEL`): `trusted` → no
prompt, `standard` → MEDIUM, `untrusted` → HIGH. `tools.permissions.<tool>`
wins over the server trust.

`oauth` block (`bridge/mcp/oauth/settings.py`):

| Key | Default | Notes |
|---|---|---|
| `client_id` | none | pre-registered client; skips CIMD and DCR. May be `${VAR}` |
| `client_secret` | none | **must** be `${VAR}`; inline literal → `McpConfigError`; unset var → needs-auth |
| `scope` | server-provided | |
| `redirect_port` | `0` (auto) | 0–65535 |
| `redirect_host` | `127.0.0.1` | e.g. `localhost` for WAF-sensitive servers |
| `redirect_uri` | none | non-loopback proxy callback |
| `client_name` | `Freyja` | Figma defaults to `Claude Code` unless set |
| `client_metadata_url` | none | https URL; enables CIMD for this server |
| `cimd` | library default (off) | `false` forces DCR |
| `timeout_s` (alias `timeout`) | `300` | browser round-trip budget |
| `token_endpoint_auth_method` | server default | `none`, `client_secret_post`, `client_secret_basic` |
| `user_agent` | `Freyja` | token-endpoint UA |
| `application_type` | `native` | |

Bad entries never take the bridge down: a server that fails validation is
skipped with a warning naming the problem, and the rest load.

## Tool naming and exposure

- Name: `mcp__<server>__<tool>`, each component lowercased and squashed to
  `[a-z0-9_]`, whole name capped at 64 chars with a 6-char hash suffix on
  truncation. The remote name is kept for dispatch. Server names starting
  with `mcp__` are reserved.
- Summary: `[<server>] <first sentence>` capped at 140 chars.
- Tier: `tier` in mcp.json → `ToolTier`. `hot` = schema always in the tools
  array; `warm` (default) = summary visible, schema loaded on demand via the
  built-in `tool_search` tool; `cold` = hidden from summaries.
- Result cap: text content is truncated to `limits.max_result_chars` before
  anything downstream sees it.
- Injection quarantine: title+description are scanned against
  `manager.INJECTION_PATTERNS` (e.g. "ignore previous instructions", "system
  prompt", `<system>`, "you are now a", `curl http`, `base64.b64decode`). A hit
  skips registration and records the tool; a poisoned re-description on a
  `tools/list_changed` unregisters and quarantines it. `/mcp tools [server]`
  lists registered proxies and the quarantine. False positives happen (Slack's
  own `slack_read_file` trips `IMPORTANT:` with its anti-injection warning):
  read the description with `/mcp test <server>` or the server's docs, then
  `/mcp approve <server> <tool>` writes the remote tool name to
  `tools.allow_quarantined` in mcp.json and reloads so it registers. The
  allowlist is per tool, never per server; `trust: trusted` does not skip the
  scan.
- Calls only succeed while the server is `active`; `degraded` keeps tools
  registered but `McpProxyTool.execute` returns an error until recovery.

## Lifecycle and states

`bridge/mcp/connection.py:State`: `disabled`, `connecting`, `active`,
`degraded`, `backoff`, `parked`, `failed`, `needs-auth`.

```
connecting -> active | needs-auth | failed | backoff
active -> degraded -> active | backoff
backoff -(5 retries exhausted)-> parked -(probe ok every 300 s)-> connecting
active -(3 unproven drops)-> parked
auth-shaped failure anywhere -> needs-auth (no retry ladder)
any -> disabled (operator)
```

- Backoff: full-jitter exponential, base 1 s, cap 60 s, 5 retries
  (`DEFAULT_BACKOFF_*`), then `parked`; parked servers probe every 300 s.
- Permanent (no ladder): missing binary (`command not found`), empty command,
  invalid URL → `failed`; missing secret `${VAR}`, HTTP 401/403, OAuth/NeedsAuth
  exceptions, auth-shaped error text → `needs-auth` with reason
  `authentication required — run /mcp login <server>`.
- Session-proven gate: reaching `active` does not clear budgets. The first
  successful post-activation exchange (keepalive, tool call, refresh) proves
  the session; an unproven drop counts toward `rapid_drop_budget` (3) and
  exhausting it parks the server.
- Keepalive: `ping` every 180 s (`settings.keepalive_interval_s`, floor 5 s).
  A `-32601` on ping latches `ping_unsupported` and uses `tools/list` instead
  (reset per transport connection). Skipped while calls are in flight. First
  failed probe → `degraded` (tools stay registered); second → ladder.
- `tools/list_changed` → diff refresh in its own task: added tools register,
  removed unregister, changed update in place. `tools.schema_ttl_s` > 0 adds a
  periodic refresh. `prompts/…` and `resources/…` list_changed are logged and
  ignored.
- Stateless HTTP: missing `Mcp-Session-Id` accepted; `initialize` rejected
  with `-32022`/`-32601` falls back to `server/discover`; the GET stream is
  never required.
- stdio supervision: each spawn is wrapped by `bridge/mcp/stdio_watchdog.py`
  (own process group, kills the group if the bridge dies, 2 s poll) and writes
  a pid file to `~/.freyja/mcp-run/`. `McpManager.start()` sweeps stale pid
  files whose owner bridge is dead and whose cmdline still matches.
- Token-file watch (`manager.token_watch_tick`, interval = keepalive
  interval): a `needs-auth` OAuth server whose `tokens.json` mtime changes is
  reconnected automatically — this is how a login completed in the gateway
  daemon revives the same server in the desktop bridge, and vice versa.

`/mcp status` rows carry `state`, `reason`, `tool_count`, `restart_count`,
`last_latency_ms`, `transport_in_use` (shown as `http->sse` after fallback),
`rapid_drops`, `quarantined`, `auth` (`oauth|header|env|none`), `needs_auth`,
`has_tokens`, `token_expires_at`, `last_error`.

## Authentication

### OAuth 2.1 (`auth: "oauth"`)

Implemented in `bridge/mcp/oauth/` on the `mcp` 2.1.1 SDK's
`OAuthClientProvider` (PKCE S256 is done by the SDK). `/mcp login <server>`:

1. `run_login(fresh=True)` snapshots and wipes stored state so the flow starts
   from discovery (RFC 9728 protected-resource metadata → AS metadata); the
   snapshot is restored if the flow fails.
2. Client identity, in order: CIMD (only if a metadata URL is configured and
   the AS advertises support), pre-registered `oauth.client_id`, else RFC 7591
   dynamic registration.
3. Loopback callback `http://127.0.0.1:<port>/callback`. Port precedence
   (`provider.resolve_callback_port`): external `redirect_uri` → pinned CIMD
   port from `27890–27894` → `oauth.redirect_port` → the port of the cached
   client registration → a freshly reserved free port.
4. Browser step. Desktop: the bridge opens the URL (`BrowserFlow`) and emits
   `mcp_oauth_url`; the renderer shows an "Authorize <server>" notice with
   countdown, **Open link** and **Copy URL** (`McpOAuthNotice.tsx`). Slack: the
   link is posted ephemerally to the caller (`:key: Authorize *server*: … (link
   valid ~N min)`), then ✅/❌ when the flow lands. Timeout 300 s.
5. Tokens land in `~/.freyja/mcp-tokens/<server>/` (dir 0700): `tokens.json`,
   `client.json`, `meta.json` (files 0600, written with `O_EXCL` + rename),
   optional `cimd-off` marker and `client.json.bak`. `tokens.json` gets an
   absolute `expires_at`; refresh is automatic via the SDK, with scope and
   refresh_token carried forward when the AS omits them.
6. On success the server is reconnected (or enabled if it was disabled) and
   `mcp_status` is re-emitted.

At connect time the bridge builds the provider **non-interactively**
(`commands.make_auth_factory`): no cached token → `needs-auth`, never a
surprise browser. A 401 after a fresh token also → `needs-auth`. An
`invalid_client` from the token endpoint poisons the cached registration
(`client.json` → `.bak`) and marks `cimd-off` so the next login re-registers.

Commands: `/mcp login <server>`, `/mcp logout <server>` (deletes stored state,
reconnects → `needs-auth`), `/mcp reauth <server>` (same as login),
`/mcp reauth --all` (every enabled OAuth server, one browser at a time). One
login per server at a time: a second attempt returns
`login already in progress for '<server>' — finish it in the browser first`.

CIMD is **off by default**: `bridge/mcp/oauth/cimd.py:FREYJA_CLIENT_METADATA_URL
= None`. To enable globally, host `docs/oauth/client-metadata.json` at a stable
https URL that serves it without redirects, set its `client_id` to that URL,
and set the constant to the same value (its `redirect_uris` must stay in sync
with `callback.CIMD_PORTS`). Per-server opt-in today:
`"oauth": {"client_metadata_url": "https://…"}`. CIMD is skipped whenever the
block sets `client_id`, `client_secret`, `client_name`, a non-`none` auth
method, `redirect_uri` or `redirect_port`.

### API key / static header

No `auth` key needed: put `"headers": {"Authorization": "Bearer ${MY_TOKEN}"}`
on the server and the variable in the environment. `/mcp status` reports
`auth=header`; a missing variable → `needs-auth` with the hint to set it.
`/mcp login` on such a server does not open a browser; it reports the missing
`${VAR}` names instead.

## Elicitation and sampling

Servers may call `elicitation/create` during a tool call. The connection
routes it through the bridge's `ElicitationBridge`
(`bridge/mcp/elicitation.py`):

- Desktop: `mcp_elicitation` event → modal (`McpElicitationPrompt.tsx`).
  `form` mode renders fields from `requestedSchema` (string/number/integer/
  boolean/enum; object/array as JSON); `url` mode shows the message, **Open
  link**, **I've completed this** (accept) / **Decline**. Esc cancels; in
  `url` mode Enter accepts (form mode submits via the accept button so
  required-field validation runs).
- Slack: the question and its fields are posted into the thread of the
  session running the tool call (fallback: the conversation that last used
  `/mcp`). Answer with `/mcp answer <requestId> key=value … | accept | decline
  | cancel`; a plain threaded reply starting with `/mcp answer` or
  `mcp answer` is promoted to the slash router. `/mcp answer` with no args
  lists pending requests. Values are coerced against the schema (bool
  yes/no, int, number, case-insensitive enum); problems return
  `cannot accept: …` without resolving.
- Fail closed: no answer within 300 s (`elicitation_timeout_s`) → `cancel`;
  handler error → `decline`; no handler wired → `decline`.

Sampling (`sampling/createMessage`) is always declined with JSON-RPC
`-32600` "Sampling is not supported by this client" and counted in
`sampling_declined`.

## Command reference

Grammar from `bridge/mcp/commands.py` (`parse_mcp_command`, used by Slack) and
`src/renderer/lib/mcp.ts` (`parseMcpSlash`, desktop). Tokens are shlex-split
(quotes respected). `/mcp` alone = `status`.

| Command | Effect |
|---|---|
| `/mcp status [server]` | table `name transport state tools reason` + detail lines |
| `/mcp enable <server>` / `disable <server>` | flip `enabled` in mcp.json and connect/disconnect live |
| `/mcp reload` | re-read mcp.json, apply add/change/remove diff |
| `/mcp login <server>` | OAuth sign-in (see above) |
| `/mcp logout <server>` | delete `~/.freyja/mcp-tokens/<server>/`, reconnect → needs-auth |
| `/mcp reauth <server>` · `/mcp reauth --all` | fresh login; `--all` = every enabled OAuth server, serially |
| `/mcp add <url \| command …> [--name N] [--transport http\|sse\|stdio] [--header K=V]* [--env K=${VAR}]* [--enable] [--trust trusted\|standard\|untrusted] [-- <verbatim command args>]` | discovery-first add; also accepts `--tier`, `--disable`; flags may use `--flag=value` |
| `/mcp remove <server> [--purge]` | delete from mcp.json, stop, unregister; `--purge` also deletes OAuth state |
| `/mcp test <server>` | isolated one-shot `initialize → tools/list → ping` with timings; never touches live registry or mcp.json |
| `/mcp tools [server]` | registered proxy tools (`server tool remote summary tier permission`) + quarantine |
| `/mcp approve <server> <tool>` | register a quarantined tool after review; persists `tools.allow_quarantined`, reloads the server |
| `/mcp catalog list [--tag T]*` · `search <query>` · `info <name>` · `install <name> [--enable] [--as NAME]` | curated catalog (`ls` = `list`) |
| `/mcp call <server> <tool> [{json} \| key=value …]` | invoke a registered proxy tool (debug) |
| `/mcp answer <requestId> key=value … \| accept \| decline \| cancel` | settle a pending elicitation (Slack); no args lists pending |
| `/mcp help` | Slack only: prints `MCP_HELP` |

Desktop differences (`parseMcpSlash`): `answer` is not offered (the modal is
used instead); `tools` requires a server; `reauth --all` works because `--all`
is passed through as the server token. The Settings panel buttons map to:
**reload** → `reload`, **refresh** → `status`, **add server** → `add <target>
[--name] [--transport] [--header]* [--enable]`, **catalog** → `catalog list` /
`catalog search <q>` / `catalog install <id> [--enable]`; per row
**enable/disable**, **login**, **reauth**, **test**, **tools**, **remove**.

`/plugin` (`bridge/plugins/commands.py`, `slash.ts`):

| Command | Effect |
|---|---|
| `/plugin` · `/plugin list` | installed plugins: `name version skills commands servers source` |
| `/plugin install <path \| git-url>` | copy/clone into `~/.freyja/plugins/<name>/`, register skills, merge `.mcp.json` servers disabled |
| `/plugin remove <name>` (`uninstall` alias on the wire) | delete plugin, record and plugin-owned servers |

`/plugins` is a desktop alias.

## Catalog

Curated, pinned manifests under `mcp-catalog/<name>/manifest.yaml`, loaded by
`bridge/mcp/catalog.py`. 40 entries ship (23 OAuth remote, 7 open remote, 10
stdio), each `verified: 2026-09-03`. Full schema and lint rules:
`mcp-catalog/README.md`.

Manifest summary (`manifest_version: 1`): `name` (== directory),
`description`, `homepage` (https), `license`, `verified` (ISO date), `tags`;
`transport.{type: http|stdio, url, command, args, package, version, env}`;
`auth.{type: oauth|api_key|none, env: [{name, description, required, secret,
default}], header, header_format, oauth: {hints}}`;
`tools.{default_enabled|default_excluded}`; `suggest.{keywords, hosts}`;
`post_install`.

What `install` writes: `env` values are always `${VAR}` (or
`${VAR:-default}` for non-secret defaults); http + api_key →
`headers.Authorization: "Bearer ${VAR}"` and `auth: "api_key"`; oauth →
`auth: "oauth"` + `oauth: {hints}`; `trust: standard`;
`source.catalog: "<name>@<verified>"`; `enabled` = `--enable`. Re-install
refreshes manifest-owned keys (transport/url/command/args/auth/source) and
preserves `enabled`, `trust`, `tier`, `tools`, `timeouts`, operator-added
`env`/`headers` keys and recorded OAuth state. The reply lists unset env var
names (presence only — values are never read) and `next:` steps.

Overrides: `~/.freyja/mcp-catalog/<name>/manifest.yaml` (or `<name>.yaml`)
replaces the shipped entry of the same name; `FREYJA_MCP_CATALOG_DIR` points
at an alternative shipped dir. Broken user manifests are skipped and reported
via `catalog_diagnostics()`, never fatal.

Pinning policy: exact pins only (`pkg@X.Y.Z`, `pkg==X.Y.Z`, docker
tag/digest), no `@latest`/`@next`/`@canary`, pins ≥ 2 weeks old when added;
nothing auto-updates — re-run `install` after a catalog update to take a new
pin. Presence in `mcp-catalog/` means PR-reviewed; there is no community tier.

## Security model

- Trust levels gate every tool call through Freyja's permission prompt:
  `trusted` none, `standard` MEDIUM, `untrusted` HIGH; per-tool overrides via
  `tools.permissions`. Secret-shaped argument values are fingerprinted, not
  shown, in the prompt.
- Secrets never live in mcp.json: literal values on secret-shaped `env`,
  `headers` or `oauth.client_secret` keys fail validation; `/mcp add` also
  refuses literal `Authorization`/`X-Api-Key` headers. Catalog and plugin
  installs write only `${VAR}` references.
- stdio: argv is exec'd, never shell-parsed; `sh|bash|zsh… -c` and shell
  metacharacters are rejected; the child env is allowlist-only.
- Injection scan on every tool title/description before registration and on
  every re-description; hits are quarantined and visible in `/mcp tools`;
  only an explicit per-tool `/mcp approve` (→ `tools.allow_quarantined`)
  bypasses it.
- Token files: `~/.freyja/mcp-tokens/` 0700, files 0600, atomic create; token
  values are never included in any `/mcp` message, event or log (status shows
  only `expires_at`).
- Elicitation defaults fail closed (timeout → cancel, error → decline); sampling
  is refused.
- Slack plugin skills (installed under `~/.freyja/plugins/slack/skills/`) call
  the Slack Web API with whatever token is in the process environment, as
  described by `bridge/gateway/slack_capabilities.py`: `SLACK_BOT_TOKEN`
  (xoxb) acts as the Freyja app — sees only channels it has joined, cannot
  call `search.*`, writes are attributed to Freyja. `SLACK_USER_TOKEN` (xoxp)
  acts as the operator — private channels, DMs, canvases and workspace search
  work, and any write appears as the operator personally. This is separate
  from the `slack` remote MCP server, whose access comes from the OAuth
  grant made during `/mcp login slack`.

## Troubleshooting

- `needs-auth` reasons: `authentication required — run /mcp login <server>`
  (HTTP 401/403, OAuth error, no cached token) or `missing secret env var(s):
  X — add to ~/.freyja/.env`. `/mcp status` prints `needs-auth → <hint>`.
- 401 after login: the provider raises needs-auth after a 401 with a fresh
  token instead of looping; `/mcp reauth <server>` wipes and re-runs the flow.
  `invalid_client` poisons the cached registration automatically.
- Missing `${VAR}` on the desktop: the desktop bridge's environment is the
  project/harness `.env`, then `~/.freyja/.env` (layered on top), then the
  launch environment; the gateway daemon reads only `~/.freyja/.env`. Put
  operator secrets in `~/.freyja/.env` so both processes see them. After
  adding one, `/restart-bridge` (env is read at bridge start; `/mcp reload`
  only re-reads mcp.json).
- Callback port in use: `OAuthCallbackPortInUseError` — another listener owns
  the pinned/configured port; set `oauth.redirect_port` to a free port or
  clear it for auto-pick.
- `login already in progress for '<server>'`: finish or wait out the running
  flow (300 s); the guard is released when `run_login` returns.
- `parked`: transport dropped repeatedly (`N rapid drops without a healthy
  session` or backoff exhausted). The server is re-probed every 300 s; force
  it with `/mcp disable` + `/mcp enable`, or `/mcp reload` after fixing config.
- `/mcp test <server>`: per-step `initialize` (protocol mode/version,
  serverInfo), `tools/list` (count), `ping` (`unsupported (-32601)` is OK),
  plus the auth block and filtered/quarantined tool lists. Runs an isolated
  connection; safe on a live system.
- The gateway daemon starts its MCP manager at boot (before the Slack adapter
  connects), so Slack sessions carry `mcp__*` tools from the first message;
  if boot fails (`mcp manager boot failed` in `~/.freyja/logs/gateway.log`)
  the first `/mcp` command builds it lazily.
- Logs: desktop bridge Python logging is forwarded as `log` events to the
  renderer debug drawer (`/debug`) and journaled at
  `~/.freyja/bridge-events.jsonl` (rolls to `.prev.jsonl`, on by default via
  `FREYJA_DEBUG_LOG`). Gateway: `~/.freyja/logs/gateway.log`.
- After editing bridge code, the packaged app keeps running the old Python:
  `npm run rebuild` (signs with the dev cert so TCC grants survive) or
  `/restart-bridge` in dev.

## Limitations / not implemented

- Sampling is refused; Freyja does not proxy model calls for servers.
- `prompts/list_changed` and `resources/list_changed` are no-ops; only tools
  are proxied.
- Tokens are plain 0600 JSON files; no Keychain storage.
- CIMD is not hosted: `FREYJA_CLIENT_METADATA_URL` is `None` and
  `docs/oauth/client-metadata.json` still carries a placeholder `client_id`.
- Project-scope `<workspace>/.freyja/mcp.json` is parsed by `load_catalog`
  but not enabled in the bridge (`include_project=False`).
- Tool calls require `active`; a `degraded` server's tools return an error
  until the next probe succeeds.
- The gateway does not start the MCP manager at daemon boot.
- Plugin `hooks`, `agents`, `lsp`, `monitors`, `settings`, `bin` sections are
  ignored; link-mode install is API-only.
