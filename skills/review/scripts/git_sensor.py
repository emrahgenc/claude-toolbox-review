#!/usr/bin/env python3
"""Git sensor (charter D5, D12) for Toolbox Review: how much of your own committed work is AI-assisted, and how
often it needs a follow-up fix.

Read-only: only `git log` and `git rev-parse`. Looks at your own commits (git config user.email; never printed) in
every repository Claude Code has been used in (keys of ~/.claude.json "projects", worktrees folded into their main
repository) plus config team_repos, across all refs, merges excluded, de-duplicated by hash.

GitLab's squash/merge commits are excluded (they drop the Co-Authored-By trailer and would count as non-AI), as are
files under config process_paths (process records such as .sdlc/). Per repository and in total:
  * commits, AI commits (the value of a Co-Authored-By trailer matches config ai_trailer_pattern), AI share
    by commits and by changed product lines;
  * post-merge fixes (needs forge.json from the same run, so run forge_sensor.py first): merged MRs whose ticket got
    a fix-like commit touching the MR's product files within 7 days after the merge — work that came back;
  * in-branch iteration rate (context only: review-round commits are normal work, not escaped defects) and reverts.
Heuristics, not verdicts: the process lens reads them next to the review threads from the GitLab sensor.

Usage: git_sensor.py [--since 30d|YYYY-MM-DD] [--run-dir DIR]
"""
import argparse
import datetime as dt
import json
import os
import re
import subprocess

HOME = os.path.expanduser("~")
import importlib.util as _ilu

