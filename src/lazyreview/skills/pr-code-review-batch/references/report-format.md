# Report formats

Two outputs: the **local HTML table** you show the user while reviewing, and the **posted
PR comment**, which happens only on explicit request. Same findings, same IDs, same
severities, so the user recognises what they approved — but **not the same shape**: the
HTML is a four-column table because you control its CSS, and the posted comment is a
**list, not a table**, because GitHub's rendering can't be controlled at all.

`$WORK/findings.md` stays the source of truth; the HTML is a view of it.

## Initial findings report (Step 7 — mandatory)

After Steps 4–6 (verify, baseline, blast-radius scan), **always** render and open the local
HTML report before the challenge loop. This is the first thing the user sees — not a markdown
findings table in chat.

**Do not wait for "a few findings".** Zero findings is still a report: say so in the table and
in "raised by others". An empty section that reads "nothing others found that I didn't" is
evidence you checked.

### Write and open

1. Finish `$WORK/findings.md` with stable IDs (`F1`, `F2`, …), severities, and dispositions.
2. Render `$WORK/findings-report.html` from `references/findings-report-template.html` (copy
   the template into `$WORK` and fill placeholders) or write equivalent HTML that meets the
   table spec below.
3. **Open it** — attempt every opener the host has, then fall back to printing the path:

```sh
REPORT="$WORK/findings-report.html"
# …write $REPORT…
open "$REPORT" 2>/dev/null \
  || xdg-open "$REPORT" 2>/dev/null \
  || wslview "$REPORT" 2>/dev/null \
  || echo "Open it manually: $REPORT"
```

4. In chat: **short pointer only** — PR link, central claim in one line, report path if open
   failed, and "your turn to challenge." **Do not duplicate the findings table in markdown**
   when the HTML opened successfully; the browser is the table.

Refresh the same file as findings change during the challenge loop (Step 8).

### Required sections (initial pass)

| Section | Content |
|---------|---------|
| Header | PR #, title, URL, branches, model from `$WORK/review-model.txt`, date |
| Central claim | Type (Coverage, Scope, …) + one sentence |
| Verified | What you ran/read; CI red/green; limits |
| Existing reviews | Who said what; note the Step 1 scan may be stale |
| Findings table | Four columns — see below |
| Raised by others | HTML-only blind spots; include even when empty |

After Step 10 dedup, re-render with the fifth **Vs. existing** column and muted duplicate
rows (see below).

## Model disclosure — required on every GitHub post

**Every** message the skill posts to GitHub must name the exact model and version so review
quality can be compared across models. That means:

- Full review comments (`gh pr comment`, review `body` with findings)
- Approvals (`gh pr review --approve`)
- Request-changes (`gh pr review --request-changes`)
- Any `gh api …/pulls/$PR/reviews` payload, including inline-only reviews

**Record the model once in Step 0** to `$WORK/review-model.txt` (one line, exact name the
host reports — e.g. `Claude Opus 5`, `Composer (Cursor)`, `GPT-5.1-codex`). Re-read that
file when drafting any post; do not paraphrase or abbreviate.

**Every posted body ends with the machine-readable tag and the human blockquote** (in this
order — tag last so it is easy to grep from the end of the comment):

```
> **How this was produced:** LLM-aided but human-driven — `<model + version>` `<what you
> actually checked on this post>`; a human confirmed before posting.

<!-- pr-code-review-model: <exact model + version, same string as review-model.txt> -->
```

- **Full review** (`$WORK/review.md`): put the tag + blockquote **after** the Suggested
  split section (Step 10 shape below). The blockquote's middle clause names the evidence
  (worktree checks, CI, assertions read, etc.).
- **Approval or request-changes** (`$WORK/approve.md`, `$WORK/request-changes.md`): put
  the tag + blockquote **at the end** of the decision text, even when there is no findings
  list. The middle clause can be shorter — e.g. "reviewed the branch and challenged the
  findings in session".
- **Inline comment bodies** do not repeat the tag (one tag on the review `body` is enough).

If you do not know the model, ask the user before posting — never guess, never write "an AI".
If the user supplies a label for measurement (e.g. "log this as `composer-2.5`"), use their
string in the HTML comment and the host's display name in the blockquote.

## The local HTML findings table

Write a self-contained `.html` to `$WORK` and open it with whatever the host has
(`xdg-open`, `open`, `wslview`) — or print the path for the user to open. Keep it
**local**: never a hosted render, an uploaded page, or a published artifact. Refresh it as
findings change.

- Columns: **# · Finding · Severity · Disposition**. Four is fine *here* because you set
  the widths. The posted comment gets no table at all (see below) — don't copy this shape
  into it.
