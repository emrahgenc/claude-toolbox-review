# Configuration

All settings live in `~/.claude/toolbox-review/` (or the folder in `$TOOLBOX_REVIEW_DATA`). Each file overrides the
plugin defaults **key by key**, so plugin updates never overwrite your choices. Keys starting with `_` are comments.

## config.json

| Key | Default | What it does |
|---|---|---|
| `language` | `"auto"` | Language of reports and menus. `auto` = the language you write in. Instructions and identifiers stay English |
| `maintained.auto_discover` | `true` | Maintain `~/.claude/skills/*`, `~/.claude/agents/*` and plugins that live in `~/.claude/skills/<name>/.claude-plugin/` |
| `maintained.plugin_dirs` | `[]` | More plugin folders you develop yourself (their skills become `<plugin>:<skill>`) |
| `maintained.extra_skill_dirs` | `[]` | Folders that hold `<skill>/SKILL.md` outside the above |
| `maintained.exclude` | `[]` | Skill or agent names to leave alone (as invoked, e.g. `my-plugin:draft`) |
| `team_repos` | `[]` | Team repositories to **read only**: their `.claude/` assets are inventoried and findings stay in the report |
| `process_paths` | `[]` | Paths ignored when judging "fixed after merge" (process or decision records that change on every merge) |
| `ai_trailer_pattern` | `(?i)\b(claude\|anthropic)\b` | Regex on `Co-Authored-By` trailers that marks a commit as AI-assisted |
| `forge.type` | `"none"` | `gitlab`, `github` or `none`. Measures your own merge/pull requests, GET requests only, with your `glab`/`gh` login |
| `forge.host` | `""` | Self-hosted GitLab or GitHub Enterprise host. Empty = gitlab.com / github.com |
| `prod_markers` | kubectl, helm, terraform, "prod … deploy/delete" | Regexes for commands that touch production; the security sensor checks they are guarded by a hook or deny rule |
| `prod_guard_words` | `["kubectl", "prod"]` | Words that identify a guard (hook command or deny rule) for those commands |
| `watchers` | `[]` | Long-running helpers to health-check: `{name, status_file, error_file, max_age_minutes}` |
| `evidence_folders` | `[]` | Folders that should keep receiving files (notes, exports): `{path, max_age_days}` |
| `commit_prefix` | `""` | Prefix for commits the review makes when applying a change (e.g. a ticket id your hooks require) |
| `approval_words` | (built-in English + Turkish list) | Extra words that count as "approved" when you answer a proposal |
| `fix_words` | (fix, revert, lint, typo …) | Extra commit-subject words that mark a commit as a fix |

## budgets.json

| Key | Default | Scorecard row |
|---|---|---|
| `always_loaded_tokens.max` / `.target` | 6000 / 4000 | Text loaded in every session (estimate) |
| `always_loaded_tokens.includes_deferred_tool_names` | `true` | Count MCP/deferred tool names in that estimate |
| `skill_md_lines.max` / `.exceptions` | 500 / `{}` | SKILL.md over its line budget; exceptions as `{"my-plugin:big-skill": 800}` |
| `memory_index_lines.max` | 200 | MEMORY.md over the lines that actually load |
| `regression.corrections_increase_pct` / `cost_increase_pct` | 50 / 20 | When an applied change counts as a regression |
| `run_cost.weekly_max_analyses` / `monthly_max_analyses` | 8 / 15 | Review agents per round |
| `complexity.additions_per_simplification` | 3 | At least one simplification per this many additions |
| `retention_days` | 90 | Episode files older than this are offered for deletion |
| `health.max_fail`, `security.max_fail` | 0 | Allowed FAIL rows |

Keep the charter's §5 and `budgets.json` in step: the charter explains, the JSON is what the scorecard reads.

## CHARTER.md

Copied from [the template](../skills/review/templates/CHARTER.md) at setup. Edit §2 (values) to match the rules you
already follow, §5 if your budgets differ, and record decisions in §10. The review reads it first in every round and
never edits it.

## Data folder layout

```
~/.claude/toolbox-review/
  config.json  budgets.json  CHARTER.md
  ledger.json            every recommendation, its status history, baseline and target measure
  exams.json             replayable cases that guard skill changes
  state.json             what period was last reviewed
  runs/<yyyymmdd-hhmmss>/
    summary.json  inventory.json  health.json  security.json  git.json  forge.json  scorecard.json
    episodes/<skill>.json   short summaries of each run of a skill (deleted after retention_days)
    recs.json  report.md
  backups/<recommendation id>/   copies of files outside git, taken before a change
```

The folder holds summaries of your real work. Keep it out of any repository and off shared drives.
