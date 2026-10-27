# Pitfalls

Each entry: what you do → what happens → what to do instead.

How to use this file: read the phase's section before the phase. When a run fails or a
number looks wrong, grep here for the symptom (error text, status code, count) before
changing anything; the scripts print `HINT:` lines pointing at the entries they recognise.
A failure this file did not predict gets an entry right after the fix.

## Collection
- **Collect through Reddit search only** → every query stops at 252 results; a year of a
  busy sub is missing and nobody notices → use the archive; search only for the last
  days and cross-checks.
- **Skip the User-Agent** → 403 on every request → UA set.
- **Retry a 422 "slow down" / 429 with a backoff ladder** → the archive meters a fixed
  60 s window; retries inside it burn a quarter of the requests and look like
  "the backoff recovered" → read `X-RateLimit-Reset`, pause every worker until it, count
  the pauses (both scripts do).
- **Bin days in the machine's timezone** → a post at 23:59 UTC lands in the next day; the
  gap check flags a "partial last day" that does not exist → `SET TimeZone='UTC'` in
  every DuckDB session (the loader does).
- **Pull a 60-day window to save time** → the window sits in the season's low or peak
  and the counts and the vocabulary carry it → 12 months, ending a week before the
  pull; read the scout's peak and low months.
- **Treat the archive's last days as complete** → the last days look like a drop in any
  count by day → exclude the current day from checks and end the window a week early.
- **Mix id formats** (archive bare ids vs search `t3_` ids) → the same post twice, dedup
  silently broken → normalise ids once in the loader.
- **Drop orphan comments** whose post is outside the window → lost first-person
  statements → keep them, report the count.
- **Scout keyword density with the archive's full-text search (`query=`)** → sitewide it
  is refused (HTTP 400: needs `subreddit` or `author`); per sub it spends the rate window
  in a few calls; `aggregate` with a keyword can answer 0 for a true 76 → download the
  last 14 days by plain listing and match locally (`subreddit_scout.py --probe`).
- **Scout through Reddit's own `search.json` / `new.json` without auth** → 403 → that
  route exists only through treg (paid).
- **Pick subs by size, name and description** → the exactly-named sub is dead and the
  big adjacent sub looks off-topic; 14 minutes of scouting produce the wrong list →
  probe the behaviour: on-topic authors per year from the last 14 days of posts.
- **Judge a sub by the share of posts with the seed phrases** → a 2% slice of a big sub
  (hundreds of people a year) is dropped, and a sub whose own vocabulary differs (its
  own tags and abbreviations) looks empty → rank by projected on-topic authors per year;
  read the random titles for whole-sub cases.
- **Bundle several behaviours into one question** → each family lives in different
  subs, the one without a home is silently represented by its neighbours → split into
  families at phase 0, one family per sub, families never pooled.
- **Pass `--require` together with explicit `--names`** → the regex filtered the named
  subs too; 11 big generic candidates vanished with no line in `not_found` → `--require`
  applies to prefix hits only (the scout does now); a name you typed is always measured.
- **Probe the N most active candidates (`--probe-top`)** → in a prefix search those are
  the noisiest giants (subs that share the prefix, not the topic); they hit the page cap
  in two days and 50 minutes buy 3 useful probes → `--probe-names` with the shortlist;
  `--probe-pages 3` for giants; the probe numbers rank, they do not plan.
- **Put a product's or a program's name in the seed phrases** → the sub named after it
  (a spreadsheet sub for "excel", a product sub for its product) tops the ranking at a 60%
  share that measures the name, not the behaviour → read the scout's phrase-hit line and
  its name-collision notes, read the on-topic titles of the top subs, drop the word.
- **Trust the probe's authors/year for a giant sub** → `--probe-pages 3` covers a day; the
  projection multiplies one day by 365 → the `days` column and the `~` mark say so; rank
  with it, never plan with it.
