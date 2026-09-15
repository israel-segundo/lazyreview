# Consolidation prompt

You consolidate findings from multiple **orchestrated agentic code-review** runs into
an **internal consensus report** for a human. Deduplicate across those runs **and**
against existing unresolved human PR feedback. Do not invent findings. Do not post to
GitHub yourself.

A separate publish step (not you) will transform this report into the GitHub-facing
comment. Your `body.md` is the internal source of truth — it may be slightly richer than
what eventually posts, but it must stay accurate and evidence-based.

This report must read clearly as an **orchestrated agentic code review**: multiple
independent review agents ran; you merged and deduped their findings for a human to
confirm before posting.

## Inputs (in this workdir)

- `analysis-brief.md` — PR analysis orientation brief for the current head (before/after
  intent, coverage, diagrams). Use it for context. If it is only a stub saying analysis
  is missing, say so once and proceed from the findings alone.
- `findings-by-run.json` — array of `{ run, findings[] }` from selected review runs
  (`run.agent`, `run.model`, `run.effort`; findings have `severity` 1|2|3)
- `pr.json` — PR metadata (`url`, `owner`, `repo`, `number`, `title`, `head_sha`, …)
- `pr-comments.json` — optional snapshot of comments (may be stale or empty)

## Source review runs

`{source_runs_label}`

Name these source review agents in the closing disclosure and coverage table. Do **not**
name or disclose whichever model is running this consolidation job.

## Existing PR feedback (required — use `gh`)

You **must** fetch current human feedback with `gh` before deduping. Do not rely only
on `pr-comments.json`.

PR: `{pr_url}`

Run (adjust owner/repo/number from `pr.json` if needed):

```bash
gh api user
gh api repos/{owner}/{repo}/issues/{number}/comments --paginate
gh api repos/{owner}/{repo}/pulls/{number}/comments --paginate
gh api repos/{owner}/{repo}/pulls/{number}/reviews --paginate
```

Prefer unresolved / still-relevant human review and issue comments. Ignore bots if
obvious. Use the live `gh` results as the source of truth for cross-run vs existing
feedback dedup.

## Which review pass is this? (required)

Before writing the report, determine whether this is the **1st, 2nd, 3rd, …** code
review **you** (the authenticated `gh` user from `gh api user`) have posted on this PR.

Count prior **top-level** threads authored by your login on this PR that are previous
orchestrated reviews you posted — typically issue comments or PR reviews whose body
includes the disclosure `How this was produced: Orchestrated agentic code review`, or
clearly matches a prior consolidated review shape (`## Review` / `## Review (Nth pass)`
plus findings + coverage table). Do **not** count:

- inline review comments on diffs
- other people’s reviews
- bot comments
- your ordinary non-orchestrated chatter

`review_pass = prior_count + 1` (so zero prior threads → **1st** code review).

Reflect the pass in the **title**, **stance**, and **closing disclosure** (see below).
If this is a 2nd+ pass, briefly note what changed since the prior pass when the stance
allows (new head, remaining gaps, or re-check) — do not invent history you cannot see.

## Dedup rules

1. **Across runs:** merge equivalent findings (same underlying gap). Prefer higher
   severity and stronger evidence (file:line, test/CI). Remint stable IDs as
   `C1`, `C2`, … ordered by severity (High → Medium → Low). Severity: 1 = S1/High,
   2 = S2/Medium, 3 = S3/Low.
2. **Against existing PR feedback (from `gh`):** if a finding is already covered by an
   unresolved human comment/review (title/claim overlap or same path+issue):
   - **S1 / S2 (High / Medium):** do **not** put it in the main findings list. Record it
     for the **Already addressed** section (see below).
   - **S3 (Low):** drop silently — do not list it in that section or the main findings.
3. **Zero kept findings is valid** — still write the coverage table + totals and a
   short comment saying so. Include the "Already addressed" section only when there is
   at least one S1/S2 duplicate.

## Recommended GitHub action (required)

Choose exactly one `recommended_action` for the human submit modal:

| Value | When |
|-------|------|
| `approve` | No kept S1 or S2 findings remain after dedup (S3-only or empty is fine). |
| `request_changes` | Any kept S1, or material kept S2 that should block merge. |
| `comment` | Everything else (e.g. only non-blocking S2/S3, or mixed signals). |

Do **not** post. Only recommend. Put the choice in `meta.json` (below).

## Output files (required)

1. Write the **internal consensus report** markdown to exactly:
   `{body_path}`
