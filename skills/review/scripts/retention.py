#!/usr/bin/env python3
"""Retention for Toolbox Review data (charter section 7): episode files older than budgets retention_days.

Episodes quote real work, so they are kept only as long as the charter allows. What goes: each old run's
episodes/ folder and its inventory/health/security snapshots. What stays: summary.json, scorecard.json, recs.json
and report.md (small, no transcript text beyond what the report already summarised), and the ledger.

Dry run by default: it lists what would be deleted. Deletion is permanent, so the review runs --apply only
after the user approves it in the decision step.

Usage: retention.py [--apply]
"""
import argparse
import datetime as dt
import glob
import json
import os
import shutil

import importlib.util as _ilu

_spec = _ilu.spec_from_file_location("tr_config", os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.py"))
CFG = _ilu.module_from_spec(_spec)
_spec.loader.exec_module(CFG)


HOME = os.path.expanduser("~")
DATA = CFG.data_dir()
DROP_FILES = ("inventory.json", "health.json", "security.json")


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args()
    try:
        days = CFG.budgets()["retention_days"]
    except (OSError, ValueError, KeyError):
        days = 90
    cutoff = dt.datetime.now() - dt.timedelta(days=days)
    victims = []
    for d in sorted(glob.glob(os.path.join(DATA, "runs", "*"))):
        try:
            when = dt.datetime.strptime(os.path.basename(d)[:8], "%Y%m%d")
        except ValueError:
            continue
        if when >= cutoff:
            continue
        for p in [os.path.join(d, "episodes")] + [os.path.join(d, f) for f in DROP_FILES]:
            if os.path.exists(p):
                size = sum(os.path.getsize(os.path.join(r, f)) for r, _, fs in os.walk(p) for f in fs) \
                    if os.path.isdir(p) else os.path.getsize(p)
                victims.append((p, size))
    if not victims:
        print("retention: nothing older than %d days" % days)
        return
    print("retention: %d item(s), %d KB older than %d days%s" % (
        len(victims), sum(s for _, s in victims) // 1024, days, "" if a.apply else " (dry run, nothing deleted)"))
    for p, s in victims:
        print("  %s  %d KB" % (p.replace(HOME, "~"), s // 1024))
        if a.apply:
            shutil.rmtree(p) if os.path.isdir(p) else os.remove(p)
    if a.apply:
        print("deleted.")


if __name__ == "__main__":
    main()
