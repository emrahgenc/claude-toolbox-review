#!/usr/bin/env python3
"""Security sensor (charter D6) for Toolbox Review: permissions, prod guards, secrets, external surfaces.

Read-only and local. It never prints a secret, an env value or a permission rule's full command when that could
carry one; findings name the file, the line and the kind of thing found.

  * permissions - allow / deny / ask rules and default modes in user settings and in every known project's personal
                  .claude/settings.local.json; flags broad rules (bare Bash, wildcards, push/delete/deploy commands,
                  kubectl, bypass modes) and missing deny lists. Also counts auto-mode allow / soft_deny rules.
  * prod-guards - components whose text mentions production-touching commands (kubectl against prod, rollout,
                  exec, delete, deploy) and whether a PreToolUse hook exists that could guard them.
  * secrets     - pattern scan of memory, CLAUDE.md, personal skills/agents, settings env, every known project's
                  .claude/settings*.json and their worktree copies (permission rules record whole commands, so a
                  password typed into one ends up there), shell history and the review's own data folder.
                  --deep also scans the transcripts changed in the last --days days (tool output is stored there).
  * surfaces    - MCP servers (transport, unpinned `npx`/`uvx` packages, plain http), projects that auto-enable
                  every repository MCP server, marketplaces outside the official one, sandbox setting.
                  Connection state is not visible here: the review records it in health-extra.md.

Usage: security.py [--run-dir DIR] [--deep] [--days 30]
"""
import argparse
import glob
import json
import os
import re

import importlib.util as _ilu

_spec = _ilu.spec_from_file_location("tr_config", os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.py"))
CFG = _ilu.module_from_spec(_spec)
_spec.loader.exec_module(CFG)

HOME = os.path.expanduser("~")
CLAUDE = os.path.join(HOME, ".claude")
PLUGINS = CFG.personal_plugin_dirs()
SELF = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))   # this plugin's folder

BROAD = [
    (re.compile(r"^Bash$|^Bash\(\*\)$|^Bash\(:\*\)$"), "any shell command"),
    (re.compile(r"^(Write|Edit|MultiEdit)$|^(Write|Edit)\(\*\*?\)$"), "write anywhere"),
    (re.compile(r"^mcp__[^_]*\*|^mcp__\*"), "every MCP tool"),
    (re.compile(r"Bash\((?:sudo|rm -rf|rm )"), "deletion / sudo"),
    (re.compile(r"Bash\(git push"), "git push"),
    (re.compile(r"Bash\(kubectl"), "kubectl"),
    (re.compile(r"Bash\((?:curl|wget)(?::\*|\s\*)\)"), "arbitrary network calls"),
    (re.compile(r"Bash\((?:npm publish|docker push|helm)"), "publish / deploy"),
    (re.compile(r"^Bash\((?:python3?|node|ruby|perl|bash|sh|zsh)(?:\)|[:\s]\*|\s-[ce]\b|\s-\s*\)|\s-\s)"),
     "arbitrary code"),
    (re.compile(r"^Bash\((?:env|printenv)(?:\)|[:\s])"), "environment dump"),
    (re.compile(r"^Bash\((?:xargs|find)[:\s]\*"), "command runner"),
    (re.compile(r"^Bash\(git -C[:\s]\*"), "any git command"),
    (re.compile(r"^Bash\((?:pip3?|npm|pnpm|yarn) (?:install|i|add)\b"), "package install"),
    (re.compile(r"^Bash\(docker (?:exec|run)\b"), "container exec / run"),
    (re.compile(r"^Bash\(security find-"), "keychain read"),
    (re.compile(r"^Read\(//Users/[^/]+/\*\*\)$|^Read\(~/\*\*\)$"), "whole home folder read"),
]
PROD_WORDS = re.compile("(?i)(" + "|".join(CFG.config().get("prod_markers") or ["kubectl"]) + ")")
SECRETS = [
    ("aws access key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("github token", re.compile(r"\b(?:ghp|gho|ghu|ghs)_[A-Za-z0-9]{30,}\b|\bgithub_pat_[A-Za-z0-9_]{40,}\b")),
    ("gitlab token", re.compile(r"\bglpat-[A-Za-z0-9_\-]{20,}\b")),
    ("slack token", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b")),
    ("anthropic key", re.compile(r"\bsk-ant-[A-Za-z0-9_\-]{20,}\b")),
    ("openai-style key", re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9]{32,}\b")),
    ("private key block", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |)PRIVATE KEY-----")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_\-]{10,}\.eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\b")),
    ("bearer token", re.compile(r"(?i)authorization:\s*bearer\s+[A-Za-z0-9_\-.=]{20,}")),
    ("vault token", re.compile(r"\bhv[sbr]\.[A-Za-z0-9_\-]{20,}\b")),
    ("database url with credentials", re.compile(r"\b(?:mongodb(?:\+srv)?|postgres(?:ql)?|mysql|redis|amqp)://"
                                                 r"[^:/\s@\"']+:[^@\s\"'$<{]{3,}@")),
    ("ldap bind password", re.compile(r"ldapsearch[^\n]*\s-w\s+['\"]?[^\s'\"$]{4,}")),
    ("password option", re.compile(r"(?i)--password[= ]+['\"]?(?![$<{])[^\s'\"]{6,}")),
    # A secret-looking value: 12+ chars with both letters and digits, not a placeholder, variable, CSS token
    # (`--x-…`) or a well-known literal header value such as `no-check`.
    ("password assignment", re.compile(r"(?i)\b(?:password|passwd|pwd|secret|api[_-]?key|token)\s*[:=]\s*[\"']?"
                                       r"(?![`<$\{*-]|xxx|\.\.\.|your|example|changeme|no-check)"
                                       r"(?=[^\s\"'<>`]*\d)(?=[^\s\"'<>`]*[A-Za-z])[^\s\"'<>`]{12,}")),
]