- Lead the Finding cell with the bold one-line claim; detail underneath.
- **Theme-aware** — light and dark via `prefers-color-scheme`.
- Color-coded severity chips (red / amber / grey is enough).
- **Make cells wrap.** This bites every time. Use `table-layout: fixed` with a
  `<colgroup>` (narrow # and Severity, wide Finding, ~25% Disposition) and
  `overflow-wrap: anywhere` on cells and `<code>`. Never leave `white-space: nowrap` on a
  column holding a long sentence.
- Multi-item dispositions render as a real `<ul>`, not a run-on inline list.
- Inline all CSS. No CDN, no external fonts — it must open offline.
- Show the model from `$WORK/review-model.txt` in the page meta/subtitle so the local
  report matches what will be posted.

After Step 11 dedup, add a **fifth column, "Vs. existing"** — New / Partly new
/ Correction / Folded in / Duplicate — and keep the dropped rows in the table, muted, with
one line saying whose finding they duplicate. A dropped row is evidence you checked;
deleting it looks like you never found it. A small count strip above the table (`5 new · 1
correction · 1 folded in · 1 duplicate`) is worth the twenty lines of CSS.

### "Raised by others, missed by me" — HTML only, and required

Below the findings table, list what other reviewers caught that you didn't, each with a
one-line note on what it is and — where you can tell — why you missed it. Verify each one
before repeating it; another reviewer's claim is not evidence, and confirming or refuting it
is part of the value.

**This section never appears in the posted comment.** On the PR it reads as noise to
everyone except you: the author already has those points, and restating them buries your
actual findings. In the local report it's the most useful thing on the page — it's the only
place the review's blind spots are visible, and it's the raw material for Step 10's skill
improvements. Include it even when it's empty; "nothing others found that I didn't" is
itself a result, and an absent section reads as a skipped check.

## The posted PR comment

Exact shape, in order:

**1. Title** — one line: `## Review: <what the review is about>`.

**2. Disclosure blockquote (opening)** — REQUIRED, immediately after the title. **One
sentence.** Name the **exact model and version** from `$WORK/review-model.txt`. Say what it
really checked; don't claim evidence you didn't gather. This opening blockquote is the
*stance* disclosure; the **closing** tag + blockquote at the end (see [Model disclosure](#model-disclosure--required-on-every-github-post)) is still required for grep.

> **How this was produced:** LLM-aided but human-driven — I had Claude Opus 5 analyze the
> branch and check its claims against the repo variables, tofu #238, and the real
> `release-deploy` run logs, then I challenged what it found and confirmed every point
> below myself.

**3. Stance paragraph** — **2–4 sentences, no more.** What's genuinely good about the PR,
what the findings are about, and which (if any) touch its purpose or blast radius.

**4. Findings — a severity-ranked list. NOT a table.** One paragraph per finding, stable
IDs, severity and disposition folded into the text:

```
**F1 · High — bold one-line claim.** Then the evidence and failure mode, grounded in a file:line, a test name, or a CI line. _Fix before merge._

**F2 · Low — bold one-line claim.** Evidence, failure mode, cheapest fix. _Author's call._
```

**Never put findings in a markdown table on GitHub — this is the formatting rule that
always bites, and every "but a table is tidier" instinct is wrong here.** GitHub sizes
columns to content and then breaks the narrower ones *mid-word* to fit: a `#` column
renders `F1` stacked as "F / 1", a severity column turns `Medium` into "Me / di / um", and
dropping to two columns just moves the damage to whichever column is now narrowest. Two
characters is not too short to be mangled. There is no escape — GitHub strips style
attributes, `<colgroup>` and `nowrap` are sanitised away, and `&nbsp;` doesn't prevent a
mid-word break. **A single column of prose is the only shape that survives, and that's a
list.** It also reads more like a person wrote it, which is the point.

Lead each finding with `**F<n> · <Severity> — <one-sentence claim>.**`, then **at most three
sentences** of detail, then the disposition in italics (`_Fix before merge._`,
`_Fast-follow._`, `_Author's call._`, `_Out of scope._`). If a finding needs more than that,
it needs a diagram or a follow-up ticket, not a longer paragraph. Order the list by severity
so it still scans top to bottom.

The local HTML table is where a real table belongs — you control its CSS there, and the
user reads it in a browser, not in a 700px PR column.

**5. Verified while reviewing** — **one short paragraph**, or a tight bullet list if it
reads better. What you actually checked: which checks you ran and their result, which
assertions you read, whether you confirmed a test fails without the change, which CI
artifacts you read. **End with the limits** — what you couldn't run or sample. This is what
makes the review credible, so keep it factual and skip the adjectives.

**6. Suggested split** — one or two sentences separating cheap fix-before-merge items from
genuine follow-ups. Be explicit that nothing blocks merge unless it truly does.

**7. Closing model disclosure** — REQUIRED. The machine-readable HTML comment + closing
blockquote from [Model disclosure](#model-disclosure--required-on-every-github-post).

## Approval and request-changes bodies

Shorter than a full review, but the **closing model disclosure is not optional**.

Shape:

1. Decision in plain language (approve / request changes / nudge).
2. Findings list if the user asked to include them (same `**F<n> · …**` list shape, not a table).
3. **Closing model disclosure** (tag + blockquote) — always last.

Example approval tail:

```
…nothing blocks merge from my side.

> **How this was produced:** LLM-aided but human-driven — Composer (Cursor) reviewed the
> branch, ran the hook unit tests in a worktree, and challenged the findings in session;
> a human confirmed before approving.

<!-- pr-code-review-model: Composer (Cursor) -->
```

Post it with a file, never an inline heredoc:

```sh
gh pr comment "$PR" --body-file "$WORK/review.md"
```

## Inline comments

Add inline comments when a finding is **about a specific changed line** and the reader
needs to look at that line to get it. They sit alongside the summary comment, not instead
of it — the table is still the full picture.

**Anchor a finding inline when:** it points at a line in the diff, and pointing is the
fastest way to explain it — a guard on the wrong branch, a renamed key its caller still
uses, an assertion that proves nothing, a hardcoded value.

**Keep it in the table only when:** the finding is about something *absent* (a missing
test, an unset variable, no protection rule), spans several files, or lives outside the
diff. There's no honest line to hang it on, and a stretched anchor confuses more than it
helps.

Rules that keep this from turning into a lint dump:

- **A few, not one per row.** If every finding got an anchor, you're commenting, not
  reviewing.
- **1–3 sentences each.** Lead with the finding ID so it ties back: `**F2** — this reads
  the old key; `resolveTenant` still writes the new one.` Detail lives in the table.
- **Never duplicate the table cell verbatim.** Inline says *what's wrong here*; the table
  says why it matters and what to do.
- **Suggest a fix as a suggestion block** when it's a genuine one-liner, so the author can
  commit it:

  ~~~
  ```suggestion
  const key = resolveTenant(ctx).id;
  ```
  ~~~

**Mechanics.** Post the summary and its inline comments as **one review** — a JSON payload
through `gh api`, so the author gets a single notification instead of a drip:

```sh
# $WORK/review.json — write it with a file tool, never an inline heredoc
# {
#   "event": "COMMENT",                      # or APPROVE / REQUEST_CHANGES
#   "body":  "<the summary comment>",
#   "comments": [
#     {"path": "src/tenant.ts", "line": 42, "side": "RIGHT", "body": "**F2** — ..."},
#     {"path": "ci/deploy.yml", "start_line": 8, "line": 12, "side": "RIGHT", "body": "..."}
#   ]
# }
gh api "repos/{owner}/{repo}/pulls/$PR/reviews" --input "$WORK/review.json"
```

Gotchas, all of which produce a 422 and lose the whole review:

- `line` must be a line **in the diff**. `side: RIGHT` for added or context lines, `LEFT`
  for removed ones. A line the diff doesn't include can't be anchored — move it to the
  table.
- Ranges need both `start_line` and `line` (same side). Whole-file comments need
  `"subject_type": "file"` and no line.
- The whole review is rejected atomically, so on a 422 fix the offending anchor or drop
  it — don't retry the same payload.

Same confirmation rule as everything else: **show the user the inline comments** — path,
line, and text — and get an explicit yes before posting. They're more visible than a
summary comment and land in the author's inbox line by line.

## Tone

**The reader is a colleague, not a linter.** Relaxed, plain, first-person. Everyday words.
Short sentences. Say what you mean without stacking hedges.

Length is part of the tone. Nobody reads the third paragraph of a review comment:

- Opening disclosure (full reviews): **one sentence**.
- Closing model tag + blockquote (all GitHub posts): **one sentence** in the blockquote;
  tag is one line, exact model string from `$WORK/review-model.txt`.
- Stance: **2–4 sentences**.
- Each findings cell: **one bold claim + up to three sentences**.
- Each inline comment: **1–3 sentences**.
- Verified: **one paragraph** (or a tight list), limits at the end.

If a section is longer than that, you're explaining rather than reporting — cut it, or move
the depth into a follow-up ticket. Prefer the concrete over the abstract: "someone deletes
the variable, cuts a release, and sees a green check" beats "insufficient enforcement of
the configuration invariant". If a paragraph reads robotic, rewrite it before showing the
user — and certainly before posting it.
