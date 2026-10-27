# Exploring the data and defining the vocabulary

Labels are not decided at the start. They are read off the data, proposed with
examples, confirmed by the owner, and only then written into a prompt.

## 1. Draw samples and read them

Every export names its slice and, on the first export of a study, creates the salt:

```
uv run scripts/export_items.py --root research/<name> --kind posts --sample 200 --seed 1 \
    --slice uniform --out items/explore_posts.ndjson --md items/explore_posts.md --new-salt
uv run scripts/export_items.py --root research/<name> --kind comments --sample 150 --seed 1 \
    --slice uniform --out items/explore_comments.ndjson --md items/explore_comments.md
```
`--md` writes the same items as readable Markdown; read that one. The script prints
what it excluded (deleted authors, bots, removed bodies, an author's duplicate posts)
before the population size, and the salt fingerprint.

When the scout's probe showed the behaviour under about 10% of a sub, a uniform sample
of 200 holds a dozen on-topic texts and the vocabulary would be built on them. Draw a
prefiltered sample per behaviour family instead (its seed phrases in `--sql-where`,
about 50 items, its own seed and slice name), plus the uniform sample as the control:

```
uv run scripts/export_items.py … --sample 50 --seed 1 --slice <family> \
    --prefilter-file state/prefilter_<family>.txt \
    --out items/explore_<family>.ndjson --md items/explore_<family>.md
```
A pooled sample across families follows the biggest family, and the vocabulary of the
others gets built on five texts. Keep each family's regex in `state/prefilter_<family>.txt`
(one line, RE2-compatible: no lookbehind; apostrophes are fine, the file is bound as a
parameter, never pasted into SQL): the same file goes into `--prefilter-file` now and
into `recall_check.py --regex-file` later, and several files at once give the union of
the families. `--sql-where` stays for conditions that are not a regex.

Read all of it. If the sample is too long for one pass, send it to a Sonnet subagent with
an open task ("for each text: is it the author's own case, who is the author, which
entities or practices, what status, what goes wrong, what else recurs") and read its
table. No schema yet.

## 2. Propose the vocabulary

From what the texts say, write (the template's kinds are a starting point, not a limit:
a study may add kinds or rename them when the data needs it, as long as every kind maps
to "counts as the author's own case" or not, and `aggregate_matrix.py --own-values` is
told which ones count; prefer a status or a flag over a new kind, and leave "off-topic"
to the prefilter):

- **kind**: what counts as the author's own case (plans, doing it, has done it; a wish
  or a doubt counts), what is someone else's case or general talk, what is promo. Note
  the borderline forms seen in the sample (a title with no body, a question to readers,
  a short case, a finished case).
- **segments**: the ways people describe themselves that recur and matter for the
  product. Each with a one-line definition and the look-alike that is not it. Always an
  `unknown` for texts that do not say. A profession, a demographic or a wish alone is
  not a basis for a segment unless the question is about exactly that. A segment must
  not restate the thing being counted ("current method" as the segment of a study that
  counts methods is circular): use a situation the method does not define, such as life
  stage, income band, household, the goal behind the behaviour.
- **entities and statuses** (choice questions): what the question counts (products,
  places, features) and the states that matter (considering / using / past / rejected,
  or the domain's words: bought / returned, enrolled / dropped). Decide whether entities
  are a closed set that a dictionary can match locally.
- **practices and pains** (practice questions): the methods, tools and formats people
  name, and a closed list of 6–12 pains, each with a definition and its look-alike. A
  pain the list lacks goes into the next prompt version, never into free text.
- **flags**: binary facts worth a column (asks for help, reports an outcome, mentions a
  price, recommends for/against). Few.
- **attributes**: a stated fact the question needs beside the segment (where from, what
  used before, budget band); define what does not count.
- **intent phrases** for the prefilter: the verbs and nouns that mark the behaviour in
  the sample ("switched to", "which X", "looking for", "been doing X for").

Show the owner one table: label, definition, two real examples from the sample, the
boundary case. Resolve each boundary in one paragraph. → go.

## 3. Entity dictionary (when entities are a closed set)

- Build `entity_dict.csv` (`alias,canonical,kind`): canonical names plus aliases,
  abbreviations, demonyms, misspellings seen in the sample. Start from a catalogue, a
  store listing or a public gazetteer, whichever the entity kind needs.
- `scripts/entity_match.py --dict entity_dict.csv --items items.ndjson --out hints.ndjson`
  matches on word boundaries, longest alias first, and requires a capital letter for
  names that are also common words (`--common-words`). A hard-exclude list handles
  function words, acronyms and month names that are also real entities.
- Run a discovery pass: capitalised phrases after the intent verbs that the dictionary
  does not know, ranked by frequency; add what recurs. Audit precision (100 random
  matches) and recall (200 texts with an intent verb and no match) with samples; fix,
  re-audit, until false positives are in the low single digits.
- The matches become HINTS in the prompt ("can be wrong or incomplete, verify") and a
  recall check on the model later.

## 4. Prepare locally (no model)

What the exporter does on every run, and what stays a decision:

- **Prefilter** = the slices. A text is in the population when it matches a family's
  phrases (`--prefilter-file`) or an entity match (`--ids-file` from `entity_match.py`), or
  the family is whole-sub. **A product's or an answer's own sub is always whole-sub**:
  posts inside r/<product> rarely name the product ("desktop slow again"), so phrases
  catch a third of them at best; phrases are for the general subs. The exporter's
  population line with the union of the files is "the texts the prefilter keeps"
  (`recall_check.py --prefilter-count`). Its recall is unknown until the control is
  labelled (§5).
- **Dedup** (default): the same author with the same normalised title and opening of the
  body keeps the earliest copy; cross-posts and reposts count once. `--no-dedup` keeps
  them. Marketing accounts are not detected locally: that is the model's "promo" label,
  and promo is excluded at aggregation.
- **Bots**: AutoModerator, names ending in "modteam", and names whose tokens include
  exactly "bot" (RemindMeBot; "Bottle" stays human). Single-token bots seen in the sample
  (sneakpeekbot) go into `state/bots.txt` and `--bots-file`.
- **Deleted authors** are removed entirely: they cannot be one person. Heavy deleters
  are therefore under-counted; the claims note says so.
- **Weights**: every item carries `weight` = population / drawn (1.0 for a full slice).
  Slices drawn at different rates aggregate correctly without further work.
- **Comment population**: comments with an entity match, plus comments without one but
  with a first-person marker in threads that pass the prefilter. Then
  `export_items.py --permutation state/comments_permutation.json --limit N` fixes a
  seeded random order so any prefix is a uniform sample and "N of M labelled" is honest.
- **Title-only** posts stay in, marked, with the note "(title-only, body removed or
  empty)" as their text.

## 5. The recall control

The prefilter defines the population, so every share in the matrix means "among people
whose posts the prefilter kept". The control measures what it misses against the
population the matrix itself describes, with the matrix's own counting rules, so the
denominator is never a choice:

- **base recall**: of the own-case texts in a uniform sample, the share the prefilter
  caught (the matrix's per-segment base);
- **cell recall**: of the own-case texts with at least one counted item (an entity in a
  counted status, or an entry of a counted list), the share caught (the texts that fill
  cells). A gap between the two means the prefilter catches people who name things and
  misses people who only describe or complain, which shifts the shares.

Procedure, in this order:
1. Export the control: a uniform sample, `--slice uniform`, sized for about 30 caught
   cell texts (30 / (cell share × expected recall); the script says how many more it
   needs; a 5% share at recall 0.5 means about 1,200 items).
2. Freeze before labelling: the prompt version, the regex files, the counting flags. They
   are printed with every result; changing any of them after the number is seen is a new
   prompt version and a new control.
3. Label the control with the **model chosen for the full run** and the final prompt,
   never with the cheapest one alone: a weak labeller marks neighbouring-topic posts as
   own and inflates both denominators (in one test the small model called 45 of 300 posts
   own; a hand read found about 17 to be the behaviour).
4. Run the check with the aggregator's flags:
   ```
   python3 scripts/recall_check.py --items items/control.ndjson --results out/<tag>_grounded.ndjson \
       --regex-file state/prefilter_<family>.txt [more files = the union] --whole-subs <product subs> \
       --slice uniform --groups <statuses> | --extra-group <name=list@key> [--extra-own-only] \
       --population <corpus after exclusions> --prefilter-count <texts kept> --out out/recall.json
   ```
5. Read the missed cell texts the script lists (30 or all), by hand, and sort them: truly
   the behaviour → the words they use go into the phrases, or their sub becomes whole-sub;
   not the behaviour → a numbered negative example in the next prompt version. Then
   re-export, relabel the control once, and report recall before and after.

Decision: widen the prefilter when the cell recall's upper bound is under about 0.6 or the
caught and missed segment mixes differ. A whole-sub family has recall 1 by construction.
Both figures, with their intervals, the prompt version and "as labelled by model M", go
into the claims note.
