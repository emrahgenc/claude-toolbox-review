#!/usr/bin/env python3
"""Forge sensor (charter D4, D5) for Toolbox Review: how your merge/pull requests fare once others see them.

Read-only. It only issues GET requests, with your own login, through the CLI of the forge in config
`forge.type`:
  * gitlab - `glab api --hostname <forge.host>` (merge requests you created)
  * github - `gh api [--hostname <forge.host>]` (pull requests you authored; host only for GitHub Enterprise)
  * none   - the sensor does nothing

Per MR/PR in the period (cap 60):
  * flow     - hours from creation to merge, pipelines / check runs (≈ push rounds);
  * quality  - review threads opened by other people, failed pipelines / check runs;
  * AI       - commits and how many carry an AI co-author trailer (config ai_trailer_pattern);
  * squash / merge commit SHAs, so git_sensor.py can leave them out (squashing drops the trailer).
Output: forge.json in the run folder. Titles are kept (truncated) for the analyst; nothing leaves this machine.

Usage: forge_sensor.py [--since 30d|YYYY-MM-DD] [--run-dir DIR]
"""
import argparse
import datetime as dt
import json
import os
import re
import statistics
import subprocess
import importlib.util as _ilu

_spec = _ilu.spec_from_file_location("tr_config", os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.py"))
CFG = _ilu.module_from_spec(_spec)
_spec.loader.exec_module(CFG)

CAP = 60


def call(args):
    try:
        p = subprocess.run(args, capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError) as e:
        return None, str(e)
    if p.returncode != 0:
        return None, (p.stderr or p.stdout).strip()[:200]
    try:
        return json.loads(p.stdout), None
    except ValueError:
        return None, "not json"


def ts(s):
    return dt.datetime.fromisoformat(s.replace("Z", "+00:00")) if s else None


def since_arg(v):
    now = dt.datetime.now(dt.timezone.utc)
    if not v:
        return now - dt.timedelta(days=30)
    m = re.match(r"^(\d+)d$", v)
    if m:
        return now - dt.timedelta(days=int(m.group(1)))
    return dt.datetime.fromisoformat(v + "T00:00:00+00:00")


def med(xs):
    xs = [x for x in xs if x is not None]
    return round(statistics.median(xs), 1) if xs else None


def ai_count(messages, ai_rx):
    n = 0
    for msg in messages:
        trailers = " ".join(l for l in (msg or "").split("\n") if l.lower().startswith("co-authored-by"))
        if ai_rx.search(trailers):
            n += 1
    return n


def hours(created, merged):
    a, b = ts(created), ts(merged)
    return round((b - a).total_seconds() / 3600, 1) if a and b else None


def gitlab(host, since, ai_rx):
    api = lambda path: call(["glab", "api", "--hostname", host, path])
    me, err = api("user")
    if err:
        return None, "cannot reach %s: %s" % (host, err)
    mrs, page = [], 1
    while len(mrs) < CAP:
        batch, err = api("merge_requests?scope=created_by_me&state=all&per_page=100&page=%d&created_after=%s"
                         % (page, since.strftime("%Y-%m-%dT%H:%M:%SZ")))
        if err or not batch:
            break
        mrs += batch
        if len(batch) < 100:
            break
        page += 1
    rows = []
    for m in mrs[:CAP]:
        base = "projects/%s/merge_requests/%s" % (m["project_id"], m["iid"])
        disc, _ = api(base + "/discussions?per_page=100")
        pipes, _ = api(base + "/pipelines?per_page=50")
        commits, _ = api(base + "/commits?per_page=100")
        threads = 0
        for d in disc or []:
            notes = [n for n in d.get("notes") or [] if not n.get("system")]
            if notes and notes[0].get("resolvable") and (notes[0].get("author") or {}).get("id") != me.get("id"):
                threads += 1
        pipes, commits = pipes or [], commits or []
        rows.append({
            "ref": (m.get("references") or {}).get("full") or "!%s" % m["iid"], "title": (m.get("title") or "")[:80],
            "state": m.get("state"), "created_at": m.get("created_at"), "merged_at": m.get("merged_at"),
            "hours_to_merge": hours(m.get("created_at"), m.get("merged_at")),
            "review_threads": threads, "pipelines": len(pipes),
            "failed_pipelines": len([p for p in pipes if p.get("status") == "failed"]),
            "last_pipeline": pipes[0].get("status") if pipes else None,
            "commits": len(commits), "ai_commits": ai_count([c.get("message") for c in commits], ai_rx),
            "squash_commit_sha": m.get("squash_commit_sha"), "merge_commit_sha": m.get("merge_commit_sha"),
        })
    return rows, None


def github(host, since, ai_rx):
    base = ["gh", "api"] + (["--hostname", host] if host else [])
    api = lambda path: call(base + [path])
    me, err = api("user")
    if err:
        return None, "cannot reach GitHub%s: %s" % (" " + host if host else "", err)
    found, _ = api("search/issues?q=%s&per_page=100" % "+".join(
        ["author:%s" % me.get("login"), "type:pr", "created:>=%s" % since.strftime("%Y-%m-%d")]))
    rows = []
    for it in ((found or {}).get("items") or [])[:CAP]:
        repo = it.get("repository_url", "").split("/repos/")[-1]
        n = it.get("number")
        pr, _ = api("repos/%s/pulls/%s" % (repo, n))
        pr = pr or {}
        comments, _ = api("repos/%s/pulls/%s/comments?per_page=100" % (repo, n))
        commits, _ = api("repos/%s/pulls/%s/commits?per_page=100" % (repo, n))
        head = (pr.get("head") or {}).get("sha")
        checks, _ = api("repos/%s/commits/%s/check-runs?per_page=100" % (repo, head)) if head else (None, None)
        runs = (checks or {}).get("check_runs") or []
        threads = len([c for c in comments or [] if not c.get("in_reply_to_id")
                       and (c.get("user") or {}).get("login") != me.get("login")])
        failed = len([r for r in runs if r.get("conclusion") in ("failure", "timed_out", "cancelled")])
        rows.append({
            "ref": "%s#%s" % (repo, n), "title": (it.get("title") or "")[:80],
            "state": "merged" if pr.get("merged_at") else it.get("state"), "created_at": it.get("created_at"),
            "merged_at": pr.get("merged_at"), "hours_to_merge": hours(it.get("created_at"), pr.get("merged_at")),
            "review_threads": threads, "pipelines": len(runs), "failed_pipelines": failed,
            "last_pipeline": ("failed" if failed else "success") if runs else None,
            "commits": len(commits or []),
            "ai_commits": ai_count([(c.get("commit") or {}).get("message") for c in commits or []], ai_rx),
            "squash_commit_sha": pr.get("merge_commit_sha") if pr.get("merged_at") else None, "merge_commit_sha": None,
        })
    return rows, None


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--since")
    ap.add_argument("--run-dir")
    a = ap.parse_args()
    cfg = CFG.config()
    forge = cfg.get("forge") or {}
    kind, host = forge.get("type", "none"), forge.get("host", "")
    ai_rx = re.compile(cfg.get("ai_trailer_pattern") or r"(?i)claude")
    since = since_arg(a.since)
    if kind == "gitlab":
        rows, err = gitlab(host or "gitlab.com", since, ai_rx)
    elif kind == "github":
        rows, err = github(host, since, ai_rx)
    else:
        print("forge: none configured (config forge.type = gitlab | github)")
        return
    if err:
        print("forge: %s" % err)
        return
    total = {
        "forge": kind, "mrs": len(rows), "merged": len([r for r in rows if r["state"] == "merged"]),
        "median_hours_to_merge": med([r["hours_to_merge"] for r in rows]),
        "median_review_threads": med([r["review_threads"] for r in rows]),
        "mrs_with_failed_pipeline": len([r for r in rows if r["failed_pipelines"]]),
        "median_pipelines_per_mr": med([r["pipelines"] for r in rows]),
        "ai_commit_share": round(sum(r["ai_commits"] for r in rows) / float(sum(r["commits"] for r in rows)), 2)
        if sum(r["commits"] for r in rows) else None,
    }
    print("forge (%s): %d since %s · merged %d · median hours to merge %s · median review threads %s · "
          "with a failed pipeline/check %d · AI commit share %s" % (
              kind, total["mrs"], since.date(), total["merged"], total["median_hours_to_merge"],
              total["median_review_threads"], total["mrs_with_failed_pipeline"], total["ai_commit_share"]))
    for r in rows:
        print("  %-34s %6sh  threads %2d  pipelines %2d (failed %d)  commits %2d (AI %2d)  %s" % (
            r["ref"][-34:], r["hours_to_merge"], r["review_threads"], r["pipelines"], r["failed_pipelines"],
            r["commits"], r["ai_commits"], r["title"][:44]))
    if a.run_dir:
        with open(os.path.join(a.run_dir, "forge.json"), "w", encoding="utf-8") as fh:
            json.dump({"since": since.isoformat(), "forge": kind, "host": host, "total": total, "mrs": rows}, fh,
                      ensure_ascii=False, indent=2)
        print("written: %s" % os.path.join(a.run_dir, "forge.json"))


if __name__ == "__main__":
    main()
