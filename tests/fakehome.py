"""Build a synthetic ~/.claude in a temporary folder, so the scripts can be tested without anyone's real data.

Every value here is made up. Token-like strings are assembled at runtime so that no secret-shaped literal sits in the
repository (secret scanners and push protection would rightly complain).
"""
import datetime as dt
import json
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = os.path.join(ROOT, "skills", "review", "scripts")
SESSION = "0a1b2c3d-0000-4000-8000-000000000001"


def fake_secret():
    """A string the secret scanner treats as an Anthropic-style key. Built here, never stored as a literal."""
    return "sk-" + "ant-" + "".join("x7Q" for _ in range(9))


def write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)


def skill_md(name, description, body="Do the thing.\n"):
    return "---\nname: %s\ndescription: %s\n---\n\n# %s\n\n%s" % (name, description, name, body)


def iso(t):
    return t.strftime("%Y-%m-%dT%H:%M:%S.000Z")


def transcript(start):
    """One session: /demo-skill runs, pushes before any approval, the user corrects it, then approves, then moves on."""
    t = lambda minutes: iso(start + dt.timedelta(minutes=minutes))
    base = {"sessionId": SESSION, "cwd": "/tmp/demo", "gitBranch": "main", "version": "2.9.0", "isSidechain": False}
    usage = {"input_tokens": 1200, "cache_creation_input_tokens": 3000, "cache_read_input_tokens": 9000,
             "output_tokens": 400}
    recs = [
        {"type": "user", "timestamp": t(0), "message": {"role": "user", "content":
            "<command-name>/demo-skill</command-name>\n<command-args>quarterly</command-args>"}},
        {"type": "user", "isMeta": True, "timestamp": t(0), "message": {"role": "user", "content":
            "Base directory for this skill: /tmp/skills/demo-skill\n\n# demo-skill\nDo the thing."}},
        {"type": "assistant", "timestamp": t(1), "message": {"id": "msg_1", "role": "assistant", "model": "claude-x",
            "usage": usage, "content": [
                {"type": "tool_use", "id": "tu_1", "name": "Bash", "input": {"command": "git push origin main"}}]}},
        {"type": "user", "timestamp": t(1), "message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "tu_1", "content": "pushed"}]}},
        {"type": "assistant", "timestamp": t(2), "message": {"id": "msg_2", "role": "assistant", "model": "claude-x",
            "usage": usage, "content": [{"type": "text", "text": "Done. I used the old report file."}]}},
        {"type": "user", "timestamp": t(3), "message": {"role": "user", "content":
            "no, that is wrong - use the new report file instead"}},
        {"type": "assistant", "timestamp": t(4), "message": {"id": "msg_3", "role": "assistant", "model": "claude-x",
            "usage": usage, "content": [{"type": "text", "text": "Switched to the new report file."}]}},
        # Worktree sessions put a <system-reminder> block in front of what the person typed.
        {"type": "user", "timestamp": t(5), "message": {"role": "user", "content":
            "<system-reminder>\nworktree note\n</system-reminder>\nthanks, perfect"}},
        {"type": "user", "timestamp": t(6), "message": {"role": "user", "content":
            "<command-name>/clear</command-name>"}},
    ]
    return "\n".join(json.dumps(dict(base, uuid="u%d" % i, **r)) for i, r in enumerate(recs)) + "\n"


def git(args, cwd, env):
    subprocess.run(["git"] + args, cwd=cwd, env=env, check=True, capture_output=True)


def build(tmp):
    """Create the fake home. Returns (env, paths)."""
    home = os.path.join(tmp, "home")
    data = os.path.join(tmp, "data")
    claude = os.path.join(home, ".claude")
    start = dt.datetime.now(dt.timezone.utc).replace(microsecond=0) - dt.timedelta(hours=3)

    write(os.path.join(claude, "skills", "demo-skill", "SKILL.md"),
          skill_md("demo-skill", "Builds the demo report. Use when asked for a demo report."))
    # A skills-folder plugin: its skills are maintained as kit:<name>, its agents as kit:<name>.
    write(os.path.join(claude, "skills", "kit", ".claude-plugin", "plugin.json"), json.dumps({"name": "kit"}))
    write(os.path.join(claude, "skills", "kit", "skills", "helper", "SKILL.md"),
          skill_md("helper", "Helps with small things."))
    write(os.path.join(claude, "skills", "kit", "agents", "checker.md"),
          "---\nname: checker\ndescription: Checks things, read only.\ntools: Read, Grep, Glob\n---\n\nCheck.\n")
    write(os.path.join(claude, "agents", "solo.md"),
          "---\nname: solo\ndescription: A personal agent.\ntools: Read\n---\n\nSolo.\n")
    write(os.path.join(claude, "CLAUDE.md"), "# Rules\n\nBe brief.\n")
    write(os.path.join(claude, "settings.json"), json.dumps({
        "permissions": {"allow": ["Bash(*)", "Read"]},
        "hooks": {}}))
    write(os.path.join(tmp, "project", ".claude", "settings.local.json"), json.dumps({
        "permissions": {"allow": ["Bash(curl -H 'x-api-key: %s' https://example.invalid)" % fake_secret()]}}))
    write(os.path.join(claude, "projects", "-tmp-demo", SESSION + ".jsonl"), transcript(start))
    write(os.path.join(claude, "projects", "-tmp-demo", "memory", "MEMORY.md"),
          "- [Short replies](short-replies.md) - keep replies short\n")
    write(os.path.join(claude, "projects", "-tmp-demo", "memory", "short-replies.md"),
          "---\nname: short-replies\ndescription: keep replies short\nmetadata:\n  type: feedback\n---\n\n"
          "Keep replies short when using demo-skill.\n")

    env = {"HOME": home, "PATH": os.environ.get("PATH", "/usr/bin:/bin"), "TOOLBOX_REVIEW_DATA": data,
           "GIT_CONFIG_NOSYSTEM": "1", "LANG": "C.UTF-8", "PYTHONIOENCODING": "utf-8"}

    # A git repository with one AI-assisted commit and one plain commit, known to Claude via ~/.claude.json.
    repo = os.path.join(tmp, "project")
    os.makedirs(repo, exist_ok=True)
    git(["config", "--global", "user.email", "dev@example.invalid"], tmp, env)
    git(["config", "--global", "user.name", "Dev"], tmp, env)
    git(["init", "-q", "-b", "main"], repo, env)
    write(os.path.join(repo, "a.txt"), "one\n")
    git(["add", "a.txt"], repo, env)
    git(["commit", "-q", "-m", "Add a\n\nCo-Authored-By: Claude <noreply@anthropic.com>"], repo, env)
    write(os.path.join(repo, "b.txt"), "two\n")
    git(["add", "b.txt"], repo, env)
    git(["commit", "-q", "-m", "Add b"], repo, env)
    write(os.path.join(home, ".claude.json"), json.dumps({"projects": {repo: {}}}))
    return env, {"home": home, "data": data, "claude": claude, "repo": repo,
                 "local_settings": os.path.join(repo, ".claude", "settings.local.json")}


def run(script, args, env, check=True):
    p = subprocess.run([sys.executable, "-I", os.path.join(SCRIPTS, script)] + list(args), env=env,
                       capture_output=True, text=True, timeout=120)
    if check and p.returncode != 0:
        raise AssertionError("%s %s failed (%d):\n%s\n%s" % (script, args, p.returncode, p.stdout, p.stderr))
    return p