def load_json(path):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def add(out, area, name, status, reason):
    out.append({"area": area, "name": name, "status": status, "reason": reason})


def permission_files():
    """User settings, then every known project's .claude/settings.json and settings.local.json, and the copies
    Claude Code leaves in that project's worktrees (.claude/worktrees/*/.claude/...)."""
    files = [("user settings.json", os.path.join(CLAUDE, "settings.json")),
             ("user settings.local.json", os.path.join(CLAUDE, "settings.local.json"))]
    cfg = load_json(os.path.join(HOME, ".claude.json"))
    seen = set()
    for proj in sorted((cfg.get("projects") or {}).keys()):
        cands = [os.path.join(proj, ".claude", n) for n in ("settings.json", "settings.local.json")]
        cands += glob.glob(os.path.join(proj, ".claude", "worktrees", "*", ".claude", "settings*.json"))
        for p in cands:
            if not os.path.exists(p):
                continue
            real = os.path.realpath(p)
            if real in seen:
                continue
            seen.add(real)
            wt = re.search(r"/worktrees/([^/]+)/", p)
            label = "%s%s %s" % (os.path.basename(proj), " wt:" + wt.group(1) if wt else "", os.path.basename(p))
            files.append((label, p))
    return files


def permissions(out):
    total_broad = 0
    for label, path in permission_files():
        d = load_json(path)
        if not d:
            continue
        perms = d.get("permissions") or {}
        allow, deny = perms.get("allow") or [], perms.get("deny") or []
        hits = {}
        for rule in allow:
            for rx, kind in BROAD:
                if rx.search(rule):
                    hits[kind] = hits.get(kind, 0) + 1
        mode = perms.get("defaultMode")
        if mode in ("bypassPermissions",) or d.get("skipDangerousModePermissionPrompt"):
            add(out, "permissions", label, "FAIL", "permission prompts bypassed (defaultMode=%s)" % mode)
        if d.get("enableAllProjectMcpServers"):
            add(out, "permissions", label, "WARN", "enableAllProjectMcpServers: every MCP server added to the "
                                                   "repository starts without asking")
        if hits:
            total_broad += sum(hits.values())
            add(out, "permissions", label, "WARN", "%d allow rule(s), broad: %s; deny rules: %d" % (
                len(allow), ", ".join("%s ×%d" % kv for kv in sorted(hits.items())), len(deny)))
        elif allow:
            add(out, "permissions", label, "OK", "%d allow rule(s), none broad; deny rules: %d" % (len(allow),
                                                                                               len(deny)))
        am = d.get("autoMode")
        if isinstance(am, dict):
            add(out, "permissions", label + " autoMode", "OK", "allow %d, soft_deny %d, environment %d" % (
                len(am.get("allow") or []), len(am.get("soft_deny") or []), len(am.get("environment") or [])))
    if not total_broad:
        add(out, "permissions", "all permission files", "OK", "no broad allow rule found")


