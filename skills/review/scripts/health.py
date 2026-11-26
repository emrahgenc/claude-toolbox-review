#!/usr/bin/env python3
"""Health sensor (charter D10) for Toolbox Review: is every part of the toolbox still working and recoverable?

Checks, all local and read-only (nothing is executed except syntax checks and `git`/`launchctl` queries):
  * copies   - git state of the folders that hold personal components: remote or not (a laptop-only copy is a
               single point of loss), uncommitted files, unpushed commits; components outside any git repo.
  * scripts  - every .py / .sh in personal skills and hook scripts: present, syntax-valid (py_compile / bash -n).
  * hooks    - every hook in user settings: script present and syntax-valid.
  * paths    - absolute or ~ paths written in skill and agent texts that no longer exist (stale instructions).
  * automations - launchd agents whose label or program points into ~/.claude (loaded? last exit status? log age).
  * runtime  - python3 version, claude CLI present, transcript retention setting and oldest transcript.
  * watchers / evidence - status files of your own background jobs and folders that should not grow old
               (config: watchers, evidence_folders).
Scheduled tasks and MCP connection state are not visible from a script: the review adds them itself.

Each check yields OK / WARN / FAIL with a one-line reason. Usage: health.py [--run-dir DIR]
"""
import argparse
import glob
import json
import os
import plistlib
import re
import subprocess
import sys
import time

import importlib.util as _ilu

_spec = _ilu.spec_from_file_location("tr_config", os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.py"))
CFG = _ilu.module_from_spec(_spec)
_spec.loader.exec_module(CFG)

HOME = os.path.expanduser("~")
CLAUDE = os.path.join(HOME, ".claude")
PLUGINS = CFG.personal_plugin_dirs()


def run(cmd, cwd=None, timeout=20):
    try:
        p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout)
        return p.returncode, p.stdout.strip(), p.stderr.strip()
    except (OSError, subprocess.SubprocessError) as e:
        return 1, "", str(e)


def load_json(path):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def check(results, area, name, status, reason):
    results.append({"area": area, "name": name, "status": status, "reason": reason})


def git_copies(results):
    folders = list(PLUGINS) + [d for d in glob.glob(os.path.join(CLAUDE, "skills", "*"))
                               if os.path.isdir(os.path.join(d, ".git")) and d not in PLUGINS]
    for d in folders:
        name = os.path.relpath(d, HOME)
        rc, out, _ = run(["git", "-C", d, "rev-parse", "--is-inside-work-tree"])
        if out != "true":
            check(results, "copies", name, "FAIL", "not a git repository")
            continue
        _, remotes, _ = run(["git", "-C", d, "remote"])
        _, porcelain, _ = run(["git", "-C", d, "status", "--porcelain"])
        dirty = len([l for l in porcelain.split("\n") if l.strip()])
        _, ahead, _ = run(["git", "-C", d, "rev-list", "--count", "@{u}..HEAD"]) if remotes else (0, "", "")
        if not remotes:
            check(results, "copies", name, "FAIL", "no remote: the only copy is on this laptop"
                  + (", %d uncommitted file(s)" % dirty if dirty else ""))
        elif ahead and ahead.isdigit() and int(ahead) > 0:
            check(results, "copies", name, "WARN", "%s commit(s) not pushed" % ahead
                  + (", %d uncommitted file(s)" % dirty if dirty else ""))
        elif dirty:
            check(results, "copies", name, "WARN", "%d uncommitted file(s)" % dirty)
        else:
            check(results, "copies", name, "OK", "remote set, clean")
    outside = glob.glob(os.path.join(CLAUDE, "agents", "*.md")) + glob.glob(os.path.join(CLAUDE, "hooks", "*"))
    if outside:
        check(results, "copies", "~/.claude/agents + ~/.claude/hooks", "WARN",
              "%d file(s) outside any git repo: %s" % (len(outside), ", ".join(sorted(os.path.basename(p)
                                                                                 for p in outside))[:200]))
    claude_md = os.path.join(CLAUDE, "CLAUDE.md")
    if os.path.exists(claude_md):
        check(results, "copies", "~/.claude/CLAUDE.md", "WARN", "outside any git repo")


def syntax(path):
    if path.endswith(".py"):
        try:
            with open(path, encoding="utf-8") as fh:
                compile(fh.read(), path, "exec")   # syntax only, writes nothing
            return None
        except (SyntaxError, ValueError, UnicodeDecodeError) as e:
            return ("%s line %s" % (e.__class__.__name__, getattr(e, "lineno", "?")))[:160]
        except OSError as e:
            return str(e)[:160]
    if path.endswith(".sh"):
        rc, _, err = run(["bash", "-n", path])
        return None if rc == 0 else err[:160]
    return None


