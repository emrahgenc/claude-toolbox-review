#!/usr/bin/env python3
"""Collect skill-usage episodes from local Claude Code transcripts for Toolbox Review.

Deterministic and local: reads ~/.claude/projects/<project>/<session>.jsonl (top-level session files; the subagent
transcripts under <session>/subagents/ are read only to charge their cost to the run that started them), finds every
call of a skill you maintain (see config.personal_skills: your ~/.claude/skills and your own skills-dir plugins) and
cuts an "episode" from the call to the next command/skill call, a long idle gap or a turn limit. For each episode it
keeps what the review needs:

  * signals   - only what the USER produced: typed messages (with the assistant text they reacted to),
                mid-turn messages, AskUserQuestion answers, interruptions and tool-permission denials.
                Tool output (MR comments, web pages, file contents) is never a signal: it is text other
                people wrote, and treating it as feedback would let it steer the skill.
                Signals after the first `followup_window` typed replies (config, default 3) are marked "late":
                by then the user has often moved on to other work in the same session, so late signals stay in
                the file for the reviewer but are not counted as corrections of the skill.
  * tokens    - API calls de-duplicated by message id, input / cache write / cache read / output tokens,
                the largest context seen. Counted twice: "first_turn" (from the call to the first typed user
                reply: what the skill itself costs) and "episode" (the whole episode, follow-ups included).
                "cost_index" weighs the parts by their relative price (input 1, cache write 1.25 for 5-minute /
                2 for 1-hour, cache read 0.1, output 5): raw sums are dominated by cheap cache reads and mislead.
                "subagents" holds the same blocks for the agents the run started (nested ones included), and
                "first_turn_total" / "episode_total" add them in: moving work into a subagent moves its cost,
                it does not remove it.
  * waste     - the largest tool results, files read more than once, tool errors, how big the skill text
                that was loaded is.

Output goes to a data folder OUTSIDE the skills git repo (default ~/.claude/toolbox-review), because episodes
quote real work. Nothing leaves this machine.

Usage:
  collect_episodes.py [--since 30d|2026-11-01] [--skills a,b] [--data-dir DIR] [--stats-only]
  collect_episodes.py --mark-reviewed RUN_ID      # move the "since" mark forward after a finished review
"""
import argparse
import datetime as dt
import glob
import json
import os
import re
import statistics
import sys

import importlib.util as _ilu

