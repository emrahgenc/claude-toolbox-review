"""Settings shared by every Toolbox Review script.

Defaults ship with the plugin (../defaults/config.json, ../defaults/budgets.json). The user's own copies live in the
data folder (~/.claude/toolbox-review/config.json, budgets.json, CHARTER.md) and override the defaults key by key, so
the plugin can be updated without touching anyone's settings.

Scripts run with `python3 -I`, which does not put their own folder on sys.path; they load this module by path:

    import importlib.util as _ilu
    _spec = _ilu.spec_from_file_location("tr_config", os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.py"))
    CFG = _ilu.module_from_spec(_spec); _spec.loader.exec_module(CFG)
"""
import glob
import json
import os

HOME = os.path.expanduser("~")
CLAUDE = os.path.join(HOME, ".claude")
PLUGIN_SKILL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))   # .../skills/review
DEFAULTS = os.path.join(PLUGIN_SKILL_DIR, "defaults")


def _load(path):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def _merge(base, over):
    out = dict(base)
    for k, v in (over or {}).items():
        if k.startswith("_"):
            continue
        out[k] = _merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


def data_dir():
    """The user's data folder. TOOLBOX_REVIEW_DATA overrides it (tests use this)."""
    d = os.environ.get("TOOLBOX_REVIEW_DATA") or os.path.join(CLAUDE, "toolbox-review")
    return os.path.expanduser(d)


def config():
    return _merge(_load(os.path.join(DEFAULTS, "config.json")), _load(os.path.join(data_dir(), "config.json")))


def budgets():
    return _merge(_load(os.path.join(DEFAULTS, "budgets.json")), _load(os.path.join(data_dir(), "budgets.json")))


def charter_path():
    p = os.path.join(data_dir(), "CHARTER.md")
    return p if os.path.exists(p) else os.path.join(PLUGIN_SKILL_DIR, "templates", "CHARTER.md")


def expand(p):
    return os.path.expanduser(p) if p else p


def _plugin_name(folder):
    meta = _load(os.path.join(folder, ".claude-plugin", "plugin.json"))
    return meta.get("name") or os.path.basename(folder)


def personal_plugin_dirs(cfg=None):
    """Plugins the user owns and maintains: skills-dir plugins under ~/.claude/skills/<name>/.claude-plugin
    (auto) plus any folders listed in maintained.plugin_dirs. Organisation-synced plugins are not personal."""
    cfg = cfg or config()
    m = cfg.get("maintained", {})
    dirs = []
    if m.get("auto_discover", True):
        for d in sorted(glob.glob(os.path.join(CLAUDE, "skills", "*"))):
            if os.path.basename(d) in ("synced",) or os.path.basename(d).startswith("."):
                continue
            if os.path.exists(os.path.join(d, ".claude-plugin", "plugin.json")):
                dirs.append(d)
    dirs += [expand(d) for d in m.get("plugin_dirs", []) if os.path.isdir(expand(d))]
    out, seen = [], set()
    for d in dirs:
        r = os.path.realpath(d)
        if r not in seen:
            seen.add(r)
            out.append(d)
    return out


def personal_skills(cfg=None):
    """{invocation name: SKILL.md path} for every skill the user maintains:
    ~/.claude/skills/<name>/SKILL.md -> "<name>"; <personal plugin>/skills/<name>/SKILL.md -> "<plugin>:<name>";
    maintained.extra_skill_dirs/<name>/SKILL.md -> "<name>". maintained.exclude drops names."""
    cfg = cfg or config()
    m = cfg.get("maintained", {})
    found = {}
    if m.get("auto_discover", True):
        for p in sorted(glob.glob(os.path.join(CLAUDE, "skills", "*", "SKILL.md"))):
            found[os.path.basename(os.path.dirname(p))] = p
    for d in personal_plugin_dirs(cfg):
        name = _plugin_name(d)
        for p in sorted(glob.glob(os.path.join(d, "skills", "*", "SKILL.md"))):
            found["%s:%s" % (name, os.path.basename(os.path.dirname(p)))] = p
    for d in m.get("extra_skill_dirs", []):
        for p in sorted(glob.glob(os.path.join(expand(d), "*", "SKILL.md"))):
            found[os.path.basename(os.path.dirname(p))] = p
    for x in m.get("exclude", []):
        found.pop(x, None)
    return found


def personal_agents(cfg=None):
    """{agent type: path}: ~/.claude/agents/<name>.md -> "<name>", <personal plugin>/agents/<name>.md -> "<plugin>:<name>"."""
    cfg = cfg or config()
    found = {}
    for p in sorted(glob.glob(os.path.join(CLAUDE, "agents", "*.md"))):
        found[os.path.basename(p)[:-3]] = p
    for d in personal_plugin_dirs(cfg):
        name = _plugin_name(d)
        for p in sorted(glob.glob(os.path.join(d, "agents", "*.md"))):
            found["%s:%s" % (name, os.path.basename(p)[:-3])] = p
    return found


def safe_name(skill):
    """A skill name usable as a file name (plugin:skill -> plugin__skill)."""
    return skill.replace(":", "__").replace("/", "_")