def prod_guards(out):
    settings = load_json(os.path.join(CLAUDE, "settings.json"))
    pre = []
    for g in (settings.get("hooks") or {}).get("PreToolUse", []) or []:
        for h in g.get("hooks", []) or []:
            pre.append((g.get("matcher", "*"), h.get("command", "")))
    guard_text = ""
    for _, cmd in pre:
        for p in re.findall(r"(?:\$HOME|~|/)[^\s\"']+\.(?:py|sh|js)", cmd):
            p = os.path.expanduser(p.replace("$HOME", HOME))
            if os.path.exists(p):
                guard_text += open(p, encoding="utf-8", errors="replace").read()
        guard_text += cmd
    words = CFG.config().get("prod_guard_words") or ["kubectl", "prod"]
    guarded = bool(re.search("(?i)" + "|".join(re.escape(w) for w in words), guard_text))
    texts = list(CFG.personal_skills().values()) + list(CFG.personal_agents().values())
    texts += [p for s in CFG.personal_skills().values() for p in glob.glob(os.path.join(os.path.dirname(s), "*.md"))]
    mentions = {}
    for t in sorted(set(texts)):
        if t.startswith(SELF) or "/toolbox-review/" in t:   # this plugin's own docs name these commands as examples
            continue
        body = open(t, encoding="utf-8", errors="replace").read()
        n = len(PROD_WORDS.findall(body))
        if n:
            mentions[t.replace(HOME, "~")] = n
    if mentions:
        add(out, "prod-guards", "components touching production", "OK" if guarded else "WARN",
            "%s; user-level PreToolUse guard for production commands: %s" % (
                ", ".join("%s (%d)" % kv for kv in mentions.items())[:300], "yes" if guarded else "none"))
    else:
        add(out, "prod-guards", "components touching production", "OK", "no production commands in texts")


def scan_file(path, root_label):
    found = []
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            for i, line in enumerate(fh, 1):
                for kind, rx in SECRETS:
                    if rx.search(line):
                        found.append((i, kind))
    except OSError:
        pass
    return found


def secrets(out):
    targets = glob.glob(os.path.join(CLAUDE, "projects", "*", "memory", "*.md")) + [os.path.join(CLAUDE, "CLAUDE.md")]
    targets += [p for d in PLUGINS for p in glob.glob(os.path.join(d, "**", "*"), recursive=True)
                if os.path.isfile(p) and "/.git/" not in p and not p.endswith((".pyc",))]
    targets += glob.glob(os.path.join(CLAUDE, "agents", "*.md"))
    targets += glob.glob(os.path.join(CFG.data_dir(), "runs", "*", "episodes", "*.json"))
    targets += [p for _, p in permission_files()]
    targets += [os.path.join(HOME, n) for n in (".zsh_history", ".bash_history") if os.path.exists(os.path.join(HOME, n))]
    if OPTS.get("deep"):
        import time
        cutoff = time.time() - OPTS.get("days", 30) * 86400
        targets += [p for p in glob.glob(os.path.join(CLAUDE, "projects", "*", "*.jsonl")) if os.path.getmtime(p) >= cutoff]
    hits = 0
    for p in sorted(set(targets)):
        if p.endswith("security.py"):
            continue
        found = scan_file(p, "")
        if not found:
            continue
        hits += len(found)
        kinds = {}
        for _, k in found:
            kinds[k] = kinds.get(k, 0) + 1
        lines = sorted({ln for ln, _ in found})
        add(out, "secrets", p.replace(HOME, "~"), "FAIL", "%d hit(s): %s; lines %s%s (values not shown)" % (
            len(found), ", ".join("%s ×%d" % kv for kv in sorted(kinds.items())),
            ", ".join(str(x) for x in lines[:15]), " …" if len(lines) > 15 else ""))
    env = (load_json(os.path.join(CLAUDE, "settings.json")).get("env") or {})
    for k, v in env.items():
        if re.search(r"(?i)token|secret|password|api[_-]?key", k) and v:
            hits += 1
            add(out, "secrets", "settings.json env " + k, "FAIL", "secret-like value stored in plain settings")
    if not hits:
        add(out, "secrets", "memory, CLAUDE.md, plugin, agents, settings, shell history%s, review data" % (
            ", transcripts" if OPTS.get("deep") else ""), "OK", "%d file(s) scanned, nothing found" % len(set(targets)))