_spec = _ilu.spec_from_file_location("tr_config", os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.py"))
CFG = _ilu.module_from_spec(_spec)
_spec.loader.exec_module(CFG)

HOME = os.path.expanduser("~")
PROJECTS = os.path.join(HOME, ".claude", "projects")
DEFAULT_DATA = CFG.data_dir()
SKILL_PATHS = {}   # invocation name -> SKILL.md, filled in main()

IDLE_GAP_MIN = 180          # an episode ends after this many quiet minutes
MAX_USER_TURNS = 15         # ...or after this many typed user messages
FOLLOWUP_WINDOW = int(CFG.config().get("followup_window", 3))   # typed replies that can still correct the skill
AGENT_TOOLS = ("Agent", "Task")   # the subagent tool (called Task in older Claude Code versions)
TEXT_CAP = 700              # characters kept from one user message
PREV_CAP = 400              # characters kept from the assistant text a user message reacts to
FINAL_CAP = 800             # characters kept from the episode's last assistant text
TOP_RESULTS = 5

CMD_RE = re.compile(r"<command-name>/?([^<\s]+)</command-name>")
ARGS_RE = re.compile(r"<command-args>(.*?)</command-args>", re.S)
INTERRUPT = "[Request interrupted by user"
DENIAL = "The user doesn't want to proceed with this tool use"
ANSWER = "Your questions have been answered:"
NOT_TYPED_PREFIXES = ("<command-", "<local-command", "<bash-", "<task-notification", "<system-reminder",
                      "<agent-message", "<cross-session", "Caveat: The messages below", "<side-sessions-event",
                      "<ci-monitor-event", "This session is being continued from a previous conversation")
WRAPPER_RE = re.compile(r"^<[a-z][a-z0-9-]*[\s/>]")   # app-injected text starts with a tag; people rarely do
READISH = re.compile(r"^\s*(cat|sed|head|tail|less|nl|wc)\b")
PATH_RE = re.compile(r"[\w@.~\-/]*[\w@\-]/[\w@.\-/]*\.\w+")


# The app sometimes puts whole blocks in front of what the person typed (in worktree sessions a <system-reminder>
# precedes the message). Those blocks are cut off first; only what remains is judged.
LEADING_BLOCK_RE = re.compile(r"^\s*<(system-reminder|ide_opened_file|ide_selection|ide_diagnostics)\b[^>]*>.*?</\1>\s*",
                              re.S)


def typed_text(rec, text):
    """What the person typed in this user record, or None when the record is app or tool text."""
    if rec.get("isMeta") or rec.get("isCompactSummary") or rec.get("isVisibleInTranscriptOnly"):
        return None
    t = text
    while True:
        m = LEADING_BLOCK_RE.match(t)
        if not m:
            break
        t = t[m.end():]
    t = t.strip()
    if not t or t.startswith(NOT_TYPED_PREFIXES) or WRAPPER_RE.match(t):
        return None
    return t


def injected(rec, text):
    """True for text the app or a tool put in a user record, not something the person typed."""
    return typed_text(rec, text) is None


def new_stats():
    return {"tools": {}, "mcp_servers": {}, "agents": {}, "skills": {}, "hooks": {}, "tool_errors": 0}


def bump(d, k, n=1):
    d[k] = d.get(k, 0) + n


def tally(rec, stats, ids):
    """Whole-toolbox usage, counted over every main-thread record in the period (not only episodes)."""
    typ = rec.get("type")
    if typ == "attachment":
        a = rec.get("attachment") or {}
        if str(a.get("type", "")).startswith("hook_"):
            bump(stats["hooks"], a.get("hookName") or a.get("hookEvent") or a.get("type"))
        return
    msg = rec.get("message") or {}
    if typ == "user" and not rec.get("isMeta"):
        m = CMD_RE.search(text_of(msg))
        if m:
            s = stats["skills"].setdefault(m.group(1), {"typed": 0, "model": 0})
            s["typed"] += 1
    for b in blocks(msg):
        if typ == "assistant" and b.get("type") == "tool_use":
            n, inp = b.get("name", "?"), b.get("input") or {}
            bump(stats["tools"], n)
            if n.startswith("mcp__"):
                bump(stats["mcp_servers"], n.split("__")[1] if n.count("__") >= 2 else n)
            elif n == "Agent":
                t = inp.get("subagent_type") or "general-purpose"
                stats["agents"].setdefault(t, {"calls": 0, "failed": 0})["calls"] += 1
                ids[b.get("id")] = t
            elif n == "Skill":
                s = stats["skills"].setdefault(inp.get("skill", "?"), {"typed": 0, "model": 0})
                s["model"] += 1
        elif typ == "user" and b.get("type") == "tool_result":
            if b.get("is_error"):
                stats["tool_errors"] += 1
                t = ids.get(b.get("tool_use_id"))
                if t:
                    stats["agents"][t]["failed"] += 1


def parse_ts(s):
    if not s:
        return None
    try:
        return dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


def squash(s, cap):
    s = re.sub(r"\s+", " ", s or "").strip()
    return s if len(s) <= cap else s[:cap] + " …"


def blocks(msg):
    c = (msg or {}).get("content")
    if isinstance(c, str):
        return [{"type": "text", "text": c}]
    return [b for b in c if isinstance(b, dict)] if isinstance(c, list) else []


def text_of(msg):
    return "\n".join(b.get("text") or "" for b in blocks(msg) if b.get("type") == "text")


def result_text(b):
    c = b.get("content")
    if isinstance(c, str):
        return c
    if isinstance(c, list):
        return "\n".join(x.get("text") or "" for x in c if isinstance(x, dict))
    return ""


def tool_hint(name, inp):
    inp = inp or {}
    if name == "Bash":
        return squash(inp.get("command", ""), 110)
    if name in ("Read", "Write", "Edit"):
        return inp.get("file_path", "")
    if name in ("Grep", "Glob"):
        return squash("%s %s" % (inp.get("pattern", ""), inp.get("path", "")), 110)
    if name == "Agent":
        return squash("%s: %s" % (inp.get("subagent_type", "general"), inp.get("description", "")), 110)
    return squash(json.dumps(inp, ensure_ascii=False), 110)


def read_paths(name, inp):
    inp = inp or {}
    if name == "Read" and inp.get("file_path"):
        return [inp["file_path"]]
    if name == "Bash" and READISH.match(inp.get("command", "")):
        return PATH_RE.findall(inp.get("command", ""))
    return []


def personal_skills():
    SKILL_PATHS.clear()
    SKILL_PATHS.update(CFG.personal_skills())
    return sorted(SKILL_PATHS)


def skill_static(name):
    """Size of what a call loads (SKILL.md) and of the always-loaded description."""
    path = SKILL_PATHS.get(name, "")
    try:
        text = open(path, encoding="utf-8").read()
    except OSError:
        return {}
    m = re.match(r"^---\n(.*?)\n---\n", text, re.S)
    fm = m.group(1) if m else ""
    desc = re.search(r"^description:\s*(.*)$", fm, re.M)
    refs = [p for p in glob.glob(os.path.join(os.path.dirname(path), "**", "*"), recursive=True)
            if os.path.isfile(p) and not p.endswith("SKILL.md") and "__pycache__" not in p]
    return {
        "skill_md_chars": len(text),
        "skill_md_lines": text.count("\n") + 1,
        "description_chars": len(desc.group(1)) if desc else 0,
        "support_files": len(refs),
        "support_chars": sum(os.path.getsize(p) for p in refs),
    }


USAGE_FIELDS = ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens", "output_tokens",
                "cw_5m", "cw_1h")


