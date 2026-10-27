# Statistics and presentation

## 1. The counting frame

Fix these four lines before the first number exists.

- **Unit = person.** `author_hash` over posts and comments together. One person is one
  vote per entity per group, however many texts they wrote. A person is in a segment's
  base only with an own-case text; people the prefilter kept whose texts are all general,
  promo or someone else's case are reported outside the base, never inside a row.
- **Groups** are named explicitly: the statuses that count (for example considering /
  using) or the lists that count (practices, pains). Past, rejected, promo and not-own
  are collected but not counted; they stay in the data for later questions.
- **Segments** are what a person states about themself: the majority of stated segments
  across their texts, `unknown` otherwise. Never from the subreddit, never from the
  thread they answered in. Ties keep the first stated; a person with one text is as
  reliable as that text.
- **The main view is the matrix segment × entity.**

## 2. Segments are never pooled

Which subreddits were chosen decides how many people of each segment are in the data.
Any number that depends on segment size (a pooled "All" ranking, "segment A is the
largest", "X% of people want Y") measures the subreddit mix, not the market.

What survey practice says: post-stratification weighting needs population shares that
do not exist for most segments and does little on self-selected samples; treating
subgroup sizes proportional to data volume as population shares is the textbook error;
equal weights per group (macro-averaging) is an accepted convention when group sizes
are not meaningful. Options rejected: pooled ranking, equal-weight-per-subreddit (still
pools), reweighting to external shares (no shares exist).

Rules (`scripts/aggregate_matrix.py`):
1. No "All". Every share is within a segment: "share of the segment's people who named
   the entity".
2. Matrix: rows = segments, columns = top-N entities by the mean of per-segment shares
   (an ordering device, never displayed). Entity levels (country vs city, product vs
   feature) get separate matrices.
3. "Top-3 together" per row = sum of the three largest cells: concentration of demand.
   "People in data" = the row's base.
4. Thresholds: row from 30 people; cell from 10 (else empty); 30 and more is a
   measurement, 10–29 is marked "a hint, not a measurement". These are conventions, not derived
   limits: a cell of 10 people in a row of 30 carries an interval of about ±16 points, a
   cell of 30 in a row of 100 about ±9. Say so in the methodology note.
5. Percent of people; a person naming three entities counts in three cells, and the note
   says so.
6. **Sampling weights.** When slices were drawn at different rates (every text of a small
   family, a 10% sample of a big one), each item carries `weight` = population / drawn
   from the exporter, a person gets the smallest weight among their items, and shares are
   weighted sums over weighted bases. Thresholds stay on plain people counts. The summary
   says whether weights were in play.
7. Entity card: share of each segment that named it; an attribute with "known for X%";
   sub-entities; advice.

## 3. Things that inflate or distort

- **Lists inflate votes**: people who name many entities give most of the votes. Show
  full votes; run fractional votes (1/k) as a sensitivity check; if the order changes,
  say so.
- **Attributes** (where from, what used before) are stated by a minority and unevenly
  across subs, and mix categories. Report "known for X%", compute within segment,
  collect separate fields when the distinction matters.
- **Ranks within the margin** are not an order. Show groups or intervals.
- **Advice ≠ experience**: a recommender rarely states they use it. Keep "recommend" as
  its own group; never merge into "using".
- **Thread segment** (the post author's segment applied to commenters) is a guess; leave
  commenters who do not say `unknown`.
- **Deleted accounts** are many people; exclude, never count as one. People who delete
  are then under-represented.
- **Double counting**: a person in two groups is fine within a group and wrong in a sum
  of groups.
- **Labeller error lands in the cells.** A cell of 10 people with a 10% labelling error
  rate holds one wrong person on average; the gold standard's per-field error rate is
  read next to the small cells, not only in the appendix.

## 4. Dynamics is not a deliverable

A 12-month window split in two halves is weak evidence: activity swings with the
season and with the subreddit's own life, and a coverage step in the archive (one sub
indexed from a certain month) looks like growth. The scheme does not compute trends. If
the owner asks, the honest answer is a share of active authors per half-year with a
two-proportion test and a seasonality check, never raw period counts, and never for a
sub with a coverage step (the loader and the scout flag those).

## 5. What the study can and cannot claim

The chat answer and the report carry this note, filled with the study's own numbers:

- **Who**: people who posted in these N subreddits in this window, in this language.
  Not the market, not all of Reddit.
- **What the prefilter keeps**: the phrases, the whole subs or the dictionary, the share
  of the corpus kept, and the two measured recalls from the labelled uniform control with
  their intervals: base recall (own-case texts) and cell recall (texts with a counted
  item), "as labelled by model M", prompt version named. Every share is "among people
  whose posts the prefilter kept"; the control says how many it misses and whether their
  segment mix differs.
- **Labelling**: one model, a verbatim-quote check that catches invented facts but not
  wrong statuses or misread meaning; the gold standard's per-field error rate and its
  sample size; the noise floor between identical runs.
- **Excluded**: deleted authors, bots, an author's duplicate posts, promo. Alt accounts
  count as separate people; shared accounts as one.
- **Thresholds and intervals**: the conventions in §2.4 and the interval width for the
  study's typical cell.
- **Privacy**: authors are salted hashes; the results show no usernames and no quotes.

## 6. External numbers

- Audience size in the world: an order of magnitude from named sources, never a
  comparison with Reddit shares.
- Comparing Reddit with official statistics: same population only (Reddit's origin mix
  is not the world's), one source per table, one definition, n ≥ 30 per row, and a label
  only when both numbers exist in a source. If a number cannot be found in a source, the
  row goes to the backlog.

## 7. What the results do not show

Pooled totals, segment shares as a result, quotes, intervals in the table (tooltip
instead), columns on single digits, sub-30 rows, period charts, a thread-derived segment,
usernames.

## 8. Wording of reliability

- Large bases per segment → reliable; small segments → hints.
- When an entity level is named by a minority (a brand named by a third of a row's people), write it
  under that matrix: "named by only N% of people".
- `unknown` = title-only texts and people who do not say who they are; keep it visible
  and explain how to read it.
- Model error: state precision and recall separately and which way the errors go;
  boundary noise of a few percent; the chosen subreddits and language are not the world.