- **Pull sub by sub under a time cap** → one sub gets 12 months, the other five nothing,
  and the families without text cannot be explored → the archive pulls round-robin,
  newest window first (default `--order roundrobin`): after the first hour every sub
  has its latest months; stop any time, the same command resumes.
- **Read a half-pulled window as "its newest days are in"** → pages run oldest to newest,
  so the stopped window has its OLDEST days and lacks the newest → the loader prints
  `covered … missing …` per unfinished window; the capped run leaves each sub its newest
  complete months plus one partial older one.
- **Estimate the time left from windows done** → a giant's window is 50 pages, a small
  sub's is 2; "0.4 h left" at 28 minutes became 60% of the giants unpulled at the stop →
  `--scout out/scout.json` gives a page-based estimate and ETA.
- **Read the loader's gap flags on an unfinished pull** → the window still in progress
  shows as 24 "gap" days → the loader reads the ledger and lists unfinished windows
  separately; a flagged day counts as a gap only inside a `done` window.
- **Treat hundreds of gap days on one small sub as a broken pull** → one small sub
  showed 350 flagged days with every window `done` on a single page: the archive holds
  2–10 posts a month for it before one date and hundreds after → the loader and the
  scout now flag a coverage step; check the ledger pages and the monthly counts before
  re-pulling; keep the sub for counts, never compare its periods.
- **Lose or regenerate `state/salt.txt` between exports** → the same author gets two
  hashes, every person splits in two and the people counts inflate with no error → the
  exporter refuses to run without a salt unless `--new-salt` is given once per study, and
  prints the salt fingerprint; keep the file out of git but never delete it.
- **Pull with a different `--end` on a resumed study** → the window extends silently and
  the last month is half-indexed → one `--start`/`--end` per study, written in the runbook.

## Headless model calls
- **Use the default `claude -p` invocation** → tens of thousands of tokens of
  interactive context per call; a run costs many times more than the texts justify →
  the minimal flags in `claude_batch.py`.
- **Leave thinking on for the small model** → thousands of thinking tokens, then the
  call exits with an error → `MAX_THINKING_TOKENS=0`; never with `--effort`.
- **`--max-turns 1` with `--json-schema`** → the schema pass takes extra turns and the
  call fails with `error_max_turns`, misdiagnosed as a thinking problem → `--max-turns 4`.
- **Put the instructions in the user turn** → nothing is cached; the full prompt is
  billed every call → instructions in `--system-prompt`, batch in the user turn.
- **Scan every response for "limit"** → a post quoting "my 90-day limit" stops the run
  → scan only responses without model output.
- **Retry at the next window after a weekly-limit message** → the whole night is lost
  to failed starts → a weekly message stops everything.
- **Leave `CLAUDECODE` in the child environment** → the nested CLI refuses to run.
- **Send the whole item object to the model** → subreddit, author_hash, slice and weight
  travel with every text while the prompt says "never infer from the subreddit" → the
  runner sends `--send-keys` only (id, title, text, hints, post_title).
- **Copy a model name from the docs** → the CLI rejects a name that was renamed since →
  check the names the CLI accepts today before a run.

## Prompts and labelling
- **Leave a label undefined** ("segment: casual") → the model and three humans each mean
  something else; the gold standard disagrees on definitions, not reading → define
  every label with its boundary case and an example.
- **Apply "a wish is not a basis" to the kind** → every "I dream of…" post is dropped as
  not-own → the rule applies to the segment only.
- **Give no title-only examples** → every title-only post is labelled "not about it" →
  two examples of title-only cases.
- **Let comments inherit the post's facts** → commenters "live" where the post author
  lives → quotes from the comment body only; `from_post` flag for "there / here".
- **Say nothing about short first-person statements** → the model skips "I've only
  lived in X" buried in long comments → say explicitly that passing statements count.
- **Use one status for "has experience of"** → "uses now" and "used once" are mixed and
  the matrix says "uses it" → considering / using / past / rejected.
