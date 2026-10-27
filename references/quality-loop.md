# Quality loop

Each turn is cheap next to a full run; skipping one costs a relabel of everything.

## 1. Pilot → validate → first matrix

- Pilot on two models, every field graded by hand against the source;
  `scripts/compare_runs.py` lists the disagreements per field and per slice. Findings
  become prompt rules; re-validate on a fresh sample; label the recall control and read
  `recall_check.py`; then a mini-check on never-used items plus two resilience tests (a
  real SIGTERM mid-run, a mocked usage-limit message). Only then the checkpoint of 500.
- Build the first matrix as soon as a first full pass exists. Numbers in a table expose
  logical errors that tables hide.

## 2. Audit by a fresh strong model

Ask a stronger model than the labeller to review the matrix, the prompt, the aggregation
code and the data, every claim backed by a query or a sample of stated size. Four lists:

- **A. Wrong numbers and claims**: a status that does not mean what the matrix says, noise
  shown as a trend, code bugs in the aggregation, comparison numbers absent from sources.
- **B. Biases**: segment size = subreddit size, pooled rankings skewed by one segment,
  list inflation, an attribute known unevenly across subs, small-n cells,
  indistinguishable ranks shown as an order, a prefilter whose missed texts have a
  different segment mix, slices sampled at different rates without weights.
- **C. Labelling quality**: "manual check" done by a model, samples too small for the
  claimed accuracy, validator evaluated on its own tuning cases, quotes that prove
  presence but not meaning, undefined labels.
- **D. Technical**: deleted accounts counted as one person, double counting across
  groups, partial last week, a changed salt between exports, a coverage step read as
  growth.

The owner reads the list and decides per item; fix everything accepted in one pass.

## 3. Gold standard with humans

- 50 items: 40 from the labelled population stratified by subreddit (floor 3 per sub,
  forced hard cases: title-only, the disputed status), 10 from the "not relevant" pool to
  measure misses. Seeded; exclusion lists for every earlier pilot id.
- `scripts/build_review_page.py` renders them with the labels and highlighted quotes;
  each field gets ok / bad and a note; export JSON. It must work for a colleague without
  Claude (open the file, mark, export, paste the JSON in chat).
- Owner, colleague and a model review independently; a second model cross-checks.
- Expect disagreement on **definitions**, not reading. Resolve each with the owner in one
  paragraph, write the rule and an example into the prompt, bump the version.
- `scripts/gold_score.py` turns the exported marks into errors out of n per field, the
  notes (where misses live) and, with several reviewers, agreement per field: agreed ok,
  agreed bad, split. A split is a definition disagreement. Note the noise floor: boundary
  items that flip between identical runs.

## 4. Relabel, then spot-check on fresh items

- Relabel the whole population with the new prompt and a new model tag.
- After every prompt change, 30 random never-used items reviewed by two models:
  fabrications, origin errors, misses. Re-run subsets that a fix targets (for instance
  title-only posts after adding their examples) and report how many flipped.
- Comments: 50 random, then 30 + 30 after fixes. Report precision and recall separately
  and say which way the errors go.

## 5. What to write down

- Every audit as `out/audit-<date>.md` with the A–D tables and the decision per row.
- Gold and spot samples as JSON in `report_data/` with each reviewer's marks.
- Accuracy with intervals: "0 of 30" is "under 10% at 95%"; 50 items give about ±14
  points on a 50% share; a claim like "98%" needs 100–200 items.
- The validator's false-reject rate, measured on fresh items.