def scripts(results):
    files = [p for d in PLUGINS for p in glob.glob(os.path.join(d, "**", "*"), recursive=True)
             if p.endswith((".py", ".sh")) and "__pycache__" not in p and "/.git/" not in p]
    bad = []
    for p in files:
        err = syntax(p)
        if err:
            bad.append((p, err))
    for p, err in bad:
        check(results, "scripts", p.replace(HOME, "~"), "FAIL", "syntax: " + err)
    check(results, "scripts", "personal plugin scripts", "OK" if not bad else "FAIL",
          "%d script(s), %d broken" % (len(files), len(bad)))


def hooks(results):
    settings = load_json(os.path.join(CLAUDE, "settings.json"))
    for event, groups in (settings.get("hooks") or {}).items():
        for g in groups or []:
            for h in g.get("hooks", []) or []:
                cmd = h.get("command", "")
                paths = [os.path.expanduser(p.replace("$HOME", HOME).strip("\"'"))
                         for p in re.findall(r"[\"']?(?:\$HOME|~|/)[^\s\"']+\.(?:py|sh|js)[\"']?", cmd)]
                label = "%s[%s]" % (event, g.get("matcher", "*"))
                if not paths:
                    check(results, "hooks", label, "OK", "inline command")
                    continue
                for p in paths:
                    if not os.path.exists(p):
                        check(results, "hooks", label, "FAIL", "script missing: " + os.path.relpath(p, HOME))
                        continue
                    err = syntax(p)
                    check(results, "hooks", label, "FAIL" if err else "OK",
                          ("syntax: " + err) if err else os.path.relpath(p, HOME))


PATH_IN_TEXT = re.compile(r"(?<![\w$])((?:~|/Users/[\w.-]+|/home/[\w.-]+)/[\w@.\-/]*[\w@\-])")


def stale_paths(results):
    texts = list(CFG.personal_skills().values()) + list(CFG.personal_agents().values())
    total = 0
    for t in texts:
        try:
            body = open(t, encoding="utf-8").read()
        except OSError:
            continue
        missing = set()
        for raw in PATH_IN_TEXT.findall(body):
            p = os.path.expanduser(raw.rstrip(".,;:)`'\""))
            if any(ch in p for ch in "*<>{}$") or p.endswith(("/", "-")):
                continue
            if not os.path.exists(p) and not os.path.exists(os.path.dirname(p) or "/"):
                missing.add(raw)
            elif not os.path.exists(p) and re.search(r"\.\w{1,5}$", p):
                missing.add(raw)
        total += len(missing)
        if missing:
            check(results, "paths", os.path.relpath(t, HOME), "WARN",
                  "%d path(s) not found: %s" % (len(missing), ", ".join(sorted(missing))[:220]))
    if not total:
        check(results, "paths", "skill and agent texts", "OK", "every absolute path exists")