- **Return every field for comments** → a third of the cost is nulls → include a field
  only when it has a value.
- **Present hints as facts** → the model copies wrong hints → "hints can be wrong or
  incomplete, verify against the text".
- **Expect identical runs to agree** → boundary items flip in both directions → state
  the noise floor instead of chasing it.
- **Build the vocabulary from a uniform sample of a low-share corpus** → 12 on-topic
  texts define every label; the first labelling run meets cases nobody saw → 150
  prefiltered plus 50 uniform as a recall control.
- **Prefilter one pooled sample across several families** → the biggest family takes
  95% of the 150 and the others get five texts each → one prefiltered sample per
  family (that family's phrases in `--sql-where`, about 50 items, its own seed and
  `--slice`), plus the uniform control.
- **Choose the recall denominator by hand** → in a sub whose own case is wider than the
  behaviour (every own money post vs own tracking) recall on `own` comes out at 16% and
  means nothing, and a definition picked after seeing the number is a crutch →
  `recall_check.py` derives two figures from the aggregator's own counting flags (base
  recall on own-case texts, cell recall on texts with a counted item), frozen with the
  prompt version before the control is labelled.
- **Prefilter a product's own sub with phrases** → posts in r/<app> say "desktop slow",
  not the app's name; recall there is a third at best → product and answer subs are
  whole-sub; phrases are for general subs.
- **Label the recall control with the cheapest model** → it marks neighbouring-topic
  posts as own and hangs pains on general money stress; both denominators inflate and
  recall reads 16–26% where a hand read gives about 40% → the control is labelled with
  the model chosen for the full run, and the missed texts are read by hand and sorted into
  "widen phrases" and "prompt example".
- **Make the segment the thing being counted** → "current method" as the segment of a
  study that counts methods: every cell is tautological → a situation the method does not
  define (life stage, income band, household, goal).
- **Draw 50 uniform items as the recall control whatever the on-topic share** → at a
  2% share that is one on-topic text, and the prefilter's recall is a guess → size the
  control for about 30 on-topic texts (30 / expected share), label it with the final
  prompt, run `recall_check.py`; a whole-sub family needs none.
- **Pick the model on a two-point agreement gap in a 50-item pilot** → the gap is inside
  the noise of one run against itself → `compare_runs.py`, then choose on error types
  (invented names, own vs not-own) and cost.

## Validator
- **Null only the evidence on a failed check** → ungrounded labels survive with no
  quote → null the value too.
- **Accept only a single contiguous quote** → the model fabricates a stitched quote or
  drops the fact → 2–3 verbatim fragments joined by " ... ", any order.
- **Compare raw strings** → curly quotes, markdown, contractions and spacing reject
  true quotes → normalise both sides; never merge look-alikes ("were" / "we're").
- **Check entity names without aliases** → "UK", "Deutschland", demonyms and codes are
  rejected → an alias table from the dictionary; apply the capitalisation gate to the
  alias that matched.
- **Ignore superstrings** → "America" inside "South America" grounds the wrong entity →
  a superstring guard and a URL mask.
- **Evaluate the validator on the cases it was tuned on** → a false-reject rate that
  is a training score → measure on fresh items.
- **Run `--name-keys name` while the pain list also keys its label as `name`** → every
  pain is rejected (a label like no_results is never inside a quote) → the entity list
  keys its name as `name`, the pain list as `pain`; name checks hit entities only.
- **Null a rejected enum field** → `role: null` is outside the schema and the aggregation
  skips the person → `--unknown-keys role,segment,occasion` writes "unknown".
- **Expect the name check to stem** → "cash envelopes" vs "cash envelope", "bank app" vs
  "my bank's app" are rejected (2 of 50 in one pilot) → aliases carry plurals and
  possessives; the prompt asks for the text's own words; read the rejects file after the
  pilot and extend the alias table, never relax the check.

