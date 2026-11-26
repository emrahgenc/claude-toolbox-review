# Changelog

## Unreleased

Measurement fixes that the improvement loop depends on: a change can only be judged if its cost and its corrections
are counted right.

### Fixed

- **Subagent cost read as zero.** Background agents, the default in recent Claude Code versions, report no tokens to
  the main transcript, so every skill's subagent cost was missing. Cost now comes from each subagent's own transcript
  (`<session>/subagents/`), linked by the call that started it, nested agents included. Episodes gain
  `tokens.subagents`, `first_turn_total` and `episode_total`; summaries gain `first_turn_total_cost_median`,
  `subagent_runs`, `subagent_cost_median` and `subagents_unresolved`. The component analyst now judges "move this
  work into a subagent" on the total, because a subagent moves cost rather than removing it.
- **Other work counted as corrections.** Once a skill had finished, later messages in the same session about
  unrelated work counted as corrections of it. Only the first `followup_window` typed replies (new setting, default 3)
  count now; later ones stay in the episode marked `late`, for the reviewer.
- **Old baselines compared with the new count.** A ledger target whose baseline was counted under the old rule is
  recounted from its baseline run (or flagged when that run is gone), and the generic check says when corrections are
  not comparable, so the new rule is never reported as an improvement.
- **Plugin skills could not be measured.** `metrics.py` rejected skill names containing `:` (`plugin:skill`).

### Changed

- Collector table: new `corr` (counted, late ones in brackets), `all/med` (run plus subagents) and `sub` columns; the
  `deny` column is gone (denials still count as corrections).
- Scorecard "costliest skills" uses the cost with subagents.

## 0.1.0 — 2026-11-26

First public version.

- `/toolbox-review:review` with `setup`, full, `--scheduled`, `decide` and `status` modes; weekly, monthly and quarterly
  round depths.
- Sensors: session episodes (corrections, interruptions, approvals, outward writes, token cost per run), inventory and
  every-session load, health and continuity, security (permissions, production guards, secrets — values never shown),
  git (AI share, fixes after merge), forge (GitLab or GitHub, read only), scorecard, retention.
- Review agents: component-analyst, portfolio-architect, safety-auditor, process-analyst, values-auditor, scout.
- Ledger with a recommendation life cycle, per-recommendation target measures, regression checks and exams.
- Plain-language decision menu; reports in the configured language.
- Charter template with ten values, fourteen areas, budgets and decision rights.