def usage_of(usage):
    u = {k: usage.get(k, 0) or 0 for k in USAGE_FIELDS[:4]}
    cc = usage.get("cache_creation") or {}
    u["cw_5m"] = cc.get("ephemeral_5m_input_tokens", 0) or 0
    u["cw_1h"] = cc.get("ephemeral_1h_input_tokens", 0) or 0
    return u


def token_block(calls):
    t = {k: sum(u.get(k, 0) for u in calls) for k in USAGE_FIELDS}
    split = t["cw_5m"] + t["cw_1h"]
    cache_write_cost = (1.25 * t["cw_5m"] + 2.0 * t["cw_1h"]) if split else 1.25 * t["cache_creation_input_tokens"]
    return {
        "api_calls": len(calls),
        "input": t["input_tokens"], "cache_write": t["cache_creation_input_tokens"],
        "cache_read": t["cache_read_input_tokens"], "output": t["output_tokens"],
        "cost_index": int(t["input_tokens"] + cache_write_cost + 0.1 * t["cache_read_input_tokens"]
                          + 5 * t["output_tokens"]),
        "peak_context": max([u["input_tokens"] + u["cache_creation_input_tokens"] + u["cache_read_input_tokens"]
                             for u in calls] or [0]),
    }


# What a typed message is, for measuring (charter D13): an approval ("yes, send it", "1"), new work (a new slash
# command) or a correction/steer. Only corrections count as "the skill needed fixing". Built-in words cover English
# and Turkish; add your own with config approval_words.
_APPROVAL_WORDS = [r"yes", r"yep", r"ok(?:ay)?", r"sure", r"go(?: ahead)?\b", r"lgtm", r"approve\w*", r"proceed",
                   r"continue", r"send it", r"ship it", r"do it", r"sounds good", r"perfect", r"great", r"thanks?",
                   r"thank you", r"all\b", r"both\b", r"recommended",
                   r"evet", r"tamam\w*", r"olur", r"devam", r"onay\w*", r"gönder", r"başla", r"yap\b", r"yapalım",
                   r"uygula", r"aynen", r"harika", r"süper", r"güzel", r"teşekkür\w*", r"sağ ?ol", r"hepsi\w*",
                   r"önerilen"] + [w for w in CFG.config().get("approval_words", [])]
APPROVAL_RE = re.compile(r"(?i)^\s*(" + "|".join(_APPROVAL_WORDS) + ")")
CHOICE_RE = re.compile(r"^\s*([0-9]+[\s,.-]*)+$|^\s*[a-dA-D]\s*$")
NEW_WORK_RE = re.compile(r"(^|\s)/[a-z][\w:-]{2,}")
# Outward writes (charter V2): pushes and GitLab / web writes run through Bash.
GIT_PUSH_RE = re.compile(r"\bgit\s+(?:-C\s+\S+\s+)?push\b")
GITLAB_WRITE_RE = re.compile(r"\bglab\s+(?:api\b[^\n]*(?:-X|--method)\s*(?:POST|PUT|PATCH|DELETE)|mr\s+(?:note|update|merge|approve|"
                             r"close|reopen)|issue\s+(?:create|note|update|close))", re.I)


def classify(text):
    t = text.strip()
    if NEW_WORK_RE.search(t):
        return "new_work"
    if len(t) <= 80 and (APPROVAL_RE.match(t) or CHOICE_RE.match(t)):
        return "approval"
    return "correction"


