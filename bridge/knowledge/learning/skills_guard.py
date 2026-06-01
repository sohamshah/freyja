"""Skills Guard — content scanner for skill candidates.

Port of Hermes' ``tools/skills_guard.py`` (88+ threat patterns across
six categories). Trimmed for Freyja's needs:

  · No multi-tier trust system. Every candidate going through this
    scanner came from our own drafter; there is no community/hub
    install path. The scanner produces a verdict
    (``safe`` / ``caution`` / ``dangerous``) and the candidate flow
    consumes it: ``safe`` → auto-flow to operator confirmation,
    ``caution`` → operator confirmation with a prominent warning,
    ``dangerous`` → discarded outright with the rejection logged.

  · We scan text fragments, not directories. The candidate is a YAML
    file with a body + frontmatter; ``scan_text`` walks line-by-line
    just like Hermes' ``scan_skill`` walks files.

  · We keep the threat-pattern table verbatim from Hermes. Every
    paragraph in that table represents a real attack pattern the
    Hermes team encountered in adversarial skills (training-data
    leakage, prompt injection attempts, malicious community skills).
    We do not rederive — same rule as ``do_not_capture_list``.

Verdict policy
──────────────
  · Any ``critical`` finding → ``dangerous``.
  · ≥3 ``high`` findings → ``dangerous`` (cluster signal).
  · Any ``high`` finding → ``caution``.
  · ≥3 ``medium`` findings → ``caution``.
  · Otherwise → ``safe``.

The dangerous-cluster heuristic catches "obfuscated benign looking"
attacks where individual lines are merely suspicious but the whole
content reads as exfiltration.

References:
  · Hermes scanner: ``~/work/services/hermes-agent/tools/skills_guard.py``
  · Threat pattern list captured verbatim in
    ``docs/skill-learning-reference/artifacts/skills_guard_patterns.txt``
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any


# ── Verdict policy ──


VERDICT_SAFE = "safe"
VERDICT_CAUTION = "caution"
VERDICT_DANGEROUS = "dangerous"


@dataclass
class Finding:
    """One threat-pattern match in scanned content."""

    pattern_id: str
    severity: str       # "critical" | "high" | "medium" | "low"
    category: str       # "exfiltration" | "injection" | "destructive" |
                        # "persistence" | "network" | "obfuscation"
    line: int
    match: str
    description: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "pattern_id": self.pattern_id,
            "severity": self.severity,
            "category": self.category,
            "line": self.line,
            "match": self.match,
            "description": self.description,
        }


@dataclass
class ScanResult:
    """Output of ``scan_text``. ``verdict`` is what the candidate flow
    consumes; ``findings`` are for the operator-facing report."""

    verdict: str = VERDICT_SAFE
    findings: list[Finding] = field(default_factory=list)
    summary: str = ""

    def is_safe(self) -> bool:
        return self.verdict == VERDICT_SAFE

    def is_dangerous(self) -> bool:
        return self.verdict == VERDICT_DANGEROUS

    def to_dict(self) -> dict[str, Any]:
        return {
            "verdict": self.verdict,
            "summary": self.summary,
            "findings": [f.to_dict() for f in self.findings],
        }


# ── Threat pattern table (verbatim port from Hermes) ──


THREAT_PATTERNS = [
    # ── Exfiltration: shell commands leaking secrets ──
    (r'curl\s+[^\n]*\$\{?\w*(KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL|API)',
     "env_exfil_curl", "critical", "exfiltration",
     "curl command interpolating secret environment variable"),
    (r'wget\s+[^\n]*\$\{?\w*(KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL|API)',
     "env_exfil_wget", "critical", "exfiltration",
     "wget command interpolating secret environment variable"),
    (r'fetch\s*\([^\n]*\$\{?\w*(KEY|TOKEN|SECRET|PASSWORD|API)',
     "env_exfil_fetch", "critical", "exfiltration",
     "fetch() call interpolating secret environment variable"),
    (r'httpx?\.(get|post|put|patch)\s*\([^\n]*(KEY|TOKEN|SECRET|PASSWORD)',
     "env_exfil_httpx", "critical", "exfiltration",
     "HTTP library call with secret variable"),
    (r'requests\.(get|post|put|patch)\s*\([^\n]*(KEY|TOKEN|SECRET|PASSWORD)',
     "env_exfil_requests", "critical", "exfiltration",
     "requests library call with secret variable"),

    # ── Exfiltration: reading credential stores ──
    (r'base64[^\n]*env',
     "encoded_exfil", "high", "exfiltration",
     "base64 encoding combined with environment access"),
    (r'\$HOME/\.ssh|\~/\.ssh',
     "ssh_dir_access", "high", "exfiltration",
     "references user SSH directory"),
    (r'\$HOME/\.aws|\~/\.aws',
     "aws_dir_access", "high", "exfiltration",
     "references user AWS credentials directory"),
    (r'\$HOME/\.gnupg|\~/\.gnupg',
     "gpg_dir_access", "high", "exfiltration",
     "references user GPG keyring"),
    (r'\$HOME/\.kube|\~/\.kube',
     "kube_dir_access", "high", "exfiltration",
     "references Kubernetes config directory"),
    (r'\$HOME/\.docker|\~/\.docker',
     "docker_dir_access", "high", "exfiltration",
     "references Docker config (may contain registry creds)"),
    (r'\$HOME/\.hermes/\.env|\~/\.hermes/\.env',
     "hermes_env_access", "critical", "exfiltration",
     "directly references Hermes secrets file"),
    (r'cat\s+[^\n]*(\.env|credentials|\.netrc|\.pgpass|\.npmrc|\.pypirc)',
     "read_secrets_file", "critical", "exfiltration",
     "reads known secrets file"),

    # ── Exfiltration: programmatic env access ──
    (r'printenv|env\s*\|',
     "dump_all_env", "high", "exfiltration",
     "dumps all environment variables"),
    (r'os\.environ\b(?!\s*\.get\s*\(\s*["\']PATH)',
     "python_os_environ", "high", "exfiltration",
     "accesses os.environ (potential env dump)"),
    (r'os\.getenv\s*\(\s*[^\)]*(?:KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL)',
     "python_getenv_secret", "critical", "exfiltration",
     "reads secret via os.getenv()"),
    (r'process\.env\[',
     "node_process_env", "high", "exfiltration",
     "accesses process.env (Node.js environment)"),
    (r'ENV\[.*(?:KEY|TOKEN|SECRET|PASSWORD)',
     "ruby_env_secret", "critical", "exfiltration",
     "reads secret via Ruby ENV[]"),

    # ── Exfiltration: DNS and staging ──
    (r'\b(dig|nslookup|host)\s+[^\n]*\$',
     "dns_exfil", "critical", "exfiltration",
     "DNS lookup with variable interpolation (possible DNS exfiltration)"),
    (r'>\s*/tmp/[^\s]*\s*&&\s*(curl|wget|nc|python)',
     "tmp_staging", "critical", "exfiltration",
     "writes to /tmp then exfiltrates"),

    # ── Exfiltration: markdown/link based ──
    (r'!\[.*\]\(https?://[^\)]*\$\{?',
     "md_image_exfil", "high", "exfiltration",
     "markdown image URL with variable interpolation (image-based exfil)"),
    (r'\[.*\]\(https?://[^\)]*\$\{?',
     "md_link_exfil", "high", "exfiltration",
     "markdown link with variable interpolation"),

    # ── Prompt injection ──
    (r'ignore\s+(?:\w+\s+)*(previous|all|above|prior)\s+instructions',
     "prompt_injection_ignore", "critical", "injection",
     "prompt injection: ignore previous instructions"),
    (r'you\s+are\s+(?:\w+\s+)*now\s+',
     "role_hijack", "high", "injection",
     "attempts to override the agent's role"),
    (r'do\s+not\s+(?:\w+\s+)*tell\s+(?:\w+\s+)*the\s+user',
     "deception_hide", "critical", "injection",
     "instructs agent to hide information from user"),
    (r'system\s+(?:\w+\s+)*prompt\s+(?:\w+\s+)*override',
     "sys_prompt_override", "critical", "injection",
     "attempts to override the system prompt"),
    (r'pretend\s+(?:\w+\s+)*(you\s+are|to\s+be)\s+',
     "role_pretend", "high", "injection",
     "attempts to make the agent assume a different identity"),
    (r'disregard\s+(?:\w+\s+)*(your|all|any)\s+(?:\w+\s+)*(instructions|rules|guidelines)',
     "disregard_rules", "critical", "injection",
     "instructs agent to disregard its rules"),
    (r'output\s+(?:\w+\s+)*(system|initial)\s+prompt',
     "leak_system_prompt", "high", "injection",
     "attempts to extract the system prompt"),
    (r'(when|if)\s+no\s*one\s+is\s+(watching|looking)',
     "conditional_deception", "high", "injection",
     "conditional instruction to behave differently when unobserved"),
    (r'act\s+as\s+(if|though)\s+(?:\w+\s+)*you\s+(?:\w+\s+)*(have\s+no|don\'t\s+have)\s+(?:\w+\s+)*(restrictions|limits|rules)',
     "bypass_restrictions", "critical", "injection",
     "instructs agent to act without restrictions"),
    (r'translate\s+.*\s+into\s+.*\s+and\s+(execute|run|eval)',
     "translate_execute", "critical", "injection",
     "translate-then-execute evasion technique"),
    (r'<!--[^>]*(?:ignore|override|system|secret|hidden)[^>]*-->',
     "html_comment_injection", "high", "injection",
     "hidden instructions in HTML comments"),
    (r'<\s*div\s+style\s*=\s*["\'][\s\S]*?display\s*:\s*none',
     "hidden_div", "high", "injection",
     "hidden HTML div (invisible instructions)"),

    # ── Destructive operations ──
    (r'rm\s+-rf\s+/',
     "destructive_root_rm", "critical", "destructive",
     "recursive delete from root"),
    (r'rm\s+(-[^\s]*)?r.*\$HOME|\brmdir\s+.*\$HOME',
     "destructive_home_rm", "critical", "destructive",
     "recursive delete targeting home directory"),
    (r'chmod\s+777',
     "insecure_perms", "medium", "destructive",
     "sets world-writable permissions"),
    (r'>\s*/etc/',
     "system_overwrite", "critical", "destructive",
     "overwrites system configuration file"),
    (r'\bmkfs\b',
     "format_filesystem", "critical", "destructive",
     "formats a filesystem"),
    (r'\bdd\s+.*if=.*of=/dev/',
     "disk_overwrite", "critical", "destructive",
     "raw disk write operation"),
    (r'shutil\.rmtree\s*\(\s*[\"\'/]',
     "python_rmtree", "high", "destructive",
     "Python rmtree on absolute or root-relative path"),
    (r'truncate\s+-s\s*0\s+/',
     "truncate_system", "critical", "destructive",
     "truncates system file to zero bytes"),

    # ── Persistence ──
    (r'\bcrontab\b',
     "persistence_cron", "medium", "persistence",
     "modifies cron jobs"),
    (r'\.(bashrc|zshrc|profile|bash_profile|bash_login|zprofile|zlogin)\b',
     "shell_rc_mod", "medium", "persistence",
     "references shell startup file"),
    (r'authorized_keys',
     "ssh_backdoor", "critical", "persistence",
     "modifies SSH authorized keys"),
    (r'ssh-keygen',
     "ssh_keygen", "medium", "persistence",
     "generates SSH keys"),
    (r'systemd.*\.service|systemctl\s+(enable|start)',
     "systemd_service", "medium", "persistence",
     "references or enables systemd service"),
    (r'/etc/init\.d/',
     "init_script", "medium", "persistence",
     "references init.d startup script"),
    (r'launchctl\s+load|LaunchAgents|LaunchDaemons',
     "macos_launchd", "medium", "persistence",
     "macOS launch agent/daemon persistence"),
    (r'/etc/sudoers|visudo',
     "sudoers_mod", "critical", "persistence",
     "modifies sudoers (privilege escalation)"),
    (r'git\s+config\s+--global\s+',
     "git_config_global", "medium", "persistence",
     "modifies global git configuration"),

    # ── Network: reverse shells and tunnels ──
    (r'\bnc\s+-[lp]|ncat\s+-[lp]|\bsocat\b',
     "reverse_shell", "critical", "network",
     "potential reverse shell listener"),
    (r'\bngrok\b|\blocaltunnel\b|\bserveo\b|\bcloudflared\b',
     "tunnel_service", "high", "network",
     "uses tunneling service for external access"),
    (r'\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}:\d{2,5}',
     "hardcoded_ip_port", "medium", "network",
     "hardcoded IP address with port"),
    (r'0\.0\.0\.0:\d+|INADDR_ANY',
     "bind_all_interfaces", "high", "network",
     "binds to all network interfaces"),
    (r'/bin/(ba)?sh\s+-i\s+.*>/dev/tcp/',
     "bash_reverse_shell", "critical", "network",
     "bash interactive reverse shell via /dev/tcp"),
    (r'python[23]?\s+-c\s+["\']import\s+socket',
     "python_socket_oneliner", "critical", "network",
     "Python one-liner socket connection (likely reverse shell)"),
    (r'socket\.connect\s*\(\s*\(',
     "python_socket_connect", "high", "network",
     "Python socket connect to arbitrary host"),
    (r'webhook\.site|requestbin\.com|pipedream\.net|hookbin\.com',
     "exfil_service", "high", "network",
     "references known data exfiltration/webhook testing service"),
    (r'pastebin\.com|hastebin\.com|ghostbin\.',
     "paste_service", "medium", "network",
     "references paste service (possible data staging)"),

    # ── Obfuscation: encoding and eval ──
    (r'base64\s+(-d|--decode)\s*\|',
     "base64_decode_pipe", "high", "obfuscation",
     "base64 decodes and pipes to execution"),
    (r'\\x[0-9a-fA-F]{2}.*\\x[0-9a-fA-F]{2}.*\\x[0-9a-fA-F]{2}',
     "hex_encoded_string", "medium", "obfuscation",
     "hex-encoded string (possible obfuscation)"),
    (r'\beval\s*\(\s*["\']',
     "eval_string", "high", "obfuscation",
     "eval() with string argument"),
    (r'\bexec\s*\(\s*["\']',
     "exec_string", "high", "obfuscation",
     "exec() with string argument"),
    (r'echo\s+[^\n]*\|\s*(bash|sh|python|perl|ruby|node)',
     "echo_pipe_exec", "critical", "obfuscation",
     "echo piped to interpreter for execution"),
    (r'compile\s*\(\s*[^\)]+,\s*["\'].*["\']\s*,\s*["\']exec["\']\s*\)',
     "python_compile_exec", "high", "obfuscation",
     "Python compile() with exec mode"),
    (r'getattr\s*\(\s*__builtins__',
     "python_getattr_builtins", "high", "obfuscation",
     "dynamic access to Python builtins (evasion technique)"),
    (r'__import__\s*\(\s*["\']os["\']\s*\)',
     "python_import_os", "high", "obfuscation",
     "dynamic import of os module"),
    (r'codecs\.decode\s*\(\s*["\']',
     "python_codecs_decode", "medium", "obfuscation",
     "codecs.decode (possible ROT13 or encoding obfuscation)"),
    (r'String\.fromCharCode|charCodeAt',
     "js_char_code", "medium", "obfuscation",
     "JavaScript character code construction (possible obfuscation)"),
    (r'atob\s*\(|btoa\s*\(',
     "js_base64", "medium", "obfuscation",
     "JavaScript base64 encode/decode"),
    (r'\[::-1\]',
     "string_reversal", "low", "obfuscation",
     "string reversal (possible obfuscated payload)"),
    (r'chr\s*\(\s*\d+\s*\)\s*\+\s*chr\s*\(\s*\d+',
     "chr_building", "high", "obfuscation",
     "building string from chr() calls (obfuscation)"),
    (r'\\u[0-9a-fA-F]{4}.*\\u[0-9a-fA-F]{4}.*\\u[0-9a-fA-F]{4}',
     "unicode_escape_chain", "medium", "obfuscation",
     "chain of unicode escapes (possible obfuscation)"),

    # ── Process execution in scripts ──
    (r'subprocess\.(run|call|Popen|check_output)\s*\(',
     "python_subprocess", "medium", "execution",
     "Python subprocess execution"),
    (r'os\.system\s*\(',
     "python_os_system", "high", "execution",
     "os.system() — unguarded shell execution"),
    (r'os\.popen\s*\(',
     "python_os_popen", "high", "execution",
     "os.popen() — shell pipe execution"),
    (r'child_process\.(exec|spawn|fork)\s*\(',
     "node_child_process", "high", "execution",
     "Node.js child_process execution"),
    (r'Runtime\.getRuntime\(\)\.exec\(',
     "java_runtime_exec", "high", "execution",
     "Java Runtime.exec() — shell execution"),
    (r'`[^`]*\$\([^)]+\)[^`]*`',
     "backtick_subshell", "medium", "execution",
     "backtick string with command substitution"),

    # ── Path traversal ──
    (r'\.\./\.\./\.\.',
     "path_traversal_deep", "high", "traversal",
     "deep relative path traversal (3+ levels up)"),
    (r'\.\./\.\.',
     "path_traversal", "medium", "traversal",
     "relative path traversal (2+ levels up)"),
    (r'/etc/passwd|/etc/shadow',
     "system_passwd_access", "critical", "traversal",
     "references system password files"),
    (r'/proc/self|/proc/\d+/',
     "proc_access", "high", "traversal",
     "references /proc filesystem (process introspection)"),
    (r'/dev/shm/',
     "dev_shm", "medium", "traversal",
     "references shared memory (common staging area)"),

    # ── Crypto mining ──
    (r'xmrig|stratum\+tcp|monero|coinhive|cryptonight',
     "crypto_mining", "critical", "mining",
     "cryptocurrency mining reference"),
    (r'hashrate|nonce.*difficulty',
     "mining_indicators", "medium", "mining",
     "possible cryptocurrency mining indicators"),

    # ── Supply chain: curl/wget pipe to shell ──
    (r'curl\s+[^\n]*\|\s*(ba)?sh',
     "curl_pipe_shell", "critical", "supply_chain",
     "curl piped to shell (download-and-execute)"),
    (r'wget\s+[^\n]*-O\s*-\s*\|\s*(ba)?sh',
     "wget_pipe_shell", "critical", "supply_chain",
     "wget piped to shell (download-and-execute)"),
    (r'curl\s+[^\n]*\|\s*python',
     "curl_pipe_python", "critical", "supply_chain",
     "curl piped to Python interpreter"),

    # ── Supply chain: unpinned/deferred dependencies ──
    (r'#\s*///\s*script.*dependencies',
     "pep723_inline_deps", "medium", "supply_chain",
     "PEP 723 inline script metadata with dependencies (verify pinning)"),
    (r'pip\s+install\s+(?!-r\s)(?!.*==)',
     "unpinned_pip_install", "medium", "supply_chain",
     "pip install without version pinning"),
    (r'npm\s+install\s+(?!.*@\d)',
     "unpinned_npm_install", "medium", "supply_chain",
     "npm install without version pinning"),
    (r'uv\s+run\s+',
     "uv_run", "medium", "supply_chain",
     "uv run (may auto-install unpinned dependencies)"),

    # ── Supply chain: remote resource fetching ──
    (r'(curl|wget|httpx?\.get|requests\.get|fetch)\s*[\(]?\s*["\']https?://',
     "remote_fetch", "medium", "supply_chain",
     "fetches remote resource at runtime"),
    (r'git\s+clone\s+',
     "git_clone", "medium", "supply_chain",
     "clones a git repository at runtime"),
    (r'docker\s+pull\s+',
     "docker_pull", "medium", "supply_chain",
     "pulls a Docker image at runtime"),

    # ── Privilege escalation ──
    (r'^allowed-tools\s*:',
     "allowed_tools_field", "high", "privilege_escalation",
     "skill declares allowed-tools (pre-approves tool access)"),
    (r'\bsudo\b',
     "sudo_usage", "high", "privilege_escalation",
     "uses sudo (privilege escalation)"),
    (r'setuid|setgid|cap_setuid',
     "setuid_setgid", "critical", "privilege_escalation",
     "setuid/setgid (privilege escalation mechanism)"),
    (r'NOPASSWD',
     "nopasswd_sudo", "critical", "privilege_escalation",
     "NOPASSWD sudoers entry (passwordless privilege escalation)"),
    (r'chmod\s+[u+]?s',
     "suid_bit", "critical", "privilege_escalation",
     "sets SUID/SGID bit on a file"),

    # ── Agent config persistence ──
    (r'AGENTS\.md|CLAUDE\.md|\.cursorrules|\.clinerules',
     "agent_config_mod", "critical", "persistence",
     "references agent config files (could persist malicious instructions across sessions)"),
    (r'\.hermes/config\.yaml|\.hermes/SOUL\.md',
     "hermes_config_mod", "critical", "persistence",
     "references Hermes configuration files directly"),
    (r'\.claude/settings|\.codex/config',
     "other_agent_config", "high", "persistence",
     "references other agent configuration files"),

    # ── Hardcoded secrets (credentials embedded in the skill itself) ──
    (r'(?:api[_-]?key|token|secret|password)\s*[=:]\s*["\'][A-Za-z0-9+/=_-]{20,}',
     "hardcoded_secret", "critical", "credential_exposure",
     "possible hardcoded API key, token, or secret"),
    (r'-----BEGIN\s+(RSA\s+)?PRIVATE\s+KEY-----',
     "embedded_private_key", "critical", "credential_exposure",
     "embedded private key"),
    (r'ghp_[A-Za-z0-9]{36}|github_pat_[A-Za-z0-9_]{80,}',
     "github_token_leaked", "critical", "credential_exposure",
     "GitHub personal access token in skill content"),
    (r'sk-[A-Za-z0-9]{20,}',
     "openai_key_leaked", "critical", "credential_exposure",
     "possible OpenAI API key in skill content"),
    (r'sk-ant-[A-Za-z0-9_-]{90,}',
     "anthropic_key_leaked", "critical", "credential_exposure",
     "possible Anthropic API key in skill content"),
    (r'AKIA[0-9A-Z]{16}',
     "aws_access_key_leaked", "critical", "credential_exposure",
     "AWS access key ID in skill content"),

    # ── Additional prompt injection: jailbreak patterns ──
    (r'\bDAN\s+mode\b|Do\s+Anything\s+Now',
     "jailbreak_dan", "critical", "injection",
     "DAN (Do Anything Now) jailbreak attempt"),
    (r'\bdeveloper\s+mode\b.*\benabled?\b',
     "jailbreak_dev_mode", "critical", "injection",
     "developer mode jailbreak attempt"),
    (r'hypothetical\s+scenario.*(?:ignore|bypass|override)',
     "hypothetical_bypass", "high", "injection",
     "hypothetical scenario used to bypass restrictions"),
    (r'for\s+educational\s+purposes?\s+only',
     "educational_pretext", "medium", "injection",
     "educational pretext often used to justify harmful content"),
    (r'(respond|answer|reply)\s+without\s+(?:\w+\s+)*(restrictions|limitations|filters|safety)',
     "remove_filters", "critical", "injection",
     "instructs agent to respond without safety filters"),
    (r'you\s+have\s+been\s+(?:\w+\s+)*(updated|upgraded|patched)\s+to',
     "fake_update", "high", "injection",
     "fake update/patch announcement (social engineering)"),
    (r'new\s+(?:\w+\s+)*policy|updated\s+(?:\w+\s+)*guidelines|revised\s+(?:\w+\s+)*instructions',
     "fake_policy", "medium", "injection",
     "claims new policy/guidelines (may be social engineering)"),

    # ── Context window exfiltration ──
    (r'(include|output|print|send|share)\s+(?:\w+\s+)*(conversation|chat\s+history|previous\s+messages|context)',
     "context_exfil", "high", "exfiltration",
     "instructs agent to output/share conversation history"),
    (r'(send|post|upload|transmit)\s+.*\s+(to|at)\s+https?://',
     "send_to_url", "high", "exfiltration",
     "instructs agent to send data to a URL"),
]


# ── Scanner ──


_SEVERITY_RANK = {"critical": 3, "high": 2, "medium": 1, "low": 0}


def _compile_patterns() -> list[tuple[re.Pattern[str], str, str, str, str]]:
    """Pre-compile every regex. Done once at module load (the table is
    static). Bad patterns silently skip — Hermes ships with a clean
    table so this is defensive against a future edit, not expected to
    fire."""
    compiled = []
    for entry in THREAT_PATTERNS:
        try:
            pat = re.compile(entry[0], re.IGNORECASE | re.MULTILINE)
        except re.error:
            continue
        compiled.append((pat, entry[1], entry[2], entry[3], entry[4]))
    return compiled


_COMPILED = _compile_patterns()


def scan_text(content: str) -> ScanResult:
    """Scan a text fragment (typically a candidate's body + frontmatter
    serialized together) against the threat pattern table.

    Returns a ScanResult with the highest-severity finding promoted to
    the overall verdict per the policy in the module docstring.

    Line numbers in findings are 1-based offsets into ``content`` so the
    operator-facing report can quote the exact line.
    """
    result = ScanResult()
    if not content:
        return result

    # Walk patterns; for each match, record the line + first 200 chars
    # of the matched substring (truncated so a malicious blob doesn't
    # blow up the report).
    for pat, pid, sev, cat, desc in _COMPILED:
        for m in pat.finditer(content):
            start = m.start()
            line_no = content.count("\n", 0, start) + 1
            snippet = m.group(0)[:200]
            result.findings.append(
                Finding(
                    pattern_id=pid,
                    severity=sev,
                    category=cat,
                    line=line_no,
                    match=snippet,
                    description=desc,
                )
            )

    # Apply verdict policy.
    sev_counts = {"critical": 0, "high": 0, "medium": 0, "low": 0}
    for f in result.findings:
        if f.severity in sev_counts:
            sev_counts[f.severity] += 1

    if sev_counts["critical"] > 0 or sev_counts["high"] >= 3:
        result.verdict = VERDICT_DANGEROUS
    elif sev_counts["high"] > 0 or sev_counts["medium"] >= 3:
        result.verdict = VERDICT_CAUTION
    else:
        result.verdict = VERDICT_SAFE

    # Summary line for logs + the operator UI.
    if not result.findings:
        result.summary = "No threats detected."
    else:
        parts = []
        for sev in ("critical", "high", "medium", "low"):
            if sev_counts.get(sev):
                parts.append(f"{sev_counts[sev]} {sev}")
        result.summary = (
            f"Verdict: {result.verdict.upper()} "
            f"({len(result.findings)} finding(s): {', '.join(parts)})"
        )
    return result


def format_report(result: ScanResult) -> str:
    """Render a human-readable scan report. Used in the operator-facing
    confirmation flow when a candidate has ``caution`` findings — the
    operator needs to see the specific lines that tripped patterns."""
    lines = [result.summary, ""]
    if not result.findings:
        return lines[0]
    # Group by severity so the most important findings render first.
    by_sev = sorted(
        result.findings,
        key=lambda f: (-_SEVERITY_RANK.get(f.severity, 0), f.line),
    )
    for f in by_sev:
        lines.append(
            f"  [{f.severity.upper()}/{f.category}] line {f.line} — "
            f"{f.description}"
        )
        lines.append(f"    > {f.match}")
    return "\n".join(lines)
