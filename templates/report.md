# NN — <Title>: <what> on Reddit (part 1)

Work period: <dates>. Result: the matrix and the claims note delivered in chat on <date>,
code and data in `research/<name>/`, technical runbook in `research/<name>/README.md`.

## 1. Why
<The product, the decision to make, why Reddit, what official sources do not give, what
Reddit gives and with which biases. What is in part 1, what is deferred to part 2.>

## 2. Data
| What | How many | Source |
|---|---|---|
| Subreddits | N: <list> | <selection rule> |
| Posts | N over <window> | Arctic Shift archive (free); search cross-check optional |
| Comments | N | Arctic Shift archive |
| Posts after filters | N | see §3 |
| Comments with <entities> | N; labelled M (random share) | see §6 |

Storage: <DuckDB, snapshot, append-only raw, ledgers, resume>.

## 3. Preparation
1. Entity dictionary: <sources, matching, recall / false positives>.
2. Prefilter: <rule, how many passed>.
3. Duplicates, spam, bots, deleted: <numbers, local, no model>.
4. Unit = person: `author_hash`; one person, one vote.

## 4. Labelling posts
### 4.1 Pilot and vocabulary (<models>, schema vX)
<Sample read, vocabulary proposed, what the pilot found, decisions.>
### 4.2 Full run (<model>, schema vY)
<Fields and definitions. The evidence rule and the quote validator. Gold standard: who
reviewed, where they disagreed, per-field results. Full run: volume, windows, cost,
failures. Spot checks.>
Result: <distribution of kind, segments, flags>.

## 5. Presentation decisions after the methodology research
<The segment-size problem. Sources consulted. Rules adopted: no "All", matrix, groups,
thresholds, card, what is not shown, audience size; no dynamics.>

## 6. Comments
<Prompt, check on 50, model choice, run: volume, cost, window, result.>

## 7. Results
- People in total N; <groups> …
- **<Group 1> by segment**: …
- **<Group 2>**: …
- <Second-level entities>: …
- Reliability: <bases per row, where solid, where hints>.
- `unknown` = <who>; read as <…>.

## 8. Limitations
<Language and subreddits ≠ the world; share without text; share with a known origin;
share of comments labelled; the model under-counts (recall) and does not invent;
boundary noise.>

## 9. Cost and limits
<Posts, comments, agents; windows; API-equivalent.>

## 10. Backlog
<Owner decisions → next steps; remaining data; comparisons; parked tools.>