_spec = _ilu.spec_from_file_location("tr_config", os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.py"))
CFG = _ilu.module_from_spec(_spec)
_spec.loader.exec_module(CFG)

# "review", "hata", "eksik" matched process and report commits more than fixes (process-analyst, 2026-11-25).
FIXLIKE = re.compile(r"(?i)\b(fix|fixes|fixed|hotfix|bugfix|revert|lint|typo|d[uü]zelt\w*" + "".join("|" + w for w in CFG.config().get("fix_words", [])) + r")\b")


def run(args, cwd=None):
    try:
        p = subprocess.run(args, cwd=cwd, capture_output=True, text=True, timeout=120)
        return p.stdout
    except (OSError, subprocess.SubprocessError):
        return ""


def since_arg(v):
    now = dt.datetime.now(dt.timezone.utc)
    if not v:
        return now - dt.timedelta(days=30)
    m = re.match(r"^(\d+)d$", v)
    if m:
        return now - dt.timedelta(days=int(m.group(1)))
    return dt.datetime.fromisoformat(v + "T00:00:00+00:00")


def repos(scope):
    cands = [CFG.expand(p) for p in scope.get("team_repos") or []]
    try:
        cands += list((json.load(open(os.path.join(HOME, ".claude.json"), encoding="utf-8")).get("projects") or {}).keys())
    except (OSError, ValueError):
        pass
    seen = {}
    for p in cands:
        if not os.path.isdir(p):
            continue
        common = run(["git", "-C", p, "rev-parse", "--path-format=absolute", "--git-common-dir"]).strip()
        if not common:
            continue
        top = os.path.dirname(common) if common.endswith("/.git") else common
        seen.setdefault(top, top)
    return sorted(seen)


def commits(repo, email, since, skip_shas, process_paths):
    """Your commits, minus GitLab's squash/merge commits (they drop the Co-Authored-By trailer and would count as
    non-AI): known squash/merge SHAs from forge.json, and any commit whose committer is not you (merged in the UI)."""
    sep, rec = "\x1f", "\x1e"
    fmt = "%x1e%H%x1f%aI%x1f%s%x1f%(trailers:key=Co-Authored-By,valueonly,separator=%x1d)%x1f%ce"
    out = run(["git", "-C", repo, "log", "--all", "--no-merges", "--author=" + email, "--since=" + since.isoformat(),
               "--numstat", "--format=" + fmt])
    items = []
    for chunk in out.split(rec)[1:]:
        lines = chunk.split("\n")
        head = lines[0].split(sep)
        if len(head) < 5:
            continue
        if head[0] in skip_shas or head[4].strip().lower() != email.lower():
            continue
        files, added, deleted = [], 0, 0
        for l in lines[1:]:
            parts = l.split("\t")
            if len(parts) == 3:
                if any(parts[2].startswith(p) for p in process_paths):   # process records, not product code
                    continue
                files.append(parts[2])
                added += int(parts[0]) if parts[0].isdigit() else 0
                deleted += int(parts[1]) if parts[1].isdigit() else 0
        if not files:
            continue
        items.append({"hash": head[0], "when": head[1], "subject": head[2], "trailers": head[3],
                      "files": files, "added": added, "deleted": deleted})
    return items


TICKET = re.compile(r"\b[A-Z][A-Z0-9]+-\d+\b")


def post_merge_fixes(repo, mrs, mine, process_paths):
    """Work that came back after merge: for each merged MR, your fix-like commits within 7 days AFTER the merge that
    name the same ticket (e.g. PROJ-123) as the MR and touch a product file the MR changed — e.g. a test-round fix. A plain
    file overlap is not enough: later MRs' review fixes touch the same large files and would be miscounted."""
    out = []
    for m in mrs:
        sha, merged = m.get("squash_commit_sha") or m.get("merge_commit_sha"), m.get("merged_at")
        tickets = set(TICKET.findall(m.get("title") or ""))
        if not sha or not merged or not tickets:
            continue
        names = run(["git", "-C", repo, "diff", "--name-only", sha + "^1", sha]).split("\n")
        files = {f for f in names if f and not any(f.startswith(p) for p in process_paths)}
        if not files:
            continue
        t0 = dt.datetime.fromisoformat(merged.replace("Z", "+00:00"))
        fixes = [c for c in mine
                 if 0 < (dt.datetime.fromisoformat(c["when"].replace("Z", "+00:00")) - t0).total_seconds() <= 7 * 86400
                 and FIXLIKE.search(c["subject"]) and tickets & set(TICKET.findall(c["subject"]))
                 and files & set(c["files"])]
        out.append({"ref": m.get("ref"), "files": len(files), "fixes_within_7d": len(fixes),
                    "fix_subjects": [c["subject"][:70] for c in fixes[:3]]})
    return out


def analyse(items, ai_rx):
    uniq = {c["hash"]: c for c in items}
    cs = sorted(uniq.values(), key=lambda c: c["when"])
    for c in cs:
        c["ai"] = bool(ai_rx.search(c["trailers"]))
        c["t"] = dt.datetime.fromisoformat(c["when"].replace("Z", "+00:00"))   # Python 3.9 has no "Z"
    followed = {}
    for i, c in enumerate(cs):
        for later in cs[i + 1:]:
            if (later["t"] - c["t"]).days > 7:
                break
            if FIXLIKE.search(later["subject"]) and set(later["files"]) & set(c["files"]):
                followed[c["hash"]] = True
                break
    ai = [c for c in cs if c["ai"]]
    non = [c for c in cs if not c["ai"]]
    lines = lambda xs: sum(c["added"] + c["deleted"] for c in xs)
    rate = lambda xs: round(len([c for c in xs if c["hash"] in followed]) / float(len(xs)), 2) if xs else None
    return {
        "commits": len(cs), "ai_commits": len(ai),
        "ai_share_commits": round(len(ai) / float(len(cs)), 2) if cs else None,
        "ai_share_lines": round(lines(ai) / float(lines(cs)), 2) if lines(cs) else None,
        # In-branch iteration (a later fix-like commit on the same file within 7 days) is normal review work, not an
        # escaped defect; kept for context only. The quality signal is post_merge_fixes below.
        "iteration_rate_ai": rate(ai), "iteration_rate_non_ai": rate(non),
        "reverts": len([c for c in cs if c["subject"].lower().startswith("revert")]),
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--since")
    ap.add_argument("--run-dir")
    a = ap.parse_args()
    scope = CFG.config()
    ai_rx = re.compile(scope.get("ai_trailer_pattern") or r"(?i)co-authored-by:.*claude")
    email = run(["git", "config", "--global", "user.email"]).strip()
    since = since_arg(a.since)
    skip, mrs = set(), []
    forge_file = os.path.join(a.run_dir or "", "forge.json")
    if a.run_dir and os.path.exists(forge_file):   # run forge_sensor.py first
        mrs = json.load(open(forge_file, encoding="utf-8")).get("mrs", [])
        for m in mrs:
            skip.update(s for s in (m.get("squash_commit_sha"), m.get("merge_commit_sha")) if s)
    process_paths = scope.get("process_paths") or []
    per, everything, escaped = {}, [], []
    for r in repos(scope):
        items = commits(r, email, since, skip, process_paths)
        if not items:
            continue
        everything += items
        per[r.replace(HOME, "~")] = analyse(items, ai_rx)
        if mrs:
            # The squash/merge SHAs only resolve in the repository they belong to; others yield empty diffs.
            escaped += [x for x in post_merge_fixes(r, mrs, items, process_paths) if x["files"]]
    total = analyse(everything, ai_rx)
    total["mrs_checked_post_merge"] = len(escaped)
    total["mrs_with_post_merge_fix"] = len([x for x in escaped if x["fixes_within_7d"]])
    out = {"since": since.isoformat(), "repos": per, "total": total, "post_merge": escaped,
           "team_repos": [p.replace(HOME, "~") for p in scope.get("team_repos") or []]}
    print("git: since %s · %d repo(s) with your commits (squash/merge commits and %s excluded)" % (
        since.date(), len(per), ", ".join(process_paths)))
    fmt = lambda v: "-" if v is None else v
    for name, m in sorted(per.items(), key=lambda kv: -kv[1]["commits"]):
        print("  %-60s commits %3d · AI %3d (%s of commits, %s of lines) · reverts %d"
              % (name[-60:], m["commits"], m["ai_commits"], fmt(m["ai_share_commits"]), fmt(m["ai_share_lines"]),
                 m["reverts"]))
    print("  TOTAL commits %d · AI share %s commits / %s lines · merged MRs with a fix on the same files within 7 days:"
          " %d of %d" % (total["commits"], fmt(total["ai_share_commits"]), fmt(total["ai_share_lines"]),
                         total["mrs_with_post_merge_fix"], total["mrs_checked_post_merge"]))
    if a.run_dir:
        with open(os.path.join(a.run_dir, "git.json"), "w", encoding="utf-8") as fh:
            json.dump(out, fh, ensure_ascii=False, indent=2)
        print("written: %s" % os.path.join(a.run_dir, "git.json"))


if __name__ == "__main__":
    main()
