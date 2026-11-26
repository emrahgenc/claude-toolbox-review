# Toolbox Review

**Scheduled, evidence-based maintenance for your Claude Code toolbox.**

You build skills, agents, hooks, CLAUDE.md rules, memory notes and plugin setups. They grow, overlap, go stale, cost
tokens in every session and get corrected by you again and again — and nothing tells you which ones. Toolbox Review is a
Claude Code plugin that reads **your own transcripts and setup**, finds where your toolbox fails you, proposes concrete
fixes, lets you decide in a plain-language menu, applies what you approve, and later **checks whether each change kept
its promise**.

```
sensors (local scripts, no model) → review agents (read only) → report + scorecard
      → you decide (plain menu) → apply (backup, commit) → measured in later rounds
```

Nothing changes without your approval. Nothing leaves your machine.

## What it looks at

| Area | Examples of what it finds |
|---|---|
| Component health | The skill you corrected in 4 of 6 runs; the step you interrupt every time |
| Form and fit | A skill that should be an agent (no input needed, bloats context); a "NEVER do X" rule that should be a hook; a repeated command chain that should be a script |
| Token cost | Text loaded in every session (descriptions, CLAUDE.md, deferred tool names) over budget; files read three times per run; oversized SKILL.md files |
| Security | Broad permission rules, production commands guarded only by prose, plain-text secrets in settings (values never printed), unpinned sources |
| Continuity | Hooks and scripts that silently broke, components outside git, a toolbox with no remote copy, expired logins |
| Memory | MEMORY.md over the 200-line load limit, broken links, notes that should become rules |
| Process and quality | How your merge/pull requests fare in review (GitLab or GitHub, read only), fixes after merge, AI share of your commits |
| Technology radar | New Claude Code and model features, tied to the components they would improve |
| Values | Whether your toolbox follows the rules you wrote down (approval gates, evidence, privacy) |

Every recommendation carries evidence (an episode id and date, file:line, a measure), an impact/effort estimate, whether
it is easy to undo, and — once applied — **its own target measure** that later rounds verify or flag as a regression.

## Install

Requirements: Claude Code with plugin support, Python 3.9+, git. Optional: `glab` or `gh` (logged in) for the forge
sensor. Tested on macOS; Linux should work (CI runs both).

```
/plugin marketplace add emrahgenc/claude-toolbox-review
/plugin install toolbox-review@claude-toolbox-review
```

Start a new session, then:

```
/toolbox-review:review setup
```

Setup creates `~/.claude/toolbox-review/`, copies the charter template, and asks four questions in plain language
(report language, forge, team repositories to read, how long to keep data). It can also create a weekly unattended round
(report only).

## Use

| Command | What happens |
|---|---|
| `/toolbox-review:review` | A full round: sensors, review agents, report, decision menu, apply what you approve |
| `/toolbox-review:review --scheduled` | Unattended: report only, then a notification. Use this in a scheduled task |
| `/toolbox-review:review decide` | Decide on the latest report later |
| `/toolbox-review:review status` | Ledger and scorecard, no new collection |
| `--monthly`, `--quarterly`, `--scout`, `--since 30d` | Force a deeper round, the external scan, or a window |

Rounds come in three depths (charter §6): **weekly** (used components, short health check, measuring applied changes),
**monthly** (+ full sweep, security, portfolio, process, external scan) and **quarterly** (+ values audit, team view,
charter review). The first round is quarterly.

### The decision menu

Recommendations are shown in everyday words, grouped (security and backup · lighter sessions · skill fixes · tidy-up ·
measurement). Each option says **what changes, what it costs, and whether you can go back**; ids, codes and file names
sit in the option's preview for those who want them. Unticked items wait for the next round; nothing is deleted.

## What it reads and writes

**Reads (local only):** `~/.claude/projects/*/*.jsonl` transcripts and subagent logs, your skills, agents, hooks,
settings, CLAUDE.md and memory folders, the git history of repositories you used with Claude, and — only if you configure
a forge — your own merge/pull requests via `glab api` / `gh api` (GET requests only).

**Writes:** `~/.claude/toolbox-review/` only (config, charter, ledger, run folders with short episode summaries).
Changes to your toolbox happen only after you approve them, one by one; files outside git are backed up first, and
changes inside git are committed path by path.

**Never:** transcript content in web searches or external calls; secret values in any output; writes to team
repositories or organisation plugins (findings stay in the report).

## Configuration

`~/.claude/toolbox-review/config.json` overrides [the defaults](skills/review/defaults/config.json) key by key;
`budgets.json` overrides [the budgets](skills/review/defaults/budgets.json). See
[docs/configuration.md](docs/configuration.md) and [examples/config.example.json](examples/config.example.json).

The **charter** (`~/.claude/toolbox-review/CHARTER.md`, from [the template](skills/review/templates/CHARTER.md)) is the
spine: your values, the 14 areas, budgets, cadence and who decides what. Only you edit it; the review raises "charter
questions" when it thinks something should change.

## How it is built

- **Sensors** are plain Python scripts (`skills/review/scripts/`): deterministic, free, repeatable and closed to prompt
  injection. They write JSON into a run folder.
- **Review agents** (`agents/`) only read (`Read`, `Grep`, `Glob`; the scout also searches the web, without any of your
  data). They return recommendations; they never decide.
- **The main session** synthesises, asks you, applies, commits, and records every recommendation in a ledger with a
  baseline and a target measure.

More in [docs/architecture.md](docs/architecture.md).

## Development

```
python3 -m unittest discover -s tests -v
```

The tests build a synthetic `~/.claude` in a temporary folder and run every sensor against it. Scripts must stay
Python 3.9 compatible and are run with `python3 -I`.

## License

MIT — see [LICENSE](LICENSE).
