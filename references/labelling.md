# Labelling with a model

## Prompt design (`templates/label_prompt_choice.md`, `templates/label_prompt_pains.md`)

Two templates: options with a status (considering / using / past / rejected, renamed to
the domain's words when needed) and practices with a closed list of pains (lists without
a status). Each comes with its schema. The prompt is a codebook, not a request. Its
parts, in order:

1. **Role and inputs**: what the texts are, what the project asks, which fields arrive
   (title, text or "(title-only, body removed or empty)", HINTS).
2. **Only the text.** Never infer from the subreddit, the username or other comments.
   When the text is silent, output null / "unknown" / []. **An empty answer is correct.**
   Models fill fields to be helpful; this sentence does the most work in the prompt.
3. **Evidence rule.** Every fact carries `evidence`: an exact verbatim substring (≤200
   chars) of this item's own title or text, copied not paraphrased; up to 3 fragments
   joined by " ... "; never skip words inside a fragment; if you cannot quote it, do not
   output it. For comments: quotes come from the comment body only; the post title is
   context for resolving "there / here / it" and gets a `from_post: true` flag.
4. **Output shape**: exact JSON, one object per item in the given order, ids echoed.
   For comments, include a field only when it has a value (about a third cheaper).
5. **Definitions with boundaries**: every label with its definition and the cases that
   look like it but are not, including every case the owner and reviewers argued about.
6. **Do-not-output list**: other people's cases, comparisons and price references, the
   domain's false friends (a brand named as a shop, a method named only in a quote).
7. **Numbered examples**, 15–25, one sentence of input and the expected labels, half of
   them negatives and boundary cases (title-only, promo vs personal story, someone
   else's case, short or finished cases, a pain that is not about the practice).

Versioning: first line `prompt_version: <topic>-vX.Y`; every change bumps it; the runner
records it per item; superseded prompts stay in git.

## Headless `claude -p` (`scripts/claude_batch.py`)

```
claude -p --model <model> --output-format json \
  --system-prompt "<prompt file minus the version line>" \
  --tools "" --setting-sources "" --strict-mcp-config --mcp-config '{"mcpServers":{}}' \
  --disable-slash-commands --no-session-persistence --max-turns 4 \
  --json-schema '<schema>' [--effort low] "<batch JSON>"
```
with `CLAUDECODE` removed from the environment and `MAX_THINKING_TOKENS=0` for the
small model.

- The default invocation loads the interactive system prompt, tool definitions and MCP
  config, tens of thousands of tokens per call; the minimal flags cut that to a few
  hundred.
- Instructions go in `--system-prompt`, byte-identical on every call, so they are cached;
  the user turn carries only the batch, and of each item only `--send-keys` (id, title,
  text, hints, post_title): subreddit, author_hash, slice and weight never reach the model,
  so "never infer from the subreddit" holds by construction.
- Model names change; `--model` must be a name the CLI accepts today. The names in these
  pages are examples.
- **Batches by character budget** (`--char-budget`, default 12,000; an item over
  `--long-threshold` goes alone), order shuffled inside a batch against position bias.
- **Id-set check**: returned ids must equal batch ids; mismatch or parse failure → one
  retry with a reminder → split in halves recursively → single items marked failed with
  the reason.
- **Raw first**: the full CLI output (usage, cost, wall time, structured output) goes to
  `raw_llm/<date>/<tag>_batch_<n>.ndjson` before parsing. A validator change never
  needs new model calls.
- **Model tag** per run: results of two prompts or models on the same items coexist
  and resume independently. Never mix tags in one aggregation.
- **Limit detection** only on responses without model output (item text quotes phrases
  like "hit your limit"). Then stop cleanly, exit 3, in-flight batches persist; re-run
  the same command to resume. A weekly-limit message means stop everything.
- `--max-minutes` ends the run inside the window the owner planned. `--workers 8` is a
  reasonable default; measure items per window at the checkpoint.

## Validator (`scripts/evidence_check.py`)

Runs after every batch, no model involved:

- `evidence` ≤200 chars, normalised (NFKC, nbsp, curly quotes, markdown chars, case,
  whitespace, contractions) substring of the normalised source; or 2–3 fragments, each a
  substring, any order.
- Fails → the fact is dropped: a list entry disappears, a scalar field and its evidence
  become null. Rejects logged with (item, field, value, evidence, reason).
- `--name-keys` + `--aliases`: the entity's name or an alias must appear in the quote or
  the title, as a substring after normalisation: no stemming, so "cash envelopes" does not
  match "cash envelope" and "bank app" does not match "my bank's app". Build the alias
  table from the entity dictionary (canonical → aliases, demonyms, codes, plurals) and
  grow it from the rejects file after the pilot. Guard against superstrings ("America"
  inside "South America") and masked URLs when the entities are places. Practices named
  by paraphrase ("a calculator" for "the basic one from a drawer") are rejected by
  design: the prompt must ask for the text's own words.
- Keep traps in the tests: fabricated entities that must all be rejected, and a
  regression file of real rejects labelled right/wrong. Measure the false-reject rate on
  fresh items, never on the cases the validator was tuned on.

## Choosing the model

Pilot two candidates on the same 30–50 items, grade every field by hand against the
source, and count fabrications first, misses second; `scripts/compare_runs.py` lists
where the runs differ, per field and per slice. A difference of two or three items in
50 is inside the noise of one run against itself: choose on the error types (invented
names, own vs not-own) and on cost, never on a two-point agreement gap. Expect the
smallest model to fail on fabrication and on "own case vs someone else's" and the
mid-size model at low effort to be the floor; the price difference rarely pays for a
relabel. Stronger models are for reviewing, not labelling.

## API or subscription

- **API**: cost per item from the pilot × items = the figure the owner approves.
- **Subscription**: no dollars; runs consume 5-hour windows and a weekly cap. Measure
  items per window at the checkpoint, plan the calendar, and let the owner steer by
  usage percent ("fill this window to 70%, then stop"). `scripts/window_loop.sh` (macOS)
  restarts a run 2/7/17 minutes after each reset and keeps the machine awake; `touch
  STOP` halts. The owner starts it in their own terminal with the command the agent
  prepared: a long-lived background process started by the agent is blocked in some
  modes and dies with the session. Compute the reset time, do not poll.

## Run discipline

1. Pilot (30–50) → validation on fresh items (50–100) → the recall control labelled and
   `recall_check.py` read → **checkpoint 500** → owner reads cost per item, items per
   window, the reject table and 20 random results → full run.
2. Posts first. Comments only after the owner has seen the posts matrix and decided
   whether comments change the answer.
3. Progress line on request: `done N / pending M · this window K batches · usage ~X% ·
   ETA`. Usage per batch is in the raw files
   (`jq '{batch, cost: .outer.total_cost_usd, usage: .outer.usage}'`).
4. Never start a new window-consuming run without a go.
