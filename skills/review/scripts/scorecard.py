#!/usr/bin/env python3
"""Scorecard for one Toolbox Review run: every charter budget, its value today, the threshold, and the trend.

Reads the run folder (summary.json, inventory.json, health.json, security.json, forge.json, git.json), the ledger and
the budgets (plugin defaults overlaid with ~/.claude/toolbox-review/budgets.json). Writes scorecard.json into the run
folder and prints a Markdown table. Labels are English; the review translates them into the user's language when it
writes the report. The trend compares with the most recent earlier run that has a scorecard.

Usage: scorecard.py RUN_DIR
"""
import datetime as dt
import glob
import json
import os
import sys
import importlib.util as _ilu

_spec = _ilu.spec_from_file_location("tr_config", os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.py"))
CFG = _ilu.module_from_spec(_spec)
_spec.loader.exec_module(CFG)

DATA = CFG.data_dir()
BUILTIN_AGENTS = ("general-purpose", "Explore", "Plan", "fork", "claude")


def load(path, default=None):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {} if default is None else default


def row(rows, key, label, value, limit, ok, note=""):
    rows.append({"key": key, "label": label, "value": value, "limit": limit,
                 "status": "OK" if ok else "OVER", "note": note})


def info(rows, key, label, value, note=""):
    rows.append({"key": key, "label": label, "value": value, "limit": "trend", "status": "INFO", "note": note})


def run_age_days(run_dir):
    try:
        return (dt.datetime.now() - dt.datetime.strptime(os.path.basename(run_dir)[:8], "%Y%m%d")).days
    except ValueError:
        return 0


def previous(run_dir):
    runs = sorted(d for d in glob.glob(os.path.join(DATA, "runs", "*")) if d < run_dir
                  and os.path.exists(os.path.join(d, "scorecard.json")))
    return load(os.path.join(runs[-1], "scorecard.json")) if runs else {}


def main():
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    run_dir = os.path.abspath(os.path.expanduser(sys.argv[1]))
    b = CFG.budgets()
    summary, inv = load(os.path.join(run_dir, "summary.json")), load(os.path.join(run_dir, "inventory.json"))
    health, sec = load(os.path.join(run_dir, "health.json")), load(os.path.join(run_dir, "security.json"))
    ledger = load(os.path.join(DATA, "ledger.json"), {"recs": []})
    rows = []

    al = (inv.get("always_loaded") or {}).get("approx_tokens")
    if al is not None:
        lim = b["always_loaded_tokens"]
        row(rows, "always_loaded_tokens", "Loaded in every session (tokens)", al,
            "<= %d (target %d)" % (lim["max"], lim["target"]), al <= lim["max"],
            "%d above target" % (al - lim["target"]) if al > lim["target"] else "on target")

    lim_lines, exc = b["skill_md_lines"]["max"], b["skill_md_lines"].get("exceptions", {})
    big = [s for s in inv.get("personal_skills", []) if s.get("lines", 0) > exc.get(s["name"], lim_lines)]
    row(rows, "skills_over_size", "SKILL.md over its line budget", len(big),
        "0 (<= %d lines%s)" % (lim_lines, "; exceptions: " + ", ".join("%s %d" % kv for kv in exc.items()) if exc else ""),
        not big, ", ".join("%s %d" % (s["name"], s["lines"]) for s in sorted(big, key=lambda s: -s["lines"]))[:200])

    mem = [m for m in inv.get("memory", []) if m.get("index_lines", 0) > b["memory_index_lines"]["max"]]
    largest = max(inv.get("memory", []) or [{}], key=lambda m: m.get("index_lines", 0))
    row(rows, "memory_over_size", "Memory index over its line budget", len(mem),
        "0 (<= %d lines)" % b["memory_index_lines"]["max"], not mem,
        "largest %s lines" % largest.get("index_lines", 0) if largest else "")

    hc, sc = (health.get("counts") or {}), (sec.get("counts") or {})
    if hc:
        row(rows, "health_fail", "Health: broken or at risk", hc.get("FAIL", 0), "<= %d FAIL" % b["health"]["max_fail"],
            hc.get("FAIL", 0) <= b["health"]["max_fail"], "%d warnings" % hc.get("WARN", 0))
    if sc:
        row(rows, "security_fail", "Security: findings", sc.get("FAIL", 0), "<= %d FAIL" % b["security"]["max_fail"],
            sc.get("FAIL", 0) <= b["security"]["max_fail"], "%d warnings" % sc.get("WARN", 0))

    skills = summary.get("skills", {})
    eps = sum(v.get("episodes", 0) for v in skills.values())
    if eps:
        corr = sum((v.get("signal_classes") or {}).get("correction", 0) for v in skills.values())
        info(rows, "corrections_per_episode", "Corrections per skill run", round(corr / float(eps), 2), "%d runs" % eps)
        # The run's own cost plus the subagents it started; older runs have only the first.
        costly = sorted(((k, v.get("first_turn_total_cost_median", v.get("first_turn_cost_median", 0)))
                         for k, v in skills.items() if v.get("episodes")), key=lambda kv: -kv[1])[:3]
        info(rows, "costliest_skills", "Costliest skills (run median incl. subagents, thousands)",
             costly[0][1] // 1000 if costly else 0,
             ", ".join("%s %dk" % (k, c // 1000) for k, c in costly))
        unapproved = sum((v.get("actions") or {}).get("writes_without_approval", 0) for v in skills.values())
        info(rows, "writes_without_approval", "Outward writes without an approval before them", unapproved,
             "pushes and forge writes")
    agents = (summary.get("usage") or {}).get("agents", {})
    total = sum(a.get("cost_total", 0) for a in agents.values())
    if total:
        builtin = sum(a.get("cost_total", 0) for k, a in agents.items() if k in BUILTIN_AGENTS)
        info(rows, "subagent_cost", "Subagent cost (thousands)", total // 1000,
             "built-in agents %d%%" % round(100.0 * builtin / total))

    fg = load(os.path.join(run_dir, "forge.json")) or load(os.path.join(run_dir, "gitlab.json"))
    gi = load(os.path.join(run_dir, "git.json"))
    if fg.get("total"):
        t = fg["total"]
        info(rows, "mr_review_threads", "Review threads per MR/PR (median)", t.get("median_review_threads"),
             "%s MR/PR · merge median %s h · with a failed pipeline %s" % (
                 t.get("mrs"), t.get("median_hours_to_merge"), t.get("mrs_with_failed_pipeline")))
    if gi.get("total"):
        t = gi["total"]
        info(rows, "post_merge_fix", "MR/PR that came back after merge", t.get("mrs_with_post_merge_fix", 0),
             "of %s · AI share commits %s / lines %s" % (
                 t.get("mrs_checked_post_merge", 0), t.get("ai_share_commits"), t.get("ai_share_lines")))

    recs = ledger.get("recs", [])
    by = {}
    for r in recs:
        by[r["status"]] = by.get(r["status"], 0) + 1
    decided = sum(by.get(k, 0) for k in ("approved", "applied", "verified", "regressed", "rejected", "deferred"))
    accepted = sum(by.get(k, 0) for k in ("approved", "applied", "verified", "regressed"))
    info(rows, "ledger", "Recommendation ledger", len(recs), ", ".join("%s %d" % kv for kv in sorted(by.items()))
         + ("; acceptance %d%%" % round(100.0 * accepted / decided) if decided else ""))
    applied = [r for r in recs if r["status"] in ("applied", "verified", "regressed")]
    plus = len([r for r in applied if r.get("complexity") == "+"])
    minus = len([r for r in applied if r.get("complexity") == "-"])
    ratio = b["complexity"]["additions_per_simplification"]
    row(rows, "complexity", "Additions / simplifications", "%d / %d" % (plus, minus),
        ">= 1 simplification per %d additions" % ratio, plus == 0 or plus <= ratio * minus + (ratio - 1),
        "applied recommendations")
    regressed = by.get("regressed", 0)
    row(rows, "regressions", "Applied changes that regressed", regressed, "0", regressed == 0)

    old = [d for d in glob.glob(os.path.join(DATA, "runs", "*"))
           if os.path.isdir(os.path.join(d, "episodes")) and run_age_days(d) > b["retention_days"]]
    row(rows, "retention", "Runs past the retention period", len(old), "0 (<= %d days)" % b["retention_days"], not old)

    prev = previous(run_dir)
    if prev:
        before = {r["key"]: r["value"] for r in prev.get("rows", [])}
        for r in rows:
            v0 = before.get(r["key"])
            if isinstance(v0, (int, float)) and isinstance(r["value"], (int, float)) and v0 != r["value"]:
                r["trend"] = "up" if r["value"] > v0 else "down"
                r["previous"] = v0
    out = {"run": os.path.basename(run_dir), "previous_run": prev.get("run") if prev else None, "rows": rows}
    with open(os.path.join(run_dir, "scorecard.json"), "w", encoding="utf-8") as fh:
        json.dump(out, fh, ensure_ascii=False, indent=2)
    print("| Measure | Today | Threshold | Status | Note |")
    print("|---|---|---|---|---|")
    for r in rows:
        t = (" %s (was %s)" % (r["trend"], r["previous"])) if r.get("trend") else ""
        print("| %s | %s%s | %s | %s | %s |" % (r["label"], r["value"], t, r["limit"],
                                               {"OK": "ok", "OVER": "over", "INFO": "-"}[r["status"]], r["note"]))


if __name__ == "__main__":
    main()
