#!/usr/bin/env python3
"""Exam set for Toolbox Review (charter D13): real past situations where a skill went wrong or
had to be right, kept so a skill change can be tried on them before and after it is applied.

File: ~/.claude/toolbox-review/exams.json (outside the skills repo; holds MR numbers and commit SHAs).
Each exam: id, skill, title, source (where it came from), input, expected (plain words), criteria (checkable lines),
runnable (true only when it can run without writing anywhere), how_to_run (the command the user starts), runs[].

  exams.py list [--skill S]                 one line per exam with its last result
  exams.py show ID                          everything about one exam, including how to run it
  exams.py for-skill S                      the runnable exams of a skill, as commands (used before/after a change)
  exams.py record ID pass|fail|partial --commit SHA [--note TEXT]
  exams.py add --file exams.json            add exams (ids assigned if missing)

Exams never write to GitLab or any other system: an exam that cannot run read-only stays runnable=false until the
skill has a dry-run path; it is still useful as a written expectation for review.
"""
import argparse
import datetime as dt
import fcntl
import json
import os
import sys

import importlib.util as _ilu

_spec = _ilu.spec_from_file_location("tr_config", os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.py"))
CFG = _ilu.module_from_spec(_spec)
_spec.loader.exec_module(CFG)


DATA = CFG.data_dir()
FILE = os.path.join(DATA, "exams.json")


def load():
    try:
        with open(FILE, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {"exams": []}


def save(db):
    os.makedirs(DATA, mode=0o700, exist_ok=True)
    tmp = "%s.tmp.%d" % (FILE, os.getpid())
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(db, fh, ensure_ascii=False, indent=2)
    os.replace(tmp, FILE)


def lock():
    os.makedirs(DATA, mode=0o700, exist_ok=True)
    fh = open(FILE + ".lock", "w")
    fcntl.flock(fh, fcntl.LOCK_EX)
    return fh


def find(db, xid):
    for x in db["exams"]:
        if x["id"] == xid:
            return x
    sys.exit("no exam %s" % xid)


def last(x):
    return x["runs"][-1] if x.get("runs") else None


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd")
    p = sub.add_parser("list"); p.add_argument("--skill")
    p = sub.add_parser("show"); p.add_argument("id")
    p = sub.add_parser("for-skill"); p.add_argument("skill")
    p = sub.add_parser("record"); p.add_argument("id"); p.add_argument("result", choices=("pass", "fail", "partial"))
    p.add_argument("--commit", required=True); p.add_argument("--note", default="")
    p = sub.add_parser("add"); p.add_argument("--file", required=True)
    a = ap.parse_args()
    if not a.cmd:
        ap.print_help()
        return
    if a.cmd in ("record", "add"):
        held = lock()  # noqa: F841
    db = load()

    if a.cmd == "list":
        xs = [x for x in db["exams"] if not a.skill or x["skill"] == a.skill]
        for x in xs:
            l = last(x)
            print("%-5s %-26s %-9s %-55s %s" % (x["id"], x["skill"], "runnable" if x.get("runnable") else "written",
                                               x["title"][:55], ("%s @%s" % (l["result"], l["commit"][:8])) if l else "-"))
        if not xs:
            print("(no exams)")
    elif a.cmd == "show":
        print(json.dumps(find(db, a.id), ensure_ascii=False, indent=2))
    elif a.cmd == "for-skill":
        xs = [x for x in db["exams"] if x["skill"] == a.skill]
        for x in xs:
            if x.get("runnable"):
                print("%s  %s\n    %s\n    expected: %s" % (x["id"], x["title"], x["how_to_run"], x["expected"]))
            else:
                print("%s  %s  (written expectation; no read-only run yet: %s)" % (x["id"], x["title"], x.get("why_not_runnable", "")))
        if not xs:
            print("(no exams for %s)" % a.skill)
    elif a.cmd == "record":
        x = find(db, a.id)
        x.setdefault("runs", []).append({"at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
                                         "commit": a.commit, "result": a.result, "note": a.note})
        save(db)
        print("%s %s @%s" % (a.id, a.result, a.commit[:8]))
    elif a.cmd == "add":
        new = json.load(open(a.file, encoding="utf-8"))
        n = len(db["exams"])
        ids = []
        for x in new:
            n += 1
            x.setdefault("id", "X%02d" % n)
            x.setdefault("runs", [])
            x.setdefault("created", dt.date.today().isoformat())
            db["exams"].append(x)
            ids.append(x["id"])
        save(db)
        print(" ".join(ids))


if __name__ == "__main__":
    main()
