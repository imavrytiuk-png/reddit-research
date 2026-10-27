# Scouting subreddits

The skill proposes the communities; the owner only confirms or strikes. A wrong choice
here biases every number later, so the proposal must show its evidence.

## 1. Collect 15–30 candidates

- Web search: "subreddit for <behaviour>", "best subreddits <topic>", Reddit's own
  "related communities" lists on candidate pages.
- `scripts/subreddit_scout.py --prefix <word>` lists every subreddit whose name starts
  with a word (Arctic Shift's subreddit index), with sizes. Try 3–6 prefixes.
- `scripts/subreddit_scout.py --names a,b,c` measures a named list. `--require <regex>`
  keeps only prefix hits whose name or description matches, which cuts prefix noise
  ("vision" also returns Apple Vision Pro, "dream" returns sleep and football subs);
  a name given with `--names` is always measured.
- For behaviours that leave a link (a shared page on one domain, a tracker, a profile),
  the archive can find where links of one domain get posted, sitewide:
  `/posts/search?url=<prefix of 14+ chars>&aggregate=subreddit`. It is the only free
  sitewide probe; the archive refuses sitewide keyword search (HTTP 400).
- treg (optional, paid): Reddit search endpoints can search communities by keyword when
  a prefix is not enough.

## 2. Measure the community, then probe the behaviour

`subreddit_scout.py` prints, per subreddit: subscribers, exact posts and comments per
month from the archive's time series (last full month, 12-month total, peak and low
month, and a note when the monthly count jumps 5x between adjacent months: a coverage
step, where the 12-month total overstates the sub's steady level), language, NSFW flag,
type and the public description. Size and activity say
whether the community is alive; they say nothing about how many of its people do the
behaviour. That is what the probe measures:

```
python3 scripts/subreddit_scout.py --names a,b,c --prefix word \
    --probe "seed phrase 1|seed phrase 2|…" --probe-days 14 --probe-names a,b,c \
    --probe-save out/probe_posts.ndjson --out out/scout.json
```
`--probe-save` keeps every probed post as `{id, subreddit, created_utc, title, text, on_topic}`
(`text` is the first 1,500 characters of the body): read 5–8 on-topic posts from every
probed sub for the first-hour note (SKILL.md, "The first hour") before any pull. The
scout also prints the hits of each `--probe` argument across the probed subs, and a note
when a seed phrase matches a sub's own name: that sub's share measures the name, not the
behaviour (a spreadsheet sub for "excel", a product sub for its product's name). Read the
on-topic titles of the top subs before trusting any ranking. The table's `days` column is
the window the probe actually covered; a giant sub reaches `--probe-pages` in a day or
two and its authors/year is marked `~` (rough).

Scout in two passes: first measure (names and prefixes, no probe) and shortlist by eye,
then probe the shortlist with `--probe-names`. Without it `--probe-top N` probes the N
most active measured candidates, which in a prefix search are the noisiest giants. For
giant subs (thousands of posts a month) add `--probe-pages 3`: 300 posts cover two or
three days, enough to rank. The probe pulls the last 14 days of posts and matches the
seed phrases locally, excluding deleted authors and bots. Per sub: posts in the window,
on-topic posts, distinct on-topic authors, **projected on-topic authors per year**
(authors × 365 / days), 5 on-topic titles and 10 random titles. The description says
what the sub wants to be, the random titles say what it is, the on-topic titles say how
its people talk about the behaviour (which also feeds the seed phrases back).

Two readings of the probe:
- A low share with many authors (2% of a 37k-posts-a-year sub ≈ 800 people) is a good
  sub: the prefilter in phase 3 keeps only the slice, and downloading the rest is free.
- A low share with the random titles all about the behaviour means the sub's own
  vocabulary differs from the seed phrases (the sub's own tags and abbreviations): mark
  it **whole-sub**, take every post, and add its words to the seed phrases. A whole-sub
  family needs no recall control; the phrase-defined ones do (`exploring.md` §5).
- **A product's or an answer's own sub is whole-sub by default** (r/<app>, r/<brand>):
  its posts rarely name the product, so a phrase prefilter there catches
  a third of them at best; in one test taking three product subs whole lifted the measured
  recall from 26% to 48%. Keep its answer-sub bias in mind: it over-represents the
  product's own users and their support pains.

The projection is rough (14 days, posts only; comments multiply the people). Use it to
rank and cut, not to plan. Probe the week before a seasonal peak if the topic has one.

A sub that is "not in the index" (private, banned, renamed, or simply not archived) is
a gap: record it in the claims note ("r/X, N subscribers, not reachable"); the optional
paid search can sample it, the archive cannot.

## 3. Selection rules

- **Cross-segment communities around the behaviour**, not around an answer. For "which
  tool do people pick": the job's community, not a product's sub; for "which service do
  people choose": the subs of people with the need, not each service's sub. Answer-subs
  pre-decide the counts.
- **Enough people doing the behaviour**: projected on-topic authors per year ≥ 100 from
  the probe, or a whole-sub verdict. Not "posts in the hundreds" and not the share:
  a tiny exactly-named sub (3 posts a month) fails this, a big adjacent sub with a 2%
  slice passes it.
- **One family per sub, stated**: say which behaviour family a sub serves. A family
  with no sub above the threshold has no home on Reddit; say so in the proposal
  instead of stretching a neighbour. Locating it needs a different tool: a paid
  sitewide search sweep sorted by new over the seed phrases, or a monthly dump from
  the archive's torrents (20+ GB, free) scanned locally for authors per sub.
- **Language**: English by default; the scout table shows each candidate's language.
  A strong candidate in another language is a question in the proposal, not a silent
  drop: keeping it means the intent phrases, the prompt examples and the entity aliases
  are written in that language too, otherwise its entities are lost silently.
- **Public, not NSFW** unless the topic requires it.
- **Mixed membership on purpose**: pick subs that attract different segments (students,
  professionals, families…). Write down that the segment mix will mirror this choice,
  so segments are never compared by size later, and that every share describes people
  who post in these subs, not the market.
- **No duplicates of one audience**: two subs with the same crowd double one segment's
  weight without adding people.

## 4. The proposal

A table: subreddit, subscribers, posts per month, on-topic authors per year (or
whole-sub), family, who writes there (from the titles), why it is in; and a second table
of rejected candidates with the reason (answer-sub, too few on-topic people, dead,
non-English, same crowd, NSFW, not in the index). 5–10 rows in the first table. One
paragraph on the expected segment mix and one line per family with no home. Then wait
for the go.