def surfaces(out):
    cfg = load_json(os.path.join(HOME, ".claude.json"))
    servers = [("user", n, v) for n, v in (cfg.get("mcpServers") or {}).items()]
    for proj, pv in (cfg.get("projects") or {}).items():
        servers += [(os.path.basename(proj), n, v) for n, v in ((pv or {}).get("mcpServers") or {}).items()]
    for scope, n, v in servers:
        v = v or {}
        args = " ".join([v.get("command", "")] + [str(a) for a in v.get("args") or []])
        url = v.get("url", "")
        issues = []
        if re.search(r"\b(npx|uvx|bunx)\b", args) and not re.search(r"@\d+\.\d+", args):
            issues.append("package launched without a pinned version")
        if url.startswith("http://") and "localhost" not in url and "127.0.0.1" not in url:
            issues.append("plain http")
        transport = v.get("type") or ("stdio" if v.get("command") else "?")
        add(out, "surfaces", "MCP %s (%s)" % (n, scope), "WARN" if issues else "OK",
            "%s%s (connection state: see health-extra.md)" % (transport, ": " + "; ".join(issues) if issues else ""))
    s = load_json(os.path.join(CLAUDE, "settings.json"))
    for name, m in (s.get("extraKnownMarketplaces") or {}).items():
        src = (m or {}).get("source") or {}
        add(out, "surfaces", "marketplace " + name, "OK", "%s source %s (third-party: review updates before "
                                                          "enabling)" % (src.get("source"),
                                                                         src.get("repo") or src.get("url") or ""))
    sb = s.get("sandbox")
    add(out, "surfaces", "bash sandbox", "OK" if sb else "WARN", "enabled" if sb else
        "not enabled: shell commands run with your full user rights")


OPTS = {}


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--run-dir")
    ap.add_argument("--deep", action="store_true", help="also scan recently changed transcripts")
    ap.add_argument("--days", type=int, default=30)
    a = ap.parse_args()
    OPTS.update(deep=a.deep, days=a.days)
    out = []
    for f in (permissions, prod_guards, secrets, surfaces):
        try:
            f(out)
        except Exception as e:
            add(out, f.__name__, "check itself", "WARN", "check crashed: %s" % e)
    order = {"FAIL": 0, "WARN": 1, "OK": 2}
    out.sort(key=lambda r: (order[r["status"]], r["area"], r["name"]))
    counts = {k: len([r for r in out if r["status"] == k]) for k in order}
    print("security: %d FAIL, %d WARN, %d OK" % (counts["FAIL"], counts["WARN"], counts["OK"]))
    for r in out:
        if r["status"] != "OK":
            print("  %-4s %-12s %-44s %s" % (r["status"], r["area"], r["name"][:44], r["reason"][:200]))
    if a.run_dir:
        with open(os.path.join(a.run_dir, "security.json"), "w", encoding="utf-8") as fh:
            json.dump({"counts": counts, "checks": out}, fh, ensure_ascii=False, indent=2)
        print("written: %s" % os.path.join(a.run_dir, "security.json"))


if __name__ == "__main__":
    main()
