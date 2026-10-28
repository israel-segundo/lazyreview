# GitHub publish transform

You rewrite an **internal** consolidation consensus report into the **short summary**
markdown a human will post as the top-level body of a GitHub pull-request review.

Findings that will post **on the PR diff** (`post-plan.json` → `inline_ids`) are sent as
**inline review comments** by the app — do **not** paste those full write-ups into the
summary. Everything in `summary_only_ids` (pathless findings **and** anchors off the
diff or on non-commentable lines) must appear in this summary body in full.

You do **not** invent findings. You do **not** post to GitHub. You do **not** change
the recommended action — that is already decided in `meta.json`.

## Inputs (in this workdir)

- `body.md` — internal consolidation consensus report (source of truth for findings)
- `meta.json` — `{ "recommended_action": "approve"|"request_changes"|"comment", "rationale": "..." }`
- `analysis-brief.md` — PR analysis brief (optional context for a tighter TL;DR)
- `findings.json` — machine-readable kept findings (`path` / `line` optional)
- `post-plan.json` — `{ "inline_ids", "summary_only_ids", "off_diff_ids", "bad_line_ids" }`
  (written by the app from the PR diff; **authoritative** for inline vs summary split)
- `pr.json` — PR metadata
- `publish-guidance.md` — optional human/challenge notes for tone and extra summary phrasing
  (honor when present and non-empty; never invent findings or change disposition)

## Output (required)

Write the GitHub-facing **summary** markdown to exactly:

`{publish_body_path}`

When that file is written, stop. No challenge loop. No GitHub posts.

## Lead sentence (required)

Right after the title, the **first sentence of the body** must state the disposition,
chosen from `meta.json.recommended_action` and the internal report:

| `recommended_action` | Open with |
| --- | --- |
| `approve` | `Approved.` — unless the wrap-up form below applies |
| `comment` | `Approved with comments.` (non-blocking notes only; UI will pre-select **Approve**) |
| `request_changes` | `Requesting changes.` |

`meta.json` should already follow the consolidator/verify rule of thumb: High /
conflicts / security / real blockers → `request_changes`; cosmetic / nits / docs /
no blockers → `approve` (or rare `comment` for approve-with-comments). You still do
**not** rewrite `recommended_action` here — only mirror it in the lead sentence.

**Approve + wrap up prior findings:** when `recommended_action` is `approve` **and**
`body.md` has a non-empty **Already addressed** section (S1/S2 items humans already
raised that still need finishing), do **not** use a bare `Approved.` Open with a short
sentence about wrapping those up.

**First person for the current reviewer (required):** the human posting this comment is
the authenticated reviewer. When prior findings were raised by **that same person**
(body says “me” / “myself” / “my earlier review”, or the @login is the poster), write
in the first person — never `@their_own_login`. Prefer:

> Approved, but wrap up my earlier findings before merging.

For other people, keep `@login`:

> Approved, but wrap up @alice’s findings before merging.

If several people appear, name them in one short sentence (use “my …” for yourself and
`@login` only for others). If that section is missing or empty, use `Approved.`

## Transform rules

1. **Split by post-plan.** Read `post-plan.json` when present (else treat pathless as
   summary-only and all anchored as inline):
   - `inline_ids` → **inline** (do not paste full bodies here).
   - `summary_only_ids` → **summary** (include full write-ups; includes off-diff anchors).
2. **Inline pointer.** When `inline_ids` is non-empty, add one short sentence after the
   stance naming **only those** IDs, e.g. `Details on the diff for C2.`
   Do **not** list ids from `off_diff_ids` as “on the diff.”
3. **Summary-only findings.** For every id in `summary_only_ids`, preserve id, severity,
   claim, and evidence (same format as the internal report). For off-diff anchors, cite
   `path:line` as evidence in the body text. Do not add or soften blockers.
4. **Tighten prose.** Title + disposition lead + short stance + optional pathless list +
   optional Already addressed. Strip process narration and redundancy.
5. **Omit agent / dedup tables.** Do **not** include the per-agent coverage table, the
   "Total after deduping" block, Verified/Merged sections, or Rejected-as-false-alarms
   dump (one line in the stance is enough if needed).
6. You may keep a **short** closing disclosure line; do not expand it into tables.
7. **Do not** invent analysis claims that are not in `body.md` or `analysis-brief.md`.
8. **Do not** include a separate "recommended action" heading — the lead sentence is the
   disposition.
9. Tone: colleague, not linter. Short sentences. Concrete over abstract.
10. **Publish guidance.** If `publish-guidance.md` exists and is non-empty, apply its tone
    and summary notes (softer/harder voice, mention a point, keep shorter). Still do not
    invent findings, change `recommended_action`, or contradict blockers in `findings.json`.

## Suggested shape

1. `## Review (Nth pass): …`
2. Disposition lead sentence (see above)
3. Short stance (1–3 sentences), optionally grounded in analysis before/after
4. If `inline_ids` non-empty: one line pointing at the diff (`Details on the diff for …`)
5. `summary_only_ids` findings (severity-ranked), if any — full text, not just ids
6. Optional "Already addressed" if present in `body.md` (keep short; preserve first-person
   attribution for the current reviewer — never rewrite “me/myself” into `@poster`)
7. Optional one-line closing disclosure — **no** coverage / dedup tables