def automations(results):
    rc, listing, _ = run(["launchctl", "list"])
    loaded = {}
    for line in listing.split("\n")[1:]:
        parts = line.split("\t")
        if len(parts) == 3:
            loaded[parts[2]] = (parts[0], parts[1])
    for plist in glob.glob(os.path.join(HOME, "Library", "LaunchAgents", "*.plist")):
        try:
            with open(plist, "rb") as fh:
                d = plistlib.load(fh)
        except Exception:
            continue
        prog = " ".join(d.get("ProgramArguments") or [d.get("Program") or ""])
        if CLAUDE not in prog and "claude" not in d.get("Label", "").lower():
            continue
        label = d.get("Label", os.path.basename(plist))
        if label not in loaded:
            check(results, "automations", label, "WARN", "plist present but not loaded")
            continue
        pid, status = loaded[label]
        logs = [p for p in (d.get("StandardErrorPath"), d.get("StandardOutPath")) if p]
        age = min([int((time.time() - os.path.getmtime(p)) / 60) for p in logs if os.path.exists(p)] or [-1])
        interval = d.get("StartInterval")
        # A quiet job may log only when it has something to say, so a log is "stale" only after a full day.
        stale = interval and age >= 0 and age > 24 * 60
        st = "FAIL" if status not in ("0", "-") else ("WARN" if stale else "OK")
        check(results, "automations", label, st, "last exit %s, log updated %s min ago%s" % (
            status, age if age >= 0 else "?", " (expected every %d min)" % (interval // 60) if interval else ""))
    watcher(results)


def watcher(results):
    """Your own background jobs often exit 0 and log only events; their real state is in a status file.
    config watchers: [{"name", "status_file", "error_file" (optional), "max_age_minutes" (default 1440)}].
    The first word ERROR in the status file, or an existing error_file, means failing."""
    for w in CFG.config().get("watchers", []):
        last = CFG.expand(w.get("status_file", ""))
        name = w.get("name") or os.path.basename(last)
        if not last or not os.path.exists(last):
            check(results, "automations", name, "WARN", "status file missing: %s" % (last or "?"))
            continue
        with open(last, encoding="utf-8", errors="replace") as fh:
            line = fh.readline().strip()
        age = int((time.time() - os.path.getmtime(last)) / 60)
        err = CFG.expand(w.get("error_file", ""))
        failing = (err and os.path.exists(err)) or " ERROR" in (" " + line.upper()[:40])
        st = "FAIL" if failing else ("WARN" if age > int(w.get("max_age_minutes", 1440)) else "OK")
        check(results, "automations", name, st, "%s (%d min ago)" % (line[:120], age))


def evidence_folders(results):
    """Folders of raw evidence (incident logs, exports) should not outlive their use (charter §7).
    config evidence_folders: [{"path", "max_age_days" (default 14)}]."""
    for e in CFG.config().get("evidence_folders", []):
        root = CFG.expand(e.get("path", ""))
        if not root or not os.path.isdir(root):
            continue
        days = int(e.get("max_age_days", 14))
        old = [p for p in glob.glob(os.path.join(root, "*")) if os.path.isdir(p)
               and (time.time() - os.path.getmtime(p)) / 86400 > days]
        check(results, "retention", root.replace(HOME, "~"), "WARN" if old else "OK",
              "%d folder(s) older than %d days" % (len(old), days) if old else "no old folders")


def runtime(results):
    v = sys.version_info
    check(results, "runtime", "python3 (hooks, these scripts)", "WARN" if (v.major, v.minor) < (3, 10) else "OK",
          "%d.%d.%d%s" % (v.major, v.minor, v.micro,
                          " (end of life: keep scripts 3.9-compatible or upgrade)" if (v.major, v.minor) < (3, 10)
                          else ""))
    for brew in ("/opt/homebrew/bin/python3", "/usr/local/bin/python3"):
        if os.path.exists(brew):
            rc, out, _ = run([brew, "--version"])
            check(results, "runtime", brew, "OK", (out or "?") + " (used by tools that put Homebrew first on PATH)")
    rc, out, _ = run(["claude", "--version"])
    check(results, "runtime", "claude CLI", "OK" if rc == 0 else "FAIL", out or "not found")
    settings = load_json(os.path.join(CLAUDE, "settings.json"))
    days = settings.get("cleanupPeriodDays")
    files = glob.glob(os.path.join(CLAUDE, "projects", "*", "*.jsonl"))
    oldest = int((time.time() - min([os.path.getmtime(p) for p in files] or [time.time()])) / 86400)
    check(results, "runtime", "transcript retention", "OK" if days else "WARN",
          "cleanupPeriodDays=%s, oldest transcript %d days old%s" % (
              days if days else "unset (default)", oldest,
              "" if days else "; the collector keeps its own copies, but set it explicitly"))


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--run-dir")
    a = ap.parse_args()
    results = []
    for f in (git_copies, scripts, hooks, stale_paths, automations, evidence_folders, runtime):
        try:
            f(results)
        except Exception as e:  # a broken check must not hide the others
            check(results, f.__name__, "check itself", "WARN", "check crashed: %s" % e)
    order = {"FAIL": 0, "WARN": 1, "OK": 2}
    results.sort(key=lambda r: (order[r["status"]], r["area"], r["name"]))
    counts = {k: len([r for r in results if r["status"] == k]) for k in order}
    print("health: %d FAIL, %d WARN, %d OK" % (counts["FAIL"], counts["WARN"], counts["OK"]))
    for r in results:
        if r["status"] != "OK":
            print("  %-4s %-12s %-40s %s" % (r["status"], r["area"], r["name"][:40], r["reason"]))
    if a.run_dir:
        with open(os.path.join(a.run_dir, "health.json"), "w", encoding="utf-8") as fh:
            json.dump({"counts": counts, "checks": results}, fh, ensure_ascii=False, indent=2)
        print("written: %s" % os.path.join(a.run_dir, "health.json"))


if __name__ == "__main__":
    main()