class Episode(object):
    def __init__(self, skill, trigger, args, rec, session, path):
        self.skill = skill
        self.d = {
            "skill": skill, "trigger": trigger, "args": squash(args, 300),
            "session": session, "transcript": path, "cwd": rec.get("cwd"), "branch": rec.get("gitBranch"),
            "claude_version": rec.get("version"), "start": rec.get("timestamp"), "end": rec.get("timestamp"),
            "skill_load_chars": 0, "models": {}, "tools": {}, "tool_errors": 0,
            "signals": [], "final_assistant": "", "ended_by": "session-end",
        }
        self.usage = {}           # message id -> usage (max per field)
        self.first_turn_ids = None  # usage ids spent before the first typed user reply
        self.tool_names = {}      # tool_use id -> (name, input)
        self.results = []         # (chars, name, hint)
        self.reads = {}
        self.agent_calls = {}     # Agent tool_use id -> {type, first_turn, agent_id, reported_tokens}
        self.typed = 0
        self.last_assistant_text = ""
        self.expect_load = True
        self.d["actions"] = {"git_push": 0, "gitlab_write": 0, "writes_after_approval": 0, "writes_without_approval": 0}
        self.approved = False   # an approval or a menu answer covers the writes that follow, until the next steer

    def note_command(self, cmd):
        for key, rx in (("git_push", GIT_PUSH_RE), ("gitlab_write", GITLAB_WRITE_RE)):
            n = len(rx.findall(cmd or ""))
            if n:
                self.d["actions"][key] += n
                self.d["actions"]["writes_after_approval" if self.approved else "writes_without_approval"] += n

    def add_signal(self, kind, text, ts, with_prev=True):
        # n = typed replies before this signal (0 = during the skill's own run, 1 = the first reply).
        # Past the follow-up window the user has often moved on to other work in the same session: such signals
        # are kept for the reviewer but marked late, and late signals are not counted as corrections of the skill.
        s = {"t": ts, "kind": kind, "n": self.typed, "text": squash(text, TEXT_CAP)}
        if self.typed > FOLLOWUP_WINDOW:
            s["late"] = True
        if kind in ("user", "midturn"):
            s["class"] = classify(text)
            self.approved = s["class"] == "approval"
        elif kind == "answer":
            s["class"] = "approval"
            self.approved = True
        else:   # interrupt, denial
            s["class"] = "correction"
            self.approved = False
        if with_prev and self.last_assistant_text:
            s["reacting_to"] = squash(self.last_assistant_text, PREV_CAP)
        self.d["signals"].append(s)

    def end_first_turn(self):
        if self.first_turn_ids is None:
            self.first_turn_ids = set(self.usage.keys())

    def note_agent_result(self, blocks_, tur):
        """Remember which subagent an Agent call started (its id links to the subagent transcript)."""
        ids = [b.get("tool_use_id") for b in blocks_ if b.get("type") == "tool_result"
               and b.get("tool_use_id") in self.agent_calls]
        if len(ids) != 1 or not isinstance(tur, dict):   # one toolUseResult per record; ambiguous otherwise
            return
        call = self.agent_calls[ids[0]]
        call["agent_id"] = tur.get("agentId") or call.get("agent_id")
        if isinstance(tur.get("totalTokens"), int):      # foreground agents in some versions report a total
            call["reported_tokens"] = tur["totalTokens"]

    def subagent_tokens(self, subs):
        first_calls, all_calls, by_type = [], [], {}
        unresolved, reported = 0, 0
        for cid, c in self.agent_calls.items():
            by_type[c["type"]] = by_type.get(c["type"], 0) + 1
            got = subs.calls(cid, c.get("agent_id")) if subs else None
            if got is None:
                unresolved += 1
                reported += c.get("reported_tokens", 0)
                continue
            all_calls.extend(got)
            if c["first_turn"]:
                first_calls.extend(got)
        raw = sum(u["input_tokens"] + u["cache_creation_input_tokens"] + u["cache_read_input_tokens"]
                  + u["output_tokens"] for u in all_calls)
        return {"runs": len(self.agent_calls), "unresolved": unresolved, "by_type": by_type,
                "first_turn": token_block(first_calls), "episode": token_block(all_calls)}, raw + reported

    def finish(self, subs=None):
        d = self.d
        first = self.first_turn_ids if self.first_turn_ids is not None else set(self.usage.keys())
        ft = token_block([u for k, u in self.usage.items() if k in first])
        ep = token_block(list(self.usage.values()))
        sub, raw = self.subagent_tokens(subs)
        d["tokens"] = {
            "first_turn": ft,
            "episode": ep,
            "subagents": sub,
            "first_turn_total": ft["cost_index"] + sub["first_turn"]["cost_index"],
            "episode_total": ep["cost_index"] + sub["episode"]["cost_index"],
            "subagent_total": raw,
        }
        self.results.sort(reverse=True)
        d["largest_results"] = [{"chars": c, "tool": n, "hint": h} for c, n, h in self.results[:TOP_RESULTS]]
        d["repeated_reads"] = sorted(({"path": p, "times": n} for p, n in self.reads.items() if n > 1),
                                     key=lambda x: -x["times"])[:8]
        d["final_assistant"] = squash(self.last_assistant_text, FINAL_CAP)
        a, b = parse_ts(d["start"]), parse_ts(d["end"])
        d["minutes"] = round((b - a).total_seconds() / 60, 1) if a and b else None
        return d


