---
name: pr-code-review-batch
description: >-
  Unattended adversarial review of an open PR for batch / CI / lazyreview-style
  runners. Same claim-vs-evidence methodology as pr-code-review (learn repo rules,
  verify against checks and CI, blast-radius scan) but never waits for a human
  challenge loop, never posts, and never asks questions. Writes findings.md and an
  HTML findings report, then exits. Use when running headless agentic review, not
  when a human wants to spar in chat — use /pr-code-review for that.
license: Unlicense
metadata:
  version: "1.0.0"
  portability: >-
    Written to the Agent Skills open standard. Body uses shell (git/gh/jq) rather than
    host-specific tools. Sibling of pr-code-review with HITL steps removed.
when_to_use: >-
  Headless or batch PR review (lazyreview, CI agent jobs, overnight sweeps) where
  the runner will parse findings.md after the process exits. Not for interactive
  chat reviews — use /pr-code-review instead.
argument-hint: "[pr-number-or-url]"
allowed-tools:
  - Bash
  - Read
  - Grep
  - Glob
  - Edit
  - Write
---

# PR code review — batch (unattended)

Adversarial review **without** a human in the loop. Same stance as `/pr-code-review`:
separate what the PR *claims* from what the code and checks *do*. You run once,
write artifacts, and **exit**. You do not ask the user questions. You do not wait.
You do not post to GitHub.

### Batch contract (non-negotiable)

1. **No human prompts.** Never ask clarifying questions, never pause for a challenge
   loop, never say "your turn."
2. **No GitHub mutation.** Never `gh pr comment`, never approve / request-changes,
   never push, commit, or merge.
3. **Always write findings + HTML report**, even if zero findings.
4. **Exit when artifacts are written.** Do not offer follow-ups in chat.

### Output locations

Prefer caller-provided paths (lazyreview sets these). Otherwise create `$WORK`:

```sh
# If the host/env already set these, use them as-is:
#   WORK, FINDINGS_PATH, REPORT_PATH, REVIEW_MODEL_PATH
WORK="${WORK:-$(mktemp -d)}"
FINDINGS_PATH="${FINDINGS_PATH:-$WORK/findings.md}"
REPORT_PATH="${REPORT_PATH:-$WORK/findings-report.html}"
REVIEW_MODEL_PATH="${REVIEW_MODEL_PATH:-$WORK/review-model.txt}"
mkdir -p "$(dirname "$FINDINGS_PATH")" "$(dirname "$REPORT_PATH")"
```

Common lazyreview layout (when cwd is the PR clone):

```sh
# If .lazyreview/ exists in cwd, prefer it:
if [ -d .lazyreview ]; then
  WORK="${WORK:-$(pwd)/.lazyreview}"
  FINDINGS_PATH="${FINDINGS_PATH:-$WORK/findings.md}"
  REPORT_PATH="${REPORT_PATH:-$WORK/report.html}"
  REVIEW_MODEL_PATH="${REVIEW_MODEL_PATH:-$WORK/review-model.txt}"
fi
```

### Guardrails (same as interactive, minus HITL)

- Read a command before you run it; never apply/deploy/migrate/publish.
- Never touch the caller's main working tree beyond the designated workdir/clone.
- If checks need credentials or live services you lack, mark claims **unverified**
  and continue — do not ask the user.
- Honor documented repo prohibitions; fall back to CI evidence.

### Reference files (load when the step needs them)

