#!/usr/bin/env python3
"""Recommendation ledger for Toolbox Review: what was proposed, decided, applied, and whether it held.

The ledger is what makes the review a loop instead of a weekly pile of the same advice:
  * rejected ideas are not proposed again without new evidence,
  * every applied change keeps a baseline (the component's metrics in the run that proposed it),
  * `check` compares a later run against that baseline and flags improvements and regressions.

File: ~/.claude/toolbox-review/ledger.json (outside the skills git repo).

  ledger.py add --run RUN --file recs.json        recs: [{component, title, kind, target, impact, effort, door,
                                                   complexity, plain_title, plain_effect, plain_cost, plain_group,
                                                   recommended, needs_choice}]
  ledger.py set ID STATUS [--reason T] [--commit SHA]
  ledger.py edit ID key=value [key=value ...]      set any field (JSON values allowed: true, ["a","b"])
  ledger.py annotate --file plain.json             {ID: {field: value}} for many recs at once
  ledger.py show [--component C] [--status S] [--json]
  ledger.py menu                                   open recs in decision-menu order, plain fields only
  ledger.py check RUN                              compare applied recs with RUN (own target metric if set)
  ledger.py target ID METRIC --goal G --baseline-run RUN [--direction down|up] [--min-n 3]
                                                   give a rec its own measure (metrics.py names), baseline before
                                                   the change
  ledger.py meta KEY [VALUE]                       small run facts (last_scout_version, last_full_sweep ...)

STATUS: proposed | approved | applied | rejected | deferred | verified | regressed

The plain_* fields are what the person sees in the decision menu (charter value V1): plain_title is the outcome
in everyday words, plain_effect what changes for them, plain_cost time and how easy it is to undo. IDs, domain
codes and tool names stay in the technical fields and appear only in the menu's preview.
"""
import argparse
import datetime as dt
import json
import os
import sys

import importlib.util as _ilu