class Subagents(object):
    """Cost of the subagents one session started, read from <session>/subagents/agent-*.jsonl.

    Background agents (the default in recent Claude Code versions) report no tokens back to the main transcript:
    their own transcript is the only record. The .meta.json beside each one names the Agent call that started it
    (toolUseId), so the cost can be charged to the skill run that made the call, nested agents included."""

    def __init__(self, transcript_path, session):
        self.dir = os.path.join(os.path.dirname(transcript_path), session, "subagents")
        self.by_call = None
        self.cache = {}

    def log_for(self, call_id, agent_id):
        if self.by_call is None:
            self.by_call = {}
            for meta in glob.glob(os.path.join(self.dir, "*.meta.json")):
                try:
                    with open(meta, encoding="utf-8") as fh:
                        call = json.load(fh).get("toolUseId")
                except (OSError, ValueError, AttributeError):
                    continue
                if call:
                    self.by_call[call] = meta[:-len(".meta.json")] + ".jsonl"
        log = self.by_call.get(call_id)
        if not log and agent_id:
            log = os.path.join(self.dir, "agent-%s.jsonl" % agent_id)
        return log if log and os.path.exists(log) else None

    def read(self, log):
        """(usage per API call, ids of the Agent calls made inside) for one subagent transcript."""
        if log not in self.cache:
            calls, children = {}, []
            with open(log, encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    if '"assistant"' not in line:
                        continue
                    try:
                        rec = json.loads(line)
                    except ValueError:
                        continue
                    msg = rec.get("message") or {}
                    if rec.get("type") != "assistant":
                        continue
                    if msg.get("usage"):
                        mid = msg.get("id") or rec.get("requestId")
                        now, prev = usage_of(msg["usage"]), calls.get(mid, {})
                        calls[mid] = {k: max(prev.get(k, 0), now[k]) for k in USAGE_FIELDS}
                    children += [b.get("id") for b in blocks(msg)
                                 if b.get("type") == "tool_use" and b.get("name") in AGENT_TOOLS]
            self.cache[log] = (list(calls.values()), children)
        return self.cache[log]

    def calls(self, call_id, agent_id=None, seen=None):
        """Every API call of the agent that call_id started and of the agents it started in turn; None if the
        subagent transcript is not there (deleted, or a version that did not write one)."""
        seen = set() if seen is None else seen
        log = self.log_for(call_id, agent_id)
        if log is None:
            return None
        if log in seen:
            return []
        seen.add(log)
        try:
            own, children = self.read(log)
        except OSError:
            return None
        out = list(own)
        for child in children:
            out.extend(self.calls(child, None, seen) or [])
        return out


def invocation(rec, my_skills):
    """Return (kind, skill, args) when this record starts a command or skill call, else None.
    kind: 'own' for a maintained skill, 'other' for anything else (still ends the running episode)."""
    if rec.get("isSidechain"):
        return None
    msg = rec.get("message") or {}
    if rec.get("type") == "user" and not rec.get("isMeta"):
        t = text_of(msg)
        m = CMD_RE.search(t)
        if m:
            name = m.group(1)
            a = ARGS_RE.search(t)
            args = a.group(1) if a else ""
            if name in my_skills:
                return ("own", name, args, "typed")
            return ("other", name, args, "typed")
    if rec.get("type") == "assistant":
        for b in blocks(msg):
            if b.get("type") == "tool_use" and b.get("name") == "Skill":
                name = (b.get("input") or {}).get("skill", "")
                args = (b.get("input") or {}).get("args", "")
                if name in my_skills:
                    return ("own", name, args, "model")
                return ("other", name, args, "model")
    return None


def scan_file(path, my_skills, wanted, stats, since):
    session = os.path.basename(path)[:-6]
    episodes, cur, last_ts = [], None, None
    agent_ids = {}
    subs = Subagents(path, session)

    def close(reason):
        nonlocal cur
        if cur is not None:
            cur.d["ended_by"] = reason
            episodes.append(cur.finish(subs))
            cur = None

    with open(path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            if '"api-request-blob"' in line[:300]:
                continue
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            typ = rec.get("type")
            if typ not in ("user", "assistant", "attachment"):
                continue
            ts = rec.get("timestamp")
            tsd = parse_ts(ts)
            if cur is not None and last_ts and tsd and (tsd - last_ts).total_seconds() > IDLE_GAP_MIN * 60:
                close("idle-gap")
            if tsd:
                last_ts = tsd
            if tsd and tsd >= since and not rec.get("isSidechain"):
                tally(rec, stats, agent_ids)

            inv = invocation(rec, my_skills)
            if inv:
                kind, name, args, trigger = inv
                close("next-call:" + name)
                if kind == "own" and name in wanted:
                    cur = Episode(name, trigger, args, rec, session, path)
                if trigger == "typed":
                    continue
            if cur is None or rec.get("isSidechain"):
                continue
            cur.d["end"] = ts or cur.d["end"]
            msg = rec.get("message") or {}

            if typ == "attachment":
                a = rec.get("attachment") or {}
                origin = a.get("origin") if isinstance(a.get("origin"), dict) else {}
                if (a.get("type") == "queued_command" and a.get("commandMode") == "prompt" and not a.get("isMeta")
                        and origin.get("kind") in (None, "user", "human")):
                    p = typed_text({}, a.get("prompt") or "")
                    if p:
                        cur.add_signal("midturn", p, ts)
                continue

            if typ == "assistant":
                usage = msg.get("usage") or {}
                mid = msg.get("id") or rec.get("requestId") or ts
                if usage:
                    prev = cur.usage.get(mid)
                    now = usage_of(usage)
                    cur.usage[mid] = {k: max((prev or {}).get(k, 0), now[k]) for k in USAGE_FIELDS}
                    if prev is None:
                        model = msg.get("model") or "?"
                        cur.d["models"][model] = cur.d["models"].get(model, 0) + 1
                for b in blocks(msg):
                    if b.get("type") == "text" and (b.get("text") or "").strip():
                        cur.last_assistant_text = b["text"]
                    elif b.get("type") == "tool_use":
                        n, inp = b.get("name", "?"), b.get("input") or {}
                        cur.tool_names[b.get("id")] = (n, inp)
                        if n == "Bash":
                            cur.note_command(inp.get("command", ""))
                        elif n in AGENT_TOOLS:
                            cur.agent_calls[b.get("id")] = {"type": inp.get("subagent_type") or "general-purpose",
                                                            "first_turn": cur.first_turn_ids is None}
                        cur.d["tools"][n] = cur.d["tools"].get(n, 0) + 1
                        for p in read_paths(n, inp):
                            cur.reads[p] = cur.reads.get(p, 0) + 1
                continue

            # user record
            if rec.get("isMeta"):
                t = text_of(msg)
                if cur.expect_load and t.startswith("Base directory for this skill"):
                    cur.d["skill_load_chars"] = len(t)
                    cur.expect_load = False
                continue
            bl = blocks(msg)
            if any(b.get("type") == "tool_result" for b in bl):
                for b in bl:
                    if b.get("type") != "tool_result":
                        continue
                    rt = result_text(b)
                    n, inp = cur.tool_names.get(b.get("tool_use_id"), ("?", {}))
                    if b.get("is_error"):
                        cur.d["tool_errors"] += 1
                    cur.results.append((len(rt), n, tool_hint(n, inp)))
                    if DENIAL in rt:
                        cur.add_signal("denial", "%s denied: %s" % (n, tool_hint(n, inp)), ts)
                    elif n == "AskUserQuestion" and ANSWER in rt:
                        cur.add_signal("answer", rt.split(ANSWER, 1)[1], ts)
                cur.note_agent_result(bl, rec.get("toolUseResult"))
                continue
            t = text_of(msg)
            if not t.strip():
                continue
            if t.lstrip().startswith(INTERRUPT):
                cur.end_first_turn()
                cur.add_signal("interrupt", t, ts)
                continue
            typed = typed_text(rec, t)
            if typed is None:
                continue
            cur.end_first_turn()
            cur.typed += 1
            cur.add_signal("user", typed, ts)
            if cur.typed >= MAX_USER_TURNS:
                close("turn-limit")
    close("session-end")
    return episodes


def subagent_usage(since):
    """Token cost per agent type, from the subagent transcripts (<session>/subagents/agent-*.jsonl + .meta.json)."""
    per = {}
    for meta_path in glob.glob(os.path.join(PROJECTS, "*", "*", "subagents", "*.meta.json")):
        log = meta_path[:-len(".meta.json")] + ".jsonl"
        try:
            if dt.datetime.fromtimestamp(os.path.getmtime(log), dt.timezone.utc) < since:
                continue
            meta = json.load(open(meta_path, encoding="utf-8"))
        except (OSError, ValueError):
            continue
        calls = {}
        with open(log, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if '"usage"' not in line:
                    continue
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                msg = rec.get("message") or {}
                if rec.get("type") == "assistant" and msg.get("usage"):
                    mid = msg.get("id") or rec.get("requestId")
                    now = usage_of(msg["usage"])
                    prev = calls.get(mid, {})
                    calls[mid] = {k: max(prev.get(k, 0), now[k]) for k in USAGE_FIELDS}
        b = token_block(list(calls.values()))
        t = meta.get("agentType") or "?"
        a = per.setdefault(t, {"runs": 0, "models": {}, "cost": [], "calls": [], "peak": []})
        a["runs"] += 1
        bump(a["models"], meta.get("model") or "?")
        a["cost"].append(b["cost_index"])
        a["calls"].append(b["api_calls"])
        a["peak"].append(b["peak_context"])
    return {t: {"runs": a["runs"], "models": a["models"], "cost_total": sum(a["cost"]),
                "cost_median": median(a["cost"]), "calls_median": median(a["calls"]),
                "peak_context_median": median(a["peak"])} for t, a in per.items()}


def feedback_memories(since, skills):
    out = []
    for p in glob.glob(os.path.join(PROJECTS, "*", "memory", "*.md")):
        if os.path.basename(p) == "MEMORY.md":
            continue
        try:
            if dt.datetime.fromtimestamp(os.path.getmtime(p), dt.timezone.utc) < since:
                continue
            text = open(p, encoding="utf-8").read()
        except OSError:
            continue
        if not re.search(r"^\s*type:\s*feedback\s*$", text, re.M):
            continue
        name = re.search(r"^name:\s*(.*)$", text, re.M)
        desc = re.search(r"^description:\s*(.*)$", text, re.M)
        mentions = [s for s in skills if re.search(r"(?<![\w-])%s(?![\w-])" % re.escape(s.split(":")[-1]), text)]
        out.append({"path": p, "name": name.group(1).strip() if name else "",
                    "description": squash(desc.group(1) if desc else "", 300), "mentions": mentions})
    return out


def since_arg(value, state):
    now = dt.datetime.now(dt.timezone.utc)
    if not value:
        mark = state.get("reviewed_until")
        return parse_ts(mark) if mark else now - dt.timedelta(days=30)
    m = re.match(r"^(\d+)d$", value)
    if m:
        return now - dt.timedelta(days=int(m.group(1)))
    d = parse_ts(value if "T" in value else value + "T00:00:00+00:00")
    if not d:
        sys.exit("--since: use 30d or YYYY-MM-DD")
    return d


def load_state(data_dir):
    try:
        return json.load(open(os.path.join(data_dir, "state.json"), encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_state(data_dir, state):
    tmp = os.path.join(data_dir, "state.json.tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(state, fh, ensure_ascii=False, indent=2)
    os.replace(tmp, os.path.join(data_dir, "state.json"))


def median(xs):
    return int(statistics.median(xs)) if xs else 0


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--since")
    ap.add_argument("--skills", help="comma list of maintained skill names as invoked, e.g. my-skill,myplugin:review "
                                     "(default: all)")
    ap.add_argument("--data-dir", default=DEFAULT_DATA)
    ap.add_argument("--stats-only", action="store_true", help="print the summary, write nothing")
    ap.add_argument("--mark-reviewed", metavar="RUN_ID")
    a = ap.parse_args()

    os.makedirs(a.data_dir, mode=0o700, exist_ok=True)
    state = load_state(a.data_dir)

    if a.mark_reviewed:
        summary = os.path.join(a.data_dir, "runs", a.mark_reviewed, "summary.json")
        try:
            until = json.load(open(summary, encoding="utf-8"))["until"]
        except (OSError, ValueError, KeyError):
            sys.exit("no run %s" % a.mark_reviewed)
        state["reviewed_until"] = until
        state["last_reviewed_run"] = a.mark_reviewed
        save_state(a.data_dir, state)
        print("reviewed_until = %s" % until)
        return

    my_skills = personal_skills()
    wanted = set(my_skills) if not a.skills else {s.strip().lstrip("/") for s in a.skills.split(",")}
    unknown = wanted - set(my_skills)
    if unknown:
        sys.exit("unknown skill(s): %s" % ", ".join(sorted(unknown)))
    since = since_arg(a.since, state)
    until = dt.datetime.now(dt.timezone.utc)

    files = [p for p in glob.glob(os.path.join(PROJECTS, "*", "*.jsonl"))
             if dt.datetime.fromtimestamp(os.path.getmtime(p), dt.timezone.utc) >= since]
    episodes = []
    stats = new_stats()
    for p in files:
        for e in scan_file(p, set(my_skills), wanted, stats, since):
            st = parse_ts(e["start"])
            if st and st >= since:
                episodes.append(e)
    # A resumed or forked session copies its history into a new transcript, so the same call shows up in two
    # files with the same start time. Keep one copy: the one that saw the most (signals, then the latest end).
    best = {}
    for e in episodes:
        k = (e["skill"], e["start"], e["trigger"])
        if k not in best or (len(e["signals"]), e["end"] or "") > (len(best[k]["signals"]), best[k]["end"] or ""):
            best[k] = e
    duplicates = len(episodes) - len(best)
    episodes = sorted(best.values(), key=lambda e: e["start"] or "")
    for i, e in enumerate(episodes, 1):
        # Stable across runs (charter V3: evidence must stay findable): start minute + session prefix.
        st = parse_ts(e["start"])
        e["id"] = "%s-%s" % (st.strftime("%y%m%d-%H%M") if st else "E%03d" % i, (e.get("session") or "")[:8])

    per = {}
    versions = sorted({e["claude_version"] for e in episodes if e.get("claude_version")})
    for s in sorted(wanted):
        eps = [e for e in episodes if e["skill"] == s]
        kinds, classes, late, acts = {}, {}, {}, {}
        for e in eps:
            for g in e["signals"]:
                kinds[g["kind"]] = kinds.get(g["kind"], 0) + 1
                if g.get("class"):
                    bucket = late if g.get("late") else classes   # late signals: kept, not counted
                    bucket[g["class"]] = bucket.get(g["class"], 0) + 1
            for k, v in (e.get("actions") or {}).items():
                acts[k] = acts.get(k, 0) + v
        ft = [e["tokens"]["first_turn"] for e in eps]
        ep = [e["tokens"]["episode"] for e in eps]
        sub = [e["tokens"]["subagents"] for e in eps]
        per[s] = dict(skill_static(s), episodes=len(eps), signals=kinds, signal_classes=classes,
                      late_signal_classes=late, actions=acts,
                      corrections_per_episode=round(classes.get("correction", 0) / float(len(eps)), 2) if eps else None,
                      first_turn_cost_median=median([x["cost_index"] for x in ft]),
                      first_turn_cost_max=max([x["cost_index"] for x in ft] or [0]),
                      first_turn_total_cost_median=median([e["tokens"]["first_turn_total"] for e in eps]),
                      first_turn_calls_median=median([x["api_calls"] for x in ft]),
                      first_turn_output_median=median([x["output"] for x in ft]),
                      episode_cost_median=median([x["cost_index"] for x in ep]),
                      peak_context_median=median([x["peak_context"] for x in ep]),
                      subagent_runs=sum(x["runs"] for x in sub),
                      subagents_unresolved=sum(x["unresolved"] for x in sub),
                      subagent_cost_median=median([x["episode"]["cost_index"] for x in sub]),
                      subagent_tokens=sum(e["tokens"]["subagent_total"] for e in eps))
    fb = feedback_memories(since, my_skills)
    for t, v in subagent_usage(since).items():
        stats["agents"].setdefault(t, {"calls": 0, "failed": 0}).update(v)
    run_id = until.strftime("%Y%m%d-%H%M%S")
    summary = {"run_id": run_id, "since": since.isoformat(), "until": until.isoformat(),
               "transcripts_scanned": len(files), "claude_versions_seen": versions,
               "followup_window": FOLLOWUP_WINDOW,
               "always_loaded_description_chars": sum(v.get("description_chars", 0) for v in per.values()),
               "skills": per, "feedback_memories": fb, "usage": stats}

    summary["duplicate_episodes_dropped"] = duplicates
    print("run %s  since %s  transcripts %d  episodes %d (duplicates dropped %d)  feedback-memories %d"
          % (run_id, since.date(), len(files), len(episodes), duplicates, len(fb)))
    print("cost = cost_index in thousands (input-token equivalents); run = call to first typed reply; "
          "all = run + the subagents it started; corr = corrections in the first %d replies (late ones in brackets)"
          % FOLLOWUP_WINDOW)
    print("%-30s %4s %5s %5s %5s %5s %8s %8s %8s %4s %6s %8s %7s %6s" % (
        "skill", "eps", "user", "corr", "intr", "ans", "run/med", "all/med", "run/max", "sub", "calls", "epi/med",
        "md-ch", "desc"))
    for s, v in sorted(per.items(), key=lambda kv: -kv[1]["episodes"]):
        k = v["signals"]
        corr = "%d(%d)" % (v["signal_classes"].get("correction", 0), v["late_signal_classes"].get("correction", 0))
        print("%-30s %4d %5d %5s %5d %5d %8d %8d %8d %4d %6d %8d %7d %6d" % (
            s, v["episodes"], k.get("user", 0) + k.get("midturn", 0), corr, k.get("interrupt", 0),
            k.get("answer", 0), v["first_turn_cost_median"] // 1000, v["first_turn_total_cost_median"] // 1000,
            v["first_turn_cost_max"] // 1000, v["subagent_runs"], v["first_turn_calls_median"],
            v["episode_cost_median"] // 1000, v.get("skill_md_chars", 0), v.get("description_chars", 0)))
    print("always-loaded description chars (all maintained skills): %d" % summary["always_loaded_description_chars"])
    top = lambda d, n=8: ", ".join("%s %s" % (k, v) for k, v in sorted(d.items(), key=lambda kv: -kv[1])[:n])
    print("agents (calls, failed, runs, cost total k): " + ", ".join(
        "%s %d/%d/%d/%dk" % (k, v["calls"], v["failed"], v.get("runs", 0), v.get("cost_total", 0) // 1000)
        for k, v in sorted(stats["agents"].items(), key=lambda kv: -kv[1]["calls"])))
    print("mcp servers: " + top(stats["mcp_servers"]))
    print("hooks: " + top(stats["hooks"]))
    print("tools: " + top(stats["tools"], 10) + " | tool errors %d" % stats["tool_errors"])
    if a.stats_only:
        return

    run_dir = os.path.join(a.data_dir, "runs", run_id)
    os.makedirs(os.path.join(run_dir, "episodes"), mode=0o700, exist_ok=True)
    with open(os.path.join(run_dir, "summary.json"), "w", encoding="utf-8") as fh:
        json.dump(summary, fh, ensure_ascii=False, indent=2)
    for s in wanted:
        eps = [e for e in episodes if e["skill"] == s]
        if eps:
            with open(os.path.join(run_dir, "episodes", CFG.safe_name(s) + ".json"), "w", encoding="utf-8") as fh:
                json.dump(eps, fh, ensure_ascii=False, indent=2)
    state["last_run"] = run_id
    save_state(a.data_dir, state)
    print("written: %s" % run_dir)


if __name__ == "__main__":
    main()