- `references/repo-profile.md` — Step 2 checklist + build-tool table
- `references/safety-scan.md` — Step 6 dimensions
- `references/report-format.md` — findings.md + HTML table spec (ignore "open browser /
  your turn to challenge" guidance; batch only **writes** the report)
- `references/findings-report-template.html` — HTML starter

---

## Step 0 — Set up (no capability chat)

```sh
command -v git gh jq || true
git rev-parse --is-inside-work-tree || true
```

Write the **exact model name and version** to `$REVIEW_MODEL_PATH` (one line). If
unknown, write `unknown` — do not ask the user.

Degrade silently into unverified claims when a tool is missing; keep going.

## Step 1 — Resolve PR

`PR_REF` from the user argument / prompt (number, URL, or empty → current branch PR).

```sh
gh pr view $PR_REF --json number,title,body,url,headRefName,baseRefName,author,additions,deletions,changedFiles
PR=$(gh pr view $PR_REF --json number -q .number)
HEAD=$(gh pr view "$PR" --json headRefName -q .headRefName)
BASE=$(gh pr view "$PR" --json baseRefName -q .baseRefName)

git fetch origin "$HEAD" "$BASE" 2>/dev/null || true
git log --oneline "origin/$BASE..origin/$HEAD"
git diff "origin/$BASE...origin/$HEAD" --stat

gh pr view "$PR" --json reviews,comments > "$WORK/existing-reviews.json" 2>/dev/null || true
gh api --paginate "repos/{owner}/{repo}/pulls/$PR/comments" > "$WORK/inline-comments.json" 2>/dev/null || true
gh pr checks "$PR" > "$WORK/checks.txt" 2>/dev/null || true
```

Read the **full** diff (`git diff "origin/$BASE...origin/$HEAD"`). If no PR exists, write
a single High finding explaining that and still emit the report, then exit.

Keep a short list of points already raised by other reviewers (bots count) for dedup
notes inside findings (`already raised by …`).

## Step 2 — Repo profile

Write `$WORK/repo-profile.md` per `references/repo-profile.md`. Read `CLAUDE.md` /
`AGENTS.md` / `CONTRIBUTING.md` / PR template / CODEOWNERS / workflows. Prefer a
repo-local review skill if one exists under `.cursor/skills/` or `.claude/skills/`.

## Step 3 — Central claim

Name the PR's central claim (Scope / Safety / Compatibility / Evidence / Enforcement /
Coverage) in a few lines. Cross-check PR template checklist and linked issues when
cheap. Do not ask the user to confirm the claim — state your best reading and proceed.

## Step 4 — Verify claims

Code wiring + repo checks in a throwaway worktree + CI artifacts. Same bars as
interactive: a test that stays green with the change reverted is not coverage.

If you cannot run checks (no toolchain, needs secrets), record **unverified** and use
CI / code reading. Never block waiting for credentials.

## Step 5 — Baseline compliance

Host code-review / security-review commands if available; otherwise a quick pass on
auth, injection, null paths, resource cleanup. Fold real issues into findings.

## Step 6 — Blast-radius scan

Walk `references/safety-scan.md` dimensions that the diff can reach. Append to
`$FINDINGS_PATH` as you go:

```
## F<n> — [High|Medium|Low] short title
- what the gap is, the failure mode, and the cheapest fix
- ground it in a file:line, test name, CI/plan line, or doc rule
- if it overlaps existing reviews, note "already raised by <reviewer>"
```

Zero findings is valid — leave a short note at the top of `$FINDINGS_PATH` that the
scan completed with nothing to raise.

## Step 7 — Write findings report and exit

**Mandatory. This is the end of the run.**

1. Finalize `$FINDINGS_PATH` (stable IDs, severities, dispositions).
2. Render `$REPORT_PATH` from `references/findings-report-template.html` (or equivalent)
   per `references/report-format.md` table sections: header, central claim, verified,
   existing reviews (stale-scan note), findings table, raised-by-others (even if empty).
3. Optionally attempt to open the HTML for a local interactive host; **do not wait** on
   open succeeding. In true headless environments, skip open entirely.
4. Print one short line to stdout if useful: PR URL, finding counts by severity, paths to
   `$FINDINGS_PATH` and `$REPORT_PATH`.
5. **Stop.** No challenge loop. No "any questions?" No approve offer. No posting.

Severity calibration (blast radius × reversibility) matches interactive: High / Medium /
Low with fix-before-merge vs follow-up dispositions noted in the finding body.

---

## Related skills

| Skill | Role |
|-------|------|
| **pr-code-review** | Same methodology with human challenge loop and optional GitHub post |
| **create-plan** / **execute-plan** | Plan authorship and implementation; batch can verify plan vs diff |
| **create-pr** | Accepts batch `findings.md` as a review gate before ready flip |
| **pr-address-feedback** | Implements fixes after human review comments |
| **request-pr-review** | Slack announcement after PR is open for humans |

## Out of scope for batch (use `/pr-code-review` instead)

- Challenge loop / answering human pushback mid-review
- Posting comments, approvals, request-changes
- Offering to write a repo-local skill or edit this skill mid-run
- Any step that requires a human "yes"

If the caller later wants human sparring on the same findings, they load the HTML /
`findings.md` in an interactive session or consolidate via a review management tool
(e.g. lazyreview).

---

## Principles (batch)

- Claim vs evidence, not opinion vs taste.
- Learn the repo's documented rules before judging.
- Verify with the strongest evidence available; say when unverified.
- Never mutate the outside world; never post.
- Write the report even when empty; then exit.