_spec = _ilu.spec_from_file_location("tr_config", os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.py"))
CFG = _ilu.module_from_spec(_spec)
_spec.loader.exec_module(CFG)


DATA = CFG.data_dir()
LEDGER = os.path.join(DATA, "ledger.json")
STATUSES = ("proposed", "approved", "applied", "rejected", "deferred", "verified", "regressed")


def now():
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def load():
    try:
        with open(LEDGER, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {"recs": [], "meta": {}}


def lock():
    """Several sessions (a decision round, a scheduled run, an architecture session) may write at once; a write
    reads, changes and saves the whole file, so writers take an exclusive lock for the whole command."""
    import fcntl
    os.makedirs(DATA, mode=0o700, exist_ok=True)
    fh = open(LEDGER + ".lock", "w")
    fcntl.flock(fh, fcntl.LOCK_EX)
    return fh   # released when the process exits


def save(db):
    os.makedirs(DATA, mode=0o700, exist_ok=True)
    tmp = "%s.tmp.%d" % (LEDGER, os.getpid())
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(db, fh, ensure_ascii=False, indent=2)
    os.replace(tmp, LEDGER)


def run_summary(run):
    path = os.path.join(DATA, "runs", run, "summary.json")
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        sys.exit("no summary for run %s" % run)


def metrics(summary, component):
    """Comparable numbers for one component in one run (skills and agents by the name they are invoked with)."""
    name = component.lstrip("/")
    s = summary.get("skills", {}).get(name)
    if s is not None:
        eps = s.get("episodes", 0)
        sig = s.get("signals", {})
        bad = sig.get("user", 0) + sig.get("midturn", 0) + sig.get("interrupt", 0) + sig.get("denial", 0)
        cpe = s.get("corrections_per_episode")   # newer runs: only real corrections, approvals excluded
        return {"episodes": eps, "corrections_per_episode": cpe if cpe is not None else (round(bad / eps, 2) if eps else None),
                "first_turn_cost_median": s.get("first_turn_cost_median"),
                "first_turn_calls_median": s.get("first_turn_calls_median")}
    a = summary.get("usage", {}).get("agents", {}).get(component)
    if a is not None:
        return {"episodes": a.get("runs", 0), "failed": a.get("failed", 0),
                "cost_median": a.get("cost_median"), "calls_median": a.get("calls_median")}
    return {}


def find(db, rid):
    for r in db["recs"]:
        if r["id"] == rid:
            return r
    sys.exit("no rec %s" % rid)


def cmd_add(a):
    db = load()
    recs = json.load(open(a.file, encoding="utf-8"))
    summary = run_summary(a.run)
    day = a.run[2:8]
    n = len([r for r in db["recs"] if r["id"].startswith("T" + day)])
    out = []
    for r in recs:
        n += 1
        r = dict(r)
        r["id"] = r.get("id") or "T%s-%02d" % (day, n)
        r.update(status="proposed", run=a.run, proposed_at=now(),
                 baseline=metrics(summary, r.get("component", "")), history=[])
        db["recs"].append(r)
        out.append(r["id"])
    save(db)
    print(" ".join(out))


def cmd_set(a):
    if a.status not in STATUSES:
        sys.exit("status must be one of: " + ", ".join(STATUSES))
    db = load()
    r = find(db, a.id)
    r["history"].append({"at": now(), "from": r["status"], "to": a.status, "reason": a.reason or ""})
    r["status"] = a.status
    if a.reason:
        r["reason"] = a.reason
    if a.commit:
        r["commit"] = a.commit
    if a.status == "applied":
        r["applied_at"] = now()
    save(db)
    print("%s -> %s" % (r["id"], a.status))


def parse_value(v):
    try:
        return json.loads(v)
    except ValueError:
        return v


def cmd_edit(a):
    db = load()
    r = find(db, a.id)
    for pair in a.pairs:
        if "=" not in pair:
            sys.exit("expected key=value, got %s" % pair)
        k, v = pair.split("=", 1)
        if k in ("id", "status", "history", "baseline"):
            sys.exit("%s is managed by add/set" % k)
        r[k] = parse_value(v)
    save(db)
    print("%s edited: %s" % (r["id"], ", ".join(p.split("=", 1)[0] for p in a.pairs)))


def cmd_annotate(a):
    db = load()
    data = json.load(open(a.file, encoding="utf-8"))
    for rid, fields in data.items():
        r = find(db, rid)
        for k, v in fields.items():
            if k in ("id", "status", "history", "baseline"):
                sys.exit("%s is managed by add/set" % k)
            r[k] = v
    save(db)
    print("annotated %d rec(s)" % len(data))


GROUP_ORDER = ["security", "lighter", "skill", "tidy", "measure"]
IMPACT_ORDER = {"high": 0, "medium-high": 1, "medium": 2, "low": 3}
# Ledgers written before 0.1 used Turkish codes; read them as the canonical ones.
LEGACY = {"güvenlik": "security", "hafiflet": "lighter", "düzen": "tidy", "ölçüm": "measure",
          "yüksek": "high", "orta-yüksek": "medium-high", "orta": "medium", "düşük": "low"}
canon = lambda v: LEGACY.get(v, v)


def cmd_menu(a):
    """Open recommendations as the decision menu will show them: grouped, recommended first, plain fields."""
    db = load()
    open_recs = [r for r in db["recs"] if r["status"] == "proposed" and not r.get("superseded_by")]
    missing = [r["id"] for r in open_recs if not (r.get("plain_title") and r.get("plain_effect") and r.get("plain_group"))]
    key = lambda r: (GROUP_ORDER.index(canon(r["plain_group"])) if canon(r.get("plain_group")) in GROUP_ORDER else 99,
                     0 if r.get("needs_choice") else 1, 0 if r.get("recommended") else 1,
                     IMPACT_ORDER.get(canon(r.get("impact")), 9))
    for r in sorted(open_recs, key=key):
        print("%-11s [%s]%s %s" % (r["id"], r.get("plain_group", "?"), " *" if r.get("recommended") else "  ",
                                   r.get("plain_title") or "(plain_title missing) " + r.get("title", "")))
        if r.get("plain_effect"):
            print("            %s %s" % (r["plain_effect"], r.get("plain_cost", "")))
        if r.get("needs_choice"):
            print("            choice: %s" % " | ".join(r["needs_choice"]))
    held = len([r for r in db["recs"] if r["status"] == "deferred"])
    print("\nopen %d · deferred %d (not shown) · superseded %d" % (
        len(open_recs), held, len([r for r in db["recs"] if r.get("superseded_by") and r["status"] == "proposed"])))
    if missing:
        print("plain fields missing: %s" % ", ".join(missing))


def cmd_show(a):
    db = load()
    recs = [r for r in db["recs"] if (not a.component or r.get("component") == a.component)
            and (not a.status or r["status"] == a.status)]
    if a.json:
        print(json.dumps(recs, ensure_ascii=False, indent=2))
        return
    for r in recs:
        print("%-11s %-10s %-26s %-10s %s%s" % (r["id"], r["status"], r.get("component", ""), r.get("kind", ""),
                                               r.get("title", ""), (" — " + r["reason"]) if r.get("reason") else ""))
    if not recs:
        print("(no recs)")


def metrics_module():
    import importlib.util
    here = os.path.dirname(os.path.abspath(__file__))
    spec = importlib.util.spec_from_file_location("tr_metrics", os.path.join(here, "metrics.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def parse_iso(s):
    return dt.datetime.fromisoformat(s.replace("Z", "+00:00")) if s else None


def cmd_target(a):
    """Give a rec its own measure: what number its promise should move, from what baseline, to what goal."""
    db = load()
    r = find(db, a.id)
    m = metrics_module()
    run_dir = os.path.join(DATA, "runs", a.baseline_run)
    before = parse_iso(a.before or r.get("applied_at"))
    base, n = m.compute(run_dir, a.name, before=before)
    r["target_metric"] = {"name": a.name, "baseline": base, "baseline_n": n, "baseline_run": a.baseline_run,
                          "goal": parse_value(a.goal), "direction": a.direction, "min_n": a.min_n}
    save(db)
    print("%s target %s: baseline %s (n=%s) -> goal %s (%s)" % (r["id"], a.name, base, n, a.goal, a.direction))


def target_verdict(r, run_dir, m):
    tm = r["target_metric"]
    after = parse_iso(r.get("applied_at"))
    cur, n = m.compute(run_dir, tm["name"], after=after if tm["name"].startswith("skill:") else None)
    base, goal = tm.get("baseline"), tm.get("goal")
    if cur is None and tm["name"].startswith("skill:"):
        return "target %s: not enough runs since the change yet (n=0, needs %s)" % (tm["name"], tm.get("min_n", 3))
    if cur is None:
        return "target %s: no data for this measure in this run" % tm["name"]
    if tm["name"].startswith("skill:") and n < tm.get("min_n", 3):
        return "target %s: not enough runs since the change yet (n=%s, needs %s)" % (tm["name"], n, tm.get("min_n", 3))
    down = tm.get("direction", "down") == "down"
    reached = cur <= goal if down else cur >= goal
    worse = base is not None and ((cur > base * 1.2) if down else (cur < base * 0.8)) and cur != base
    word = "REACHED" if reached else ("REGRESSED" if worse else "not yet")
    return "target %s: %s -> %s (goal %s, n=%s) %s" % (tm["name"], base, cur, goal, n, word)


def cmd_check(a):
    db = load()
    summary = run_summary(a.run)
    run_dir = os.path.join(DATA, "runs", a.run)
    m = metrics_module()
    rows = 0
    for r in db["recs"]:
        if r["status"] not in ("applied", "verified", "regressed"):
            continue
        if r.get("target_metric"):
            rows += 1
            print("%-11s %-26s %-8s %s" % (r["id"], r.get("component", "")[:26], r["status"], target_verdict(r, run_dir, m)))
            continue
        if r.get("applied_at") and summary.get("since") and summary["since"] < r["applied_at"][:19]:
            verdict = "partial period (run starts before the change)"
        else:
            verdict = ""
        cur, base = metrics(summary, r.get("component", "")), r.get("baseline", {})
        if not cur or not cur.get("episodes"):
            verdict = verdict or "no use in this period"
        else:
            notes = []
            for k in ("first_turn_cost_median", "cost_median"):
                if base.get(k) and cur.get(k) is not None:
                    d = (cur[k] - base[k]) / float(base[k])
                    notes.append("%s %+d%%" % (k.split("_median")[0], round(d * 100)))
                    if d > 0.2:
                        notes.append("COST UP")
            b, c = base.get("corrections_per_episode"), cur.get("corrections_per_episode")
            if b is not None and c is not None:
                notes.append("corrections/ep %.2f -> %.2f" % (b, c))
                if c > b * 1.5 and c - b >= 0.5:
                    notes.append("MORE CORRECTIONS")
            verdict = (verdict + "; " if verdict else "") + ", ".join(notes)
        rows += 1
        print("%-11s %-26s %-8s %s" % (r["id"], r.get("component", ""), r["status"], verdict))
    if not rows:
        print("(no applied recs)")


def cmd_meta(a):
    db = load()
    if a.value is None:
        print(db["meta"].get(a.key, ""))
        return
    db["meta"][a.key] = a.value
    save(db)
    print("%s = %s" % (a.key, a.value))


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd")
    p = sub.add_parser("add"); p.add_argument("--run", required=True); p.add_argument("--file", required=True)
    p = sub.add_parser("set"); p.add_argument("id"); p.add_argument("status")
    p.add_argument("--reason"); p.add_argument("--commit")
    p = sub.add_parser("show"); p.add_argument("--component"); p.add_argument("--status")
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("check"); p.add_argument("run")
    p = sub.add_parser("target"); p.add_argument("id"); p.add_argument("name"); p.add_argument("--goal", required=True)
    p.add_argument("--direction", choices=("down", "up"), default="down"); p.add_argument("--baseline-run", required=True)
    p.add_argument("--before"); p.add_argument("--min-n", type=int, default=3)
    p = sub.add_parser("meta"); p.add_argument("key"); p.add_argument("value", nargs="?")
    p = sub.add_parser("edit"); p.add_argument("id"); p.add_argument("pairs", nargs="+")
    p = sub.add_parser("annotate"); p.add_argument("--file", required=True)
    sub.add_parser("menu")
    a = ap.parse_args()
    if not a.cmd:
        ap.print_help()
        return
    if a.cmd in ("add", "set", "edit", "annotate", "target") or (a.cmd == "meta" and a.value is not None):
        held = lock()  # noqa: F841 (kept open until exit)
    {"add": cmd_add, "set": cmd_set, "show": cmd_show, "check": cmd_check, "meta": cmd_meta,
     "edit": cmd_edit, "annotate": cmd_annotate, "menu": cmd_menu, "target": cmd_target}[a.cmd](a)


if __name__ == "__main__":
    main()