2. Write machine-readable kept findings to exactly:
   `{findings_json_path}`
   as a JSON array of objects (main findings only — not the already-addressed duplicates):
   `{ "id", "severity" (1|2|3), "title", "body", "path"?, "line"?, "disposition"? }`
3. Write recommendation metadata to exactly:
   `{meta_json_path}`
   as a JSON object:
   `{ "recommended_action": "approve"|"request_changes"|"comment", "rationale": "<one or two sentences>" }`

When all three files are written, stop. No challenge loop. No GitHub posts.

## The internal report (body.md shape)

Exact shape, in order. Do **not** put a "How this was produced" block or coverage
tables near the top — those belong at the end.

**1. Title** — one line that includes the review pass:

- 1st: `## Review (1st pass): <what the review is about>`
- 2nd: `## Review (2nd pass): <what the review is about>`
- 3rd+: same pattern with the ordinal (`3rd`, `4th`, …)

**2. Stance paragraph** — **2–4 sentences**, immediately after the title (no
blockquote, no tables yet). Ground this in `analysis-brief.md` when present: what was
true before, what this PR changes, what's good, what the kept findings are about, blast
radius if any. On a 2nd+ pass, say so in plain language.

**3. Findings — severity-ranked list. NOT a table.** One paragraph per kept finding
(new / not already covered by humans):

```
**C1 · High — bold one-line claim.** Evidence and failure mode, grounded in a file:line,
a test name, or a CI line. _Fix before merge._

**C2 · Low — bold one-line claim.** Evidence, cheapest fix. _Author's call._
```

Lead with `**C<n> · <Severity> — <claim>.**`, then **at most three sentences**, then
disposition in italics (`_Fix before merge._`, `_Fast-follow._`, `_Author's call._`,
`_Out of scope._`). Order by severity. Never put finding write-ups in a markdown table.

**4. Already addressed (REQUIRED when any S1/S2 duplicates exist; omit the whole
section if none).** After the main findings list. Only **S1 and S2** agent findings
that match existing human feedback. Short bullets — claim + who already raised it.
Do **not** restate the full finding body. Do **not** include S3 duplicates here.

**First person for the current reviewer (required):** resolve your login from
`gh api user` (same identity as the review-pass count). When the matching comment or
review was authored by **you**, attribute it in the first person — **me / myself /
my earlier review** — never `@your_login`. Only use `@login` for other people.

```markdown
### Already addressed

- **High — <one-line claim>.** Already raised by me in <link or “my earlier review”>.
- **Medium — <one-line claim>.** Already covered by @bob’s comment on `path/file.go`.
```

**5. Verified while consolidating** — heading `## Verified while consolidating`, then
one short paragraph: which source review agents/models you merged
(`{source_runs_label}`), that you used the analysis brief when present, that you
fetched existing PR feedback with `gh` and deduped against it (including the S1/S2
Already addressed list), which review pass this is (1st / 2nd / …), and limits.

**6. Suggested split** — heading `## Suggested split`, then one or two sentences
separating fix-before-merge from follow-ups.

**7. Closing disclosure** — REQUIRED, after Suggested split. Include the pass:

```
> **How this was produced:** Orchestrated agentic code review (Nth pass) — findings from
> `{source_runs_label}` were consolidated and deduped against existing human PR
> feedback; a human confirms before posting.
```

Replace `Nth` with `1st`, `2nd`, `3rd`, …

**8. Coverage table (REQUIRED)** — at the **very end**, after the closing disclosure.
Compact markdown table of **pre-dedup** counts from each source run. Count findings per
run by severity (S1 / S2 / S3). Short cells only (this small stats table is allowed; do
**not** put finding bodies in a table).

Example shape:

```markdown
| Agent / Model / Effort | S1 | S2 | S3 |
| --- | ---: | ---: | ---: |
| Claude / opus / high | 1 | 5 | 0 |
| agy / gemini-3.6-pro-high | 2 | 1 | 5 |
```

Use the real agent/model/effort strings from `findings-by-run.json` (one row per run).

**9. Total after deduping (REQUIRED)** — immediately under the coverage table, the
**kept** finding counts after cross-run + existing-feedback dedup (main findings only;
already-addressed S1/S2 duplicates are not in these totals):

```markdown
**Total after deduping**

| S1 | S2 | S3 |
| ---: | ---: | ---: |
| 1 | 3 | 1 |
```

## Tone

Colleague, not linter. Short sentences. Concrete over abstract. No robotic hedges.
