# Architecture

Toolbox Review treats your Claude Code setup as a system that needs maintenance, not as a pile of files. It is built
around one loop:

```
 CHARTER  ── your values, 14 areas, budgets, decision rights ── only you change it
    │
 SENSORS  (Python, local, deterministic, no model)
    session episodes · inventory · health · security · git · forge · scorecard
    │      → ~/.claude/toolbox-review/runs/<run>/*.json
 LENSES   (read-only agents, in parallel)
    component-analyst · portfolio-architect · safety-auditor · process-analyst · values-auditor · scout
    │      → recommendations with evidence, impact, effort, door, complexity
 DECISION (main session)
    synthesis → report + scorecard → plain-language menu → apply what you approved (backup, commit)
    │
 MEASURE
    ledger: status history, baseline, own target measure → verified or regressed in later rounds
```

## Why sensors are scripts

Reading thousands of transcript lines with a model would be expensive, slow and open to prompt injection (transcripts
contain pasted emails, logs and review comments). The sensors are plain Python: they produce the same numbers every time,
cost nothing, and treat every text as data. Agents only see their JSON output and the files they are pointed at.

## Episodes — the unit of evidence

An **episode** is everything between one call of a maintained skill and the next call of any command (or a long idle
gap). For each episode the collector keeps:

- **signals** from you only: typed messages, messages typed while work ran, menu answers, interrupts, refused tool
  permissions — each with `n` (how many typed replies came before it; `n=1` is the first reaction) and a first-guess
  class (approval, new work, correction). Signals after the first `followup_window` replies (default 3) are marked
  **late**: once a skill has finished, people often go on to other work in the same session, and those messages would
  otherwise count as corrections of the skill. Late signals stay in the episode for the reviewer; they are not counted;
- **cost**: tokens of the skill's own run (from the call to your first typed reply) and of the whole episode, as an
  input-token-equivalent `cost_index` (input ×1, cache write ×1.25 or ×2, cache read ×0.1, output ×5). The subagents a
  run starts are charged to it from their own transcripts (`<session>/subagents/`, linked by the call that started
  them, nested agents included), because background agents report no tokens to the main transcript. `first_turn_total`
  adds them in: moving work into a subagent moves its cost, it does not remove it;
- **shape**: tools used, largest tool results, repeated reads, errors, models, outward actions (pushes, forge writes) and
  whether an approval came before them.

Episode ids (`yymmdd-hhmm-<session prefix>`) are stable across runs, so a report's evidence can be found again.

## Lenses

| Agent | Areas | Looks at |
|---|---|---|
| component-analyst | D1, D3 | One component: corrections, form conversions, token savings with an equivalence test |
| portfolio-architect | D2, D3, D7, D11, D12 | The whole: overlaps, budgets, memory hygiene, subagent cost, developer experience, team view |
| safety-auditor | D6, D10 | Sorts sensor findings into real risks, false alarms, accepted risks; proposes the right tool for each fix |
| process-analyst | D4, D5, D13 | Flow and output quality of AI-assisted work; approval gates; exam candidates |
| values-auditor | D8 | A sample of components and output against the charter's ten values |
| scout | D9 | Official release notes and docs since the last scan, tied to your components (no personal data leaves) |

All are read only (`Read`, `Grep`, `Glob`; the scout adds web search and fetch). If a plugin agent is not yet visible in
a session, the skill falls back to the built-in `Plan` agent with the agent file as its instructions — never to an agent
that can write.

## The ledger

Every recommendation gets an id and walks through `proposed → approved → applied → verified | regressed`, or
`rejected` / `deferred` with your reason. Rejected ideas are not proposed again without new evidence. When a change is
applied, the review picks **the measure that proves its promise** (for example
`skill:<name>:writes_without_approval` → 0, or `always_loaded_tokens` → below 4000) with a baseline from before the
change. Later rounds compare episodes after the change against that baseline and say REACHED, REGRESSED, not yet, or
"not enough runs yet".

## Exams

Repeated corrections become **exams**: a replayable case (input, expected behaviour) attached to a skill. Before and
after a skill change the review lists its exams; an exam that passed before and fails after means the change is not
done.

## Decision rights

| What | Who decides |
|---|---|
| The charter | You only |
| Your own components | You, per recommendation |
| One-way doors (delete, move, rename, command-blocking hooks, settings) | You, with a separate yes |
| Team repositories | The team — read only, findings in the report |
| Organisation plugins | The organisation — on/off suggestion or feedback draft only |
