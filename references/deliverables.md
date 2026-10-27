# Deliverables

## 1. Results in chat

The result is delivered in the chat answer: no HTML page, no published artifact.

- Headline numbers: people in data, people per group, window, subreddits.
- A table per group (for example Considering · Using · Advice, or Practices · Pains):
  the matrix segments × top-N entities as a markdown table with the n of each row, and
  under it the second-level entities with the line "named by only N% of people".
- The claims-and-limits note (`statistics.md` §5) in plain words: who is in the data,
  what the prefilter keeps and its recall, the labeller's error rate with its sample
  size, thresholds and what a thin cell means, that a person can be in several cells,
  who is excluded.
- No quotes, no usernames. Every number names the file it comes from (`report_data/`).

## 2. Review pages

`scripts/build_review_page.py` renders a sample with labels and highlighted quotes;
reviewers mark ok / bad per field, add notes, export JSON. Works from a file in a
browser without Claude. Marks are kept as JSON in `report_data/` with the sample; the
sample itself holds text and quotes, so it stays out of a public repository.

## 3. Report

`templates/report.md`, in the project's docs folder when it has one, else
`out/report.md`: why; data; preparation; labelling (pilot, audit, relabel, gold,
full-run numbers); presentation decisions with the sources consulted; comments; results;
the claims-and-limits note; cost; backlog. Every number names its file.

## 4. Runbook (`<study root>/README.md` or `out/runbook.md`)

Pipeline table (stage → scripts → output), commands (run, resume, loop, re-ground,
aggregate, self-tests), the headless call, counting rules, model/prompt
version table, what is gitignored, backups. Written so a new session can answer "where
did we stop?" without the chat.

## 5. Project rules

When the study lives inside a project that keeps agent instructions (a `CLAUDE.md` or
similar), add: plan first; paid runs only on command; Sonnet agents; nothing invented;
data never lost; honest statistics; report as is. When the project keeps a history file,
append a dated entry per session: decisions, numbers, what is pending.

## 6. Git hygiene

Commit: scripts, versioned prompts, `report_data/` matrices and summaries, docs.
Keep out of a public repository: `*.duckdb`, `raw/`, `raw_llm/`, `logs/`,
`state/salt.txt`, items and review files (they hold text and quotes), large CSVs.
Before ending a session: no stray temp files, nothing from the research only in the chat.

## 7. Backlog shape

Owner decisions pending (which segment and entities to build for), the next part
(pains in the chosen entities from flagged items), remaining unlabelled data with its
cost, deferred comparisons, tools considered and parked.
