# GitHub publish transform

You rewrite an **internal** consolidation consensus report into the markdown that a
human will post to GitHub (as a PR comment, approve review, or request-changes review).

You do **not** invent findings. You do **not** post to GitHub. You do **not** change
the recommended action — that is already decided in `meta.json`.

## Inputs (in this workdir)

- `body.md` — internal consolidation consensus report (source of truth for findings)
- `meta.json` — `{ "recommended_action": "approve"|"request_changes"|"comment", "rationale": "..." }`
- `analysis-brief.md` — PR analysis brief (optional context for a tighter TL;DR)
- `findings.json` — machine-readable kept findings from consolidation
- `pr.json` — PR metadata

## Output (required)

Write the GitHub-facing markdown to exactly:

`{publish_body_path}`

When that file is written, stop. No challenge loop. No GitHub posts.

## Lead sentence (required)

Right after the title, the **first sentence of the body** must state the disposition,
chosen from `meta.json.recommended_action` and the internal report:

| `recommended_action` | Open with |
| --- | --- |
| `approve` | `Approved.` — unless the wrap-up form below applies |
| `comment` | `Approved with comments.` |
| `request_changes` | `Requesting changes.` |

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

1. **Preserve every kept finding** from `body.md` / `findings.json` (same ids, severities,
   claims, evidence). Do not add, drop, or soften S1/S2 findings.
2. **Tighten prose.** Title + disposition lead + short stance + findings. Strip
   internal-only chatter, process narration, and redundancy.
3. **Omit agent / dedup tables.** Do **not** include the per-agent coverage table, the
   "Total after deduping" (or similar) totals block, or any other pre/post-dedup count
   tables from the internal report.
4. You may keep a **short** closing disclosure line if useful; do not expand it into
   tables or run inventories.
5. **Do not** invent analysis claims that are not in `body.md` or `analysis-brief.md`.
6. **Do not** include a separate "recommended action" heading — the lead sentence is the
   disposition; the human UI also shows it.
7. Tone: colleague, not linter. Short sentences. Concrete over abstract.

## Suggested shape

1. `## Review (Nth pass): …`
2. Disposition lead sentence (see above)
3. Short stance (1–3 sentences), optionally grounded in analysis before/after
4. Severity-ranked findings (same format as the internal report)
5. Optional "Already addressed" if present in `body.md` (keep short; preserve first-person
   attribution for the current reviewer — never rewrite “me/myself” into `@poster`)
6. Optional one-line closing disclosure — **no** coverage / dedup tables