## Statistics
- **Show a pooled "All" or compare segments by count** → the subreddit mix is reported
  as the market → shares within segment only, matrix as the main view.
- **Let every labelled person into the segment base** → general, promo and other-person
  posters land in the `unknown` row (299 of 300 on one control where 45 were own-case);
  the row's shares are diluted and "people in data" is inflated → the base is own-case
  people only; the rest are reported outside it.
- **Count votes, not people** → people who list many entities dominate → one person,
  one vote per entity per group; fractional votes as a sensitivity check.
- **Report an attribute (where from, what used before) without its coverage** → a figure from a minority
  presented as everyone's → "known for X%", computed within segment.
- **Chart raw period counts or call a half-year difference a trend** → activity swings,
  seasonality and archive coverage steps look like growth → dynamics is not a
  deliverable; if the owner insists, shares of active authors per half-year with a
  two-proportion test and a seasonality check, never for a sub with a coverage step.
- **Aggregate slices drawn at different rates without weights** → the fully drawn small
  family outweighs the 10% sample of the big one in every within-segment share → the
  exporter writes `weight` = population / drawn on every item and the aggregator uses it.
- **Run the aggregator with a default group list** → statuses from another study's
  schema are counted, or nothing is, with no error → `--groups` / `--extra-group` are
  named explicitly every time; the script refuses to guess.
- **Count pains or practices from every row** → pains of general posts and promo land in
  the author's pains → `--extra-own-only` for lists that describe the author; every-row
  counting is for advice lists only.
- **Fill cells with single digits** → the heatmap is noise with colour → cell ≥10,
  fill ≥30, row ≥30.
- **Infer a commenter's segment from the thread** → wrong in about a third of cases →
  `unknown`.
- **Merge advice into experience** → recommenders become residents → separate group.
- **Compare with official statistics across populations** → the biggest origin group's
  own country becomes "invisible" → same population, one source per table, n ≥ 30.
- **Write "100% accurate" after 30 checks** → an interval of 0–10% presented as zero →
  state the interval.
- **Count `[deleted]` as one author** → one giant "person" → exclude deleted authors.
- **Keep AutoModerator in the population** → a weekly sticky becomes the most prolific
  "person" and a dedup signal → bots out at export (the exporter prints the count).

## Process
- **Let subagents inherit the session model** → they hit that model's limit and die
  mid-task → pass `model: "sonnet"` explicitly.
- **Add scope a subagent suggested** → the owner did not agree, and the counts are
  biased by the addition → propose first, wait.
- **Give a reviewing agent write access to the live database** → a review becomes a
  rewrite → it writes to a copy.
- **Poll the usage limit overnight** → the laptop stays busy and the run starts late →
  compute the reset time; attempts at +2/+7/+17 minutes.
- **Leave the status in your head** → every "how is it going?" costs a query → keep the
  one-line status current.
- **Run the overnight loop without `caffeinate`** → the Mac sleeps, the window is lost.
- **Start the overnight loop from the agent's session** → the auto-mode classifier
  blocks long-lived background processes ("persistence"), or the process dies with the
  session → the agent prepares the command, the owner starts `window_loop.sh` in their
  own terminal, the agent reads the log in the morning.
- **Keep the research state only in the chat** → the next session cannot answer "where
  did we stop?" → runbook, history entry, backlog at the end of every session.
- **Share a review page that needs Claude to save** → the colleague's marks are lost →
  export JSON in the page, paste in chat.
- **Run two studies' archive steps at once on one machine** → they share one per-IP
  60-second budget: both scouts crawl at a request a minute and every pull reports
  dozens of pauses → one archive process at a time per machine; the second study waits.
- **Kill a script by name (`pkill -f subreddit_scout`)** → the other study's scout dies
  mid-run and its output "vanishes" with no error line → kill only the PID you started;
  never `pkill -f` a shared script name.
- **Fix a failure without writing it down** → the next study pays for it again →
  add the entry here, same format, in the same session.
