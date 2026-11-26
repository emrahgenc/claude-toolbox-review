#!/usr/bin/env python3
"""Named, comparable metrics over one Toolbox Review run folder, so every applied recommendation can be checked
against its own promise instead of a generic number.

  metrics.py RUN_DIR NAME [--after ISO] [--before ISO]     prints: value n

Episode metrics can be limited to episodes that started after/before a moment (e.g. before and after a change was
applied). n is the sample size behind the value; with too few episodes a verdict should wait.

Names:
  skill:<skill>:pushes_per_episode            git push commands per episode (mean)
  skill:<skill>:writes_without_approval       outward writes with no approval or menu answer before them (sum)
  skill:<skill>:corrections_per_episode       typed corrections, interrupts and denials per episode (mean); only
                                              those within the first followup_window replies count (later ones
                                              are often other work in the same session)
  skill:<skill>:first_turn_cost_median        the skill's own run cost, main thread only (cost_index, median)
  skill:<skill>:first_turn_total_cost_median  the same plus the subagents the run started (median); use this one to
                                              judge "move work into a subagent", which moves cost rather than
                                              removing it. Episodes collected before subagent costs were recorded
                                              are left out (n says how many count)
  skill:<skill>:subagent_cost_median          cost of the subagents an episode started (median, same caveat)
  skill:<skill>:largest_result_median         the largest single tool result in an episode, chars (median)
  agent:<type>:failed                         failed delegations to this agent type in the run's period
  always_loaded_tokens | always_loaded:<part> every-session load (inventory.json), tokens or a part's chars
  security:prod_guard                         1 if production commands are guarded by a PreToolUse hook, else 0
  security:broad_permission_files             permission files with at least one broad allow rule
  forge:median_review_threads | git:post_merge_fix_mrs
"""
import argparse
import datetime as dt
import json
import os
import statistics
import sys
import importlib.util as _ilu

_spec = _ilu.spec_from_file_location("tr_config", os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.py"))
CFG = _ilu.module_from_spec(_spec)
_spec.loader.exec_module(CFG)


def load(path, default=None):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return default


def ts(s):
    return dt.datetime.fromisoformat(s.replace("Z", "+00:00")) if s else None


def episodes(run_dir, skill, after=None, before=None):
    eps = load(os.path.join(run_dir, "episodes", CFG.safe_name(skill) + ".json"), []) or []
    out = []
    for e in eps:
        t = ts(e.get("start"))
        if not t or (after and t < after) or (before and t >= before):
            continue
        out.append(e)
    return out


WINDOW = int(CFG.config().get("followup_window", 3))

# How each metric is counted today. A ledger target remembers the rule its baseline was counted with; when the rule
# changes, the baseline is counted again from its run (or flagged), so a new rule never looks like an improvement.
RULES = {"corrections_per_episode": "followup-window-%d" % WINDOW,
         "first_turn_total_cost_median": "subagents-1", "subagent_cost_median": "subagents-1"}


def rule(name):
    return RULES.get(name.split(":")[-1]) if name.startswith("skill:") else None


def late(g):
    # Episode files from before the follow-up window carry no "late" mark: judge them by n, the same way.
    return g.get("late", (g.get("n") or 0) > WINDOW)


def corrections(e):
    return len([g for g in e.get("signals", []) if not late(g) and (g.get("class") == "correction"
                or (not g.get("class") and g.get("kind") in ("user", "midturn", "interrupt", "denial")))])


def subagent_eps(eps):
    """Episodes collected after subagent costs were recorded (older ones would look free)."""
    return [e for e in eps if "first_turn_total" in (e.get("tokens") or {})]


def compute(run_dir, name, after=None, before=None):
    """Return (value, n). value is None when the run has nothing to measure."""
    parts = name.split(":")
    if parts[0] == "skill" and len(parts) >= 3:   # plugin skills contain ':' (plugin:skill)
        eps = episodes(run_dir, ":".join(parts[1:-1]), after, before)
        n = len(eps)
        if not eps:
            return None, 0
        what = parts[-1]
        if what == "pushes_per_episode":
            return round(sum((e.get("actions") or {}).get("git_push", 0) for e in eps) / float(n), 2), n
        if what == "writes_without_approval":
            return sum((e.get("actions") or {}).get("writes_without_approval", 0) for e in eps), n
        if what == "corrections_per_episode":
            return round(sum(corrections(e) for e in eps) / float(n), 2), n
        if what == "first_turn_cost_median":
            return int(statistics.median(e["tokens"]["first_turn"]["cost_index"] for e in eps)), n
        if what in ("first_turn_total_cost_median", "subagent_cost_median"):
            sub = subagent_eps(eps)
            if not sub:
                return None, 0
            if what == "first_turn_total_cost_median":
                return int(statistics.median(e["tokens"]["first_turn_total"] for e in sub)), len(sub)
            return int(statistics.median(e["tokens"]["subagents"]["episode"]["cost_index"] for e in sub)), len(sub)
        if what == "largest_result_median":
            return int(statistics.median(max([r["chars"] for r in e.get("largest_results", [])] or [0]) for e in eps)), n
        raise SystemExit("unknown skill metric: " + what)
    if parts[0] == "agent" and len(parts) >= 3:   # agent types may contain ':' (plugin:agent)
        a = ((load(os.path.join(run_dir, "summary.json"), {}) or {}).get("usage", {}).get("agents", {})).get(":".join(parts[1:-1]))
        if not a:
            return None, 0
        return a.get(parts[-1], 0), a.get("calls", 0)
    if name == "always_loaded_tokens" or parts[0] == "always_loaded":
        al = (load(os.path.join(run_dir, "inventory.json"), {}) or {}).get("always_loaded")
        if not al:
            return None, 0
        return (al.get("approx_tokens") if name == "always_loaded_tokens" else al.get(parts[1])), 1
    if name == "security:prod_guard":
        sec = load(os.path.join(run_dir, "security.json"), {}) or {}
        rows = [c for c in sec.get("checks", []) if c.get("area") == "prod-guards"]
        if not rows:
            return None, 0
        return (1 if all(c.get("status") == "OK" for c in rows) else 0), 1
    if name == "security:broad_permission_files":
        sec = load(os.path.join(run_dir, "security.json"), {}) or {}
        if not sec.get("checks"):
            return None, 0
        # personal files only: team settings.json is the team's (charter decision: read only)
        rows = [c for c in sec.get("checks", []) if c.get("area") == "permissions" and "broad:" in c.get("reason", "")
                and (c.get("name", "").startswith("user ") or "settings.local.json" in c.get("name", ""))]
        return len(rows), 1
    if name in ("forge:median_review_threads", "gitlab:median_review_threads"):
        f = os.path.join(run_dir, "forge.json")
        t = (load(f if os.path.exists(f) else os.path.join(run_dir, "gitlab.json"), {}) or {}).get("total") or {}
        return t.get("median_review_threads"), t.get("mrs", 0)
    if name == "git:post_merge_fix_mrs":
        t = (load(os.path.join(run_dir, "git.json"), {}) or {}).get("total") or {}
        return t.get("mrs_with_post_merge_fix"), t.get("mrs_checked_post_merge", 0)
    raise SystemExit("unknown metric: " + name)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("run_dir")
    ap.add_argument("name")
    ap.add_argument("--after")
    ap.add_argument("--before")
    a = ap.parse_args()
    v, n = compute(os.path.expanduser(a.run_dir), a.name, ts(a.after), ts(a.before))
    print("%s %s" % (v, n))


if __name__ == "__main__":
    main()
