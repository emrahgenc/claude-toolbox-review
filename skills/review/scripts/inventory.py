#!/usr/bin/env python3
"""Inventory of the user's whole Claude Code toolbox for Toolbox Review (filesystem only, transcripts only for the
deferred tool list).

Lists every component the review maintains and the static facts it needs:
  skills (your own skills and personal plugins, other skill folders, enabled marketplace and organisation plugins),
  agents (user, personal plugins),
  hooks (settings + plugins), global CLAUDE.md, memory folders, MCP servers, settings, output styles,
  status line, and the git state of the folders that hold personal components.

"always_loaded" totals estimate what enters EVERY session before any work starts (skill and agent
descriptions, global CLAUDE.md): the cheapest tokens to cut, because they are paid in every session.
Secrets never leave this script: env values, MCP commands' env/headers and permission rule bodies are not
printed; only names and counts are.

Usage: inventory.py [--run-dir DIR]   (writes DIR/inventory.json; without it, prints only)
"""
import argparse
import glob
import json
import os
import re
import subprocess
import time

import importlib.util as _ilu

_spec = _ilu.spec_from_file_location("tr_config", os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.py"))
CFG = _ilu.module_from_spec(_spec)
_spec.loader.exec_module(CFG)

HOME = os.path.expanduser("~")
CLAUDE = os.path.join(HOME, ".claude")
SKILL_DIRS = os.path.join(CLAUDE, "skills")


def read(path):
    try:
        with open(path, encoding="utf-8") as fh:
            return fh.read()
    except (OSError, UnicodeDecodeError):
        return ""


def load_json(path):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def frontmatter(text):
    """Small YAML-frontmatter reader: single-line values, quoted values, > / | blocks and - lists."""
    m = re.match(r"^---\n(.*?)\n---\n", text, re.S)
    if not m:
        return {}
    out, key = {}, None
    for line in m.group(1).split("\n"):
        km = re.match(r"^([A-Za-z][\w-]*):\s*(.*)$", line)
        if km:
            key, val = km.group(1), km.group(2).strip()
            if re.match(r"^[>|][-+]?$", val):
                val = ""
            elif len(val) >= 2 and val[0] == val[-1] and val[0] in "\"'":
                val = val[1:-1]
            out[key] = val
        elif key and re.match(r"^\s+\S", line):
            item = line.strip()
            item = item[2:] if item.startswith("- ") else item
            out[key] = (out[key] + (", " if line.strip().startswith("- ") and out[key] else " ") + item).strip(", ")
    return out


def days_ago(path):
    try:
        return int((time.time() - os.path.getmtime(path)) / 86400)
    except OSError:
        return None


def skill_entry(path, namespace=""):
    text = read(path)
    fm = frontmatter(text)
    folder = os.path.dirname(path)
    support = [p for p in glob.glob(os.path.join(folder, "**", "*"), recursive=True)
               if os.path.isfile(p) and p != path and "__pycache__" not in p]
    name = fm.get("name") or os.path.basename(folder)
    return {
        "name": namespace + name, "path": path, "chars": len(text), "lines": text.count("\n") + 1,
        "description_chars": len(fm.get("description", "")),
        "model_invocable": fm.get("disable-model-invocation", "").lower() != "true",
        "allowed_tools": bool(fm.get("allowed-tools")), "disallowed_tools": bool(fm.get("disallowed-tools")),
        "context": fm.get("context", ""), "agent": fm.get("agent", ""), "model": fm.get("model", ""),
        "support_files": len(support), "support_chars": sum(os.path.getsize(p) for p in support),
        "scripts": len([p for p in support if re.search(r"\.(py|sh|js|ts)$", p)]),
        "modified_days_ago": days_ago(path),
        # The review lenses can only Read (no search or listing tool in this environment), so the
        # sensor hands them the paths.
        "support_paths": sorted(support),
    }


def agent_entry(path, namespace=""):
    text = read(path)
    fm = frontmatter(text)
    return {
        "name": namespace + (fm.get("name") or os.path.basename(path)[:-3]), "path": path,
        "chars": len(text), "description_chars": len(fm.get("description", "")),
        "model": fm.get("model", "inherit"), "tools": fm.get("tools", "(all)"),
        "skills": fm.get("skills", ""), "modified_days_ago": days_ago(path),
    }


def git_state(folder):
    def run(*args):
        try:
            return subprocess.run(["git", "-C", folder] + list(args), capture_output=True, text=True,
                                  timeout=20).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            return ""
    if run("rev-parse", "--is-inside-work-tree") != "true":
        return {"git": False}
    return {"git": True, "dirty_files": len([l for l in run("status", "--porcelain").split("\n") if l.strip()]),
            "last_commit": run("log", "-1", "--format=%ad %s", "--date=short"), "remote": bool(run("remote"))}


def tracked(folder, path):
    try:
        r = subprocess.run(["git", "-C", folder, "ls-files", "--error-unmatch", path], capture_output=True,
                           timeout=20)
        return r.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def hook_entries(hooks, source):
    out = []
    for event, groups in (hooks or {}).items():
        for g in groups or []:
            for h in g.get("hooks", []) or []:
                cmd = h.get("command", "")
                script = next((os.path.expanduser(p.replace("$HOME", HOME).strip("\"'"))
                               for p in re.findall(r"[\"']?(?:\$HOME|~|/)[^\s\"']+\.(?:py|sh|js|ts)[\"']?", cmd)), "")
                out.append({"source": source, "event": event, "matcher": g.get("matcher", "*"),
                            "script": script, "script_exists": bool(script) and os.path.exists(script),
                            "timeout": h.get("timeout")})
    return out


def plugin_entries(settings):
    installed = load_json(os.path.join(CLAUDE, "plugins", "installed_plugins.json")).get("plugins", {})
    enabled = settings.get("enabledPlugins", {}) or {}
    out = []
    for key, installs in installed.items():
        if not installs:
            continue
        path = installs[0].get("installPath", "")
        name = key.split("@")[0]
        skills = [skill_entry(p, name + ":") for p in glob.glob(os.path.join(path, "skills", "*", "SKILL.md"))]
        agents = [agent_entry(p, name + ":") for p in glob.glob(os.path.join(path, "agents", "*.md"))]
        hooks = load_json(os.path.join(path, "hooks", "hooks.json")).get("hooks", {})
        out.append({
            "plugin": key, "enabled": bool(enabled.get(key)), "version": installs[0].get("version"),
            "updated": (installs[0].get("lastUpdated") or "")[:10],
            "skills": len(skills), "skill_description_chars": sum(s["description_chars"] for s in skills),
            "agents": len(agents), "agent_description_chars": sum(a["description_chars"] for a in agents),
            "hooks": len(hook_entries(hooks, key)), "mcp": os.path.exists(os.path.join(path, ".mcp.json")),
            "commands": len(glob.glob(os.path.join(path, "commands", "*.md"))),
        })
    return out


def synced_entries():
    """Plugins and skills the desktop app syncs from the organisation: not editable here, but their
    descriptions are loaded in every session, so they count toward the always-loaded cost."""
    out = []
    for pj in glob.glob(os.path.join(CLAUDE, "plugins", "synced", "*", "*", ".claude-plugin", "plugin.json")):
        path = os.path.dirname(os.path.dirname(pj))
        name = load_json(pj).get("name") or os.path.basename(path)
        skills = [skill_entry(p, name + ":") for p in glob.glob(os.path.join(path, "skills", "*", "SKILL.md"))]
        agents = [agent_entry(p, name + ":") for p in glob.glob(os.path.join(path, "agents", "*.md"))]
        hooks = load_json(os.path.join(path, "hooks", "hooks.json")).get("hooks", {})
        out.append({"plugin": name + "@synced", "enabled": True, "version": load_json(pj).get("version"),
                    "updated": "", "skills": len(skills),
                    "skill_description_chars": sum(s["description_chars"] for s in skills if s["model_invocable"]),
                    "agents": len(agents), "agent_description_chars": sum(a["description_chars"] for a in agents),
                    "hooks": len(hook_entries(hooks, name)), "mcp": os.path.exists(os.path.join(path, ".mcp.json")),
                    "commands": len(glob.glob(os.path.join(path, "commands", "*.md")))})
    skills = [skill_entry(p) for p in glob.glob(os.path.join(SKILL_DIRS, "synced", "*", "*", "SKILL.md"))]
    if skills:
        out.append({"plugin": "synced-skills", "enabled": True, "version": None, "updated": "",
                    "skills": len(skills),
                    "skill_description_chars": sum(s["description_chars"] for s in skills if s["model_invocable"]),
                    "agents": 0, "agent_description_chars": 0, "hooks": 0, "mcp": False, "commands": 0})
    return out


def memory_entries():
    out = []
    for d in sorted(glob.glob(os.path.join(CLAUDE, "projects", "*", "memory"))):
        files = [p for p in glob.glob(os.path.join(d, "*.md")) if os.path.basename(p) != "MEMORY.md"]
        index = read(os.path.join(d, "MEMORY.md"))
        indexed = set(re.findall(r"\]\(([^)]+\.md)\)", index))
        names = set()
        links = {}
        types = {}
        notes = []
        for p in sorted(files):
            t = read(p)
            n = re.search(r"^name:\s*(.*)$", t, re.M)
            if n:
                names.add(n.group(1).strip())
            ty = re.search(r"^\s*type:\s*(\w+)", t, re.M)
            k = ty.group(1) if ty else "?"
            types[k] = types.get(k, 0) + 1
            for l in re.findall(r"\[\[([^\]]+)\]\]", t):
                links.setdefault(l, []).append(os.path.basename(p))
            desc = re.search(r"^description:\s*(.*)$", t, re.M)
            notes.append({"file": p, "name": n.group(1).strip() if n else "", "type": k,
                          "description": (desc.group(1).strip().strip("\"'") if desc else "")[:140],
                          "indexed": os.path.basename(p) in indexed, "modified_days_ago": days_ago(p)})
        dangling = {l: srcs for l, srcs in links.items() if l not in names}
        broken_index = sorted(f for f in indexed if not os.path.exists(os.path.join(d, f)))
        out.append({
            "project": os.path.basename(os.path.dirname(d)), "folder": d, "files": len(files), "types": types,
            "index_chars": len(index), "index_lines": len([l for l in index.split("\n") if l.strip()]),
            "not_in_index": len([p for p in files if os.path.basename(p) not in indexed]),
            "dangling_links": len(dangling), "dangling": dangling, "index_points_to_missing": broken_index,
            "oldest_days": max([days_ago(p) or 0 for p in files] or [0]),
            "notes": notes,
        })
    return out


def team_repo_entries():
    """Team repositories from config team_repos, read only: what AI assets they carry and what they load into every
    session opened in them. Nothing is written; findings stay in the report."""
    scope = CFG.config()
    out = []
    for repo in scope.get("team_repos") or []:
        if not os.path.isdir(repo):
            out.append({"repo": repo.replace(HOME, "~"), "missing": True})
            continue
        c = os.path.join(repo, ".claude")
        skills = [skill_entry(p) for p in sorted(glob.glob(os.path.join(c, "skills", "*", "SKILL.md")))]
        for s in skills:
            s.pop("support_paths", None)
        agents = [agent_entry(p) for p in sorted(glob.glob(os.path.join(c, "agents", "*.md")))]
        rules = []
        for p in sorted(glob.glob(os.path.join(c, "rules", "**", "*.md"), recursive=True)):
            t = read(p)
            rules.append({"path": os.path.relpath(p, repo), "chars": len(t), "path_scoped": bool(frontmatter(t).get("paths"))})
        settings = load_json(os.path.join(c, "settings.json"))
        claude_md = read(os.path.join(repo, "CLAUDE.md"))
        sdlc = {}
        for d in sorted(glob.glob(os.path.join(repo, ".sdlc", "*"))):
            if os.path.isdir(d):
                sdlc[os.path.basename(d)] = len([p for p in glob.glob(os.path.join(d, "**", "*"), recursive=True)
                                                 if os.path.isfile(p)])
        always = len(claude_md) + sum(r["chars"] for r in rules if not r["path_scoped"]) \
            + sum(s["description_chars"] for s in skills if s["model_invocable"]) \
            + sum(a["description_chars"] for a in agents)
        out.append({
            "repo": repo.replace(HOME, "~"), "claude_md_chars": len(claude_md),
            "claude_md_lines": claude_md.count("\n") + 1 if claude_md else 0,
            "skills": skills, "agents": agents, "rules": rules,
            "commands": len(glob.glob(os.path.join(c, "commands", "*.md"))),
            "hooks": hook_entries(settings.get("hooks"), "team settings.json"),
            "permissions": {k: len(v or []) for k, v in (settings.get("permissions") or {}).items() if isinstance(v, list)},
            "sdlc_files": sdlc, "always_loaded_chars": always, "always_loaded_tokens": always // 4,
        })
    return out


def deferred_tool_names():
    """Tool names Claude Code lists in every session without loading their schemas (mostly MCP tools).
    Rebuilt from the newest transcript's deferred_tools_delta attachments (added/readded minus removed).
    An estimate: the list differs a little between sessions and folders."""
    files = sorted(glob.glob(os.path.join(CLAUDE, "projects", "*", "*.jsonl")), key=os.path.getmtime, reverse=True)
    for f in files[:10]:
        names, seen = set(), False
        try:
            with open(f, encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    if "deferred_tools_delta" not in line:
                        continue
                    try:
                        a = (json.loads(line).get("attachment") or {})
                    except ValueError:
                        continue
                    if a.get("type") != "deferred_tools_delta":
                        continue
                    seen = True
                    for k in ("addedNames", "readdedNames"):
                        names.update(x for x in a.get(k) or [] if isinstance(x, str))
                    names.difference_update(x for x in a.get("removedNames") or [] if isinstance(x, str))
        except OSError:
            continue
        if seen:
            servers = {}
            for n in names:
                if n.startswith("mcp__") and n.count("__") >= 2:
                    s = n.split("__")[1]
                    servers[s] = servers.get(s, 0) + 1
            started = ""
            try:
                with open(f, encoding="utf-8", errors="replace") as fh:
                    for line in fh:
                        if '"timestamp"' in line:
                            started = (json.loads(line).get("timestamp") or "")[:19]
                            if started:
                                break
            except (OSError, ValueError):
                pass
            # The list is the one that session saw when it started: MCP servers removed later still show here
            # until a newer session exists. Readers should compare source_started with config changes.
            return {"source": os.path.basename(f), "source_started": started, "names": len(names),
                    "chars": sum(len(n) for n in names),
                    "by_server": dict(sorted(servers.items(), key=lambda kv: -kv[1]))}
    return {"source": None, "names": 0, "chars": 0, "by_server": {}}


def mcp_entries():
    cfg = load_json(os.path.join(HOME, ".claude.json"))
    out = [{"scope": "user", "name": n, "transport": (v or {}).get("type") or ("stdio" if (v or {}).get("command")
                                                                              else "?")}
           for n, v in (cfg.get("mcpServers") or {}).items()]
    for proj, pv in (cfg.get("projects") or {}).items():
        for n, v in ((pv or {}).get("mcpServers") or {}).items():
            out.append({"scope": "project", "project": os.path.basename(proj), "name": n,
                        "transport": (v or {}).get("type") or ("stdio" if (v or {}).get("command") else "?")})
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--run-dir")
    a = ap.parse_args()

    settings = load_json(os.path.join(CLAUDE, "settings.json"))
    local = load_json(os.path.join(CLAUDE, "settings.local.json"))
    cfg = CFG.config()
    plugin_dirs = CFG.personal_plugin_dirs(cfg)
    owner = lambda p: next((d for d in plugin_dirs if p.startswith(d.rstrip("/") + "/")), os.path.dirname(p))

    personal_skills = []
    for name, p in sorted(CFG.personal_skills(cfg).items()):
        e = skill_entry(p)
        e["name"] = name
        e["git_tracked"] = tracked(owner(p), p)
        personal_skills.append(e)
    other_skill_folders = []
    for d in sorted(glob.glob(os.path.join(SKILL_DIRS, "*"))):
        if not os.path.isdir(d) or d in plugin_dirs or os.path.basename(d) == "synced" \
                or os.path.basename(d).startswith("."):
            continue
        kind = "plugin" if os.path.exists(os.path.join(d, ".claude-plugin", "plugin.json")) else (
            "skill" if os.path.exists(os.path.join(d, "SKILL.md")) else "folder")
        entry = {"folder": d, "kind": kind}
        entry.update(git_state(d))
        other_skill_folders.append(entry)
    user_agents, plugin_agents = [], []
    for name, p in sorted(CFG.personal_agents(cfg).items()):
        e = agent_entry(p)
        e["name"] = name
        if p.startswith(os.path.join(CLAUDE, "agents")):
            e["git_tracked"] = tracked(os.path.dirname(p), p)
            user_agents.append(e)
        else:
            e["git_tracked"] = tracked(owner(p), p)
            plugin_agents.append(e)
    plugins = plugin_entries(settings) + synced_entries()
    hooks = hook_entries(settings.get("hooks"), "settings.json") + hook_entries(local.get("hooks"),
                                                                                "settings.local.json")
    claude_md = read(os.path.join(CLAUDE, "CLAUDE.md"))
    perms = settings.get("permissions", {}) or {}

    always = {
        "global_claude_md": len(claude_md),
        "personal_skill_descriptions": sum(s["description_chars"] for s in personal_skills if s["model_invocable"]),
        "plugin_skill_descriptions": sum(p["skill_description_chars"] for p in plugins if p["enabled"]),
        "agent_descriptions": sum(x["description_chars"] for x in user_agents + plugin_agents)
                              + sum(p["agent_description_chars"] for p in plugins if p["enabled"]),
    }
    # Deferred (mostly MCP) tool names are listed in every session; counted when budgets say so (estimate).
    deferred = deferred_tool_names()
    if CFG.budgets().get("always_loaded_tokens", {}).get("includes_deferred_tool_names", True):
        always["deferred_tool_names"] = deferred["chars"]
    always["total_chars"] = sum(always.values())
    always["approx_tokens"] = always["total_chars"] // 4

    inv = {
        "personal_plugins": [dict(git_state(d), path=d) for d in plugin_dirs],
        "personal_skills": personal_skills, "other_skill_folders": other_skill_folders,
        "user_agents": user_agents, "plugin_agents": plugin_agents, "plugins": plugins, "hooks": hooks,
        "global_claude_md": {"chars": len(claude_md), "imports": re.findall(r"^@(\S+)", claude_md, re.M)},
        "memory": memory_entries(), "mcp_servers": mcp_entries(),
        "settings": {
            "keys": sorted(settings.keys()), "env_names": sorted((settings.get("env") or {}).keys()),
            "permissions": {k: len(v or []) for k, v in perms.items() if isinstance(v, list)},
            "local_keys": sorted(local.keys()), "status_line": bool(settings.get("statusLine")),
            "output_style": settings.get("outputStyle"), "cleanup_period_days": settings.get("cleanupPeriodDays"),
        },
        "output_styles": [os.path.basename(p) for p in glob.glob(os.path.join(CLAUDE, "output-styles", "*.md"))],
        "always_loaded": always,
        "deferred_tools": deferred,
        "team_repos": team_repo_entries(),
    }

    for pp in inv["personal_plugins"]:
        print("personal plugin %s: git=%s dirty=%s remote=%s" % (pp["path"].replace(HOME, "~"), pp.get("git"),
                                                                pp.get("dirty_files"), pp.get("remote")))
    print("maintained: skills %d (not in git %d) | plugin agents %d | user agents %d" % (
        len(personal_skills), len([s for s in personal_skills if not s["git_tracked"]]), len(plugin_agents),
        len(user_agents)))
    print("plugins: %d installed, %d enabled: %s" % (len(plugins), len([p for p in plugins if p["enabled"]]),
          ", ".join("%s(%ds/%da/%dh)" % (p["plugin"].split("@")[0], p["skills"], p["agents"], p["hooks"])
                    for p in plugins if p["enabled"])))
    print("hooks: " + ", ".join("%s[%s] %s%s" % (h["event"], h["matcher"], os.path.basename(h["script"]),
                                                 "" if h["script_exists"] or not h["script"] else " MISSING")
                                for h in hooks))
    print("memory: %d folders, %d files; largest index %d chars; dangling links %d; not indexed %d" % (
        len(inv["memory"]), sum(m["files"] for m in inv["memory"]),
        max([m["index_chars"] for m in inv["memory"]] or [0]), sum(m["dangling_links"] for m in inv["memory"]),
        sum(m["not_in_index"] for m in inv["memory"])))
    print("mcp: " + ", ".join("%s(%s)" % (m["name"], m["scope"]) for m in inv["mcp_servers"]))
    print("always loaded each session ~%d tokens: %s" % (always["approx_tokens"], ", ".join(
        "%s %d" % (k, v) for k, v in always.items() if k not in ("total_chars", "approx_tokens"))))
    if a.run_dir:
        os.makedirs(a.run_dir, mode=0o700, exist_ok=True)
        with open(os.path.join(a.run_dir, "inventory.json"), "w", encoding="utf-8") as fh:
            json.dump(inv, fh, ensure_ascii=False, indent=2)
        print("written: %s" % os.path.join(a.run_dir, "inventory.json"))


if __name__ == "__main__":
    main()
